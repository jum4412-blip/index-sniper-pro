from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None  # type: ignore

ROOT = Path.cwd()
if load_dotenv is not None:
    try:
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

BASE = os.getenv("BITGET_BASE_URL", "https://api.bitget.com").rstrip("/")
DATA_DIR = ROOT / "data" / "quant_v41"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso_ms(ts: int | float | None = None) -> str:
    if ts is None:
        ts = _now_ms()
    return datetime.fromtimestamp(float(ts) / 1000.0, tz=timezone.utc).isoformat()


def _float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b not in (0, 0.0) else default


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _ema(xs: list[float], period: int) -> list[float]:
    if not xs:
        return []
    alpha = 2.0 / (period + 1.0)
    out: list[float] = []
    v = xs[0]
    for x in xs:
        v = (x * alpha) + (v * (1.0 - alpha))
        out.append(v)
    return out


def _http_get(path: str, params: dict[str, Any], timeout: int = 15) -> dict[str, Any]:
    url = BASE + path
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if str(data.get("code")) not in {"00000", "0"}:
        raise RuntimeError(f"Bitget error for {path}: {data}")
    return data


def fetch_history_candles(symbol: str, interval: str = "1H", limit: int = 500) -> list[dict[str, float]]:
    data = _http_get(
        "/api/v3/market/history-candles",
        {"category": "USDT-FUTURES", "symbol": symbol, "interval": interval, "limit": str(limit)},
    )
    rows = data.get("data") or []
    candles: list[dict[str, float]] = []
    for row in rows:
        try:
            candles.append(
                {
                    "ts": int(float(row[0])),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]) if len(row) > 5 else 0.0,
                    "turnover": float(row[6]) if len(row) > 6 else 0.0,
                }
            )
        except Exception:
            continue
    candles.sort(key=lambda x: x["ts"])
    return candles


def fetch_current_funding(symbol: str) -> dict[str, Any]:
    # UTA endpoint. If current endpoint changes or fails, fall back to latest history record.
    try:
        data = _http_get(
            "/api/v3/market/current-fund-rate",
            {"category": "USDT-FUTURES", "symbol": symbol},
        )
        payload = data.get("data") or {}
        if isinstance(payload, list) and payload:
            payload = payload[0]
        return {
            "funding_rate": _float(payload.get("fundingRate")),
            "funding_time_ms": int(_float(payload.get("fundingRateTimestamp") or payload.get("nextFundingTime") or _now_ms())),
            "source": "current-fund-rate",
        }
    except Exception:
        hist = fetch_funding_history(symbol, limit=5)
        if hist:
            last = hist[-1]
            return {"funding_rate": last["funding_rate"], "funding_time_ms": last["ts"], "source": "history-fund-rate"}
        return {"funding_rate": 0.0, "funding_time_ms": _now_ms(), "source": "none"}


def fetch_funding_history(symbol: str, limit: int = 100) -> list[dict[str, float]]:
    data = _http_get(
        "/api/v3/market/history-fund-rate",
        {"category": "USDT-FUTURES", "symbol": symbol, "limit": str(min(limit, 100)), "cursor": "1"},
    )
    payload = data.get("data") or {}
    rows = payload.get("resultList") or []
    out: list[dict[str, float]] = []
    for row in rows:
        try:
            out.append({"ts": int(float(row.get("fundingRateTimestamp"))), "funding_rate": float(row.get("fundingRate"))})
        except Exception:
            continue
    out.sort(key=lambda x: x["ts"])
    return out


def fetch_open_interest(symbol: str) -> dict[str, Any]:
    # Bitget classic futures public endpoint. Returns current platform open interest.
    data = _http_get(
        "/api/v2/mix/market/open-interest",
        {"symbol": symbol, "productType": "usdt-futures"},
    )
    payload = data.get("data") or {}
    ts = int(_float(payload.get("ts"), _now_ms()))
    rows = payload.get("openInterestList") or []
    size = 0.0
    if rows:
        size = _float(rows[0].get("size"))
    return {"open_interest": size, "oi_time_ms": ts}


def _ret(closes: list[float], bars: int) -> float:
    if len(closes) <= bars or closes[-bars - 1] <= 0:
        return 0.0
    return (closes[-1] / closes[-bars - 1]) - 1.0


def _atr(candles: list[dict[str, float]], period: int = 24) -> float:
    if len(candles) < period + 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return _mean(trs[-period:]) if len(trs) >= period else 0.0


