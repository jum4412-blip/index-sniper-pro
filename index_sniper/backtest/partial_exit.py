from __future__ import annotations

import argparse
import csv
import math
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Literal

import pandas as pd
from dotenv import load_dotenv

from index_sniper.backtest.data import load_or_fetch_symbol
from index_sniper.backtest.runner import cfg_from_env
from index_sniper.strategy.indicators import Candle, true_ranges

ROOT = Path(__file__).resolve().parents[2]
SYMBOL = "BTCUSDT"
Side = Literal["long", "short"]


@dataclass(frozen=True)
class PartialTarget:
    atr_mult: float
    fraction: float


@dataclass(frozen=True)
class PartialProfile:
    name: str
    targets: tuple[PartialTarget, ...]
    move_stop_to_breakeven_after_first: bool = True


PROFILES: dict[str, PartialProfile] = {
    # Default 실전 후보: 50% at +1.0 ATR, remaining 50% at +2.5 ATR, move stop to BE after first take-profit.
    "p50_be_25": PartialProfile(
        name="p50_be_25",
        targets=(PartialTarget(1.0, 0.50), PartialTarget(2.5, 0.50)),
        move_stop_to_breakeven_after_first=True,
    ),
    # More laddered: 40% at +1.0 ATR, 30% at +2.0 ATR, 30% at +3.0 ATR.
    "p40_30_30_be": PartialProfile(
        name="p40_30_30_be",
        targets=(PartialTarget(1.0, 0.40), PartialTarget(2.0, 0.30), PartialTarget(3.0, 0.30)),
        move_stop_to_breakeven_after_first=True,
    ),
    # Conservative first take-profit and modest runner.
    "p50_be_20": PartialProfile(
        name="p50_be_20",
        targets=(PartialTarget(1.0, 0.50), PartialTarget(2.0, 0.50)),
        move_stop_to_breakeven_after_first=True,
    ),
}


@dataclass
class PositionState:
    symbol: str
    side: Side
    qty: float
    initial_qty: float
    entry_price: float
    entry_date: str
    stop: float
    original_stop: float
    notional_remaining: float
    entry_fee_remaining: float
    risk_per_unit: float
    atr_at_entry: float
    targets: list[PartialTarget]
    next_target_index: int = 0
    moved_to_be: bool = False


@dataclass
class SliceTrade:
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
    slice_fraction: float
    remaining_qty_after: float


def _date(c: Candle) -> str:
    return datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc).date().isoformat()


def _slip(price: float, bps: float, adverse: int) -> float:
    return price * (1.0 + adverse * (bps / 10000.0))


def _targets(candles: list[Candle], idx: int, k: float) -> tuple[float, float]:
    current = candles[idx]
    prev = candles[idx - 1]
    prev_range = max(prev.high - prev.low, 0.0)
    return current.open + prev_range * k, current.open - prev_range * k


def _atr_at(candles: list[Candle], idx: int, period: int) -> float | None:
    prev_idx = idx - 1
    if prev_idx < 1:
        return None
    hist = candles[: prev_idx + 1]
    trs = true_ranges(hist)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def _previous_change_and_range_atr(candles: list[Candle], idx: int, atr: float) -> tuple[float | None, float | None]:
    prev_idx = idx - 1
    prev_change = None
    if prev_idx >= 1 and candles[prev_idx - 1].close:
        prev_change = ((candles[prev_idx].close - candles[prev_idx - 1].close) / candles[prev_idx - 1].close) * 100.0
    range_atr = None
    if atr > 0:
        range_atr = max(candles[prev_idx].high - candles[prev_idx].low, 0.0) / atr
    return prev_change, range_atr


