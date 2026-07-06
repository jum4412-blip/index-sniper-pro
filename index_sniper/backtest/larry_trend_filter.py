from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
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
ExitSameCandleMode = Literal["stop_first", "target_first", "open_distance"]

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
class TrendAtrConfig:
    initial_equity: float = 1374.0
    capital_ratio: float = 0.30
    leverage: float = 5.0
    max_order_notional_usdt: float = 999999.0
    k_value: float = 0.50
    taker_fee_rate: float = 0.0006
    slippage_bps: float = 2.0
    same_candle_mode: SameCandleMode = "skip"
    exit_same_candle_mode: ExitSameCandleMode = "stop_first"
    min_bars_per_day: int = 20
    trend_profile: str = "4H_20_60"
    atr_period: int = 14
    atr_stop_mult: float = 1.30
    atr_take_profit_mult: float = 2.00


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
class TrendComponent:
    interval: str
    fast: int
    slow: int


@dataclass
class TrendSnapshot:
    profile: str
    direction: str
    detail: str


@dataclass
class TrendAtrSignal:
    date: str
    symbol: str
    status: str
    side: str
    reason: str
    day_open: float
    day_high: float
    day_low: float
    day_close: float
    previous_high: float
    previous_low: float
    previous_range: float
    long_target: float
    short_target: float
    atr: float | None
    trend_profile: str
    trend_direction: str
    trend_detail: str
    first_touch_ts: int | None
    first_touch_utc: str
    first_touch_bar_open: float | None
    long_hit: bool
    short_hit: bool
    both_same_bar: bool


@dataclass
class TrendAtrTrade:
    symbol: str
    side: str
    entry_date: str
    entry_time_utc: str
    exit_date: str
    exit_time_utc: str
    entry_price: float
    exit_price: float
    stop_price: float
    take_profit_price: float
    qty: float
    notional: float
    pnl: float
    fees: float
    net_pnl: float
    return_on_notional_pct: float
    return_on_equity_pct: float
    exit_reason: str
    k_value: float
    trend_profile: str
    same_candle_mode: str
    atr_period: int
    atr_stop_mult: float
    atr_take_profit_mult: float


@dataclass
class TrendAtrCurvePoint:
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


def _entry_price(side: Side, target: float, cfg: TrendAtrConfig) -> float:
    if side == "long":
        return _slip(target, cfg.slippage_bps, +1)
    return _slip(target, cfg.slippage_bps, -1)


def _exit_price(side: Side, raw_price: float, cfg: TrendAtrConfig) -> float:
    if side == "long":
        return _slip(raw_price, cfg.slippage_bps, -1)
    return _slip(raw_price, cfg.slippage_bps, +1)


def _size(equity: float, entry_price: float, symbol_count: int, cfg: TrendAtrConfig) -> tuple[float, float]:
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


def _data_dir() -> Path:
    # Reuse v3.1/v3.2 1H data cache by default.
    raw = os.getenv("BT_V33_DATA_DIR", os.getenv("BT_V32_DATA_DIR", "backtests/v31_larry_first_touch/data"))
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


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
        raise RuntimeError(f"unsupported interval for v3.3 backtest: {interval}")
    safe_interval = interval.replace("/", "_")
    path = data_dir / f"{symbol.upper()}_{safe_interval}_{years}y_bitget.csv"
    meta_path = data_dir / f"{symbol.upper()}_{safe_interval}_{years}y_bitget.meta.json"
    if path.exists() and not refresh:
        return candles_from_csv(path)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=int(years * 366 + 10))
    step_ms = INTERVAL_MS[interval]
    start_ms = _utc_ms(start_dt)
    end_ms = _utc_ms(end_dt)
    page_span_ms = step_ms * 100 - 1

    url = "https://api.bitget.com/api/v3/market/history-candles"
    headers = {"User-Agent": "IndexSniperProV33TrendFilter/3.3", "locale": "en-US"}
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
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.4 * (attempt + 1))
        if data is None:
            raise RuntimeError(f"Bitget history candle fetch failed near {cursor}: {last_error}")
        for c in _parse_bitget_rows(data):
            if start_ms <= c.ts <= end_ms:
                all_rows[c.ts] = c
        calls += 1
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


