from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from index_sniper.backtest.data import load_or_fetch_symbol
from index_sniper.backtest.engine import BacktestConfig, run_portfolio_backtest, write_results

ROOT = Path(__file__).resolve().parents[2]


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _symbols(v: str | None) -> list[str]:
    if not v:
        return ["SP500USDT", "NDX100USDT", "BTCUSDT"]
    return [s.strip().upper() for s in v.split(",") if s.strip()]


def _tuple_symbols(v: str | None) -> tuple[str, ...]:
    if not v:
        return ()
    return tuple(s.strip().upper() for s in v.split(",") if s.strip())


def cfg_from_env(args) -> BacktestConfig:
    return BacktestConfig(
        initial_equity=float(os.getenv("BT_INITIAL_EQUITY", os.getenv("BACKTEST_INITIAL_EQUITY", "1374"))),
        capital_ratio=float(os.getenv("BT_CAPITAL_RATIO", os.getenv("CAPITAL_RATIO", "0.30"))),
        leverage=float(os.getenv("BT_LEVERAGE", os.getenv("LEVERAGE", "5"))),
        max_order_notional_usdt=float(os.getenv("BT_MAX_ORDER_NOTIONAL_USDT", os.getenv("MAX_ORDER_NOTIONAL_USDT", "1000"))),
        k_value=float(os.getenv("BT_K_VALUE", os.getenv("K_VALUE", "0.50"))),
        ema_fast=int(os.getenv("BT_EMA_FAST", os.getenv("EMA_FAST", "20"))),
        ema_slow=int(os.getenv("BT_EMA_SLOW", os.getenv("EMA_SLOW", "60"))),
        atr_period=int(os.getenv("BT_ATR_PERIOD", os.getenv("ATR_PERIOD", "14"))),
        atr_stop_mult=float(os.getenv("BT_ATR_STOP_MULT", os.getenv("ATR_STOP_MULT", "1.30"))),
        atr_take_profit_mult=float(os.getenv("BT_ATR_TAKE_PROFIT_MULT", os.getenv("ATR_TAKE_PROFIT_MULT", "2.00"))),
        taker_fee_rate=float(os.getenv("BT_TAKER_FEE_RATE", "0.0006")),
        slippage_bps=float(os.getenv("BT_SLIPPAGE_BPS", "2.0")),
        survival_min_breakout_atr=float(os.getenv("BT_SURVIVAL_MIN_BREAKOUT_ATR", os.getenv("SURVIVAL_MIN_BREAKOUT_ATR", "0.05"))),
        max_entry_extension_atr=float(os.getenv("BT_MAX_ENTRY_EXTENSION_ATR", os.getenv("MAX_ENTRY_EXTENSION_ATR", "0.40"))),
        anti_chase_enabled=_bool(os.getenv("BT_ANTI_CHASE_ENABLED", os.getenv("ANTI_CHASE_ENABLED", "true")), True),
        anti_chase_extreme_up_pct=float(os.getenv("BT_ANTI_CHASE_EXTREME_UP_PCT", os.getenv("ANTI_CHASE_EXTREME_UP_PCT", "7.0"))),
        anti_chase_extreme_down_pct=float(os.getenv("BT_ANTI_CHASE_EXTREME_DOWN_PCT", os.getenv("ANTI_CHASE_EXTREME_DOWN_PCT", "7.0"))),
        anti_chase_extreme_range_atr=float(os.getenv("BT_ANTI_CHASE_EXTREME_RANGE_ATR", os.getenv("ANTI_CHASE_EXTREME_RANGE_ATR", "1.8"))),
        max_open_positions=int(os.getenv("BT_MAX_OPEN_POSITIONS", os.getenv("MAX_OPEN_POSITIONS", "2"))),
        max_new_positions_per_day=int(os.getenv("BT_MAX_NEW_POSITIONS_PER_DAY", os.getenv("MAX_NEW_POSITIONS_PER_CYCLE", "1"))),
        max_index_group_open=int(os.getenv("BT_MAX_INDEX_GROUP_OPEN", os.getenv("SURVIVAL_MAX_CORRELATED_OPEN", "1"))),
        block_index_friday_entries=_bool(os.getenv("BT_BLOCK_INDEX_FRIDAY_ENTRIES"), True),
        weekend_flat_index=_bool(os.getenv("BT_WEEKEND_FLAT_INDEX", os.getenv("INDEX_WEEKEND_FLAT", "true")), True),
        long_only_symbols=_tuple_symbols(os.getenv("BT_LONG_ONLY_SYMBOLS")),
        short_only_symbols=_tuple_symbols(os.getenv("BT_SHORT_ONLY_SYMBOLS")),
        long_disabled_symbols=_tuple_symbols(os.getenv("BT_LONG_DISABLED_SYMBOLS")),
        short_disabled_symbols=_tuple_symbols(os.getenv("BT_SHORT_DISABLED_SYMBOLS")),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Index Sniper Pro v2.1 backtest")
    parser.add_argument("--years", type=int, default=5, help="history years to fetch, normally 3 or 5")
    parser.add_argument("--symbols", default=os.getenv("BT_SYMBOLS", os.getenv("SYMBOLS", "SP500USDT,NDX100USDT,BTCUSDT")))
    parser.add_argument("--refresh", action="store_true", help="refetch public market data instead of cached CSV")
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    symbols = _symbols(args.symbols)
    data_dir = ROOT / "backtests" / "data"
    out_dir = ROOT / "backtests"
    candles = {}
    source_info = []
    for symbol in symbols:
        r = load_or_fetch_symbol(symbol, args.years, data_dir, refresh=args.refresh)
        candles[symbol] = r.candles
        source_info.append({"symbol": symbol, "provider": r.provider, "provider_symbol": r.provider_symbol, "candles": len(r.candles), "path": str(r.path) if r.path else None})
        print(f"[data] {symbol}: {r.provider}:{r.provider_symbol} candles={len(r.candles)}")
    cfg = cfg_from_env(args)
    result = run_portfolio_backtest(candles, cfg)
    result["data_sources"] = source_info
    label = args.label or f"{args.years}y_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    paths = write_results(result, out_dir, label)
    print("===== BACKTEST SUMMARY =====")
    print((out_dir / "backtest_summary_latest.txt").read_text(encoding="utf-8"))
    print("===== OUTPUT FILES =====")
    for k, p in paths.items():
        print(f"{k}: {p}")
    print(f"latest_summary: {out_dir / 'backtest_summary_latest.txt'}")
    print(f"latest_trades: {out_dir / 'trades_latest.csv'}")
    print(f"latest_equity: {out_dir / 'equity_curve_latest.csv'}")


if __name__ == "__main__":
    main()
