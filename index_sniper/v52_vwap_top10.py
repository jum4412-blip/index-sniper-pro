from __future__ import annotations

import argparse
import json
import math
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from index_sniper.exchange.bitget_uta import BitgetUTAClient, OrderIntent
from index_sniper.position import open_positions
from index_sniper.risk.sizing import (
    build_size_plan,
    extract_instrument,
    extract_symbol_config,
    extract_usdt_equity_available,
)
from index_sniper.strategy.indicators import Candle, parse_candles
from index_sniper.telegram.bot import TelegramBot

ROOT = Path(__file__).resolve().parents[1]
LIVE_CONFIRM = "START_V52_VWAP_LIVE"
STATE_PATH_DEFAULT = "data/v52_vwap_top10_state.json"
JSONL_PATH_DEFAULT = "data/v52_vwap_top10_events.jsonl"
LOG_PATH_DEFAULT = "logs/v52-vwap-top10.log"

DEFAULT_TOP10_FALLBACK = [
    "BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT", "TRXUSDT", "LINKUSDT", "BCHUSDT",
    "AVAXUSDT", "XLMUSDT", "TONUSDT", "SUIUSDT", "HBARUSDT", "LTCUSDT", "DOTUSDT", "UNIUSDT", "AAVEUSDT", "NEARUSDT",
]
STABLE_OR_WRAPPED = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "WBTC", "WETH", "STETH", "WEETH", "WSTETH", "CBBTC"}


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
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


def _required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing environment variable: {name}")
    return v.strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _kst_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


def _day_key() -> str:
    now = _kst_now()
    reset = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now < reset:
        reset = reset - timedelta(days=1)
    return reset.strftime("%Y-%m-%d_09KST")


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


def _pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a / b - 1.0) * 100.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _fmt_price(x: float) -> str:
    if abs(x) >= 1000:
        return f"{x:,.2f}"
    if abs(x) >= 1:
        return f"{x:.5f}"
    return f"{x:.8f}"


def _price_str(x: float, instrument: dict[str, Any]) -> str:
    try:
        precision = int(instrument.get("pricePrecision") or 4)
    except Exception:
        precision = 4
    precision = max(0, min(10, precision))
    return f"{x:.{precision}f}"


def _extract_rows(resp: dict[str, Any]) -> list[dict[str, Any]]:
    data = resp.get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("list", "rows", "result", "data"):
            rows = data.get(key)
            if isinstance(rows, list):
                return [x for x in rows if isinstance(x, dict)]
        if any(k in data for k in ("symbol", "instId")):
            return [data]
    return []


def _symbol_from_instrument(row: dict[str, Any]) -> str:
    return str(row.get("symbol") or row.get("instId") or "").upper().strip()


@dataclass
class V52Settings:
    category: str
    margin_coin: str
    margin_mode: str
    leverage: int
    dry_run: bool
    live_enabled: bool
    live_confirm: str
    loop_seconds: int
    candle_refresh_seconds: int
    universe_count: int
    max_active_symbols: int
    max_open_positions: int
    total_capital_ratio: float
    per_order_capital_ratio_cap: float
    max_order_notional_usdt: float
    vwap_window_minutes: int
    warmup_minutes: int
    band_std_mult: float
    adx_period: int
    adx_max: float
    tp_pct: float
    sl_pct: float
    shock_window_seconds: int
    shock_pct: float
    shock_cooldown_minutes: int
    daily_target_pct: float
    daily_loss_pct: float
    max_trades_per_day: int
    state_path: str
    jsonl_path: str
    notify_every_minutes: int
    notify_actions_only: bool
    fill_notify_enabled: bool
    fill_lookback_minutes: int
    notify_all_fills: bool
    daily_guard_enabled: bool
    use_coingecko: bool
    symbols_override: tuple[str, ...]


