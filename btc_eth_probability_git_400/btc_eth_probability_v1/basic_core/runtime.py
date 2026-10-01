"""Public sampling stays independent of slow private order reconciliation.

Workers only publish immutable frames/quotes and transient crossings. The main
thread alone owns SQLite, positions, orders, and their durable state.
"""
import contextlib,copy,fcntl,json,logging,logging.handlers,os,queue,signal,threading,time
from pathlib import Path
from .core import *
from .api import Rest,credentials
from .store import Store
from .portfolio import Portfolio
from . import strategy

MINUTE=60_000

@contextlib.contextmanager
def file_lock(path,label):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+') as f:
        try:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise SafetyError(label+' already running') from None
        yield

def lock(root,mode):return file_lock(Path(root)/'data'/f'{mode}.lock',mode)
def running(root,mode):
    try:
        with lock(root,mode):return False
    except SafetyError:return True

def connection(root):
    home=Path.home()
    c={'env':str(home/'index-sniper-pro/.env'),'legacy_root':str(home/'index-sniper-pro'),
       'trend_root':str(home/'btc_eth_trend_v1'),'basic_root':str(home/'btc_eth_larry_basic_v1'),'psych_root':str(home/'btc_eth_psych_trend_v1'),'trail3_root':str(home/'eth_larry_trail3_500')}
    path=Path(root)/'connection.json'
    if path.exists():
        v=json.loads(path.read_text())
        if not isinstance(v,dict) or set(v)-set(c) or not {'env','legacy_root','trend_root'}<=set(v):
            raise SafetyError('connection.json expects env, legacy_root, trend_root; basic_root optional')
        c.update(v)
    if any(not isinstance(v,str) or not v for v in c.values()):raise SafetyError('invalid connection path')
    return {k:str(Path(v).expanduser().resolve()) for k,v in c.items()}

def authorized(root,digest):
    try:
        arm=json.loads((Path(root)/'data/LIVE_ENABLED.json').read_text())
        return arm.get('fingerprint')==digest and fingerprint(root)==digest
    except (OSError,ValueError):return False

def paper_enabled(root):return not (Path(root)/'data/PAPER_PAUSED.json').exists()

