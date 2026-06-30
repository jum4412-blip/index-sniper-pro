from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def _bool(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value.strip()


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
    margin_coin: str
    allow_live_smoke: bool
    live_smoke_confirm: str
    live_smoke_symbol: str
    live_smoke_side: str
    live_smoke_notional_usdt: float
    live_smoke_max_notional_usdt: float
    live_smoke_wait_seconds: int


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    symbols_raw = os.getenv("SYMBOLS", "SP500USDT,NDX100USDT,BTCUSDT")
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
    if not symbols:
        raise RuntimeError("SYMBOLS is empty")
    return Settings(
        bitget_api_key=_required("BITGET_API_KEY"),
        bitget_secret_key=_required("BITGET_SECRET_KEY"),
        bitget_passphrase=_required("BITGET_PASSPHRASE"),
        telegram_token=_required("TELEGRAM_TOKEN"),
        telegram_chat_id=_required("TELEGRAM_CHAT_ID"),
        dry_run=_bool(os.getenv("DRY_RUN"), True),
        leverage=int(os.getenv("LEVERAGE", "5")),
        capital_ratio=float(os.getenv("CAPITAL_RATIO", "0.10")),
        symbols=symbols,
        category=os.getenv("CATEGORY", "USDT-FUTURES").strip(),
        margin_mode=os.getenv("MARGIN_MODE", "crossed").strip(),
        margin_coin=os.getenv("MARGIN_COIN", "USDT").strip().upper(),
        allow_live_smoke=_bool(os.getenv("ALLOW_LIVE_SMOKE"), False),
        live_smoke_confirm=os.getenv("LIVE_SMOKE_CONFIRM", "").strip(),
        live_smoke_symbol=os.getenv("LIVE_SMOKE_SYMBOL", "BTCUSDT").strip().upper(),
        live_smoke_side=os.getenv("LIVE_SMOKE_SIDE", "long").strip().lower(),
        live_smoke_notional_usdt=float(os.getenv("LIVE_SMOKE_NOTIONAL_USDT", "12")),
        live_smoke_max_notional_usdt=float(os.getenv("LIVE_SMOKE_MAX_NOTIONAL_USDT", "20")),
        live_smoke_wait_seconds=int(os.getenv("LIVE_SMOKE_WAIT_SECONDS", "3")),
    )
