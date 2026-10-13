#!/usr/bin/env bash
set -Eeuo pipefail
app="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="${LAB_PYTHON:-python3}"
export PYTHONDONTWRITEBYTECODE=1
"$python" -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10 or later is required"'
(cd "$app/core" && "$python" -m unittest discover -s tests -v)
(cd "$app" && "$python" -m unittest discover -s tests -v)
for script in "$app"/*.sh; do bash -n "$script"; done
