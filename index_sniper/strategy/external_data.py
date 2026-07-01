from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import requests

from index_sniper.strategy.indicators import Candle


@dataclass(frozen=True)
class ExternalCandles:
    symbol: str
    provider_symbol: str
    provider: str
    candles: list[Candle]
    latest_ts: int
    latest_close: float
    age_hours: float
    scale_ratio: float
    scaled: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "provider_symbol": self.provider_symbol,
            "provider": self.provider,
            "candle_count": len(self.candles),
            "latest_ts": self.latest_ts,
            "latest_close": self.latest_close,
            "age_hours": self.age_hours,
            "scale_ratio": self.scale_ratio,
            "scaled": self.scaled,
        }


def parse_symbol_map(raw: str | None) -> dict[str, str]:
    """Parse 'SP500USDT:ES=F|^GSPC,NDX100USDT:NQ=F|^NDX' into a dict."""
    result: dict[str, str] = {}
    if not raw:
        return result
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        left, right = part.split(":", 1)
        left = left.strip().upper()
        right = right.strip()
        if left and right:
            result[left] = right
    return result


def provider_symbols(raw: str | None) -> list[str]:
    """Allow multiple fallback symbols with '|': 'NQ=F|^NDX'."""
    if not raw:
        return []
    return [x.strip() for x in str(raw).split("|") if x.strip()]


def _clean_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _http_get_json(url: str, *, params: dict[str, Any], timeout: int, attempts: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 IndexSniperPro/1.5",
        "Accept": "application/json,text/plain,*/*",
        "Connection": "close",
    }
    for i in range(max(1, attempts)):
        try:
            response = requests.get(url, params=params, timeout=timeout, headers=headers)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            time.sleep(min(0.75 * (i + 1), 2.0))
    raise RuntimeError(str(last_error) if last_error else "unknown HTTP error")


def fetch_yahoo_daily(provider_symbol: str, *, range_value: str, interval: str, timeout: int) -> list[Candle]:
    # Public Yahoo chart endpoint. Try query1 and query2 because either host can occasionally reset connections.
    encoded = quote(provider_symbol, safe="")
    params = {"range": range_value, "interval": interval, "includePrePost": "false", "events": "history"}
    errors: list[str] = []
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            url = f"https://{host}/v8/finance/chart/{encoded}"
            data = _http_get_json(url, params=params, timeout=timeout, attempts=3)
            chart = data.get("chart") or {}
            if chart.get("error"):
                raise RuntimeError(f"Yahoo chart error for {provider_symbol}: {chart['error']}")
            results = chart.get("result") or []
            if not results:
                raise RuntimeError(f"Yahoo chart empty for {provider_symbol}")
            result = results[0]
            timestamps = result.get("timestamp") or []
            indicators = result.get("indicators") or {}
            quotes = indicators.get("quote") or []
            if not quotes:
                raise RuntimeError(f"Yahoo quote empty for {provider_symbol}")
            q = quotes[0]
            opens = q.get("open") or []
            highs = q.get("high") or []
            lows = q.get("low") or []
            closes = q.get("close") or []
            volumes = q.get("volume") or []
            candles: list[Candle] = []
            for i, ts in enumerate(timestamps):
                o = _clean_float(opens[i] if i < len(opens) else None)
                h = _clean_float(highs[i] if i < len(highs) else None)
                l = _clean_float(lows[i] if i < len(lows) else None)
                c = _clean_float(closes[i] if i < len(closes) else None)
                if o is None or h is None or l is None or c is None:
                    continue
                v = _clean_float(volumes[i] if i < len(volumes) else None) or 0.0
                candles.append(Candle(ts=int(ts) * 1000, open=o, high=h, low=l, close=c, volume=v, turnover=0.0))
            candles.sort(key=lambda c: c.ts)
            return candles
        except Exception as exc:
            errors.append(f"{host}: {exc}")
    raise RuntimeError("; ".join(errors))


