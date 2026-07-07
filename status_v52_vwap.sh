#!/usr/bin/env bash
set -euo pipefail
screen -ls | grep -E 'v52-vwap|No Sockets' || true
