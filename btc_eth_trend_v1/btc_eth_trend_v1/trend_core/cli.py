import argparse,copy,json,os,shutil,signal,sqlite3,subprocess,sys,time
from pathlib import Path
from .core import *
from .api import Rest,credentials
from .store import Store
from .engine import Live
from . import account,risk,strategy,runtime

def processes(legacy_root=None):
    result=[]
    for d in Path('/proc').glob('[0-9]*'):
        try:
            if d.stat().st_uid!=os.getuid():continue
            args=[x.decode(errors='replace') for x in (d/'cmdline').read_bytes().split(b'\0') if x]
            if '-m' not in args:continue
            i=args.index('-m');module=args[i+1];tail=args[i+2:]
            if not ((module=='larry_v2' and 'live' in tail) or (module=='index_sniper.larry_williams_core_v1' and 'loop' in tail)):continue
            cwd=(d/'cwd').resolve()
            if legacy_root is not None and cwd!=Path(legacy_root).resolve():continue
            # PID identity includes Linux process start time to avoid PID reuse.
            start=(d/'stat').read_text().rsplit(')',1)[1].split()[19]
            result.append({'pid':int(d.name),'start':start,'module':module,'cwd':str(cwd)})
        except (OSError,ValueError,IndexError):continue
    return result

def pause_legacy(root):
    con=runtime.connection(root);old=Path(con['legacy_root']);backup=Path(root)/'data'/'legacy_pause_backup'
    count=0
    for p in risk.legacy_arms(old):
        if p.exists():
            backup.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(p,backup/(str(time.time_ns())+'_'+p.name));p.unlink();count+=1
    print(f'이전 봇 진입 허가 파일 {count}개 해제. 기존 포지션 관리는 유지됩니다.')

def retire_legacy(root):
    pause_legacy(root);con=runtime.connection(root);old=Path(con['legacy_root'])
    api=Rest(credentials(con['env']))
    old_s,uid=risk.old_state(old)
    info=api.get('/api/v3/account/info',private=True)
    if uid and str(info.get('userId'))!=uid:raise SafetyError('LEGACY_ACCOUNT_MISMATCH')
    # No termination until both local state and exchange confirm flat twice.
    for _ in range(2):
        risk.legacy_check(old);account.require_flat(account.inventory(api))
        time.sleep(2)
    targets=processes(old)
    for p in targets:
        if p in processes(old):os.kill(p['pid'],signal.SIGTERM)
    time.sleep(3)
    remaining=processes()
    if remaining:
        raise SafetyError('LEGACY_PROCESS_RUNNING_OR_RESTARTED: '+','.join(str(p['pid']) for p in remaining)+
                          '. Stop its existing supervisor/service; no SIGKILL was sent.')
    print(f'이전 실매매 프로세스 {len(targets)}개에 정상 종료 요청. 거래소 주문/포지션은 변경하지 않았습니다.')

def legacy_process_guard():
    if processes():raise SafetyError('LEGACY_PROCESS_RUNNING: run ./trend retire-legacy after existing trades are flat')

def public_check(root,api,c):
    result={}
    for sym in SYMBOLS:
        inst=api.instrument(sym)
        daily=api.candles(sym,'1D',max(200,c['daily_slow']*3+10))
        h4=api.candles(sym,'4H',max(100,c['entry_channel']+20,c['atr_period']+20,c['initial_structure_bars']+20,c['exit_channel']+20))
        f=strategy.frame(sym,daily,h4,c,now_ms());q=api.quote(sym)
        result[sym]={'price_step':inst.price_step,'qty_step':inst.qty_step,'min_qty':inst.min_qty,
                     'min_value':inst.min_value,'taker_fee':inst.fee,'mark':q.mark,
                     'last_4h_close':utc(f.bar_end),'daily_direction':f.direction,'atr_4h':f.atr}
    return result

