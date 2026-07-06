from __future__ import annotations

import argparse
import csv
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Iterable, Literal

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore

from index_sniper.backtest.data import load_or_fetch_symbol
from index_sniper.strategy.indicators import Candle

ROOT = Path.cwd()
if load_dotenv is not None:
    try:
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

Side = Literal["long", "short"]
ExitMode = Literal["next_open", "open_stop_conservative", "close_fail"]
BothMode = Literal["skip", "stronger", "candle"]


@dataclass
class LarryConfig:
    initial_equity: float = 1374.0
    capital_ratio: float = 0.30
    leverage: float = 5.0
    max_order_notional_usdt: float = 999999.0
    k_value: float = 0.50
    taker_fee_rate: float = 0.0006
    slippage_bps: float = 2.0
    both_mode: BothMode = "stronger"
    exit_mode: ExitMode = "next_open"
    max_new_positions_per_day: int = 1
    record_signals: bool = True


@dataclass
class LarrySignal:
    date: str
    symbol: str
    status: str
    side: str
    open: float
    high: float
    low: float
    close: float
    previous_high: float
    previous_low: float
    previous_range: float
    long_target: float
    short_target: float
    long_hit: bool
    short_hit: bool
    reason: str


@dataclass
class LarryTrade:
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
    return_on_notional_pct: float
    return_on_equity_pct: float
    exit_reason: str
    k_value: float
    exit_mode: str
    both_mode: str


@dataclass
class LarryCurvePoint:
    date: str
    equity: float
    drawdown_pct: float
    trade_count: int


def _date(c: Candle) -> str:
    return datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc).date().isoformat()


def _slip(price: float, bps: float, adverse: int) -> float:
    # adverse=+1 means buyer pays more / short-cover pays more.
    # adverse=-1 means seller receives less / long-exit sells lower.
    return price * (1.0 + adverse * (bps / 10000.0))


def _split_csv(text: str) -> list[str]:
    return [x.strip() for x in str(text or "").split(",") if x.strip()]


def _parse_ints(text: str) -> list[int]:
    return [int(x) for x in _split_csv(text)]


def _parse_floats(text: str) -> list[float]:
    return [float(x) for x in _split_csv(text)]


def _targets(candles: list[Candle], idx: int, k: float) -> tuple[float, float, float]:
    cur = candles[idx]
    prev = candles[idx - 1]
    prev_range = max(prev.high - prev.low, 0.0)
    return cur.open + prev_range * k, cur.open - prev_range * k, prev_range


def _choose_side(c: Candle, long_target: float, short_target: float, mode: BothMode) -> tuple[Side | None, str, float]:
    long_hit = c.high >= long_target
    short_hit = c.low <= short_target
    if long_hit and short_hit:
        if mode == "skip":
            return None, "both_targets_touched_skip_intraday_order_unknown", 0.0
        if mode == "candle":
            if c.close >= c.open:
                return "long", "both_targets_touched_choose_long_green_candle", max(0.0, c.high - long_target)
            return "short", "both_targets_touched_choose_short_red_candle", max(0.0, short_target - c.low)
        # stronger: choose larger same-day follow-through.
        long_strength = max(0.0, c.high - long_target)
        short_strength = max(0.0, short_target - c.low)
        if long_strength >= short_strength:
            return "long", "both_targets_touched_choose_long_stronger", long_strength
        return "short", "both_targets_touched_choose_short_stronger", short_strength
    if long_hit:
        return "long", "upper_volatility_breakout", max(0.0, c.high - long_target)
    if short_hit:
        return "short", "lower_volatility_breakout", max(0.0, short_target - c.low)
    return None, "no_breakout", 0.0


def _entry_price(side: Side, target: float, cfg: LarryConfig) -> float:
    if side == "long":
        return _slip(target, cfg.slippage_bps, +1)
    return _slip(target, cfg.slippage_bps, -1)


def _exit_price(side: Side, raw_price: float, cfg: LarryConfig) -> float:
    if side == "long":
        return _slip(raw_price, cfg.slippage_bps, -1)
    return _slip(raw_price, cfg.slippage_bps, +1)


