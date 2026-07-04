from __future__ import annotations

import argparse
import csv
import json
import math
import os
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from index_sniper.backtest.data import load_or_fetch_symbol
from index_sniper.backtest.engine import BacktestConfig, run_portfolio_backtest, write_results
from index_sniper.backtest.runner import cfg_from_env

ROOT = Path(__file__).resolve().parents[2]
ALL_SYMBOLS = ["BTCUSDT", "SP500USDT", "NDX100USDT"]


def _scenario_definitions() -> list[dict[str, Any]]:
    return [
        {
            "id": "all_ls",
            "name": "ALL: BTC + SP500 + NDX long/short",
            "symbols": ["BTCUSDT", "SP500USDT", "NDX100USDT"],
        },
        {"id": "btc_only_ls", "name": "BTC only long/short", "symbols": ["BTCUSDT"]},
        {"id": "sp500_only_ls", "name": "SP500 only long/short", "symbols": ["SP500USDT"]},
        {"id": "ndx_only_ls", "name": "NDX100 only long/short", "symbols": ["NDX100USDT"]},
        {"id": "btc_sp500_ls", "name": "BTC + SP500 long/short", "symbols": ["BTCUSDT", "SP500USDT"]},
        {"id": "btc_ndx_ls", "name": "BTC + NDX100 long/short", "symbols": ["BTCUSDT", "NDX100USDT"]},
        {"id": "indices_ls", "name": "SP500 + NDX100 long/short", "symbols": ["SP500USDT", "NDX100USDT"]},
        {
            "id": "indices_long_only",
            "name": "SP500 + NDX100 long-only",
            "symbols": ["SP500USDT", "NDX100USDT"],
            "long_only_symbols": ["SP500USDT", "NDX100USDT"],
        },
        {
            "id": "all_index_long_only",
            "name": "BTC long/short + indices long-only",
            "symbols": ["BTCUSDT", "SP500USDT", "NDX100USDT"],
            "long_only_symbols": ["SP500USDT", "NDX100USDT"],
        },
        {
            "id": "btc_sp500_index_long_only",
            "name": "BTC long/short + SP500 long-only",
            "symbols": ["BTCUSDT", "SP500USDT"],
            "long_only_symbols": ["SP500USDT"],
        },
        {
            "id": "btc_ndx_index_long_only",
            "name": "BTC long/short + NDX100 long-only",
            "symbols": ["BTCUSDT", "NDX100USDT"],
            "long_only_symbols": ["NDX100USDT"],
        },
        {"id": "sp500_long_only", "name": "SP500 only long-only", "symbols": ["SP500USDT"], "long_only_symbols": ["SP500USDT"]},
        {"id": "ndx_long_only", "name": "NDX100 only long-only", "symbols": ["NDX100USDT"], "long_only_symbols": ["NDX100USDT"]},
        {"id": "btc_long_only", "name": "BTC only long-only", "symbols": ["BTCUSDT"], "long_only_symbols": ["BTCUSDT"]},
        {"id": "btc_short_only", "name": "BTC only short-only", "symbols": ["BTCUSDT"], "short_only_symbols": ["BTCUSDT"]},
    ]


def _safe_float(x: Any) -> float:
    try:
        if x is None:
            return 0.0
        if isinstance(x, str) and x.lower() == "inf":
            return math.inf
        return float(x)
    except Exception:
        return 0.0


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


def _scenario_cfg(base: BacktestConfig, s: dict[str, Any]) -> BacktestConfig:
    return replace(
        base,
        long_only_symbols=tuple(x.upper() for x in s.get("long_only_symbols", [])),
        short_only_symbols=tuple(x.upper() for x in s.get("short_only_symbols", [])),
        long_disabled_symbols=tuple(x.upper() for x in s.get("long_disabled_symbols", [])),
        short_disabled_symbols=tuple(x.upper() for x in s.get("short_disabled_symbols", [])),
    )


def _scenario_row(s: dict[str, Any], result: dict, label: str, paths: dict[str, Path]) -> dict[str, Any]:
    start = _safe_float(result.get("start_equity"))
    end = _safe_float(result.get("end_equity"))
    ret = _safe_float(result.get("return_pct"))
    mdd = _safe_float(result.get("max_drawdown_pct"))
    pf = _safe_float(result.get("profit_factor"))
    trades = result.get("trades", []) or []
    by_symbol = result.get("by_symbol", {}) or {}
    return {
        "scenario_id": s["id"],
        "scenario_name": s["name"],
        "symbols": ",".join(s["symbols"]),
        "long_only_symbols": ",".join(s.get("long_only_symbols", [])),
        "short_only_symbols": ",".join(s.get("short_only_symbols", [])),
        "start_equity": round(start, 6),
        "end_equity": round(end, 6),
        "return_pct": round(ret, 6),
        "max_drawdown_pct": round(mdd, 6),
        "return_over_mdd": round(ret / mdd, 6) if mdd > 0 else None,
        "trade_count": result.get("trade_count"),
        "win_rate_pct": result.get("win_rate_pct"),
        "profit_factor": result.get("profit_factor"),
        "avg_net_pnl": result.get("avg_net_pnl"),
        "max_win_streak": _max_win_streak(trades),
        "max_loss_streak": _max_loss_streak(trades),
        "btc_net_pnl": by_symbol.get("BTCUSDT", {}).get("net_pnl", 0.0),
        "sp500_net_pnl": by_symbol.get("SP500USDT", {}).get("net_pnl", 0.0),
        "ndx_net_pnl": by_symbol.get("NDX100USDT", {}).get("net_pnl", 0.0),
        "summary_txt": str(paths.get("summary_txt", "")),
        "trades_csv": str(paths.get("trades_csv", "")),
        "equity_csv": str(paths.get("equity_csv", "")),
    }


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