def load_settings() -> V52Settings:
    load_dotenv(ROOT / ".env")
    override = tuple(s.strip().upper() for s in os.getenv("V52_SYMBOLS", "").split(",") if s.strip())
    return V52Settings(
        category=os.getenv("V52_CATEGORY", os.getenv("CATEGORY", "USDT-FUTURES")).strip(),
        margin_coin=os.getenv("V52_MARGIN_COIN", os.getenv("MARGIN_COIN", "USDT")).strip().upper(),
        margin_mode=os.getenv("V52_MARGIN_MODE", os.getenv("MARGIN_MODE", "crossed")).strip(),
        leverage=_int("V52_LEVERAGE", 1),
        dry_run=_bool(os.getenv("V52_DRY_RUN"), False),
        live_enabled=_bool(os.getenv("V52_LIVE_ENABLED"), False),
        live_confirm=os.getenv("V52_LIVE_CONFIRM", "").strip(),
        loop_seconds=_int("V52_LOOP_SECONDS", 10),
        candle_refresh_seconds=_int("V52_CANDLE_REFRESH_SECONDS", 60),
        universe_count=_int("V52_UNIVERSE_COUNT", 10),
        max_active_symbols=_int("V52_MAX_ACTIVE_SYMBOLS", 10),
        max_open_positions=_int("V52_MAX_OPEN_POSITIONS", 3),
        total_capital_ratio=_float("V52_TOTAL_CAPITAL_RATIO", 0.30),
        per_order_capital_ratio_cap=_float("V52_PER_ORDER_CAPITAL_RATIO_CAP", 0.003),
        max_order_notional_usdt=_float("V52_MAX_ORDER_NOTIONAL_USDT", 200.0),
        vwap_window_minutes=_int("V52_VWAP_WINDOW_MINUTES", 180),
        warmup_minutes=_int("V52_WARMUP_MINUTES", 10),
        band_std_mult=_float("V52_BAND_STD_MULT", 2.0),
        adx_period=_int("V52_ADX_PERIOD", 14),
        adx_max=_float("V52_ADX_MAX", 20.0),
        tp_pct=_float("V52_TP_PCT", 0.006),
        sl_pct=_float("V52_SL_PCT", 0.003),
        shock_window_seconds=_int("V52_SHOCK_WINDOW_SECONDS", 5),
        shock_pct=_float("V52_SHOCK_PCT", 0.15),
        shock_cooldown_minutes=_int("V52_SHOCK_COOLDOWN_MINUTES", 10),
        daily_target_pct=_float("V52_DAILY_TARGET_PCT", 2.0),
        daily_loss_pct=_float("V52_DAILY_LOSS_PCT", 1.0),
        max_trades_per_day=_int("V52_MAX_TRADES_PER_DAY", 30),
        state_path=os.getenv("V52_STATE_PATH", STATE_PATH_DEFAULT).strip(),
        jsonl_path=os.getenv("V52_JSONL_PATH", JSONL_PATH_DEFAULT).strip(),
        notify_every_minutes=_int("V52_NOTIFY_EVERY_MINUTES", 30),
        notify_actions_only=_bool(os.getenv("V52_NOTIFY_ACTIONS_ONLY"), True),
        fill_notify_enabled=_bool(os.getenv("V52_FILL_NOTIFY_ENABLED"), True),
        fill_lookback_minutes=_int("V52_FILL_LOOKBACK_MINUTES", 180),
        notify_all_fills=_bool(os.getenv("V52_NOTIFY_ALL_FILLS"), True),
        daily_guard_enabled=_bool(os.getenv("V52_DAILY_GUARD_ENABLED"), False),
        use_coingecko=_bool(os.getenv("V52_USE_COINGECKO"), True),
        symbols_override=override,
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


def ensure_daily_state(state: dict[str, Any], equity: float) -> dict[str, Any]:
    key = _day_key()
    if state.get("day_key") != key:
        state = {
            "day_key": key,
            "baseline_equity": equity,
            "halted": False,
            "halt_reason": "",
            "trades_today": 0,
            "last_notify_ts": None,
            "symbols": {},
            "orders": {},
            "last_prices": {},
            "cooldowns": {},
        }
    state.setdefault("baseline_equity", equity)
    state.setdefault("halted", False)
    state.setdefault("trades_today", 0)
    state.setdefault("symbols", {})
    state.setdefault("orders", {})
    state.setdefault("last_prices", {})
    state.setdefault("cooldowns", {})
    return state


def _send(bot: TelegramBot, text: str) -> None:
    try:
        bot.send(text)
    except Exception:
        pass


def _instrument_map(client: BitgetUTAClient, settings: V52Settings) -> dict[str, dict[str, Any]]:
    resp = client.instruments(category=settings.category)
    out: dict[str, dict[str, Any]] = {}
    for row in _extract_rows(resp):
        sym = _symbol_from_instrument(row)
        if sym:
            out[sym] = row
    return out


def _coingecko_top_symbols(count: int) -> list[str]:
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": min(100, max(count * 3, 50)),
        "page": 1,
        "sparkline": "false",
    }
    rows = requests.get(url, params=params, timeout=10).json()
    syms: list[str] = []
    if not isinstance(rows, list):
        return syms
    for row in rows:
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym or sym in STABLE_OR_WRAPPED:
            continue
        usdt = sym + "USDT"
        if usdt not in syms:
            syms.append(usdt)
        if len(syms) >= count:
            break
    return syms


