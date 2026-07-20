#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${INDEX_SNIPER_ROOT:-$HOME/index-sniper-pro}"
PY="$ROOT/.venv/bin/python"
TARGET="$ROOT/index_sniper/vwap_video_live_v1.py"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/local_backups/vwap_video_isolated_mode_fix_$TS"

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

old_leverage = '''def leverage_values_from_row(row: dict[str, Any] | None) -> list[int]:
    if not isinstance(row, dict):
        return []
    values: list[int] = []
    for key in ("leverage", "longLeverage", "shortLeverage"):
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            values.extend(safe_int(x) for x in value if x not in (None, ""))
        elif value not in (None, ""):
            values.append(safe_int(value))
    return [x for x in values if x > 0]
'''

new_leverage = '''def leverage_values_from_row(row: dict[str, Any] | None) -> list[int]:
    if not isinstance(row, dict):
        return []

    # In isolated hedge mode Bitget may expose long/short leverage separately.
    # When those fields exist, they are authoritative and the generic leverage
    # value can be an old crossed-mode value.
    directional: list[int] = []
    for key in ("longLeverage", "shortLeverage"):
        value = row.get(key)
        if isinstance(value, (list, tuple)):
            directional.extend(safe_int(x) for x in value if x not in (None, ""))
        elif value not in (None, ""):
            directional.append(safe_int(value))
    directional = [x for x in directional if x > 0]
    if directional:
        return directional

    value = row.get("leverage")
    if isinstance(value, (list, tuple)):
        return [safe_int(x) for x in value if x not in (None, "") and safe_int(x) > 0]
    if value not in (None, "") and safe_int(value) > 0:
        return [safe_int(value)]
    return []
'''

if old_leverage not in text:
    raise SystemExit("leverage_values_from_row 블록을 찾지 못했습니다.")
text = text.replace(old_leverage, new_leverage, 1)

helper_marker = '\n\ndef account_symbol_row(settings: dict[str, Any], symbol: str) -> dict[str, Any] | None:\n'
helper = '''\n\ndef account_level(settings: dict[str, Any]) -> str:\n    return str(settings.get("accountLevel", "")).strip().lower()\n'''
if "def account_level(" not in text:
    if helper_marker not in text:
        raise SystemExit("account helper 삽입 위치를 찾지 못했습니다.")
    text = text.replace(helper_marker, helper + helper_marker, 1)

setup_start = text.index("def setup_account(cfg: dict[str, Any]) -> dict[str, Any]:")
setup_end = text.index("\n\ndef doctor(cfg: dict[str, Any], *, prearm: bool = False)", setup_start)

