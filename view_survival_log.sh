#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
tail -f logs/sniper-survival-dry.log
