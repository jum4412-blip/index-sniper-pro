#!/usr/bin/env bash
# Entry point fetched with git show. Does NOT git pull, switch, reset or clean.
set -Eeuo pipefail
umask 077
repo="${1:-$HOME/index-sniper-pro}"
ref="${2:-origin/main}"
[[ "$ref" != -* ]] || { echo 'ERROR: invalid git ref' >&2; exit 2; }
git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 || {
  echo "ERROR: $repo is not a Git repository. Do not run this from an extracted bot directory." >&2; exit 2;
}
# Only read Git objects. Current branch and tracked/untracked working files stay unchanged.
commit="$(git -C "$repo" rev-parse --verify "${ref}^{commit}")"
git -C "$repo" cat-file -e "$commit:trading_lab_ec2/install.sh" 2>/dev/null || {
  echo 'ERROR: trading_lab_ec2/install.sh not found in this commit.' >&2
  echo 'Upload the extracted trading_lab_ec2 folder to the repository root, then fetch the branch.' >&2
  exit 2
}
stage="$(mktemp -d "$HOME/.lab1030-stage.XXXXXXXX")"
trap 'rm -rf -- "$stage"' EXIT
git -C "$repo" archive --format=tar "$commit" -- trading_lab_ec2 | tar -xf - -C "$stage"
bash "$stage/trading_lab_ec2/install.sh" "$commit"
