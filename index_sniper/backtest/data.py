from __future__ import annotations

import csv
import json
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from typing import Iterable

import requests

from index_sniper.strategy.indicators import Candle

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DataSourceResult:
    symbol: str
    provider: str
    provider_symbol: str
    candles: list[Candle]
    path: Path | None = None


DEFAULT_MAPS = {
    "BTCUSDT": {
        "yahoo": ["BTC-USD"],
        "stooq": [],
    },
    "SP500USDT": {
        "yahoo": ["ES=F", "^GSPC"],
        "stooq": ["^spx"],
    },
    "NDX100USDT": {
        "yahoo": ["NQ=F", "^NDX"],
        "stooq": ["^ndx"],
    },
}


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _to_float(x) -> float | None:
    if x in (None, "", "null"):
        return None
    try:
        return float(x)
    except Exception:
        return None


def _daily_timestamp(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def candles_to_csv(path: Path, candles: Iterable[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            d = datetime.fromtimestamp(c.ts / 1000, tz=timezone.utc).date().isoformat()
            writer.writerow([d, c.ts, c.open, c.high, c.low, c.close, c.volume])


def candles_from_csv(path: Path) -> list[Candle]:
    candles: list[Candle] = []
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append(
                Candle(
                    ts=int(float(row.get("ts") or _daily_timestamp(datetime.fromisoformat(row["date"]).date()))),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume") or 0),
                    turnover=0.0,
                )
            )
    candles.sort(key=lambda c: c.ts)
    return candles


def fetch_yahoo_daily(provider_symbol: str, years: int, timeout: int = 20) -> list[Candle]:
    end = int(time.time())
    start = int((datetime.now(timezone.utc) - timedelta(days=int(years * 366 + 120))).timestamp())
    encoded = urllib.parse.quote(provider_symbol, safe="")
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{encoded}",
    ]
    last_error: Exception | None = None
    params = {
        "period1": str(start),
        "period2": str(end),
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    headers = {"User-Agent": "Mozilla/5.0 IndexSniperBacktest/2.1"}
    for url in urls:
        for attempt in range(3):
            try:
                r = requests.get(url, params=params, headers=headers, timeout=timeout)
                r.raise_for_status()
                data = r.json()
                result = (data.get("chart", {}).get("result") or [None])[0]
                if not result:
                    raise RuntimeError(f"empty yahoo result for {provider_symbol}: {data.get('chart', {}).get('error')}")
                timestamps = result.get("timestamp") or []
                quote = (result.get("indicators", {}).get("quote") or [None])[0] or {}
                opens = quote.get("open") or []
                highs = quote.get("high") or []
                lows = quote.get("low") or []
                closes = quote.get("close") or []
                volumes = quote.get("volume") or []
                out: list[Candle] = []
                for i, ts_s in enumerate(timestamps):
                    o = _to_float(opens[i] if i < len(opens) else None)
                    h = _to_float(highs[i] if i < len(highs) else None)
                    l = _to_float(lows[i] if i < len(lows) else None)
                    c = _to_float(closes[i] if i < len(closes) else None)
                    if o is None or h is None or l is None or c is None:
                        continue
                    v = _to_float(volumes[i] if i < len(volumes) else 0) or 0.0
                    d = datetime.fromtimestamp(int(ts_s), tz=timezone.utc).date()
                    out.append(Candle(ts=_daily_timestamp(d), open=o, high=h, low=l, close=c, volume=v, turnover=0.0))
                out.sort(key=lambda c: c.ts)
                if len(out) < 80:
                    raise RuntimeError(f"not enough yahoo candles for {provider_symbol}: {len(out)}")
                return out
            except Exception as exc:  # pragma: no cover - network environment dependent
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Yahoo fetch failed for {provider_symbol}: {last_error}")


def fetch_stooq_daily(provider_symbol: str, years: int, timeout: int = 20) -> list[Candle]:
    # Stooq CSV usually accepts symbols like ^spx or ^ndx.
    s = urllib.parse.quote(provider_symbol.lower(), safe="^")
    url = f"https://stooq.com/q/d/l/?s={s}&i=d"
    headers = {"User-Agent": "Mozilla/5.0 IndexSniperBacktest/2.1"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    text = r.text.strip()
    if not text or "No data" in text or text.lower().startswith("html"):
        raise RuntimeError(f"empty stooq response for {provider_symbol}")
    reader = csv.DictReader(text.splitlines())
    out: list[Candle] = []
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=int(years * 366 + 120))
    for row in reader:
        try:
            d = datetime.fromisoformat(row["Date"]).date()
            if d < cutoff:
                continue
            o, h, l, c = float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"])
            v = _to_float(row.get("Volume")) or 0.0
            out.append(Candle(ts=_daily_timestamp(d), open=o, high=h, low=l, close=c, volume=v, turnover=0.0))
        except Exception:
            continue
    out.sort(key=lambda c: c.ts)
    if len(out) < 80:
        raise RuntimeError(f"not enough stooq candles for {provider_symbol}: {len(out)}")
    return out


def load_or_fetch_symbol(symbol: str, years: int, data_dir: Path, refresh: bool = False) -> DataSourceResult:
    symbol = symbol.upper()
    path = data_dir / f"{symbol}_{years}y.csv"
    meta_path = data_dir / f"{symbol}_{years}y.meta.json"
    if path.exists() and not refresh:
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return DataSourceResult(symbol, meta.get("provider", "CSV"), meta.get("provider_symbol", str(path)), candles_from_csv(path), path)

    maps = DEFAULT_MAPS.get(symbol)
    if not maps:
        raise RuntimeError(f"no data-source mapping for {symbol}")
    errors: list[str] = []
    for provider_symbol in maps.get("yahoo", []):
        try:
            candles = fetch_yahoo_daily(provider_symbol, years)
            candles_to_csv(path, candles)
            meta_path.write_text(json.dumps({"provider": "YAHOO", "provider_symbol": provider_symbol}, ensure_ascii=False, indent=2), encoding="utf-8")
            return DataSourceResult(symbol, "YAHOO", provider_symbol, candles, path)
        except Exception as exc:
            errors.append(f"YAHOO:{provider_symbol}:{exc}")
    for provider_symbol in maps.get("stooq", []):
        try:
            candles = fetch_stooq_daily(provider_symbol, years)
            candles_to_csv(path, candles)
            meta_path.write_text(json.dumps({"provider": "STOOQ", "provider_symbol": provider_symbol}, ensure_ascii=False, indent=2), encoding="utf-8")
            return DataSourceResult(symbol, "STOOQ", provider_symbol, candles, path)
        except Exception as exc:
            errors.append(f"STOOQ:{provider_symbol}:{exc}")
    raise RuntimeError(f"failed to fetch {symbol}: {' | '.join(errors)}")
