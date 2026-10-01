#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ -x .venv/bin/python ]]; then
  exec .venv/bin/python -m basic_core.service --python "$PWD/.venv/bin/python" "$@"
fi
exec python3 -m basic_core.service "$@"
