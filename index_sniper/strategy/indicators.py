from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


def parse_candles(response: dict[str, Any]) -> list[Candle]:
    rows = response.get("data") or []
    parsed: list[Candle] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        parsed.append(
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
    parsed.sort(key=lambda x: x.ts)
    return parsed


def ema(values: list[float], period: int) -> float | None:
    if period <= 0 or len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = (value * alpha) + (current * (1.0 - alpha))
    return current


def true_ranges(candles: list[Candle]) -> list[float]:
    if len(candles) < 2:
        return []
    trs: list[float] = []
    prev_close = candles[0].close
    for c in candles[1:]:
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        trs.append(tr)
        prev_close = c.close
    return trs


def atr(candles: list[Candle], period: int) -> float | None:
    trs = true_ranges(candles)
    if period <= 0 or len(trs) < period:
        return None
    return sum(trs[-period:]) / period
