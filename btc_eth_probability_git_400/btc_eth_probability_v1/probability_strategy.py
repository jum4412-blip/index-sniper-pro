"""Causal price/volume events and an auditable frozen probability lookup.

No exchange requests or orders occur in this module.  A price event is a proxy
for crowded levels and failed/accepted breakouts, not an observation of minds.
All event features use completed bars; an event first permits execution one
minute after its five-minute bar ends.  Models learn only resolved, purged,
chronologically thinned historical labels and never update themselves live.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import beta


@dataclass(frozen=True)
class StrategySpec:
    timeframe_minutes: int = 5
    channel_bars: int = 20
    context_bars: int = 96
    range_scale_bars: int = 48
    volume_reference_bars: int = 96
    minimum_stop_fraction: float = 0.0025
    maximum_stop_fraction: float = 0.0060
    wick_buffer_scale: float = 0.03
    sweep_penetration_scale: float = 0.05
    sweep_close_location: float = 0.65
    sweep_wick_fraction: float = 0.25
    breakout_buffer_scale: float = 0.10
    breakout_first_close_location: float = 0.70
    acceptance_close_location: float = 0.55
    acceptance_retest_tolerance_scale: float = 0.25
    target_gross_r: float = 2.0
    max_hold_minutes: int = 60
    execution_delay_minutes: int = 1
    prior_strength: float = 30.0
    minimum_group_labels: int = 300
    minimum_state_labels: int = 80
    minimum_state_days: int = 30
    confidence_quantile: float = 0.05
    required_probability_margin: float = 0.02


SPEC = StrategySpec()
SPEC_DICT = asdict(SPEC)
SPEC_HASH = hashlib.sha256(json.dumps(SPEC_DICT, sort_keys=True).encode()).hexdigest()
MINUTE_MS = 60_000
DAY_MS = 86_400_000
SYMBOLS = ("BTCUSDT", "ETHUSDT")
EVENT_TYPES = ("sweep_reclaim", "breakout_acceptance")
EVENT_COLUMNS = ["symbol", "bar_index", "bar_close_ms", "entry_after_ms", "side",
                 "entry_reference", "stop", "target_reference", "max_hold_minutes",
                 "event_type", "state_key", "group_key", "volume_bin", "context_bin",
                 "quality_bin", "stop_fraction", "structure_valid", "level",
                 "volume_percentile", "close_location", "range_scale", "near_round_level"]


def validate_candles(candles: np.ndarray, timeframe_minutes: int = 5) -> np.ndarray:
    """Require ascending, contiguous OHLCV+turnover [open_ms,O,H,L,C,V,Q]."""
    a = np.asarray(candles, dtype=np.float64)
    if a.ndim != 2 or a.shape[1] != 7:
        raise ValueError("candles must have seven columns [open_ms,O,H,L,C,V,Q]")
    if timeframe_minutes <= 0:
        raise ValueError("timeframe must be positive")
    if not np.isfinite(a).all():
        raise ValueError("nonfinite candles")
    if len(a) == 0:
        return a
    step = timeframe_minutes * MINUTE_MS
    if np.any(a[:, 0] != np.floor(a[:, 0])) or np.any(a[:, 0] % step != 0):
        raise ValueError("candle timestamps must be UTC-aligned opening milliseconds")
    if len(a) > 1 and np.any(np.diff(a[:, 0]) != step):
        raise ValueError("candle gaps, duplicate bars or out-of-order timestamps")
    if np.any(a[:, 1:5] <= 0) or np.any(a[:, 5:] < 0):
        raise ValueError("invalid price or volume")
    if np.any(a[:, 2] < a[:, [1, 3, 4]].max(axis=1)) or np.any(a[:, 3] > a[:, [1, 2, 4]].min(axis=1)):
        raise ValueError("OHLC geometry invalid")
    return a


def aggregate_candles(minute_candles: np.ndarray, timeframe_minutes: int = 5,
                      now_ms: int | None = None) -> np.ndarray:
    """Aggregate exact complete minute groups; never use an unfinished bar."""
    a = validate_candles(minute_candles, 1)
    if timeframe_minutes != SPEC.timeframe_minutes:
        raise ValueError("the frozen strategy uses five-minute bars")
    if not len(a):
        return a.copy()
    tfms = timeframe_minutes * MINUTE_MS
    if now_ms is not None:
        a = a[a[:, 0] + MINUTE_MS <= int(now_ms)]
    if not len(a):
        return np.empty((0, 7), dtype=np.float64)
    start = int((-int(a[0, 0]) // MINUTE_MS) % timeframe_minutes)
    a = a[start:]
    count = len(a) // timeframe_minutes
    if not count:
        return np.empty((0, 7), dtype=np.float64)
    g = a[:count * timeframe_minutes].reshape(count, timeframe_minutes, 7)
    result = np.column_stack((g[:, 0, 0], g[:, 0, 1], g[:, :, 2].max(axis=1),
                              g[:, :, 3].min(axis=1), g[:, -1, 4],
                              g[:, :, 5].sum(axis=1), g[:, :, 6].sum(axis=1)))
    if now_ms is not None:
        result = result[result[:, 0] + tfms <= int(now_ms)]
    return validate_candles(result, timeframe_minutes)


def _rolling_prior(a: np.ndarray, n: int, operation: str) -> np.ndarray:
    roll = pd.Series(a).shift(1).rolling(n, min_periods=n)
    return getattr(roll, operation)().to_numpy()


def _prior_percentile(values: np.ndarray, n: int) -> np.ndarray:
    """Current value's midrank against prior n values, excluding current value."""
    result = np.full(len(values), np.nan)
    if len(values) <= n:
        return result
    windows = np.lib.stride_tricks.sliding_window_view(values, n)[:-1]
    # Bound temporary memory on the multi-year sample.
    for start in range(0, len(windows), 32_768):
        stop = min(start + 32_768, len(windows))
        previous = windows[start:stop]
        current = values[n + start:n + stop, None]
        result[n + start:n + stop] = ((previous < current).sum(axis=1)
                                      + .5 * (previous == current).sum(axis=1)) / n
    return result


