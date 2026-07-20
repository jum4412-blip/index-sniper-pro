#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${INDEX_SNIPER_ROOT:-$HOME/index-sniper-pro}"
PY="$ROOT/.venv/bin/python"
TARGET="$ROOT/index_sniper/vwap_video_live_v1.py"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/local_backups/vwap_video_live_leverage_fix_$TS"

if [[ ! -x "$PY" || ! -f "$TARGET" ]]; then
  echo "프로젝트 또는 대상 파일을 찾을 수 없습니다: $ROOT" >&2
  exit 1
fi

mkdir -p "$BACKUP/index_sniper"
cp -a "$TARGET" "$BACKUP/index_sniper/"

"$PY" - "$TARGET" <<'PY_PATCH'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

old = '''        side_results = []
        for side in ("long", "short"):
            payload = {
                "category": CATEGORY,
                "symbol": symbol,
                "leverage": "3",
                "posSide": side,
                "marginMode": "isolated",
            }
            side_results.append(require_success(client_post(client, "/api/v3/account/set-leverage", payload), f"set {symbol} {side} 3x"))
        results[symbol] = side_results
'''

new = '''        # Bitget UTA의 hedge(two-way) + isolated 조합에서는
        # 일부 계정에서 posSide + leverage만 보내면 code 25200
        # (afterShortLeverage/afterLongLeverage none)가 발생한다.
        # longLeverage와 shortLeverage를 함께 명시해 양 방향을 3x로 설정한다.
        payload = {
            "category": CATEGORY,
            "symbol": symbol,
            "leverage": "3",
            "longLeverage": "3",
            "shortLeverage": "3",
            "posSide": "long",
            "marginMode": "isolated",
        }
        first = require_success(
            client_post(client, "/api/v3/account/set-leverage", payload),
            f"set {symbol} hedge isolated long/short 3x",
        )

        # 일부 UTA 계정은 posSide별 호출도 요구하므로 short 방향을 한 번 더 확정한다.
        payload_short = dict(payload)
        payload_short["posSide"] = "short"
        second = require_success(
            client_post(client, "/api/v3/account/set-leverage", payload_short),
            f"confirm {symbol} hedge isolated short 3x",
        )
        results[symbol] = [first, second]
'''

if new in text:
    print("이미 leverage fix가 적용되어 있습니다.")
elif old not in text:
    raise SystemExit(
        "예상한 leverage 설정 블록을 찾지 못했습니다. "
        "다른 버전에 무리하게 적용하지 않고 중단합니다."
    )
else:
    text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    print(f"patched: {path}")
PY_PATCH

cd "$ROOT"
PYTHONPATH="$ROOT" "$PY" -m py_compile index_sniper/vwap_video_live_v1.py

echo
echo "✅ Bitget UTA hedge isolated leverage 25200 수정 완료"
echo "백업: $BACKUP"
echo
echo "다시 실행:"
echo "  bash setup_vwap_video_account.sh"
echo
echo "그 다음 검사:"
echo "  bash doctor_vwap_video_live.sh --prearm"
