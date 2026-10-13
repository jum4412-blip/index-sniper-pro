"""Frozen multi-timeframe entry with selected-frame CCI20 AND CHOP14.

The two supplemental filters apply only to new entries. They cannot loosen
protection or turn an existing position into a discretionary indicator exit.
The structural management rules remain those of ``base_strategy``. This pure
module creates no exchange orders and makes no profitability assertion.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import base_strategy as _base
from .base_strategy import (
    Candle, DAY_MS, FRAME_RULES, GuardUpdate, H4_MS, HOUR_MS, M5_MS, Pivot,
    Signal, StrategyDataError, SYMBOLS, WEEK_ANCHOR_MS, WEEK_MS,
    completed_candles, confirmed_pivots, rolling_bias,
)

CCI_PERIOD = 20
CCI_THRESHOLD = 100.0
CHOP_PERIOD = 14
CHOP_MAXIMUM = 38.2
INDICATOR_FRAMES = {
    "W": (WEEK_MS, WEEK_ANCHOR_MS), "D": (DAY_MS, 0),
    "H12": (12 * HOUR_MS, 0), "H6": (6 * HOUR_MS, 0),
    "H4": (H4_MS, 0), "H1": (HOUR_MS, 0),
    "M30": (30 * 60_000, 0), "M15": (15 * 60_000, 0),
    "M5": (M5_MS, 0), "M3": (3 * 60_000, 0), "M1": (60_000, 0),
}


def _read_entry_rule() -> dict[str, Any]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise StrategyDataError("DUPLICATE_ENTRY_RULE_KEY")
            result[key] = value
        return result

    try:
        rule = json.loads(Path(__file__).with_name("entry_rule.json").read_text(encoding="utf-8"),
                          object_pairs_hook=unique)
    except (OSError, ValueError) as exc:
        raise StrategyDataError("ENTRY_RULE_UNAVAILABLE_OR_INVALID") from exc
    if not isinstance(rule, dict):
        raise StrategyDataError("ENTRY_RULE_INVALID")
    frozen = {"cci_period": CCI_PERIOD, "cci_long": CCI_THRESHOLD,
              "cci_short": -CCI_THRESHOLD, "chop_period": CHOP_PERIOD,
              "chop_max": CHOP_MAXIMUM}
    for key, expected in frozen.items():
        value = rule.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != expected:
            raise StrategyDataError("ENTRY_RULE_FIXED_THRESHOLD_MISMATCH")
    for key in ("cci_frame", "chop_frame"):
        if not isinstance(rule.get(key), str) or rule[key] not in INDICATOR_FRAMES:
            raise StrategyDataError("ENTRY_RULE_FRAME_NOT_SUPPORTED")
    return {"cci_frame": rule["cci_frame"], "chop_frame": rule["chop_frame"], **frozen}


ENTRY_RULE = _read_entry_rule()  # Frozen once per process; edits require a new process.
BASE_SPEC_SHA256 = _base.spec_sha256()
SPEC = deepcopy(_base.SPEC)
SPEC.update({
    "name": "W_D_H4_H1_M5_CCI20_CHOP14_V1",
    "base_spec_sha256": BASE_SPEC_SHA256,
    "supplemental_entry_filters": {
        "cci_frame": ENTRY_RULE["cci_frame"],
        "chop_frame": ENTRY_RULE["chop_frame"],
        "frame_rule": "latest_completed_selected_frame_UTC_aligned_no_gaps_no_future_bars",
        "combination": "CCI_AND_CHOP_AND_ALL_BASE_ENTRY_CONDITIONS",
        "cci": {
            "period": CCI_PERIOD,
            "typical_price": "(high+low+close)/3",
            "formula": "(latest_TP-mean20TP)/(0.015*mean20_abs_TP_minus_same_mean)",
            "long_minimum_inclusive": CCI_THRESHOLD,
            "short_maximum_inclusive": -CCI_THRESHOLD,
        },
        "chop": {
            "period": CHOP_PERIOD,
            "true_range": "max(high-low,abs(high-previous_close),abs(low-previous_close))",
            "formula": "100*log10(sum14TR/(max14High-min14Low))/log10(14)",
            "maximum_inclusive": CHOP_MAXIMUM,
            "preceding_close_required": True,
        },
        "flat_zero_nonfinite": "deny_new_entry_without_fabricating_a_value",
        "entry_only": True,
        "indicator_reversal_is_exit": False,
        "management": "unchanged_base_structural_guards_and_exits",
    },
    "measured_probability": "not_provided_requires_new_joint_filter_frozen_calibration",
    "individual_indicator_results_are_joint_results": False,
})


def spec_sha256() -> str:
    return hashlib.sha256(json.dumps(SPEC, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _finite(values: Iterable[float]) -> bool:
    return all(math.isfinite(value) for value in values)


def cci20(bars: Iterable[Candle]) -> float | None:
    """Latest CCI on exactly 20 validated bars; zero deviation is unavailable."""
    window = tuple(bars)[-CCI_PERIOD:]
    if len(window) != CCI_PERIOD:
        return None
    typical = tuple((bar.high + bar.low + bar.close) / 3.0 for bar in window)
    if not _finite(typical):
        return None
    mean = math.fsum(typical) / CCI_PERIOD
    deviation = math.fsum(abs(value - mean) for value in typical) / CCI_PERIOD
    if not math.isfinite(mean) or not math.isfinite(deviation) or deviation <= 0:
        return None
    denominator = 0.015 * deviation
    if not math.isfinite(denominator) or denominator <= 0:
        return None
    value = (typical[-1] - mean) / denominator
    return value if math.isfinite(value) else None


def chop14(bars: Iterable[Candle]) -> float | None:
    """Latest CHOP requires 14 bars PLUS the close preceding their first bar."""
    window = tuple(bars)[-(CHOP_PERIOD + 1):]
    if len(window) != CHOP_PERIOD + 1:
        return None
    active = window[1:]
    tr = tuple(max(bar.high - bar.low, abs(bar.high - previous.close),
                   abs(bar.low - previous.close))
               for previous, bar in zip(window, active))
    price_range = max(bar.high for bar in active) - min(bar.low for bar in active)
    if not _finite(tr) or not math.isfinite(price_range) or price_range <= 0:
        return None
    ratio = math.fsum(tr) / price_range
    if not math.isfinite(ratio) or ratio <= 0:
        return None
    value = 100.0 * math.log10(ratio) / math.log10(CHOP_PERIOD)
    return value if math.isfinite(value) else None


def _indicator_window(frames: Mapping[str, Any], now_ms: int, frame: str,
                      count: int) -> tuple[Candle, ...]:
    duration, anchor = INDICATOR_FRAMES[frame]
    now = _base._integer(now_ms, "NOW_MS")
    if not isinstance(frames, Mapping) or frame not in frames or frames[frame] is None:
        raise StrategyDataError("MISSING_FRAME")
    bars = completed_candles(frames[frame], now, duration, anchor, max_count=count)
    if len(bars) != count:
        raise StrategyDataError("INSUFFICIENT_HISTORY")
    expected = (now - anchor) // duration * duration + anchor
    if bars[-1].close_ms(duration) != expected:
        raise StrategyDataError("LATEST_COMPLETE_CANDLE_MISSING")
    if any(right.open_ms - left.open_ms != duration for left, right in zip(bars, bars[1:])):
        raise StrategyDataError("NONCONTIGUOUS_DECISION_WINDOW")
    return bars


def indicator_evidence(frames: Mapping[str, Any], now_ms: int) -> dict[str, Any]:
    """Causal filter evidence on each independently selected, completed frame."""
    cci_frame, chop_frame = ENTRY_RULE["cci_frame"], ENTRY_RULE["chop_frame"]
    result = {
        "frame": cci_frame if cci_frame == chop_frame else None,
        "cci_frame": cci_frame, "chop_frame": chop_frame,
        "cci20": None, "chop14": None,
        "cci_open_ms": None, "cci_closed_ms": None,
        "chop_open_ms": None, "chop_closed_ms": None,
        "open_ms": None, "closed_ms": None, "valid": False,
        "long": False, "short": False, "reason": None,
        "entry_only": True, "cci_long_minimum": CCI_THRESHOLD,
        "cci_short_maximum": -CCI_THRESHOLD, "chop_maximum": CHOP_MAXIMUM,
    }
    failures = {}
    for label, frame, count, calculation in (
            ("cci", cci_frame, CCI_PERIOD, cci20),
            ("chop", chop_frame, CHOP_PERIOD + 1, chop14)):
        try:
            bars = _indicator_window(frames, now_ms, frame, count)
        except (StrategyDataError, TypeError) as exc:
            failures[label] = str(exc) if isinstance(exc, StrategyDataError) else "FRAME_NOT_ITERABLE"
            continue
        result[label + "_open_ms"] = bars[-1].open_ms
        result[label + "_closed_ms"] = bars[-1].close_ms(INDICATOR_FRAMES[frame][0])
        result["cci20" if label == "cci" else "chop14"] = calculation(bars)
    if cci_frame == chop_frame:
        result["open_ms"] = result["cci_open_ms"]
        result["closed_ms"] = result["cci_closed_ms"]
    if failures:
        result["reason"] = "CCI_CHOP_SELECTED_FRAME_DATA_UNAVAILABLE"
        result["data_errors"] = failures
        return result
    if result["cci20"] is None or result["chop14"] is None:
        result["reason"] = "CCI_CHOP_DEGENERATE_SELECTED_FRAME"
        return result
    result["valid"] = True
    chop_pass = result["chop14"] <= CHOP_MAXIMUM
    result["long"] = chop_pass and result["cci20"] >= CCI_THRESHOLD
    result["short"] = chop_pass and result["cci20"] <= -CCI_THRESHOLD
    return result


def analyze(frames: Mapping[str, Any], now_ms: int, tick_size: Any = None) -> dict[str, Any]:
    result = _base.analyze(frames, now_ms, tick_size)
    indicator = indicator_evidence(frames, now_ms)
    reasons = list(result["wait_reasons"])
    side = result["side"]
    if not indicator["valid"]:
        reasons.append(indicator["reason"])
    elif side is not None:
        if (side == "long" and indicator["cci20"] < CCI_THRESHOLD) or (
                side == "short" and indicator["cci20"] > -CCI_THRESHOLD):
            reasons.append("CCI20_LONG_NOT_CONFIRMED" if side == "long" else
                           "CCI20_SHORT_NOT_CONFIRMED")
        if indicator["chop14"] > CHOP_MAXIMUM:
            reasons.append("CHOP14_NOT_TRENDING")
    result.update({"spec_sha256": spec_sha256(), "base_spec_sha256": BASE_SPEC_SHA256,
                   "eligible": bool(result["eligible"] and indicator.get(side, False)),
                   "wait_reasons": reasons, "indicators": indicator})
    return result


def generate_signal(symbol: str, frames: Mapping[str, Any], now_ms: int,
                    tick_size: Any = None) -> Signal | None:
    if symbol not in SYMBOLS:
        raise StrategyDataError("UNSUPPORTED_SYMBOL")
    result = analyze(frames, now_ms, tick_size)
    if not result["eligible"]:
        return None
    base_signal = _base.generate_signal(symbol, frames, now_ms, tick_size)
    if base_signal is None:
        return None
    metadata = {**base_signal.metadata, "spec_sha256": spec_sha256(),
                "base_spec_sha256": BASE_SPEC_SHA256,
                "indicators": result["indicators"],
                "supplemental_entry_filters": dict(ENTRY_RULE),
                "individual_indicator_results_are_joint_results": False,
                "probability_reason": "UNMEASURED_JOINT_CCI_CHOP_REQUIRES_FROZEN_NET_OUTCOME_CALIBRATION"}
    identity = f"{spec_sha256()}|{symbol}|{base_signal.side}|{base_signal.closed_ms}"
    return replace(base_signal, event_id=hashlib.sha256(identity.encode()).hexdigest(),
                   metadata=metadata)


def context(frames: Mapping[str, Any], now_ms: int, tick_size: Any = None) -> dict[str, Any]:
    """Keep base management context; filters never alter guards or exits."""
    result = _base.context(frames, now_ms, tick_size)
    result.update({"spec_sha256": spec_sha256(), "base_spec_sha256": BASE_SPEC_SHA256,
                   "indicators": indicator_evidence(frames, now_ms),
                   "entry_filters_apply_to_exit": False})
    return result


def update_guard(side: str, current_guard: float, frames: Mapping[str, Any], now_ms: int,
                 tick_size: Any = None) -> GuardUpdate:
    result = _base.update_guard(side, current_guard, frames, now_ms, tick_size)
    return replace(result, metadata={**result.metadata, "spec_sha256": spec_sha256(),
                                    "base_spec_sha256": BASE_SPEC_SHA256,
                                    "entry_filters_apply_to_exit": False})
