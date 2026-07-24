#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_btc_structure_common.sh"
"$PY" -m "$MODULE" --config "$CONFIG" doctor "$@"