def _size(equity: float, entry_price: float, symbol_count: int, cfg: LarryConfig) -> tuple[float, float]:
    capital = equity * cfg.capital_ratio / max(symbol_count, 1)
    notional = min(capital * cfg.leverage, cfg.max_order_notional_usdt)
    if entry_price <= 0 or notional <= 0:
        return 0.0, 0.0
    return notional / entry_price, notional


def _calc_trade(
    *,
    symbol: str,
    side: Side,
    entry_date: str,
    exit_date: str,
    entry_raw: float,
    exit_raw: float,
    exit_reason: str,
    equity_before: float,
    symbol_count: int,
    cfg: LarryConfig,
) -> LarryTrade | None:
    entry = _entry_price(side, entry_raw, cfg)
    qty, notional = _size(equity_before, entry, symbol_count, cfg)
    if qty <= 0 or notional <= 0:
        return None
    exit_px = _exit_price(side, exit_raw, cfg)
    gross = (exit_px - entry) * qty if side == "long" else (entry - exit_px) * qty
    fees = (abs(entry * qty) + abs(exit_px * qty)) * cfg.taker_fee_rate
    net = gross - fees
    return LarryTrade(
        symbol=symbol,
        side=side,
        entry_date=entry_date,
        exit_date=exit_date,
        entry_price=round(entry, 8),
        exit_price=round(exit_px, 8),
        qty=round(qty, 10),
        notional=round(notional, 8),
        pnl=round(gross, 8),
        fees=round(fees, 8),
        net_pnl=round(net, 8),
        return_on_notional_pct=round((net / notional) * 100.0 if notional else 0.0, 8),
        return_on_equity_pct=round((net / equity_before) * 100.0 if equity_before else 0.0, 8),
        exit_reason=exit_reason,
        k_value=cfg.k_value,
        exit_mode=cfg.exit_mode,
        both_mode=cfg.both_mode,
    )


def _exit_raw_for_mode(side: Side, cur: Candle, next_candle: Candle | None, cfg: LarryConfig) -> tuple[float, str, str]:
    """Return raw exit price, exit date, reason.

    v3.0 pure principle:
    - No ATR TP.
    - Default profit-taking is next UTC 00:00 open (= KST 09:00).
    - Optional day-open failure stop variants are provided for comparison.

    With daily OHLC data, exact intraday order is unknown. Therefore
    open_stop_conservative is intentionally pessimistic if the day touched both
    entry target and the day-open stop.
    """
    cur_date = _date(cur)
    next_date = _date(next_candle) if next_candle else cur_date
    next_open = next_candle.open if next_candle else cur.close

    if cfg.exit_mode == "open_stop_conservative":
        if side == "long" and cur.low <= cur.open:
            return cur.open, cur_date, "day_open_stop_conservative"
        if side == "short" and cur.high >= cur.open:
            return cur.open, cur_date, "day_open_stop_conservative"
    elif cfg.exit_mode == "close_fail":
        if side == "long" and cur.close <= cur.open:
            return cur.close, cur_date, "close_failed_below_day_open"
        if side == "short" and cur.close >= cur.open:
            return cur.close, cur_date, "close_failed_above_day_open"

    return next_open, next_date, "next_day_open_time_exit"


