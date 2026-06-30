from __future__ import annotations

import json
from datetime import datetime, timezone

from index_sniper.config import load_settings
from index_sniper.exchange.bitget_uta import BitgetUTAClient, BitgetUTAError
from index_sniper.telegram.bot import TelegramBot


def short_json(data: dict, limit: int = 700) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text[:limit] + ("..." if len(text) > limit else "")


def main() -> None:
    settings = load_settings()
    tg = TelegramBot(settings.telegram_token, settings.telegram_chat_id)
    client = BitgetUTAClient(
        api_key=settings.bitget_api_key,
        secret_key=settings.bitget_secret_key,
        passphrase=settings.bitget_passphrase,
    )

    started = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    tg.send(
        "🚀 <b>Index Sniper Pro v0.1</b>\n"
        f"시작: {started}\n"
        f"DRY_RUN: {settings.dry_run}\n"
        f"Symbols: {', '.join(settings.symbols)}\n"
        "목표: UTA 연결/잔고/계정 조회 테스트"
    )

    results = {}
    try:
        results["account_info"] = client.account_info()
    except Exception as e:
        results["account_info_error"] = str(e)

    try:
        results["assets"] = client.assets()
    except Exception as e:
        results["assets_error"] = str(e)

    try:
        results["settings"] = client.settings()
    except Exception as e:
        results["settings_error"] = str(e)

    print("===== UTA CHECK RESULT =====")
    print(short_json(results, 3000))

    ok = all(k in results and isinstance(results[k], dict) and str(results[k].get("code")) in {"00000", "0"} for k in ["account_info", "assets"])
    if ok:
        tg.send("✅ <b>UTA 연결 성공</b>\n계정/자산 조회가 정상 응답했습니다.")
    else:
        tg.send("⚠️ <b>UTA 연결 확인 필요</b>\n터미널 출력의 오류 메시지를 확인하세요.")


if __name__ == "__main__":
    main()
