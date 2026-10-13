"""Frozen, causal W/D regime and H4/H1/M5 structural-entry hypothesis.

This is a Turtle-inspired adaptation, not the original Turtle rules. Weekly
and daily completed channels define direction; confirmed H4/H1 pivots define
structure; M5 close and volume define entry timing. No profitable probability
is invented here. A model must be separately calibrated to the exact spec.

A pivot is usable only after its two right-hand bars have closed. A newly
computed guard applies to future execution; it cannot protect the bars that
confirmed it. Native exchange protection and fill reconciliation belong to
execution, not this pure module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from statistics import median
from typing import Any, Iterable, Mapping

M5_MS = 300_000
HOUR_MS = 3_600_000
H4_MS = 4 * HOUR_MS
DAY_MS = 24 * HOUR_MS
WEEK_MS = 7 * DAY_MS
WEEK_ANCHOR_MS = 4 * DAY_MS  # 1970-01-05 00:00 UTC, Monday
SYMBOLS = ("BTCUSDT", "ETHUSDT")
FRAME_RULES = {
    "W": (WEEK_MS, WEEK_ANCHOR_MS, 20, 5),
    "D": (DAY_MS, 0, 90, 21),
    "H4": (H4_MS, 0, 120, 5),
    "H1": (HOUR_MS, 0, 72, 5),
    "M5": (M5_MS, 0, 64, 21),
}
SPEC = {
    "name": "W_D_H4_H1_M5_STRUCTURAL_TREND_V1", "version": 1,
    "time_alignment": "UTC_Monday_weeks_UTC_days_fixed_H4_H1_M5",
    "decision": "latest_completed_M5_close_then_60_second_execution_delay",
    "closed_data_only": True,
    "weekly_bias": {"channel": 4, "total_reconstruction_bars": 20,
                    "rule": "latest_close_strictly_outside_previous_channel_sets_direction_else_carry_within_window_no_event_neutral"},
    "daily_bias": {"channel": 20, "total_reconstruction_bars": 90,
                   "rule": "latest_close_strictly_outside_previous_channel_sets_direction_else_carry_within_window_no_event_neutral"},
    "entry_regime": "weekly_equals_daily_and_nonzero",
    "pivot": {"left": 2, "right": 2, "strict": True,
              "availability": "after_both_right_bars_closed", "plateau": "ignored"},
    "H4": {"total_bars": 120, "structure": "latest_confirmed_opposite_pivot_close_and_M5_on_valid_side"},
    "H1": {"total_bars": 72, "structure": "latest_two_confirmed_opposite_pivots_HL_long_LH_short_H1_and_M5_on_valid_side"},
    "M5": {"total_bars": 64, "channel": 20,
           "entry": "close_strictly_breaks_previous_20_high_low",
           "volume": "base_volume_ge_1.5_median_previous_20", "volume_threshold": 1.5},
    "initial_guard": "long_max_H4_low_H1_low_minus_exchange_tick_short_min_H4_high_H1_high_plus_exchange_tick",
    "ratchet": "latest_confirmed_H1_opposite_pivot_minus_plus_tick_never_loosen",
    "exit": "native_guard_or_M5_close_through_guard_or_H4_completed_close_structure_breach_or_either_W_D_opposite_bias",
    "ordinary_M5_countertrend_exit": False,
    "unavailable_neutral_W_D": "block_entries_preserve_position_management",
    "fixed_take_profit": False, "fixed_percentage_stop": False,
    "pyramiding": False, "original_turtle_reproduction": False,
    "conditional_bins": ["symbol", "side", "confirmed_volume_1.5_to_2_or_high_volume_ge_2"],
    "measured_probability": "not_provided_requires_separate_frozen_calibration",
    "psychology": "observable_support_resistance_only_no_direct_psychology_measurement",
}


def spec_sha256() -> str:
    return hashlib.sha256(json.dumps(SPEC, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


class StrategyDataError(ValueError):
    """Invalid, missing or stale completed evidence cannot authorize an entry."""


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise StrategyDataError(label + "_INVALID")
    try:
        result = int(value)
        if float(value) != result:
            raise ValueError()
    except (TypeError, ValueError, OverflowError) as exc:
        raise StrategyDataError(label + "_INVALID") from exc
    return result


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise StrategyDataError(label + "_INVALID")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise StrategyDataError(label + "_INVALID") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise StrategyDataError(label + "_INVALID")
    return result


def _timestamp(row: Any) -> int:
    if isinstance(row, (list, tuple)):
        if not row:
            raise StrategyDataError("INCOMPLETE_CANDLE")
        stamp = row[0]
    elif isinstance(row, Mapping):
        try:
            stamp = row["open_ms"] if "open_ms" in row else row["ts"]
        except KeyError as exc:
            raise StrategyDataError("INCOMPLETE_CANDLE") from exc
    else:
        try:
            stamp = row.open_ms if hasattr(row, "open_ms") else row.ts
        except AttributeError as exc:
            raise StrategyDataError("INCOMPLETE_CANDLE") from exc
    return _integer(stamp, "OPEN_MS")


@dataclass(frozen=True)
class Candle:
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def close_ms(self, interval_ms: int) -> int:
        return self.open_ms + interval_ms

    @classmethod
    def from_row(cls, row: Any, interval_ms: int, anchor_ms: int = 0) -> "Candle":
        stamp = _timestamp(row)
        duration = _integer(interval_ms, "INTERVAL_MS")
        anchor = _integer(anchor_ms, "ANCHOR_MS")
        if duration <= 0 or stamp < 0 or (stamp - anchor) % duration:
            raise StrategyDataError("MISALIGNED_CANDLE")
        if isinstance(row, (list, tuple)):
            if len(row) < 6:
                raise StrategyDataError("INCOMPLETE_CANDLE")
            values = row[1:6]
        elif isinstance(row, Mapping):
            try:
                values = [row[key] for key in ("open", "high", "low", "close", "volume")]
            except KeyError as exc:
                raise StrategyDataError("INCOMPLETE_CANDLE") from exc
        else:
            try:
                values = [row.open, row.high, row.low, row.close, row.volume]
            except AttributeError as exc:
                raise StrategyDataError("INCOMPLETE_CANDLE") from exc
        op, high, low, close = [_number(value, "PRICE", positive=True) for value in values[:4]]
        volume = _number(values[4], "VOLUME")
        if volume < 0 or low > min(op, close) or high < max(op, close) or high < low:
            raise StrategyDataError("INVALID_OHLCV")
        return cls(stamp, op, high, low, close, volume)


def completed_candles(candles: Iterable[Any], now_ms: int, interval_ms: int,
                      anchor_ms: int = 0, *, max_count: int | None = None) -> tuple[Candle, ...]:
    """Filter forming/future OHLCV by time BEFORE parsing their prices.

    Closed duplicates and alignment errors are refused. When bounded, prices
    outside the reconstruction window do not participate in this hypothesis.
    """
    now = _integer(now_ms, "NOW_MS")
    duration = _integer(interval_ms, "INTERVAL_MS")
    anchor = _integer(anchor_ms, "ANCHOR_MS")
    if now < 0 or duration <= 0:
        raise StrategyDataError("TIME_INVALID")
    if max_count is not None and (isinstance(max_count, bool) or max_count <= 0):
        raise StrategyDataError("MAX_COUNT_INVALID")
    selected: dict[int, Any] = {}
    for row in candles:
        stamp = _timestamp(row)
        if stamp + duration > now:
            continue
        if stamp < 0 or (stamp - anchor) % duration:
            raise StrategyDataError("MISALIGNED_CANDLE")
        if stamp in selected:
            raise StrategyDataError("DUPLICATE_COMPLETE_CANDLE")
        selected[stamp] = row
    stamps = sorted(selected)
    if max_count is not None:
        stamps = stamps[-max_count:]
    return tuple(Candle.from_row(selected[stamp], duration, anchor) for stamp in stamps)


def _window(rows: Iterable[Any], now: int, frame: str) -> tuple[Candle, ...]:
    duration, anchor, limit, minimum = FRAME_RULES[frame]
    bars = completed_candles(rows, now, duration, anchor, max_count=limit)
    if len(bars) < minimum:
        raise StrategyDataError("INSUFFICIENT_HISTORY")
    expected_close = (now - anchor) // duration * duration + anchor
    if bars[-1].close_ms(duration) != expected_close:
        raise StrategyDataError("LATEST_COMPLETE_CANDLE_MISSING")
    if any(right.open_ms - left.open_ms != duration for left, right in zip(bars, bars[1:])):
        raise StrategyDataError("NONCONTIGUOUS_DECISION_WINDOW")
    return bars


def rolling_bias(bars: Iterable[Candle], lookback: int) -> dict[str, Any]:
    """No event before this bounded history is silently carried into it."""
    history = tuple(bars)
    direction, event_open_ms = 0, None
    event_level = None
    for index in range(lookback, len(history)):
        channel = history[index - lookback:index]
        high, low = max(bar.high for bar in channel), min(bar.low for bar in channel)
        if history[index].close > high:
            direction, event_open_ms, event_level = 1, history[index].open_ms, high
        elif history[index].close < low:
            direction, event_open_ms, event_level = -1, history[index].open_ms, low
    return {"direction": direction, "event_open_ms": event_open_ms,
            "event_level": event_level, "total_bars": len(history),
            "lookback": lookback, "bounded_reconstruction": True}


@dataclass(frozen=True)
class Pivot:
    side: str
    open_ms: int
    price: float
    confirmed_ms: int

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def confirmed_pivots(bars: Iterable[Candle], side: str, interval_ms: int) -> tuple[Pivot, ...]:
    """Strict 2-left/2-right pivots; tied/plateau extrema are not pivots."""
    if side not in ("low", "high"):
        raise StrategyDataError("INVALID_PIVOT_SIDE")
    history = tuple(bars)
    result = []
    for index in range(2, len(history) - 2):
        value = getattr(history[index], side)
        neighbours = history[index - 2:index] + history[index + 1:index + 3]
        valid = all(value < getattr(bar, side) for bar in neighbours) if side == "low" else all(
            value > getattr(bar, side) for bar in neighbours)
        if valid:
            result.append(Pivot(side, history[index].open_ms, value,
                                history[index + 2].close_ms(interval_ms)))
    return tuple(result)


def _load_frames(frames: Mapping[str, Any], now_ms: int) -> tuple[dict[str, tuple[Candle, ...]], dict[str, str]]:
    if not isinstance(frames, Mapping):
        raise StrategyDataError("FRAMES_INVALID")
    now = _integer(now_ms, "NOW_MS")
    if now < 0:
        raise StrategyDataError("NOW_MS_INVALID")
    loaded, errors = {}, {}
    for frame in FRAME_RULES:
        if frame not in frames or frames[frame] is None:
            errors[frame] = "MISSING_FRAME"
            continue
        try:
            loaded[frame] = _window(frames[frame], now, frame)
        except (StrategyDataError, TypeError) as exc:
            errors[frame] = str(exc) if isinstance(exc, StrategyDataError) else "FRAME_NOT_ITERABLE"
    return loaded, errors


def _structure(loaded: Mapping[str, tuple[Candle, ...]]) -> dict[str, Any]:
    result = {"H4": {"low": None, "high": None}, "H1": {"lows": [], "highs": []}}
    if "H4" in loaded:
        for side in ("low", "high"):
            pivots = confirmed_pivots(loaded["H4"], side, H4_MS)
            result["H4"][side] = pivots[-1].asdict() if pivots else None
        result["H4"]["close"] = loaded["H4"][-1].close
        result["H4"]["closed_ms"] = loaded["H4"][-1].close_ms(H4_MS)
    if "H1" in loaded:
        for side in ("low", "high"):
            pivots = confirmed_pivots(loaded["H1"], side, HOUR_MS)
            result["H1"][side + "s"] = [pivot.asdict() for pivot in pivots[-2:]]
        result["H1"]["close"] = loaded["H1"][-1].close
        result["H1"]["closed_ms"] = loaded["H1"][-1].close_ms(HOUR_MS)
    return result


def analyze(frames: Mapping[str, Any], now_ms: int, tick_size: Any = None) -> dict[str, Any]:
    """Explain all five frame roles without inventing a success probability."""
    if tick_size is not None:
        frames = {**frames, "tick_size": tick_size}
    loaded, errors = _load_frames(frames, now_ms)
    reasons = [f"{frame}_{error}" for frame, error in errors.items()]
    bias = {"W": rolling_bias(loaded["W"], 4) if "W" in loaded else {"direction": None},
            "D": rolling_bias(loaded["D"], 20) if "D" in loaded else {"direction": None}}
    for frame, label in (("W", "WEEKLY"), ("D", "DAILY")):
        if bias[frame]["direction"] == 0:
            reasons.append("NO_" + label + "_DIRECTION")
    w, d = bias["W"]["direction"], bias["D"]["direction"]
    side = "long" if w == d == 1 else "short" if w == d == -1 else None
    if w in (-1, 1) and d in (-1, 1) and w != d:
        reasons.append("W_D_DIRECTION_CONFLICT")
    structure = _structure(loaded)
    breakout = {"side": None, "level": None, "volume_ratio": None, "signal_close": None}
    if "M5" in loaded:
        decision, prior = loaded["M5"][-1], loaded["M5"][-21:-1]
        high, low = max(bar.high for bar in prior), min(bar.low for bar in prior)
        baseline = median(bar.volume for bar in prior)
        ratio = decision.volume / baseline if baseline > 0 else None
        breakout = {"side": "long" if decision.close > high else "short" if decision.close < low else None,
                    "level": high if decision.close > high else low if decision.close < low else None,
                    "high": high, "low": low, "volume_ratio": ratio,
                    "signal_close": decision.close, "closed_ms": decision.close_ms(M5_MS)}
    guard = None
    try:
        tick = _number(frames.get("tick_size"), "TICK_SIZE", positive=True)
    except StrategyDataError:
        tick = None
        reasons.append("TICK_SIZE_INVALID")
    if side:
        opposite = "low" if side == "long" else "high"
        h4 = structure["H4"][opposite]
        h1pivots = structure["H1"][opposite + "s"]
        valid_side = lambda price, level: price > level if side == "long" else price < level
        if "H4" in loaded:
            if not h4:
                reasons.append("NO_H4_CONFIRMED_" + opposite.upper())
            elif not valid_side(loaded["H4"][-1].close, h4["price"]):
                reasons.append("H4_STRUCTURE_BREACHED")
            elif "M5" in loaded and not valid_side(loaded["M5"][-1].close, h4["price"]):
                reasons.append("M5_BEYOND_H4_STRUCTURE")
        if "H1" in loaded:
            if len(h1pivots) < 2:
                reasons.append("NO_TWO_H1_CONFIRMED_" + opposite.upper() + "_PIVOTS")
            else:
                sequence = h1pivots[-1]["price"] > h1pivots[-2]["price"] if side == "long" else (
                    h1pivots[-1]["price"] < h1pivots[-2]["price"])
                if not sequence:
                    reasons.append("NO_H1_HIGHER_LOW" if side == "long" else "NO_H1_LOWER_HIGH")
                if not valid_side(loaded["H1"][-1].close, h1pivots[-1]["price"]):
                    reasons.append("H1_STRUCTURE_BREACHED")
                if "M5" in loaded and not valid_side(loaded["M5"][-1].close, h1pivots[-1]["price"]):
                    reasons.append("M5_BEYOND_H1_STRUCTURE")
        if "M5" in loaded:
            if breakout["side"] != side:
                reasons.append("NO_ALIGNED_M5_BREAKOUT")
            if breakout["volume_ratio"] is None or breakout["volume_ratio"] < 1.5:
                reasons.append("M5_VOLUME_NOT_CONFIRMED")
        if h4 and h1pivots and tick:
            guard = max(h4["price"], h1pivots[-1]["price"]) - tick if side == "long" else (
                min(h4["price"], h1pivots[-1]["price"]) + tick)
            if guard <= 0 or ("M5" in loaded and not valid_side(loaded["M5"][-1].close, guard)):
                reasons.append("INITIAL_GUARD_INVALID")
    evidence = {frame: {"total_bars": len(bars), "latest_open_ms": bars[-1].open_ms,
                       "latest_closed_ms": bars[-1].close_ms(FRAME_RULES[frame][0])}
                for frame, bars in loaded.items()}
    return {"spec_sha256": spec_sha256(), "eligible": side is not None and not reasons,
            "side": side, "wait_reasons": reasons, "frame_errors": errors,
            "bias": bias, "structure": structure, "breakout": breakout,
            "guard": guard, "tick_size": tick, "per_frame_evidence": evidence}


def context(frames: Mapping[str, Any], now_ms: int, tick_size: Any = None) -> dict[str, Any]:
    """Causal higher-frame evidence, cacheable until the next H1 close.

    M5 timing and a held guard remain outside this cache. Missing or invalid
    higher frames remain explicit; native guards alone cannot authorize entry.
    """
    if tick_size is not None:
        frames = {**frames, "tick_size": tick_size}
    loaded, errors = _load_frames(frames, now_ms)
    errors.pop("M5", None)
    bias = {frame: rolling_bias(loaded[frame], lookback) if frame in loaded else {"direction": None}
            for frame, lookback in (("W", 4), ("D", 20))}
    structure = _structure(loaded)
    try:
        tick = _number(frames.get("tick_size"), "TICK_SIZE", positive=True)
    except StrategyDataError:
        tick = None
        errors["tick_size"] = "TICK_SIZE_INVALID"
    native_guards, h1_guards, structure_valid = {}, {}, {}
    for side, opposite in (("long", "low"), ("short", "high")):
        h4 = structure["H4"][opposite]
        h1 = structure["H1"][opposite + "s"]
        h1_guards[side] = (h1[-1]["price"] - tick if side == "long" else h1[-1]["price"] + tick) if h1 and tick else None
        native_guards[side] = ((max(h4["price"], h1[-1]["price"]) - tick) if side == "long" else
                               (min(h4["price"], h1[-1]["price"]) + tick)) if h4 and h1 and tick else None
        strict_sequence = len(h1) == 2 and (h1[-1]["price"] > h1[-2]["price"] if side == "long" else h1[-1]["price"] < h1[-2]["price"])
        on_side = lambda price, level: price > level if side == "long" else price < level
        structure_valid[side] = bool(h4 and strict_sequence and
                                      on_side(structure["H4"]["close"], h4["price"]) and
                                      on_side(structure["H1"]["close"], h1[-1]["price"]))
    w, d = bias["W"]["direction"], bias["D"]["direction"]
    return {"spec_sha256": spec_sha256(), "bias": bias, "structure": structure,
            "direction": 1 if w == d == 1 else -1 if w == d == -1 else 0,
            "side": "long" if w == d == 1 else "short" if w == d == -1 else None,
            "native_guards": native_guards, "h1_guards": h1_guards,
            "long_guard": native_guards["long"], "short_guard": native_guards["short"],
            "structure_valid": structure_valid, "frame_errors": errors,
            "cache_valid_before_ms": (_integer(now_ms, "NOW_MS") // HOUR_MS + 1) * HOUR_MS}


@dataclass(frozen=True)
class Signal:
    event_id: str
    symbol: str
    side: str
    breakout_level: float
    guard: float
    closed_ms: int
    score: float
    metadata: dict[str, Any]

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def generate_signal(symbol: str, frames: Mapping[str, Any], now_ms: int, tick_size: Any = None) -> Signal | None:
    if symbol not in SYMBOLS:
        raise StrategyDataError("UNSUPPORTED_SYMBOL")
    context = analyze(frames, now_ms, tick_size)
    if not context["eligible"]:
        return None
    event = context["breakout"]
    ratio, side, closed = event["volume_ratio"], context["side"], event["closed_ms"]
    bucket = "high_volume" if ratio >= 2 else "confirmed_volume"
    metadata = {"spec_sha256": context["spec_sha256"], "signal_close": event["signal_close"],
                "decision_open_ms": closed - M5_MS, "volume_ratio": ratio,
                "volume_bucket": bucket, "conditional_bin": f"{symbol}|{side}|{bucket}",
                "probability": None, "probability_reason": "UNMEASURED_REQUIRES_FROZEN_NET_OUTCOME_CALIBRATION",
                "bias": context["bias"], "structure": context["structure"],
                "per_frame_evidence": context["per_frame_evidence"], "tick_size": context["tick_size"],
                "guard_source": "confirmed_H4_H1_opposite_pivots_one_exchange_tick",
                "channel_high": event["high"], "channel_low": event["low"],
                "guard_applies_after_ms": closed, "trader_psychology_measured": False}
    identity = f"{spec_sha256()}|{symbol}|{side}|{closed}"
    return Signal(hashlib.sha256(identity.encode()).hexdigest(), symbol, side,
                  event["level"], context["guard"], closed, ratio, metadata)


@dataclass(frozen=True)
class GuardUpdate:
    side: str
    previous_guard: float
    candidate_guard: float
    guard: float
    closed_ms: int
    close: float | None
    tightened: bool
    should_exit: bool
    reason: str
    metadata: dict[str, Any]

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


def update_guard(side: str, current_guard: float, frames: Mapping[str, Any], now_ms: int,
                 tick_size: Any = None) -> GuardUpdate:
    """Manage existing exposure even when the full entry regime is unavailable.

    Neutral/missing W/D does not request liquidation. Opposite W or D and a
    confirmed H4 structure break do. H1 pivots only tighten a protective guard;
    a proposed guard beyond the known M5 close requests exit instead of an
    invalid protection modification. M5 countertrend alone is not an exit.
    """
    if side not in ("long", "short"):
        raise StrategyDataError("INVALID_SIDE")
    previous = _number(current_guard, "CURRENT_GUARD", positive=True)
    if tick_size is not None:
        frames = {**frames, "tick_size": tick_size}
    loaded, errors = _load_frames(frames, now_ms)
    reasons = []
    target = 1 if side == "long" else -1
    bias = {}
    for frame, lookback in (("W", 4), ("D", 20)):
        if frame in loaded:
            bias[frame] = rolling_bias(loaded[frame], lookback)
            if bias[frame]["direction"] == -target:
                reasons.append(frame + "_BIAS_REVERSED")
    structure = _structure(loaded)
    opposite = "low" if side == "long" else "high"
    crossed = lambda price, guard: price <= guard if side == "long" else price >= guard
    h4 = structure["H4"][opposite]
    if h4 and crossed(loaded["H4"][-1].close, h4["price"]):
        reasons.append("H4_CONFIRMED_STRUCTURE_BREACHED")
    latest = max(((bar[-1].close_ms(FRAME_RULES[frame][0]), bar[-1].close)
                  for frame, bar in loaded.items()), default=(0, None))
    close_ms, price = (loaded["M5"][-1].close_ms(M5_MS), loaded["M5"][-1].close) if "M5" in loaded else latest
    candidate, candidate_available = previous, None
    pivots = structure["H1"][opposite + "s"]
    try:
        tick = _number(frames.get("tick_size"), "TICK_SIZE", positive=True)
    except StrategyDataError:
        tick = None
        errors["tick_size"] = "TICK_SIZE_INVALID"
    if pivots and "M5" in loaded and tick:
        candidate = pivots[-1]["price"] - tick if side == "long" else pivots[-1]["price"] + tick
        candidate_available = pivots[-1]["confirmed_ms"]
        if candidate <= 0:
            candidate = previous
            errors["guard"] = "NONPOSITIVE_PIVOT_GUARD"
    proposed = max(previous, candidate) if side == "long" else min(previous, candidate)
    if "M5" in loaded and crossed(price, previous):
        reasons.append("M5_CLOSE_THROUGH_EXISTING_GUARD")
    elif "M5" in loaded and crossed(price, proposed):
        reasons.append("M5_CLOSE_THROUGH_PROPOSED_GUARD")
    should_exit = bool(reasons)
    guard = previous if should_exit else proposed
    tightened = guard > previous if side == "long" else guard < previous
    return GuardUpdate(side, previous, candidate, guard, close_ms, price, tightened, should_exit,
                       reasons[0] if reasons else ("STRUCTURAL_GUARD_TIGHTENED" if tightened else "STRUCTURAL_GUARD_UNCHANGED"),
                       {"spec_sha256": spec_sha256(), "exit_reasons": reasons, "frame_errors": errors,
                        "bias": bias, "structure": structure,
                        "candidate_confirmed_ms": candidate_available,
                        "guard_applies_after_ms": max(close_ms, candidate_available or 0),
                        "proposed_guard": proposed, "ordinary_M5_countertrend_exit": False})