def _resample(candles: list[Candle], interval: str) -> list[Candle]:
    if interval == "1H":
        return list(candles)
    if interval not in INTERVAL_MS:
        raise RuntimeError(f"unsupported trend interval: {interval}")
    step = INTERVAL_MS[interval]
    groups: dict[int, list[Candle]] = {}
    for c in sorted(candles, key=lambda x: x.ts):
        bucket = (c.ts // step) * step
        groups.setdefault(bucket, []).append(c)
    out: list[Candle] = []
    for ts in sorted(groups):
        bars = sorted(groups[ts], key=lambda x: x.ts)
        out.append(
            Candle(
                ts=ts,
                open=bars[0].open,
                high=max(b.high for b in bars),
                low=min(b.low for b in bars),
                close=bars[-1].close,
                volume=sum(b.volume for b in bars),
                turnover=sum(getattr(b, "turnover", 0.0) for b in bars),
            )
        )
    return out


def _ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    cur = sum(values[:period]) / period
    for v in values[period:]:
        cur = (v * alpha) + (cur * (1.0 - alpha))
    return cur


def _parse_trend_profile(profile: str) -> list[TrendComponent]:
    p = (profile or "none").strip()
    if p.lower() in {"none", "off", "no", "no_filter", "nofilter"}:
        return []
    comps: list[TrendComponent] = []
    for raw in re.split(r"[+|]", p):
        s = raw.strip()
        m = re.fullmatch(r"(\d+[mH])_(\d+)_(\d+)", s)
        if not m:
            raise RuntimeError(f"invalid trend profile component: {s}. Example: 4H_20_60 or 1H_20_60+4H_20_60")
        interval = m.group(1)
        fast = int(m.group(2))
        slow = int(m.group(3))
        if interval not in INTERVAL_MS:
            raise RuntimeError(f"unsupported trend interval in profile {s}: {interval}")
        if fast >= slow:
            raise RuntimeError(f"trend profile fast period must be smaller than slow period: {s}")
        comps.append(TrendComponent(interval=interval, fast=fast, slow=slow))
    return comps


def _trend_snapshot(profile: str, trend_bars: dict[str, list[Candle]], day_ts: int) -> TrendSnapshot:
    comps = _parse_trend_profile(profile)
    if not comps:
        return TrendSnapshot(profile=profile, direction="both", detail="no_filter")
    dirs: list[str] = []
    details: list[str] = []
    for comp in comps:
        step = INTERVAL_MS[comp.interval]
        closes = [b.close for b in trend_bars[comp.interval] if b.ts + step <= day_ts]
        fast = _ema(closes, comp.fast)
        slow = _ema(closes, comp.slow)
        if fast is None or slow is None:
            dirs.append("neutral")
            details.append(f"{comp.interval}_{comp.fast}_{comp.slow}:not_enough_bars")
            continue
        if fast > slow:
            dirs.append("long")
            d = "LONG"
        elif fast < slow:
            dirs.append("short")
            d = "SHORT"
        else:
            dirs.append("neutral")
            d = "NEUTRAL"
        gap_pct = ((fast / slow) - 1.0) * 100.0 if slow else 0.0
        details.append(f"{comp.interval}_{comp.fast}_{comp.slow}:{d}:fast={fast:.2f}:slow={slow:.2f}:gap={gap_pct:.3f}%")
    if all(x == "long" for x in dirs):
        direction = "long"
    elif all(x == "short" for x in dirs):
        direction = "short"
    else:
        direction = "neutral"
    return TrendSnapshot(profile=profile, direction=direction, detail="; ".join(details))


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
            if abs(bar.open - long_target) <= abs(bar.open - short_target):
                return "long", bar, "both_targets_same_intraday_bar_choose_open_distance_long", True, True, True
            return "short", bar, "both_targets_same_intraday_bar_choose_open_distance_short", True, True, True
        if long_hit:
            return "long", bar, "long_target_first_touch", True, short_hit_day, False
        if short_hit:
            return "short", bar, "short_target_first_touch", long_hit_day, True, False
    return None, None, "no_breakout", long_hit_day, short_hit_day, False


def _calc_atr(days: list[SessionDay], idx: int, period: int) -> float | None:
    if idx <= 0 or period <= 0:
        return None
    start = max(1, idx - period)
    trs: list[float] = []
    for j in range(start, idx):
        prev_close = days[j - 1].close
        d = days[j]
        tr = max(d.high - d.low, abs(d.high - prev_close), abs(d.low - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def _resolve_atr_exit(
    *,
    side: Side,
    entry_bar: Candle,
    day: SessionDay,
    next_day: SessionDay,
    stop_price: float,
    take_profit_price: float,
    cfg: TrendAtrConfig,
) -> tuple[float, int, str]:
    for bar in day.bars:
        if bar.ts < entry_bar.ts:
            continue
        if side == "long":
            stop_hit = bar.low <= stop_price
            target_hit = bar.high >= take_profit_price
        else:
            stop_hit = bar.high >= stop_price
            target_hit = bar.low <= take_profit_price

        if stop_hit and target_hit:
            if cfg.exit_same_candle_mode == "target_first":
                return take_profit_price, bar.ts, "atr_tp_same_bar_target_first"
            if cfg.exit_same_candle_mode == "open_distance":
                if abs(bar.open - take_profit_price) < abs(bar.open - stop_price):
                    return take_profit_price, bar.ts, "atr_tp_same_bar_open_distance"
                return stop_price, bar.ts, "atr_sl_same_bar_open_distance"
            return stop_price, bar.ts, "atr_sl_same_bar_stop_first"
        if stop_hit:
            return stop_price, bar.ts, "atr_stop_loss"
        if target_hit:
            return take_profit_price, bar.ts, "atr_take_profit"
    return next_day.open, next_day.ts, "next_utc00_open_time_exit"


def _calc_trade(
    *,
    symbol: str,
    side: Side,
    entry_bar: Candle,
    entry_raw: float,
    day: SessionDay,
    next_day: SessionDay,
    atr: float,
    equity_before: float,
    symbol_count: int,
    cfg: TrendAtrConfig,
) -> TrendAtrTrade | None:
    entry = _entry_price(side, entry_raw, cfg)
    qty, notional = _size(equity_before, entry, symbol_count, cfg)
    if qty <= 0 or notional <= 0:
        return None
    if side == "long":
        stop = entry - (atr * cfg.atr_stop_mult)
        tp = entry + (atr * cfg.atr_take_profit_mult)
    else:
        stop = entry + (atr * cfg.atr_stop_mult)
        tp = entry - (atr * cfg.atr_take_profit_mult)
    raw_exit, exit_ts, exit_reason = _resolve_atr_exit(
        side=side,
        entry_bar=entry_bar,
        day=day,
        next_day=next_day,
        stop_price=stop,
        take_profit_price=tp,
        cfg=cfg,
    )
    exit_px = _exit_price(side, raw_exit, cfg)
    gross = (exit_px - entry) * qty if side == "long" else (entry - exit_px) * qty
    fees = (abs(entry * qty) + abs(exit_px * qty)) * cfg.taker_fee_rate
    net = gross - fees
    return TrendAtrTrade(
        symbol=symbol,
        side=side,
        entry_date=_date_from_ts(entry_bar.ts),
        entry_time_utc=_iso_ms(entry_bar.ts),
        exit_date=_date_from_ts(exit_ts),
        exit_time_utc=_iso_ms(exit_ts),
        entry_price=round(entry, 8),
        exit_price=round(exit_px, 8),
        stop_price=round(stop, 8),
        take_profit_price=round(tp, 8),
        qty=round(qty, 10),
        notional=round(notional, 8),
        pnl=round(gross, 8),
        fees=round(fees, 8),
        net_pnl=round(net, 8),
        return_on_notional_pct=round((net / notional) * 100.0 if notional else 0.0, 8),
        return_on_equity_pct=round((net / equity_before) * 100.0 if equity_before else 0.0, 8),
        exit_reason=exit_reason,
        k_value=cfg.k_value,
        trend_profile=cfg.trend_profile,
        same_candle_mode=cfg.same_candle_mode,
        atr_period=cfg.atr_period,
        atr_stop_mult=cfg.atr_stop_mult,
        atr_take_profit_mult=cfg.atr_take_profit_mult,
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


def _profit_factor(trades: list[TrendAtrTrade]) -> float:
    gross_profit = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gross_loss = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _max_loss_streak(trades: list[TrendAtrTrade]) -> int:
    best = 0
    cur = 0
    for t in trades:
        if t.net_pnl < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _monthly_returns(curve: list[TrendAtrCurvePoint], initial_equity: float) -> dict[str, float]:
    if not curve:
        return {}
    month_start: dict[str, float] = {}
    month_end: dict[str, float] = {}
    prev_equity = initial_equity
    for p in curve:
        m = p.date[:7]
        if m not in month_start:
            month_start[m] = prev_equity
        month_end[m] = p.equity
        prev_equity = p.equity
    return {m: ((month_end[m] / month_start[m]) - 1.0) * 100.0 for m in month_end if month_start.get(m, 0) > 0}


def run_trend_atr_backtest(symbol_bars: dict[str, list[Candle]], cfg: TrendAtrConfig) -> dict:
    required_intervals = sorted({c.interval for c in _parse_trend_profile(cfg.trend_profile)}, key=lambda x: INTERVAL_MS[x])
    symbol_days = {s: _aggregate_utc_days(bars, cfg.min_bars_per_day) for s, bars in symbol_bars.items()}
    symbol_trends = {
        s: {interval: _resample(bars, interval) for interval in required_intervals}
        for s, bars in symbol_bars.items()
    }
    by_date: dict[str, dict[str, tuple[int, SessionDay]]] = {}
    for symbol, days in symbol_days.items():
        for i, d in enumerate(days):
            by_date.setdefault(d.date, {})[symbol] = (i, d)

    dates = sorted(by_date)
    equity = cfg.initial_equity
    peak = cfg.initial_equity
    max_dd = 0.0
    trades: list[TrendAtrTrade] = []
    signals: list[TrendAtrSignal] = []
    curve: list[TrendAtrCurvePoint] = []
    symbol_count = max(len(symbol_bars), 1)

    for d in dates:
        candidates: list[tuple[float, str, Side, SessionDay, SessionDay, Candle, float, float]] = []
        for symbol, days in symbol_days.items():
            item = by_date[d].get(symbol)
            if item is None:
                continue
            idx, day = item
            if idx < 1 or idx + 1 >= len(days):
                continue
            prev = days[idx - 1]
            next_day = days[idx + 1]
            atr = _calc_atr(days, idx, cfg.atr_period)
            prev_range = max(prev.high - prev.low, 0.0)
            long_target = day.open + prev_range * cfg.k_value
            short_target = day.open - prev_range * cfg.k_value
            trend = _trend_snapshot(cfg.trend_profile, symbol_trends.get(symbol, {}), day.ts)
            side, touch_bar, touch_reason, long_hit, short_hit, both_same = _first_touch(day, long_target, short_target, cfg.same_candle_mode)

            status = "HOLD"
            reason = touch_reason
            allowed_by_trend = True
            if side is not None:
                status = "ENTRY"
                if trend.direction not in {"both", side}:
                    status = "FILTERED"
                    allowed_by_trend = False
                    reason = f"trend_filter_block_{side}_trend_{trend.direction}"
            if atr is None and side is not None and allowed_by_trend:
                status = "FILTERED"
                allowed_by_trend = False
                reason = "not_enough_atr_days"

            signals.append(
                TrendAtrSignal(
                    date=d,
                    symbol=symbol,
                    status=status,
                    side=(side or "").upper(),
                    reason=reason,
                    day_open=day.open,
                    day_high=day.high,
                    day_low=day.low,
                    day_close=day.close,
                    previous_high=prev.high,
                    previous_low=prev.low,
                    previous_range=prev_range,
                    long_target=long_target,
                    short_target=short_target,
                    atr=atr,
                    trend_profile=cfg.trend_profile,
                    trend_direction=trend.direction,
                    trend_detail=trend.detail,
                    first_touch_ts=touch_bar.ts if touch_bar else None,
                    first_touch_utc=_iso_ms(touch_bar.ts) if touch_bar else "",
                    first_touch_bar_open=touch_bar.open if touch_bar else None,
                    long_hit=long_hit,
                    short_hit=short_hit,
                    both_same_bar=both_same,
                )
            )

            if side is None or touch_bar is None or not allowed_by_trend or atr is None:
                continue
            if side == "long":
                strength = max(0.0, day.high - long_target) / max(prev_range, 1e-9)
                entry_raw = long_target
            else:
                strength = max(0.0, short_target - day.low) / max(prev_range, 1e-9)
                entry_raw = short_target
            candidates.append((strength, symbol, side, day, next_day, touch_bar, entry_raw, atr))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            _strength, symbol, side, day, next_day, touch_bar, entry_raw, atr = candidates[0]
            tr = _calc_trade(
                symbol=symbol,
                side=side,
                entry_bar=touch_bar,
                entry_raw=entry_raw,
                day=day,
                next_day=next_day,
                atr=atr,
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
        curve.append(TrendAtrCurvePoint(date=d, equity=round(equity, 8), drawdown_pct=round(dd, 8), trade_count=len(trades)))
        if equity <= 0:
            break

    wins = sum(1 for t in trades if t.net_pnl > 0)
    pf = _profit_factor(trades)
    months = _monthly_returns(curve, cfg.initial_equity)
    return {
        "start_equity": cfg.initial_equity,
        "end_equity": round(equity, 8),
        "return_pct": round(((equity / cfg.initial_equity) - 1.0) * 100.0 if cfg.initial_equity else 0.0, 8),
        "max_drawdown_pct": round(max_dd, 8),
        "trade_count": len(trades),
        "win_rate_pct": round((wins / len(trades)) * 100.0 if trades else 0.0, 8),
        "profit_factor": round(pf, 8) if math.isfinite(pf) else float("inf"),
        "avg_net_pnl": round(sum(t.net_pnl for t in trades) / len(trades), 8) if trades else 0.0,
        "max_loss_streak": _max_loss_streak(trades),
        "trades": trades,
        "signals": signals,
        "curve": curve,
        "monthly_returns_pct": months,
    }


def _load_intraday(symbols: list[str], years: int, interval: str, refresh: bool) -> dict[str, list[Candle]]:
    data_dir = _data_dir()
    out: dict[str, list[Candle]] = {}
    for s in symbols:
        print(f"[data] {s} {interval} {years}y: loading Bitget history candles...")
        candles = fetch_bitget_history_candles(symbol=s, interval=interval, years=years, data_dir=data_dir, refresh=refresh)
        print(f"[data] {s}: bars={len(candles)} from {_iso_ms(candles[0].ts)} to {_iso_ms(candles[-1].ts)}")
        out[s] = candles
    return out


def _cell(result: dict) -> str:
    return f"{result['end_equity']:,.0f} / {result['max_drawdown_pct']:.1f}% / PF {result['profit_factor']:.2f}"


def _out_dir() -> Path:
    p = ROOT / "backtests" / "v33_larry_trend_filter"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _result_rows_to_table(rows: list[dict], row_key: str, col_key: str, row_vals: list, col_vals: list, title: str, subtitle: str, csv_path: Path, txt_path: Path) -> None:
    header = [row_key] + [str(x) for x in col_vals]
    widths = {h: max(len(h), 12) for h in header}
    table: list[dict[str, str]] = []
    for rv in row_vals:
        line = {row_key: str(rv)}
        for cv in col_vals:
            cv_s = str(cv)
            hit = next((r for r in rows if str(r[row_key]) == str(rv) and str(r[col_key]) == cv_s), None)
            line[cv_s] = str(hit["cell"]) if hit else "-"
        table.append(line)
        for h in header:
            widths[h] = max(widths[h], len(line[h]))
    with txt_path.open("w", encoding="utf-8") as f:
        f.write(title + "\n")
        f.write("=" * len(title) + "\n")
        f.write("Format: final equity / MDD / PF\n")
        f.write(subtitle + "\n\n")
        f.write("  ".join(h.ljust(widths[h]) for h in header) + "\n")
        f.write("  ".join("-" * widths[h] for h in header) + "\n")
        for line in table:
            f.write("  ".join(line[h].ljust(widths[h]) for h in header) + "\n")
        f.write("\nCSV: " + str(csv_path) + "\n")
    print(txt_path.read_text(encoding="utf-8"))
    print("saved:", txt_path)


def _base_cfg(args: argparse.Namespace, *, capital_ratio: float | None = None, leverage: float | None = None, k: float | None = None, trend_profile: str | None = None) -> TrendAtrConfig:
    return TrendAtrConfig(
        initial_equity=args.initial_equity,
        capital_ratio=args.capital_ratio if capital_ratio is None else capital_ratio,
        leverage=args.leverage if leverage is None else leverage,
        max_order_notional_usdt=args.max_notional,
        k_value=args.k if k is None else k,
        taker_fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        same_candle_mode=args.same_candle_mode,
        exit_same_candle_mode=args.exit_same_candle_mode,
        min_bars_per_day=args.min_bars_per_day,
        trend_profile=args.trend_profile if trend_profile is None else trend_profile,
        atr_period=args.atr_period,
        atr_stop_mult=args.atr_stop_mult,
        atr_take_profit_mult=args.atr_take_profit_mult,
    )


def cmd_run(args: argparse.Namespace) -> None:
    symbols = _split_csv(args.symbols)
    bars = _load_intraday(symbols, args.years, args.interval, args.refresh)
    cfg = _base_cfg(args)
    result = run_trend_atr_backtest(bars, cfg)
    out_dir = _out_dir()
    _write_csv(out_dir / "larry_trend_filter_trades_latest.csv", result["trades"])
    _write_csv(out_dir / "larry_trend_filter_signals_latest.csv", result["signals"])
    _write_csv(out_dir / "larry_trend_filter_equity_latest.csv", result["curve"])
    month_rows = [{"month": m, "return_pct": round(v, 8)} for m, v in sorted(result["monthly_returns_pct"].items())]
    _write_csv(out_dir / "larry_trend_filter_months_latest.csv", month_rows)
    worst = sorted(result["monthly_returns_pct"].items(), key=lambda x: x[1])[:10]
    best = sorted(result["monthly_returns_pct"].items(), key=lambda x: x[1], reverse=True)[:10]
    lines = [
        "Index Sniper Pro v3.3 Larry First-Touch + Trend Filter + ATR Exit",
        "===================================================================",
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
        "- direction filter: trend profile allows only matching LONG/SHORT entries",
        "- trend uses previous closed trend candles only, no current-day lookahead",
        "- exit: ATR stop/take-profit using prior daily ATR; else next UTC 00:00 / KST 09:00 open",
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
        f"- trend_profile: {cfg.trend_profile}",
        f"- atr_period: {cfg.atr_period}",
        f"- atr_stop_mult: {cfg.atr_stop_mult}",
        f"- atr_take_profit_mult: {cfg.atr_take_profit_mult}",
        f"- taker_fee_rate: {cfg.taker_fee_rate}",
        f"- slippage_bps: {cfg.slippage_bps}",
        f"- same_candle_mode: {cfg.same_candle_mode}",
        f"- exit_same_candle_mode: {cfg.exit_same_candle_mode}",
        f"- min_bars_per_day: {cfg.min_bars_per_day}",
        "",
        "Worst months:",
    ]
    lines += [f"- {m}: {round(v, 6)}%" for m, v in worst]
    lines += ["", "Best months:"]
    lines += [f"- {m}: {round(v, 6)}%" for m, v in best]
    txt = "\n".join(lines) + "\n"
    (out_dir / "larry_trend_filter_summary_latest.txt").write_text(txt, encoding="utf-8")
    name = f"larry_trend_filter_summary_{'_'.join(symbols)}_{args.years}y_k{cfg.k_value:g}_{cfg.leverage:g}x_cr{cfg.capital_ratio:g}_{cfg.trend_profile.replace('+','_')}.txt"
    (out_dir / name).write_text(txt, encoding="utf-8")
    print(txt)
    print("saved:", out_dir / "larry_trend_filter_summary_latest.txt")
    print("saved:", out_dir / name)


def cmd_trend_sweep(args: argparse.Namespace) -> None:
    symbols = _split_csv(args.symbols)
    years_list = _parse_ints(args.years)
    trend_profiles = _split_csv(args.trend_profiles)
    out_dir = _out_dir()
    rows: list[dict] = []
    for years in years_list:
        bars = _load_intraday(symbols, years, args.interval, args.refresh)
        for profile in trend_profiles:
            cfg = _base_cfg(args, trend_profile=profile)
            result = run_trend_atr_backtest({s: list(c) for s, c in bars.items()}, cfg)
            rows.append({
                "years": years,
                "trend_profile": profile,
                "k": args.k,
                "leverage": f"{args.leverage:g}x",
                "capital_ratio": f"{args.capital_ratio:g}",
                "cell": _cell(result),
                "end_equity": result["end_equity"],
                "return_pct": result["return_pct"],
                "mdd_pct": result["max_drawdown_pct"],
                "trade_count": result["trade_count"],
                "win_rate_pct": result["win_rate_pct"],
                "profit_factor": result["profit_factor"],
                "max_loss_streak": result["max_loss_streak"],
            })
    csv_path = out_dir / "larry_trend_filter_trend_sweep_latest.csv"
    _write_csv(csv_path, rows)
    txt_path = out_dir / "larry_trend_filter_trend_sweep_latest.txt"
    _result_rows_to_table(
        rows, "years", "trend_profile", years_list, trend_profiles,
        "Index Sniper Pro v3.3 Larry Trend Filter Sweep",
        f"symbols={','.join(symbols)} k={args.k:g} leverage={args.leverage:g}x interval={args.interval} capital_ratio={args.capital_ratio} ATR={args.atr_stop_mult:g}/{args.atr_take_profit_mult:g} same_candle_mode={args.same_candle_mode}",
        csv_path, txt_path,
    )


def cmd_ksweep(args: argparse.Namespace) -> None:
    symbols = _split_csv(args.symbols)
    years_list = _parse_ints(args.years)
    k_values = _parse_floats(args.k_values)
    out_dir = _out_dir()
    rows: list[dict] = []
    for years in years_list:
        bars = _load_intraday(symbols, years, args.interval, args.refresh)
        for k in k_values:
            cfg = _base_cfg(args, k=k)
            result = run_trend_atr_backtest({s: list(c) for s, c in bars.items()}, cfg)
            rows.append({
                "years": years,
                "k": f"{k:g}",
                "trend_profile": args.trend_profile,
                "leverage": f"{args.leverage:g}x",
                "capital_ratio": f"{args.capital_ratio:g}",
                "cell": _cell(result),
                "end_equity": result["end_equity"],
                "return_pct": result["return_pct"],
                "mdd_pct": result["max_drawdown_pct"],
                "trade_count": result["trade_count"],
                "win_rate_pct": result["win_rate_pct"],
                "profit_factor": result["profit_factor"],
                "max_loss_streak": result["max_loss_streak"],
            })
    csv_path = out_dir / "larry_trend_filter_k_sweep_latest.csv"
    _write_csv(csv_path, rows)
    txt_path = out_dir / "larry_trend_filter_k_sweep_latest.txt"
    k_cols = [f"{x:g}" for x in k_values]
    _result_rows_to_table(
        rows, "years", "k", years_list, k_cols,
        "Index Sniper Pro v3.3 Larry Trend Filter K Sweep",
        f"symbols={','.join(symbols)} trend_profile={args.trend_profile} leverage={args.leverage:g}x interval={args.interval} capital_ratio={args.capital_ratio} ATR={args.atr_stop_mult:g}/{args.atr_take_profit_mult:g}",
        csv_path, txt_path,
    )


def cmd_sweep(args: argparse.Namespace) -> None:
    symbols = _split_csv(args.symbols)
    years_list = _parse_ints(args.years)
    leverages = _parse_floats(args.leverages)
    out_dir = _out_dir()
    rows: list[dict] = []
    for years in years_list:
        bars = _load_intraday(symbols, years, args.interval, args.refresh)
        for lev in leverages:
            cfg = _base_cfg(args, leverage=lev)
            result = run_trend_atr_backtest({s: list(c) for s, c in bars.items()}, cfg)
            rows.append({
                "years": years,
                "leverage": f"{lev:g}x",
                "k": args.k,
                "trend_profile": args.trend_profile,
                "capital_ratio": f"{args.capital_ratio:g}",
                "cell": _cell(result),
                "end_equity": result["end_equity"],
                "return_pct": result["return_pct"],
                "mdd_pct": result["max_drawdown_pct"],
                "trade_count": result["trade_count"],
                "win_rate_pct": result["win_rate_pct"],
                "profit_factor": result["profit_factor"],
                "max_loss_streak": result["max_loss_streak"],
            })
    csv_path = out_dir / "larry_trend_filter_sweep_latest.csv"
    _write_csv(csv_path, rows)
    txt_path = out_dir / "larry_trend_filter_sweep_latest.txt"
    lev_cols = [f"{x:g}x" for x in leverages]
    _result_rows_to_table(
        rows, "years", "leverage", years_list, lev_cols,
        "Index Sniper Pro v3.3 Larry Trend Filter Leverage Sweep",
        f"symbols={','.join(symbols)} trend_profile={args.trend_profile} k={args.k:g} interval={args.interval} capital_ratio={args.capital_ratio} ATR={args.atr_stop_mult:g}/{args.atr_take_profit_mult:g}",
        csv_path, txt_path,
    )


def cmd_capital_sweep(args: argparse.Namespace) -> None:
    symbols = _split_csv(args.symbols)
    years_list = _parse_ints(args.years)
    capital_ratios = _parse_floats(args.capital_ratios)
    out_dir = _out_dir()
    rows: list[dict] = []
    for years in years_list:
        bars = _load_intraday(symbols, years, args.interval, args.refresh)
        for cr in capital_ratios:
            cfg = _base_cfg(args, capital_ratio=cr)
            result = run_trend_atr_backtest({s: list(c) for s, c in bars.items()}, cfg)
            rows.append({
                "years": years,
                "capital_ratio": f"{cr:g}",
                "k": args.k,
                "trend_profile": args.trend_profile,
                "leverage": f"{args.leverage:g}x",
                "cell": _cell(result),
                "end_equity": result["end_equity"],
                "return_pct": result["return_pct"],
                "mdd_pct": result["max_drawdown_pct"],
                "trade_count": result["trade_count"],
                "win_rate_pct": result["win_rate_pct"],
                "profit_factor": result["profit_factor"],
                "max_loss_streak": result["max_loss_streak"],
            })
    csv_path = out_dir / "larry_trend_filter_capital_sweep_latest.csv"
    _write_csv(csv_path, rows)
    txt_path = out_dir / "larry_trend_filter_capital_sweep_latest.txt"
    cr_cols = [f"{x:g}" for x in capital_ratios]
    _result_rows_to_table(
        rows, "years", "capital_ratio", years_list, cr_cols,
        "Index Sniper Pro v3.3 Larry Trend Filter Capital Sweep",
        f"symbols={','.join(symbols)} trend_profile={args.trend_profile} k={args.k:g} leverage={args.leverage:g}x interval={args.interval} ATR={args.atr_stop_mult:g}/{args.atr_take_profit_mult:g}",
        csv_path, txt_path,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="v3.3 Larry first-touch + trend filter + ATR exit backtester")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--symbols", default=os.getenv("BT_V33_SYMBOLS", os.getenv("BT_V32_SYMBOLS", "BTCUSDT")))
        sp.add_argument("--interval", choices=sorted(INTERVAL_MS), default=os.getenv("BT_V33_INTERVAL", os.getenv("BT_V32_INTERVAL", "1H")))
        sp.add_argument("--initial-equity", type=float, default=float(os.getenv("BT_V33_INITIAL_EQUITY", os.getenv("BT_INITIAL_EQUITY", "1374"))))
        sp.add_argument("--capital-ratio", type=float, default=float(os.getenv("BT_V33_CAPITAL_RATIO", os.getenv("BT_CAPITAL_RATIO", "0.30"))))
        sp.add_argument("--max-notional", type=float, default=float(os.getenv("BT_V33_MAX_NOTIONAL", os.getenv("BT_OPT_MAX_ORDER_NOTIONAL_USDT", "999999"))))
        sp.add_argument("--fee-rate", type=float, default=float(os.getenv("BT_V33_FEE_RATE", "0.0006")))
        sp.add_argument("--slippage-bps", type=float, default=float(os.getenv("BT_V33_SLIPPAGE_BPS", "2.0")))
        sp.add_argument("--same-candle-mode", choices=["skip", "open_distance", "candle"], default=os.getenv("BT_V33_SAME_CANDLE_MODE", os.getenv("BT_V32_SAME_CANDLE_MODE", "skip")))
        sp.add_argument("--exit-same-candle-mode", choices=["stop_first", "target_first", "open_distance"], default=os.getenv("BT_V33_EXIT_SAME_CANDLE_MODE", "stop_first"))
        sp.add_argument("--min-bars-per-day", type=int, default=int(os.getenv("BT_V33_MIN_BARS_PER_DAY", os.getenv("BT_V32_MIN_BARS_PER_DAY", "20"))))
        sp.add_argument("--atr-period", type=int, default=int(os.getenv("BT_V33_ATR_PERIOD", "14")))
        sp.add_argument("--atr-stop-mult", type=float, default=float(os.getenv("BT_V33_ATR_STOP_MULT", os.getenv("ATR_STOP_MULT", "1.30"))))
        sp.add_argument("--atr-take-profit-mult", type=float, default=float(os.getenv("BT_V33_ATR_TAKE_PROFIT_MULT", os.getenv("ATR_TAKE_PROFIT_MULT", "2.00"))))
        sp.add_argument("--refresh", action="store_true")

    run = sub.add_parser("run")
    add_common(run)
    run.add_argument("--years", type=int, default=int(os.getenv("BT_V33_YEARS_ONE", "5")))
    run.add_argument("--leverage", type=float, default=float(os.getenv("BT_V33_LEVERAGE", os.getenv("LEVERAGE", "5"))))
    run.add_argument("--k", type=float, default=float(os.getenv("BT_V33_K", os.getenv("K_VALUE", "0.50"))))
    run.add_argument("--trend-profile", default=os.getenv("BT_V33_TREND_PROFILE", "4H_20_60"))
    run.set_defaults(func=cmd_run)

    trend_sweep = sub.add_parser("trend-sweep")
    add_common(trend_sweep)
    trend_sweep.add_argument("--years", default=os.getenv("BT_V33_YEARS", "1,2,3,4,5"))
    trend_sweep.add_argument("--leverage", type=float, default=float(os.getenv("BT_V33_LEVERAGE", os.getenv("LEVERAGE", "5"))))
    trend_sweep.add_argument("--k", type=float, default=float(os.getenv("BT_V33_K", os.getenv("K_VALUE", "0.50"))))
    trend_sweep.add_argument("--capital-ratio", type=float, default=float(os.getenv("BT_V33_CAPITAL_RATIO", os.getenv("BT_CAPITAL_RATIO", "0.30"))))
    trend_sweep.add_argument("--trend-profile", default=os.getenv("BT_V33_TREND_PROFILE", "4H_20_60"))
    trend_sweep.add_argument("--trend-profiles", default=os.getenv("BT_V33_TREND_PROFILES", "none,1H_20_60,1H_50_200,4H_20_60,4H_50_200,1H_20_60+4H_20_60,1H_50_200+4H_50_200"))
    trend_sweep.set_defaults(func=cmd_trend_sweep)

    ksweep = sub.add_parser("ksweep")
    add_common(ksweep)
    ksweep.add_argument("--years", default=os.getenv("BT_V33_YEARS", "1,2,3,4,5"))
    ksweep.add_argument("--leverage", type=float, default=float(os.getenv("BT_V33_LEVERAGE", os.getenv("LEVERAGE", "5"))))
    ksweep.add_argument("--k", type=float, default=float(os.getenv("BT_V33_K", os.getenv("K_VALUE", "0.50"))))
    ksweep.add_argument("--k-values", default=os.getenv("BT_V33_K_VALUES", "0.25,0.35,0.50,0.65,0.80,1.00"))
    ksweep.add_argument("--trend-profile", default=os.getenv("BT_V33_TREND_PROFILE", "4H_20_60"))
    ksweep.set_defaults(func=cmd_ksweep)

    sweep = sub.add_parser("sweep")
    add_common(sweep)
    sweep.add_argument("--years", default=os.getenv("BT_V33_YEARS", "1,2,3,4,5"))
    sweep.add_argument("--leverage", type=float, default=float(os.getenv("BT_V33_LEVERAGE", os.getenv("LEVERAGE", "5"))))
    sweep.add_argument("--leverages", default=os.getenv("BT_V33_LEVERAGES", "1,2,3,4,5,6,7,8,9,10"))
    sweep.add_argument("--k", type=float, default=float(os.getenv("BT_V33_K", os.getenv("K_VALUE", "0.50"))))
    sweep.add_argument("--trend-profile", default=os.getenv("BT_V33_TREND_PROFILE", "4H_20_60"))
    sweep.set_defaults(func=cmd_sweep)

    cap = sub.add_parser("capital-sweep")
    add_common(cap)
    cap.add_argument("--years", default=os.getenv("BT_V33_YEARS", "1,2,3,4,5"))
    cap.add_argument("--leverage", type=float, default=float(os.getenv("BT_V33_LEVERAGE", os.getenv("LEVERAGE", "5"))))
    cap.add_argument("--k", type=float, default=float(os.getenv("BT_V33_K", os.getenv("K_VALUE", "0.50"))))
    cap.add_argument("--trend-profile", default=os.getenv("BT_V33_TREND_PROFILE", "4H_20_60"))
    cap.add_argument("--capital-ratios", default=os.getenv("BT_V33_CAPITAL_RATIOS", "0.30,0.70,1.00"))
    cap.set_defaults(func=cmd_capital_sweep)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
