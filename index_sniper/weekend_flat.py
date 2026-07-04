from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any
import time

from index_sniper.config import Settings
from index_sniper.event_log import append_jsonl
from index_sniper.exchange.bitget_uta import BitgetUTAClient, OrderIntent
from index_sniper.position import open_positions
from index_sniper.telegram.bot import TelegramBot


def _minute_of_day(hour: int, minute: int) -> int:
    return int(hour) * 60 + int(minute)


def _parse_hhmm(value: str, default: str) -> tuple[int, int]:
    raw = (value or default).strip()
    try:
        hh, mm = raw.split(":", 1)
        h = max(0, min(23, int(hh)))
        m = max(0, min(59, int(mm)))
        return h, m
    except Exception:
        hh, mm = default.split(":", 1)
        return int(hh), int(mm)


def _client_oid(prefix: str, symbol: str) -> str:
    return f"{prefix}-{symbol.lower()}-{str(int(time.time() * 1000))[-10:]}"[:32]


@dataclass(frozen=True)
class WeekendFlatWindow:
    enabled: bool
    now_utc: str
    now_ny: str
    weekday: int
    weekday_name: str
    new_entries_blocked: bool
    force_flat_due: bool
    reason: str
    block_start_et: str
    force_flat_after_et: str
    reopen_after_et: str
    symbols: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "now_utc": self.now_utc,
            "now_ny": self.now_ny,
            "weekday": self.weekday,
            "weekday_name": self.weekday_name,
            "new_entries_blocked": self.new_entries_blocked,
            "force_flat_due": self.force_flat_due,
            "reason": self.reason,
            "block_start_et": self.block_start_et,
            "force_flat_after_et": self.force_flat_after_et,
            "reopen_after_et": self.reopen_after_et,
            "symbols": self.symbols,
        }


