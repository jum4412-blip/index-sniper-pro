from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from index_sniper.strategy.indicators import Candle, atr, ema

SignalSide = Literal["LONG", "SHORT", "HOLD"]


@dataclass(frozen=True)
class BreakoutSignal:
    symbol: str
    signal: SignalSide
    reason: str
    current_price: float
    current_open: float
    previous_high: float
    previous_low: float
    previous_range: float
    long_target: float
    short_target: float
    ema_fast: float | None
    ema_slow: float | None
    atr: float | None
    stop_price: float | None
    take_profit_price: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def build_breakout_signal(
    *,
    symbol: str,
    candles: list[Candle],
    current_price: float,
    k_value: float,
    ema_fast_period: int,
    ema_slow_period: int,
    atr_period: int,
    atr_stop_mult: float,
    atr_take_profit_mult: float,
) -> BreakoutSignal:
    min_required = max(ema_slow_period, atr_period + 2, 3)
    if len(candles) < min_required:
        raise RuntimeError(f"not enough candles for {symbol}: got {len(candles)}, need {min_required}")

    current = candles[-1]
    previous = candles[-2]
    closes = [c.close for c in candles]
    fast = ema(closes, ema_fast_period)
    slow = ema(closes, ema_slow_period)
    atr_value = atr(candles, atr_period)

    prev_range = previous.high - previous.low
    long_target = current.open + (prev_range * k_value)
    short_target = current.open - (prev_range * k_value)

    bullish = fast is not None and slow is not None and fast > slow
    bearish = fast is not None and slow is not None and fast < slow

    signal: SignalSide = "HOLD"
    reason = "no breakout"
    stop_price = None
    take_profit_price = None

    if atr_value is None:
        reason = "atr unavailable"
    elif current_price >= long_target and bullish:
        signal = "LONG"
        reason = "upper breakout + EMA trend bullish"
        stop_price = current_price - (atr_value * atr_stop_mult)
        take_profit_price = current_price + (atr_value * atr_take_profit_mult)
    elif current_price <= short_target and bearish:
        signal = "SHORT"
        reason = "lower breakout + EMA trend bearish"
        stop_price = current_price + (atr_value * atr_stop_mult)
        take_profit_price = current_price - (atr_value * atr_take_profit_mult)
    elif current_price >= long_target and not bullish:
        reason = "upper breakout but EMA filter rejected"
    elif current_price <= short_target and not bearish:
        reason = "lower breakout but EMA filter rejected"
    elif bullish:
        reason = "bullish trend, waiting upper target"
    elif bearish:
        reason = "bearish trend, waiting lower target"
    else:
        reason = "EMA trend neutral"

    return BreakoutSignal(
        symbol=symbol,
        signal=signal,
        reason=reason,
        current_price=current_price,
        current_open=current.open,
        previous_high=previous.high,
        previous_low=previous.low,
        previous_range=prev_range,
        long_target=long_target,
        short_target=short_target,
        ema_fast=fast,
        ema_slow=slow,
        atr=atr_value,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
    )