def _write_matrix_txt(path: Path, rows: list[dict], years: int, base: BacktestConfig) -> None:
    ranked = sorted(rows, key=lambda r: (_safe_float(r.get("return_over_mdd")) if r.get("return_over_mdd") is not None else -999, _safe_float(r.get("return_pct"))), reverse=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("Index Sniper Pro Backtest Matrix Summary\n")
        f.write("========================================\n")
        f.write(f"years: {years}\n")
        f.write(f"initial_equity: {base.initial_equity}\n")
        f.write(f"capital_ratio: {base.capital_ratio}\n")
        f.write(f"leverage: {base.leverage}\n")
        f.write(f"max_order_notional_usdt: {base.max_order_notional_usdt}\n")
        f.write("\nRanked by return/max_drawdown:\n")
        for i, r in enumerate(ranked, start=1):
            f.write(
                f"{i:02d}. {r['scenario_id']} | ret {r['return_pct']}% | MDD {r['max_drawdown_pct']}% | "
                f"ret/MDD {r['return_over_mdd']} | PF {r['profit_factor']} | trades {r['trade_count']} | "
                f"BTC {r['btc_net_pnl']} / SP {r['sp500_net_pnl']} / NDX {r['ndx_net_pnl']}\n"
            )
        f.write("\nRaw rows:\n")
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Index Sniper Pro v2.2 decomposed backtest matrix")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--label", default="")
    parser.add_argument("--scenarios", default="", help="comma separated scenario ids. Empty means all.")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    out_dir = ROOT / "backtests"
    data_dir = out_dir / "data"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    matrix_label = args.label or f"matrix_{args.years}y_{timestamp}"

    # Fetch/cache all base symbols once.
    all_candles = {}
    sources = []
    for symbol in ALL_SYMBOLS:
        r = load_or_fetch_symbol(symbol, args.years, data_dir, refresh=args.refresh)
        all_candles[symbol] = r.candles
        sources.append({"symbol": symbol, "provider": r.provider, "provider_symbol": r.provider_symbol, "candles": len(r.candles)})
        print(f"[data] {symbol}: {r.provider}:{r.provider_symbol} candles={len(r.candles)}")

    base_cfg = cfg_from_env(args)
    scenarios = _scenario_definitions()
    if args.scenarios.strip():
        wanted = {x.strip() for x in args.scenarios.split(",") if x.strip()}
        scenarios = [s for s in scenarios if s["id"] in wanted]
        missing = wanted - {s["id"] for s in scenarios}
        if missing:
            raise SystemExit(f"unknown scenario ids: {sorted(missing)}")

    rows = []
    detail_dir = out_dir / "matrix_runs" / matrix_label
    detail_dir.mkdir(parents=True, exist_ok=True)
    for s in scenarios:
        print(f"[scenario] {s['id']}: {s['name']}")
        sub_candles = {sym: all_candles[sym] for sym in s["symbols"] if sym in all_candles}
        cfg = _scenario_cfg(base_cfg, s)
        result = run_portfolio_backtest(sub_candles, cfg)
        result["scenario"] = s
        result["data_sources"] = sources
        label = f"{matrix_label}_{s['id']}"
        paths = write_results(result, detail_dir, label)
        rows.append(_scenario_row(s, result, label, paths))

    csv_path = out_dir / f"backtest_matrix_{matrix_label}.csv"
    txt_path = out_dir / f"backtest_matrix_{matrix_label}.txt"
    json_path = out_dir / f"backtest_matrix_{matrix_label}.json"
    _write_rows_csv(csv_path, rows)
    _write_matrix_txt(txt_path, rows, args.years, base_cfg)
    json_path.write_text(json.dumps({"years": args.years, "config": asdict(base_cfg), "data_sources": sources, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    # latest aliases
    (out_dir / "backtest_matrix_latest.csv").write_bytes(csv_path.read_bytes())
    (out_dir / "backtest_matrix_latest.txt").write_bytes(txt_path.read_bytes())
    (out_dir / "backtest_matrix_latest.json").write_bytes(json_path.read_bytes())

    print("===== BACKTEST MATRIX SUMMARY =====")
    print(txt_path.read_text(encoding="utf-8"))
    print("===== OUTPUT FILES =====")
    print(f"matrix_csv: {csv_path}")
    print(f"matrix_txt: {txt_path}")
    print(f"matrix_json: {json_path}")
    print(f"latest_matrix: {out_dir / 'backtest_matrix_latest.txt'}")
    print(f"detail_dir: {detail_dir}")


if __name__ == "__main__":
    main()