def _anti_chase_block(side: Side, prev_change: float | None, prev_range_atr: float | None, cfg) -> str | None:
    if not cfg.anti_chase_enabled:
        return None
    if side == "long":
        if prev_change is not None and prev_change >= cfg.anti_chase_extreme_up_pct:
            return f"anti_chase_prev_up_{prev_change:.2f}%"
        if prev_change is not None and prev_change > 0 and prev_range_atr is not None and prev_range_atr >= cfg.anti_chase_extreme_range_atr:
            return f"anti_chase_prev_up_range_{prev_range_atr:.2f}ATR"
    if side == "short":
        if prev_change is not None and prev_change <= -cfg.anti_chase_extreme_down_pct:
            return f"anti_chase_prev_down_{prev_change:.2f}%"
        if prev_change is not None and prev_change < 0 and prev_range_atr is not None and prev_range_atr >= cfg.anti_chase_extreme_range_atr:
            return f"anti_chase_prev_down_range_{prev_range_atr:.2f}ATR"
    return None


def _apply_side_allowed(side: Side, side_mode: str) -> bool:
    side_mode = side_mode.lower().strip()
    if side_mode in {"ls", "longshort", "long_short", "both"}:
        return True
    if side_mode in {"long", "long_only"}:
        return side == "long"
    if side_mode in {"short", "short_only"}:
        return side == "short"
    raise ValueError(f"unknown side mode: {side_mode}")


def _mark_to_market(equity_realized: float, pos: PositionState | None, close: float) -> float:
    if pos is None:
        return equity_realized
    if pos.side == "long":
        return equity_realized + (close - pos.entry_price) * pos.qty
    return equity_realized + (pos.entry_price - close) * pos.qty


def _size(equity: float, price: float, cfg) -> tuple[float, float]:
    notional = min(equity * cfg.capital_ratio * cfg.leverage, cfg.max_order_notional_usdt)
    if price <= 0 or notional <= 0:
        return 0.0, 0.0
    return notional / price, notional


def _make_slice_trade(pos: PositionState, date_s: str, exit_price: float, qty_to_close: float, reason: str, cfg) -> SliceTrade:
    qty_to_close = min(max(qty_to_close, 0.0), pos.qty)
    if qty_to_close <= 0:
        raise ValueError("qty_to_close must be positive")
    close_ratio = qty_to_close / pos.qty if pos.qty else 1.0
    entry_fee_alloc = pos.entry_fee_remaining * close_ratio
    notional_alloc = pos.notional_remaining * close_ratio
    if pos.side == "long":
        gross = (exit_price - pos.entry_price) * qty_to_close
    else:
        gross = (pos.entry_price - exit_price) * qty_to_close
    exit_fee = abs(exit_price * qty_to_close) * cfg.taker_fee_rate
    fees = entry_fee_alloc + exit_fee
    net = gross - fees
    r = None
    if pos.risk_per_unit > 0:
        r = gross / (pos.risk_per_unit * qty_to_close)
    remaining_after = pos.qty - qty_to_close
    # Mutate remaining cost/fee after creating the trade.
    pos.qty = remaining_after
    pos.entry_fee_remaining -= entry_fee_alloc
    pos.notional_remaining -= notional_alloc
    if pos.qty < 1e-12:
        pos.qty = 0.0
        pos.entry_fee_remaining = 0.0
        pos.notional_remaining = 0.0
    return SliceTrade(
        symbol=pos.symbol,
        side=pos.side,
        entry_date=pos.entry_date,
        exit_date=date_s,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        qty=qty_to_close,
        notional=notional_alloc,
        pnl=gross,
        fees=fees,
        net_pnl=net,
        r_multiple=r,
        exit_reason=reason,
        slice_fraction=qty_to_close / pos.initial_qty if pos.initial_qty else 0.0,
        remaining_qty_after=remaining_after,
    )


def _target_price(pos: PositionState, target: PartialTarget) -> float:
    if pos.side == "long":
        return pos.entry_price + pos.atr_at_entry * target.atr_mult
    return pos.entry_price - pos.atr_at_entry * target.atr_mult


def _target_hit(pos: PositionState, candle: Candle, target: PartialTarget) -> bool:
    px = _target_price(pos, target)
    if pos.side == "long":
        return candle.high >= px
    return candle.low <= px


def _stop_hit(pos: PositionState, candle: Candle) -> bool:
    if pos.side == "long":
        return candle.low <= pos.stop
    return candle.high >= pos.stop


