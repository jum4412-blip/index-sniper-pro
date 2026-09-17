"""Slow candle retrieval is separate from the position management loop."""
from pathlib import Path
import contextlib,fcntl,json,logging,logging.handlers,os,signal as signal_module,threading,time
from .api import Rest,credentials
from .core import *
from .engine import Live,Paper
from .store import Store
from . import strategy,risk

@contextlib.contextmanager
def lock(root,mode):
    with file_lock(Path(root)/'data'/f'{mode}.lock',mode):yield

@contextlib.contextmanager
def file_lock(path,label):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SafetyError(label+' already running') from None
        yield

def running(root,mode):
    try:
        with lock(root,mode):return False
    except SafetyError:return True

def logger(root,mode):
    log=logging.getLogger('trend.'+mode);log.setLevel(logging.INFO);log.propagate=False
    for h in list(log.handlers):h.close();log.removeHandler(h)
    path=Path(root)/'data';path.mkdir(parents=True,exist_ok=True)
    h=logging.handlers.RotatingFileHandler(path/(mode+'.log'),maxBytes=5_000_000,backupCount=5,encoding='utf-8')
    h.setFormatter(logging.Formatter('%(message)s'));log.addHandler(h)
    def emit(kind,data):
        text=json.dumps({'at':utc(now_ms()),'kind':kind,'data':data},ensure_ascii=False,allow_nan=False)
        log.info(text)
        # systemd captures stdout as well; never log credentials or raw API bodies.
        print(text,flush=True)
    return emit

class Frames:
    def __init__(self,c,root,log):
        self.c=c;self.root=Path(root);self.log=log;self.api=Rest()
        self.frames={};self.errors={};self.mutex=threading.Lock();self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,name='candles',daemon=True)
    def start(self):self.thread.start()
    def get(self,symbol):
        with self.mutex:return self.frames.get(symbol)
    def run(self):
        while not self.stop.is_set():
            for sym in SYMBOLS:
                if self.stop.is_set():return
                try:
                    d=self.api.candles(sym,'1D',max(200,self.c['daily_slow']*3+10))
                    h=self.api.candles(sym,'4H',max(100,self.c['entry_channel']+20,self.c['atr_period']+20,self.c['exit_channel']+20,self.c['initial_structure_bars']+20))
                    f=strategy.frame(sym,d,h,self.c,now_ms())
                    with self.mutex:self.frames[sym]=f;self.errors.pop(sym,None)
                    # A reproducible, credential-free latest market snapshot.
                    atomic(self.root/'data'/'market'/f'{sym}.json',{'asof':now_ms(),'daily':[b.__dict__ for b in d],'h4':[b.__dict__ for b in h],'frame':f.asdict()})
                except (SafetyError,KeyError,ValueError,TypeError,OSError) as exc:
                    message=str(exc) if isinstance(exc,SafetyError) else type(exc).__name__
                    with self.mutex:self.errors[sym]=message
                    self.log('CANDLES_UNAVAILABLE',{'symbol':sym,'reason':message})
            self.stop.wait(self.c['frame_refresh_seconds'])

def connection(root):
    path=Path(root)/'connection.json'
    default={'env':str(Path.home()/'index-sniper-pro/.env'),'legacy_root':str(Path.home()/'index-sniper-pro')}
    if path.exists():
        c=json.loads(path.read_text())
        if set(c)!=set(default):raise SafetyError('connection.json expects env and legacy_root')
        return {k:str(Path(v).expanduser().resolve()) for k,v in c.items()}
    return default

def authorized(root,digest):
    try:
        arm=json.loads((Path(root)/'data'/'LIVE_ENABLED.json').read_text())
        return arm.get('fingerprint')==digest and fingerprint(root)==digest
    except (OSError,ValueError):return False

