from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

try:
    from index_sniper.exchange.bitget_uta import BitgetUTAClient
except Exception:  # pragma: no cover
    BitgetUTAClient = None  # type: ignore

try:
    from index_sniper.telegram.bot import TelegramBot
except Exception:  # pragma: no cover
    TelegramBot = None  # type: ignore

try:
    from index_sniper.strategy.indicators import Candle, ema, atr, parse_candles
except Exception:  # pragma: no cover
    Candle = Any  # type: ignore
    ema = None  # type: ignore
    atr = None  # type: ignore
    parse_candles = None  # type: ignore

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))

DEFAULT_STATE_PATH = "data/signal_lab_state.json"
DEFAULT_SNAPSHOTS_PATH = "research/signal_lab_snapshots.csv"
DEFAULT_SIGNALS_PATH = "research/signal_lab_signals.csv"
DEFAULT_TRADES_PATH = "research/signal_lab_paper_trades.csv"
DEFAULT_EVENTS_PATH = "research/signal_lab_events.jsonl"
DEFAULT_REPORT_PATH = "research/signal_lab_report_latest.txt"
DEFAULT_LOG_PATH = "logs/signal-lab.log"


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def _csv_list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_kst() -> datetime:
    return datetime.now(KST)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now_utc()).astimezone(timezone.utc).isoformat()


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _day_key_kst09(now: datetime | None = None) -> str:
    n = (now or _now_utc()).astimezone(KST)
    reset = n.replace(hour=9, minute=0, second=0, microsecond=0)
    if n < reset:
        reset -= timedelta(days=1)
    return reset.strftime("%Y-%m-%d_09KST")


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _mean(xs: Iterable[float]) -> float:
    vals = list(xs)
    return sum(vals) / len(vals) if vals else 0.0


def _std(xs: Iterable[float]) -> float:
    vals = list(xs)
    if len(vals) < 2:
        return 0.0
    m = _mean(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / (len(vals) - 1))


def _zscore(x: float, hist: list[float]) -> float:
    vals = [v for v in hist if math.isfinite(v)]
    if len(vals) < 8:
        return 0.0
    m = _mean(vals)
    s = _std(vals)
    if s <= 0:
        return 0.0
    return (x - m) / s


def _pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new / old - 1.0) * 100.0


def _fmt(x: Any, n: int = 4) -> str:
    try:
        v = float(x)
    except Exception:
        return str(x)
    if abs(v) >= 1000:
        return f"{v:,.1f}"
    if abs(v) >= 1:
        return f"{v:.{n}f}"
    return f"{v:.8f}".rstrip("0").rstrip(".")


def _read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return default


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def _append_csv(path: Path, row: dict[str, Any], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ExitPlan:
    name: str
    tp_pct: float
    sl_pct: float
    max_hold_minutes: int


@dataclass
class SignalLabSettings:
    symbol: str
    category: str
    loop_seconds: int
    snapshot_interval_seconds: int
    signal_threshold: float
    weak_threshold: float
    min_edge: float
    cooldown_minutes: int
    max_active_per_side: int
    min_price_move_bps: float
    exit_plans: list[ExitPlan]
    state_path: str
    snapshots_path: str
    signals_path: str
    trades_path: str
    events_path: str
    report_path: str
    notify: bool
    notify_signals: bool
    notify_closes: bool
    notify_summary_minutes: int
    report_days: int
    # score weights / controls
    funding_weight: float
    oi_weight: float
    risk_atr_soft_pct: float
    risk_atr_hard_pct: float
    score_window_minutes: int


def _parse_exit_plans(raw: str) -> list[ExitPlan]:
    # Format: name:tp:sl:max_hold_minutes,name:tp:sl:max_hold_minutes
    plans: list[ExitPlan] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(":")]
        if len(parts) != 4:
            continue
        try:
            plans.append(ExitPlan(parts[0], float(parts[1]), float(parts[2]), int(float(parts[3]))))
        except Exception:
            continue
    if not plans:
        plans = [
            ExitPlan("tp05_sl03_2h", 0.005, 0.003, 120),
            ExitPlan("tp08_sl04_4h", 0.008, 0.004, 240),
            ExitPlan("tp12_sl06_8h", 0.012, 0.006, 480),
            ExitPlan("tp20_sl08_12h", 0.020, 0.008, 720),
        ]
    return plans


