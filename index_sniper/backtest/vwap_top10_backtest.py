from __future__ import annotations

import argparse
import csv
import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

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

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 3 * 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1H": 60 * 60_000,
}

DEFAULT_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT",
    "TRXUSDT", "HYPEUSDT", "DOGEUSDT", "ZECUSDT", "ADAUSDT",
)

Side = Literal["long", "short"]
SameCandleMode = Literal["skip", "worst", "long_first", "short_first"]


@dataclass
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0


@dataclass
class Feature:
    ts: int
    price: float
    vwap: float
    upper: float
    lower: float
    std: float
    adx: float
    band_width_pct: float
    eligible: bool


@dataclass
class Position:
    symbol: str
    side: Side
    entry_ts: int
    entry_price: float
    qty: float
    notional: float
    tp: float
    sl: float
    entry_fee: float


@dataclass
class Trade:
    symbol: str
    side: str
    entry_time_utc: str
    exit_time_utc: str
    entry_price: float
    exit_price: float
    qty: float
    notional: float
    pnl: float
    fees: float
    net_pnl: float
    return_on_equity_pct: float
    exit_reason: str
    leverage: float
    capital_ratio: float


@dataclass
class Config:
    symbols: tuple[str, ...]
    interval: str = "1m"
    days: int = 30
    initial_equity: float = 1374.0
    total_capital_ratio: float = 0.30
    per_order_capital_ratio_cap: float = 0.003
    leverage: float = 1.0
    max_order_notional_usdt: float = 200.0
    max_open_positions: int = 3
    max_trades_per_day: int = 999999
    vwap_window: int = 180
    warmup_bars: int = 30
    band_std_mult: float = 2.0
    adx_period: int = 14
    adx_max: float = 20.0
    tp_pct: float = 0.006
    sl_pct: float = 0.003
    maker_fee_rate: float = 0.0002
    taker_fee_rate: float = 0.0005
    shock_pct: float = 0.15
    shock_cooldown_bars: int = 10
    same_candle_mode: SameCandleMode = "skip"
    min_trade_notional: float = 5.0
    min_trades: int = 10


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except Exception:
        return default


def _env_symbols() -> tuple[str, ...]:
    raw = os.getenv("BT_V52_SYMBOLS") or os.getenv("V52_SYMBOLS") or ""
    if raw.strip():
        return tuple(s.strip().upper() for s in raw.split(",") if s.strip())
    count = _env_int("BT_V52_UNIVERSE_COUNT", _env_int("V52_UNIVERSE_COUNT", 10))
    return DEFAULT_SYMBOLS[: max(1, min(count, len(DEFAULT_SYMBOLS)))]


