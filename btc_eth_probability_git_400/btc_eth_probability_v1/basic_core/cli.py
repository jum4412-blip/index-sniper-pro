"""Operator commands. start-live is the explicit real-order start command."""
import argparse,json,os,signal,sqlite3,subprocess,sys,time
from pathlib import Path
from .core import *
from .api import Rest,credentials
from .store import Store
from .portfolio import Portfolio
from . import account,risk,strategy,runtime

SERVICE='btc-eth-probability-v1.service'

def processes(legacy_root=None,current_root=None,proc_root='/proc'):
    """Recognize old trading managers, excluding this installation and self."""
    result=[];current_root=Path(current_root or Path(__file__).resolve().parents[1]).resolve()
    for d in Path(proc_root).glob('[0-9]*'):
        try:
            if int(d.name)==os.getpid() or d.stat().st_uid!=os.getuid():continue
            args=[x.decode(errors='replace') for x in (d/'cmdline').read_bytes().split(b'\0') if x]
            cwd=(d/'cwd').resolve();module=''
            if '-m' in args:
                i=args.index('-m');module=args[i+1];tail=args[i+2:]
                known=((module=='larry_v2' and 'live' in tail) or (module=='larry_live' and 'run' in tail)
                       or (module=='index_sniper.larry_williams_core_v1' and 'loop' in tail)
                       or (module=='trend_core' and 'live' in tail) or (module=='eth_core' and 'live' in tail)
                       or (module=='basic_core' and 'live' in tail and cwd!=current_root))
            else:
                known=any(Path(a).name in ('dual_live_v63.py','larry_live.py') for a in args)
            if not known:continue
            if legacy_root is not None and cwd!=Path(legacy_root).resolve():continue
            result.append({'pid':int(d.name),'start':(d/'stat').read_text().rsplit(')',1)[1].split()[19],
                           'module':module,'cwd':str(cwd)})
        except (OSError,ValueError,IndexError):continue
    return result

def legacy_process_guard(root=None):
    if processes(current_root=root):raise SafetyError('LEGACY_PROCESS_RUNNING: pause and reconcile the previous bot; this program will not stop it')

def trend_state(path):
    p=Path(path)/'data/live.sqlite'
    if not p.exists():return {},None
    with sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True) as db:
        def val(k):
            r=db.execute('SELECT json FROM state WHERE key=?',(k,)).fetchone();return json.loads(r[0]) if r else None
        return val('engine') or {},val('account_uid')

def occupied(st):
    if any(st.get(k) for k in ('position','pending','managed_position','pending_entry')):return True
    return any(occupied(s) for s in st.get('legs',{}).values())

def legacy_state_guard(root):
    con=runtime.connection(root);risk.legacy_check(con['legacy_root'])
    for kind in ('trend_root','basic_root','psych_root','trail3_root'):
        old=Path(con[kind])
        if old.resolve()==Path(root).resolve():continue
        if (old/'data/LIVE_ENABLED.json').exists():raise SafetyError('OLD_'+kind.upper()+'_ARMED: pause the previous program first')
        st,_=trend_state(old)
        if occupied(st):raise SafetyError('OLD_'+kind.upper()+'_POSITION_OR_PENDING')

def public_check(root,api,c):
    result={};model,model_hash=strategy.load_model(root,now_ms())
    for sym in SYMBOLS:
        inst=api.instrument(sym);five=api.candles(sym,'5m',240)
        f=strategy.frame(sym,five,c,now_ms(),model,model_hash);q=api.quote(sym)
        result[sym]={'price_step':inst.price_step,'qty_step':inst.qty_step,'min_qty':inst.min_qty,
                     'min_value':inst.min_value,'taker_fee':inst.fee,'mark':q.mark,
                     'quote_age_seconds':(now_ms()-q.ts)/1000,'frame':f.asdict(),
                     'probability_status':runtime.trend_status(f,now_ms()),
                     'target_notional_usdt':c['target_notional_usdt'],'nominal_margin_usdt':c['allocation_usdt']}
    matched=model.get('label_notional_usdt')==c['target_notional_usdt']
    result['model']={'sha256':model_hash,'training_cut_ms':model['training_cut_ms'],'fit_label_count':model['fit_label_count'],
                     'label_notional_usdt':model.get('label_notional_usdt'),'live_sizing_model_compatible':matched,
                     'qualifying_state_count':sum(v.get('eligible') is True for v in model['states'].values()) if matched else 0,
                     'live_order_execution_tested':False,'future_profitability_guaranteed':False}
    return result

