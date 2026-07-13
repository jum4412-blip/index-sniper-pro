from __future__ import annotations

import argparse
import csv
import datetime as dt
import fcntl
import json
import math
import os
import re
import statistics
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

from index_sniper.exchange.bitget_uta import BitgetUTAClient, OrderIntent
from index_sniper.strategy.indicators import Candle, atr, ema, parse_candles

try:
    from index_sniper.telegram.bot import TelegramBot
except Exception:  # pragma: no cover - Telegram is optional
    TelegramBot = None  # type: ignore[assignment]


def _api_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Return list-like rows from Bitget UTA responses."""
    payload = response.get("data") if isinstance(response, dict) else None
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("list", "rows", "result", "data"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return [payload]
    return []


def row_qty_decimal(row: dict[str, Any]) -> Decimal:
    for key in ("total", "size", "qty", "positionQty", "available"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return abs(Decimal(str(value)))
            except Exception:
                continue
    return Decimal("0")


def row_avg_price(row: dict[str, Any]) -> float:
    for key in ("avgPrice", "averageOpenPrice", "openPriceAvg", "entryPrice"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except Exception:
                continue
    return 0.0


def row_side(row: dict[str, Any]) -> str:
    value = str(row.get("posSide") or row.get("holdSide") or row.get("positionSide") or "").strip().lower()
    if value in {"long", "short"}:
        return value
    side = str(row.get("side") or "").strip().lower()
    return {"buy": "long", "sell": "short"}.get(side, side)


def open_positions(response: dict[str, Any], symbol: str | None = None) -> list[dict[str, Any]]:
    wanted = str(symbol or "").upper()
    rows: list[dict[str, Any]] = []
    for row in _api_rows(response):
        if wanted and str(row.get("symbol") or "").upper() != wanted:
            continue
        if row_qty_decimal(row) > 0:
            rows.append(row)
    return rows


def extract_instrument(response: dict[str, Any], symbol: str) -> dict[str, Any]:
    wanted = symbol.upper()
    for row in _api_rows(response):
        if str(row.get("symbol") or "").upper() == wanted:
            return row
    raise RuntimeError(f"instrument metadata missing for {symbol}")


def extract_usdt_equity_available(response: dict[str, Any]) -> tuple[float, float]:
    rows = _api_rows(response)
    candidates = [row for row in rows if str(row.get("coin") or row.get("marginCoin") or row.get("asset") or "").upper() == "USDT"]
    if not candidates and len(rows) == 1:
        candidates = rows
    if not candidates:
        raise RuntimeError("USDT account asset row missing")
    row = candidates[0]
    def first_number(keys: tuple[str, ...]) -> float:
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except Exception:
                    continue
        return 0.0
    equity = first_number(("accountEquity", "equity", "usdValue", "walletBalance", "balance"))
    available = first_number(("available", "availableBalance", "availableToWithdraw", "maxTransferOut"))
    if equity <= 0:
        raise RuntimeError(f"invalid USDT equity: {equity}")
    return equity, max(0.0, available)


def format_price(value: float, instrument: dict[str, Any]) -> str:
    d = Decimal(str(value))
    step = Decimal(str(instrument.get("priceMultiplier") or instrument.get("priceStep") or instrument.get("tickSize") or "0"))
    if step > 0:
        d = (d / step).to_integral_value(rounding=ROUND_HALF_UP) * step
    precision = int(instrument.get("pricePrecision") or instrument.get("pricePlace") or 0)
    quant = Decimal("1") if precision <= 0 else Decimal("1") / (Decimal(10) ** precision)
    d = d.quantize(quant, rounding=ROUND_HALF_UP)
    text = format(d, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


VERSION = "6.3.2"
CATEGORY = "USDT-FUTURES"
MARGIN_COIN = "USDT"
LIVE_PHRASE = "START_V63_DUAL_LIVE_5X_BTC_ETH"
ROTATION_PHRASE = "I_ROTATED_ALL_EXPOSED_KEYS"
NO_WITHDRAW_PHRASE = "API_HAS_NO_WITHDRAW_PERMISSION"
IP_WHITELIST_PHRASE = "API_IP_WHITELISTED"
PANIC_PHRASE = "FLAT_BTC_ETH_NOW"
DEFAULT_CONFIG = "config/v63_dual_live.json"
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1H": 3_600_000,
    "4H": 14_400_000,
}


@dataclass(frozen=True)
class SymbolConfig:
    supply_proxy: float
    stop_atr_mult: float
    min_stop_pct: float
    max_stop_pct: float


@dataclass(frozen=True)
class Config:
    symbols: dict[str, SymbolConfig]
    loop_seconds: int
    leverage: int
    margin_mode: str
    bucket_margin_ratio: float
    risk_per_trade_pct: float
    both_positions_risk_pct_each: float
    max_total_initial_margin_pct: float
    max_total_notional_equity_ratio: float
    max_open_positions: int
    max_new_positions_per_cycle: int
    max_entries_per_symbol_per_day: int
    daily_loss_block_pct: float
    weekly_loss_block_pct: float
    max_drawdown_block_pct: float
    cooldown_after_loss_minutes: int
    cooldown_after_two_losses_minutes: int
    trend_regime_threshold: float
    trend_entry_threshold: float
    impulse_entry_threshold: float
    min_edge: float
    impulse_min_edge: float
    impulse_volume_z: float
    impulse_move_atr: float
    impulse_confirm_cycles: int
    anti_chase_atr: float
    impulse_anti_chase_atr: float
    oi_confirm_pct: float
    event_caution_risk: float
    event_high_risk: float
    event_hard_block_risk: float
    event_state_max_age_minutes: int
    event_stale_size_mult: float
    event_caution_size_mult: float
    event_high_size_mult: float
    news_bias_max: float
    news_require_providers: int
    news_fresh_minutes: int
    tp_r_trend: float
    tp_r_impulse: float
    no_followthrough_minutes: int
    no_followthrough_mfe_r: float
    time_stop_hours_trend: float
    time_stop_hours_impulse: float
    trail_activate_r: float
    trail_atr_mult: float
    heartbeat_minutes: int
    position_absence_confirm_cycles: int
    require_dedicated_account: bool
    data_dir: Path
    log_path: Path
    state_path: Path
    snapshots_path: Path
    trades_path: Path
    events_path: Path
    v62_snapshots_path: Path
    raw_events_path: Path


@dataclass
class EventOverlay:
    level: str = "NORMAL"
    risk: float = 0.0
    size_mult: float = 1.0
    directional_bias: float = 0.0
    providers: int = 0
    hard_block: bool = False
    stale: bool = False
    reasons: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["reasons"] = list(self.reasons or [])
        return out


@dataclass
class Signal:
    symbol: str
    ts: str
    price: float
    atr1h: float
    atr1h_pct: float
    ema20_1h: float
    ema50_1h: float
    ema20_4h: float
    ema50_4h: float
    regime_long: float
    regime_short: float
    trigger_long: float
    trigger_short: float
    edge: float
    side: str
    mode: str
    entry_ready: bool
    volume_z5: float
    momentum_atr: float
    ret_1h_pct: float
    breakout_long: bool
    breakout_short: bool
    anti_chase_distance_atr: float
    price_response_efficiency: float
    close_location: float
    rejection_wick_ratio: float
    oi: float | None
    oi_15m_pct: float | None
    funding_rate: float | None
    turnover24h: float
    market_cap_proxy: float
    turnover_cap_ratio: float
    opportunity_score: float
    event: dict[str, Any]
    blockers: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            obj = json.loads(self.path.read_text(encoding="utf-8"))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    def save(self) -> None:
        self.data["updated_utc"] = utc_now().isoformat()
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_to_dt(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value) / 1000.0 if float(value) > 10_000_000_000 else float(value)
        return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new / old - 1.0) * 100.0


def load_dotenv_safely(path: Path) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(path, override=False)
        return
    except Exception:
        pass
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def tail_lines(path: Path, limit: int = 5000) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            lines = handle.readlines()
        return lines[-limit:]
    except Exception:
        return []


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")


def append_trade_csv(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "ts",
        "event",
        "symbol",
        "side",
        "mode",
        "qty",
        "entry_price",
        "exit_price",
        "stop_price",
        "take_profit_price",
        "net_profit",
        "r_multiple",
        "reason",
        "client_oid",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fields})


def load_config(path: Path) -> Config:
    raw = json.loads(path.read_text(encoding="utf-8"))
    config_version = str(raw.get("version") or "").strip()
    if config_version != VERSION:
        raise ValueError(f"config version {config_version or '-'} does not match engine {VERSION}")
    symbols: dict[str, SymbolConfig] = {}
    for symbol, item in (raw.get("symbols") or {}).items():
        symbols[str(symbol).upper()] = SymbolConfig(
            supply_proxy=safe_float(item.get("supply_proxy")),
            stop_atr_mult=safe_float(item.get("stop_atr_mult"), 1.2),
            min_stop_pct=safe_float(item.get("min_stop_pct"), 0.006),
            max_stop_pct=safe_float(item.get("max_stop_pct"), 0.012),
        )
    if set(symbols) != {"BTCUSDT", "ETHUSDT"}:
        raise ValueError("symbols must be exactly BTCUSDT and ETHUSDT")

    def p(name: str, default: str) -> Path:
        return DEFAULT_ROOT / str(raw.get(name, default))

    return Config(
        symbols=symbols,
        loop_seconds=int(raw.get("loop_seconds", 60)),
        leverage=int(raw.get("leverage", 5)),
        margin_mode=str(raw.get("margin_mode", "isolated")),
        bucket_margin_ratio=safe_float(raw.get("bucket_margin_ratio"), 0.50),
        risk_per_trade_pct=safe_float(raw.get("risk_per_trade_pct"), 0.60),
        both_positions_risk_pct_each=safe_float(raw.get("both_positions_risk_pct_each"), 0.40),
        max_total_initial_margin_pct=safe_float(raw.get("max_total_initial_margin_pct"), 40.0),
        max_total_notional_equity_ratio=safe_float(raw.get("max_total_notional_equity_ratio"), 2.0),
        max_open_positions=int(raw.get("max_open_positions", 2)),
        max_new_positions_per_cycle=int(raw.get("max_new_positions_per_cycle", 1)),
        max_entries_per_symbol_per_day=int(raw.get("max_entries_per_symbol_per_day", 3)),
        daily_loss_block_pct=safe_float(raw.get("daily_loss_block_pct"), 2.0),
        weekly_loss_block_pct=safe_float(raw.get("weekly_loss_block_pct"), 5.0),
        max_drawdown_block_pct=safe_float(raw.get("max_drawdown_block_pct"), 6.0),
        cooldown_after_loss_minutes=int(raw.get("cooldown_after_loss_minutes", 60)),
        cooldown_after_two_losses_minutes=int(raw.get("cooldown_after_two_losses_minutes", 240)),
        trend_regime_threshold=safe_float(raw.get("trend_regime_threshold"), 55.0),
        trend_entry_threshold=safe_float(raw.get("trend_entry_threshold"), 44.0),
        impulse_entry_threshold=safe_float(raw.get("impulse_entry_threshold"), 30.0),
        min_edge=safe_float(raw.get("min_edge"), 14.0),
        impulse_min_edge=safe_float(raw.get("impulse_min_edge"), 16.0),
        impulse_volume_z=safe_float(raw.get("impulse_volume_z"), 1.2),
        impulse_move_atr=safe_float(raw.get("impulse_move_atr"), 0.50),
        impulse_confirm_cycles=int(raw.get("impulse_confirm_cycles", 2)),
        anti_chase_atr=safe_float(raw.get("anti_chase_atr"), 1.25),
        impulse_anti_chase_atr=safe_float(raw.get("impulse_anti_chase_atr"), 1.55),
        oi_confirm_pct=safe_float(raw.get("oi_confirm_pct"), 0.08),
        event_caution_risk=safe_float(raw.get("event_caution_risk"), 30.0),
        event_high_risk=safe_float(raw.get("event_high_risk"), 55.0),
        event_hard_block_risk=safe_float(raw.get("event_hard_block_risk"), 75.0),
        event_state_max_age_minutes=int(raw.get("event_state_max_age_minutes", 20)),
        event_stale_size_mult=safe_float(raw.get("event_stale_size_mult"), 0.35),
        event_caution_size_mult=safe_float(raw.get("event_caution_size_mult"), 0.75),
        event_high_size_mult=safe_float(raw.get("event_high_size_mult"), 0.40),
        news_bias_max=safe_float(raw.get("news_bias_max"), 6.0),
        news_require_providers=int(raw.get("news_require_providers", 2)),
        news_fresh_minutes=int(raw.get("news_fresh_minutes", 90)),
        tp_r_trend=safe_float(raw.get("tp_r_trend"), 2.0),
        tp_r_impulse=safe_float(raw.get("tp_r_impulse"), 1.6),
        no_followthrough_minutes=int(raw.get("no_followthrough_minutes", 25)),
        no_followthrough_mfe_r=safe_float(raw.get("no_followthrough_mfe_r"), 0.25),
        time_stop_hours_trend=safe_float(raw.get("time_stop_hours_trend"), 8.0),
        time_stop_hours_impulse=safe_float(raw.get("time_stop_hours_impulse"), 4.0),
        trail_activate_r=safe_float(raw.get("trail_activate_r"), 1.10),
        trail_atr_mult=safe_float(raw.get("trail_atr_mult"), 0.85),
        heartbeat_minutes=int(raw.get("heartbeat_minutes", 60)),
        position_absence_confirm_cycles=int(raw.get("position_absence_confirm_cycles", 2)),
        require_dedicated_account=bool(raw.get("require_dedicated_account", True)),
        data_dir=p("data_dir", "data/v63_dual_live"),
        log_path=p("log_path", "logs/v63-dual-live.log"),
        state_path=p("state_path", "data/v63_dual_live/state.json"),
        snapshots_path=p("snapshots_path", "data/v63_dual_live/snapshots.jsonl"),
        trades_path=p("trades_path", "data/v63_dual_live/trades.csv"),
        events_path=p("events_path", "data/v63_dual_live/events.jsonl"),
        v62_snapshots_path=p("v62_snapshots_path", "data/v62_unified/snapshots.jsonl"),
        raw_events_path=p("raw_events_path", "data/event_risk_pro/events.jsonl"),
    )


def make_client() -> BitgetUTAClient:
    api_key = os.getenv("BITGET_API_KEY", "").strip()
    secret = (os.getenv("BITGET_SECRET_KEY") or os.getenv("BITGET_API_SECRET") or "").strip()
    passphrase = os.getenv("BITGET_PASSPHRASE", "").strip()
    if not api_key or not secret or not passphrase:
        raise RuntimeError("BITGET_API_KEY / BITGET_SECRET_KEY(or BITGET_API_SECRET) / BITGET_PASSPHRASE are required")
    return BitgetUTAClient(api_key=api_key, secret_key=secret, passphrase=passphrase, timeout=12)


def make_bot() -> Any | None:
    if TelegramBot is None:
        return None
    token = (os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id or not bool_env("V63_NOTIFY", True):
        return None
    return TelegramBot(token, chat_id)


def send(bot: Any | None, text: str) -> None:
    if bot is None:
        return
    try:
        bot.send(text)
    except Exception:
        pass



def security_audit(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    """Detect obvious plaintext credentials in Python source without exposing values."""
    findings: list[dict[str, Any]] = []
    assignment = re.compile(
        r"(?i)^\s*(TELEGRAM_TOKEN|TELEGRAM_BOT_TOKEN|API_KEY|BITGET_API_KEY|"
        r"SECRET_KEY|BITGET_SECRET_KEY|BITGET_API_SECRET|PASSWORD|PASSPHRASE|"
        r"BITGET_PASSPHRASE)\s*=\s*([\"'])([^\"']{8,})\2"
    )
    telegram_token = re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b")
    api_key_literal = re.compile(r"\bbg_[A-Za-z0-9]{20,}\b", re.I)
    skip_parts = {".git", ".venv", "venv", "__pycache__", "local_backups", "data", "logs", "research"}
    for file in root.rglob("*.py"):
        try:
            rel = file.relative_to(root)
        except Exception:
            rel = file
        if any(part in skip_parts for part in rel.parts):
            continue
        try:
            lines = file.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for idx, line in enumerate(lines, 1):
            m = assignment.search(line)
            kind: str | None = None
            if m and "getenv" not in line and "environ" not in line:
                value = m.group(3).strip().upper()
                placeholders = ("YOUR", "REDACTED", "CHANGE_ME", "CHANGEME", "EXAMPLE", "DUMMY", "TEST", "PLACEHOLDER")
                if not any(token in value for token in placeholders):
                    kind = m.group(1).upper()
            elif telegram_token.search(line):
                kind = "TELEGRAM_TOKEN_LITERAL"
            elif api_key_literal.search(line):
                kind = "BITGET_API_KEY_LITERAL"
            if kind:
                findings.append({"file": str(rel), "line": idx, "kind": kind})
    return {"ok": not findings, "findings": findings}


def live_is_armed(cfg: Config) -> tuple[bool, list[str]]:
    """Fail closed unless every independent local live confirmation is present."""
    reasons: list[str] = []
    if bool_env("V63_FORCE_SHADOW", False):
        reasons.append("V63_FORCE_SHADOW=true")
    if not bool_env("V63_LIVE_ENABLED", False):
        reasons.append("V63_LIVE_ENABLED=false")
    if os.getenv("V63_LIVE_CONFIRM", "").strip() != LIVE_PHRASE:
        reasons.append("live confirmation phrase missing")
    if os.getenv("V63_KEYS_ROTATED_CONFIRM", "").strip() != ROTATION_PHRASE:
        reasons.append("key rotation confirmation missing")
    if os.getenv("V63_NO_WITHDRAW_CONFIRM", "").strip() != NO_WITHDRAW_PHRASE:
        reasons.append("no-withdraw confirmation missing")
    if os.getenv("V63_IP_WHITELIST_CONFIRM", "").strip() != IP_WHITELIST_PHRASE:
        reasons.append("IP whitelist confirmation missing")
    audit = security_audit(DEFAULT_ROOT)
    if not audit["ok"]:
        reasons.append(f"plaintext credential source detected ({len(audit['findings'])})")
    arm_file = cfg.data_dir / "LIVE_ARMED"
    try:
        if arm_file.read_text(encoding="utf-8").strip() != LIVE_PHRASE:
            reasons.append("LIVE_ARMED file missing or invalid")
    except Exception:
        reasons.append("LIVE_ARMED file missing or invalid")
    if (cfg.data_dir / "PAUSE_NEW_ENTRIES").exists():
        reasons.append("PAUSE_NEW_ENTRIES exists")
    return not reasons, reasons


# ---------------------------------------------------------------------------
# Bitget payload parsing and safety checks
# ---------------------------------------------------------------------------


def payload_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    payload = data.get("data")
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("list", "rows", "result", "data"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [x for x in rows if isinstance(x, dict)]
        return [payload]
    return []


def symbol_config(settings_response: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    payload = settings_response.get("data") or {}
    if not isinstance(payload, dict):
        return None
    for row in payload.get("symbolConfigList") or []:
        if isinstance(row, dict) and str(row.get("symbol", "")).upper() == symbol.upper():
            return row
    return None


def hold_mode(settings_response: dict[str, Any]) -> str:
    payload = settings_response.get("data") or {}
    return str(payload.get("holdMode", "")) if isinstance(payload, dict) else ""


def open_orders(client: BitgetUTAClient, symbol: str) -> list[dict[str, Any]]:
    data = client.get("/api/v3/trade/unfilled-orders", {"category": CATEGORY, "symbol": symbol})
    return payload_rows(data)


def all_open_orders(client: BitgetUTAClient) -> list[dict[str, Any]]:
    data = client.get("/api/v3/trade/unfilled-orders", {"category": CATEGORY})
    return payload_rows(data)


def strategy_orders(client: BitgetUTAClient, symbol: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"category": CATEGORY, "type": "tpsl"}
    if symbol:
        params["symbol"] = symbol
    data = client.get("/api/v3/trade/unfilled-strategy-orders", params)
    return payload_rows(data)


def all_strategy_orders(client: BitgetUTAClient) -> list[dict[str, Any]]:
    return strategy_orders(client, None)


def cancel_strategy_order(client: BitgetUTAClient, row: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"category": CATEGORY}
    symbol = str(row.get("symbol") or "").upper()
    if symbol:
        payload["symbol"] = symbol
    order_id = row.get("orderId") or row.get("strategyOrderId") or row.get("planOrderId")
    client_oid = row.get("clientOid")
    if order_id not in (None, ""):
        payload["orderId"] = str(order_id)
    elif client_oid not in (None, ""):
        payload["clientOid"] = str(client_oid)
    else:
        return {"code": "LOCAL_MISSING_ID", "msg": "strategy order has no orderId/clientOid", "row": row}
    return client.post("/api/v3/trade/cancel-strategy-order", payload)


def cancel_all_strategy_orders(client: BitgetUTAClient, symbol: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        rows = strategy_orders(client, symbol)
    except Exception as exc:
        return [{"code": "LOCAL_QUERY_ERROR", "msg": str(exc)}]
    for row in rows:
        try:
            results.append(cancel_strategy_order(client, row))
        except Exception as exc:
            results.append({"code": "LOCAL_CANCEL_ERROR", "msg": str(exc), "row": row})
    return results


def all_current_positions(client: BitgetUTAClient) -> list[dict[str, Any]]:
    data = client.get("/api/v3/position/current-position", {"category": CATEGORY})
    rows = payload_rows(data)
    return [row for row in rows if row_qty_decimal(row) > 0]


def set_hold_mode(client: BitgetUTAClient, mode: str = "hedge_mode") -> dict[str, Any]:
    return client.post("/api/v3/account/set-hold-mode", {"holdMode": mode})


def set_leverage(client: BitgetUTAClient, symbol: str, leverage: int, margin_mode: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for side in ("long", "short"):
        payload = {
            "category": CATEGORY,
            "symbol": symbol,
            "leverage": str(leverage),
            "posSide": side,
            "marginMode": margin_mode,
        }
        results.append(client.post("/api/v3/account/set-leverage", payload))
    return results


def order_info(client: BitgetUTAClient, client_oid: str) -> dict[str, Any]:
    return client.get("/api/v3/trade/order-info", {"clientOid": client_oid})


def order_status_kind(row: dict[str, Any]) -> str:
    raw = str(row.get("orderStatus") or row.get("status") or row.get("state") or "").strip().lower()
    normalized = re.sub(r"[^a-z]", "", raw)
    if "partial" in normalized and "fill" in normalized:
        return "partial"
    if normalized in {"filled", "fullfill", "fullfilled", "fullyfilled"}:
        return "filled"
    if normalized in {"cancelled", "canceled", "rejected", "failed", "expired"}:
        return "terminal_failure"
    return "open_or_unknown"


def wait_for_order(client: BitgetUTAClient, client_oid: str, timeout_seconds: int = 12) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    latest: dict[str, Any] = {}
    while time.time() < deadline:
        try:
            latest = order_info(client, client_oid)
            rows = payload_rows(latest)
            row = rows[0] if rows else {}
            if order_status_kind(row) in {"filled", "partial", "terminal_failure"}:
                return latest
        except Exception:
            pass
        time.sleep(1.0)
    return latest


def order_adapter_diagnostics(client: BitgetUTAClient) -> dict[str, Any]:
    """Build, but never submit, a protected market-order payload."""
    try:
        intent = OrderIntent(
            symbol="BTCUSDT",
            side="buy",
            pos_side="long",
            qty="0.001",
            category=CATEGORY,
            margin_coin=MARGIN_COIN,
            margin_mode="isolated",
            order_type="market",
            reduce_only=False,
            client_oid="v63-adapter-selftest",
            take_profit="100000",
            stop_loss="50000",
            tp_trigger_by="mark",
            sl_trigger_by="mark",
            tp_order_type="market",
            sl_order_type="market",
        )
        payload = client.build_market_order_payload(intent)
        keys = sorted(str(k) for k in payload)
        lower = {str(k).lower(): v for k, v in payload.items()}
        required = {"symbol", "side", "posside", "qty", "category", "ordertype", "clientoid"}
        missing = sorted(k for k in required if k not in lower)
        has_tp = any("profit" in k or k in {"takeprofit", "tp"} for k in lower)
        has_sl = any("loss" in k or k in {"stoploss", "sl"} for k in lower)
        ok = not missing and has_tp and has_sl
        return {"ok": ok, "payload_keys": keys, "missing": missing, "has_tp": has_tp, "has_sl": has_sl}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "payload_keys": []}


def history_positions(client: BitgetUTAClient, symbol: str, start_ms: int) -> list[dict[str, Any]]:
    data = client.get(
        "/api/v3/position/history-position",
        {
            "category": CATEGORY,
            "symbol": symbol,
            "startTime": str(max(0, start_ms - 60_000)),
            "endTime": str(int(time.time() * 1000)),
            "limit": "20",
        },
    )
    rows = payload_rows(data)
    rows.sort(key=lambda x: safe_float(x.get("updatedTime")), reverse=True)
    return rows


def cancel_symbol_orders(client: BitgetUTAClient, symbol: str) -> dict[str, Any]:
    return client.post("/api/v3/trade/cancel-symbol-order", {"category": CATEGORY, "symbol": symbol})


def close_all_symbol(client: BitgetUTAClient, symbol: str, pos_side: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"category": CATEGORY, "symbol": symbol}
    if pos_side in {"long", "short"}:
        payload["posSide"] = pos_side
    return client.post("/api/v3/trade/close-positions", payload)


def protection_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    has_tp = False
    has_sl = False
    for row in rows:
        descriptor = " ".join(
            str(row.get(k) or "")
            for k in ("planType", "orderType", "strategyType", "tpslType", "type")
        ).lower()
        tp_values = [row.get(k) for k in ("takeProfit", "takeProfitPrice", "presetTakeProfitPrice", "profitTriggerPrice", "tpTriggerPrice")]
        sl_values = [row.get(k) for k in ("stopLoss", "stopLossPrice", "presetStopLossPrice", "lossTriggerPrice", "slTriggerPrice", "stopSurplusTriggerPrice", "stopLossTriggerPrice")]
        if any(safe_float(x) > 0 for x in tp_values) or "profit" in descriptor or descriptor in {"tp", "take_profit"}:
            has_tp = True
        if any(safe_float(x) > 0 for x in sl_values) or "loss" in descriptor or descriptor in {"sl", "stop_loss"}:
            has_sl = True
    # Some API variants expose two generic TPSL rows without a distinct planType.
    if len(rows) >= 2 and not (has_tp or has_sl):
        has_tp = True
        has_sl = True
    return {"ok": has_tp and has_sl, "has_tp": has_tp, "has_sl": has_sl, "count": len(rows), "rows": rows}


def wait_for_protection(client: BitgetUTAClient, symbol: str, timeout_seconds: int = 15) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    latest: dict[str, Any] = {"ok": False, "has_tp": False, "has_sl": False, "count": 0, "rows": []}
    while time.time() < deadline:
        try:
            latest = protection_status(strategy_orders(client, symbol))
            if latest["ok"]:
                return latest
        except Exception as exc:
            latest = {"ok": False, "has_tp": False, "has_sl": False, "count": 0, "rows": [], "error": str(exc)}
        time.sleep(1.0)
    return latest


def verify_api_and_account(client: BitgetUTAClient, cfg: Config, *, require_no_positions: bool) -> dict[str, Any]:
    assets = client.assets()
    settings = client.settings()
    equity, available = extract_usdt_equity_available(assets)
    errors: list[str] = []
    warnings: list[str] = []
    if hold_mode(settings) != "hedge_mode":
        errors.append(f"holdMode must be hedge_mode, got {hold_mode(settings) or '-'}")
    positions: dict[str, list[dict[str, Any]]] = {}
    pending: dict[str, int] = {}
    pending_strategy: dict[str, int] = {}
    for symbol in cfg.symbols:
        rows = open_positions(client.current_position(symbol, CATEGORY), symbol=symbol)
        positions[symbol] = rows
        try:
            pending[symbol] = len(open_orders(client, symbol))
        except Exception as exc:
            errors.append(f"{symbol} open-order query failed: {exc}")
            pending[symbol] = -1
        try:
            pending_strategy[symbol] = len(strategy_orders(client, symbol))
        except Exception as exc:
            errors.append(f"{symbol} strategy-order query failed: {exc}")
            pending_strategy[symbol] = -1
        sym = symbol_config(settings, symbol)
        if not sym:
            warnings.append(f"{symbol} has no saved symbol configuration yet")
        else:
            if str(sym.get("marginMode")) != cfg.margin_mode:
                errors.append(f"{symbol} marginMode={sym.get('marginMode')} expected {cfg.margin_mode}")
            if int(safe_float(sym.get("leverage"))) != cfg.leverage:
                errors.append(f"{symbol} leverage={sym.get('leverage')} expected {cfg.leverage}")
        if require_no_positions and rows:
            errors.append(f"{symbol} has existing position; refuse to arm")
        if require_no_positions and pending[symbol] > 0:
            errors.append(f"{symbol} has pending ordinary orders; refuse to arm")
        if require_no_positions and pending_strategy[symbol] > 0:
            errors.append(f"{symbol} has pending TP/SL strategy orders; refuse to arm")
    all_position_count = 0
    all_pending_order_count = 0
    all_pending_strategy_count = 0
    if require_no_positions and cfg.require_dedicated_account:
        try:
            all_position_count = len(all_current_positions(client))
            if all_position_count:
                errors.append(f"dedicated-account rule: {all_position_count} USDT-FUTURES position(s) exist")
        except Exception as exc:
            errors.append(f"all-position query failed: {exc}")
        try:
            all_pending_order_count = len(all_open_orders(client))
            if all_pending_order_count:
                errors.append(f"dedicated-account rule: {all_pending_order_count} ordinary order(s) exist")
        except Exception as exc:
            errors.append(f"all-order query failed: {exc}")
        try:
            all_pending_strategy_count = len(all_strategy_orders(client))
            if all_pending_strategy_count:
                errors.append(f"dedicated-account rule: {all_pending_strategy_count} strategy order(s) exist")
        except Exception as exc:
            errors.append(f"all-strategy-order query failed: {exc}")
    return {
        "ok": not errors,
        "equity": equity,
        "available": available,
        "hold_mode": hold_mode(settings),
        "positions": {k: len(v) for k, v in positions.items()},
        "pending_orders": pending,
        "pending_strategy_orders": pending_strategy,
        "all_position_count": all_position_count,
        "all_pending_order_count": all_pending_order_count,
        "all_pending_strategy_count": all_pending_strategy_count,
        "errors": errors,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Market and event data
# ---------------------------------------------------------------------------


def interval_ms(interval: str) -> int:
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval: {interval}")
    return INTERVAL_MS[interval]


def closed_candles(client: BitgetUTAClient, symbol: str, interval: str, limit: int) -> list[Candle]:
    response = client.candles(symbol, CATEGORY, interval, limit)
    candles = parse_candles(response)
    if len(candles) < min(40, limit // 2):
        raise RuntimeError(f"not enough candles: {symbol} {interval} {len(candles)}")
    now_ms = int(time.time() * 1000)
    step = interval_ms(interval)
    if candles and candles[-1].ts + step > now_ms - 2_000:
        candles = candles[:-1]
    return candles


def ticker_row(client: BitgetUTAClient, symbol: str) -> dict[str, Any]:
    rows = payload_rows(client.tickers(symbol, CATEGORY))
    if not rows:
        raise RuntimeError(f"ticker empty for {symbol}")
    return rows[0]


def ticker_price(row: dict[str, Any]) -> float:
    for key in ("lastPrice", "lastPr", "last", "close", "price", "markPrice"):
        if row.get(key) not in (None, ""):
            return safe_float(row[key])
    raise RuntimeError("ticker price missing")


def ticker_turnover(row: dict[str, Any]) -> float:
    for key in ("turnover24h", "quoteVolume", "quoteVol", "usdtVolume", "amount24h"):
        value = safe_float(row.get(key))
        if value > 0:
            return value
    return 0.0


def volume_z(candles: list[Candle], window: int = 30) -> float:
    if len(candles) < window + 1:
        return 0.0
    history = [max(0.0, c.volume) for c in candles[-(window + 1) : -1]]
    current = max(0.0, candles[-1].volume)
    mean = statistics.fmean(history)
    std = statistics.pstdev(history)
    if std <= 1e-12:
        return 0.0
    return (current - mean) / std


def extract_oi(data: dict[str, Any]) -> float | None:
    rows = payload_rows(data)
    row = rows[0] if rows else {}
    for key in ("openInterest", "openInterestAmount", "openInterestValue", "oi", "size"):
        value = safe_float(row.get(key), float("nan"))
        if math.isfinite(value) and value > 0:
            return value
    return None


def fetch_oi(client: BitgetUTAClient, symbol: str) -> float | None:
    try:
        data = client.get("/api/v3/market/open-interest", {"category": CATEGORY, "symbol": symbol}, auth=False)
        return extract_oi(data)
    except Exception:
        return None


def fetch_funding(client: BitgetUTAClient, symbol: str) -> float | None:
    try:
        data = client.get("/api/v3/market/current-fund-rate", {"category": CATEGORY, "symbol": symbol}, auth=False)
        rows = payload_rows(data)
        row = rows[0] if rows else {}
        for key in ("fundingRate", "fundingRateInterval", "currentFundingRate"):
            if row.get(key) not in (None, ""):
                return safe_float(row.get(key))
    except Exception:
        return None
    return None


def update_oi_history(state: StateStore, symbol: str, value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    now = time.time()
    histories = state.data.setdefault("oi_history", {})
    history = histories.setdefault(symbol, [])
    history.append({"ts": now, "value": value})
    history[:] = [x for x in history if now - safe_float(x.get("ts")) <= 8 * 3600]
    target = now - 15 * 60
    older = [x for x in history if safe_float(x.get("ts")) <= target]
    if not older:
        return None
    previous = max(older, key=lambda x: safe_float(x.get("ts")))
    prev_value = safe_float(previous.get("value"))
    return pct_change(value, prev_value) if prev_value > 0 else None


def latest_v62_event(cfg: Config) -> tuple[str, float, float | None, bool, bool, list[str]]:
    """Read the newest v6.2 snapshot and normalize its event overlay."""
    now = utc_now()
    for line in reversed(tail_lines(cfg.v62_snapshots_path, 500)):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ts = iso_to_dt(obj.get("ts_utc") or obj.get("ts"))
        if not ts:
            continue
        age = (now - ts).total_seconds() / 60.0
        if age > cfg.event_state_max_age_minutes:
            return "STALE", 0.0, None, False, True, [f"v6.2 event state age {age:.1f}m"]
        event_obj = obj.get("event")
        if isinstance(event_obj, dict):
            level = str(event_obj.get("status") or event_obj.get("level") or "NORMAL").upper()
            risk = max(
                safe_float(event_obj.get("risk_score")),
                safe_float(event_obj.get("market_stress_score")),
            )
            size = event_obj.get("size_multiplier")
            size_mult = clamp(safe_float(size, 1.0), 0.0, 1.0) if size not in (None, "") else None
            hard = bool(event_obj.get("hard_block") or event_obj.get("block_new"))
            freshness = event_obj.get("freshness")
            stale = False
            reasons: list[str] = []
            if isinstance(freshness, dict) and not bool(freshness.get("valid", True)):
                stale = True
                reasons.append(str(freshness.get("reason") or "v6.2 event feed stale"))
            if level in {"STALE", "STALE_NO_FILTER", "NO_COVERAGE"}:
                stale = True
            return level, risk, size_mult, hard, stale, reasons
        level = str(obj.get("event_level") or obj.get("event_status") or "NORMAL").upper()
        risk = safe_float(obj.get("event_risk") or obj.get("eventRisk") or 0.0)
        return level, risk, None, False, False, []
    return "STALE", 0.0, None, False, True, ["v6.2 event state unavailable"]


def event_time(obj: dict[str, Any]) -> dt.datetime | None:
    for key in ("ts_utc", "published_utc", "created_utc", "ts", "time", "published_at"):
        parsed = iso_to_dt(obj.get(key))
        if parsed:
            return parsed
    raw = obj.get("raw")
    if isinstance(raw, dict):
        for key in ("cTime", "publishTime", "published_at"):
            parsed = iso_to_dt(raw.get(key))
            if parsed:
                return parsed
    return None


def relevant_event(obj: dict[str, Any], symbol: str) -> bool:
    asset = "BTC" if symbol == "BTCUSDT" else "ETH"
    names = {asset, "BITCOIN" if asset == "BTC" else "ETHEREUM"}
    symbols = obj.get("symbols")
    symbol_values: set[str] = set()
    if isinstance(symbols, str):
        symbol_values = {x.strip().upper() for x in symbols.replace(",", " ").split() if x.strip()}
    elif isinstance(symbols, list):
        symbol_values = {str(x).strip().upper() for x in symbols if str(x).strip()}
    if symbol_values & names:
        return True
    relevance = str(obj.get("relevance") or obj.get("scope") or "").upper()
    if relevance in {"GLOBAL", asset}:
        return True
    text = " ".join(str(obj.get(k, "")) for k in ("title", "body", "fingerprint")).upper()
    if any(name in text for name in names):
        return True
    return False


def news_direction(obj: dict[str, Any]) -> float:
    direction = str(obj.get("direction") or "").lower()
    if direction in {"bullish", "positive", "up", "long", "risk_on"}:
        return 1.0
    if direction in {"bearish", "negative", "down", "short", "risk_off"}:
        return -1.0
    sentiment = safe_float(obj.get("sentiment_score"), 0.0)
    if sentiment >= 20:
        return clamp(sentiment / 100.0, 0.0, 1.0)
    if sentiment <= -20:
        return clamp(sentiment / 100.0, -1.0, 0.0)
    return 0.0


def structural_keyword(obj: dict[str, Any]) -> bool:
    text = " ".join(
        str(obj.get(k, ""))
        for k in ("event_family", "event_type", "title", "body", "fingerprint")
    ).lower()
    phrases = (
        "depeg",
        "de-peg",
        "insolvency",
        "bankruptcy",
        "withdrawal freeze",
        "withdrawals frozen",
        "all withdrawals",
        "exchange hack",
        "cex hack",
        "network halt",
        "network outage",
        "chain halt",
        "consensus failure",
    )
    return any(x in text for x in phrases)


def build_event_overlay(cfg: Config, symbol: str) -> EventOverlay:
    level, v62_risk, v62_size, v62_hard, stale, reasons = latest_v62_event(cfg)
    now = utc_now()
    providers: set[str] = set()
    directional: list[float] = []
    hard_providers: set[str] = set()
    event_reasons = list(reasons)
    for line in reversed(tail_lines(cfg.raw_events_path, 5000)):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ts = event_time(obj)
        if not ts:
            continue
        age = (now - ts).total_seconds() / 60.0
        if age < -5 or age > cfg.news_fresh_minutes:
            continue
        if not relevant_event(obj, symbol):
            continue
        provider = str(obj.get("provider") or obj.get("source") or "unknown").strip().lower()
        providers.add(provider)
        direction = news_direction(obj)
        if direction:
            confidence = clamp(safe_float(obj.get("confidence"), 0.70), 0.25, 1.0)
            directional.append(direction * confidence)
        item_risk = max(safe_float(obj.get("risk_score")), safe_float(obj.get("impact_score")))
        if structural_keyword(obj) and item_risk >= cfg.event_hard_block_risk:
            hard_providers.add(provider)
            title = str(obj.get("title") or obj.get("event_type") or "structural risk")[:100]
            event_reasons.append(title)

    bias = 0.0
    if len(providers) >= cfg.news_require_providers and directional:
        bias = clamp(statistics.fmean(directional) * cfg.news_bias_max, -cfg.news_bias_max, cfg.news_bias_max)
    local_hard = len(hard_providers) >= cfg.news_require_providers
    hard_block = v62_hard or local_hard
    risk = v62_risk
    if hard_block:
        risk = max(risk, cfg.event_hard_block_risk)
        level = "HARD_BLOCK"

    if hard_block or risk >= cfg.event_hard_block_risk:
        size_mult = 0.0
    elif stale:
        size_mult = cfg.event_stale_size_mult
    elif v62_size is not None:
        size_mult = v62_size
    elif risk >= cfg.event_high_risk:
        size_mult = cfg.event_high_size_mult
    elif risk >= cfg.event_caution_risk:
        size_mult = cfg.event_caution_size_mult
    else:
        size_mult = 1.0

    # Local thresholds can only reduce the official v6.2 size, never enlarge it.
    if risk >= cfg.event_high_risk:
        size_mult = min(size_mult, cfg.event_high_size_mult)
    elif risk >= cfg.event_caution_risk:
        size_mult = min(size_mult, cfg.event_caution_size_mult)

    return EventOverlay(
        level=level,
        risk=round(risk, 2),
        size_mult=round(clamp(size_mult, 0.0, 1.0), 3),
        directional_bias=round(bias, 3),
        providers=len(providers),
        hard_block=hard_block or risk >= cfg.event_hard_block_risk,
        stale=stale,
        reasons=event_reasons[:5],
    )


# ---------------------------------------------------------------------------
# Signal construction
# ---------------------------------------------------------------------------


def relation_score(condition: bool, points: float) -> float:
    return points if condition else 0.0


def build_signal(client: BitgetUTAClient, cfg: Config, state: StateStore, symbol: str) -> Signal:
    c5 = closed_candles(client, symbol, "5m", 140)
    c15 = closed_candles(client, symbol, "15m", 140)
    c1h = closed_candles(client, symbol, "1H", 220)
    c4h = closed_candles(client, symbol, "4H", 120)
    ticker = ticker_row(client, symbol)
    price = ticker_price(ticker)
    atr1h = atr(c1h, 14)
    if atr1h is None or atr1h <= 0:
        raise RuntimeError(f"ATR unavailable for {symbol}")
    atr_pct = atr1h / price
    e20_1h = ema([x.close for x in c1h], 20)
    e50_1h = ema([x.close for x in c1h], 50)
    e20_4h = ema([x.close for x in c4h], 20)
    e50_4h = ema([x.close for x in c4h], 50)
    if None in {e20_1h, e50_1h, e20_4h, e50_4h}:
        raise RuntimeError(f"EMA unavailable for {symbol}")
    e20_1h = float(e20_1h)
    e50_1h = float(e50_1h)
    e20_4h = float(e20_4h)
    e50_4h = float(e50_4h)

    ret1h = pct_change(price, c1h[-2].close)
    momentum_atr = (price - c15[-4].close) / atr1h
    vz = volume_z(c5, 30)
    prior_high = max(x.high for x in c5[-21:-1])
    prior_low = min(x.low for x in c5[-21:-1])
    breakout_long = price > prior_high and c5[-1].close >= prior_high * 0.9995
    breakout_short = price < prior_low and c5[-1].close <= prior_low * 1.0005
    anti_distance = abs(price - e20_1h) / atr1h
    last5 = c5[-1]
    last_range = max(last5.high - last5.low, price * 1e-9)
    response_efficiency = abs(last5.close - last5.open) / last_range
    close_location = clamp((last5.close - last5.low) / last_range, 0.0, 1.0)

    regime_long = (
        relation_score(e20_1h > e50_1h, 22)
        + relation_score(price > e20_1h, 16)
        + relation_score(e20_4h > e50_4h, 27)
        + relation_score(c4h[-1].close > e20_4h, 15)
        + relation_score(ret1h > 0, 10)
        + relation_score(momentum_atr > 0, 10)
    )
    regime_short = (
        relation_score(e20_1h < e50_1h, 22)
        + relation_score(price < e20_1h, 16)
        + relation_score(e20_4h < e50_4h, 27)
        + relation_score(c4h[-1].close < e20_4h, 15)
        + relation_score(ret1h < 0, 10)
        + relation_score(momentum_atr < 0, 10)
    )

    oi = fetch_oi(client, symbol)
    oi15 = update_oi_history(state, symbol, oi)
    funding = fetch_funding(client, symbol)
    event = build_event_overlay(cfg, symbol)

    def trigger(side: str) -> float:
        sign = 1.0 if side == "LONG" else -1.0
        mom = sign * momentum_atr
        r1 = sign * ret1h
        breakout = breakout_long if side == "LONG" else breakout_short
        regime = regime_long if side == "LONG" else regime_short
        oi_component = 0.0
        if oi15 is not None:
            oi_component = 10.0 * clamp((sign * oi15) / 0.25, 0.0, 1.0)
        score = (
            20.0 * clamp(mom / 0.80, 0.0, 1.0)
            + (20.0 if breakout else 0.0)
            + 15.0 * clamp(vz / 2.0, 0.0, 1.0)
            + oi_component
            + 15.0 * clamp(regime / 100.0, 0.0, 1.0)
            + 10.0 * clamp(r1 / max(atr_pct * 100.0 * 0.50, 0.05), 0.0, 1.0)
        )
        news = event.directional_bias if side == "LONG" else -event.directional_bias
        score += max(-cfg.news_bias_max, min(cfg.news_bias_max, news))
        # Funding is a crowding filter, never a direction by itself.
        if funding is not None:
            if side == "LONG" and funding > 0.0008:
                score -= 6.0
            if side == "SHORT" and funding < -0.0008:
                score -= 6.0
        return round(max(0.0, score), 3)

    tl = trigger("LONG")
    ts = trigger("SHORT")
    side = "LONG" if tl >= ts else "SHORT"
    winning = tl if side == "LONG" else ts
    losing = ts if side == "LONG" else tl
    edge = winning - losing
    blockers: list[str] = []
    mode = "WAIT"
    entry_ready = False
    side_regime = regime_long if side == "LONG" else regime_short
    side_breakout = breakout_long if side == "LONG" else breakout_short
    side_momentum = momentum_atr if side == "LONG" else -momentum_atr
    oi_confirm = oi15 is not None and ((oi15 >= cfg.oi_confirm_pct) if side == "LONG" else (oi15 <= -cfg.oi_confirm_pct))
    exceptional_tape = vz >= 2.0 and side_momentum >= 0.75
    upper_wick = max(0.0, last5.high - max(last5.open, last5.close)) / last_range
    lower_wick = max(0.0, min(last5.open, last5.close) - last5.low) / last_range
    rejection_wick = upper_wick if side == "LONG" else lower_wick
    close_location_ok = close_location >= 0.62 if side == "LONG" else close_location <= 0.38
    high_volume_low_response = vz >= 1.5 and response_efficiency < 0.18

    if event.hard_block:
        blockers.append("event_hard_block")
    if anti_distance > max(cfg.anti_chase_atr, cfg.impulse_anti_chase_atr):
        blockers.append("anti_chase")
    if high_volume_low_response:
        blockers.append("high_volume_low_price_response")

    trend_ok = (
        side_regime >= cfg.trend_regime_threshold
        and winning >= cfg.trend_entry_threshold
        and edge >= cfg.min_edge
        and anti_distance <= cfg.anti_chase_atr
        and not high_volume_low_response
        and not event.hard_block
    )
    impulse_ok = (
        winning >= cfg.impulse_entry_threshold
        and edge >= cfg.impulse_min_edge
        and side_breakout
        and side_momentum >= cfg.impulse_move_atr
        and vz >= cfg.impulse_volume_z
        and (oi_confirm or exceptional_tape)
        and anti_distance <= cfg.impulse_anti_chase_atr
        and close_location_ok
        and rejection_wick <= 0.45
        and not high_volume_low_response
        and (regime_short if side == "LONG" else regime_long) < 70
        and not event.hard_block
    )

    if trend_ok:
        mode = "TREND"
        entry_ready = True
    elif impulse_ok:
        mode = "IMPULSE"
        entry_ready = True
    else:
        if side_regime < cfg.trend_regime_threshold and not side_breakout:
            blockers.append("no_regime_or_breakout")
        if winning < min(cfg.trend_entry_threshold, cfg.impulse_entry_threshold):
            blockers.append("trigger_low")
        if edge < min(cfg.min_edge, cfg.impulse_min_edge):
            blockers.append("edge_low")
        if side_breakout and vz < cfg.impulse_volume_z:
            blockers.append("volume_not_confirmed")
        if side_breakout and not (oi_confirm or exceptional_tape):
            blockers.append("oi_or_exceptional_tape_not_confirmed")
        if side_breakout and not close_location_ok:
            blockers.append("breakout_close_not_strong")
        if side_breakout and rejection_wick > 0.45:
            blockers.append("breakout_rejection_wick")

    turnover = ticker_turnover(ticker)
    if turnover <= 0:
        turnover = sum(safe_float(getattr(x, "turnover", 0.0)) or (x.close * x.volume) for x in c1h[-24:])
    market_cap = max(price * cfg.symbols[symbol].supply_proxy, 1.0)
    turnover_cap = turnover / market_cap
    opportunity = (
        winning
        + 15.0 * max(0.0, side_momentum)
        + 8.0 * max(0.0, vz)
        + 20.0 * math.log1p(max(0.0, turnover_cap) * 10.0)
    )

    return Signal(
        symbol=symbol,
        ts=utc_now().isoformat(),
        price=round(price, 10),
        atr1h=round(atr1h, 10),
        atr1h_pct=round(atr_pct * 100.0, 6),
        ema20_1h=round(e20_1h, 10),
        ema50_1h=round(e50_1h, 10),
        ema20_4h=round(e20_4h, 10),
        ema50_4h=round(e50_4h, 10),
        regime_long=round(regime_long, 3),
        regime_short=round(regime_short, 3),
        trigger_long=tl,
        trigger_short=ts,
        edge=round(edge, 3),
        side=side,
        mode=mode,
        entry_ready=entry_ready,
        volume_z5=round(vz, 4),
        momentum_atr=round(momentum_atr, 4),
        ret_1h_pct=round(ret1h, 4),
        breakout_long=breakout_long,
        breakout_short=breakout_short,
        anti_chase_distance_atr=round(anti_distance, 4),
        price_response_efficiency=round(response_efficiency, 4),
        close_location=round(close_location, 4),
        rejection_wick_ratio=round(rejection_wick, 4),
        oi=round(oi, 8) if oi is not None else None,
        oi_15m_pct=round(oi15, 5) if oi15 is not None else None,
        funding_rate=round(funding, 10) if funding is not None else None,
        turnover24h=round(turnover, 4),
        market_cap_proxy=round(market_cap, 4),
        turnover_cap_ratio=round(turnover_cap, 8),
        opportunity_score=round(opportunity, 4),
        event=event.to_dict(),
        blockers=sorted(set(blockers)),
    )


def confirmation_ready(state: StateStore, signal: Signal, cfg: Config) -> bool:
    items = state.data.setdefault("candidate_confirmations", {})
    item = items.setdefault(signal.symbol, {"key": "", "count": 0, "last_ts": 0.0})
    now = time.time()
    if not signal.entry_ready:
        item.update({"key": "", "count": 0, "last_ts": now})
        return False
    key = f"{signal.symbol}:{signal.side}:{signal.mode}"
    max_gap = max(20.0, cfg.loop_seconds * 2.5)
    consecutive = item.get("key") == key and now - safe_float(item.get("last_ts"), 0.0) <= max_gap
    if consecutive:
        item["count"] = int(item.get("count", 0)) + 1
    else:
        item.update({"key": key, "count": 1, "first_ts": now})
    item["last_ts"] = now
    needed = cfg.impulse_confirm_cycles if signal.mode == "IMPULSE" else 1
    return int(item.get("count", 0)) >= needed


# ---------------------------------------------------------------------------
# Risk, sizing, state reconciliation, and execution
# ---------------------------------------------------------------------------


def day_key(now: dt.datetime) -> str:
    return now.strftime("%Y-%m-%d")


def week_key(now: dt.datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def update_equity_guards(state: StateStore, cfg: Config, equity: float) -> dict[str, Any]:
    now = utc_now()
    daily = state.data.setdefault("daily", {})
    weekly = state.data.setdefault("weekly", {})
    if daily.get("key") != day_key(now):
        daily.clear()
        daily.update({"key": day_key(now), "start_equity": equity, "entries": {}})
    if weekly.get("key") != week_key(now):
        weekly.clear()
        weekly.update({"key": week_key(now), "start_equity": equity})
    peak = max(safe_float(state.data.get("peak_equity"), equity), equity)
    state.data["peak_equity"] = peak
    dstart = max(safe_float(daily.get("start_equity"), equity), 1e-9)
    wstart = max(safe_float(weekly.get("start_equity"), equity), 1e-9)
    daily_loss = max(0.0, (dstart - equity) / dstart * 100.0)
    weekly_loss = max(0.0, (wstart - equity) / wstart * 100.0)
    drawdown = max(0.0, (peak - equity) / peak * 100.0) if peak > 0 else 0.0
    reasons: list[str] = []
    if daily_loss >= cfg.daily_loss_block_pct:
        reasons.append("daily_loss_block")
    if weekly_loss >= cfg.weekly_loss_block_pct:
        reasons.append("weekly_loss_block")
    if drawdown >= cfg.max_drawdown_block_pct:
        reasons.append("max_drawdown_block")
    cooldown_until = safe_float(state.data.get("cooldown_until"), 0.0)
    if time.time() < cooldown_until:
        reasons.append(f"cooldown_until_{dt.datetime.fromtimestamp(cooldown_until, tz=dt.timezone.utc).isoformat()}")
    return {
        "ok": not reasons,
        "daily_loss_pct": round(daily_loss, 4),
        "weekly_loss_pct": round(weekly_loss, 4),
        "drawdown_pct": round(drawdown, 4),
        "reasons": reasons,
    }


def daily_entry_count(state: StateStore, symbol: str) -> int:
    daily = state.data.setdefault("daily", {})
    entries = daily.setdefault("entries", {})
    return int(entries.get(symbol, 0))


def record_entry_count(state: StateStore, symbol: str) -> None:
    daily = state.data.setdefault("daily", {})
    entries = daily.setdefault("entries", {})
    entries[symbol] = int(entries.get(symbol, 0)) + 1


def current_position_rows(client: BitgetUTAClient, cfg: Config) -> dict[str, list[dict[str, Any]]]:
    return {
        symbol: open_positions(client.current_position(symbol, CATEGORY), symbol=symbol)
        for symbol in cfg.symbols
    }


def position_notional(rows: Iterable[dict[str, Any]], prices: dict[str, float]) -> float:
    total = 0.0
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        price = safe_float(row.get("markPrice") or row.get("marketPrice") or row.get("lastPrice") or prices.get(symbol))
        total += float(row_qty_decimal(row)) * price
    return total


def round_qty(raw_qty: float, instrument: dict[str, Any], price: float) -> tuple[str, bool, str]:
    precision = int(instrument.get("quantityPrecision") or instrument.get("volumePlace") or 0)
    step = Decimal(str(instrument.get("quantityMultiplier") or instrument.get("sizeMultiplier") or "0"))
    value = Decimal(str(max(0.0, raw_qty)))
    if step > 0:
        value = (value / step).to_integral_value(rounding=ROUND_DOWN) * step
    quant = Decimal("1") if precision <= 0 else Decimal("1") / (Decimal(10) ** precision)
    value = value.quantize(quant, rounding=ROUND_DOWN)
    min_qty = Decimal(str(instrument.get("minOrderQty") or instrument.get("minTradeNum") or "0"))
    min_amount = Decimal(str(instrument.get("minOrderAmount") or "0"))
    max_market = Decimal(str(instrument.get("maxMarketOrderQty") or "0"))
    if max_market > 0 and value > max_market:
        value = (max_market / step).to_integral_value(rounding=ROUND_DOWN) * step if step > 0 else max_market
        value = value.quantize(quant, rounding=ROUND_DOWN)
    if value <= 0:
        return str(value), False, "qty<=0"
    if min_qty > 0 and value < min_qty:
        return str(value), False, f"qty<{min_qty}"
    if min_amount > 0 and value * Decimal(str(price)) < min_amount:
        return str(value), False, f"notional<{min_amount}"
    return str(value), True, "ok"


def planned_stop_and_tp(cfg: Config, signal: Signal) -> tuple[float, float, float]:
    scfg = cfg.symbols[signal.symbol]
    stop_pct = clamp((signal.atr1h / signal.price) * scfg.stop_atr_mult, scfg.min_stop_pct, scfg.max_stop_pct)
    tp_r = cfg.tp_r_impulse if signal.mode == "IMPULSE" else cfg.tp_r_trend
    if signal.side == "LONG":
        stop = signal.price * (1.0 - stop_pct)
        take = signal.price * (1.0 + stop_pct * tp_r)
    else:
        stop = signal.price * (1.0 + stop_pct)
        take = signal.price * (1.0 - stop_pct * tp_r)
    return stop_pct, stop, take


def risk_size(
    cfg: Config,
    signal: Signal,
    equity: float,
    available: float,
    open_count: int,
    existing_notional: float,
    event_size_mult: float,
    instrument: dict[str, Any],
) -> dict[str, Any]:
    stop_pct, stop, take = planned_stop_and_tp(cfg, signal)
    base_risk_pct = cfg.both_positions_risk_pct_each if open_count >= 1 else cfg.risk_per_trade_pct
    mode_mult = 0.50 if signal.mode == "IMPULSE" else 1.0
    risk_pct = base_risk_pct * mode_mult * event_size_mult
    risk_usdt = equity * risk_pct / 100.0
    notional_by_risk = risk_usdt / max(stop_pct, 1e-9)
    bucket_cap = equity * cfg.bucket_margin_ratio * cfg.leverage
    total_notional_cap = equity * cfg.max_total_notional_equity_ratio
    remaining_total_notional = max(0.0, total_notional_cap - existing_notional)
    total_margin_cap = equity * cfg.max_total_initial_margin_pct / 100.0
    remaining_margin_notional = max(0.0, (total_margin_cap - existing_notional / cfg.leverage) * cfg.leverage)
    available_notional = max(0.0, available * 0.95 * cfg.leverage)
    notional = min(notional_by_risk, bucket_cap, remaining_total_notional, remaining_margin_notional, available_notional)
    raw_qty = notional / signal.price if signal.price > 0 else 0.0
    qty, valid, reason = round_qty(raw_qty, instrument, signal.price)
    return {
        "valid": valid and notional > 0,
        "reason": reason if notional > 0 else "notional_cap_exhausted",
        "risk_pct": round(risk_pct, 5),
        "risk_usdt": round(risk_usdt, 6),
        "stop_pct": round(stop_pct * 100.0, 6),
        "stop_price": stop,
        "take_profit_price": take,
        "notional": round(notional, 4),
        "initial_margin": round(notional / cfg.leverage, 4),
        "qty": qty,
    }


def managed_positions(state: StateStore) -> dict[str, dict[str, Any]]:
    obj = state.data.setdefault("managed_positions", {})
    return obj if isinstance(obj, dict) else {}


def reconcile_closed(client: BitgetUTAClient, cfg: Config, state: StateStore, actual: dict[str, list[dict[str, Any]]], bot: Any | None) -> None:
    managed = managed_positions(state)
    holds = state.data.setdefault("reconciliation_holds", {})
    if not isinstance(holds, dict):
        holds = {}
        state.data["reconciliation_holds"] = holds
    for symbol in list(managed):
        record = managed[symbol]
        expected = str(record.get("side") or "").lower()
        matching_actual = [row for row in (actual.get(symbol) or []) if row_side(row) == expected]
        if matching_actual:
            record["missing_position_count"] = 0
            record.pop("reconcile_alert_sent", None)
            holds.pop(symbol, None)
            continue
        record["missing_position_count"] = int(record.get("missing_position_count", 0)) + 1
        if int(record["missing_position_count"]) < cfg.position_absence_confirm_cycles:
            continue
        entry_ms = int(safe_float(record.get("entry_ms"), 0.0))
        history: list[dict[str, Any]] = []
        try:
            history = history_positions(client, symbol, entry_ms)
        except Exception:
            history = []
        matching: list[dict[str, Any]] = []
        for row in history:
            if str(row.get("symbol", "")).upper() != symbol:
                continue
            row_pos_side = str(row.get("posSide") or row.get("holdSide") or "").lower()
            if row_pos_side and expected and row_pos_side != expected:
                continue
            updated_ms = int(safe_float(row.get("updatedTime") or row.get("uTime") or row.get("closeTime"), 0.0))
            created_ms = int(safe_float(row.get("createdTime") or row.get("cTime"), 0.0))
            if updated_ms and updated_ms < max(0, entry_ms - 60_000):
                continue
            if created_ms and created_ms < max(0, entry_ms - 120_000):
                continue
            matching.append(row)
        matching.sort(key=lambda x: safe_float(x.get("updatedTime") or x.get("uTime") or x.get("closeTime")), reverse=True)
        latest = matching[0] if matching else {}
        if not latest:
            holds[symbol] = {
                "since": (holds.get(symbol) or {}).get("since") or utc_now().isoformat(),
                "reason": "position absent but matching close history unavailable",
            }
            if not record.get("reconcile_alert_sent"):
                record["reconcile_alert_sent"] = True
                send(bot, f"🚨 <b>V6.3 reconciliation hold</b>\n{symbol}: position absent, close history unavailable. New entries blocked.")
            continue
        net = safe_float(latest.get("netProfit"), float("nan"))
        exit_price = safe_float(latest.get("closePriceAvg") or latest.get("avgClosePrice"), 0.0)
        if not math.isfinite(net):
            net = 0.0
        risk_usdt = max(safe_float(record.get("risk_usdt"), 0.0), 1e-9)
        r_multiple = net / risk_usdt
        reason = str(record.get("pending_exit_reason") or "exchange_tp_sl_or_external_close")
        strategy_cleanup = cancel_all_strategy_orders(client, symbol)
        event = {
            "ts": utc_now().isoformat(),
            "event": "exit",
            "symbol": symbol,
            "side": record.get("side"),
            "mode": record.get("mode"),
            "qty": record.get("qty"),
            "entry_price": record.get("entry_price"),
            "exit_price": exit_price,
            "stop_price": record.get("stop_price"),
            "take_profit_price": record.get("take_profit_price"),
            "net_profit": round(net, 8),
            "r_multiple": round(r_multiple, 5),
            "reason": reason,
            "client_oid": record.get("client_oid"),
            "history": latest,
            "strategy_cleanup": strategy_cleanup,
        }
        append_jsonl(cfg.events_path, event)
        append_trade_csv(cfg.trades_path, event)
        send(bot, f"🔚 <b>V6.3 {symbol} EXIT</b>\n{record.get('side')} / {reason}\nnet {net:+.4f} USDT / R {r_multiple:+.2f}")
        if net < 0:
            losses = int(state.data.get("consecutive_losses", 0)) + 1
            state.data["consecutive_losses"] = losses
            minutes = cfg.cooldown_after_two_losses_minutes if losses >= 2 else cfg.cooldown_after_loss_minutes
            state.data["cooldown_until"] = time.time() + minutes * 60
        else:
            state.data["consecutive_losses"] = 0
        holds.pop(symbol, None)
        del managed[symbol]


def reconcile_uncertain_orders(
    client: BitgetUTAClient,
    cfg: Config,
    state: StateStore,
    actual: dict[str, list[dict[str, Any]]],
    bot: Any | None,
) -> None:
    uncertain = state.data.setdefault("uncertain_orders", {})
    if not isinstance(uncertain, dict):
        state.data["uncertain_orders"] = {}
        return
    for symbol in list(uncertain):
        item = uncertain.get(symbol) or {}
        client_oid = str(item.get("client_oid") or "")
        detail: dict[str, Any] = {}
        try:
            detail = order_info(client, client_oid) if client_oid else {}
        except Exception:
            detail = {}
        detail_rows = payload_rows(detail)
        detail_row = detail_rows[0] if detail_rows else {}
        status = str(detail_row.get("orderStatus") or "").lower()
        position_rows = actual.get(symbol) or []
        signal_data = item.get("signal") if isinstance(item.get("signal"), dict) else {}
        size = item.get("size") if isinstance(item.get("size"), dict) else {}
        expected_side = str(signal_data.get("side") or "").upper()
        if position_rows:
            row = position_rows[0]
            live_side = row_side(row).upper()
            if expected_side and live_side != expected_side:
                append_jsonl(
                    cfg.events_path,
                    {"ts": utc_now().isoformat(), "event": "uncertain_order_side_mismatch", "symbol": symbol, "expected": expected_side, "actual": live_side, "detail": detail},
                )
                continue
            entry_price = safe_float(row_avg_price(row)) or safe_float(detail_row.get("avgPrice")) or safe_float(signal_data.get("price"))
            qty = str(row_qty_decimal(row))
            record = {
                "symbol": symbol,
                "side": live_side or expected_side,
                "mode": signal_data.get("mode") or "UNKNOWN",
                "qty": qty,
                "entry_price": entry_price,
                "entry_time": time.time(),
                "entry_ms": int(time.time() * 1000),
                "stop_price": safe_float(size.get("stop_price")),
                "take_profit_price": safe_float(size.get("take_profit_price")),
                "risk_usdt": safe_float(size.get("risk_usdt")),
                "risk_pct": safe_float(size.get("risk_pct")),
                "atr1h": safe_float(signal_data.get("atr1h")),
                "mfe_r": 0.0,
                "mae_r": 0.0,
                "best_price": entry_price,
                "client_oid": client_oid,
                "live_position": True,
                "entry_signal": signal_data,
                "recovered_from_uncertain": True,
            }
            managed_positions(state)[symbol] = record
            record_entry_count(state, symbol)
            protection = wait_for_protection(client, symbol, 5)
            event = {
                "ts": utc_now().isoformat(),
                "event": "entry",
                "symbol": symbol,
                "side": record["side"],
                "mode": record["mode"],
                "qty": qty,
                "entry_price": entry_price,
                "exit_price": "",
                "stop_price": record["stop_price"],
                "take_profit_price": record["take_profit_price"],
                "net_profit": "",
                "r_multiple": "",
                "reason": "recovered_uncertain_fill",
                "client_oid": client_oid,
                "detail": detail_row,
                "protection": protection,
                "size": size,
                "signal": signal_data,
            }
            append_jsonl(cfg.events_path, event)
            append_trade_csv(cfg.trades_path, event)
            send(bot, f"⚠️ <b>V6.3 recovered {symbol} position</b>\n{record['side']} qty {qty} / protection {'OK' if protection.get('ok') else 'MISSING'}")
            del uncertain[symbol]
            if not protection.get("ok"):
                managed_positions(state)[symbol]["pending_exit_reason"] = "protective_order_missing_after_recovery"
                close_managed_position(client, cfg, state, symbol, row, "protective_order_missing_after_recovery", True, bot)
            continue
        if status in {"cancelled", "canceled", "rejected", "failed"}:
            append_jsonl(cfg.events_path, {"ts": utc_now().isoformat(), "event": "uncertain_order_cleared", "symbol": symbol, "status": status, "detail": detail_row})
            del uncertain[symbol]
            continue
        created = iso_to_dt(item.get("ts"))
        age_minutes = (utc_now() - created).total_seconds() / 60.0 if created else 0.0
        if age_minutes >= 10:
            item["requires_manual_review"] = True
            item["last_status"] = status or "unknown"


def unknown_positions(actual: dict[str, list[dict[str, Any]]], state: StateStore) -> list[str]:
    managed = managed_positions(state)
    unknown: list[str] = []
    for symbol, rows in actual.items():
        if not rows:
            continue
        record = managed.get(symbol)
        if not record:
            unknown.append(symbol)
            continue
        expected = str(record.get("side") or "").lower()
        matching = [row for row in rows if row_side(row) == expected]
        if len(rows) != 1 or len(matching) != 1:
            unknown.append(symbol)
    return unknown


def close_managed_position(
    client: BitgetUTAClient,
    cfg: Config,
    state: StateStore,
    symbol: str,
    row: dict[str, Any],
    reason: str,
    live: bool,
    bot: Any | None,
) -> dict[str, Any]:
    side = row_side(row)
    qty = str(row_qty_decimal(row))
    if side not in {"long", "short"} or safe_float(qty) <= 0:
        return {"ok": False, "reason": "invalid position row"}
    managed = managed_positions(state).get(symbol, {})
    managed["pending_exit_reason"] = reason
    last_request = safe_float(managed.get("exit_requested_at"), 0.0)
    if time.time() - last_request < 30.0:
        return {"ok": True, "pending": True, "symbol": symbol}
    managed["exit_requested_at"] = time.time()
    execute_live = live or bool(managed.get("live_position"))
    if not execute_live:
        return {"ok": True, "dry_run": True, "symbol": symbol, "side": side, "qty": qty}
    result = close_all_symbol(client, symbol, side)
    ok = client.is_success(result)
    append_jsonl(
        cfg.events_path,
        {"ts": utc_now().isoformat(), "event": "exit_order", "symbol": symbol, "side": side, "reason": reason, "result": result},
    )
    if ok:
        send(bot, f"⚠️ <b>V6.3 {symbol} EXIT ORDER</b>\n{side} / {reason} / qty {qty}")
    return {"ok": ok, "result": result}


def manage_open_positions(
    client: BitgetUTAClient,
    cfg: Config,
    state: StateStore,
    actual: dict[str, list[dict[str, Any]]],
    signals: dict[str, Signal],
    live: bool,
    bot: Any | None,
) -> None:
    managed = managed_positions(state)
    now = time.time()
    for symbol, rows in actual.items():
        if not rows or symbol not in managed:
            continue
        row = rows[0]
        record = managed[symbol]
        signal = signals.get(symbol)
        price = signal.price if signal else safe_float(row.get("markPrice") or row.get("marketPrice"))
        entry = safe_float(record.get("entry_price"))
        stop_distance = abs(entry - safe_float(record.get("stop_price")))
        if entry <= 0 or stop_distance <= 0 or price <= 0:
            continue
        side = str(record.get("side", "")).upper()
        favorable = price - entry if side == "LONG" else entry - price
        adverse = entry - price if side == "LONG" else price - entry
        current_r = favorable / stop_distance
        record["mfe_r"] = max(safe_float(record.get("mfe_r")), current_r)
        record["mae_r"] = max(safe_float(record.get("mae_r")), adverse / stop_distance)
        if side == "LONG":
            record["best_price"] = max(safe_float(record.get("best_price"), entry), price)
            retrace = safe_float(record.get("best_price")) - price
        else:
            best = safe_float(record.get("best_price"), entry)
            record["best_price"] = min(best, price)
            retrace = price - safe_float(record.get("best_price"))
        age_minutes = (now - safe_float(record.get("entry_time"), now)) / 60.0
        reason: str | None = None
        event = build_event_overlay(cfg, symbol)
        if event.hard_block:
            reason = "structural_event_hard_block"
        elif safe_float(record.get("mfe_r")) >= cfg.trail_activate_r and retrace >= safe_float(record.get("atr1h")) * cfg.trail_atr_mult:
            reason = "local_atr_trailing_exit"
        elif age_minutes >= cfg.no_followthrough_minutes and safe_float(record.get("mfe_r")) < cfg.no_followthrough_mfe_r:
            if signal:
                side_trigger = signal.trigger_long if side == "LONG" else signal.trigger_short
                if side_trigger < cfg.impulse_entry_threshold:
                    reason = "no_followthrough"
        max_hours = cfg.time_stop_hours_impulse if record.get("mode") == "IMPULSE" else cfg.time_stop_hours_trend
        if reason is None and age_minutes >= max_hours * 60.0:
            reason = "time_stop"
        if reason is None and signal:
            opposite = signal.trigger_short if side == "LONG" else signal.trigger_long
            current = signal.trigger_long if side == "LONG" else signal.trigger_short
            invalid = opposite >= current + 10 and opposite >= cfg.impulse_entry_threshold
            inv_count = int(record.get("invalidation_count", 0)) + 1 if invalid else 0
            record["invalidation_count"] = inv_count
            if inv_count >= 2:
                reason = "signal_invalidation"
        if reason:
            close_managed_position(client, cfg, state, symbol, row, reason, live, bot)


def place_entry(
    client: BitgetUTAClient,
    cfg: Config,
    state: StateStore,
    signal: Signal,
    size: dict[str, Any],
    instrument: dict[str, Any],
    live: bool,
    bot: Any | None,
) -> dict[str, Any]:
    side = "buy" if signal.side == "LONG" else "sell"
    pos_side = "long" if signal.side == "LONG" else "short"
    oid = f"v63-{signal.side[0].lower()}-{signal.symbol.lower()}-{int(time.time()*1000)%1_000_000_000:09d}"[:32]
    stop_text = format_price(size["stop_price"], instrument)
    take_text = format_price(size["take_profit_price"], instrument)
    intent = OrderIntent(
        symbol=signal.symbol,
        side=side,
        pos_side=pos_side,
        qty=str(size["qty"]),
        category=CATEGORY,
        margin_coin=MARGIN_COIN,
        margin_mode=cfg.margin_mode,
        order_type="market",
        reduce_only=False,
        client_oid=oid,
        take_profit=take_text,
        stop_loss=stop_text,
        tp_trigger_by="mark",
        sl_trigger_by="mark",
        tp_order_type="market",
        sl_order_type="market",
    )
    payload = client.build_market_order_payload(intent)
    if not live:
        return {"ok": True, "dry_run": True, "payload": payload, "client_oid": oid}
    result = client.place_order(intent, dry_run=False)
    if not client.is_success(result):
        return {"ok": False, "result": result, "payload": payload, "client_oid": oid}
    detail = wait_for_order(client, oid, 12)
    rows = payload_rows(detail)
    detail_row = rows[0] if rows else {}
    status_kind = order_status_kind(detail_row)
    if status_kind not in {"filled", "partial"}:
        # API acceptance is not proof of fill. Lock the symbol for manual review.
        state.data.setdefault("uncertain_orders", {})[signal.symbol] = {
            "ts": utc_now().isoformat(),
            "client_oid": oid,
            "place_result": result,
            "order_detail": detail,
            "signal": signal.to_dict(),
            "size": size,
        }
        state.data["cooldown_until"] = max(safe_float(state.data.get("cooldown_until")), time.time() + 30 * 60)
        return {"ok": False, "uncertain": True, "result": result, "detail": detail, "client_oid": oid}
    if status_kind == "partial":
        try:
            cancel_symbol_orders(client, signal.symbol)
        except Exception:
            pass
    entry_price = safe_float(detail_row.get("avgPrice"), signal.price)
    filled_qty = str(detail_row.get("cumExecQty") or size["qty"])
    record = {
        "symbol": signal.symbol,
        "side": signal.side,
        "mode": signal.mode,
        "qty": filled_qty,
        "entry_price": entry_price,
        "entry_time": time.time(),
        "entry_ms": int(time.time() * 1000),
        "stop_price": safe_float(stop_text),
        "take_profit_price": safe_float(take_text),
        "risk_usdt": size["risk_usdt"],
        "risk_pct": size["risk_pct"],
        "atr1h": signal.atr1h,
        "mfe_r": 0.0,
        "mae_r": 0.0,
        "best_price": entry_price,
        "client_oid": oid,
        "live_position": True,
        "entry_signal": signal.to_dict(),
    }
    protection = wait_for_protection(client, signal.symbol, 15)
    record["protection"] = protection
    managed_positions(state)[signal.symbol] = record
    record_entry_count(state, signal.symbol)
    event = {
        "ts": utc_now().isoformat(),
        "event": "entry",
        "symbol": signal.symbol,
        "side": signal.side,
        "mode": signal.mode,
        "qty": filled_qty,
        "entry_price": entry_price,
        "exit_price": "",
        "stop_price": stop_text,
        "take_profit_price": take_text,
        "net_profit": "",
        "r_multiple": "",
        "reason": "signal_entry",
        "client_oid": oid,
        "order": result,
        "detail": detail_row,
        "protection": protection,
        "size": size,
        "signal": signal.to_dict(),
    }
    append_jsonl(cfg.events_path, event)
    append_trade_csv(cfg.trades_path, event)
    if not protection.get("ok"):
        record["pending_exit_reason"] = "protective_tp_sl_not_verified"
        position_rows = open_positions(client.current_position(signal.symbol, CATEGORY), symbol=signal.symbol)
        emergency = None
        if position_rows:
            emergency = close_managed_position(
                client, cfg, state, signal.symbol, position_rows[0], "protective_tp_sl_not_verified", True, bot
            )
        state.data["cooldown_until"] = max(safe_float(state.data.get("cooldown_until")), time.time() + 60 * 60)
        append_jsonl(
            cfg.events_path,
            {"ts": utc_now().isoformat(), "event": "protection_failure", "symbol": signal.symbol, "client_oid": oid, "protection": protection, "emergency_close": emergency},
        )
        send(
            bot,
            f"🚨 <b>V6.3 {signal.symbol} protection verification failed</b>\n"
            f"position emergency-close requested / {protection}",
        )
        return {"ok": False, "protection_missing": True, "emergency_close": emergency, "result": result, "detail": detail, "client_oid": oid, "record": record}
    send(
        bot,
        f"🚀 <b>V6.3 LIVE {signal.symbol} {signal.side}</b>\n"
        f"mode {signal.mode} / qty {filled_qty}\n"
        f"entry {entry_price:,.4f}\nSL {safe_float(stop_text):,.4f} / TP {safe_float(take_text):,.4f}\n"
        f"risk {size['risk_pct']:.3f}% equity / margin {size['initial_margin']:.2f} USDT / protection VERIFIED",
    )
    return {"ok": True, "result": result, "detail": detail, "protection": protection, "client_oid": oid, "record": record}


# ---------------------------------------------------------------------------
# Cycle, doctor, setup, reporting, panic flat
# ---------------------------------------------------------------------------


def cached_account_gate(client: BitgetUTAClient, cfg: Config, state: StateStore, armed: bool) -> dict[str, Any]:
    if not armed:
        return {"ok": True, "skipped": "not armed"}
    now = time.time()
    cached = state.data.get("account_gate")
    if isinstance(cached, dict) and now - safe_float(cached.get("checked_at")) < 15 * 60:
        return cached
    try:
        report = verify_api_and_account(client, cfg, require_no_positions=False)
        gate = {"ok": bool(report.get("ok")), "checked_at": now, "errors": report.get("errors") or [], "warnings": report.get("warnings") or []}
    except Exception as exc:
        gate = {"ok": False, "checked_at": now, "errors": [str(exc)], "warnings": []}
    state.data["account_gate"] = gate
    return gate


def run_cycle(cfg: Config, *, force_shadow: bool = False) -> dict[str, Any]:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    client = make_client()
    bot = make_bot()
    state = StateStore(cfg.state_path)
    armed, arm_reasons = live_is_armed(cfg)
    pause_new_entries = (cfg.data_dir / "PAUSE_NEW_ENTRIES").exists()
    account_gate = cached_account_gate(client, cfg, state, armed)
    if armed and not account_gate.get("ok"):
        arm_reasons.extend(f"account_gate:{x}" for x in (account_gate.get("errors") or ["failed"]))
    live = armed and bool(account_gate.get("ok")) and not force_shadow
    assets = client.assets()
    equity, available = extract_usdt_equity_available(assets)
    guards = update_equity_guards(state, cfg, equity)

    signals: dict[str, Signal] = {}
    errors: dict[str, str] = {}
    for symbol in cfg.symbols:
        try:
            signals[symbol] = build_signal(client, cfg, state, symbol)
        except Exception as exc:
            errors[symbol] = str(exc)

    actual = current_position_rows(client, cfg)
    reconcile_uncertain_orders(client, cfg, state, actual, bot)
    reconcile_closed(client, cfg, state, actual, bot)
    unknown = unknown_positions(actual, state)
    manage_open_positions(client, cfg, state, actual, signals, live, bot)
    external_positions: list[dict[str, Any]] = []
    external_orders: list[dict[str, Any]] = []
    external_strategy_orders: list[dict[str, Any]] = []
    if cfg.require_dedicated_account:
        try:
            managed_syms = set(managed_positions(state))
            for row in all_current_positions(client):
                sym = str(row.get("symbol") or "").upper()
                expected = str((managed_positions(state).get(sym) or {}).get("side") or "").lower()
                if sym not in managed_syms or row_side(row) != expected:
                    external_positions.append(row)
        except Exception as exc:
            errors["account_positions"] = str(exc)
        try:
            for row in all_open_orders(client):
                oid = str(row.get("clientOid") or "")
                if not oid.startswith("v63-"):
                    external_orders.append(row)
        except Exception as exc:
            errors["account_orders"] = str(exc)
        try:
            for row in all_strategy_orders(client):
                sym = str(row.get("symbol") or "").upper()
                if sym not in managed_positions(state):
                    external_strategy_orders.append(row)
        except Exception as exc:
            errors["account_strategy_orders"] = str(exc)

    # Refresh after possible local exits. One cycle places at most one new order.
    actual = current_position_rows(client, cfg)
    open_count = sum(1 for rows in actual.values() if rows)
    prices = {s: sig.price for s, sig in signals.items()}
    existing_notional = sum(position_notional(rows, prices) for rows in actual.values())
    global_blockers: list[str] = []
    if errors:
        global_blockers.append("data_error")
    if unknown:
        global_blockers.append("unknown_existing_position")
    if external_positions:
        global_blockers.append("dedicated_account_external_position")
    if external_orders:
        global_blockers.append("dedicated_account_external_order")
    if external_strategy_orders:
        global_blockers.append("dedicated_account_external_strategy_order")
    if state.data.get("reconciliation_holds"):
        global_blockers.append("position_reconciliation_hold")
    if not guards["ok"]:
        global_blockers.extend(guards["reasons"])
    if open_count >= cfg.max_open_positions:
        global_blockers.append("max_open_positions")
    uncertain = state.data.get("uncertain_orders") or {}
    if uncertain:
        global_blockers.append("uncertain_order_requires_manual_review")
    if pause_new_entries:
        global_blockers.append("new_entries_paused")
    if not live:
        global_blockers.extend([f"shadow:{x}" for x in arm_reasons])

    candidates: list[Signal] = []
    for sig in signals.values():
        confirmed = confirmation_ready(state, sig, cfg)
        if not sig.entry_ready or not confirmed:
            continue
        if actual.get(sig.symbol):
            continue
        if daily_entry_count(state, sig.symbol) >= cfg.max_entries_per_symbol_per_day:
            continue
        candidates.append(sig)
    candidates.sort(key=lambda x: x.opportunity_score, reverse=True)

    entry_result: dict[str, Any] | None = None
    selected = candidates[0] if candidates else None
    if selected and not global_blockers:
        event_size = safe_float(selected.event.get("size_mult"), 1.0)
        try:
            instrument = extract_instrument(client.instruments(selected.symbol, CATEGORY), selected.symbol)
            size = risk_size(
                cfg,
                selected,
                equity,
                available,
                open_count,
                existing_notional,
                event_size,
                instrument,
            )
            if size["valid"]:
                entry_result = place_entry(client, cfg, state, selected, size, instrument, live, bot)
            else:
                entry_result = {"ok": False, "blocked": size["reason"], "size": size}
        except Exception as exc:
            entry_result = {"ok": False, "error": str(exc)}

    snapshot = {
        "ts": utc_now().isoformat(),
        "version": VERSION,
        "mode": ("LIVE_PAUSED" if live and pause_new_entries else ("LIVE" if live else "SHADOW")),
        "equity": round(equity, 6),
        "available": round(available, 6),
        "guards": guards,
        "account_gate": account_gate,
        "open_positions": {k: len(v) for k, v in actual.items()},
        "unknown_positions": unknown,
        "external_position_count": len(external_positions),
        "external_order_count": len(external_orders),
        "external_strategy_order_count": len(external_strategy_orders),
        "reconciliation_holds": state.data.get("reconciliation_holds") or {},
        "uncertain_orders": list((state.data.get("uncertain_orders") or {}).keys()),
        "existing_notional": round(existing_notional, 4),
        "signals": {k: v.to_dict() for k, v in signals.items()},
        "errors": errors,
        "candidates": [x.symbol for x in candidates],
        "selected": selected.symbol if selected else None,
        "global_blockers": global_blockers,
        "entry_result": entry_result,
    }
    append_jsonl(cfg.snapshots_path, snapshot)
    state.data["last_snapshot"] = snapshot

    last_hb = safe_float(state.data.get("last_heartbeat"), 0.0)
    if time.time() - last_hb >= cfg.heartbeat_minutes * 60:
        state.data["last_heartbeat"] = time.time()
        signal_text = " | ".join(
            f"{s.symbol} {s.mode} {s.side} T {max(s.trigger_long,s.trigger_short):.1f} edge {s.edge:.1f}"
            for s in signals.values()
        ) or "signal unavailable"
        send(
            bot,
            f"💓 <b>BTC/ETH Quant v6.3 {'LIVE_PAUSED' if live and pause_new_entries else ('LIVE' if live else 'SHADOW')}</b>\n"
            f"equity {equity:.2f} / positions {open_count}\n{signal_text}\n"
            f"guard {'OK' if guards['ok'] else 'BLOCK'} / event-aware impulse ON",
        )
    state.save()
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return snapshot


def setup_account(cfg: Config) -> dict[str, Any]:
    client = make_client()
    settings = client.settings()
    current_positions = current_position_rows(client, cfg)
    pending = {symbol: len(open_orders(client, symbol)) for symbol in cfg.symbols}
    pending_strategy = {symbol: len(strategy_orders(client, symbol)) for symbol in cfg.symbols}
    if cfg.require_dedicated_account:
        all_positions = all_current_positions(client)
        all_pending = all_open_orders(client)
        all_pending_strategy = all_strategy_orders(client)
        if all_positions:
            raise RuntimeError("dedicated-account setup requires all USDT-FUTURES positions to be closed")
        if all_pending:
            raise RuntimeError("dedicated-account setup requires all USDT-FUTURES ordinary orders to be cancelled")
        if all_pending_strategy:
            raise RuntimeError("dedicated-account setup requires all USDT-FUTURES strategy orders to be cancelled")
    if any(current_positions.values()):
        raise RuntimeError("close BTC/ETH positions before setup")
    if any(count > 0 for count in pending.values()):
        raise RuntimeError("cancel BTC/ETH pending ordinary orders before setup")
    if any(count > 0 for count in pending_strategy.values()):
        raise RuntimeError("cancel BTC/ETH TP/SL strategy orders before setup")
    actions: list[dict[str, Any]] = []
    if hold_mode(settings) != "hedge_mode":
        all_positions = all_current_positions(client)
        all_pending = all_open_orders(client)
        all_pending_strategy = strategy_orders(client)
        if all_positions:
            raise RuntimeError("all USDT-FUTURES positions must be closed before switching hedge mode")
        if all_pending:
            raise RuntimeError("all USDT-FUTURES ordinary orders must be cancelled before switching hedge mode")
        if all_pending_strategy:
            raise RuntimeError("all USDT-FUTURES TP/SL strategy orders must be cancelled before switching hedge mode")
        result = set_hold_mode(client, "hedge_mode")
        if not client.is_success(result):
            raise RuntimeError(f"set hedge_mode failed: {result}")
        actions.append({"set_hold_mode": result})
    for symbol in cfg.symbols:
        results = set_leverage(client, symbol, cfg.leverage, cfg.margin_mode)
        if not all(client.is_success(x) for x in results):
            raise RuntimeError(f"set leverage failed for {symbol}: {results}")
        actions.append({"symbol": symbol, "set_leverage": results})
    time.sleep(1.0)
    verification = verify_api_and_account(client, cfg, require_no_positions=True)
    if not verification["ok"]:
        raise RuntimeError("post-setup verification failed: " + "; ".join(verification["errors"]))
    return {"ok": True, "actions": actions, "verification": verification}


def doctor(cfg: Config, *, prearm: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {"version": VERSION, "root": str(DEFAULT_ROOT), "config": str(os.getenv("V63_CONFIG", DEFAULT_CONFIG))}
    errors: list[str] = []
    warnings: list[str] = []
    if cfg.leverage != 5:
        errors.append("leverage must be 5")
    if cfg.margin_mode != "isolated":
        errors.append("margin_mode must be isolated")
    if not (0 < cfg.risk_per_trade_pct <= 1.0):
        errors.append("risk_per_trade_pct must be in (0, 1.0]")
    if cfg.bucket_margin_ratio != 0.50:
        warnings.append("bucket_margin_ratio is not 0.50")
    if cfg.max_total_initial_margin_pct > 40:
        warnings.append("max_total_initial_margin_pct above 40% is aggressive")
    if cfg.position_absence_confirm_cycles < 2:
        errors.append("position_absence_confirm_cycles must be >= 2")
    if cfg.impulse_anti_chase_atr < cfg.anti_chase_atr:
        errors.append("impulse_anti_chase_atr must be >= anti_chase_atr")
    if not cfg.v62_snapshots_path.exists():
        warnings.append("v6.2 snapshots missing; event overlay will be stale-size only")
    if not cfg.raw_events_path.exists():
        warnings.append("raw event JSONL missing; news directional bias disabled")
    audit = security_audit(DEFAULT_ROOT)
    report["security_audit"] = audit
    if not audit["ok"]:
        errors.append("plaintext credentials detected in Python source; rotate keys and quarantine files before live")
    try:
        client = make_client()
        account = verify_api_and_account(client, cfg, require_no_positions=prearm)
        report["account"] = account
        errors.extend(account["errors"])
        warnings.extend(account["warnings"])
        adapter = order_adapter_diagnostics(client)
        report["order_adapter"] = adapter
        if not adapter.get("ok"):
            errors.append("protected order adapter self-test failed")
        market: dict[str, Any] = {}
        state = StateStore(cfg.state_path)
        for symbol in cfg.symbols:
            sig = build_signal(client, cfg, state, symbol)
            market[symbol] = {
                "price": sig.price,
                "atr1h_pct": sig.atr1h_pct,
                "regime_long": sig.regime_long,
                "regime_short": sig.regime_short,
                "trigger_long": sig.trigger_long,
                "trigger_short": sig.trigger_short,
                "mode": sig.mode,
                "event": sig.event,
            }
        report["market"] = market
    except Exception as exc:
        errors.append(str(exc))
        report["trace"] = traceback.format_exc(limit=3)
    armed, arm_reasons = live_is_armed(cfg)
    report["live_armed"] = armed
    report["arm_reasons"] = arm_reasons
    report["errors"] = errors
    report["warnings"] = warnings
    report["ok"] = not errors
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def arm_live(cfg: Config, phrase: str) -> dict[str, Any]:
    if phrase != LIVE_PHRASE:
        raise RuntimeError(f"arm phrase must be {LIVE_PHRASE}")
    required_env = {
        "V63_KEYS_ROTATED_CONFIRM": ROTATION_PHRASE,
        "V63_NO_WITHDRAW_CONFIRM": NO_WITHDRAW_PHRASE,
        "V63_IP_WHITELIST_CONFIRM": IP_WHITELIST_PHRASE,
    }
    missing = [name for name, expected in required_env.items() if os.getenv(name, "").strip() != expected]
    if missing:
        raise RuntimeError("security confirmations missing: " + ", ".join(missing))
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    preflight = doctor(cfg, prearm=True)
    if not preflight.get("ok"):
        raise RuntimeError("pre-arm doctor failed: " + "; ".join(preflight.get("errors") or ["unknown error"]))
    pause = cfg.data_dir / "PAUSE_NEW_ENTRIES"
    pause.unlink(missing_ok=True)
    arm_file = cfg.data_dir / "LIVE_ARMED"
    arm_file.write_text(LIVE_PHRASE + "\n", encoding="utf-8")
    os.chmod(arm_file, 0o600)
    result = {"ok": True, "armed": True, "arm_file": str(arm_file), "version": VERSION}
    append_jsonl(cfg.events_path, {"ts": utc_now().isoformat(), "event": "live_armed", "result": result})
    send(make_bot(), "🟢 <b>BTC/ETH Quant v6.3 LIVE ARMED</b>\n5x isolated / BTC+ETH / risk-sized 50:50 caps")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def disarm_live(cfg: Config, reason: str = "manual") -> dict[str, Any]:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "LIVE_ARMED").unlink(missing_ok=True)
    pause = cfg.data_dir / "PAUSE_NEW_ENTRIES"
    pause.write_text(reason + "\n", encoding="utf-8")
    os.chmod(pause, 0o600)
    result = {"ok": True, "armed": False, "new_entries_paused": True, "reason": reason}
    append_jsonl(cfg.events_path, {"ts": utc_now().isoformat(), "event": "live_disarmed", "result": result})
    send(make_bot(), f"🟡 <b>BTC/ETH Quant v6.3 DISARMED</b>\nnew entries paused / existing managed positions stay protected and monitored\nreason: {reason}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def panic_flat(cfg: Config, phrase: str) -> dict[str, Any]:
    if phrase != PANIC_PHRASE:
        raise RuntimeError(f"panic phrase must be {PANIC_PHRASE}")
    disarm_live(cfg, "panic_flat")
    client = make_client()
    results: dict[str, Any] = {}
    for symbol in cfg.symbols:
        ordinary_before: Any
        try:
            ordinary_before = cancel_symbol_orders(client, symbol)
        except Exception as exc:
            ordinary_before = {"error": str(exc)}
        strategies_before = cancel_all_strategy_orders(client, symbol)
        try:
            close = close_all_symbol(client, symbol)
        except Exception as exc:
            close = {"error": str(exc)}
        results[symbol] = {"ordinary_cancel_before": ordinary_before, "strategy_cancel_before": strategies_before, "close": close}
    deadline = time.time() + 20
    remaining: dict[str, list[dict[str, Any]]] = {}
    while time.time() < deadline:
        remaining = current_position_rows(client, cfg)
        if not any(remaining.values()):
            break
        time.sleep(1)
    for symbol in cfg.symbols:
        results[symbol]["strategy_cancel_after"] = cancel_all_strategy_orders(client, symbol)
        if remaining.get(symbol):
            try:
                results[symbol]["second_close"] = close_all_symbol(client, symbol)
            except Exception as exc:
                results[symbol]["second_close"] = {"error": str(exc)}
    time.sleep(2)
    final_positions = current_position_rows(client, cfg)
    final_strategies: dict[str, Any] = {}
    for symbol in cfg.symbols:
        try:
            final_strategies[symbol] = len(strategy_orders(client, symbol))
        except Exception as exc:
            final_strategies[symbol] = {"error": str(exc)}
    out = {
        "ok": (
            not any(final_positions.values())
            and all(isinstance(v, int) and v == 0 for v in final_strategies.values())
        ),
        "results": results,
        "remaining_positions": {k: len(v) for k, v in final_positions.items()},
        "remaining_strategy_orders": final_strategies,
    }
    append_jsonl(cfg.events_path, {"ts": utc_now().isoformat(), "event": "panic_flat", "result": out})
    send(make_bot(), f"🚨 <b>V6.3 PANIC FLAT</b>\nflat verification: {'OK' if out['ok'] else 'FAILED - MANUAL CHECK'}")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def report(cfg: Config, days: int = 30) -> dict[str, Any]:
    cutoff = utc_now() - dt.timedelta(days=days)
    exits: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    for line in tail_lines(cfg.events_path, 200_000):
        try:
            obj = json.loads(line)
        except Exception:
            continue
        ts = iso_to_dt(obj.get("ts"))
        if not ts or ts < cutoff:
            continue
        if obj.get("event") == "entry":
            entries.append(obj)
        elif obj.get("event") == "exit":
            exits.append(obj)

    def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        pnl = [safe_float(x.get("net_profit")) for x in rows]
        rs = [safe_float(x.get("r_multiple")) for x in rows]
        wins = [x for x in pnl if x > 0]
        losses = [x for x in pnl if x < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        pf = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)
        return {
            "closed": len(rows),
            "wins": len(wins),
            "win_rate_pct": round(len(wins) / len(rows) * 100.0, 2) if rows else 0.0,
            "net_profit_usdt": round(sum(pnl), 6),
            "gross_profit_usdt": round(gross_win, 6),
            "gross_loss_usdt": round(gross_loss, 6),
            "profit_factor": round(pf, 3),
            "avg_win_usdt": round(statistics.fmean(wins), 6) if wins else 0.0,
            "avg_loss_usdt": round(statistics.fmean(losses), 6) if losses else 0.0,
            "avg_r": round(statistics.fmean(rs), 4) if rs else 0.0,
            "median_r": round(statistics.median(rs), 4) if rs else 0.0,
            "best_r": round(max(rs), 4) if rs else 0.0,
            "worst_r": round(min(rs), 4) if rs else 0.0,
        }

    def grouped(field: str) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in exits:
            key = str(row.get(field) or "UNKNOWN").upper()
            groups.setdefault(key, []).append(row)
        return {k: stats(v) for k, v in sorted(groups.items())}

    reasons: dict[str, int] = {}
    for x in exits:
        reason = str(x.get("reason") or "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for row in sorted(exits, key=lambda x: str(x.get("ts") or "")):
        running += safe_float(row.get("net_profit"))
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    state = StateStore(cfg.state_path).data
    out = {
        "version": VERSION,
        "days": days,
        "entries": len(entries),
        "overall": stats(exits),
        "by_symbol": grouped("symbol"),
        "by_side": grouped("side"),
        "by_mode": grouped("mode"),
        "exit_reasons": reasons,
        "realized_curve_max_drawdown_usdt": round(max_dd, 6),
        "consecutive_losses": int(state.get("consecutive_losses", 0)),
        "cooldown_until": state.get("cooldown_until"),
        "managed_positions": list((state.get("managed_positions") or {}).keys()),
        "uncertain_orders": list((state.get("uncertain_orders") or {}).keys()),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return out


def self_test() -> dict[str, Any]:
    tests: dict[str, bool] = {}
    tests["clamp"] = clamp(5, 0, 3) == 3 and clamp(-1, 0, 3) == 0
    tests["pct_change"] = abs(pct_change(101, 100) - 1.0) < 1e-9
    tests["protection_explicit"] = protection_status([{"planType": "profit_plan"}, {"planType": "loss_plan"}])["ok"]
    tests["protection_generic_pair"] = protection_status([{"orderId": "1"}, {"orderId": "2"}])["ok"]
    tests["protection_single_missing"] = not protection_status([{"planType": "profit_plan"}])["ok"]
    tests["protection_official_single_row"] = protection_status([{"takeProfit": "110000", "stopLoss": "90000", "status": "pending"}])["ok"]
    tests["embedded_position_parser"] = len(open_positions({"code": "00000", "data": [{"symbol": "BTCUSDT", "posSide": "long", "total": "0.01"}]}, symbol="BTCUSDT")) == 1
    tests["embedded_asset_parser"] = extract_usdt_equity_available({"code": "00000", "data": [{"coin": "USDT", "accountEquity": "1000", "available": "900"}]}) == (1000.0, 900.0)
    tests["order_status_normalization"] = (
        order_status_kind({"orderStatus": "partial_fill"}) == "partial"
        and order_status_kind({"orderStatus": "filled"}) == "filled"
        and order_status_kind({"orderStatus": "cancelled"}) == "terminal_failure"
    )
    fake_cfg = Config(
        symbols={
            "BTCUSDT": SymbolConfig(19_900_000, 1.05, 0.0055, 0.0100),
            "ETHUSDT": SymbolConfig(120_700_000, 1.10, 0.0070, 0.0140),
        },
        loop_seconds=60,
        leverage=5,
        margin_mode="isolated",
        bucket_margin_ratio=0.5,
        risk_per_trade_pct=0.6,
        both_positions_risk_pct_each=0.4,
        max_total_initial_margin_pct=40,
        max_total_notional_equity_ratio=2.0,
        max_open_positions=2,
        max_new_positions_per_cycle=1,
        max_entries_per_symbol_per_day=3,
        daily_loss_block_pct=2,
        weekly_loss_block_pct=5,
        max_drawdown_block_pct=6,
        cooldown_after_loss_minutes=60,
        cooldown_after_two_losses_minutes=240,
        trend_regime_threshold=55,
        trend_entry_threshold=44,
        impulse_entry_threshold=30,
        min_edge=14,
        impulse_min_edge=16,
        impulse_volume_z=1.2,
        impulse_move_atr=0.50,
        impulse_confirm_cycles=2,
        anti_chase_atr=1.25,
        impulse_anti_chase_atr=1.55,
        oi_confirm_pct=0.08,
        event_caution_risk=30,
        event_high_risk=55,
        event_hard_block_risk=75,
        event_state_max_age_minutes=20,
        event_stale_size_mult=0.35,
        event_caution_size_mult=0.75,
        event_high_size_mult=0.40,
        news_bias_max=6,
        news_require_providers=2,
        news_fresh_minutes=90,
        tp_r_trend=2,
        tp_r_impulse=1.6,
        no_followthrough_minutes=25,
        no_followthrough_mfe_r=0.25,
        time_stop_hours_trend=8,
        time_stop_hours_impulse=4,
        trail_activate_r=1.10,
        trail_atr_mult=0.85,
        heartbeat_minutes=60,
        position_absence_confirm_cycles=2,
        require_dedicated_account=True,
        data_dir=Path("/tmp/v63"),
        log_path=Path("/tmp/v63.log"),
        state_path=Path("/tmp/v63/state.json"),
        snapshots_path=Path("/tmp/v63/s.jsonl"),
        trades_path=Path("/tmp/v63/t.csv"),
        events_path=Path("/tmp/v63/e.jsonl"),
        v62_snapshots_path=Path("/tmp/x"),
        raw_events_path=Path("/tmp/y"),
    )
    sig = Signal(
        symbol="BTCUSDT",
        ts="",
        price=100,
        atr1h=1,
        atr1h_pct=1,
        ema20_1h=99,
        ema50_1h=98,
        ema20_4h=97,
        ema50_4h=96,
        regime_long=90,
        regime_short=0,
        trigger_long=60,
        trigger_short=0,
        edge=60,
        side="LONG",
        mode="TREND",
        entry_ready=True,
        volume_z5=2,
        momentum_atr=1,
        ret_1h_pct=1,
        breakout_long=True,
        breakout_short=False,
        anti_chase_distance_atr=1,
        price_response_efficiency=0.8,
        close_location=0.9,
        rejection_wick_ratio=0.1,
        oi=1,
        oi_15m_pct=0.2,
        funding_rate=0,
        turnover24h=1,
        market_cap_proxy=1,
        turnover_cap_ratio=1,
        opportunity_score=1,
        event={"size_mult": 1},
        blockers=[],
    )
    stop_pct, stop, take = planned_stop_and_tp(fake_cfg, sig)
    tests["long_stop_tp"] = stop < sig.price < take and abs(stop_pct - 0.010) < 1e-12
    sig.side = "SHORT"
    stop_pct, stop, take = planned_stop_and_tp(fake_cfg, sig)
    tests["short_stop_tp"] = take < sig.price < stop
    ok = all(tests.values())
    out = {"ok": ok, "tests": tests, "version": VERSION}
    print(json.dumps(out, indent=2))
    if not ok:
        raise SystemExit(1)
    return out


def acquire_engine_lock(cfg: Config) -> Any:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.data_dir / "engine.lock"
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"another v6.3 engine process holds {path}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()} started={utc_now().isoformat()}\n")
    handle.flush()
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BTC/ETH Quant v6.3 dual live executor")
    parser.add_argument("command", choices=["once", "loop", "doctor", "setup", "arm", "disarm", "report", "panic-flat", "self-test"])
    parser.add_argument("--config", default=os.getenv("V63_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--force-shadow", action="store_true")
    parser.add_argument("--prearm", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--phrase", default="")
    parser.add_argument("--reason", default="manual")
    args = parser.parse_args(argv)
    load_dotenv_safely(DEFAULT_ROOT / ".env")
    if args.command == "self-test":
        self_test()
        return 0
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = DEFAULT_ROOT / cfg_path
    cfg = load_config(cfg_path)
    if args.command == "doctor":
        return 0 if doctor(cfg, prearm=args.prearm)["ok"] else 1
    if args.command == "setup":
        print(json.dumps(setup_account(cfg), ensure_ascii=False, indent=2))
        return 0
    if args.command == "arm":
        arm_live(cfg, args.phrase)
        return 0
    if args.command == "disarm":
        disarm_live(cfg, args.reason)
        return 0
    if args.command == "report":
        report(cfg, args.days)
        return 0
    if args.command == "panic-flat":
        panic_flat(cfg, args.phrase)
        return 0
    if args.command == "once":
        _lock = acquire_engine_lock(cfg)
        run_cycle(cfg, force_shadow=args.force_shadow)
        return 0
    _lock = acquire_engine_lock(cfg)
    while True:
        started = time.time()
        try:
            run_cycle(cfg, force_shadow=args.force_shadow)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            payload = {
                "ts": utc_now().isoformat(),
                "event": "cycle_error",
                "error": str(exc),
                "trace": traceback.format_exc(limit=8),
            }
            append_jsonl(cfg.events_path, payload)
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)
            send(make_bot(), f"🚨 <b>V6.3 cycle error</b>\n{str(exc)[:500]}")
        elapsed = time.time() - started
        time.sleep(max(1.0, cfg.loop_seconds - elapsed))


if __name__ == "__main__":
    raise SystemExit(main())
