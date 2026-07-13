#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PHRASE="${1:-}"
if [[ "$PHRASE" != "FLAT_BTC_ETH_NOW" ]]; then
  echo "사용법: bash panic_flat_v63.sh FLAT_BTC_ETH_NOW"
  exit 2
fi
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
export PYTHONPATH="$ROOT"
"$PY" -m index_sniper.dual_live_v63 panic-flat --phrase "$PHRASE"
echo
echo "⚠️ 청산 요청 전송 완료. 반드시 거래소 앱과 아래 상태를 직접 확인하세요."
bash "$ROOT/status_v63_dual_live.sh" || true
