from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import Any

from index_sniper.config import Settings
from index_sniper.exchange.bitget_uta import BitgetUTAClient, OrderIntent
from index_sniper.position import open_positions
from index_sniper.risk.sizing import build_size_plan, extract_instrument, extract_symbol_config, extract_usdt_equity_available
from index_sniper.strategy.breakout import build_breakout_signal
from index_sniper.strategy.indicators import parse_candles
from index_sniper.telegram.bot import TelegramBot


def _short(data: object, limit: int = 30000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text[:limit] + ("..." if len(text) > limit else "")


def _fmt_price(x: float | None) -> str:
    if x is None:
        return "-"
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:,.4f}"
    return f"{x:.8f}"


def run_strategy_dry(settings: Settings, client: BitgetUTAClient, tg: TelegramBot) -> list[dict[str, Any]]:
    if not settings.dry_run:
        msg = "strategy-dry는 DRY_RUN=true에서만 실행합니다."
        tg.send(f"🛑 <b>v0.5 STRATEGY_DRY 중단</b>\n{msg}")
        raise RuntimeError(msg)

    assets = client.assets()
    settings_response = client.settings()
    equity, available = extract_usdt_equity_available(assets)
    reports: list[dict[str, Any]] = []

    tg.send(
        "🧠 <b>Index Sniper Pro v0.5 STRATEGY_DRY</b>\n"
        "실주문 없음\n"
        f"대상: {', '.join(settings.symbols)}\n"
        f"interval: {settings.strategy_interval}, K: {settings.k_value}\n"
        f"EMA: {settings.ema_fast}/{settings.ema_slow}, ATR: {settings.atr_period}\n"
        f"USDT available: {available:.4f}, 사용비율: {settings.capital_ratio * 100:.2f}%"
    )

    for symbol in settings.symbols:
        item: dict[str, Any] = {"symbol": symbol}
        try:
            price = client.last_price(symbol, settings.category)
            instrument = extract_instrument(client.instruments(symbol, settings.category), symbol)
            positions = client.current_position(symbol, settings.category)
            opens = open_positions(positions, symbol=symbol)
            sym_cfg = extract_symbol_config(settings_response, symbol) or {}
            current_leverage = int(sym_cfg.get("leverage") or 0) if sym_cfg else None
            current_margin_mode = sym_cfg.get("marginMode") if sym_cfg else None

            candles_response = client.candles(symbol=symbol, category=settings.category, interval=settings.strategy_interval, limit=settings.strategy_candle_limit, candle_type="market")
            candles = parse_candles(candles_response)
            signal = build_breakout_signal(
                symbol=symbol,
                candles=candles,
                current_price=price,
                k_value=settings.k_value,
                ema_fast_period=settings.ema_fast,
                ema_slow_period=settings.ema_slow,
                atr_period=settings.atr_period,
                atr_stop_mult=settings.atr_stop_mult,
                atr_take_profit_mult=settings.atr_take_profit_mult,
            )
            size_plan = build_size_plan(equity=equity, available=available, symbol_count=len(settings.symbols), capital_ratio=settings.capital_ratio, leverage=settings.leverage, price=price, instrument=instrument)
            action_allowed = signal.signal in {"LONG", "SHORT"} and current_leverage == settings.leverage and current_margin_mode == settings.margin_mode and len(opens) == 0 and size_plan.valid
            payload = None
            if signal.signal == "LONG":
                oid = str(int(time.time() * 1000))[-10:]
                payload = client.place_order(OrderIntent(symbol=symbol, side="buy", pos_side="long", qty=size_plan.final_qty, category=settings.category, margin_coin=settings.margin_coin, margin_mode=settings.margin_mode, client_oid=f"siglo-{symbol}-{oid}"), dry_run=True)
            elif signal.signal == "SHORT":
                oid = str(int(time.time() * 1000))[-10:]
                payload = client.place_order(OrderIntent(symbol=symbol, side="sell", pos_side="short", qty=size_plan.final_qty, category=settings.category, margin_coin=settings.margin_coin, margin_mode=settings.margin_mode, client_oid=f"sigso-{symbol}-{oid}"), dry_run=True)
            item.update({
                "price": price,
                "candles": len(candles),
                "current_leverage": current_leverage,
                "target_leverage": settings.leverage,
                "leverage_ok": current_leverage == settings.leverage,
                "current_margin_mode": current_margin_mode,
                "target_margin_mode": settings.margin_mode,
                "margin_mode_ok": current_margin_mode == settings.margin_mode,
                "open_position_count": len(opens),
                "signal": signal.to_dict(),
                "size_plan": asdict(size_plan),
                "action_allowed": action_allowed,
                "dry_order_payload_if_signal": payload,
            })
        except Exception as exc:
            item["error"] = str(exc)
        reports.append(item)

    print("===== STRATEGY DRY v0.5 =====")
    print(_short(reports, 40000))

    errors = [r for r in reports if "error" in r]
    active = [r for r in reports if r.get("signal", {}).get("signal") in {"LONG", "SHORT"}]
    lines = ["✅ <b>v0.5 STRATEGY_DRY 완료</b>", "실주문 없음"]
    if errors:
        lines.append("⚠️ 오류 심볼: " + ", ".join(r["symbol"] for r in errors))
    if active:
        lines.append("🔥 신호 발생:")
        for r in active:
            s = r["signal"]
            lines.append(f"- {r['symbol']} {s['signal']} qty {r.get('size_plan', {}).get('final_qty')} price {_fmt_price(s.get('current_price'))} SL {_fmt_price(s.get('stop_price'))} TP {_fmt_price(s.get('take_profit_price'))} allowed={r.get('action_allowed')}")
    else:
        lines.append("현재 신호: 없음/HOLD")
    lines.append("요약:")
    for r in reports:
        if "error" in r:
            lines.append(f"- {r['symbol']}: ERROR {r['error'][:80]}")
            continue
        s = r["signal"]
        lines.append(f"- {r['symbol']}: {s['signal']} / {s['reason']} / now {_fmt_price(s['current_price'])}, L {_fmt_price(s['long_target'])}, S {_fmt_price(s['short_target'])}")
    tg.send("\n".join(lines[:30]))
    return reports
