from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Literal

from index_sniper.strategy.indicators import Candle, ema, true_ranges

ROOT = Path(__file__).resolve().parents[2]

Side = Literal["long", "short"]
INDEX_GROUP = {"SP500USDT", "NDX100USDT"}


@dataclass
class BacktestConfig:
    initial_equity: float = 1374.0
    capital_ratio: float = 0.30
    leverage: float = 5.0
    max_order_notional_usdt: float = 1000.0
    k_value: float = 0.50
    ema_fast: int = 20
    ema_slow: int = 60
    use_ema_filter: bool = True
    no_ma_both_breakout_mode: str = "skip"  # skip | stronger | candle
    atr_period: int = 14
    atr_stop_mult: float = 1.30
    atr_take_profit_mult: float = 2.00
    taker_fee_rate: float = 0.0006
    slippage_bps: float = 2.0
    survival_min_breakout_atr: float = 0.05
    max_entry_extension_atr: float = 0.40
    anti_chase_enabled: bool = True
    anti_chase_extreme_up_pct: float = 7.0
    anti_chase_extreme_down_pct: float = 7.0
    anti_chase_extreme_range_atr: float = 1.8
    max_open_positions: int = 2
    max_new_positions_per_day: int = 1
    max_index_group_open: int = 1
    block_index_friday_entries: bool = True
    weekend_flat_index: bool = True
    conservative_intraday_order: bool = True
    long_only_symbols: tuple[str, ...] = ()
    short_only_symbols: tuple[str, ...] = ()
    long_disabled_symbols: tuple[str, ...] = ()
    short_disabled_symbols: tuple[str, ...] = ()
    record_signals: bool = True


@dataclass
class IndicatorRow:
    ema_fast: float | None
    ema_slow: float | None
    atr: float | None
    previous_change_pct: float | None
    previous_range_atr: float | None


@dataclass
class Position:
    symbol: str
    side: Side
    qty: float
    entry_price: float
    entry_date: str
    stop: float
    take_profit: float
    notional: float
    entry_fee: float
    risk_per_unit: float


@dataclass
class Trade:
    symbol: str
    side: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    qty: float
    notional: float
    pnl: float
    fees: float
    net_pnl: float
    r_multiple: float | None
    exit_reason: str


@dataclass
class SignalLog:
    date: str
    symbol: str
    status: str
    side: str
    price_open: float
    high: float
    low: float
    close: float
    long_target: float | None
    short_target: float | None
    ema_fast: float | None
    ema_slow: float | None
    atr: float | None
    reason: str


def _date(c: Candle) -> str:
    return datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc).date().isoformat()


def _weekday(c: Candle) -> int:
    return datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc).date().weekday()


def _slip(price: float, bps: float, adverse: int) -> float:
    # adverse=+1 means buyer pays more / short-cover pays more, adverse=-1 means seller receives less.
    return price * (1.0 + adverse * (bps / 10000.0))


def _indicator_at(candles: list[Candle], idx: int, cfg: BacktestConfig) -> IndicatorRow:
    # Use information through previous candle only. This is intentionally conservative and avoids same-day close lookahead.
    prev_idx = idx - 1
    if prev_idx < 1:
        return IndicatorRow(None, None, None, None, None)
    hist = candles[: prev_idx + 1]
    closes = [c.close for c in hist]
    # v2.4: allow a pure volatility-breakout test with no EMA trend filter.
    # We still keep ATR and previous-day anti-chase inputs.
    fast = ema(closes, cfg.ema_fast) if cfg.use_ema_filter else None
    slow = ema(closes, cfg.ema_slow) if cfg.use_ema_filter else None
    trs = true_ranges(hist)
    atr = sum(trs[-cfg.atr_period:]) / cfg.atr_period if len(trs) >= cfg.atr_period else None
    prev_change = None
    if prev_idx >= 1 and candles[prev_idx - 1].close:
        prev_change = ((candles[prev_idx].close - candles[prev_idx - 1].close) / candles[prev_idx - 1].close) * 100.0
    range_atr = None
    if atr and atr > 0:
        range_atr = max(candles[prev_idx].high - candles[prev_idx].low, 0.0) / atr
    return IndicatorRow(fast, slow, atr, prev_change, range_atr)