def run_larry_backtest(symbol_candles: dict[str, list[Candle]], cfg: LarryConfig) -> dict:
    # Convert to a date-indexed loop so multi-symbol tests can choose one candidate per day.
    by_date: dict[str, dict[str, tuple[int, Candle]]] = {}
    for symbol, candles in symbol_candles.items():
        candles = sorted(candles, key=lambda x: x.ts)
        symbol_candles[symbol] = candles
        for i, c in enumerate(candles):
            by_date.setdefault(_date(c), {})[symbol] = (i, c)

    dates = sorted(by_date)
    equity = cfg.initial_equity
    peak = cfg.initial_equity
    max_dd = 0.0
    trades: list[LarryTrade] = []
    signals: list[LarrySignal] = []
    curve: list[LarryCurvePoint] = []
    symbol_count = max(len(symbol_candles), 1)

    for d in dates:
        candidates: list[tuple[float, str, Side, Candle, Candle | None, float, str, float, float, float]] = []
        for symbol, candles in symbol_candles.items():
            item = by_date[d].get(symbol)
            if item is None:
                continue
            idx, cur = item
            if idx < 1:
                continue
            next_c = candles[idx + 1] if idx + 1 < len(candles) else None
            long_t, short_t, prev_range = _targets(candles, idx, cfg.k_value)
            side, reason, strength = _choose_side(cur, long_t, short_t, cfg.both_mode)
            prev = candles[idx - 1]
            long_hit = cur.high >= long_t
            short_hit = cur.low <= short_t
            signals.append(
                LarrySignal(
                    date=d,
                    symbol=symbol,
                    status="ENTRY" if side else "HOLD",
                    side=(side or "").upper(),
                    open=cur.open,
                    high=cur.high,
                    low=cur.low,
                    close=cur.close,
                    previous_high=prev.high,
                    previous_low=prev.low,
                    previous_range=prev_range,
                    long_target=long_t,
                    short_target=short_t,
                    long_hit=long_hit,
                    short_hit=short_hit,
                    reason=reason,
                )
            )
            if side is None:
                continue
            # If there are multiple symbols on the same day, choose the strongest normalized follow-through.
            norm_strength = strength / prev_range if prev_range > 0 else 0.0
            candidates.append((norm_strength, symbol, side, cur, next_c, long_t if side == "long" else short_t, reason, long_t, short_t, prev_range))

        candidates.sort(key=lambda x: x[0], reverse=True)
        entries = 0
        for _score, symbol, side, cur, next_c, entry_raw, reason, long_t, short_t, prev_range in candidates:
            if entries >= cfg.max_new_positions_per_day:
                continue
            exit_raw, exit_date, exit_reason = _exit_raw_for_mode(side, cur, next_c, cfg)
            trade = _calc_trade(
                symbol=symbol,
                side=side,
                entry_date=d,
                exit_date=exit_date,
                entry_raw=entry_raw,
                exit_raw=exit_raw,
                exit_reason=exit_reason,
                equity_before=equity,
                symbol_count=symbol_count,
                cfg=cfg,
            )
            if trade is None:
                continue
            equity += trade.net_pnl
            trades.append(trade)
            entries += 1

        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0 if peak else 0.0
        max_dd = max(max_dd, dd)
        curve.append(LarryCurvePoint(date=d, equity=round(equity, 8), drawdown_pct=round(dd, 8), trade_count=len(trades)))

    return _summary(cfg, curve, trades, signals, max_dd)


def _max_loss_streak(trades: Iterable[LarryTrade]) -> int:
    worst = cur = 0
    for t in trades:
        if t.net_pnl < 0:
            cur += 1
            worst = max(worst, cur)
        else:
            cur = 0
    return worst


def _month_key(date_s: str) -> str:
    return date_s[:7]


def _year_key(date_s: str) -> str:
    return date_s[:4]


def _period_returns(curve: list[LarryCurvePoint], key_func) -> dict[str, float]:
    if not curve:
        return {}
    grouped: dict[str, list[float]] = {}
    for p in curve:
        grouped.setdefault(key_func(p.date), []).append(p.equity)
    out: dict[str, float] = {}
    prev_equity = None
    for k in sorted(grouped):
        end = grouped[k][-1]
        if prev_equity is None:
            prev_equity = grouped[k][0]
        start = prev_equity
        out[k] = round((end / start - 1.0) * 100.0 if start else 0.0, 6)
        prev_equity = end
    return out