def _exit_slip(side: Side, price: float, cfg) -> float:
    # Long exit sells lower, short exit buys higher.
    return _slip(price, cfg.slippage_bps, -1 if side == "long" else +1)


def _process_position_day(pos: PositionState, candle: Candle, date_s: str, cfg, profile: PartialProfile, reason_prefix: str = "") -> tuple[list[SliceTrade], PositionState | None]:
    """Conservative daily-bar partial-exit handling.

    Exact intraday order is unknown with daily candles. Rules are intentionally conservative:
    - Before first partial TP, if stop and a target are both touched, assume stop first.
    - After first partial TP, if breakeven stop and final target are both touched, assume stop first.
    - If a target is hit and the moved breakeven stop is also touched in the same candle, the runner is closed at breakeven.
    """
    out: list[SliceTrade] = []

    # If no target has been taken yet and the original stop is also hit, prefer full stop.
    stop_hit_initial = _stop_hit(pos, candle)
    any_target_hit = any(_target_hit(pos, candle, t) for t in pos.targets[pos.next_target_index :])
    if pos.next_target_index == 0 and stop_hit_initial and any_target_hit:
        tr = _make_slice_trade(pos, date_s, _exit_slip(pos.side, pos.stop, cfg), pos.qty, reason_prefix + "both_hit_assume_sl", cfg)
        out.append(tr)
        return out, None
    if stop_hit_initial and pos.next_target_index == 0:
        tr = _make_slice_trade(pos, date_s, _exit_slip(pos.side, pos.stop, cfg), pos.qty, reason_prefix + "stop_loss", cfg)
        out.append(tr)
        return out, None

    # Sequentially take targets visible in the daily range.
    first_taken_this_day = False
    while pos.qty > 0 and pos.next_target_index < len(pos.targets):
        target = pos.targets[pos.next_target_index]
        if not _target_hit(pos, candle, target):
            break
        target_px = _target_price(pos, target)
        qty_to_close = pos.initial_qty * target.fraction
        # Last target closes everything left to avoid dust and fraction rounding.
        if pos.next_target_index == len(pos.targets) - 1:
            qty_to_close = pos.qty
        else:
            qty_to_close = min(qty_to_close, pos.qty)
        tr = _make_slice_trade(pos, date_s, _exit_slip(pos.side, target_px, cfg), qty_to_close, reason_prefix + f"partial_tp_{target.atr_mult:g}ATR", cfg)
        out.append(tr)
        pos.next_target_index += 1
        first_taken_this_day = True
        if profile.move_stop_to_breakeven_after_first and pos.next_target_index >= 1 and not pos.moved_to_be:
            pos.stop = pos.entry_price
            pos.moved_to_be = True
        if pos.qty <= 0:
            return out, None

    # After partial TP, check breakeven/runner stop. If target and stop both happened after first TP, conservative BE stop.
    if pos.qty > 0 and pos.next_target_index >= 1 and _stop_hit(pos, candle):
        tr = _make_slice_trade(pos, date_s, _exit_slip(pos.side, pos.stop, cfg), pos.qty, reason_prefix + ("breakeven_stop" if pos.moved_to_be else "stop_loss"), cfg)
        out.append(tr)
        return out, None

    # If first TP was hit and same candle low/high touched BE after entry, this conservative rule closes the runner at BE.
    if pos.qty > 0 and first_taken_this_day and pos.moved_to_be and _stop_hit(pos, candle):
        tr = _make_slice_trade(pos, date_s, _exit_slip(pos.side, pos.stop, cfg), pos.qty, reason_prefix + "same_day_breakeven_stop", cfg)
        out.append(tr)
        return out, None

    return out, pos