def resolve_universe(client: BitgetUTAClient, settings: V52Settings) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, Any]]:
    instruments = _instrument_map(client, settings)
    available = set(instruments)
    source = "override"
    candidates: list[str]
    if settings.symbols_override:
        candidates = list(settings.symbols_override)
    else:
        source = "coingecko"
        candidates = []
        if settings.use_coingecko:
            try:
                candidates = _coingecko_top_symbols(settings.universe_count)
            except Exception:
                candidates = []
        if not candidates:
            source = "fallback"
            candidates = DEFAULT_TOP10_FALLBACK[: settings.universe_count]

    selected: list[str] = []
    for sym in candidates:
        if sym in available and sym not in selected:
            selected.append(sym)
        if len(selected) >= settings.universe_count:
            break
    # If market-cap candidates are missing on Bitget, fill with default majors that are tradable.
    for sym in DEFAULT_TOP10_FALLBACK:
        if len(selected) >= settings.universe_count:
            break
        if sym in available and sym not in selected:
            selected.append(sym)
    return selected, instruments, {"source": source, "candidates": candidates, "selected": selected}


def _candles(client: BitgetUTAClient, symbol: str, category: str, interval: str, limit: int) -> list[Candle]:
    resp = client.candles(symbol=symbol, category=category, interval=interval, limit=limit)
    candles = parse_candles(resp)
    candles.sort(key=lambda c: c.ts)
    return candles


def _typical(c: Candle) -> float:
    return (float(c.high) + float(c.low) + float(c.close)) / 3.0


def _vwap_bands(candles: list[Candle], mult: float) -> dict[str, float]:
    vols = [max(0.0, float(c.volume)) for c in candles]
    prices = [_typical(c) for c in candles]
    total_vol = sum(vols)
    if total_vol <= 0:
        vwap = _avg(prices)
        sd = _std(prices)
    else:
        vwap = sum(p * v for p, v in zip(prices, vols)) / total_vol
        var = sum(v * ((p - vwap) ** 2) for p, v in zip(prices, vols)) / total_vol
        sd = math.sqrt(max(0.0, var))
    return {
        "vwap": vwap,
        "std": sd,
        "upper": vwap + sd * mult,
        "lower": vwap - sd * mult,
    }


