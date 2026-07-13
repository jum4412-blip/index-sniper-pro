#!/usr/bin/env bash
set -euo pipefail

VERSION="6.3.2"
DO_SYSTEMD=0
DO_START=1
for arg in "$@"; do
  case "$arg" in
    --systemd) DO_SYSTEMD=1 ;;
    --no-start) DO_START=0 ;;
    -h|--help)
      cat <<'EOF'
Usage: from ~/index-sniper-pro
  bash /path/to/v63_btc_eth_live_release_6.3.2/apply_v63_btc_eth_live.sh
  bash /path/to/v63_btc_eth_live_release_6.3.2/apply_v63_btc_eth_live.sh --systemd
  bash /path/to/v63_btc_eth_live_release_6.3.2/apply_v63_btc_eth_live.sh --no-start

Installs DISARMED/SHADOW only. Live requires a separate arm command.
EOF
      exit 0 ;;
    *) echo "Unknown argument: $arg"; exit 2 ;;
  esac
done

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(pwd)"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP="$ROOT/local_backups/v63_install_$STAMP"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

if [[ ! -d "$ROOT/index_sniper" ]]; then
  echo "❌ ~/index-sniper-pro 루트에서 실행하세요. 현재: $ROOT"
  exit 1
fi
for required in index_sniper/exchange/bitget_uta.py index_sniper/strategy/indicators.py; do
  [[ -f "$ROOT/$required" ]] || { echo "❌ 필수 파일 없음: $ROOT/$required"; exit 1; }
done
if [[ -f "$PKG_DIR/PACKAGE_SHA256SUMS.txt" ]]; then
  (cd "$PKG_DIR" && sha256sum -c PACKAGE_SHA256SUMS.txt)
fi
if pgrep -af 'python(3)? .*index_sniper[.]dual_live_v63 loop' >/dev/null 2>&1; then
  echo "❌ 기존 v6.3 loop를 먼저 중지하세요: bash stop_v63_dual_live.sh"
  exit 1
fi
"$PY" - <<'PY'
import sys
if sys.version_info < (3,10):
    raise SystemExit(f"Python 3.10+ required; found {sys.version.split()[0]}")
print("Python", sys.version.split()[0])
PY

FILES=(
  index_sniper/dual_live_v63.py
  config/v63_dual_live.json
  README_BTC_ETH_V63_LIVE.md
  V63_MANIFEST.json
  arm_v63_dual_live.sh
  collect_v63_results.sh
  disarm_v63_dual_live.sh
  doctor_v63_dual_live.sh
  install_v63_systemd.sh
  panic_flat_v63.sh
  pause_v63_entries.sh
  report_v63_dual_live.sh
  resume_v63_entries.sh
  rollback_v63_dual_live.sh
  run_v63_once.sh
  setup_v63_account.sh
  start_v63_dual_live.sh
  status_v63_dual_live.sh
  stop_v63_dual_live.sh
)
mkdir -p "$BACKUP" "$ROOT/config" "$ROOT/logs" "$ROOT/data/v63_dual_live"
created=()
for rel in "${FILES[@]}"; do
  dst="$ROOT/$rel"
  if [[ -f "$dst" ]]; then
    mkdir -p "$BACKUP/$(dirname "$rel")"
    cp -a "$dst" "$BACKUP/$rel"
  else
    created+=("$rel")
  fi
done
[[ -f "$ROOT/.env" ]] && cp -a "$ROOT/.env" "$BACKUP/.env"
printf '%s\n' "${FILES[@]}" | "$PY" -c 'import json,sys; print(json.dumps([x.strip() for x in sys.stdin if x.strip()], indent=2))' > "$BACKUP/manifest_paths.json"
printf '%s\n' "${created[@]:-}" | "$PY" -c 'import json,sys; print(json.dumps([x.strip() for x in sys.stdin if x.strip()], indent=2))' > "$BACKUP/created_files.json"

