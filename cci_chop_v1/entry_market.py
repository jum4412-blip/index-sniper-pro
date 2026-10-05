"""Selected supplemental frames; base structural management stays independent."""
from .market import MarketFrames as BaseMarketFrames, _safe_error
from .strategy import ENTRY_RULE, INDICATOR_FRAMES
from ._compat.core import DataError, SafetyError

INTERVALS = {'H12':'12H','H6':'6H','M30':'30m','M15':'15m','M3':'3m','M1':'1m'}


class MarketFrames(BaseMarketFrames):
    def get_partial(self, symbol, decision_ms):
        result = super().get_partial(symbol, decision_ms)
        for frame in set((ENTRY_RULE['cci_frame'], ENTRY_RULE['chop_frame'])):
            if frame not in INTERVALS:
                continue
            count = max(20 if frame == ENTRY_RULE['cci_frame'] else 0,
                        15 if frame == ENTRY_RULE['chop_frame'] else 0)
            try:
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
