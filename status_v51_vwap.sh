#!/usr/bin/env bash
set -euo pipefail
screen -ls | grep -E 'v51-vwap|No Sockets' || true