def _read_snapshots(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with path.open("r", newline="") as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _append_snapshot(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)


def _past_change(rows: list[dict[str, Any]], key: str, hours: int) -> float:
    if not rows:
        return 0.0
    now_ts = _now_ms()
    target = now_ts - hours * 3600_000
    past = None
    for r in rows:
        ts = int(_float(r.get("ts_ms"), 0))
        if ts <= target:
            past = r
        else:
            break
    if not past:
        return 0.0
    current = _float(rows[-1].get(key), 0.0)
    previous = _float(past.get(key), 0.0)
    return _safe_div(current - previous, previous, 0.0)


def build_quant_state(symbol: str, interval: str = "1H", candle_limit: int = 500) -> dict[str, Any]:
    candles = fetch_history_candles(symbol, interval=interval, limit=candle_limit)
    if len(candles) < 120:
        raise RuntimeError(f"not enough candles: {len(candles)}")
    closes = [c["close"] for c in candles]
    vols = [c["volume"] for c in candles]
    price = closes[-1]
    ema80 = _ema(closes, 80)[-1]
    ema240 = _ema(closes, 240)[-1]
    atr24 = _atr(candles, 24)
    atr_pct = _safe_div(atr24, price, 0.0)

    r4 = _ret(closes, 4)
    r12 = _ret(closes, 12)
    r24 = _ret(closes, 24)
    r72 = _ret(closes, 72)

    # Normalize momentum roughly by volatility. Clamp to avoid one feature dominating.
    vol_unit = max(atr_pct, 0.001)
    mom_raw = (0.20 * (r4 / vol_unit)) + (0.25 * (r12 / vol_unit)) + (0.30 * (r24 / vol_unit)) + (0.25 * (r72 / vol_unit))
    momentum_score = _clip(mom_raw * 12.0, -40.0, 40.0)

    trend_gap = _safe_div(ema80 - ema240, price, 0.0)
    trend_score = _clip((trend_gap / vol_unit) * 18.0, -25.0, 25.0)

    vol_mean = _mean(vols[-48:-1])
    vol_std = _std(vols[-48:-1])
    volume_z = _safe_div(vols[-1] - vol_mean, vol_std, 0.0)
    volume_score = _clip(volume_z * 5.0, -10.0, 15.0)

    funding_hist = fetch_funding_history(symbol, limit=100)
    current_funding = fetch_current_funding(symbol)
    f_values = [x["funding_rate"] for x in funding_hist]
    f_mean = _mean(f_values)
    f_std = _std(f_values)
    funding_rate = float(current_funding.get("funding_rate", 0.0))
    funding_z = _safe_div(funding_rate - f_mean, f_std, 0.0)
    # Funding crowding: positive funding penalizes long and supports short, negative funding does the opposite.
    funding_crowd_score = _clip(-funding_z * 8.0, -20.0, 20.0)

    oi = fetch_open_interest(symbol)
    snap_path = DATA_DIR / f"{symbol}_snapshots.csv"
    previous_rows = _read_snapshots(snap_path)
    # Include current OI as last row only for scoring if previous rows exist.
    temp_rows = previous_rows + [{"ts_ms": _now_ms(), "open_interest": oi["open_interest"]}]
    oi_chg_4h = _past_change(temp_rows, "open_interest", 4)
    oi_chg_24h = _past_change(temp_rows, "open_interest", 24)
    price_chg_4h = r4
    # OI confirmation: price up + OI up supports trend, price down + OI up supports short. OI down after move weakens.
    oi_score = 0.0
    if abs(oi_chg_4h) > 0:
        oi_score += _clip((1 if price_chg_4h >= 0 else -1) * oi_chg_4h * 800.0, -12.0, 12.0)
    if abs(oi_chg_24h) > 0:
        oi_score += _clip((1 if r24 >= 0 else -1) * oi_chg_24h * 500.0, -8.0, 8.0)
    oi_score = _clip(oi_score, -20.0, 20.0)

    risk_penalty = 0.0
    if atr_pct > 0.04:
        risk_penalty -= 15.0
    elif atr_pct > 0.03:
        risk_penalty -= 8.0
    if abs(volume_z) > 4.0:
        risk_penalty -= 8.0

    final_score = momentum_score + trend_score + volume_score + funding_crowd_score + oi_score + risk_penalty
    final_score = _clip(final_score, -100.0, 100.0)

    if final_score >= 70:
        state = "STRONG_LONG"
    elif final_score >= 45:
        state = "WEAK_LONG"
    elif final_score <= -70:
        state = "STRONG_SHORT"
    elif final_score <= -45:
        state = "WEAK_SHORT"
    else:
        state = "NEUTRAL"

    row = {
        "ts_ms": _now_ms(),
        "time_utc": _iso_ms(),
        "symbol": symbol,
        "price": round(price, 4),
        "ema80": round(ema80, 4),
        "ema240": round(ema240, 4),
        "atr24": round(atr24, 4),
        "atr_pct": round(atr_pct * 100.0, 6),
        "ret_4h_pct": round(r4 * 100.0, 6),
        "ret_12h_pct": round(r12 * 100.0, 6),
        "ret_24h_pct": round(r24 * 100.0, 6),
        "ret_72h_pct": round(r72 * 100.0, 6),
        "volume_z": round(volume_z, 6),
        "funding_rate": round(funding_rate, 10),
        "funding_z": round(funding_z, 6),
        "open_interest": round(float(oi["open_interest"]), 8),
        "oi_chg_4h_pct": round(oi_chg_4h * 100.0, 6),
        "oi_chg_24h_pct": round(oi_chg_24h * 100.0, 6),
        "momentum_score": round(momentum_score, 4),
        "trend_score": round(trend_score, 4),
        "volume_score": round(volume_score, 4),
        "funding_crowd_score": round(funding_crowd_score, 4),
        "oi_score": round(oi_score, 4),
        "risk_penalty": round(risk_penalty, 4),
        "final_score": round(final_score, 4),
        "state": state,
    }
    _append_snapshot(snap_path, row)
    return row


def _telegram_send(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"},
            timeout=10,
        )
    except Exception:
        pass


def format_state(row: dict[str, Any]) -> str:
    arrow = "🟢" if row["final_score"] >= 45 else "🔴" if row["final_score"] <= -45 else "⚪"
    return "\n".join(
        [
            f"{arrow} BTC Quant v4.1 State",
            f"상태: {row['state']} / score {row['final_score']}",
            f"가격: {row['price']}",
            f"모멘텀: {row['momentum_score']} | 추세: {row['trend_score']} | 거래량: {row['volume_score']}",
            f"펀딩: {row['funding_rate']} / z {row['funding_z']} / score {row['funding_crowd_score']}",
            f"OI: {row['open_interest']} / 4h {row['oi_chg_4h_pct']}% / 24h {row['oi_chg_24h_pct']}% / score {row['oi_score']}",
            f"위험: ATR {row['atr_pct']}% / penalty {row['risk_penalty']}",
            "실주문: 없음 / 관찰 전용",
        ]
    )


def cmd_once(args: argparse.Namespace) -> None:
    row = build_quant_state(args.symbol, args.interval, args.candle_limit)
    text = format_state(row)
    print(text)
    print(json.dumps(row, ensure_ascii=False, indent=2))
    if args.notify:
        _telegram_send(text)


def cmd_loop(args: argparse.Namespace) -> None:
    while True:
        try:
            row = build_quant_state(args.symbol, args.interval, args.candle_limit)
            text = format_state(row)
            print(f"[{datetime.now(timezone.utc).isoformat()}] {text}", flush=True)
            if args.notify:
                _telegram_send(text)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            msg = f"⚠️ BTC Quant v4.1 observer error: {e}"
            print(msg, flush=True)
            if args.notify:
                _telegram_send(msg)
        time.sleep(max(60, int(args.minutes) * 60))


def cmd_view(args: argparse.Namespace) -> None:
    path = DATA_DIR / f"{args.symbol}_snapshots.csv"
    rows = _read_snapshots(path)
    if not rows:
        print("no snapshots yet")
        return
    for r in rows[-args.tail :]:
        print(json.dumps(r, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="v4.1 BTC quant data observer: OHLCV + funding + current OI")
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--symbol", default=os.getenv("BT_V41_SYMBOL", "BTCUSDT"))
    common.add_argument("--interval", default=os.getenv("BT_V41_INTERVAL", "1H"))
    common.add_argument("--candle-limit", type=int, default=int(os.getenv("BT_V41_CANDLE_LIMIT", "500")))
    one = sub.add_parser("once", parents=[common])
    one.add_argument("--notify", action="store_true", default=os.getenv("BT_V41_NOTIFY", "false").lower() == "true")
    one.set_defaults(func=cmd_once)
    loop = sub.add_parser("loop", parents=[common])
    loop.add_argument("--minutes", type=int, default=int(os.getenv("BT_V41_LOOP_MINUTES", "15")))
    loop.add_argument("--notify", action="store_true", default=os.getenv("BT_V41_NOTIFY", "true").lower() == "true")
    loop.set_defaults(func=cmd_loop)
    view = sub.add_parser("view", parents=[common])
    view.add_argument("--tail", type=int, default=int(os.getenv("BT_V41_TAIL", "20")))
    view.set_defaults(func=cmd_view)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
