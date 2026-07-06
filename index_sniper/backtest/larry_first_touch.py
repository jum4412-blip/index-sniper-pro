from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal

import requests

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore

from index_sniper.strategy.indicators import Candle

ROOT = Path.cwd()
if load_dotenv is not None:
    try:
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

Side = Literal["long", "short"]
SameCandleMode = Literal["skip", "open_distance", "candle"]

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1H": 60 * 60_000,
    "4H": 4 * 60 * 60_000,
    "6H": 6 * 60 * 60_000,
    "12H": 12 * 60 * 60_000,
}


@dataclass
class FirstTouchConfig:
    initial_equity: float = 1374.0
    capital_ratio: float = 0.30
    leverage: float = 5.0
    max_order_notional_usdt: float = 999999.0
    k_value: float = 0.50
    taker_fee_rate: float = 0.0006
    slippage_bps: float = 2.0
    same_candle_mode: SameCandleMode = "skip"
    min_bars_per_day: int = 20


@dataclass
class SessionDay:
    date: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    bars: list[Candle]


@dataclass
class FirstTouchSignal:
    date: str
    symbol: str
    status: str
    side: str
    day_open: float
    day_high: float
    day_low: float
    day_close: float
    previous_high: float
    previous_low: float
    previous_range: float
    long_target: float
    short_target: float
    first_touch_ts: int | None
    first_touch_utc: str
    first_touch_bar_open: float | None
    long_hit: bool
    short_hit: bool
    both_same_bar: bool
    reason: str


@dataclass
class FirstTouchTrade:
    symbol: str
    side: str
    entry_date: str
    entry_time_utc: str
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
    same_candle_mode: str


@dataclass
class FirstTouchCurvePoint:
    date: str
    equity: float
    drawdown_pct: float
    trade_count: int


def _split_csv(text: str) -> list[str]:
    return [x.strip() for x in str(text or "").split(",") if x.strip()]


def _parse_ints(text: str) -> list[int]:
    return [int(x) for x in _split_csv(text)]


def _parse_floats(text: str) -> list[float]:
    return [float(x) for x in _split_csv(text)]


def _utc_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _iso_ms(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()


def _date_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date().isoformat()


def _slip(price: float, bps: float, adverse: int) -> float:
    return price * (1.0 + adverse * (bps / 10000.0))


def _entry_price(side: Side, target: float, cfg: FirstTouchConfig) -> float:
    if side == "long":
        return _slip(target, cfg.slippage_bps, +1)
    return _slip(target, cfg.slippage_bps, -1)


def _exit_price(side: Side, raw_price: float, cfg: FirstTouchConfig) -> float:
    if side == "long":
        return _slip(raw_price, cfg.slippage_bps, -1)
    return _slip(raw_price, cfg.slippage_bps, +1)


def _size(equity: float, entry_price: float, symbol_count: int, cfg: FirstTouchConfig) -> tuple[float, float]:
    capital = equity * cfg.capital_ratio / max(symbol_count, 1)
    notional = min(capital * cfg.leverage, cfg.max_order_notional_usdt)
    if entry_price <= 0 or notional <= 0:
        return 0.0, 0.0
    return notional / entry_price, notional


def _parse_bitget_rows(rows: list) -> list[Candle]:
    out: list[Candle] = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            out.append(
                Candle(
                    ts=int(float(row[0])),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]) if len(row) > 5 and row[5] not in (None, "") else 0.0,
                    turnover=float(row[6]) if len(row) > 6 and row[6] not in (None, "") else 0.0,
                )
            )
        except Exception:
            continue
    out.sort(key=lambda c: c.ts)
    return out


def candles_to_csv(path: Path, candles: Iterable[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "datetime_utc", "open", "high", "low", "close", "volume", "turnover"])
        for c in candles:
            w.writerow([c.ts, _iso_ms(c.ts), c.open, c.high, c.low, c.close, c.volume, c.turnover])


