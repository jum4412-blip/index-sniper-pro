#!/usr/bin/env bash
set -euo pipefail
TREND_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TREND_USER="$(id -un)"
TREND_HOME="$(getent passwd "$TREND_USER" | cut -d: -f6)"
TREND_PYTHON="$(command -v python3)"
if [[ "$TREND_USER" == root ]]; then
  echo 'ubuntu 사용자로 실행하세요. 이 스크립트 전체를 sudo로 실행하지 마세요.' >&2
  exit 2
fi
if [[ ! "$TREND_ROOT" =~ ^/[a-zA-Z0-9_./-]+$ || ! "$TREND_HOME" =~ ^/[a-zA-Z0-9_./-]+$ || ! "$TREND_USER" =~ ^[a-zA-Z0-9_-]+$ || ! "$TREND_PYTHON" =~ ^/[a-zA-Z0-9_./-]+$ ]]; then
  echo '서비스 경로/사용자 이름에 지원하지 않는 문자가 있습니다.' >&2
  exit 2
fi
TREND_UNIT=/etc/systemd/system/btc-eth-trend.service
if [[ -f "$TREND_UNIT" ]] && ! grep -Fxq "# trend-root: $TREND_ROOT" "$TREND_UNIT"; then
  echo '다른 설치의 btc-eth-trend.service가 있습니다. 덮어쓰지 않았습니다.' >&2
  exit 2
fi
TREND_TEMP="$(mktemp)"
trap 'rm -f -- "$TREND_TEMP"' EXIT
cat > "$TREND_TEMP" <<EOF
# trend-root: $TREND_ROOT
[Unit]
Description=BTC ETH Trend Core (Bitget UTA)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$TREND_USER
WorkingDirectory=$TREND_ROOT
Environment=HOME=$TREND_HOME
Environment=PYTHONUNBUFFERED=1
ExecStart=$TREND_PYTHON -m trend_core live
Restart=on-failure
RestartSec=15
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 0644 "$TREND_TEMP" "$TREND_UNIT"
sudo systemctl daemon-reload
sudo systemctl enable btc-eth-trend.service
echo '자동 재시작/부팅 서비스 설치 완료. 아직 실매매를 시작하지 않았습니다. ./trend start-live 로 시작하세요.'
