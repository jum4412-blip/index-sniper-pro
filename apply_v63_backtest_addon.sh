#!/usr/bin/env bash
set -euo pipefail
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP="$ROOT/local_backups/v63_backtest_$STAMP"

[[ -d "$ROOT/index_sniper" ]] || { echo "~/index-sniper-pro 루트에서 실행하세요: $ROOT"; exit 1; }
[[ -f "$ROOT/config/v63_dual_live.json" ]] || { echo "먼저 v6.3.2 live 패치를 SHADOW로 설치하세요."; exit 1; }
[[ -f "$ROOT/index_sniper/strategy/indicators.py" ]] || { echo "필수 파일 없음: index_sniper/strategy/indicators.py"; exit 1; }
if [[ -f "$PKG_DIR/PACKAGE_SHA256SUMS.txt" ]]; then
  (cd "$PKG_DIR" && sha256sum -c PACKAGE_SHA256SUMS.txt)
fi

mkdir -p "$BACKUP" "$ROOT/reports/v63_backtest" "$ROOT/data/v63_backtest/cache"
for rel in index_sniper/backtest_v63.py run_v63_backtest.sh report_v63_backtest.sh README_BACKTEST_V63.md; do
  [[ -f "$ROOT/$rel" ]] || continue
  mkdir -p "$BACKUP/$(dirname "$rel")"
  cp -a "$ROOT/$rel" "$BACKUP/$rel"
done

install -m 0600 "$PKG_DIR/backtest_v63.py" "$ROOT/index_sniper/backtest_v63.py"
install -m 0700 "$PKG_DIR/run_v63_backtest.sh" "$ROOT/run_v63_backtest.sh"
install -m 0700 "$PKG_DIR/report_v63_backtest.sh" "$ROOT/report_v63_backtest.sh"
install -m 0600 "$PKG_DIR/README_BACKTEST_V63.md" "$ROOT/README_BACKTEST_V63.md"

cd "$ROOT"
export PYTHONPATH="$ROOT"
"$PY" -m py_compile index_sniper/backtest_v63.py
"$PY" -m index_sniper.backtest_v63 --self-test

echo
echo "✅ v6.3.2 백테스트 추가 완료"
echo "백업: $BACKUP"
echo "실행: bash run_v63_backtest.sh 60 1000 suite"
echo "이 스크립트는 주문을 전혀 보내지 않습니다."