def _targets(candles: list[Candle], idx: int, cfg: BacktestConfig) -> tuple[float, float]:
    current = candles[idx]
    prev = candles[idx - 1]
    prev_range = max(prev.high - prev.low, 0.0)
    return current.open + prev_range * cfg.k_value, current.open - prev_range * cfg.k_value


def _side_allowed(symbol: str, side: Side, cfg: BacktestConfig) -> bool:
    symbol = symbol.upper()
    if symbol in set(s.upper() for s in cfg.long_disabled_symbols) and side == "long":
        return False
    if symbol in set(s.upper() for s in cfg.short_disabled_symbols) and side == "short":
        return False
    if symbol in set(s.upper() for s in cfg.long_only_symbols) and side != "long":
        return False
    if symbol in set(s.upper() for s in cfg.short_only_symbols) and side != "short":
        return False
    return True


def _anti_chase_block(side: Side, ind: IndicatorRow, cfg: BacktestConfig) -> str | None:
    if not cfg.anti_chase_enabled:
        return None
    p = ind.previous_change_pct
    r = ind.previous_range_atr
    if side == "long":
        if p is not None and p >= cfg.anti_chase_extreme_up_pct:
            return f"anti_chase_prev_up_{p:.2f}%"
        if p is not None and p > 0 and r is not None and r >= cfg.anti_chase_extreme_range_atr:
            return f"anti_chase_prev_up_range_{r:.2f}ATR"
    if side == "short":
        if p is not None and p <= -cfg.anti_chase_extreme_down_pct:
            return f"anti_chase_prev_down_{p:.2f}%"
        if p is not None and p < 0 and r is not None and r >= cfg.anti_chase_extreme_range_atr:
            return f"anti_chase_prev_down_range_{r:.2f}ATR"
    return None


def _size(equity: float, price: float, symbol_count: int, cfg: BacktestConfig) -> tuple[float, float]:
    capital_per_symbol = equity * cfg.capital_ratio / max(symbol_count, 1)
    notional = min(capital_per_symbol * cfg.leverage, cfg.max_order_notional_usdt)
    if notional <= 0 or price <= 0:
        return 0.0, 0.0
    qty = notional / price
    return qty, notional


def _mark_to_market(equity_realized: float, positions: list[Position], price_by_symbol: dict[str, float]) -> float:
    unreal = 0.0
    for p in positions:
        px = price_by_symbol.get(p.symbol, p.entry_price)
        if p.side == "long":
            unreal += (px - p.entry_price) * p.qty
        else:
            unreal += (p.entry_price - px) * p.qty
    return equity_realized + unreal


def _exit_position(p: Position, date_s: str, exit_price: float, reason: str, cfg: BacktestConfig) -> Trade:
    if p.side == "long":
        gross = (exit_price - p.entry_price) * p.qty
    else:
        gross = (p.entry_price - exit_price) * p.qty
    exit_fee = abs(exit_price * p.qty) * cfg.taker_fee_rate
    fees = p.entry_fee + exit_fee
    net = gross - fees
    r = None
    if p.risk_per_unit > 0:
        r = gross / (p.risk_per_unit * p.qty)
    return Trade(
        symbol=p.symbol,
        side=p.side,
        entry_date=p.entry_date,
        exit_date=date_s,
        entry_price=p.entry_price,
        exit_price=exit_price,
        qty=p.qty,
        notional=p.notional,
        pnl=gross,
        fees=fees,
        net_pnl=net,
        r_multiple=r,
        exit_reason=reason,
    )


def _check_exit(p: Position, candle: Candle, cfg: BacktestConfig) -> tuple[float, str] | None:
    if p.side == "long":
        sl_hit = candle.low <= p.stop
        tp_hit = candle.high >= p.take_profit
        if sl_hit and tp_hit:
            return _slip(p.stop, cfg.slippage_bps, -1), "both_hit_assume_sl"
        if sl_hit:
            return _slip(p.stop, cfg.slippage_bps, -1), "stop_loss"
        if tp_hit:
            return _slip(p.take_profit, cfg.slippage_bps, -1), "take_profit"
    else:
        sl_hit = candle.high >= p.stop
        tp_hit = candle.low <= p.take_profit
        if sl_hit and tp_hit:
            return _slip(p.stop, cfg.slippage_bps, +1), "both_hit_assume_sl"
        if sl_hit:
            return _slip(p.stop, cfg.slippage_bps, +1), "stop_loss"
        if tp_hit:
            return _slip(p.take_profit, cfg.slippage_bps, +1), "take_profit"
    return None


