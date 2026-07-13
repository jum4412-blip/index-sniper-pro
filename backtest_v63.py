from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from index_sniper.strategy.indicators import Candle, atr, ema

BASE_URL = "https://api.bitget.com"
CATEGORY = "USDT-FUTURES"
INTERVAL_MS = {
    "5m": 300_000,
    "15m": 900_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
}


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pct_change(new: float, old: float) -> float:
    return ((new / old) - 1.0) * 100.0 if old else 0.0


def utc_ms(value: dt.datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return int(value.timestamp() * 1000)


def parse_utc_date(text: str) -> dt.datetime:
    raw = text.strip()
    if len(raw) == 10:
        raw += "T00:00:00+00:00"
    raw = raw.replace("Z", "+00:00")
    value = dt.datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def iso_ms(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"JSON object expected: {path}")
    return obj


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def parse_candle_rows(rows: Any) -> list[Candle]:
    out: list[Candle] = []
    if not isinstance(rows, list):
        return out
    for row in rows:
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
    return out


class PublicBitget:
    def __init__(self, timeout: int = 20, min_request_gap: float = 0.075):
        self.session = requests.Session()
        self.timeout = timeout
        self.min_request_gap = min_request_gap
        self._last_request = 0.0

    def get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        wait = self.min_request_gap - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                response = self.session.get(
                    BASE_URL + path,
                    params=params,
                    headers={"Content-Type": "application/json", "locale": "en-US"},
                    timeout=self.timeout,
                )
                self._last_request = time.monotonic()
                response.raise_for_status()
                data = response.json()
                if str(data.get("code")) not in {"00000", "0"}:
                    raise RuntimeError(f"Bitget error: {data}")
                return data
            except Exception as exc:
                last_error = exc
                time.sleep(min(8.0, 0.5 * (2**attempt)))
        raise RuntimeError(f"Bitget request failed: {path} {params}: {last_error}")

    def historical_candles(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        cache_path: Path,
        refresh: bool = False,
    ) -> list[Candle]:
        if cache_path.exists() and not refresh:
            rows = json.loads(cache_path.read_text(encoding="utf-8"))
            candles = parse_candle_rows(rows)
            if candles:
                return sorted({c.ts: c for c in candles}.values(), key=lambda x: x.ts)

        step = INTERVAL_MS[interval]
        dedup: dict[int, Candle] = {}
        cursor_end = ((end_ms - 1) // step) * step + step - 1
        hard_floor = (start_ms // step) * step
        calls = 0
        while cursor_end >= hard_floor:
            chunk_start = max(hard_floor - step, cursor_end - step * 99)
            data = self.get(
                "/api/v3/market/history-candles",
                {
                    "category": CATEGORY,
                    "symbol": symbol,
                    "interval": interval,
                    "type": "market",
                    "startTime": str(chunk_start),
                    "endTime": str(cursor_end),
                    "limit": "100",
                },
            )
            batch = parse_candle_rows(data.get("data"))
            calls += 1
            for candle in batch:
                if hard_floor <= candle.ts < end_ms:
                    dedup[candle.ts] = candle
            if batch:
                earliest = min(c.ts for c in batch)
                next_end = earliest - 1
                if next_end >= cursor_end:
                    next_end = chunk_start - 1
            else:
                next_end = chunk_start - 1
            cursor_end = next_end
            if calls > 20_000:
                raise RuntimeError(f"pagination runaway: {symbol} {interval}")

        candles = sorted(dedup.values(), key=lambda x: x.ts)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [[c.ts, c.open, c.high, c.low, c.close, c.volume, c.turnover] for c in candles]
        atomic_write(cache_path, json.dumps(payload, separators=(",", ":")))
        return candles

    def funding_history(self, symbol: str, cache_path: Path, refresh: bool = False) -> list[tuple[int, float]]:
        if cache_path.exists() and not refresh:
            rows = json.loads(cache_path.read_text(encoding="utf-8"))
            return sorted((int(x[0]), float(x[1])) for x in rows)
        out: dict[int, float] = {}
        for cursor in range(1, 101):
            data = self.get(
                "/api/v3/market/history-fund-rate",
                {"category": CATEGORY, "symbol": symbol, "limit": "100", "cursor": str(cursor)},
            )
            payload = data.get("data")
            rows = payload.get("resultList") if isinstance(payload, dict) else []
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                ts = int(safe_float(row.get("fundingRateTimestamp")))
                rate = safe_float(row.get("fundingRate"), float("nan"))
                if ts > 0 and math.isfinite(rate):
                    out[ts] = rate
            if len(rows) < 100:
                break
        result = sorted(out.items())
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(cache_path, json.dumps(result, separators=(",", ":")))
        return result


@dataclass
class HistoricalSignal:
    symbol: str
    ts: int
    price: float
    atr1h: float
    atr1h_pct: float
    regime_long: float
    regime_short: float
    trigger_long: float
    trigger_short: float
    edge: float
    side: str
    mode: str
    entry_ready: bool
    volume_z5: float
    momentum_atr: float
    ret_1h_pct: float
    breakout_long: bool
    breakout_short: bool
    anti_chase_distance_atr: float
    opportunity_score: float
    funding_rate: float | None
    turnover24h: float
    blockers: list[str]


@dataclass
class Position:
    symbol: str
    side: str
    mode: str
    signal_ts: int
    entry_ts: int
    entry_price: float
    qty: float
    notional: float
    margin: float
    stop_price: float
    take_profit_price: float
    stop_distance: float
    atr1h: float
    risk_usdt: float
    entry_fee: float
    best_price: float
    mfe_r: float = 0.0
    mae_r: float = 0.0
    invalidation_count: int = 0
    trailing_active: bool = False


@dataclass
class PendingEntry:
    signal: HistoricalSignal


@dataclass
class Trade:
    symbol: str
    side: str
    mode: str
    signal_time: str
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    qty: float
    notional: float
    initial_margin: float
    gross_pnl: float
    fees: float
    net_pnl: float
    r_multiple: float
    mfe_r: float
    mae_r: float
    exit_reason: str
    bars_held: int


def volume_z(candles: list[Candle], window: int = 30) -> float:
    if len(candles) < window + 1:
        return 0.0
    history = [max(0.0, c.volume) for c in candles[-(window + 1) : -1]]
    current = max(0.0, candles[-1].volume)
    mean = statistics.fmean(history)
    std = statistics.pstdev(history)
    return 0.0 if std <= 1e-12 else (current - mean) / std


def relation_score(condition: bool, points: float) -> float:
    return points if condition else 0.0


def nearest_prior_funding(history: list[tuple[int, float]], ts: int) -> float | None:
    if not history:
        return None
    times = [x[0] for x in history]
    idx = bisect.bisect_right(times, ts) - 1
    return history[idx][1] if idx >= 0 else None


def completed_slice(candles: list[Candle], end_times: list[int], close_time: int, limit: int) -> list[Candle]:
    idx = bisect.bisect_right(end_times, close_time)
    return candles[max(0, idx - limit) : idx]


def build_historical_signal(
    symbol: str,
    close_time: int,
    c5: list[Candle],
    c15: list[Candle],
    c1h: list[Candle],
    c4h: list[Candle],
    funding: float | None,
    cfg: dict[str, Any],
    oi_mode: str,
) -> HistoricalSignal | None:
    if len(c5) < 40 or len(c15) < 10 or len(c1h) < 55 or len(c4h) < 55:
        return None
    price = c5[-1].close
    atr1h = atr(c1h, 14)
    if atr1h is None or atr1h <= 0 or price <= 0:
        return None
    atr_pct = atr1h / price
    e20_1h = ema([x.close for x in c1h], 20)
    e50_1h = ema([x.close for x in c1h], 50)
    e20_4h = ema([x.close for x in c4h], 20)
    e50_4h = ema([x.close for x in c4h], 50)
    if None in {e20_1h, e50_1h, e20_4h, e50_4h}:
        return None
    e20_1h = float(e20_1h)
    e50_1h = float(e50_1h)
    e20_4h = float(e20_4h)
    e50_4h = float(e50_4h)

    ret1h = pct_change(price, c1h[-2].close)
    momentum_atr = (price - c15[-4].close) / atr1h
    vz = volume_z(c5, 30)
    prior_high = max(x.high for x in c5[-21:-1])
    prior_low = min(x.low for x in c5[-21:-1])
    breakout_long = price > prior_high and c5[-1].close >= prior_high * 0.9995
    breakout_short = price < prior_low and c5[-1].close <= prior_low * 1.0005
    anti_distance = abs(price - e20_1h) / atr1h
    last5 = c5[-1]
    last_range = max(last5.high - last5.low, price * 1e-9)
    response_efficiency = abs(last5.close - last5.open) / last_range
    close_location = clamp((last5.close - last5.low) / last_range, 0.0, 1.0)

    regime_long = (
        relation_score(e20_1h > e50_1h, 22)
        + relation_score(price > e20_1h, 16)
        + relation_score(e20_4h > e50_4h, 27)
        + relation_score(c4h[-1].close > e20_4h, 15)
        + relation_score(ret1h > 0, 10)
        + relation_score(momentum_atr > 0, 10)
    )
    regime_short = (
        relation_score(e20_1h < e50_1h, 22)
        + relation_score(price < e20_1h, 16)
        + relation_score(e20_4h < e50_4h, 27)
        + relation_score(c4h[-1].close < e20_4h, 15)
        + relation_score(ret1h < 0, 10)
        + relation_score(momentum_atr < 0, 10)
    )

    def trigger(side: str) -> float:
        sign = 1.0 if side == "LONG" else -1.0
        mom = sign * momentum_atr
        r1 = sign * ret1h
        breakout = breakout_long if side == "LONG" else breakout_short
        regime = regime_long if side == "LONG" else regime_short
        oi_component = 0.0
        if oi_mode == "proxy":
            # Public historical OI series is not available through this UTA endpoint.
            # This intentionally weak proxy only adds points when price and volume agree.
            oi_component = 6.0 * clamp((mom - 0.25) / 0.75, 0.0, 1.0) * clamp(vz / 2.0, 0.0, 1.0)
        score = (
            20.0 * clamp(mom / 0.80, 0.0, 1.0)
            + (20.0 if breakout else 0.0)
            + 15.0 * clamp(vz / 2.0, 0.0, 1.0)
            + oi_component
            + 15.0 * clamp(regime / 100.0, 0.0, 1.0)
            + 10.0 * clamp(r1 / max(atr_pct * 100.0 * 0.50, 0.05), 0.0, 1.0)
        )
        if funding is not None:
            if side == "LONG" and funding > 0.0008:
                score -= 6.0
            if side == "SHORT" and funding < -0.0008:
                score -= 6.0
        return round(max(0.0, score), 3)

    tl = trigger("LONG")
    ts = trigger("SHORT")
    side = "LONG" if tl >= ts else "SHORT"
    winning = tl if side == "LONG" else ts
    losing = ts if side == "LONG" else tl
    edge = winning - losing
    side_regime = regime_long if side == "LONG" else regime_short
    side_breakout = breakout_long if side == "LONG" else breakout_short
    side_momentum = momentum_atr if side == "LONG" else -momentum_atr
    exceptional_tape = vz >= 2.0 and side_momentum >= 0.75
    proxy_oi_confirm = oi_mode == "proxy" and vz >= 1.2 and side_momentum >= 0.50
    upper_wick = max(0.0, last5.high - max(last5.open, last5.close)) / last_range
    lower_wick = max(0.0, min(last5.open, last5.close) - last5.low) / last_range
    rejection_wick = upper_wick if side == "LONG" else lower_wick
    close_location_ok = close_location >= 0.62 if side == "LONG" else close_location <= 0.38
    high_volume_low_response = vz >= 1.5 and response_efficiency < 0.18

    trend_ok = (
        side_regime >= safe_float(cfg.get("trend_regime_threshold"), 55.0)
        and winning >= safe_float(cfg.get("trend_entry_threshold"), 44.0)
        and edge >= safe_float(cfg.get("min_edge"), 14.0)
        and anti_distance <= safe_float(cfg.get("anti_chase_atr"), 1.25)
        and not high_volume_low_response
    )
    impulse_ok = (
        winning >= safe_float(cfg.get("impulse_entry_threshold"), 30.0)
        and edge >= safe_float(cfg.get("impulse_min_edge"), 16.0)
        and side_breakout
        and side_momentum >= safe_float(cfg.get("impulse_move_atr"), 0.5)
        and vz >= safe_float(cfg.get("impulse_volume_z"), 1.2)
        and (proxy_oi_confirm or exceptional_tape)
        and anti_distance <= safe_float(cfg.get("impulse_anti_chase_atr"), 1.55)
        and close_location_ok
        and rejection_wick <= 0.45
        and not high_volume_low_response
        and (regime_short if side == "LONG" else regime_long) < 70
    )
    blockers: list[str] = []
    mode = "WAIT"
    entry_ready = False
    if trend_ok:
        mode = "TREND"
        entry_ready = True
    elif impulse_ok:
        mode = "IMPULSE"
        entry_ready = True
    else:
        if side_regime < safe_float(cfg.get("trend_regime_threshold"), 55.0) and not side_breakout:
            blockers.append("no_regime_or_breakout")
        if winning < min(safe_float(cfg.get("trend_entry_threshold"), 44.0), safe_float(cfg.get("impulse_entry_threshold"), 30.0)):
            blockers.append("trigger_low")
        if edge < min(safe_float(cfg.get("min_edge"), 14.0), safe_float(cfg.get("impulse_min_edge"), 16.0)):
            blockers.append("edge_low")
        if high_volume_low_response:
            blockers.append("high_volume_low_price_response")

    turnover = sum(max(0.0, x.turnover or x.close * x.volume) for x in c1h[-24:])
    supply = safe_float((cfg.get("symbols") or {}).get(symbol, {}).get("supply_proxy"), 1.0)
    market_cap = max(price * supply, 1.0)
    turnover_cap = turnover / market_cap
    opportunity = winning + 15.0 * max(0.0, side_momentum) + 8.0 * max(0.0, vz) + 20.0 * math.log1p(max(0.0, turnover_cap) * 10.0)

    return HistoricalSignal(
        symbol=symbol,
        ts=close_time,
        price=price,
        atr1h=atr1h,
        atr1h_pct=atr_pct * 100.0,
        regime_long=regime_long,
        regime_short=regime_short,
        trigger_long=tl,
        trigger_short=ts,
        edge=edge,
        side=side,
        mode=mode,
        entry_ready=entry_ready,
        volume_z5=vz,
        momentum_atr=momentum_atr,
        ret_1h_pct=ret1h,
        breakout_long=breakout_long,
        breakout_short=breakout_short,
        anti_chase_distance_atr=anti_distance,
        opportunity_score=opportunity,
        funding_rate=funding,
        turnover24h=turnover,
        blockers=sorted(set(blockers)),
    )


class BacktestEngine:
    def __init__(
        self,
        cfg: dict[str, Any],
        data: dict[str, dict[str, list[Candle]]],
        funding: dict[str, list[tuple[int, float]]],
        start_ms: int,
        end_ms: int,
        initial_equity: float,
        fee_bps: float,
        slippage_bps: float,
        oi_mode: str,
        impulse_confirm_bars: int,
    ):
        self.cfg = cfg
        self.data = data
        self.funding = funding
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.initial_equity = initial_equity
        self.cash = initial_equity
        self.fee_rate = fee_bps / 10_000.0
        self.slip_rate = slippage_bps / 10_000.0
        self.oi_mode = oi_mode
        self.impulse_confirm_bars = max(1, impulse_confirm_bars)
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, PendingEntry] = {}
        self.trades: list[Trade] = []
        self.confirmations: dict[str, tuple[str, int]] = {}
        self.entries_by_day_symbol: dict[tuple[str, str], int] = {}
        self.cooldown_until = 0
        self.consecutive_losses = 0
        self.high_equity = initial_equity
        self.day_start: dict[str, float] = {}
        self.week_start: dict[str, float] = {}
        self.equity_curve: list[tuple[int, float]] = []
        self.signal_counts = {"TREND": 0, "IMPULSE": 0, "WAIT": 0}
        self.entry_block_counts: dict[str, int] = {}
        self.last_signals: dict[str, HistoricalSignal] = {}
        self.bar_index: dict[str, dict[int, int]] = {
            s: {c.ts: i for i, c in enumerate(frames["5m"])} for s, frames in data.items()
        }
        self.end_times: dict[str, dict[str, list[int]]] = {}
        for symbol, frames in data.items():
            self.end_times[symbol] = {
                interval: [c.ts + INTERVAL_MS[interval] for c in candles]
                for interval, candles in frames.items()
            }

    def current_price(self, symbol: str, close_time: int) -> float | None:
        candles = self.data[symbol]["5m"]
        starts = [c.ts for c in candles]
        idx = bisect.bisect_left(starts, close_time) - 1
        return candles[idx].close if idx >= 0 else None

    def mtm_equity(self, close_time: int) -> float:
        equity = self.cash
        for symbol, pos in self.positions.items():
            price = self.current_price(symbol, close_time) or pos.entry_price
            direction = 1.0 if pos.side == "LONG" else -1.0
            equity += direction * (price - pos.entry_price) * pos.qty
        return equity

    def mark_guard_baselines(self, close_time: int, equity: float) -> None:
        now = dt.datetime.fromtimestamp(close_time / 1000, tz=dt.timezone.utc)
        day = now.strftime("%Y-%m-%d")
        iso = now.isocalendar()
        week = f"{iso.year}-W{iso.week:02d}"
        self.day_start.setdefault(day, equity)
        self.week_start.setdefault(week, equity)
        self.high_equity = max(self.high_equity, equity)

    def guards_ok(self, close_time: int, equity: float) -> tuple[bool, str]:
        self.mark_guard_baselines(close_time, equity)
        now = dt.datetime.fromtimestamp(close_time / 1000, tz=dt.timezone.utc)
        day = now.strftime("%Y-%m-%d")
        iso = now.isocalendar()
        week = f"{iso.year}-W{iso.week:02d}"
        daily_loss = 100.0 * (equity / self.day_start[day] - 1.0)
        weekly_loss = 100.0 * (equity / self.week_start[week] - 1.0)
        drawdown = 100.0 * (equity / self.high_equity - 1.0)
        if daily_loss <= -safe_float(self.cfg.get("daily_loss_block_pct"), 2.0):
            return False, "daily_loss_block"
        if weekly_loss <= -safe_float(self.cfg.get("weekly_loss_block_pct"), 5.0):
            return False, "weekly_loss_block"
        if drawdown <= -safe_float(self.cfg.get("max_drawdown_block_pct"), 6.0):
            return False, "drawdown_block"
        if close_time < self.cooldown_until:
            return False, "loss_cooldown"
        return True, ""

    def slices(self, symbol: str, close_time: int) -> dict[str, list[Candle]]:
        frames = self.data[symbol]
        return {
            "5m": completed_slice(frames["5m"], self.end_times[symbol]["5m"], close_time, 140),
            "15m": completed_slice(frames["15m"], self.end_times[symbol]["15m"], close_time, 140),
            "1H": completed_slice(frames["1H"], self.end_times[symbol]["1H"], close_time, 220),
            "4H": completed_slice(frames["4H"], self.end_times[symbol]["4H"], close_time, 120),
        }

    def signal(self, symbol: str, close_time: int) -> HistoricalSignal | None:
        frames = self.slices(symbol, close_time)
        funding = nearest_prior_funding(self.funding.get(symbol, []), close_time)
        return build_historical_signal(symbol, close_time, frames["5m"], frames["15m"], frames["1H"], frames["4H"], funding, self.cfg, self.oi_mode)

    def confirm(self, signal: HistoricalSignal) -> bool:
        if not signal.entry_ready:
            self.confirmations.pop(signal.symbol, None)
            return False
        key = f"{signal.side}:{signal.mode}"
        old_key, count = self.confirmations.get(signal.symbol, ("", 0))
        count = count + 1 if old_key == key else 1
        self.confirmations[signal.symbol] = (key, count)
        needed = self.impulse_confirm_bars if signal.mode == "IMPULSE" else 1
        return count >= needed

    def planned_size(self, signal: HistoricalSignal, equity: float) -> dict[str, float] | None:
        scfg = (self.cfg.get("symbols") or {}).get(signal.symbol) or {}
        stop_pct = clamp(
            (signal.atr1h / signal.price) * safe_float(scfg.get("stop_atr_mult"), 1.0),
            safe_float(scfg.get("min_stop_pct"), 0.005),
            safe_float(scfg.get("max_stop_pct"), 0.015),
        )
        open_count = len(self.positions)
        base_risk = safe_float(self.cfg.get("both_positions_risk_pct_each"), 0.4) if open_count >= 1 else safe_float(self.cfg.get("risk_per_trade_pct"), 0.6)
        mode_mult = 0.5 if signal.mode == "IMPULSE" else 1.0
        risk_usdt = equity * (base_risk / 100.0) * mode_mult
        notional = risk_usdt / stop_pct
        leverage = max(1.0, safe_float(self.cfg.get("leverage"), 5.0))
        per_symbol_cap = equity * safe_float(self.cfg.get("bucket_margin_ratio"), 0.5) * leverage
        total_notional_cap = equity * safe_float(self.cfg.get("max_total_notional_equity_ratio"), 2.0)
        total_margin_cap_notional = equity * (safe_float(self.cfg.get("max_total_initial_margin_pct"), 40.0) / 100.0) * leverage
        existing_notional = sum(x.notional for x in self.positions.values())
        room = max(0.0, min(total_notional_cap, total_margin_cap_notional) - existing_notional)
        notional = min(notional, per_symbol_cap, room)
        if notional <= 10.0:
            return None
        margin = notional / leverage
        qty = notional / signal.price
        tp_r = safe_float(self.cfg.get("tp_r_impulse"), 1.6) if signal.mode == "IMPULSE" else safe_float(self.cfg.get("tp_r_trend"), 2.0)
        return {"stop_pct": stop_pct, "risk_usdt": notional * stop_pct, "notional": notional, "margin": margin, "qty": qty, "tp_r": tp_r}

    def adverse_fill(self, price: float, side: str, entry: bool) -> float:
        if entry:
            return price * (1.0 + self.slip_rate) if side == "LONG" else price * (1.0 - self.slip_rate)
        return price * (1.0 - self.slip_rate) if side == "LONG" else price * (1.0 + self.slip_rate)

    def execute_pending(self, bar_start: int, bars: dict[str, Candle]) -> None:
        for symbol in list(self.pending):
            if symbol not in bars or symbol in self.positions:
                continue
            pending = self.pending.pop(symbol)
            signal = pending.signal
            equity = self.mtm_equity(bar_start)
            size = self.planned_size(signal, equity)
            if not size:
                self.entry_block_counts["size_cap"] = self.entry_block_counts.get("size_cap", 0) + 1
                continue
            fill = self.adverse_fill(bars[symbol].open, signal.side, True)
            stop_pct = size["stop_pct"]
            if signal.side == "LONG":
                stop = fill * (1.0 - stop_pct)
                take = fill * (1.0 + stop_pct * size["tp_r"])
            else:
                stop = fill * (1.0 + stop_pct)
                take = fill * (1.0 - stop_pct * size["tp_r"])
            qty = size["notional"] / fill
            entry_fee = size["notional"] * self.fee_rate
            self.cash -= entry_fee
            self.positions[symbol] = Position(
                symbol=symbol,
                side=signal.side,
                mode=signal.mode,
                signal_ts=signal.ts,
                entry_ts=bar_start,
                entry_price=fill,
                qty=qty,
                notional=size["notional"],
                margin=size["margin"],
                stop_price=stop,
                take_profit_price=take,
                stop_distance=abs(fill - stop),
                atr1h=signal.atr1h,
                risk_usdt=size["risk_usdt"],
                entry_fee=entry_fee,
                best_price=fill,
            )
            day = dt.datetime.fromtimestamp(bar_start / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")
            key = (day, symbol)
            self.entries_by_day_symbol[key] = self.entries_by_day_symbol.get(key, 0) + 1

    def close_position(self, symbol: str, raw_exit: float, exit_ts: int, reason: str, bars_held: int) -> None:
        pos = self.positions.pop(symbol)
        exit_price = self.adverse_fill(raw_exit, pos.side, False)
        direction = 1.0 if pos.side == "LONG" else -1.0
        gross = direction * (exit_price - pos.entry_price) * pos.qty
        exit_notional = abs(exit_price * pos.qty)
        exit_fee = exit_notional * self.fee_rate
        fees = pos.entry_fee + exit_fee
        net = gross - exit_fee
        self.cash += gross - exit_fee
        r_multiple = net / pos.risk_usdt if pos.risk_usdt > 0 else 0.0
        self.trades.append(
            Trade(
                symbol=symbol,
                side=pos.side,
                mode=pos.mode,
                signal_time=iso_ms(pos.signal_ts),
                entry_time=iso_ms(pos.entry_ts),
                exit_time=iso_ms(exit_ts),
                entry_price=pos.entry_price,
                exit_price=exit_price,
                qty=pos.qty,
                notional=pos.notional,
                initial_margin=pos.margin,
                gross_pnl=gross,
                fees=fees,
                net_pnl=net,
                r_multiple=r_multiple,
                mfe_r=pos.mfe_r,
                mae_r=pos.mae_r,
                exit_reason=reason,
                bars_held=bars_held,
            )
        )
        if net < 0:
            self.consecutive_losses += 1
            minutes = safe_float(self.cfg.get("cooldown_after_two_losses_minutes"), 240.0) if self.consecutive_losses >= 2 else safe_float(self.cfg.get("cooldown_after_loss_minutes"), 60.0)
            self.cooldown_until = max(self.cooldown_until, exit_ts + int(minutes * 60_000))
        else:
            self.consecutive_losses = 0

    def manage_intrabar(self, symbol: str, bar: Candle, close_time: int) -> None:
        pos = self.positions.get(symbol)
        if not pos:
            return
        bars_held = max(1, int((bar.ts - pos.entry_ts) / INTERVAL_MS["5m"]) + 1)
        # Gap logic first.
        if pos.side == "LONG":
            if bar.open <= pos.stop_price:
                self.close_position(symbol, bar.open, bar.ts, "stop_gap", bars_held)
                return
            if bar.open >= pos.take_profit_price:
                self.close_position(symbol, bar.open, bar.ts, "take_profit_gap", bars_held)
                return
        else:
            if bar.open >= pos.stop_price:
                self.close_position(symbol, bar.open, bar.ts, "stop_gap", bars_held)
                return
            if bar.open <= pos.take_profit_price:
                self.close_position(symbol, bar.open, bar.ts, "take_profit_gap", bars_held)
                return

        # A trail activated on a prior bar may trigger now.
        if pos.trailing_active:
            trail = pos.best_price - pos.atr1h * safe_float(self.cfg.get("trail_atr_mult"), 0.85) if pos.side == "LONG" else pos.best_price + pos.atr1h * safe_float(self.cfg.get("trail_atr_mult"), 0.85)
            if (pos.side == "LONG" and bar.low <= trail) or (pos.side == "SHORT" and bar.high >= trail):
                self.close_position(symbol, trail, close_time, "local_atr_trailing_exit", bars_held)
                return

        stop_hit = bar.low <= pos.stop_price if pos.side == "LONG" else bar.high >= pos.stop_price
        tp_hit = bar.high >= pos.take_profit_price if pos.side == "LONG" else bar.low <= pos.take_profit_price
        if stop_hit and tp_hit:
            self.close_position(symbol, pos.stop_price, close_time, "stop_and_tp_same_bar_stop_first", bars_held)
            return
        if stop_hit:
            self.close_position(symbol, pos.stop_price, close_time, "stop_loss", bars_held)
            return
        if tp_hit:
            self.close_position(symbol, pos.take_profit_price, close_time, "take_profit", bars_held)
            return

        if pos.side == "LONG":
            favorable = bar.high - pos.entry_price
            adverse = pos.entry_price - bar.low
            pos.best_price = max(pos.best_price, bar.high)
        else:
            favorable = pos.entry_price - bar.low
            adverse = bar.high - pos.entry_price
            pos.best_price = min(pos.best_price, bar.low)
        pos.mfe_r = max(pos.mfe_r, favorable / pos.stop_distance)
        pos.mae_r = max(pos.mae_r, adverse / pos.stop_distance)
        if pos.mfe_r >= safe_float(self.cfg.get("trail_activate_r"), 1.1):
            pos.trailing_active = True

    def manage_close_conditions(self, symbol: str, close_time: int, signal: HistoricalSignal | None) -> None:
        pos = self.positions.get(symbol)
        if not pos:
            return
        candles = self.data[symbol]["5m"]
        starts = [c.ts for c in candles]
        idx = bisect.bisect_left(starts, close_time) - 1
        if idx < 0:
            return
        price = candles[idx].close
        age_minutes = (close_time - pos.entry_ts) / 60_000.0
        reason: str | None = None
        if age_minutes >= safe_float(self.cfg.get("no_followthrough_minutes"), 25.0) and pos.mfe_r < safe_float(self.cfg.get("no_followthrough_mfe_r"), 0.25):
            if signal:
                side_trigger = signal.trigger_long if pos.side == "LONG" else signal.trigger_short
                if side_trigger < safe_float(self.cfg.get("impulse_entry_threshold"), 30.0):
                    reason = "no_followthrough"
        max_hours = safe_float(self.cfg.get("time_stop_hours_impulse"), 4.0) if pos.mode == "IMPULSE" else safe_float(self.cfg.get("time_stop_hours_trend"), 8.0)
        if reason is None and age_minutes >= max_hours * 60.0:
            reason = "time_stop"
        if reason is None and signal:
            opposite = signal.trigger_short if pos.side == "LONG" else signal.trigger_long
            current = signal.trigger_long if pos.side == "LONG" else signal.trigger_short
            invalid = opposite >= current + 10 and opposite >= safe_float(self.cfg.get("impulse_entry_threshold"), 30.0)
            pos.invalidation_count = pos.invalidation_count + 1 if invalid else 0
            if pos.invalidation_count >= 2:
                reason = "signal_invalidation"
        if reason:
            bars_held = max(1, int((close_time - pos.entry_ts) / INTERVAL_MS["5m"]))
            self.close_position(symbol, price, close_time, reason, bars_held)

    def schedule_entries(self, close_time: int, signals: list[HistoricalSignal]) -> None:
        equity = self.mtm_equity(close_time)
        ok, reason = self.guards_ok(close_time, equity)
        if not ok:
            self.entry_block_counts[reason] = self.entry_block_counts.get(reason, 0) + 1
            return
        if len(self.positions) + len(self.pending) >= int(self.cfg.get("max_open_positions", 2)):
            self.entry_block_counts["max_open_positions"] = self.entry_block_counts.get("max_open_positions", 0) + 1
            return
        candidates: list[HistoricalSignal] = []
        for signal in signals:
            if signal.symbol in self.positions or signal.symbol in self.pending:
                continue
            day = dt.datetime.fromtimestamp(close_time / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")
            if self.entries_by_day_symbol.get((day, signal.symbol), 0) >= int(self.cfg.get("max_entries_per_symbol_per_day", 3)):
                self.entry_block_counts["daily_symbol_entry_cap"] = self.entry_block_counts.get("daily_symbol_entry_cap", 0) + 1
                continue
            if self.confirm(signal):
                candidates.append(signal)
        candidates.sort(key=lambda s: s.opportunity_score, reverse=True)
        limit = max(1, int(self.cfg.get("max_new_positions_per_cycle", 1)))
        for signal in candidates[:limit]:
            self.pending[signal.symbol] = PendingEntry(signal=signal)

    def run(self) -> dict[str, Any]:
        timeline = sorted(
            {
                c.ts
                for symbol, frames in self.data.items()
                for c in frames["5m"]
                if self.start_ms <= c.ts < self.end_ms
            }
        )
        for bar_start in timeline:
            close_time = bar_start + INTERVAL_MS["5m"]
            bars: dict[str, Candle] = {}
            for symbol, frames in self.data.items():
                idx = self.bar_index[symbol].get(bar_start)
                if idx is not None:
                    bars[symbol] = frames["5m"][idx]
            self.execute_pending(bar_start, bars)
            for symbol, bar in bars.items():
                self.manage_intrabar(symbol, bar, close_time)

            signals: list[HistoricalSignal] = []
            for symbol in self.data:
                signal = self.signal(symbol, close_time)
                if signal:
                    self.last_signals[symbol] = signal
                    self.signal_counts[signal.mode] = self.signal_counts.get(signal.mode, 0) + 1
                    signals.append(signal)
                    self.manage_close_conditions(symbol, close_time, signal)
            self.schedule_entries(close_time, signals)
            equity = self.mtm_equity(close_time)
            self.mark_guard_baselines(close_time, equity)
            self.equity_curve.append((close_time, equity))

        # Cancel entries that would execute outside the test and close residual positions.
        self.pending.clear()
        final_ts = self.end_ms
        for symbol in list(self.positions):
            price = self.current_price(symbol, final_ts) or self.positions[symbol].entry_price
            bars_held = max(1, int((final_ts - self.positions[symbol].entry_ts) / INTERVAL_MS["5m"]))
            self.close_position(symbol, price, final_ts, "end_of_test", bars_held)
        final_equity = self.cash
        self.equity_curve.append((final_ts, final_equity))
        return self.summary()

    def summary(self) -> dict[str, Any]:
        net = [t.net_pnl for t in self.trades]
        wins = [x for x in net if x > 0]
        losses = [x for x in net if x < 0]
        gross_profit = sum(wins)
        gross_loss = -sum(losses)
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
        peak = -float("inf")
        max_dd = 0.0
        daily_last: dict[str, float] = {}
        for ts, equity in self.equity_curve:
            peak = max(peak, equity)
            if peak > 0:
                max_dd = min(max_dd, (equity / peak - 1.0) * 100.0)
            day = dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")
            daily_last[day] = equity
        daily_returns: list[float] = []
        prev = None
        for day in sorted(daily_last):
            value = daily_last[day]
            if prev and prev > 0:
                daily_returns.append(value / prev - 1.0)
            prev = value
        sharpe = 0.0
        if len(daily_returns) >= 2 and statistics.pstdev(daily_returns) > 1e-12:
            sharpe = statistics.fmean(daily_returns) / statistics.pstdev(daily_returns) * math.sqrt(365)

        def group(key: str) -> dict[str, Any]:
            out: dict[str, list[Trade]] = {}
            for trade in self.trades:
                out.setdefault(str(getattr(trade, key)), []).append(trade)
            return {
                name: {
                    "trades": len(items),
                    "win_rate_pct": 100.0 * sum(1 for x in items if x.net_pnl > 0) / len(items),
                    "net_pnl": sum(x.net_pnl for x in items),
                    "avg_r": statistics.fmean(x.r_multiple for x in items),
                    "profit_factor": (
                        sum(max(0.0, x.net_pnl) for x in items) / -sum(min(0.0, x.net_pnl) for x in items)
                        if sum(min(0.0, x.net_pnl) for x in items) < 0
                        else None
                    ),
                }
                for name, items in out.items()
            }

        return {
            "initial_equity": self.initial_equity,
            "final_equity": self.cash,
            "net_pnl": self.cash - self.initial_equity,
            "return_pct": (self.cash / self.initial_equity - 1.0) * 100.0,
            "max_drawdown_pct": max_dd,
            "trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": 100.0 * len(wins) / len(self.trades) if self.trades else 0.0,
            "profit_factor": profit_factor if math.isfinite(profit_factor) else None,
            "avg_r": statistics.fmean(t.r_multiple for t in self.trades) if self.trades else 0.0,
            "median_r": statistics.median(t.r_multiple for t in self.trades) if self.trades else 0.0,
            "total_fees": sum(t.fees for t in self.trades),
            "sharpe_daily_annualized": sharpe,
            "signal_counts": self.signal_counts,
            "entry_block_counts": self.entry_block_counts,
            "by_symbol": group("symbol"),
            "by_mode": group("mode"),
            "by_side": group("side"),
        }


def write_results(out_dir: Path, args: argparse.Namespace, summary: dict[str, Any], engine: BacktestEngine) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    trades_path = out_dir / "trades.csv"
    with trades_path.open("w", newline="", encoding="utf-8") as f:
        fields = list(asdict(engine.trades[0]).keys()) if engine.trades else list(Trade.__dataclass_fields__.keys())
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for trade in engine.trades:
            writer.writerow(asdict(trade))
    curve_path = out_dir / "equity_curve.csv"
    with curve_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["ts", "equity"])
        for ts, equity in engine.equity_curve:
            writer.writerow([iso_ms(ts), f"{equity:.8f}"])
    meta = {
        "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "arguments": vars(args),
        "limitations": [
            "Historical news/event overlay is unavailable and is neutral in this replay.",
            "Historical UTA open-interest series is unavailable; conservative mode uses no OI confirmation and proxy mode uses a weak price-volume proxy.",
            "Orders are entered at the next 5-minute open to avoid look-ahead.",
            "If stop and take-profit are touched in the same candle, stop is assumed first.",
            "Funding cashflows are not charged; funding history is used only for the crowding score filter.",
        ],
        "summary": summary,
    }
    atomic_write(out_dir / "summary.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    pf = summary.get("profit_factor")
    pf_text = "-" if pf is None else f"{pf:.3f}"
    lines = [
        "BTC/ETH Quant v6.3.2 Historical Replay",
        "=" * 48,
        f"Period: {args.start or ('last ' + str(args.days) + ' days')} -> {args.end or 'now'} UTC",
        f"OI mode: {args.oi_mode} / impulse confirmation: {args.impulse_confirm_bars} historical bar(s)",
        f"Costs: fee {args.fee_bps:.2f} bps/side + slippage {args.slippage_bps:.2f} bps/side",
        "",
        f"Initial equity: {summary['initial_equity']:.2f}",
        f"Final equity:   {summary['final_equity']:.2f}",
        f"Return:         {summary['return_pct']:+.3f}%",
        f"Max drawdown:   {summary['max_drawdown_pct']:.3f}%",
        f"Trades:         {summary['trades']} / win rate {summary['win_rate_pct']:.2f}%",
        f"Profit factor:  {pf_text}",
        f"Average R:      {summary['avg_r']:+.3f}",
        f"Total fees:     {summary['total_fees']:.4f}",
        f"Daily Sharpe*:  {summary['sharpe_daily_annualized']:.3f}",
        "",
        "By symbol:",
    ]
    for name, row in summary.get("by_symbol", {}).items():
        lines.append(f"- {name}: {row['trades']} trades / win {row['win_rate_pct']:.1f}% / PnL {row['net_pnl']:+.3f} / avgR {row['avg_r']:+.3f}")
    lines.append("By mode:")
    for name, row in summary.get("by_mode", {}).items():
        lines.append(f"- {name}: {row['trades']} trades / win {row['win_rate_pct']:.1f}% / PnL {row['net_pnl']:+.3f} / avgR {row['avg_r']:+.3f}")
    lines.extend(
        [
            "",
            "* Sharpe is based on daily marked-to-market returns and is only a rough diagnostic.",
            "* This replay does not validate live API execution, server-side TP/SL attachment, news filtering, or true historical OI.",
        ]
    )
    atomic_write(out_dir / "summary.txt", "\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nSaved: {out_dir}")


def synthetic_candles(start: int, count: int, step: int, base: float, drift: float) -> list[Candle]:
    out: list[Candle] = []
    price = base
    for i in range(count):
        op = price
        price = max(1.0, price * (1.0 + drift + math.sin(i / 11.0) * 0.0004))
        hi = max(op, price) * 1.001
        lo = min(op, price) * 0.999
        out.append(Candle(start + i * step, op, hi, lo, price, 100 + (i % 13) * 3, price * (100 + (i % 13) * 3)))
    return out


def self_test() -> None:
    now = 1_700_000_000_000
    cfg = {
        "symbols": {
            "BTCUSDT": {"supply_proxy": 19_900_000, "stop_atr_mult": 1.05, "min_stop_pct": 0.0055, "max_stop_pct": 0.01},
            "ETHUSDT": {"supply_proxy": 120_700_000, "stop_atr_mult": 1.10, "min_stop_pct": 0.007, "max_stop_pct": 0.014},
        },
        "leverage": 5,
        "risk_per_trade_pct": 0.6,
        "both_positions_risk_pct_each": 0.4,
        "max_total_initial_margin_pct": 40,
        "max_total_notional_equity_ratio": 2.0,
        "bucket_margin_ratio": 0.5,
        "max_open_positions": 2,
        "max_new_positions_per_cycle": 1,
        "max_entries_per_symbol_per_day": 3,
        "daily_loss_block_pct": 2.0,
        "weekly_loss_block_pct": 5.0,
        "max_drawdown_block_pct": 6.0,
        "cooldown_after_loss_minutes": 60,
        "cooldown_after_two_losses_minutes": 240,
        "trend_regime_threshold": 55,
        "trend_entry_threshold": 44,
        "impulse_entry_threshold": 30,
        "min_edge": 14,
        "impulse_min_edge": 16,
        "impulse_volume_z": 1.2,
        "impulse_move_atr": 0.5,
        "anti_chase_atr": 1.25,
        "impulse_anti_chase_atr": 1.55,
        "tp_r_trend": 2.0,
        "tp_r_impulse": 1.6,
        "no_followthrough_minutes": 25,
        "no_followthrough_mfe_r": 0.25,
        "time_stop_hours_trend": 8,
        "time_stop_hours_impulse": 4,
        "trail_activate_r": 1.1,
        "trail_atr_mult": 0.85,
    }
    data: dict[str, dict[str, list[Candle]]] = {}
    for symbol, base in (("BTCUSDT", 60_000.0), ("ETHUSDT", 3_000.0)):
        data[symbol] = {
            "5m": synthetic_candles(now, 1200, INTERVAL_MS["5m"], base, 0.00003),
            "15m": synthetic_candles(now, 400, INTERVAL_MS["15m"], base, 0.00009),
            "1H": synthetic_candles(now, 180, INTERVAL_MS["1H"], base, 0.00035),
            "4H": synthetic_candles(now, 80, INTERVAL_MS["4H"], base, 0.0012),
        }
    engine = BacktestEngine(cfg, data, {"BTCUSDT": [], "ETHUSDT": []}, now + 60 * INTERVAL_MS["5m"], now + 1100 * INTERVAL_MS["5m"], 1000.0, 6.0, 3.0, "conservative", 1)
    result = engine.run()
    assert result["final_equity"] > 0
    assert len(engine.equity_curve) > 100
    print(json.dumps({"self_test": "ok", "trades": result["trades"], "final_equity": result["final_equity"]}, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="No-lookahead BTC/ETH v6.3.2 historical replay")
    parser.add_argument("--config", default="config/v63_dual_live.json")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--start", default="")
    parser.add_argument("--end", default="")
    parser.add_argument("--initial-equity", type=float, default=1000.0)
    parser.add_argument("--fee-bps", type=float, default=6.0, help="Assumed taker fee per side")
    parser.add_argument("--slippage-bps", type=float, default=3.0, help="Adverse market-order slippage per side")
    parser.add_argument("--oi-mode", choices=("conservative", "proxy"), default="conservative")
    parser.add_argument("--impulse-confirm-bars", type=int, default=1)
    parser.add_argument("--cache-dir", default="data/v63_backtest/cache")
    parser.add_argument("--output-root", default="reports/v63_backtest")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.initial_equity <= 0 or args.days <= 0:
        raise SystemExit("initial equity and days must be positive")

    cfg_path = Path(args.config)
    cfg = load_json(cfg_path)
    end = parse_utc_date(args.end) if args.end else dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    start = parse_utc_date(args.start) if args.start else end - dt.timedelta(days=args.days)
    if end <= start:
        raise SystemExit("end must be after start")
    warmup_start = start - dt.timedelta(days=16)
    start_ms, end_ms, warmup_ms = utc_ms(start), utc_ms(end), utc_ms(warmup_start)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")
    scenario = f"{start:%Y%m%d}_{end:%Y%m%d}_{args.oi_mode}_c{args.impulse_confirm_bars}_f{args.fee_bps:g}_s{args.slippage_bps:g}_{stamp}"
    out_dir = Path(args.output_root) / scenario
    cache_dir = Path(args.cache_dir)
    client = PublicBitget()
    symbols = [s for s in (cfg.get("symbols") or {}) if s in {"BTCUSDT", "ETHUSDT"}]
    if not symbols:
        raise SystemExit("BTCUSDT/ETHUSDT missing from config")

    data: dict[str, dict[str, list[Candle]]] = {}
    funding: dict[str, list[tuple[int, float]]] = {}
    print(f"Downloading/reusing public Bitget data: {start.isoformat()} -> {end.isoformat()} (warmup from {warmup_start.date()})")
    for symbol in symbols:
        data[symbol] = {}
        for interval in ("5m", "15m", "1H", "4H"):
            cache = cache_dir / f"{symbol}_{interval}_{warmup_start:%Y%m%d}_{end:%Y%m%d}.json"
            candles = client.historical_candles(symbol, interval, warmup_ms, end_ms, cache, args.refresh)
            if not candles:
                raise RuntimeError(f"No candles: {symbol} {interval}")
            data[symbol][interval] = candles
            print(f"  {symbol} {interval}: {len(candles):,}")
        fcache = cache_dir / f"{symbol}_funding.json"
        funding[symbol] = client.funding_history(symbol, fcache, args.refresh)
        print(f"  {symbol} funding records: {len(funding[symbol]):,}")

    engine = BacktestEngine(
        cfg=cfg,
        data=data,
        funding=funding,
        start_ms=start_ms,
        end_ms=end_ms,
        initial_equity=args.initial_equity,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
        oi_mode=args.oi_mode,
        impulse_confirm_bars=args.impulse_confirm_bars,
    )
    summary = engine.run()
    write_results(out_dir, args, summary, engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
