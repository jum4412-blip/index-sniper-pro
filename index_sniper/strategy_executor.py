from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Any

from index_sniper.config import Settings
from index_sniper.event_log import append_jsonl, append_trade_csv
from index_sniper.exchange.bitget_uta import BitgetUTAClient, OrderIntent
from index_sniper.position import open_positions
from index_sniper.risk.equity_guard import check_daily_equity_guard
from index_sniper.risk.sizing import build_size_plan, extract_instrument, extract_symbol_config, extract_usdt_equity_available
from index_sniper.state import StrategyState, utc_day
from index_sniper.strategy.breakout import BreakoutSignal, build_breakout_signal_adaptive
from index_sniper.strategy.indicators import parse_candles
from index_sniper.strategy.external_data import fetch_external_daily_for_symbol
from index_sniper.telegram.bot import TelegramBot
from index_sniper.utils.formatting import format_price

CONFIRM_PHRASE = "I_UNDERSTAND_AUTO_TRADING"
START_PHRASE = "START_LIVE_INDEX_SNIPER"


def _live_safety_errors(settings: Settings) -> list[str]:
    errors: list[str] = []
    allowed = set(settings.allowed_live_symbols)
    symbols = set(settings.symbols)
    if not settings.live_trading_enabled:
        errors.append("LIVE_TRADING_ENABLED=true is required")
    if settings.live_start_confirm != START_PHRASE:
        errors.append(f"LIVE_START_CONFIRM={START_PHRASE} is required")
    if not symbols.issubset(allowed):
        errors.append(f"SYMBOLS contains non-allowed live symbols: {sorted(symbols - allowed)}")
    if settings.capital_ratio > settings.max_live_capital_ratio:
        errors.append(f"CAPITAL_RATIO {settings.capital_ratio:.4f} > MAX_LIVE_CAPITAL_RATIO {settings.max_live_capital_ratio:.4f}")
    if settings.leverage != 5:
        errors.append(f"LEVERAGE must be 5 for live mode, got {settings.leverage}")
    if settings.max_new_positions_per_cycle > 1:
        errors.append("MAX_NEW_POSITIONS_PER_CYCLE must be <= 1")
    if settings.max_daily_entries_per_symbol > 1:
        errors.append("MAX_DAILY_ENTRIES_PER_SYMBOL must be <= 1")
    if settings.risk_profile == "SURVIVAL":
        if settings.max_open_positions > settings.survival_max_live_open_positions:
            errors.append(
                f"SURVIVAL requires MAX_OPEN_POSITIONS <= {settings.survival_max_live_open_positions}, got {settings.max_open_positions}"
            )
        if settings.survival_max_correlated_open > 1:
            errors.append("SURVIVAL requires SURVIVAL_MAX_CORRELATED_OPEN <= 1")
        if settings.max_daily_loss_pct > 1.5:
            errors.append("SURVIVAL recommends MAX_DAILY_LOSS_PCT <= 1.5")
    return errors


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
    """Build a signal for one Bitget symbol.

    v1.5 external signal rule:
    - BTCUSDT keeps using Bitget candles.
    - SP500USDT/NDX100USDT can use external futures/index history for trend, ATR, and Larry breakout levels.
    - Bitget price is still used as the final execution price. External OHLC is scaled to Bitget price level.
    """
    price = client.last_price(symbol, settings.category)
    instrument = extract_instrument(client.instruments(symbol, settings.category), symbol)

    external_symbols = {s.upper() for s in settings.external_signal_symbols}
    if settings.external_signal_enabled and symbol.upper() in external_symbols:
        try:
            external = fetch_external_daily_for_symbol(
                symbol=symbol,
                bitget_price=price,
                provider_order=settings.external_provider_order,
                yahoo_map=settings.external_yahoo_symbol_map,
                stooq_map=settings.external_stooq_symbol_map,
                yahoo_range=settings.external_yahoo_range,
                yahoo_interval=settings.external_yahoo_interval,
                timeout=settings.external_timeout_seconds,
                limit=settings.external_candle_limit,
                max_staleness_hours=settings.external_max_staleness_hours,
                max_scale_deviation_pct=settings.external_max_scale_deviation_pct,
            )
            daily_candles = external.candles
            warmup_candles = None
            signal = build_breakout_signal_adaptive(
                symbol=symbol,
                daily_candles=daily_candles,
                trend_candles=None,
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
            signal = replace(
                signal,
                trend_mode=f"EXT_{external.provider}_{signal.trend_mode}",
                trend_interval=f"EXT_{settings.external_yahoo_interval.upper() if external.provider == 'YAHOO' else '1D'}",
                warmup_mode=False,
                data_quality=(
                    f"external_{external.provider}:{external.provider_symbol}"
                    f":scaled_to_bitget:ratio={external.scale_ratio:.8f}"
                    f":age_hours={external.age_hours:.1f}"
                ),
            )
            return signal, price, instrument, daily_candles, warmup_candles
        except Exception as exc:
            # Survival rule: index external data is a hard gate. If long-history data is unavailable,
            # do not raise a symbol error that can spam alerts. Return a HOLD signal and block trading.
            # Orders still require a LONG/SHORT signal, so this cannot create a live order.
            reason = f"external data unavailable; trading disabled for {symbol}: {str(exc)[:300]}"
            signal = BreakoutSignal(
                symbol=symbol,
                signal="HOLD",
                reason=reason,
                current_price=price,
                current_open=price,
                previous_high=price,
                previous_low=price,
                previous_range=0.0,
                long_target=price,
                short_target=price,
                ema_fast=None,
                ema_slow=None,
                atr=None,
                stop_price=None,
                take_profit_price=None,
                trend_mode="EXT_DATA_UNAVAILABLE",
                trend_interval="EXT",
                trend_candle_count=0,
                daily_candle_count=0,
                atr_period_used=None,
                warmup_mode=False,
                data_quality="external_unavailable",
            )
            return signal, price, instrument, [], None

    daily_response = client.candles(
        symbol=symbol,
        category=settings.category,
        interval=settings.strategy_interval,
        limit=settings.strategy_candle_limit,
        candle_type="market",
    )
    daily_candles = parse_candles(daily_response)
    warmup_candles = None
    if settings.adaptive_trend and len(daily_candles) < settings.ema_slow:
        warmup_response = client.candles(
            symbol=symbol,
            category=settings.category,
            interval=settings.warmup_trend_interval,
            limit=settings.warmup_trend_candle_limit,
            candle_type="market",
        )
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
    prefix = "v11lo" if signal.signal == "LONG" else "v11so"
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


def _survival_group_set(settings: Settings) -> set[str]:
    return {s.upper() for s in settings.survival_correlated_group}


def _survival_group_open_count(settings: Settings, client: BitgetUTAClient) -> int:
    group = _survival_group_set(settings)
    count = 0
    for symbol in settings.symbols:
        if symbol not in group:
            continue
        try:
            count += len(open_positions(client.current_position(symbol, settings.category), symbol=symbol))
        except Exception:
            count += 999
    return count


def _breakout_atr_distance(signal: Any) -> float:
    atr = signal.atr or signal.previous_range or max(abs(signal.current_price) * 0.001, 1e-9)
    if atr <= 0:
        return 0.0
    if signal.signal == "LONG":
        return max(0.0, signal.current_price - signal.long_target) / atr
    if signal.signal == "SHORT":
        return max(0.0, signal.short_target - signal.current_price) / atr
    return 0.0


def _trend_gap_score(signal: Any) -> float:
    if signal.ema_fast is None or signal.ema_slow is None:
        return 0.0
    denom = signal.atr or signal.previous_range or max(abs(signal.current_price) * 0.001, 1e-9)
    if denom <= 0:
        return 0.0
    return abs(signal.ema_fast - signal.ema_slow) / denom


def _survival_signal_score(signal: Any, size_multiplier: float) -> float:
    if signal.signal not in {"LONG", "SHORT"}:
        return 0.0
    breakout = _breakout_atr_distance(signal)
    trend_gap = _trend_gap_score(signal)
    quality_mult = 0.70 if signal.warmup_mode else 1.0
    size_mult = max(0.0, float(size_multiplier or 0.0))
    return round(((breakout * 100.0) + (trend_gap * 10.0)) * quality_mult * size_mult, 6)


def _is_correlated_symbol(settings: Settings, symbol: str) -> bool:
    return symbol in set(settings.survival_correlated_group)


def _breakout_atr(signal: BreakoutSignal) -> float:
    if not signal.atr or signal.atr <= 0:
        return 0.0
    if signal.signal == "LONG":
        return max(0.0, (signal.current_price - signal.long_target) / signal.atr)
    if signal.signal == "SHORT":
        return max(0.0, (signal.short_target - signal.current_price) / signal.atr)
    return 0.0


def _signal_score(signal: BreakoutSignal) -> float:
    """Survival score: prefer clean trend + real breakout beyond target.

    This is not a prediction. It is only a ranking tool when more than one symbol fires.
    """
    if signal.signal not in {"LONG", "SHORT"} or not signal.atr or signal.atr <= 0:
        return 0.0
    breakout_component = _breakout_atr(signal) * 10.0
    trend_component = 0.0
    if signal.ema_fast is not None and signal.ema_slow is not None:
        trend_component = min(abs(signal.ema_fast - signal.ema_slow) / signal.atr, 3.0) * 2.0
    range_component = 0.0
    if signal.previous_range > 0:
        range_component = min(signal.previous_range / signal.atr, 3.0) * 0.5
    data_quality_multiplier = 0.70 if signal.warmup_mode else 1.0
    return round((breakout_component + trend_component + range_component) * data_quality_multiplier, 6)


def _select_survival_candidates(settings: Settings, reports: list[dict[str, Any]]) -> set[str]:
    candidates = [
        r for r in reports
        if r.get("candidate_before_selection") and r.get("survival_score", 0.0) >= settings.survival_min_signal_score
    ]
    if not candidates:
        return set()
    if settings.survival_select_best_signal:
        candidates.sort(key=lambda r: (float(r.get("survival_score") or 0.0), 0 if r.get("signal", {}).get("warmup_mode") else 1), reverse=True)
    selected: set[str] = set()
    correlated_selected = 0
    for r in candidates:
        symbol = str(r.get("symbol"))
        if _is_correlated_symbol(settings, symbol):
            if correlated_selected >= settings.survival_max_correlated_open:
                continue
            correlated_selected += 1
        selected.add(symbol)
        if len(selected) >= settings.max_new_positions_per_cycle:
            break
    return selected


def run_strategy_exec(settings: Settings, client: BitgetUTAClient, tg: TelegramBot, notify_policy: str = "always") -> list[dict[str, Any]]:
    """Run one strategy execution cycle.

    v1.5 EXTERNAL/SURVIVAL change:
    - Build every symbol report first.
    - If more than one symbol has a valid signal, choose only the highest-scoring candidate.
    - SP500USDT and NDX100USDT are treated as one correlated US-index group.
    - In SURVIVAL profile, only one correlated index position can be open at a time.
    """
    live = not settings.dry_run
    if live and settings.strategy_live_confirm != CONFIRM_PHRASE:
        msg = f"DRY_RUN=false이지만 STRATEGY_LIVE_CONFIRM가 없습니다. 실제 자동매매를 하려면 {CONFIRM_PHRASE}가 필요합니다."
        tg.send(f"🛑 <b>v1.5 STRATEGY_EXEC 중단</b>\n{msg}")
        raise RuntimeError(msg)
    if live:
        live_errors = _live_safety_errors(settings)
        if live_errors:
            msg = "LIVE 안전 조건 불일치: " + "; ".join(live_errors)
            tg.send(f"🛑 <b>v1.5 LIVE 안전장치 중단</b>\n{msg}")
            raise RuntimeError(msg)

    assets = client.assets()
    settings_response = client.settings()
    equity, available = extract_usdt_equity_available(assets)
    equity_guard = check_daily_equity_guard(settings, equity=equity, available=available)
    state = StrategyState(settings.strategy_state_path)
    today = utc_day()
    reports: list[dict[str, Any]] = []
    candidate_intents: dict[int, OrderIntent] = {}
    global_open_before = _global_open_count(settings, client)
    survival_group_open_before = _survival_group_open_count(settings, client)
    mode_label = "LIVE" if live else "DRY"
    survival_group = _survival_group_set(settings)

    if notify_policy == "always":
        tg.send(
            f"🧠 <b>Index Sniper Pro v1.5 EXTERNAL/SURVIVAL STRATEGY_EXEC {mode_label}</b>\n"
            f"실주문: {'있음' if live else '없음'}\n"
            f"대상: {', '.join(settings.symbols)}\n"
            f"계좌 사용비율: {settings.capital_ratio * 100:.2f}% / 레버리지 {settings.leverage}x\n"
            f"Risk profile: {settings.risk_profile}\n"
            f"Index group: {', '.join(settings.survival_correlated_group)} max open {settings.survival_max_correlated_open}\n"
            f"TP/SL preset: {settings.use_exchange_tpsl}\n"
            f"daily loss guard: {'OK' if equity_guard.ok else 'BLOCK'} ({equity_guard.loss_pct:.3f}% / {equity_guard.max_daily_loss_pct:.3f}%)\n"
            f"open positions before: {global_open_before} / max {settings.max_open_positions}"
        )

    # 1) Build reports and candidates. No order is placed in this pass.
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
            size_plan = build_size_plan(
                equity=equity,
                available=available,
                symbol_count=len(settings.symbols),
                capital_ratio=effective_capital_ratio,
                leverage=settings.leverage,
                price=price,
                instrument=instrument,
            )
            daily_count = state.entry_count(symbol, today)
            notional_ok = float(size_plan.notional_per_symbol or 0) <= float(settings.max_order_notional_usdt)
            breakout_atr_distance = _breakout_atr_distance(signal)
            score = _survival_signal_score(signal, effective_size_multiplier)
            is_index_group = symbol in survival_group
            group_limit_ok = True
            if settings.risk_profile == "SURVIVAL" and is_index_group:
                group_limit_ok = survival_group_open_before < settings.survival_max_correlated_open
            breakout_strength_ok = True
            if settings.risk_profile == "SURVIVAL" and signal.signal in {"LONG", "SHORT"}:
                breakout_strength_ok = breakout_atr_distance >= settings.survival_min_breakout_atr

            checks = {
                "has_signal": signal.signal in {"LONG", "SHORT"},
                "leverage_ok": current_leverage == settings.leverage,
                "margin_mode_ok": current_margin_mode == settings.margin_mode,
                "no_open_position_for_symbol": len(opens) == 0,
                "size_valid": size_plan.valid,
                "max_order_notional_ok": notional_ok,
                "daily_entry_limit_ok": daily_count < settings.max_daily_entries_per_symbol,
                "global_open_limit_ok": global_open_before < settings.max_open_positions,
                "cycle_entry_limit_ok": True,
                "daily_loss_guard_ok": equity_guard.ok,
                "warmup_allowed": (not signal.warmup_mode) or settings.live_allow_warmup_entries,
                "survival_correlated_group_ok": group_limit_ok,
                "survival_breakout_strength_ok": breakout_strength_ok,
                "survival_min_score_ok": score >= settings.survival_min_signal_score if signal.signal in {"LONG", "SHORT"} else False,
                "survival_best_candidate_ok": False,
            }

            intent_payload = None
            if signal.signal in {"LONG", "SHORT"} and size_plan.valid:
                intent = _make_entry_intent(settings, symbol, signal, size_plan.final_qty, instrument)
                intent_payload = client.build_market_order_payload(intent)
                base_checks_ok = all(v for k, v in checks.items() if k != "survival_best_candidate_ok")
                if base_checks_ok:
                    candidate_intents[len(reports)] = intent

            item.update({
                "price": price,
                "daily_candles": len(daily_candles),
                "warmup_candles": len(warmup_candles or []),
                "current_leverage": current_leverage,
                "target_leverage": settings.leverage,
                "current_margin_mode": current_margin_mode,
                "target_margin_mode": settings.margin_mode,
                "open_position_count": len(opens),
                "global_open_before": global_open_before,
                "survival_group_open_before": survival_group_open_before,
                "daily_entries_today": daily_count,
                "equity_guard": equity_guard.to_dict(),
                "signal": signal.to_dict(),
                "breakout_atr_distance": breakout_atr_distance,
                "survival_signal_score": score,
                "effective_size_multiplier": effective_size_multiplier,
                "effective_capital_ratio": effective_capital_ratio,
                "size_plan": asdict(size_plan),
                "checks": checks,
                "action_allowed": False,
                "order_payload_if_signal": intent_payload,
                "order_result": None,
            })
        except Exception as exc:
            item["error"] = str(exc)
            append_jsonl(settings.log_dir, "events.jsonl", {"type": "strategy_symbol_error", "symbol": symbol, "error": str(exc)})
        reports.append(item)

    # 2) SURVIVAL selection: only one candidate per cycle, highest score wins.
    eligible: list[tuple[float, int]] = []
    for idx, intent in candidate_intents.items():
        r = reports[idx]
        checks = r.get("checks") or {}
        base_ok = all(v for k, v in checks.items() if k != "survival_best_candidate_ok")
        if base_ok:
            eligible.append((float(r.get("survival_signal_score") or 0.0), idx))

    selected_idx: int | None = None
    if eligible:
        if settings.survival_select_best_signal:
            eligible.sort(key=lambda x: (x[0], 0 if reports[x[1]].get("signal", {}).get("warmup_mode") else 1), reverse=True)
            selected_idx = eligible[0][1]
        else:
            selected_idx = eligible[0][1]

    # 3) Place only the selected order, or dry-run it.
    if selected_idx is not None:
        r = reports[selected_idx]
        r["checks"]["survival_best_candidate_ok"] = True
        r["action_allowed"] = True
        intent = candidate_intents[selected_idx]
        order_result = client.place_order(intent, dry_run=settings.dry_run)
        r["order_result"] = order_result
        success = bool(settings.dry_run) or client.is_success(order_result)
        if success:
            event = {
                "ts": datetime.now(timezone.utc).isoformat(), "mode": mode_label,
                "symbol": r["symbol"], "signal": r["signal"]["signal"], "side": intent.side,
                "pos_side": intent.pos_side, "qty": r.get("size_plan", {}).get("final_qty"), "price": r.get("price"),
                "stop_loss": intent.stop_loss or "", "take_profit": intent.take_profit or "",
                "dry_run": settings.dry_run, "client_oid": intent.client_oid or "",
                "result_code": order_result.get("code", "DRY") if isinstance(order_result, dict) else "",
                "result_msg": order_result.get("msg", "") if isinstance(order_result, dict) else "",
                "survival_signal_score": r.get("survival_signal_score"),
            }
            append_jsonl(settings.log_dir, "events.jsonl", {"type": "strategy_entry", **event, "order_result": order_result})
            append_trade_csv(settings.log_dir, event)
            if live:
                state.record_entry(r["symbol"], {**event, "order_result": order_result}, day=today)

    # 4) Log blocked active signals.
    for idx, r in enumerate(reports):
        if r.get("signal", {}).get("signal") in {"LONG", "SHORT"} and not r.get("action_allowed"):
            append_jsonl(settings.log_dir, "events.jsonl", {"type": "strategy_signal_blocked", "symbol": r["symbol"], "signal": r["signal"], "checks": r.get("checks"), "survival_signal_score": r.get("survival_signal_score")})

    print(f"===== STRATEGY EXEC v1.5 EXTERNAL/SURVIVAL {mode_label} =====")
    print(_short(reports, 50000))

    active = [r for r in reports if r.get("signal", {}).get("signal") in {"LONG", "SHORT"}]
    executed = [r for r in reports if r.get("order_result") is not None and r.get("action_allowed")]
    errors = [r for r in reports if "error" in r]
    blocked = [r for r in active if not r.get("action_allowed")]

    lines = [
        f"✅ <b>v1.5 EXTERNAL/SURVIVAL STRATEGY_EXEC {mode_label} 완료</b>",
        f"실주문: {'있음' if live else '없음'}",
        "원칙: 많이 버는 것보다 오래 살아남기",
        "Exchange preset TP/SL 포함" if settings.use_exchange_tpsl else "Exchange preset TP/SL 미사용",
        f"Daily loss guard: {'OK' if equity_guard.ok else 'BLOCK'} {equity_guard.loss_pct:.3f}% / {equity_guard.max_daily_loss_pct:.3f}%",
        f"Index group open: {survival_group_open_before} / {settings.survival_max_correlated_open}",
    ]
    if errors:
        lines.append("⚠️ 오류: " + ", ".join(r["symbol"] for r in errors))
    if executed:
        lines.append("🔥 선택된 실행/예정 주문:")
        for r in executed:
            s = r["signal"]
            payload = r.get("order_payload_if_signal") or {}
            lines.append(f"- {r['symbol']} {s['signal']} score {r.get('survival_signal_score')} qty {r.get('size_plan', {}).get('final_qty')} now {_fmt_price(s.get('current_price'))} SL {payload.get('stopLoss', '-')} TP {payload.get('takeProfit', '-')}")
    elif active:
        lines.append("신호는 있으나 생존형 필터로 차단됨:")
        for r in blocked:
            bad = [k for k, v in (r.get("checks") or {}).items() if not v]
            lines.append(f"- {r['symbol']} {r.get('signal', {}).get('signal')} score {r.get('survival_signal_score')} blocked: {', '.join(bad)}")
    else:
        lines.append("현재 신호: 없음/HOLD")
    lines.append("요약:")
    for r in reports:
        if "error" in r:
            lines.append(f"- {r['symbol']}: ERROR {r['error'][:100]}")
            continue
        s = r["signal"]
        lines.append(f"- {r['symbol']}: {s['signal']} / {s['reason']} / trend {s.get('trend_mode')} / score {r.get('survival_signal_score')} / size x{r.get('effective_size_multiplier')} / now {_fmt_price(s.get('current_price'))}, L {_fmt_price(s.get('long_target'))}, S {_fmt_price(s.get('short_target'))}")
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
