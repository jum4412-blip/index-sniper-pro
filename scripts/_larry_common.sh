#!/usr/bin/env bash
set -euo pipefail

ROOT="${INDEX_SNIPER_ROOT:-$HOME/index-sniper-pro}"
if [[ ! -d "$ROOT" ]]; then
  echo "프로젝트 경로가 없습니다: $ROOT" >&2
  exit 1
fi

PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

CONFIG="${LARRY_V1_CONFIG:-$ROOT/config/larry_williams_core_v1.json}"
LOGFILE="$ROOT/logs/larry-williams-core-v1.log"
PIDFILE="$ROOT/data/larry-williams-core-v1.pid"
SESSION="larry-core-v1"
MODULE_PATTERN='[i]ndex_sniper[.]larry_williams_core_v1 .*loop'

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"

process_running() {
  pgrep -af "$MODULE_PATTERN" >/dev/null 2>&1
}

show_processes() {
  pgrep -af "$MODULE_PATTERN" || true
}

set_env_key() {
  local key="$1"
  local value="$2"
  ROOT_ENV="$ROOT" KEY_ENV="$key" VALUE_ENV="$value" "$PY" - <<'PY'
from pathlib import Path
import os, re
root = Path(os.environ["ROOT_ENV"])
p = root / ".env"
key = os.environ["KEY_ENV"]
value = os.environ["VALUE_ENV"]
lines = p.read_text(encoding="utf-8", errors="ignore").splitlines() if p.exists() else []
out = []
pat = re.compile(r"^\s*" + re.escape(key) + r"\s*=")
for line in lines:
    if not pat.match(line):
        out.append(line)
if out and out[-1].strip():
    out.append("")
out.append(f'{key}="{value}"')
p.write_text("\n".join(out) + "\n", encoding="utf-8")
p.chmod(0o600)
PY
}
