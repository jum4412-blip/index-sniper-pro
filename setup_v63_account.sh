#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
if pgrep -af 'python(3)? .*index_sniper[.]dual_live_v63 loop' >/dev/null 2>&1; then
  echo "❌ v6.3 loop가 실행 중입니다. 먼저 bash stop_v63_dual_live.sh 실행"
  exit 1
fi
export PYTHONPATH="$ROOT"
exec "$PY" -m index_sniper.dual_live_v63 setup
