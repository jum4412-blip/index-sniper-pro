from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
import json

from index_sniper.alert_state import AlertState
from index_sniper.event_log import append_jsonl
from index_sniper.exchange.bitget_uta import BitgetUTAClient
from index_sniper.position import open_positions, row_avg_price, row_qty_decimal, row_side
from index_sniper.state import StrategyState
from index_sniper.telegram.bot import TelegramBot

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class ManagedPosition:
    symbol: str
    side: str
    qty: str
    avg_price: float | None
    current_price: float | None
    entry_ts: str | None
    hold_hours: float | None
    stop_loss: float | None
    take_profit: float | None
    r_multiple: float | None
    status: str
    action: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except Exception:
        return None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def _latest_order_for_symbol(state: StrategyState, symbol: str) -> dict[str, Any] | None:
    orders = state.data.get("orders") or []
    if not isinstance(orders, list):
        return None
    for order in reversed(orders):
        if not isinstance(order, dict):
            continue
        if str(order.get("symbol") or "").upper() == symbol.upper() and not bool(order.get("dry_run")):
            return order
    return None


def _r_multiple(side: str, entry: float | None, current: float | None, stop: float | None) -> float | None:
    if entry is None or current is None or stop is None:
        return None
    if side == "long":
        risk = entry - stop
        pnl = current - entry
    elif side == "short":
        risk = stop - entry
        pnl = entry - current
    else:
        return None
    if risk <= 0:
        return None
    return pnl / risk


def _max_hold_hours(settings: Any, symbol: str) -> float:
    index_symbols = {s.upper() for s in getattr(settings, "index_weekend_flat_symbols", [])}
    if symbol.upper() in index_symbols:
        return float(getattr(settings, "position_max_hold_hours_index", 48.0))
    return float(getattr(settings, "position_max_hold_hours_btc", 72.0))


def evaluate_positions(settings: Any, client: BitgetUTAClient, tg: TelegramBot | None = None) -> list[ManagedPosition]:
    enabled = bool(getattr(settings, "position_manager_enabled", True))
    if not enabled:
        return []
    state = StrategyState(getattr(settings, "strategy_state_path", "data/strategy_state.json"))
    rows: list[ManagedPosition] = []
    now = datetime.now(timezone.utc)
    warn_hours = float(getattr(settings, "position_warn_after_hours", 24.0))
    breakeven_r = float(getattr(settings, "position_breakeven_alert_r", 1.0))

    for symbol in getattr(settings, "symbols", []):
        try:
            pos_resp = client.current_position(symbol, getattr(settings, "category", "USDT-FUTURES"))
            current_price = client.last_price(symbol, getattr(settings, "category", "USDT-FUTURES"))
            for row in open_positions(pos_resp, symbol=symbol):
                side = row_side(row)
                qty = str(row_qty_decimal(row))
                avg = _to_float(str(row_avg_price(row)))
                order = _latest_order_for_symbol(state, symbol) or {}
                entry_ts = order.get("ts")
                entry_dt = _parse_ts(entry_ts)
                hold_hours = (now - entry_dt).total_seconds() / 3600.0 if entry_dt else None
                stop = _to_float(order.get("stop_loss"))
                tp = _to_float(order.get("take_profit"))
                entry_price = avg or _to_float(order.get("price"))
                r = _r_multiple(side, entry_price, current_price, stop)
                max_hours = _max_hold_hours(settings, symbol)

                status = "OK"
                action = "HOLD"
                reasons: list[str] = []
                if stop is None or tp is None:
                    status = "MISSING_TPSL"
                    action = "CHECK_NOW"
                    reasons.append("TP/SL information missing from strategy state; verify exchange app")
                if hold_hours is not None and hold_hours >= max_hours:
                    status = "TIME_EXIT_CANDIDATE"
                    action = "MANUAL_REVIEW"
                    reasons.append(f"hold_hours {hold_hours:.2f} >= max_hold_hours {max_hours:.2f}")
                elif hold_hours is not None and hold_hours >= warn_hours:
                    status = "TIME_WARNING" if status == "OK" else status
                    action = "MONITOR" if action == "HOLD" else action
                    reasons.append(f"hold_hours {hold_hours:.2f} >= warn_after_hours {warn_hours:.2f}")
                if r is not None and r >= breakeven_r:
                    status = "BREAKEVEN_STOP_CANDIDATE" if status == "OK" else status
                    action = "CONSIDER_BE_STOP" if action == "HOLD" else action
                    reasons.append(f"r_multiple {r:.3f} >= breakeven_alert_r {breakeven_r:.3f}")

                rows.append(ManagedPosition(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    avg_price=round(entry_price, 8) if entry_price is not None else None,
                    current_price=round(current_price, 8) if current_price is not None else None,
                    entry_ts=entry_ts,
                    hold_hours=round(hold_hours, 4) if hold_hours is not None else None,
                    stop_loss=stop,
                    take_profit=tp,
                    r_multiple=round(r, 6) if r is not None else None,
                    status=status,
                    action=action,
                    reason="; ".join(reasons) if reasons else "ok",
                ))
        except Exception as exc:
            append_jsonl(getattr(settings, "log_dir", "logs"), "events.jsonl", {"type": "position_manager_error", "symbol": symbol, "error": str(exc)})

    _persist(settings, rows)
    if tg is not None:
        _notify_changes(settings, tg, rows)
    return rows


def _persist(settings: Any, rows: list[ManagedPosition]) -> None:
    path = Path(getattr(settings, "position_manager_latest_path", "data/position_manager.json"))
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": "2.0", "updated_at": datetime.now(timezone.utc).isoformat(), "positions": [r.to_dict() for r in rows]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    for r in rows:
        append_jsonl(getattr(settings, "log_dir", "logs"), "position_manager.jsonl", {"type": "position_status", **r.to_dict()})


def _notify_changes(settings: Any, tg: TelegramBot, rows: list[ManagedPosition]) -> None:
    state_path = getattr(settings, "position_manager_alert_state_path", "data/position_alert_state.json")
    path = Path(state_path)
    if not path.is_absolute():
        path = ROOT / path
    alert_state = AlertState(str(path))
    for r in rows:
        if r.status == "OK":
            alert_state.clear(f"pm:{r.symbol}:{r.side}")
            continue
        sig = f"{r.status}|{r.action}|{r.reason}|{r.qty}"
        key = f"pm:{r.symbol}:{r.side}"
        if alert_state.changed(key, sig):
            tg.send(
                f"🧭 <b>Position Manager {r.status}</b>\n"
                f"{r.symbol} {r.side.upper()} qty {r.qty}\n"
                f"avg {r.avg_price} / now {r.current_price}\n"
                f"hold {r.hold_hours}h / R {r.r_multiple}\n"
                f"action: {r.action}\nreason: {r.reason}"
            )
