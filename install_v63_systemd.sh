#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"
UNIT="/etc/systemd/system/index-sniper-v63.service"
mkdir -p "$ROOT/logs" "$ROOT/data/v63_dual_live"
if pgrep -af 'python(3)? .*index_sniper[.]dual_live_v63 loop' >/dev/null 2>&1; then
  bash "$ROOT/stop_v63_dual_live.sh" || true
fi
TMP="$(mktemp)"
cat > "$TMP" <<EOF
[Unit]
Description=Index Sniper BTC ETH Quant v6.3 Dual Live
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$GROUP_NAME
WorkingDirectory=$ROOT
Environment=PYTHONPATH=$ROOT
Environment=PYTHONUNBUFFERED=1
ExecStart=/bin/bash -lc 'exec "$PY" -m index_sniper.dual_live_v63 loop >> "$ROOT/logs/v63-dual-live.log" 2>&1'
Restart=always
RestartSec=5
TimeoutStopSec=20
KillSignal=SIGTERM
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
sudo install -m 0644 "$TMP" "$UNIT"
rm -f "$TMP"
sudo systemctl daemon-reload
sudo systemctl enable --now index-sniper-v63.service
sleep 3
sudo systemctl --no-pager --full status index-sniper-v63.service | sed -n '1,24p' || true
