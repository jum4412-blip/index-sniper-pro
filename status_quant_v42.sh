#!/usr/bin/env bash
set -euo pipefail
screen -ls | grep -E 'quant-v42|No Sockets' || true