def run_btc_partial_backtest(candles: list[Candle], cfg, side_mode: str, profile: PartialProfile) -> dict:
    dates = [_date(c) for c in candles]
    equity_realized = cfg.initial_equity
    pos: PositionState | None = None
    trades: list[SliceTrade] = []
    curve: list[dict] = []
    peak_equity = cfg.initial_equity
    max_dd = 0.0

    required_history = cfg.atr_period + 1
    for idx, c in enumerate(candles):
        d = dates[idx]
        # Exits first.
        if pos is not None:
            slice_trades, pos = _process_position_day(pos, c, d, cfg, profile)
            for tr in slice_trades:
                equity_realized += tr.net_pnl
                trades.append(tr)

        # Entries after exits. One BTC position max.
        price_close = c.close
        equity_now = _mark_to_market(equity_realized, pos, price_close)
        if pos is None and idx >= required_history:
            atr = _atr_at(candles, idx, cfg.atr_period)
            if atr and atr > 0:
                long_t, short_t = _targets(candles, idx, cfg.k_value)
                long_hit = c.high >= long_t + atr * cfg.survival_min_breakout_atr
                short_hit = c.low <= short_t - atr * cfg.survival_min_breakout_atr
                side: Side | None = None
                entry_target = 0.0
                if long_hit and short_hit:
                    long_strength = max(0.0, c.high - long_t) / atr
                    short_strength = max(0.0, short_t - c.low) / atr
                    mode = (cfg.no_ma_both_breakout_mode or "stronger").strip().lower()
                    if mode == "stronger":
                        if long_strength >= short_strength:
                            side = "long"
                            entry_target = max(long_t, c.open) if c.open > long_t else long_t
                        else:
                            side = "short"
                            entry_target = min(short_t, c.open) if c.open < short_t else short_t
                    elif mode == "candle":
                        if c.close >= c.open:
                            side = "long"
                            entry_target = max(long_t, c.open) if c.open > long_t else long_t
                        else:
                            side = "short"
                            entry_target = min(short_t, c.open) if c.open < short_t else short_t
                    else:
                        side = None
                elif long_hit:
                    side = "long"
                    entry_target = max(long_t, c.open) if c.open > long_t else long_t
                elif short_hit:
                    side = "short"
                    entry_target = min(short_t, c.open) if c.open < short_t else short_t

                if side and _apply_side_allowed(side, side_mode):
                    extension = max(0.0, entry_target - long_t) / atr if side == "long" else max(0.0, short_t - entry_target) / atr
                    prev_change, prev_range_atr = _previous_change_and_range_atr(candles, idx, atr)
                    if extension <= cfg.max_entry_extension_atr and not _anti_chase_block(side, prev_change, prev_range_atr, cfg):
                        entry = _slip(entry_target, cfg.slippage_bps, +1 if side == "long" else -1)
                        qty, notional = _size(equity_now, entry, cfg)
                        if qty > 0 and notional > 0:
                            entry_fee = notional * cfg.taker_fee_rate
                            if side == "long":
                                stop = entry - atr * cfg.atr_stop_mult
                                risk = entry - stop
                            else:
                                stop = entry + atr * cfg.atr_stop_mult
                                risk = stop - entry
                            pos = PositionState(
                                symbol=SYMBOL,
                                side=side,
                                qty=qty,
                                initial_qty=qty,
                                entry_price=entry,
                                entry_date=d,
                                stop=stop,
                                original_stop=stop,
                                notional_remaining=notional,
                                entry_fee_remaining=entry_fee,
                                risk_per_unit=risk,
                                atr_at_entry=atr,
                                targets=list(profile.targets),
                            )
                            # Same-day exits after entry.
                            slice_trades, pos = _process_position_day(pos, c, d, cfg, profile, reason_prefix="same_day_")
                            for tr in slice_trades:
                                equity_realized += tr.net_pnl
                                trades.append(tr)

        equity_eod = _mark_to_market(equity_realized, pos, c.close)
        peak_equity = max(peak_equity, equity_eod)
        dd = (peak_equity - equity_eod) / peak_equity * 100.0 if peak_equity else 0.0
        max_dd = max(max_dd, dd)
        curve.append({"date": d, "equity": equity_eod, "realized_equity": equity_realized, "open_positions": 1 if pos else 0, "drawdown_pct": dd})

    if pos is not None and candles:
        d = dates[-1]
        exit_px = _exit_slip(pos.side, candles[-1].close, cfg)
        tr = _make_slice_trade(pos, d, exit_px, pos.qty, "final_liquidation", cfg)
        equity_realized += tr.net_pnl
        trades.append(tr)
        pos = None
        curve.append({"date": d, "equity": equity_realized, "realized_equity": equity_realized, "open_positions": 0, "drawdown_pct": max_dd})

    return _summary(cfg, profile, side_mode, curve, trades, max_dd)


