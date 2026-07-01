#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source venv/bin/activate
python - <<'PY'
from index_sniper.config import load_settings
from index_sniper.telegram.bot import TelegramBot
from index_sniper import __version__
s = load_settings()
tg = TelegramBot(s.telegram_token, s.telegram_chat_id)
ok = tg.send(f"❤️ <b>Index Sniper Pro v{__version__} heartbeat 테스트</b>\n텔레그램 생존알림 경로 정상 확인")
print("telegram heartbeat test:", "OK" if ok else "FAILED")
PY
