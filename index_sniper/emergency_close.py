from __future__ import annotations

import json
import os
import time
from typing import Any

from index_sniper.config import Settings
from index_sniper.exchange.bitget_uta import BitgetUTAClient, OrderIntent
from index_sniper.position import open_positions
from index_sniper.telegram.bot import TelegramBot

CONFIRM_PHRASE = "I_UNDERSTAND_CLOSE_POSITION"


def _short(data: object, limit: int = 20000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text[:limit] + ("..." if len(text) > limit else "")


def run_emergency_close(settings: Settings, client: BitgetUTAClient, tg: TelegramBot) -> dict[str, Any]:
    confirm = os.getenv("EMERGENCY_CLOSE_CONFIRM", "")
    if confirm != CONFIRM_PHRASE:
        raise RuntimeError(
            "EMERGENCY_CLOSE_CONFIRM is missing. To close real positions: "
            f"DRY_RUN=false EMERGENCY_CLOSE_CONFIRM={CONFIRM_PHRASE} EMERGENCY_CLOSE_SYMBOL=BTCUSDT bash run_emergency_close.sh"
        )
    if settings.dry_run:
        raise RuntimeError("DRY_RUN=true 상태에서는 emergency close를 실행하지 않습니다.")

    symbol = os.getenv("EMERGENCY_CLOSE_SYMBOL", "BTCUSDT").strip().upper()
    if symbol not in settings.symbols:
        raise RuntimeError(f"EMERGENCY_CLOSE_SYMBOL {symbol} is not in SYMBOLS: {settings.symbols}")

    pos_resp = client.current_position(symbol, settings.category)
    positions = open_positions(pos_resp, symbol=symbol)
    result: dict[str, Any] = {"symbol": symbol, "open_positions_before": positions, "close_results": []}

    if not positions:
        tg.send(f"✅ <b>Emergency Close 확인</b>\n{symbol}: 열린 포지션 없음")
        print("===== EMERGENCY CLOSE v1.2 HOTFIX =====")
        print(_short(result, 20000))
        return result

    tg.send(f"🚨 <b>Emergency Close 시작</b>\n{symbol}: 열린 포지션 {len(positions)}개\nhedge-mode 방식으로 청산 시도")

    for row in positions:
        side = row.get("_parsed_side") or str(row.get("posSide") or row.get("holdSide") or "").lower()
        qty = row.get("_parsed_qty") or row.get("available") or row.get("total") or row.get("size") or row.get("qty")
        if side not in {"long", "short"}:
            result["close_results"].append({"row": row, "error": f"unknown side: {side}"})
            continue
        close_side = "sell" if side == "long" else "buy"
        oid = str(int(time.time() * 1000))[-10:]
        intent = OrderIntent(
            symbol=symbol,
            side=close_side,
            pos_side=side,
            qty=str(qty),
            category=settings.category,
            margin_coin=settings.margin_coin,
            margin_mode=settings.margin_mode,
            reduce_only=True,  # builder omits reduceOnly in hedge-mode; posSide controls close.
            client_oid=f"emclose-{symbol.lower()}-{side}-{oid}",
        )
        payload_preview = client.place_order(intent, dry_run=True)
        try:
            close_res = client.place_order(intent, dry_run=False)
            result["close_results"].append({"side": side, "qty": str(qty), "payload": payload_preview, "response": close_res})
        except Exception as exc:
            result["close_results"].append({"side": side, "qty": str(qty), "payload": payload_preview, "error": str(exc)})

    time.sleep(2)
    after = open_positions(client.current_position(symbol, settings.category), symbol=symbol)
    result["open_positions_after"] = after
    if after:
        tg.send(f"🛑 <b>Emergency Close 후에도 포지션 남음</b>\n{symbol}\nBitget 앱에서 직접 확인 필요\n남은 포지션: {len(after)}")
    else:
        tg.send(f"✅ <b>Emergency Close 완료</b>\n{symbol}: 남은 포지션 없음")

    print("===== EMERGENCY CLOSE v1.2 HOTFIX =====")
    print(_short(result, 30000))
    return result
