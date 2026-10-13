#!/usr/bin/env bash
# Install this service only. Authorization and starting are separate user commands.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "$0")/../.." && pwd -P)"
service_name='cci-chop-live.service'
service_path="/etc/systemd/system/$service_name"
runner_user="$(id -un)"

if [[ "$runner_user" == 'root' ]]; then
  printf '%s\n' 'EC2의 ubuntu 계정에서 실행하세요. root 계정으로 봇을 설치하지 않습니다.' >&2
  exit 2
fi
if [[ "$project_dir" == *$'\n'* || "$project_dir" == *$'\r'* || "$project_dir" == *' '* || "$project_dir" == *'%'* || "$project_dir" == *'"'* || "$project_dir" == *'\'* ]]; then
  printf '%s\n' '설치 경로에 공백·줄바꿈·백슬래시·따옴표·%를 사용할 수 없습니다.' >&2
  exit 2
fi
if ! command -v systemctl >/dev/null 2>&1 || ! command -v sudo >/dev/null 2>&1; then
  printf '%s\n' '이 설치기는 systemd와 sudo가 있는 Ubuntu EC2용입니다.' >&2
  exit 2
fi
if [[ ! -f "$project_dir/cc" || ! -x "$project_dir/.venv/bin/python" || ! -f "$project_dir/cci_chop_v1/entry_rule.json" ]]; then
  printf '%s\n' 'cc, .venv/bin/python 또는 동결 entry_rule.json이 없습니다. README의 설치 단계를 먼저 실행하세요.' >&2
  exit 2
fi
cd -- "$project_dir"
./.venv/bin/python -B - <<'PY'
import sys
from cci_chop_v1 import config, strategy
from pathlib import Path
if sys.version_info < (3, 10):
    raise SystemExit('Python 3.10 이상이 필요합니다.')
config.load(Path.cwd())
print('동결 규칙·설정 확인:', strategy.spec_sha256())
PY
chmod 700 "$project_dir/cc"
if [[ -f "$project_dir/credentials.env" ]]; then
  chmod 600 "$project_dir/credentials.env"
fi
if [[ -f "$project_dir/cci_chop_demo_credentials.env" ]]; then
  chmod 600 "$project_dir/cci_chop_demo_credentials.env"
fi
# Changing a running service can disrupt protection or position reconciliation.
if systemctl is-active --quiet "$service_name"; then
  printf '%s\n' 'CCI·CHOP 서비스가 실행 중입니다. 포지션 정산을 확인하고 종료한 뒤 설치기를 다시 실행하세요.' >&2
  exit 2
fi
temporary_unit="$(mktemp)"
trap 'rm -f -- "$temporary_unit"' EXIT
chmod 600 "$temporary_unit"
cat > "$temporary_unit" <<UNIT
[Unit]
Description=BTC ETH CCI CHOP user-directed experimental live manager
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$runner_user
WorkingDirectory=$project_dir
ExecStart=$project_dir/cc run --mode live
Restart=on-failure
RestartSec=30
KillSignal=SIGTERM
TimeoutStopSec=45
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT
sudo install -m 644 "$temporary_unit" "$service_path"
sudo systemctl daemon-reload
printf '%s\n' '서비스 파일만 설치했습니다. 자동 시작·실매매 승인·주문 전송은 수행하지 않았습니다.'
printf '%s\n' 'README의 doctor, notify-test, arm 명령 이후 사용자가 서비스 시작 명령을 실행하세요.'