def _summary(cfg: LarryConfig, curve: list[LarryCurvePoint], trades: list[LarryTrade], signals: list[LarrySignal], max_dd: float) -> dict:
    start = cfg.initial_equity
    end = curve[-1].equity if curve else start
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    by_symbol: dict[str, dict] = {}
    for t in trades:
        b = by_symbol.setdefault(t.symbol, {"trades": 0, "wins": 0, "net_pnl": 0.0})
        b["trades"] += 1
        b["wins"] += 1 if t.net_pnl > 0 else 0
        b["net_pnl"] += t.net_pnl
    for b in by_symbol.values():
        b["win_rate_pct"] = round((b["wins"] / b["trades"] * 100.0) if b["trades"] else 0.0, 6)
        b["net_pnl"] = round(b["net_pnl"], 8)
    return {
        "strategy": "v3.0 Larry Pure No-MA Daily Reset",
        "rules": {
            "entry": "today_open +/- previous_day_range * K",
            "indicators": "none",
            "take_profit": "none; time exit at next UTC 00:00 open / KST 09:00",
            "stop_loss": cfg.exit_mode,
            "note": "open_stop_conservative is pessimistic with daily OHLC because intraday order is unknown",
        },
        "config": asdict(cfg),
        "start_equity": round(start, 8),
        "end_equity": round(end, 8),
        "return_pct": round((end / start - 1.0) * 100.0 if start else 0.0, 6),
        "max_drawdown_pct": round(max_dd, 6),
        "trade_count": len(trades),
        "win_rate_pct": round((len(wins) / len(trades) * 100.0) if trades else 0.0, 6),
        "profit_factor": round((gross_win / gross_loss) if gross_loss else (math.inf if gross_win > 0 else 0.0), 6),
        "avg_net_pnl": round(mean([t.net_pnl for t in trades]), 8) if trades else 0.0,
        "max_loss_streak": _max_loss_streak(trades),
        "by_symbol": by_symbol,
        "monthly_returns_pct": _period_returns(curve, _month_key),
        "yearly_returns_pct": _period_returns(curve, _year_key),
        "curve": [asdict(x) for x in curve],
        "trades": [asdict(x) for x in trades],
        "signals": [asdict(x) for x in signals],
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def write_result(result: dict, out_dir: Path, label: str) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary_json": out_dir / f"larry_pure_summary_{label}.json",
        "summary_txt": out_dir / f"larry_pure_summary_{label}.txt",
        "trades_csv": out_dir / f"larry_pure_trades_{label}.csv",
        "equity_csv": out_dir / f"larry_pure_equity_{label}.csv",
        "signals_csv": out_dir / f"larry_pure_signals_{label}.csv",
    }
    payload = {k: v for k, v in result.items() if k not in {"curve", "trades", "signals"}}
    paths["summary_json"].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with paths["summary_txt"].open("w", encoding="utf-8") as f:
        f.write("Index Sniper Pro v3.0 Larry Pure No-MA Daily Reset\n")
        f.write("=====================================================\n")
        for key in ["start_equity", "end_equity", "return_pct", "max_drawdown_pct", "trade_count", "win_rate_pct", "profit_factor", "avg_net_pnl", "max_loss_streak"]:
            f.write(f"{key}: {result.get(key)}\n")
        f.write("\nRules:\n")
        for k, v in result.get("rules", {}).items():
            f.write(f"- {k}: {v}\n")
        f.write("\nConfig:\n")
        for k, v in result.get("config", {}).items():
            f.write(f"- {k}: {v}\n")
        f.write("\nBy symbol:\n")
        for sym, data in result.get("by_symbol", {}).items():
            f.write(f"- {sym}: {data}\n")
        months = result.get("monthly_returns_pct", {})
        worst_months = sorted(months.items(), key=lambda x: x[1])[:10]
        best_months = sorted(months.items(), key=lambda x: x[1], reverse=True)[:10]
        f.write("\nWorst months:\n")
        for m, r in worst_months:
            f.write(f"- {m}: {r}%\n")
        f.write("\nBest months:\n")
        for m, r in best_months:
            f.write(f"- {m}: {r}%\n")
    _write_csv(paths["trades_csv"], result.get("trades", []))
    _write_csv(paths["equity_csv"], result.get("curve", []))
    _write_csv(paths["signals_csv"], result.get("signals", []))
    # latest aliases for easy cat/open
    for p in paths.values():
        alias = out_dir / p.name.replace(f"_{label}", "_latest")
        alias.write_bytes(p.read_bytes())
    return paths