def group_key(symbol: str, event_type: str, side: int) -> str:
    if symbol not in SYMBOLS or event_type not in EVENT_TYPES or side not in (-1, 1):
        raise ValueError("unknown event group")
    return f"{symbol}|{event_type}|{'L' if side == 1 else 'S'}"


def state_key(symbol: str, event_type: str, side: int, volume_bin: int,
              context_bin: int, quality_bin: int) -> str:
    if volume_bin not in (0, 1, 2) or context_bin not in (0, 1, 2) or quality_bin not in (0, 1):
        raise ValueError("unknown feature bucket")
    return f"{group_key(symbol, event_type, side)}|v{volume_bin}|c{context_bin}|q{quality_bin}"


def build_events(symbol: str, candles: np.ndarray, timeframe_minutes: int = 5,
                 *, now_ms: int | None = None) -> pd.DataFrame:
    """Build the two frozen event types from completed, aligned five-minute bars.

    Rows include wide-stop events for honest event diagnostics.  Such rows have
    structure_valid=False and cannot be traded.  One unambiguous event per bar
    is permitted; contradictory events are discarded.  target_reference is a
    diagnostic value; execution must recompute 2R from the actual entry fill.
    """
    if symbol not in SYMBOLS or timeframe_minutes != SPEC.timeframe_minutes:
        raise ValueError("strategy supports BTCUSDT/ETHUSDT on five-minute bars")
    a = validate_candles(candles, timeframe_minutes)
    tfms = timeframe_minutes * MINUTE_MS
    if now_ms is not None:
        a = a[a[:, 0] + tfms <= int(now_ms)]
    if len(a) <= max(SPEC.context_bars, SPEC.volume_reference_bars):
        return pd.DataFrame(columns=EVENT_COLUMNS)
    ts, op, hi, lo, cl, vol, turnover = a.T
    n = len(a)
    scale = _rolling_prior(hi - lo, SPEC.range_scale_bars, "median")
    high = _rolling_prior(hi, SPEC.channel_bars, "max")
    low = _rolling_prior(lo, SPEC.channel_bars, "min")
    wide_high = _rolling_prior(hi, SPEC.context_bars, "max")
    wide_low = _rolling_prior(lo, SPEC.context_bars, "min")
    # Use exchange base-volume column 5 in research and live.  Quote turnover
    # may be absent in a live candle payload; it never changes event features.
    v_rank = _prior_percentile(vol, SPEC.volume_reference_bars)
    candle_range = hi - lo
    close_location = np.divide(cl - lo, candle_range, out=np.full(n, .5), where=candle_range > 0)
    lower_wick = np.divide(np.minimum(op, cl) - lo, candle_range, out=np.zeros(n), where=candle_range > 0)
    upper_wick = np.divide(hi - np.maximum(op, cl), candle_range, out=np.zeros(n), where=candle_range > 0)
    context = np.divide(cl - wide_low, wide_high - wide_low, out=np.full(n, .5), where=wide_high > wide_low)
    context = np.clip(context, 0, 1)
    known = np.isfinite(scale) & (scale > 0) & np.isfinite(v_rank) & np.isfinite(context)
    known[:SPEC.context_bars] = False
    sweep_long = known & (lo < low - SPEC.sweep_penetration_scale * scale) & (cl > low + SPEC.sweep_penetration_scale * scale) & (close_location >= SPEC.sweep_close_location) & (lower_wick >= SPEC.sweep_wick_fraction)
    sweep_short = known & (hi > high + SPEC.sweep_penetration_scale * scale) & (cl < high - SPEC.sweep_penetration_scale * scale) & (close_location <= 1 - SPEC.sweep_close_location) & (upper_wick >= SPEC.sweep_wick_fraction)
    first_long = known & (cl > high + SPEC.breakout_buffer_scale * scale) & (close_location >= SPEC.breakout_first_close_location) & (cl > op)
    first_short = known & (cl < low - SPEC.breakout_buffer_scale * scale) & (close_location <= 1 - SPEC.breakout_first_close_location) & (cl < op)
    previous_first_long = np.r_[False, first_long[:-1]]
    previous_first_short = np.r_[False, first_short[:-1]]
    frozen_high = np.r_[np.nan, high[:-1]]
    frozen_low = np.r_[np.nan, low[:-1]]
    frozen_scale = np.r_[np.nan, scale[:-1]]
    accept_long = known & previous_first_long & (cl > frozen_high + SPEC.breakout_buffer_scale * frozen_scale) & (lo >= frozen_high - SPEC.acceptance_retest_tolerance_scale * frozen_scale) & (close_location >= SPEC.acceptance_close_location) & (cl >= op)
    accept_short = known & previous_first_short & (cl < frozen_low - SPEC.breakout_buffer_scale * frozen_scale) & (hi <= frozen_low + SPEC.acceptance_retest_tolerance_scale * frozen_scale) & (close_location <= 1 - SPEC.acceptance_close_location) & (cl <= op)
    flags = np.column_stack((sweep_long, sweep_short, accept_long, accept_short))
    indices = np.flatnonzero(flags.sum(axis=1) == 1)
    rows: list[dict[str, Any]] = []
    for i in indices:
        kind_index = int(np.flatnonzero(flags[i])[0])
        event_type = "sweep_reclaim" if kind_index < 2 else "breakout_acceptance"
        side = 1 if kind_index in (0, 2) else -1
        level = (low[i] if side == 1 else high[i]) if kind_index < 2 else (frozen_high[i] if side == 1 else frozen_low[i])
        if kind_index < 2:
            stop = lo[i] - SPEC.wick_buffer_scale * scale[i] if side == 1 else hi[i] + SPEC.wick_buffer_scale * scale[i]
            quality = lower_wick[i] if side == 1 else upper_wick[i]
            quality_bin = int(quality >= .45)
        else:
            stop = min(lo[i], level - .15 * scale[i]) - SPEC.wick_buffer_scale * scale[i] if side == 1 else max(hi[i], level + .15 * scale[i]) + SPEC.wick_buffer_scale * scale[i]
            body_fraction = abs(cl[i] - op[i]) / candle_range[i] if candle_range[i] > 0 else 0.
            direction_close = close_location[i] if side == 1 else 1 - close_location[i]
            quality_bin = int(direction_close >= .8 and body_fraction >= .5)
        # Moving a narrow stop farther away preserves the observed structural
        # extremum.  A wide structural stop is never moved closer to force size.
        stop = min(stop, cl[i] * (1 - SPEC.minimum_stop_fraction)) if side == 1 else max(stop, cl[i] * (1 + SPEC.minimum_stop_fraction))
        stop_fraction = side * (cl[i] - stop) / cl[i]
        volume_bin = int(np.searchsorted((.5, .8), v_rank[i], side="right"))
        directional_context = context[i] if side == 1 else 1 - context[i]
        context_bin = int(np.searchsorted((1/3, 2/3), directional_context, side="right"))
        round_step = 10.0 ** (math.floor(math.log10(cl[i])) - 2)
        near_round = abs(level - round(level / round_step) * round_step) <= .2 * scale[i]
        close_ms = int(ts[i]) + tfms
        rows.append({"symbol": symbol, "bar_index": int(i), "bar_close_ms": close_ms,
                     "entry_after_ms": close_ms + SPEC.execution_delay_minutes * MINUTE_MS,
                     "side": side, "entry_reference": float(cl[i]), "stop": float(stop),
                     "target_reference": float(cl[i] + side * SPEC.target_gross_r * abs(cl[i] - stop)),
                     "max_hold_minutes": SPEC.max_hold_minutes, "event_type": event_type,
                     "state_key": state_key(symbol, event_type, side, volume_bin, context_bin, quality_bin),
                     "group_key": group_key(symbol, event_type, side), "volume_bin": volume_bin,
                     "context_bin": context_bin, "quality_bin": quality_bin,
                     "stop_fraction": float(stop_fraction),
                     "structure_valid": bool(SPEC.minimum_stop_fraction - 1e-12 <= stop_fraction <= SPEC.maximum_stop_fraction + 1e-12),
                     "level": float(level), "volume_percentile": float(v_rank[i]),
                     "close_location": float(close_location[i]), "range_scale": float(scale[i]),
                     "near_round_level": bool(near_round)})
    return pd.DataFrame(rows, columns=EVENT_COLUMNS)


