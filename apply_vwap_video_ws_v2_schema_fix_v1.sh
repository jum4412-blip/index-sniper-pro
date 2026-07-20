#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${INDEX_SNIPER_ROOT:-$HOME/index-sniper-pro}"
PY="$ROOT/.venv/bin/python"
TARGET="$ROOT/index_sniper/vwap_video_live_v1.py"
CONFIG="$ROOT/config/vwap_video_live_v1.json"
START="$ROOT/start_vwap_video_live.sh"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$ROOT/local_backups/vwap_video_ws_v2_schema_fix_$TS"

if [[ ! -x "$PY" || ! -f "$TARGET" || ! -f "$CONFIG" ]]; then
  echo "프로젝트 또는 VWAP LIVE 파일을 찾을 수 없습니다: $ROOT" >&2
  exit 1
fi

mkdir -p "$BACKUP/index_sniper" "$BACKUP/config"
cp -a "$TARGET" "$BACKUP/index_sniper/"
cp -a "$CONFIG" "$BACKUP/config/"
[[ -f "$START" ]] && cp -a "$START" "$BACKUP/"

"$PY" - "$TARGET" "$CONFIG" "$START" <<'PY_PATCH'
from pathlib import Path
import json
import sys

target = Path(sys.argv[1])
config_path = Path(sys.argv[2])
start_path = Path(sys.argv[3])

text = target.read_text(encoding="utf-8")

marker = "VWAP_VIDEO_CLASSIC_WS_V2_SCHEMA_FIX_V1"
if marker in text:
    print("이미 WebSocket v2 schema fix가 적용되어 있습니다.")