def logger(root,mode):
    log=logging.getLogger('probability.'+mode);log.setLevel(logging.INFO);log.propagate=False
    for h in list(log.handlers):h.close();log.removeHandler(h)
    path=Path(root)/'data';path.mkdir(exist_ok=True)
    handler=logging.handlers.RotatingFileHandler(path/(mode+'.log'),maxBytes=5000000,backupCount=5,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'));log.addHandler(handler)
    def emit(kind,data):
        line=json.dumps({'at':utc(now_ms()),'kind':kind,'data':data},ensure_ascii=False,allow_nan=False)
        log.info(line);print(line,flush=True)
    return emit

def frame_fresh(f,now):
    return bool(f and 0<=now-f.built<=strategy.FRESH_MS and 0<=now-f.bar_end<=strategy.FRESH_MS)

ENTRY_HIERARCHY=['COMPLETED_5M_PRICE_BASE_VOLUME_EVENT','FROZEN_NET_PROBABILITY_GATE','ONE_MINUTE_DELAY','FRESH_QUOTE_COST_MARGIN_RISK_CHECK']

def trend_status(f,now,error=None,higher_error=None):
    if f is None:return {'fresh':False,'wait_reason':error or 'CANDLE_WARMUP_PENDING','entry_hierarchy':ENTRY_HIERARCHY}
    p=f.probability;eligible_at=(f.event or {}).get('entry_after_ms')
    fresh=frame_fresh(f,now) and not error
    reason=(error or 'STALE_FRAME') if not fresh else p.get('reason','UNKNOWN_STATE')
    if p.get('eligible') and eligible_at is not None:
        reason='WAIT_ONE_MINUTE_DELAY' if now<eligible_at else 'QUALIFIED_EVENT_WINDOW' if now<=eligible_at+20000 else 'EVENT_WINDOW_EXPIRED'
    return {'fresh':bool(fresh),'five_minute_end':f.bar_end,'event':f.setup,'direction':f.direction,'wait_reason':reason,'entry_after_ms':eligible_at,
            'entry_hierarchy':ENTRY_HIERARCHY,'model_sha256':f.model_hash,'probability':p}

class Frames:
    PERIODS=(('5m',300000),)
    def __init__(self,c,root,log,api=None):
        self.c=c;self.root=Path(root);self.log=log;self.api=api or Rest()
        self.model_error=None
        try:self.model,self.model_hash=strategy.load_model(root,now_ms())
        except (SafetyError,OSError,ValueError,KeyError,TypeError) as exc:
            self.model=None;self.model_hash=None;self.model_error=str(exc) if isinstance(exc,SafetyError) else type(exc).__name__
        self.frames={};self.errors={s:'CANDLE_WARMUP_PENDING' for s in SYMBOLS};self.higher_errors={}
        self.boundaries={s:None for s in SYMBOLS};self.mutex=threading.Lock();self.stop=threading.Event()
        self.thread=threading.Thread(target=self.run,daemon=True);self.last_error={}
    def start(self):self.thread.start()
    def get(self,symbol,now=None):
        with self.mutex:
            f=self.frames.get(symbol)
            return f if symbol not in self.errors and frame_fresh(f,now if now is not None else now_ms()) else None
    def status(self):
        with self.mutex:return dict(self.errors)
    def trends(self,now):
        with self.mutex:return {s:trend_status(self.frames.get(s),now,self.errors.get(s)) for s in SYMBOLS}
    def step(self,symbol,now):
        if self.model is None:raise DataError('PROBABILITY_MODEL_UNAVAILABLE:'+str(self.model_error))
        expected=(now-2000)//300000*300000
        if self.boundaries[symbol]==expected and symbol not in self.errors:return
        bars=self.api.candles(symbol,'5m',240)
        if not any(b.ts+300000==expected for b in bars):raise DataError('LATEST_CLOSED_5m_PENDING')
        f=strategy.frame(symbol,bars,self.c,now,self.model,self.model_hash)
        with self.mutex:self.frames[symbol]=f;self.errors.pop(symbol,None);self.boundaries[symbol]=expected
        atomic(self.root/'data/market'/f'{symbol}.json',{'asof':now,'frame':f.asdict(),'source':'Bitget public completed UTC five-minute bars','sampling':'REST; execution latency not guaranteed'})
    def run(self):
        while not self.stop.is_set():
            for s in SYMBOLS:
                if self.stop.is_set():return
                try:self.step(s,now_ms())
                except (SafetyError,OSError,ValueError,KeyError,TypeError) as exc:
                    why=str(exc) if isinstance(exc,SafetyError) else type(exc).__name__
                    with self.mutex:self.errors[s]=why
                    if now_ms()-self.last_error.get((s,why),0)>60000:
                        self.log('CANDLES_UNAVAILABLE',{'symbol':s,'reason':why});self.last_error[s,why]=now_ms()
            self.stop.wait(self.c['frame_refresh_seconds'])

class Market:
    """One REST ticker worker per symbol; no worker accesses trading SQLite.

Crossings sampled during private-API waits are retained only within their entry
window. Lost/old samples reset the crossing baseline instead of inventing one.
"""
    def __init__(self,c,frames,log,api_factory=Rest):
        self.c=c;self.frames=frames;self.log=log;self.api_factory=api_factory;self.entry_allowed=lambda:True
        self.mutex=threading.RLock();self.stop=threading.Event();self.quotes={};self.queue={};self.errors={}
        self.observed={};self.position_guards={};self.stop_hits={};self.entry_context=None;self.crosses={s:strategy.Crossings() for s in SYMBOLS};self.acked=set()
        self.threads=[threading.Thread(target=self.run,args=(s,),daemon=True) for s in SYMBOLS]
    def start(self):
        for thread in self.threads:thread.start()
    def observe(self,s,q,f,now):
        q.validate(now)
        with self.mutex:
            self.quotes[s]=q;self.observed[s]=now;self.errors.pop(s,None)
            guard=self.position_guards.get(s)
            if guard and (1 if guard['side']=='LONG' else -1)*(q.mark-guard['stop'])<=0:
                self.stop_hits[s]={'opened':guard['opened'],'observed':now,'quote_ts':q.ts,'mark':q.mark}
            if not self.entry_allowed() or not frame_fresh(f,now):
                self.crosses[s]=strategy.Crossings();self.queue.pop(s,None);return
            sig=self.crosses[s].observe(f,q,self.c,now)
            if sig and sig['key'] not in self.acked:self.queue[s]=copy.deepcopy(sig)
            else:self.queue.pop(s,None)
            pending=self.queue.get(s)
            if pending and (now-pending['crossed']>self.c['entry_window_seconds']*1000
                            or now-pending['crossed']>self.c['entry_window_seconds']*1000):self.queue.pop(s,None)
    def publish_positions(self,state):
        with self.mutex:
            self.position_guards={s:{k:p[k] for k in ('opened','side','stop')} for s,leg in state['legs'].items() if (p:=leg.get('position'))}
    def set_entry_context(self,sig):
        with self.mutex:self.entry_context=sig
    def can_submit(self):
        with self.mutex:
            if self.stop_hits:return False
            sig=self.entry_context
            if sig is None:return True
            if self.queue.get(sig['symbol'],{}).get('key')!=sig['key']:return False
            if not 0<=now_ms()-sig['crossed']<=self.c['entry_window_seconds']*1000:return False
            f=self.frames.get(sig['symbol']) if self.frames else None
            if not f:return False
            return bool(f.event and f.bar_end==sig['bar_end'] and f.model_hash==sig['frame']['model_hash'] and strategy.score_eligible(f.probability,self.c))

    def has_stop_hits(self):
        with self.mutex:return bool(self.stop_hits)
    def apply_observed_stops(self,broker):
        with self.mutex:
            hits=self.stop_hits;self.stop_hits={}
        changed=False
        for s,hit in hits.items():
            p=broker.state['legs'][s].get('position')
            if p and p['opened']==hit['opened']:
                p['force_exit']=p.get('force_exit') or 'OBSERVED_STRUCTURAL_STOP'
                p['observed_stop_hit']=hit;changed=True
        if changed:broker.save()
    def snapshot(self,now):
        with self.mutex:
            signals={s:copy.deepcopy(v) for s,v in self.queue.items()
                     if 0<=now-v['crossed']<=self.c['entry_window_seconds']*1000}
            return dict(self.quotes),signals,dict(self.errors),dict(self.observed)
    def acknowledge(self,sig):
        with self.mutex:
            self.acked.add(sig['key'])
            if len(self.acked)>10_000:self.acked={v['key'] for v in self.queue.values()}|{sig['key']}
            s=sig['symbol']
            if self.queue.get(s,{}).get('key')==sig['key']:self.queue.pop(s,None)
            if self.crosses[s].queue.get(s,{}).get('key')==sig['key']:self.crosses[s].queue.pop(s,None)
    def run(self,s):
        api=self.api_factory();last_error=0
        while not self.stop.is_set():
            started=time.monotonic()
            try:
                q=api.quote(s);now=now_ms();self.observe(s,q,self.frames.get(s,now),now)
            except (SafetyError,OSError,ValueError,KeyError,TypeError) as exc:
                why=str(exc) if isinstance(exc,SafetyError) else type(exc).__name__
                with self.mutex:self.errors[s]=why;self.crosses[s]=strategy.Crossings();self.queue.pop(s,None)
                if now_ms()-last_error>60_000:self.log('QUOTE_UNAVAILABLE',{'symbol':s,'reason':why});last_error=now_ms()
            self.stop.wait(max(.05,self.c['poll_seconds']-(time.monotonic()-started)))

def run(root,mode='paper'):
    if mode not in ('live','paper'):raise SafetyError('unknown mode')
    root=Path(root).resolve();c=config(root);digest=fingerprint(root);os.umask(0o077)
    with contextlib.ExitStack() as leases:
        leases.enter_context(lock(root,mode))
        con=None
        if mode=='live':
            from .cli import legacy_process_guard,legacy_state_guard
            con=connection(root);legacy_process_guard(root);legacy_state_guard(root)
            oldlocks=[(Path(con['legacy_root'])/'data/larry_v2/live.lock','old Larry'),
                      (Path(con['trend_root'])/'data/live.lock','old trend'),
                      (Path(con['basic_root'])/'data/live.lock','old basic'),(Path(con['psych_root'])/'data/live.lock','old psych'),(Path(con['trail3_root'])/'data/live.lock','old ETH Larry Trail3')]
            for path,label in oldlocks:
                if path.parent.exists() and path.parent.parent.resolve()!=root:leases.enter_context(file_lock(path,label))
        raw_log=logger(root,mode);db=Store(root/'data'/f'{mode}.sqlite');stop=threading.Event()
        from . import notify
        notification_queue=queue.SimpleQueue()
        def log(kind,data):
            raw_log(kind,data);notification_queue.put((kind,copy.deepcopy(data)))
        def flush_notifications():
            while not notification_queue.empty():
                kind,data=notification_queue.get()
                # Executed orders and safety events are recovered once from the
                # committed event ledger. The logger is not a second alert path.
                if kind not in notify.DURABLE_EVENTS:notify.enqueue(db,kind,data,mode)
        notifier=None
        if mode=='live':
            notifier=notify.Outbox(root/'data'/f'{mode}.sqlite',notify.credentials(con['env']),raw_log)
            notifier.start()
        frames=Frames(c,root,log);market=Market(c,frames,log)
        signal.signal(signal.SIGTERM,lambda *_:stop.set());signal.signal(signal.SIGINT,lambda *_:stop.set())
        entry_auth=(lambda:authorized(root,digest)) if mode=='live' else (lambda:paper_enabled(root))
        if mode=='live':
            broker=Portfolio(Rest(credentials(con['env']),write=True),db,c,lambda:entry_auth() and market.can_submit(),con['legacy_root'],con['trend_root'],log,basic_root=con['basic_root'],psych_root=con['psych_root'],trail3_root=con['trail3_root'])
        else:
            from .paper import Paper
            broker=Paper(Rest(),db,c,lambda:entry_auth() and market.can_submit(),log)
        errors={};last_success=0;last_issue={};heartbeat=0
        def guarded(fn):
            try:fn();return True
            except (SafetyError,OSError,ValueError,KeyError,TypeError,OverflowError) as exc:
                why=str(exc) if isinstance(exc,SafetyError) else 'UNEXPECTED_'+type(exc).__name__
                last_issue.update(at=now_ms(),reason=why)
                if now_ms()-errors.get(why,0)>60_000:log('BLOCKED_OR_UNAVAILABLE',{'reason':why});errors[why]=now_ms()
                if not isinstance(exc,(SafetyError,OSError)):broker.halt(why)
                return False
        def enter(sig):
            market.set_entry_context(sig)
            try:return broker.entry(sig,now_ms())
            finally:market.set_entry_context(None)
        def exit_priority():
            market.apply_observed_stops(broker)
            latest=market.snapshot(now_ms())[0];success=True
            for s,q in latest.items():
                success=guarded(lambda q=q,s=s:broker.tick(q,frames.get(s),now_ms())) and success
            market.publish_positions(broker.state)
            return success
        try:
            broker.boot();market.entry_allowed=entry_auth;market.publish_positions(broker.state);frames.start();market.start()
            log('RUNNING',{'mode':mode,'symbols':list(SYMBOLS),'allocation_each':c['allocation_usdt'],'target_notional_each':c['target_notional_usdt'],
                           'entry_enabled':broker.authorized(),'qualifying_state_count':sum(v.get('eligible') is True for v in frames.model['states'].values()) if frames.model and frames.model.get('label_notional_usdt')==c['target_notional_usdt'] else 0,
                           'live_sizing_model_compatible':bool(frames.model and frames.model.get('label_notional_usdt')==c['target_notional_usdt']),
                           'model_available':frames.model is not None,'entry_hierarchy':ENTRY_HIERARCHY,'sampling':'REST workers; latency measured, not guaranteed',
                           'funding': 'exchange ledger' if mode=='live' else 'UNMODELED'})
            while not stop.is_set():
                cycle=time.monotonic();flush_notifications();ok=True;now=now_ms()
                quotes,signals,quote_errors,observed=market.snapshot(now)
                market.apply_observed_stops(broker)
                for s,q in quotes.items():
                    ok=guarded(lambda q=q,s=s:broker.tick(q,frames.get(s),now_ms())) and ok
                market.publish_positions(broker.state)
                private_ok=guarded(lambda:broker.poll(now_ms()));ok=private_ok and ok
                ok=exit_priority() and ok
                # Re-read after private calls: a signal that became old meanwhile
                # must never become a delayed market entry.
                quotes,signals,quote_errors,observed=market.snapshot(now_ms())
                for s,sig in signals.items():
                    ok=exit_priority() and ok
                    if any((leg.get('position') or {}).get('force_exit') or (leg.get('position') or {}).get('closing') for leg in broker.state['legs'].values()):break
                    latest_signals=market.snapshot(now_ms())[1]
                    if latest_signals.get(s,{}).get('key')!=sig['key']:continue
                    if db.seen(sig['key']):market.acknowledge(sig);continue
                    if not broker.authorized():
                        db.signal(sig['key'],{'decision':'PAUSED_CROSS_CONSUMED','signal':sig});market.acknowledge(sig);continue
                    if not frames.get(s):continue
                    if private_ok and broker.can_enter(s):
                        ok=guarded(lambda sig=sig:enter(sig)) and ok
                        if db.seen(sig['key']):market.acknowledge(sig)
                market.publish_positions(broker.state)
                if ok and len(quotes)==len(SYMBOLS) and not quote_errors and all(-2000<=now_ms()-q.ts<=8000 for q in quotes.values()):last_success=now_ms()
                state=broker.state
                legs={}
                for s in SYMBOLS:
                    leg=state['legs'][s];p=leg.get('position');pending=leg.get('pending')
                    legs[s]={'cash':leg['cash'],'equity':broker.leg_equity(s),'halt':leg.get('halt'),
                             'position':{k:p.get(k) for k in ('symbol','side','qty','entry','stop','target','expires','initial_stop','protection_ids','protection_checked','protection_mode','exchange_stop','local_trail_stale','last_quote_at')} if p else None,
                             'pending':{k:pending.get(k) for k in ('kind','cid','created')} if pending else None}
                status={'mode':mode,'at':now_ms(),'last_success':last_success,'last_issue':last_issue,'fingerprint':digest,
                        'allocation_each':c['allocation_usdt'],'target_notional_each':c['target_notional_usdt'],
                        'entry_enabled':broker.authorized(),'equity':state.get('equity'),'account_equity':state.get('account_equity'),
                        'risk_halt':state.get('halt'),'entry_blocks':state.get('entry_blocks',[]),'candle_errors':frames.status(),
                        'trends':frames.trends(now_ms()),'qualifying_state_count':sum(v.get('eligible') is True for v in frames.model['states'].values()) if frames.model and frames.model.get('label_notional_usdt')==c['target_notional_usdt'] else 0,
                        'live_sizing_model_compatible':bool(frames.model and frames.model.get('label_notional_usdt')==c['target_notional_usdt']),
                        'model_available':frames.model is not None,'model_error':frames.model_error,'entry_metadata_errors':dict(broker.metadata_errors) if mode=='live' else {},'higher_timeframe_errors':dict(frames.higher_errors),'entry_hierarchy':ENTRY_HIERARCHY,
                        'quote_errors':quote_errors,'quote_age_seconds':{s:round((now_ms()-q.ts)/1000,3) for s,q in quotes.items()},
                        'sample_age_seconds':{s:round((now_ms()-t)/1000,3) for s,t in observed.items()},
                        'legs':legs,'funding_accounting':'EXCHANGE_LEDGER' if mode=='live' else 'UNMODELED',
                        'liquidation_model':'NOT_SIMULATED' if mode=='paper' else 'EXCHANGE_EXECUTED'}
                atomic(root/'data'/f'{mode}_status.json',status)
                if now_ms()-heartbeat>=c['heartbeat_minutes']*60_000:log('HEARTBEAT',status);heartbeat=now_ms()
                stop.wait(max(.05,c['poll_seconds']-(time.monotonic()-cycle)))
        finally:
            frames.stop.set();market.stop.set()
            path=root/'data'/f'{mode}_status.json'
            try:
                status=json.loads(path.read_text()) if path.exists() else {}
                status.update(stopped=True,at=now_ms(),entry_enabled=False);atomic(path,status)
                log('STOPPED',{'mode':mode,'action':'native initial stop remains; local target/time manager stopped'})
            except (OSError,ValueError):pass
            flush_notifications()
            if notifier:notifier.stop.set();notifier.thread.join(timeout=4)
            db.close()