def prepare(root,setup=False):
    c=config(root);con=runtime.connection(root)
    with runtime.lock(root,'live'):
        print('사전 점검: 기존 봇/계좌/미체결 주문을 확인합니다.',file=sys.stderr,flush=True)
        risk.legacy_check(con['legacy_root']);legacy_process_guard()
        store=Store(root/'data'/'live.sqlite');api=Rest(credentials(con['env']),write=setup)
        try:
            b=Live(api,store,c,lambda:False,con['legacy_root']);b.boot()
            if b.state.get('position') or b.state.get('pending'):
                raise SafetyError('LOCAL_POSITION_OR_PENDING: use ./trend resume-live to reconcile; do not delete the database')
            snap=account.inventory(api);account.require_flat(snap)
            if snap['settings'].get('accountMode')!='unified' or snap['settings'].get('holdMode') not in ('one_way_mode','hedge_mode'):
                raise SafetyError('UTA unified account with a known hold mode is required')
            print('사전 점검: BTC·ETH 상품 단위와 일봉/4시간봉을 수집합니다.',file=sys.stderr,flush=True)
            pub=public_check(root,api,c)
            if setup:
                print('교차 5배를 설정하고 거래소 값을 다시 확인합니다.',file=sys.stderr,flush=True)
                for sym in SYMBOLS:
                    try:api.post('/api/v3/account/set-leverage',{'category':CAT,'symbol':sym,'marginMode':'crossed','leverage':'5'})
                    except UnknownOrder:pass # Read back actual settings; never assume the write succeeded.
                snap=account.inventory(api);account.require_flat(snap)
            correct=all(account.correct_settings(snap['settings'],sym) for sym in SYMBOLS)
            blocks=risk.blocks(b.state,snap['equity'],c,now_ms());b.save()
            result={'at':now_ms(),'equity_usdt':snap['equity'],'flat':True,'crossed_5x_confirmed':correct,
                    'risk_blocks':blocks,'private_reads_verified':True,'live_order_execution_tested':False,
                    'strategy_profitability_verified':False,'symbols':pub}
            atomic(root/'data'/'preflight.json',result)
            if not correct:raise SafetyError('CROSS_5X_NOT_CONFIRMED: run ./trend setup')
            return result
        finally:store.close()

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
             'paper_net_before_funding':sum(t.get('net_before_funding_usdt',0) for t in trades) if mode=='paper' else None,
             'paper_sample_gaps':sum(bool(t.get('sample_gap')) for t in trades) if mode=='paper' else None,
             'last_trades':[{k:t.get(k) for k in ('symbol','side','closed','net_usdt','net_before_funding_usdt','reason','accounting')} for t in trades[-10:]]}
    else:
        status=root/'data'/f'{mode}_status.json'
        out=json.loads(status.read_text()) if status.exists() else {'mode':mode,'status':'NOT_STARTED'}
        out['process_running']=runtime.running(root,mode)
        if out.get('at'):out['status_age_seconds']=round((now_ms()-out['at'])/1000,1)
    print(json.dumps(out,ensure_ascii=False,indent=2))

def start(root,mode,resume=False):
    if runtime.running(root,mode):raise SafetyError(mode+' is already running; use ./trend status')
    if mode=='live' and not resume:
        result=prepare(root)
        atomic(root/'data'/'LIVE_ENABLED.json',{'fingerprint':fingerprint(root),'at':now_ms()})
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if result['risk_blocks']:print('손실/대기 한도로 신규 진입은 차단됩니다. 관리 프로세스는 시작합니다.')
    if mode=='live' and resume:
        # Resume position/pending reconciliation without enabling any fresh entries.
        (root/'data'/'LIVE_ENABLED.json').unlink(missing_ok=True)
        print('기존 포지션/미확정 주문 복구만 시작합니다. 신규 진입은 비활성화됩니다.')
    service='btc-eth-trend.service'
    unit=Path('/etc/systemd/system')/service
    if mode=='live' and unit.exists():
        marker='# trend-root: '+str(root)
        if marker not in unit.read_text().splitlines():raise SafetyError('service belongs to another installation')
        subprocess.run(['sudo','-n','systemctl','start',service],check=True)
    else:
        (root/'data').mkdir(exist_ok=True)
        # Rotating application log is authoritative; redirect bootstrap exceptions.
        with (root/'data'/f'{mode}_bootstrap.log').open('ab',buffering=0) as out:
            proc=subprocess.Popen([sys.executable,'-m','trend_core',mode],cwd=root,stdin=subprocess.DEVNULL,
                                  stdout=out,stderr=out,start_new_session=True)
            atomic(root/'data'/f'{mode}_pid.json',{'pid':proc.pid,'at':now_ms()})
    time.sleep(1)
    print(f'{mode.upper()} 시작 요청 완료. ./trend status --mode {mode} 로 실행 상태를 확인하세요.')
    if mode=='live' and not unit.exists():print('자동 재시작 서비스가 없습니다. bash install_service.sh 설치를 권장합니다.')