def load_settings() -> SignalLabSettings:
    load_dotenv(ROOT / ".env")
    default_plans = "tp05_sl03_2h:0.005:0.003:120,tp08_sl04_4h:0.008:0.004:240,tp12_sl06_8h:0.012:0.006:480,tp20_sl08_12h:0.020:0.008:720"
    return SignalLabSettings(
        symbol=os.getenv("V60_SYMBOL", "BTCUSDT").strip().upper(),
        category=os.getenv("V60_CATEGORY", os.getenv("CATEGORY", "USDT-FUTURES")).strip(),
        loop_seconds=_int("V60_LOOP_SECONDS", 300),
        snapshot_interval_seconds=_int("V60_SNAPSHOT_INTERVAL_SECONDS", 300),
        signal_threshold=_float("V60_SIGNAL_THRESHOLD", 70.0),
        weak_threshold=_float("V60_WEAK_THRESHOLD", 50.0),
        min_edge=_float("V60_MIN_EDGE", 15.0),
        cooldown_minutes=_int("V60_COOLDOWN_MINUTES", 30),
        max_active_per_side=_int("V60_MAX_ACTIVE_PER_SIDE", 1),
        min_price_move_bps=_float("V60_MIN_PRICE_MOVE_BPS", 2.0),
        exit_plans=_parse_exit_plans(os.getenv("V60_EXIT_PLANS", default_plans)),
        state_path=os.getenv("V60_STATE_PATH", DEFAULT_STATE_PATH),
        snapshots_path=os.getenv("V60_SNAPSHOTS_PATH", DEFAULT_SNAPSHOTS_PATH),
        signals_path=os.getenv("V60_SIGNALS_PATH", DEFAULT_SIGNALS_PATH),
        trades_path=os.getenv("V60_TRADES_PATH", DEFAULT_TRADES_PATH),
        events_path=os.getenv("V60_EVENTS_PATH", DEFAULT_EVENTS_PATH),
        report_path=os.getenv("V60_REPORT_PATH", DEFAULT_REPORT_PATH),
        notify=_bool(os.getenv("V60_NOTIFY"), True),
        notify_signals=_bool(os.getenv("V60_NOTIFY_SIGNALS"), True),
        notify_closes=_bool(os.getenv("V60_NOTIFY_CLOSES"), True),
        notify_summary_minutes=_int("V60_NOTIFY_SUMMARY_MINUTES", 60),
        report_days=_int("V60_REPORT_DAYS", 14),
        funding_weight=_float("V60_FUNDING_WEIGHT", 8.0),
        oi_weight=_float("V60_OI_WEIGHT", 8.0),
        risk_atr_soft_pct=_float("V60_RISK_ATR_SOFT_PCT", 1.2),
        risk_atr_hard_pct=_float("V60_RISK_ATR_HARD_PCT", 2.4),
        score_window_minutes=_int("V60_SCORE_WINDOW_MINUTES", 60),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Data access
# ─────────────────────────────────────────────────────────────────────────────


def make_client() -> Any:
    if BitgetUTAClient is None:
        raise RuntimeError("BitgetUTAClient import failed")
    return BitgetUTAClient(
        api_key=os.getenv("BITGET_API_KEY", ""),
        secret_key=os.getenv("BITGET_SECRET_KEY", ""),
        passphrase=os.getenv("BITGET_PASSPHRASE", ""),
    )


def make_bot() -> Any | None:
    if TelegramBot is None:
        return None
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return None
    return TelegramBot(token, chat_id)


def _send(text: str, settings: SignalLabSettings | None = None) -> None:
    if settings and not settings.notify:
        return
    bot = make_bot()
    if bot is None:
        return
    try:
        bot.send(text)
    except Exception:
        pass


def _parse_bitget_payload(data: dict[str, Any]) -> Any:
    payload = data.get("data")
    if isinstance(payload, dict) and "list" in payload:
        return payload.get("list")
    return payload


def _extract_row(data: dict[str, Any]) -> dict[str, Any] | None:
    payload = _parse_bitget_payload(data)
    if isinstance(payload, list):
        return payload[0] if payload and isinstance(payload[0], dict) else None
    if isinstance(payload, dict):
        return payload
    return None


def fetch_candles(client: Any, settings: SignalLabSettings, interval: str, limit: int) -> list[Any]:
    data = client.candles(settings.symbol, settings.category, interval, limit)
    if parse_candles is None:
        raise RuntimeError("parse_candles unavailable")
    rows = parse_candles(data)
    if len(rows) < min(30, limit // 3):
        raise RuntimeError(f"not enough candles {settings.symbol} {interval}: {len(rows)}")
    return rows


def fetch_price(client: Any, settings: SignalLabSettings) -> float:
    try:
        return float(client.last_price(settings.symbol, settings.category))
    except Exception:
        row = _extract_row(client.tickers(settings.symbol, settings.category))
        if not row:
            raise
        for k in ("lastPr", "lastPrice", "price", "close", "markPrice"):
            if row.get(k) not in (None, ""):
                return float(row[k])
        raise RuntimeError(f"ticker missing price: {row}")


def _try_public_get(client: Any, paths: list[tuple[str, dict[str, Any]]]) -> tuple[dict[str, Any] | None, str]:
    last_err = ""
    for path, params in paths:
        try:
            data = client.get(path, params, auth=False)
            if str(data.get("code")) in {"00000", "0"} or data.get("data") is not None:
                return data, path
        except Exception as exc:
            last_err = f"{path}: {exc}"
    return None, last_err


def fetch_funding_rate(client: Any, settings: SignalLabSettings) -> tuple[float | None, str]:
    symbol = settings.symbol
    category = settings.category
    product_type = "USDT-FUTURES"
    paths = [
        ("/api/v3/market/current-fund-rate", {"category": category, "symbol": symbol}),
        ("/api/v3/market/funding-rate", {"category": category, "symbol": symbol}),
        ("/api/v2/mix/market/current-fund-rate", {"symbol": symbol, "productType": product_type}),
        ("/api/v2/mix/market/current-fund-rate", {"symbol": symbol.replace("USDT", "USDT"), "productType": "USDT-FUTURES"}),
    ]
    data, source = _try_public_get(client, paths)
    if data is None:
        return None, source
    row = _extract_row(data)
    if not row:
        return None, source
    for k in ("fundingRate", "fundRate", "rate", "currentFundingRate"):
        if row.get(k) not in (None, ""):
            return float(row[k]), source
    return None, source


def fetch_open_interest(client: Any, settings: SignalLabSettings) -> tuple[float | None, str]:
    symbol = settings.symbol
    category = settings.category
    product_type = "USDT-FUTURES"
    paths = [
        ("/api/v3/market/open-interest", {"category": category, "symbol": symbol}),
        ("/api/v3/market/open-interest", {"symbol": symbol, "category": category}),
        ("/api/v2/mix/market/open-interest", {"symbol": symbol, "productType": product_type}),
    ]
    data, source = _try_public_get(client, paths)
    if data is None:
        return None, source
    row = _extract_row(data)
    if not row:
        return None, source
    for k in ("openInterest", "openInterestAmount", "amount", "oi", "size"):
        if row.get(k) not in (None, ""):
            return float(row[k]), source
    return None, source


# ─────────────────────────────────────────────────────────────────────────────
# Feature engineering / scores
# ─────────────────────────────────────────────────────────────────────────────


def _closes(candles: list[Any]) -> list[float]:
    return [float(c.close) for c in candles]


def _volumes(candles: list[Any]) -> list[float]:
    return [float(c.volume) for c in candles]


def _ret(candles: list[Any], bars: int) -> float:
    if len(candles) <= bars:
        return 0.0
    return _pct_change(float(candles[-1].close), float(candles[-1 - bars].close))


def _ema_val(candles: list[Any], period: int) -> float | None:
    if ema is None:
        return None
    return ema(_closes(candles), period)


def _atr_pct(candles: list[Any], period: int = 14) -> float:
    if atr is None or not candles:
        return 0.0
    a = atr(candles, period) or 0.0
    price = float(candles[-1].close)
    return (a / price * 100.0) if price > 0 else 0.0


def _volume_z(candles: list[Any], lookback: int = 48) -> float:
    if len(candles) < lookback + 1:
        return 0.0
    vols = _volumes(candles)
    hist = vols[-lookback-1:-1]
    return _zscore(vols[-1], hist)


def _wick_risk(candle: Any) -> float:
    o = float(candle.open)
    h = float(candle.high)
    l = float(candle.low)
    c = float(candle.close)
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    wick_ratio = 1.0 - (body / rng)
    return _clamp((wick_ratio - 0.55) * 20.0, 0.0, 8.0)


def _find_snapshot_before(history: list[dict[str, Any]], minutes: int) -> dict[str, Any] | None:
    if not history:
        return None
    target = _now_utc() - timedelta(minutes=minutes)
    best = None
    for row in history:
        try:
            ts = _dt(str(row.get("ts")))
        except Exception:
            continue
        if ts <= target:
            best = row
        else:
            break
    return best


def build_features(client: Any, settings: SignalLabSettings, state: dict[str, Any]) -> dict[str, Any]:
    c5 = fetch_candles(client, settings, "5m", 220)
    c15 = fetch_candles(client, settings, "15m", 220)
    c1h = fetch_candles(client, settings, "1H", 320)
    c4h = fetch_candles(client, settings, "4H", 320)
    price = fetch_price(client, settings)

    funding, funding_source = fetch_funding_rate(client, settings)
    oi, oi_source = fetch_open_interest(client, settings)

    # Use stored history for funding z-score and OI changes.
    market_hist = state.setdefault("market_history", [])
    funding_hist = [float(x.get("funding_rate")) for x in market_hist if x.get("funding_rate") not in (None, "")]
    funding_rate = funding if funding is not None else 0.0
    funding_z = _zscore(funding_rate, funding_hist[-200:])

    oi4_row = _find_snapshot_before(market_hist, 240)
    oi24_row = _find_snapshot_before(market_hist, 1440)
    oi_now = oi if oi is not None else 0.0
    oi_4h_pct = _pct_change(oi_now, _safe_float(oi4_row.get("oi") if oi4_row else None, oi_now)) if oi_now else 0.0
    oi_24h_pct = _pct_change(oi_now, _safe_float(oi24_row.get("oi") if oi24_row else None, oi_now)) if oi_now else 0.0

    r5_3 = _ret(c5, 3)
    r5_12 = _ret(c5, 12)
    r15_4 = _ret(c15, 4)
    r1_4 = _ret(c1h, 4)
    r1_24 = _ret(c1h, 24)
    r4_6 = _ret(c4h, 6)
    r4_18 = _ret(c4h, 18)

    ema1_20 = _ema_val(c1h, 20)
    ema1_60 = _ema_val(c1h, 60)
    ema4_20 = _ema_val(c4h, 20)
    ema4_60 = _ema_val(c4h, 60)
    ema4_120 = _ema_val(c4h, 120)

    # Directional trend scores.
    trend_long = 0.0
    trend_short = 0.0
    if ema4_20 and ema4_60:
        if price > ema4_20 > ema4_60:
            trend_long += 22
        elif price < ema4_20 < ema4_60:
            trend_short += 22
        elif ema4_20 > ema4_60:
            trend_long += 12
        elif ema4_20 < ema4_60:
            trend_short += 12
    if ema1_20 and ema1_60:
        if price > ema1_20 > ema1_60:
            trend_long += 15
        elif price < ema1_20 < ema1_60:
            trend_short += 15
        elif ema1_20 > ema1_60:
            trend_long += 8
        elif ema1_20 < ema1_60:
            trend_short += 8
    if ema4_120:
        if price > ema4_120:
            trend_long += 6
        elif price < ema4_120:
            trend_short += 6

    # Momentum is asymmetric: positive returns boost long, negative returns boost short.
    mom_raw = (r5_3 * 3.0) + (r5_12 * 2.5) + (r15_4 * 2.5) + (r1_4 * 2.0) + (r1_24 * 1.2) + (r4_6 * 1.0)
    mom_long = _clamp(mom_raw, 0.0, 30.0)
    mom_short = _clamp(-mom_raw, 0.0, 30.0)

    v5_z = _volume_z(c5, 48)
    v15_z = _volume_z(c15, 48)
    vol_impulse = _clamp((v5_z * 3.0) + (v15_z * 2.0), -12.0, 18.0)
    short_ret = r5_12 + r15_4
    volume_long = max(0.0, vol_impulse) if short_ret > 0 else min(0.0, vol_impulse) * 0.25
    volume_short = max(0.0, vol_impulse) if short_ret < 0 else min(0.0, vol_impulse) * 0.25
    volume_long = _clamp(volume_long, -5.0, 16.0)
    volume_short = _clamp(volume_short, -5.0, 16.0)

    # Funding crowding. Positive funding penalizes longs and can help shorts; negative does the reverse.
    fz = _clamp(funding_z, -3.0, 3.0)
    funding_long = _clamp(-fz * settings.funding_weight, -18.0, 18.0)
    funding_short = _clamp(fz * settings.funding_weight, -18.0, 18.0)

    # OI confirmation: price direction + OI expansion supports continuation; OI contraction weakens it.
    oi4 = _clamp(oi_4h_pct, -5.0, 5.0)
    price_dir = 1 if r1_4 > 0 else (-1 if r1_4 < 0 else 0)
    oi_long = 0.0
    oi_short = 0.0
    if price_dir > 0:
        oi_long += _clamp(oi4 * settings.oi_weight, -16.0, 16.0)
        oi_short -= _clamp(oi4 * settings.oi_weight * 0.5, -8.0, 8.0)
    elif price_dir < 0:
        oi_short += _clamp(oi4 * settings.oi_weight, -16.0, 16.0)
        oi_long -= _clamp(oi4 * settings.oi_weight * 0.5, -8.0, 8.0)

    # Liquidation/reversal proxies: very fast move + volume + OI contraction.
    liq_long = 0.0
    liq_short = 0.0
    if r1_4 < -1.5 and v5_z > 1.0 and oi_4h_pct < -0.2:
        liq_long += _clamp(abs(r1_4) * 4.0 + v5_z * 2.0, 0.0, 18.0)
    if r1_4 > 1.5 and v5_z > 1.0 and oi_4h_pct < -0.2:
        liq_short += _clamp(abs(r1_4) * 4.0 + v5_z * 2.0, 0.0, 18.0)

    atr5_pct = _atr_pct(c5, 14)
    atr1h_pct = _atr_pct(c1h, 14)
    risk_penalty = 0.0
    if atr1h_pct > settings.risk_atr_soft_pct:
        risk_penalty += (atr1h_pct - settings.risk_atr_soft_pct) * 12.0
    if atr1h_pct > settings.risk_atr_hard_pct:
        risk_penalty += 20.0
    risk_penalty += _wick_risk(c5[-1])
    risk_penalty = _clamp(risk_penalty, 0.0, 35.0)

    long_score = trend_long + mom_long + volume_long + funding_long + oi_long + liq_long - risk_penalty
    short_score = trend_short + mom_short + volume_short + funding_short + oi_short + liq_short - risk_penalty
    long_score = round(_clamp(long_score, 0.0, 100.0), 4)
    short_score = round(_clamp(short_score, 0.0, 100.0), 4)

    state_label = "NEUTRAL"
    candidate_side = "NONE"
    if long_score >= settings.signal_threshold and (long_score - short_score) >= settings.min_edge:
        state_label = "PAPER_LONG_CANDIDATE"
        candidate_side = "LONG"
    elif short_score >= settings.signal_threshold and (short_score - long_score) >= settings.min_edge:
        state_label = "PAPER_SHORT_CANDIDATE"
        candidate_side = "SHORT"
    elif long_score >= settings.weak_threshold and (long_score - short_score) >= 5:
        state_label = "WEAK_LONG"
    elif short_score >= settings.weak_threshold and (short_score - long_score) >= 5:
        state_label = "WEAK_SHORT"

    features = {
        "ts": _iso(),
        "day_key": _day_key_kst09(),
        "symbol": settings.symbol,
        "price": price,
        "state": state_label,
        "candidate_side": candidate_side,
        "long_score": long_score,
        "short_score": short_score,
        "edge": round(long_score - short_score, 4),
        "trend_long": round(trend_long, 4),
        "trend_short": round(trend_short, 4),
        "momentum_long": round(mom_long, 4),
        "momentum_short": round(mom_short, 4),
        "momentum_raw": round(mom_raw, 4),
        "volume_long": round(volume_long, 4),
        "volume_short": round(volume_short, 4),
        "volume_z_5m": round(v5_z, 4),
        "volume_z_15m": round(v15_z, 4),
        "funding_rate": funding_rate,
        "funding_z": round(funding_z, 6),
        "funding_long": round(funding_long, 4),
        "funding_short": round(funding_short, 4),
        "funding_source": funding_source,
        "oi": oi_now,
        "oi_4h_pct": round(oi_4h_pct, 6),
        "oi_24h_pct": round(oi_24h_pct, 6),
        "oi_long": round(oi_long, 4),
        "oi_short": round(oi_short, 4),
        "oi_source": oi_source,
        "liq_long": round(liq_long, 4),
        "liq_short": round(liq_short, 4),
        "risk_penalty": round(risk_penalty, 4),
        "atr5_pct": round(atr5_pct, 6),
        "atr1h_pct": round(atr1h_pct, 6),
        "ret5m_3": round(r5_3, 6),
        "ret5m_12": round(r5_12, 6),
        "ret15m_4": round(r15_4, 6),
        "ret1h_4": round(r1_4, 6),
        "ret1h_24": round(r1_24, 6),
        "ret4h_6": round(r4_6, 6),
        "ret4h_18": round(r4_18, 6),
    }

    # Save compact market history for OI/funding deltas. Keep 7 days at 5m = about 2016 rows.
    market_hist.append({"ts": features["ts"], "price": price, "funding_rate": funding_rate, "oi": oi_now})
    market_hist = market_hist[-2500:]
    state["market_history"] = market_hist

    return features


# ─────────────────────────────────────────────────────────────────────────────
# Paper trade engine
# ─────────────────────────────────────────────────────────────────────────────


SNAPSHOT_FIELDS = [
    "ts", "day_key", "symbol", "price", "state", "candidate_side", "long_score", "short_score", "edge",
    "trend_long", "trend_short", "momentum_long", "momentum_short", "momentum_raw", "volume_long", "volume_short",
    "volume_z_5m", "volume_z_15m", "funding_rate", "funding_z", "funding_long", "funding_short",
    "oi", "oi_4h_pct", "oi_24h_pct", "oi_long", "oi_short", "liq_long", "liq_short", "risk_penalty",
    "atr5_pct", "atr1h_pct", "ret5m_3", "ret5m_12", "ret15m_4", "ret1h_4", "ret1h_24", "ret4h_6", "ret4h_18",
]

SIGNAL_FIELDS = SNAPSHOT_FIELDS + ["signal_id", "side", "created_paper_trades", "exit_plans"]

TRADE_FIELDS = [
    "trade_id", "signal_id", "symbol", "side", "plan", "entry_ts", "exit_ts", "entry_price", "exit_price",
    "tp_price", "sl_price", "tp_pct", "sl_pct", "max_hold_minutes", "exit_reason", "pnl_pct", "mfe_pct", "mae_pct",
    "hold_minutes", "entry_long_score", "entry_short_score", "entry_edge", "entry_state", "entry_funding_rate",
    "entry_funding_z", "entry_oi_4h_pct", "entry_oi_24h_pct", "entry_risk_penalty", "entry_volume_z_5m", "entry_atr1h_pct",
]


def _side_active_count(active: list[dict[str, Any]], side: str) -> int:
    return sum(1 for t in active if t.get("side") == side)


def _last_signal_ts(state: dict[str, Any], side: str) -> datetime | None:
    raw = state.get("last_signal_ts", {}).get(side)
    if not raw:
        return None
    try:
        return _dt(raw)
    except Exception:
        return None


def _in_cooldown(state: dict[str, Any], side: str, settings: SignalLabSettings) -> bool:
    last = _last_signal_ts(state, side)
    if not last:
        return False
    return _now_utc() - last < timedelta(minutes=settings.cooldown_minutes)


def _make_trade_id(side: str, plan: ExitPlan) -> str:
    return f"v60-{side.lower()}-{plan.name}-{int(time.time() * 1000)}"


def maybe_create_paper_trades(features: dict[str, Any], state: dict[str, Any], settings: SignalLabSettings) -> list[dict[str, Any]]:
    side = str(features.get("candidate_side") or "NONE")
    if side not in {"LONG", "SHORT"}:
        return []
    active = state.setdefault("active_trades", [])
    if _side_active_count(active, side) >= settings.max_active_per_side:
        return []
    if _in_cooldown(state, side, settings):
        return []

    price = float(features["price"])
    signal_id = f"sig-{side.lower()}-{int(time.time() * 1000)}"
    created: list[dict[str, Any]] = []
    now = _iso()
    for plan in settings.exit_plans:
        if side == "LONG":
            tp = price * (1.0 + plan.tp_pct)
            sl = price * (1.0 - plan.sl_pct)
        else:
            tp = price * (1.0 - plan.tp_pct)
            sl = price * (1.0 + plan.sl_pct)
        trade = {
            "trade_id": _make_trade_id(side, plan),
            "signal_id": signal_id,
            "symbol": settings.symbol,
            "side": side,
            "plan": plan.name,
            "entry_ts": now,
            "entry_price": price,
            "tp_price": tp,
            "sl_price": sl,
            "tp_pct": plan.tp_pct,
            "sl_pct": plan.sl_pct,
            "max_hold_minutes": plan.max_hold_minutes,
            "mfe_pct": 0.0,
            "mae_pct": 0.0,
            "entry_features": features,
        }
        active.append(trade)
        created.append(trade)
    state.setdefault("last_signal_ts", {})[side] = now

    sig_row = {**features, "signal_id": signal_id, "side": side, "created_paper_trades": len(created), "exit_plans": ",".join(p.name for p in settings.exit_plans)}
    _append_csv(ROOT / settings.signals_path, sig_row, SIGNAL_FIELDS)
    _append_jsonl(ROOT / settings.events_path, {"event": "paper_signal", "signal": sig_row})
    return created


def _trade_pnl_pct(side: str, entry: float, price: float) -> float:
    if entry <= 0:
        return 0.0
    if side == "LONG":
        return (price / entry - 1.0) * 100.0
    return (entry / price - 1.0) * 100.0


def update_active_trades(client: Any, settings: SignalLabSettings, state: dict[str, Any]) -> list[dict[str, Any]]:
    active = state.setdefault("active_trades", [])
    if not active:
        return []
    c5 = fetch_candles(client, settings, "5m", 5)
    last = c5[-1]
    high = float(last.high)
    low = float(last.low)
    close = float(last.close)
    now = _now_utc()
    closed: list[dict[str, Any]] = []
    still_active: list[dict[str, Any]] = []

    for trade in active:
        side = str(trade.get("side"))
        entry = float(trade.get("entry_price"))
        tp = float(trade.get("tp_price"))
        sl = float(trade.get("sl_price"))
        entry_ts = _dt(str(trade.get("entry_ts")))
        max_hold = int(trade.get("max_hold_minutes") or 0)

        # Update MFE/MAE using latest 5m bar.
        if side == "LONG":
            mfe = max(float(trade.get("mfe_pct") or 0.0), _trade_pnl_pct(side, entry, high))
            mae = min(float(trade.get("mae_pct") or 0.0), _trade_pnl_pct(side, entry, low))
            tp_hit = high >= tp
            sl_hit = low <= sl
        else:
            mfe = max(float(trade.get("mfe_pct") or 0.0), _trade_pnl_pct(side, entry, low))
            mae = min(float(trade.get("mae_pct") or 0.0), _trade_pnl_pct(side, entry, high))
            tp_hit = low <= tp
            sl_hit = high >= sl
        trade["mfe_pct"] = mfe
        trade["mae_pct"] = mae

        exit_reason = None
        exit_price = None
        # Conservative policy if both TP/SL appear in same 5m bar: count SL first.
        if tp_hit and sl_hit:
            exit_reason = "same_candle_sl_conservative"
            exit_price = sl
        elif sl_hit:
            exit_reason = "sl_hit"
            exit_price = sl
        elif tp_hit:
            exit_reason = "tp_hit"
            exit_price = tp
        elif max_hold and now - entry_ts >= timedelta(minutes=max_hold):
            exit_reason = "max_hold_exit"
            exit_price = close

        if exit_reason:
            ef = dict(trade.get("entry_features") or {})
            exit_ts = _iso(now)
            hold_minutes = (now - entry_ts).total_seconds() / 60.0
            pnl_pct = _trade_pnl_pct(side, entry, float(exit_price))
            row = {
                "trade_id": trade.get("trade_id"),
                "signal_id": trade.get("signal_id"),
                "symbol": trade.get("symbol"),
                "side": side,
                "plan": trade.get("plan"),
                "entry_ts": trade.get("entry_ts"),
                "exit_ts": exit_ts,
                "entry_price": round(entry, 8),
                "exit_price": round(float(exit_price), 8),
                "tp_price": round(tp, 8),
                "sl_price": round(sl, 8),
                "tp_pct": float(trade.get("tp_pct") or 0),
                "sl_pct": float(trade.get("sl_pct") or 0),
                "max_hold_minutes": max_hold,
                "exit_reason": exit_reason,
                "pnl_pct": round(pnl_pct, 6),
                "mfe_pct": round(float(trade.get("mfe_pct") or 0), 6),
                "mae_pct": round(float(trade.get("mae_pct") or 0), 6),
                "hold_minutes": round(hold_minutes, 2),
                "entry_long_score": ef.get("long_score"),
                "entry_short_score": ef.get("short_score"),
                "entry_edge": ef.get("edge"),
                "entry_state": ef.get("state"),
                "entry_funding_rate": ef.get("funding_rate"),
                "entry_funding_z": ef.get("funding_z"),
                "entry_oi_4h_pct": ef.get("oi_4h_pct"),
                "entry_oi_24h_pct": ef.get("oi_24h_pct"),
                "entry_risk_penalty": ef.get("risk_penalty"),
                "entry_volume_z_5m": ef.get("volume_z_5m"),
                "entry_atr1h_pct": ef.get("atr1h_pct"),
            }
            _append_csv(ROOT / settings.trades_path, row, TRADE_FIELDS)
            _append_jsonl(ROOT / settings.events_path, {"event": "paper_trade_closed", "trade": row})
            closed.append(row)
        else:
            still_active.append(trade)
    state["active_trades"] = still_active
    return closed


# ─────────────────────────────────────────────────────────────────────────────
# Reports / messages
# ─────────────────────────────────────────────────────────────────────────────


def _score_bucket(score: float) -> str:
    if score >= 90:
        return "90+"
    if score >= 80:
        return "80-90"
    if score >= 70:
        return "70-80"
    if score >= 60:
        return "60-70"
    return "<60"


def _stats(rows: list[dict[str, str]]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "pf": 0.0, "avg": 0.0, "sum": 0.0, "best": 0.0, "worst": 0.0}
    vals = [_safe_float(r.get("pnl_pct")) for r in rows]
    wins = [x for x in vals if x > 0]
    losses = [x for x in vals if x < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "n": n,
        "win_rate": (len(wins) / n * 100.0),
        "pf": (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0),
        "avg": sum(vals) / n,
        "sum": sum(vals),
        "best": max(vals),
        "worst": min(vals),
    }


def _fmt_stats(label: str, rows: list[dict[str, str]]) -> str:
    s = _stats(rows)
    return f"{label:<24} n={s['n']:>4} win={s['win_rate']:>5.1f}% PF={s['pf']:>5.2f} avg={s['avg']:>7.3f}% sum={s['sum']:>8.2f}% worst={s['worst']:>7.3f}%"


def build_report(settings: SignalLabSettings, days: int | None = None) -> str:
    days = days or settings.report_days
    path = ROOT / settings.trades_path
    rows = _read_csv(path)
    cutoff = _now_utc() - timedelta(days=days)
    recent = []
    for r in rows:
        try:
            if _dt(r.get("exit_ts", "")) >= cutoff:
                recent.append(r)
        except Exception:
            pass
    lines = []
    lines.append("BTC Quant Signal Lab Report")
    lines.append("============================")
    lines.append(f"window_days: {days}")
    lines.append(f"closed_paper_trades: {len(recent)}")
    lines.append("")
    lines.append(_fmt_stats("ALL", recent))
    lines.append("")
    lines.append("By side")
    for side in ["LONG", "SHORT"]:
        lines.append(_fmt_stats(side, [r for r in recent if r.get("side") == side]))
    lines.append("")
    lines.append("By exit plan")
    for plan in sorted(set(r.get("plan", "") for r in recent)):
        if plan:
            lines.append(_fmt_stats(plan, [r for r in recent if r.get("plan") == plan]))
    lines.append("")
    lines.append("By score bucket")
    for bucket in ["60-70", "70-80", "80-90", "90+"]:
        b_rows = []
        for r in recent:
            score = _safe_float(r.get("entry_long_score")) if r.get("side") == "LONG" else _safe_float(r.get("entry_short_score"))
            if _score_bucket(score) == bucket:
                b_rows.append(r)
        lines.append(_fmt_stats(bucket, b_rows))
    lines.append("")
    lines.append("Funding/OI diagnostics")
    lines.append(_fmt_stats("funding_z <= 0", [r for r in recent if _safe_float(r.get("entry_funding_z")) <= 0]))
    lines.append(_fmt_stats("funding_z > 0", [r for r in recent if _safe_float(r.get("entry_funding_z")) > 0]))
    lines.append(_fmt_stats("OI4h >= 0", [r for r in recent if _safe_float(r.get("entry_oi_4h_pct")) >= 0]))
    lines.append(_fmt_stats("OI4h < 0", [r for r in recent if _safe_float(r.get("entry_oi_4h_pct")) < 0]))
    lines.append("")
    lines.append("Rule of thumb")
    if len(recent) < 50:
        lines.append("- 아직 표본이 부족함. 최소 50~100개 paper trade 전까지 실주문 판단 금지.")
    else:
        s_all = _stats(recent)
        if s_all["pf"] > 1.2 and s_all["win_rate"] > 52:
            lines.append("- 전체 성과는 관찰 가치 있음. 점수 bucket/exit plan 기준으로 더 좁혀야 함.")
        else:
            lines.append("- 아직 실전 투입 신호 아님. PF/승률이 부족하거나 조건 분리가 필요함.")
    text = "\n".join(lines)
    out = ROOT / settings.report_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    return text


def _status_message(features: dict[str, Any], created: list[dict[str, Any]], closed: list[dict[str, Any]], settings: SignalLabSettings) -> str:
    icon = "⚪"
    if features.get("state") == "PAPER_LONG_CANDIDATE":
        icon = "🧪🟢"
    elif features.get("state") == "PAPER_SHORT_CANDIDATE":
        icon = "🧪🔴"
    elif features.get("state") in {"WEAK_LONG", "WEAK_SHORT"}:
        icon = "🟡"
    created_msg = ""
    if created:
        created_msg = f"\n가상진입 생성: {len(created)}개 / {created[0]['side']} / plans {', '.join(sorted(set(t['plan'] for t in created)))}"
    closed_msg = ""
    if closed:
        wins = sum(1 for r in closed if _safe_float(r.get("pnl_pct")) > 0)
        avg = _mean([_safe_float(r.get("pnl_pct")) for r in closed])
        closed_msg = f"\n종료된 paper trades: {len(closed)}개 / wins {wins} / avg {avg:.3f}%"
    return (
        f"{icon} <b>BTC Quant Signal Lab v6.0</b>\n"
        f"상태: <b>{features['state']}</b> / L {features['long_score']} / S {features['short_score']} / edge {features['edge']}\n"
        f"가격: {_fmt(features['price'], 2)}\n"
        f"추세 L/S: {features['trend_long']} / {features['trend_short']} | 모멘텀 L/S: {features['momentum_long']} / {features['momentum_short']}\n"
        f"거래량 L/S: {features['volume_long']} / {features['volume_short']} | vZ5 {features['volume_z_5m']}\n"
        f"펀딩: {features['funding_rate']} / z {features['funding_z']} / L {features['funding_long']} / S {features['funding_short']}\n"
        f"OI: {features['oi']} / 4h {features['oi_4h_pct']}% / 24h {features['oi_24h_pct']}% / L {features['oi_long']} / S {features['oi_short']}\n"
        f"청산프록시 L/S: {features['liq_long']} / {features['liq_short']} | 위험 {features['risk_penalty']} / ATR1H {features['atr1h_pct']}%\n"
        f"실주문: 없음 / paper only"
        f"{created_msg}{closed_msg}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────


def run_once(*, send_summary: bool = False) -> dict[str, Any]:
    settings = load_settings()
    state_path = ROOT / settings.state_path
    state = _read_json(state_path, {})
    client = make_client()

    features = build_features(client, settings, state)
    closed = update_active_trades(client, settings, state)
    created = maybe_create_paper_trades(features, state, settings)

    _append_csv(ROOT / settings.snapshots_path, features, SNAPSHOT_FIELDS)
    _append_jsonl(ROOT / settings.events_path, {"event": "snapshot", "features": features})

    # Throttled notification.
    now = _now_utc()
    last_summary = state.get("last_summary_notify_ts")
    due_summary = send_summary
    if not due_summary and last_summary:
        try:
            due_summary = now - _dt(last_summary) >= timedelta(minutes=settings.notify_summary_minutes)
        except Exception:
            due_summary = True
    elif not last_summary:
        due_summary = True

    should_notify = False
    if created and settings.notify_signals:
        should_notify = True
    if closed and settings.notify_closes:
        should_notify = True
    if due_summary:
        should_notify = True

    if should_notify:
        _send(_status_message(features, created, closed, settings), settings)
        state["last_summary_notify_ts"] = _iso(now)

    _write_json(state_path, state)
    return {"features": features, "created": created, "closed": closed, "active_count": len(state.get("active_trades", []))}


def cmd_once(_: argparse.Namespace) -> None:
    res = run_once(send_summary=True)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))


def cmd_loop(_: argparse.Namespace) -> None:
    settings = load_settings()
    _send(f"🧪 <b>BTC Quant Signal Lab v6.0 시작</b>\n실주문 없음 / paper only\n주기: {settings.loop_seconds}s\nthreshold: {settings.signal_threshold}", settings)
    while True:
        try:
            res = run_once(send_summary=False)
            print(json.dumps({"ts": _iso(), "state": res["features"].get("state"), "long_score": res["features"].get("long_score"), "short_score": res["features"].get("short_score"), "created": len(res.get("created", [])), "closed": len(res.get("closed", [])), "active": res.get("active_count")}, ensure_ascii=False), flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"ERROR {datetime.now().isoformat()} {exc}", flush=True)
            _send(f"⚠️ <b>BTC Quant Signal Lab 오류</b>\n{exc}", settings)
        time.sleep(max(10, settings.loop_seconds))


def cmd_report(args: argparse.Namespace) -> None:
    settings = load_settings()
    text = build_report(settings, days=args.days or settings.report_days)
    print(text)
    if args.send:
        _send("<pre>" + text[-3500:] + "</pre>", settings)


def cmd_state(_: argparse.Namespace) -> None:
    settings = load_settings()
    state = _read_json(ROOT / settings.state_path, {})
    active = state.get("active_trades", [])
    print(json.dumps({"active_trades": active, "active_count": len(active), "state_path": settings.state_path}, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="BTC Quant Signal Lab v6.0 - paper signal laboratory")
    sub = p.add_subparsers(dest="cmd", required=True)
    once = sub.add_parser("once")
    once.set_defaults(func=cmd_once)
    loop = sub.add_parser("loop")
    loop.set_defaults(func=cmd_loop)
    report = sub.add_parser("report")
    report.add_argument("--days", type=int, default=None)
    report.add_argument("--send", action="store_true")
    report.set_defaults(func=cmd_report)
    state = sub.add_parser("state")
    state.set_defaults(func=cmd_state)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