def fetch_stooq_daily(provider_symbol: str, *, timeout: int) -> list[Candle]:
    url = "https://stooq.com/q/d/l/"
    errors: list[str] = []
    # Stooq can be case-sensitive for some symbols; try both supplied and lower-case.
    candidates = []
    for s in (provider_symbol, provider_symbol.lower(), provider_symbol.upper()):
        if s not in candidates:
            candidates.append(s)
    for symbol_try in candidates:
        try:
            response = requests.get(
                url,
                params={"s": symbol_try, "i": "d"},
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 IndexSniperPro/1.5", "Connection": "close"},
            )
            response.raise_for_status()
            text = response.text.strip()
            if not text or "No data" in text[:100]:
                raise RuntimeError(f"Stooq daily empty for {symbol_try}: {text[:120]}")
            reader = csv.DictReader(io.StringIO(text))
            candles: list[Candle] = []
            for row in reader:
                try:
                    dt = datetime.strptime(row.get("Date", ""), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                o = _clean_float(row.get("Open"))
                h = _clean_float(row.get("High"))
                l = _clean_float(row.get("Low"))
                c = _clean_float(row.get("Close"))
                if o is None or h is None or l is None or c is None:
                    continue
                v = _clean_float(row.get("Volume")) or 0.0
                candles.append(Candle(ts=int(dt.timestamp()) * 1000, open=o, high=h, low=l, close=c, volume=v, turnover=0.0))
            candles.sort(key=lambda c: c.ts)
            if candles:
                return candles
            raise RuntimeError(f"Stooq parsed zero candles for {symbol_try}")
        except Exception as exc:
            errors.append(f"{symbol_try}: {exc}")
    raise RuntimeError("; ".join(errors))


def scale_candles(candles: list[Candle], ratio: float) -> list[Candle]:
    return [
        Candle(
            ts=c.ts,
            open=c.open * ratio,
            high=c.high * ratio,
            low=c.low * ratio,
            close=c.close * ratio,
            volume=c.volume,
            turnover=c.turnover,
        )
        for c in candles
    ]


def fetch_external_daily_for_symbol(
    *,
    symbol: str,
    bitget_price: float,
    provider_order: str,
    yahoo_map: dict[str, str],
    stooq_map: dict[str, str],
    yahoo_range: str,
    yahoo_interval: str,
    timeout: int,
    limit: int,
    max_staleness_hours: float,
    max_scale_deviation_pct: float,
) -> ExternalCandles:
    symbol_u = symbol.upper()
    providers = [p.strip().upper() for p in provider_order.split(",") if p.strip()]
    if not providers:
        providers = ["STOOQ", "YAHOO"]
    errors: list[str] = []

    for provider in providers:
        symbols_to_try: list[str] = []
        if provider == "YAHOO":
            symbols_to_try = provider_symbols(yahoo_map.get(symbol_u))
        elif provider == "STOOQ":
            symbols_to_try = provider_symbols(stooq_map.get(symbol_u))
        else:
            errors.append(f"{provider}: unknown external provider")
            continue
        if not symbols_to_try:
            errors.append(f"{provider}: no mapping for {symbol_u}")
            continue

        for provider_symbol in symbols_to_try:
            try:
                if provider == "YAHOO":
                    candles = fetch_yahoo_daily(provider_symbol, range_value=yahoo_range, interval=yahoo_interval, timeout=timeout)
                else:
                    candles = fetch_stooq_daily(provider_symbol, timeout=timeout)
                if limit > 0:
                    candles = candles[-limit:]
                if len(candles) < 60:
                    raise RuntimeError(f"not enough external candles: {len(candles)}")
                latest = candles[-1]
                age_hours = (time.time() - (latest.ts / 1000.0)) / 3600.0
                if max_staleness_hours > 0 and age_hours > max_staleness_hours:
                    raise RuntimeError(f"external candle stale: age {age_hours:.1f}h > {max_staleness_hours:.1f}h")
                if latest.close <= 0:
                    raise RuntimeError("external latest close <= 0")
                ratio = float(bitget_price) / float(latest.close)
                deviation_pct = abs(ratio - 1.0) * 100.0
                if max_scale_deviation_pct > 0 and deviation_pct > max_scale_deviation_pct:
                    raise RuntimeError(
                        f"external/Bitget scale deviation too large: {deviation_pct:.2f}% > {max_scale_deviation_pct:.2f}%"
                    )
                scaled = scale_candles(candles, ratio)
                return ExternalCandles(
                    symbol=symbol_u,
                    provider_symbol=provider_symbol,
                    provider=provider,
                    candles=scaled,
                    latest_ts=latest.ts,
                    latest_close=latest.close,
                    age_hours=age_hours,
                    scale_ratio=ratio,
                    scaled=True,
                )
            except Exception as exc:
                errors.append(f"{provider}:{provider_symbol}: {exc}")

    raise RuntimeError(f"external data unavailable for {symbol_u}: " + " | ".join(errors))