def _summary(cfg, profile: PartialProfile, side_mode: str, curve: list[dict], trades: list[SliceTrade], max_dd: float) -> dict:
    start = cfg.initial_equity
    end = curve[-1]["equity"] if curve else start
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    return {
        "profile": profile.name,
        "side_mode": side_mode,
        "start_equity": round(start, 6),
        "end_equity": round(end, 6),
        "return_pct": round((end / start - 1.0) * 100.0, 6) if start else 0.0,
        "max_drawdown_pct": round(max_dd, 6),
        "trade_count": len(trades),
        "win_rate_pct": round((len(wins) / len(trades) * 100.0) if trades else 0.0, 6),
        "profit_factor": round((gross_win / gross_loss) if gross_loss else (math.inf if gross_win > 0 else 0.0), 6),
        "avg_net_pnl": round(mean([t.net_pnl for t in trades]), 6) if trades else 0.0,
        "curve": curve,
        "trades": [t.__dict__.copy() for t in trades],
        "config": {
            "initial_equity": cfg.initial_equity,
            "capital_ratio": cfg.capital_ratio,
            "leverage": cfg.leverage,
            "max_order_notional_usdt": cfg.max_order_notional_usdt,
            "k_value": cfg.k_value,
            "atr_stop_mult": cfg.atr_stop_mult,
            "partial_profile": profile.name,
            "partial_targets": [(t.atr_mult, t.fraction) for t in profile.targets],
            "move_stop_to_be": profile.move_stop_to_breakeven_after_first,
            "no_ma_both_breakout_mode": cfg.no_ma_both_breakout_mode,
        },
    }


def _parse_leverages(text: str) -> list[int]:
    text = text.strip()
    if "-" in text:
        a, b = text.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(x.strip()) for x in text.split(",") if x.strip()]


def _make_cfg(leverage: int, args) -> object:
    base = cfg_from_env(None)
    return replace(
        base,
        initial_equity=float(os.getenv("BT_INITIAL_EQUITY", "1374")),
        capital_ratio=float(os.getenv("BT_CAPITAL_RATIO", "0.30")),
        leverage=float(leverage),
        max_order_notional_usdt=float(os.getenv("BT_OPT_MAX_ORDER_NOTIONAL_USDT", os.getenv("BT_MAX_ORDER_NOTIONAL_USDT", "999999"))),
        use_ema_filter=False,
        no_ma_both_breakout_mode=args.both_mode,
        k_value=float(args.k),
        atr_stop_mult=float(args.sl),
        atr_take_profit_mult=2.0,  # not used directly in partial mode; kept in config snapshot.
        survival_min_breakout_atr=float(args.min_breakout_atr),
        max_entry_extension_atr=float(args.max_entry_extension_atr),
        anti_chase_enabled=True,
        anti_chase_extreme_up_pct=7.0,
        anti_chase_extreme_down_pct=7.0,
        anti_chase_extreme_range_atr=1.8,
        max_open_positions=1,
        max_new_positions_per_day=1,
        record_signals=False,
    )


def _max_dd_info(curve: list[dict]) -> dict:
    peak_eq = None
    peak_date = ""
    best = {"mdd_pct": 0.0, "peak_date": "", "trough_date": "", "peak_equity": 0.0, "trough_equity": 0.0}
    for row in curve:
        d = row["date"]
        eq = float(row["equity"])
        if peak_eq is None or eq > peak_eq:
            peak_eq = eq
            peak_date = d
        if peak_eq and peak_eq > 0:
            dd = (peak_eq - eq) / peak_eq * 100.0
            if dd > best["mdd_pct"]:
                best = {"mdd_pct": dd, "peak_date": peak_date, "trough_date": d, "peak_equity": peak_eq, "trough_equity": eq}
    return best


