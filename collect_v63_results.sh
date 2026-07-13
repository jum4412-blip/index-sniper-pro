#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
DAYS="${1:-30}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$HOME/v63_result_bundle_$STAMP"
ARCHIVE="$OUT.tar.gz"
rm -rf "$OUT"
mkdir -p "$OUT"
cp -f README_BTC_ETH_V63_LIVE.md "$OUT/" 2>/dev/null || true
cp -f config/v63_dual_live.json "$OUT/" 2>/dev/null || true
cp -f data/v63_dual_live/state.json "$OUT/" 2>/dev/null || true
cp -f data/v63_dual_live/trades.csv "$OUT/" 2>/dev/null || true
PYTHONPATH="$ROOT" "$PY" -m index_sniper.dual_live_v63 report --days "$DAYS" > "$OUT/report_${DAYS}d.json" || true
ROOT_ENV="$ROOT" OUT_ENV="$OUT" DAYS_ENV="$DAYS" "$PY" - <<'PY'
from pathlib import Path
import datetime as dt, json, os
root=Path(os.environ['ROOT_ENV']); out=Path(os.environ['OUT_ENV']); days=int(os.environ['DAYS_ENV'])
cut=dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=days)
def parse(v):
    try:
        x=dt.datetime.fromisoformat(str(v).replace('Z','+00:00'))
        if x.tzinfo is None: x=x.replace(tzinfo=dt.timezone.utc)
        return x.astimezone(dt.timezone.utc)
    except Exception: return None
for src_name,dst_name in [('events.jsonl','events_filtered.jsonl'),('snapshots.jsonl','snapshots_filtered.jsonl')]:
    src=root/'data/v63_dual_live'/src_name
    dst=out/dst_name
    if not src.exists(): continue
    with src.open('r',encoding='utf-8',errors='ignore') as r, dst.open('w',encoding='utf-8') as w:
        for line in r:
            try: obj=json.loads(line)
            except Exception: continue
            t=parse(obj.get('ts'))
            if t and t>=cut:
                w.write(json.dumps(obj,ensure_ascii=False,separators=(',',':'))+'\n')
log=root/'logs/v63-dual-live.log'
if log.exists():
    lines=log.read_text(encoding='utf-8',errors='ignore').splitlines()[-10000:]
    (out/'v63_log_tail.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
PY
# Final redaction pass. .env is never copied.
OUT_ENV="$OUT" "$PY" - <<'PY'
from pathlib import Path
import os,re
out=Path(os.environ['OUT_ENV'])
patterns=[
 (re.compile(r'(?i)\b(TELEGRAM_TOKEN|TELEGRAM_BOT_TOKEN|BITGET_API_KEY|BITGET_SECRET_KEY|BITGET_API_SECRET|BITGET_PASSPHRASE|API_KEY|SECRET_KEY|PASSWORD|PASSPHRASE)\s*[:=]\s*["\']?[^,\s"\']+'),r'\1=[REDACTED]'),
 (re.compile(r'\b\d{8,}:[A-Za-z0-9_-]{20,}\b'),'[REDACTED_TELEGRAM_TOKEN]'),
 (re.compile(r'\bbg_[A-Za-z0-9]{20,}\b',re.I),'[REDACTED_BITGET_KEY]'),
]
for p in out.rglob('*'):
    if not p.is_file() or p.stat().st_size>100*1024*1024: continue
    try: text=p.read_text(encoding='utf-8',errors='ignore')
    except Exception: continue
    new=text
    for rg,repl in patterns: new=rg.sub(repl,new)
    if new!=text: p.write_text(new,encoding='utf-8')
PY
tar -C "$HOME" -czf "$ARCHIVE" "$(basename "$OUT")"
rm -rf "$OUT"
echo "✅ 결과 묶음 생성: $ARCHIVE"
ls -lh "$ARCHIVE"