def prepare(root,setup=False):
    c=config(root);con=runtime.connection(root)
    with runtime.lock(root,'live'):
        legacy_state_guard(root);legacy_process_guard(root)
        store=Store(root/'data/live.sqlite');api=Rest(credentials(con['env']),write=setup)
        try:
            b=Portfolio(api,store,c,lambda:False,con['legacy_root'],con['trend_root'],basic_root=con['basic_root'],psych_root=con['psych_root'],trail3_root=con['trail3_root']);b.boot()
            if occupied(b.state):raise SafetyError('LOCAL_POSITION_OR_PENDING: use ./prob resume-live; never delete the DB')
            account.require_flat(b.snapshot)
            if b.snapshot['settings'].get('accountMode')!='unified' or b.snapshot['settings'].get('holdMode') not in ('one_way_mode','hedge_mode'):
                raise SafetyError('UTA unified account with known hold mode required')
            from . import notify
            notify.credentials(con['env'])
            print('BTC·ETH 5분봉 가격·거래량 사건, 동결 확률 모형, 주문 단위·수수료·텔레그램 설정을 확인합니다.',flush=True)
            pub=public_check(root,api,c)
            if setup:
                for sym in SYMBOLS:
                    try:api.post('/api/v3/account/set-leverage',{'category':CAT,'symbol':sym,'marginMode':'crossed','leverage':'5'})
                    except UnknownOrder:pass
                b.refresh(True);account.require_flat(b.snapshot)
            correct=all(account.correct_settings(b.snapshot['settings'],sym) for sym in SYMBOLS)
            blocks=list(b.state.get('entry_blocks',[]))
            if b.state.get('halt'):blocks.append(b.state['halt'])
            for sym,leg in b.legs.items():blocks.extend(sym+':'+x for x in risk.blocks(leg.state,b.leg_equity(sym),c,now_ms()))
            b.save()
            result={'at':now_ms(),'account_equity_usdt':b.snapshot['equity'],'allocation_each_usdt':c['allocation_usdt'],
                    'target_notional_each_usdt':c['target_notional_usdt'],'minimum_initial_account_equity_usdt':c['min_account_equity_usdt'],
                    'flat':True,'crossed_5x_confirmed':correct,'risk_blocks':list(dict.fromkeys(blocks)),
                    'private_reads_verified':True,'live_order_execution_tested':False,'strategy_profitability_verified':False,'symbols':pub}
            atomic(root/'data/preflight.json',result)
            if not correct:raise SafetyError('CROSS_5X_NOT_CONFIRMED: run ./prob setup')
            return result
        finally:store.close()

def launch(root,mode='live'):
    (root/'data').mkdir(parents=True,exist_ok=True)
    unit=Path('/etc/systemd/system')/SERVICE
    if mode=='live' and unit.exists():
        from .service import verify_unit,verify_effective_unit
        verified=verify_unit(unit,root)
        verify_effective_unit(unit,root,verified,subprocess.run)
        subprocess.run(['sudo','-n','systemctl','start',SERVICE],check=True)
    else:
        with (root/'data'/f'{mode}_bootstrap.log').open('ab',buffering=0) as out:
            proc=subprocess.Popen([sys.executable,'-m','basic_core',mode,'--root',str(root)],cwd=root,stdin=subprocess.DEVNULL,stdout=out,stderr=out,start_new_session=True)
            atomic(root/'data'/f'{mode}_pid.json',{'pid':proc.pid,'at':now_ms()})
    print('관리 프로세스 시작 요청. ./prob status --mode '+mode+' 로 실행 여부를 확인하세요.')

def start(root,resume=False):
    if runtime.running(root,'live'):raise SafetyError('already running; use ./prob status --mode live or enable-live')
    (root/'data/LIVE_ENABLED.json').unlink(missing_ok=True)
    if resume:print('기존 포지션/미확정 주문 복구로 시작합니다. 신규 진입은 중지 상태입니다.')
    else:
        result=prepare(root)
        if result['risk_blocks']:raise SafetyError('RISK_BLOCKED: '+','.join(result['risk_blocks']))
        atomic(root/'data/LIVE_ENABLED.json',{'fingerprint':fingerprint(root),'at':now_ms()})
    try:launch(root)
    except Exception:
        (root/'data/LIVE_ENABLED.json').unlink(missing_ok=True);raise

