#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -d "$PWD/index_sniper" && -x "$PWD/.venv/bin/python" ]]; then
  ROOT="$PWD"
else
  ROOT="$HOME/index-sniper-pro"
fi

PY="$ROOT/.venv/bin/python"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/local_backups/vwap_top10_universe_$TS"

if [[ ! -d "$ROOT/index_sniper" || ! -x "$PY" ]]; then
  echo "프로젝트 또는 .venv를 찾을 수 없습니다: $ROOT" >&2
  exit 1
fi

if [[ ! -f "$ROOT/config/vwap_scalper_v2.json" ]]; then
  echo "먼저 VWAP Scalper v2 SHADOW를 설치해야 합니다." >&2
  exit 2
fi

mkdir -p "$BACKUP/config" "$BACKUP/scripts" \
         "$ROOT/data/vwap_scalper_v2" "$ROOT/logs"

cp -a "$ROOT/config/vwap_scalper_v2.json" "$BACKUP/config/" || true
cp -a "$ROOT/start_vwap_scalper_v2_shadow.sh" "$BACKUP/scripts/" || true
cp -a "$ROOT/status_vwap_scalper_v2.sh" "$BACKUP/scripts/" || true

cat > "$ROOT/index_sniper/vwap_top10_universe.py" <<'PY'
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/vwap_scalper_v2.json"
CACHE_PATH = ROOT / "data/vwap_scalper_v2/universe_cache.json"
LATEST_PATH = ROOT / "data/vwap_scalper_v2/universe_latest.json"

COINGECKO_DEMO = "https://api.coingecko.com/api/v3/coins/markets"
COINGECKO_PRO = "https://pro-api.coingecko.com/api/v3/coins/markets"
BITGET_INSTRUMENTS = "https://api.bitget.com/api/v3/market/instruments"
BITGET_TICKERS = "https://api.bitget.com/api/v3/market/tickers"

STABLE_SYMBOLS = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD",
    "FRAX", "USD1", "RLUSD", "USDD", "GUSD", "LUSD", "BUSD", "EURC",
}
EXCLUDED_IDS = {
    "staked-ether", "wrapped-bitcoin", "wrapped-steth", "weth",
    "binance-bridged-usdt-bnb-smart-chain", "binance-bridged-usdc-bnb-smart-chain",
}
EXCLUDED_NAME_PARTS = (
    "wrapped ", "staked ", "bridged ", "liquid staking", "restaked ",
    "synthetic usd", "tokenized treasury",
)

FALLBACK_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT",
    "TRXUSDT", "HYPEUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT",
]


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def http_json(
    url: str,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: int = 15,
) -> Any:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{url}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "index-sniper-vwap-top10/2.1",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_coingecko(limit: int = 100) -> list[dict[str, Any]]:
    pro_key = os.getenv("COINGECKO_PRO_API_KEY", "").strip()
    demo_key = (
        os.getenv("COINGECKO_DEMO_API_KEY", "").strip()
        or os.getenv("COINGECKO_API_KEY", "").strip()
    )

    headers: dict[str, str] = {}
    url = COINGECKO_DEMO
    if pro_key:
        url = COINGECKO_PRO
        headers["x-cg-pro-api-key"] = pro_key
    elif demo_key:
        headers["x-cg-demo-api-key"] = demo_key

    data = http_json(
        url,
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": min(max(limit, 20), 250),
            "page": 1,
            "sparkline": "false",
        },
        headers=headers,
    )
    if not isinstance(data, list):
        raise RuntimeError("CoinGecko response is not a list")
    return data


def fetch_bitget() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    instruments_raw = http_json(
        BITGET_INSTRUMENTS,
        {"category": "USDT-FUTURES"},
    )
    tickers_raw = http_json(
        BITGET_TICKERS,
        {"category": "USDT-FUTURES"},
    )

    instruments = {
        str(x.get("symbol", "")).upper(): x
        for x in instruments_raw.get("data", [])
        if isinstance(x, dict) and x.get("symbol")
    }
    tickers = {
        str(x.get("symbol", "")).upper(): x
        for x in tickers_raw.get("data", [])
        if isinstance(x, dict) and x.get("symbol")
    }
    return instruments, tickers