else:
    old_subs = """        subscriptions: list[dict[str, str]] = []
        for symbol in self.symbols:
            subscriptions.append({"instType": INST_TYPE, "topic": "publicTrade", "symbol": symbol})
            subscriptions.append({"instType": INST_TYPE, "topic": "ticker", "symbol": symbol})
"""
    new_subs = """        # VWAP_VIDEO_CLASSIC_WS_V2_SCHEMA_FIX_V1
        # wss://ws.bitget.com/v2/ws/public uses the classic futures
        # subscription schema: instType/channel/instId.
        subscriptions: list[dict[str, str]] = []
        for symbol in self.symbols:
            subscriptions.append({
                "instType": "USDT-FUTURES",
                "channel": "trade",
                "instId": symbol,
            })
            subscriptions.append({
                "instType": "USDT-FUTURES",
                "channel": "ticker",
                "instId": symbol,
            })
"""
    if old_subs not in text:
        raise SystemExit("기존 WebSocket subscription 블록을 찾지 못했습니다.")
    text = text.replace(old_subs, new_subs, 1)

    old_connected = """                    await ws.send(json.dumps({"op": "subscribe", "args": subscriptions}))
                    self.log("public websocket connected: " + ",".join(self.symbols))
                    self.ws_reconnects += 1
                    backoff = 1.0
                    last_ping = time.monotonic()
"""
    new_connected = """                    await ws.send(json.dumps({"op": "subscribe", "args": subscriptions}))
                    self.log("public websocket connected: " + ",".join(self.symbols))
                    self.ws_reconnects += 1
                    # Do not reset backoff until valid market data is received.
                    last_ping = time.monotonic()
"""
    if old_connected not in text:
        raise SystemExit("기존 WebSocket connect 블록을 찾지 못했습니다.")
    text = text.replace(old_connected, new_connected, 1)

    old_parse = """                        arg = payload.get("arg") or {}
                        symbol = str(arg.get("symbol", "")).upper()
                        topic = str(arg.get("topic", ""))
                        if symbol not in self.runtime:
                            continue
                        ts = safe_int(payload.get("ts"), now_ms())
                        self.last_ws_message_ts = ts
                        rows = payload.get("data") or []
                        if topic == "publicTrade":
                            for row in sorted(rows, key=lambda x: safe_int(x.get("T"), ts)):
                                tick = Tick(
                                    ts=safe_int(row.get("T"), ts),
                                    price=safe_float(row.get("p")),
                                    qty=safe_float(row.get("v")),
                                    side=str(row.get("S", "")).lower(),
                                    exec_id=str(row.get("i") or row.get("L") or ""),
                                )
                                if tick.price > 0 and tick.qty > 0:
                                    self.process_tick(symbol, tick)
                        elif topic == "ticker" and rows:
                            self.process_ticker(symbol, rows[0], ts)
"""
    new_parse = """                        # Subscription acknowledgements contain no market rows.
                        if payload.get("event") in {"subscribe", "unsubscribe"}:
                            continue

                        arg = payload.get("arg") or {}
                        symbol = str(
                            arg.get("symbol")
                            or arg.get("instId")
                            or ""
                        ).upper()
                        topic = str(
                            arg.get("topic")
                            or arg.get("channel")
                            or ""
                        )
                        if symbol not in self.runtime:
                            continue

                        ts = safe_int(payload.get("ts"), now_ms())
                        rows = payload.get("data") or []
                        if not rows:
                            continue

                        self.last_ws_message_ts = ts
                        backoff = 1.0

                        if topic in {"publicTrade", "trade"}:
                            def trade_ts(row: dict[str, Any]) -> int:
                                return safe_int(
                                    row.get("T")
                                    or row.get("ts"),
                                    ts,
                                )

                            for row in sorted(rows, key=trade_ts):
                                tick = Tick(
                                    ts=safe_int(
                                        row.get("T")
                                        or row.get("ts"),
                                        ts,
                                    ),
                                    price=safe_float(
                                        row.get("p")
                                        or row.get("price")
                                    ),
                                    qty=safe_float(
                                        row.get("v")
                                        or row.get("size")
                                    ),
                                    side=str(
                                        row.get("S")
                                        or row.get("side")
                                        or ""
                                    ).lower(),
                                    exec_id=str(
                                        row.get("i")
                                        or row.get("L")
                                        or row.get("tradeId")
                                        or ""
                                    ),
                                )
                                if tick.price > 0 and tick.qty > 0:
                                    self.process_tick(symbol, tick)

                        elif topic == "ticker":
                            row = rows[0]
                            normalized = {
                                "lastPrice": (
                                    row.get("lastPrice")
                                    or row.get("lastPr")
                                ),
                                "bid1Price": (
                                    row.get("bid1Price")
                                    or row.get("bidPr")
                                ),
                                "ask1Price": (
                                    row.get("ask1Price")
                                    or row.get("askPr")
                                ),
                            }
                            row_ts = safe_int(row.get("ts"), ts)
                            self.process_ticker(
                                symbol,
                                normalized,
                                row_ts,
                            )
"""
    if old_parse not in text:
        raise SystemExit("기존 WebSocket parser 블록을 찾지 못했습니다.")
    text = text.replace(old_parse, new_parse, 1)

    old_log = """def log_line(message: str, path: Path = DEFAULT_LOG) -> None:
    line = f"[{iso()}] {message}"
    print(line, flush=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\\n")
    except Exception:
        pass
"""
    new_log = """def log_line(message: str, path: Path = DEFAULT_LOG) -> None:
    line = f"[{iso()}] {message}"
    print(line, flush=True)

    # The screen launcher redirects stdout/stderr to the same log file.
    # Avoid writing the same line twice in loop mode.
    if bool_env("VWAP_STDOUT_LOG_ONLY", False):
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\\n")
    except Exception:
        pass
"""
    if old_log not in text:
        raise SystemExit("기존 log_line 블록을 찾지 못했습니다.")
    text = text.replace(old_log, new_log, 1)

    target.write_text(text, encoding="utf-8")
    print(f"patched: {target}")

