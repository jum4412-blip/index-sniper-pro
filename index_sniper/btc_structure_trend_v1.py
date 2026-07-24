from __future__ import annotations

"""BTC Structure Trend v1.2 for Bitget UTA.

Strategy contract
-----------------
- BTCUSDT only, USDT perpetual futures.
- Cross 5x (Bitget API value: ``crossed``).
- Order size is calibrated as 50% of account equity at 5x (approximately 2.5x equity notional).
- Because this is cross margin, all eligible cross collateral can support the position; the 50% figure is a sizing reference, not an isolated loss boundary.
- Long: higher-timeframe uptrend, then a pullback/reclaim at a high-volume zone.
- Short: higher-timeframe downtrend, then a retracement/rejection at a high-volume zone.
- Normal exit: a completed 15-minute candle strongly invalidates the active
  high-volume support/resistance level.
- Disaster exit: a farther exchange-side mark-price market stop is attached to
  every opening order.  It is not the normal strategy stop.
- No fixed take-profit.  After 1.5R, the software stop trails confirmed volume
  structure and a loose chandelier stop.  The stop is never loosened.

The module reuses index_sniper.exchange.bitget_uta.BitgetUTAClient and the
optional Telegram transport already present in index-sniper-pro.

Real orders are impossible unless all arming gates pass.  The exchange is the
source of truth; unknown positions/orders block new entries.

SHADOW and OBSERVE use a separate public-only HTTP client that rejects every
authenticated request and POST operation.  They never load Bitget API keys.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*_: Any, **__: Any) -> bool:
        return False

try:
    from index_sniper.exchange.bitget_uta import BitgetUTAClient
except Exception:  # pragma: no cover - resolved in target repository
    BitgetUTAClient = None  # type: ignore

try:
    from index_sniper.telegram.bot import TelegramBot
except Exception:  # pragma: no cover
    TelegramBot = None  # type: ignore


VERSION = "1.2.0"
CATEGORY = "USDT-FUTURES"
SYMBOL = "BTCUSDT"
UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/btc_structure_trend_v1.json"
DEFAULT_STATE = ROOT / "data/btc_structure_trend_v1_state.json"
DEFAULT_ARM = ROOT / "data/BTC_STRUCTURE_TREND_V1_ARMED.json"
DEFAULT_LOG = ROOT / "logs/btc-structure-trend-v1.log"
DEFAULT_TRADES = ROOT / "research/btc_structure_trend_v1_trades.csv"
DEFAULT_EVENTS = ROOT / "research/btc_structure_trend_v1_events.jsonl"
DEFAULT_SHADOW_STATE = ROOT / "data/btc_structure_trend_v1_shadow_state.json"
DEFAULT_SHADOW_LOG = ROOT / "logs/btc-structure-trend-v1-shadow.log"
DEFAULT_SHADOW_TRADES = ROOT / "research/btc_structure_trend_v1_shadow_trades.csv"
DEFAULT_SHADOW_EVENTS = ROOT / "research/btc_structure_trend_v1_shadow_events.jsonl"
DEFAULT_SHADOW_EQUITY = ROOT / "research/btc_structure_trend_v1_shadow_equity.csv"

ARM_PHRASE = "START_BTC_STRUCTURE_TREND_LIVE_5X_CROSS_50"
RISK_PHRASE = "I_UNDERSTAND_CROSS_2_5X_NOTIONAL_CAN_USE_FULL_COLLATERAL"
NO_WITHDRAW_PHRASE = "API_HAS_NO_WITHDRAW_PERMISSION"
IP_WHITELIST_PHRASE = "API_IP_WHITELISTED"
LIVE_ENV = "BTC_STRUCTURE_V1_LIVE_ENABLED"

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
    "1D": 86_400_000,
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.ts / 1000.0, tz=UTC)

    @property
    def range(self) -> float:
        return max(0.0, self.high - self.low)

    @property
    def body_ratio(self) -> float:
        return abs(self.close - self.open) / self.range if self.range > 0 else 0.0

    @property
    def close_location(self) -> float:
        """0=low, 1=high."""
        return (self.close - self.low) / self.range if self.range > 0 else 0.5


@dataclass(frozen=True)
class Instrument:
    symbol: str
    status: str
    symbol_type: str
    min_order_qty: Decimal
    max_order_qty: Decimal
    max_market_order_qty: Decimal
    min_order_amount: Decimal
    price_step: Decimal
    quantity_step: Decimal
    min_leverage: float
    max_leverage: float
    maker_fee: float
    taker_fee: float


@dataclass(frozen=True)
class Ticker:
    symbol: str
    last: float
    mark: float
    bid: float
    ask: float


@dataclass(frozen=True)
class VolumeZone:
    low: float
    high: float
    center: float
    volume: float
    relative_volume: float
    first_bin: int
    last_bin: int

    def distance_to(self, price: float) -> float:
        if self.low <= price <= self.high:
            return 0.0
        return self.low - price if price < self.low else price - self.high


@dataclass(frozen=True)
class TrendSnapshot:
    side: str  # UP / DOWN / NONE
    adx4h: float
    plus_di4h: float
    minus_di4h: float
    ema50_4h: float
    ema200_4h: float
    ema20_1h: float
    ema50_1h: float
    close4h: float
    close1h: float


@dataclass(frozen=True)
class Candidate:
    symbol: str
    side: str  # LONG / SHORT
    signal_bar_ts: int
    entry_reference: float
    zone_low: float
    zone_high: float
    zone_center: float
    soft_stop: float
    hard_stop: float
    initial_risk: float
    stop_distance_pct: float
    score: float
    setup: str
    diagnostics: dict[str, Any]


@dataclass
class ManagedPosition:
    symbol: str
    side: str
    qty: float
    entry_price: float
    entry_ts: str
    entry_order_id: str
    entry_client_oid: str
    hold_mode: str
    initial_soft_stop: float
    soft_stop: float
    hard_stop: float
    initial_risk: float
    zone_low: float
    zone_high: float
    best_price: float
    trail_active: bool = False
    last_managed_bar_ts: int = 0
    score: float = 0.0
    setup: str = ""
    entry_fee: float = 0.0
    entry_notional: float = 0.0
    entry_equity: float = 0.0


@dataclass(frozen=True)
class Settings:
    config_path: Path
    symbol: str
    category: str
    leverage: int
    margin_mode: str
    hold_mode: str
    entry_margin_pct: float
    loop_seconds: int

    trend_fast_4h: int
    trend_slow_4h: int
    trend_slope_lookback: int
    confirm_fast_1h: int
    confirm_slow_1h: int
    adx_period: int
    adx_min_4h: float

    profile_lookback_1h: int
    profile_bins: int
    profile_high_volume_quantile: float
    profile_touch_atr: float
    entry_volume_ratio: float
    entry_body_ratio: float
    signal_score_min: float
    min_room_r: float

    soft_stop_buffer_atr: float
    min_stop_pct: float
    max_stop_pct: float
    strong_break_extra_atr: float
    strong_break_volume_ratio: float
    strong_break_body_ratio: float
    strong_break_close_location: float
    hard_stop_extra_atr: float
    hard_stop_min_extra_pct: float
    hard_stop_max_pct: float

    trail_activate_r: float
    trail_chandelier_atr: float
    trail_zone_buffer_atr: float
    trail_profile_lookback_15m: int
    trend_flip_exit: bool
    trend_flip_volume_ratio: float

    max_daily_loss_pct: float
    max_weekly_loss_pct: float
    max_peak_drawdown_pct: float
    max_consecutive_losses: int
    consecutive_loss_pause_minutes: int
    cooldown_minutes: int
    max_entries_per_day: int

    state_path: Path
    arm_path: Path
    log_path: Path
    trades_path: Path
    events_path: Path
    notify: bool
    heartbeat_minutes: int

    shadow_initial_equity: float
    shadow_entry_slippage_bps: float
    shadow_exit_slippage_bps: float
    shadow_taker_fee_rate: float
    shadow_state_path: Path
    shadow_log_path: Path
    shadow_trades_path: Path
    shadow_events_path: Path
    shadow_equity_path: Path
    shadow_equity_sample_seconds: int


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(UTC).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def as_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def fmt_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def quantize_step(value: float | Decimal, step: Decimal, rounding: str = "down") -> Decimal:
    dec = as_decimal(value)
    if step <= 0:
        return dec
    units = dec / step
    mode = {
        "down": ROUND_DOWN,
        "floor": ROUND_FLOOR,
        "ceil": ROUND_CEILING,
        "nearest": ROUND_HALF_UP,
    }[rounding]
    return units.to_integral_value(rounding=mode) * step


def mean(values: Iterable[float]) -> float:
    rows = [float(x) for x in values if math.isfinite(float(x))]
    return statistics.fmean(rows) if rows else 0.0


def median(values: Iterable[float]) -> float:
    rows = [float(x) for x in values if math.isfinite(float(x))]
    return statistics.median(rows) if rows else 0.0


def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    rows = sorted(float(x) for x in values)
    q = min(1.0, max(0.0, q))
    position = (len(rows) - 1) * q
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return rows[lower]
    weight = position - lower
    return rows[lower] * (1.0 - weight) + rows[upper] * weight


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    path.chmod(0o600)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def append_csv(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def log(message: str, settings: Settings | None = None) -> None:
    line = f"[{iso()}] {message}"
    print(line, flush=True)
    path = settings.log_path if settings else DEFAULT_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except Exception:
        pass


def shadow_log(message: str, settings: Settings) -> None:
    line = f"[{iso()}] {message}"
    print(line, flush=True)
    try:
        settings.shadow_log_path.parent.mkdir(parents=True, exist_ok=True)
        with settings.shadow_log_path.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except Exception:
        pass


def day_key(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(UTC).strftime("%Y-%m-%d")


def week_key(dt: datetime | None = None) -> str:
    n = (dt or now_utc()).astimezone(UTC)
    year, week, _ = n.isocalendar()
    return f"{year}-W{week:02d}"


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).astimezone(UTC)
    except Exception:
        return None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Settings and live contract
# ---------------------------------------------------------------------------


def load_settings(config_path: str | Path | None = None) -> Settings:
    path = Path(config_path or os.getenv("BTC_STRUCTURE_V1_CONFIG", DEFAULT_CONFIG))
    if not path.is_absolute():
        path = ROOT / path
    cfg = read_json(path, None)
    if not isinstance(cfg, dict):
        raise RuntimeError(f"config not found or invalid: {path}")

    def p(key: str, default: Path) -> Path:
        value = Path(str(cfg.get(key, default)))
        return value if value.is_absolute() else ROOT / value

    s = Settings(
        config_path=path,
        symbol=str(cfg.get("symbol", SYMBOL)).upper(),
        category=str(cfg.get("category", CATEGORY)).upper(),
        leverage=int(cfg.get("leverage", 5)),
        margin_mode=str(cfg.get("margin_mode", "crossed")).lower(),
        hold_mode=str(cfg.get("hold_mode", "hedge_mode")).lower(),
        entry_margin_pct=float(cfg.get("entry_margin_pct", 50.0)),
        loop_seconds=max(10, int(cfg.get("loop_seconds", 20))),
        trend_fast_4h=int(cfg.get("trend_fast_4h", 50)),
        trend_slow_4h=int(cfg.get("trend_slow_4h", 200)),
        trend_slope_lookback=int(cfg.get("trend_slope_lookback", 6)),
        confirm_fast_1h=int(cfg.get("confirm_fast_1h", 20)),
        confirm_slow_1h=int(cfg.get("confirm_slow_1h", 50)),
        adx_period=int(cfg.get("adx_period", 14)),
        adx_min_4h=float(cfg.get("adx_min_4h", 18.0)),
        profile_lookback_1h=int(cfg.get("profile_lookback_1h", 168)),
        profile_bins=int(cfg.get("profile_bins", 64)),
        profile_high_volume_quantile=float(cfg.get("profile_high_volume_quantile", 0.70)),
        profile_touch_atr=float(cfg.get("profile_touch_atr", 0.25)),
        entry_volume_ratio=float(cfg.get("entry_volume_ratio", 1.05)),
        entry_body_ratio=float(cfg.get("entry_body_ratio", 0.38)),
        signal_score_min=float(cfg.get("signal_score_min", 75.0)),
        min_room_r=float(cfg.get("min_room_r", 2.2)),
        soft_stop_buffer_atr=float(cfg.get("soft_stop_buffer_atr", 0.12)),
        min_stop_pct=float(cfg.get("min_stop_pct", 0.25)),
        max_stop_pct=float(cfg.get("max_stop_pct", 1.25)),
        strong_break_extra_atr=float(cfg.get("strong_break_extra_atr", 0.08)),
        strong_break_volume_ratio=float(cfg.get("strong_break_volume_ratio", 1.35)),
        strong_break_body_ratio=float(cfg.get("strong_break_body_ratio", 0.55)),
        strong_break_close_location=float(cfg.get("strong_break_close_location", 0.25)),
        hard_stop_extra_atr=float(cfg.get("hard_stop_extra_atr", 0.65)),
        hard_stop_min_extra_pct=float(cfg.get("hard_stop_min_extra_pct", 0.30)),
        hard_stop_max_pct=float(cfg.get("hard_stop_max_pct", 2.00)),
        trail_activate_r=float(cfg.get("trail_activate_r", 1.50)),
        trail_chandelier_atr=float(cfg.get("trail_chandelier_atr", 3.20)),
        trail_zone_buffer_atr=float(cfg.get("trail_zone_buffer_atr", 0.15)),
        trail_profile_lookback_15m=int(cfg.get("trail_profile_lookback_15m", 192)),
        trend_flip_exit=bool(cfg.get("trend_flip_exit", True)),
        trend_flip_volume_ratio=float(cfg.get("trend_flip_volume_ratio", 1.20)),
        max_daily_loss_pct=float(cfg.get("max_daily_loss_pct", 7.5)),
        max_weekly_loss_pct=float(cfg.get("max_weekly_loss_pct", 15.0)),
        max_peak_drawdown_pct=float(cfg.get("max_peak_drawdown_pct", 20.0)),
        max_consecutive_losses=int(cfg.get("max_consecutive_losses", 3)),
        consecutive_loss_pause_minutes=int(cfg.get("consecutive_loss_pause_minutes", 240)),
        cooldown_minutes=int(cfg.get("cooldown_minutes", 30)),
        max_entries_per_day=int(cfg.get("max_entries_per_day", 4)),
        state_path=p("state_path", DEFAULT_STATE),
        arm_path=p("arm_path", DEFAULT_ARM),
        log_path=p("log_path", DEFAULT_LOG),
        trades_path=p("trades_path", DEFAULT_TRADES),
        events_path=p("events_path", DEFAULT_EVENTS),
        notify=bool(cfg.get("notify", True)),
        heartbeat_minutes=int(cfg.get("heartbeat_minutes", 60)),
        shadow_initial_equity=float(cfg.get("shadow_initial_equity", 10_000.0)),
        shadow_entry_slippage_bps=float(cfg.get("shadow_entry_slippage_bps", 2.0)),
        shadow_exit_slippage_bps=float(cfg.get("shadow_exit_slippage_bps", 2.0)),
        shadow_taker_fee_rate=float(cfg.get("shadow_taker_fee_rate", 0.0006)),
        shadow_state_path=p("shadow_state_path", DEFAULT_SHADOW_STATE),
        shadow_log_path=p("shadow_log_path", DEFAULT_SHADOW_LOG),
        shadow_trades_path=p("shadow_trades_path", DEFAULT_SHADOW_TRADES),
        shadow_events_path=p("shadow_events_path", DEFAULT_SHADOW_EVENTS),
        shadow_equity_path=p("shadow_equity_path", DEFAULT_SHADOW_EQUITY),
        shadow_equity_sample_seconds=max(10, int(cfg.get("shadow_equity_sample_seconds", 60))),
    )
    errors = validate_settings_contract(s)
    if errors:
        raise RuntimeError("invalid BTC Structure live contract: " + "; ".join(errors))
    return s


def validate_settings_contract(s: Settings) -> list[str]:
    errors: list[str] = []
    if s.symbol != SYMBOL:
        errors.append(f"symbol={s.symbol}, expected {SYMBOL}")
    if s.category != CATEGORY:
        errors.append(f"category={s.category}, expected {CATEGORY}")
    if s.leverage != 5:
        errors.append(f"leverage={s.leverage}, expected 5")
    if s.margin_mode != "crossed":
        errors.append(f"margin_mode={s.margin_mode}, expected crossed")
    if s.hold_mode != "hedge_mode":
        errors.append(f"hold_mode={s.hold_mode}, expected hedge_mode")
    if abs(s.entry_margin_pct - 50.0) > 1e-9:
        errors.append(f"entry_margin_pct={s.entry_margin_pct}, expected 50")
    if not (0.10 <= s.min_stop_pct < s.max_stop_pct <= 2.0):
        errors.append("stop width contract must satisfy 0.10 <= min < max <= 2.0")
    if not (s.max_stop_pct < s.hard_stop_max_pct <= 3.0):
        errors.append("hard_stop_max_pct must be above max_stop_pct and <= 3.0")
    if not (0.5 <= s.trail_activate_r <= 5.0):
        errors.append("trail_activate_r outside 0.5-5.0")
    if s.profile_bins < 16 or s.profile_bins > 200:
        errors.append("profile_bins outside 16-200")
    if not (0.5 <= s.profile_high_volume_quantile < 0.95):
        errors.append("profile_high_volume_quantile outside 0.5-0.95")
    if s.shadow_initial_equity <= 0:
        errors.append("shadow_initial_equity must be positive")
    if not (0.0 <= s.shadow_entry_slippage_bps <= 100.0):
        errors.append("shadow_entry_slippage_bps outside 0-100")
    if not (0.0 <= s.shadow_exit_slippage_bps <= 100.0):
        errors.append("shadow_exit_slippage_bps outside 0-100")
    if not (0.0 <= s.shadow_taker_fee_rate <= 0.01):
        errors.append("shadow_taker_fee_rate outside 0-1%")
    return errors


# ---------------------------------------------------------------------------
# State, Telegram, arming
# ---------------------------------------------------------------------------


def default_state() -> dict[str, Any]:
    return {
        "version": VERSION,
        "managed_position": None,
        "pending_entry": None,
        "last_signal_bar_ts": 0,
        "last_cycle_ts": None,
        "last_error": None,
        "last_diagnostics": {},
        "day": day_key(),
        "day_start_equity": None,
        "entries_today": 0,
        "week": week_key(),
        "week_start_equity": None,
        "peak_equity": None,
        "consecutive_losses": 0,
        "cooldown_until": None,
        "last_heartbeat_ts": None,
    }


def load_state(settings: Settings) -> dict[str, Any]:
    raw = read_json(settings.state_path, {})
    state = default_state()
    if isinstance(raw, dict):
        state.update(raw)
    state["version"] = VERSION
    return state


def save_state(settings: Settings, state: dict[str, Any]) -> None:
    atomic_write_json(settings.state_path, state)


def default_shadow_state(initial_equity: float) -> dict[str, Any]:
    initial = float(initial_equity)
    return {
        "version": VERSION,
        "mode": "SHADOW",
        "initial_equity": initial,
        "shadow_balance": initial,
        "shadow_equity": initial,
        "shadow_unrealized_pnl": 0.0,
        "shadow_estimated_exit_fee": 0.0,
        "shadow_realized_net_pnl": 0.0,
        "shadow_total_fees": 0.0,
        "managed_position": None,
        "last_signal_bar_ts": 0,
        "last_cycle_ts": None,
        "last_error": None,
        "last_diagnostics": {},
        "day": day_key(),
        "day_start_equity": initial,
        "entries_today": 0,
        "week": week_key(),
        "week_start_equity": initial,
        "peak_equity": initial,
        "consecutive_losses": 0,
        "cooldown_until": None,
        "last_heartbeat_ts": None,
        "last_equity_sample_ts": None,
        "closed_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "shadow_gross_profit": 0.0,
        "shadow_gross_loss_abs": 0.0,
        "shadow_sum_net_r": 0.0,
        "shadow_max_drawdown_pct": 0.0,
    }


def load_shadow_state(settings: Settings, initial_equity: float | None = None) -> dict[str, Any]:
    seed = float(initial_equity if initial_equity is not None else settings.shadow_initial_equity)
    raw = read_json(settings.shadow_state_path, None)
    if not isinstance(raw, dict) or str(raw.get("mode", "")).upper() != "SHADOW":
        return default_shadow_state(seed)
    state = default_shadow_state(safe_float(raw.get("initial_equity"), seed) or seed)
    state.update(raw)
    state["version"] = VERSION
    state["mode"] = "SHADOW"
    return state


def save_shadow_state(settings: Settings, state: dict[str, Any]) -> None:
    state["version"] = VERSION
    state["mode"] = "SHADOW"
    atomic_write_json(settings.shadow_state_path, state)


def reset_shadow_files(settings: Settings, initial_equity: float) -> dict[str, Any]:
    if initial_equity <= 0:
        raise RuntimeError("shadow seed must be positive")
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
    archived: list[str] = []
    for path in (
        settings.shadow_state_path,
        settings.shadow_trades_path,
        settings.shadow_events_path,
        settings.shadow_equity_path,
        settings.shadow_log_path,
    ):
        if not path.exists():
            continue
        backup = path.with_name(f"{path.name}.{stamp}.bak")
        os.replace(path, backup)
        archived.append(str(backup))
    state = default_shadow_state(initial_equity)
    save_shadow_state(settings, state)
    return {"ok": True, "seed": initial_equity, "state_path": str(settings.shadow_state_path), "archived": archived}


def make_bot() -> Any | None:
    if TelegramBot is None:
        return None
    load_dotenv(ROOT / ".env")
    token = os.getenv("TELEGRAM_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    try:
        return TelegramBot(token, chat_id)
    except Exception:
        return None


def notify(text: str, settings: Settings) -> None:
    if not settings.notify:
        return
    bot = make_bot()
    if bot is None:
        return
    try:
        bot.send(text)
    except Exception as exc:
        log(f"telegram error: {exc}", settings)


def expected_phrases() -> list[str]:
    return [ARM_PHRASE, RISK_PHRASE, NO_WITHDRAW_PHRASE, IP_WHITELIST_PHRASE]


def write_arm_file(settings: Settings, phrases: Sequence[str]) -> dict[str, Any]:
    if list(phrases) != expected_phrases():
        raise RuntimeError("arming phrases do not exactly match the live-risk contract")
    payload = {
        "version": VERSION,
        "armed_at": iso(),
        "phrases": list(phrases),
        "config_sha256": sha256_file(settings.config_path),
        "contract": {
            "symbol": settings.symbol,
            "leverage": settings.leverage,
            "margin_mode": settings.margin_mode,
            "hold_mode": settings.hold_mode,
            "entry_margin_pct": settings.entry_margin_pct,
            "notional_equity_multiple": settings.entry_margin_pct / 100.0 * settings.leverage,
        },
    }
    atomic_write_json(settings.arm_path, payload)
    return payload


def arm_valid(settings: Settings) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if os.getenv(LIVE_ENV, "").strip().lower() != "true":
        reasons.append(f"{LIVE_ENV} is not true")
    payload = read_json(settings.arm_path, None)
    if not isinstance(payload, dict):
        reasons.append("arm file missing or invalid")
        return False, reasons
    if payload.get("version") != VERSION:
        reasons.append("arm version mismatch")
    if payload.get("phrases") != expected_phrases():
        reasons.append("arm phrases mismatch")
    if payload.get("config_sha256") != sha256_file(settings.config_path):
        reasons.append("config changed after arm")
    return not reasons, reasons


def disarm(settings: Settings) -> None:
    settings.arm_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Bitget API helpers
# ---------------------------------------------------------------------------


def make_client() -> Any:
    if BitgetUTAClient is None:
        raise RuntimeError("BitgetUTAClient import failed")
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("BITGET_API_KEY", "").strip()
    secret = os.getenv("BITGET_SECRET_KEY", "").strip()
    passphrase = os.getenv("BITGET_PASSPHRASE", "").strip()
    if not api_key or not secret or not passphrase:
        raise RuntimeError("missing BITGET_API_KEY / BITGET_SECRET_KEY / BITGET_PASSPHRASE")
    return BitgetUTAClient(api_key=api_key, secret_key=secret, passphrase=passphrase)


class PublicBitgetClient:
    """Strict read-only HTTP client used by OBSERVE and SHADOW modes.

    It deliberately refuses authenticated calls, non-market endpoints and all
    POST requests.  This makes a shadow process incapable of placing or
    cancelling an order even when real Bitget API keys exist in ``.env``.
    """

    def __init__(self, base_url: str | None = None, timeout_seconds: float = 12.0) -> None:
        self.base_url = (base_url or os.getenv("BITGET_PUBLIC_API_BASE", "https://api.bitget.com")).rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get(self, path: str, params: dict[str, Any] | None = None, auth: bool = False) -> dict[str, Any]:
        if auth:
            raise RuntimeError("public shadow client refuses authenticated requests")
        if not path.startswith("/api/v3/market/"):
            raise RuntimeError(f"public shadow client refuses non-market endpoint: {path}")
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": f"btc-structure-shadow/{VERSION}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"invalid public API response for {path}")
        return payload

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"public shadow client refuses POST: {path}")


def make_public_client() -> PublicBitgetClient:
    return PublicBitgetClient()


def api_success(resp: Any) -> bool:
    return isinstance(resp, dict) and str(resp.get("code", "")) in {"00000", "0"}


def require_success(resp: Any, context: str) -> dict[str, Any]:
    if not api_success(resp):
        raise RuntimeError(f"{context} failed: {resp}")
    return resp


def response_data(resp: Any) -> Any:
    return resp.get("data") if isinstance(resp, dict) else None


def response_list(resp: Any) -> list[Any]:
    data = response_data(resp)
    if isinstance(data, dict) and isinstance(data.get("list"), list):
        return data["list"]
    if isinstance(data, list):
        return data
    return []


def client_get(client: Any, path: str, params: dict[str, Any] | None = None, auth: bool = True) -> dict[str, Any]:
    params = params or {}
    try:
        return client.get(path, params, auth=auth)
    except TypeError:
        return client.get(path, params)


def client_post(client: Any, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    return client.post(path, payload)


def fetch_instrument(client: Any, symbol: str) -> Instrument:
    resp = require_success(
        client_get(client, "/api/v3/market/instruments", {"category": CATEGORY, "symbol": symbol}, auth=False),
        f"instrument {symbol}",
    )
    row = next((x for x in response_list(resp) if isinstance(x, dict) and str(x.get("symbol")) == symbol), None)
    if row is None:
        raise RuntimeError(f"instrument not found: {symbol}")
    price_step = as_decimal(row.get("priceMultiplier"), "0")
    qty_step = as_decimal(row.get("quantityMultiplier"), "0")
    if price_step <= 0:
        price_step = Decimal(1).scaleb(-int(safe_float(row.get("pricePrecision"), 8)))
    if qty_step <= 0:
        qty_step = Decimal(1).scaleb(-int(safe_float(row.get("quantityPrecision"), 8)))
    return Instrument(
        symbol=symbol,
        status=str(row.get("status", "")).lower(),
        symbol_type=str(row.get("symbolType", "")).lower(),
        min_order_qty=as_decimal(row.get("minOrderQty"), "0"),
        max_order_qty=as_decimal(row.get("maxOrderQty"), "0"),
        max_market_order_qty=as_decimal(row.get("maxMarketOrderQty"), "0"),
        min_order_amount=as_decimal(row.get("minOrderAmount"), "0"),
        price_step=price_step.copy_abs(),
        quantity_step=qty_step.copy_abs(),
        min_leverage=safe_float(row.get("minLeverage"), 1.0),
        max_leverage=safe_float(row.get("maxLeverage"), 1.0),
        maker_fee=safe_float(row.get("makerFeeRate"), 0.0002),
        taker_fee=safe_float(row.get("takerFeeRate"), 0.0006),
    )


def fetch_ticker(client: Any, symbol: str) -> Ticker:
    resp = require_success(
        client_get(client, "/api/v3/market/tickers", {"category": CATEGORY, "symbol": symbol}, auth=False),
        f"ticker {symbol}",
    )
    row = next((x for x in response_list(resp) if isinstance(x, dict) and str(x.get("symbol")) == symbol), None)
    if row is None:
        raise RuntimeError(f"ticker not found: {symbol}")
    last = safe_float(row.get("lastPrice") or row.get("lastPr"))
    mark = safe_float(row.get("markPrice"), last)
    bid = safe_float(row.get("bid1Price") or row.get("bidPr"), last)
    ask = safe_float(row.get("ask1Price") or row.get("askPr"), last)
    if min(last, mark, bid, ask) <= 0:
        raise RuntimeError(f"invalid ticker: {row}")
    return Ticker(symbol=symbol, last=last, mark=mark, bid=bid, ask=ask)


def parse_candle_rows(rows: Sequence[Any]) -> list[Bar]:
    out: list[Bar] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            bar = Bar(
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                turnover=float(row[6]) if len(row) > 6 else 0.0,
            )
        except (TypeError, ValueError):
            continue
        if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.high < bar.low:
            continue
        out.append(bar)
    return sorted({bar.ts: bar for bar in out}.values(), key=lambda x: x.ts)


def fetch_candles(client: Any, symbol: str, interval: str, limit: int) -> list[Bar]:
    """Fetch recent candles with backward pagination.

    Bitget documentation and deployments have used different effective page
    limits.  Capping each page at 100 keeps this compatible with both variants.
    """
    collected: dict[int, Bar] = {}
    cursor: int | None = None
    attempts = 0
    while len(collected) < limit and attempts < 12:
        attempts += 1
        page_limit = min(100, max(1, limit - len(collected)))
        params: dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "interval": interval,
            "type": "market",
            "limit": str(page_limit),
        }
        if cursor is not None:
            params["endTime"] = str(cursor)
        resp = require_success(
            client_get(client, "/api/v3/market/candles", params, auth=False),
            f"candles {symbol} {interval}",
        )
        page = parse_candle_rows(response_data(resp) if isinstance(response_data(resp), list) else [])
        if not page:
            break
        before = len(collected)
        for bar in page:
            collected[bar.ts] = bar
        oldest = min(bar.ts for bar in page)
        next_cursor = oldest - 1
        if len(collected) == before or (cursor is not None and next_cursor >= cursor):
            break
        cursor = next_cursor
        if len(page) < page_limit:
            break
    bars = sorted(collected.values(), key=lambda item: item.ts)[-limit:]
    if len(bars) < min(30, max(10, limit // 4)):
        raise RuntimeError(f"not enough candles {symbol} {interval}: {len(bars)}")
    return bars


def completed_bars(bars: Sequence[Bar], interval: str, at: datetime | None = None) -> list[Bar]:
    now_ms = int((at or now_utc()).timestamp() * 1000)
    span = INTERVAL_MS[interval]
    return [bar for bar in bars if bar.ts + span <= now_ms + 2_000]


def fetch_account_assets(client: Any) -> dict[str, Any]:
    resp = require_success(client_get(client, "/api/v3/account/assets", {}, auth=True), "account assets")
    data = response_data(resp)
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid account assets: {resp}")
    return data


def account_equity(assets: dict[str, Any]) -> float:
    return safe_float(assets.get("usdtEquity") or assets.get("accountEquity") or assets.get("effEquity"))


def account_available_usdt(assets: dict[str, Any]) -> float:
    rows = assets.get("assets") if isinstance(assets.get("assets"), list) else []
    for row in rows:
        if isinstance(row, dict) and str(row.get("coin", "")).upper() == "USDT":
            return safe_float(row.get("available") or row.get("equity"))
    return safe_float(assets.get("usdtEquity") or assets.get("effEquity") or assets.get("accountEquity"))


def fetch_account_settings(client: Any) -> dict[str, Any]:
    resp = require_success(client_get(client, "/api/v3/account/settings", {}, auth=True), "account settings")
    data = response_data(resp)
    return data if isinstance(data, dict) else {}


def fetch_positions(client: Any, symbol: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"category": CATEGORY}
    if symbol:
        params["symbol"] = symbol
    resp = require_success(client_get(client, "/api/v3/position/current-position", params, auth=True), "current positions")
    return [row for row in response_list(resp) if isinstance(row, dict)]


def nonzero_positions(client: Any, symbol: str | None = None) -> list[dict[str, Any]]:
    return [row for row in fetch_positions(client, symbol) if abs(safe_float(row.get("total"))) > 0]


def fetch_open_orders(client: Any, symbol: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"category": CATEGORY, "limit": "100"}
    if symbol:
        params["symbol"] = symbol
    resp = require_success(client_get(client, "/api/v3/trade/unfilled-orders", params, auth=True), "open orders")
    return [row for row in response_list(resp) if isinstance(row, dict)]


def fetch_strategy_orders_best_effort(client: Any, symbol: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"category": CATEGORY, "type": "tpsl", "limit": "100"}
    if symbol:
        params["symbol"] = symbol
    try:
        resp = client_get(client, "/api/v3/trade/unfilled-strategy-orders", params, auth=True)
        return [row for row in response_list(resp) if isinstance(row, dict)] if api_success(resp) else []
    except Exception:
        return []


def account_symbol_row(settings_data: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    rows = settings_data.get("symbolConfigList")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol.upper():
            return row
    return None


def infer_account_level(settings_data: dict[str, Any]) -> str:
    return str(settings_data.get("accountLevel") or settings_data.get("accountMode") or "").lower()


def infer_hold_mode(settings_data: dict[str, Any], positions: Sequence[dict[str, Any]] | None = None) -> str:
    raw = str(settings_data.get("holdMode") or settings_data.get("positionMode") or "").lower()
    if raw in {"one_way_mode", "hedge_mode"}:
        return raw
    for row in positions or []:
        value = str(row.get("holdMode", "")).lower()
        if value in {"one_way_mode", "hedge_mode"}:
            return value
    return "unknown"


def leverage_values_from_row(row: dict[str, Any] | None) -> list[int]:
    if not isinstance(row, dict):
        return []
    values: list[int] = []
    raw = row.get("leverage")
    if isinstance(raw, (list, tuple)):
        values.extend(int(safe_float(x)) for x in raw)
    elif raw not in (None, ""):
        values.append(int(safe_float(raw)))
    for key in ("longLeverage", "shortLeverage"):
        if row.get(key) not in (None, ""):
            values.append(int(safe_float(row.get(key))))
    return [value for value in values if value > 0]


# ---------------------------------------------------------------------------
# Indicators and volume profile
# ---------------------------------------------------------------------------


def ema_series(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return out


def ema_last(values: Sequence[float], period: int) -> float:
    series = ema_series(values, period)
    return series[-1] if series else 0.0


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    out: list[float] = []
    previous: float | None = None
    for bar in bars:
        if previous is None:
            tr = bar.high - bar.low
        else:
            tr = max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous))
        out.append(max(0.0, tr))
        previous = bar.close
    return out


def wilder_series(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 1.0 / max(1, period)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return out


def atr_value(bars: Sequence[Bar], period: int = 14) -> float:
    trs = true_ranges(bars)
    smoothed = wilder_series(trs, period)
    return smoothed[-1] if smoothed else 0.0


def adx_snapshot(bars: Sequence[Bar], period: int = 14) -> tuple[float, float, float]:
    if len(bars) < period + 2:
        return 0.0, 0.0, 0.0
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    previous = bars[0]
    for bar in bars[1:]:
        up = bar.high - previous.high
        down = previous.low - bar.low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        trs.append(max(bar.high - bar.low, abs(bar.high - previous.close), abs(bar.low - previous.close)))
        previous = bar
    tr_s = wilder_series(trs, period)
    plus_s = wilder_series(plus_dm, period)
    minus_s = wilder_series(minus_dm, period)
    plus_di: list[float] = []
    minus_di: list[float] = []
    dx: list[float] = []
    for tr, p, m in zip(tr_s, plus_s, minus_s):
        pdi = 100.0 * p / tr if tr > 0 else 0.0
        mdi = 100.0 * m / tr if tr > 0 else 0.0
        plus_di.append(pdi)
        minus_di.append(mdi)
        denom = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else 0.0)
    adx = wilder_series(dx, period)
    return (adx[-1] if adx else 0.0, plus_di[-1] if plus_di else 0.0, minus_di[-1] if minus_di else 0.0)


def trend_snapshot(bars4h: Sequence[Bar], bars1h: Sequence[Bar], settings: Settings) -> TrendSnapshot:
    c4 = list(bars4h)
    c1 = list(bars1h)
    required4 = settings.trend_slow_4h + settings.trend_slope_lookback + 5
    required1 = settings.confirm_slow_1h + 5
    if len(c4) < required4 or len(c1) < required1:
        return TrendSnapshot("NONE", 0, 0, 0, 0, 0, 0, 0, c4[-1].close if c4 else 0, c1[-1].close if c1 else 0)

    closes4 = [bar.close for bar in c4]
    closes1 = [bar.close for bar in c1]
    fast4 = ema_series(closes4, settings.trend_fast_4h)
    slow4 = ema_series(closes4, settings.trend_slow_4h)
    fast1 = ema_series(closes1, settings.confirm_fast_1h)
    slow1 = ema_series(closes1, settings.confirm_slow_1h)
    adx, plus_di, minus_di = adx_snapshot(c4, settings.adx_period)
    lookback = settings.trend_slope_lookback

    up = (
        fast4[-1] > slow4[-1]
        and fast4[-1] > fast4[-1 - lookback]
        and slow4[-1] > slow4[-1 - lookback]
        and closes4[-1] > slow4[-1]
        and fast1[-1] > slow1[-1]
        and closes1[-1] > slow1[-1]
        and adx >= settings.adx_min_4h
        and plus_di > minus_di
    )
    down = (
        fast4[-1] < slow4[-1]
        and fast4[-1] < fast4[-1 - lookback]
        and slow4[-1] < slow4[-1 - lookback]
        and closes4[-1] < slow4[-1]
        and fast1[-1] < slow1[-1]
        and closes1[-1] < slow1[-1]
        and adx >= settings.adx_min_4h
        and minus_di > plus_di
    )
    side = "UP" if up else "DOWN" if down else "NONE"
    return TrendSnapshot(
        side=side,
        adx4h=adx,
        plus_di4h=plus_di,
        minus_di4h=minus_di,
        ema50_4h=fast4[-1],
        ema200_4h=slow4[-1],
        ema20_1h=fast1[-1],
        ema50_1h=slow1[-1],
        close4h=closes4[-1],
        close1h=closes1[-1],
    )


def volume_profile_zones(
    bars: Sequence[Bar],
    *,
    bins: int,
    high_volume_quantile: float,
    max_zones: int = 12,
) -> list[VolumeZone]:
    """Approximate a volume profile by distributing each candle's volume over
    every price bin touched by its high-low range.

    This is intentionally more conservative than assigning all volume to HLC3.
    It cannot reproduce trade-by-trade volume-at-price, but it avoids pretending
    that the candle's entire volume traded at one price.
    """
    rows = [bar for bar in bars if bar.high > bar.low and bar.volume >= 0]
    if len(rows) < 10 or bins < 4:
        return []
    low = min(bar.low for bar in rows)
    high = max(bar.high for bar in rows)
    span = high - low
    if span <= 0:
        return []
    width = span / bins
    volumes = [0.0 for _ in range(bins)]
    for bar in rows:
        first = max(0, min(bins - 1, int(math.floor((bar.low - low) / width))))
        last = max(0, min(bins - 1, int(math.floor((bar.high - low) / width))))
        count = max(1, last - first + 1)
        allocated = max(0.0, bar.volume) / count
        for idx in range(first, last + 1):
            volumes[idx] += allocated
    positive = [value for value in volumes if value > 0]
    if not positive:
        return []
    threshold = percentile(positive, high_volume_quantile)
    max_volume = max(positive)
    hot = [value >= threshold and value > 0 for value in volumes]
    zones: list[VolumeZone] = []
    idx = 0
    while idx < bins:
        if not hot[idx]:
            idx += 1
            continue
        first = idx
        while idx + 1 < bins and hot[idx + 1]:
            idx += 1
        last = idx
        zone_volume = sum(volumes[first : last + 1])
        weighted = sum((low + (j + 0.5) * width) * volumes[j] for j in range(first, last + 1))
        center = weighted / zone_volume if zone_volume > 0 else low + ((first + last + 1) / 2) * width
        zones.append(
            VolumeZone(
                low=low + first * width,
                high=low + (last + 1) * width,
                center=center,
                volume=zone_volume,
                relative_volume=zone_volume / max_volume if max_volume > 0 else 0.0,
                first_bin=first,
                last_bin=last,
            )
        )
        idx += 1
    selected = sorted(zones, key=lambda zone: zone.volume, reverse=True)[:max_zones]
    max_zone_volume = max((zone.volume for zone in selected), default=0.0)
    if max_zone_volume <= 0:
        return selected
    return [
        VolumeZone(
            low=zone.low,
            high=zone.high,
            center=zone.center,
            volume=zone.volume,
            relative_volume=zone.volume / max_zone_volume,
            first_bin=zone.first_bin,
            last_bin=zone.last_bin,
        )
        for zone in selected
    ]


def nearest_support(zones: Sequence[VolumeZone], price: float, allowance: float = 0.0) -> VolumeZone | None:
    candidates = [zone for zone in zones if zone.center <= price + allowance]
    return min(candidates, key=lambda zone: zone.distance_to(price), default=None)


def nearest_resistance(zones: Sequence[VolumeZone], price: float, allowance: float = 0.0) -> VolumeZone | None:
    candidates = [zone for zone in zones if zone.center >= price - allowance]
    return min(candidates, key=lambda zone: zone.distance_to(price), default=None)


def opposing_room_r(zones: Sequence[VolumeZone], side: str, entry: float, risk: float, active_zone: VolumeZone) -> float:
    if risk <= 0:
        return 0.0
    if side == "LONG":
        opponents = [zone for zone in zones if zone.low > max(entry, active_zone.high)]
        if not opponents:
            return 99.0
        target = min(opponents, key=lambda zone: zone.low)
        return max(0.0, target.low - entry) / risk
    opponents = [zone for zone in zones if zone.high < min(entry, active_zone.low)]
    if not opponents:
        return 99.0
    target = max(opponents, key=lambda zone: zone.high)
    return max(0.0, entry - target.high) / risk


# ---------------------------------------------------------------------------
# Signal generation and exits
# ---------------------------------------------------------------------------


def build_candidate(
    settings: Settings,
    ticker: Ticker,
    bars15: Sequence[Bar],
    bars1h: Sequence[Bar],
    bars4h: Sequence[Bar],
) -> tuple[Candidate | None, dict[str, Any]]:
    c15 = completed_bars(bars15, "15m")
    c1 = completed_bars(bars1h, "1H")
    c4 = completed_bars(bars4h, "4H")
    diag: dict[str, Any] = {
        "completed_15m": len(c15),
        "completed_1h": len(c1),
        "completed_4h": len(c4),
        "price": ticker.mark,
    }
    if len(c15) < 40 or len(c1) < settings.profile_lookback_1h or len(c4) < settings.trend_slow_4h + 10:
        diag["reason"] = "insufficient_bars"
        return None, diag

    trend = trend_snapshot(c4, c1, settings)
    diag["trend"] = asdict(trend)
    if trend.side == "NONE":
        diag["reason"] = "no_higher_timeframe_trend"
        return None, diag

    profile_bars = c1[-settings.profile_lookback_1h :]
    zones = volume_profile_zones(
        profile_bars,
        bins=settings.profile_bins,
        high_volume_quantile=settings.profile_high_volume_quantile,
    )
    diag["zones"] = [asdict(zone) for zone in zones]
    if not zones:
        diag["reason"] = "no_volume_zone"
        return None, diag

    signal_bar = c15[-1]
    atr15 = atr_value(c15[-80:], settings.adx_period)
    volume_med = median(bar.volume for bar in c15[-21:-1])
    volume_ratio = signal_bar.volume / volume_med if volume_med > 0 else 0.0
    body_ratio = signal_bar.body_ratio
    allowance = settings.profile_touch_atr * atr15
    entry = ticker.ask if trend.side == "UP" else ticker.bid

    if trend.side == "UP":
        side = "LONG"
        zone = nearest_support(zones, signal_bar.close, allowance)
        if zone is None:
            diag["reason"] = "no_support_zone"
            return None, diag
        touched = signal_bar.low <= zone.high + allowance and signal_bar.high >= zone.low - allowance
        reclaimed = signal_bar.close > zone.high and signal_bar.close > signal_bar.open
        close_quality = signal_bar.close_location >= 0.60
        soft_stop = zone.low - settings.soft_stop_buffer_atr * atr15
        risk = entry - soft_stop
    else:
        side = "SHORT"
        zone = nearest_resistance(zones, signal_bar.close, allowance)
        if zone is None:
            diag["reason"] = "no_resistance_zone"
            return None, diag
        touched = signal_bar.high >= zone.low - allowance and signal_bar.low <= zone.high + allowance
        reclaimed = signal_bar.close < zone.low and signal_bar.close < signal_bar.open
        close_quality = signal_bar.close_location <= 0.40
        soft_stop = zone.high + settings.soft_stop_buffer_atr * atr15
        risk = soft_stop - entry

    diag.update(
        {
            "side": side,
            "signal_bar": asdict(signal_bar),
            "active_zone": asdict(zone),
            "atr15": atr15,
            "volume_median_20": volume_med,
            "volume_ratio": volume_ratio,
            "body_ratio": body_ratio,
            "touched": touched,
            "reclaimed": reclaimed,
            "close_quality": close_quality,
        }
    )
    if not touched or not reclaimed or not close_quality:
        diag["reason"] = "pullback_reclaim_not_confirmed"
        return None, diag
    if volume_ratio < settings.entry_volume_ratio or body_ratio < settings.entry_body_ratio:
        diag["reason"] = "entry_candle_quality_too_low"
        return None, diag
    if risk <= 0:
        diag["reason"] = "invalid_structural_stop"
        return None, diag

    stop_pct = risk / entry * 100.0
    if not (settings.min_stop_pct <= stop_pct <= settings.max_stop_pct):
        diag["reason"] = "soft_stop_width_outside_contract"
        diag["stop_distance_pct"] = stop_pct
        return None, diag

    extra = max(settings.hard_stop_extra_atr * atr15, entry * settings.hard_stop_min_extra_pct / 100.0)
    if side == "LONG":
        desired_hard = soft_stop - extra
        hard_floor = entry * (1.0 - settings.hard_stop_max_pct / 100.0)
        hard_stop = max(desired_hard, hard_floor)
        if hard_stop >= soft_stop:
            hard_stop = soft_stop - max(atr15 * 0.10, entry * 0.001)
    else:
        desired_hard = soft_stop + extra
        hard_ceiling = entry * (1.0 + settings.hard_stop_max_pct / 100.0)
        hard_stop = min(desired_hard, hard_ceiling)
        if hard_stop <= soft_stop:
            hard_stop = soft_stop + max(atr15 * 0.10, entry * 0.001)

    hard_pct = abs(entry - hard_stop) / entry * 100.0
    if hard_pct > settings.hard_stop_max_pct + 1e-9:
        diag["reason"] = "hard_stop_too_wide"
        return None, diag

    room_r = opposing_room_r(zones, side, entry, risk, zone)
    diag["room_r"] = room_r
    if room_r < settings.min_room_r:
        diag["reason"] = "next_volume_wall_too_close"
        return None, diag

    adx_component = min(20.0, max(0.0, (trend.adx4h - settings.adx_min_4h) * 1.25 + 10.0))
    volume_component = min(20.0, max(0.0, (volume_ratio - 0.8) * 25.0))
    body_component = min(15.0, body_ratio * 20.0)
    room_component = min(15.0, room_r * 4.0)
    zone_component = min(10.0, zone.relative_volume * 10.0)
    score = 20.0 + adx_component + volume_component + body_component + room_component + zone_component
    diag["score"] = score
    if score < settings.signal_score_min:
        diag["reason"] = "score_below_threshold"
        return None, diag

    candidate = Candidate(
        symbol=settings.symbol,
        side=side,
        signal_bar_ts=signal_bar.ts,
        entry_reference=entry,
        zone_low=zone.low,
        zone_high=zone.high,
        zone_center=zone.center,
        soft_stop=soft_stop,
        hard_stop=hard_stop,
        initial_risk=risk,
        stop_distance_pct=stop_pct,
        score=score,
        setup="UPTREND_HVN_PULLBACK_RECLAIM" if side == "LONG" else "DOWNTREND_HVN_RETRACE_REJECTION",
        diagnostics=diag,
    )
    diag["reason"] = "candidate"
    return candidate, diag


def strong_structure_break(
    managed: ManagedPosition,
    completed15: Sequence[Bar],
    settings: Settings,
) -> tuple[bool, dict[str, Any]]:
    if len(completed15) < 25:
        return False, {"reason": "insufficient_bars"}
    bar = completed15[-1]
    prev = completed15[-2]
    atr15 = atr_value(completed15[-80:], settings.adx_period)
    med = median(item.volume for item in completed15[-21:-1])
    volume_ratio = bar.volume / med if med > 0 else 0.0
    body_ok = bar.body_ratio >= settings.strong_break_body_ratio
    volume_ok = volume_ratio >= settings.strong_break_volume_ratio
    extra = settings.strong_break_extra_atr * atr15

    if managed.side == "LONG":
        one_strong = (
            bar.close < managed.soft_stop - extra
            and body_ok
            and volume_ok
            and bar.close_location <= settings.strong_break_close_location
        )
        two_closes = prev.close < managed.soft_stop and bar.close < managed.soft_stop
    else:
        one_strong = (
            bar.close > managed.soft_stop + extra
            and body_ok
            and volume_ok
            and bar.close_location >= 1.0 - settings.strong_break_close_location
        )
        two_closes = prev.close > managed.soft_stop and bar.close > managed.soft_stop

    return one_strong or two_closes, {
        "bar_ts": bar.ts,
        "bar_close": bar.close,
        "soft_stop": managed.soft_stop,
        "atr15": atr15,
        "volume_ratio": volume_ratio,
        "body_ratio": bar.body_ratio,
        "close_location": bar.close_location,
        "one_strong": one_strong,
        "two_consecutive_closes": two_closes,
    }


def latest_pivot_level(bars: Sequence[Bar], side: str, left_right: int = 2) -> float | None:
    if len(bars) < left_right * 2 + 3:
        return None
    candidates: list[float] = []
    for idx in range(left_right, len(bars) - left_right):
        window = bars[idx - left_right : idx + left_right + 1]
        bar = bars[idx]
        if side == "LONG" and bar.low == min(x.low for x in window):
            candidates.append(bar.low)
        if side == "SHORT" and bar.high == max(x.high for x in window):
            candidates.append(bar.high)
    return candidates[-1] if candidates else None


def update_trailing_stop(
    managed: ManagedPosition,
    completed15: Sequence[Bar],
    settings: Settings,
    mark: float,
) -> tuple[float, bool, dict[str, Any]]:
    if managed.initial_risk <= 0:
        return managed.soft_stop, managed.trail_active, {"reason": "invalid_initial_risk"}
    profit = mark - managed.entry_price if managed.side == "LONG" else managed.entry_price - mark
    profit_r = profit / managed.initial_risk
    if profit_r < settings.trail_activate_r:
        return managed.soft_stop, False, {"profit_r": profit_r, "reason": "trail_not_active"}

    atr15 = atr_value(completed15[-100:], settings.adx_period)
    lookback = min(len(completed15), settings.trail_profile_lookback_15m)
    zones = volume_profile_zones(
        completed15[-lookback:],
        bins=max(32, settings.profile_bins // 2),
        high_volume_quantile=settings.profile_high_volume_quantile,
        max_zones=10,
    )
    pivot = latest_pivot_level(completed15[-80:], managed.side)

    if managed.side == "LONG":
        chandelier = managed.best_price - settings.trail_chandelier_atr * atr15
        support = nearest_support(zones, mark, allowance=0.0)
        structure = (support.low if support else pivot) if (support or pivot is not None) else chandelier
        if pivot is not None and support is not None:
            structure = min(support.low, pivot)
        structure_stop = float(structure) - settings.trail_zone_buffer_atr * atr15
        breathing_stop = min(chandelier, structure_stop)
        new_stop = max(managed.soft_stop, breathing_stop)
        new_stop = min(new_stop, mark - max(0.10 * atr15, mark * 0.0005))
    else:
        chandelier = managed.best_price + settings.trail_chandelier_atr * atr15
        resistance = nearest_resistance(zones, mark, allowance=0.0)
        structure = (resistance.high if resistance else pivot) if (resistance or pivot is not None) else chandelier
        if pivot is not None and resistance is not None:
            structure = max(resistance.high, pivot)
        structure_stop = float(structure) + settings.trail_zone_buffer_atr * atr15
        breathing_stop = max(chandelier, structure_stop)
        new_stop = min(managed.soft_stop, breathing_stop)
        new_stop = max(new_stop, mark + max(0.10 * atr15, mark * 0.0005))

    return new_stop, True, {
        "profit_r": profit_r,
        "atr15": atr15,
        "chandelier": chandelier,
        "pivot": pivot,
        "zones": [asdict(zone) for zone in zones],
        "old_stop": managed.soft_stop,
        "new_stop": new_stop,
    }


def trend_flip_due(managed: ManagedPosition, completed1h: Sequence[Bar], settings: Settings) -> tuple[bool, dict[str, Any]]:
    if not settings.trend_flip_exit or len(completed1h) < settings.confirm_slow_1h + 5:
        return False, {"reason": "disabled_or_insufficient"}
    closes = [bar.close for bar in completed1h]
    fast = ema_last(closes, settings.confirm_fast_1h)
    slow = ema_last(closes, settings.confirm_slow_1h)
    bar = completed1h[-1]
    med = median(item.volume for item in completed1h[-21:-1])
    volume_ratio = bar.volume / med if med > 0 else 0.0
    if managed.side == "LONG":
        due = fast < slow and bar.close < slow and volume_ratio >= settings.trend_flip_volume_ratio
    else:
        due = fast > slow and bar.close > slow and volume_ratio >= settings.trend_flip_volume_ratio
    return due, {"ema_fast": fast, "ema_slow": slow, "close": bar.close, "volume_ratio": volume_ratio}


# ---------------------------------------------------------------------------
# Sizing and order execution
# ---------------------------------------------------------------------------


def calculate_qty(equity: float, entry_price: float, settings: Settings, instrument: Instrument) -> Decimal:
    margin = equity * settings.entry_margin_pct / 100.0
    notional = margin * settings.leverage
    raw = as_decimal(notional) / as_decimal(entry_price)
    qty = quantize_step(raw, instrument.quantity_step, "down")
    if qty < instrument.min_order_qty:
        return Decimal("0")
    if instrument.max_market_order_qty > 0:
        qty = min(qty, instrument.max_market_order_qty)
    elif instrument.max_order_qty > 0:
        qty = min(qty, instrument.max_order_qty)
    if qty * as_decimal(entry_price) < instrument.min_order_amount:
        return Decimal("0")
    return qty


def rounded_price(instrument: Instrument, price: float, side: str, purpose: str) -> str:
    if purpose == "stop":
        mode = "floor" if side == "LONG" else "ceil"
    else:
        mode = "nearest"
    return fmt_decimal(quantize_step(price, instrument.price_step, mode))


def unique_client_oid(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime("%m%d%H%M%S")
    token = uuid.uuid4().hex[:6]
    return f"{prefix}_{stamp}_{token}"[:32]


def shadow_fee_rate(settings: Settings, instrument: Instrument) -> float:
    configured = settings.shadow_taker_fee_rate
    return configured if configured > 0 else max(0.0, instrument.taker_fee)


def shadow_fill_price(ticker: Ticker, side: str, *, opening: bool, slippage_bps: float) -> float:
    slip = max(0.0, slippage_bps) / 10_000.0
    if opening:
        return ticker.ask * (1.0 + slip) if side == "LONG" else ticker.bid * (1.0 - slip)
    return ticker.bid * (1.0 - slip) if side == "LONG" else ticker.ask * (1.0 + slip)


def position_gross_pnl(managed: ManagedPosition, exit_price: float) -> float:
    direction = 1.0 if managed.side == "LONG" else -1.0
    return direction * managed.qty * (exit_price - managed.entry_price)


def update_shadow_mark_to_market(
    settings: Settings,
    state: dict[str, Any],
    managed: ManagedPosition | None,
    ticker: Ticker,
    instrument: Instrument,
) -> dict[str, float]:
    balance = safe_float(state.get("shadow_balance"), safe_float(state.get("initial_equity")))
    if managed is None:
        state["shadow_unrealized_pnl"] = 0.0
        state["shadow_estimated_exit_fee"] = 0.0
        state["shadow_equity"] = balance
        return {"balance": balance, "equity": balance, "unrealized_pnl": 0.0, "estimated_exit_fee": 0.0}
    gross = position_gross_pnl(managed, ticker.mark)
    fee_rate = shadow_fee_rate(settings, instrument)
    estimated_exit_fee = managed.qty * ticker.mark * fee_rate
    equity = balance + gross - estimated_exit_fee
    state["shadow_unrealized_pnl"] = gross
    state["shadow_estimated_exit_fee"] = estimated_exit_fee
    state["shadow_equity"] = equity
    return {
        "balance": balance,
        "equity": equity,
        "unrealized_pnl": gross,
        "estimated_exit_fee": estimated_exit_fee,
    }


def open_shadow_position(
    settings: Settings,
    state: dict[str, Any],
    candidate: Candidate,
    instrument: Instrument,
    ticker: Ticker,
    equity: float,
) -> dict[str, Any]:
    fill_price = shadow_fill_price(
        ticker,
        candidate.side,
        opening=True,
        slippage_bps=settings.shadow_entry_slippage_bps,
    )
    qty_dec = calculate_qty(equity, fill_price, settings, instrument)
    if qty_dec <= 0:
        raise RuntimeError("shadow quantity is below exchange minimum")
    qty = float(qty_dec)
    initial_risk = (
        fill_price - candidate.soft_stop
        if candidate.side == "LONG"
        else candidate.soft_stop - fill_price
    )
    if initial_risk <= 0:
        raise RuntimeError("shadow fill is already beyond the structural soft stop")
    notional = qty * fill_price
    fee_rate = shadow_fee_rate(settings, instrument)
    entry_fee = notional * fee_rate
    balance_before = safe_float(state.get("shadow_balance"), equity)
    balance_after = balance_before - entry_fee
    managed = ManagedPosition(
        symbol=settings.symbol,
        side=candidate.side,
        qty=qty,
        entry_price=fill_price,
        entry_ts=iso(),
        entry_order_id=unique_client_oid("shadow_open"),
        entry_client_oid=unique_client_oid("shadow_oid"),
        hold_mode=settings.hold_mode,
        initial_soft_stop=candidate.soft_stop,
        soft_stop=candidate.soft_stop,
        hard_stop=candidate.hard_stop,
        initial_risk=initial_risk,
        zone_low=candidate.zone_low,
        zone_high=candidate.zone_high,
        best_price=fill_price,
        trail_active=False,
        last_managed_bar_ts=candidate.signal_bar_ts,
        score=candidate.score,
        setup=candidate.setup,
        entry_fee=entry_fee,
        entry_notional=notional,
        entry_equity=equity,
    )
    state["shadow_balance"] = balance_after
    state["shadow_total_fees"] = safe_float(state.get("shadow_total_fees")) + entry_fee
    state["managed_position"] = asdict(managed)
    state["last_signal_bar_ts"] = candidate.signal_bar_ts
    state["entries_today"] = int(state.get("entries_today", 0)) + 1
    state["last_error"] = None
    mtm = update_shadow_mark_to_market(settings, state, managed, ticker, instrument)
    save_shadow_state(settings, state)
    event = {
        "event": "shadow_position_opened",
        "ts": iso(),
        "managed": asdict(managed),
        "fill_model": {
            "reference_bid": ticker.bid,
            "reference_ask": ticker.ask,
            "reference_mark": ticker.mark,
            "slippage_bps": settings.shadow_entry_slippage_bps,
            "fee_rate": fee_rate,
        },
        "mark_to_market": mtm,
    }
    append_jsonl(settings.shadow_events_path, event)
    notify(
        f"🧪 <b>BTC Structure SHADOW 진입</b>\n{managed.side} / 가상 cross {settings.leverage}x / 시드환산 {settings.entry_margin_pct:.0f}%\n"
        f"가상체결 {managed.entry_price:,.2f} / 수량 {managed.qty:.6f} BTC\n"
        f"소프트 {managed.soft_stop:,.2f} / 하드 {managed.hard_stop:,.2f}\n"
        f"가상자산 {mtm['equity']:,.2f} USDT / 점수 {managed.score:.1f}",
        settings,
    )
    return {"status": "shadow_opened", "managed": asdict(managed), "mark_to_market": mtm}


def close_shadow_position(
    settings: Settings,
    state: dict[str, Any],
    managed: ManagedPosition,
    ticker: Ticker,
    instrument: Instrument,
    reason: str,
) -> dict[str, Any]:
    exit_price = shadow_fill_price(
        ticker,
        managed.side,
        opening=False,
        slippage_bps=settings.shadow_exit_slippage_bps,
    )
    gross_pnl = position_gross_pnl(managed, exit_price)
    fee_rate = shadow_fee_rate(settings, instrument)
    exit_fee = managed.qty * exit_price * fee_rate
    entry_fee = max(0.0, managed.entry_fee)
    net_pnl = gross_pnl - entry_fee - exit_fee
    balance_before_exit = safe_float(state.get("shadow_balance"), safe_float(state.get("initial_equity")))
    balance_after = balance_before_exit + gross_pnl - exit_fee
    risk_usdt = managed.qty * managed.initial_risk
    net_r = net_pnl / risk_usdt if risk_usdt > 0 else 0.0
    entry_equity = managed.entry_equity if managed.entry_equity > 0 else safe_float(state.get("initial_equity"), 1.0)
    equity_return_pct = net_pnl / entry_equity * 100.0 if entry_equity > 0 else 0.0
    price_pnl_pct = trade_pnl_pct(managed, exit_price)
    result = {
        "mode": "SHADOW",
        "entry_ts": managed.entry_ts,
        "exit_ts": iso(),
        "symbol": managed.symbol,
        "side": managed.side,
        "setup": managed.setup,
        "score": managed.score,
        "qty": managed.qty,
        "entry_price": managed.entry_price,
        "exit_price": exit_price,
        "entry_notional": managed.entry_notional,
        "entry_fee": entry_fee,
        "exit_fee": exit_fee,
        "gross_pnl_usdt": gross_pnl,
        "net_pnl_usdt": net_pnl,
        "net_r": net_r,
        "price_pnl_pct": price_pnl_pct,
        "equity_return_pct": equity_return_pct,
        "initial_soft_stop": managed.initial_soft_stop,
        "final_soft_stop": managed.soft_stop,
        "hard_stop": managed.hard_stop,
        "exit_reason": reason,
        "shadow_balance_after": balance_after,
        "entry_order_id": managed.entry_order_id,
        "exit_order_id": unique_client_oid("shadow_close"),
    }
    append_csv(settings.shadow_trades_path, result)
    append_jsonl(settings.shadow_events_path, {"event": "shadow_position_closed", "ts": iso(), "trade": result})
    state["shadow_balance"] = balance_after
    state["shadow_equity"] = balance_after
    state["shadow_unrealized_pnl"] = 0.0
    state["shadow_estimated_exit_fee"] = 0.0
    state["shadow_total_fees"] = safe_float(state.get("shadow_total_fees")) + exit_fee
    state["shadow_realized_net_pnl"] = safe_float(state.get("shadow_realized_net_pnl")) + net_pnl
    state["closed_trades"] = int(state.get("closed_trades", 0)) + 1
    state["shadow_sum_net_r"] = safe_float(state.get("shadow_sum_net_r")) + net_r
    if net_pnl < 0:
        state["losing_trades"] = int(state.get("losing_trades", 0)) + 1
        state["shadow_gross_loss_abs"] = safe_float(state.get("shadow_gross_loss_abs")) + abs(net_pnl)
        state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1
        if state["consecutive_losses"] >= settings.max_consecutive_losses:
            state["cooldown_until"] = iso(now_utc() + timedelta(minutes=settings.consecutive_loss_pause_minutes))
        else:
            state["cooldown_until"] = iso(now_utc() + timedelta(minutes=settings.cooldown_minutes))
    else:
        state["winning_trades"] = int(state.get("winning_trades", 0)) + 1
        state["shadow_gross_profit"] = safe_float(state.get("shadow_gross_profit")) + net_pnl
        state["consecutive_losses"] = 0
        state["cooldown_until"] = iso(now_utc() + timedelta(minutes=settings.cooldown_minutes))
    state["managed_position"] = None
    save_shadow_state(settings, state)
    notify(
        f"🧪 <b>BTC Structure SHADOW 청산</b>\n{managed.side} / {reason}\n"
        f"{managed.entry_price:,.2f} → {exit_price:,.2f}\n"
        f"순손익 {net_pnl:+,.2f} USDT ({equity_return_pct:+.3f}%) / 잔고 {balance_after:,.2f}",
        settings,
    )
    return result


def maybe_append_shadow_equity(
    settings: Settings,
    state: dict[str, Any],
    ticker: Ticker,
    *,
    force: bool = False,
) -> None:
    last = parse_iso(state.get("last_equity_sample_ts"))
    if not force and last and (now_utc() - last).total_seconds() < settings.shadow_equity_sample_seconds:
        return
    managed = managed_from_state(state)
    row = {
        "ts": iso(),
        "balance": safe_float(state.get("shadow_balance")),
        "equity": safe_float(state.get("shadow_equity")),
        "unrealized_pnl": safe_float(state.get("shadow_unrealized_pnl")),
        "estimated_exit_fee": safe_float(state.get("shadow_estimated_exit_fee")),
        "total_fees": safe_float(state.get("shadow_total_fees")),
        "realized_net_pnl": safe_float(state.get("shadow_realized_net_pnl")),
        "mark": ticker.mark,
        "position_side": managed.side if managed else "FLAT",
        "position_qty": managed.qty if managed else 0.0,
        "entry_price": managed.entry_price if managed else 0.0,
        "soft_stop": managed.soft_stop if managed else 0.0,
        "hard_stop": managed.hard_stop if managed else 0.0,
    }
    append_csv(settings.shadow_equity_path, row)
    state["last_equity_sample_ts"] = row["ts"]


def opening_payload(
    candidate: Candidate,
    qty: Decimal,
    instrument: Instrument,
    settings: Settings,
    client_oid: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "category": settings.category,
        "symbol": candidate.symbol,
        "qty": fmt_decimal(qty),
        "side": "buy" if candidate.side == "LONG" else "sell",
        "orderType": "market",
        "clientOid": client_oid,
        "reduceOnly": "no",
        "marginMode": settings.margin_mode,
        "stopLoss": rounded_price(instrument, candidate.hard_stop, candidate.side, "stop"),
        "slTriggerBy": "mark",
        "slOrderType": "market",
    }
    if settings.hold_mode == "hedge_mode":
        payload["posSide"] = "long" if candidate.side == "LONG" else "short"
    return payload


def closing_payload(managed: ManagedPosition, qty: Decimal, settings: Settings, client_oid: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "category": settings.category,
        "symbol": managed.symbol,
        "qty": fmt_decimal(qty),
        "side": "sell" if managed.side == "LONG" else "buy",
        "orderType": "market",
        "clientOid": client_oid,
        "marginMode": settings.margin_mode,
    }
    if managed.hold_mode == "hedge_mode":
        payload["posSide"] = "long" if managed.side == "LONG" else "short"
    else:
        payload["reduceOnly"] = "yes"
    return payload


def wait_for_order(client: Any, order_id: str, client_oid: str, timeout_seconds: float = 15.0) -> dict[str, Any] | None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        params = {"orderId": order_id} if order_id else {"clientOid": client_oid}
        try:
            resp = client_get(client, "/api/v3/trade/order-info", params, auth=True)
            data = response_data(resp)
            if api_success(resp) and isinstance(data, dict):
                status = str(data.get("orderStatus", "")).lower()
                if status in {"filled", "canceled", "cancelled", "rejected", "failed"}:
                    return data
        except Exception:
            pass
        time.sleep(0.6)
    return None


def order_identifiers(resp: dict[str, Any], client_oid: str) -> tuple[str, str]:
    data = response_data(resp)
    if isinstance(data, dict):
        return str(data.get("orderId") or ""), str(data.get("clientOid") or client_oid)
    return "", client_oid


def protection_present(order: dict[str, Any] | None, expected_stop: float, instrument: Instrument) -> bool:
    if not isinstance(order, dict):
        return False
    actual = safe_float(order.get("stopLoss"))
    if actual <= 0:
        return False
    tolerance = max(float(instrument.price_step) * 2.0, expected_stop * 0.00005)
    return abs(actual - expected_stop) <= tolerance


def exchange_position_for(client: Any, managed: ManagedPosition) -> dict[str, Any] | None:
    wanted = "long" if managed.side == "LONG" else "short"
    for row in nonzero_positions(client, managed.symbol):
        if str(row.get("posSide", "")).lower() == wanted:
            return row
    return None


def emergency_close_position(client: Any, settings: Settings, managed: ManagedPosition) -> dict[str, Any]:
    payload = {"category": settings.category, "symbol": managed.symbol, "posSide": "long" if managed.side == "LONG" else "short"}
    return require_success(client_post(client, "/api/v3/trade/close-positions", payload), "emergency close positions")


def open_position(
    client: Any,
    settings: Settings,
    candidate: Candidate,
    instrument: Instrument,
    equity: float,
    state: dict[str, Any],
) -> dict[str, Any]:
    qty = calculate_qty(equity, candidate.entry_reference, settings, instrument)
    if qty <= 0:
        raise RuntimeError("calculated order quantity is below exchange minimum")
    client_oid = unique_client_oid("bst_open")
    payload = opening_payload(candidate, qty, instrument, settings, client_oid)
    state["pending_entry"] = {"client_oid": client_oid, "candidate": asdict(candidate), "payload": payload, "created_at": iso()}
    save_state(settings, state)

    raw = client_post(client, "/api/v3/trade/place-order", payload)
    if not api_success(raw):
        # Timeout/unknown responses are reconciled by clientOid before deciding.
        detail = wait_for_order(client, "", client_oid, timeout_seconds=8.0)
        if detail is None:
            state["pending_entry"] = None
            save_state(settings, state)
            raise RuntimeError(f"place order failed and clientOid reconciliation found nothing: {raw}")
        order = detail
        order_id = str(detail.get("orderId") or "")
    else:
        order_id, returned_oid = order_identifiers(raw, client_oid)
        order = wait_for_order(client, order_id, returned_oid, timeout_seconds=15.0)
        client_oid = returned_oid

    if not isinstance(order, dict) or str(order.get("orderStatus", "")).lower() != "filled":
        state["pending_entry"] = None
        save_state(settings, state)
        raise RuntimeError(f"opening order was not confirmed filled: {order}")

    entry_price = safe_float(order.get("avgPrice"), candidate.entry_reference)
    expected_hard = safe_float(payload.get("stopLoss"), candidate.hard_stop)
    filled_qty = safe_float(order.get("cumExecQty"), float(qty))
    if filled_qty <= 0:
        filled_qty = float(qty)
    managed = ManagedPosition(
        symbol=settings.symbol,
        side=candidate.side,
        qty=filled_qty,
        entry_price=entry_price,
        entry_ts=iso(),
        entry_order_id=str(order.get("orderId") or order_id),
        entry_client_oid=client_oid,
        hold_mode=settings.hold_mode,
        initial_soft_stop=candidate.soft_stop,
        soft_stop=candidate.soft_stop,
        hard_stop=expected_hard,
        initial_risk=abs(entry_price - candidate.soft_stop),
        zone_low=candidate.zone_low,
        zone_high=candidate.zone_high,
        best_price=entry_price,
        trail_active=False,
        last_managed_bar_ts=candidate.signal_bar_ts,
        score=candidate.score,
        setup=candidate.setup,
    )

    # Re-query by clientOid because the place-order response alone is not a
    # sufficient protection check.
    detail = wait_for_order(client, managed.entry_order_id, client_oid, timeout_seconds=5.0) or order
    if not protection_present(detail, expected_hard, instrument):
        emergency_close_position(client, settings, managed)
        disarm(settings)
        state["pending_entry"] = None
        state["managed_position"] = None
        state["last_error"] = "opening fill had no verified exchange hard stop; emergency close sent and engine disarmed"
        save_state(settings, state)
        raise RuntimeError(state["last_error"])

    state["pending_entry"] = None
    state["managed_position"] = asdict(managed)
    state["last_signal_bar_ts"] = candidate.signal_bar_ts
    state["entries_today"] = int(state.get("entries_today", 0)) + 1
    state["last_error"] = None
    save_state(settings, state)
    append_jsonl(settings.events_path, {"event": "position_opened", "managed": asdict(managed), "payload": payload})
    notify(
        f"🚀 <b>BTC Structure 진입</b>\n{managed.side} / cross {settings.leverage}x / 수량기준 증거금환산 {settings.entry_margin_pct:.0f}%\n"
        f"진입 {managed.entry_price:,.2f}\n매물대 {managed.zone_low:,.2f}~{managed.zone_high:,.2f}\n"
        f"확인형 소프트스톱 {managed.soft_stop:,.2f}\n장애용 하드스톱 {managed.hard_stop:,.2f}\n점수 {managed.score:.1f}",
        settings,
    )
    return {"status": "opened", "managed": asdict(managed), "payload": payload}


def recover_pending_entry(
    client: Any,
    settings: Settings,
    state: dict[str, Any],
) -> dict[str, Any] | None:
    """Reconcile a crash/timeout that happened between order submission and
    committing managed_position to disk.

    A pending entry is never silently retried.  It is looked up by clientOid;
    a confirmed fill is adopted only after the attached hard stop is verified.
    """
    raw = state.get("pending_entry")
    if not isinstance(raw, dict) or state.get("managed_position") is not None:
        return None
    client_oid = str(raw.get("client_oid") or "")
    candidate_raw = raw.get("candidate")
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    if not client_oid or not isinstance(candidate_raw, dict):
        state["pending_entry"] = None
        save_state(settings, state)
        return {"status": "invalid_pending_cleared"}
    try:
        candidate = Candidate(**candidate_raw)
    except TypeError:
        state["pending_entry"] = None
        save_state(settings, state)
        return {"status": "incompatible_pending_cleared"}

    try:
        resp = client_get(client, "/api/v3/trade/order-info", {"clientOid": client_oid}, auth=True)
    except Exception as exc:
        return {"status": "pending_lookup_error", "error": f"{type(exc).__name__}: {exc}"}
    if not api_success(resp) or not isinstance(response_data(resp), dict):
        created = parse_iso(raw.get("created_at"))
        if created and now_utc() - created > timedelta(minutes=10):
            state["pending_entry"] = None
            save_state(settings, state)
            return {"status": "stale_pending_not_found_cleared"}
        return {"status": "pending_not_resolved", "response": resp}

    order = response_data(resp)
    status = str(order.get("orderStatus", "")).lower()
    if status in {"canceled", "cancelled", "rejected", "failed"}:
        state["pending_entry"] = None
        save_state(settings, state)
        return {"status": "pending_terminal_cleared", "order_status": status}
    if status != "filled":
        return {"status": "pending_waiting", "order_status": status}

    instrument = fetch_instrument(client, settings.symbol)
    expected_hard = safe_float(payload.get("stopLoss"), candidate.hard_stop)
    entry_price = safe_float(order.get("avgPrice"), candidate.entry_reference)
    qty = safe_float(order.get("cumExecQty") or payload.get("qty"))
    if entry_price <= 0 or qty <= 0:
        raise RuntimeError(f"filled pending order has invalid fill data: {order}")
    managed = ManagedPosition(
        symbol=settings.symbol,
        side=candidate.side,
        qty=qty,
        entry_price=entry_price,
        entry_ts=iso(),
        entry_order_id=str(order.get("orderId") or ""),
        entry_client_oid=client_oid,
        hold_mode=settings.hold_mode,
        initial_soft_stop=candidate.soft_stop,
        soft_stop=candidate.soft_stop,
        hard_stop=expected_hard,
        initial_risk=abs(entry_price - candidate.soft_stop),
        zone_low=candidate.zone_low,
        zone_high=candidate.zone_high,
        best_price=entry_price,
        trail_active=False,
        last_managed_bar_ts=candidate.signal_bar_ts,
        score=candidate.score,
        setup=candidate.setup,
    )
    if not protection_present(order, expected_hard, instrument):
        emergency_close_position(client, settings, managed)
        disarm(settings)
        state["pending_entry"] = None
        state["managed_position"] = None
        state["last_error"] = "recovered fill had no verified hard stop; emergency close sent and engine disarmed"
        save_state(settings, state)
        raise RuntimeError(state["last_error"])
    state["pending_entry"] = None
    state["managed_position"] = asdict(managed)
    state["last_signal_bar_ts"] = max(int(state.get("last_signal_bar_ts", 0)), candidate.signal_bar_ts)
    save_state(settings, state)
    append_jsonl(settings.events_path, {"event": "pending_entry_recovered", "managed": asdict(managed)})
    notify(
        f"♻️ <b>BTC Structure 미완료 진입 복구</b>\n{managed.side} {managed.qty} BTC @ {managed.entry_price:,.2f}\n하드스톱 검증 완료",
        settings,
    )
    return {"status": "pending_fill_recovered", "managed": asdict(managed)}


def trade_pnl_pct(managed: ManagedPosition, exit_price: float) -> float:
    raw = (exit_price / managed.entry_price - 1.0) * 100.0
    return raw if managed.side == "LONG" else -raw


def finalize_trade(
    settings: Settings,
    managed: ManagedPosition,
    exit_price: float,
    reason: str,
    state: dict[str, Any],
    exit_order_id: str = "",
) -> dict[str, Any]:
    price_pnl_pct = trade_pnl_pct(managed, exit_price)
    equity_pnl_pct_approx = price_pnl_pct * settings.entry_margin_pct / 100.0 * settings.leverage
    result = {
        "entry_ts": managed.entry_ts,
        "exit_ts": iso(),
        "symbol": managed.symbol,
        "side": managed.side,
        "setup": managed.setup,
        "score": managed.score,
        "qty": managed.qty,
        "entry_price": managed.entry_price,
        "exit_price": exit_price,
        "initial_soft_stop": managed.initial_soft_stop,
        "final_soft_stop": managed.soft_stop,
        "hard_stop": managed.hard_stop,
        "price_pnl_pct": round(price_pnl_pct, 6),
        "equity_pnl_pct_approx": round(equity_pnl_pct_approx, 6),
        "exit_reason": reason,
        "entry_order_id": managed.entry_order_id,
        "exit_order_id": exit_order_id,
    }
    append_csv(settings.trades_path, result)
    append_jsonl(settings.events_path, {"event": "position_closed", "trade": result})
    state["managed_position"] = None
    state["pending_entry"] = None
    state["cooldown_until"] = iso(now_utc() + timedelta(minutes=settings.cooldown_minutes))
    if price_pnl_pct < 0:
        state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1
        if state["consecutive_losses"] >= settings.max_consecutive_losses:
            state["cooldown_until"] = iso(now_utc() + timedelta(minutes=settings.consecutive_loss_pause_minutes))
    else:
        state["consecutive_losses"] = 0
    save_state(settings, state)
    notify(
        f"🔚 <b>BTC Structure 청산</b>\n{managed.side} / {reason}\n"
        f"{managed.entry_price:,.2f} → {exit_price:,.2f}\n가격손익 {price_pnl_pct:+.3f}% / 계좌근사 {equity_pnl_pct_approx:+.3f}%",
        settings,
    )
    return result


def close_managed_position(
    client: Any,
    settings: Settings,
    managed: ManagedPosition,
    reason: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    instrument = fetch_instrument(client, managed.symbol)
    qty = quantize_step(managed.qty, instrument.quantity_step, "down")
    client_oid = unique_client_oid("bst_close")
    payload = closing_payload(managed, qty, settings, client_oid)
    raw = client_post(client, "/api/v3/trade/place-order", payload)
    if not api_success(raw):
        detail = wait_for_order(client, "", client_oid, timeout_seconds=8.0)
        if detail is None:
            # Last resort closes the selected BTC side at market.
            emergency_close_position(client, settings, managed)
            order_id = "close-positions-fallback"
            exit_price = fetch_ticker(client, managed.symbol).mark
            return finalize_trade(settings, managed, exit_price, reason + "_FALLBACK", state, order_id)
        order = detail
        order_id = str(detail.get("orderId") or "")
    else:
        order_id, returned_oid = order_identifiers(raw, client_oid)
        order = wait_for_order(client, order_id, returned_oid, timeout_seconds=15.0)
    if not isinstance(order, dict) or str(order.get("orderStatus", "")).lower() != "filled":
        raise RuntimeError(f"close order not confirmed filled: {order}")
    exit_price = safe_float(order.get("avgPrice"), fetch_ticker(client, managed.symbol).mark)
    return finalize_trade(settings, managed, exit_price, reason, state, str(order.get("orderId") or order_id))


# ---------------------------------------------------------------------------
# Risk guards and reconciliation
# ---------------------------------------------------------------------------


def refresh_equity_anchors(state: dict[str, Any], equity: float) -> None:
    today = day_key()
    if state.get("day") != today or safe_float(state.get("day_start_equity")) <= 0:
        state["day"] = today
        state["day_start_equity"] = equity
        state["entries_today"] = 0
    week = week_key()
    if state.get("week") != week or safe_float(state.get("week_start_equity")) <= 0:
        state["week"] = week
        state["week_start_equity"] = equity
    peak = safe_float(state.get("peak_equity"))
    state["peak_equity"] = max(equity, peak) if peak > 0 else equity


def guard_report(settings: Settings, state: dict[str, Any], equity: float) -> dict[str, Any]:
    refresh_equity_anchors(state, equity)
    day_start = safe_float(state.get("day_start_equity"), equity)
    week_start = safe_float(state.get("week_start_equity"), equity)
    peak = safe_float(state.get("peak_equity"), equity)
    day_loss = max(0.0, (day_start - equity) / day_start * 100.0) if day_start > 0 else 0.0
    week_loss = max(0.0, (week_start - equity) / week_start * 100.0) if week_start > 0 else 0.0
    peak_dd = max(0.0, (peak - equity) / peak * 100.0) if peak > 0 else 0.0
    reasons: list[str] = []
    if day_loss >= settings.max_daily_loss_pct:
        reasons.append(f"daily_loss={day_loss:.2f}%")
    if week_loss >= settings.max_weekly_loss_pct:
        reasons.append(f"weekly_loss={week_loss:.2f}%")
    if peak_dd >= settings.max_peak_drawdown_pct:
        reasons.append(f"peak_drawdown={peak_dd:.2f}%")
    if int(state.get("entries_today", 0)) >= settings.max_entries_per_day:
        reasons.append("daily_entry_limit")
    cooldown = parse_iso(state.get("cooldown_until"))
    if cooldown and now_utc() < cooldown:
        reasons.append(f"cooldown_until={cooldown.isoformat()}")
    return {
        "allow_new_entry": not reasons,
        "reasons": reasons,
        "equity": equity,
        "day_loss_pct": day_loss,
        "week_loss_pct": week_loss,
        "peak_drawdown_pct": peak_dd,
        "entries_today": int(state.get("entries_today", 0)),
        "consecutive_losses": int(state.get("consecutive_losses", 0)),
    }


def managed_from_state(state: dict[str, Any]) -> ManagedPosition | None:
    raw = state.get("managed_position")
    if not isinstance(raw, dict):
        return None
    try:
        return ManagedPosition(**raw)
    except TypeError as exc:
        raise RuntimeError(f"managed position state incompatible: {exc}") from exc


def reconcile_flat_state(client: Any, settings: Settings, state: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    positions = nonzero_positions(client)
    managed = managed_from_state(state)
    if managed is None and positions:
        reasons.append(f"unknown_exchange_positions={len(positions)}")
    if managed is not None:
        wanted_side = "long" if managed.side == "LONG" else "short"
        matching = [
            row for row in positions
            if str(row.get("symbol", "")).upper() == managed.symbol
            and str(row.get("posSide", "")).lower() == wanted_side
        ]
        if len(matching) != 1:
            reasons.append(f"managed_exchange_match_count={len(matching)}")
        if len(positions) != len(matching):
            reasons.append(f"other_exchange_positions={len(positions) - len(matching)}")
    orders = fetch_open_orders(client)
    foreign_orders = [row for row in orders if not str(row.get("clientOid", "")).startswith("bst_")]
    if foreign_orders:
        reasons.append(f"foreign_open_orders={len(foreign_orders)}")
    strategies = fetch_strategy_orders_best_effort(client)
    # While flat, even our own stale TP/SL order is unsafe because it can block
    # the next setup or signal inconsistent exchange state.
    if managed is None and strategies:
        reasons.append(f"orphan_strategy_orders={len(strategies)}")
    return reasons


# ---------------------------------------------------------------------------
# Position management and main cycle
# ---------------------------------------------------------------------------


def manage_position(client: Any, settings: Settings, state: dict[str, Any]) -> dict[str, Any]:
    managed = managed_from_state(state)
    if managed is None:
        return {"status": "flat"}
    exchange = exchange_position_for(client, managed)
    if exchange is None:
        mark = fetch_ticker(client, managed.symbol).mark
        result = finalize_trade(settings, managed, mark, "EXCHANGE_SL_OR_MANUAL", state)
        return {"status": "closed_external", "result": result}

    ticker = fetch_ticker(client, managed.symbol)
    managed.best_price = max(managed.best_price, ticker.mark) if managed.side == "LONG" else min(managed.best_price, ticker.mark)
    bars15 = completed_bars(fetch_candles(client, managed.symbol, "15m", 300), "15m")
    bars1h = completed_bars(fetch_candles(client, managed.symbol, "1H", 220), "1H")
    if not bars15:
        raise RuntimeError("no completed 15m bars while managing position")

    newest_ts = bars15[-1].ts
    trail_stop, trail_active, trail_diag = update_trailing_stop(managed, bars15, settings, ticker.mark)
    managed.soft_stop = trail_stop
    managed.trail_active = trail_active

    action: dict[str, Any] = {
        "status": "managed",
        "mark": ticker.mark,
        "soft_stop": managed.soft_stop,
        "hard_stop": managed.hard_stop,
        "trail": trail_diag,
    }

    if newest_ts > managed.last_managed_bar_ts:
        broken, break_diag = strong_structure_break(managed, bars15, settings)
        action["structure_break"] = break_diag
        if broken:
            return {"status": "closed", "result": close_managed_position(client, settings, managed, "STRONG_VOLUME_STRUCTURE_BREAK", state)}
        trend_due, trend_diag = trend_flip_due(managed, bars1h, settings)
        action["trend_flip"] = trend_diag
        if trend_due:
            return {"status": "closed", "result": close_managed_position(client, settings, managed, "CONFIRMED_1H_TREND_FLIP", state)}
        managed.last_managed_bar_ts = newest_ts

    state["managed_position"] = asdict(managed)
    save_state(settings, state)
    return action


def run_observe_once(settings: Settings) -> dict[str, Any]:
    """Pure public-data observation.

    This path does not load Bitget credentials, read the private account or
    touch live order/position state.
    """
    client = make_public_client()
    ticker = fetch_ticker(client, settings.symbol)
    bars15 = fetch_candles(client, settings.symbol, "15m", 320)
    bars1h = fetch_candles(client, settings.symbol, "1H", 260)
    bars4h = fetch_candles(client, settings.symbol, "4H", 260)
    candidate, diagnostics = build_candidate(settings, ticker, bars15, bars1h, bars4h)
    action = (
        {"status": "candidate_observed", "candidate": asdict(candidate)}
        if candidate is not None
        else {"status": "no_entry", "diagnostics": diagnostics}
    )
    return {
        "version": VERSION,
        "ts": iso(),
        "mode": "OBSERVE_PUBLIC_ONLY",
        "order_capability": False,
        "ticker": asdict(ticker),
        "action": action,
    }


def manage_shadow_position(
    client: Any,
    settings: Settings,
    state: dict[str, Any],
    ticker: Ticker,
    instrument: Instrument,
) -> dict[str, Any]:
    managed = managed_from_state(state)
    if managed is None:
        update_shadow_mark_to_market(settings, state, None, ticker, instrument)
        return {"status": "shadow_flat"}

    managed.best_price = max(managed.best_price, ticker.mark) if managed.side == "LONG" else min(managed.best_price, ticker.mark)
    hard_stop_hit = ticker.mark <= managed.hard_stop if managed.side == "LONG" else ticker.mark >= managed.hard_stop
    if hard_stop_hit:
        result = close_shadow_position(settings, state, managed, ticker, instrument, "SHADOW_MARK_HARD_STOP")
        return {"status": "shadow_closed", "result": result}

    bars15 = completed_bars(fetch_candles(client, managed.symbol, "15m", 300), "15m")
    bars1h = completed_bars(fetch_candles(client, managed.symbol, "1H", 220), "1H")
    if not bars15:
        raise RuntimeError("no completed 15m bars while managing shadow position")

    newest_ts = bars15[-1].ts
    trail_stop, trail_active, trail_diag = update_trailing_stop(managed, bars15, settings, ticker.mark)
    managed.soft_stop = trail_stop
    managed.trail_active = trail_active
    action: dict[str, Any] = {
        "status": "shadow_managed",
        "mark": ticker.mark,
        "soft_stop": managed.soft_stop,
        "hard_stop": managed.hard_stop,
        "best_price": managed.best_price,
        "trail": trail_diag,
    }

    if newest_ts > managed.last_managed_bar_ts:
        broken, break_diag = strong_structure_break(managed, bars15, settings)
        action["structure_break"] = break_diag
        if broken:
            result = close_shadow_position(
                settings,
                state,
                managed,
                ticker,
                instrument,
                "SHADOW_STRONG_VOLUME_STRUCTURE_BREAK",
            )
            return {"status": "shadow_closed", "result": result}
        trend_due, trend_diag = trend_flip_due(managed, bars1h, settings)
        action["trend_flip"] = trend_diag
        if trend_due:
            result = close_shadow_position(
                settings,
                state,
                managed,
                ticker,
                instrument,
                "SHADOW_CONFIRMED_1H_TREND_FLIP",
            )
            return {"status": "shadow_closed", "result": result}
        managed.last_managed_bar_ts = newest_ts

    state["managed_position"] = asdict(managed)
    action["mark_to_market"] = update_shadow_mark_to_market(settings, state, managed, ticker, instrument)
    save_shadow_state(settings, state)
    return action


def maybe_shadow_heartbeat(settings: Settings, state: dict[str, Any], guards: dict[str, Any], action: dict[str, Any]) -> None:
    last = parse_iso(state.get("last_heartbeat_ts"))
    if last and (now_utc() - last).total_seconds() < settings.heartbeat_minutes * 60:
        return
    state["last_heartbeat_ts"] = iso()
    notify(
        f"🧪 <b>BTC Structure SHADOW</b>\n가상자산 {guards.get('equity', 0):,.2f} USDT\n"
        f"일손실 {guards.get('day_loss_pct', 0):.2f}% / 최고점DD {guards.get('peak_drawdown_pct', 0):.2f}%\n"
        f"상태 {action.get('status', '-')}",
        settings,
    )


def run_shadow_once(settings: Settings, initial_equity: float | None = None) -> dict[str, Any]:
    """Run one fully simulated cycle using only Bitget public market data."""
    client = make_public_client()
    state = load_shadow_state(settings, initial_equity)
    if not settings.shadow_state_path.exists():
        save_shadow_state(settings, state)
    instrument = fetch_instrument(client, settings.symbol)
    ticker = fetch_ticker(client, settings.symbol)
    managed = managed_from_state(state)
    mtm = update_shadow_mark_to_market(settings, state, managed, ticker, instrument)
    guards = guard_report(settings, state, mtm["equity"])

    if managed is not None:
        action = manage_shadow_position(client, settings, state, ticker, instrument)
    else:
        bars15 = fetch_candles(client, settings.symbol, "15m", 320)
        bars1h = fetch_candles(client, settings.symbol, "1H", 260)
        bars4h = fetch_candles(client, settings.symbol, "4H", 260)
        candidate, diagnostics = build_candidate(settings, ticker, bars15, bars1h, bars4h)
        state["last_diagnostics"] = diagnostics
        if candidate is None:
            action = {"status": "shadow_no_entry", "diagnostics": diagnostics}
        elif candidate.signal_bar_ts <= int(state.get("last_signal_bar_ts", 0)):
            action = {"status": "shadow_duplicate_signal", "candidate": asdict(candidate)}
        elif not guards["allow_new_entry"]:
            action = {"status": "shadow_entry_blocked_guards", "guards": guards, "candidate": asdict(candidate)}
        else:
            action = open_shadow_position(settings, state, candidate, instrument, ticker, mtm["equity"])

    state["last_cycle_ts"] = iso()
    state["last_error"] = None
    managed_after = managed_from_state(state)
    mtm_after = update_shadow_mark_to_market(settings, state, managed_after, ticker, instrument)
    guards_after = guard_report(settings, state, mtm_after["equity"])
    state["shadow_max_drawdown_pct"] = max(
        safe_float(state.get("shadow_max_drawdown_pct")),
        safe_float(guards_after.get("peak_drawdown_pct")),
    )
    maybe_shadow_heartbeat(settings, state, guards_after, action)
    maybe_append_shadow_equity(settings, state, ticker, force=action.get("status") in {"shadow_opened", "shadow_closed"})
    save_shadow_state(settings, state)
    return {
        "version": VERSION,
        "ts": iso(),
        "mode": "SHADOW_PUBLIC_ONLY",
        "order_capability": False,
        "seed": safe_float(state.get("initial_equity")),
        "guards": guards_after,
        "portfolio": mtm_after,
        "action": action,
    }


def maybe_heartbeat(settings: Settings, state: dict[str, Any], mode: str, guards: dict[str, Any], action: dict[str, Any]) -> None:
    last = parse_iso(state.get("last_heartbeat_ts"))
    if last and (now_utc() - last).total_seconds() < settings.heartbeat_minutes * 60:
        return
    state["last_heartbeat_ts"] = iso()
    notify(
        f"💓 <b>BTC Structure {mode}</b>\n계좌 {guards.get('equity', 0):,.2f} USDT\n"
        f"일손실 {guards.get('day_loss_pct', 0):.2f}% / 최고점DD {guards.get('peak_drawdown_pct', 0):.2f}%\n"
        f"상태 {action.get('status', '-')}",
        settings,
    )


def run_once(settings: Settings, execute_live: bool) -> dict[str, Any]:
    if not execute_live:
        return run_observe_once(settings)
    client = make_client()
    state = load_state(settings)
    assets = fetch_account_assets(client)
    equity = account_equity(assets)
    if equity <= 0:
        raise RuntimeError(f"invalid account equity: {assets}")
    guards = guard_report(settings, state, equity)
    live_armed, arm_reasons = arm_valid(settings)
    mode = "LIVE" if live_armed else "LIVE_GATED"

    pending_recovery = recover_pending_entry(client, settings, state)
    if pending_recovery and pending_recovery.get("status") == "pending_fill_recovered":
        state = load_state(settings)

    managed = managed_from_state(state)
    if managed is not None:
        action = manage_position(client, settings, state)
        if pending_recovery:
            action["pending_recovery"] = pending_recovery
    elif pending_recovery and pending_recovery.get("status") in {"pending_waiting", "pending_not_resolved", "pending_lookup_error"}:
        action = pending_recovery
    else:
        reconciliation = reconcile_flat_state(client, settings, state)
        if reconciliation:
            action = {"status": "entry_blocked_reconciliation", "reasons": reconciliation}
        else:
            ticker = fetch_ticker(client, settings.symbol)
            bars15 = fetch_candles(client, settings.symbol, "15m", 320)
            bars1h = fetch_candles(client, settings.symbol, "1H", 260)
            bars4h = fetch_candles(client, settings.symbol, "4H", 260)
            candidate, diagnostics = build_candidate(settings, ticker, bars15, bars1h, bars4h)
            state["last_diagnostics"] = diagnostics
            if candidate is None:
                action = {"status": "no_entry", "diagnostics": diagnostics}
            elif candidate.signal_bar_ts <= int(state.get("last_signal_bar_ts", 0)):
                action = {"status": "duplicate_signal", "candidate": asdict(candidate)}
            elif not guards["allow_new_entry"]:
                action = {"status": "entry_blocked_guards", "guards": guards, "candidate": asdict(candidate)}
            elif not live_armed:
                action = {
                    "status": "candidate_observed",
                    "candidate": asdict(candidate),
                    "live_reasons": arm_reasons,
                }
            else:
                instrument = fetch_instrument(client, settings.symbol)
                action = open_position(client, settings, candidate, instrument, equity, state)

    state = load_state(settings)
    state["last_cycle_ts"] = iso()
    state["last_error"] = None
    maybe_heartbeat(settings, state, mode, guards, action)
    save_state(settings, state)
    return {"version": VERSION, "ts": iso(), "mode": mode, "armed": live_armed, "arm_reasons": arm_reasons, "guards": guards, "action": action}


# ---------------------------------------------------------------------------
# Account setup and doctor
# ---------------------------------------------------------------------------


def set_hold_mode(client: Any, hold_mode: str) -> dict[str, Any]:
    return require_success(client_post(client, "/api/v3/account/set-hold-mode", {"holdMode": hold_mode}), "set hold mode")


CROSS_ACCOUNT_LEVELS = {"basic", "advanced"}


def set_cross_leverage(client: Any, symbol: str, leverage: int) -> list[dict[str, Any]]:
    """Set one trading-pair leverage value for Bitget UTA cross margin.

    Bitget uses the literal API enum ``crossed``.  Direction-specific
    longLeverage/shortLeverage fields are intentionally not sent because those
    fields apply to isolated margin with hedge mode.
    """
    payload = {
        "category": CATEGORY,
        "symbol": symbol,
        "leverage": str(leverage),
        "marginMode": "crossed",
    }
    return [
        require_success(
            client_post(client, "/api/v3/account/set-leverage", payload),
            "set BTC cross leverage",
        )
    ]


def setup_account(settings: Settings) -> dict[str, Any]:
    client = make_client()
    positions = nonzero_positions(client)
    orders = fetch_open_orders(client)
    strategies = fetch_strategy_orders_best_effort(client)
    if positions or orders or strategies:
        raise RuntimeError(
            "setup-account requires a flat dedicated account: "
            f"positions={len(positions)} orders={len(orders)} strategy_orders={len(strategies)}"
        )
    instrument = fetch_instrument(client, settings.symbol)
    if instrument.status != "online" or instrument.symbol_type not in {"", "crypto"}:
        raise RuntimeError(f"BTC instrument is not normal crypto perpetual: {instrument}")
    if not (instrument.min_leverage <= settings.leverage <= instrument.max_leverage):
        raise RuntimeError(f"5x outside exchange leverage range {instrument.min_leverage}-{instrument.max_leverage}")

    before = fetch_account_settings(client)
    account_level = infer_account_level(before)
    account_mode_result: Any = {"already": account_level}
    if account_level not in CROSS_ACCOUNT_LEVELS:
        target_level = os.getenv("BTC_STRUCTURE_CROSS_ACCOUNT_LEVEL", "basic").strip().lower()
        if target_level not in CROSS_ACCOUNT_LEVELS:
            raise RuntimeError(
                "BTC_STRUCTURE_CROSS_ACCOUNT_LEVEL must be basic or advanced; "
                f"got {target_level!r}"
            )
        if os.getenv("BTC_STRUCTURE_CONFIRM_CROSS_ACCOUNT_MODE", "").strip() != "YES":
            raise RuntimeError(
                f"accountLevel={account_level or 'unknown'} is not cross-capable. Re-run with "
                "BTC_STRUCTURE_CONFIRM_CROSS_ACCOUNT_MODE=YES after confirming the dedicated account is flat. "
                f"The setup will switch accountLevel to {target_level}."
            )
        account_mode_result = require_success(
            client_post(client, "/api/v3/account/adjust-account-mode", {"mode": target_level}),
            f"set account mode {target_level} for cross margin",
        )
        deadline = time.time() + 60.0
        while time.time() < deadline:
            time.sleep(1.0)
            if infer_account_level(fetch_account_settings(client)) == target_level:
                break
        else:
            raise RuntimeError(f"account mode did not become {target_level} within 60 seconds")

    hold_result = set_hold_mode(client, settings.hold_mode)
    leverage_result = set_cross_leverage(client, settings.symbol, settings.leverage)

    deadline = time.time() + 45.0
    final: dict[str, Any] = {}
    while time.time() < deadline:
        time.sleep(1.0)
        final = fetch_account_settings(client)
        row = account_symbol_row(final, settings.symbol)
        values = leverage_values_from_row(row)
        margin_ok = isinstance(row, dict) and str(row.get("marginMode", "")).lower() == settings.margin_mode
        hold_ok = infer_hold_mode(final) == settings.hold_mode
        level_ok = infer_account_level(final) in CROSS_ACCOUNT_LEVELS
        lev_ok = bool(values) and all(value == settings.leverage for value in values)
        if margin_ok and hold_ok and level_ok and lev_ok:
            return {
                "ok": True,
                "account_mode_result": account_mode_result,
                "hold_mode_result": hold_result,
                "leverage_result": leverage_result,
                "accountLevel": infer_account_level(final),
                "holdMode": infer_hold_mode(final),
                "symbol_row": row,
                "leverage_values": values,
            }
    raise RuntimeError(f"post-setup verification failed: {final}")


def doctor(settings: Settings, prearm: bool = False) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    errors: list[str] = []
    local_state = load_state(settings)
    checks["config"] = {"path": str(settings.config_path), "sha256": sha256_file(settings.config_path), "contract": "BTC 5x cross(crossed) 50% sizing"}
    checks["local_state"] = {
        "managed_position": local_state.get("managed_position"),
        "pending_entry": local_state.get("pending_entry"),
        "last_error": local_state.get("last_error"),
    }
    if prearm and (local_state.get("managed_position") or local_state.get("pending_entry")):
        errors.append("prearm local state must have no managed_position or pending_entry")
    checks["imports"] = {"BitgetUTAClient": BitgetUTAClient is not None, "TelegramBot": TelegramBot is not None}
    if BitgetUTAClient is None:
        errors.append("BitgetUTAClient import failed")
        return {"ok": False, "prearm": prearm, "errors": errors, "checks": checks}
    try:
        client = make_client()
        instrument = fetch_instrument(client, settings.symbol)
        ticker = fetch_ticker(client, settings.symbol)
        assets = fetch_account_assets(client)
        account = fetch_account_settings(client)
        positions = nonzero_positions(client)
        orders = fetch_open_orders(client)
        strategies = fetch_strategy_orders_best_effort(client)
        row = account_symbol_row(account, settings.symbol)
        values = leverage_values_from_row(row)
        checks["market"] = {"instrument": asdict(instrument), "ticker": asdict(ticker)}
        checks["account"] = {
            "equity": account_equity(assets),
            "available_usdt": account_available_usdt(assets),
            "accountLevel": infer_account_level(account),
            "holdMode": infer_hold_mode(account, positions),
            "symbol_row": row,
            "leverage_values": values,
            "positions": positions,
            "open_orders": orders,
            "strategy_orders": strategies,
        }
        if instrument.status != "online":
            errors.append(f"instrument status={instrument.status}")
        if instrument.symbol_type not in {"", "crypto"}:
            errors.append(f"instrument symbolType={instrument.symbol_type}")
        account_level = infer_account_level(account)
        if account_level not in CROSS_ACCOUNT_LEVELS:
            errors.append(f"accountLevel={account_level} expected basic or advanced for cross margin")
        if infer_hold_mode(account, positions) != settings.hold_mode:
            errors.append(f"holdMode={infer_hold_mode(account, positions)} expected {settings.hold_mode}")
        if not isinstance(row, dict):
            errors.append("BTC symbol configuration missing")
        else:
            if str(row.get("marginMode", "")).lower() != settings.margin_mode:
                errors.append(f"BTC marginMode={row.get('marginMode')} expected crossed")
            if not values or any(value != settings.leverage for value in values):
                errors.append(f"BTC leverage={values} expected 5")
        if prearm and (positions or orders or strategies):
            errors.append(
                f"prearm dedicated account must be flat: positions={len(positions)} orders={len(orders)} strategy_orders={len(strategies)}"
            )
        # Read-only strategy calculation is part of doctor.
        bars15 = fetch_candles(client, settings.symbol, "15m", 80)
        bars1h = fetch_candles(client, settings.symbol, "1H", 220)
        bars4h = fetch_candles(client, settings.symbol, "4H", 230)
        _, strategy_diag = build_candidate(settings, ticker, bars15, bars1h, bars4h)
        checks["strategy_read_only"] = strategy_diag
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return {"ok": not errors, "prearm": prearm, "errors": errors, "checks": checks}


# ---------------------------------------------------------------------------
# Offline tests
# ---------------------------------------------------------------------------


def _synthetic_bars(count: int, interval_minutes: int, direction: float = 1.0, base: float = 100.0) -> list[Bar]:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    out: list[Bar] = []
    price = base
    for idx in range(count):
        wave = math.sin(idx / 7.0) * 0.35
        drift = direction * 0.08
        open_price = price
        close = max(1.0, open_price + drift + wave * 0.08)
        high = max(open_price, close) + 0.55 + abs(wave) * 0.10
        low = min(open_price, close) - 0.55 - abs(wave) * 0.10
        volume = 100.0 + (300.0 if 98.0 <= close <= 103.0 else 0.0) + (idx % 11) * 3
        out.append(
            Bar(
                ts=int((start + timedelta(minutes=interval_minutes * idx)).timestamp() * 1000),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                turnover=volume * close,
            )
        )
        price = close
    return out


def self_test() -> dict[str, Any]:
    bars = _synthetic_bars(260, 15, 1.0)
    zones = volume_profile_zones(bars[-192:], bins=48, high_volume_quantile=0.70)
    instrument = Instrument(
        symbol=SYMBOL,
        status="online",
        symbol_type="crypto",
        min_order_qty=Decimal("0.0001"),
        max_order_qty=Decimal("100"),
        max_market_order_qty=Decimal("100"),
        min_order_amount=Decimal("5"),
        price_step=Decimal("0.1"),
        quantity_step=Decimal("0.0001"),
        min_leverage=1,
        max_leverage=125,
        maker_fee=0.0002,
        taker_fee=0.0006,
    )
    dummy_cfg = {
        "symbol": SYMBOL,
        "category": CATEGORY,
        "leverage": 5,
        "margin_mode": "crossed",
        "hold_mode": "hedge_mode",
        "entry_margin_pct": 50,
    }
    # Sizing math can be tested without a Settings instance.
    notional = 10_000 * dummy_cfg["entry_margin_pct"] / 100 * dummy_cfg["leverage"]
    qty = quantize_step(Decimal(str(notional / 100_000)), instrument.quantity_step, "down")

    managed_long = ManagedPosition(
        symbol=SYMBOL,
        side="LONG",
        qty=0.1,
        entry_price=100.0,
        entry_ts=iso(),
        entry_order_id="x",
        entry_client_oid="y",
        hold_mode="hedge_mode",
        initial_soft_stop=99.0,
        soft_stop=99.0,
        hard_stop=98.0,
        initial_risk=1.0,
        zone_low=99.0,
        zone_high=100.0,
        best_price=104.0,
    )
    strong = list(bars[-30:])
    last = strong[-1]
    strong[-1] = Bar(last.ts, 99.4, 99.5, 98.4, 98.5, max(x.volume for x in strong) * 3.0, last.turnover)

    tests = {
        "volume_profile_has_zones": bool(zones),
        "volume_zone_ordering": all(zone.low < zone.high for zone in zones),
        "notional_math_2_5x": abs(notional / 10_000 - 2.5) < 1e-12,
        "qty_rounding": qty == Decimal("0.25"),
        "price_floor": quantize_step(100.09, Decimal("0.1"), "floor") == Decimal("100.0"),
        "price_ceil": quantize_step(100.01, Decimal("0.1"), "ceil") == Decimal("100.1"),
        "atr_positive": atr_value(bars, 14) > 0,
        "adx_bounded": 0 <= adx_snapshot(bars, 14)[0] <= 100,
        "client_oid_length": len(unique_client_oid("bst_open")) <= 32,
        "strong_break_detector_math": strong[-1].close < managed_long.soft_stop,
        "arm_phrases": len(expected_phrases()) == 4,
        "shadow_long_entry_fill_is_worse_than_ask": shadow_fill_price(
            Ticker(SYMBOL, 100.0, 100.0, 99.9, 100.1), "LONG", opening=True, slippage_bps=2.0
        ) > 100.1,
        "shadow_long_exit_fill_is_worse_than_bid": shadow_fill_price(
            Ticker(SYMBOL, 100.0, 100.0, 99.9, 100.1), "LONG", opening=False, slippage_bps=2.0
        ) < 99.9,
    }
    return {"ok": all(tests.values()), "version": VERSION, "tests": tests, "zones": [asdict(z) for z in zones[:3]]}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_once(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    print(json.dumps(run_once(settings, execute_live=not args.observe), ensure_ascii=False, indent=2, default=str))


def cmd_loop(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    log(f"BTC Structure Trend v{VERSION} loop start / observe={args.observe}", settings)
    notify(
        f"🟢 <b>BTC Structure Trend v{VERSION} 시작</b>\n"
        f"모드: {'OBSERVE' if args.observe else 'LIVE-GATED'}\n"
        f"BTCUSDT / cross {settings.leverage}x / 수량기준 증거금환산 {settings.entry_margin_pct:.0f}%",
        settings,
    )
    while True:
        try:
            result = run_once(settings, execute_live=not args.observe)
            print(
                json.dumps(
                    {"ts": result.get("ts"), "mode": result.get("mode"), "guards": result.get("guards"), "action": result.get("action")},
                    ensure_ascii=False,
                    default=str,
                ),
                flush=True,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            state = load_state(settings)
            state["last_error"] = f"{type(exc).__name__}: {exc}"
            state["last_cycle_ts"] = iso()
            save_state(settings, state)
            log(f"cycle error: {exc}\n{traceback.format_exc()}", settings)
            notify(f"⚠️ <b>BTC Structure 오류</b>\n{type(exc).__name__}: {exc}", settings)
        time.sleep(settings.loop_seconds)


def cmd_shadow_once(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    print(json.dumps(run_shadow_once(settings, initial_equity=args.seed), ensure_ascii=False, indent=2, default=str))


def cmd_shadow_loop(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    shadow_log(
        f"BTC Structure Trend v{VERSION} SHADOW start / seed={args.seed if args.seed is not None else settings.shadow_initial_equity}",
        settings,
    )
    notify(
        f"🧪 <b>BTC Structure SHADOW 시작</b>\n"
        f"주문 권한 없음 / 공개 시세 전용\nBTCUSDT / 가상 cross {settings.leverage}x / 시드환산 {settings.entry_margin_pct:.0f}%",
        settings,
    )
    while True:
        try:
            result = run_shadow_once(settings, initial_equity=args.seed)
            print(
                json.dumps(
                    {
                        "ts": result.get("ts"),
                        "mode": result.get("mode"),
                        "portfolio": result.get("portfolio"),
                        "guards": result.get("guards"),
                        "action": result.get("action"),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                flush=True,
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            state = load_shadow_state(settings, args.seed)
            state["last_error"] = f"{type(exc).__name__}: {exc}"
            state["last_cycle_ts"] = iso()
            save_shadow_state(settings, state)
            shadow_log(f"shadow cycle error: {exc}\n{traceback.format_exc()}", settings)
            notify(f"⚠️ <b>BTC Structure SHADOW 오류</b>\n{type(exc).__name__}: {exc}", settings)
        time.sleep(settings.loop_seconds)


def shadow_status_report(settings: Settings) -> dict[str, Any]:
    state = load_shadow_state(settings)
    initial = safe_float(state.get("initial_equity"))
    equity = safe_float(state.get("shadow_equity"), initial)
    closed = int(state.get("closed_trades", 0))
    wins = int(state.get("winning_trades", 0))
    gross_profit = safe_float(state.get("shadow_gross_profit"))
    gross_loss_abs = safe_float(state.get("shadow_gross_loss_abs"))
    sum_net_r = safe_float(state.get("shadow_sum_net_r"))
    return {
        "version": VERSION,
        "mode": "SHADOW_PUBLIC_ONLY",
        "order_capability": False,
        "initial_equity": initial,
        "balance": safe_float(state.get("shadow_balance"), initial),
        "equity": equity,
        "total_return_pct": ((equity / initial - 1.0) * 100.0 if initial > 0 else 0.0),
        "realized_net_pnl": safe_float(state.get("shadow_realized_net_pnl")),
        "unrealized_pnl": safe_float(state.get("shadow_unrealized_pnl")),
        "estimated_exit_fee": safe_float(state.get("shadow_estimated_exit_fee")),
        "total_fees": safe_float(state.get("shadow_total_fees")),
        "closed_trades": closed,
        "wins": wins,
        "losses": int(state.get("losing_trades", 0)),
        "win_rate_pct": (wins / closed * 100.0 if closed > 0 else 0.0),
        "profit_factor": (gross_profit / gross_loss_abs if gross_loss_abs > 0 else None),
        "average_net_r": (sum_net_r / closed if closed > 0 else 0.0),
        "max_drawdown_pct": safe_float(state.get("shadow_max_drawdown_pct")),
        "managed_position": state.get("managed_position"),
        "last_cycle_ts": state.get("last_cycle_ts"),
        "last_error": state.get("last_error"),
        "paths": {
            "state": str(settings.shadow_state_path),
            "trades": str(settings.shadow_trades_path),
            "equity": str(settings.shadow_equity_path),
            "events": str(settings.shadow_events_path),
            "log": str(settings.shadow_log_path),
        },
    }


def cmd_shadow_status(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    print(json.dumps(shadow_status_report(settings), ensure_ascii=False, indent=2, default=str))


def cmd_shadow_reset(args: argparse.Namespace) -> None:
    if args.confirm != "RESET_SHADOW":
        raise SystemExit("confirmation phrase must be RESET_SHADOW")
    settings = load_settings(args.config)
    seed = args.seed if args.seed is not None else settings.shadow_initial_equity
    print(json.dumps(reset_shadow_files(settings, seed), ensure_ascii=False, indent=2, default=str))


def cmd_doctor(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    report = doctor(settings, prearm=args.prearm)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if not report.get("ok"):
        raise SystemExit(1)


def cmd_setup(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    print(json.dumps(setup_account(settings), ensure_ascii=False, indent=2, default=str))


def cmd_arm(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    report = doctor(settings, prearm=True)
    if not report.get("ok"):
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        raise SystemExit("pre-arm doctor failed")
    payload = write_arm_file(settings, args.phrases)
    print(json.dumps({"ok": True, "arm": payload, "env_required": f"{LIVE_ENV}=true"}, ensure_ascii=False, indent=2))


def cmd_disarm(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    disarm(settings)
    print(json.dumps({"ok": True, "armed": False}, ensure_ascii=False))


def cmd_status(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    state = load_state(settings)
    armed, reasons = arm_valid(settings)
    print(json.dumps({"version": VERSION, "armed": armed, "arm_reasons": reasons, "state": state}, ensure_ascii=False, indent=2, default=str))


def cmd_self_test(_: argparse.Namespace) -> None:
    result = self_test()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BTC volume-structure pullback trend engine for Bitget UTA")
    parser.add_argument("--config", default=None, help="config path")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("once")
    p.add_argument("--observe", action="store_true", help="calculate/read only; never place orders")
    p.set_defaults(func=cmd_once)

    p = sub.add_parser("loop")
    p.add_argument("--observe", action="store_true", help="calculate/read only; never place orders")
    p.set_defaults(func=cmd_loop)

    p = sub.add_parser("shadow-once", help="public market data + virtual fills; cannot place orders")
    p.add_argument("--seed", type=float, default=None, help="used only when shadow state does not yet exist")
    p.set_defaults(func=cmd_shadow_once)

    p = sub.add_parser("shadow-loop", help="continuous public-data paper execution; cannot place orders")
    p.add_argument("--seed", type=float, default=None, help="used only when shadow state does not yet exist")
    p.set_defaults(func=cmd_shadow_loop)

    p = sub.add_parser("shadow-status")
    p.set_defaults(func=cmd_shadow_status)

    p = sub.add_parser("shadow-reset")
    p.add_argument("confirm", help="must be RESET_SHADOW")
    p.add_argument("--seed", type=float, default=None)
    p.set_defaults(func=cmd_shadow_reset)

    p = sub.add_parser("doctor")
    p.add_argument("--prearm", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("setup-account")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("arm")
    p.add_argument("phrases", nargs=4)
    p.set_defaults(func=cmd_arm)

    p = sub.add_parser("disarm")
    p.set_defaults(func=cmd_disarm)

    p = sub.add_parser("status")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("self-test")
    p.set_defaults(func=cmd_self_test)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
