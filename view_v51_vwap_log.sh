#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
tail -f logs/v51-vwap-top20.log
