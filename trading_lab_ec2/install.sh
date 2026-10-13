#!/usr/bin/env bash
# Local installation only. Own service is left stopped; start is a separate explicit step.
set -Eeuo pipefail
umask 077
app="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
commit="${1:?commit SHA required}"
[[ "$commit" =~ ^[0-9a-f]{40,64}$ ]] || { echo 'ERROR: invalid commit SHA' >&2; exit 2; }
[[ "$(id -u)" != 0 ]] || { echo 'ERROR: run as ubuntu, not with sudo bash.' >&2; exit 2; }
[[ "$HOME" =~ ^/[A-Za-z0-9_./-]+$ ]] || { echo 'ERROR: unsupported home path' >&2; exit 2; }
command -v systemctl >/dev/null
[[ -d /run/systemd/system ]] || { echo 'ERROR: systemd must run on the target EC2 host.' >&2; exit 2; }
for cmd in python3 sudo flock tar; do command -v "$cmd" >/dev/null; done
python3 -c 'import sys,sqlite3,venv; assert sys.version_info >= (3,10), "Python >=3.10 required"'
base="$HOME/trading_lab_ec2_runtime"
link="$HOME/lab1030"
if [[ -e "$base" && ! -f "$base/.lab1030-runtime" ]]; then
  echo "ERROR: unrecognized existing directory $base; no changes made." >&2; exit 2
fi
[[ ! -L "$base" ]] || { echo 'ERROR: runtime root must not be a symlink.' >&2; exit 2; }
if [[ -L "$link" ]]; then
  [[ "$(readlink "$link")" == "$base/current/lab.sh" ]] || { echo 'ERROR: lab1030 points elsewhere.' >&2; exit 2; }
elif [[ -e "$link" ]]; then
  echo 'ERROR: ~/lab1030 already exists and is not our launcher.' >&2; exit 2
fi
# Do not overwrite another person's services with coincidentally matching names.
for unit in lab1030-observer.service lab1030-backup.service lab1030-backup.timer; do
  path="/etc/systemd/system/$unit"
  if [[ -e "$path" || -L "$path" ]]; then
    grep -Fq '# Managed by LAB1030 public observer v1' "$path" || {
      echo "ERROR: existing $unit is not owned by this package." >&2; exit 2;
    }
  fi
done
mkdir -p "$base"
chmod 700 "$base"
printf 'LAB1030 public observer runtime v1\n' > "$base/.lab1030-runtime"
exec 9>"$base/install.lock"
flock -n 9 || { echo 'ERROR: another install is running.' >&2; exit 2; }
mkdir -p "$base/releases" "$base/config" "$base/shared/data" "$base/shared/backups" "$base/shared/logs"
if [[ ! -x "$base/venv/bin/python" ]]; then
  python3 -m venv --without-pip "$base/venv" || {
    echo 'ERROR: venv unavailable. Install python3-venv on EC2, then retry; no existing bot was stopped.' >&2; exit 2;
  }
fi
# Verify before stopping the observer or switching code. Never run tests against the user's DB.
LAB_PYTHON="$base/venv/bin/python" bash "$app/selftest.sh"
release="$base/releases/$commit"
if [[ -e "$release" ]]; then
  [[ -f "$release/DEPLOY_COMMIT" && "$(cat "$release/DEPLOY_COMMIT")" == "$commit" ]] || {
    echo 'ERROR: existing release directory is invalid.' >&2; exit 2;
  }
  # Refuse silent local edits to a supposedly fixed release.
  diff -qr --exclude=DEPLOY_COMMIT --exclude=RUNTIME_ROOT --exclude=__pycache__ "$app" "$release" >/dev/null || {
    echo 'ERROR: release content differs. Commit code changes as a new version; do not overwrite the old release.' >&2; exit 2;
  }
else
  temp_release="$(mktemp -d "$base/releases/.staging.XXXXXXXX")"
  trap '[[ -z "${temp_release:-}" ]] || rm -rf -- "$temp_release"' EXIT
  cp -a "$app/." "$temp_release/"
  find "$temp_release" -type d -name __pycache__ -prune -exec rm -rf -- {} +
  printf '%s\n' "$commit" > "$temp_release/DEPLOY_COMMIT"
  printf '%s\n' "$base" > "$temp_release/RUNTIME_ROOT"
  chmod u+x "$temp_release"/*.sh
  mv "$temp_release" "$release"
  temp_release=''
fi
# Config is initialized once, never replaced during an update.
if [[ ! -e "$base/config/observer.json" ]]; then
  install -m 600 "$app/config.example.json" "$base/config/observer.json"
fi
if [[ ! -e "$base/config/telegram.env" ]]; then
  install -m 600 "$app/telegram.env.example" "$base/config/telegram.env"
fi
"$base/venv/bin/python" "$release/observer.py" doctor --root "$base" --offline
# Obtain permission before changing service state. Only names below are touched.
sudo -v
old=''
if [[ -L "$base/current" ]]; then
  old="$(readlink -f "$base/current")"
  [[ "$old" == "$base/releases/"* && -f "$old/DEPLOY_COMMIT" ]] || {
    echo 'ERROR: current release link is invalid.' >&2; exit 2;
  }
elif [[ -e "$base/current" ]]; then
  echo 'ERROR: current must be a managed release symlink.' >&2; exit 2
fi
for unit in lab1030-observer.service lab1030-backup.timer lab1030-backup.service; do
  if systemctl is-active --quiet "$unit"; then sudo systemctl stop "$unit"; fi
done
exec 8>"$base/shared/collector.lock"
flock -n 8 || { echo "ERROR: a manually started collector is still running; update aborted." >&2; exit 2; }
if [[ -n "$old" && -f "$old/observer.py" ]]; then
  "$base/venv/bin/python" "$old/observer.py" backup --root "$base"
fi
user="$(id -un)"
# All generated paths are simple validated Linux paths; code has no pip dependencies.
unit_dir="$(mktemp -d "$base/.units.XXXXXXXX")"
for name in lab1030-observer.service lab1030-backup.service lab1030-backup.timer; do
  sed -e "s|@@ROOT@@|$base|g" -e "s|@@USER@@|$user|g" "$app/templates/$name" > "$unit_dir/$name"
done
if command -v systemd-analyze >/dev/null; then systemd-analyze verify "$unit_dir"/*; fi
for name in lab1030-observer.service lab1030-backup.service lab1030-backup.timer; do
  sudo install -m 644 "$unit_dir/$name" "/etc/systemd/system/$name"
done
rm -rf "$unit_dir"
if [[ -n "$old" && "$old" != "$release" ]]; then
  if [[ -L "$base/previous.new" ]]; then rm -- "$base/previous.new"; fi
  ln -s "$old" "$base/previous.new"
  mv -Tf "$base/previous.new" "$base/previous"
fi
if [[ -L "$base/current.new" ]]; then rm -- "$base/current.new"; fi
ln -s "$release" "$base/current.new"
mv -Tf "$base/current.new" "$base/current"
if [[ ! -L "$link" ]]; then ln -s "$base/current/lab.sh" "$link"; fi
sudo systemctl daemon-reload
# Disable only this new collector during installation, including on updates/reboots,
# until the user performs the public network checks and explicitly starts it.
sudo systemctl disable lab1030-observer.service lab1030-backup.timer >/dev/null
printf '\nInstalled commit: %s\n' "$commit"
printf 'Existing trading bots were NOT stopped, edited or restarted.\n'
printf 'Observer is NOT started. Next: ~/lab1030 doctor && ~/lab1030 start\n'
printf 'Config/data: %s\n' "$base"
