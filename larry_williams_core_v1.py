from __future__ import annotations

"""Larry Williams Core v1.0 for Bitget UTA.

Live-capable two-profile engine:
- ETHUSDT: 24/7 crypto session adaptation of volatility breakout + OOPS.
- SKHYUSDT: stock-perpetual profile tied to U.S. regular trading hours.

The module is deliberately independent from prior signal logic.  It reuses only the
existing BitgetUTAClient and TelegramBot transport classes in index-sniper-pro.

IMPORTANT
---------
This software places real orders only after all explicit arming gates pass.  The
exchange remains the source of truth.  An exchange-side initial stop-loss and an
emergency take-profit are attached to every opening order; the Larry-style
price-point trail and bailout exits are managed by the running process.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

try:
    from index_sniper.exchange.bitget_uta import BitgetUTAClient
except Exception:  # pragma: no cover - resolved in the target repository
    BitgetUTAClient = None  # type: ignore

try:
    from index_sniper.telegram.bot import TelegramBot
except Exception:  # pragma: no cover
    TelegramBot = None  # type: ignore


VERSION = "1.1.0"
CATEGORY = "USDT-FUTURES"
UTC = timezone.utc
KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config/larry_williams_core_v1.json"
DEFAULT_STATE = ROOT / "data/larry_williams_core_v1_state.json"
DEFAULT_ARM = ROOT / "data/LARRY_WILLIAMS_CORE_V1_ARMED.json"
DEFAULT_LOG = ROOT / "logs/larry-williams-core-v1.log"
DEFAULT_TRADES = ROOT / "research/larry_williams_core_v1_trades.csv"
DEFAULT_EVENTS = ROOT / "research/larry_williams_core_v1_events.jsonl"
ARM_PHRASE = "START_LARRY_CORE_LIVE_5X_CROSS_30_ETH_SKHY"
RISK_PHRASE = "I_UNDERSTAND_30PCT_MARGIN_5X"
NO_WITHDRAW_PHRASE = "API_HAS_NO_WITHDRAW_PERMISSION"
IP_WHITELIST_PHRASE = "API_IP_WHITELISTED"

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
        return datetime.fromtimestamp(self.ts / 1000, tz=UTC)


@dataclass
class Instrument:
    symbol: str
    status: str
    symbol_type: str
    is_reality: str
    min_order_qty: Decimal
    max_order_qty: Decimal
    max_market_order_qty: Decimal
    min_order_amount: Decimal
    price_precision: int
    quantity_precision: int
    price_step: Decimal
    quantity_step: Decimal
    min_leverage: float
    max_leverage: float
    maker_fee: float
    taker_fee: float


@dataclass
class Ticker:
    symbol: str
    last: float
    mark: float
    index: float
    bid: float
    ask: float
    funding: float
    open_interest: float
    ts: int

    @property
    def spread_pct(self) -> float:
        mid = (self.bid + self.ask) / 2.0
        return ((self.ask - self.bid) / mid * 100.0) if mid > 0 and self.ask >= self.bid else 999.0


@dataclass
class Candidate:
    symbol: str
    profile: str
    side: str
    setup: str
    score: float
    entry_reference: float
    trigger_price: float
    stop_price: float
    emergency_tp_price: float
    stop_distance_pct: float
    account_risk_pct: float
    signal_bar_ts: int
    reasons: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class ManagedPosition:
    symbol: str
    profile: str
    side: str
    setup: str
    qty: float
    entry_price: float
    entry_ts: str
    order_id: str
    client_oid: str
    initial_stop: float
    software_stop: float
    emergency_tp: float
    initial_r: float
    best_price: float
    entry_equity: float
    score: float
    hold_mode: str
    last_bailout_key: str = ""
    trail_active: bool = False
    signal_bar_ts: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class Settings:
    config_path: Path
    loop_seconds: int
    leverage: int
    margin_mode: str
    entry_margin_pct: float
    max_open_positions: int
    max_daily_loss_pct: float
    max_weekly_drawdown_pct: float
    max_consecutive_losses: int
    signal_threshold: float
    cooldown_minutes: int
    state_path: Path
    arm_path: Path
    log_path: Path
    trades_path: Path
    events_path: Path
    notify: bool
    heartbeat_minutes: int
    symbols: dict[str, dict[str, Any]]


# ---------------------------------------------------------------------------
# General utility
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(UTC).isoformat()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def as_decimal(value: Any, default: str = "0") -> Decimal:
    try:
        if value in (None, ""):
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new / old - 1.0) * 100.0


def median(values: Iterable[float], default: float = 0.0) -> float:
    clean = [float(v) for v in values if math.isfinite(float(v))]
    return float(statistics.median(clean)) if clean else default


def mean(values: Iterable[float], default: float = 0.0) -> float:
    clean = [float(v) for v in values if math.isfinite(float(v))]
    return float(statistics.fmean(clean)) if clean else default


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


def day_key(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(UTC).strftime("%Y-%m-%d")


def week_key(dt: datetime | None = None) -> str:
    n = (dt or now_utc()).astimezone(UTC)
    y, w, _ = n.isocalendar()
    return f"{y}-W{w:02d}"


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


def load_settings(config_path: str | Path | None = None) -> Settings:
    path = Path(config_path or os.getenv("LARRY_V1_CONFIG", DEFAULT_CONFIG))
    if not path.is_absolute():
        path = ROOT / path
    cfg = read_json(path, None)
    if not isinstance(cfg, dict):
        raise RuntimeError(f"config not found or invalid: {path}")

    def p(key: str, default: Path) -> Path:
        value = Path(str(cfg.get(key, default)))
        return value if value.is_absolute() else ROOT / value

    settings = Settings(
        config_path=path,
        loop_seconds=max(10, int(cfg.get("loop_seconds", 30))),
        leverage=int(cfg.get("leverage", 5)),
        margin_mode=str(cfg.get("margin_mode", "crossed")).lower(),
        entry_margin_pct=float(cfg.get("entry_margin_pct", 30.0)),
        max_open_positions=int(cfg.get("max_open_positions", 1)),
        max_daily_loss_pct=float(cfg.get("max_daily_loss_pct", 5.0)),
        max_weekly_drawdown_pct=float(cfg.get("max_weekly_drawdown_pct", 9.0)),
        max_consecutive_losses=int(cfg.get("max_consecutive_losses", 2)),
        signal_threshold=float(cfg.get("signal_threshold", 72.0)),
        cooldown_minutes=int(cfg.get("cooldown_minutes", 90)),
        state_path=p("state_path", DEFAULT_STATE),
        arm_path=p("arm_path", DEFAULT_ARM),
        log_path=p("log_path", DEFAULT_LOG),
        trades_path=p("trades_path", DEFAULT_TRADES),
        events_path=p("events_path", DEFAULT_EVENTS),
        notify=bool(cfg.get("notify", True)),
        heartbeat_minutes=int(cfg.get("heartbeat_minutes", 60)),
        symbols=dict(cfg.get("symbols") or {}),
    )
    contract_errors = validate_settings_contract(settings)
    if contract_errors:
        raise RuntimeError("invalid Larry Core live-risk contract: " + "; ".join(contract_errors))
    return settings


def validate_settings_contract(settings: Settings) -> list[str]:
    """Lock the user-requested live-risk contract against accidental drift."""
    errors: list[str] = []
    if settings.leverage != 5:
        errors.append(f"leverage={settings.leverage}, expected 5")
    if settings.margin_mode != "crossed":
        errors.append(f"margin_mode={settings.margin_mode}, expected crossed")
    if abs(settings.entry_margin_pct - 30.0) > 1e-9:
        errors.append(f"entry_margin_pct={settings.entry_margin_pct}, expected 30")
    if settings.max_open_positions != 1:
        errors.append(f"max_open_positions={settings.max_open_positions}, expected 1")
    for symbol, profile in (("ETHUSDT", "crypto"), ("SKHYUSDT", "stock")):
        cfg = settings.symbols.get(symbol)
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled", True)):
            errors.append(f"required symbol disabled/missing: {symbol}")
            continue
        if str(cfg.get("profile", "")).lower() != profile:
            errors.append(f"{symbol} profile={cfg.get('profile')}, expected {profile}")
        max_risk = safe_float(cfg.get("max_account_risk_pct"), 0.0)
        if not 0.25 <= max_risk <= 3.0:
            errors.append(f"{symbol} max_account_risk_pct={max_risk} outside 0.25-3.0")
    return errors


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


# ---------------------------------------------------------------------------
# Bitget API helpers
# ---------------------------------------------------------------------------


def api_success(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return False
    code = str(resp.get("code", ""))
    return code in {"00000", "0"}


def require_success(resp: Any, context: str) -> dict[str, Any]:
    if not api_success(resp):
        raise RuntimeError(f"{context} failed: {resp}")
    return resp


def response_data(resp: Any) -> Any:
    if not isinstance(resp, dict):
        return None
    return resp.get("data")


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
    rows = response_list(resp)
    row = next((x for x in rows if isinstance(x, dict) and str(x.get("symbol")) == symbol), None)
    if not row:
        raise RuntimeError(f"instrument not found: {symbol}")
    price_precision = int(safe_float(row.get("pricePrecision"), 8))
    qty_precision = int(safe_float(row.get("quantityPrecision"), 8))
    price_step = as_decimal(row.get("priceMultiplier"), "1").copy_abs()
    qty_step = as_decimal(row.get("quantityMultiplier"), "1").copy_abs()
    if price_step <= 0:
        price_step = Decimal(1).scaleb(-price_precision)
    if qty_step <= 0:
        qty_step = Decimal(1).scaleb(-qty_precision)
    return Instrument(
        symbol=symbol,
        status=str(row.get("status", "")).lower(),
        symbol_type=str(row.get("symbolType", "")).lower(),
        is_reality=str(row.get("isReality", "no")).lower(),
        min_order_qty=as_decimal(row.get("minOrderQty"), "0"),
        max_order_qty=as_decimal(row.get("maxOrderQty"), "0"),
        max_market_order_qty=as_decimal(row.get("maxMarketOrderQty"), "0"),
        min_order_amount=as_decimal(row.get("minOrderAmount"), "0"),
        price_precision=price_precision,
        quantity_precision=qty_precision,
        price_step=price_step,
        quantity_step=qty_step,
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
    rows = response_list(resp)
    row = next((x for x in rows if isinstance(x, dict) and str(x.get("symbol")) == symbol), None)
    if not row:
        raise RuntimeError(f"ticker not found: {symbol}")
    last = safe_float(row.get("lastPrice") or row.get("lastPr"))
    mark = safe_float(row.get("markPrice"), last)
    index = safe_float(row.get("indexPrice"), mark)
    bid = safe_float(row.get("bid1Price") or row.get("bidPr"), last)
    ask = safe_float(row.get("ask1Price") or row.get("askPr"), last)
    if min(last, mark, bid, ask) <= 0:
        raise RuntimeError(f"invalid ticker: {row}")
    return Ticker(
        symbol=symbol,
        last=last,
        mark=mark,
        index=index,
        bid=bid,
        ask=ask,
        funding=safe_float(row.get("fundingRate")),
        open_interest=safe_float(row.get("openInterest")),
        ts=int(safe_float(row.get("ts"), time.time() * 1000)),
    )


def parse_bar(row: Any) -> Bar | None:
    try:
        if isinstance(row, (list, tuple)) and len(row) >= 6:
            return Bar(
                ts=int(float(row[0])),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                turnover=float(row[6]) if len(row) > 6 else 0.0,
            )
        if isinstance(row, dict):
            return Bar(
                ts=int(safe_float(row.get("ts") or row.get("timestamp") or row.get("time"))),
                open=safe_float(row.get("open") or row.get("openPrice")),
                high=safe_float(row.get("high") or row.get("highPrice")),
                low=safe_float(row.get("low") or row.get("lowPrice")),
                close=safe_float(row.get("close") or row.get("closePrice")),
                volume=safe_float(row.get("volume") or row.get("baseVolume")),
                turnover=safe_float(row.get("turnover") or row.get("quoteVolume")),
            )
    except Exception:
        return None
    return None


def fetch_candles(client: Any, symbol: str, interval: str, count: int) -> list[Bar]:
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval {interval}")
    target = max(10, count)
    rows_by_ts: dict[int, Bar] = {}
    end_time: int | None = None
    # UTA supports up to 1,000 candles per page. Walk backward only when needed.
    page_limit = min(1_000, target)
    for _ in range(max(1, math.ceil(target / page_limit) + 2)):
        params: dict[str, Any] = {
            "category": CATEGORY,
            "symbol": symbol,
            "interval": interval,
            "type": "market",
            "limit": str(page_limit),
        }
        if end_time is not None:
            params["endTime"] = str(end_time)
        resp = require_success(client_get(client, "/api/v3/market/candles", params, auth=False), f"candles {symbol} {interval}")
        batch = [bar for bar in (parse_bar(x) for x in response_list(resp)) if bar and bar.ts > 0]
        if not batch:
            break
        for bar in batch:
            rows_by_ts[bar.ts] = bar
        oldest = min(bar.ts for bar in batch)
        if len(rows_by_ts) >= target or end_time == oldest - 1:
            break
        end_time = oldest - 1
        time.sleep(0.03)
    rows = sorted(rows_by_ts.values(), key=lambda x: x.ts)
    return rows[-target:]


def completed_bars(bars: Sequence[Bar], interval: str, at: datetime | None = None) -> list[Bar]:
    now_ms = int((at or now_utc()).timestamp() * 1000)
    span = INTERVAL_MS[interval]
    return [b for b in bars if b.ts + span <= now_ms + 2_000]


def fetch_account_assets(client: Any) -> dict[str, Any]:
    resp = require_success(client_get(client, "/api/v3/account/assets", {}, auth=True), "account assets")
    data = response_data(resp)
    if not isinstance(data, dict):
        raise RuntimeError(f"invalid account assets: {resp}")
    return data


def fetch_account_settings(client: Any) -> dict[str, Any]:
    resp = require_success(client_get(client, "/api/v3/account/settings", {}, auth=True), "account settings")
    data = response_data(resp)
    return data if isinstance(data, dict) else {}


def fetch_account_info(client: Any) -> dict[str, Any]:
    resp = require_success(client_get(client, "/api/v3/account/info", {}, auth=True), "account info")
    data = response_data(resp)
    return data if isinstance(data, dict) else {}


def fetch_positions(client: Any, symbol: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"category": CATEGORY}
    if symbol:
        params["symbol"] = symbol
    resp = require_success(client_get(client, "/api/v3/position/current-position", params, auth=True), "current positions")
    return [x for x in response_list(resp) if isinstance(x, dict)]


def nonzero_positions(client: Any) -> list[dict[str, Any]]:
    return [p for p in fetch_positions(client) if abs(safe_float(p.get("total"))) > 0]


def fetch_open_orders(client: Any, symbol: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"category": CATEGORY, "limit": "100"}
    if symbol:
        params["symbol"] = symbol
    resp = require_success(client_get(client, "/api/v3/trade/unfilled-orders", params, auth=True), "open orders")
    return [x for x in response_list(resp) if isinstance(x, dict)]


def fetch_strategy_orders_best_effort(client: Any, symbol: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"category": CATEGORY, "type": "tpsl", "limit": "100"}
    if symbol:
        params["symbol"] = symbol
    try:
        resp = client_get(client, "/api/v3/trade/unfilled-strategy-orders", params, auth=True)
        return [x for x in response_list(resp) if isinstance(x, dict)] if api_success(resp) else []
    except Exception:
        return []


def account_equity(assets: dict[str, Any]) -> float:
    return safe_float(assets.get("accountEquity") or assets.get("usdtEquity") or assets.get("effEquity"))


def account_available_usdt(assets: dict[str, Any]) -> float:
    rows = assets.get("assets") if isinstance(assets.get("assets"), list) else []
    for row in rows:
        if isinstance(row, dict) and str(row.get("coin")) == "USDT":
            return safe_float(row.get("available") or row.get("equity"))
    return safe_float(assets.get("effEquity") or assets.get("usdtEquity") or assets.get("accountEquity"))


def infer_hold_mode(settings_data: dict[str, Any], positions: Sequence[dict[str, Any]] | None = None) -> str:
    def scan(value: Any) -> str:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key) in {"holdMode", "positionMode", "posMode"} and item:
                    raw = str(item).lower()
                    if "hedge" in raw or "double" in raw:
                        return "hedge_mode"
                    if "one" in raw or "single" in raw:
                        return "one_way_mode"
                found = scan(item)
                if found != "unknown":
                    return found
        elif isinstance(value, list):
            for item in value:
                found = scan(item)
                if found != "unknown":
                    return found
        return "unknown"

    found = scan(settings_data)
    if found != "unknown":
        return found
    for p in positions or []:
        raw = str(p.get("holdMode", "")).lower()
        if raw in {"hedge_mode", "one_way_mode"}:
            return raw
    return "unknown"


def account_symbol_config(settings_data: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    rows = settings_data.get("symbolConfigList")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol.upper():
            return row
    return None


def verify_symbol_account_setup(settings_data: dict[str, Any], symbol: str, settings: Settings) -> list[str]:
    row = account_symbol_config(settings_data, symbol)
    if row is None:
        return ["symbol configuration absent from account settings"]
    errors: list[str] = []
    if str(row.get("marginMode", "")).lower() != settings.margin_mode:
        errors.append(f"marginMode={row.get('marginMode')} expected {settings.margin_mode}")
    raw_lev = row.get("leverage")
    values: list[int] = []
    if isinstance(raw_lev, (list, tuple)):
        values = [int(safe_float(x)) for x in raw_lev]
    elif raw_lev not in (None, ""):
        values = [int(safe_float(raw_lev))]
    for key in ("longLeverage", "shortLeverage"):
        if row.get(key) not in (None, ""):
            values.append(int(safe_float(row.get(key))))
    if not values or any(v != settings.leverage for v in values):
        errors.append(f"leverage={values or raw_lev} expected {settings.leverage}")
    return errors


def set_leverage(client: Any, symbol: str, leverage: int, margin_mode: str) -> dict[str, Any]:
    payload = {
        "category": CATEGORY,
        "symbol": symbol,
        "leverage": str(leverage),
        "marginMode": margin_mode,
    }
    return require_success(client_post(client, "/api/v3/account/set-leverage", payload), f"set leverage {symbol}")


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


def ema_last(values: Sequence[float], period: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    out = float(values[0])
    for value in values[1:]:
        out = alpha * float(value) + (1.0 - alpha) * out
    return out


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    out: list[float] = []
    prev_close: float | None = None
    for bar in bars:
        if prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        out.append(max(0.0, tr))
        prev_close = bar.close
    return out


def atr_value(bars: Sequence[Bar], period: int = 14) -> float:
    trs = true_ranges(bars)
    return mean(trs[-period:]) if len(trs) >= period else mean(trs)


def williams_r(bars: Sequence[Bar], period: int = 14, offset: int = 0) -> float:
    end = len(bars) - offset
    start = end - period
    if start < 0 or end <= 0:
        return -50.0
    window = bars[start:end]
    hh = max(b.high for b in window)
    ll = min(b.low for b in window)
    close = window[-1].close
    return -100.0 * (hh - close) / (hh - ll) if hh > ll else -50.0


def ultimate_oscillator(bars: Sequence[Bar], p1: int = 7, p2: int = 14, p3: int = 28, offset: int = 0) -> float:
    end = len(bars) - offset
    if end < p3 + 1:
        return 50.0
    subset = bars[:end]
    bp: list[float] = []
    tr: list[float] = []
    for prev, cur in zip(subset[:-1], subset[1:]):
        low_ref = min(cur.low, prev.close)
        high_ref = max(cur.high, prev.close)
        bp.append(cur.close - low_ref)
        tr.append(max(1e-12, high_ref - low_ref))

    def avg_period(period: int) -> float:
        return sum(bp[-period:]) / max(1e-12, sum(tr[-period:]))

    return 100.0 * (4.0 * avg_period(p1) + 2.0 * avg_period(p2) + avg_period(p3)) / 7.0


def williams_ad_line(bars: Sequence[Bar]) -> list[float]:
    """Williams Accumulation/Distribution line using prior-close references."""
    if not bars:
        return []
    out = [0.0]
    cumulative = 0.0
    prev_close = bars[0].close
    for cur in bars[1:]:
        if cur.close > prev_close:
            move = cur.close - min(cur.low, prev_close)
        elif cur.close < prev_close:
            move = cur.close - max(cur.high, prev_close)
        else:
            move = 0.0
        cumulative += move
        out.append(cumulative)
        prev_close = cur.close
    return out


def crypto_seasonality_bias(bars1h: Sequence[Bar], at: datetime | None = None) -> dict[str, float]:
    """Past-only, shrunk hour/weekday tendency; neutral until sample counts are adequate."""
    rows = completed_bars(bars1h, "1H", at=at)
    if len(rows) < 120:
        return {"bias": 0.0, "hour_median": 0.0, "weekday_median": 0.0, "confidence": 0.0}
    now = (at or now_utc()).astimezone(UTC)
    rets = [(b, pct_change(b.close, b.open)) for b in rows if b.open > 0]
    hour_values = [r for b, r in rets if b.dt.hour == now.hour]
    weekday_values = [r for b, r in rets if b.dt.weekday() == now.weekday()]
    scale = max(0.05, median([abs(r) for _, r in rets[-240:]], 0.05))
    hour_med = median(hour_values, 0.0)
    weekday_med = median(weekday_values, 0.0)
    confidence = min(1.0, len(hour_values) / 12.0) * min(1.0, len(weekday_values) / 36.0)
    raw = (0.65 * hour_med + 0.35 * weekday_med) / scale
    return {
        "bias": clamp(raw * confidence, -1.0, 1.0),
        "hour_median": hour_med,
        "weekday_median": weekday_med,
        "confidence": confidence,
    }


def volume_ratio(bars: Sequence[Bar], lookback: int = 20) -> float:
    if len(bars) < lookback + 1:
        return 1.0
    base = median([b.volume for b in bars[-lookback - 1 : -1]], 0.0)
    return bars[-1].volume / base if base > 0 else 1.0


def recent_swing_low(bars: Sequence[Bar], lookback: int = 4) -> float:
    return min(b.low for b in bars[-lookback:])


def recent_swing_high(bars: Sequence[Bar], lookback: int = 4) -> float:
    return max(b.high for b in bars[-lookback:])


# ---------------------------------------------------------------------------
# U.S. market calendar helpers
# ---------------------------------------------------------------------------


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def observed_fixed_holiday(year: int, month: int, day: int) -> date:
    d = date(year, month, day)
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def us_equity_holidays(year: int) -> set[date]:
    return {
        observed_fixed_holiday(year, 1, 1),
        nth_weekday(year, 1, 0, 3),  # MLK
        nth_weekday(year, 2, 0, 3),  # Presidents' Day
        easter_sunday(year) - timedelta(days=2),  # Good Friday
        last_weekday(year, 5, 0),  # Memorial Day
        observed_fixed_holiday(year, 6, 19),
        observed_fixed_holiday(year, 7, 4),
        nth_weekday(year, 9, 0, 1),  # Labor Day
        nth_weekday(year, 11, 3, 4),  # Thanksgiving
        observed_fixed_holiday(year, 12, 25),
    }


def previous_trading_day(d: date) -> date:
    candidate = d - timedelta(days=1)
    while candidate.weekday() >= 5 or candidate in us_equity_holidays(candidate.year):
        candidate -= timedelta(days=1)
    return candidate


def stock_session_close_for(d: date) -> dtime:
    thanksgiving = nth_weekday(d.year, 11, 3, 4)
    if d == thanksgiving + timedelta(days=1):
        return dtime(13, 0)
    if d.month == 12 and d.day == 24 and d.weekday() < 5 and d not in us_equity_holidays(d.year):
        return dtime(13, 0)
    # The final trading day before the observed Independence Day holiday is a
    # common 13:00 ET close (for example, 2026-07-02).
    july_observed = observed_fixed_holiday(d.year, 7, 4)
    if d == previous_trading_day(july_observed):
        return dtime(13, 0)
    return dtime(16, 0)


def stock_market_open(dt: datetime) -> bool:
    ny = dt.astimezone(NY)
    d = ny.date()
    if d.weekday() >= 5 or d in us_equity_holidays(d.year):
        return False
    return dtime(9, 30) <= ny.time() < stock_session_close_for(d)


def stock_entry_window(dt: datetime, start: str, end: str) -> bool:
    if not stock_market_open(dt):
        return False
    ny = dt.astimezone(NY)
    sh, sm = (int(x) for x in start.split(":"))
    eh, em = (int(x) for x in end.split(":"))
    close_time = stock_session_close_for(ny.date())
    effective_end = min(dtime(eh, em), close_time)
    return dtime(sh, sm) <= ny.time() < effective_end


def stock_force_flat_due(dt: datetime, force_flat: str) -> bool:
    ny = dt.astimezone(NY)
    if ny.date().weekday() >= 5:
        return True
    if ny.date() in us_equity_holidays(ny.year):
        return True
    fh, fm = (int(x) for x in force_flat.split(":"))
    configured = dtime(fh, fm)
    close_time = stock_session_close_for(ny.date())
    effective = min(configured, (datetime.combine(ny.date(), close_time) - timedelta(minutes=5)).time())
    return ny.time() >= effective


def group_stock_rth_sessions(bars: Sequence[Bar]) -> dict[date, list[Bar]]:
    sessions: dict[date, list[Bar]] = {}
    for bar in bars:
        ny = bar.dt.astimezone(NY)
        d = ny.date()
        if d.weekday() >= 5 or d in us_equity_holidays(d.year):
            continue
        close_time = stock_session_close_for(d)
        if dtime(9, 30) <= ny.time() < close_time:
            sessions.setdefault(d, []).append(bar)
    for rows in sessions.values():
        rows.sort(key=lambda b: b.ts)
    return sessions


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------


def validate_instrument_for_profile(instrument: Instrument, profile: str, leverage: int) -> list[str]:
    errors: list[str] = []
    if instrument.status != "online":
        errors.append(f"status={instrument.status}")
    if instrument.is_reality.lower() == "yes":
        errors.append("Reality stock token is not a perpetual-futures instrument")
    expected = "stock" if profile == "stock" else "crypto"
    if instrument.symbol_type and instrument.symbol_type != expected:
        errors.append(f"symbolType={instrument.symbol_type}, expected {expected}")
    if not (instrument.min_leverage <= leverage <= instrument.max_leverage):
        errors.append(f"leverage {leverage} outside {instrument.min_leverage}-{instrument.max_leverage}")
    return errors


def risk_cap_stop_distance_pct(settings: Settings, symbol_cfg: dict[str, Any]) -> float:
    # Notional/equity = entry_margin_pct/100 * leverage.
    exposure_multiple = settings.entry_margin_pct / 100.0 * settings.leverage
    max_account_risk = float(symbol_cfg.get("max_account_risk_pct", 2.0))
    return max_account_risk / max(exposure_multiple, 1e-9)


def make_candidate(
    *,
    settings: Settings,
    symbol: str,
    cfg: dict[str, Any],
    ticker: Ticker,
    instrument: Instrument,
    bars15: Sequence[Bar],
    bars1h: Sequence[Bar],
    side: str,
    setup: str,
    base_score: float,
    trigger: float,
    structural_stop: float,
    signal_bar_ts: int,
    special_reasons: list[str],
    special_diag: dict[str, Any],
) -> Candidate | None:
    profile = str(cfg.get("profile"))
    completed15 = completed_bars(bars15, "15m")
    completed1h = completed_bars(bars1h, "1H")
    if len(completed15) < 35 or len(completed1h) < 55:
        return None

    entry = ticker.ask if side == "LONG" else ticker.bid
    atr15 = atr_value(completed15, 14)
    atr1h = atr_value(completed1h, 14)
    min_stop_atr = float(cfg.get("min_stop_atr15", 0.45))
    min_distance = max(atr15 * min_stop_atr, entry * 0.0015)

    if side == "LONG":
        stop = min(structural_stop, entry - min_distance)
        if stop <= 0 or stop >= entry:
            return None
        stop_distance = entry - stop
    else:
        stop = max(structural_stop, entry + min_distance)
        if stop <= entry:
            return None
        stop_distance = stop - entry

    stop_distance_pct = stop_distance / entry * 100.0
    cap_pct = risk_cap_stop_distance_pct(settings, cfg)
    if stop_distance_pct > cap_pct:
        return None

    r_multiple = float(cfg.get("emergency_tp_r", 3.0))
    tp = entry + stop_distance * r_multiple if side == "LONG" else entry - stop_distance * r_multiple

    closes1h = [b.close for b in completed1h]
    ema20 = ema_last(closes1h[-120:], 20)
    ema50 = ema_last(closes1h[-160:], 50)
    wpr_now = williams_r(completed15, 14, 0)
    wpr_prev = williams_r(completed15, 14, 1)
    uo_now = ultimate_oscillator(completed15, 7, 14, 28, 0)
    uo_prev = ultimate_oscillator(completed15, 7, 14, 28, 1)
    vol_ratio = volume_ratio(completed15, 20)

    score = base_score
    reasons = list(special_reasons)

    trend_aligned = (side == "LONG" and ema20 > ema50) or (side == "SHORT" and ema20 < ema50)
    if trend_aligned:
        score += 10
        reasons.append("1H EMA20/50 aligned")
    else:
        score -= 4
        reasons.append("1H trend counter")

    if setup.startswith("OOPS"):
        wpr_ok = (side == "LONG" and wpr_prev <= -80 and wpr_now > -80) or (
            side == "SHORT" and wpr_prev >= -20 and wpr_now < -20
        )
    else:
        wpr_ok = (side == "LONG" and wpr_now >= -35 and wpr_now >= wpr_prev) or (
            side == "SHORT" and wpr_now <= -65 and wpr_now <= wpr_prev
        )
    if wpr_ok:
        score += 10
        reasons.append("Williams %R timing")

    uo_ok = (side == "LONG" and uo_now >= 50 and uo_now >= uo_prev) or (
        side == "SHORT" and uo_now <= 50 and uo_now <= uo_prev
    )
    if uo_ok:
        score += 10
        reasons.append("Ultimate Oscillator aligned")

    if vol_ratio >= float(cfg.get("min_volume_ratio", 1.15)):
        score += 7
        reasons.append(f"volume expansion {vol_ratio:.2f}x")
    elif vol_ratio < 0.7:
        score -= 5

    if profile == "crypto":
        crowd_limit = float(cfg.get("funding_crowding_abs", 0.0005))
        if side == "LONG":
            if ticker.funding <= crowd_limit:
                score += 4
            else:
                score -= 8
                reasons.append("long funding crowded")
        else:
            if ticker.funding >= -crowd_limit:
                score += 4
            else:
                score -= 8
                reasons.append("short funding crowded")

    spread_limit = float(cfg.get("max_spread_pct", 0.08 if profile == "crypto" else 0.20))
    if ticker.spread_pct > spread_limit:
        return None
    if ticker.spread_pct <= spread_limit / 2:
        score += 3

    account_risk = stop_distance_pct * (settings.entry_margin_pct / 100.0 * settings.leverage)
    diag = {
        **special_diag,
        "entry": entry,
        "trigger": trigger,
        "atr15": atr15,
        "atr1h": atr1h,
        "ema20_1h": ema20,
        "ema50_1h": ema50,
        "wpr": wpr_now,
        "wpr_prev": wpr_prev,
        "uo": uo_now,
        "uo_prev": uo_prev,
        "volume_ratio": vol_ratio,
        "spread_pct": ticker.spread_pct,
        "funding": ticker.funding,
        "oi": ticker.open_interest,
        "risk_cap_price_pct": cap_pct,
    }

    return Candidate(
        symbol=symbol,
        profile=profile,
        side=side,
        setup=setup,
        score=round(score, 3),
        entry_reference=entry,
        trigger_price=trigger,
        stop_price=stop,
        emergency_tp_price=tp,
        stop_distance_pct=stop_distance_pct,
        account_risk_pct=account_risk,
        signal_bar_ts=signal_bar_ts,
        reasons=reasons,
        diagnostics=diag,
    )


def crypto_candidate(
    settings: Settings,
    symbol: str,
    cfg: dict[str, Any],
    instrument: Instrument,
    ticker: Ticker,
    bars15: Sequence[Bar],
    bars1h: Sequence[Bar],
    bars1d: Sequence[Bar],
) -> Candidate | None:
    c15 = completed_bars(bars15, "15m")
    c1d = completed_bars(bars1d, "1D")
    if len(c15) < 40 or len(c1d) < 5:
        return None

    # Current UTC session open comes from the latest 1D candle, including the live candle.
    live_daily = bars1d[-1]
    today_utc = now_utc().date()
    if live_daily.dt.date() != today_utc:
        return None
    session_open = live_daily.open
    prev = c1d[-1]
    recent_ranges = [b.high - b.low for b in c1d[-4:-1]] or [prev.high - prev.low]
    range_ref = median(recent_ranges, prev.high - prev.low)
    if range_ref <= 0:
        return None
    prev_range = prev.high - prev.low
    compression = prev_range / max(range_ref, 1e-9)
    base_k = float(cfg.get("breakout_k_base", 0.35))
    if compression < 0.85:
        k = float(cfg.get("breakout_k_compressed", 0.28))
    elif compression > 1.25:
        k = float(cfg.get("breakout_k_expanded", 0.45))
    else:
        k = base_k
    long_trigger = session_open + k * range_ref
    short_trigger = session_open - k * range_ref
    prior_close = c15[-1].close
    price = ticker.last
    atr15 = atr_value(c15, 14)
    chase_atr = float(cfg.get("anti_chase_atr15", 0.35))

    # Crypto adaptation of OOPS: a prior-session extreme is swept and then reclaimed.
    sweep_window = c15[-6:]
    sweep_low = min(b.low for b in sweep_window)
    sweep_high = max(b.high for b in sweep_window)
    latest = c15[-1]
    if sweep_low < prev.low and latest.close > prev.low and price > prev.low:
        stop = sweep_low - 0.10 * atr15
        return make_candidate(
            settings=settings,
            symbol=symbol,
            cfg=cfg,
            ticker=ticker,
            instrument=instrument,
            bars15=bars15,
            bars1h=bars1h,
            side="LONG",
            setup="OOPS_SESSION_RECLAIM",
            base_score=57,
            trigger=prev.low,
            structural_stop=stop,
            signal_bar_ts=latest.ts,
            special_reasons=["prior UTC-session low swept and reclaimed"],
            special_diag={"session_open": session_open, "prev_low": prev.low, "sweep_low": sweep_low, "k": k},
        )
    if sweep_high > prev.high and latest.close < prev.high and price < prev.high:
        stop = sweep_high + 0.10 * atr15
        return make_candidate(
            settings=settings,
            symbol=symbol,
            cfg=cfg,
            ticker=ticker,
            instrument=instrument,
            bars15=bars15,
            bars1h=bars1h,
            side="SHORT",
            setup="OOPS_SESSION_RECLAIM",
            base_score=57,
            trigger=prev.high,
            structural_stop=stop,
            signal_bar_ts=latest.ts,
            special_reasons=["prior UTC-session high swept and rejected"],
            special_diag={"session_open": session_open, "prev_high": prev.high, "sweep_high": sweep_high, "k": k},
        )

    if price >= long_trigger and prior_close < long_trigger and (price - long_trigger) <= chase_atr * atr15:
        stop = latest.low - 0.12 * atr15
        return make_candidate(
            settings=settings,
            symbol=symbol,
            cfg=cfg,
            ticker=ticker,
            instrument=instrument,
            bars15=bars15,
            bars1h=bars1h,
            side="LONG",
            setup="VOLATILITY_BREAKOUT",
            base_score=54,
            trigger=long_trigger,
            structural_stop=stop,
            signal_bar_ts=latest.ts,
            special_reasons=[f"UTC session range expansion above open + {k:.2f}×range"],
            special_diag={"session_open": session_open, "range_ref": range_ref, "compression": compression, "k": k},
        )
    if price <= short_trigger and prior_close > short_trigger and (short_trigger - price) <= chase_atr * atr15:
        stop = latest.high + 0.12 * atr15
        return make_candidate(
            settings=settings,
            symbol=symbol,
            cfg=cfg,
            ticker=ticker,
            instrument=instrument,
            bars15=bars15,
            bars1h=bars1h,
            side="SHORT",
            setup="VOLATILITY_BREAKOUT",
            base_score=54,
            trigger=short_trigger,
            structural_stop=stop,
            signal_bar_ts=latest.ts,
            special_reasons=[f"UTC session range expansion below open - {k:.2f}×range"],
            special_diag={"session_open": session_open, "range_ref": range_ref, "compression": compression, "k": k},
        )
    return None


def stock_candidate(
    settings: Settings,
    symbol: str,
    cfg: dict[str, Any],
    instrument: Instrument,
    ticker: Ticker,
    bars15: Sequence[Bar],
    bars1h: Sequence[Bar],
) -> Candidate | None:
    now = now_utc()
    if not stock_entry_window(now, str(cfg.get("entry_start_ny", "09:45")), str(cfg.get("entry_end_ny", "15:30"))):
        return None
    c15 = completed_bars(bars15, "15m")
    sessions = group_stock_rth_sessions(c15)
    current_date = now.astimezone(NY).date()
    dates = sorted(d for d, rows in sessions.items() if rows)
    if current_date not in sessions or len(dates) < 2:
        return None
    previous_dates = [d for d in dates if d < current_date]
    if not previous_dates:
        return None
    prev_date = previous_dates[-1]
    prev_rows = sessions[prev_date]
    today_rows = sessions[current_date]
    if not today_rows:
        return None
    prev_high = max(b.high for b in prev_rows)
    prev_low = min(b.low for b in prev_rows)
    prev_close = prev_rows[-1].close
    current_open = today_rows[0].open
    current_low = min(b.low for b in today_rows)
    current_high = max(b.high for b in today_rows)
    latest = today_rows[-1]
    prev_range = prev_high - prev_low
    if prev_range <= 0:
        return None
    atr15 = atr_value(c15, 14)
    price = ticker.last

    # Original OOPS is especially natural here because the underlying has a real daily open.
    if (current_open < prev_low or current_low < prev_low) and latest.close > prev_low and price > prev_low:
        return make_candidate(
            settings=settings,
            symbol=symbol,
            cfg=cfg,
            ticker=ticker,
            instrument=instrument,
            bars15=bars15,
            bars1h=bars1h,
            side="LONG",
            setup="OOPS_GAP_RECLAIM",
            base_score=60,
            trigger=prev_low,
            structural_stop=current_low - 0.10 * atr15,
            signal_bar_ts=latest.ts,
            special_reasons=["U.S. session downside gap/sweep reclaimed previous low"],
            special_diag={"current_open": current_open, "prev_low": prev_low, "prev_close": prev_close},
        )
    if (current_open > prev_high or current_high > prev_high) and latest.close < prev_high and price < prev_high:
        return make_candidate(
            settings=settings,
            symbol=symbol,
            cfg=cfg,
            ticker=ticker,
            instrument=instrument,
            bars15=bars15,
            bars1h=bars1h,
            side="SHORT",
            setup="OOPS_GAP_RECLAIM",
            base_score=60,
            trigger=prev_high,
            structural_stop=current_high + 0.10 * atr15,
            signal_bar_ts=latest.ts,
            special_reasons=["U.S. session upside gap/sweep rejected previous high"],
            special_diag={"current_open": current_open, "prev_high": prev_high, "prev_close": prev_close},
        )

    k = float(cfg.get("breakout_k_base", 0.28))
    long_trigger = current_open + k * prev_range
    short_trigger = current_open - k * prev_range
    prior_close = today_rows[-2].close if len(today_rows) >= 2 else current_open
    chase_atr = float(cfg.get("anti_chase_atr15", 0.25))
    if price >= long_trigger and prior_close < long_trigger and (price - long_trigger) <= chase_atr * atr15:
        return make_candidate(
            settings=settings,
            symbol=symbol,
            cfg=cfg,
            ticker=ticker,
            instrument=instrument,
            bars15=bars15,
            bars1h=bars1h,
            side="LONG",
            setup="VOLATILITY_BREAKOUT_RTH",
            base_score=56,
            trigger=long_trigger,
            structural_stop=latest.low - 0.12 * atr15,
            signal_bar_ts=latest.ts,
            special_reasons=[f"RTH expansion above open + {k:.2f}×prior range"],
            special_diag={"current_open": current_open, "prev_range": prev_range, "prev_date": str(prev_date)},
        )
    if price <= short_trigger and prior_close > short_trigger and (short_trigger - price) <= chase_atr * atr15:
        return make_candidate(
            settings=settings,
            symbol=symbol,
            cfg=cfg,
            ticker=ticker,
            instrument=instrument,
            bars15=bars15,
            bars1h=bars1h,
            side="SHORT",
            setup="VOLATILITY_BREAKOUT_RTH",
            base_score=56,
            trigger=short_trigger,
            structural_stop=latest.high + 0.12 * atr15,
            signal_bar_ts=latest.ts,
            special_reasons=[f"RTH expansion below open - {k:.2f}×prior range"],
            special_diag={"current_open": current_open, "prev_range": prev_range, "prev_date": str(prev_date)},
        )
    return None


def evaluate_symbol(client: Any, settings: Settings, symbol: str, cfg: dict[str, Any]) -> tuple[Candidate | None, dict[str, Any]]:
    profile = str(cfg.get("profile", "crypto"))
    instrument = fetch_instrument(client, symbol)
    errors = validate_instrument_for_profile(instrument, profile, settings.leverage)
    if errors:
        return None, {"symbol": symbol, "profile": profile, "blocked": errors}
    ticker = fetch_ticker(client, symbol)
    bars15 = fetch_candles(client, symbol, "15m", int(cfg.get("bars_15m", 420)))
    bars1h = fetch_candles(client, symbol, "1H", int(cfg.get("bars_1h", 220)))
    diag: dict[str, Any] = {
        "symbol": symbol,
        "profile": profile,
        "price": ticker.last,
        "mark": ticker.mark,
        "spread_pct": ticker.spread_pct,
        "funding": ticker.funding,
        "open_interest": ticker.open_interest,
        "status": instrument.status,
        "symbol_type": instrument.symbol_type,
        "max_leverage": instrument.max_leverage,
    }
    if profile == "stock":
        candidate = stock_candidate(settings, symbol, cfg, instrument, ticker, bars15, bars1h)
        diag["stock_market_open"] = stock_market_open(now_utc())
        diag["stock_entry_window"] = stock_entry_window(
            now_utc(), str(cfg.get("entry_start_ny", "09:45")), str(cfg.get("entry_end_ny", "15:30"))
        )
    else:
        bars1d = fetch_candles(client, symbol, "1D", int(cfg.get("bars_1d", 80)))
        candidate = crypto_candidate(settings, symbol, cfg, instrument, ticker, bars15, bars1h, bars1d)
    if candidate:
        diag["candidate"] = asdict(candidate)
    return candidate, diag


# ---------------------------------------------------------------------------
# Sizing and order execution
# ---------------------------------------------------------------------------


def rounded_price(instrument: Instrument, price: float, side: str, purpose: str) -> str:
    if purpose == "stop":
        mode = "floor" if side == "LONG" else "ceil"
    elif purpose == "tp":
        mode = "floor" if side == "LONG" else "ceil"
    else:
        mode = "nearest"
    return fmt_decimal(quantize_step(price, instrument.price_step, mode))


def calculate_qty(
    equity: float,
    entry_price: float,
    settings: Settings,
    instrument: Instrument,
) -> Decimal:
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


def unique_client_oid(prefix: str, symbol: str) -> str:
    stamp = datetime.now(UTC).strftime("%m%d%H%M%S")
    token = uuid.uuid4().hex[:5]
    value = f"{prefix}_{symbol[:5]}_{stamp}_{token}"
    return value[:32]


def opening_payload(
    candidate: Candidate,
    qty: Decimal,
    instrument: Instrument,
    settings: Settings,
    hold_mode: str,
    client_oid: str,
) -> dict[str, Any]:
    side = "buy" if candidate.side == "LONG" else "sell"
    payload: dict[str, Any] = {
        "category": CATEGORY,
        "symbol": candidate.symbol,
        "qty": fmt_decimal(qty),
        "side": side,
        "orderType": "market",
        "clientOid": client_oid,
        "reduceOnly": "no",
        "marginMode": settings.margin_mode,
        "takeProfit": rounded_price(instrument, candidate.emergency_tp_price, candidate.side, "tp"),
        "stopLoss": rounded_price(instrument, candidate.stop_price, candidate.side, "stop"),
        "tpTriggerBy": "mark",
        "slTriggerBy": "mark",
        "tpOrderType": "market",
        "slOrderType": "market",
    }
    if hold_mode == "hedge_mode":
        payload["posSide"] = "long" if candidate.side == "LONG" else "short"
    return payload


def closing_payload(position: ManagedPosition, qty: Decimal, settings: Settings, client_oid: str) -> dict[str, Any]:
    side = "sell" if position.side == "LONG" else "buy"
    payload: dict[str, Any] = {
        "category": CATEGORY,
        "symbol": position.symbol,
        "qty": fmt_decimal(qty),
        "side": side,
        "orderType": "market",
        "clientOid": client_oid,
        "marginMode": settings.margin_mode,
    }
    if position.hold_mode == "hedge_mode":
        payload["posSide"] = "long" if position.side == "LONG" else "short"
    else:
        payload["reduceOnly"] = "yes"
    return payload


def wait_for_fill(client: Any, order_id: str, client_oid: str, timeout_seconds: float = 12.0) -> dict[str, Any] | None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        params = {"orderId": order_id} if order_id else {"clientOid": client_oid}
        try:
            resp = client_get(client, "/api/v3/trade/order-info", params, auth=True)
            data = response_data(resp)
            if api_success(resp) and isinstance(data, dict):
                status = str(data.get("orderStatus", "")).lower()
                if status == "filled":
                    return data
                if status in {"canceled", "cancelled", "rejected", "failed"}:
                    return data
        except Exception:
            pass
        time.sleep(0.6)
    return None


def find_exchange_position(client: Any, symbol: str, side: str) -> dict[str, Any] | None:
    expected = "long" if side == "LONG" else "short"
    rows = fetch_positions(client, symbol)
    matches = [p for p in rows if abs(safe_float(p.get("total"))) > 0]
    for p in matches:
        if str(p.get("posSide", "")).lower() == expected:
            return p
    if len(matches) == 1:
        return matches[0]
    return None


def place_open_order(
    client: Any,
    settings: Settings,
    candidate: Candidate,
    instrument: Instrument,
    equity: float,
    hold_mode: str,
) -> ManagedPosition:
    qty = calculate_qty(equity, candidate.entry_reference, settings, instrument)
    if qty <= 0:
        raise RuntimeError(f"calculated quantity invalid for {candidate.symbol}")
    client_oid = unique_client_oid("LW1O", candidate.symbol)
    payload = opening_payload(candidate, qty, instrument, settings, hold_mode, client_oid)
    resp = require_success(client_post(client, "/api/v3/trade/place-order", payload), f"open {candidate.symbol}")
    data = response_data(resp) if isinstance(response_data(resp), dict) else {}
    order_id = str(data.get("orderId", ""))
    fill = wait_for_fill(client, order_id, client_oid)
    exchange_position = find_exchange_position(client, candidate.symbol, candidate.side)
    if exchange_position is None:
        raise RuntimeError(f"order accepted but position not confirmed: {resp}; fill={fill}")
    entry_price = safe_float((fill or {}).get("avgPrice"), safe_float(exchange_position.get("avgPrice"), candidate.entry_reference))
    actual_qty = safe_float(exchange_position.get("total"), float(qty))
    if entry_price <= 0 or actual_qty <= 0:
        raise RuntimeError(f"invalid confirmed position: {exchange_position}")
    initial_r = abs(entry_price - candidate.stop_price)
    return ManagedPosition(
        symbol=candidate.symbol,
        profile=candidate.profile,
        side=candidate.side,
        setup=candidate.setup,
        qty=actual_qty,
        entry_price=entry_price,
        entry_ts=iso(),
        order_id=order_id,
        client_oid=client_oid,
        initial_stop=candidate.stop_price,
        software_stop=candidate.stop_price,
        emergency_tp=candidate.emergency_tp_price,
        initial_r=initial_r,
        best_price=entry_price,
        entry_equity=equity,
        score=candidate.score,
        hold_mode=hold_mode,
        signal_bar_ts=candidate.signal_bar_ts,
        reasons=candidate.reasons,
    )


def close_position(
    client: Any,
    settings: Settings,
    managed: ManagedPosition,
    reason: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    instrument = fetch_instrument(client, managed.symbol)
    exchange = find_exchange_position(client, managed.symbol, managed.side)
    if exchange is None:
        return reconcile_exchange_closed(client, settings, managed, reason="exchange_already_flat", state=state)
    qty = quantize_step(safe_float(exchange.get("total"), managed.qty), instrument.quantity_step, "down")
    if qty <= 0:
        raise RuntimeError(f"close qty invalid: {exchange}")
    client_oid = unique_client_oid("LW1C", managed.symbol)
    payload = closing_payload(managed, qty, settings, client_oid)
    resp = require_success(client_post(client, "/api/v3/trade/place-order", payload), f"close {managed.symbol}")
    data = response_data(resp) if isinstance(response_data(resp), dict) else {}
    order_id = str(data.get("orderId", ""))
    fill = wait_for_fill(client, order_id, client_oid)
    exit_price = safe_float((fill or {}).get("avgPrice"))
    if exit_price <= 0:
        exit_price = fetch_ticker(client, managed.symbol).mark
    return finalize_trade(settings, managed, exit_price, reason, state, order_id)


def trade_pnl_pct(managed: ManagedPosition, exit_price: float) -> float:
    raw = pct_change(exit_price, managed.entry_price)
    return raw if managed.side == "LONG" else -raw


def finalize_trade(
    settings: Settings,
    managed: ManagedPosition,
    exit_price: float,
    reason: str,
    state: dict[str, Any],
    exit_order_id: str = "",
) -> dict[str, Any]:
    pnl_price_pct = trade_pnl_pct(managed, exit_price)
    pnl_equity_pct_approx = pnl_price_pct * (settings.entry_margin_pct / 100.0 * settings.leverage)
    hold_minutes = (now_utc() - datetime.fromisoformat(managed.entry_ts)).total_seconds() / 60.0
    result = {
        "entry_ts": managed.entry_ts,
        "exit_ts": iso(),
        "symbol": managed.symbol,
        "profile": managed.profile,
        "side": managed.side,
        "setup": managed.setup,
        "score": managed.score,
        "qty": managed.qty,
        "entry_price": managed.entry_price,
        "exit_price": exit_price,
        "initial_stop": managed.initial_stop,
        "software_stop": managed.software_stop,
        "emergency_tp": managed.emergency_tp,
        "price_pnl_pct": round(pnl_price_pct, 6),
        "equity_pnl_pct_approx": round(pnl_equity_pct_approx, 6),
        "hold_minutes": round(hold_minutes, 2),
        "exit_reason": reason,
        "entry_order_id": managed.order_id,
        "exit_order_id": exit_order_id,
    }
    append_csv(settings.trades_path, result)
    append_jsonl(settings.events_path, {"event": "trade_closed", "trade": result})
    state["managed_position"] = None
    state.setdefault("cooldown_until", {})[managed.symbol] = iso(now_utc() + timedelta(minutes=settings.cooldown_minutes))
    if pnl_price_pct < 0:
        state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1
    else:
        state["consecutive_losses"] = 0
    notify(
        f"🔚 <b>Larry Core 청산</b>\n{managed.symbol} {managed.side}\n사유: {reason}\n"
        f"진입 {managed.entry_price:.6g} → 청산 {exit_price:.6g}\n가격손익 {pnl_price_pct:+.3f}%",
        settings,
    )
    return result


def reconcile_exchange_closed(
    client: Any,
    settings: Settings,
    managed: ManagedPosition,
    reason: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    # TP/SL may have closed the position while the process was sleeping.  The
    # current mark is an estimate; equity guards use actual account equity.
    exit_price = fetch_ticker(client, managed.symbol).mark
    return finalize_trade(settings, managed, exit_price, reason, state)


# ---------------------------------------------------------------------------
# Position management: bailout + price-point trail
# ---------------------------------------------------------------------------


def side_profit(managed: ManagedPosition, price: float) -> float:
    return price - managed.entry_price if managed.side == "LONG" else managed.entry_price - price


def update_price_point_trail(
    managed: ManagedPosition,
    cfg: dict[str, Any],
    bars15: Sequence[Bar],
    mark: float,
) -> tuple[float, bool, dict[str, Any]]:
    c15 = completed_bars(bars15, "15m")
    if len(c15) < 5 or managed.initial_r <= 0:
        return managed.software_stop, managed.trail_active, {}
    profit_r = side_profit(managed, mark) / managed.initial_r
    best = max(managed.best_price, mark) if managed.side == "LONG" else min(managed.best_price, mark)
    managed.best_price = best
    start_r = float(cfg.get("trail_start_r", 1.0))
    if profit_r < start_r and not managed.trail_active:
        return managed.software_stop, False, {"profit_r": profit_r}
    atr15 = atr_value(c15, 14)
    buffer_atr = float(cfg.get("trail_buffer_atr15", 0.10))
    lookback = int(cfg.get("trail_price_point_bars", 2))
    if managed.side == "LONG":
        price_point = min(b.low for b in c15[-lookback:]) - buffer_atr * atr15
        breakeven = managed.entry_price * (1.0 + float(cfg.get("breakeven_fee_buffer_pct", 0.15)) / 100.0)
        new_stop = max(managed.software_stop, price_point, breakeven)
    else:
        price_point = max(b.high for b in c15[-lookback:]) + buffer_atr * atr15
        breakeven = managed.entry_price * (1.0 - float(cfg.get("breakeven_fee_buffer_pct", 0.15)) / 100.0)
        new_stop = min(managed.software_stop, price_point, breakeven)
    return new_stop, True, {"profit_r": profit_r, "price_point": price_point, "breakeven": breakeven}


def bailout_key(dt: datetime, minutes: int) -> str:
    n = dt.astimezone(UTC)
    bucket = (n.minute // minutes) * minutes
    return n.replace(minute=bucket, second=0, microsecond=0).isoformat()


def bailout_due(managed: ManagedPosition, cfg: dict[str, Any], mark: float) -> tuple[bool, str]:
    entered = datetime.fromisoformat(managed.entry_ts)
    held = (now_utc() - entered).total_seconds() / 60.0
    min_hold = float(cfg.get("bailout_min_hold_minutes", 120 if managed.profile == "crypto" else 60))
    if held < min_hold or managed.trail_active:
        return False, ""
    interval = int(cfg.get("bailout_interval_minutes", 60 if managed.profile == "crypto" else 30))
    key = bailout_key(now_utc(), interval)
    if key == managed.last_bailout_key:
        return False, ""
    managed.last_bailout_key = key
    fee_buffer_pct = float(cfg.get("bailout_min_profit_pct", 0.18))
    pnl = trade_pnl_pct(managed, mark)
    if pnl >= fee_buffer_pct:
        return True, f"LARRY_BAILOUT_FIRST_PROFITABLE_{interval}M_OPEN"
    return False, ""


def manage_position(client: Any, settings: Settings, state: dict[str, Any]) -> dict[str, Any]:
    raw = state.get("managed_position")
    if not isinstance(raw, dict):
        return {"status": "flat"}
    managed = ManagedPosition(**raw)
    cfg = settings.symbols.get(managed.symbol) or {}
    exchange = find_exchange_position(client, managed.symbol, managed.side)
    if exchange is None:
        result = reconcile_exchange_closed(client, settings, managed, "EXCHANGE_TP_SL_OR_MANUAL", state)
        return {"status": "closed", "result": result}

    ticker = fetch_ticker(client, managed.symbol)
    mark = ticker.mark
    managed.best_price = max(managed.best_price, mark) if managed.side == "LONG" else min(managed.best_price, mark)

    if managed.profile == "stock" and stock_force_flat_due(now_utc(), str(cfg.get("force_flat_ny", "15:55"))):
        result = close_position(client, settings, managed, "STOCK_RTH_FORCE_FLAT", state)
        return {"status": "closed", "result": result}

    max_hold = float(cfg.get("max_hold_minutes", 2_160 if managed.profile == "crypto" else 390))
    held = (now_utc() - datetime.fromisoformat(managed.entry_ts)).total_seconds() / 60.0
    if held >= max_hold:
        result = close_position(client, settings, managed, "MAX_HOLD", state)
        return {"status": "closed", "result": result}

    bars15 = fetch_candles(client, managed.symbol, "15m", 120)
    new_stop, trail_active, trail_diag = update_price_point_trail(managed, cfg, bars15, mark)
    managed.software_stop = new_stop
    managed.trail_active = trail_active

    hit_software_stop = (managed.side == "LONG" and mark <= managed.software_stop) or (
        managed.side == "SHORT" and mark >= managed.software_stop
    )
    if hit_software_stop:
        result = close_position(client, settings, managed, "WILLSTOP_PRICE_POINT_TRAIL", state)
        return {"status": "closed", "result": result}

    due, reason = bailout_due(managed, cfg, mark)
    if due:
        result = close_position(client, settings, managed, reason, state)
        return {"status": "closed", "result": result}

    state["managed_position"] = asdict(managed)
    return {
        "status": "managed",
        "symbol": managed.symbol,
        "side": managed.side,
        "mark": mark,
        "entry": managed.entry_price,
        "software_stop": managed.software_stop,
        "trail_active": managed.trail_active,
        "profit_r": side_profit(managed, mark) / managed.initial_r if managed.initial_r > 0 else 0.0,
        "trail": trail_diag,
    }


# ---------------------------------------------------------------------------
# State, guards, doctor, arming
# ---------------------------------------------------------------------------


def default_state() -> dict[str, Any]:
    return {
        "version": VERSION,
        "day_key": "",
        "day_start_equity": 0.0,
        "week_key": "",
        "week_start_equity": 0.0,
        "consecutive_losses": 0,
        "managed_position": None,
        "cooldown_until": {},
        "last_heartbeat_ts": "",
        "last_cycle_ts": "",
        "last_error": "",
    }


def load_state(settings: Settings) -> dict[str, Any]:
    state = read_json(settings.state_path, default_state())
    if not isinstance(state, dict):
        state = default_state()
    merged = default_state()
    merged.update(state)
    return merged


def refresh_equity_baselines(state: dict[str, Any], equity: float) -> None:
    dk = day_key()
    wk = week_key()
    if state.get("day_key") != dk or safe_float(state.get("day_start_equity")) <= 0:
        state["day_key"] = dk
        state["day_start_equity"] = equity
        state["consecutive_losses"] = 0
    if state.get("week_key") != wk or safe_float(state.get("week_start_equity")) <= 0:
        state["week_key"] = wk
        state["week_start_equity"] = equity


def drawdown_pct(equity: float, baseline: float) -> float:
    if baseline <= 0:
        return 0.0
    return max(0.0, (baseline - equity) / baseline * 100.0)


def arm_valid(settings: Settings) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not bool_env("LARRY_V1_LIVE_ENABLED", False):
        reasons.append("LARRY_V1_LIVE_ENABLED=false")
    payload = read_json(settings.arm_path, {})
    if not isinstance(payload, dict) or payload.get("phrase") != ARM_PHRASE:
        reasons.append("arm file missing/invalid")
    if payload.get("risk_phrase") != RISK_PHRASE:
        reasons.append("risk acknowledgement missing")
    if payload.get("no_withdraw") != NO_WITHDRAW_PHRASE:
        reasons.append("no-withdraw acknowledgement missing")
    if payload.get("ip_whitelist") != IP_WHITELIST_PHRASE:
        reasons.append("IP-whitelist acknowledgement missing")
    return not reasons, reasons


def cooldown_active(state: dict[str, Any], symbol: str) -> bool:
    raw = (state.get("cooldown_until") or {}).get(symbol)
    if not raw:
        return False
    try:
        return datetime.fromisoformat(str(raw)) > now_utc()
    except Exception:
        return False


def guard_reasons(settings: Settings, state: dict[str, Any], equity: float, positions: list[dict[str, Any]], orders: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    day_dd = drawdown_pct(equity, safe_float(state.get("day_start_equity"), equity))
    week_dd = drawdown_pct(equity, safe_float(state.get("week_start_equity"), equity))
    if day_dd >= settings.max_daily_loss_pct:
        reasons.append(f"daily drawdown {day_dd:.2f}% >= {settings.max_daily_loss_pct:.2f}%")
    if week_dd >= settings.max_weekly_drawdown_pct:
        reasons.append(f"weekly drawdown {week_dd:.2f}% >= {settings.max_weekly_drawdown_pct:.2f}%")
    if int(state.get("consecutive_losses", 0)) >= settings.max_consecutive_losses:
        reasons.append(f"consecutive losses {state.get('consecutive_losses')}")
    if len(positions) >= settings.max_open_positions:
        reasons.append(f"open positions {len(positions)} >= max {settings.max_open_positions}")
    # Opening orders create duplicate-order risk. Position TP/SL strategy orders are queried separately.
    non_reduce = [o for o in orders if str(o.get("reduceOnly", "NO")).upper() != "YES"]
    if non_reduce:
        reasons.append(f"unfilled opening orders={len(non_reduce)}")
    return reasons


def doctor(settings: Settings, prearm: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {
        "version": VERSION,
        "root": str(ROOT),
        "config": str(settings.config_path),
        "prearm": prearm,
        "errors": [],
        "warnings": [],
        "symbols": {},
    }
    try:
        client = make_client()
        assets = fetch_account_assets(client)
        positions = nonzero_positions(client)
        orders = fetch_open_orders(client)
        strategies = fetch_strategy_orders_best_effort(client)
        account_settings = fetch_account_settings(client)
        hold_mode = infer_hold_mode(account_settings, positions)
        report["account"] = {
            "equity": account_equity(assets),
            "available_usdt": account_available_usdt(assets),
            "imr": safe_float(assets.get("imr")),
            "position_value": safe_float(assets.get("positionValue")),
            "hold_mode": hold_mode,
            "positions": len(positions),
            "open_orders": len(orders),
            "strategy_orders": len(strategies),
        }
        if hold_mode == "unknown":
            report["errors"].append("account hold mode could not be determined")
        if prearm and positions:
            report["errors"].append("existing position must be closed before arming")
        if prearm and orders:
            report["errors"].append("existing open order must be cleared before arming")
        for symbol, cfg in settings.symbols.items():
            if not bool(cfg.get("enabled", True)):
                continue
            try:
                inst = fetch_instrument(client, symbol)
                tick = fetch_ticker(client, symbol)
                errors = validate_instrument_for_profile(inst, str(cfg.get("profile", "crypto")), settings.leverage)
                report["symbols"][symbol] = {
                    "status": inst.status,
                    "symbol_type": inst.symbol_type,
                    "is_reality": inst.is_reality,
                    "min_leverage": inst.min_leverage,
                    "max_leverage": inst.max_leverage,
                    "price_step": str(inst.price_step),
                    "qty_step": str(inst.quantity_step),
                    "min_order_qty": str(inst.min_order_qty),
                    "min_order_amount": str(inst.min_order_amount),
                    "price": tick.last,
                    "spread_pct": tick.spread_pct,
                    "funding": tick.funding,
                    "errors": errors,
                }
                report["errors"].extend(f"{symbol}: {x}" for x in errors)
            except Exception as exc:
                report["symbols"][symbol] = {"error": str(exc)}
                report["errors"].append(f"{symbol}: {exc}")
        armed, arm_reasons = arm_valid(settings)
        report["live_armed"] = armed
        report["arm_reasons"] = arm_reasons
    except Exception as exc:
        report["errors"].append(str(exc))
        report["traceback"] = traceback.format_exc()
    report["ok"] = not report["errors"]
    return report


def write_arm_file(settings: Settings, args: Sequence[str]) -> dict[str, Any]:
    expected = [ARM_PHRASE, RISK_PHRASE, NO_WITHDRAW_PHRASE, IP_WHITELIST_PHRASE]
    if list(args) != expected:
        raise RuntimeError("arming phrases do not match exactly")
    payload = {
        "version": VERSION,
        "phrase": ARM_PHRASE,
        "risk_phrase": RISK_PHRASE,
        "no_withdraw": NO_WITHDRAW_PHRASE,
        "ip_whitelist": IP_WHITELIST_PHRASE,
        "armed_at": iso(),
        "config_sha256": hashlib.sha256(settings.config_path.read_bytes()).hexdigest(),
    }
    atomic_write_json(settings.arm_path, payload)
    return payload


def disarm(settings: Settings) -> None:
    try:
        settings.arm_path.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main cycle
# ---------------------------------------------------------------------------


def maybe_heartbeat(settings: Settings, state: dict[str, Any], message: str) -> None:
    raw = state.get("last_heartbeat_ts")
    last = datetime.fromisoformat(raw) if raw else datetime.min.replace(tzinfo=UTC)
    if (now_utc() - last).total_seconds() >= settings.heartbeat_minutes * 60:
        notify(f"❤️ <b>Larry Williams Core v{VERSION}</b>\n{message}", settings)
        state["last_heartbeat_ts"] = iso()


def run_once(settings: Settings, execute_live: bool = True) -> dict[str, Any]:
    client = make_client()
    state = load_state(settings)
    assets = fetch_account_assets(client)
    equity = account_equity(assets)
    if equity <= 0:
        raise RuntimeError(f"invalid account equity: {assets}")
    refresh_equity_baselines(state, equity)

    positions = nonzero_positions(client)
    orders = fetch_open_orders(client)
    account_settings = fetch_account_settings(client)
    hold_mode = infer_hold_mode(account_settings, positions)

    # Reconcile/manage an existing bot position first.
    managed_result = manage_position(client, settings, state)
    if managed_result.get("status") in {"managed", "closed"}:
        state["last_cycle_ts"] = iso()
        state["last_error"] = ""
        atomic_write_json(settings.state_path, state)
        maybe_heartbeat(settings, state, f"포지션 관리 중: {managed_result}")
        atomic_write_json(settings.state_path, state)
        return {"mode": "MANAGE", "equity": equity, "managed": managed_result}

    # A position not recorded by this engine is never adopted automatically.
    if positions and not state.get("managed_position"):
        raise RuntimeError(f"unmanaged exchange position detected: {positions}")

    guards = guard_reasons(settings, state, equity, positions, orders)
    armed, arm_reasons = arm_valid(settings)
    if execute_live and not armed:
        guards.extend(arm_reasons)
    if hold_mode == "unknown":
        guards.append("unknown hold mode")

    diagnostics: list[dict[str, Any]] = []
    candidates: list[Candidate] = []
    for symbol, cfg in settings.symbols.items():
        if not bool(cfg.get("enabled", True)):
            continue
        if cooldown_active(state, symbol):
            diagnostics.append({"symbol": symbol, "blocked": ["cooldown"]})
            continue
        try:
            candidate, diag = evaluate_symbol(client, settings, symbol, cfg)
            diagnostics.append(diag)
            if candidate and candidate.score >= max(settings.signal_threshold, float(cfg.get("signal_threshold", 0))):
                candidates.append(candidate)
        except Exception as exc:
            diagnostics.append({"symbol": symbol, "error": str(exc)})
            log(f"evaluate {symbol} error: {exc}", settings)

    candidates.sort(key=lambda c: (c.score, -c.account_risk_pct), reverse=True)
    chosen = candidates[0] if candidates else None
    action: dict[str, Any] | None = None

    if chosen and not guards:
        instrument = fetch_instrument(client, chosen.symbol)
        if execute_live:
            position = place_open_order(client, settings, chosen, instrument, equity, hold_mode)
            state["managed_position"] = asdict(position)
            action = {"type": "OPEN", "position": asdict(position), "candidate": asdict(chosen)}
            append_jsonl(settings.events_path, {"event": "trade_opened", **action})
            notify(
                f"🚀 <b>Larry Core 실진입</b>\n{position.symbol} {position.side} / {position.setup}\n"
                f"점수 {position.score:.1f}\n진입 {position.entry_price:.6g}\n"
                f"초기SL {position.initial_stop:.6g} / 비상TP {position.emergency_tp:.6g}\n"
                f"증거금 {settings.entry_margin_pct:.0f}% / Cross {settings.leverage}x",
                settings,
            )
        else:
            action = {"type": "SIGNAL_ONLY", "candidate": asdict(chosen)}
    state["last_cycle_ts"] = iso()
    state["last_error"] = ""
    atomic_write_json(settings.state_path, state)
    summary = {
        "mode": "LIVE" if execute_live else "OBSERVE",
        "equity": equity,
        "available": account_available_usdt(assets),
        "hold_mode": hold_mode,
        "guards": sorted(set(guards)),
        "candidates": [asdict(c) for c in candidates],
        "chosen": asdict(chosen) if chosen else None,
        "action": action,
        "diagnostics": diagnostics,
    }
    maybe_heartbeat(
        settings,
        state,
        f"상태: {'BLOCKED' if guards else 'READY'} / equity {equity:.2f}\n"
        f"후보: {chosen.symbol + ' ' + chosen.side if chosen else '없음'}",
    )
    atomic_write_json(settings.state_path, state)
    return summary


def setup_account(settings: Settings) -> dict[str, Any]:
    client = make_client()
    positions = nonzero_positions(client)
    orders = fetch_open_orders(client)
    if positions or orders:
        raise RuntimeError("setup-account requires zero positions and zero open orders")
    results: dict[str, Any] = {}
    for symbol, cfg in settings.symbols.items():
        if not bool(cfg.get("enabled", True)):
            continue
        instrument = fetch_instrument(client, symbol)
        errors = validate_instrument_for_profile(instrument, str(cfg.get("profile", "crypto")), settings.leverage)
        if errors:
            raise RuntimeError(f"{symbol} instrument validation failed: {errors}")
        results[symbol] = set_leverage(client, symbol, settings.leverage, settings.margin_mode)
    return {"ok": True, "leverage": settings.leverage, "margin_mode": settings.margin_mode, "results": results}


# ---------------------------------------------------------------------------
# Self-tests and CLI
# ---------------------------------------------------------------------------


def self_test() -> dict[str, Any]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        Bar(
            ts=int((base + timedelta(minutes=15 * i)).timestamp() * 1000),
            open=100 + i * 0.1,
            high=101 + i * 0.1,
            low=99 + i * 0.1,
            close=100.5 + i * 0.1,
            volume=100 + i,
        )
        for i in range(80)
    ]
    tests = {
        "pct_change": abs(pct_change(110, 100) - 10) < 1e-9,
        "qty_round": quantize_step(1.239, Decimal("0.01"), "down") == Decimal("1.23"),
        "price_ceil": quantize_step(1.231, Decimal("0.01"), "ceil") == Decimal("1.24"),
        "atr_positive": atr_value(bars, 14) > 0,
        "wpr_range": -100 <= williams_r(bars, 14) <= 0,
        "uo_range": 0 <= ultimate_oscillator(bars) <= 100,
        "thanksgiving": nth_weekday(2026, 11, 3, 4).isoformat() == "2026-11-26",
        "good_friday": easter_sunday(2026).isoformat() == "2026-04-05",
        "stock_holiday": date(2026, 7, 3) in us_equity_holidays(2026),
        "risk_math": abs((30 / 100 * 5) - 1.5) < 1e-12,
    }
    return {"ok": all(tests.values()), "version": VERSION, "tests": tests}


def cmd_once(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    print(json.dumps(run_once(settings, execute_live=not args.observe), ensure_ascii=False, indent=2, default=str))


def cmd_loop(args: argparse.Namespace) -> None:
    settings = load_settings(args.config)
    log(f"Larry Williams Core v{VERSION} loop start / live={not args.observe}", settings)
    notify(
        f"🟢 <b>Larry Williams Core v{VERSION} 시작</b>\n"
        f"모드: {'OBSERVE' if args.observe else 'LIVE-GATED'}\n"
        f"ETHUSDT + SKHYUSDT / Cross {settings.leverage}x / 증거금 {settings.entry_margin_pct:.0f}%",
        settings,
    )
    while True:
        try:
            result = run_once(settings, execute_live=not args.observe)
            print(json.dumps({"ts": iso(), "mode": result.get("mode"), "guards": result.get("guards"), "action": result.get("action")}, ensure_ascii=False, default=str), flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            state = load_state(settings)
            state["last_error"] = str(exc)
            state["last_cycle_ts"] = iso()
            atomic_write_json(settings.state_path, state)
            log(f"cycle error: {exc}\n{traceback.format_exc()}", settings)
            notify(f"⚠️ <b>Larry Core 오류</b>\n{exc}", settings)
        time.sleep(settings.loop_seconds)


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
    print(json.dumps({"ok": True, "arm": payload, "env_required": "LARRY_V1_LIVE_ENABLED=true"}, ensure_ascii=False, indent=2))


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
    parser = argparse.ArgumentParser(description="Larry Williams Core v1.0 live engine for Bitget UTA")
    parser.add_argument("--config", default=None, help="config path")
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("once")
    p.add_argument("--observe", action="store_true", help="calculate only; never place orders")
    p.set_defaults(func=cmd_once)

    p = subs.add_parser("loop")
    p.add_argument("--observe", action="store_true", help="calculate only; never place orders")
    p.set_defaults(func=cmd_loop)

    p = subs.add_parser("doctor")
    p.add_argument("--prearm", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = subs.add_parser("setup-account")
    p.set_defaults(func=cmd_setup)

    p = subs.add_parser("arm")
    p.add_argument("phrases", nargs=4)
    p.set_defaults(func=cmd_arm)

    p = subs.add_parser("disarm")
    p.set_defaults(func=cmd_disarm)

    p = subs.add_parser("status")
    p.set_defaults(func=cmd_status)

    p = subs.add_parser("self-test")
    p.set_defaults(func=cmd_self_test)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
