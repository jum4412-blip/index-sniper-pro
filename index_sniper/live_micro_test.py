from __future__ import annotations

import json
import os
import time
from decimal import Decimal, ROUND_UP
from typing import Any

from index_sniper.config import Settings
from index_sniper.exchange.bitget_uta import BitgetUTAClient, OrderIntent
from index_sniper.position import open_positions
from index_sniper.risk.sizing import extract_instrument, extract_symbol_config
from index_sniper.telegram.bot import TelegramBot

CONFIRM_PHRASE = "I_UNDERSTAND_REAL_ORDER"


def _d(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def _round_up_to_step(value: Decimal, step: Decimal, precision: int) -> Decimal:
    if step > 0:
        units = (value / step).to_integral_value(rounding=ROUND_UP)
        value = units * step
    quant = Decimal("1") if precision <= 0 else Decimal("1") / (Decimal(10) ** precision)
    return value.quantize(quant, rounding=ROUND_UP)


def _micro_qty(price: float, instrument: dict[str, Any], max_notional: Decimal) -> tuple[str, Decimal]:
    price_d = _d(price)
    precision = int(instrument.get("quantityPrecision") or 0)
    step = _d(instrument.get("quantityMultiplier"), "0")
    min_qty = _d(instrument.get("minOrderQty"), "0")
    min_amount = _d(instrument.get("minOrderAmount"), "0")
    max_market_qty = _d(instrument.get("maxMarketOrderQty"), "0")
    qty_from_amount = Decimal("0") if min_amount <= 0 or price_d <= 0 else (min_amount * Decimal("1.03")) / price_d
    raw = max(min_qty, qty_from_amount)
    qty = _round_up_to_step(raw, step, precision)
    if max_market_qty > 0 and qty > max_market_qty:
        raise RuntimeError(f"micro qty {qty} exceeds maxMarketOrderQty {max_market_qty}")
    notional = qty * price_d
    if notional > max_notional:
        raise RuntimeError(f"micro notional {notional} USDT exceeds safety max {max_notional} USDT")
    if min_amount > 0 and notional < min_amount:
        raise RuntimeError(f"micro notional {notional} below minOrderAmount {min_amount}")
    if min_qty > 0 and qty < min_qty:
        raise RuntimeError(f"micro qty {qty} below minOrderQty {min_qty}")
    return str(qty), notional


def _short(data: object, limit: int = 20000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text[:limit] + ("..." if len(text) > limit else "")


def run_micro_live_test(settings: Settings, client: BitgetUTAClient, tg: TelegramBot) -> dict[str, Any]:
    confirm = os.getenv("LIVE_TEST_CONFIRM", "")
    if confirm != CONFIRM_PHRASE:
        raise RuntimeError(
            "LIVE_TEST_CONFIRM is missing. To run the real micro test: "
            f"DRY_RUN=false LIVE_TEST_CONFIRM={CONFIRM_PHRASE} bash run_micro_live_test.sh"
        )
    if settings.dry_run:
        raise RuntimeError("DRY_RUN=true 상태에서는 실제 micro order test를 실행하지 않습니다.")

    symbol = os.getenv("LIVE_TEST_SYMBOL", "BTCUSDT").strip().upper()
    if symbol not in settings.symbols:
        raise RuntimeError(f"LIVE_TEST_SYMBOL {symbol} is not in SYMBOLS: {settings.symbols}")
    max_notional = _d(os.getenv("LIVE_TEST_MAX_NOTIONAL_USDT", "15"), "15")

    price = client.last_price(symbol, settings.category)
    instrument = extract_instrument(client.instruments(symbol, settings.category), symbol)
    sym_cfg = extract_symbol_config(client.settings(), symbol) or {}
    current_leverage = int(sym_cfg.get("leverage") or 0) if sym_cfg else None
    current_margin_mode = sym_cfg.get("marginMode") if sym_cfg else None
    if current_leverage != settings.leverage:
        raise RuntimeError(f"{symbol} leverage mismatch: current={current_leverage}, target={settings.leverage}")
    if current_margin_mode != settings.margin_mode:
        raise RuntimeError(f"{symbol} margin mode mismatch: current={current_margin_mode}, target={settings.margin_mode}")

    before_pos = client.current_position(symbol, settings.category)
    before_open = open_positions(before_pos, symbol=symbol)
    if before_open:
        raise RuntimeError(f"{symbol} already has open position; refusing micro test: {before_open}")

    qty, notional = _micro_qty(price, instrument, max_notional)
    oid = str(int(time.time() * 1000))[-10:]
    open_intent = OrderIntent(
        symbol=symbol,
        side="buy",
        pos_side="long",
        qty=qty,
        category=settings.category,
        margin_coin=settings.margin_coin,
        margin_mode=settings.margin_mode,
        client_oid=f"micro-open-{symbol}-{oid}",
    )
    close_intent = OrderIntent(
        symbol=symbol,
        side="sell",
        pos_side="long",
        qty=qty,
        category=settings.category,
        margin_coin=settings.margin_coin,
        margin_mode=settings.margin_mode,
        reduce_only=True,
        client_oid=f"micro-close-{symbol}-{oid}",
    )

    tg.send(
        "🚨 <b>실주문 MICRO TEST 시작</b>\n"
        f"symbol: {symbol}\n"
        f"qty: {qty}\n"
        f"approx notional: {notional:.6f} USDT\n"
        "시장가 LONG 진입 후 즉시 hedge-mode 청산 시도"
    )

    result: dict[str, Any] = {
        "symbol": symbol,
        "qty": qty,
        "price_at_start": price,
        "approx_notional": str(notional),
        "current_leverage": current_leverage,
        "current_margin_mode": current_margin_mode,
    }
    opened = False
    try:
        open_res = client.place_order(open_intent, dry_run=False)
        result["open_order"] = open_res
        opened = client.is_success(open_res)
        if not opened:
            raise RuntimeError(f"open order failed: {open_res}")
        time.sleep(2)
        close_res = client.place_order(close_intent, dry_run=False)
        result["close_order"] = close_res
        if not client.is_success(close_res):
            raise RuntimeError(f"close order failed: {close_res}")
        time.sleep(2)
        after_pos = client.current_position(symbol, settings.category)
        result["after_open_positions"] = open_positions(after_pos, symbol=symbol)
        tg.send(
            "✅ <b>MICRO TEST 완료</b>\n"
            f"symbol: {symbol}\n"
            f"qty: {qty}\n"
            f"approx notional: {notional:.6f} USDT\n"
            f"남은 포지션 수: {len(result['after_open_positions'])}"
        )
    except Exception as exc:
        result["error"] = str(exc)
        if opened:
            try:
                emergency = client.place_order(close_intent, dry_run=False)
                result["emergency_close_order"] = emergency
                tg.send(f"🚑 <b>긴급 청산 시도</b>\n{symbol} {qty}\n결과: {emergency}")
            except Exception as close_exc:
                result["emergency_close_error"] = str(close_exc)
                tg.send(f"🛑 <b>긴급 청산 실패</b>\n{symbol} {qty}\n즉시 Bitget 앱에서 포지션 확인 필요\n{close_exc}")
        tg.send(f"⚠️ <b>MICRO TEST 실패/확인 필요</b>\n{symbol}\n{exc}")
    print("===== MICRO LIVE TEST v1.2 HOTFIX =====")
    print(_short(result, 30000))
    return result