def _max_loss_streak(trades: list[dict]) -> dict:
    best: list[dict] = []
    cur: list[dict] = []
    ordered = sorted(trades, key=lambda t: (t["exit_date"], t["entry_date"]))

    def better(a: list[dict], b: list[dict]) -> bool:
        if len(a) != len(b):
            return len(a) > len(b)
        return sum(float(x["net_pnl"]) for x in a) < sum(float(x["net_pnl"]) for x in b)

    for t in ordered:
        if float(t["net_pnl"]) < 0:
            cur.append(t)
        else:
            if better(cur, best):
                best = cur[:]
            cur = []
    if better(cur, best):
        best = cur[:]
    if not best:
        return {"loss_streak_count": 0, "loss_streak_start": "", "loss_streak_end": "", "loss_streak_net_pnl": 0.0}
    return {
        "loss_streak_count": len(best),
        "loss_streak_start": best[0]["entry_date"],
        "loss_streak_end": best[-1]["exit_date"],
        "loss_streak_net_pnl": sum(float(x["net_pnl"]) for x in best),
    }


def _monthly_pnl(result: dict) -> pd.DataFrame:
    curve = pd.DataFrame(result["curve"])
    curve["date"] = pd.to_datetime(curve["date"])
    curve = curve.sort_values("date").drop_duplicates("date", keep="last")
    curve["month"] = curve["date"].dt.to_period("M").astype(str)
    month_end = curve.groupby("month").tail(1).copy()
    prev = result["start_equity"]
    rows = []
    for _, row in month_end.iterrows():
        eq = float(row["equity"])
        pnl = eq - prev
        pct = pnl / prev * 100.0 if prev else 0.0
        rows.append({"month": row["month"], "pnl": pnl, "pct": pct, "end_equity": eq})
        prev = eq
    return pd.DataFrame(rows)


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for row in rows:
        for k in row:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def run_final(args) -> None:
    load_dotenv(ROOT / ".env")
    out_dir = ROOT / "backtests"
    data_dir = out_dir / "data"
    profile = PROFILES[args.profile]
    leverages = _parse_leverages(args.leverages)
    rows = []
    raw = []
    for years in range(args.start_years, args.end_years + 1):
        data = load_or_fetch_symbol(SYMBOL, years, data_dir, refresh=args.refresh)
        for lev in leverages:
            cfg = _make_cfg(lev, args)
            result = run_btc_partial_backtest(data.candles, cfg, args.side_mode, profile)
            cell = f"{float(result['end_equity']):,.0f} / {float(result['max_drawdown_pct']):.1f}%"
            rows.append({"years": years, "leverage": f"{lev}x", "cell": cell})
            raw.append({
                "years": years,
                "leverage": f"{lev}x",
                "end_equity": result["end_equity"],
                "return_pct": result["return_pct"],
                "max_drawdown_pct": result["max_drawdown_pct"],
                "trade_count": result["trade_count"],
                "win_rate_pct": result["win_rate_pct"],
                "profit_factor": result["profit_factor"],
            })
    df = pd.DataFrame(rows)
    pivot = df.pivot(index="years", columns="leverage", values="cell").reindex(columns=[f"{x}x" for x in leverages])
    label = f"btc_partial_final_{args.start_years}y_{args.end_years}y_{args.profile}_{args.side_mode}_{args.both_mode}"
    out_txt = out_dir / f"{label}.txt"
    out_csv = out_dir / f"{label}.csv"
    raw_csv = out_dir / f"{label}_raw.csv"
    pivot.to_csv(out_csv)
    pd.DataFrame(raw).to_csv(raw_csv, index=False)
    with out_txt.open("w", encoding="utf-8") as f:
        f.write("BTC partial-exit final equity / MDD\n")
        f.write("===================================\n")
        f.write(f"Profile: {profile.name}\n")
        f.write("Format: final equity USDT / max drawdown %\n\n")
        f.write(pivot.to_string())
    latest = out_dir / "btc_partial_final_latest.txt"
    latest.write_text(out_txt.read_text(encoding="utf-8"), encoding="utf-8")
    print(pivot.to_string())
    print("\nsaved:", out_txt)


