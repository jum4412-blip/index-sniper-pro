import requests


class TelegramBot:
    def __init__(self, token: str, chat_id: str, timeout: int = 10):
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout

    def send(self, text: str) -> bool:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            r = requests.post(url, data={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}, timeout=self.timeout)
            return r.status_code == 200
        except Exception:
            return False
