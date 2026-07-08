from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from index_sniper.exchange.bitget_uta import BitgetUTAClient
from index_sniper.strategy.indicators import Candle, parse_candles
from index_sniper.telegram.bot import TelegramBot

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH_DEFAULT = "data/quant_v42_observer_state.json"
JSONL_PATH_DEFAULT = "data/quant_v42_observer_events.jsonl"
LOG_PATH_DEFAULT = "logs/quant-v42.log"


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing environment variable: {name}")
    return v.strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _avg(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _ema(xs: list[float], n: int) -> float | None:
    if len(xs) < n:
        return None
    k = 2.0 / (n + 1.0)
    e = sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1.0 - k)
    return e


def _pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a / b - 1.0) * 100.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _round(x: Any, n: int = 6) -> float | None:
    try:
        return round(float(x), n)
    except Exception:
        return None


def _extract_rows(resp: dict[str, Any]) -> list[dict[str, Any]]:
    data = resp.get("data") if isinstance(resp, dict) else None
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("list", "rows", "result", "data"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [x for x in rows if isinstance(x, dict)]
        return [data]
    return []


@dataclass
class V42Settings:
    symbol: str
    category: str
    interval_signal: str
    interval_trend: str
    loop_seconds: int
    candle_limit_1h: int
    candle_limit_4h: int
    state_path: str
    jsonl_path: str
    log_path: str
    notify_neutral: bool
    notify_evals: bool
    notify_every_minutes: int
    confirm_count: int
    signal_cooldown_minutes: int
    weak_long_score: float
    strong_long_score: float
    weak_short_score: float
    strong_short_score: float
    atr_penalty_start_pct: float
    atr_penalty_hard_pct: float
    trend_dynamic_enabled: bool
    pressure_enabled: bool
    score_weights_profile: str


def load_settings() -> V42Settings:
    load_dotenv(ROOT / ".env")
    return V42Settings(
        symbol=os.getenv("V42_SYMBOL", "BTCUSDT").strip().upper(),
        category=os.getenv("V42_CATEGORY", "USDT-FUTURES").strip(),
        interval_signal=os.getenv("V42_INTERVAL_SIGNAL", "1H").strip(),
        interval_trend=os.getenv("V42_INTERVAL_TREND", "4H").strip(),
        loop_seconds=_int("V42_LOOP_SECONDS", 900),
        candle_limit_1h=_int("V42_CANDLE_LIMIT_1H", 360),
        candle_limit_4h=_int("V42_CANDLE_LIMIT_4H", 240),
        state_path=os.getenv("V42_STATE_PATH", STATE_PATH_DEFAULT).strip(),
        jsonl_path=os.getenv("V42_JSONL_PATH", JSONL_PATH_DEFAULT).strip(),
        log_path=os.getenv("V42_LOG_PATH", LOG_PATH_DEFAULT).strip(),
        notify_neutral=_bool(os.getenv("V42_NOTIFY_NEUTRAL"), False),
        notify_evals=_bool(os.getenv("V42_NOTIFY_EVALS"), True),
        notify_every_minutes=_int("V42_NOTIFY_EVERY_MINUTES", 60),
        confirm_count=_int("V42_CONFIRM_COUNT", 2),
        signal_cooldown_minutes=_int("V42_SIGNAL_COOLDOWN_MINUTES", 60),
        weak_long_score=_float("V42_WEAK_LONG_SCORE", 40.0),
        strong_long_score=_float("V42_STRONG_LONG_SCORE", 60.0),
        weak_short_score=_float("V42_WEAK_SHORT_SCORE", -30.0),
        strong_short_score=_float("V42_STRONG_SHORT_SCORE", -55.0),
        atr_penalty_start_pct=_float("V42_ATR_PENALTY_START_PCT", 1.20),
        atr_penalty_hard_pct=_float("V42_ATR_PENALTY_HARD_PCT", 2.20),
        trend_dynamic_enabled=_bool(os.getenv("V42_TREND_DYNAMIC_ENABLED"), True),
        pressure_enabled=_bool(os.getenv("V42_PRESSURE_ENABLED"), True),
        score_weights_profile=os.getenv("V42_SCORE_PROFILE", "balanced").strip(),
    )


def make_client() -> BitgetUTAClient:
    return BitgetUTAClient(
        api_key=_required("BITGET_API_KEY"),
        secret_key=_required("BITGET_SECRET_KEY"),
        passphrase=_required("BITGET_PASSPHRASE"),
        timeout=10,
    )


def make_bot() -> TelegramBot:
    return TelegramBot(_required("TELEGRAM_TOKEN"), _required("TELEGRAM_CHAT_ID"))


def load_state(path: str) -> dict[str, Any]:
    p = ROOT / path
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_state(path: str, state: dict[str, Any]) -> None:
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))


