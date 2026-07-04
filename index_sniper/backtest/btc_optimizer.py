from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

from index_sniper.backtest.data import load_or_fetch_symbol
from index_sniper.backtest.engine import BacktestConfig, run_portfolio_backtest, write_results
from index_sniper.backtest.runner import cfg_from_env

ROOT = Path(__file__).resolve().parents[2]
SYMBOL = "BTCUSDT"


def _safe_float(x: Any) -> float:
    try:
        if x is None:
            return 0.0
        if isinstance(x, str) and x.lower() == "inf":
            return math.inf
        return float(x)
    except Exception:
        return 0.0


def _parse_float_list(text: str | None, default: Iterable[float]) -> list[float]:
    if not text:
        return list(default)
    out: list[float] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


def _parse_int_range_or_list(text: str | None, default: Iterable[int]) -> list[int]:
    if not text:
        return list(default)
    out: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
            step = 1 if end >= start else -1
            out.extend(list(range(start, end + step, step)))
        else:
            out.append(int(part))
    # stable de-dup
    seen = set()
    uniq = []
    for x in out:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq


def _parse_ema_pairs(text: str | None, default: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    if not text:
        return list(default)
    out: list[tuple[int, int]] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            a, b = part.split("/", 1)
        elif ":" in part:
            a, b = part.split(":", 1)
        else:
            raise ValueError(f"EMA pair must be fast/slow, got {part}")
        fast, slow = int(a), int(b)
        if fast <= 0 or slow <= 0 or fast >= slow:
            raise ValueError(f"EMA pair must satisfy 0 < fast < slow, got {part}")
        out.append((fast, slow))
    return out


def _max_win_streak(trades: list[dict]) -> int:
    best = cur = 0
    for t in trades:
        if _safe_float(t.get("net_pnl")) > 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _max_loss_streak(trades: list[dict]) -> int:
    best = cur = 0
    for t in trades:
        if _safe_float(t.get("net_pnl")) < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _side_cfg(base: BacktestConfig, side_mode: str) -> BacktestConfig:
    side_mode = side_mode.strip().lower()
    if side_mode in {"ls", "longshort", "long_short", "both"}:
        return replace(base, long_only_symbols=(), short_only_symbols=(), long_disabled_symbols=(), short_disabled_symbols=())
    if side_mode in {"long", "long_only"}:
        return replace(base, long_only_symbols=(SYMBOL,), short_only_symbols=(), long_disabled_symbols=(), short_disabled_symbols=())
    if side_mode in {"short", "short_only"}:
        return replace(base, long_only_symbols=(), short_only_symbols=(SYMBOL,), long_disabled_symbols=(), short_disabled_symbols=())
    raise ValueError(f"unknown side mode: {side_mode}")


def _strategy_id(row: dict[str, Any]) -> str:
    keys = [
        "side_mode", "ma_mode", "no_ma_both_breakout_mode", "leverage", "k_value", "ema_fast", "ema_slow", "atr_stop_mult", "atr_take_profit_mult",
        "survival_min_breakout_atr", "max_entry_extension_atr", "anti_chase_enabled",
        "anti_chase_extreme_up_pct", "anti_chase_extreme_down_pct", "anti_chase_extreme_range_atr",
    ]
    return "|".join(f"{k}={row.get(k)}" for k in keys)


def _row_from_result(result: dict, years: int, side_mode: str, cfg: BacktestConfig) -> dict[str, Any]:
    start = _safe_float(result.get("start_equity"))
    end = _safe_float(result.get("end_equity"))
    ret = _safe_float(result.get("return_pct"))
    mdd = _safe_float(result.get("max_drawdown_pct"))
    trades = result.get("trades", []) or []
    by_symbol = result.get("by_symbol", {}) or {}
    cagr = ((end / start) ** (1.0 / years) - 1.0) * 100.0 if start > 0 and end > 0 and years > 0 else 0.0
    calmar = (cagr / mdd) if mdd > 0 else (math.inf if cagr > 0 else 0.0)
    row = {
        "years": years,
        "symbol": SYMBOL,
        "side_mode": side_mode,
        "ma_mode": "ema" if cfg.use_ema_filter else "none",
        "use_ema_filter": cfg.use_ema_filter,
        "no_ma_both_breakout_mode": cfg.no_ma_both_breakout_mode,
        "start_equity": round(start, 6),
        "end_equity": round(end, 6),
        "return_pct": round(ret, 6),
        "cagr_pct": round(cagr, 6),
        "max_drawdown_pct": round(mdd, 6),
        "return_over_mdd": round(ret / mdd, 6) if mdd > 0 else None,
        "calmar": round(calmar, 6) if math.isfinite(calmar) else "inf",
        "trade_count": result.get("trade_count"),
        "win_rate_pct": result.get("win_rate_pct"),
        "profit_factor": result.get("profit_factor"),
        "avg_net_pnl": result.get("avg_net_pnl"),
        "max_win_streak": _max_win_streak(trades),
        "max_loss_streak": _max_loss_streak(trades),
        "btc_net_pnl": by_symbol.get(SYMBOL, {}).get("net_pnl", 0.0),
        "leverage": cfg.leverage,
        "capital_ratio": cfg.capital_ratio,
        "max_order_notional_usdt": cfg.max_order_notional_usdt,
        "k_value": cfg.k_value,
        "ema_fast": cfg.ema_fast,
        "ema_slow": cfg.ema_slow,
        "atr_stop_mult": cfg.atr_stop_mult,
        "atr_take_profit_mult": cfg.atr_take_profit_mult,
        "survival_min_breakout_atr": cfg.survival_min_breakout_atr,
        "max_entry_extension_atr": cfg.max_entry_extension_atr,
        "anti_chase_enabled": cfg.anti_chase_enabled,
        "anti_chase_extreme_up_pct": cfg.anti_chase_extreme_up_pct,
        "anti_chase_extreme_down_pct": cfg.anti_chase_extreme_down_pct,
        "anti_chase_extreme_range_atr": cfg.anti_chase_extreme_range_atr,
    }
    row["strategy_id"] = _strategy_id(row)
    return row


def _write_rows_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    for row in rows:
        for k in row.keys():
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def _rank_value(row: dict, key: str) -> float:
    return _safe_float(row.get(key))


def _write_summary_txt(path: Path, rows: list[dict], years: int, base: BacktestConfig, preset: str, grid_count: int, top_n: int) -> None:
    ranked_calmar = sorted(rows, key=lambda r: (_rank_value(r, "calmar"), _rank_value(r, "return_pct"), _rank_value(r, "profit_factor")), reverse=True)
    ranked_return = sorted(rows, key=lambda r: (_rank_value(r, "return_pct"), -_rank_value(r, "max_drawdown_pct"), _rank_value(r, "profit_factor")), reverse=True)
    ranked_pf = sorted(rows, key=lambda r: (_rank_value(r, "profit_factor"), _rank_value(r, "return_pct")), reverse=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("Index Sniper Pro BTC Optimizer Summary\n")
        f.write("======================================\n")
        f.write(f"years: {years}\n")
        f.write(f"symbol: {SYMBOL}\n")
        f.write(f"initial_equity: {base.initial_equity}\n")
        f.write(f"capital_ratio: {base.capital_ratio}\n")
        f.write(f"max_order_notional_usdt: {base.max_order_notional_usdt}\n")
        f.write(f"preset: {preset}\n")
        f.write(f"grid_count: {grid_count}\n")
        f.write("\nRanked by Calmar = CAGR / MDD:\n")
        for i, r in enumerate(ranked_calmar[:top_n], start=1):
            f.write(_format_rank_line(i, r) + "\n")
        f.write("\nRanked by total return:\n")
        for i, r in enumerate(ranked_return[:top_n], start=1):
            f.write(_format_rank_line(i, r) + "\n")
        f.write("\nRanked by Profit Factor:\n")
        for i, r in enumerate(ranked_pf[:top_n], start=1):
            f.write(_format_rank_line(i, r) + "\n")


def _format_rank_line(i: int, r: dict) -> str:
    ma_label = "EMA " + str(r.get("ema_fast")) + "/" + str(r.get("ema_slow")) if r.get("ma_mode") == "ema" else "NO_MA both=" + str(r.get("no_ma_both_breakout_mode"))
    return (
        f"{i:02d}. {r['side_mode']} {ma_label} lev {r['leverage']}x K {r['k_value']} "
        f"SL {r['atr_stop_mult']} TP {r['atr_take_profit_mult']} EXT {r['max_entry_extension_atr']} "
        f"AC {r['anti_chase_extreme_up_pct']}/{r['anti_chase_extreme_range_atr']} | "
        f"ret {r['return_pct']}% CAGR {r['cagr_pct']}% MDD {r['max_drawdown_pct']}% "
        f"Calmar {r['calmar']} PF {r['profit_factor']} trades {r['trade_count']} "
        f"loss_streak {r['max_loss_streak']}"
    )


def _grid_from_args(args) -> list[dict[str, Any]]:
    preset = args.preset.lower()
    if preset == "leverage":
        defaults = {
            "leverages": list(range(1, 11)),
            "k_values": [0.50],
            "ema_pairs": [(20, 60)],
            "stop_values": [1.30],
            "tp_values": [2.00],
            "min_breakout_values": [0.05],
            "extension_values": [0.40],
            "anti_up_values": [7.0],
            "anti_range_values": [1.8],
            "side_modes": ["ls", "long", "short"],
            "anti_enabled_values": [True],
            "ma_modes": ["ema"],
            "no_ma_both_modes": ["skip"],
        }
    elif preset == "no_ma_leverage":
        defaults = {
            "leverages": list(range(1, 11)),
            "k_values": [0.50],
            "ema_pairs": [(20, 60)],
            "stop_values": [1.30],
            "tp_values": [2.00],
            "min_breakout_values": [0.05],
            "extension_values": [0.40],
            "anti_up_values": [7.0],
            "anti_range_values": [1.8],
            "side_modes": ["ls", "long", "short"],
            "anti_enabled_values": [True],
            "ma_modes": ["none"],
            "no_ma_both_modes": ["skip", "stronger"],
        }
    elif preset == "no_ma_quick":
        defaults = {
            "leverages": list(range(1, 11)),
            "k_values": [0.25, 0.35, 0.50, 0.65, 0.80],
            "ema_pairs": [(20, 60)],
            "stop_values": [0.8, 1.0, 1.3, 1.6],
            "tp_values": [1.5, 2.0, 2.8, 3.5],
            "min_breakout_values": [0.0, 0.05, 0.10],
            "extension_values": [0.25, 0.40, 0.65],
            "anti_up_values": [5.0, 7.0, 10.0],
            "anti_range_values": [1.5, 1.8, 2.2],
            "side_modes": ["ls", "long", "short"],
            "anti_enabled_values": [True],
            "ma_modes": ["none"],
            "no_ma_both_modes": ["skip", "stronger"],
        }
    elif preset == "ma_mix":
        defaults = {
            "leverages": list(range(1, 11)),
            "k_values": [0.35, 0.50, 0.65],
            "ema_pairs": [(10, 40), (20, 60), (30, 90)],
            "stop_values": [1.0, 1.3, 1.6],
            "tp_values": [2.0, 2.8, 3.5],
            "min_breakout_values": [0.05],
            "extension_values": [0.25, 0.40, 0.65],
            "anti_up_values": [7.0],
            "anti_range_values": [1.8],
            "side_modes": ["ls", "long", "short"],
            "anti_enabled_values": [True],
            "ma_modes": ["ema", "none"],
            "no_ma_both_modes": ["skip", "stronger"],
        }
    elif preset == "no_ma_wide":
        defaults = {
            "leverages": list(range(1, 11)),
            "k_values": [0.20, 0.25, 0.35, 0.45, 0.55, 0.65, 0.80, 1.00],
            "ema_pairs": [(20, 60)],
            "stop_values": [0.6, 0.8, 1.0, 1.3, 1.6, 2.0],
            "tp_values": [1.2, 1.5, 2.0, 2.8, 3.5, 4.5, 6.0],
            "min_breakout_values": [0.0, 0.05, 0.10, 0.20],
            "extension_values": [0.15, 0.25, 0.40, 0.65, 1.00],
            "anti_up_values": [5.0, 7.0, 10.0, 14.0],
            "anti_range_values": [1.2, 1.5, 1.8, 2.2, 3.0],
            "side_modes": ["ls", "long", "short"],
            "anti_enabled_values": [True],
            "ma_modes": ["none"],
            "no_ma_both_modes": ["skip", "stronger", "candle"],
        }
    elif preset == "quick":
        defaults = {
            "leverages": list(range(1, 11)),
            "k_values": [0.35, 0.50, 0.65],
            "ema_pairs": [(10, 40), (20, 60)],
            "stop_values": [1.0, 1.3],
            "tp_values": [2.0, 2.8],
            "min_breakout_values": [0.05],
            "extension_values": [0.40],
            "anti_up_values": [7.0],
            "anti_range_values": [1.8],
            "side_modes": ["ls", "long", "short"],
            "anti_enabled_values": [True],
            "ma_modes": ["ema"],
            "no_ma_both_modes": ["skip"],
        }
    elif preset == "wide":
        defaults = {
            "leverages": list(range(1, 11)),
            "k_values": [0.25, 0.35, 0.45, 0.55, 0.65, 0.80],
            "ema_pairs": [(5, 20), (10, 40), (20, 60), (30, 90), (50, 150)],
            "stop_values": [0.8, 1.0, 1.3, 1.6, 2.0],
            "tp_values": [1.5, 2.0, 2.8, 3.5, 4.5],
            "min_breakout_values": [0.0, 0.05, 0.10],
            "extension_values": [0.25, 0.40, 0.65],
            "anti_up_values": [5.0, 7.0, 10.0],
            "anti_range_values": [1.5, 1.8, 2.2],
            "side_modes": ["ls", "long", "short"],
            "anti_enabled_values": [True],
            "ma_modes": ["ema"],
            "no_ma_both_modes": ["skip"],
        }
    else:  # default
        defaults = {
            "leverages": list(range(1, 11)),
            "k_values": [0.25, 0.35, 0.45, 0.55, 0.65],
            "ema_pairs": [(10, 40), (20, 60), (30, 90)],
            "stop_values": [1.0, 1.3, 1.6],
            "tp_values": [1.8, 2.2, 2.8],
            "min_breakout_values": [0.05],
            "extension_values": [0.25, 0.40],
            "anti_up_values": [5.0, 7.0, 10.0],
            "anti_range_values": [1.8],
            "side_modes": ["ls", "long", "short"],
            "anti_enabled_values": [True],
            "ma_modes": ["ema"],
            "no_ma_both_modes": ["skip"],
        }

    leverages = _parse_int_range_or_list(args.leverages, defaults["leverages"])
    k_values = _parse_float_list(args.k_values, defaults["k_values"])
    ema_pairs = _parse_ema_pairs(args.ema_pairs, defaults["ema_pairs"])
    stop_values = _parse_float_list(args.stop_values, defaults["stop_values"])
    tp_values = _parse_float_list(args.tp_values, defaults["tp_values"])
    min_breakout_values = _parse_float_list(args.min_breakout_values, defaults["min_breakout_values"])
    extension_values = _parse_float_list(args.extension_values, defaults["extension_values"])
    anti_up_values = _parse_float_list(args.anti_up_values, defaults["anti_up_values"])
    anti_range_values = _parse_float_list(args.anti_range_values, defaults["anti_range_values"])
    side_modes = [x.strip().lower() for x in (args.side_modes or ",".join(defaults["side_modes"])).split(",") if x.strip()]
    ma_modes = [x.strip().lower() for x in (args.ma_modes or ",".join(defaults["ma_modes"])).split(",") if x.strip()]
    no_ma_both_modes = [x.strip().lower() for x in (args.no_ma_both_modes or ",".join(defaults["no_ma_both_modes"])).split(",") if x.strip()]
    anti_enabled_values = list(defaults["anti_enabled_values"])
    if args.include_anti_off:
        anti_enabled_values = [True, False]

    grid = []
    for ma_mode, lev, k, (fast, slow), stop, tp, min_b, ext, anti_up, anti_range, side_mode, anti_enabled, both_mode in itertools.product(
        ma_modes, leverages, k_values, ema_pairs, stop_values, tp_values, min_breakout_values, extension_values, anti_up_values, anti_range_values, side_modes, anti_enabled_values, no_ma_both_modes
    ):
        if tp <= 0 or stop <= 0:
            continue
        if ma_mode not in {"ema", "none", "no_ma"}:
            raise ValueError(f"ma mode must be ema or none, got {ma_mode}")
        if both_mode not in {"skip", "stronger", "candle"}:
            raise ValueError(f"no-ma both mode must be skip, stronger, or candle, got {both_mode}")
        # EMA runs do not need multiple no-MA both-hit modes.
        if ma_mode == "ema" and both_mode != "skip":
            continue
        grid.append(
            {
                "ma_mode": "none" if ma_mode in {"none", "no_ma"} else "ema",
                "use_ema_filter": not (ma_mode in {"none", "no_ma"}),
                "no_ma_both_breakout_mode": both_mode,
                "leverage": float(lev),
                "k_value": float(k),
                "ema_fast": int(fast),
                "ema_slow": int(slow),
                "atr_stop_mult": float(stop),
                "atr_take_profit_mult": float(tp),
                "survival_min_breakout_atr": float(min_b),
                "max_entry_extension_atr": float(ext),
                "anti_chase_enabled": bool(anti_enabled),
                "anti_chase_extreme_up_pct": float(anti_up),
                "anti_chase_extreme_down_pct": float(anti_up),
                "anti_chase_extreme_range_atr": float(anti_range),
                "side_mode": side_mode,
            }
        )
    return grid

def _cfg_for_combo(base: BacktestConfig, combo: dict[str, Any]) -> BacktestConfig:
    cfg = replace(
        base,
        leverage=combo["leverage"],
        k_value=combo["k_value"],
        ema_fast=combo["ema_fast"],
        ema_slow=combo["ema_slow"],
        use_ema_filter=combo["use_ema_filter"],
        no_ma_both_breakout_mode=combo["no_ma_both_breakout_mode"],
        atr_stop_mult=combo["atr_stop_mult"],
        atr_take_profit_mult=combo["atr_take_profit_mult"],
        survival_min_breakout_atr=combo["survival_min_breakout_atr"],
        max_entry_extension_atr=combo["max_entry_extension_atr"],
        anti_chase_enabled=combo["anti_chase_enabled"],
        anti_chase_extreme_up_pct=combo["anti_chase_extreme_up_pct"],
        anti_chase_extreme_down_pct=combo["anti_chase_extreme_down_pct"],
        anti_chase_extreme_range_atr=combo["anti_chase_extreme_range_atr"],
        max_open_positions=1,
        max_new_positions_per_day=1,
        record_signals=False,
    )
    return _side_cfg(cfg, combo["side_mode"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Index Sniper Pro v2.4 BTC-only MA/no-MA leverage/parameter optimizer")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--label", default="")
    parser.add_argument("--preset", choices=["leverage", "quick", "default", "wide", "no_ma_leverage", "no_ma_quick", "no_ma_wide", "ma_mix"], default="default")
    parser.add_argument("--leverages", default="1-10", help="e.g. 1-10 or 1,2,3,5,10")
    parser.add_argument("--k-values", default="")
    parser.add_argument("--ema-pairs", default="", help="e.g. 10/40,20/60,30/90")
    parser.add_argument("--ma-modes", default="", help="ema,none. none disables moving-average trend filter")
    parser.add_argument("--no-ma-both-modes", default="", help="skip,stronger,candle for days where both upper/lower targets hit without MA")
    parser.add_argument("--stop-values", default="")
    parser.add_argument("--tp-values", default="")
    parser.add_argument("--min-breakout-values", default="")
    parser.add_argument("--extension-values", default="")
    parser.add_argument("--anti-up-values", default="")
    parser.add_argument("--anti-range-values", default="")
    parser.add_argument("--side-modes", default="", help="ls,long,short")
    parser.add_argument("--include-anti-off", action="store_true")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--top-detail", type=int, default=10, help="rerun and save full trade/equity detail for top N by Calmar and top N by return")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    out_dir = ROOT / "backtests"
    data_dir = out_dir / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    label = args.label or f"btc_optimizer_{args.preset}_{args.years}y_{timestamp}"

    r = load_or_fetch_symbol(SYMBOL, args.years, data_dir, refresh=args.refresh)
    candles = {SYMBOL: r.candles}
    print(f"[data] {SYMBOL}: {r.provider}:{r.provider_symbol} candles={len(r.candles)}")

    base = cfg_from_env(args)
    # Optimizer is intentionally BTC-only. Remove the live order cap by default so 1x..10x actually changes size.
    base = replace(
        base,
        initial_equity=float(os.getenv("BT_INITIAL_EQUITY", os.getenv("BACKTEST_INITIAL_EQUITY", "1374"))),
        capital_ratio=float(os.getenv("BT_CAPITAL_RATIO", "0.30")),
        max_order_notional_usdt=float(os.getenv("BT_OPT_MAX_ORDER_NOTIONAL_USDT", os.getenv("BT_MAX_ORDER_NOTIONAL_USDT", "999999"))),
        max_open_positions=1,
        max_new_positions_per_day=1,
        record_signals=False,
    )

    grid = _grid_from_args(args)
    print(f"[optimizer] preset={args.preset} years={args.years} combinations={len(grid)} capital_ratio={base.capital_ratio} max_notional={base.max_order_notional_usdt}")

    rows: list[dict[str, Any]] = []
    for i, combo in enumerate(grid, start=1):
        cfg = _cfg_for_combo(base, combo)
        result = run_portfolio_backtest(candles, cfg)
        row = _row_from_result(result, args.years, combo["side_mode"], cfg)
        rows.append(row)
        if i % 500 == 0 or i == len(grid):
            print(f"[optimizer] {i}/{len(grid)} done")

    csv_path = out_dir / f"btc_optimizer_{label}.csv"
    txt_path = out_dir / f"btc_optimizer_{label}.txt"
    json_path = out_dir / f"btc_optimizer_{label}.json"
    _write_rows_csv(csv_path, rows)
    _write_summary_txt(txt_path, rows, args.years, base, args.preset, len(grid), args.top)
    json_path.write_text(
        json.dumps({"years": args.years, "symbol": SYMBOL, "config_base": asdict(base), "data_source": {"provider": r.provider, "provider_symbol": r.provider_symbol, "candles": len(r.candles)}, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Latest aliases, plus per-year aliases so a 3y run does not destroy the 5y file needed for comparison.
    for suffix, path in {"csv": csv_path, "txt": txt_path, "json": json_path}.items():
        (out_dir / f"btc_optimizer_latest.{suffix}").write_bytes(path.read_bytes())
        (out_dir / f"btc_optimizer_{args.years}y_latest.{suffix}").write_bytes(path.read_bytes())

    # Save detailed trades/equity for the strongest candidates only.
    top_detail = max(0, int(args.top_detail))
    detail_dir = out_dir / "btc_optimizer_runs" / label
    if top_detail > 0 and rows:
        ranked_calmar = sorted(rows, key=lambda row: (_rank_value(row, "calmar"), _rank_value(row, "return_pct")), reverse=True)[:top_detail]
        ranked_return = sorted(rows, key=lambda row: (_rank_value(row, "return_pct"), -_rank_value(row, "max_drawdown_pct")), reverse=True)[:top_detail]
        selected = []
        seen = set()
        for row in ranked_calmar + ranked_return:
            sid = row["strategy_id"]
            if sid not in seen:
                selected.append(row)
                seen.add(sid)
        detail_dir.mkdir(parents=True, exist_ok=True)
        for n, row in enumerate(selected, start=1):
            combo = {
                "leverage": row["leverage"],
                "k_value": row["k_value"],
                "ema_fast": row["ema_fast"],
                "ema_slow": row["ema_slow"],
                "use_ema_filter": row["use_ema_filter"],
                "no_ma_both_breakout_mode": row["no_ma_both_breakout_mode"],
                "atr_stop_mult": row["atr_stop_mult"],
                "atr_take_profit_mult": row["atr_take_profit_mult"],
                "survival_min_breakout_atr": row["survival_min_breakout_atr"],
                "max_entry_extension_atr": row["max_entry_extension_atr"],
                "anti_chase_enabled": row["anti_chase_enabled"],
                "anti_chase_extreme_up_pct": row["anti_chase_extreme_up_pct"],
                "anti_chase_extreme_down_pct": row["anti_chase_extreme_down_pct"],
                "anti_chase_extreme_range_atr": row["anti_chase_extreme_range_atr"],
                "side_mode": row["side_mode"],
            }
            cfg = replace(_cfg_for_combo(base, combo), record_signals=True)
            result = run_portfolio_backtest(candles, cfg)
            write_results(result, detail_dir, f"{label}_top{n:02d}_{row['side_mode']}_lev{row['leverage']}x_ret{row['return_pct']}")

    print("===== BTC OPTIMIZER SUMMARY =====")
    print(txt_path.read_text(encoding="utf-8"))
    print("===== OUTPUT FILES =====")
    print(f"optimizer_csv: {csv_path}")
    print(f"optimizer_txt: {txt_path}")
    print(f"optimizer_json: {json_path}")
    print(f"latest_txt: {out_dir / 'btc_optimizer_latest.txt'}")
    print(f"latest_{args.years}y_txt: {out_dir / f'btc_optimizer_{args.years}y_latest.txt'}")
    print(f"detail_dir: {detail_dir if detail_dir.exists() else '(skipped)'}")


if __name__ == "__main__":
    main()
