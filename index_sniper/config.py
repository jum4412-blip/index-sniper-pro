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


def _symbols(value: str | None) -> list[str]:
    if not value:
        return []
    return [s.strip().upper() for s in value.split(",") if s.strip()]


def _symbol_map(value: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    if not value:
        return result
    for part in value.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        left, right = part.split(":", 1)
        left = left.strip().upper()
        right = right.strip()
        if left and right:
            result[left] = right
    return result


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

    # Micro live smoke test
    allow_live_smoke: bool
    live_smoke_confirm: str
    live_smoke_symbol: str
    live_smoke_side: str
    live_smoke_notional_usdt: float
    live_smoke_max_notional_usdt: float
    live_smoke_wait_seconds: int

    # Strategy
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
    use_ema_filter: bool
    no_ma_both_breakout_mode: str

    # v2.6 whipsaw / sideways market filter
    whipsaw_filter_enabled: bool
    whipsaw_filter_symbols: list[str]
    whipsaw_filter_lookback_days: int
    whipsaw_min_efficiency_ratio: float
    whipsaw_max_flip_ratio: float

    # Runtime
    loop_seconds: int
    heartbeat_minutes: int
    strategy_heartbeat_minutes: int
    strategy_live_confirm: str
    strategy_state_path: str
    log_dir: str

    # Core execution limits
    max_open_positions: int
    max_new_positions_per_cycle: int
    max_daily_entries_per_symbol: int
    live_allow_warmup_entries: bool
    use_exchange_tpsl: bool

    # Survival profile v1.1
    risk_profile: str
    survival_correlated_group: list[str]
    survival_max_correlated_open: int
    survival_max_live_open_positions: int
    survival_min_breakout_atr: float
    survival_min_signal_score: float
    survival_select_best_signal: bool

    # Notifications
    notify_hold_summary: bool
    notify_loop_start: bool
    notify_heartbeat: bool
    notify_signal: bool
    notify_error: bool
    notify_blocked_signal: bool

    # Live safety
    live_trading_enabled: bool
    live_start_confirm: str
    allowed_live_symbols: list[str]
    max_live_capital_ratio: float
    max_daily_loss_pct: float
    max_daily_loss_usdt: float
    max_order_notional_usdt: float
    risk_state_path: str
    daily_loss_guard_enabled: bool

    # v2.6 BTC live guard
    live_guard_enabled: bool
    live_guard_state_path: str
    live_guard_monthly_loss_block_pct: float
    live_guard_mdd_block_pct: float
    live_guard_drawdown_warn_pct: float

    # External signal data v1.5/v1.6
    external_signal_enabled: bool
    external_signal_symbols: list[str]
    external_provider_order: str
    external_yahoo_symbol_map: dict[str, str]
    external_stooq_symbol_map: dict[str, str]
    external_yahoo_range: str
    external_yahoo_interval: str
    external_timeout_seconds: int
    external_candle_limit: int
    external_max_staleness_hours: float
    external_max_scale_deviation_pct: float

    # Observation engine v1.6
    observation_enabled: bool
    observation_latest_path: str
    observation_jsonl: str
    observation_csv: str
    observation_near_target_pct: float

    # Weekend flat manager v1.7
    index_weekend_flat_enabled: bool
    index_weekend_flat_symbols: list[str]
    index_weekend_flat_auto_close: bool
    index_weekend_timezone: str
    index_weekend_block_new_after_et: str
    index_weekend_force_flat_after_et: str
    index_weekend_reopen_after_et: str

    # v2.0 signal-quality / anti-chase filter
    anti_chase_enabled: bool
    anti_chase_symbols: list[str]
    anti_chase_extreme_up_pct: float
    anti_chase_extreme_down_pct: float
    anti_chase_extreme_range_atr: float
    anti_chase_extreme_long_size_multiplier: float
    anti_chase_extreme_short_size_multiplier: float
    max_entry_extension_atr: float

    # v2.0 position manager
    position_manager_enabled: bool
    position_manager_latest_path: str
    position_manager_alert_state_path: str
    position_warn_after_hours: float
    position_max_hold_hours_index: float
    position_max_hold_hours_btc: float
    position_breakeven_alert_r: float
    position_manager_auto_close: bool


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    symbols = _symbols(os.getenv("SYMBOLS", "SP500USDT,NDX100USDT,BTCUSDT"))
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
        use_ema_filter=_bool(os.getenv("USE_EMA_FILTER"), True),
        no_ma_both_breakout_mode=os.getenv("NO_MA_BOTH_BREAKOUT_MODE", "skip").strip().lower(),

        whipsaw_filter_enabled=_bool(os.getenv("WHIPSAW_FILTER_ENABLED"), False),
        whipsaw_filter_symbols=_symbols(os.getenv("WHIPSAW_FILTER_SYMBOLS", "BTCUSDT")),
        whipsaw_filter_lookback_days=int(os.getenv("WHIPSAW_FILTER_LOOKBACK_DAYS", "10")),
        whipsaw_min_efficiency_ratio=float(os.getenv("WHIPSAW_MIN_EFFICIENCY_RATIO", "0.22")),
        whipsaw_max_flip_ratio=float(os.getenv("WHIPSAW_MAX_FLIP_RATIO", "0.60")),

        loop_seconds=int(os.getenv("LOOP_SECONDS", "300")),
        heartbeat_minutes=int(os.getenv("HEARTBEAT_MINUTES", "60")),
        strategy_heartbeat_minutes=int(os.getenv("STRATEGY_HEARTBEAT_MINUTES", os.getenv("HEARTBEAT_MINUTES", "60"))),
        strategy_live_confirm=os.getenv("STRATEGY_LIVE_CONFIRM", "").strip(),
        strategy_state_path=os.getenv("STRATEGY_STATE_PATH", "data/strategy_state.json").strip(),
        log_dir=os.getenv("LOG_DIR", "logs").strip(),

        max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "2")),
        max_new_positions_per_cycle=int(os.getenv("MAX_NEW_POSITIONS_PER_CYCLE", "1")),
        max_daily_entries_per_symbol=int(os.getenv("MAX_DAILY_ENTRIES_PER_SYMBOL", "1")),
        live_allow_warmup_entries=_bool(os.getenv("LIVE_ALLOW_WARMUP_ENTRIES"), True),
        use_exchange_tpsl=_bool(os.getenv("USE_EXCHANGE_TPSL"), True),

        risk_profile=os.getenv("RISK_PROFILE", "SURVIVAL").strip().upper(),
        survival_correlated_group=_symbols(os.getenv("SURVIVAL_CORRELATED_GROUP", "SP500USDT,NDX100USDT")),
        survival_max_correlated_open=int(os.getenv("SURVIVAL_MAX_CORRELATED_OPEN", "1")),
        survival_max_live_open_positions=int(os.getenv("SURVIVAL_MAX_LIVE_OPEN_POSITIONS", "2")),
        survival_min_breakout_atr=float(os.getenv("SURVIVAL_MIN_BREAKOUT_ATR", "0.05")),
        survival_min_signal_score=float(os.getenv("SURVIVAL_MIN_SIGNAL_SCORE", "0")),
        survival_select_best_signal=_bool(os.getenv("SURVIVAL_SELECT_BEST_SIGNAL"), True),

        notify_hold_summary=_bool(os.getenv("NOTIFY_HOLD_SUMMARY"), False),
        notify_loop_start=_bool(os.getenv("NOTIFY_LOOP_START"), True),
        notify_heartbeat=_bool(os.getenv("NOTIFY_HEARTBEAT"), True),
        notify_signal=_bool(os.getenv("NOTIFY_SIGNAL"), True),
        notify_error=_bool(os.getenv("NOTIFY_ERROR"), True),
        notify_blocked_signal=_bool(os.getenv("NOTIFY_BLOCKED_SIGNAL"), True),

        live_trading_enabled=_bool(os.getenv("LIVE_TRADING_ENABLED"), False),
        live_start_confirm=os.getenv("LIVE_START_CONFIRM", "").strip(),
        allowed_live_symbols=_symbols(os.getenv("LIVE_ALLOWED_SYMBOLS", "SP500USDT,NDX100USDT,BTCUSDT")),
        max_live_capital_ratio=float(os.getenv("MAX_LIVE_CAPITAL_RATIO", "0.10")),
        max_daily_loss_pct=float(os.getenv("MAX_DAILY_LOSS_PCT", "1.00")),
        max_daily_loss_usdt=float(os.getenv("MAX_DAILY_LOSS_USDT", "0")),
        max_order_notional_usdt=float(os.getenv("MAX_ORDER_NOTIONAL_USDT", "250")),
        risk_state_path=os.getenv("RISK_STATE_PATH", "data/equity_guard.json").strip(),
        daily_loss_guard_enabled=_bool(os.getenv("DAILY_LOSS_GUARD_ENABLED"), True),

        live_guard_enabled=_bool(os.getenv("LIVE_GUARD_ENABLED"), False),
        live_guard_state_path=os.getenv("LIVE_GUARD_STATE_PATH", "data/live_guard_v26.json").strip(),
        live_guard_monthly_loss_block_pct=float(os.getenv("LIVE_GUARD_MONTHLY_LOSS_BLOCK_PCT", "15.0")),
        live_guard_mdd_block_pct=float(os.getenv("LIVE_GUARD_MDD_BLOCK_PCT", "25.0")),
        live_guard_drawdown_warn_pct=float(os.getenv("LIVE_GUARD_DRAWDOWN_WARN_PCT", "15.0")),

        external_signal_enabled=_bool(os.getenv("EXTERNAL_SIGNAL_ENABLED"), True),
        external_signal_symbols=_symbols(os.getenv("EXTERNAL_SIGNAL_SYMBOLS", "SP500USDT,NDX100USDT")),
        external_provider_order=os.getenv("EXTERNAL_PROVIDER_ORDER", "STOOQ,YAHOO").strip(),
        external_yahoo_symbol_map=_symbol_map(os.getenv("EXTERNAL_YAHOO_SYMBOL_MAP", "SP500USDT:ES=F|^GSPC,NDX100USDT:NQ=F|^NDX")),
        external_stooq_symbol_map=_symbol_map(os.getenv("EXTERNAL_STOOQ_SYMBOL_MAP", "SP500USDT:^spx,NDX100USDT:^ndx")),
        external_yahoo_range=os.getenv("EXTERNAL_YAHOO_RANGE", "2y").strip(),
        external_yahoo_interval=os.getenv("EXTERNAL_YAHOO_INTERVAL", "1d").strip(),
        external_timeout_seconds=int(os.getenv("EXTERNAL_TIMEOUT_SECONDS", "10")),
        external_candle_limit=int(os.getenv("EXTERNAL_CANDLE_LIMIT", "260")),
        external_max_staleness_hours=float(os.getenv("EXTERNAL_MAX_STALENESS_HOURS", "120")),
        external_max_scale_deviation_pct=float(os.getenv("EXTERNAL_MAX_SCALE_DEVIATION_PCT", "20")),

        observation_enabled=_bool(os.getenv("OBSERVATION_ENABLED"), True),
        observation_latest_path=os.getenv("OBSERVATION_LATEST_PATH", "data/market_observer.json").strip(),
        observation_jsonl=os.getenv("OBSERVATION_JSONL", "signal_observer.jsonl").strip(),
        observation_csv=os.getenv("OBSERVATION_CSV", "signal_distance.csv").strip(),
        observation_near_target_pct=float(os.getenv("OBSERVATION_NEAR_TARGET_PCT", "0.20")),

        index_weekend_flat_enabled=_bool(os.getenv("INDEX_WEEKEND_FLAT"), True),
        index_weekend_flat_symbols=_symbols(os.getenv("INDEX_WEEKEND_FLAT_SYMBOLS", "SP500USDT,NDX100USDT")),
        index_weekend_flat_auto_close=_bool(os.getenv("INDEX_WEEKEND_AUTO_CLOSE"), True),
        index_weekend_timezone=os.getenv("INDEX_WEEKEND_TIMEZONE", "America/New_York").strip(),
        index_weekend_block_new_after_et=os.getenv("INDEX_WEEKEND_BLOCK_NEW_AFTER_ET", "15:30").strip(),
        index_weekend_force_flat_after_et=os.getenv("INDEX_WEEKEND_FORCE_FLAT_AFTER_ET", "16:30").strip(),
        index_weekend_reopen_after_et=os.getenv("INDEX_WEEKEND_REOPEN_AFTER_ET", "18:30").strip(),

        anti_chase_enabled=_bool(os.getenv("ANTI_CHASE_ENABLED"), True),
        anti_chase_symbols=_symbols(os.getenv("ANTI_CHASE_SYMBOLS", "SP500USDT,NDX100USDT,BTCUSDT")),
        anti_chase_extreme_up_pct=float(os.getenv("ANTI_CHASE_EXTREME_UP_PCT", "7.0")),
        anti_chase_extreme_down_pct=float(os.getenv("ANTI_CHASE_EXTREME_DOWN_PCT", "7.0")),
        anti_chase_extreme_range_atr=float(os.getenv("ANTI_CHASE_EXTREME_RANGE_ATR", "1.8")),
        anti_chase_extreme_long_size_multiplier=float(os.getenv("ANTI_CHASE_EXTREME_LONG_SIZE_MULTIPLIER", "0.0")),
        anti_chase_extreme_short_size_multiplier=float(os.getenv("ANTI_CHASE_EXTREME_SHORT_SIZE_MULTIPLIER", "0.0")),
        max_entry_extension_atr=float(os.getenv("MAX_ENTRY_EXTENSION_ATR", "0.40")),

        position_manager_enabled=_bool(os.getenv("POSITION_MANAGER_ENABLED"), True),
        position_manager_latest_path=os.getenv("POSITION_MANAGER_LATEST_PATH", "data/position_manager.json").strip(),
        position_manager_alert_state_path=os.getenv("POSITION_MANAGER_ALERT_STATE_PATH", "data/position_alert_state.json").strip(),
        position_warn_after_hours=float(os.getenv("POSITION_WARN_AFTER_HOURS", "24")),
        position_max_hold_hours_index=float(os.getenv("POSITION_MAX_HOLD_HOURS_INDEX", "48")),
        position_max_hold_hours_btc=float(os.getenv("POSITION_MAX_HOLD_HOURS_BTC", "72")),
        position_breakeven_alert_r=float(os.getenv("POSITION_BREAKEVEN_ALERT_R", "1.0")),
        position_manager_auto_close=_bool(os.getenv("POSITION_MANAGER_AUTO_CLOSE"), False),
    )