def append_jsonl(path: str, obj: dict[str, Any]) -> None:
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")


def append_log(path: str, text: str) -> None:
    p = ROOT / path
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(text.rstrip() + "\n")


def _send(bot: TelegramBot, text: str) -> None:
    try:
        bot.send(text)
    except Exception:
        pass


def _candles(client: BitgetUTAClient, symbol: str, category: str, interval: str, limit: int) -> list[Candle]:
    resp = client.candles(symbol=symbol, category=category, interval=interval, limit=limit)
    candles = parse_candles(resp)
    candles.sort(key=lambda c: c.ts)
    return candles


def _ticker_row(client: BitgetUTAClient, symbol: str, category: str) -> dict[str, Any]:
    try:
        resp = client.tickers(category=category)
        for row in _extract_rows(resp):
            sym = str(row.get("symbol") or row.get("instId") or "").upper()
            if sym == symbol:
                return row
    except Exception:
        pass
    return {}


def _last_price_from_ticker(row: dict[str, Any]) -> float | None:
    for key in ("lastPrice", "lastPr", "last", "close", "price", "markPrice"):
        if row.get(key) not in (None, ""):
            return _safe_float(row.get(key))
    return None


def _funding_rate(client: BitgetUTAClient, symbol: str, ticker: dict[str, Any]) -> float | None:
    # Prefer the dedicated UTA public endpoint, fall back to ticker field if present.
    try:
        resp = client.get("/api/v3/market/current-fund-rate", {"symbol": symbol})
        rows = _extract_rows(resp)
        for row in rows:
            for key in ("fundingRate", "fundRate", "rate"):
                if row.get(key) not in (None, ""):
                    return _safe_float(row.get(key))
    except Exception:
        pass
    for key in ("fundingRate", "fundRate", "rate"):
        if ticker.get(key) not in (None, ""):
            return _safe_float(ticker.get(key))
    return None


def _open_interest(client: BitgetUTAClient, symbol: str, category: str, ticker: dict[str, Any]) -> float | None:
    try:
        resp = client.get("/api/v3/market/open-interest", {"category": category, "symbol": symbol})
        rows = _extract_rows(resp)
        for row in rows:
            for key in ("openInterest", "openInterestValue", "oi", "amount"):
                if row.get(key) not in (None, ""):
                    return _safe_float(row.get(key))
    except Exception:
        pass
    for key in ("openInterest", "openInterestValue", "oi"):
        if ticker.get(key) not in (None, ""):
            return _safe_float(ticker.get(key))
    return None