def run_risk(args) -> None:
    load_dotenv(ROOT / ".env")
    out_dir = ROOT / "backtests"
    data_dir = out_dir / "data"
    profile = PROFILES[args.profile]
    leverages = _parse_leverages(args.leverages)
    periods = [int(x) for x in args.periods.split(",") if x.strip()]
    summary_rows: list[dict] = []
    worst_rows: list[dict] = []
    monthly_by_period: dict[int, list[dict]] = {p: [] for p in periods}

    for years in periods:
        data = load_or_fetch_symbol(SYMBOL, years, data_dir, refresh=args.refresh)
        for lev in leverages:
            cfg = _make_cfg(lev, args)
            result = run_btc_partial_backtest(data.candles, cfg, args.side_mode, profile)
            dd = _max_dd_info(result["curve"])
            ls = _max_loss_streak(result["trades"])
            summary_rows.append({
                "years": years,
                "leverage": f"{lev}x",
                "end_equity": round(float(result["end_equity"]), 2),
                "return_pct": round(float(result["return_pct"]), 2),
                "mdd_pct": round(float(dd["mdd_pct"]), 2),
                "mdd_peak_date": dd["peak_date"],
                "mdd_trough_date": dd["trough_date"],
                "mdd_peak_equity": round(float(dd["peak_equity"]), 2),
                "mdd_trough_equity": round(float(dd["trough_equity"]), 2),
                "max_loss_streak": ls["loss_streak_count"],
                "loss_streak_start": ls["loss_streak_start"],
                "loss_streak_end": ls["loss_streak_end"],
                "loss_streak_net_pnl": round(float(ls["loss_streak_net_pnl"]), 2),
                "trade_count": result["trade_count"],
                "win_rate_pct": round(float(result["win_rate_pct"]), 2),
                "profit_factor": round(float(result["profit_factor"]), 3),
            })
            mp = _monthly_pnl(result)
            for _, row in mp.iterrows():
                monthly_by_period[years].append({
                    "month": row["month"],
                    "leverage": f"{lev}x",
                    "cell": f"{row['pnl']:,.0f} / {row['pct']:.1f}%",
                    "pnl": round(float(row["pnl"]), 2),
                    "pct": round(float(row["pct"]), 2),
                    "end_equity": round(float(row["end_equity"]), 2),
                })
            trades = pd.DataFrame(result["trades"])
            if len(trades):
                trades["net_pnl"] = trades["net_pnl"].astype(float)
                trades = trades.sort_values("net_pnl", ascending=True).head(args.worst_n)
                for rank, (_, t) in enumerate(trades.iterrows(), start=1):
                    worst_rows.append({
                        "years": years,
                        "leverage": f"{lev}x",
                        "rank": rank,
                        "side": t["side"],
                        "entry_date": t["entry_date"],
                        "exit_date": t["exit_date"],
                        "entry_price": round(float(t["entry_price"]), 2),
                        "exit_price": round(float(t["exit_price"]), 2),
                        "notional": round(float(t["notional"]), 2),
                        "net_pnl": round(float(t["net_pnl"]), 2),
                        "r_multiple": round(float(t["r_multiple"]), 3) if pd.notna(t["r_multiple"]) else "",
                        "exit_reason": t["exit_reason"],
                        "slice_fraction": round(float(t["slice_fraction"]), 3),
                    })

    summary = pd.DataFrame(summary_rows)
    worst = pd.DataFrame(worst_rows)
    label = f"btc_partial_risk_{args.profile}_{args.side_mode}_{args.both_mode}"
    summary_txt = out_dir / f"{label}.txt"
    summary_csv = out_dir / f"{label}.csv"
    summary.to_csv(summary_csv, index=False)
    with summary_txt.open("w", encoding="utf-8") as f:
        f.write("BTC partial-exit risk summary\n")
        f.write("=============================\n")
        f.write(f"Profile: {profile.name}\n")
        f.write(f"Side mode: {args.side_mode}, both mode: {args.both_mode}\n")
        f.write("Strategy: BTC only / No-MA / 30% capital / partial exits / Anti-Chase ON\n")
        for years in periods:
            f.write(f"\n===== Recent {years}Y summary =====\n")
            f.write(summary[summary["years"] == years].to_string(index=False))
            f.write("\n")

    for years, rows in monthly_by_period.items():
        monthly = pd.DataFrame(rows)
        monthly_csv = out_dir / f"btc_partial_monthly_pnl_{years}y_{args.profile}_{args.side_mode}_{args.both_mode}.csv"
        monthly_txt = out_dir / f"btc_partial_monthly_pnl_{years}y_{args.profile}_{args.side_mode}_{args.both_mode}.txt"
        monthly.to_csv(monthly_csv, index=False)
        pivot = monthly.pivot(index="month", columns="leverage", values="cell").reindex(columns=[f"{x}x" for x in leverages])
        with monthly_txt.open("w", encoding="utf-8") as f:
            f.write(f"BTC partial monthly PnL recent {years}Y\n")
            f.write("=================================\n")
            f.write("Format: monthly PnL USDT / monthly return %\n\n")
            f.write(pivot.to_string())

    worst_txt = out_dir / f"btc_partial_worst{args.worst_n}_trades_{args.profile}_{args.side_mode}_{args.both_mode}.txt"
    worst_csv = out_dir / f"btc_partial_worst{args.worst_n}_trades_{args.profile}_{args.side_mode}_{args.both_mode}.csv"
    worst.to_csv(worst_csv, index=False)
    with worst_txt.open("w", encoding="utf-8") as f:
        f.write(f"BTC partial worst {args.worst_n} trades by leverage\n")
        f.write("=====================================\n")
        f.write(f"Profile: {profile.name}\n")
        for years in periods:
            for lev in [f"{x}x" for x in leverages]:
                sub = worst[(worst["years"] == years) & (worst["leverage"] == lev)]
                if len(sub) == 0:
                    continue
                f.write(f"\n===== {years}Y / {lev} worst {args.worst_n} =====\n")
                f.write(sub.to_string(index=False))
                f.write("\n")

    (out_dir / "btc_partial_risk_latest.txt").write_text(summary_txt.read_text(encoding="utf-8"), encoding="utf-8")
    print("saved:")
    print(summary_txt)
    for years in periods:
        print(out_dir / f"btc_partial_monthly_pnl_{years}y_{args.profile}_{args.side_mode}_{args.both_mode}.txt")
    print(worst_txt)
    print("\n===== SUMMARY PREVIEW =====")
    print(summary.to_string(index=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BTC No-MA partial-exit backtest tools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("final", "risk"):
        p = sub.add_parser(name)
        p.add_argument("--profile", default=os.getenv("PARTIAL_PROFILE", "p50_be_25"), choices=sorted(PROFILES))
        p.add_argument("--side-mode", default=os.getenv("SIDE_MODE", "ls"))
        p.add_argument("--both-mode", default=os.getenv("BOTH_MODE", "stronger"), choices=["skip", "stronger", "candle"])
        p.add_argument("--leverages", default=os.getenv("LEVERAGES", "1-10"))
        p.add_argument("--k", default=os.getenv("BT_K_VALUE", "0.50"))
        p.add_argument("--sl", default=os.getenv("BT_ATR_STOP_MULT", "1.30"))
        p.add_argument("--min-breakout-atr", default=os.getenv("BT_SURVIVAL_MIN_BREAKOUT_ATR", "0.05"))
        p.add_argument("--max-entry-extension-atr", default=os.getenv("BT_MAX_ENTRY_EXTENSION_ATR", "0.40"))
        p.add_argument("--refresh", action="store_true")
    sub.choices["final"].add_argument("--start-years", type=int, default=1)
    sub.choices["final"].add_argument("--end-years", type=int, default=5)
    sub.choices["risk"].add_argument("--periods", default="1,3")
    sub.choices["risk"].add_argument("--worst-n", type=int, default=10)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd == "final":
        run_final(args)
    elif args.cmd == "risk":
        run_risk(args)
    else:  # pragma: no cover
        raise SystemExit(2)


if __name__ == "__main__":
    main()
