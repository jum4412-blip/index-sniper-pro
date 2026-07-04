from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from index_sniper import __version__
from index_sniper.config import load_settings
from index_sniper.exchange.bitget_uta import BitgetUTAClient
from index_sniper.live_micro_test import run_micro_live_test
from index_sniper.emergency_close import run_emergency_close
from index_sniper.order_dry_run import run_dry_order_check
from index_sniper.preflight import run_preflight
from index_sniper.strategy_dry_run import run_strategy_dry
from index_sniper.strategy_executor import run_strategy_exec
from index_sniper.telegram.bot import TelegramBot
from index_sniper.weekend_flat import weekend_flat_window, weekend_flat_human, close_index_positions_if_due
from index_sniper.position_manager import evaluate_positions


def _short(data: object, limit: int = 5000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text[:limit] + ("..." if len(text) > limit else "")


def make_client_and_tg():
    settings = load_settings()
    client = BitgetUTAClient(settings.bitget_api_key, settings.bitget_secret_key, settings.bitget_passphrase)
    tg = TelegramBot(settings.telegram_token, settings.telegram_chat_id)
    return settings, client, tg


def mode_check() -> None:
    settings, client, tg = make_client_and_tg()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    tg.send(f"🚀 <b>Index Sniper Pro v{__version__}</b>\n모드: CHECK\n시작: {started}\nDRY_RUN: {settings.dry_run}\nSymbols: {', '.join(settings.symbols)}")
    result = {}
    for name, fn in {"account_info": client.account_info, "assets": client.assets, "settings": client.settings}.items():
        try:
            result[name] = fn()
        except Exception as exc:
            result[f"{name}_error"] = str(exc)
    for symbol in settings.symbols:
        try:
            result[f"ticker_{symbol}"] = client.tickers(symbol, settings.category)
        except Exception as exc:
            result[f"ticker_{symbol}_error"] = str(exc)
        try:
            result[f"positions_{symbol}"] = client.current_position(symbol, settings.category)
        except Exception as exc:
            result[f"positions_{symbol}_error"] = str(exc)
    print("===== CHECK RESULT =====")
    print(_short(result, 20000))
    errors = [k for k in result if k.endswith("_error")]
    if errors:
        tg.send(f"⚠️ <b>v{__version__} CHECK 확인 필요</b>\n오류 항목: {', '.join(errors)}")
    else:
        tg.send(f"✅ <b>v{__version__} CHECK 성공</b>\n계정/자산/심볼 조회 정상\n대상: {', '.join(settings.symbols)}")


def mode_order_dry() -> None:
    settings, client, tg = make_client_and_tg()
    run_dry_order_check(settings, client, tg)


def mode_preflight() -> None:
    settings, client, tg = make_client_and_tg()
    run_preflight(settings, client, tg)


def mode_micro_live_test() -> None:
    settings, client, tg = make_client_and_tg()
    run_micro_live_test(settings, client, tg)


def mode_emergency_close() -> None:
    settings, client, tg = make_client_and_tg()
    run_emergency_close(settings, client, tg)


def mode_strategy_dry() -> None:
    settings, client, tg = make_client_and_tg()
    run_strategy_dry(settings, client, tg)


def mode_strategy_exec() -> None:
    settings, client, tg = make_client_and_tg()
    run_strategy_exec(settings, client, tg, notify_policy="always")


def _write_loop_status(settings, loop_name: str, status: dict) -> None:
    """Persist loop health so status_sniper.sh can show whether the bot is alive."""
    from pathlib import Path
    import json
    from datetime import datetime, timezone
    root = Path(__file__).resolve().parent
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {"loop": loop_name, "updated_at": datetime.now(timezone.utc).isoformat(), **status}
    tmp = data_dir / "loop_status.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(data_dir / "loop_status.json")


def _append_heartbeat_log(message: str) -> None:
    from pathlib import Path
    from datetime import datetime, timezone
    root = Path(__file__).resolve().parent
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "heartbeat.log").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")


def _send_heartbeat(tg: TelegramBot, text: str) -> None:
    ok = tg.send(text)
    _append_heartbeat_log(("telegram_ok " if ok else "telegram_failed ") + text.replace("\n", " | "))



