#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -d "$PWD/index_sniper" && -x "$PWD/.venv/bin/python" ]]; then
  ROOT="$PWD"
else
  ROOT="$HOME/index-sniper-pro"
fi

PY="$ROOT/.venv/bin/python"
TARGET="$ROOT/index_sniper/vwap_backtest_v23.py"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/local_backups/vwap_backtest_v231_pagination_fix_$TS"

if [[ ! -x "$PY" || ! -f "$TARGET" ]]; then
  echo "프로젝트 또는 v2.3 백테스트 모듈을 찾을 수 없습니다: $ROOT" >&2
  exit 1
fi

mkdir -p "$BACKUP/index_sniper"
cp -a "$TARGET" "$BACKUP/index_sniper/"

"$PY" - "$TARGET" <<'PY_PATCH'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

if 'VERSION = "2.3.1-range-backtest-pagination-fix"' in text:
    print("이미 v2.3.1 pagination fix가 적용되어 있습니다.")
    raise SystemExit(0)

old_version = 'VERSION = "2.3.0-range-backtest"'
if old_version not in text:
    raise SystemExit(
        "예상한 v2.3.0 버전 문자열이 없습니다. "
        "수동 확인 없이 다른 버전에 덮어쓰지 않습니다."
    )

new_method = '''    def iter_bars(self, symbol: str, start_ms: int, end_ms: int) -> Iterator[Bar]:
        # Bitget history-candles는 요청 구간 안의 최신 페이지를 반환한다.
        # 따라서 각 응답의 가장 오래된 시각 이전으로 endTime을 옮겨야 한다.
        window_ms = 89 * 24 * 3600 * 1000
        window_start = start_ms
        last_yielded = start_ms - self.interval_ms

        while window_start <= end_ms:
            window_end = min(end_ms, window_start + window_ms)
            page_end = window_end
            window_bars: dict[int, Bar] = {}
            previous_oldest: int | None = None

            while page_end >= window_start:
                rows = self._request({
                    "category": "USDT-FUTURES",
                    "symbol": symbol,
                    "interval": self.interval,
                    "type": "market",
                    "startTime": str(window_start),
                    "endTime": str(page_end),
                    "limit": str(self.limit),
                })

                parsed: list[Bar] = []
                for row in rows:
                    if not isinstance(row, (list, tuple)) or len(row) < 7:
                        continue
                    ts = safe_int(row[0])
                    if ts < window_start or ts > page_end:
                        continue
                    parsed.append(Bar(
                        ts,
                        safe_float(row[1]),
                        safe_float(row[2]),
                        safe_float(row[3]),
                        safe_float(row[4]),
                        safe_float(row[5]),
                        safe_float(row[6]),
                    ))

                if not parsed:
                    break

                parsed.sort(key=lambda b: b.ts)
                for bar in parsed:
                    window_bars[bar.ts] = bar

                oldest = parsed[0].ts
                if previous_oldest is not None and oldest >= previous_oldest:
                    raise RuntimeError(
                        f"non-advancing history pagination for {symbol}: "
                        f"oldest={oldest} previous_oldest={previous_oldest}"
                    )
                previous_oldest = oldest

                if oldest <= window_start:
                    break

                next_page_end = oldest - self.interval_ms
                if next_page_end >= page_end:
                    raise RuntimeError(
                        f"invalid backward cursor for {symbol}: "
                        f"page_end={page_end} next={next_page_end}"
                    )
                page_end = next_page_end

                if len(rows) < self.limit:
                    break

            for ts in sorted(window_bars):
                if ts <= last_yielded:
                    continue
                last_yielded = ts
                yield window_bars[ts]

            window_start = window_end + self.interval_ms
'''

method_start = text.index("    def iter_bars(")
method_end = text.index("\n\nclass RangeStrategy:", method_start)
text = text[:method_start] + new_method + text[method_end:]
text = text.replace(
    old_version,
    'VERSION = "2.3.1-range-backtest-pagination-fix"',
    1,
)

old_coverage = '''        all_candidates.extend(sim.candidates)
        coverage.append({
            "symbol": symbol,
            "bars": bars,
            "first_utc": iso_ms(sim.coverage_first) if sim.coverage_first else None,
            "last_utc": iso_ms(sim.coverage_last) if sim.coverage_last else None,
            "candidates": len(sim.candidates),
            "ambiguous_skips": sim.skipped_ambiguous,
        })
'''

new_coverage = '''        expected_bars = max(1, int((end_ms - start_ms) / fetcher.interval_ms))
        coverage_ratio_pct = bars / expected_bars * 100.0

        # BTC/ETH는 장기 데이터 기준점이다. 데이터가 부족하면 잘못된
        # 0거래 리포트를 만들지 말고 전체 실행을 실패시킨다.
        if symbol in {"BTCUSDT", "ETHUSDT"}:
            last_is_recent = (
                sim.coverage_last is not None
                and sim.coverage_last >= end_ms - 2 * 24 * 3600 * 1000
            )
            if coverage_ratio_pct < 85.0 or not last_is_recent:
                raise RuntimeError(
                    f"incomplete historical coverage for {symbol}: "
                    f"bars={bars:,}/{expected_bars:,} "
                    f"ratio={coverage_ratio_pct:.2f}% "
                    f"last={iso_ms(sim.coverage_last) if sim.coverage_last else None}"
                )

        all_candidates.extend(sim.candidates)
        coverage.append({
            "symbol": symbol,
            "bars": bars,
            "expected_bars_approx": expected_bars,
            "coverage_ratio_pct": round(coverage_ratio_pct, 4),
            "first_utc": iso_ms(sim.coverage_first) if sim.coverage_first else None,
            "last_utc": iso_ms(sim.coverage_last) if sim.coverage_last else None,
            "candidates": len(sim.candidates),
            "ambiguous_skips": sim.skipped_ambiguous,
        })
'''