install -m 0600 "$PKG_DIR/dual_live_v63.py" "$ROOT/index_sniper/dual_live_v63.py"
install -m 0600 "$PKG_DIR/v63_dual_live.json" "$ROOT/config/v63_dual_live.json"
install -m 0600 "$PKG_DIR/README_BTC_ETH_V63_LIVE.md" "$ROOT/README_BTC_ETH_V63_LIVE.md"
install -m 0600 "$PKG_DIR/V63_MANIFEST.json" "$ROOT/V63_MANIFEST.json"
for f in arm_v63_dual_live.sh collect_v63_results.sh disarm_v63_dual_live.sh doctor_v63_dual_live.sh install_v63_systemd.sh panic_flat_v63.sh pause_v63_entries.sh report_v63_dual_live.sh resume_v63_entries.sh rollback_v63_dual_live.sh run_v63_once.sh setup_v63_account.sh start_v63_dual_live.sh status_v63_dual_live.sh stop_v63_dual_live.sh; do
  install -m 0700 "$PKG_DIR/$f" "$ROOT/$f"
done

ROOT_ENV="$ROOT" "$PY" - <<'PY'
from pathlib import Path
import json, os, re
root=Path(os.environ['ROOT_ENV'])
p=root/'.env'
lines=p.read_text(encoding='utf-8',errors='ignore').splitlines() if p.exists() else []
values={
 'V63_CONFIG':'config/v63_dual_live.json',
 'V63_NOTIFY':'true',
 'V63_FORCE_SHADOW':'false',
 'V63_LIVE_ENABLED':'false',
 'V63_LIVE_CONFIRM':'',
 'V63_KEYS_ROTATED_CONFIRM':'',
 'V63_NO_WITHDRAW_CONFIRM':'',
 'V63_IP_WHITELIST_CONFIRM':'',
}
keys=set(values); out=[]
for line in lines:
    m=re.match(r'^([A-Za-z_][A-Za-z0-9_]*)=',line.strip())
    if m and m.group(1) in keys: continue
    out.append(line)
if out and out[-1].strip(): out.append('')
for k,v in values.items(): out.append(f'{k}="{v}"')
p.write_text('\n'.join(out)+'\n',encoding='utf-8'); p.chmod(0o600)
(root/'data/v63_dual_live/LIVE_ARMED').unlink(missing_ok=True)
(root/'data/v63_dual_live/PAUSE_NEW_ENTRIES').unlink(missing_ok=True)
PY

cd "$ROOT"
export PYTHONPATH="$ROOT"
"$PY" -m py_compile index_sniper/dual_live_v63.py
"$PY" -m index_sniper.dual_live_v63 self-test

ROOT_ENV="$ROOT" BACKUP_ENV="$BACKUP" VERSION_ENV="$VERSION" "$PY" - <<'PY'
from pathlib import Path
import json, os, datetime as dt
root=Path(os.environ['ROOT_ENV']); backup=Path(os.environ['BACKUP_ENV'])
record={'version':os.environ['VERSION_ENV'],'installed_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'backup_dir':str(backup)}
p=root/'data/v63_dual_live/install_record.json'
p.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); p.chmod(0o600)
PY

if [[ "$DO_START" -eq 1 ]]; then
  if [[ "$DO_SYSTEMD" -eq 1 ]]; then
    bash "$ROOT/install_v63_systemd.sh"
  else
    bash "$ROOT/start_v63_dual_live.sh"
  fi
else
  echo "ℹ️ --no-start: 엔진은 시작하지 않았습니다."
fi

echo
echo "✅ BTC/ETH Quant v$VERSION 설치 완료 — SHADOW / 실주문 차단"
echo "백업: $BACKUP"
echo "다음: bash setup_v63_account.sh → bash doctor_v63_dual_live.sh --prearm → bash run_v63_once.sh --force-shadow"
echo "LIVE arm은 README의 4개 확인 문구를 실제로 충족한 뒤에만 실행하세요."