def mode_weekend_flat_status() -> None:
    settings, client, tg = make_client_and_tg()
    window = weekend_flat_window(settings)
    result = close_index_positions_if_due(settings, client, tg, dry_run=True, window=window)
    print("===== WEEKEND FLAT STATUS =====")
    print(_short({"window": window.to_dict(), "dry_run_close_preview": result}, 20000))
    tg.send(
        f"🧭 <b>v{__version__} WEEKEND FLAT CHECK</b>\n"
        f"{weekend_flat_human(window)}\n"
        f"NY: {window.now_ny}\n"
        f"symbols: {', '.join(window.symbols)}\n"
        f"close_preview_count: {len(result.get('attempted', []))}"
    )


def mode_position_manager() -> None:
    settings, client, tg = make_client_and_tg()
    rows = evaluate_positions(settings, client, tg)
    data = [r.to_dict() for r in rows]
    print("===== POSITION MANAGER =====")
    print(_short(data, 20000))
    if rows:
        lines = [f"🧭 <b>v{__version__} POSITION MANAGER</b>"]
        for r in rows[:5]:
            lines.append(f"- {r.symbol} {r.side.upper()} qty {r.qty} status {r.status} R {r.r_multiple} hold {r.hold_hours}h")
        tg.send("\n".join(lines))
    else:
        tg.send(f"✅ <b>v{__version__} POSITION MANAGER</b>\n현재 열린 포지션 없음")