def candles_from_csv(path: Path) -> list[Candle]:
    candles: list[Candle] = []
    with path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            candles.append(
                Candle(
                    ts=int(float(row["ts"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0.0),
                    turnover=float(row.get("turnover") or 0.0),
                )
            )
    candles.sort(key=lambda c: c.ts)
    return candles


def fetch_bitget_history_candles(
    *,
    symbol: str,
    interval: str,
    years: int,
    data_dir: Path,
    refresh: bool = False,
    category: str = "USDT-FUTURES",
    candle_type: str = "market",
    timeout: int = 20,
) -> list[Candle]:
    interval = interval.strip()
    if interval not in INTERVAL_MS:
        raise RuntimeError(f"unsupported interval for first-touch backtest: {interval}")
    safe_interval = interval.replace("/", "_")
    path = data_dir / f"{symbol.upper()}_{safe_interval}_{years}y_bitget.csv"
    meta_path = data_dir / f"{symbol.upper()}_{safe_interval}_{years}y_bitget.meta.json"
    if path.exists() and not refresh:
        return candles_from_csv(path)

    # Add a warmup buffer so the first session has a previous day range.
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=int(years * 366 + 10))
    step_ms = INTERVAL_MS[interval]
    start_ms = _utc_ms(start_dt)
    end_ms = _utc_ms(end_dt)
    page_span_ms = step_ms * 100 - 1  # Bitget UTA history endpoint returns max 100 rows.

    url = "https://api.bitget.com/api/v3/market/history-candles"
    headers = {"User-Agent": "IndexSniperProFirstTouch/3.1", "locale": "en-US"}
    all_rows: dict[int, Candle] = {}
    cursor = start_ms - (start_ms % step_ms)
    calls = 0
    last_error: str | None = None

    while cursor < end_ms:
        page_end = min(cursor + page_span_ms, end_ms)
        params = {
            "category": category,
            "symbol": symbol.upper(),
            "interval": interval,
            "type": candle_type,
            "startTime": str(cursor),
            "endTime": str(page_end),
            "limit": "100",
        }
        data = None
        for attempt in range(4):
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=timeout)
                payload = resp.json()
                if resp.status_code >= 400 or str(payload.get("code")) not in {"00000", "0"}:
                    raise RuntimeError(f"HTTP {resp.status_code}: {payload}")
                data = payload.get("data") or []
                break
            except Exception as exc:  # pragma: no cover - network dependent
                last_error = str(exc)
                time.sleep(0.4 * (attempt + 1))
        if data is None:
            raise RuntimeError(f"Bitget history candle fetch failed near {cursor}: {last_error}")
        for c in _parse_bitget_rows(data):
            if start_ms <= c.ts <= end_ms:
                all_rows[c.ts] = c
        calls += 1
        # Public rate limit is high, but a small pause makes long downloads gentler.
        if calls % 20 == 0:
            time.sleep(0.15)
        cursor = page_end + 1

    candles = sorted(all_rows.values(), key=lambda c: c.ts)
    if len(candles) < max(200, years * 200):
        raise RuntimeError(f"not enough Bitget {interval} candles for {symbol}: {len(candles)}")
    candles_to_csv(path, candles)
    meta_path.write_text(
        json.dumps(
            {
                "provider": "BITGET_UTA_HISTORY",
                "symbol": symbol.upper(),
                "interval": interval,
                "years": years,
                "rows": len(candles),
                "category": category,
                "type": candle_type,
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return candles


def _aggregate_utc_days(candles: list[Candle], min_bars_per_day: int) -> list[SessionDay]:
    groups: dict[str, list[Candle]] = {}
    for c in sorted(candles, key=lambda x: x.ts):
        groups.setdefault(_date_from_ts(c.ts), []).append(c)
    days: list[SessionDay] = []
    for d in sorted(groups):
        bars = sorted(groups[d], key=lambda x: x.ts)
        if len(bars) < min_bars_per_day:
            continue
        o = bars[0].open
        h = max(b.high for b in bars)
        l = min(b.low for b in bars)
        cl = bars[-1].close
        vol = sum(b.volume for b in bars)
        days.append(SessionDay(date=d, ts=bars[0].ts, open=o, high=h, low=l, close=cl, volume=vol, bars=bars))
    return days


def _first_touch(day: SessionDay, long_target: float, short_target: float, mode: SameCandleMode) -> tuple[Side | None, Candle | None, str, bool, bool, bool]:
    long_hit_day = False
    short_hit_day = False
    for bar in day.bars:
        long_hit = bar.high >= long_target
        short_hit = bar.low <= short_target
        long_hit_day = long_hit_day or long_hit
        short_hit_day = short_hit_day or short_hit
        if long_hit and short_hit:
            if mode == "skip":
                return None, bar, "both_targets_same_intraday_bar_skip", True, True, True
            if mode == "candle":
                if bar.close >= bar.open:
                    return "long", bar, "both_targets_same_intraday_bar_choose_green", True, True, True
                return "short", bar, "both_targets_same_intraday_bar_choose_red", True, True, True
            # open_distance: choose the target closer to the intraday bar open.
            if abs(bar.open - long_target) <= abs(bar.open - short_target):
                return "long", bar, "both_targets_same_intraday_bar_choose_open_distance_long", True, True, True
            return "short", bar, "both_targets_same_intraday_bar_choose_open_distance_short", True, True, True
        if long_hit:
            return "long", bar, "long_target_first_touch", True, short_hit_day, False
        if short_hit:
            return "short", bar, "short_target_first_touch", long_hit_day, True, False
    return None, None, "no_breakout", long_hit_day, short_hit_day, False


def _calc_trade(
    *,
    symbol: str,
    side: Side,
    entry_bar: Candle,
    entry_raw: float,
    exit_day: SessionDay,
    equity_before: float,
    symbol_count: int,
    cfg: FirstTouchConfig,
) -> FirstTouchTrade | None:
    entry = _entry_price(side, entry_raw, cfg)
    qty, notional = _size(equity_before, entry, symbol_count, cfg)
    if qty <= 0 or notional <= 0:
        return None
    exit_px = _exit_price(side, exit_day.open, cfg)
    gross = (exit_px - entry) * qty if side == "long" else (entry - exit_px) * qty
    fees = (abs(entry * qty) + abs(exit_px * qty)) * cfg.taker_fee_rate
    net = gross - fees
    return FirstTouchTrade(
        symbol=symbol,
        side=side,
        entry_date=_date_from_ts(entry_bar.ts),
        entry_time_utc=_iso_ms(entry_bar.ts),
        exit_date=exit_day.date,
        entry_price=round(entry, 8),
        exit_price=round(exit_px, 8),
        qty=round(qty, 10),
        notional=round(notional, 8),
        pnl=round(gross, 8),
        fees=round(fees, 8),
        net_pnl=round(net, 8),
        return_on_notional_pct=round((net / notional) * 100.0 if notional else 0.0, 8),
        return_on_equity_pct=round((net / equity_before) * 100.0 if equity_before else 0.0),
        exit_reason="next_utc00_open_time_exit",
        k_value=cfg.k_value,
        same_candle_mode=cfg.same_candle_mode,
    )


def _write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    first = rows[0]
    if hasattr(first, "__dataclass_fields__"):
        dict_rows = [asdict(x) for x in rows]
    else:
        dict_rows = rows
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(dict_rows[0].keys()))
        w.writeheader()
        w.writerows(dict_rows)


def _profit_factor(trades: list[FirstTouchTrade]) -> float:
    gross_profit = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gross_loss = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _max_loss_streak(trades: list[FirstTouchTrade]) -> int:
    best = 0
    cur = 0
    for t in trades:
        if t.net_pnl < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _monthly_returns(curve: list[FirstTouchCurvePoint], initial_equity: float) -> dict[str, float]:
    if not curve:
        return {}
    month_start: dict[str, float] = {}
    month_end: dict[str, float] = {}
    prev_equity = initial_equity
    prev_month = curve[0].date[:7]
    month_start[prev_month] = initial_equity
    for p in curve:
        m = p.date[:7]
        if m not in month_start:
            month_start[m] = prev_equity
        month_end[m] = p.equity
        prev_equity = p.equity
    return {m: ((month_end[m] / month_start[m]) - 1.0) * 100.0 for m in month_end if month_start.get(m, 0) > 0}


def run_first_touch_backtest(symbol_bars: dict[str, list[Candle]], cfg: FirstTouchConfig) -> dict:
    symbol_days = {s: _aggregate_utc_days(bars, cfg.min_bars_per_day) for s, bars in symbol_bars.items()}
    # Date-indexed loop. For multiple symbols, one trade per day: strongest first-touch follow-through wins.
    by_date: dict[str, dict[str, tuple[int, SessionDay]]] = {}
    for symbol, days in symbol_days.items():
        for i, d in enumerate(days):
            by_date.setdefault(d.date, {})[symbol] = (i, d)

    dates = sorted(by_date)
    equity = cfg.initial_equity
    peak = cfg.initial_equity
    max_dd = 0.0
    trades: list[FirstTouchTrade] = []
    signals: list[FirstTouchSignal] = []
    curve: list[FirstTouchCurvePoint] = []
    symbol_count = max(len(symbol_bars), 1)

    for d in dates:
        candidates: list[tuple[float, str, Side, SessionDay, SessionDay, Candle, float, float, float, str, bool, bool, bool]] = []
        for symbol, days in symbol_days.items():
            item = by_date[d].get(symbol)
            if item is None:
                continue
            idx, day = item
            if idx < 1 or idx + 1 >= len(days):
                continue
            prev = days[idx - 1]
            next_day = days[idx + 1]
            prev_range = max(prev.high - prev.low, 0.0)
            long_target = day.open + prev_range * cfg.k_value
            short_target = day.open - prev_range * cfg.k_value
            side, touch_bar, reason, long_hit, short_hit, both_same = _first_touch(day, long_target, short_target, cfg.same_candle_mode)
            signals.append(
                FirstTouchSignal(
                    date=d,
                    symbol=symbol,
                    status="ENTRY" if side else "HOLD",
                    side=(side or "").upper(),
                    day_open=day.open,
                    day_high=day.high,
                    day_low=day.low,
                    day_close=day.close,
                    previous_high=prev.high,
                    previous_low=prev.low,
                    previous_range=prev_range,
                    long_target=long_target,
                    short_target=short_target,
                    first_touch_ts=touch_bar.ts if touch_bar else None,
                    first_touch_utc=_iso_ms(touch_bar.ts) if touch_bar else "",
                    first_touch_bar_open=touch_bar.open if touch_bar else None,
                    long_hit=long_hit,
                    short_hit=short_hit,
                    both_same_bar=both_same,
                    reason=reason,
                )
            )
            if side is None or touch_bar is None:
                continue
            # If multiple symbols are tested, choose the candidate with larger same-day follow-through after touch.
            if side == "long":
                strength = max(0.0, day.high - long_target) / max(prev_range, 1e-9)
                entry_raw = long_target
            else:
                strength = max(0.0, short_target - day.low) / max(prev_range, 1e-9)
                entry_raw = short_target
            candidates.append((strength, symbol, side, day, next_day, touch_bar, entry_raw, long_target, short_target, reason, long_hit, short_hit, both_same))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            _strength, symbol, side, day, next_day, touch_bar, entry_raw, _lt, _st, _reason, _lh, _sh, _bs = candidates[0]
            tr = _calc_trade(
                symbol=symbol,
                side=side,
                entry_bar=touch_bar,
                entry_raw=entry_raw,
                exit_day=next_day,
                equity_before=equity,
                symbol_count=symbol_count,
                cfg=cfg,
            )
            if tr is not None:
                trades.append(tr)
                equity += tr.net_pnl
                if equity <= 0:
                    equity = 0.0
        peak = max(peak, equity)
        dd = ((peak - equity) / peak) * 100.0 if peak else 0.0
        max_dd = max(max_dd, dd)
        curve.append(FirstTouchCurvePoint(date=d, equity=round(equity, 8), drawdown_pct=round(dd, 8), trade_count=len(trades)))
        if equity <= 0:
            # Account effectively dead. Keep curve flat for summary clarity and stop compounding.
            break

    wins = sum(1 for t in trades if t.net_pnl > 0)
    months = _monthly_returns(curve, cfg.initial_equity)
    return {
        "start_equity": cfg.initial_equity,
        "end_equity": round(equity, 8),
        "return_pct": round(((equity / cfg.initial_equity) - 1.0) * 100.0 if cfg.initial_equity else 0.0, 8),
        "max_drawdown_pct": round(max_dd, 8),
        "trade_count": len(trades),
        "win_rate_pct": round((wins / len(trades)) * 100.0 if trades else 0.0, 8),
        "profit_factor": round(_profit_factor(trades), 8) if math.isfinite(_profit_factor(trades)) else float("inf"),
        "avg_net_pnl": round(sum(t.net_pnl for t in trades) / len(trades), 8) if trades else 0.0,
        "max_loss_streak": _max_loss_streak(trades),
        "trades": trades,
        "signals": signals,
        "curve": curve,
        "monthly_returns_pct": months,
    }


def _load_intraday(symbols: list[str], years: int, interval: str, refresh: bool) -> dict[str, list[Candle]]:
    data_dir = ROOT / "backtests" / "v31_larry_first_touch" / "data"
    out: dict[str, list[Candle]] = {}
    for s in symbols:
        print(f"[data] {s} {interval} {years}y: loading Bitget history candles...")
        candles = fetch_bitget_history_candles(symbol=s, interval=interval, years=years, data_dir=data_dir, refresh=refresh)
        print(f"[data] {s}: bars={len(candles)} from {_iso_ms(candles[0].ts)} to {_iso_ms(candles[-1].ts)}")
        out[s] = candles
    return out


def _cell(result: dict) -> str:
    return f"{result['end_equity']:,.0f} / {result['max_drawdown_pct']:.1f}% / PF {result['profit_factor']:.2f}"


def _print_summary(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    print(text)
    print("saved:", path)


def cmd_run(args: argparse.Namespace) -> None:
    symbols = _split_csv(args.symbols)
    bars = _load_intraday(symbols, args.years, args.interval, args.refresh)
    cfg = FirstTouchConfig(
        initial_equity=args.initial_equity,
        capital_ratio=args.capital_ratio,
        leverage=args.leverage,
        max_order_notional_usdt=args.max_notional,
        k_value=args.k,
        taker_fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        same_candle_mode=args.same_candle_mode,
        min_bars_per_day=args.min_bars_per_day,
    )
    result = run_first_touch_backtest(bars, cfg)
    out_dir = ROOT / "backtests" / "v31_larry_first_touch"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "larry_first_touch_trades_latest.csv", result["trades"])
    _write_csv(out_dir / "larry_first_touch_signals_latest.csv", result["signals"])
    _write_csv(out_dir / "larry_first_touch_equity_latest.csv", result["curve"])
    month_rows = [{"month": m, "return_pct": round(v, 8)} for m, v in sorted(result["monthly_returns_pct"].items())]
    _write_csv(out_dir / "larry_first_touch_months_latest.csv", month_rows)
    worst = sorted(result["monthly_returns_pct"].items(), key=lambda x: x[1])[:10]
    best = sorted(result["monthly_returns_pct"].items(), key=lambda x: x[1], reverse=True)[:10]
    lines = [
        "Index Sniper Pro v3.1 Larry Pure First-Touch Backtest",
        "======================================================",
        f"start_equity: {result['start_equity']}",
        f"end_equity: {result['end_equity']}",
        f"return_pct: {result['return_pct']}",
        f"max_drawdown_pct: {result['max_drawdown_pct']}",
        f"trade_count: {result['trade_count']}",
        f"win_rate_pct: {result['win_rate_pct']}",
        f"profit_factor: {result['profit_factor']}",
        f"avg_net_pnl: {result['avg_net_pnl']}",
        f"max_loss_streak: {result['max_loss_streak']}",
        "",
        "Rules:",
        "- entry: first intraday touch of today_open +/- previous_day_range * K",
        "- indicators: none",
        "- take_profit: none; time exit at next UTC 00:00 open / KST 09:00",
        "- stop_loss: none; next_open daily reset",
        "- ambiguous same intraday candle: controlled by same_candle_mode",
        "",
        "Config:",
        f"- symbols: {','.join(symbols)}",
        f"- interval: {args.interval}",
        f"- years: {args.years}",
        f"- initial_equity: {cfg.initial_equity}",
        f"- capital_ratio: {cfg.capital_ratio}",
        f"- leverage: {cfg.leverage}",
        f"- max_order_notional_usdt: {cfg.max_order_notional_usdt}",
        f"- k_value: {cfg.k_value}",
        f"- taker_fee_rate: {cfg.taker_fee_rate}",
        f"- slippage_bps: {cfg.slippage_bps}",
        f"- same_candle_mode: {cfg.same_candle_mode}",
        f"- min_bars_per_day: {cfg.min_bars_per_day}",
        "",
        "Worst months:",
    ]
    lines.extend([f"- {m}: {round(v, 6)}%" for m, v in worst])
    lines.append("")
    lines.append("Best months:")
    lines.extend([f"- {m}: {round(v, 6)}%" for m, v in best])
    lines.append("")
    summary_path = out_dir / "larry_first_touch_summary_latest.txt"
    _print_summary(summary_path, "\n".join(lines))
    specific = out_dir / f"larry_first_touch_summary_{'+'.join(symbols)}_{args.years}y_k{args.k:g}_{args.leverage:g}x_{args.same_candle_mode}_{args.interval}.txt"
    specific.write_text("\n".join(lines), encoding="utf-8")
    print("saved:", specific)


def cmd_sweep(args: argparse.Namespace) -> None:
    symbols = _split_csv(args.symbols)
    years_list = _parse_ints(args.years)
    leverages = _parse_floats(args.leverages)
    out_dir = ROOT / "backtests" / "v31_larry_first_touch"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for years in years_list:
        bars = _load_intraday(symbols, years, args.interval, args.refresh)
        for lev in leverages:
            cfg = FirstTouchConfig(
                initial_equity=args.initial_equity,
                capital_ratio=args.capital_ratio,
                leverage=lev,
                max_order_notional_usdt=args.max_notional,
                k_value=args.k,
                taker_fee_rate=args.fee_rate,
                slippage_bps=args.slippage_bps,
                same_candle_mode=args.same_candle_mode,
                min_bars_per_day=args.min_bars_per_day,
            )
            result = run_first_touch_backtest({s: list(c) for s, c in bars.items()}, cfg)
            rows.append(
                {
                    "symbols": "+".join(symbols),
                    "years": years,
                    "k": args.k,
                    "interval": args.interval,
                    "leverage": f"{lev:g}x",
                    "same_candle_mode": args.same_candle_mode,
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
    csv_path = out_dir / "larry_first_touch_sweep_latest.csv"
    _write_csv(csv_path, rows)
    txt_path = out_dir / "larry_first_touch_sweep_latest.txt"
    lev_cols = [f"{x:g}x" for x in leverages]
    header = ["years"] + lev_cols
    widths = {h: max(len(h), 12) for h in header}
    table: list[dict[str, str]] = []
    for y in years_list:
        line = {"years": str(y)}
        for col in lev_cols:
            hit = next((r for r in rows if r["years"] == y and r["leverage"] == col), None)
            line[col] = str(hit["cell"]) if hit else "-"
        table.append(line)
        for h in header:
            widths[h] = max(widths[h], len(line[h]))
    with txt_path.open("w", encoding="utf-8") as f:
        f.write("Index Sniper Pro v3.1 Larry Pure First-Touch Sweep\n")
        f.write("====================================================\n")
        f.write("Format: final equity / MDD / PF\n")
        f.write(f"symbols={','.join(symbols)} k={args.k:g} interval={args.interval} capital_ratio={args.capital_ratio} same_candle_mode={args.same_candle_mode}\n")
        f.write("Rules: first intraday target touch, no TP/SL, next UTC 00:00/KST 09:00 exit\n\n")
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
    out_dir = ROOT / "backtests" / "v31_larry_first_touch"
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for years in years_list:
        bars = _load_intraday(symbols, years, args.interval, args.refresh)
        for k in k_values:
            cfg = FirstTouchConfig(
                initial_equity=args.initial_equity,
                capital_ratio=args.capital_ratio,
                leverage=args.leverage,
                max_order_notional_usdt=args.max_notional,
                k_value=k,
                taker_fee_rate=args.fee_rate,
                slippage_bps=args.slippage_bps,
                same_candle_mode=args.same_candle_mode,
                min_bars_per_day=args.min_bars_per_day,
            )
            result = run_first_touch_backtest({s: list(c) for s, c in bars.items()}, cfg)
            rows.append(
                {
                    "symbols": "+".join(symbols),
                    "years": years,
                    "k": f"{k:g}",
                    "interval": args.interval,
                    "leverage": f"{args.leverage:g}x",
                    "same_candle_mode": args.same_candle_mode,
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
    csv_path = out_dir / "larry_first_touch_k_sweep_latest.csv"
    _write_csv(csv_path, rows)
    txt_path = out_dir / "larry_first_touch_k_sweep_latest.txt"
    k_cols = [f"{x:g}" for x in k_values]
    header = ["years"] + k_cols
    widths = {h: max(len(h), 12) for h in header}
    table: list[dict[str, str]] = []
    for y in years_list:
        line = {"years": str(y)}
        for col in k_cols:
            hit = next((r for r in rows if r["years"] == y and r["k"] == col), None)
            line[col] = str(hit["cell"]) if hit else "-"
        table.append(line)
        for h in header:
            widths[h] = max(widths[h], len(line[h]))
    with txt_path.open("w", encoding="utf-8") as f:
        f.write("Index Sniper Pro v3.1 Larry Pure First-Touch K Sweep\n")
        f.write("======================================================\n")
        f.write("Format: final equity / MDD / PF\n")
        f.write(f"symbols={','.join(symbols)} leverage={args.leverage:g}x interval={args.interval} capital_ratio={args.capital_ratio} same_candle_mode={args.same_candle_mode}\n\n")
        f.write("  ".join(h.ljust(widths[h]) for h in header) + "\n")
        f.write("  ".join("-" * widths[h] for h in header) + "\n")
        for line in table:
            f.write("  ".join(line[h].ljust(widths[h]) for h in header) + "\n")
        f.write("\nCSV: " + str(csv_path) + "\n")
    print(txt_path.read_text(encoding="utf-8"))
    print("saved:", txt_path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="v3.1 Larry Pure First-Touch intraday backtester")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--symbols", default=os.getenv("BT_FT_SYMBOLS", "BTCUSDT"))
        sp.add_argument("--interval", choices=sorted(INTERVAL_MS), default=os.getenv("BT_FT_INTERVAL", "1H"))
        sp.add_argument("--initial-equity", type=float, default=float(os.getenv("BT_FT_INITIAL_EQUITY", os.getenv("BT_INITIAL_EQUITY", "1374"))))
        sp.add_argument("--capital-ratio", type=float, default=float(os.getenv("BT_FT_CAPITAL_RATIO", os.getenv("BT_CAPITAL_RATIO", "0.30"))))
        sp.add_argument("--max-notional", type=float, default=float(os.getenv("BT_FT_MAX_NOTIONAL", os.getenv("BT_OPT_MAX_ORDER_NOTIONAL_USDT", "999999"))))
        sp.add_argument("--fee-rate", type=float, default=float(os.getenv("BT_FT_FEE_RATE", "0.0006")))
        sp.add_argument("--slippage-bps", type=float, default=float(os.getenv("BT_FT_SLIPPAGE_BPS", "2.0")))
        sp.add_argument("--same-candle-mode", choices=["skip", "open_distance", "candle"], default=os.getenv("BT_FT_SAME_CANDLE_MODE", "skip"))
        sp.add_argument("--min-bars-per-day", type=int, default=int(os.getenv("BT_FT_MIN_BARS_PER_DAY", "20")))
        sp.add_argument("--refresh", action="store_true")

    run = sub.add_parser("run")
    add_common(run)
    run.add_argument("--years", type=int, default=int(os.getenv("BT_FT_YEARS_ONE", "5")))
    run.add_argument("--leverage", type=float, default=float(os.getenv("BT_FT_LEVERAGE", os.getenv("LEVERAGE", "5"))))
    run.add_argument("--k", type=float, default=float(os.getenv("BT_FT_K", os.getenv("K_VALUE", "0.50"))))
    run.set_defaults(func=cmd_run)

    sweep = sub.add_parser("sweep")
    add_common(sweep)
    sweep.add_argument("--years", default=os.getenv("BT_FT_YEARS", "1,2,3,4,5"))
    sweep.add_argument("--leverages", default=os.getenv("BT_FT_LEVERAGES", "1,2,3,4,5,6,7,8,9,10"))
    sweep.add_argument("--k", type=float, default=float(os.getenv("BT_FT_K", os.getenv("K_VALUE", "0.50"))))
    sweep.set_defaults(func=cmd_sweep)

    ksweep = sub.add_parser("ksweep")
    add_common(ksweep)
    ksweep.add_argument("--years", default=os.getenv("BT_FT_YEARS", "1,2,3,4,5"))
    ksweep.add_argument("--leverage", type=float, default=float(os.getenv("BT_FT_LEVERAGE", os.getenv("LEVERAGE", "5"))))
    ksweep.add_argument("--k-values", default=os.getenv("BT_FT_K_VALUES", "0.25,0.35,0.50,0.65,0.80,1.00"))
    ksweep.set_defaults(func=cmd_ksweep)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
