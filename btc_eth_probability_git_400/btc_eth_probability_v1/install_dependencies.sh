#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if ! command -v g++ >/dev/null 2>&1; then
  printf '%s\n' '전체 self-test에는 g++가 필요합니다. Ubuntu: sudo apt-get update && sudo apt-get install -y python3-venv build-essential'
  exit 2
fi
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
printf '%s\n' '설치 완료. ./prob self-test 후 ./install_service.sh 를 실행하세요.'
