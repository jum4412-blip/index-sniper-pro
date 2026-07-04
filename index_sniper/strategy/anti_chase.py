from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from index_sniper.strategy.indicators import Candle


@dataclass(frozen=True)
class AntiChaseDecision:
    enabled: bool
    ok: bool
    reason: str
    previous_change_pct: float | None
    previous_range_atr: float | None
    max_entry_extension_atr: float | None
    entry_extension_atr: float | None
    size_multiplier: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _previous_change_pct(candles: list[Candle]) -> float | None:
    if len(candles) < 3:
        return None
    prev = candles[-2]
    prior = candles[-3]
    if prior.close == 0:
        return None
    return ((prev.close - prior.close) / prior.close) * 100.0


def _previous_range_atr(candles: list[Candle], atr_value: float | None) -> float | None:
    if len(candles) < 2 or not atr_value or atr_value <= 0:
        return None
    prev = candles[-2]
    return max(prev.high - prev.low, 0.0) / atr_value


def _entry_extension_atr(signal: Any) -> float | None:
    atr = getattr(signal, "atr", None)
    if not atr or atr <= 0:
        return None
    if getattr(signal, "signal", "HOLD") == "LONG":
        return max(0.0, float(signal.current_price) - float(signal.long_target)) / float(atr)
    if getattr(signal, "signal", "HOLD") == "SHORT":
        return max(0.0, float(signal.short_target) - float(signal.current_price)) / float(atr)
    return 0.0


def evaluate_anti_chase(settings: Any, symbol: str, signal: Any, daily_candles: list[Candle]) -> AntiChaseDecision:
    """Block late/extreme continuation entries.

    Survival logic:
    - If yesterday was an extreme UP day, do not open new LONG the next day.
    - If yesterday was an extreme DOWN day, do not open new SHORT the next day.
    - If current price has already run too far past the breakout level, skip late chase.

    This only blocks NEW entries. Existing positions remain managed by TP/SL and position manager.
    """
    enabled = bool(getattr(settings, "anti_chase_enabled", True))
    allowed_symbols = {s.upper() for s in getattr(settings, "anti_chase_symbols", [])}
    if not enabled or (allowed_symbols and symbol.upper() not in allowed_symbols):
        return AntiChaseDecision(False, True, "disabled", None, None, None, None, 1.0)

    side = getattr(signal, "signal", "HOLD")
    prev_pct = _previous_change_pct(daily_candles)
    range_atr = _previous_range_atr(daily_candles, getattr(signal, "atr", None))
    extension = _entry_extension_atr(signal)
    max_extension = float(getattr(settings, "max_entry_extension_atr", 0.40))

    if side not in {"LONG", "SHORT"}:
        return AntiChaseDecision(True, True, "no active entry signal", prev_pct, range_atr, max_extension, extension, 1.0)

    reasons: list[str] = []
    ok = True
    size_mult = 1.0

    up_pct = float(getattr(settings, "anti_chase_extreme_up_pct", 7.0))
    down_pct = float(getattr(settings, "anti_chase_extreme_down_pct", 7.0))
    range_mult = float(getattr(settings, "anti_chase_extreme_range_atr", 1.8))
    long_mult = float(getattr(settings, "anti_chase_extreme_long_size_multiplier", 0.0))
    short_mult = float(getattr(settings, "anti_chase_extreme_short_size_multiplier", 0.0))

    if side == "LONG":
        extreme_up = (prev_pct is not None and prev_pct >= up_pct) or (range_atr is not None and range_atr >= range_mult and prev_pct is not None and prev_pct > 0)
        if extreme_up:
            reasons.append(f"anti-chase: previous extreme up day prev_change={prev_pct:.3f}% range_atr={range_atr}")
            size_mult = min(size_mult, max(0.0, long_mult))
            if long_mult <= 0:
                ok = False
        if extension is not None and extension > max_extension:
            reasons.append(f"late LONG chase: extension_atr={extension:.3f} > {max_extension:.3f}")
            ok = False
    elif side == "SHORT":
        extreme_down = (prev_pct is not None and prev_pct <= -down_pct) or (range_atr is not None and range_atr >= range_mult and prev_pct is not None and prev_pct < 0)
        if extreme_down:
            reasons.append(f"anti-chase: previous extreme down day prev_change={prev_pct:.3f}% range_atr={range_atr}")
            size_mult = min(size_mult, max(0.0, short_mult))
            if short_mult <= 0:
                ok = False
        if extension is not None and extension > max_extension:
            reasons.append(f"late SHORT chase: extension_atr={extension:.3f} > {max_extension:.3f}")
            ok = False

    return AntiChaseDecision(
        True,
        ok,
        "; ".join(reasons) if reasons else "ok",
        round(prev_pct, 6) if prev_pct is not None else None,
        round(range_atr, 6) if range_atr is not None else None,
        max_extension,
        round(extension, 6) if extension is not None else None,
        size_mult,
    )