def _iso_ms(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()


def _utc_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _avg(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _avg(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _pct(a: float, b: float) -> float:
    return 0.0 if b == 0 else (a / b - 1.0) * 100.0


def _parse_bitget_rows(rows: list[Any]) -> list[Candle]:
    out: list[Candle] = []
    for r in rows:
        try:
            if isinstance(r, dict):
                ts = int(float(r.get("ts") or r.get("time") or r.get("openTime") or r.get("startTime")))
                op = float(r.get("open") or r.get("openPr"))
                hi = float(r.get("high") or r.get("highPr"))
                lo = float(r.get("low") or r.get("lowPr"))
                cl = float(r.get("close") or r.get("closePr"))
                vol = float(r.get("volume") or r.get("baseVolume") or r.get("vol") or 0)
                turnover = float(r.get("turnover") or r.get("quoteVolume") or r.get("quoteVol") or 0)
            else:
                # Bitget UTA rows normally: [ts, open, high, low, close, volume, turnover]
                ts = int(float(r[0])); op = float(r[1]); hi = float(r[2]); lo = float(r[3]); cl = float(r[4])
                vol = float(r[5]) if len(r) > 5 else 0.0
                turnover = float(r[6]) if len(r) > 6 else 0.0
            if ts > 10_000_000_000_000:
                ts = int(ts / 1000)
            out.append(Candle(ts=ts, open=op, high=hi, low=lo, close=cl, volume=vol, turnover=turnover))
        except Exception:
            continue
    out.sort(key=lambda c: c.ts)
    return out


def candles_to_csv(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ts", "utc", "open", "high", "low", "close", "volume", "turnover"])
        for c in candles:
            w.writerow([c.ts, _iso_ms(c.ts), c.open, c.high, c.low, c.close, c.volume, c.turnover])


def candles_from_csv(path: Path) -> list[Candle]:
    out: list[Candle] = []
    with path.open("r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            out.append(Candle(
                ts=int(float(row["ts"])),
                open=float(row["open"]), high=float(row["high"]), low=float(row["low"]), close=float(row["close"]),
                volume=float(row.get("volume") or 0.0), turnover=float(row.get("turnover") or 0.0),
            ))
    out.sort(key=lambda c: c.ts)
    return out


def fetch_bitget_history_candles(symbol: str, interval: str, days: int, data_dir: Path, refresh: bool = False) -> list[Candle]:
    if interval not in INTERVAL_MS:
        raise RuntimeError(f"unsupported interval: {interval}")
    safe = interval.replace("/", "_")
    path = data_dir / f"{symbol}_{safe}_{days}d_bitget.csv"
    if path.exists() and not refresh:
        return candles_from_csv(path)

    end_dt = datetime.now(timezone.utc)
    # add warmup buffer for VWAP/ADX
    start_dt = end_dt - timedelta(days=days + 2)
    step_ms = INTERVAL_MS[interval]
    page_span_ms = step_ms * 100 - 1
    start_ms = _utc_ms(start_dt)
    end_ms = _utc_ms(end_dt)
    cursor = start_ms - (start_ms % step_ms)
    url = "https://api.bitget.com/api/v3/market/history-candles"
    headers = {"User-Agent": "IndexSniperV52Backtest/1.0", "locale": "en-US"}
    all_rows: dict[int, Candle] = {}
    calls = 0
    last_error: str | None = None
    while cursor < end_ms:
        page_end = min(cursor + page_span_ms, end_ms)
        params = {
            "category": "USDT-FUTURES",
            "symbol": symbol.upper(),
            "interval": interval,
            "type": "market",
            "startTime": str(cursor),
            "endTime": str(page_end),
            "limit": "100",
        }
        data = None
        for attempt in range(4):
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=20)
                payload = resp.json()
                if resp.status_code >= 400 or str(payload.get("code")) not in {"00000", "0"}:
                    raise RuntimeError(f"HTTP {resp.status_code}: {payload}")
                data = payload.get("data") or []
                break
            except Exception as exc:
                last_error = str(exc)
                time.sleep(0.4 * (attempt + 1))
        if data is None:
            raise RuntimeError(f"Bitget fetch failed {symbol} near {cursor}: {last_error}")
        for c in _parse_bitget_rows(data):
            if start_ms <= c.ts <= end_ms:
                all_rows[c.ts] = c
        calls += 1
        if calls % 20 == 0:
            time.sleep(0.15)
        cursor = page_end + 1
    candles = sorted(all_rows.values(), key=lambda c: c.ts)
    if len(candles) < max(100, days * 24 * 60 // max(1, (step_ms // 60_000)) // 5):
        raise RuntimeError(f"not enough candles for {symbol}: {len(candles)}")
    candles_to_csv(path, candles)
    return candles


def typical(c: Candle) -> float:
    return (c.high + c.low + c.close) / 3.0


def vwap_bands(window: list[Candle], mult: float) -> tuple[float, float, float, float]:
    prices = [typical(c) for c in window]
    vols = [max(0.0, c.volume) for c in window]
    tv = sum(vols)
    if tv <= 0:
        vwap = _avg(prices)
        sd = _std(prices)
    else:
        vwap = sum(p * v for p, v in zip(prices, vols)) / tv
        var = sum(v * ((p - vwap) ** 2) for p, v in zip(prices, vols)) / tv
        sd = math.sqrt(max(0.0, var))
    return vwap, vwap + sd * mult, vwap - sd * mult, sd


def adx(candles: list[Candle], period: int) -> float:
    if len(candles) < period * 2 + 2:
        return 999.0
    trs: list[float] = []
    pdms: list[float] = []
    ndms: list[float] = []
    for i in range(1, len(candles)):
        cur = candles[i]; prev = candles[i - 1]
        tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        up = cur.high - prev.high
        down = prev.low - cur.low
        pdm = up if up > down and up > 0 else 0.0
        ndm = down if down > up and down > 0 else 0.0
        trs.append(tr); pdms.append(pdm); ndms.append(ndm)
    dxs: list[float] = []
    for i in range(period, len(trs) + 1):
        tr_sum = sum(trs[i - period : i])
        if tr_sum <= 0:
            continue
        pdi = 100.0 * sum(pdms[i - period : i]) / tr_sum
        ndi = 100.0 * sum(ndms[i - period : i]) / tr_sum
        if pdi + ndi <= 0:
            continue
        dxs.append(abs(pdi - ndi) / (pdi + ndi) * 100.0)
    if len(dxs) < period:
        return 999.0
    return _avg(dxs[-period:])


def build_features(candles: list[Candle], cfg: Config) -> list[Feature | None]:
    out: list[Feature | None] = [None] * len(candles)
    min_len = max(cfg.warmup_bars, cfg.vwap_window, cfg.adx_period * 2 + 2)
    for i in range(min_len, len(candles)):
        w = candles[max(0, i - cfg.vwap_window + 1): i + 1]
        vwap, upper, lower, sd = vwap_bands(w, cfg.band_std_mult)
        a = adx(candles[max(0, i - cfg.adx_period * 4): i + 1], cfg.adx_period)
        width = ((upper - lower) / vwap * 100.0) if vwap > 0 else 0.0
        out[i] = Feature(
            ts=candles[i].ts,
            price=candles[i].close,
            vwap=vwap,
            upper=upper,
            lower=lower,
            std=sd,
            adx=a,
            band_width_pct=width,
            eligible=(a <= cfg.adx_max and width > 0),
        )
    return out


def day_key(ts: int) -> str:
    kst = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(timezone(timedelta(hours=9)))
    reset = kst.replace(hour=9, minute=0, second=0, microsecond=0)
    if kst < reset:
        reset -= timedelta(days=1)
    return reset.strftime("%Y-%m-%d")


def per_order_capital_ratio(cfg: Config, symbol_count: int) -> float:
    denom = max(1, symbol_count * 2)
    return min(cfg.per_order_capital_ratio_cap, cfg.total_capital_ratio / denom)


def _choose_entry_from_bar(bar: Candle, prev: Feature, mode: SameCandleMode) -> Side | None:
    long_hit = bar.low <= prev.lower
    short_hit = bar.high >= prev.upper
    if long_hit and short_hit:
        if mode == "skip":
            return None
        if mode == "long_first":
            return "long"
        if mode == "short_first":
            return "short"
        # worst: choose the side with worse close-to-entry mark-to-market at bar close
        long_ret = (bar.close / prev.lower - 1.0)
        short_ret = (prev.upper / bar.close - 1.0)
        return "long" if long_ret < short_ret else "short"
    if long_hit:
        return "long"
    if short_hit:
        return "short"
    return None


def _exit_position(pos: Position, bar: Candle, feat: Feature | None, cfg: Config) -> tuple[float, str, float] | None:
    # Returns exit_price, reason, exit_fee_rate. Conservative priority: SL before TP before VWAP touch.
    if pos.side == "long":
        if bar.low <= pos.sl:
            return pos.sl, "stop_loss", cfg.taker_fee_rate
        if bar.high >= pos.tp:
            return pos.tp, "take_profit", cfg.maker_fee_rate
        if feat is not None and feat.vwap > 0 and bar.high >= feat.vwap:
            return feat.vwap, "vwap_touch", cfg.taker_fee_rate
    else:
        if bar.high >= pos.sl:
            return pos.sl, "stop_loss", cfg.taker_fee_rate
        if bar.low <= pos.tp:
            return pos.tp, "take_profit", cfg.maker_fee_rate
        if feat is not None and feat.vwap > 0 and bar.low <= feat.vwap:
            return feat.vwap, "vwap_touch", cfg.taker_fee_rate
    return None


def run_backtest(data: dict[str, list[Candle]], cfg: Config) -> tuple[dict[str, Any], list[Trade], list[dict[str, Any]]]:
    features = {sym: build_features(c, cfg) for sym, c in data.items()}
    index_by_ts: dict[int, list[tuple[str, int]]] = {}
    for sym, candles in data.items():
        for i, c in enumerate(candles):
            index_by_ts.setdefault(c.ts, []).append((sym, i))

    equity = cfg.initial_equity
    peak = equity
    max_dd = 0.0
    positions: dict[str, Position] = {}
    cooldown_until: dict[str, int] = {}
    last_close: dict[str, float] = {}
    trades: list[Trade] = []
    curve: list[dict[str, Any]] = []
    trades_per_day: dict[str, int] = {}
    po_ratio = per_order_capital_ratio(cfg, len(cfg.symbols))

    start_cutoff = min(c[-1].ts for c in data.values() if c) - cfg.days * 24 * 60 * 60 * 1000

    for ts in sorted(index_by_ts):
        # Do not trade warmup buffer period.
        if ts < start_cutoff:
            for sym, i in index_by_ts[ts]:
                last_close[sym] = data[sym][i].close
            continue

        # 1) Manage exits first.
        for sym, i in list(index_by_ts[ts]):
            if sym not in positions:
                continue
            pos = positions[sym]
            bar = data[sym][i]
            feat = features[sym][i]
            exit_info = _exit_position(pos, bar, feat, cfg)
            if exit_info is None:
                continue
            exit_price, reason, exit_fee_rate = exit_info
            if pos.side == "long":
                pnl = (exit_price - pos.entry_price) * pos.qty
            else:
                pnl = (pos.entry_price - exit_price) * pos.qty
            exit_fee = pos.notional * exit_fee_rate
            fees = pos.entry_fee + exit_fee
            net = pnl - fees
            equity += net
            trades.append(Trade(
                symbol=sym,
                side=pos.side,
                entry_time_utc=_iso_ms(pos.entry_ts),
                exit_time_utc=_iso_ms(ts),
                entry_price=pos.entry_price,
                exit_price=exit_price,
                qty=pos.qty,
                notional=pos.notional,
                pnl=pnl,
                fees=fees,
                net_pnl=net,
                return_on_equity_pct=(net / max(1e-12, equity - net) * 100.0),
                exit_reason=reason,
                leverage=cfg.leverage,
                capital_ratio=cfg.total_capital_ratio,
            ))
            del positions[sym]

        # 2) Entries from previous feature orders, valid for current bar.
        for sym, i in index_by_ts[ts]:
            if sym in positions:
                continue
            if len(positions) >= cfg.max_open_positions:
                continue
            if i <= 0:
                continue
            dk = day_key(ts)
            if trades_per_day.get(dk, 0) >= cfg.max_trades_per_day:
                continue
            if cooldown_until.get(sym, 0) > ts:
                continue
            candles = data[sym]
            bar = candles[i]
            prev_bar = candles[i - 1]
            prev_feat = features[sym][i - 1]
            if prev_feat is None or not prev_feat.eligible:
                last_close[sym] = bar.close
                continue
            # Shock filter, approximate video rule.
            lc = last_close.get(sym, prev_bar.close)
            if lc > 0 and abs(_pct(bar.close, lc)) >= cfg.shock_pct:
                cooldown_until[sym] = ts + cfg.shock_cooldown_bars * INTERVAL_MS[cfg.interval]
                last_close[sym] = bar.close
                continue
            side = _choose_entry_from_bar(bar, prev_feat, cfg.same_candle_mode)
            if side is None:
                last_close[sym] = bar.close
                continue
            entry = prev_feat.lower if side == "long" else prev_feat.upper
            if entry <= 0:
                last_close[sym] = bar.close
                continue
            notional = min(equity * po_ratio * cfg.leverage, cfg.max_order_notional_usdt)
            if notional < cfg.min_trade_notional:
                last_close[sym] = bar.close
                continue
            qty = notional / entry
            entry_fee = notional * cfg.maker_fee_rate
            if side == "long":
                tp = entry * (1.0 + cfg.tp_pct)
                sl = entry * (1.0 - cfg.sl_pct)
            else:
                tp = entry * (1.0 - cfg.tp_pct)
                sl = entry * (1.0 + cfg.sl_pct)
            equity -= entry_fee  # entry fee is paid immediately.
            positions[sym] = Position(symbol=sym, side=side, entry_ts=ts, entry_price=entry, qty=qty, notional=notional, tp=tp, sl=sl, entry_fee=entry_fee)
            trades_per_day[dk] = trades_per_day.get(dk, 0) + 1
            last_close[sym] = bar.close

        # 3) Mark curve by timestamp using realized equity only.
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        if len(curve) == 0 or ts - int(curve[-1]["ts"]) >= 60 * 60 * 1000:
            curve.append({"ts": ts, "utc": _iso_ms(ts), "equity": equity, "drawdown_pct": dd, "open_positions": len(positions)})

    # Close any leftovers at last close, taker fee.
    last_ts = max(index_by_ts) if index_by_ts else 0
    for sym, pos in list(positions.items()):
        exit_price = data[sym][-1].close
        pnl = (exit_price - pos.entry_price) * pos.qty if pos.side == "long" else (pos.entry_price - exit_price) * pos.qty
        exit_fee = pos.notional * cfg.taker_fee_rate
        fees = pos.entry_fee + exit_fee
        net = pnl - fees
        equity += net
        trades.append(Trade(
            symbol=sym, side=pos.side, entry_time_utc=_iso_ms(pos.entry_ts), exit_time_utc=_iso_ms(last_ts),
            entry_price=pos.entry_price, exit_price=exit_price, qty=pos.qty, notional=pos.notional,
            pnl=pnl, fees=fees, net_pnl=net, return_on_equity_pct=(net / max(1e-12, equity - net) * 100.0),
            exit_reason="end_of_test", leverage=cfg.leverage, capital_ratio=cfg.total_capital_ratio,
        ))

    wins = sum(1 for t in trades if t.net_pnl > 0)
    gross_profit = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gross_loss = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    pf = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    peak2 = cfg.initial_equity
    max_dd2 = 0.0
    eq_tmp = cfg.initial_equity
    max_loss_streak = 0
    cur_loss_streak = 0
    for t in trades:
        eq_tmp += t.net_pnl
        peak2 = max(peak2, eq_tmp)
        max_dd2 = max(max_dd2, (peak2 - eq_tmp) / peak2 * 100.0 if peak2 > 0 else 0.0)
        if t.net_pnl < 0:
            cur_loss_streak += 1
            max_loss_streak = max(max_loss_streak, cur_loss_streak)
        else:
            cur_loss_streak = 0
    summary = {
        "start_equity": round(cfg.initial_equity, 6),
        "end_equity": round(equity, 6),
        "return_pct": round((equity / cfg.initial_equity - 1.0) * 100.0, 6) if cfg.initial_equity > 0 else 0.0,
        "max_drawdown_pct": round(max_dd2, 6),
        "trade_count": len(trades),
        "win_rate_pct": round(wins / len(trades) * 100.0, 6) if trades else 0.0,
        "profit_factor": round(pf, 6),
        "max_loss_streak": max_loss_streak,
        "symbols": list(cfg.symbols),
        "interval": cfg.interval,
        "days": cfg.days,
        "leverage": cfg.leverage,
        "capital_ratio": cfg.total_capital_ratio,
        "per_order_capital_ratio": po_ratio,
        "max_open_positions": cfg.max_open_positions,
        "fees": {"maker": cfg.maker_fee_rate, "taker": cfg.taker_fee_rate},
        "assumption": "1-bar valid maker orders at prior VWAP bands; conservative SL priority; 1m/5m candle approximation, not tick-perfect.",
    }
    return summary, trades, curve


def load_data(cfg: Config, refresh: bool = False) -> dict[str, list[Candle]]:
    data_dir = ROOT / "backtests" / "v52_vwap_top10" / "data"
    out: dict[str, list[Candle]] = {}
    for sym in cfg.symbols:
        print(f"[data] {sym} {cfg.interval} {cfg.days}d loading Bitget history candles...", flush=True)
        cs = fetch_bitget_history_candles(sym, cfg.interval, cfg.days, data_dir, refresh=refresh)
        print(f"[data] {sym}: bars={len(cs)} from {_iso_ms(cs[0].ts)} to {_iso_ms(cs[-1].ts)}", flush=True)
        out[sym] = cs
    return out


def config_from_env(leverage: float | None = None, capital_ratio: float | None = None) -> Config:
    syms = _env_symbols()
    return Config(
        symbols=syms,
        interval=os.getenv("BT_V52_INTERVAL", "1m").strip(),
        days=_env_int("BT_V52_DAYS", 30),
        initial_equity=_env_float("BT_INITIAL_EQUITY", _env_float("BT_V52_INITIAL_EQUITY", 1374.0)),
        total_capital_ratio=capital_ratio if capital_ratio is not None else _env_float("BT_V52_TOTAL_CAPITAL_RATIO", _env_float("V52_TOTAL_CAPITAL_RATIO", 0.30)),
        per_order_capital_ratio_cap=_env_float("BT_V52_PER_ORDER_CAPITAL_RATIO_CAP", _env_float("V52_PER_ORDER_CAPITAL_RATIO_CAP", 0.003)),
        leverage=leverage if leverage is not None else _env_float("BT_V52_LEVERAGE", _env_float("V52_LEVERAGE", 1.0)),
        max_order_notional_usdt=_env_float("BT_V52_MAX_ORDER_NOTIONAL_USDT", _env_float("V52_MAX_ORDER_NOTIONAL_USDT", 200.0)),
        max_open_positions=_env_int("BT_V52_MAX_OPEN_POSITIONS", _env_int("V52_MAX_OPEN_POSITIONS", 3)),
        max_trades_per_day=_env_int("BT_V52_MAX_TRADES_PER_DAY", _env_int("V52_MAX_TRADES_PER_DAY", 999999)),
        vwap_window=_env_int("BT_V52_VWAP_WINDOW", _env_int("V52_VWAP_WINDOW_MINUTES", 180)),
        warmup_bars=_env_int("BT_V52_WARMUP_BARS", _env_int("V52_WARMUP_MINUTES", 10)),
        band_std_mult=_env_float("BT_V52_BAND_STD_MULT", _env_float("V52_BAND_STD_MULT", 2.0)),
        adx_period=_env_int("BT_V52_ADX_PERIOD", _env_int("V52_ADX_PERIOD", 14)),
        adx_max=_env_float("BT_V52_ADX_MAX", _env_float("V52_ADX_MAX", 20.0)),
        tp_pct=_env_float("BT_V52_TP_PCT", _env_float("V52_TP_PCT", 0.006)),
        sl_pct=_env_float("BT_V52_SL_PCT", _env_float("V52_SL_PCT", 0.003)),
        maker_fee_rate=_env_float("BT_V52_MAKER_FEE_RATE", 0.0002),
        taker_fee_rate=_env_float("BT_V52_TAKER_FEE_RATE", 0.0005),
        shock_pct=_env_float("BT_V52_SHOCK_PCT", _env_float("V52_SHOCK_PCT", 0.15)),
        shock_cooldown_bars=_env_int("BT_V52_SHOCK_COOLDOWN_BARS", 10),
        same_candle_mode=os.getenv("BT_V52_SAME_CANDLE_MODE", "skip").strip(),  # type: ignore[arg-type]
        min_trade_notional=_env_float("BT_V52_MIN_TRADE_NOTIONAL", 5.0),
        min_trades=_env_int("BT_V52_MIN_TRADES", 10),
    )


def write_trades(path: Path, trades: list[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        fields = list(asdict(trades[0]).keys()) if trades else ["symbol", "side", "entry_time_utc", "exit_time_utc", "net_pnl"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            w.writerow(asdict(t))


def write_curve(path: Path, curve: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        fields = ["ts", "utc", "equity", "drawdown_pct", "open_positions"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in curve:
            w.writerow(row)


def _cell(s: dict[str, Any]) -> str:
    return f"{s['end_equity']:,.0f} / {s['max_drawdown_pct']:.1f}% / PF {s['profit_factor']:.2f} / T {s['trade_count']}"


def cmd_sweep(args: argparse.Namespace) -> None:
    out_dir = ROOT / "backtests" / "v52_vwap_top10"
    out_dir.mkdir(parents=True, exist_ok=True)
    levs = [int(x) for x in str(args.leverages).split(",") if str(x).strip()]
    cfg0 = config_from_env()
    cfg0.days = int(args.days)
    cfg0.interval = args.interval
    data = load_data(cfg0, refresh=args.refresh)
    rows: list[dict[str, Any]] = []
    for lev in levs:
        cfg = config_from_env(leverage=float(lev))
        cfg.days = int(args.days)
        cfg.interval = args.interval
        summary, trades, curve = run_backtest(data, cfg)
        rows.append(summary)
        print(f"[sweep] {lev}x => {_cell(summary)}", flush=True)
    csv_path = out_dir / "v52_vwap_leverage_sweep_latest.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        fields = list(rows[0].keys()) if rows else []
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    txt_path = out_dir / "v52_vwap_leverage_sweep_latest.txt"
    with txt_path.open("w", encoding="utf-8") as f:
        f.write("Index Sniper Pro v5.2 Top10 VWAP Scalp Backtest\n")
        f.write("================================================\n")
        f.write("Format: final equity / MDD / PF / trades\n")
        f.write(f"symbols={','.join(cfg0.symbols)} interval={args.interval} days={args.days} capital={cfg0.total_capital_ratio}\n")
        f.write(f"VWAP window={cfg0.vwap_window}, band={cfg0.band_std_mult}σ, ADX≤{cfg0.adx_max}, TP={cfg0.tp_pct*100:.2f}%, SL={cfg0.sl_pct*100:.2f}%\n")
        f.write("Assumption: 1-bar valid maker entries at prior VWAP bands; candle approximation, not tick-perfect.\n\n")
        f.write("leverage\tresult\n")
        for r in rows:
            f.write(f"{int(r['leverage'])}x\t{_cell(r)}\n")
        f.write(f"\nCSV: {csv_path}\n")
    print(txt_path.read_text())


def cmd_detail(args: argparse.Namespace) -> None:
    out_dir = ROOT / "backtests" / "v52_vwap_top10"
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = config_from_env(leverage=float(args.leverage))
    cfg.days = int(args.days)
    cfg.interval = args.interval
    data = load_data(cfg, refresh=args.refresh)
    summary, trades, curve = run_backtest(data, cfg)
    trades_path = out_dir / "v52_vwap_trades_latest.csv"
    curve_path = out_dir / "v52_vwap_equity_latest.csv"
    write_trades(trades_path, trades)
    write_curve(curve_path, curve)
    txt_path = out_dir / "v52_vwap_summary_latest.txt"
    wins = sum(1 for t in trades if t.net_pnl > 0)
    by_reason: dict[str, int] = {}
    by_symbol: dict[str, dict[str, Any]] = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
        d = by_symbol.setdefault(t.symbol, {"trades": 0, "net_pnl": 0.0, "wins": 0})
        d["trades"] += 1; d["net_pnl"] += t.net_pnl; d["wins"] += 1 if t.net_pnl > 0 else 0
    with txt_path.open("w", encoding="utf-8") as f:
        f.write("Index Sniper Pro v5.2 Top10 VWAP Scalp Detail\n")
        f.write("================================================\n")
        for k, v in summary.items():
            f.write(f"{k}: {v}\n")
        f.write("\nExit reasons:\n")
        for k, v in sorted(by_reason.items(), key=lambda x: -x[1]):
            f.write(f"- {k}: {v}\n")
        f.write("\nBy symbol:\n")
        for sym, d in sorted(by_symbol.items()):
            wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            f.write(f"- {sym}: trades={d['trades']} net={d['net_pnl']:.4f} win_rate={wr:.2f}%\n")
        f.write(f"\nTrades CSV: {trades_path}\nEquity CSV: {curve_path}\n")
    print(txt_path.read_text())


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="v5.2 Top10 VWAP scalp candle backtest")
    sub = p.add_subparsers(dest="cmd", required=True)
    sw = sub.add_parser("sweep")
    sw.add_argument("--leverages", default=os.getenv("BT_V52_LEVERAGES", "1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20"))
    sw.add_argument("--days", type=int, default=_env_int("BT_V52_DAYS", 30))
    sw.add_argument("--interval", default=os.getenv("BT_V52_INTERVAL", "1m"))
    sw.add_argument("--refresh", action="store_true")
    sw.set_defaults(func=cmd_sweep)

    de = sub.add_parser("detail")
    de.add_argument("--leverage", type=float, default=_env_float("BT_V52_LEVERAGE", 1.0))
    de.add_argument("--days", type=int, default=_env_int("BT_V52_DAYS", 30))
    de.add_argument("--interval", default=os.getenv("BT_V52_INTERVAL", "1m"))
    de.add_argument("--refresh", action="store_true")
    de.set_defaults(func=cmd_detail)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
