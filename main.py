from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from index_sniper import __version__
from index_sniper.config import load_settings
from index_sniper.exchange.bitget_uta import BitgetUTAClient
from index_sniper.live_micro_test import run_micro_live_test
from index_sniper.order_dry_run import run_dry_order_check
from index_sniper.preflight import run_preflight
from index_sniper.strategy_dry_run import run_strategy_dry
from index_sniper.strategy_executor import run_strategy_exec
from index_sniper.telegram.bot import TelegramBot


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


def mode_strategy_dry() -> None:
    settings, client, tg = make_client_and_tg()
    run_strategy_dry(settings, client, tg)


def mode_strategy_exec() -> None:
    settings, client, tg = make_client_and_tg()
    run_strategy_exec(settings, client, tg)


def mode_strategy_loop_dry() -> None:
    import time
    settings, client, tg = make_client_and_tg()
    tg.send(f"🟢 <b>v{__version__} 전략 드라이 루프 시작</b>\n실주문 없음\n주기: {settings.loop_seconds}초")
    last_heartbeat = 0.0
    while True:
        run_strategy_dry(settings, client, tg)
        now = time.time()
        if now - last_heartbeat >= settings.heartbeat_minutes * 60:
            tg.send(f"❤️ Index Sniper Pro v{__version__} dry loop alive")
            last_heartbeat = now
        time.sleep(max(10, settings.loop_seconds))


def mode_strategy_exec_loop() -> None:
    import time
    settings, client, tg = make_client_and_tg()
    mode = "LIVE" if not settings.dry_run else "DRY"
    tg.send(f"🟢 <b>v{__version__} 전략 실행 루프 시작</b>\n모드: {mode}\n실주문: {'있음' if not settings.dry_run else '없음'}\n주기: {settings.loop_seconds}초")
    last_heartbeat = 0.0
    while True:
        try:
            run_strategy_exec(settings, client, tg)
        except Exception as exc:
            tg.send(f"⚠️ <b>v{__version__} 전략 실행 루프 오류</b>\n{exc}")
        now = time.time()
        if now - last_heartbeat >= settings.strategy_heartbeat_minutes * 60:
            tg.send(f"❤️ Index Sniper Pro v{__version__} strategy-exec-loop alive ({mode})")
            last_heartbeat = now
        time.sleep(max(10, settings.loop_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(description="Index Sniper Pro")
    parser.add_argument("--mode", choices=["check", "order-dry", "preflight", "micro-live-test", "strategy-dry", "strategy-loop-dry", "strategy-exec", "strategy-exec-loop"], default="check")
    args = parser.parse_args()
    if args.mode == "check":
        mode_check()
    elif args.mode == "order-dry":
        mode_order_dry()
    elif args.mode == "preflight":
        mode_preflight()
    elif args.mode == "micro-live-test":
        mode_micro_live_test()
    elif args.mode == "strategy-dry":
        mode_strategy_dry()
    elif args.mode == "strategy-loop-dry":
        mode_strategy_loop_dry()
    elif args.mode == "strategy-exec":
        mode_strategy_exec()
    elif args.mode == "strategy-exec-loop":
        mode_strategy_exec_loop()


if __name__ == "__main__":
    main()
