#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${INDEX_SNIPER_ROOT:-$HOME/index-sniper-pro}"
PY="$ROOT/.venv/bin/python"
TARGET="$ROOT/index_sniper/vwap_video_live_v1.py"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/local_backups/vwap_video_isolated_per_side_fix_$TS"

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

marker = "VWAP_VIDEO_ISOLATED_PER_SIDE_V3"
if marker in text:
    print("이미 v3 per-side isolated fix가 적용되어 있습니다.")
    raise SystemExit(0)

setup_start = text.index("def setup_account(cfg: dict[str, Any]) -> dict[str, Any]:")
setup_end = text.index(
    "\n\ndef doctor(cfg: dict[str, Any], *, prearm: bool = False)",
    setup_start,
)

new_setup = r'''def setup_account(cfg: dict[str, Any]) -> dict[str, Any]:
    # VWAP_VIDEO_ISOLATED_PER_SIDE_V3
    universe = refresh_universe(cfg, allow_cache=False)
    symbols = [str(x).upper() for x in universe["symbols"]]
    update_config_symbols(cfg, symbols)
    cfg = load_config(cfg["config_path"])
    client = make_client()

    positions = [
        x for x in fetch_positions(client)
        if abs(safe_float(x.get("total"))) > 0
    ]
    orders = fetch_open_orders(client)
    strategies = fetch_strategy_orders(client)

    if positions or orders or strategies:
        raise RuntimeError(
            "setup requires flat dedicated account: "
            f"positions={len(positions)} "
            f"orders={len(orders)} "
            f"strategies={len(strategies)}"
        )

    settings = fetch_account_settings(client)
    initial_level = account_level(settings)
    initial_hold = account_hold_mode(settings)

    print(
        json.dumps(
            {
                "stage": "before_setup",
                "accountLevel": initial_level,
                "holdMode": initial_hold,
                "symbols": symbols,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )

    mode_result: Any = {"already": initial_level}
    if initial_level != "isolated":
        if os.getenv("VWAP_CONFIRM_ISOLATED_ACCOUNT_MODE", "") != "YES":
            raise RuntimeError(
                "accountLevel is not isolated. Re-run with "
                "VWAP_CONFIRM_ISOLATED_ACCOUNT_MODE=YES"
            )

        mode_result = require_success(
            client_post(
                client,
                "/api/v3/account/adjust-account-mode",
                {"mode": "isolated"},
            ),
            "set UTA account mode isolated",
        )

        deadline = time.time() + 60.0
        while time.time() < deadline:
            time.sleep(1.0)
            settings = fetch_account_settings(client)
            if account_level(settings) == "isolated":
                break
        else:
            raise RuntimeError(
                "UTA account mode did not become isolated; "
                f"current={account_level(settings) or 'missing'}"
            )

    hold_result = require_success(
        client_post(
            client,
            "/api/v3/account/set-hold-mode",
            {"holdMode": "hedge_mode"},
        ),
        "set hedge mode",
    )

    deadline = time.time() + 30.0
    while time.time() < deadline:
        time.sleep(0.75)
        settings = fetch_account_settings(client)
        if account_hold_mode(settings) == "hedge_mode":
            break
    else:
        raise RuntimeError(
            "hold mode did not become hedge_mode; "
            f"current={account_hold_mode(settings) or 'missing'}"
        )

    def row_state(symbol: str) -> tuple[dict[str, Any] | None, list[int]]:
        current = fetch_account_settings(client)
        row = account_symbol_row(current, symbol)
        values = leverage_values_from_row(row)
        return row, values

    def row_is_ready(
        row: dict[str, Any] | None,
        values: list[int],
    ) -> bool:
        return bool(
            row is not None
            and str(row.get("marginMode", "")).lower() == "isolated"
            and values
            and all(value == 3 for value in values)
        )

    results: dict[str, Any] = {}

    for index, symbol in enumerate(symbols, start=1):
        instrument = fetch_instrument(client, symbol)

        if (
            instrument.status != "online"
            or instrument.symbol_type != "crypto"
            or instrument.is_reality == "yes"
        ):
            raise RuntimeError(f"invalid instrument {symbol}: {instrument}")

        if not (instrument.min_leverage <= 3 <= instrument.max_leverage):
            raise RuntimeError(f"{symbol} does not support 3x")

        before_row, before_values = row_state(symbol)
        print(
            json.dumps(
                {
                    "stage": "symbol_before",
                    "progress": f"{index}/{len(symbols)}",
                    "symbol": symbol,
                    "row": before_row,
                    "leverage_values": before_values,
                },
                ensure_ascii=False,
                default=str,
            ),
            flush=True,
        )

        # Official UTA isolated-margin form:
        # posSide is required, so set LONG and SHORT separately.
        # Do not mix same-leverage requests with longLeverage/shortLeverage.
        calls: list[dict[str, Any]] = []

        for side in ("long", "short"):
            payload = {
                "category": CATEGORY,
                "symbol": symbol,
                "leverage": "3",
                "posSide": side,
                "marginMode": "isolated",
            }

            try:
                response = client_post(
                    client,
                    "/api/v3/account/set-leverage",
                    payload,
                )
                response = require_success(
                    response,
                    f"set {symbol} {side} isolated 3x",
                )
            except Exception as exc:
                row, values = row_state(symbol)
                raise RuntimeError(
                    f"{symbol} {side} isolated 3x request failed: "
                    f"{type(exc).__name__}: {exc}; "
                    f"current_row={row}; leverage={values or 'missing'}"
                ) from exc

            calls.append(
                {
                    "side": side,
                    "payload": payload,
                    "response": response,
                }
            )

            # Avoid rate bursts and allow the exchange to commit the side.
            time.sleep(1.1)

        ready = False
        final_row: dict[str, Any] | None = None
        final_values: list[int] = []

        deadline = time.time() + 45.0
        while time.time() < deadline:
            final_row, final_values = row_state(symbol)
            if row_is_ready(final_row, final_values):
                ready = True
                break
            time.sleep(1.0)

        if not ready:
            raise RuntimeError(
                f"{symbol} per-side setup not committed after 45s: "
                f"marginMode={(final_row or {}).get('marginMode')} "
                f"leverage={final_values or 'missing'} "
                f"row={final_row} "
                f"calls={calls}"
            )

        results[symbol] = {
            "calls": calls,
            "verified_row": final_row,
            "verified_leverage": final_values,
        }

        print(
            json.dumps(
                {
                    "stage": "symbol_ok",
                    "progress": f"{index}/{len(symbols)}",
                    "symbol": symbol,
                    "marginMode": final_row.get("marginMode"),
                    "leverage": final_values,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    settings = fetch_account_settings(client)
    level = account_level(settings)
    hold = account_hold_mode(settings)
    errors: list[str] = []

    if level != "isolated":
        errors.append(f"accountLevel={level}, expected isolated")
    if hold != "hedge_mode":
        errors.append(f"holdMode={hold}, expected hedge_mode")

    for symbol in symbols:
        row = account_symbol_row(settings, symbol)
        values = leverage_values_from_row(row)

        if row is None:
            errors.append(f"{symbol}: account symbol config missing")
            continue
        if str(row.get("marginMode", "")).lower() != "isolated":
            errors.append(f"{symbol}: marginMode={row.get('marginMode')}")
        if not values or any(value != 3 for value in values):
            errors.append(
                f"{symbol}: leverage={values or 'missing'}, expected 3"
            )

    if errors:
        raise RuntimeError(
            "account setup final verification failed: "
            + "; ".join(errors)
        )

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
path.write_text(text, encoding="utf-8")
print(f"patched: {path}")
PY_PATCH

cat > "$ROOT/test_vwap_btc_isolated_3x.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

VWAP_CONFIRM_ISOLATED_ACCOUNT_MODE=YES \
PYTHONPATH="$PWD" .venv/bin/python - <<'PY'
import json
import os
import time

from index_sniper import vwap_video_live_v1 as v

cfg = v.load_config(v.DEFAULT_CONFIG)
client = v.make_client()

positions = [
    x for x in v.fetch_positions(client)
    if abs(v.safe_float(x.get("total"))) > 0
]
orders = v.fetch_open_orders(client)
strategies = v.fetch_strategy_orders(client)

if positions or orders or strategies:
    raise SystemExit(
        f"flat account required: positions={len(positions)} "
        f"orders={len(orders)} strategies={len(strategies)}"
    )

settings = v.fetch_account_settings(client)

if v.account_level(settings) != "isolated":
    v.require_success(
        v.client_post(
            client,
            "/api/v3/account/adjust-account-mode",
            {"mode": "isolated"},
        ),
        "set isolated account mode",
    )
    for _ in range(60):
        time.sleep(1)
        settings = v.fetch_account_settings(client)
        if v.account_level(settings) == "isolated":
            break
    else:
        raise SystemExit("accountLevel did not become isolated")

v.require_success(
    v.client_post(
        client,
        "/api/v3/account/set-hold-mode",
        {"holdMode": "hedge_mode"},
    ),
    "set hedge mode",
)

for _ in range(30):
    time.sleep(1)
    settings = v.fetch_account_settings(client)
    if v.account_hold_mode(settings) == "hedge_mode":
        break
else:
    raise SystemExit("holdMode did not become hedge_mode")

responses = []

for side in ("long", "short"):
    payload = {
        "category": v.CATEGORY,
        "symbol": "BTCUSDT",
        "leverage": "3",
        "posSide": side,
        "marginMode": "isolated",
    }
    try:
        response = v.client_post(
            client,
            "/api/v3/account/set-leverage",
            payload,
        )
        responses.append(
            {
                "side": side,
                "payload": payload,
                "response": response,
            }
        )
    except Exception as exc:
        settings = v.fetch_account_settings(client)
        row = v.account_symbol_row(settings, "BTCUSDT")
        print(
            json.dumps(
                {
                    "ok": False,
                    "failed_side": side,
                    "error": f"{type(exc).__name__}: {exc}",
                    "accountLevel": v.account_level(settings),
                    "holdMode": v.account_hold_mode(settings),
                    "btc_row": row,
                    "responses": responses,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        raise
    time.sleep(1.1)

deadline = time.time() + 45
while time.time() < deadline:
    settings = v.fetch_account_settings(client)
    row = v.account_symbol_row(settings, "BTCUSDT")
    values = v.leverage_values_from_row(row)
    if (
        row
        and str(row.get("marginMode", "")).lower() == "isolated"
        and values
        and all(x == 3 for x in values)
    ):
        print(
            json.dumps(
                {
                    "ok": True,
                    "accountLevel": v.account_level(settings),
                    "holdMode": v.account_hold_mode(settings),
                    "btc_row": row,
                    "leverage_values": values,
                    "responses": responses,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        raise SystemExit(0)
    time.sleep(1)

print(
    json.dumps(
        {
            "ok": False,
            "reason": "BTC setting not committed after 45 seconds",
            "accountLevel": v.account_level(settings),
            "holdMode": v.account_hold_mode(settings),
            "btc_row": row,
            "leverage_values": values,
            "responses": responses,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )
)
raise SystemExit(2)
PY
SH

chmod 755 "$ROOT/test_vwap_btc_isolated_3x.sh"

cd "$ROOT"
PYTHONPATH="$ROOT" "$PY" -m py_compile \
  index_sniper/vwap_video_live_v1.py

echo
echo "✅ VWAP isolated 3x per-side v3 fix 적용 완료"
echo "백업: $BACKUP"
echo
echo "먼저 BTC 단일 설정 검사:"
echo "  bash test_vwap_btc_isolated_3x.sh"
echo
echo "BTC 결과가 ok=true이면 전체 10종목 설정:"
echo "  VWAP_CONFIRM_ISOLATED_ACCOUNT_MODE=YES bash setup_vwap_video_account.sh"
echo
echo "그 다음:"
echo "  bash doctor_vwap_video_live.sh --prearm"