def _atr_pct(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        high = float(cur.high)
        low = float(cur.low)
        pc = float(prev.close)
        trs.append(max(high - low, abs(high - pc), abs(low - pc)))
    atr = _avg(trs[-period:])
    close = float(candles[-1].close)
    return atr / close * 100.0 if close else 0.0


def _history_change(history: list[dict[str, Any]], hours: float, now: datetime, current: float) -> float:
    if current <= 0 or not history:
        return 0.0
    target = now - timedelta(hours=hours)
    best = None
    best_dt = None
    for row in history:
        try:
            t = datetime.fromisoformat(str(row.get("ts")))
            val = float(row.get("value"))
        except Exception:
            continue
        if t <= target and (best_dt is None or t > best_dt):
            best_dt = t
            best = val
    if best is None or best <= 0:
        return 0.0
    return _pct(current, best)


def _zscore(value: float, xs: list[float]) -> float:
    if len(xs) < 8:
        return 0.0
    m = _avg(xs)
    s = _std(xs)
    if s <= 0:
        return 0.0
    return (value - m) / s


def _build_scores(settings: V42Settings, candles_1h: list[Candle], candles_4h: list[Candle], funding: float, funding_z: float, oi4h: float, oi24h: float, price: float) -> dict[str, float]:
    closes = [float(c.close) for c in candles_1h]
    vols = [float(c.volume) for c in candles_1h]
    closes4 = [float(c.close) for c in candles_4h]
    if len(closes) < 80 or len(closes4) < 80:
        raise RuntimeError("not enough candles for v4.2 scoring")

    ret4 = _pct(closes[-1], closes[-5]) if len(closes) >= 5 else 0.0
    ret12 = _pct(closes[-1], closes[-13]) if len(closes) >= 13 else 0.0
    ret24 = _pct(closes[-1], closes[-25]) if len(closes) >= 25 else 0.0
    ret72 = _pct(closes[-1], closes[-73]) if len(closes) >= 73 else 0.0
    momentum = _clamp(ret4 * 3.4 + ret12 * 2.0 + ret24 * 1.1 + ret72 * 0.45, -35.0, 35.0)

    e20_4 = _ema(closes4, 20)
    e60_4 = _ema(closes4, 60)
    e50_1 = _ema(closes, 50)
    e200_1 = _ema(closes, 200) if len(closes) >= 200 else None
    trend = 0.0
    if e20_4 is not None and e60_4 is not None:
        if closes4[-1] > e20_4 > e60_4:
            trend += 25.0
        elif closes4[-1] < e20_4 < e60_4:
            trend -= 25.0
        elif e20_4 > e60_4:
            trend += 12.0
        elif e20_4 < e60_4:
            trend -= 12.0
    if e50_1 is not None and e200_1 is not None:
        if closes[-1] > e50_1 > e200_1:
            trend += 8.0
        elif closes[-1] < e50_1 < e200_1:
            trend -= 8.0

    # Dynamic trend haircut: the previous v4.1 kept +25 during fast selloffs.
    if settings.trend_dynamic_enabled:
        if trend > 0 and momentum < -12:
            trend = max(0.0, trend - 22.0)
        elif trend < 0 and momentum > 12:
            trend = min(0.0, trend + 22.0)

    v_now = vols[-1]
    v_mean = _avg(vols[-25:-1]) if len(vols) >= 26 else _avg(vols[:-1])
    v_sd = _std(vols[-49:-1]) if len(vols) >= 50 else _std(vols[:-1])
    vz = (v_now - v_mean) / v_sd if v_sd > 0 else 0.0
    # Volume by itself is not directional. It amplifies current momentum sign.
    volume = _clamp(vz * 4.0 * (1 if momentum >= 0 else -1), -15.0, 15.0)

    # Positive funding means longs pay; that is a long-crowding drag and can support short pressure.
    funding_score = _clamp(-8.0 * funding_z, -20.0, 20.0)

    oi_score = 0.0
    if momentum < -8 and oi4h > 0:
        oi_score -= min(20.0, oi4h * 8.0)
    elif momentum < -8 and oi4h < 0:
        oi_score += min(15.0, abs(oi4h) * 8.0)
    elif momentum > 8 and oi4h > 0:
        oi_score += min(15.0, oi4h * 6.0)
    elif momentum > 8 and oi4h < 0:
        oi_score -= min(12.0, abs(oi4h) * 6.0)
    if momentum < -8 and oi24h > 0:
        oi_score -= min(10.0, oi24h * 3.0)
    elif momentum > 8 and oi24h > 0:
        oi_score += min(10.0, oi24h * 2.5)
    oi_score = _clamp(oi_score, -25.0, 25.0)

    pressure = 0.0
    if settings.pressure_enabled:
        if momentum < -12 and funding_z > 0.8 and oi4h > 0.20:
            pressure -= _clamp(8.0 + oi4h * 6.0 + max(0.0, funding_z - 0.8) * 6.0, 8.0, 25.0)
        if momentum > 12 and funding_z < -0.3 and oi4h > 0.20:
            pressure += _clamp(7.0 + oi4h * 5.0 + abs(funding_z) * 4.0, 7.0, 22.0)

    atr = _atr_pct(candles_1h, 14)
    if atr <= settings.atr_penalty_start_pct:
        penalty = 0.0
    else:
        span = max(0.01, settings.atr_penalty_hard_pct - settings.atr_penalty_start_pct)
        penalty = _clamp((atr - settings.atr_penalty_start_pct) / span * 20.0, 0.0, 35.0)

    total = momentum + trend + volume + funding_score + oi_score + pressure
    if total > 0:
        total -= penalty
    elif total < 0:
        total += penalty
    total = _clamp(total, -100.0, 100.0)

    return {
        "score": total,
        "momentum": momentum,
        "trend": trend,
        "volume": volume,
        "funding_score": funding_score,
        "oi_score": oi_score,
        "pressure": pressure,
        "atr_pct": atr,
        "penalty": penalty,
        "ret4h": ret4,
        "ret12h": ret12,
        "ret24h": ret24,
        "ret72h": ret72,
    }


def _classify(settings: V42Settings, score: float) -> str:
    if score >= settings.strong_long_score:
        return "STRONG_LONG"
    if score >= settings.weak_long_score:
        return "WEAK_LONG"
    if score <= settings.strong_short_score:
        return "STRONG_SHORT"
    if score <= settings.weak_short_score:
        return "WEAK_SHORT"
    return "NEUTRAL"


def _direction(zone: str) -> str | None:
    if zone.endswith("LONG"):
        return "LONG"
    if zone.endswith("SHORT"):
        return "SHORT"
    return None


def _update_streak(state: dict[str, Any], zone: str) -> int:
    prev = state.get("last_zone")
    if zone == prev:
        streak = int(state.get("zone_streak") or 0) + 1
    else:
        streak = 1
    state["last_zone"] = zone
    state["zone_streak"] = streak
    return streak


def _maybe_add_signal(settings: V42Settings, state: dict[str, Any], now: datetime, result: dict[str, Any]) -> dict[str, Any] | None:
    zone = result["zone"]
    direction = _direction(zone)
    if not direction or not result.get("confirmed"):
        return None
    last = state.get("last_signal") or {}
    cooldown_ok = True
    try:
        last_ts = datetime.fromisoformat(str(last.get("ts")))
        cooldown_ok = (now - last_ts).total_seconds() >= settings.signal_cooldown_minutes * 60
    except Exception:
        pass
    if last.get("zone") == zone and not cooldown_ok:
        return None
    sig_id = f"{now.strftime('%Y%m%dT%H%M%S')}_{zone}"
    sig = {
        "id": sig_id,
        "ts": now.isoformat(),
        "zone": zone,
        "direction": direction,
        "entry_price": result["price"],
        "score": result["score"],
        "evaluated": {},
    }
    state["last_signal"] = {"ts": now.isoformat(), "zone": zone, "id": sig_id}
    state.setdefault("pending_signals", []).append(sig)
    return sig


def _evaluate_pending(settings: V42Settings, state: dict[str, Any], now: datetime, price: float) -> list[dict[str, Any]]:
    horizons = {"1h": 1, "4h": 4, "12h": 12, "24h": 24}
    pending = state.setdefault("pending_signals", [])
    events: list[dict[str, Any]] = []
    keep: list[dict[str, Any]] = []
    for sig in pending:
        try:
            ts = datetime.fromisoformat(str(sig.get("ts")))
            entry = float(sig.get("entry_price"))
            direction = str(sig.get("direction"))
        except Exception:
            continue
        evaluated = sig.setdefault("evaluated", {})
        for name, hours in horizons.items():
            if evaluated.get(name):
                continue
            if now >= ts + timedelta(hours=hours):
                if direction == "LONG":
                    pnl = _pct(price, entry)
                else:
                    pnl = _pct(entry, price)
                ev = {
                    "type": "signal_eval",
                    "signal_id": sig.get("id"),
                    "signal_ts": sig.get("ts"),
                    "horizon": name,
                    "direction": direction,
                    "zone": sig.get("zone"),
                    "entry_price": entry,
                    "eval_price": price,
                    "pnl_pct": pnl,
                    "score": sig.get("score"),
                    "ts": now.isoformat(),
                }
                events.append(ev)
                evaluated[name] = ev
        if now < ts + timedelta(hours=30):
            keep.append(sig)
    state["pending_signals"] = keep[-200:]
    return events


def _message(result: dict[str, Any], evals: list[dict[str, Any]] | None = None) -> str:
    emoji = "⚪"
    if result["zone"].endswith("LONG"):
        emoji = "🟢"
    elif result["zone"].endswith("SHORT"):
        emoji = "🔴"
    conf = "확정" if result.get("confirmed") else f"대기 {result.get('streak')}/{result.get('confirm_count')}"
    lines = [
        f"{emoji} <b>BTC Quant v4.2 State</b>",
        f"상태: {result['zone']} / {conf} / score {result['score']:.4f}",
        f"가격: {result['price']:.1f}",
        f"모멘텀: {result['components']['momentum']:.4f} | 추세: {result['components']['trend']:.4f} | 거래량: {result['components']['volume']:.4f}",
        f"펀딩: {result['funding']} / z {result['funding_z']:.6f} / score {result['components']['funding_score']:.4f}",
        f"OI: {result['oi']} / 4h {result['oi4h_pct']:.6f}% / 24h {result['oi24h_pct']:.6f}% / score {result['components']['oi_score']:.4f}",
        f"압력: {result['components']['pressure']:.4f} / 위험: ATR {result['components']['atr_pct']:.6f}% / penalty {result['components']['penalty']:.4f}",
        "실주문: 없음 / 관찰 전용 / 성과기록 ON",
    ]
    if result.get("new_signal"):
        lines.append(f"\n📍 신호 기록: {result['new_signal']['id']} / {result['new_signal']['direction']} / entry {result['new_signal']['entry_price']:.1f}")
    if evals:
        lines.append("\n<b>성과 평가</b>")
        for ev in evals[:8]:
            lines.append(f"- {ev['zone']} {ev['horizon']}: {ev['pnl_pct']:.3f}% / {ev['entry_price']:.1f} → {ev['eval_price']:.1f}")
    return "\n".join(lines)


def run_once(*, notify: bool = True) -> dict[str, Any]:
    settings = load_settings()
    client = make_client()
    bot = make_bot()
    state = load_state(settings.state_path)
    now = _utc_now()

    candles_1h = _candles(client, settings.symbol, settings.category, settings.interval_signal, settings.candle_limit_1h)
    candles_4h = _candles(client, settings.symbol, settings.category, settings.interval_trend, settings.candle_limit_4h)
    ticker = _ticker_row(client, settings.symbol, settings.category)
    price = _last_price_from_ticker(ticker) or float(candles_1h[-1].close)
    funding = _funding_rate(client, settings.symbol, ticker)
    if funding is None:
        funding = 0.0
    oi = _open_interest(client, settings.symbol, settings.category, ticker)
    if oi is None:
        oi = 0.0

    hist = state.setdefault("history", {})
    funding_hist = hist.setdefault("funding", [])
    oi_hist = hist.setdefault("oi", [])
    funding_hist.append({"ts": now.isoformat(), "value": funding})
    oi_hist.append({"ts": now.isoformat(), "value": oi})
    cutoff = now - timedelta(hours=72)
    hist["funding"] = [x for x in funding_hist if datetime.fromisoformat(str(x.get("ts"))) >= cutoff][-500:]
    hist["oi"] = [x for x in oi_hist if datetime.fromisoformat(str(x.get("ts"))) >= cutoff][-500:]
    funding_vals = [float(x.get("value")) for x in hist["funding"] if x.get("value") is not None]
    funding_z = _zscore(funding, funding_vals[:-1] if len(funding_vals) > 1 else funding_vals)
    oi4h = _history_change(hist["oi"], 4, now, oi)
    oi24h = _history_change(hist["oi"], 24, now, oi)

    components = _build_scores(settings, candles_1h, candles_4h, funding, funding_z, oi4h, oi24h, price)
    score = components["score"]
    zone = _classify(settings, score)
    streak = _update_streak(state, zone)
    confirmed = zone != "NEUTRAL" and streak >= settings.confirm_count
    evals = _evaluate_pending(settings, state, now, price)

    result: dict[str, Any] = {
        "type": "state",
        "ts": now.isoformat(),
        "symbol": settings.symbol,
        "zone": zone,
        "score": score,
        "streak": streak,
        "confirm_count": settings.confirm_count,
        "confirmed": confirmed,
        "price": price,
        "funding": funding,
        "funding_z": funding_z,
        "oi": oi,
        "oi4h_pct": oi4h,
        "oi24h_pct": oi24h,
        "components": components,
        "settings": asdict(settings),
    }
    new_signal = _maybe_add_signal(settings, state, now, result)
    if new_signal:
        result["new_signal"] = new_signal
    for ev in evals:
        append_jsonl(settings.jsonl_path, ev)
    append_jsonl(settings.jsonl_path, result)
    text = _message(result, evals)
    append_log(settings.log_path, f"[{now.isoformat()}] " + text.replace("<b>", "").replace("</b>", ""))
    save_state(settings.state_path, state)

    should_notify = False
    if zone != "NEUTRAL" and confirmed:
        should_notify = True
    if settings.notify_neutral:
        try:
            last = datetime.fromisoformat(str(state.get("last_neutral_notify_ts")))
            if (now - last).total_seconds() >= settings.notify_every_minutes * 60:
                should_notify = True
        except Exception:
            should_notify = True
        if should_notify and zone == "NEUTRAL":
            state["last_neutral_notify_ts"] = now.isoformat()
            save_state(settings.state_path, state)
    if evals and settings.notify_evals:
        should_notify = True
    if notify and should_notify:
        _send(bot, text)
    return result


def loop() -> None:
    settings = load_settings()
    while True:
        try:
            run_once(notify=True)
        except Exception as exc:
            now = _utc_now().isoformat()
            msg = f"⚠️ <b>BTC Quant v4.2 오류</b>\n{type(exc).__name__}: {exc}"
            append_log(settings.log_path, f"[{now}] ERROR {type(exc).__name__}: {exc}")
            try:
                _send(make_bot(), msg)
            except Exception:
                pass
        time.sleep(max(60, settings.loop_seconds))


def summarize() -> None:
    settings = load_settings()
    p = ROOT / settings.jsonl_path
    rows = []
    evals = []
    if p.exists():
        for line in p.read_text(errors="ignore").splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("type") == "state":
                rows.append(obj)
            elif obj.get("type") == "signal_eval":
                evals.append(obj)
    print("BTC Quant v4.2 Observer Summary")
    print("================================")
    print(f"states: {len(rows)}")
    print(f"evals: {len(evals)}")
    if rows:
        scores = [float(r.get("score") or 0) for r in rows]
        print(f"score first/last/min/max/avg: {scores[0]:.4f} / {scores[-1]:.4f} / {min(scores):.4f} / {max(scores):.4f} / {_avg(scores):.4f}")
        counts: dict[str, int] = {}
        for r in rows:
            z = str(r.get("zone"))
            counts[z] = counts.get(z, 0) + 1
        print("zones:")
        for k, v in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"- {k}: {v}")
    if evals:
        by = {}
        for ev in evals:
            key = (ev.get("zone"), ev.get("horizon"))
            by.setdefault(key, []).append(float(ev.get("pnl_pct") or 0))
        print("eval avg pnl:")
        for key, xs in sorted(by.items()):
            print(f"- {key[0]} {key[1]}: n={len(xs)} avg={_avg(xs):.4f}% win={sum(1 for x in xs if x>0)/len(xs)*100:.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["once", "loop", "summary"], nargs="?", default="once")
    args = ap.parse_args()
    if args.cmd == "loop":
        loop()
    elif args.cmd == "summary":
        summarize()
    else:
        print(json.dumps(run_once(notify=True), ensure_ascii=False, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