def mode_strategy_loop_dry() -> None:
    import time
    from datetime import datetime, timezone
    settings, client, tg = make_client_and_tg()
    loop_name = "strategy-loop-dry"
    mode = "DRY"
    cycle = 0
    error_count = 0
    started_at = datetime.now(timezone.utc).isoformat()
    heartbeat_seconds = max(60, settings.strategy_heartbeat_minutes * 60)
    last_heartbeat = 0.0
    _write_loop_status(settings, loop_name, {"mode": mode, "dry_run": True, "cycle": cycle, "started_at": started_at, "status": "starting"})
    if settings.notify_loop_start:
        tg.send(
            f"🟢 <b>v{__version__} 전략 드라이 루프 시작</b>\n"
            f"실주문 없음\n"
            f"대상: {', '.join(settings.symbols)}\n"
            f"주기: {settings.loop_seconds}초\n"
            f"Heartbeat: {settings.strategy_heartbeat_minutes}분\n"
            "알림정책: HOLD 무음 / 신호·오류·heartbeat만 알림"
        )
    # v0.9: startup heartbeat also proves the heartbeat path works.
    if settings.notify_heartbeat:
        _send_heartbeat(tg, f"❤️ <b>Index Sniper Pro v{__version__} heartbeat 시작</b>\n모드: {mode}\n실주문 없음")
        last_heartbeat = time.time()
    while True:
        cycle += 1
        cycle_started = datetime.now(timezone.utc).isoformat()
        last_error = ""
        try:
            reports = run_strategy_exec(settings, client, tg, notify_policy="important")
            signals = [r.get("symbol") for r in reports if r.get("signal", {}).get("signal") in {"LONG", "SHORT"}]
            _write_loop_status(settings, loop_name, {
                "mode": mode, "dry_run": True, "cycle": cycle, "started_at": started_at,
                "last_cycle_at": cycle_started, "last_cycle_ok": True,
                "last_signal_symbols": signals, "error_count": error_count,
                "status": "running",
            })
        except Exception as exc:
            error_count += 1
            last_error = str(exc)
            _write_loop_status(settings, loop_name, {
                "mode": mode, "dry_run": True, "cycle": cycle, "started_at": started_at,
                "last_cycle_at": cycle_started, "last_cycle_ok": False,
                "last_error": last_error, "error_count": error_count,
                "status": "running_with_errors",
            })
            if settings.notify_error:
                tg.send(f"⚠️ <b>v{__version__} 전략 드라이 루프 오류</b>\ncycle {cycle}\n{last_error[:1000]}")
        now = time.time()
        if settings.notify_heartbeat and now - last_heartbeat >= heartbeat_seconds:
            _send_heartbeat(
                tg,
                f"❤️ <b>Index Sniper Pro v{__version__} alive</b>\n"
                f"모드: {mode}\n실주문 없음\ncycle: {cycle}\nerrors: {error_count}\nlast: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )
            last_heartbeat = now
        time.sleep(max(10, settings.loop_seconds))


def mode_strategy_exec_loop() -> None:
    import time
    from datetime import datetime, timezone
    settings, client, tg = make_client_and_tg()
    loop_name = "strategy-exec-loop"
    mode = "LIVE" if not settings.dry_run else "DRY"
    cycle = 0
    error_count = 0
    started_at = datetime.now(timezone.utc).isoformat()
    heartbeat_seconds = max(60, settings.strategy_heartbeat_minutes * 60)
    last_heartbeat = 0.0
    _write_loop_status(settings, loop_name, {"mode": mode, "dry_run": settings.dry_run, "cycle": cycle, "started_at": started_at, "status": "starting"})
    if settings.notify_loop_start:
        tg.send(
            f"🟢 <b>v{__version__} 전략 실행 루프 시작</b>\n"
            f"모드: {mode}\n"
            f"실주문: {'있음' if not settings.dry_run else '없음'}\n"
            f"대상: {', '.join(settings.symbols)}\n"
            f"주기: {settings.loop_seconds}초\n"
            f"Heartbeat: {settings.strategy_heartbeat_minutes}분\n"
            "알림정책: HOLD 무음 / 신호·오류·heartbeat만 알림"
        )
    if settings.notify_heartbeat:
        _send_heartbeat(
            tg,
            f"❤️ <b>Index Sniper Pro v{__version__} heartbeat 시작</b>\n"
            f"모드: {mode}\n실주문: {'있음' if not settings.dry_run else '없음'}"
        )
        last_heartbeat = time.time()
    while True:
        cycle += 1
        cycle_started = datetime.now(timezone.utc).isoformat()
        last_error = ""
        try:
            reports = run_strategy_exec(settings, client, tg, notify_policy="important")
            signals = [r.get("symbol") for r in reports if r.get("signal", {}).get("signal") in {"LONG", "SHORT"}]
            executed = [r.get("symbol") for r in reports if r.get("order_result") is not None and r.get("action_allowed")]
            _write_loop_status(settings, loop_name, {
                "mode": mode, "dry_run": settings.dry_run, "cycle": cycle, "started_at": started_at,
                "last_cycle_at": cycle_started, "last_cycle_ok": True,
                "last_signal_symbols": signals, "last_executed_symbols": executed,
                "error_count": error_count, "status": "running",
            })
        except Exception as exc:
            error_count += 1
            last_error = str(exc)
            _write_loop_status(settings, loop_name, {
                "mode": mode, "dry_run": settings.dry_run, "cycle": cycle, "started_at": started_at,
                "last_cycle_at": cycle_started, "last_cycle_ok": False,
                "last_error": last_error, "error_count": error_count,
                "status": "running_with_errors",
            })
            if settings.notify_error:
                tg.send(f"⚠️ <b>v{__version__} 전략 실행 루프 오류</b>\ncycle {cycle}\n{last_error[:1000]}")
        now = time.time()
        if settings.notify_heartbeat and now - last_heartbeat >= heartbeat_seconds:
            _send_heartbeat(
                tg,
                f"❤️ <b>Index Sniper Pro v{__version__} alive</b>\n"
                f"모드: {mode}\n실주문: {'있음' if not settings.dry_run else '없음'}\ncycle: {cycle}\nerrors: {error_count}\nlast: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )
            last_heartbeat = now
        time.sleep(max(10, settings.loop_seconds))

def main() -> None:
    parser = argparse.ArgumentParser(description="Index Sniper Pro")
    parser.add_argument("--mode", choices=["check", "order-dry", "preflight", "micro-live-test", "emergency-close", "strategy-dry", "strategy-loop-dry", "strategy-exec", "strategy-exec-loop", "weekend-flat-status", "position-manager"], default="check")
    args = parser.parse_args()
    if args.mode == "check":
        mode_check()
    elif args.mode == "order-dry":
        mode_order_dry()
    elif args.mode == "preflight":
        mode_preflight()
    elif args.mode == "micro-live-test":
        mode_micro_live_test()
    elif args.mode == "emergency-close":
        mode_emergency_close()
    elif args.mode == "strategy-dry":
        mode_strategy_dry()
    elif args.mode == "strategy-loop-dry":
        mode_strategy_loop_dry()
    elif args.mode == "strategy-exec":
        mode_strategy_exec()
    elif args.mode == "strategy-exec-loop":
        mode_strategy_exec_loop()
    elif args.mode == "weekend-flat-status":
        mode_weekend_flat_status()
    elif args.mode == "position-manager":
        mode_position_manager()


if __name__ == "__main__":
    main()
