from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def _safe_float(x: Any) -> float:
    try:
        if x is None:
            return 0.0
        if isinstance(x, str) and x.lower() == "inf":
            return math.inf
        return float(x)
    except Exception:
        return 0.0


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, rows: list[dict]) -> None:
    keys: list[str] = []
    for row in rows:
        for k in row.keys():
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def _format_line(i: int, r: dict) -> str:
    return (
        f"{i:02d}. {r['side_mode']} lev {r['leverage']}x K {r['k_value']} EMA {r['ema_fast']}/{r['ema_slow']} "
        f"SL {r['atr_stop_mult']} TP {r['atr_take_profit_mult']} EXT {r['max_entry_extension_atr']} "
        f"AC {r['anti_chase_extreme_up_pct']}/{r['anti_chase_extreme_range_atr']} | "
        f"5y ret {r['return_5y_pct']}% MDD {r['mdd_5y_pct']}% Calmar {r['calmar_5y']} | "
        f"3y ret {r['return_3y_pct']}% MDD {r['mdd_3y_pct']}% Calmar {r['calmar_3y']} | "
        f"robust {r['robust_score']} min_ret {r['min_return_pct']}% max_mdd {r['max_mdd_pct']}% PFmin {r['min_profit_factor']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare BTC optimizer 3y and 5y results")
    parser.add_argument("--csv5", default=str(ROOT / "backtests" / "btc_optimizer_5y_latest.csv"))
    parser.add_argument("--csv3", default=str(ROOT / "backtests" / "btc_optimizer_3y_latest.csv"))
    parser.add_argument("--top", type=int, default=50)
    args = parser.parse_args()

    p5 = Path(args.csv5)
    p3 = Path(args.csv3)
    if not p5.exists():
        raise SystemExit(f"missing 5y optimizer csv: {p5}")
    if not p3.exists():
        raise SystemExit(f"missing 3y optimizer csv: {p3}")

    rows5 = _read_csv(p5)
    rows3 = _read_csv(p3)
    by3 = {r["strategy_id"]: r for r in rows3}
    rows = []
    for r5 in rows5:
        r3 = by3.get(r5.get("strategy_id", ""))
        if not r3:
            continue
        ret5 = _safe_float(r5.get("return_pct"))
        ret3 = _safe_float(r3.get("return_pct"))
        mdd5 = _safe_float(r5.get("max_drawdown_pct"))
        mdd3 = _safe_float(r3.get("max_drawdown_pct"))
        cal5 = _safe_float(r5.get("calmar"))
        cal3 = _safe_float(r3.get("calmar"))
        pf5 = _safe_float(r5.get("profit_factor"))
        pf3 = _safe_float(r3.get("profit_factor"))
        # Penalize unstable high-return settings that collapse in the shorter recent window.
        min_ret = min(ret5, ret3)
        max_mdd = max(mdd5, mdd3)
        min_pf = min(pf5, pf3)
        min_calmar = min(cal5, cal3)
        robust_score = min_calmar + max(0.0, min_ret) / 100.0 + max(0.0, min_pf - 1.0)
        row = {
            "strategy_id": r5["strategy_id"],
            "side_mode": r5["side_mode"],
            "leverage": r5["leverage"],
            "k_value": r5["k_value"],
            "ema_fast": r5["ema_fast"],
            "ema_slow": r5["ema_slow"],
            "atr_stop_mult": r5["atr_stop_mult"],
            "atr_take_profit_mult": r5["atr_take_profit_mult"],
            "survival_min_breakout_atr": r5["survival_min_breakout_atr"],
            "max_entry_extension_atr": r5["max_entry_extension_atr"],
            "anti_chase_enabled": r5["anti_chase_enabled"],
            "anti_chase_extreme_up_pct": r5["anti_chase_extreme_up_pct"],
            "anti_chase_extreme_down_pct": r5["anti_chase_extreme_down_pct"],
            "anti_chase_extreme_range_atr": r5["anti_chase_extreme_range_atr"],
            "return_5y_pct": round(ret5, 6),
            "mdd_5y_pct": round(mdd5, 6),
            "cagr_5y_pct": r5.get("cagr_pct"),
            "calmar_5y": r5.get("calmar"),
            "pf_5y": r5.get("profit_factor"),
            "trades_5y": r5.get("trade_count"),
            "return_3y_pct": round(ret3, 6),
            "mdd_3y_pct": round(mdd3, 6),
            "cagr_3y_pct": r3.get("cagr_pct"),
            "calmar_3y": r3.get("calmar"),
            "pf_3y": r3.get("profit_factor"),
            "trades_3y": r3.get("trade_count"),
            "min_return_pct": round(min_ret, 6),
            "max_mdd_pct": round(max_mdd, 6),
            "min_profit_factor": round(min_pf, 6),
            "min_calmar": round(min_calmar, 6),
            "robust_score": round(robust_score, 6),
        }
        rows.append(row)

    ranked = sorted(rows, key=lambda r: (_safe_float(r["robust_score"]), _safe_float(r["min_return_pct"]), -_safe_float(r["max_mdd_pct"])), reverse=True)
    out_dir = ROOT / "backtests"
    csv_path = out_dir / "btc_optimizer_compare_latest.csv"
    txt_path = out_dir / "btc_optimizer_compare_latest.txt"
    json_path = out_dir / "btc_optimizer_compare_latest.json"
    _write_csv(csv_path, ranked)
    json_path.write_text(json.dumps({"rows": ranked}, ensure_ascii=False, indent=2), encoding="utf-8")
    with txt_path.open("w", encoding="utf-8") as f:
        f.write("Index Sniper Pro BTC Optimizer 3y/5y Comparison\n")
        f.write("=================================================\n")
        f.write(f"source_5y: {p5}\n")
        f.write(f"source_3y: {p3}\n")
        f.write(f"matched_strategies: {len(ranked)}\n")
        f.write("\nRanked by robust score = min(Calmar 3y/5y) + positive min return bonus + PF bonus:\n")
        for i, r in enumerate(ranked[: args.top], start=1):
            f.write(_format_line(i, r) + "\n")

    print("===== BTC OPTIMIZER 3Y/5Y COMPARISON =====")
    print(txt_path.read_text(encoding="utf-8"))
    print("===== OUTPUT FILES =====")
    print(f"compare_csv: {csv_path}")
    print(f"compare_txt: {txt_path}")
    print(f"compare_json: {json_path}")


if __name__ == "__main__":
    main()
