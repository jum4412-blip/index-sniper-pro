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


def _whipsaw_metrics(candles: list[Candle], lookback_days: int) -> tuple[float | None, float | None, int]:
    """Return (directional efficiency, close-direction flip ratio, move_count).

    Uses completed candles only: the currently forming daily candle is excluded.
    Low efficiency or high flip ratio is a simple sideways/whipsaw proxy.
    """
    lookback = max(3, int(lookback_days or 0))
    completed = candles[:-1]
    if len(completed) < lookback + 1:
        return None, None, max(0, len(completed) - 1)
    closes = [c.close for c in completed[-(lookback + 1):]]
    moves = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    abs_sum = sum(abs(x) for x in moves)
    direct = abs(closes[-1] - closes[0])
    efficiency = (direct / abs_sum) if abs_sum > 0 else 0.0
    signs = [1 if x > 0 else -1 if x < 0 else 0 for x in moves]
    signs = [s for s in signs if s != 0]
    if len(signs) <= 1:
        flip_ratio = 0.0
    else:
        flips = sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])
        flip_ratio = flips / (len(signs) - 1)
    return efficiency, flip_ratio, len(moves)



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
    use_ema_filter: bool = True,
    no_ma_both_breakout_mode: str = "skip",
    survival_min_breakout_atr: float = 0.0,
    whipsaw_filter_enabled: bool = False,
    whipsaw_filter_symbols: tuple[str, ...] | list[str] = (),
    whipsaw_filter_lookback_days: int = 10,
    whipsaw_min_efficiency_ratio: float = 0.22,
    whipsaw_max_flip_ratio: float = 0.60,
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
    min_breakout = max(0.0, float(survival_min_breakout_atr or 0.0))
    long_hit = atr_value is not None and current_price >= long_target + (atr_value * min_breakout)
    short_hit = atr_value is not None and current_price <= short_target - (atr_value * min_breakout)

    if atr_value is None:
        reason = f"atr unavailable: got {max(len(daily_candles) - 1, 0)} TR, need at least {min_atr_period}"
    elif not use_ema_filter:
        # v2.6 live no-MA mode: pure volatility breakout.
        # In live trading current_price normally cannot be above long_target and below short_target at the same time,
        # but the branch is kept for safety and parity with the backtest option.
        whipsaw_symbols = {s.upper() for s in (whipsaw_filter_symbols or [])}
        whipsaw_applies = whipsaw_filter_enabled and (not whipsaw_symbols or symbol.upper() in whipsaw_symbols)
        if whipsaw_applies and (long_hit or short_hit):
            eff, flip, move_count = _whipsaw_metrics(daily_candles, whipsaw_filter_lookback_days)
            blocked: list[str] = []
            if eff is not None and eff < whipsaw_min_efficiency_ratio:
                blocked.append(f"efficiency {eff:.3f} < {whipsaw_min_efficiency_ratio:.3f}")
            if flip is not None and flip > whipsaw_max_flip_ratio:
                blocked.append(f"flip {flip:.3f} > {whipsaw_max_flip_ratio:.3f}")
            if blocked:
                reason = "NO_MA whipsaw filter blocked: " + ", ".join(blocked) + f"; lookback={move_count}"
            else:
                reason = f"NO_MA whipsaw filter passed: efficiency={eff if eff is not None else -1:.3f}, flip={flip if flip is not None else -1:.3f}"

        if reason.startswith("NO_MA whipsaw filter blocked"):
            pass
        elif long_hit and short_hit:
            mode = (no_ma_both_breakout_mode or "skip").strip().lower()
            long_strength = max(0.0, current_price - long_target) / atr_value
            short_strength = max(0.0, short_target - current_price) / atr_value
            if mode == "stronger":
                if long_strength >= short_strength:
                    signal = "LONG"
                    reason = "NO_MA both breakout choose LONG stronger"
                else:
                    signal = "SHORT"
                    reason = "NO_MA both breakout choose SHORT stronger"
            elif mode == "candle":
                if current.close >= current.open:
                    signal = "LONG"
                    reason = "NO_MA both breakout choose LONG green candle"
                else:
                    signal = "SHORT"
                    reason = "NO_MA both breakout choose SHORT red candle"
            else:
                reason = "NO_MA both breakout skipped"
        elif long_hit:
            signal = "LONG"
            reason = "NO_MA upper volatility breakout"
        elif short_hit:
            signal = "SHORT"
            reason = "NO_MA lower volatility breakout"
        elif current_price >= long_target:
            reason = "NO_MA upper target touched but min breakout ATR not met"
        elif current_price <= short_target:
            reason = "NO_MA lower target touched but min breakout ATR not met"
        else:
            reason = "NO_MA waiting breakout"

        if signal == "LONG":
            stop_price = current_price - (atr_value * atr_stop_mult)
            take_profit_price = current_price + (atr_value * atr_take_profit_mult)
        elif signal == "SHORT":
            stop_price = current_price + (atr_value * atr_stop_mult)
            take_profit_price = current_price - (atr_value * atr_take_profit_mult)
    elif fast is None or slow is None:
        if current_price >= long_target:
            reason = "upper breakout but trend data insufficient"
        elif current_price <= short_target:
            reason = "lower breakout but trend data insufficient"
        else:
            reason = "trend data insufficient, waiting"
    elif long_hit and bullish:
        signal = "LONG"
        reason = f"upper breakout + trend bullish ({trend_mode})"
        stop_price = current_price - (atr_value * atr_stop_mult)
        take_profit_price = current_price + (atr_value * atr_take_profit_mult)
    elif short_hit and bearish:
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
        ema_fast=(None if not use_ema_filter else fast),
        ema_slow=(None if not use_ema_filter else slow),
        atr=atr_value,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        trend_mode=("NO_MA_VOL_BREAKOUT" if not use_ema_filter else trend_mode),
        trend_interval=("1D_NO_MA" if not use_ema_filter else trend_interval),
        trend_candle_count=trend_count,
        daily_candle_count=len(daily_candles),
        atr_period_used=atr_period_used,
        warmup_mode=warmup_mode,
        data_quality=data_quality,
    )