class _SignalSink(list):
    """List-like signal logger that can be disabled for large optimizer runs."""

    def __init__(self, enabled: bool = True):
        super().__init__()
        self.enabled = enabled

    def append(self, item):  # type: ignore[override]
        if self.enabled:
            super().append(item)


def run_portfolio_backtest(symbol_candles: dict[str, list[Candle]], cfg: BacktestConfig) -> dict:
    candles_by_date: dict[str, dict[str, Candle]] = {}
    index_by_date: dict[str, dict[str, int]] = {}
    for symbol, candles in symbol_candles.items():
        for i, c in enumerate(candles):
            d = _date(c)
            candles_by_date.setdefault(d, {})[symbol] = c
            index_by_date.setdefault(d, {})[symbol] = i
    dates = sorted(candles_by_date)

    equity_realized = cfg.initial_equity
    positions: list[Position] = []
    trades: list[Trade] = []
    signal_logs: list[SignalLog] = _SignalSink(cfg.record_signals)
    curve: list[dict] = []
    daily_entries: dict[tuple[str, str], int] = {}
    symbol_count = max(len(symbol_candles), 1)
    peak_equity = cfg.initial_equity
    max_dd = 0.0

    for d in dates:
        day_candles = candles_by_date[d]
        # 1) Existing position exits first.
        remaining: list[Position] = []
        for p in positions:
            candle = day_candles.get(p.symbol)
            if candle is None:
                remaining.append(p)
                continue
            exit_info = _check_exit(p, candle, cfg)
            if exit_info is None and cfg.weekend_flat_index and p.symbol in INDEX_GROUP and _weekday(candle) == 4:
                exit_info = (_slip(candle.close, cfg.slippage_bps, -1 if p.side == "long" else +1), "weekend_flat_friday_close")
            if exit_info is not None:
                tr = _exit_position(p, d, exit_info[0], exit_info[1], cfg)
                equity_realized += tr.net_pnl
                trades.append(tr)
            else:
                remaining.append(p)
        positions = remaining

        price_by_symbol = {s: c.close for s, c in day_candles.items()}
        equity_now = _mark_to_market(equity_realized, positions, price_by_symbol)

        # 2) Candidate entries.
        candidates: list[tuple[float, str, Side, float, float, float, Candle, IndicatorRow, str]] = []
        if len(positions) < cfg.max_open_positions:
            for symbol, candles in symbol_candles.items():
                if symbol not in day_candles:
                    continue
                if any(p.symbol == symbol for p in positions):
                    signal_logs.append(SignalLog(d, symbol, "BLOCKED", "", day_candles[symbol].open, day_candles[symbol].high, day_candles[symbol].low, day_candles[symbol].close, None, None, None, None, None, "open_position_exists"))
                    continue
                if cfg.weekend_flat_index and cfg.block_index_friday_entries and symbol in INDEX_GROUP and _weekday(day_candles[symbol]) == 4:
                    signal_logs.append(SignalLog(d, symbol, "BLOCKED", "", day_candles[symbol].open, day_candles[symbol].high, day_candles[symbol].low, day_candles[symbol].close, None, None, None, None, None, "index_friday_entry_block"))
                    continue
                idx = index_by_date[d].get(symbol)
                required_history = max(cfg.atr_period, cfg.ema_slow if cfg.use_ema_filter else 0) + 1
                if idx is None or idx < required_history:
                    continue
                c = day_candles[symbol]
                ind = _indicator_at(candles, idx, cfg)
                if ind.atr is None or ind.atr <= 0:
                    continue
                if cfg.use_ema_filter and (ind.ema_fast is None or ind.ema_slow is None):
                    continue
                long_t, short_t = _targets(candles, idx, cfg)
                side: Side | None = None
                entry_target = 0.0
                trigger_strength = 0.0
                reason = "no_signal"
                long_hit = c.high >= long_t + ind.atr * cfg.survival_min_breakout_atr
                short_hit = c.low <= short_t - ind.atr * cfg.survival_min_breakout_atr
                if cfg.use_ema_filter:
                    bullish = (ind.ema_fast or 0.0) > (ind.ema_slow or 0.0)
                    bearish = (ind.ema_fast or 0.0) < (ind.ema_slow or 0.0)
                    if bullish and long_hit:
                        side = "long"
                        entry_target = max(long_t, c.open) if c.open > long_t else long_t
                        trigger_strength = max(0.0, c.high - long_t) / ind.atr
                        reason = "long_breakout_bullish"
                    elif bearish and short_hit:
                        side = "short"
                        entry_target = min(short_t, c.open) if c.open < short_t else short_t
                        trigger_strength = max(0.0, short_t - c.low) / ind.atr
                        reason = "short_breakout_bearish"
                    elif c.high >= long_t and not bullish:
                        reason = "upper_breakout_trend_rejected"
                    elif c.low <= short_t and not bearish:
                        reason = "lower_breakout_trend_rejected"
                else:
                    # v2.4 pure volatility breakout: no EMA direction filter.
                    # If both upper/lower breakout touch in the same daily candle, exact intraday order is unknown.
                    # Modes:
                    # - skip: conservative, ignore both-hit days.
                    # - stronger: choose the side with larger ATR-normalized follow-through.
                    # - candle: choose long on green candle and short on red candle.
                    long_strength = max(0.0, c.high - long_t) / ind.atr if long_hit else 0.0
                    short_strength = max(0.0, short_t - c.low) / ind.atr if short_hit else 0.0
                    if long_hit and short_hit:
                        mode = (cfg.no_ma_both_breakout_mode or "skip").strip().lower()
                        if mode == "stronger":
                            if long_strength >= short_strength:
                                side = "long"
                                entry_target = max(long_t, c.open) if c.open > long_t else long_t
                                trigger_strength = long_strength
                                reason = "no_ma_both_breakout_choose_long_stronger"
                            else:
                                side = "short"
                                entry_target = min(short_t, c.open) if c.open < short_t else short_t
                                trigger_strength = short_strength
                                reason = "no_ma_both_breakout_choose_short_stronger"
                        elif mode == "candle":
                            if c.close >= c.open:
                                side = "long"
                                entry_target = max(long_t, c.open) if c.open > long_t else long_t
                                trigger_strength = long_strength
                                reason = "no_ma_both_breakout_choose_long_green"
                            else:
                                side = "short"
                                entry_target = min(short_t, c.open) if c.open < short_t else short_t
                                trigger_strength = short_strength
                                reason = "no_ma_both_breakout_choose_short_red"
                        else:
                            reason = "no_ma_both_breakout_skipped"
                    elif long_hit:
                        side = "long"
                        entry_target = max(long_t, c.open) if c.open > long_t else long_t
                        trigger_strength = long_strength
                        reason = "no_ma_long_breakout"
                    elif short_hit:
                        side = "short"
                        entry_target = min(short_t, c.open) if c.open < short_t else short_t
                        trigger_strength = short_strength
                        reason = "no_ma_short_breakout"
                if side is None:
                    signal_logs.append(SignalLog(d, symbol, "HOLD", "", c.open, c.high, c.low, c.close, long_t, short_t, ind.ema_fast, ind.ema_slow, ind.atr, reason))
                    continue
                if not _side_allowed(symbol, side, cfg):
                    signal_logs.append(SignalLog(d, symbol, "BLOCKED", side.upper(), c.open, c.high, c.low, c.close, long_t, short_t, ind.ema_fast, ind.ema_slow, ind.atr, f"side_disabled_{side}"))
                    continue
                # Late gap/extension block.
                extension = 0.0
                if side == "long":
                    extension = max(0.0, entry_target - long_t) / ind.atr
                else:
                    extension = max(0.0, short_t - entry_target) / ind.atr
                block_reason = _anti_chase_block(side, ind, cfg)
                if block_reason:
                    signal_logs.append(SignalLog(d, symbol, "BLOCKED", side.upper(), c.open, c.high, c.low, c.close, long_t, short_t, ind.ema_fast, ind.ema_slow, ind.atr, block_reason))
                    continue
                if extension > cfg.max_entry_extension_atr:
                    signal_logs.append(SignalLog(d, symbol, "BLOCKED", side.upper(), c.open, c.high, c.low, c.close, long_t, short_t, ind.ema_fast, ind.ema_slow, ind.atr, f"late_entry_extension_{extension:.3f}ATR"))
                    continue
                # Index group one-open rule.
                if symbol in INDEX_GROUP and sum(1 for p in positions if p.symbol in INDEX_GROUP) >= cfg.max_index_group_open:
                    signal_logs.append(SignalLog(d, symbol, "BLOCKED", side.upper(), c.open, c.high, c.low, c.close, long_t, short_t, ind.ema_fast, ind.ema_slow, ind.atr, "index_group_open_limit"))
                    continue
                score = trigger_strength * 100.0
                candidates.append((score, symbol, side, entry_target, long_t, short_t, c, ind, reason))

        # Survival: only one new position per day, pick highest score.
        candidates.sort(key=lambda x: x[0], reverse=True)
        entries_today = 0
        for score, symbol, side, entry_target, long_t, short_t, c, ind, reason in candidates:
            if entries_today >= cfg.max_new_positions_per_day:
                signal_logs.append(SignalLog(d, symbol, "BLOCKED", side.upper(), c.open, c.high, c.low, c.close, long_t, short_t, ind.ema_fast, ind.ema_slow, ind.atr, "cycle_entry_limit"))
                continue
            if len(positions) >= cfg.max_open_positions:
                signal_logs.append(SignalLog(d, symbol, "BLOCKED", side.upper(), c.open, c.high, c.low, c.close, long_t, short_t, ind.ema_fast, ind.ema_slow, ind.atr, "global_open_limit"))
                continue
            if symbol in INDEX_GROUP and sum(1 for p in positions if p.symbol in INDEX_GROUP) >= cfg.max_index_group_open:
                signal_logs.append(SignalLog(d, symbol, "BLOCKED", side.upper(), c.open, c.high, c.low, c.close, long_t, short_t, ind.ema_fast, ind.ema_slow, ind.atr, "index_group_open_limit"))
                continue
            key = (d, symbol)
            if daily_entries.get(key, 0) >= 1:
                signal_logs.append(SignalLog(d, symbol, "BLOCKED", side.upper(), c.open, c.high, c.low, c.close, long_t, short_t, ind.ema_fast, ind.ema_slow, ind.atr, "daily_symbol_entry_limit"))
                continue
            # Fill price with adverse slippage.
            entry = _slip(entry_target, cfg.slippage_bps, +1 if side == "long" else -1)
            qty, notional = _size(equity_now, entry, symbol_count, cfg)
            if qty <= 0 or notional <= 0:
                signal_logs.append(SignalLog(d, symbol, "BLOCKED", side.upper(), c.open, c.high, c.low, c.close, long_t, short_t, ind.ema_fast, ind.ema_slow, ind.atr, "invalid_size"))
                continue
            entry_fee = notional * cfg.taker_fee_rate
            if side == "long":
                stop = entry - ind.atr * cfg.atr_stop_mult
                tp = entry + ind.atr * cfg.atr_take_profit_mult
                risk = entry - stop
            else:
                stop = entry + ind.atr * cfg.atr_stop_mult
                tp = entry - ind.atr * cfg.atr_take_profit_mult
                risk = stop - entry
            positions.append(Position(symbol, side, qty, entry, d, stop, tp, notional, entry_fee, risk))
            daily_entries[key] = daily_entries.get(key, 0) + 1
            entries_today += 1
            signal_logs.append(SignalLog(d, symbol, "ENTRY", side.upper(), c.open, c.high, c.low, c.close, long_t, short_t, ind.ema_fast, ind.ema_slow, ind.atr, f"{reason};score={score:.3f}"))
            # Check same-day TP/SL after entry with conservative assumption.
            exit_info = _check_exit(positions[-1], c, cfg)
            if exit_info is not None:
                p = positions.pop()
                tr = _exit_position(p, d, exit_info[0], "same_day_" + exit_info[1], cfg)
                equity_realized += tr.net_pnl
                trades.append(tr)
                equity_now = _mark_to_market(equity_realized, positions, price_by_symbol)

        equity_eod = _mark_to_market(equity_realized, positions, price_by_symbol)
        peak_equity = max(peak_equity, equity_eod)
        dd = (peak_equity - equity_eod) / peak_equity * 100.0 if peak_equity else 0.0
        max_dd = max(max_dd, dd)
        curve.append({"date": d, "equity": equity_eod, "realized_equity": equity_realized, "open_positions": len(positions), "drawdown_pct": dd})

    # Liquidate remaining at last available close for summary.
    if positions and dates:
        last_date = dates[-1]
        price_by_symbol = {s: symbol_candles[s][-1].close for s in symbol_candles if symbol_candles[s]}
        for p in positions:
            px = price_by_symbol.get(p.symbol, p.entry_price)
            exit_px = _slip(px, cfg.slippage_bps, -1 if p.side == "long" else +1)
            tr = _exit_position(p, last_date, exit_px, "final_liquidation", cfg)
            equity_realized += tr.net_pnl
            trades.append(tr)
        positions = []
        curve.append({"date": last_date, "equity": equity_realized, "realized_equity": equity_realized, "open_positions": 0, "drawdown_pct": max_dd})

    return _summary(cfg, curve, trades, signal_logs, max_dd)


