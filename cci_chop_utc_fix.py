from pathlib import Path
import fcntl
import shutil
import time
from cci_chop_v1.cli import single_runner, read_status

root = Path.cwd()
with single_runner(root):
    state = read_status(root, "live")
    if any(state.get(k) for k in ("positions", "pending", "modifications")):
        raise SystemExit("보유 포지션 또는 미확정 주문이 있습니다. 기존 코드로 먼저 정산하세요.")
    market = root / "cci_chop_v1/market.py"
    entry = root / "cci_chop_v1/entry_market.py"
    original = {market: market.read_text(), entry: entry.read_text()}
    updated = dict(original)
    edits = [
        (market, "end_time - 101 * H4_MS", "end_time - (int(limit) - 1) * H4_MS"),
        (market, "100, earliest - 1", "100, earliest"),
        (entry, "from .market import MarketFrames as BaseMarketFrames, _safe_error\n",
         "from .market import MarketFrames as BaseMarketFrames, _safe_error, _closed_rows, aggregate_rows, _tail, H1_MS\n"),
    ]
    for path, old, new in edits:
        if old in updated[path]:
            if updated[path].count(old) != 1: raise SystemExit("예상과 다른 코드입니다: " + path.name)
            updated[path] = updated[path].replace(old, new)
        elif new not in updated[path]:
            raise SystemExit("예상과 다른 코드입니다: " + path.name)

    method = '    def _utc_six_hours(self, symbol, decision_ms, count):\n        width = 6 * H1_MS\n        key = (symbol, "UTC6H_FROM_1H", count)\n        bucket = decision_ms // width\n        cached = self._cache.get(key)\n        if cached is not None and cached[0] == bucket:\n            return [row[:] for row in cached[1]]\n        raw = self._read(symbol, "1H", max(100, count * 6 + 12))\n        hourly = _closed_rows(raw, H1_MS, decision_ms)\n        complete = aggregate_rows(hourly, H1_MS, width, decision_ms)\n        result = _tail(complete, width, decision_ms, count)\n        if cached is not None:\n            previous = {row[0]: row for row in cached[1]}\n            if any(row[0] in previous and previous[row[0]] != row for row in result):\n                raise DataError("CC_CLOSED_CANDLE_CONFLICT")\n        self._cache[key] = (bucket, result)\n        return [row[:] for row in result]\n\n'
    marker = "    def get_partial(self, symbol, decision_ms):"
    if "    def _utc_six_hours(" not in updated[entry]:
        if updated[entry].count(marker) != 1: raise SystemExit("시간봉 코드 형식이 다릅니다.")
        updated[entry] = updated[entry].replace(marker, method + marker)
    old = "                result[frame] = self._recent(symbol, INTERVALS[frame],\n                                             INDICATOR_FRAMES[frame][0], decision_ms, count)"
    new = '                if frame == "H6":\n                    result[frame] = self._utc_six_hours(symbol, decision_ms, count)\n                else:\n                    result[frame] = self._recent(symbol, INTERVALS[frame],\n                                                 INDICATOR_FRAMES[frame][0], decision_ms, count)'
    if old in updated[entry]:
        updated[entry] = updated[entry].replace(old, new)
    elif new not in updated[entry]:
        raise SystemExit("시간봉 코드 형식이 다릅니다.")
    for path, source in updated.items(): compile(source, str(path), "exec")
    backup = root / "data/cci_chop/source_backups" / str(time.time_ns())
    for path, source in updated.items():
        if source == original[path]: continue
        backup.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup / (path.name + ".before"))
        temporary = path.with_suffix(".fix.tmp")
        temporary.write_text(source)
        temporary.replace(path)
    print("UTC 시간봉 조회 수정 완료. 기존 코드 백업과 주문 기록을 보존했습니다.")