def _adx(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period * 2 + 2:
        return 999.0
    trs: list[float] = []
    pdms: list[float] = []
    ndms: list[float] = []
    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        high = float(cur.high)
        low = float(cur.low)
        ph = float(prev.high)
        pl = float(prev.low)
        pc = float(prev.close)
        tr = max(high - low, abs(high - pc), abs(low - pc))
        up = high - ph
        down = pl - low
        pdm = up if up > down and up > 0 else 0.0
        ndm = down if down > up and down > 0 else 0.0
        trs.append(tr)
        pdms.append(pdm)
        ndms.append(ndm)
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


def _ticker_map(client: BitgetUTAClient, settings: V52Settings) -> dict[str, float]:
    resp = client.tickers(category=settings.category)
    out: dict[str, float] = {}
    for row in _extract_rows(resp):
        sym = _symbol_from_instrument(row)
        if not sym:
            continue
        for key in ("lastPrice", "lastPr", "last", "close", "price", "markPrice"):
            if row.get(key) not in (None, ""):
                out[sym] = _safe_float(row.get(key))
                break
    return out


def _open_positions_map(client: BitgetUTAClient, settings: V52Settings) -> dict[str, dict[str, Any]]:
    rows = open_positions(client.current_position(category=settings.category))
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        sym = str(row.get("symbol") or row.get("instId") or "").upper()
        if sym:
            out[sym] = row
    return out


def _is_success(resp: dict[str, Any]) -> bool:
    return str(resp.get("code")) in {"00000", "0"} or resp.get("dry_run") is True


def _place_limit_order(
    client: BitgetUTAClient,
    *,
    settings: V52Settings,
    symbol: str,
    side: str,
    pos_side: str,
    qty: str,
    price: str,
    client_oid: str,
    take_profit: str | None = None,
    stop_loss: str | None = None,
    tp_limit_price: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "category": settings.category,
        "symbol": symbol,
        "marginCoin": settings.margin_coin,
        "marginMode": settings.margin_mode,
        "orderType": "limit",
        "timeInForce": "post_only",
        "side": side,
        "posSide": pos_side,
        "qty": str(qty),
        "price": str(price),
        "clientOid": client_oid,
    }
    if take_profit:
        payload["takeProfit"] = str(take_profit)
        payload["tpTriggerBy"] = "market"
        payload["tpOrderType"] = "limit"
        if tp_limit_price:
            payload["tpLimitPrice"] = str(tp_limit_price)
    if stop_loss:
        payload["stopLoss"] = str(stop_loss)
        payload["slTriggerBy"] = "market"
        payload["slOrderType"] = "market"
    if settings.dry_run:
        return {"dry_run": True, "payload": payload}
    return client.post("/api/v3/trade/place-order", payload)


def _cancel_symbol_orders(client: BitgetUTAClient, settings: V52Settings, symbol: str) -> dict[str, Any]:
    payload = {"category": settings.category, "symbol": symbol}
    if settings.dry_run:
        return {"dry_run": True, "payload": payload}
    return client.post("/api/v3/trade/cancel-symbol-order", payload)


def _close_position(client: BitgetUTAClient, settings: V52Settings, pos: dict[str, Any], reason: str) -> dict[str, Any]:
    symbol = str(pos.get("symbol") or pos.get("instId") or "").upper()
    side = str(pos.get("_parsed_side") or pos.get("posSide") or pos.get("holdSide") or "").lower()
    qty = str(pos.get("_parsed_qty") or pos.get("total") or pos.get("available") or "0")
    if symbol == "" or side not in {"long", "short"}:
        raise RuntimeError(f"unknown position row: {pos}")
    close_side = "sell" if side == "long" else "buy"
    intent = OrderIntent(
        symbol=symbol,
        side=close_side,
        pos_side=side,
        qty=qty,
        category=settings.category,
        margin_coin=settings.margin_coin,
        margin_mode=settings.margin_mode,
        client_oid=f"v52close-{symbol.lower()}-{int(time.time())}",
    )
    res = client.place_order(intent, dry_run=settings.dry_run)
    return {"reason": reason, "symbol": symbol, "side": side, "qty": qty, "result": res}


def _per_order_capital_ratio(settings: V52Settings, selected_count: int) -> float:
    # The video uses many tiny orders. To avoid margin explosion, split total risk across both bands and symbols.
    denom = max(1, min(selected_count, settings.max_active_symbols) * 2)
    return min(settings.per_order_capital_ratio_cap, settings.total_capital_ratio / denom)


def _signal_for_symbol(
    client: BitgetUTAClient,
    settings: V52Settings,
    symbol: str,
    price: float,
    state: dict[str, Any],
) -> dict[str, Any]:
    limit = max(60, min(1000, settings.vwap_window_minutes))
    c1 = _candles(client, symbol, settings.category, "1m", limit)
    if len(c1) < max(30, settings.warmup_minutes + settings.adx_period * 2):
        return {"symbol": symbol, "ok": False, "reason": f"not enough 1m candles {len(c1)}", "price": price}

    bands = _vwap_bands(c1[-limit:], settings.band_std_mult)
    adx_val = _adx(c1[-max(80, settings.adx_period * 4):], settings.adx_period)
    vwap = bands["vwap"]
    upper = bands["upper"]
    lower = bands["lower"]
    dist_vwap_pct = _pct(price, vwap)
    width_pct = ((upper - lower) / vwap * 100.0) if vwap > 0 else 0.0
    eligible = adx_val <= settings.adx_max and width_pct > 0
    return {
        "symbol": symbol,
        "ok": True,
        "eligible": eligible,
        "reason": "eligible" if eligible else f"adx {adx_val:.2f} > {settings.adx_max}",
        "price": price,
        "vwap": vwap,
        "upper": upper,
        "lower": lower,
        "std": bands["std"],
        "band_width_pct": width_pct,
        "dist_vwap_pct": dist_vwap_pct,
        "adx": adx_val,
    }


def _daily_guard(
    client: BitgetUTAClient,
    settings: V52Settings,
    state: dict[str, Any],
    positions: dict[str, dict[str, Any]],
    equity: float,
) -> tuple[bool, list[dict[str, Any]], float]:
    baseline = float(state.get("baseline_equity") or equity or 0)
    pnl_pct = ((equity - baseline) / baseline * 100.0) if baseline > 0 else 0.0
    actions: list[dict[str, Any]] = []
    if not settings.daily_guard_enabled:
        state["halted"] = False
        state["halt_reason"] = "daily guard disabled"
        return True, actions, pnl_pct
    if pnl_pct >= settings.daily_target_pct:
        state["halted"] = True
        state["halt_reason"] = f"daily target reached {pnl_pct:.3f}%"
        for sym, pos in list(positions.items()):
            try:
                actions.append(_close_position(client, settings, pos, "daily target"))
            except Exception as exc:
                actions.append({"symbol": sym, "error": str(exc)})
        return False, actions, pnl_pct
    if pnl_pct <= -settings.daily_loss_pct:
        state["halted"] = True
        state["halt_reason"] = f"daily loss reached {pnl_pct:.3f}%"
        for sym, pos in list(positions.items()):
            try:
                actions.append(_close_position(client, settings, pos, "daily loss"))
            except Exception as exc:
                actions.append({"symbol": sym, "error": str(exc)})
        return False, actions, pnl_pct
    if state.get("halted"):
        return False, actions, pnl_pct
    return True, actions, pnl_pct


def _should_notify(state: dict[str, Any], settings: V52Settings, force: bool = False) -> bool:
    if force:
        return True
    last = state.get("last_notify_ts")
    if not last:
        return True
    try:
        return (_utc_now() - datetime.fromisoformat(last)).total_seconds() >= settings.notify_every_minutes * 60
    except Exception:
        return True



def _recent_fills(client: BitgetUTAClient, settings: V52Settings) -> list[dict[str, Any]]:
    if settings.dry_run or not settings.fill_notify_enabled:
        return []
    now_ms = int(time.time() * 1000)
    lookback_ms = max(1, settings.fill_lookback_minutes) * 60 * 1000
    params = {
        "category": settings.category,
        "startTime": str(now_ms - lookback_ms),
        "endTime": str(now_ms),
        "limit": "100",
    }
    try:
        resp = client.get("/api/v3/trade/fills", params)
    except Exception as exc:
        return [{"_error": str(exc)}]
    return _extract_rows(resp)


def _is_bot_fill(fill: dict[str, Any]) -> bool:
    oid = str(fill.get("clientOid") or "").lower()
    return oid.startswith("v52-") or oid.startswith("v52close-") or oid.startswith("v51-") or oid.startswith("v51close-")


def _poll_new_fills(client: BitgetUTAClient, settings: V52Settings, state: dict[str, Any]) -> list[dict[str, Any]]:
    raw = _recent_fills(client, settings)
    if raw and raw[0].get("_error"):
        return raw
    seen = set(str(x) for x in state.get("seen_fill_exec_ids", []))
    new: list[dict[str, Any]] = []
    for f in raw:
        if not settings.notify_all_fills and not _is_bot_fill(f):
            continue
        key = str(f.get("execId") or f.get("execLinkId") or f.get("orderId") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        new.append(f)
    # newest APIs may return newest-first; notify oldest-first for readability
    def _t(x: dict[str, Any]) -> int:
        try:
            return int(x.get("createdTime") or x.get("updatedTime") or 0)
        except Exception:
            return 0
    new.sort(key=_t)
    state["seen_fill_exec_ids"] = list(seen)[-1000:]
    return new


def _fee_text(fill: dict[str, Any]) -> str:
    fees = fill.get("feeDetail") or []
    if isinstance(fees, list) and fees:
        parts = []
        for row in fees:
            if isinstance(row, dict):
                parts.append(f"{row.get('fee','?')} {row.get('feeCoin','')}")
        return ", ".join(parts)
    return ""


def _fills_message(fills: list[dict[str, Any]]) -> str:
    if not fills:
        return ""
    if fills and fills[0].get("_error"):
        return f"⚠️ <b>v5.2 VWAP fill 조회 오류</b>\n{str(fills[0].get('_error'))[:800]}"
    lines = ["📌 <b>v5.2 VWAP 체결내역</b>"]
    for f in fills[:20]:
        ts = str(f.get("createdTime") or "")
        try:
            ts_dt = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).astimezone(timezone(timedelta(hours=9)))
            ts_s = ts_dt.strftime("%H:%M:%S KST")
        except Exception:
            ts_s = ts
        pnl = f.get("execPnl")
        pnl_s = f" / PnL {pnl}" if pnl not in (None, "", "0", "0.0") else ""
        fee_s = _fee_text(f)
        fee_txt = f" / fee {fee_s}" if fee_s else ""
        lines.append(
            f"- {ts_s} {f.get('symbol')} {str(f.get('tradeSide') or '').upper()} {str(f.get('side') or '').upper()} "
            f"{f.get('execQty')} @ {f.get('execPrice')} / {str(f.get('tradeScope') or '').upper()}{pnl_s}{fee_txt}"
        )
    if len(fills) > 20:
        lines.append(f"...외 {len(fills)-20}건")
    return "\n".join(lines)

def _summary_message(settings: V52Settings, result: dict[str, Any]) -> str:
    actions = result.get("actions") or []
    sigs = result.get("signals") or []
    eligible = [s for s in sigs if s.get("eligible")]
    top = sorted(sigs, key=lambda s: abs(float(s.get("dist_vwap_pct") or 0)), reverse=True)[:5]
    lines = [
        "🧲 <b>v5.2 Top10 VWAP Rubber Band</b>",
        f"상태: {result.get('status')} / 실주문: {'없음(DRY)' if settings.dry_run else '있음(LIVE)'}",
        (f"일일손익: {float(result.get('day_pnl_pct') or 0):.3f}% / 일일가드 OFF" if not settings.daily_guard_enabled else f"일일손익: {float(result.get('day_pnl_pct') or 0):.3f}% / 목표 +{settings.daily_target_pct}% / 손실 -{settings.daily_loss_pct}%"),
        f"유니버스: {len(result.get('universe') or [])}개 / eligible {len(eligible)}개 / actions {len(actions)}개",
        f"규칙: ADX≤{settings.adx_max}, VWAP±{settings.band_std_mult}σ, TP {settings.tp_pct*100:.2f}%, SL {settings.sl_pct*100:.2f}%",
    ]
    if actions:
        lines.append("\n<b>실행/주문</b>")
        for a in actions[:8]:
            lines.append(f"- {a.get('symbol')} {a.get('action') or a.get('type') or a.get('reason')} {a.get('side','')} {a.get('price','')} {a.get('qty','')}")
    if top:
        lines.append("\n<b>상위 이탈</b>")
        for s in top:
            lines.append(
                f"- {s.get('symbol')}: px {_fmt_price(float(s.get('price') or 0))} / VWAP {_fmt_price(float(s.get('vwap') or 0))} / dist {float(s.get('dist_vwap_pct') or 0):.3f}% / ADX {float(s.get('adx') or 0):.1f}"
            )
    return "\n".join(lines)


def preflight() -> dict[str, Any]:
    settings = load_settings()
    client = make_client()
    assets = client.assets()
    equity, available = extract_usdt_equity_available(assets)
    universe, instruments, meta = resolve_universe(client, settings)
    positions = _open_positions_map(client, settings)
    errors = []
    if not settings.dry_run:
        if not settings.live_enabled:
            errors.append("V52_LIVE_ENABLED=true required")
        if settings.live_confirm != LIVE_CONFIRM:
            errors.append(f"V52_LIVE_CONFIRM={LIVE_CONFIRM} required")
    if settings.total_capital_ratio > 0.30:
        errors.append("V52_TOTAL_CAPITAL_RATIO > 0.30 blocked in v5.2")
    if settings.leverage not in {1, 2, 3, 4, 5}:
        errors.append("V52_LEVERAGE must be 1~5")
    if len(universe) == 0:
        errors.append("universe empty")
    sample_settings = {}
    for sym in universe[:5]:
        try:
            cfg = extract_symbol_config(client.settings(), sym)
            sample_settings[sym] = {"leverage": cfg.get("leverage") if cfg else None, "marginMode": cfg.get("marginMode") if cfg else None}
        except Exception as exc:
            sample_settings[sym] = {"error": str(exc)}
    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "settings": asdict(settings),
        "equity": equity,
        "available": available,
        "universe_meta": meta,
        "universe": universe,
        "tradable_count": len(instruments),
        "open_positions": positions,
        "per_order_capital_ratio": _per_order_capital_ratio(settings, len(universe)),
        "sample_account_settings": sample_settings,
    }


