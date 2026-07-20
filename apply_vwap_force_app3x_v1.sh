#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${INDEX_SNIPER_ROOT:-$HOME/index-sniper-pro}"
PY="$ROOT/.venv/bin/python"
TARGET="$ROOT/index_sniper/vwap_video_live_v1.py"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/local_backups/vwap_force_app3x_$TS"

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
marker = "VWAP_FORCE_APP_3X_V1"

if marker in text:
    print("이미 force-app-3x v1이 적용되어 있습니다.")
    raise SystemExit(0)

# 1) Doctor pre-arm: keep account-level/hedge/flat checks, but allow the
#    stale symbolConfigList crossed/5 row to be bypassed only with an explicit
#    environment flag. All actual orders already carry marginMode=isolated.
old = '''                if prearm:
                    if row is None or str(row.get("marginMode", "")).lower() != "isolated":
                        report["errors"].append(f"{symbol}: isolated not verified")
                    leverage_values = leverage_values_from_row(row)
                    if not leverage_values or any(x != 3 for x in leverage_values):
                        report["errors"].append(f"{symbol}: 3x not verified ({leverage_values or 'missing'})")
'''
new = '''                if prearm:
                    trust_app_3x = os.getenv("VWAP_TRUST_APP_3X", "") == "YES"
                    if trust_app_3x:
                        # VWAP_FORCE_APP_3X_V1
                        item["prearm_override"] = "trust_app_3x_first_fill_guard"
                    else:
                        if row is None or str(row.get("marginMode", "")).lower() != "isolated":
                            report["errors"].append(f"{symbol}: isolated not verified")
                        leverage_values = leverage_values_from_row(row)
                        if not leverage_values or any(x != 3 for x in leverage_values):
                            report["errors"].append(f"{symbol}: 3x not verified ({leverage_values or 'missing'})")
'''
if old not in text:
    raise SystemExit("doctor prearm block을 찾지 못해 중단합니다.")
text = text.replace(old, new, 1)

# 2) After the first actual fill, verify the real position row returned by
#    /current-position. If it is not isolated 3x, disarm and close immediately.
old = '''        self.cancel_symbol_entries(rt, "entry_filled")
        self.event(rt.symbol, "POSITION_ADOPTED", asdict(rt.position))
        asyncio.create_task(self.notify(
'''
new = '''        self.cancel_symbol_entries(rt, "entry_filled")
        self.event(rt.symbol, "POSITION_ADOPTED", asdict(rt.position))

        actual_margin = str(row.get("marginMode", "")).lower()
        actual_leverage_values = leverage_values_from_row(row)
        actual_leverage_ok = bool(actual_leverage_values) and all(
            value == 3 for value in actual_leverage_values
        )
        if actual_margin != "isolated" or not actual_leverage_ok:
            mismatch = {
                "marginMode": actual_margin or "missing",
                "leverage": actual_leverage_values or "missing",
                "row": row,
            }
            self.event(rt.symbol, "FIRST_FILL_MODE_MISMATCH", mismatch)
            disarm(self.cfg)
            self.account_block_reason = f"first_fill_mode_mismatch_{rt.symbol}"
            asyncio.create_task(self.notify(
                f"🛑 VWAP FIRST FILL MISMATCH {rt.symbol} {side.upper()}\\n"
                f"actual margin={actual_margin or 'missing'} "
                f"leverage={actual_leverage_values or 'missing'}\\n"
                "봇을 DISARM하고 즉시 청산을 시도합니다."
            ))
            self.market_close(rt, rt.position, "first_fill_mode_mismatch")
            return

        asyncio.create_task(self.notify(
'''
if old not in text:
    raise SystemExit("adopt_position injection point를 찾지 못해 중단합니다.")
text = text.replace(old, new, 1)

