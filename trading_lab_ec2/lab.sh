#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
app="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")"
root="$(cat "$app/RUNTIME_ROOT")"
py="$root/venv/bin/python"
export PYTHONDONTWRITEBYTECODE=1
cmd="${1:-help}"
if (( $# )); then shift; fi
case "$cmd" in
  doctor|status|once|backup|notify-test)
    exec "$py" "$app/observer.py" "$cmd" --root "$root" "$@" ;;
  test)
    exec env LAB_PYTHON="$py" bash "$app/selftest.sh" ;;
  demo)
    exec "$py" "$app/core/cli.py" demo ;;
  ledger)
    case "${1:-}" in append|report) ;; *) echo 'Usage: ~/lab1030 ledger report | append [actual event arguments]'; exit 2;; esac
    exec "$py" "$app/core/cli.py" "$@" --db "$root/shared/data/nav.sqlite" ;;
  start)
    # Public checks first. Never starts any legacy trading service.
    "$py" "$app/observer.py" doctor --root "$root"
    sudo systemctl reset-failed lab1030-observer.service lab1030-backup.service 2>/dev/null || true
    sudo systemctl enable --now lab1030-observer.service lab1030-backup.timer
    echo 'Public collector started. After its first cycle, check ~/lab1030 status.' ;;
  stop)
    sudo systemctl disable --now lab1030-observer.service lab1030-backup.timer
    sudo systemctl stop lab1030-backup.service
    echo 'Only LAB1030 observer and its backup timer stopped; data retained.' ;;
  restart)
    "$py" "$app/observer.py" doctor --root "$root"
    sudo systemctl restart lab1030-observer.service ;;
  service-status)
    systemctl status lab1030-observer.service lab1030-backup.timer --no-pager || true ;;
  logs)
    exec journalctl -u lab1030-observer.service -n 100 -f --no-pager ;;
  paths)
    printf 'RELEASE=%s\nROOT=%s\nDATA=%s/shared/data\nCONFIG=%s/config\n' "$app" "$root" "$root" "$root" ;;
  *)
    cat <<'EOF'
LAB1030: OBSERVE_ONLY (no account API, no exchange orders)
  ~/lab1030 doctor          Public API and config checks (no writes to exchange)
  ~/lab1030 doctor --offline  Local checks only
  ~/lab1030 start           Public checks, then enable this observer + local backup
  ~/lab1030 status          Data freshness, gaps, current observation status
  ~/lab1030 service-status  systemd process state (not proof of fresh data)
  ~/lab1030 logs            Follow collector logs; Ctrl+C leaves service running
  ~/lab1030 stop            Stop/disable ONLY this collector and backup
  ~/lab1030 restart         Restart ONLY this collector
  ~/lab1030 once            One collection cycle; fails if collector already running
  ~/lab1030 backup          Online SQLite snapshots, same-instance only
  ~/lab1030 notify-test     Send one Telegram test after local credentials configured
  ~/lab1030 test            Offline unit tests, no production DB changes
  ~/lab1030 demo            Fictional risk and NAV examples, no real funds used
  ~/lab1030 ledger report   Report MANUALLY maintained NAV ledger, if initialized
  ~/lab1030 paths           Show runtime paths
EOF
    ;;
esac
