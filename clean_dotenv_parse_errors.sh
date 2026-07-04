#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  echo ".env not found"
  exit 1
fi
bak=".env.bak.clean.$(date +%Y%m%d_%H%M%S)"
cp .env "$bak"
invalid=".env.invalid_lines.$(date +%Y%m%d_%H%M%S).txt"
awk '
  /^[[:space:]]*$/ {print; next}
  /^[[:space:]]*#/ {print; next}
  /^[A-Za-z_][A-Za-z0-9_]*=.*/ {print; next}
  {print NR ":" $0 >> invalid_file}
' invalid_file="$invalid" "$bak" > .env
if [ -s "$invalid" ]; then
  echo "Cleaned invalid dotenv lines. Backup: $bak"
  echo "Removed lines saved: $invalid"
  cat "$invalid"
else
  rm -f "$invalid"
  echo "No invalid dotenv lines found. Backup: $bak"
fi