new_setup = '''def setup_account(cfg: dict[str, Any]) -> dict[str, Any]:
    universe = refresh_universe(cfg, allow_cache=False)
    symbols = [str(x).upper() for x in universe["symbols"]]
    update_config_symbols(cfg, symbols)
    cfg = load_config(cfg["config_path"])
    client = make_client()

    positions = [x for x in fetch_positions(client) if abs(safe_float(x.get("total"))) > 0]
    orders = fetch_open_orders(client)
    strategies = fetch_strategy_orders(client)
    if positions or orders or strategies:
        raise RuntimeError(
            f"setup requires flat dedicated account: positions={len(positions)} "
            f"orders={len(orders)} strategies={len(strategies)}"
        )

    # A per-order marginMode field is not enough to convert a UTA account that
    # is still in basic/advanced crossed mode. Switch the dedicated flat
    # account itself to isolated margin mode first, then enable hedge mode.
    settings = fetch_account_settings(client)
    initial_level = account_level(settings)
    mode_result: Any = {"already": initial_level}
    if initial_level != "isolated":
        if os.getenv("VWAP_CONFIRM_ISOLATED_ACCOUNT_MODE", "") != "YES":
            raise RuntimeError(
                "accountLevel is not isolated. Re-run setup with "
                "VWAP_CONFIRM_ISOLATED_ACCOUNT_MODE=YES on the dedicated flat account."
            )
        mode_result = require_success(
            client_post(client, "/api/v3/account/adjust-account-mode", {"mode": "isolated"}),
            "set UTA account mode isolated",
        )
        deadline = time.time() + 45.0
        while time.time() < deadline:
            time.sleep(1.0)
            settings = fetch_account_settings(client)
            if account_level(settings) == "isolated":
                break
        else:
            raise RuntimeError(
                f"UTA account mode did not become isolated; current={account_level(settings) or 'missing'}"
            )

    hold_result = require_success(
        client_post(client, "/api/v3/account/set-hold-mode", {"holdMode": "hedge_mode"}),
        "set hedge mode",
    )
    deadline = time.time() + 20.0
    while time.time() < deadline:
        time.sleep(0.75)
        settings = fetch_account_settings(client)
        if account_hold_mode(settings) == "hedge_mode":
            break
    else:
        raise RuntimeError(
            f"hold mode did not become hedge_mode; current={account_hold_mode(settings) or 'missing'}"
        )

    results: dict[str, Any] = {}

    def verified(symbol: str) -> tuple[bool, dict[str, Any] | None, list[int]]:
        current = fetch_account_settings(client)
        row = account_symbol_row(current, symbol)
        leverages = leverage_values_from_row(row)
        ok = (
            row is not None
            and str(row.get("marginMode", "")).lower() == "isolated"
            and bool(leverages)
            and all(x == 3 for x in leverages)
        )
        return ok, row, leverages

    for symbol in symbols:
        instrument = fetch_instrument(client, symbol)
        if instrument.status != "online" or instrument.symbol_type != "crypto" or instrument.is_reality == "yes":
            raise RuntimeError(f"invalid instrument {symbol}: {instrument}")
        if not (instrument.min_leverage <= 3 <= instrument.max_leverage):
            raise RuntimeError(f"{symbol} does not support 3x")

        # One combined request first. This avoids the previous 20-call burst
        # against a 10 requests/sec endpoint and gives the account time to
        # commit each symbol configuration.
        payload = {
            "category": CATEGORY,
            "symbol": symbol,
            "leverage": "3",
            "longLeverage": "3",
            "shortLeverage": "3",
            "posSide": "long",
            "marginMode": "isolated",
        }
        calls: list[Any] = [
            require_success(
                client_post(client, "/api/v3/account/set-leverage", payload),
                f"set {symbol} isolated hedge 3x",
            )
        ]

        ok = False
        row: dict[str, Any] | None = None
        leverages: list[int] = []
        for _ in range(12):
            time.sleep(0.5)
            ok, row, leverages = verified(symbol)
            if ok:
                break

        # Some UTA accounts commit the short-side setting only after an
        # explicit short request. Retry once, then poll again.
        if not ok:
            payload_short = dict(payload)
            payload_short["posSide"] = "short"
            calls.append(
                require_success(
                    client_post(client, "/api/v3/account/set-leverage", payload_short),
                    f"confirm {symbol} isolated hedge short 3x",
                )
            )
            for _ in range(20):
                time.sleep(0.5)
                ok, row, leverages = verified(symbol)
                if ok:
                    break

        if not ok:
            raise RuntimeError(
                f"{symbol} setup not committed: "
                f"marginMode={(row or {}).get('marginMode')} leverage={leverages or 'missing'} row={row}"
            )
        results[symbol] = calls
        time.sleep(0.15)

    settings = fetch_account_settings(client)
    hold = account_hold_mode(settings)
    level = account_level(settings)
    errors: list[str] = []
    if level != "isolated":
        errors.append(f"accountLevel={level}, expected isolated")
    if hold != "hedge_mode":
        errors.append(f"holdMode={hold}, expected hedge_mode")
    for symbol in symbols:
        row = account_symbol_row(settings, symbol)
        if row is None:
            errors.append(f"{symbol}: account symbol config missing")
            continue
        if str(row.get("marginMode", "")).lower() != "isolated":
            errors.append(f"{symbol}: marginMode={row.get('marginMode')}")
        leverage_values = leverage_values_from_row(row)
        if not leverage_values or any(x != 3 for x in leverage_values):
            errors.append(f"{symbol}: leverage={leverage_values or 'missing'}, expected 3")
    if errors:
        raise RuntimeError("account setup verification failed: " + "; ".join(errors))

    return {
        "ok": True,
        "account_level": level,
        "hold_mode": hold,
        "margin_mode": "isolated",
        "leverage": 3,
        "symbols": symbols,
        "mode_result": mode_result,
        "hold_result": hold_result,
        "results": results,
    }
'''

text = text[:setup_start] + new_setup + text[setup_end:]

# Expose and verify accountLevel in doctor --prearm.
old_account = '''        report["account"] = {
            "equity": safe_float(assets.get("accountEquity") or assets.get("usdtEquity") or assets.get("effEquity")),
            "hold_mode": account_hold_mode(settings),
            "positions": len(positions),
            "open_orders": len(orders),
            "strategy_orders": len(strategies),
        }
'''
new_account = '''        report["account"] = {
            "equity": safe_float(assets.get("accountEquity") or assets.get("usdtEquity") or assets.get("effEquity")),
            "account_level": account_level(settings),
            "hold_mode": account_hold_mode(settings),
            "positions": len(positions),
            "open_orders": len(orders),
            "strategy_orders": len(strategies),
        }
'''
if old_account not in text:
    raise SystemExit("doctor account report 블록을 찾지 못했습니다.")
text = text.replace(old_account, new_account, 1)

old_prearm = '''        if prearm:
            if account_hold_mode(settings) != "hedge_mode":
                report["errors"].append("account hold mode is not hedge_mode")
'''
new_prearm = '''        if prearm:
            if account_level(settings) != "isolated":
                report["errors"].append(
                    f"account level is not isolated ({account_level(settings) or 'missing'})"
                )
            if account_hold_mode(settings) != "hedge_mode":
                report["errors"].append("account hold mode is not hedge_mode")
'''
if old_prearm not in text:
    raise SystemExit("doctor prearm 블록을 찾지 못했습니다.")
text = text.replace(old_prearm, new_prearm, 1)

path.write_text(text, encoding="utf-8")
print(f"patched: {path}")
PY_PATCH

cd "$ROOT"
PYTHONPATH="$ROOT" "$PY" -m py_compile index_sniper/vwap_video_live_v1.py
PYTHONPATH="$ROOT" "$PY" -m index_sniper.vwap_video_live_v1 \
  --config config/vwap_video_live_v1.json self-test

cat <<EOF

✅ VWAP VIDEO UTA isolated-account setup fix 적용 완료

수정 사항:
- 전용 flat 계정을 UTA isolated account mode로 먼저 전환
- hedge_mode 확인 후 종목별 3x 설정
- 종목마다 설정 반영을 polling하여 확인
- 10 req/sec를 넘기던 일괄 20호출 제거
- doctor --prearm에서 accountLevel=isolated까지 검증

백업: $BACKUP

다시 실행:
  bash setup_vwap_video_account.sh

성공 후:
  bash doctor_vwap_video_live.sh --prearm
EOF