def run(root,mode):
    root=Path(root).resolve();c=config(root);digest=fingerprint(root);con=connection(root)
    os.umask(0o077)
    with contextlib.ExitStack() as leases:
        leases.enter_context(lock(root,mode))
        if mode=='live':
            from .cli import legacy_process_guard
            legacy_process_guard()
            if Path(con['legacy_root']).is_dir():
                leases.enter_context(file_lock(Path(con['legacy_root'])/'data/larry_v2/live.lock','legacy live'))
        log=logger(root,mode);store=Store(root/'data'/f'{mode}.sqlite')
        stop=threading.Event();frames=None
        def shutdown(*_):stop.set()
        signal_module.signal(signal_module.SIGTERM,shutdown);signal_module.signal(signal_module.SIGINT,shutdown)
        api=Rest(credentials(con['env']),write=True) if mode=='live' else Rest()
        broker=Live(api,store,c,lambda:authorized(root,digest),con['legacy_root'],log) if mode=='live' else Paper(api,store,c,log)
        try:
            broker.boot();c=broker.c
            frames=Frames(c,root,log);frames.start()
            log('RUNNING',{'mode':mode.upper(),'entry_enabled':mode=='paper' or authorized(root,digest),
                           'symbols':list(SYMBOLS),'leverage':5,'risk_pct':c['risk_pct']})
            heartbeat=0;error_at={};successful=0;last_issue={}
            def guarded(fn):
                try:fn();return True
                except (SafetyError,KeyError,ValueError,TypeError,OverflowError) as exc:
                    message=str(exc) if isinstance(exc,SafetyError) else 'UNEXPECTED_SCHEMA_'+type(exc).__name__
                    clock=now_ms()
                    last_issue.update({'at':clock,'reason':message})
                    if clock-error_at.get(message,0)>60_000:
                        log('BLOCKED_OR_UNAVAILABLE',{'reason':message});error_at[message]=clock
                    if not isinstance(exc,SafetyError) and mode=='live':broker.halt(message)
                    return False
            while not stop.is_set():
                cycle=time.monotonic();now=now_ms()
                # Quote failure must not skip private order/protection reconciliation.
                def manage_tick():
                    p=broker.state.get('position')
                    if p:
                        q=api.quote(p['symbol']);broker.tick(q,frames.get(p['symbol']),now_ms())
                tick_ok=guarded(manage_tick)
                poll_ok=guarded(lambda:broker.poll(now_ms()))
                def enter():
                    if not broker.state.get('position') and not broker.state.get('pending'):
                        candidates=[]
                        for sym in SYMBOLS:
                            f=frames.get(sym)
                            if f and now_ms()-f.built<180_000:
                                sig=strategy.signal(f,c,now_ms())
                                if sig:candidates.append(sig)
                        # Stable tie break. No hindsight ranking or retry of the same bar.
                        candidates.sort(key=lambda s:(s['bar_end'],SYMBOLS.index(s['symbol'])))
                        for sig in candidates:
                            if broker.entry(sig,now_ms()):break
                entry_ok=guarded(enter) if poll_ok else False
                if tick_ok and poll_ok and entry_ok:successful=now_ms()
                state=broker.state;p=state.get('position')
                status={'mode':mode,'at':now_ms(),'last_success':successful,'entry_enabled':mode=='paper' or authorized(root,digest),
                        'fingerprint':digest,
                        'equity':state.get('equity',state.get('cash')),'risk_halt':state.get('halt'),
                        'day_blocked':state.get('day_blocked',False),'week_blocked':state.get('week_blocked',False),
                        'loss_pause_until':state.get('loss_pause_until',0),'cooldown_until':state.get('cooldown_until',0),
                        'pending':{k:state['pending'].get(k) for k in ('kind','cid','created')} if state.get('pending') else None,
                        'position':{k:p.get(k) for k in ('symbol','side','qty','entry','stop','initial_stop','protection_checked','protection_ids')} if p else None,
                        'frames':{s:frames.get(s).bar_end if frames.get(s) else None for s in SYMBOLS},
                        'candle_errors':dict(frames.errors),'last_issue':dict(last_issue)}
                atomic(root/'data'/f'{mode}_status.json',status)
                if now-heartbeat>=c['heartbeat_minutes']*60_000:log('HEARTBEAT',status);heartbeat=now
                stop.wait(max(.05,c['poll_seconds']-(time.monotonic()-cycle)))
        finally:
            if frames:frames.stop.set()
            log('STOPPED',{'mode':mode,'managed_position':bool(broker.state.get('position')),
                            'note':'Existing exchange initial SL remains; local structural trail is inactive while stopped.'})
            store.close()