def is_excluded_coin(coin: dict[str, Any]) -> tuple[bool, str]:
    coin_id = str(coin.get("id", "")).lower()
    symbol = str(coin.get("symbol", "")).upper()
    name = str(coin.get("name", "")).lower()

    if symbol in STABLE_SYMBOLS:
        return True, "stablecoin"
    if coin_id in EXCLUDED_IDS:
        return True, "wrapped_or_staked"
    if any(part in name for part in EXCLUDED_NAME_PARTS):
        return True, "wrapped_staked_or_tokenized"
    if not symbol or not coin.get("market_cap"):
        return True, "missing_market_data"
    return False, ""


def float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def instrument_is_eligible(item: dict[str, Any]) -> bool:
    return (
        str(item.get("category", "")).upper() == "USDT-FUTURES"
        and str(item.get("status", "")).lower() == "online"
        and str(item.get("type", "")).lower() == "perpetual"
        and str(item.get("symbolType", "")).lower() == "crypto"
        and str(item.get("isRwa", "NO")).upper() != "YES"
        and str(item.get("isReality", "no")).lower() != "yes"
    )


def ticker_quality(ticker: dict[str, Any]) -> tuple[float, float]:
    turnover = float_or_zero(ticker.get("turnover24h"))
    bid = float_or_zero(ticker.get("bid1Price"))
    ask = float_or_zero(ticker.get("ask1Price"))
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
    spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 999.0
    return turnover, spread_pct


def resolve(config: dict[str, Any]) -> dict[str, Any]:
    universe_cfg = config.get("universe", {})
    target_count = int(universe_cfg.get("target_count", 10))
    min_turnover = float(universe_cfg.get("min_turnover_24h_usdt", 5_000_000))
    max_spread_pct = float(universe_cfg.get("max_spread_pct", 0.08))

    coins = fetch_coingecko(limit=int(universe_cfg.get("coingecko_scan_count", 100)))
    instruments, tickers = fetch_bitget()

    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    used_symbols: set[str] = set()

    for coin in coins:
        excluded, reason = is_excluded_coin(coin)
        base = str(coin.get("symbol", "")).upper()
        pair = f"{base}USDT"

        if excluded:
            skipped.append({"rank": coin.get("market_cap_rank"), "coin": base, "reason": reason})
            continue
        if pair in used_symbols:
            skipped.append({"rank": coin.get("market_cap_rank"), "coin": base, "reason": "duplicate_symbol"})
            continue

        instrument = instruments.get(pair)
        ticker = tickers.get(pair)
        if not instrument or not instrument_is_eligible(instrument):
            skipped.append({"rank": coin.get("market_cap_rank"), "coin": base, "reason": "no_online_bitget_perpetual"})
            continue
        if not ticker:
            skipped.append({"rank": coin.get("market_cap_rank"), "coin": base, "reason": "ticker_missing"})
            continue

        turnover, spread_pct = ticker_quality(ticker)
        if turnover < min_turnover:
            skipped.append({
                "rank": coin.get("market_cap_rank"),
                "coin": base,
                "reason": "low_turnover",
                "turnover24h": turnover,
            })
            continue
        if spread_pct > max_spread_pct:
            skipped.append({
                "rank": coin.get("market_cap_rank"),
                "coin": base,
                "reason": "wide_spread",
                "spread_pct": spread_pct,
            })
            continue

        selected.append({
            "symbol": pair,
            "coin_id": coin.get("id"),
            "name": coin.get("name"),
            "base": base,
            "market_cap_rank": coin.get("market_cap_rank"),
            "market_cap_usd": coin.get("market_cap"),
            "turnover24h_usdt": round(turnover, 2),
            "spread_pct": round(spread_pct, 6),
        })
        used_symbols.add(pair)

        if len(selected) >= target_count:
            break

    if len(selected) < target_count:
        raise RuntimeError(
            f"eligible universe too small: {len(selected)}/{target_count}. "
            "기존 universe를 유지합니다."
        )

    return {
        "generated_utc": now_utc(),
        "method": "coingecko_market_cap_desc_then_bitget_usdt_perpetual_filter",
        "target_count": target_count,
        "symbols": [x["symbol"] for x in selected],
        "selected": selected,
        "skipped_preview": skipped[:30],
        "filters": {
            "stablecoins_excluded": True,
            "wrapped_staked_excluded": True,
            "bitget_status": "online",
            "product": "USDT-FUTURES perpetual crypto",
            "min_turnover_24h_usdt": min_turnover,
            "max_spread_pct": max_spread_pct,
        },
    }


