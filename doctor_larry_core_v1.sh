#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_larry_common.sh"
exec "$PY" -m index_sniper.larry_williams_core_v1 --config "$CONFIG" doctor "$@"
