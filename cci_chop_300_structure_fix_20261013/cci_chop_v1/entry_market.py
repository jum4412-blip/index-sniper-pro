"""Selected supplemental frames; base structural management stays independent."""
from .market import MarketFrames as BaseMarketFrames, _safe_error, _closed_rows, aggregate_rows, _tail, H1_MS
from .strategy import ENTRY_RULE, INDICATOR_FRAMES
from ._compat.core import DataError, SafetyError

INTERVALS = {'H12':'12H','H6':'6H','M30':'30m','M15':'15m','M3':'3m','M1':'1m'}


class MarketFrames(BaseMarketFrames):
    def _utc_six_hours(self, symbol, decision_ms, count):
        width = 6 * H1_MS
        key = (symbol, "UTC6H_FROM_1H", count)
        bucket = decision_ms // width
        cached = self._cache.get(key)
        if cached is not None and cached[0] == bucket:
            return [row[:] for row in cached[1]]
        raw = self._read(symbol, "1H", max(100, count * 6 + 12))
        hourly = _closed_rows(raw, H1_MS, decision_ms)
        complete = aggregate_rows(hourly, H1_MS, width, decision_ms)
        result = _tail(complete, width, decision_ms, count)
        if cached is not None:
            previous = {row[0]: row for row in cached[1]}
            if any(row[0] in previous and previous[row[0]] != row for row in result):
                raise DataError("CC_CLOSED_CANDLE_CONFLICT")
        self._cache[key] = (bucket, result)
        return [row[:] for row in result]

    def get_partial(self, symbol, decision_ms):
        result = super().get_partial(symbol, decision_ms)
        for frame in set((ENTRY_RULE['cci_frame'], ENTRY_RULE['chop_frame'])):
            if frame not in INTERVALS:
                continue
            count = max(20 if frame == ENTRY_RULE['cci_frame'] else 0,
                        15 if frame == ENTRY_RULE['chop_frame'] else 0)
            try:
                if frame == "H6":
                    result[frame] = self._utc_six_hours(symbol, decision_ms, count)
                else:
                    result[frame] = self._recent(symbol, INTERVALS[frame],
                                                 INDICATOR_FRAMES[frame][0], decision_ms, count)
                result['_errors'].pop(frame, None)
            except SafetyError as exc:
                result['_errors'][frame] = _safe_error(exc)
        return result

    def get(self, symbol, decision_ms):
        result = self.get_partial(symbol, decision_ms)
        required = {'W','D','H4','H1','M5','tick_size',ENTRY_RULE['cci_frame'],ENTRY_RULE['chop_frame']}
        for key in required:
            if key not in result or key in result['_errors']:
                raise DataError(result['_errors'].get(key,'CC_SELECTED_FRAME_UNAVAILABLE'))
        return {key:result[key] for key in required}
