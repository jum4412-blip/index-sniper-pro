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
    strategy_interval: str
    strategy_candle_limit: int
    k_value: float
    ema_fast: int
    ema_slow: int
    adaptive_trend: bool
    warmup_trend_interval: str
    warmup_trend_candle_limit: int
    warmup_ema_fast: int
    warmup_ema_slow: int
    fallback_ema_fast: int
    fallback_ema_slow: int
    fallback_size_multiplier: float
    atr_period: int
    min_atr_period: int
    atr_stop_mult: float
    atr_take_profit_mult: float
    loop_seconds: int
    heartbeat_minutes: int
    strategy_live_confirm: str
    strategy_state_path: str
    log_dir: str
    max_open_positions: int
    max_new_positions_per_cycle: int
    max_daily_entries_per_symbol: int
    live_allow_warmup_entries: bool
    use_exchange_tpsl: bool
    strategy_heartbeat_minutes: int


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
        strategy_interval=os.getenv("STRATEGY_INTERVAL", "1D").strip(),
        strategy_candle_limit=int(os.getenv("STRATEGY_CANDLE_LIMIT", "100")),
        k_value=float(os.getenv("K_VALUE", "0.50")),
        ema_fast=int(os.getenv("EMA_FAST", "20")),
        ema_slow=int(os.getenv("EMA_SLOW", "60")),
        adaptive_trend=_bool(os.getenv("ADAPTIVE_TREND"), True),
        warmup_trend_interval=os.getenv("WARMUP_TREND_INTERVAL", os.getenv("FALLBACK_TREND_INTERVAL", "4H")).strip(),
        warmup_trend_candle_limit=int(os.getenv("WARMUP_TREND_CANDLE_LIMIT", os.getenv("FALLBACK_TREND_CANDLE_LIMIT", "300"))),
        warmup_ema_fast=int(os.getenv("WARMUP_EMA_FAST", os.getenv("FALLBACK_EMA_FAST", "50"))),
        warmup_ema_slow=int(os.getenv("WARMUP_EMA_SLOW", os.getenv("FALLBACK_EMA_SLOW", "200"))),
        fallback_ema_fast=int(os.getenv("DAILY_FALLBACK_EMA_FAST", "8")),
        fallback_ema_slow=int(os.getenv("DAILY_FALLBACK_EMA_SLOW", "21")),
        fallback_size_multiplier=float(os.getenv("FALLBACK_SIZE_MULTIPLIER", "0.50")),
        atr_period=int(os.getenv("ATR_PERIOD", "14")),
        min_atr_period=int(os.getenv("MIN_ATR_PERIOD", "10")),
        atr_stop_mult=float(os.getenv("ATR_STOP_MULT", "1.30")),
        atr_take_profit_mult=float(os.getenv("ATR_TAKE_PROFIT_MULT", "2.00")),
        loop_seconds=int(os.getenv("LOOP_SECONDS", "300")),
        heartbeat_minutes=int(os.getenv("HEARTBEAT_MINUTES", "60")),
        strategy_live_confirm=os.getenv("STRATEGY_LIVE_CONFIRM", "").strip(),
        strategy_state_path=os.getenv("STRATEGY_STATE_PATH", "data/strategy_state.json").strip(),
        log_dir=os.getenv("LOG_DIR", "logs").strip(),
        max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "3")),
        max_new_positions_per_cycle=int(os.getenv("MAX_NEW_POSITIONS_PER_CYCLE", "1")),
        max_daily_entries_per_symbol=int(os.getenv("MAX_DAILY_ENTRIES_PER_SYMBOL", "1")),
        live_allow_warmup_entries=_bool(os.getenv("LIVE_ALLOW_WARMUP_ENTRIES"), True),
        use_exchange_tpsl=_bool(os.getenv("USE_EXCHANGE_TPSL"), True),
        strategy_heartbeat_minutes=int(os.getenv("STRATEGY_HEARTBEAT_MINUTES", os.getenv("HEARTBEAT_MINUTES", "60"))),
    )
