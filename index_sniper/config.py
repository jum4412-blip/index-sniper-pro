from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv
import os

ROOT = Path(__file__).resolve().parents[1]

@dataclass(frozen=True)
class Settings:
    bitget_api_key: str
    bitget_secret_key: str
    bitget_passphrase: str
    telegram_token: str
    telegram_chat_id: str
    dry_run: bool
    leverage: int
    capital_ratio: float
    symbols: list[str]
    category: str
    margin_mode: str


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing environment variable: {name}")
    return value.strip()


def load_settings(env_path: str | None = None) -> Settings:
    path = Path(env_path) if env_path else ROOT / ".env"
    load_dotenv(path)

    return Settings(
        bitget_api_key=_required("BITGET_API_KEY"),
        bitget_secret_key=_required("BITGET_SECRET_KEY"),
        bitget_passphrase=_required("BITGET_PASSPHRASE"),
        telegram_token=_required("TELEGRAM_TOKEN"),
        telegram_chat_id=_required("TELEGRAM_CHAT_ID"),
        dry_run=os.getenv("DRY_RUN", "true").lower() == "true",
        leverage=int(os.getenv("LEVERAGE", "5")),
        capital_ratio=float(os.getenv("CAPITAL_RATIO", "0.10")),
        symbols=[s.strip() for s in os.getenv("SYMBOLS", "SPX500USDT,NDX100USDT").split(",") if s.strip()],
        category=os.getenv("CATEGORY", "USDT-FUTURES"),
        margin_mode=os.getenv("MARGIN_MODE", "crossed"),
    )
