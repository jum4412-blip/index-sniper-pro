from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from index_sniper.config import Settings
from index_sniper.event_log import append_jsonl, append_trade_csv
from index_sniper.exchange.bitget_uta import BitgetUTAClient, OrderIntent
from index_sniper.position import open_positions
from index_sniper.risk.sizing import build_size_plan, extract_instrument, extract_symbol_config, extract_usdt_equity_available
from index_sniper.state import StrategyState, utc_day
from index_sniper.strategy.breakout import build_breakout_signal_adaptive
from index_sniper.strategy.indicators import parse_candles
from index_sniper.telegram.bot import TelegramBot
from index_sniper.utils.formatting import format_price

CONFIRM_PHRASE = "I_UNDERSTAND_AUTO_TRADING"


def _short(data: object, limit: int = 50000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text[:limit] + ("..." if len(text) > limit else "")


def _fmt_price(x: float | str | None) -> str:
    if x is None:
        return "-"
    try:
        f = float(x)
    except Exception:
        return str(x)
    if abs(f) >= 1000:
        return f"{f:,.2f}"
    if abs(f) >= 1:
        return f"{f:.4f}"
    return f"{f:.8f}"


def _signal_for_symbol(settings: Settings, client: BitgetUTAClient, symbol: str):
    price = client.last_price(symbol, settings.category)
    instrument = extract_instrument(client.instruments(symbol, settings.category), symbol)
    daily_response = client.candles(symbol=symbol, category=settings.category, interval=settings.strategy_interval, limit=settings.strategy_candle_limit, candle_type="market")
    daily_candles = parse_candles(daily_response)
    warmup_candles = None
    if settings.adaptive_trend and len(daily_candles) < settings.ema_slow:
        warmup_response = client.candles(symbol=symbol, category=settings.category, interval=settings.warmup_trend_interval, limit=settings.warmup_trend_candle_limit, candle_type="market")
        warmup_candles = parse_candles(warmup_response)
    signal = build_breakout_signal_adaptive(
        symbol=symbol,
        daily_candles=daily_candles,
        trend_candles=warmup_candles,
        current_price=price,
        k_value=settings.k_value,
        ema_fast_period=settings.ema_fast,
        ema_slow_period=settings.ema_slow,
        atr_period=settings.atr_period,
        atr_stop_mult=settings.atr_stop_mult,
        atr_take_profit_mult=settings.atr_take_profit_mult,
        warmup_trend_interval=settings.warmup_trend_interval,
        warmup_ema_fast=settings.warmup_ema_fast,
        warmup_ema_slow=settings.warmup_ema_slow,
        fallback_ema_fast=settings.fallback_ema_fast,
        fallback_ema_slow=settings.fallback_ema_slow,
        min_atr_period=settings.min_atr_period,
    )
    return signal, price, instrument, daily_candles, warmup_candles


def _make_entry_intent(settings: Settings, symbol: str, signal: Any, qty: str, instrument: dict[str, Any]) -> OrderIntent:
    side = "buy" if signal.signal == "LONG" else "sell"
    pos_side = "long" if signal.signal == "LONG" else "short"
    oid = str(int(time.time() * 1000))[-10:]
    prefix = "v08lo" if signal.signal == "LONG" else "v08so"
    client_oid = f"{prefix}-{symbol.lower()}-{oid}"[:32]
    tp = format_price(signal.take_profit_price, instrument) if settings.use_exchange_tpsl and signal.take_profit_price else None
    sl = format_price(signal.stop_price, instrument) if settings.use_exchange_tpsl and signal.stop_price else None
    return OrderIntent(
        symbol=symbol,
        side=side,
        pos_side=pos_side,
        qty=qty,
        category=settings.category,
        margin_coin=settings.margin_coin,
        margin_mode=settings.margin_mode,
        client_oid=client_oid,
        take_profit=tp,
        stop_loss=sl,
        tp_trigger_by="market",
        sl_trigger_by="market",
        tp_order_type="market",
        sl_order_type="market",
    )


def _global_open_count(settings: Settings, client: BitgetUTAClient) -> int:
    count = 0
    for symbol in settings.symbols:
        try:
            count += len(open_positions(client.current_position(symbol, settings.category), symbol=symbol))
        except Exception:
            count += 999
    return count


def run_strategy_exec(settings: Settings, client: BitgetUTAClient, tg: TelegramBot, notify_policy: str = "always") -> list[dict[str, Any]]:
    live = not settings.dry_run
    if live and settings.strategy_live_confirm != CONFIRM_PHRASE:
        msg = f"DRY_RUN=false이지만 STRATEGY_LIVE_CONFIRM가 없습니다. 실제 자동매매를 하려면 {CONFIRM_PHRASE}가 필요합니다."
        tg.send(f"🛑 <b>v0.7 STRATEGY_EXEC 중단</b>\n{msg}")
        raise RuntimeError(msg)

    assets = client.assets()
    settings_response = client.settings()
    equity, available = extract_usdt_equity_available(assets)
    state = StrategyState(settings.strategy_state_path)
    today = utc_day()
    reports: list[dict[str, Any]] = []
    global_open_before = _global_open_count(settings, client)
    new_entries_this_cycle = 0
    mode_label = "LIVE" if live else "DRY"

    if notify_policy == "always":
        tg.send(
            f"🧠 <b>Index Sniper Pro v0.8 STRATEGY_EXEC {mode_label}</b>\n"
            f"실주문: {'있음' if live else '없음'}\n"
            f"대상: {', '.join(settings.symbols)}\n"
            f"계좌 사용비율: {settings.capital_ratio * 100:.2f}% / 레버리지 {settings.leverage}x\n"
            f"TP/SL preset: {settings.use_exchange_tpsl}\n"
            f"open positions before: {global_open_before} / max {settings.max_open_positions}"
        )

    for symbol in settings.symbols:
        item: dict[str, Any] = {"symbol": symbol, "mode": mode_label, "day": today}
        try:
            price = client.last_price(symbol, settings.category)
            instrument = extract_instrument(client.instruments(symbol, settings.category), symbol)
            positions = client.current_position(symbol, settings.category)
            opens = open_positions(positions, symbol=symbol)
            sym_cfg = extract_symbol_config(settings_response, symbol) or {}
            current_leverage = int(sym_cfg.get("leverage") or 0) if sym_cfg else None
            current_margin_mode = sym_cfg.get("marginMode") if sym_cfg else None
            signal, _, _, daily_candles, warmup_candles = _signal_for_symbol(settings, client, symbol)

            effective_size_multiplier = settings.fallback_size_multiplier if signal.warmup_mode else 1.0
            if signal.warmup_mode and not settings.live_allow_warmup_entries:
                effective_size_multiplier = 0.0
            effective_capital_ratio = settings.capital_ratio * max(0.0, effective_size_multiplier)
            size_plan = build_size_plan(equity=equity, available=available, symbol_count=len(settings.symbols), capital_ratio=effective_capital_ratio, leverage=settings.leverage, price=price, instrument=instrument)
            daily_count = state.entry_count(symbol, today)
            checks = {
                "has_signal": signal.signal in {"LONG", "SHORT"},
                "leverage_ok": current_leverage == settings.leverage,
                "margin_mode_ok": current_margin_mode == settings.margin_mode,
                "no_open_position_for_symbol": len(opens) == 0,
                "size_valid": size_plan.valid,
                "daily_entry_limit_ok": daily_count < settings.max_daily_entries_per_symbol,
                "global_open_limit_ok": global_open_before + new_entries_this_cycle < settings.max_open_positions,
                "cycle_entry_limit_ok": new_entries_this_cycle < settings.max_new_positions_per_cycle,
                "warmup_allowed": (not signal.warmup_mode) or settings.live_allow_warmup_entries,
            }
            action_allowed = all(checks.values())
            order_result = None
            intent_payload = None
            if signal.signal in {"LONG", "SHORT"} and size_plan.valid:
                intent = _make_entry_intent(settings, symbol, signal, size_plan.final_qty, instrument)
                intent_payload = client.build_market_order_payload(intent)
                if action_allowed:
                    order_result = client.place_order(intent, dry_run=settings.dry_run)
                    success = bool(settings.dry_run) or client.is_success(order_result)
                    if success:
                        new_entries_this_cycle += 1
                        event = {
                            "ts": datetime.now(timezone.utc).isoformat(), "mode": mode_label,
                            "symbol": symbol, "signal": signal.signal, "side": intent.side,
                            "pos_side": intent.pos_side, "qty": size_plan.final_qty, "price": price,
                            "stop_loss": intent.stop_loss or "", "take_profit": intent.take_profit or "",
                            "dry_run": settings.dry_run, "client_oid": intent.client_oid or "",
                            "result_code": order_result.get("code", "DRY") if isinstance(order_result, dict) else "",
                            "result_msg": order_result.get("msg", "") if isinstance(order_result, dict) else "",
                        }
                        append_jsonl(settings.log_dir, "events.jsonl", {"type": "strategy_entry", **event, "order_result": order_result})
                        append_trade_csv(settings.log_dir, event)
                        if live:
                            state.record_entry(symbol, {**event, "order_result": order_result}, day=today)
                else:
                    append_jsonl(settings.log_dir, "events.jsonl", {"type": "strategy_signal_blocked", "symbol": symbol, "signal": signal.signal, "checks": checks})

            item.update({
                "price": price, "daily_candles": len(daily_candles), "warmup_candles": len(warmup_candles or []),
                "current_leverage": current_leverage, "target_leverage": settings.leverage,
                "current_margin_mode": current_margin_mode, "target_margin_mode": settings.margin_mode,
                "open_position_count": len(opens), "global_open_before": global_open_before,
                "daily_entries_today": daily_count, "signal": signal.to_dict(),
                "effective_size_multiplier": effective_size_multiplier, "effective_capital_ratio": effective_capital_ratio,
                "size_plan": asdict(size_plan), "checks": checks, "action_allowed": action_allowed,
                "order_payload_if_signal": intent_payload, "order_result": order_result,
            })
        except Exception as exc:
            item["error"] = str(exc)
            append_jsonl(settings.log_dir, "events.jsonl", {"type": "strategy_symbol_error", "symbol": symbol, "error": str(exc)})
        reports.append(item)

    print(f"===== STRATEGY EXEC v0.7 {mode_label} =====")
    print(_short(reports, 50000))

    active = [r for r in reports if r.get("signal", {}).get("signal") in {"LONG", "SHORT"}]
    executed = [r for r in reports if r.get("order_result") is not None and r.get("action_allowed")]
    errors = [r for r in reports if "error" in r]
    blocked = [r for r in active if not r.get("action_allowed")]

    lines = [
        f"✅ <b>v0.8 STRATEGY_EXEC {mode_label} 완료</b>",
        f"실주문: {'있음' if live else '없음'}",
        "Exchange preset TP/SL 포함" if settings.use_exchange_tpsl else "Exchange preset TP/SL 미사용",
    ]
    if errors:
        lines.append("⚠️ 오류: " + ", ".join(r["symbol"] for r in errors))
    if executed:
        lines.append("🔥 실행/예정 주문:")
        for r in executed:
            s = r["signal"]
            payload = r.get("order_payload_if_signal") or {}
            lines.append(f"- {r['symbol']} {s['signal']} qty {r.get('size_plan', {}).get('final_qty')} now {_fmt_price(s.get('current_price'))} SL {payload.get('stopLoss', '-')} TP {payload.get('takeProfit', '-')}")
    elif active:
        lines.append("신호는 있으나 차단됨:")
        for r in blocked:
            bad = [k for k, v in (r.get("checks") or {}).items() if not v]
            lines.append(f"- {r['symbol']} {r.get('signal', {}).get('signal')} blocked: {', '.join(bad)}")
    else:
        lines.append("현재 신호: 없음/HOLD")
    lines.append("요약:")
    for r in reports:
        if "error" in r:
            lines.append(f"- {r['symbol']}: ERROR {r['error'][:100]}")
            continue
        s = r["signal"]
        lines.append(f"- {r['symbol']}: {s['signal']} / {s['reason']} / trend {s.get('trend_mode')} / size x{r.get('effective_size_multiplier')} / now {_fmt_price(s.get('current_price'))}, L {_fmt_price(s.get('long_target'))}, S {_fmt_price(s.get('short_target'))}")
    if notify_policy == "always":
        should_send = True
    else:
        should_send = False
        if errors and settings.notify_error:
            should_send = True
        elif executed and settings.notify_signal:
            should_send = True
        elif blocked and settings.notify_blocked_signal:
            should_send = True
        elif active and settings.notify_signal:
            should_send = True
        elif settings.notify_hold_summary:
            should_send = True

    if should_send:
        tg.send("\n".join(lines[:35]))
    return reports
