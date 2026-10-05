"""Read-only, causal UTC market frames for the multi-timeframe hypothesis.

Bitget UTA's documented intervals stop at 1D.  Completed UTC days and Monday
weeks are therefore built from verified, contiguous 4H candles.  A missing bar
is never padded.  All prices of forming/future bars are excluded before OHLCV
validation, so a decision cannot depend on their later values.

MarketFrames.get(symbol, decision_ms) returns ascending numeric OHLCV rows in
W, D, H4, H1 and M5, plus tick_size.  The caller supplies its delayed decision
time, rather than this module silently consulting the machine clock.
"""
from __future__ import annotations

import copy
import math
import re
from collections import defaultdict

from cci_chop_v1._compat.core import CAT, SYMBOLS, DataError, SafetyError


M5_MS = 300_000
H1_MS = 3_600_000
H4_MS = 14_400_000
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
MONDAY_ANCHOR_MS = 4 * DAY_MS  # 1970-01-05 00:00 UTC
H4_WARMUP = 924
MAX_HISTORY_PAGES = 12
RECENT_PATH = "/api/v3/market/candles"
HISTORY_PATH = "/api/v3/market/history-candles"


def _integer(value, name):
    if isinstance(value, bool):
        raise DataError("CC_" + name + "_INVALID")
    try:
        result = int(value)
        if float(value) != result:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise DataError("CC_" + name + "_INVALID") from None
    return result


def _number(value, name, positive=False):
    if isinstance(value, bool):
        raise DataError("CC_" + name + "_INVALID")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise DataError("CC_" + name + "_INVALID") from None
    if not math.isfinite(result) or (positive and result <= 0):
        raise DataError("CC_" + name + "_INVALID")
    return result


def _safe_error(exc):
    message = str(exc)
    if re.fullmatch(r"CC_[A-Z0-9_]+", message):
        return message
    # Never carry an arbitrary transport body or account value into a report.
    return "CC_PUBLIC_READ_FAILED_" + type(exc).__name__.upper()


def _closed_rows(raw, source_ms, now_ms, anchor=0):
    if not isinstance(raw, (list, tuple)):
        raise DataError("CC_CANDLE_ARRAY_MISSING")
    by_time = {}
    for row in raw:
        if not isinstance(row, (list, tuple)) or not row:
            raise DataError("CC_CANDLE_ROW_INVALID")
        stamp = _integer(row[0], "CANDLE_TIME")
        if stamp < 0 or (stamp - anchor) % source_ms:
            raise DataError("CC_CANDLE_UTC_ALIGNMENT_INVALID")
        # Time is the only field inspected for bars unavailable at decision.
        if stamp + source_ms > now_ms:
            continue
        if len(row) < 6:
            raise DataError("CC_CANDLE_ROW_INCOMPLETE")
        op, high, low, close = [_number(x, "CANDLE_PRICE", True) for x in row[1:5]]
        volume = _number(row[5], "CANDLE_VOLUME")
        if volume < 0 or low > min(op, close) or high < max(op, close) or high < low:
            raise DataError("CC_CANDLE_OHLCV_INVALID")
        normalized = [stamp, op, high, low, close, volume]
        if stamp in by_time and by_time[stamp] != normalized:
            raise DataError("CC_CLOSED_CANDLE_CONFLICT")
        by_time[stamp] = normalized
    return [by_time[stamp] for stamp in sorted(by_time)]


def _contiguous(rows, source_ms):
    if any(b[0] - a[0] != source_ms for a, b in zip(rows, rows[1:])):
        raise DataError("CC_CANDLE_GAP")