def run_once(*, notify: bool = True) -> dict[str, Any]:
    settings = load_settings()
    client = make_client()
    bot = make_bot()
    state = ensure_daily_state(load_state(settings.state_path), 0.0)

    equity, available = extract_usdt_equity_available(client.assets())
    state = ensure_daily_state(state, equity)
    positions = _open_positions_map(client, settings)
    guard_ok, guard_actions, day_pnl_pct = _daily_guard(client, settings, state, positions, equity)
    result: dict[str, Any] = {
        "ts": _utc_now().isoformat(),
        "status": "OK",
        "equity": equity,
        "available": available,
        "day_pnl_pct": day_pnl_pct,
        "actions": list(guard_actions),
        "fills": [],
        "signals": [],
        "universe": [],
    }

    fill_events = _poll_new_fills(client, settings, state)
    if fill_events:
        result["fills"] = fill_events

    if not settings.dry_run and (not settings.live_enabled or settings.live_confirm != LIVE_CONFIRM):
        result.update(status="BLOCKED", reason="live confirmation missing")
        append_jsonl(settings.jsonl_path, result)
        save_state(settings.state_path, state)
        if notify:
            _send(bot, "⛔ <b>v5.2 VWAP live blocked</b>\nV52_LIVE_ENABLED / V52_LIVE_CONFIRM 확인 필요")
        return result

    universe, instruments, meta = resolve_universe(client, settings)
    result["universe"] = universe
    result["universe_meta"] = meta

    if not guard_ok:
        result.update(status="HALTED", reason=state.get("halt_reason"))
        append_jsonl(settings.jsonl_path, result)
        save_state(settings.state_path, state)
        if notify:
            _send(bot, _summary_message(settings, result))
        return result

    tickers = _ticker_map(client, settings)
    per_order_ratio = _per_order_capital_ratio(settings, len(universe))

    # Manage open positions first: VWAP touch exit; TP/SL are also preset on exchange.
    for sym, pos in list(positions.items()):
        if sym not in universe or sym not in tickers:
            continue
        try:
            sig = _signal_for_symbol(client, settings, sym, tickers[sym], state)
            side = str(pos.get("_parsed_side") or "").lower()
            price = float(sig.get("price") or tickers[sym])
            vwap = float(sig.get("vwap") or 0.0)
            if vwap > 0 and ((side == "long" and price >= vwap) or (side == "short" and price <= vwap)):
                close_res = _close_position(client, settings, pos, "vwap touch exit")
                result["actions"].append({"symbol": sym, "action": "CLOSE", "reason": "vwap_touch", "result": close_res})
        except Exception as exc:
            result["actions"].append({"symbol": sym, "action": "ERROR", "reason": f"position manage: {exc}"})

    # Refresh positions after management.
    positions = _open_positions_map(client, settings)
    open_count = len(positions)

    active_symbols = 0
    for sym in universe:
        if active_symbols >= settings.max_active_symbols:
            break
        if sym not in tickers or sym not in instruments:
            continue
        price = tickers[sym]
        now = _utc_now()

        # Shock filter based on ticker change over recent loop.
        lp = state.setdefault("last_prices", {}).get(sym)
        if lp:
            try:
                last_ts = datetime.fromisoformat(lp.get("ts"))
                last_price = float(lp.get("price"))
                dt = (now - last_ts).total_seconds()
                chg = abs(_pct(price, last_price))
                if 0 < dt <= max(settings.shock_window_seconds * 2, settings.loop_seconds * 2) and chg >= settings.shock_pct:
                    state.setdefault("cooldowns", {})[sym] = (now + timedelta(minutes=settings.shock_cooldown_minutes)).isoformat()
                    try:
                        cancel_res = _cancel_symbol_orders(client, settings, sym)
                    except Exception as exc:
                        cancel_res = {"error": str(exc)}
                    result["actions"].append({"symbol": sym, "action": "CANCEL", "reason": f"shock {chg:.3f}%", "result": cancel_res})
            except Exception:
                pass
        state.setdefault("last_prices", {})[sym] = {"ts": now.isoformat(), "price": price}

        cooldown_until = state.setdefault("cooldowns", {}).get(sym)
        if cooldown_until:
            try:
                if now < datetime.fromisoformat(cooldown_until):
                    continue
            except Exception:
                pass

        if sym in positions:
            continue
        if open_count >= settings.max_open_positions:
            continue
        if int(state.get("trades_today") or 0) >= settings.max_trades_per_day:
            continue

        try:
            sig = _signal_for_symbol(client, settings, sym, price, state)
            result["signals"].append(sig)
        except Exception as exc:
            result["signals"].append({"symbol": sym, "ok": False, "reason": str(exc), "price": price})
            continue

        if not sig.get("eligible"):
            continue

        instrument = instruments[sym]
        # Place maker entry limits at both bands. Split total risk across all top-20 bands.
        orders_to_place = [
            ("LONG", "buy", "long", float(sig["lower"])),
            ("SHORT", "sell", "short", float(sig["upper"])),
        ]
        for action, side, pos_side, entry_price in orders_to_place:
            if int(state.get("trades_today") or 0) >= settings.max_trades_per_day:
                break
            if open_count >= settings.max_open_positions:
                break
            if entry_price <= 0:
                continue
            size_plan = build_size_plan(
                equity=equity,
                available=available,
                symbol_count=1,
                capital_ratio=per_order_ratio,
                leverage=settings.leverage,
                price=entry_price,
                instrument=instrument,
            )
            if not size_plan.valid or size_plan.notional_per_symbol > settings.max_order_notional_usdt:
                continue
            if action == "LONG":
                tp = entry_price * (1.0 + settings.tp_pct)
                sl = entry_price * (1.0 - settings.sl_pct)
            else:
                tp = entry_price * (1.0 - settings.tp_pct)
                sl = entry_price * (1.0 + settings.sl_pct)
            oid = f"v52-{sym.lower()}-{action.lower()}-{int(time.time())}"[-32:]
            try:
                order_res = _place_limit_order(
                    client,
                    settings=settings,
                    symbol=sym,
                    side=side,
                    pos_side=pos_side,
                    qty=size_plan.final_qty,
                    price=_price_str(entry_price, instrument),
                    client_oid=oid,
                    take_profit=_price_str(tp, instrument),
                    stop_loss=_price_str(sl, instrument),
                    tp_limit_price=_price_str(tp, instrument),
                )
                if _is_success(order_res):
                    state.setdefault("orders", {})[oid] = {"symbol": sym, "action": action, "price": entry_price, "ts": now.isoformat()}
                    state["trades_today"] = int(state.get("trades_today") or 0) + 1
                result["actions"].append({"symbol": sym, "action": "LIMIT_" + action, "side": side, "qty": size_plan.final_qty, "price": _price_str(entry_price, instrument), "tp": _price_str(tp, instrument), "sl": _price_str(sl, instrument), "result": order_res})
                time.sleep(0.12)
            except Exception as exc:
                result["actions"].append({"symbol": sym, "action": "ERROR", "reason": str(exc)})
        active_symbols += 1

    # Poll fills once more after placing/managing orders so fast fills are reported immediately.
    more_fills = _poll_new_fills(client, settings, state)
    if more_fills:
        result.setdefault("fills", []).extend(more_fills)

    result["status"] = "ACTIONS" if result.get("actions") else ("FILLS" if result.get("fills") else "OBSERVE")
    append_jsonl(settings.jsonl_path, result)
    save_state(settings.state_path, state)
    if notify:
        if result.get("fills"):
            _send(bot, _fills_message(result["fills"]))
        force = bool(result.get("actions")) or bool(result.get("fills")) or not settings.notify_actions_only
        if force and _should_notify(state, settings, force=bool(result.get("actions")) or bool(result.get("fills"))):
            state["last_notify_ts"] = _utc_now().isoformat()
            save_state(settings.state_path, state)
            _send(bot, _summary_message(settings, result))
    return result