def enable_live(root):
    if not runtime.running(root,'live'):raise SafetyError('LIVE_NOT_RUNNING: use ./prob start-live')
    status=json.loads((root/'data/live_status.json').read_text())
    if now_ms()-status['at']>60_000 or status.get('stopped'):raise SafetyError('STALE_PROCESS_STATUS')
    if status.get('fingerprint')!=fingerprint(root):raise SafetyError('CODE_CONFIG_CHANGED: restart manager with entries paused')
    if status.get('risk_halt') or status.get('entry_blocks'):raise SafetyError('RISK_BLOCKED')
    if not status.get('last_success') or now_ms()-status['last_success']>60_000:raise SafetyError('NO_RECENT_SUCCESSFUL_ACCOUNT_CHECK')
    legacy_state_guard(root);legacy_process_guard(root)
    with sqlite3.connect((root/'data/live.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        st=json.loads(db.execute("SELECT json FROM state WHERE key='engine'").fetchone()[0])
    if occupied(st) or any(s.get('halt') for s in st['legs'].values()):raise SafetyError('WAIT_UNTIL_POSITIONS_AND_PENDING_ARE_FLAT')
    api=Rest(credentials(runtime.connection(root)['env']));snap=account.inventory(api);account.require_flat(snap)
    if not all(account.correct_settings(snap['settings'],s) for s in SYMBOLS):raise SafetyError('CROSS_5X_NOT_CONFIRMED')
    c=config(root);eq=2*c['allocation_usdt']+snap['equity']-st['baseline_account_equity']
    blocks=risk.blocks(st['portfolio_guard'],max(eq,.000001),c,now_ms())+risk.blocks(st['account_guard'],snap['equity'],c,now_ms())
    for leg in st['legs'].values():blocks+=risk.blocks(leg,max(leg['cash'],.000001),c,now_ms())
    if blocks:raise SafetyError(','.join(blocks))
    atomic(root/'data/LIVE_ENABLED.json',{'fingerprint':fingerprint(root),'at':now_ms()})
    print('신규 진입 허용. 새 완료 5분봉 사건과 동결 확률 조건을 기다립니다.')

def stop_flat(root,mode):
    pause(root,mode)
    p=root/'data'/f'{mode}.sqlite'
    if p.exists():
        with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as db:
            r=db.execute("SELECT json FROM state WHERE key='engine'").fetchone();st=json.loads(r[0]) if r else {}
        if occupied(st):raise SafetyError('POSITION_OR_PENDING: entries paused, manager kept running')
    if mode=='live':
        api=Rest(credentials(runtime.connection(root)['env']))
        for _ in range(2):account.require_flat(account.inventory(api));time.sleep(1)
    unit=Path('/etc/systemd/system')/SERVICE
    if mode=='live' and unit.exists():
        from .service import verify_unit,verify_effective_unit
        verified=verify_unit(unit,root)
        verify_effective_unit(unit,root,verified,subprocess.run)
        subprocess.run(['sudo','-n','systemctl','stop',SERVICE],check=True)
    else:
        pidfile=root/'data'/f'{mode}_pid.json'
        if pidfile.exists():
            pid=json.loads(pidfile.read_text())['pid'];proc=Path('/proc')/str(pid)
            if proc.exists() and (proc/'cwd').resolve()==root and ('basic_core\0'+mode).encode() in (proc/'cmdline').read_bytes():os.kill(pid,signal.SIGTERM)
    print('평탄 상태 확인 후 정상 종료 요청.')

def pause(root,mode):
    if mode=='live':(root/'data/LIVE_ENABLED.json').unlink(missing_ok=True)
    else:atomic(root/'data/PAPER_PAUSED.json',{'at':now_ms()})
    print(mode+' 신규 진입 중지. 기존 포지션의 손절·2R·60분 청산 관리는 계속합니다.')

def show(root,mode,report=False):
    path=root/'data'/f'{mode}.sqlite'
    if report:
        if not path.exists():print('거래 기록이 아직 없습니다.');return
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            trades=[json.loads(r[0]) for r in db.execute('SELECT json FROM trades ORDER BY closed')]
        verified=[t for t in trades if t.get('net_usdt') is not None]
        out={'mode':mode,'closed_trades':len(trades),'net_verified_trades':len(verified),
             'net_usdt':sum(t['net_usdt'] for t in verified) if verified else None,
             'wins':sum(t['net_usdt']>0 for t in verified),'losses':sum(t['net_usdt']<0 for t in verified),
             'paper_net_before_funding_usdt':sum(t.get('net_before_funding_usdt',0) for t in trades) if mode=='paper' else None,
             'paper_sample_gaps':sum(bool(t.get('sample_gap')) for t in trades) if mode=='paper' else None,
             'paper_limitations':'funding, liquidation, depth, unsampled price excursions not simulated' if mode=='paper' else None,
             'last_trades':[{k:t.get(k) for k in ('symbol','side','closed','net_usdt','net_before_funding_usdt','reason','accounting')} for t in trades[-10:]]}
    else:
        status=root/'data'/f'{mode}_status.json'
        out=json.loads(status.read_text()) if status.exists() else {'mode':mode,'status':'NOT_STARTED'}
        out['process_running']=runtime.running(root,mode)
        if out.get('at'):out['status_age_seconds']=round((now_ms()-out['at'])/1000,1)
    print(json.dumps(out,ensure_ascii=False,indent=2))

def main(argv=None):
    parser=argparse.ArgumentParser(description='BTC/ETH causal price/volume probability bot: 2000 USDT each, crossed 5x. start-live sends real orders.')
    parser.add_argument('command',nargs='?',default='status',choices=['self-test','doctor-public','doctor','setup','recover-collateral','start-live','resume-live','enable-live','live','paper','start-paper','enable-paper','pause','stop','status','report','notify-preview','notify-test','notify-now'])
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1]);parser.add_argument('--mode',choices=['live','paper'],default='live')
    args=parser.parse_args(argv);root=args.root.expanduser().resolve();os.umask(0o077)
    try:
        cmd=args.command
        if cmd=='self-test':return subprocess.run([sys.executable,'-m','unittest','discover','-s',str(root/'tests'),'-v'],cwd=root).returncode
        elif cmd=='doctor-public':print(json.dumps(public_check(root,Rest(),config(root)),ensure_ascii=False,indent=2))
        elif cmd in ('doctor','setup'):print(json.dumps(prepare(root,cmd=='setup'),ensure_ascii=False,indent=2))
        elif cmd in ('start-live','resume-live'):start(root,cmd=='resume-live')
        elif cmd=='enable-live':enable_live(root)
        elif cmd=='recover-collateral':
            from .recovery import recover_collateral
            print(json.dumps(recover_collateral(root),ensure_ascii=False,indent=2))
        elif cmd in ('paper','live'):runtime.run(root,cmd)
        elif cmd=='start-paper':
            if runtime.running(root,'paper'):raise SafetyError('PAPER_ALREADY_RUNNING')
            launch(root,'paper')
        elif cmd=='enable-paper':(root/'data/PAPER_PAUSED.json').unlink(missing_ok=True);print('모의 신규 진입 허용.')
        elif cmd=='stop':stop_flat(root,args.mode)
        elif cmd=='pause':pause(root,args.mode)
        elif cmd in ('status','report'):show(root,args.mode,cmd=='report')
        elif cmd=='notify-test':
            from . import notify
            c=config(root)
            notify.send(notify.credentials(runtime.connection(root)['env']),f"[BTC·ETH 확률봇 LIVE] 알림 연결 테스트. 각 {c['allocation_usdt']:,.0f} USDT / 교차 {c['leverage']}배. 이 명령은 거래 주문을 보내지 않습니다.")
            print('설정된 단일 텔레그램 대화에 테스트 알림을 보냈습니다.')
        elif cmd=='notify-preview':
            from .notify import summary
            print(summary(root,args.mode))
        elif cmd=='notify-now':
            from . import notify
            message=notify.summary(root,args.mode)
            notify.send(notify.credentials(runtime.connection(root)['env']),message)
            print('BTC·ETH 현황을 설정된 텔레그램 대화에 한 건 보냈습니다.')
        return 0
    except (SafetyError,OSError,ValueError,KeyError,TypeError,sqlite3.Error,subprocess.CalledProcessError) as exc:
        print('BLOCKED: '+(str(exc) if isinstance(exc,SafetyError) else type(exc).__name__),file=sys.stderr)
        print('data/*.log 와 상태를 확인하세요. 미확정 주문/손실 기록을 지우지 마세요.',file=sys.stderr);return 2