def weekend_flat_window(settings: Settings, *, now_utc: datetime | None = None) -> WeekendFlatWindow:
    """Return NY-time weekend flat status for index symbols.

    Policy:
    - Friday >= INDEX_WEEKEND_BLOCK_NEW_AFTER_ET: block new SP500/NDX entries.
    - Friday >= INDEX_WEEKEND_FORCE_FLAT_AFTER_ET: close/keep flat index positions.
    - Saturday: block entries and flat any remaining index positions.
    - Sunday < INDEX_WEEKEND_REOPEN_AFTER_ET: block entries and flat any remaining index positions.
    - Sunday >= reopen: allow again.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(settings.index_weekend_timezone)
    now_ny = now_utc.astimezone(tz)
    weekday = now_ny.weekday()  # Mon=0 ... Sun=6
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    block_h, block_m = _parse_hhmm(settings.index_weekend_block_new_after_et, "15:30")
    flat_h, flat_m = _parse_hhmm(settings.index_weekend_force_flat_after_et, "16:30")
    reopen_h, reopen_m = _parse_hhmm(settings.index_weekend_reopen_after_et, "18:30")
    minute = _minute_of_day(now_ny.hour, now_ny.minute)
    block_min = _minute_of_day(block_h, block_m)
    flat_min = _minute_of_day(flat_h, flat_m)
    reopen_min = _minute_of_day(reopen_h, reopen_m)

    new_block = False
    force_flat = False
    reason = "regular_session"
    if not settings.index_weekend_flat_enabled:
        reason = "disabled"
    else:
        if weekday == 4:  # Friday
            if minute >= block_min:
                new_block = True
                reason = "friday_entry_block_window"
            if minute >= flat_min:
                force_flat = True
                reason = "friday_force_flat_window"
        elif weekday == 5:  # Saturday
            new_block = True
            force_flat = True
            reason = "saturday_weekend_flat"
        elif weekday == 6:  # Sunday
            if minute < reopen_min:
                new_block = True
                force_flat = True
                reason = "sunday_before_reopen"
            else:
                reason = "sunday_after_reopen"

    return WeekendFlatWindow(
        enabled=settings.index_weekend_flat_enabled,
        now_utc=now_utc.isoformat(),
        now_ny=now_ny.isoformat(),
        weekday=weekday,
        weekday_name=names[weekday],
        new_entries_blocked=new_block,
        force_flat_due=force_flat,
        reason=reason,
        block_start_et=f"{block_h:02d}:{block_m:02d}",
        force_flat_after_et=f"{flat_h:02d}:{flat_m:02d}",
        reopen_after_et=f"{reopen_h:02d}:{reopen_m:02d}",
        symbols=settings.index_weekend_flat_symbols,
    )


def is_index_weekend_symbol(settings: Settings, symbol: str) -> bool:
    return symbol.upper() in {s.upper() for s in settings.index_weekend_flat_symbols}


def index_new_entry_allowed(settings: Settings, symbol: str, window: WeekendFlatWindow) -> bool:
    if not settings.index_weekend_flat_enabled:
        return True
    if not is_index_weekend_symbol(settings, symbol):
        return True
    return not window.new_entries_blocked


def close_index_positions_if_due(
    settings: Settings,
    client: BitgetUTAClient,
    tg: TelegramBot | None,
    *,
    dry_run: bool,
    window: WeekendFlatWindow | None = None,
) -> dict[str, Any]:
    window = window or weekend_flat_window(settings)
    result: dict[str, Any] = {"window": window.to_dict(), "attempted": [], "errors": []}
    if not settings.index_weekend_flat_enabled or not window.force_flat_due:
        return result

    for symbol in settings.index_weekend_flat_symbols:
        try:
            positions = open_positions(client.current_position(symbol, settings.category), symbol=symbol)
        except Exception as exc:
            err = {"symbol": symbol, "error": str(exc)}
            result["errors"].append(err)
            append_jsonl(settings.log_dir, "events.jsonl", {"type": "weekend_flat_position_error", **err, "window": window.to_dict()})
            continue
        for pos in positions:
            side = (pos.get("_parsed_side") or "").lower()
            qty = str(pos.get("_parsed_qty") or "")
            if side not in {"long", "short"} or not qty:
                err = {"symbol": symbol, "position": pos, "error": "unable_to_parse_position_side_or_qty"}
                result["errors"].append(err)
                append_jsonl(settings.log_dir, "events.jsonl", {"type": "weekend_flat_parse_error", **err, "window": window.to_dict()})
                continue
            close_side = "sell" if side == "long" else "buy"
            intent = OrderIntent(
                symbol=symbol,
                side=close_side,
                pos_side=side,
                qty=qty,
                category=settings.category,
                margin_coin=settings.margin_coin,
                margin_mode=settings.margin_mode,
                client_oid=_client_oid("wkflat", symbol),
            )
            preview = client.place_order(intent, dry_run=True)
            order_result = preview
            should_live_close = settings.index_weekend_flat_auto_close and not dry_run
            if should_live_close:
                order_result = client.place_order(intent, dry_run=False)
            row = {
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "dry_run": dry_run or not settings.index_weekend_flat_auto_close,
                "auto_close_enabled": settings.index_weekend_flat_auto_close,
                "preview": preview,
                "order_result": order_result,
            }
            result["attempted"].append(row)
            append_jsonl(settings.log_dir, "events.jsonl", {"type": "weekend_flat_close", **row, "window": window.to_dict()})
            if tg and (should_live_close or settings.notify_blocked_signal):
                mode = "실청산" if should_live_close else "드라이런"
                tg.send(
                    "🛑 <b>INDEX WEEKEND FLAT</b>\n"
                    f"{symbol} {side.upper()} qty {qty}\n"
                    f"mode: {mode}\n"
                    f"reason: {window.reason}\n"
                    f"NY: {window.now_ny}"
                )
    return result


def weekend_flat_human(window: WeekendFlatWindow) -> str:
    if not window.enabled:
        return "INDEX_WEEKEND_FLAT disabled"
    if window.force_flat_due:
        return f"INDEX WEEKEND FLAT: force-flat window ({window.reason})"
    if window.new_entries_blocked:
        return f"INDEX WEEKEND BLOCK: new entries blocked ({window.reason})"
    return f"INDEX WEEKEND: entries allowed ({window.reason})"
