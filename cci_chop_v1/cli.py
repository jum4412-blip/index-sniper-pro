"""Separate multi-timeframe CLI; importing this module never starts a runner."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal as process_signal
import sqlite3
import subprocess
import threading

from cci_chop_v1._compat import account
from cci_chop_v1._compat.api import credentials
from cci_chop_v1._compat.core import SafetyError, atomic, now_ms
from cci_chop_v1._compat.notify import credentials as telegram_credentials, send as telegram_send
from cci_chop_v1._compat.runtime import connection
from cci_chop_v1._compat.store import Store
from . import config, strategy
from .api import CCAPI
from .engine import CCEngine
from .notifications import Notifier, render
from .evidence import probability_gate, experimental_check, AUTH_KIND
from .entry_market import MarketFrames


class Blocked(SafetyError):
    pass


def state_dir(root):
    return Path(root) / "data" / "cci_chop"


def db_path(root, mode):
    return state_dir(root) / (mode + ".sqlite")


def model_path(root):
    return Path(root) / "cci_chop_v1" / "results" / "probability_model.json"


def load_model(root):
    path = model_path(root)
    raw=path.read_bytes()
    model = json.loads(raw)
    if not isinstance(model, dict) or model.get("strategy_sha256") != strategy.spec_sha256():
        raise Blocked("MODEL_STRATEGY_MISMATCH")
    return model, hashlib.sha256(raw).hexdigest()


def legacy_guard(root):
    from cci_chop_v1._compat.risk import legacy_check
    legacy_check(connection(Path(root))["legacy_root"])
    result=subprocess.run(["ps","-eo","pid,args"],capture_output=True,text=True,check=True)
    old=("larry_v2","larry_live","trend_core","eth_core","basic_core","dual_live_v63")
    for line in result.stdout.splitlines()[1:]:
        words=line.strip().split(None,1)
        if len(words)!=2 or not words[0].isdigit() or int(words[0])==os.getpid():
            continue
        command=words[1]
        if any(name in command for name in old) and "notify" not in command:
            raise Blocked("LEGACY_TRADING_PROCESS_RUNNING")
        if any(name in command for name in ("turtle_structure_v1.cli","mtf_structure_v1.cli")) and "run" in command and "--mode live" in command:
            raise Blocked("PREVIOUS_LIVE_MANAGER_RUNNING")


def release_check(model):
    """A research model is never promoted by a live command or an arm file."""
    required = ("native_bitget_data_verified", "execution_and_protection_verified",
                "prospective_validation_verified", "deployment_approved")
    if any(model.get(k) is not True for k in required):
        raise Blocked("MODEL_NOT_RELEASED: native/prospective/execution validation missing")
    if model.get("future_profitability_guaranteed") is not False:
        raise Blocked("INVALID_PROFIT_GUARANTEE_FIELD")
    if (not isinstance(model.get("source"), str) or "proxy" in model["source"].lower()
            or "binance" in model["source"].lower() or "bitget" not in model["source"].lower()):
        raise Blocked("NATIVE_BITGET_MODEL_REQUIRED")
    cells = model.get("cells")
    if not isinstance(cells, dict) or not any(v.get("statistical_gate_passed") is True for v in cells.values() if isinstance(v,dict)):
        raise Blocked("NO_QUALIFIED_MODEL_STATES")


def arm_payload(root, c, model_sha, uid, experimental=False):
    return {"release_sha256": config.release_digest(), "config_sha256": config.digest(c),
            "model_sha256": model_sha, "account_uid": str(uid), "created_ms": now_ms(),
            "authorization_kind": AUTH_KIND if experimental else "VERIFIED_MODEL",
            "unvalidated_live_acknowledged": experimental}


def arm_matches(root, expected):
    if (state_dir(root) / "PAUSE").exists():
        return False
    path = state_dir(root) / "ARM.json"
    try:
        stored = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(stored, dict) and all(stored.get(k) == expected[k] for k in
        ("release_sha256", "config_sha256", "model_sha256", "account_uid", "authorization_kind", "unvalidated_live_acknowledged"))


def strict_demo_credentials(root, path):
    """A local Demo key only; process environment cannot substitute a live key."""
    values = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        if "=" not in line:
            raise Blocked("DEMO_ENV_INVALID")
        key, value = line.split("=",1)
        key, value = key.strip(), value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in ("'",'"'):
            value=value[1:-1]
        else:
            value=value.split(" #",1)[0].rstrip()
        if key in values:
            raise Blocked("DEMO_ENV_DUPLICATE_KEY")
        values[key]=value
    c={"key":values.get("BITGET_API_KEY", ""),
       "secret":values.get("BITGET_SECRET_KEY", values.get("BITGET_API_SECRET", "")),
       "passphrase":values.get("BITGET_PASSPHRASE", values.get("BITGET_API_PASSPHRASE", ""))}
    if not all(c.values()) or any(v.startswith("YOUR_") for v in c.values()):
        raise Blocked("SEPARATE_DEMO_KEY_REQUIRED")
    try:
        live=credentials(connection(Path(root))["env"])
    except SafetyError:
        live=None
    if live and c["key"] == live["key"]:
        raise Blocked("LIVE_KEY_CANNOT_BE_USED_AS_DEMO_KEY")
    return c


def make_api(root, mode, write=False, demo_env=None):
    if mode == "demo":
        path = demo_env or Path(root)/"cci_chop_demo_credentials.env"
        return CCAPI(write=write, demo=True, creds=strict_demo_credentials(root,path))
    return CCAPI(root=root, write=write)


@contextlib.contextmanager
def single_runner(root):
    directory = state_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/"runner.lock").open("a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked("ANOTHER_CC_RUNNER_IS_ACTIVE") from None
        yield


def read_status(root, mode):
    path = db_path(root,mode)
    if not path.exists():
        return {"mode":mode,"status":"NOT_STARTED"}
    with sqlite3.connect(f"file:{path}?mode=ro",uri=True,timeout=3) as db:
        row=db.execute("SELECT json FROM state WHERE key='cci_chop_engine'").fetchone()
        state=json.loads(row[0]) if row else {}
        status=db.execute("SELECT json FROM state WHERE key='cci_chop_runtime'").fetchone()
        observation=db.execute("SELECT json FROM state WHERE key='cci_chop_last_observation'").fetchone()
        markets=db.execute("SELECT json FROM state WHERE key='cci_chop_market_status'").fetchone()
        return {"mode":mode,"runtime":json.loads(status[0]) if status else None,
                "positions":state.get("positions",{}),"pending":state.get("pending",{}),
                "modifications":state.get("modifications",{}),"halt":state.get("halt"),
                "last_tick":state.get("last_tick"),"risk":state.get("risk",{}),
                "observation":json.loads(observation[0]) if observation else {},
                "markets":json.loads(markets[0]) if markets else {},
                "operational_execution_verified":False}


def status_summary(status, paused=False, at=None):
    at=now_ms() if at is None else at
    observation=status.get("observation") or {}
    observed=observation.get("at")
    fresh=type(observed) is int and 0<=at-observed<=30000
    running=(status.get("runtime") or {}).get("running") is True and fresh
    return {"kind":"SUMMARY","at":at,"positions":status.get("positions",{}),
            "position_count":len(status.get("positions",{})),
            "pending_count":len(status.get("pending",{})),
            "entry_enabled":running and not paused and not status.get("halt")
                and not (status.get("risk") or {}).get("active_blocks")
                and observation.get("entry_enabled") is True,
            "equity":(status.get("risk") or {}).get("last_equity"),
            "reason":status.get("halt"),"state_only":True,"running":running,
            "last_observation_ms":observed,"markets":status.get("markets",{})}


def summary_event(engine, entry_enabled):
    s=engine.state
    return {"kind":"SUMMARY","at":now_ms(),"positions":s.get("positions",{}),
            "position_count":len(s.get("positions",{})),"pending_count":len(s.get("pending",{})),
            "entry_enabled":entry_enabled and not s.get("halt") and not s.get("risk",{}).get("active_blocks"),
            "equity":s.get("risk",{}).get("last_equity"),
            "reason":s.get("halt")}


def run(root, mode, c, demo_env=None):
    stop=threading.Event()
    for signum in (process_signal.SIGINT,process_signal.SIGTERM):
        process_signal.signal(signum,lambda *_:stop.set())
    model, model_sha = load_model(root)
    settings=telegram_credentials(connection(root)["env"])
    with single_runner(root):
        store=Store(db_path(root,mode))
        notifier=Notifier(store,db_path(root,mode),settings,mode)
        try:
            api=make_api(root,mode,write=mode in ("live","demo"),demo_env=demo_env)
            if mode=="shadow":
                from .paper import PaperAdapter
                api=PaperAdapter(api,store,config=c,initial_equity=c["shadow_equity_reference_usdt"])
                uid="SHADOW_STATIC_REFERENCE"
            else:
                uid=account.identity(api,store)
            try:
                stored_arm=json.loads((state_dir(root)/"ARM.json").read_text())
            except (OSError,ValueError):
                stored_arm={}
            if not isinstance(stored_arm,dict): stored_arm={}
            runtime=store.get("cci_chop_runtime",{})
            current=store.get("cci_chop_engine",{})
            unresolved=any(current.get(k) for k in ("positions","pending","modifications"))
            policy=runtime.get("binding",{}) if unresolved else stored_arm
            experimental=mode=="live" and isinstance(policy,dict) and policy.get("authorization_kind")==AUTH_KIND and policy.get("unvalidated_live_acknowledged") is True
            expected=arm_payload(root,c,model_sha,uid,experimental)
            frozen={k:expected[k] for k in ("release_sha256","config_sha256","model_sha256","account_uid","authorization_kind","unvalidated_live_acknowledged")}
            if runtime and runtime.get("binding")!=frozen:
                raise Blocked("RUNTIME_CHANGED: retain state and previous code for reconciliation")
            if mode=="live":
                current=store.get("cci_chop_engine",{})
                unresolved=any(current.get(k) for k in ("positions","pending","modifications"))
                if not unresolved:
                    legacy_guard(root)
                    experimental_check(model) if experimental else release_check(model)
                    if not arm_matches(root,expected):
                        raise Blocked("LIVE_NOT_ARMED")
            started=runtime.get("started_ms",now_ms())
            store.set("cci_chop_runtime",{"mode":mode,"binding":frozen,"started_ms":started,"running":True})
            def authorized():
                if (state_dir(root)/"PAUSE").exists():
                    return False
                if mode!="live":
                    return mode != "shadow" or api.scenario_status().get("valid") is True
                try:
                    experimental_check(model) if experimental else release_check(model)
                    legacy_guard(root)
                except SafetyError:
                    return False
                return arm_matches(root,expected)
            engine=CCEngine(api,store,{**c,"mode":mode,"experimental_live":experimental},authorized,notify=notifier.queue)
            notifier.start()
            notifier.queue({"kind":"START","at":now_ms()},key=f"START:{mode}:{started}")
            market=MarketFrames(api)
            next_summary=store.get("cci_chop_next_summary",0)
            while not stop.is_set():
                at=now_ms()
                if mode=="shadow" and at>=started+c["shadow_days"]*86400000:
                    # A virtual open trade is not silently dropped from results.
                    store.set("cci_chop_shadow_end",{"at":at,"open_positions_unverified":engine.state.get("positions",{})})
                    break
                signals=[];trails={};exits={};errors=[];market_status={}
                for symbol in c["symbols"]:
                    try:
                        decision_time=now_ms()-60000
                        frames=market.get_partial(symbol,decision_time)
                        errors.extend((symbol,"FRAME_"+frame) for frame in frames.get("_errors",{}))
                        analysis=strategy.analyze(frames,decision_time)
                        market_status[symbol]={"at":decision_time,"bias":analysis.get("bias",{}),
                            "wait_reasons":analysis.get("wait_reasons",[]), "indicators":analysis.get("indicators",{}),
                            "eligible_setup":analysis.get("eligible") is True,
                            "source":"Bitget completed UTC frames; W/D aggregated from H4"}
                        candidate=strategy.generate_signal(symbol,frames,decision_time)
                        p=engine.state.get("positions",{}).get(symbol)
                        if p:
                            update=strategy.update_guard(p["side"].lower(),p["stop"],frames,decision_time)
                            if update.should_exit:
                                exits[symbol]=update.reason or "CC_STRUCTURE_EXIT"
                            elif update.tightened:
                                trails[symbol]=update.guard
                        if candidate:
                            if mode=="live":
                                gate=probability_gate(model,symbol,candidate.side,candidate.metadata["volume_ratio"],strategy.spec_sha256(),experimental=experimental)
                                gate.update({"side":candidate.side.upper(),"model_sha256":model_sha,
                                             "paper_only":False,"demo_only":False,
                                             "deployment_approved":model.get("deployment_approved") is True,
                                             "native_execution_verified":model.get("execution_and_protection_verified") is True,
                                             "prospective_verified":model.get("prospective_validation_verified") is True})
                            else:
                                gate={"eligible":True,"side":candidate.side.upper(),"model_sha256":model_sha,
                                      "paper_only":mode=="shadow","demo_only":mode=="demo",
                                      "reason":"EXECUTION_TEST_NOT_PROFITABILITY_APPROVAL"}
                            signals.append({"symbol":symbol,"side":candidate.side.upper(),"stop":candidate.guard,
                                            "signal_ts":candidate.closed_ms,"signal_id":candidate.event_id,
                                            "entry":candidate.metadata["signal_close"],"probability":gate,
                                            "setup_meta":candidate.metadata})
                    except (SafetyError,ValueError,KeyError,TypeError,OSError) as exc:
                        errors.append((symbol,type(exc).__name__))
                        market_status[symbol]={"at":decision_time,"bias":{},"eligible_setup":False,
                                               "wait_reasons":["FRAME_DATA_UNAVAILABLE"]}
                store.set("cci_chop_market_status",market_status)
                try:
                    # Existing positions are still reconciled if candles or probability fail.
                    engine.tick(now_ms(),signals=signals if not errors else [],dynamic_stops=trails,exits=exits)
                except (SafetyError,ValueError,KeyError,TypeError,OSError) as exc:
                    errors.append(("ACCOUNT",type(exc).__name__))
                if at>=next_summary:
                    notifier.queue({**summary_event(engine,authorized() and not errors),"markets":market_status},
                        key=f"SUMMARY:{mode}:{at//(c['summary_hours']*3600000)}")
                    if errors:
                        notifier.queue({"kind":"ERROR","at":at,"reason":"MARKET_OR_ACCOUNT_READ_FAILED"},key=f"ERROR:{mode}:{at//21600000}")
                    next_summary=at+int(c["summary_hours"]*3600000)
                    store.set("cci_chop_next_summary",next_summary)
                store.set("cci_chop_last_observation",{"at":now_ms(),"errors":errors,
                            "entry_enabled":authorized() and not errors and not engine.state.get("halt")
                                and not engine.state.get("risk",{}).get("active_blocks")})
                stop.wait(c["poll_seconds"])
        finally:
            runtime=store.get("cci_chop_runtime",{})
            if runtime:
                store.set("cci_chop_runtime",{**runtime,"running":False,"stopped_ms":now_ms()})
                if notifier.thread.is_alive():
                    reason="SHADOW_WEEK_COMPLETE" if mode=="shadow" and store.get("cci_chop_shadow_end") else "PROCESS_STOPPED"
                    notifier.queue({"kind":"STOPPED","at":now_ms(),"reason":reason})
            notifier.close()
            store.close()


def validate_account_settings(settings,c):
    if not isinstance(settings,dict) or settings.get("accountMode")!="unified" or settings.get("holdMode") not in ("one_way_mode","hedge_mode"):
        raise Blocked("UNIFIED_ACCOUNT_AND_HOLD_MODE_REQUIRED")
    for symbol in c["symbols"]:
        rows=[r for r in settings.get("symbolConfigList",[]) if r.get("category")=="USDT-FUTURES" and r.get("symbol")==symbol]
        if not rows or any(r.get("marginMode")!="crossed" or float(r.get("leverage",0))!=c["leverage"] for r in rows):
            raise Blocked("MANUALLY_SET_CROSSED_AND_CONFIGURED_LEVERAGE:"+symbol)


def validate_market_execution(api,c):
    for symbol in c["symbols"]:
        api.instrument(symbol)
        q=api.quote(symbol); q.validate(now_ms())
        if 10000*(q.ask-q.bid)/((q.ask+q.bid)/2)>c["max_spread_bps"]:
            raise Blocked("SPREAD_TOO_WIDE:"+symbol)
        fee=api.fee(symbol)
        if not 0<fee<=.002:
            raise Blocked("PERSONAL_FEE_INVALID:"+symbol)


def main(argv=None):
    parser=argparse.ArgumentParser(description="CCI·CHOP 구조추종: 주·일 방향, 4H·1H 구조, 5m 진입, 동결 시간봉 필터")
    parser.add_argument("command",choices=("init","doctor","arm","pause","resume-shadow","run","status","notify-now","notify-test"))
    parser.add_argument("--root",type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument("--mode",choices=("shadow","demo","live"),default="shadow")
    parser.add_argument("--demo-env",type=Path)
    parser.add_argument("--allow-unvalidated-live",action="store_true",help="명시적으로 수익성 미승인 실전 실험을 허용 (arm 전용)")
    args=parser.parse_args(argv);root=args.root.resolve()
    try:
        c=config.load(root)
        if args.allow_unvalidated_live and args.command!="arm":
            raise Blocked("EXPERIMENTAL_ACK_IS_ARM_ONLY")
        if args.command=="init":
            if not (root/"cci_chop_config.json").exists():
                atomic(root/"cci_chop_config.json",c)
            print("다중 시간봉 설정을 준비했습니다. API·텔레그램 키는 기존 로컬 설정을 사용합니다.")
        elif args.command=="pause":
            state_dir(root).mkdir(parents=True,exist_ok=True)
            atomic(state_dir(root)/"PAUSE",{"at":now_ms()})
            (state_dir(root)/"ARM.json").unlink(missing_ok=True)
            print("신규 진입을 중지했습니다. 실행 중인 기존 포지션 관리는 계속됩니다.")
        elif args.command=="resume-shadow":
            (state_dir(root)/"PAUSE").unlink(missing_ok=True)
            print("쉐도우·데모의 진입 중지를 해제했습니다. 실매매는 별도 arm이 필요합니다.")
        elif args.command=="status":
            print(json.dumps(read_status(root,args.mode),ensure_ascii=False,indent=2))
        elif args.command=="notify-test":
            telegram_send(telegram_credentials(connection(root)["env"]),render({"kind":"START","reason":"TELEGRAM_TEST"},args.mode))
            print("설정된 단일 텔레그램 대화에 한국어 테스트 알림 한 건을 보냈습니다.")
        elif args.command=="notify-now":
            status=read_status(root,args.mode)
            ev=status_summary(status,(state_dir(root)/"PAUSE").exists())
            telegram_send(telegram_credentials(connection(root)["env"]),render(ev,args.mode))
            print("설정된 텔레그램 대화에 다중 시간봉 현황 한 건을 보냈습니다.")
        elif args.command=="doctor":
            api=make_api(root,args.mode,demo_env=args.demo_env)
            result={"mode":args.mode,"real_orders_sent_by_this_command":0,"symbols":{},"live_release_ready":False}
            market=MarketFrames(api)
            model,_=load_model(root)
            experimental_check(model)
            result["experimental_policy_available"]=True
            result["profitability_approved"]=False
            result["selected_entry_rule"]=strategy.ENTRY_RULE
            try:release_check(model);result["live_release_ready"]=True
            except SafetyError as exc:result["release_block"]=str(exc)
            for symbol in c["symbols"]:
                inst=api.instrument(symbol);q=api.quote(symbol);q.validate(now_ms())
                result["symbols"][symbol]={"price_step":inst.price_step,"quantity_step":inst.qty_step,"personal_taker_fee":api.fee(symbol)}
                decision_time=now_ms()-60000
                frames=market.get(symbol,decision_time)
                analysis=strategy.analyze(frames,decision_time)
                result["symbols"][symbol].update({"completed_frames":{frame:len(rows) for frame,rows in frames.items() if isinstance(rows,list)},
                    "indicators":analysis.get("indicators",{}),
                    "large_direction":{frame:analysis["bias"][frame]["direction"] for frame in ("W","D")},
                    "entry_setup_eligible":analysis["eligible"],"setup_wait_reasons":analysis["wait_reasons"]})
            if args.mode != "shadow":
                validate_account_settings(api.settings(),c)
                result["execution_settings_ready"]=True
            result["telegram_configured"]=bool(telegram_credentials(connection(root)["env"]))
            print(json.dumps(result,ensure_ascii=False,indent=2))
        elif args.command=="arm":
            model,model_sha=load_model(root)
            experimental=args.allow_unvalidated_live
            experimental_check(model) if experimental else release_check(model)
            legacy_guard(root)
            api=make_api(root,"live")
            with single_runner(root):
                store=Store(db_path(root,"live"))
                try:
                    uid=account.identity(api,store);snap=api.inventory();account.require_flat(snap)
                    validate_account_settings(snap["settings"],c)
                    available=account.available_usdt(snap["assets"])
                    if available<=c["margin_reserve_usdt"]:
                        raise Blocked("AVAILABLE_MARGIN_BELOW_RESERVE")
                    validate_market_execution(api,c)
                    if any(store.get("cci_chop_engine",{}).get(k) for k in ("positions","pending","modifications")):
                        raise Blocked("LOCAL_POSITION_OR_ORDER_UNRESOLVED")
                    (state_dir(root)/"PAUSE").unlink(missing_ok=True)
                    atomic(state_dir(root)/"ARM.json",arm_payload(root,c,model_sha,uid,experimental))
                    os.chmod(state_dir(root)/"ARM.json",0o600)
                finally:store.close()
            print("동결 코드·설정·증거·계정에 신규 진입 권한을 연결했습니다. 주문은 아직 보내지 않았습니다.")
            if experimental: print("미검증 실전 실험: 수익성·미래 검증·실제 보호 주문 검증은 승인되지 않았습니다.")
        else:
            run(root,args.mode,c,args.demo_env)
        return 0
    except (SafetyError,ValueError,KeyError,TypeError,OSError,sqlite3.Error) as exc:
        print("BLOCKED: "+(str(exc) if isinstance(exc,SafetyError) else type(exc).__name__))
        print("미확정 주문·포지션·손실 상태를 지우지 마세요.")
        return 2


if __name__=="__main__":
    raise SystemExit(main())