# 3) Closing fallback: if an exchange position is unexpectedly crossed,
#    retry closing with crossed marginMode. In hedge mode side+posSide remains
#    a close operation for the tracked position direction.
old = '''        response = self.query_ambiguous(client_oid, client_post(self.client, "/api/v3/trade/place-order", payload))
        if not api_success(response):
            payload.pop("reduceOnly", None)
            response = self.query_ambiguous(client_oid, client_post(self.client, "/api/v3/trade/place-order", payload))
        require_success(response, f"close {rt.symbol} {position.side}")
'''
new = '''        response = self.query_ambiguous(client_oid, client_post(self.client, "/api/v3/trade/place-order", payload))
        if not api_success(response):
            payload.pop("reduceOnly", None)
            response = self.query_ambiguous(client_oid, client_post(self.client, "/api/v3/trade/place-order", payload))
        if not api_success(response):
            payload["marginMode"] = "crossed"
            payload["reduceOnly"] = "yes"
            response = self.query_ambiguous(client_oid, client_post(self.client, "/api/v3/trade/place-order", payload))
        if not api_success(response):
            payload.pop("reduceOnly", None)
            response = self.query_ambiguous(client_oid, client_post(self.client, "/api/v3/trade/place-order", payload))
        require_success(response, f"close {rt.symbol} {position.side}")
'''
if old not in text:
    raise SystemExit("market_close fallback block을 찾지 못해 중단합니다.")
text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
print(f"patched: {path}")
PY_PATCH

cat > "$ROOT/doctor_vwap_video_force_app3x.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_vwap_video_common.sh"
cd "$ROOT"
VWAP_TRUST_APP_3X=YES \
  "$PY" -m index_sniper.vwap_video_live_v1 \
  --config "$CONFIG" doctor --prearm
SH

cat > "$ROOT/arm_vwap_video_force_app3x.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname "$0")/_vwap_video_common.sh"
cd "$ROOT"

if [[ "$#" -ne 4 ]]; then
  cat >&2 <<'USAGE'
사용법:
  bash arm_vwap_video_force_app3x.sh \
    START_VWAP_VIDEO_TOP10_LIVE_3X \
    I_CONFIRM_REAL_ORDERS_MINIMUM_QTY \
    API_HAS_NO_WITHDRAW_PERMISSION \
    DEDICATED_SUBACCOUNT_ONLY
USAGE
  exit 2
fi

VWAP_TRUST_APP_3X=YES \
  "$PY" -m index_sniper.vwap_video_live_v1 \
  --config "$CONFIG" arm "$@"
set_env_value VWAP_VIDEO_LIVE_ENABLED true
echo "✅ VWAP FORCE-APP-3X ARMED"
echo "첫 실제 체결 직후 position API의 marginMode/leverage를 검사합니다."
echo "isolated 3x가 아니면 자동 DISARM + 즉시 청산을 시도합니다."
SH

chmod 755 \
  "$ROOT/doctor_vwap_video_force_app3x.sh" \
  "$ROOT/arm_vwap_video_force_app3x.sh"

cd "$ROOT"
PYTHONPATH="$ROOT" "$PY" -m py_compile index_sniper/vwap_video_live_v1.py

# Static contract checks. These do not place orders.
PYTHONPATH="$ROOT" "$PY" - <<'PY_TEST'
from pathlib import Path
p = Path("index_sniper/vwap_video_live_v1.py")
text = p.read_text(encoding="utf-8")
checks = {
    "entry_order_forces_isolated": '"marginMode": "isolated"' in text,
    "force_override_marker": "VWAP_FORCE_APP_3X_V1" in text,
    "first_fill_guard": "FIRST_FILL_MODE_MISMATCH" in text,
    "crossed_close_fallback": 'payload["marginMode"] = "crossed"' in text,
}
print(checks)
if not all(checks.values()):
    raise SystemExit("static contract check failed")
PY_TEST

cat <<EOF

✅ VWAP force-app-3x v1 적용 완료

핵심:
  주문마다 marginMode=isolated를 강제
  주문 API에는 leverage 필드가 없으므로 앱의 종목별 3x 설정을 사용
  stale crossed/5 settings row만 명시적으로 우회
  첫 실제 체결 후 실제 position row가 isolated 3x인지 검사
  불일치 시 자동 DISARM + 즉시 청산 시도

먼저 강제 사전검사:
  bash doctor_vwap_video_force_app3x.sh

ARM:
  bash arm_vwap_video_force_app3x.sh \
    START_VWAP_VIDEO_TOP10_LIVE_3X \
    I_CONFIRM_REAL_ORDERS_MINIMUM_QTY \
    API_HAS_NO_WITHDRAW_PERMISSION \
    DEDICATED_SUBACCOUNT_ONLY

시작:
  bash start_vwap_video_live.sh

백업:
  $BACKUP

EOF