def _load_data(symbols: list[str], years: int, refresh: bool = False) -> dict[str, list[Candle]]:
    data_dir = ROOT / "backtests" / "data"
    out: dict[str, list[Candle]] = {}
    for symbol in symbols:
        r = load_or_fetch_symbol(symbol, years, data_dir, refresh=refresh)
        out[symbol] = r.candles
    return out


def _cell(result: dict) -> str:
    return f"{float(result['end_equity']):,.0f} / {float(result['max_drawdown_pct']):.1f}% / PF {float(result['profit_factor']):.2f}"


def cmd_run(args: argparse.Namespace) -> None:
    symbols = _split_csv(args.symbols)
    cfg = LarryConfig(
        initial_equity=args.initial_equity,
        capital_ratio=args.capital_ratio,
        leverage=args.leverage,
        max_order_notional_usdt=args.max_notional,
        k_value=args.k,
        taker_fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        both_mode=args.both_mode,
        exit_mode=args.exit_mode,
        max_new_positions_per_day=args.max_new_positions_per_day,
        record_signals=not args.no_signals,
    )
    candles = _load_data(symbols, args.years, refresh=args.refresh)
    result = run_larry_backtest(candles, cfg)
    label = f"{'+'.join(symbols)}_{args.years}y_k{args.k:g}_{args.leverage:g}x_{args.exit_mode}_{args.both_mode}".replace("/", "-")
    paths = write_result(result, ROOT / "backtests" / "v30_larry_pure", label)
    print(paths["summary_txt"].read_text(encoding="utf-8"))
    print("saved:", paths["summary_txt"])


def cmd_sweep(args: argparse.Namespace) -> None:
    symbols = _split_csv(args.symbols)
    years_list = _parse_ints(args.years)
    leverages = _parse_floats(args.leverages)
    exit_modes = _split_csv(args.exit_modes)
    out_dir = ROOT / "backtests" / "v30_larry_pure"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for years in years_list:
        candles = _load_data(symbols, years, refresh=args.refresh)
        for exit_mode in exit_modes:
            for lev in leverages:
                cfg = LarryConfig(
                    initial_equity=args.initial_equity,
                    capital_ratio=args.capital_ratio,
                    leverage=lev,
                    max_order_notional_usdt=args.max_notional,
                    k_value=args.k,
                    taker_fee_rate=args.fee_rate,
                    slippage_bps=args.slippage_bps,
                    both_mode=args.both_mode,
                    exit_mode=exit_mode,  # type: ignore[arg-type]
                    record_signals=False,
                )
                result = run_larry_backtest({s: list(c) for s, c in candles.items()}, cfg)
                rows.append(
                    {
                        "symbols": "+".join(symbols),
                        "years": years,
                        "k": args.k,
                        "leverage": f"{lev:g}x",
                        "exit_mode": exit_mode,
                        "both_mode": args.both_mode,
                        "cell": _cell(result),
                        "end_equity": result["end_equity"],
                        "return_pct": result["return_pct"],
                        "mdd_pct": result["max_drawdown_pct"],
                        "trade_count": result["trade_count"],
                        "win_rate_pct": result["win_rate_pct"],
                        "profit_factor": result["profit_factor"],
                        "max_loss_streak": result["max_loss_streak"],
                    }
                )
    csv_path = out_dir / "larry_pure_sweep_latest.csv"
    _write_csv(csv_path, rows)
    txt_path = out_dir / "larry_pure_sweep_latest.txt"
    with txt_path.open("w", encoding="utf-8") as f:
        f.write("Index Sniper Pro v3.0 Larry Pure Sweep\n")
        f.write("========================================\n")
        f.write("Format: final equity / MDD / PF\n")
        f.write(f"symbols={','.join(symbols)} k={args.k} capital_ratio={args.capital_ratio} both_mode={args.both_mode}\n")
        f.write("Rules: No MA, no ATR TP/SL, previous range breakout, next-open time exit variants\n\n")
        for exit_mode in exit_modes:
            f.write(f"\n===== exit_mode: {exit_mode} =====\n")
            subset = [r for r in rows if r["exit_mode"] == exit_mode]
            lev_cols = [f"{x:g}x" for x in leverages]
            header = ["years"] + lev_cols
            widths = {h: max(len(h), 12) for h in header}
            table: list[dict[str, str]] = []
            for y in years_list:
                line = {"years": str(y)}
                for col in lev_cols:
                    hit = next((r for r in subset if r["years"] == y and r["leverage"] == col), None)
                    line[col] = str(hit["cell"]) if hit else "-"
                table.append(line)
                for h in header:
                    widths[h] = max(widths[h], len(line[h]))
            f.write("  ".join(h.ljust(widths[h]) for h in header) + "\n")
            f.write("  ".join("-" * widths[h] for h in header) + "\n")
            for line in table:
                f.write("  ".join(line[h].ljust(widths[h]) for h in header) + "\n")
        f.write("\nCSV: " + str(csv_path) + "\n")
    print(txt_path.read_text(encoding="utf-8"))
    print("saved:", txt_path)