def enable_live(root):
    if not runtime.running(root,'live'):raise SafetyError('LIVE_NOT_RUNNING: use ./trend start-live')
    status_path=root/'data/live_status.json'
    status=json.loads(status_path.read_text())
    if now_ms()-status['at']>60_000:raise SafetyError('STALE_PROCESS_STATUS')
    if status.get('fingerprint')!=fingerprint(root):raise SafetyError('CODE_CONFIG_CHANGED: restart only after flat review')
    con=runtime.connection(root);risk.legacy_check(con['legacy_root']);legacy_process_guard()
    with sqlite3.connect((root/'data/live.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        state=json.loads(db.execute("SELECT json FROM state WHERE key='engine'").fetchone()[0])
    if state.get('position') or state.get('pending'):raise SafetyError('WAIT_UNTIL_POSITION_AND_PENDING_ARE_FLAT')
    api=Rest(credentials(con['env']));snap=account.inventory(api);account.require_flat(snap)
    if not all(account.correct_settings(snap['settings'],s) for s in SYMBOLS):raise SafetyError('CROSS_5X_NOT_CONFIRMED')
    b=risk.blocks(state,snap['equity'],config(root),now_ms())
    if b:raise SafetyError(','.join(b))
    atomic(root/'data/LIVE_ENABLED.json',{'fingerprint':fingerprint(root),'at':now_ms()})
    print('신규 진입을 다시 허용했습니다. 다음 새로운 확정봉 신호를 기다립니다.')

def main(argv=None):
    parser=argparse.ArgumentParser(description='BTC/ETH 4H trend — Bitget UTA crossed 5x, one aggregate position')
    parser.add_argument('command',choices=['self-test','doctor-public','doctor','setup','pause-legacy','retire-legacy',
                      'start-paper','start-live','resume-live','enable-live','paper','live','pause','status','report'])
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--mode',choices=['live','paper'],default='live')
    args=parser.parse_args(argv);root=args.root.expanduser().resolve();os.umask(0o077)
    try:
        cmd=args.command
        if cmd=='self-test':
            return subprocess.run([sys.executable,'-m','unittest','discover','-s',str(root/'tests'),'-v'],cwd=root).returncode
        if cmd=='doctor-public':print(json.dumps(public_check(root,Rest(),config(root)),ensure_ascii=False,indent=2))
        elif cmd in ('doctor','setup'):print(json.dumps(prepare(root,cmd=='setup'),ensure_ascii=False,indent=2))
        elif cmd=='pause-legacy':pause_legacy(root)
        elif cmd=='retire-legacy':retire_legacy(root)
        elif cmd in ('start-live','start-paper','resume-live'):start(root,'paper' if cmd=='start-paper' else 'live',cmd=='resume-live')
        elif cmd=='enable-live':enable_live(root)
        elif cmd in ('live','paper'):runtime.run(root,cmd)
        elif cmd=='pause':
            (root/'data'/'LIVE_ENABLED.json').unlink(missing_ok=True)
            print('신규 실진입 중지. 기존 포지션의 손절/청산과 미확정 주문 복구는 계속합니다.')
        elif cmd in ('status','report'):show(root,args.mode,cmd=='report')
        return 0
    except (SafetyError,OSError,ValueError,KeyError,TypeError,subprocess.CalledProcessError) as exc:
        text=str(exc) if isinstance(exc,SafetyError) else type(exc).__name__
        print('BLOCKED: '+text,file=sys.stderr)
        print('기존 SQLite/손실 기록을 삭제하거나 한도를 높여 우회하지 마세요. data/*.log와 상태를 확인하세요.',file=sys.stderr)
        return 2
