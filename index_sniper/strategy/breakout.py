from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from index_sniper.strategy.indicators import Candle, ema, true_ranges

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
    trend_mode: str
    trend_interval: str
    trend_candle_count: int
    daily_candle_count: int
    atr_period_used: int | None
    warmup_mode: bool
    data_quality: str

    def to_dict(self) -> dict:
        return asdict(self)


def _available_atr(candles: list[Candle], target_period: int, min_period: int) -> tuple[float | None, int | None]:
    trs = true_ranges(candles)
    if target_period <= 0 or min_period <= 0 or len(trs) < min_period:
        return None, None
    period_used = min(target_period, len(trs))
    return sum(trs[-period_used:]) / period_used, period_used


def _choose_trend(
    *,
    daily_candles: list[Candle],
    trend_candles: list[Candle] | None,
    ema_fast_period: int,
    ema_slow_period: int,
    warmup_trend_interval: str,
    warmup_ema_fast: int,
    warmup_ema_slow: int,
    fallback_ema_fast: int,
    fallback_ema_slow: int,
) -> tuple[float | None, float | None, str, str, int, bool, str]:
    daily_closes = [c.close for c in daily_candles]
    if len(daily_candles) >= ema_slow_period:
        fast = ema(daily_closes, ema_fast_period)
        slow = ema(daily_closes, ema_slow_period)
        return fast, slow, f"1D_EMA{ema_fast_period}/{ema_slow_period}", "1D", len(daily_candles), False, "normal"
    if trend_candles and len(trend_candles) >= warmup_ema_slow:
        trend_closes = [c.close for c in trend_candles]
        fast = ema(trend_closes, warmup_ema_fast)
        slow = ema(trend_closes, warmup_ema_slow)
        return fast, slow, f"{warmup_trend_interval}_EMA{warmup_ema_fast}/{warmup_ema_slow}_WARMUP", warmup_trend_interval, len(trend_candles), True, "warmup_4h"
    if len(daily_candles) >= fallback_ema_slow:
        fast = ema(daily_closes, fallback_ema_fast)
        slow = ema(daily_closes, fallback_ema_slow)
        return fast, slow, f"1D_EMA{fallback_ema_fast}/{fallback_ema_slow}_FALLBACK", "1D", len(daily_candles), True, "warmup_daily_fallback"
    return None, None, "INSUFFICIENT_TREND_DATA", "-", len(daily_candles), True, "insufficient"


def build_breakout_signal_adaptive(
    *,
    symbol: str,
    daily_candles: list[Candle],
    trend_candles: list[Candle] | None,
    current_price: float,
    k_value: float,
    ema_fast_period: int,
    ema_slow_period: int,
    atr_period: int,
    atr_stop_mult: float,
    atr_take_profit_mult: float,
    warmup_trend_interval: str,
    warmup_ema_fast: int,
    warmup_ema_slow: int,
    fallback_ema_fast: int,
    fallback_ema_slow: int,
    min_atr_period: int,
) -> BreakoutSignal:
    if len(daily_candles) < 3:
        raise RuntimeError(f"not enough daily candles for {symbol}: got {len(daily_candles)}, need 3")
    current = daily_candles[-1]
    previous = daily_candles[-2]
    fast, slow, trend_mode, trend_interval, trend_count, warmup_mode, data_quality = _choose_trend(
        daily_candles=daily_candles,
        trend_candles=trend_candles,
        ema_fast_period=ema_fast_period,
        ema_slow_period=ema_slow_period,
        warmup_trend_interval=warmup_trend_interval,
        warmup_ema_fast=warmup_ema_fast,
        warmup_ema_slow=warmup_ema_slow,
        fallback_ema_fast=fallback_ema_fast,
        fallback_ema_slow=fallback_ema_slow,
    )
    atr_value, atr_period_used = _available_atr(daily_candles, atr_period, min_atr_period)
    prev_range = max(previous.high - previous.low, 0.0)
    long_target = current.open + (prev_range * k_value)
    short_target = current.open - (prev_range * k_value)
    bullish = fast is not None and slow is not None and fast > slow
    bearish = fast is not None and slow is not None and fast < slow
    signal: SignalSide = "HOLD"
    reason = "no breakout"
    stop_price = None
    take_profit_price = None
    if atr_value is None:
        reason = f"atr unavailable: got {max(len(daily_candles) - 1, 0)} TR, need at least {min_atr_period}"
    elif fast is None or slow is None:
        if current_price >= long_target:
            reason = "upper breakout but trend data insufficient"
        elif current_price <= short_target:
            reason = "lower breakout but trend data insufficient"
        else:
            reason = "trend data insufficient, waiting"
    elif current_price >= long_target and bullish:
        signal = "LONG"
        reason = f"upper breakout + trend bullish ({trend_mode})"
        stop_price = current_price - (atr_value * atr_stop_mult)
        take_profit_price = current_price + (atr_value * atr_take_profit_mult)
    elif current_price <= short_target and bearish:
        signal = "SHORT"
        reason = f"lower breakout + trend bearish ({trend_mode})"
        stop_price = current_price + (atr_value * atr_stop_mult)
        take_profit_price = current_price - (atr_value * atr_take_profit_mult)
    elif current_price >= long_target and not bullish:
        reason = f"upper breakout but trend filter rejected ({trend_mode})"
    elif current_price <= short_target and not bearish:
        reason = f"lower breakout but trend filter rejected ({trend_mode})"
    elif bullish:
        reason = f"bullish trend, waiting upper target ({trend_mode})"
    elif bearish:
        reason = f"bearish trend, waiting lower target ({trend_mode})"
    else:
        reason = f"trend neutral ({trend_mode})"
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
        trend_mode=trend_mode,
        trend_interval=trend_interval,
        trend_candle_count=trend_count,
        daily_candle_count=len(daily_candles),
        atr_period_used=atr_period_used,
        warmup_mode=warmup_mode,
        data_quality=data_quality,
    )