def loop() -> None:
    settings = load_settings()
    bot = make_bot()
    _send(bot, f"🟢 <b>v5.2 Top10 VWAP Rubber Band loop 시작</b>\nlive={'NO/DRY' if settings.dry_run else 'YES'} capital={settings.total_capital_ratio*100:.1f}% lev={settings.leverage}x\nADX≤{settings.adx_max}, band {settings.band_std_mult}σ, TP {settings.tp_pct*100:.2f}%, SL {settings.sl_pct*100:.2f}%")
    while True:
        try:
            res = run_once(notify=True)
            print(json.dumps(res, ensure_ascii=False, indent=2)[:50000], flush=True)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            msg = f"⚠️ <b>v5.2 VWAP loop 오류</b>\n{type(exc).__name__}: {str(exc)[:800]}"
            print(msg, flush=True)
            try:
                bot.send(msg)
            except Exception:
                pass
        time.sleep(max(3, settings.loop_seconds))


def main() -> None:
    p = argparse.ArgumentParser(description="v5.2 Top10 VWAP Rubber Band scalp live bot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    sub.add_parser("universe")
    sub.add_parser("once")
    sub.add_parser("loop")
    sub.add_parser("state")
    sub.add_parser("cancel-all")
    args = p.parse_args()

    if args.cmd == "preflight":
        print(json.dumps(preflight(), ensure_ascii=False, indent=2))
    elif args.cmd == "universe":
        settings = load_settings()
        client = make_client()
        universe, _, meta = resolve_universe(client, settings)
        print(json.dumps({"universe": universe, "meta": meta}, ensure_ascii=False, indent=2))
    elif args.cmd == "once":
        print(json.dumps(run_once(notify=True), ensure_ascii=False, indent=2)[:60000])
    elif args.cmd == "loop":
        loop()
    elif args.cmd == "state":
        settings = load_settings()
        print(json.dumps(load_state(settings.state_path), ensure_ascii=False, indent=2))
    elif args.cmd == "cancel-all":
        settings = load_settings()
        client = make_client()
        universe, _, _ = resolve_universe(client, settings)
        out = []
        for sym in universe:
            try:
                out.append({"symbol": sym, "result": _cancel_symbol_orders(client, settings, sym)})
                time.sleep(0.22)
            except Exception as exc:
                out.append({"symbol": sym, "error": str(exc)})
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