def _summary(cfg: BacktestConfig, curve: list[dict], trades: list[Trade], signal_logs: list[SignalLog], max_dd: float) -> dict:
    start = cfg.initial_equity
    end = curve[-1]["equity"] if curve else cfg.initial_equity
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    by_symbol: dict[str, dict] = {}
    for t in trades:
        b = by_symbol.setdefault(t.symbol, {"trades": 0, "net_pnl": 0.0, "wins": 0})
        b["trades"] += 1
        b["net_pnl"] += t.net_pnl
        if t.net_pnl > 0:
            b["wins"] += 1
    for b in by_symbol.values():
        b["win_rate_pct"] = (b["wins"] / b["trades"] * 100.0) if b["trades"] else 0.0
        b["net_pnl"] = round(b["net_pnl"], 6)
    return {
        "config": asdict(cfg),
        "start_equity": round(start, 6),
        "end_equity": round(end, 6),
        "return_pct": round((end / start - 1.0) * 100.0, 6) if start else 0.0,
        "max_drawdown_pct": round(max_dd, 6),
        "trade_count": len(trades),
        "win_rate_pct": round((len(wins) / len(trades) * 100.0) if trades else 0.0, 6),
        "profit_factor": round((gross_win / gross_loss) if gross_loss else (math.inf if gross_win > 0 else 0.0), 6),
        "avg_net_pnl": round(mean([t.net_pnl for t in trades]), 6) if trades else 0.0,
        "by_symbol": by_symbol,
        "curve": curve,
        "trades": [asdict(t) for t in trades],
        "signals": [asdict(s) for s in signal_logs],
    }