def refresh(config_path: Path, dry_run: bool = False) -> dict[str, Any]:
    config = read_json(config_path, {})
    if not config:
        raise RuntimeError(f"config load failed: {config_path}")

    try:
        result = resolve(config)
        result["source_status"] = "LIVE"
    except Exception as exc:
        cache = read_json(CACHE_PATH, {})
        cached_symbols = cache.get("symbols", []) if isinstance(cache, dict) else []
        if len(cached_symbols) >= 10:
            result = dict(cache)
            result["source_status"] = "CACHE_FALLBACK"
            result["fallback_reason"] = f"{type(exc).__name__}: {exc}"
        else:
            result = {
                "generated_utc": now_utc(),
                "method": "static_emergency_fallback",
                "source_status": "STATIC_FALLBACK",
                "fallback_reason": f"{type(exc).__name__}: {exc}",
                "target_count": 10,
                "symbols": list(FALLBACK_SYMBOLS),
                "selected": [
                    {"symbol": symbol, "market_cap_rank": None}
                    for symbol in FALLBACK_SYMBOLS
                ],
                "filters": {
                    "warning": "CoinGecko/Bitget universe resolution failed"
                },
            }

    if not dry_run:
        config["symbols"] = result["symbols"][:10]
        config["universe"] = {
            **config.get("universe", {}),
            "enabled": True,
            "mode": "market_cap_top10_eligible",
            "target_count": 10,
            "refresh_on_start": True,
            "coingecko_scan_count": 100,
            "exclude_stablecoins": True,
            "exclude_wrapped_staked": True,
            "require_bitget_usdt_perpetual": True,
            "min_turnover_24h_usdt": 5_000_000,
            "max_spread_pct": 0.08,
            "last_refresh_utc": result.get("generated_utc"),
            "last_source_status": result.get("source_status"),
        }
        config["max_total_positions"] = min(
            int(config.get("max_total_positions", 2)),
            2,
        )
        write_json(config_path, config)
        write_json(LATEST_PATH, result)
        if result.get("source_status") == "LIVE":
            write_json(CACHE_PATH, result)

    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["refresh", "show", "doctor"])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = ROOT / config_path

    if args.command == "show":
        print(json.dumps(read_json(LATEST_PATH, {}), ensure_ascii=False, indent=2))
        return 0

    result = refresh(config_path, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.command == "doctor":
        ok = len(result.get("symbols", [])) == 10
        print(json.dumps({"ok": ok, "count": len(result.get("symbols", []))}))
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY

cat > "$ROOT/refresh_vwap_top10_universe.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
.venv/bin/python -m index_sniper.vwap_top10_universe refresh \
  --config config/vwap_scalper_v2.json
SH

cat > "$ROOT/show_vwap_top10_universe.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
.venv/bin/python -m index_sniper.vwap_top10_universe show
SH

cat > "$ROOT/restart_vwap_scalper_top10_shadow.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
bash stop_vwap_scalper_v2_shadow.sh || true
bash refresh_vwap_top10_universe.sh
bash start_vwap_scalper_v2_shadow.sh
SH

"$PY" - "$ROOT/start_vwap_scalper_v2_shadow.sh" <<'PY_PATCH'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
marker = "mkdir -p logs data/vwap_scalper_v2\n"
insert = (
    "mkdir -p logs data/vwap_scalper_v2\n\n"
    "# 시총 순으로 내려가며 Bitget에서 거래 가능한 상위 10개를 갱신한다.\n"
    ".venv/bin/python -m index_sniper.vwap_top10_universe refresh \\\n"
    "  --config config/vwap_scalper_v2.json \\\n"
    "  >> logs/vwap-top10-universe.log 2>&1 || {\n"
    "    echo \"⚠️ universe refresh failed; 기존 config symbols로 시작합니다.\" >&2\n"
    "  }\n"
)
if "vwap_top10_universe refresh" not in text:
    if marker not in text:
        raise SystemExit("start script marker not found")
    text = text.replace(marker, insert, 1)
    path.write_text(text, encoding="utf-8")
PY_PATCH

"$PY" - "$ROOT/status_vwap_scalper_v2.sh" <<'PY_PATCH'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
block = (
    "\necho\n"
    "echo \"===== TOP10 UNIVERSE =====\"\n"
    ".venv/bin/python -m index_sniper.vwap_top10_universe show 2>/dev/null || true\n"
)
if "===== TOP10 UNIVERSE =====" not in text:
    text += block
    path.write_text(text, encoding="utf-8")
PY_PATCH

"$PY" - "$ROOT/config/vwap_scalper_v2.json" <<'PY_CFG'
from pathlib import Path
import json
import sys

path = Path(sys.argv[1])
cfg = json.loads(path.read_text(encoding="utf-8"))
cfg["universe"] = {
    **cfg.get("universe", {}),
    "enabled": True,
    "mode": "market_cap_top10_eligible",
    "target_count": 10,
    "refresh_on_start": True,
    "coingecko_scan_count": 100,
    "exclude_stablecoins": True,
    "exclude_wrapped_staked": True,
    "require_bitget_usdt_perpetual": True,
    "min_turnover_24h_usdt": 5000000,
    "max_spread_pct": 0.08,
}
cfg["max_total_positions"] = min(int(cfg.get("max_total_positions", 2)), 2)
path.write_text(
    json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY_CFG

chmod 700 \
  "$ROOT/refresh_vwap_top10_universe.sh" \
  "$ROOT/show_vwap_top10_universe.sh" \
  "$ROOT/restart_vwap_scalper_top10_shadow.sh" \
  "$ROOT/start_vwap_scalper_v2_shadow.sh" \
  "$ROOT/status_vwap_scalper_v2.sh"
chmod 644 "$ROOT/index_sniper/vwap_top10_universe.py"

cd "$ROOT"
PYTHONPATH="$ROOT" "$PY" -m py_compile index_sniper/vwap_top10_universe.py
PYTHONPATH="$ROOT" "$PY" -m index_sniper.vwap_top10_universe doctor \
  --config config/vwap_scalper_v2.json

cat <<EOF

✅ VWAP Scalper 시총 Top-10 eligible universe 적용 완료

선정 방식:
  CoinGecko 시총 내림차순
  → 스테이블코인 제외
  → wrapped/staked/bridged 자산 제외
  → Bitget USDT-FUTURES perpetual / crypto / online 확인
  → 24h 거래대금과 스프레드 필터
  → 상위 10개 선택

중요:
  감시종목은 10개지만 동시 포지션은 최대 2개로 유지합니다.
  현재 엔진은 SHADOW/PAPER ONLY입니다.

현재 선정 종목:
  bash show_vwap_top10_universe.sh

재시작:
  bash restart_vwap_scalper_top10_shadow.sh

상태:
  bash status_vwap_scalper_v2.sh

백업:
  $BACKUP
EOF