def cmd_ksweep(args: argparse.Namespace) -> None:
    symbols = _split_csv(args.symbols)
    years_list = _parse_ints(args.years)
    k_values = _parse_floats(args.k_values)
    exit_modes = _split_csv(args.exit_modes)
    out_dir = ROOT / "backtests" / "v30_larry_pure"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for years in years_list:
        candles = _load_data(symbols, years, refresh=args.refresh)
        for exit_mode in exit_modes:
            for k in k_values:
                cfg = LarryConfig(
                    initial_equity=args.initial_equity,
                    capital_ratio=args.capital_ratio,
                    leverage=args.leverage,
                    max_order_notional_usdt=args.max_notional,
                    k_value=k,
                    taker_fee_rate=args.fee_rate,
                    slippage_bps=args.slippage_bps,
                    both_mode=args.both_mode,
                    exit_mode=exit_mode,  # type: ignore[arg-type]
                    record_signals=False,
                )
                result = run_larry_backtest({s: list(c) for s, c in candles.items()}, cfg)
                rows.append(
                    {
                        "symbols": "+".join(symbols),
                        "years": years,
                        "k": f"{k:g}",
                        "leverage": f"{args.leverage:g}x",
                        "exit_mode": exit_mode,
                        "both_mode": args.both_mode,
                        "cell": _cell(result),
                        "end_equity": result["end_equity"],
                        "return_pct": result["return_pct"],
                        "mdd_pct": result["max_drawdown_pct"],
                        "trade_count": result["trade_count"],
                        "win_rate_pct": result["win_rate_pct"],
                        "profit_factor": result["profit_factor"],
                        "max_loss_streak": result["max_loss_streak"],
                    }
                )
    csv_path = out_dir / "larry_pure_k_sweep_latest.csv"
    _write_csv(csv_path, rows)
    txt_path = out_dir / "larry_pure_k_sweep_latest.txt"
    with txt_path.open("w", encoding="utf-8") as f:
        f.write("Index Sniper Pro v3.0 Larry Pure K Sweep\n")
        f.write("==========================================\n")
        f.write("Format: final equity / MDD / PF\n")
        f.write(f"symbols={','.join(symbols)} leverage={args.leverage:g}x capital_ratio={args.capital_ratio} both_mode={args.both_mode}\n\n")
        for exit_mode in exit_modes:
            f.write(f"\n===== exit_mode: {exit_mode} =====\n")
            subset = [r for r in rows if r["exit_mode"] == exit_mode]
            k_cols = [f"{x:g}" for x in k_values]
            header = ["years"] + k_cols
            widths = {h: max(len(h), 12) for h in header}
            table: list[dict[str, str]] = []
            for y in years_list:
                line = {"years": str(y)}
                for col in k_cols:
                    hit = next((r for r in subset if r["years"] == y and r["k"] == col), None)
                    line[col] = str(hit["cell"]) if hit else "-"
                table.append(line)
                for h in header:
                    widths[h] = max(widths[h], len(line[h]))
            f.write("  ".join(h.ljust(widths[h]) for h in header) + "\n")
            f.write("  ".join("-" * widths[h] for h in header) + "\n")
            for line in table:
                f.write("  ".join(line[h].ljust(widths[h]) for h in header) + "\n")
        f.write("\nCSV: " + str(csv_path) + "\n")
    print(txt_path.read_text(encoding="utf-8"))
    print("saved:", txt_path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="v3.0 Larry Pure No-MA Daily Reset backtester")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--symbols", default=os.getenv("BT_LARRY_SYMBOLS", "BTCUSDT"))
        sp.add_argument("--initial-equity", type=float, default=float(os.getenv("BT_LARRY_INITIAL_EQUITY", os.getenv("BT_INITIAL_EQUITY", "1374"))))
        sp.add_argument("--capital-ratio", type=float, default=float(os.getenv("BT_LARRY_CAPITAL_RATIO", os.getenv("BT_CAPITAL_RATIO", "0.30"))))
        sp.add_argument("--max-notional", type=float, default=float(os.getenv("BT_LARRY_MAX_NOTIONAL", os.getenv("BT_OPT_MAX_ORDER_NOTIONAL_USDT", "999999"))))
        sp.add_argument("--fee-rate", type=float, default=float(os.getenv("BT_LARRY_FEE_RATE", "0.0006")))
        sp.add_argument("--slippage-bps", type=float, default=float(os.getenv("BT_LARRY_SLIPPAGE_BPS", "2.0")))
        sp.add_argument("--both-mode", choices=["skip", "stronger", "candle"], default=os.getenv("BT_LARRY_BOTH_MODE", "stronger"))
        sp.add_argument("--refresh", action="store_true")

    run = sub.add_parser("run")
    add_common(run)
    run.add_argument("--years", type=int, default=int(os.getenv("BT_LARRY_YEARS_ONE", "5")))
    run.add_argument("--leverage", type=float, default=float(os.getenv("BT_LARRY_LEVERAGE", os.getenv("BT_LEVERAGE", "5"))))
    run.add_argument("--k", type=float, default=float(os.getenv("BT_LARRY_K", os.getenv("BT_K_VALUE", "0.50"))))
    run.add_argument("--exit-mode", choices=["next_open", "open_stop_conservative", "close_fail"], default=os.getenv("BT_LARRY_EXIT_MODE", "next_open"))
    run.add_argument("--max-new-positions-per-day", type=int, default=int(os.getenv("BT_LARRY_MAX_NEW_POSITIONS_PER_DAY", "1")))
    run.add_argument("--no-signals", action="store_true")
    run.set_defaults(func=cmd_run)

    sweep = sub.add_parser("sweep")
    add_common(sweep)
    sweep.add_argument("--years", default=os.getenv("BT_LARRY_YEARS", "1,2,3,4,5"))
    sweep.add_argument("--leverages", default=os.getenv("BT_LARRY_LEVERAGES", "1,2,3,4,5,6,7,8,9,10"))
    sweep.add_argument("--k", type=float, default=float(os.getenv("BT_LARRY_K", os.getenv("BT_K_VALUE", "0.50"))))
    sweep.add_argument("--exit-modes", default=os.getenv("BT_LARRY_EXIT_MODES", "next_open,open_stop_conservative,close_fail"))
    sweep.set_defaults(func=cmd_sweep)

    ksweep = sub.add_parser("ksweep")
    add_common(ksweep)
    ksweep.add_argument("--years", default=os.getenv("BT_LARRY_YEARS", "1,2,3,4,5"))
    ksweep.add_argument("--leverage", type=float, default=float(os.getenv("BT_LARRY_LEVERAGE", os.getenv("BT_LEVERAGE", "5"))))
    ksweep.add_argument("--k-values", default=os.getenv("BT_LARRY_K_VALUES", "0.25,0.35,0.50,0.65,0.80,1.00"))
    ksweep.add_argument("--exit-modes", default=os.getenv("BT_LARRY_EXIT_MODES", "next_open,open_stop_conservative,close_fail"))
    ksweep.set_defaults(func=cmd_ksweep)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
