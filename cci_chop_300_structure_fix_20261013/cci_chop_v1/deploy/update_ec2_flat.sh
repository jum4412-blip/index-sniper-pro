#!/usr/bin/env bash
# Run from the staged new source. Preserve credentials, state and signed losses.
set -euo pipefail
staged_dir="$(cd -- "$(dirname -- "$0")/../.." && pwd -P)"
live_dir="${1:-/home/ubuntu/btc_eth_probability_v1_git}"
service_name='cci-chop-live.service'
for required in cc cci_chop_v1/engine.py cci_chop_v1/recovery.py tests README_CC300_KO.txt; do
  if [[ ! -e "$staged_dir/$required" ]]; then
    printf '%s\n' "수정 패키지 파일 누락: $required. 실행 중인 서비스와 기존 코드를 변경하지 않습니다." >&2
    exit 2
  fi
done
if [[ "$staged_dir" == "$live_dir" || ! -x "$live_dir/.venv/bin/python" ]]; then
  printf '%s\n' '별도 준비 폴더에서 실행하고 기존 .venv가 있는 봇 경로를 지정하세요.' >&2
  exit 2
fi
if [[ ! -f "$live_dir/cci_chop_v1/engine.py" || ! -f "$live_dir/cc" ]]; then
  printf '%s\n' '기존 CCI·CHOP 코드 경로를 확인하세요.' >&2
  exit 2
fi
(
  cd -- "$staged_dir"
  "$live_dir/.venv/bin/python" -B -m unittest discover -s tests -p 'test_cci_chop_*.py'
)
"$live_dir/.venv/bin/python" -B "$staged_dir/cci_chop_v1/deploy/check_flat_before_update.py" --root "$live_dir"
was_active=0
if systemctl is-active --quiet "$service_name"; then
  was_active=1
  sudo systemctl stop "$service_name"
fi
if systemctl is-active --quiet "$service_name"; then
  printf '%s\n' '기존 서비스 종료를 확인하지 못했습니다. 코드를 변경하지 않습니다.' >&2
  exit 2
fi
if ! "$live_dir/.venv/bin/python" -B "$staged_dir/cci_chop_v1/deploy/check_flat_before_update.py" --root "$live_dir"; then
  if [[ "$was_active" == 1 ]]; then
    sudo systemctl start "$service_name"
  fi
  exit 2
fi
backup_archive="$(mktemp "$live_dir/cci_code_before_300.XXXXXXXX.tgz")"
chmod 600 "$backup_archive"
tar -czf "$backup_archive" -C "$live_dir" cc cci_chop_v1
tar -cf - -C "$staged_dir" cc cci_chop_v1 tests README_CC300_KO.txt |
  tar -xf - -C "$live_dir"
chmod 700 "$live_dir/cc"
cd -- "$live_dir"
./cc recover-flat --mode live
./cc doctor --mode live
./cc notify-test --mode live
bash cci_chop_v1/deploy/install_ec2.sh
./cc arm --mode live --allow-unvalidated-live
sudo systemctl enable --now "$service_name"
sudo systemctl status "$service_name" --no-pager
./cc status --mode live
./cc notify-now --mode live
