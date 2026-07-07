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

ROOT = Path.cwd()
if load_dotenv is not None:
    try:
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1H": 60 * 60_000,
    "4H": 4 * 60 * 60_000,
}

Side = Literal["long", "short"]


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    turnover: float = 0.0


@dataclass(frozen=True)
class QuantConfig:
    symbol: str = "BTCUSDT"
    initial_equity: float = 1374.0
    capital_ratio: float = 0.30
    leverage: float = 3.0
    max_order_notional_usdt: float = 999999.0
    taker_fee_rate: float = 0.0006
    slippage_bps: float = 2.0
    profile: str = "trend_volume"
    trend_gate: str = "ema80_240"
    entry_threshold: float = 55.0
    exit_threshold: float = 15.0
    max_hold_bars: int = 24
    atr_period: int = 24
    atr_stop_mult: float = 1.50
    atr_take_profit_mult: float = 3.00
    min_vol_mult: float = 0.0
    max_vol_mult: float = 2.50
    allow_long: bool = True
    allow_short: bool = True


@dataclass
class Position:
    side: Side
    entry_ts: int
    entry_price: float
    qty: float
    notional: float
    stop_price: float
    take_profit_price: float
    bars_held: int = 0
    entry_score: float = 0.0


@dataclass
class Trade:
    symbol: str
    side: Side
    entry_time_utc: str
    exit_time_utc: str
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
    bars_held: int
    entry_score: float
    exit_score: float
    profile: str
    trend_gate: str
    capital_ratio: float
    leverage: float


@dataclass
class CurvePoint:
    time_utc: str
    equity: float
    drawdown_pct: float
    trade_count: int


@dataclass
class RunResult:
    config: dict
    years: int
    start_equity: float
    end_equity: float
    return_pct: float
    max_drawdown_pct: float
    trade_count: int
    win_rate_pct: float
    profit_factor: float
    avg_net_pnl: float
    max_loss_streak: int
    worst_month: str
    worst_month_pct: float
    best_month: str
    best_month_pct: float


def _split_csv(text: str) -> list[str]:
    return [x.strip() for x in str(text or "").split(",") if x.strip()]


def _parse_floats(text: str) -> list[float]:
    return [float(x) for x in _split_csv(text)]


def _parse_ints(text: str) -> list[int]:
    return [int(x) for x in _split_csv(text)]


