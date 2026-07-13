#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
export PYTHONPATH="$ROOT"
mkdir -p "$ROOT/data/v63_dual_live"
printf 'rollback_check=%s\n' "$(date -Is)" > "$ROOT/data/v63_dual_live/PAUSE_NEW_ENTRIES"
bash "$ROOT/stop_v63_dual_live.sh" >/dev/null 2>&1 || true
set +e
"$PY" - <<'PY'
from index_sniper.dual_live_v63 import load_dotenv_safely, DEFAULT_ROOT, make_client, all_current_positions
load_dotenv_safely(DEFAULT_ROOT/'.env')
rows=all_current_positions(make_client())
if rows:
    print(f"❌ 열린 USDT 선물 포지션 {len(rows)}개. rollback 전에 청산하세요.")
    for row in rows:
        print(f"- {row.get('symbol')} {row.get('posSide') or row.get('side')} qty={row.get('total') or row.get('size') or row.get('qty')}")
    raise SystemExit(3)
PY
RC=$?
set -e
if [[ "$RC" -ne 0 ]]; then
  echo "rollback 중단. 필요하면 bash panic_flat_v63.sh FLAT_BTC_ETH_NOW"
  bash "$ROOT/start_v63_dual_live.sh" || true
  exit "$RC"
fi
if command -v systemctl >/dev/null 2>&1 && systemctl cat index-sniper-v63.service >/dev/null 2>&1; then
  sudo systemctl disable --now index-sniper-v63.service || true
  sudo rm -f /etc/systemd/system/index-sniper-v63.service
  sudo systemctl daemon-reload
fi
ROOT_ENV="$ROOT" "$PY" - <<'PY'
from pathlib import Path
import json, os, shutil
root=Path(os.environ['ROOT_ENV'])
record_path=root/'data/v63_dual_live/install_record.json'
if not record_path.exists():
    raise SystemExit('install_record.json 없음; 자동 복구 불가')
record=json.loads(record_path.read_text(encoding='utf-8'))
backup=Path(record['backup_dir'])
created=set(json.loads((backup/'created_files.json').read_text(encoding='utf-8'))) if (backup/'created_files.json').exists() else set()
paths=json.loads((backup/'manifest_paths.json').read_text(encoding='utf-8'))
for rel in paths:
    src=backup/rel
    dst=root/rel
    if src.exists():
        dst.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(src,dst)
    elif rel in created and dst.exists() and dst.is_file():
        dst.unlink()
env=backup/'.env'
if env.exists(): shutil.copy2(env,root/'.env')
print(f'복구 완료: {backup}')
PY
rm -f "$ROOT/data/v63_dual_live/PAUSE_NEW_ENTRIES" "$ROOT/data/v63_dual_live/LIVE_ARMED"
echo "✅ v6.3 설치 파일 rollback 완료"
echo "거래 데이터와 로그, 보안 격리 파일은 삭제하지 않았습니다."
