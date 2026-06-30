from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from index_sniper.config import Settings, load_settings
from index_sniper.exchange.bitget_uta import BitgetUTAClient
from index_sniper.orders.planner import build_dry_order_plan
from index_sniper.risk.extract import extract_usdt_equity, first_instrument
from index_sniper.telegram.bot import TelegramBot


VERSION = "v0.2"


def short_json(data: Any, limit: int = 1200) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text[:limit] + ("..." if len(text) > limit else "")


def make_client(settings: Settings) -> BitgetUTAClient:
    return BitgetUTAClient(
        api_key=settings.bitget_api_key,
        secret_key=settings.bitget_secret_key,
        passphrase=settings.bitget_passphrase,
    )


def make_tg(settings: Settings) -> TelegramBot:
    return TelegramBot(settings.telegram_token, settings.telegram_chat_id)


def mode_check(settings: Settings, client: BitgetUTAClient, tg: TelegramBot) -> int:
    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    tg.send(
        f"🚀 <b>Index Sniper Pro {VERSION}</b>\n"
        f"모드: CHECK\n"
        f"시작: {started}\n"
        f"DRY_RUN: {settings.dry_run}\n"
        f"Symbols: {', '.join(settings.symbols)}"
    )

    results: dict[str, Any] = {}
    for name, fn in [
        ("account_info", client.account_info),
        ("assets", client.assets),
        ("settings", client.settings),
    ]:
        try:
            results[name] = fn()
        except Exception as e:
            results[f"{name}_error"] = str(e)

    for symbol in settings.symbols:
        try:
            results[f"ticker_{symbol}"] = client.ticker(symbol, settings.category)
        except Exception as e:
            results[f"ticker_{symbol}_error"] = str(e)
        try:
            results[f"positions_{symbol}"] = client.positions(settings.category, symbol)
        except Exception as e:
            results[f"positions_{symbol}_error"] = str(e)

    print("===== CHECK RESULT =====")
    print(short_json(results, 5000))

    ok_account = client.ok(results.get("account_info", {}))
    ok_assets = client.ok(results.get("assets", {}))
    equity = extract_usdt_equity(results.get("assets", {}))
    if ok_account and ok_assets:
        msg = "✅ <b>v0.2 CHECK 성공</b>\n계정/자산 조회 정상"
        if equity is not None:
            msg += f"\nUSDT equity/available 추정: {equity:,.4f}"
        tg.send(msg)
        return 0

    tg.send("⚠️ <b>v0.2 CHECK 확인 필요</b>\n터미널 출력의 오류 메시지를 확인하세요.")
    return 1


def mode_dry_order(settings: Settings, client: BitgetUTAClient, tg: TelegramBot) -> int:
    if not settings.dry_run:
        print("ABORT: DRY_RUN=false 상태에서는 dry-order 모드를 실행하지 않습니다.")
        tg.send("🛑 <b>dry-order 중단</b>\nDRY_RUN=false 상태입니다. 안전을 위해 중단했습니다.")
        return 2

    tg.send(
        f"🧪 <b>Index Sniper Pro {VERSION}</b>\n"
        "모드: DRY_ORDER\n"
        "실주문 없음. 주문 payload 생성/검증만 진행합니다."
    )

    summary: list[dict[str, Any]] = []
    for symbol in settings.symbols:
        item: dict[str, Any] = {"symbol": symbol}
        try:
            ticker = client.ticker(symbol, settings.category)
            positions = client.positions(settings.category, symbol)
            instruments = client.instruments(settings.category, symbol)
            instrument = first_instrument(instruments, symbol)
            last_price = client.ticker_last_price(symbol, settings.category)
            plan = build_dry_order_plan(
                symbol=symbol,
                category=settings.category,
                margin_coin=settings.margin_coin,
                margin_mode=settings.margin_mode,
                qty=settings.dry_test_qty,
            )
            item.update(
                {
                    "last_price": last_price,
                    "instrument": instrument,
                    "positions_ok": client.ok(positions),
                    "ticker_ok": client.ok(ticker),
                    "instruments_ok": client.ok(instruments),
                    "long_open_payload": plan.long_open_payload,
                    "long_close_payload": plan.long_close_payload,
                    "short_open_payload": plan.short_open_payload,
                    "short_close_payload": plan.short_close_payload,
                }
            )
        except Exception as e:
            item["error"] = str(e)
        summary.append(item)

    print("===== DRY ORDER PLAN =====")
    print(short_json(summary, 8000))

    failed = [x for x in summary if "error" in x]
    if failed:
        tg.send(f"⚠️ <b>DRY_ORDER 일부 실패</b>\n실패 심볼: {', '.join(x['symbol'] for x in failed)}")
        return 1

    tg.send(
        "✅ <b>DRY_ORDER 성공</b>\n"
        "실주문 없이 주문 payload 생성 완료\n"
        f"테스트 수량: {settings.dry_test_qty}\n"
        f"심볼: {', '.join(settings.symbols)}"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Index Sniper Pro")
    parser.add_argument("--mode", choices=["check", "dry-order"], default="check")
    args = parser.parse_args()

    settings = load_settings()
    client = make_client(settings)
    tg = make_tg(settings)

    if args.mode == "check":
        raise SystemExit(mode_check(settings, client, tg))
    if args.mode == "dry-order":
        raise SystemExit(mode_dry_order(settings, client, tg))


if __name__ == "__main__":
    main()