if old_coverage not in text:
    raise SystemExit("coverage block을 찾지 못해 안전하게 중단합니다.")
text = text.replace(old_coverage, new_coverage, 1)

old_report = '''            f"{c['symbol']}: bars={c['bars']} first={c.get('first_utc')} last={c.get('last_utc')} "
            f"candidates={c['candidates']} ambiguous_skips={c['ambiguous_skips']}"
'''

new_report = '''            f"{c['symbol']}: bars={c['bars']} coverage={c.get('coverage_ratio_pct', 0):.2f}% "
            f"first={c.get('first_utc')} last={c.get('last_utc')} "
            f"candidates={c['candidates']} ambiguous_skips={c['ambiguous_skips']}"
'''

if old_report not in text:
    raise SystemExit("report coverage line을 찾지 못해 안전하게 중단합니다.")
text = text.replace(old_report, new_report, 1)

path.write_text(text, encoding="utf-8")
print(f"patched: {path}")
PY_PATCH

cat > "$ROOT/run_vwap_backtest_v231_smoke.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

SESSION="vwap-bt-v231-smoke"
LOG="logs/vwap-backtest-v231-smoke.log"

screen -S "$SESSION" -X quit 2>/dev/null || true
mkdir -p logs backtests/vwap_range_v231_smoke
: > "$LOG"

screen -dmS "$SESSION" bash -lc "
  cd '$PWD'
  PYTHONPATH='$PWD' .venv/bin/python -m index_sniper.vwap_backtest_v23 run \
    --interval 1m \
    --years 0.04 \
    --warmup-days 2 \
    --symbols BTCUSDT,ETHUSDT \
    --rps 8 \
    --fail-fast \
    --output-dir backtests/vwap_range_v231_smoke \
    2>&1 | tee '$LOG'
"

echo "✅ v2.3.1 1분봉 다운로드 스모크 테스트 시작"
echo "대상: BTCUSDT, ETHUSDT / 약 16일"
echo "screen: $SESSION"
echo "로그: tail -f $LOG"
SH

cat > "$ROOT/check_vwap_backtest_v231_smoke.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

echo "===== SCREEN ====="
screen -ls 2>/dev/null | grep 'vwap-bt-v231-smoke' || true

echo
echo "===== LOG ====="
tail -100 logs/vwap-backtest-v231-smoke.log 2>/dev/null || true

echo
echo "===== LATEST COVERAGE ====="
DIR="$(find backtests/vwap_range_v231_smoke -maxdepth 1 -type d -name 'run_*_1m' | sort | tail -1)"
echo "DIR=$DIR"
if [[ -n "$DIR" && -f "$DIR/coverage.csv" ]]; then
  cat "$DIR/coverage.csv"
elif [[ -n "$DIR" && -f "$DIR/coverage_progress.json" ]]; then
  cat "$DIR/coverage_progress.json"
else
  echo "아직 coverage 결과가 없습니다."
fi
SH

chmod 755 \
  "$ROOT/run_vwap_backtest_v231_smoke.sh" \
  "$ROOT/check_vwap_backtest_v231_smoke.sh"

cd "$ROOT"
PYTHONPATH="$ROOT" "$PY" -m py_compile index_sniper/vwap_backtest_v23.py
PYTHONPATH="$ROOT" "$PY" -m index_sniper.vwap_backtest_v23 self-test

PYTHONPATH="$ROOT" "$PY" - <<'PY_TEST'
import importlib

m = importlib.import_module("index_sniper.vwap_backtest_v23")

class FakeHistory(m.BitgetHistory):
    def __init__(self):
        super().__init__("1m", rps=1000, retries=1)
        start = 1_700_000_000_000
        self.rows = [
            [str(start + i * 60_000), "1", "2", "0.5", "1.5", "10", "15"]
            for i in range(1000)
        ]

    def _request(self, params):
        start = int(params["startTime"])
        end = int(params["endTime"])
        limit = int(params["limit"])
        rows = [row for row in self.rows if start <= int(row[0]) <= end]
        return rows[-limit:]

fake = FakeHistory()
start = 1_700_000_000_000
bars = list(fake.iter_bars("BTCUSDT", start, start + 999 * 60_000))
assert len(bars) == 1000, len(bars)
assert len({bar.ts for bar in bars}) == 1000
assert bars[0].ts == start
assert bars[-1].ts == start + 999 * 60_000
print({"pagination_test": "OK", "bars": len(bars)})
PY_TEST

cat <<EOF

✅ VWAP Backtest v2.3.1 pagination fix 적용 완료

원인:
  기존 코드는 구간 안의 최신 100개를 받은 뒤 cursor를 앞으로 이동해
  실제로는 종목당 100개 캔들만 수집했습니다.

수정:
  endTime을 가장 오래된 응답 시각 이전으로 이동하며 역방향 페이지네이션
  BTC/ETH 데이터 커버리지 85% 미만이면 백테스트 강제 실패
  report.txt에 coverage 비율 표시
  네트워크 없는 1,000봉 페이지네이션 단위검사 포함

먼저 약 16일 스모크 테스트:
  bash run_vwap_backtest_v231_smoke.sh

진행 확인:
  bash check_vwap_backtest_v231_smoke.sh

스모크 성공 후 기존 FULL 재실행:
  bash run_vwap_backtest_v23_full.sh

백업:
  $BACKUP
EOF