def nonoverlapping_training_labels(labels: pd.DataFrame, training_cut_ms: int,
                                   purge_ms: int = 3_600_000,
                                   training_start_ms: int | None = None) -> pd.DataFrame:
    """Only closed labels, then greedily retain nonoverlapping exposures per asset.

    This reduces mechanical dependence; temporal market dependence remains and
    is addressed conservatively with UTC-day clustered uncertainty estimates.
    Raw event count is not the number of independent observations.
    """
    required = {"symbol", "entry_after_ms", "label_end_ms", "net_r", "label_profitable",
                "event_type", "side", "state_key", "group_key"}
    absent = required - set(labels.columns)
    if absent:
        raise ValueError(f"labels missing columns {sorted(absent)}")
    if purge_ms < SPEC.max_hold_minutes * MINUTE_MS:
        raise ValueError("purge must cover the full sixty-minute label horizon")
    a = labels.loc[labels.label_end_ms < int(training_cut_ms) - int(purge_ms)].copy()
    if training_start_ms is not None:
        a = a.loc[a.entry_after_ms >= int(training_start_ms)].copy()
    if "structure_valid" in a:
        a = a.loc[a.structure_valid.astype(bool)]
    if "execution_eligible" in a:
        # A rejected order is not a flat losing trade or an outcome sample.
        a = a.loc[a.execution_eligible == 1]
    finite = np.isfinite(a.net_r.to_numpy(dtype=float))
    a = a.loc[finite]
    if len(a):
        if (a.label_end_ms < a.entry_after_ms).any():
            raise ValueError("label closes before entry")
        if (a.label_profitable.astype(bool) != (a.net_r > 0)).any():
            raise ValueError("profitable labels must describe net profitability")
        if not a.symbol.isin(SYMBOLS).all():
            raise ValueError("unexpected label symbol")
    # Input labels may combine assets whose original DataFrame indices overlap.
    # Stable positional indices prevent .loc[keep] from multiplying those rows.
    a = a.sort_values(["symbol", "entry_after_ms", "event_type", "side"], kind="stable").reset_index(drop=True)
    keep = []
    last_end: dict[str, int] = {}
    for index, row in a.iterrows():
        entry = int(row.entry_after_ms)
        if entry > last_end.get(row.symbol, -1):
            keep.append(index)
            last_end[row.symbol] = int(row.label_end_ms)
    result = a.loc[keep].copy()
    result["cluster_day"] = (result.entry_after_ms // DAY_MS).astype(np.int64)
    return result


def _cluster_standard_error(values: np.ndarray, days: np.ndarray) -> float:
    if len(values) < 2:
        return float("inf")
    mean = float(np.mean(values))
    unique, indices = np.unique(days, return_inverse=True)
    if len(unique) < 2:
        return float("inf")
    centered_sums = np.bincount(indices, weights=values - mean)
    cluster_se = math.sqrt(len(unique) / (len(unique) - 1) * float(np.sum(centered_sums ** 2))) / len(values)
    # Never let a fortuitous cluster cancellation shrink below IID uncertainty.
    iid_se = float(np.std(values, ddof=1)) / math.sqrt(len(values))
    return max(cluster_se, iid_se)


def _sample_summary(data: pd.DataFrame) -> dict[str, Any]:
    r = data.net_r.to_numpy(dtype=float)
    profitable = (r > 0).astype(float)
    days = data.cluster_day.to_numpy(dtype=np.int64)
    win = r[r > 0]
    loss = -r[r <= 0]
    return {"n": int(len(r)), "wins": int(np.sum(profitable)), "days": int(len(np.unique(days))),
            "mean_net_r": float(np.mean(r)), "net_r_se": _cluster_standard_error(r, days),
            "win_probability_se": _cluster_standard_error(profitable, days),
            "average_net_win_r": float(np.mean(win)) if len(win) else 0.,
            "average_net_loss_r": float(np.mean(loss)) if len(loss) else 0.}


def fit_probability_model(label_df: pd.DataFrame, training_cut_ms: int,
                          purge_ms: int = 3_600_000,
                          *, training_start_ms: int | None = None,
                          min_group: int = SPEC.minimum_group_labels,
                          min_leaf: int = SPEC.minimum_state_labels) -> dict[str, Any]:
    """Train one JSON-safe empirical-Bayes lookup from historical net-R labels.

    Probability posterior: leaf successes/failures plus 30 observations centered
    on its asset/event/direction group's smoothed rate.  Leaf means and average
    net wins/losses shrink toward the same group.  Lower bounds use the worse
    of the beta 5% quantile and a day-cluster uncertainty approximation.  These
    are conservative diagnostics, not a statistical guarantee of future edge.
    """
    if min_group != SPEC.minimum_group_labels or min_leaf != SPEC.minimum_state_labels:
        raise ValueError("minimum support is frozen at group300/state80")
    data = nonoverlapping_training_labels(label_df, training_cut_ms, purge_ms, training_start_ms)
    groups: dict[str, Any] = {}
    states: dict[str, Any] = {}
    z = 1.6448536269514722
    for key, frame in data.groupby("group_key", sort=True):
        groups[str(key)] = _sample_summary(frame)
    for key, frame in data.groupby("state_key", sort=True):
        keys = frame.group_key.unique()
        if len(keys) != 1:
            raise ValueError("state mixes different event groups")
        parent = groups[str(keys[0])]
        leaf = _sample_summary(frame)
        parent_probability = (parent["wins"] + 1.) / (parent["n"] + 2.)
        strength = SPEC.prior_strength
        alpha = leaf["wins"] + strength * parent_probability
        beta_parameter = leaf["n"] - leaf["wins"] + strength * (1 - parent_probability)
        p_mean = alpha / (alpha + beta_parameter)
        p_beta05 = float(beta.ppf(SPEC.confidence_quantile, alpha, beta_parameter))
        p_se = max(leaf["win_probability_se"], parent["win_probability_se"])
        p_cluster05 = max(0., p_mean - z * p_se)
        p05 = min(p_beta05, p_cluster05)
        total = leaf["n"] + strength
        mean_r = (leaf["n"] * leaf["mean_net_r"] + strength * parent["mean_net_r"]) / total
        uncertainty = max(leaf["net_r_se"], parent["net_r_se"])
        lower95 = mean_r - z * uncertainty
        win_r = (leaf["n"] * leaf["average_net_win_r"] + strength * parent["average_net_win_r"]) / total
        loss_r = (leaf["n"] * leaf["average_net_loss_r"] + strength * parent["average_net_loss_r"]) / total
        break_even = loss_r / (win_r + loss_r) if win_r > 0 and loss_r > 0 else 1.
        supported = (parent["n"] >= min_group and leaf["n"] >= min_leaf
                     and leaf["days"] >= SPEC.minimum_state_days)
        eligible = supported and p05 >= break_even + SPEC.required_probability_margin and lower95 > 0
        reason = "EDGE_GATE_PASSED" if eligible else ("INSUFFICIENT_SUPPORT" if not supported else "UNCERTAIN_OR_NEGATIVE_NET_EDGE")
        states[str(key)] = {**leaf, "group_key": str(keys[0]), "n_group": parent["n"],
                            "n_state": leaf["n"], "alpha": alpha, "beta": beta_parameter,
                            "p_mean": p_mean, "p05": p05, "p_beta05": p_beta05,
                            "p_cluster05": p_cluster05, "break_even": break_even,
                            "expected_net_r_mean": mean_r, "lower95": lower95,
                            "average_shrunken_net_win_r": win_r,
                            "average_shrunken_net_loss_r": loss_r,
                            "eligible": bool(eligible), "reason": reason}
    # Reject NaN and infinities rather than serializing a model live cannot use.
    result = {"schema_version": 1, "strategy_spec": SPEC_DICT, "strategy_spec_hash": SPEC_HASH,
              "training_cut_ms": int(training_cut_ms), "purge_ms": int(purge_ms),
              "training_start_ms": int(training_start_ms) if training_start_ms is not None else None,
              "raw_label_count": int(len(label_df)), "fit_label_count": int(len(data)),
              "latest_label_end_ms": int(data.label_end_ms.max()) if len(data) else None,
              "independence_note": "nonoverlap thinning per asset; temporal dependence remains; UTC-day clustered lower bounds",
              "groups": groups, "states": states}
    # Unsupported tiny states can carry undefined standard errors; replace their
    # confidence values with finite fail-closed sentinels before writing JSON.
    for collection in (groups, states):
        for record in collection.values():
            for field, value in list(record.items()):
                if isinstance(value, float) and not math.isfinite(value):
                    record[field] = 1e9 if field.endswith("se") else (-1e9 if field == "lower95" else 0.)
            if collection is states and (record["n_state"] < min_leaf or record["days"] < SPEC.minimum_state_days):
                record["eligible"] = False
                record["reason"] = "INSUFFICIENT_SUPPORT"
    json.dumps(result, allow_nan=False)
    return result


def validate_model(model: dict[str, Any]) -> None:
    if model.get("schema_version") != 1 or model.get("strategy_spec_hash") != SPEC_HASH or model.get("strategy_spec") != SPEC_DICT:
        raise ValueError("model and strategy protocol mismatch")
    if not isinstance(model.get("states"), dict) or not isinstance(model.get("groups"), dict):
        raise ValueError("model lookup missing")
    json.dumps(model, allow_nan=False)


def predict_event(event: dict[str, Any] | pd.Series, model: dict[str, Any]) -> dict[str, Any]:
    """Pure lookup; execution checks price, costs, funds, guards and account state."""
    # Full structural/JSON validation is done once at file loading, not once
    # for each of tens of thousands of research events.
    if model.get("schema_version") != 1 or model.get("strategy_spec_hash") != SPEC_HASH:
        raise ValueError("model and strategy protocol mismatch")
    event = dict(event)
    key = state_key(str(event["symbol"]), str(event["event_type"]), int(event["side"]),
                    int(event["volume_bin"]), int(event["context_bin"]), int(event["quality_bin"]))
    if event.get("state_key") != key:
        raise ValueError("event state key disagrees with causal feature buckets")
    entry = float(event["entry_reference"])
    stop = float(event["stop"])
    fraction = int(event["side"]) * (entry - stop) / entry if entry > 0 else -1.
    if not math.isfinite(fraction) or not SPEC.minimum_stop_fraction - 1e-12 <= fraction <= SPEC.maximum_stop_fraction + 1e-12:
        return {"eligible": False, "reason": "STRUCTURAL_STOP_OUTSIDE_FROZEN_RANGE", "state_key": key,
                "n_group": 0, "n_state": 0, "p_mean": None, "p05": None,
                "expected_net_r_mean": None, "lower95": None, "break_even": None}
    leaf = model["states"].get(key)
    if leaf is None:
        return {"eligible": False, "reason": "UNKNOWN_STATE", "state_key": key,
                "n_group": 0, "n_state": 0, "p_mean": None, "p05": None,
                "expected_net_r_mean": None, "lower95": None, "break_even": None}
    return {"state_key": key, **leaf}


def score_event(model: dict[str, Any], event: dict[str, Any] | pd.Series) -> dict[str, Any]:
    """Compatibility alias, retaining the model-first research interface."""
    return predict_event(event, model)


def latest_event(symbol: str, candles: np.ndarray, *, now_ms: int) -> dict[str, Any] | None:
    """Return only an event on the newest complete bar, never replay an old one."""
    closed = validate_candles(candles, SPEC.timeframe_minutes)
    closed = closed[closed[:, 0] + SPEC.timeframe_minutes * MINUTE_MS <= now_ms]
    if not len(closed):
        return None
    events = build_events(symbol, closed)
    if len(events) and int(events.iloc[-1].bar_index) == len(closed) - 1:
        return events.iloc[-1].to_dict()
    return None