def _tail(rows, interval_ms, decision_ms, count, anchor=0):
    if len(rows) < count:
        raise DataError("CC_FRAME_WARMUP_INCOMPLETE")
    result = rows[-count:]
    expected = ((decision_ms - anchor) // interval_ms) * interval_ms + anchor - interval_ms
    if result[-1][0] != expected:
        raise DataError("CC_LATEST_CLOSED_CANDLE_MISSING")
    _contiguous(result, interval_ms)
    return [row[:] for row in result]


def aggregate_rows(rows, source_ms, target_ms, now_ms, anchor=0):
    """Aggregate only complete target periods; drop partial edge periods.

    ``anchor`` determines target boundaries (Monday for weeks).  Interior
    source gaps or conflicting closed duplicates raise DataError.  Returned
    rows have six fields and never include an incomplete target period.
    """
    source_ms = _integer(source_ms, "SOURCE_INTERVAL")
    target_ms = _integer(target_ms, "TARGET_INTERVAL")
    now_ms = _integer(now_ms, "DECISION_TIME")
    anchor = _integer(anchor, "ANCHOR")
    if source_ms <= 0 or target_ms < source_ms or target_ms % source_ms or anchor % source_ms or now_ms < 0:
        raise DataError("CC_AGGREGATION_INTERVAL_INVALID")
    closed = _closed_rows(rows, source_ms, now_ms)
    _contiguous(closed, source_ms)
    grouped = defaultdict(list)
    for row in closed:
        start = ((row[0] - anchor) // target_ms) * target_ms + anchor
        grouped[start].append(row)
    result = []
    count = target_ms // source_ms
    for start in sorted(grouped):
        group = grouped[start]
        if start + target_ms > now_ms:
            continue
        if len(group) != count or group[0][0] != start or group[-1][0] + source_ms != start + target_ms:
            # Global continuity above means these can only be edge fragments.
            continue
        result.append([start, group[0][1], max(r[2] for r in group), min(r[3] for r in group), group[-1][4], sum(r[5] for r in group)])
    return result


class MarketFrames:
    """Public GETs only, with closed-bar bucket caches and bounded warmup."""

    def __init__(self, api):
        self.api = api
        self._cache = {}
        self._h4_history = {}
        self._h4_recent_cache = {}
        self._ticks = {}
        self._failures = {}
        self._partial_cache = {}

    def _read(self, symbol, interval, limit, end_time=None):
        params = {"category": CAT, "symbol": symbol, "interval": interval, "type": "market", "limit": str(limit)}
        path = RECENT_PATH
        if end_time is not None:
            path = HISTORY_PATH
            params["endTime"] = str(end_time)
            # A bounded ~17-day request is comfortably under the 90-day maximum.
            params["startTime"] = str(max(0, end_time - 101 * H4_MS))
        return self.api.get(path, params, private=False)

    def _recent(self, symbol, interval, interval_ms, decision_ms, count):
        key = (symbol, interval)
        bucket = decision_ms // interval_ms
        cached = self._cache.get(key)
        if cached is not None and cached[0] == bucket:
            return [row[:] for row in cached[1]]
        failure = self._failures.get(key)
        retry_bucket = decision_ms // M5_MS
        if failure is not None and failure[0] == retry_bucket:
            raise DataError(failure[1])
        try:
            closed = _closed_rows(self._read(symbol, interval, 100), interval_ms, decision_ms)
            result = _tail(closed, interval_ms, decision_ms, count)
            if cached is not None:
                previous = {r[0]: r for r in cached[1]}
                if any(r[0] in previous and previous[r[0]] != r for r in result):
                    raise DataError("CC_CLOSED_CANDLE_CONFLICT")
        except SafetyError as exc:
            message = _safe_error(exc)
            self._failures[key] = (retry_bucket, message)
            raise DataError(message) from None
        self._cache[key] = (bucket, result)
        self._failures.pop(key, None)
        return [row[:] for row in result]

    def _h4(self, symbol, decision_ms):
        key = (symbol, "4H")
        bucket = decision_ms // H4_MS
        cached = self._cache.get(key)
        if cached is not None and cached[0] == bucket:
            return [row[:] for row in cached[1]]
        retry_bucket = decision_ms // M5_MS
        failure = self._failures.get(key)
        if failure is not None and failure[0] == retry_bucket:
            raise DataError(failure[1])
        try:
            result = self._build_h4(symbol, decision_ms)
        except SafetyError as exc:
            message = _safe_error(exc)
            self._failures[key] = (retry_bucket, message)
            # A contradiction or malformed closed evidence cannot be recycled
            # as a supposedly valid shorter frame for managing a position.
            if any(word in message for word in ("CONFLICT", "INVALID", "MISSING")):
                self._h4_recent_cache.pop(symbol, None)
            raise DataError(message) from None
        self._failures.pop(key, None)
        return result

    def _build_h4(self, symbol, decision_ms):
        key = (symbol, "4H")
        bucket = decision_ms // H4_MS
        recent = _closed_rows(self._read(symbol, "4H", 1000), H4_MS, decision_ms)
        # A historical cache must never hide a stale current API response.
        _tail(recent, H4_MS, decision_ms, 1)
        merged = {r[0]: r for r in self._h4_history.get(symbol, []) if r[0] + H4_MS <= decision_ms}

        def merge(batch):
            for row in batch:
                if row[0] in merged and merged[row[0]] != row:
                    raise DataError("CC_CLOSED_CANDLE_CONFLICT")
                merged[row[0]] = row

        merge(recent)
        self._h4_recent_cache[symbol] = (bucket, [row[:] for row in recent])
        # Cached history must not silently heal a gap in a fresh required
        # decision slice.  A shorter, independently contiguous partial slice
        # can still be checked by get_partial below.
        _contiguous(recent[-H4_WARMUP:], H4_MS)
        for page in range(MAX_HISTORY_PAGES + 1):
            ordered = [merged[stamp] for stamp in sorted(merged)]
            needed = ordered[-H4_WARMUP:]
            _contiguous(needed, H4_MS)
            if len(needed) == H4_WARMUP:
                result = _tail(needed, H4_MS, decision_ms, H4_WARMUP)
                self._h4_history[symbol] = [row[:] for row in ordered[-1040:]]
                self._cache[key] = (bucket, result)
                return [row[:] for row in result]
            if page == MAX_HISTORY_PAGES:
                raise DataError("CC_H4_HISTORY_PAGE_LIMIT_INCOMPLETE")
            earliest = ordered[0][0]
            batch = _closed_rows(self._read(symbol, "4H", 100, earliest - 1), H4_MS, decision_ms)
            if not batch or min(r[0] for r in batch) >= earliest:
                raise DataError("CC_H4_HISTORY_COMPLETENESS_UNKNOWN")
            merge(batch)
        raise DataError("CC_H4_HISTORY_COMPLETENESS_UNKNOWN")

    def _tick(self, symbol, decision_ms):
        tick_key = (symbol, decision_ms // H4_MS)
        if tick_key not in self._ticks:
            failure_key = (symbol, "tick_size")
            retry_bucket = decision_ms // M5_MS
            failure = self._failures.get(failure_key)
            if failure is not None and failure[0] == retry_bucket:
                raise DataError(failure[1])
            try:
                instrument = self.api.instrument(symbol)
                value = _number(getattr(instrument, "price_step", None), "TICK_SIZE", True)
            except SafetyError as exc:
                message = _safe_error(exc)
                self._failures[failure_key] = (retry_bucket, message)
                raise DataError(message) from None
            self._ticks[tick_key] = value
            self._ticks = {key: value for key, value in self._ticks.items() if key[0] != symbol or key == tick_key}
            self._failures.pop(failure_key, None)
        return self._ticks[tick_key]

    @staticmethod
    def _decision(symbol, decision_ms):
        if symbol not in SYMBOLS:
            raise DataError("CC_MARKET_SYMBOL_NOT_ALLOWED")
        decision_ms = _integer(decision_ms, "DECISION_TIME")
        if decision_ms < 0:
            raise DataError("CC_DECISION_TIME_INVALID")
        return decision_ms

    def get_partial(self, symbol, decision_ms):
        """Return independently valid evidence for held-position management.

        Missing keys are described by ``_errors: {frame_key: safe_error_code}``.
        Incomplete macro warmup cannot prevent an available M5 guard breach or
        H1 structural exit from being evaluated.  A caller must still require
        every normal frame for a new entry.  Results/failures are cached for a
        five-minute decision bucket to bound repeated failed history reads.
        """
        decision_ms = self._decision(symbol, decision_ms)
        bucket = decision_ms // M5_MS
        cached = self._partial_cache.get(symbol)
        if cached is not None and cached[0] == bucket:
            return copy.deepcopy(cached[1])
        result = {"_errors": {}}
        errors = result["_errors"]
        h4 = None
        try:
            h4 = self._h4(symbol, decision_ms)
        except SafetyError as exc:
            errors["W"] = errors["D"] = errors["H4"] = _safe_error(exc)
            recent = self._h4_recent_cache.get(symbol)
            if recent is not None and recent[0] == decision_ms // H4_MS:
                # This cache contains the fresh public response, not old bars
                # spliced across a missing interval.  Each slice is rechecked.
                h4 = recent[1]
        if h4 is not None:
            try:
                result["H4"] = _tail(h4, H4_MS, decision_ms, 120)
                errors.pop("H4", None)
            except SafetyError as exc:
                errors["H4"] = _safe_error(exc)
            try:
                daily = aggregate_rows(h4, H4_MS, DAY_MS, decision_ms)
            except SafetyError as exc:
                errors["W"] = errors["D"] = _safe_error(exc)
            else:
                try:
                    result["D"] = _tail(daily, DAY_MS, decision_ms, 90)
                    errors.pop("D", None)
                except SafetyError as exc:
                    errors["D"] = _safe_error(exc)
                try:
                    weekly = aggregate_rows(daily, DAY_MS, WEEK_MS, decision_ms, MONDAY_ANCHOR_MS)
                    result["W"] = _tail(weekly, WEEK_MS, decision_ms, 20, MONDAY_ANCHOR_MS)
                    errors.pop("W", None)
                except SafetyError as exc:
                    errors["W"] = _safe_error(exc)
        for key, interval, interval_ms, count in (("H1", "1H", H1_MS, 72), ("M5", "5m", M5_MS, 64)):
            try:
                result[key] = self._recent(symbol, interval, interval_ms, decision_ms, count)
            except SafetyError as exc:
                errors[key] = _safe_error(exc)
        try:
            result["tick_size"] = self._tick(symbol, decision_ms)
        except SafetyError as exc:
            errors["tick_size"] = _safe_error(exc)
        self._partial_cache[symbol] = (bucket, copy.deepcopy(result))
        return result

    def get(self, symbol, decision_ms):
        """Require every actual specification frame, not surplus warmup bars.

        The 924-bar fetch target provides capacity for partial UTC edge weeks.
        It is not an additional strategy condition: fewer contiguous source
        bars can suffice when W20/D90/H4-120/H1-72/M5-64 are all complete.
        """
        frames = self.get_partial(symbol, decision_ms)
        required = ("W", "D", "H4", "H1", "M5", "tick_size")
        for key in required:
            if key not in frames or key in frames["_errors"]:
                raise DataError(frames["_errors"].get(key, "CC_FRAME_WARMUP_INCOMPLETE"))
        return {key: frames[key] for key in required}
