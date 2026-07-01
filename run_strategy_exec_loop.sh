#!/usr/bin/env bash
set -euo pipefail
source venv/bin/activate
PYTHONUNBUFFERED=1 python -u main.py --mode strategy-exec-loop