def write_results(result: dict, out_dir: Path, label: str) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary_json": out_dir / f"backtest_summary_{label}.json",
        "summary_txt": out_dir / f"backtest_summary_{label}.txt",
        "equity_csv": out_dir / f"equity_curve_{label}.csv",
        "trades_csv": out_dir / f"trades_{label}.csv",
        "signals_csv": out_dir / f"signals_{label}.csv",
    }
    paths["summary_json"].write_text(json.dumps({k: v for k, v in result.items() if k not in {"curve", "trades", "signals"}}, ensure_ascii=False, indent=2), encoding="utf-8")
    with paths["summary_txt"].open("w", encoding="utf-8") as f:
        f.write("Index Sniper Pro Backtest Summary\n")
        f.write("=================================\n")
        for key in ["start_equity", "end_equity", "return_pct", "max_drawdown_pct", "trade_count", "win_rate_pct", "profit_factor", "avg_net_pnl"]:
            f.write(f"{key}: {result.get(key)}\n")
        f.write("\nBy symbol:\n")
        for sym, data in result.get("by_symbol", {}).items():
            f.write(f"- {sym}: {data}\n")
    _write_csv(paths["equity_csv"], result.get("curve", []))
    _write_csv(paths["trades_csv"], result.get("trades", []))
    _write_csv(paths["signals_csv"], result.get("signals", []))
    # latest aliases
    for name, p in paths.items():
        alias = out_dir / p.name.replace(f"_{label}", "_latest")
        alias.write_bytes(p.read_bytes())
    return paths


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