cfg = json.loads(config_path.read_text(encoding="utf-8"))
cfg["ws_url"] = "wss://ws.bitget.com/v2/ws/public"
config_path.write_text(
    json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(f"updated: {config_path}")

if start_path.exists():
    start_text = start_path.read_text(encoding="utf-8")
    old_env = """  exec env PYTHONPATH='$ROOT' '$PY' -m index_sniper.vwap_video_live_v1 \\
"""
    new_env = """  exec env PYTHONPATH='$ROOT' VWAP_STDOUT_LOG_ONLY=YES '$PY' -m index_sniper.vwap_video_live_v1 \\
"""
    if "VWAP_STDOUT_LOG_ONLY=YES" not in start_text:
        if old_env not in start_text:
            raise SystemExit("start script의 exec env 블록을 찾지 못했습니다.")
        start_text = start_text.replace(old_env, new_env, 1)
        start_path.write_text(start_text, encoding="utf-8")
        print(f"updated: {start_path}")
PY_PATCH

cat > "$ROOT/test_vwap_public_ws_v2.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

PYTHONPATH="$PWD" .venv/bin/python - <<'PY'
import asyncio
import json
import time

import websockets

URL = "wss://ws.bitget.com/v2/ws/public"
SUB = {
    "op": "subscribe",
    "args": [
        {
            "instType": "USDT-FUTURES",
            "channel": "trade",
            "instId": "BTCUSDT",
        },
        {
            "instType": "USDT-FUTURES",
            "channel": "ticker",
            "instId": "BTCUSDT",
        },
    ],
}

async def main() -> None:
    got_trade = False
    got_ticker = False
    deadline = time.monotonic() + 20.0

    async with websockets.connect(
        URL,
        ping_interval=None,
        open_timeout=15,
        close_timeout=5,
        max_queue=10_000,
    ) as ws:
        await ws.send(json.dumps(SUB))
        print(json.dumps({"sent": SUB}, ensure_ascii=False))

        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                await ws.send("ping")
                continue

            if raw == "pong":
                continue

            msg = json.loads(raw)
            print(json.dumps(msg, ensure_ascii=False)[:1200])

            if msg.get("event") == "error":
                raise RuntimeError(msg)

            arg = msg.get("arg") or {}
            channel = arg.get("channel")
            rows = msg.get("data") or []

            if channel == "trade" and rows:
                row = rows[0]
                assert float(row["price"]) > 0
                assert float(row["size"]) > 0
                got_trade = True

            if channel == "ticker" and rows:
                row = rows[0]
                assert float(row["lastPr"]) > 0
                assert float(row["bidPr"]) > 0
                assert float(row["askPr"]) > 0
                got_ticker = True

            if got_trade and got_ticker:
                print(json.dumps({
                    "ok": True,
                    "trade": got_trade,
                    "ticker": got_ticker,
                    "url": URL,
                }, ensure_ascii=False, indent=2))
                return

    raise RuntimeError(
        f"market data timeout: trade={got_trade} ticker={got_ticker}"
    )

asyncio.run(main())
PY
SH

cat > "$ROOT/restart_vwap_video_after_ws_fix.sh" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"

bash disarm_vwap_video_live.sh || true
bash stop_vwap_video_live.sh || true
sleep 2

bash test_vwap_public_ws_v2.sh

echo
echo "✅ WebSocket 검사는 성공했습니다."
echo "현재는 DISARMED 상태입니다."
echo "force-app-3x ARM 명령을 다시 실행한 뒤 시작하세요."
SH

chmod 755 \
  "$ROOT/test_vwap_public_ws_v2.sh" \
  "$ROOT/restart_vwap_video_after_ws_fix.sh" \
  "$ROOT/start_vwap_video_live.sh"

cd "$ROOT"
PYTHONPATH="$ROOT" "$PY" -m py_compile \
  index_sniper/vwap_video_live_v1.py

PYTHONPATH="$ROOT" "$PY" - <<'PY_TEST'
from pathlib import Path
import json

root = Path.cwd()
text = (root / "index_sniper/vwap_video_live_v1.py").read_text(
    encoding="utf-8"
)
cfg = json.loads(
    (root / "config/vwap_video_live_v1.json").read_text(
        encoding="utf-8"
    )
)

assert cfg["ws_url"] == "wss://ws.bitget.com/v2/ws/public"
assert '"channel": "trade"' in text
assert '"channel": "ticker"' in text
assert '"instId": symbol' in text
assert 'arg.get("channel")' in text
assert 'row.get("price")' in text
assert 'row.get("lastPr")' in text
assert "VWAP_VIDEO_CLASSIC_WS_V2_SCHEMA_FIX_V1" in text
print({
    "static_contract": "OK",
    "ws_url": cfg["ws_url"],
    "schema": "instType/channel/instId",
})
PY_TEST

cat <<EOF

✅ VWAP Bitget public WebSocket schema 수정 완료

원인:
  v2 JSON WebSocket URL에 v3/SBE 구독 형식
  (instType/topic/symbol)을 보내 code 30001이 반복됐습니다.

수정:
  URL: wss://ws.bitget.com/v2/ws/public
  trade:  instType/channel/instId
  ticker: instType/channel/instId
  v2 trade/ticker 응답 필드 파싱
  유효 데이터 수신 후에만 reconnect backoff 초기화
  로그 중복 기록 제거

백업:
  $BACKUP

지금 실행:
  bash restart_vwap_video_after_ws_fix.sh
EOF