def _utc_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _iso_ms(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()


def _month_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m")


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b not in (0, 0.0) else default


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _slip(price: float, bps: float, adverse: int) -> float:
    return price * (1.0 + adverse * (bps / 10000.0))


def _entry_price(side: Side, raw: float, cfg: QuantConfig) -> float:
    return _slip(raw, cfg.slippage_bps, +1 if side == "long" else -1)


def _exit_price(side: Side, raw: float, cfg: QuantConfig) -> float:
    return _slip(raw, cfg.slippage_bps, -1 if side == "long" else +1)


def _size(equity: float, price: float, cfg: QuantConfig) -> tuple[float, float]:
    capital = equity * cfg.capital_ratio
    notional = min(capital * cfg.leverage, cfg.max_order_notional_usdt)
    if price <= 0 or notional <= 0 or equity <= 0:
        return 0.0, 0.0
    return notional / price, notional


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
    # Reuse v3.1/v3.2/v3.3 cache where possible.
    raw = os.getenv("BT_V40_DATA_DIR", os.getenv("BT_V33_DATA_DIR", os.getenv("BT_V32_DATA_DIR", "backtests/v31_larry_first_touch/data")))
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
        raise RuntimeError(f"unsupported interval for v4.0 backtest: {interval}")
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
    headers = {"User-Agent": "IndexSniperProV40QuantMultiAlpha/4.0", "locale": "en-US"}
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


def _slice_years(candles: list[Candle], years: int) -> list[Candle]:
    if not candles:
        return []
    end_ts = candles[-1].ts
    start_ts = end_ts - int(years * 366 * 24 * 60 * 60 * 1000)
    return [c for c in candles if c.ts >= start_ts]


def _ema_series(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    alpha = 2.0 / (period + 1.0)
    cur = sum(values[:period]) / period
    out[period - 1] = cur
    for i in range(period, len(values)):
        cur = (values[i] * alpha) + (cur * (1.0 - alpha))
        out[i] = cur
    return out


def _rolling_mean(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        if i >= period - 1:
            out[i] = s / period
    return out


def _rolling_std(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 1:
        return out
    for i in range(period - 1, len(values)):
        xs = values[i - period + 1 : i + 1]
        out[i] = _std(xs)
    return out


def _rolling_atr(candles: list[Candle], period: int) -> list[float | None]:
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c.high - c.low)
        else:
            prev = candles[i - 1].close
            trs.append(max(c.high - c.low, abs(c.high - prev), abs(c.low - prev)))
    return _rolling_mean(trs, period)


@dataclass
class FeaturePack:
    score: list[float | None]
    vol_mult: list[float | None]
    ema_fast: list[float | None]
    ema_slow: list[float | None]
    atr: list[float | None]


def _build_features(candles: list[Candle], cfg: QuantConfig) -> FeaturePack:
    closes = [c.close for c in candles]
    vols = [max(0.0, c.volume) for c in candles]
    rets: list[float] = [0.0]
    for i in range(1, len(closes)):
        rets.append(math.log(closes[i] / closes[i - 1]) if closes[i - 1] > 0 else 0.0)

    vol24 = _rolling_std(rets, 24)
    vol168 = _rolling_mean([abs(x) for x in rets], 168)
    atr = _rolling_atr(candles, cfg.atr_period)
    atr_long = _rolling_mean([x or 0.0 for x in atr], 240)
    vma48 = _rolling_mean(vols, 48)
    vstd48 = _rolling_std(vols, 48)

    # 1H EMAs. 80/240 roughly map to 4H 20/60. 24/96 is a faster regime gate.
    ema24 = _ema_series(closes, 24)
    ema96 = _ema_series(closes, 96)
    ema80 = _ema_series(closes, 80)
    ema240 = _ema_series(closes, 240)
    ema_fast = ema80 if cfg.trend_gate in {"ema80_240", "ema80_240_strict"} else ema24
    ema_slow = ema240 if cfg.trend_gate in {"ema80_240", "ema80_240_strict"} else ema96

    scores: list[float | None] = [None] * len(candles)
    vol_mult: list[float | None] = [None] * len(candles)

    for i in range(len(candles)):
        if i < 260:
            continue
        sigma = vol24[i] or 0.0
        if sigma <= 0:
            continue
        # Momentum z-scores over multiple horizons, clipped to avoid one bar dominating.
        def z(h: int) -> float:
            if i - h < 0:
                return 0.0
            r = math.log(closes[i] / closes[i - h]) if closes[i - h] > 0 else 0.0
            return _clip(_safe_div(r, sigma * math.sqrt(h), 0.0), -3.0, 3.0)

        z4 = z(4)
        z12 = z(12)
        z24 = z(24)
        z72 = z(72)
        trend_score = (8.0 * z4) + (12.0 * z12) + (18.0 * z24) + (14.0 * z72)

        # Regime score from EMAs.
        ef = ema_fast[i]
        es = ema_slow[i]
        if ef is None or es is None or es <= 0:
            regime_score = 0.0
        else:
            gap_pct = ((ef / es) - 1.0) * 100.0
            regime_score = _clip(gap_pct * 12.0, -35.0, 35.0)

        # Volume confirmation: directional move with abnormal volume gets modest support.
        vz = 0.0
        if vma48[i] is not None and vstd48[i] is not None and (vstd48[i] or 0.0) > 0:
            vz = _clip((vols[i] - (vma48[i] or 0.0)) / (vstd48[i] or 1.0), -3.0, 3.0)
        last_z = _clip(_safe_div(rets[i], sigma, 0.0), -4.0, 4.0)
        volume_score = math.copysign(min(abs(last_z) * max(vz, 0.0) * 5.0, 20.0), last_z)

        # Liquidation-like reversal proxy from OHLCV only.
        # Large 1H shock + volume spike often means local forced flow; fade part of it.
        reversal_score = 0.0
        if abs(last_z) >= 2.0 and vz >= 1.0:
            reversal_score = -math.copysign(min((abs(last_z) - 1.5) * (vz + 0.5) * 8.0, 35.0), last_z)

        # Volatility risk scales down entries when current ATR is much higher than its long average.
        vm = None
        if atr[i] is not None and atr_long[i] is not None and (atr_long[i] or 0.0) > 0:
            vm = (atr[i] or 0.0) / (atr_long[i] or 1.0)
        vm = vm or 1.0
        vol_mult[i] = vm
        risk_scale = 1.0
        if cfg.max_vol_mult > 0 and vm > cfg.max_vol_mult:
            risk_scale = 0.25
        elif vm > 1.8:
            risk_scale = 0.55
        elif vm > 1.4:
            risk_scale = 0.75
        elif cfg.min_vol_mult > 0 and vm < cfg.min_vol_mult:
            risk_scale = 0.50

        if cfg.profile == "trend":
            raw = trend_score + regime_score
        elif cfg.profile == "trend_volume":
            raw = trend_score + regime_score + volume_score
        elif cfg.profile == "hybrid":
            raw = trend_score + regime_score + volume_score + reversal_score
        elif cfg.profile == "hybrid_defensive":
            raw = (0.85 * trend_score) + regime_score + (0.75 * volume_score) + (1.20 * reversal_score)
        elif cfg.profile == "reversal":
            raw = (0.30 * trend_score) + (0.50 * regime_score) + (1.50 * reversal_score)
        else:
            raw = trend_score + regime_score + volume_score + reversal_score
        scores[i] = _clip(raw * risk_scale, -100.0, 100.0)

    return FeaturePack(score=scores, vol_mult=vol_mult, ema_fast=ema_fast, ema_slow=ema_slow, atr=atr)


def _trend_gate_allows(side: Side, i: int, features: FeaturePack, cfg: QuantConfig) -> bool:
    gate = cfg.trend_gate.strip().lower()
    if gate in {"none", "off", "no", "no_filter"}:
        return True
    ef = features.ema_fast[i]
    es = features.ema_slow[i]
    if ef is None or es is None:
        return False
    if side == "long":
        if gate.endswith("strict"):
            return ef > es and ef > (features.ema_fast[i - 1] or ef)
        return ef > es
    if gate.endswith("strict"):
        return ef < es and ef < (features.ema_fast[i - 1] or ef)
    return ef < es


def _close_position(pos: Position, bar: Candle, raw_price: float, reason: str, score: float, equity_before: float, cfg: QuantConfig) -> tuple[Trade, float]:
    exit_px = _exit_price(pos.side, raw_price, cfg)
    if pos.side == "long":
        pnl = (exit_px - pos.entry_price) * pos.qty
    else:
        pnl = (pos.entry_price - exit_px) * pos.qty
    fees = (pos.entry_price * pos.qty + exit_px * pos.qty) * cfg.taker_fee_rate
    net = pnl - fees
    trade = Trade(
        symbol=cfg.symbol,
        side=pos.side,
        entry_time_utc=_iso_ms(pos.entry_ts),
        exit_time_utc=_iso_ms(bar.ts),
        entry_price=round(pos.entry_price, 8),
        exit_price=round(exit_px, 8),
        qty=round(pos.qty, 10),
        notional=round(pos.notional, 8),
        pnl=round(pnl, 8),
        fees=round(fees, 8),
        net_pnl=round(net, 8),
        return_on_notional_pct=round(_safe_div(net, pos.notional, 0.0) * 100.0, 8),
        return_on_equity_pct=round(_safe_div(net, equity_before, 0.0) * 100.0, 8),
        exit_reason=reason,
        bars_held=pos.bars_held,
        entry_score=round(pos.entry_score, 6),
        exit_score=round(score, 6),
        profile=cfg.profile,
        trend_gate=cfg.trend_gate,
        capital_ratio=cfg.capital_ratio,
        leverage=cfg.leverage,
    )
    return trade, equity_before + net


def run_backtest(candles: list[Candle], cfg: QuantConfig, years: int = 5, record: bool = False) -> tuple[RunResult, list[Trade], list[CurvePoint]]:
    candles = _slice_years(candles, years)
    if len(candles) < 500:
        raise RuntimeError(f"not enough candles after {years}y slice: {len(candles)}")
    features = _build_features(candles, cfg)

    equity = cfg.initial_equity
    peak = equity
    max_dd = 0.0
    pos: Position | None = None
    pending: Side | None = None
    pending_score = 0.0
    trades: list[Trade] = []
    curve: list[CurvePoint] = []

    for i, bar in enumerate(candles):
        score = features.score[i]
        score_f = float(score or 0.0)
        atr = features.atr[i]

        # Execute pending entry at this bar open, using only prior close signal.
        if pos is None and pending is not None and atr is not None and atr > 0 and equity > 0:
            side = pending
            raw_entry = bar.open
            entry_px = _entry_price(side, raw_entry, cfg)
            qty, notional = _size(equity, entry_px, cfg)
            if qty > 0 and notional > 0:
                if side == "long":
                    stop = entry_px - atr * cfg.atr_stop_mult
                    tp = entry_px + atr * cfg.atr_take_profit_mult
                else:
                    stop = entry_px + atr * cfg.atr_stop_mult
                    tp = entry_px - atr * cfg.atr_take_profit_mult
                pos = Position(side=side, entry_ts=bar.ts, entry_price=entry_px, qty=qty, notional=notional, stop_price=stop, take_profit_price=tp, entry_score=pending_score)
            pending = None
            pending_score = 0.0

        # Manage current position inside this bar, stop-first if both happen.
        if pos is not None:
            pos.bars_held += 1
            exit_reason = None
            exit_raw = None
            if pos.side == "long":
                stop_hit = bar.low <= pos.stop_price
                tp_hit = bar.high >= pos.take_profit_price
                if stop_hit:
                    exit_reason = "atr_stop_loss"
                    exit_raw = pos.stop_price
                elif tp_hit:
                    exit_reason = "atr_take_profit"
                    exit_raw = pos.take_profit_price
            else:
                stop_hit = bar.high >= pos.stop_price
                tp_hit = bar.low <= pos.take_profit_price
                if stop_hit:
                    exit_reason = "atr_stop_loss"
                    exit_raw = pos.stop_price
                elif tp_hit:
                    exit_reason = "atr_take_profit"
                    exit_raw = pos.take_profit_price

            # Close on score decay/opposite score at bar close.
            if exit_reason is None and score is not None:
                if pos.side == "long" and score_f <= cfg.exit_threshold:
                    exit_reason = "score_decay_or_flip"
                    exit_raw = bar.close
                elif pos.side == "short" and score_f >= -cfg.exit_threshold:
                    exit_reason = "score_decay_or_flip"
                    exit_raw = bar.close
            if exit_reason is None and pos.bars_held >= cfg.max_hold_bars:
                exit_reason = "max_hold_exit"
                exit_raw = bar.close

            if exit_reason is not None and exit_raw is not None:
                trade, equity = _close_position(pos, bar, exit_raw, exit_reason, score_f, equity, cfg)
                trades.append(trade)
                pos = None
                pending = None
                pending_score = 0.0

        # Decide next-bar entry at close.
        if pos is None and pending is None and score is not None and atr is not None and atr > 0 and equity > 0:
            vm = features.vol_mult[i] or 1.0
            vol_ok = (cfg.max_vol_mult <= 0 or vm <= cfg.max_vol_mult) and (cfg.min_vol_mult <= 0 or vm >= cfg.min_vol_mult)
            if vol_ok and score_f >= cfg.entry_threshold and cfg.allow_long and _trend_gate_allows("long", i, features, cfg):
                pending = "long"
                pending_score = score_f
            elif vol_ok and score_f <= -cfg.entry_threshold and cfg.allow_short and _trend_gate_allows("short", i, features, cfg):
                pending = "short"
                pending_score = score_f

        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        if record or i % 24 == 0:
            curve.append(CurvePoint(time_utc=_iso_ms(bar.ts), equity=round(equity, 8), drawdown_pct=round(dd, 6), trade_count=len(trades)))

    # Close leftover at final close.
    if pos is not None:
        last = candles[-1]
        trade, equity = _close_position(pos, last, last.close, "final_close", features.score[-1] or 0.0, equity, cfg)
        trades.append(trade)
        pos = None

    peak = cfg.initial_equity
    max_dd = 0.0
    eq = cfg.initial_equity
    month_start: dict[str, float] = {}
    month_end: dict[str, float] = {}
    wins = 0
    gp = 0.0
    gl = 0.0
    loss_streak = 0
    max_loss_streak = 0
    for tr in trades:
        m = tr.exit_time_utc[:7]
        month_start.setdefault(m, eq)
        eq += tr.net_pnl
        month_end[m] = eq
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100.0 if peak > 0 else 0.0)
        if tr.net_pnl >= 0:
            wins += 1
            gp += tr.net_pnl
            loss_streak = 0
        else:
            gl += abs(tr.net_pnl)
            loss_streak += 1
            max_loss_streak = max(max_loss_streak, loss_streak)
    month_rets = {m: ((month_end[m] / month_start[m]) - 1.0) * 100.0 for m in month_start if month_start[m] > 0 and m in month_end}
    if month_rets:
        worst_m, worst_v = min(month_rets.items(), key=lambda kv: kv[1])
        best_m, best_v = max(month_rets.items(), key=lambda kv: kv[1])
    else:
        worst_m, worst_v, best_m, best_v = "", 0.0, "", 0.0

    result = RunResult(
        config=asdict(cfg),
        years=years,
        start_equity=cfg.initial_equity,
        end_equity=round(equity, 8),
        return_pct=round(((equity / cfg.initial_equity) - 1.0) * 100.0, 8) if cfg.initial_equity > 0 else 0.0,
        max_drawdown_pct=round(max_dd, 8),
        trade_count=len(trades),
        win_rate_pct=round(wins / len(trades) * 100.0, 8) if trades else 0.0,
        profit_factor=round(gp / gl, 8) if gl > 0 else (999.0 if gp > 0 else 0.0),
        avg_net_pnl=round(_mean([t.net_pnl for t in trades]), 8) if trades else 0.0,
        max_loss_streak=max_loss_streak,
        worst_month=worst_m,
        worst_month_pct=round(worst_v, 8),
        best_month=best_m,
        best_month_pct=round(best_v, 8),
    )
    return result, trades, curve


def _fmt_cell(r: RunResult) -> str:
    return f"{r.end_equity:,.0f} / {r.max_drawdown_pct:.1f}% / PF {r.profit_factor:.2f}"


def _write_trades(path: Path, trades: list[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        if not trades:
            f.write("")
            return
        w = csv.DictWriter(f, fieldnames=list(asdict(trades[0]).keys()))
        w.writeheader()
        for t in trades:
            w.writerow(asdict(t))


def _write_curve(path: Path, curve: list[CurvePoint]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        if not curve:
            f.write("")
            return
        w = csv.DictWriter(f, fieldnames=list(asdict(curve[0]).keys()))
        w.writeheader()
        for p in curve:
            w.writerow(asdict(p))


def _out_dir() -> Path:
    p = ROOT / "backtests" / "v40_quant_multi_alpha"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_candles(symbol: str, years: int, refresh: bool = False) -> list[Candle]:
    data_dir = _data_dir()
    print(f"[data] {symbol} 1H {years}y: loading Bitget history candles...")
    candles = fetch_bitget_history_candles(symbol=symbol, interval="1H", years=years, data_dir=data_dir, refresh=refresh)
    print(f"[data] {symbol}: bars={len(candles)} from {_iso_ms(candles[0].ts)} to {_iso_ms(candles[-1].ts)}")
    return candles


def _base_cfg(args: argparse.Namespace) -> QuantConfig:
    return QuantConfig(
        symbol=args.symbol.upper(),
        initial_equity=args.initial_equity,
        capital_ratio=args.capital_ratio,
        leverage=args.leverage,
        max_order_notional_usdt=args.max_order_notional,
        taker_fee_rate=args.taker_fee_rate,
        slippage_bps=args.slippage_bps,
        profile=args.profile,
        trend_gate=args.trend_gate,
        entry_threshold=args.entry_threshold,
        exit_threshold=args.exit_threshold,
        max_hold_bars=args.max_hold_bars,
        atr_period=args.atr_period,
        atr_stop_mult=args.atr_stop_mult,
        atr_take_profit_mult=args.atr_take_profit_mult,
        min_vol_mult=args.min_vol_mult,
        max_vol_mult=args.max_vol_mult,
    )


def cmd_detail(args: argparse.Namespace) -> None:
    candles = _load_candles(args.symbol, args.years, args.refresh)
    cfg = _base_cfg(args)
    result, trades, curve = run_backtest(candles, cfg, years=args.years, record=True)
    out = _out_dir()
    summary_path = out / "quant_multi_alpha_summary_latest.txt"
    trades_path = out / "quant_multi_alpha_trades_latest.csv"
    curve_path = out / "quant_multi_alpha_equity_latest.csv"
    _write_trades(trades_path, trades)
    _write_curve(curve_path, curve)

    lines = []
    lines.append("Index Sniper Pro v4.0 BTC OHLCV Multi-Alpha Quant")
    lines.append("=================================================")
    for k, v in asdict(result).items():
        if k != "config":
            lines.append(f"{k}: {v}")
    lines.append("")
    lines.append("Config:")
    for k, v in asdict(cfg).items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("Rules:")
    lines.append("- data: 1H OHLCV only; no funding/OI in this backtest")
    lines.append("- signal: weighted multi-horizon momentum + EMA regime + volume confirmation + liquidation-like reversal proxy")
    lines.append("- entry: next 1H open when score passes threshold")
    lines.append("- exit: ATR SL/TP, score decay/flip, or max-hold")
    lines.append(f"CSV trades: {trades_path}")
    lines.append(f"CSV equity: {curve_path}")
    text = "\n".join(lines) + "\n"
    summary_path.write_text(text, encoding="utf-8")
    print(text)
    print("saved:", summary_path)


def _grid_configs(args: argparse.Namespace) -> list[QuantConfig]:
    profiles = _split_csv(args.profiles)
    gates = _split_csv(args.trend_gates)
    entry_thresholds = _parse_floats(args.entry_thresholds)
    exit_thresholds = _parse_floats(args.exit_thresholds)
    holds = _parse_ints(args.max_hold_values)
    stops = _parse_floats(args.atr_stop_values)
    tps = _parse_floats(args.atr_tp_values)
    configs: list[QuantConfig] = []
    for profile in profiles:
        for gate in gates:
            for ent in entry_thresholds:
                for ex in exit_thresholds:
                    if ex >= ent:
                        continue
                    for hold in holds:
                        for sl in stops:
                            for tp in tps:
                                configs.append(
                                    QuantConfig(
                                        symbol=args.symbol.upper(),
                                        initial_equity=args.initial_equity,
                                        capital_ratio=args.capital_ratio,
                                        leverage=args.leverage,
                                        max_order_notional_usdt=args.max_order_notional,
                                        taker_fee_rate=args.taker_fee_rate,
                                        slippage_bps=args.slippage_bps,
                                        profile=profile,
                                        trend_gate=gate,
                                        entry_threshold=ent,
                                        exit_threshold=ex,
                                        max_hold_bars=hold,
                                        atr_period=args.atr_period,
                                        atr_stop_mult=sl,
                                        atr_take_profit_mult=tp,
                                        min_vol_mult=args.min_vol_mult,
                                        max_vol_mult=args.max_vol_mult,
                                    )
                                )
    return configs


def cmd_search(args: argparse.Namespace) -> None:
    candles = _load_candles(args.symbol, args.years, args.refresh)
    configs = _grid_configs(args)
    out = _out_dir()
    rows = []
    print(f"[search] configs={len(configs)} years={args.years} capital={args.capital_ratio} leverage={args.leverage}")
    for n, cfg in enumerate(configs, 1):
        result, _, _ = run_backtest(candles, cfg, years=args.years, record=False)
        positive = result.end_equity > result.start_equity and result.profit_factor > args.min_profit_factor and result.trade_count >= args.min_trades
        if args.positive_only and not positive:
            continue
        d = asdict(result)
        d.update({
            "profile": cfg.profile,
            "trend_gate": cfg.trend_gate,
            "entry_threshold": cfg.entry_threshold,
            "exit_threshold": cfg.exit_threshold,
            "max_hold_bars": cfg.max_hold_bars,
            "atr_stop_mult": cfg.atr_stop_mult,
            "atr_take_profit_mult": cfg.atr_take_profit_mult,
            "capital_ratio": cfg.capital_ratio,
            "leverage": cfg.leverage,
            "positive_filter": positive,
        })
        rows.append(d)
        if n % 100 == 0:
            print(f"[search] done {n}/{len(configs)} kept={len(rows)}")
    rows.sort(key=lambda r: (r["profit_factor"], -r["max_drawdown_pct"], r["end_equity"]), reverse=True)
    rows = rows[: args.top]

    csv_path = out / "quant_multi_alpha_search_latest.csv"
    txt_path = out / "quant_multi_alpha_search_latest.txt"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows:
                w.writerow(r)
        else:
            f.write("")

    lines = []
    lines.append("Index Sniper Pro v4.0 BTC OHLCV Multi-Alpha Search")
    lines.append("===================================================")
    lines.append("Filter: 5y end_equity > start_equity, PF > min PF, trade_count >= min trades" if args.positive_only else "Filter: none")
    lines.append(f"symbol={args.symbol.upper()} years={args.years} leverage={args.leverage} capital_ratio={args.capital_ratio}")
    lines.append("Data: 1H OHLCV only. Funding/OI are not included in this historical test.")
    lines.append("")
    if not rows:
        lines.append("NO POSITIVE CANDIDATES FOUND")
    else:
        header = f"{'rank':>4}  {'end':>12}  {'MDD':>7}  {'PF':>5}  {'trades':>6}  {'win%':>6}  profile/gate/entry/exit/hold/sl/tp"
        lines.append(header)
        lines.append("-" * len(header))
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i:>4}  {r['end_equity']:>12,.0f}  {r['max_drawdown_pct']:>6.1f}%  {r['profit_factor']:>5.2f}  "
                f"{r['trade_count']:>6}  {r['win_rate_pct']:>5.1f}%  "
                f"{r['profile']}/{r['trend_gate']}/E{r['entry_threshold']}/X{r['exit_threshold']}/H{r['max_hold_bars']}/SL{r['atr_stop_mult']}/TP{r['atr_take_profit_mult']}"
            )
    lines.append("")
    lines.append(f"CSV: {csv_path}")
    text = "\n".join(lines) + "\n"
    txt_path.write_text(text, encoding="utf-8")
    print(text)
    print("saved:", txt_path)


def cmd_robust(args: argparse.Namespace) -> None:
    candles = _load_candles(args.symbol, 5, args.refresh)
    cfg = _base_cfg(args)
    years_list = _parse_ints(args.years_list)
    results = []
    for y in years_list:
        r, _, _ = run_backtest(candles, cfg, years=y, record=False)
        results.append(r)
    out = _out_dir()
    txt_path = out / "quant_multi_alpha_robust_latest.txt"
    lines = []
    lines.append("Index Sniper Pro v4.0 BTC OHLCV Multi-Alpha Robust Check")
    lines.append("=========================================================")
    lines.append("Format: final equity / MDD / PF")
    lines.append(f"profile={cfg.profile} gate={cfg.trend_gate} E={cfg.entry_threshold} X={cfg.exit_threshold} H={cfg.max_hold_bars} SL={cfg.atr_stop_mult} TP={cfg.atr_take_profit_mult} capital={cfg.capital_ratio} lev={cfg.leverage}")
    lines.append("")
    all_positive = True
    for r in results:
        ok = r.end_equity > r.start_equity and r.profit_factor > 1.0
        all_positive = all_positive and ok
        lines.append(f"{r.years}y: {_fmt_cell(r)} | trades {r.trade_count} | {'OK' if ok else 'FAIL'}")
    lines.append("")
    lines.append(f"ALL_WINDOWS_POSITIVE: {all_positive}")
    text = "\n".join(lines) + "\n"
    txt_path.write_text(text, encoding="utf-8")
    print(text)
    print("saved:", txt_path)


def cmd_capital_sweep(args: argparse.Namespace) -> None:
    candles = _load_candles(args.symbol, 5, args.refresh)
    ratios = _parse_floats(args.capital_ratios)
    years_list = _parse_ints(args.years_list)
    out = _out_dir()
    txt_path = out / "quant_multi_alpha_capital_sweep_latest.txt"
    csv_path = out / "quant_multi_alpha_capital_sweep_latest.csv"
    rows = []
    for y in years_list:
        for cr in ratios:
            cfg = _base_cfg(args)
            cfg = QuantConfig(**{**asdict(cfg), "capital_ratio": cr})
            r, _, _ = run_backtest(candles, cfg, years=y, record=False)
            row = asdict(r)
            row["capital_ratio"] = cr
            rows.append(row)

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for row in rows:
                w.writerow(row)
    by = {(r["years"], r["capital_ratio"]): r for r in rows}
    lines = []
    lines.append("Index Sniper Pro v4.0 BTC OHLCV Multi-Alpha Capital Sweep")
    lines.append("==========================================================")
    lines.append("Format: final equity / MDD / PF")
    lines.append(f"profile={args.profile} gate={args.trend_gate} E={args.entry_threshold} X={args.exit_threshold} H={args.max_hold_bars} SL={args.atr_stop_mult} TP={args.atr_take_profit_mult} lev={args.leverage}")
    lines.append("")
    header = "years".ljust(8) + "  " + "  ".join(str(x).ljust(22) for x in ratios)
    lines.append(header)
    lines.append("-" * len(header))
    for y in years_list:
        parts = [str(y).ljust(8)]
        for cr in ratios:
            r = by.get((y, cr))
            if not r:
                parts.append("".ljust(22))
            else:
                parts.append(f"{r['end_equity']:,.0f} / {r['max_drawdown_pct']:.1f}% / PF {r['profit_factor']:.2f}".ljust(22))
        lines.append("  ".join(parts))
    lines.append("")
    lines.append(f"CSV: {csv_path}")
    text = "\n".join(lines) + "\n"
    txt_path.write_text(text, encoding="utf-8")
    print(text)
    print("saved:", txt_path)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--symbol", default=os.getenv("BT_V40_SYMBOL", "BTCUSDT"))
    p.add_argument("--years", type=int, default=int(os.getenv("BT_V40_YEARS", "5")))
    p.add_argument("--initial-equity", type=float, default=float(os.getenv("BT_INITIAL_EQUITY", "1374")))
    p.add_argument("--capital-ratio", type=float, default=float(os.getenv("BT_V40_CAPITAL_RATIO", os.getenv("BT_CAPITAL_RATIO", "0.30"))))
    p.add_argument("--leverage", type=float, default=float(os.getenv("BT_V40_LEVERAGE", os.getenv("BT_LEVERAGE", "3"))))
    p.add_argument("--max-order-notional", type=float, default=float(os.getenv("BT_V40_MAX_ORDER_NOTIONAL_USDT", os.getenv("BT_OPT_MAX_ORDER_NOTIONAL_USDT", "999999"))))
    p.add_argument("--taker-fee-rate", type=float, default=float(os.getenv("BT_TAKER_FEE_RATE", "0.0006")))
    p.add_argument("--slippage-bps", type=float, default=float(os.getenv("BT_SLIPPAGE_BPS", "2.0")))
    p.add_argument("--profile", default=os.getenv("BT_V40_PROFILE", "trend_volume"))
    p.add_argument("--trend-gate", default=os.getenv("BT_V40_TREND_GATE", "ema80_240"))
    p.add_argument("--entry-threshold", type=float, default=float(os.getenv("BT_V40_ENTRY_THRESHOLD", "55")))
    p.add_argument("--exit-threshold", type=float, default=float(os.getenv("BT_V40_EXIT_THRESHOLD", "15")))
    p.add_argument("--max-hold-bars", type=int, default=int(os.getenv("BT_V40_MAX_HOLD_BARS", "24")))
    p.add_argument("--atr-period", type=int, default=int(os.getenv("BT_V40_ATR_PERIOD", "24")))
    p.add_argument("--atr-stop-mult", type=float, default=float(os.getenv("BT_V40_ATR_STOP_MULT", "1.5")))
    p.add_argument("--atr-take-profit-mult", type=float, default=float(os.getenv("BT_V40_ATR_TP_MULT", "3.0")))
    p.add_argument("--min-vol-mult", type=float, default=float(os.getenv("BT_V40_MIN_VOL_MULT", "0.0")))
    p.add_argument("--max-vol-mult", type=float, default=float(os.getenv("BT_V40_MAX_VOL_MULT", "2.5")))
    p.add_argument("--refresh", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index Sniper Pro v4.0 BTC OHLCV multi-alpha quant backtest")
    sub = parser.add_subparsers(dest="cmd", required=True)

    detail = sub.add_parser("detail")
    _add_common(detail)
    detail.set_defaults(func=cmd_detail)

    robust = sub.add_parser("robust")
    _add_common(robust)
    robust.add_argument("--years-list", default=os.getenv("BT_V40_YEARS_LIST", "1,2,3,5"))
    robust.set_defaults(func=cmd_robust)

    cap = sub.add_parser("capital-sweep")
    _add_common(cap)
    cap.add_argument("--years-list", default=os.getenv("BT_V40_YEARS_LIST", "1,2,3,5"))
    cap.add_argument("--capital-ratios", default=os.getenv("BT_V40_CAPITAL_RATIOS", "0.30,0.70,1.00"))
    cap.set_defaults(func=cmd_capital_sweep)

    search = sub.add_parser("search")
    _add_common(search)
    search.add_argument("--profiles", default=os.getenv("BT_V40_PROFILES", "trend,trend_volume,hybrid,hybrid_defensive,reversal"))
    search.add_argument("--trend-gates", default=os.getenv("BT_V40_TREND_GATES", "none,ema24_96,ema80_240,ema80_240_strict"))
    search.add_argument("--entry-thresholds", default=os.getenv("BT_V40_ENTRY_THRESHOLDS", "45,55,65,75"))
    search.add_argument("--exit-thresholds", default=os.getenv("BT_V40_EXIT_THRESHOLDS", "5,15,25"))
    search.add_argument("--max-hold-values", default=os.getenv("BT_V40_MAX_HOLD_VALUES", "12,24,48"))
    search.add_argument("--atr-stop-values", default=os.getenv("BT_V40_ATR_STOP_VALUES", "1.0,1.5,2.0"))
    search.add_argument("--atr-tp-values", default=os.getenv("BT_V40_ATR_TP_VALUES", "2.0,3.0,4.0"))
    search.add_argument("--positive-only", action=argparse.BooleanOptionalAction, default=os.getenv("BT_V40_POSITIVE_ONLY", "true").lower() not in {"0", "false", "no"})
    search.add_argument("--min-profit-factor", type=float, default=float(os.getenv("BT_V40_MIN_PF", "1.02")))
    search.add_argument("--min-trades", type=int, default=int(os.getenv("BT_V40_MIN_TRADES", "40")))
    search.add_argument("--top", type=int, default=int(os.getenv("BT_V40_TOP", "30")))
    search.set_defaults(func=cmd_search)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
