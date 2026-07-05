from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable

from index_sniper.exchange.bitget_uta import BitgetUTAClient
from index_sniper.strategy.indicators import Candle, parse_candles


def _utc_day_start_ms(ts_ms: int) -> int:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    day = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    return int(day.timestamp() * 1000)


def aggregate_to_utc_daily(candles: Iterable[Candle]) -> list[Candle]:
    """Aggregate intraday candles into UTC 00:00 daily candles.

    For crypto, UTC 00:00 is Korea time 09:00. This avoids using exchange-provided
    daily candles that may be aligned to a different session boundary.
    """
    rows = sorted(list(candles), key=lambda c: c.ts)
    buckets: dict[int, list[Candle]] = {}
    for c in rows:
        buckets.setdefault(_utc_day_start_ms(c.ts), []).append(c)

    daily: list[Candle] = []
    for day_ts in sorted(buckets):
        group = sorted(buckets[day_ts], key=lambda c: c.ts)
        if not group:
            continue
        daily.append(
            Candle(
                ts=day_ts,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
                turnover=sum(c.turnover for c in group),
            )
        )
    return daily


def fetch_utc_daily_candles(
    client: BitgetUTAClient,
    symbol: str,
    category: str,
    *,
    interval: str = "1H",
    limit: int = 500,
    candle_type: str = "market",
) -> list[Candle]:
    response = client.candles(
        symbol=symbol,
        category=category,
        interval=interval,
        limit=limit,
        candle_type=candle_type,
    )
    intraday = parse_candles(response)
    daily = aggregate_to_utc_daily(intraday)
    if len(daily) < 3:
        raise RuntimeError(f"UTC daily aggregation produced too few candles for {symbol}: {len(daily)}")
    return daily
