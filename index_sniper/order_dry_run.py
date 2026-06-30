from __future__ import annotations

import json
import time
from dataclasses import asdict

from index_sniper.config import Settings
from index_sniper.exchange.bitget_uta import BitgetUTAClient, OrderIntent
from index_sniper.risk.sizing import build_size_plan, extract_instrument, extract_symbol_config, extract_usdt_equity_available
from index_sniper.telegram.bot import TelegramBot


def _short(data: object, limit: int = 4000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text[:limit] + ("..." if len(text) > limit else "")


def run_dry_order_check(settings: Settings, client: BitgetUTAClient, tg: TelegramBot) -> list[dict]:
    if not settings.dry_run:
        msg = "DRY_RUN=false 상태에서는 v0.3 dry-order를 실행하지 않습니다."
        tg.send(f"🛑 <b>dry-order 중단</b>\n{msg}")
        raise RuntimeError(msg)

    assets = client.assets()
    settings_response = client.settings()
    equity, available = extract_usdt_equity_available(assets)
    symbol_count = len(settings.symbols)
    reports: list[dict] = []
    warnings: list[str] = []

    tg.send(
        "🧪 <b>Index Sniper Pro v0.3</b>\n"
        "모드: DRY_ORDER\n"
        "실주문 없음\n"
        f"대상: {', '.join(settings.symbols)}\n"
        f"USDT available: {available:.4f}\n"
        f"사용비율: {settings.capital_ratio * 100:.2f}%\n"
        f"목표 레버리지: {settings.leverage}x"
    )

    for symbol in settings.symbols:
        item: dict = {"symbol": symbol}
        try:
            price = client.last_price(symbol, settings.category)
            instruments = client.instruments(symbol, settings.category)
            instrument = extract_instrument(instruments, symbol)
            positions = client.current_position(symbol, settings.category)
            sym_cfg = extract_symbol_config(settings_response, symbol) or {}
            current_leverage = int(sym_cfg.get("leverage") or 0) if sym_cfg else None
            current_margin_mode = sym_cfg.get("marginMode") if sym_cfg else None
            leverage_ok = current_leverage == settings.leverage
            margin_mode_ok = current_margin_mode == settings.margin_mode
            if not leverage_ok:
                warnings.append(f"{symbol}: 현재 레버리지 {current_leverage}, 목표 {settings.leverage}")
            if not margin_mode_ok:
                warnings.append(f"{symbol}: 현재 마진모드 {current_margin_mode}, 목표 {settings.margin_mode}")

            size_plan = build_size_plan(
                equity=equity,
                available=available,
                symbol_count=symbol_count,
                capital_ratio=settings.capital_ratio,
                leverage=settings.leverage,
                price=price,
                instrument=instrument,
            )

            oid = str(int(time.time() * 1000))[-10:]
            long_open = client.place_order(OrderIntent(symbol=symbol, side="buy", pos_side="long", qty=size_plan.final_qty, category=settings.category, margin_coin=settings.margin_coin, margin_mode=settings.margin_mode, client_oid=f"drylo-{symbol}-{oid}"), dry_run=True)
            long_close = client.place_order(OrderIntent(symbol=symbol, side="sell", pos_side="long", qty=size_plan.final_qty, category=settings.category, margin_coin=settings.margin_coin, margin_mode=settings.margin_mode, reduce_only=True, client_oid=f"drylc-{symbol}-{oid}"), dry_run=True)
            short_open = client.place_order(OrderIntent(symbol=symbol, side="sell", pos_side="short", qty=size_plan.final_qty, category=settings.category, margin_coin=settings.margin_coin, margin_mode=settings.margin_mode, client_oid=f"dryso-{symbol}-{oid}"), dry_run=True)
            short_close = client.place_order(OrderIntent(symbol=symbol, side="buy", pos_side="short", qty=size_plan.final_qty, category=settings.category, margin_coin=settings.margin_coin, margin_mode=settings.margin_mode, reduce_only=True, client_oid=f"drysc-{symbol}-{oid}"), dry_run=True)

            item.update({
                "price": price,
                "current_leverage": current_leverage,
                "target_leverage": settings.leverage,
                "leverage_ok": leverage_ok,
                "current_margin_mode": current_margin_mode,
                "target_margin_mode": settings.margin_mode,
                "margin_mode_ok": margin_mode_ok,
                "instrument": {
                    "minOrderQty": instrument.get("minOrderQty"),
                    "minOrderAmount": instrument.get("minOrderAmount"),
                    "quantityPrecision": instrument.get("quantityPrecision"),
                    "quantityMultiplier": instrument.get("quantityMultiplier"),
                    "maxMarketOrderQty": instrument.get("maxMarketOrderQty"),
                },
                "size_plan": asdict(size_plan),
                "position_ok": client.is_success(positions),
                "long_open_payload": long_open,
                "long_close_payload": long_close,
                "short_open_payload": short_open,
                "short_close_payload": short_close,
            })
        except Exception as exc:
            item["error"] = str(exc)
        reports.append(item)

    print("===== DRY ORDER PLAN v0.3 =====")
    print(_short(reports, 20000))

    failed = [r["symbol"] for r in reports if "error" in r or not r.get("size_plan", {}).get("valid", False)]
    warning_text = "\n".join(f"- {w}" for w in warnings) if warnings else "없음"
    if failed:
        tg.send(f"⚠️ <b>v0.3 DRY_ORDER 확인 필요</b>\n실패 심볼: {', '.join(failed)}\n레버리지/마진 경고:\n{warning_text}")
    else:
        tg.send(
            "✅ <b>v0.3 DRY_ORDER 성공</b>\n"
            "실주문 없이 10% 시드 기준 수량과 주문 payload 생성 완료\n"
            f"대상: {', '.join(settings.symbols)}\n"
            f"레버리지/마진 경고:\n{warning_text}"
        )
    return reports
