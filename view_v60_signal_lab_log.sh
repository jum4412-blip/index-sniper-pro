#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
touch logs/signal-lab.log
tail -f logs/signal-lab.log
