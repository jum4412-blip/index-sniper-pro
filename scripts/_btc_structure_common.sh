#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${INDEX_SNIPER_ROOT:-$HOME/index-sniper-pro}"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
CONFIG="$ROOT/config/btc_structure_trend_v1.json"
MODULE="index_sniper.btc_structure_trend_v1"
STATE="$ROOT/data/btc_structure_trend_v1_state.json"
ARM_FILE="$ROOT/data/BTC_STRUCTURE_TREND_V1_ARMED.json"
LOG="$ROOT/logs/btc-structure-trend-v1.log"
LIVE_SESSION="btc-structure-trend-live"
OBSERVE_SESSION="btc-structure-trend-observe"

[[ -d "$ROOT" ]] || { echo "프로젝트 경로가 없습니다: $ROOT" >&2; exit 1; }
[[ -x "$PY" ]] || { echo "Python을 찾을 수 없습니다." >&2; exit 1; }

set_env_key() {
  local key="$1" value="$2"
  local env_file="$ROOT/.env"
  "$PY" - "$env_file" "$key" "$value" <<'PYENV'
from pathlib import Path
import os, sys, tempfile
p=Path(sys.argv[1]); key=sys.argv[2]; value=sys.argv[3]
lines=p.read_text(encoding='utf-8',errors='ignore').splitlines() if p.exists() else []
out=[]; found=False
for line in lines:
    stripped=line.lstrip()
    if stripped.startswith('#') or '=' not in line:
        out.append(line); continue
    current=line.split('=',1)[0].strip()
    if current == key:
        if not found: out.append(f'{key}={value}')
        found=True
    else:
        out.append(line)
if not found: out.append(f'{key}={value}')
p.parent.mkdir(parents=True,exist_ok=True)
fd,tmp=tempfile.mkstemp(prefix=p.name+'.',dir=str(p.parent))
with os.fdopen(fd,'w',encoding='utf-8') as fp:
    fp.write('\n'.join(out).rstrip('\n')+'\n'); fp.flush(); os.fsync(fp.fileno())
os.chmod(tmp,0o600); os.replace(tmp,p)
PYENV
}

stop_session() {
  local name="$1"
  screen -S "$name" -X quit >/dev/null 2>&1 || true
}

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
