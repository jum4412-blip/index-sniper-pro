from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _pct(part: float | None, base: float | None) -> float | None:
    if part is None or base in (None, 0):
        return None
    return (part / base) * 100.0


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _resolve_path(raw: str, default: str) -> Path:
    value = raw or default
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _trend_direction(signal: dict[str, Any]) -> str:
    fast = _to_float(signal.get("ema_fast"))
    slow = _to_float(signal.get("ema_slow"))
    if fast is None or slow is None:
        return "unknown"
    if fast > slow:
        return "bullish"
    if fast < slow:
        return "bearish"
    return "neutral"


def _context(signal: dict[str, Any]) -> tuple[str, str, str]:
    """Return (watch_side, status, reason_class)."""
    side = str(signal.get("signal") or "HOLD").upper()
    reason = str(signal.get("reason") or "").lower()
    trend_mode = str(signal.get("trend_mode") or "")
    data_quality = str(signal.get("data_quality") or "")

    if side in {"LONG", "SHORT"}:
        return side, "ACTIVE_SIGNAL", "active_signal"
    if "external_unavailable" in data_quality or "ext_data_unavailable" in trend_mode.lower() or "external data unavailable" in reason:
        return "NONE", "DATA_BLOCKED", "external_data_unavailable"
    if "upper breakout but trend filter rejected" in reason:
        return "LONG", "REJECTED_BREAKOUT", "upper_breakout_trend_rejected"
    if "lower breakout but trend filter rejected" in reason:
        return "SHORT", "REJECTED_BREAKOUT", "lower_breakout_trend_rejected"
    if "bullish trend" in reason and "waiting upper" in reason:
        return "LONG", "WAITING_TARGET", "bullish_waiting_upper"
    if "bearish trend" in reason and "waiting lower" in reason:
        return "SHORT", "WAITING_TARGET", "bearish_waiting_lower"
    if "trend neutral" in reason:
        return "NONE", "NEUTRAL", "trend_neutral"
    if "trend data insufficient" in reason:
        return "NONE", "DATA_BLOCKED", "trend_data_insufficient"
    return "NONE", "HOLD", "hold"


def _distance_fields(signal: dict[str, Any], watch_side: str) -> dict[str, Any]:
    price = _to_float(signal.get("current_price"))
    long_target = _to_float(signal.get("long_target"))
    short_target = _to_float(signal.get("short_target"))
    atr = _to_float(signal.get("atr"))

    to_long = None if price is None or long_target is None else long_target - price
    to_short = None if price is None or short_target is None else price - short_target

    active_target = None
    active_distance = None
    if watch_side == "LONG":
        active_target = long_target
        active_distance = to_long
    elif watch_side == "SHORT":
        active_target = short_target
        active_distance = to_short

    nearest_side = None
    nearest_distance = None
    candidates: list[tuple[str, float]] = []
    if to_long is not None:
        candidates.append(("LONG", abs(to_long)))
    if to_short is not None:
        candidates.append(("SHORT", abs(to_short)))
    if candidates:
        nearest_side, nearest_distance = sorted(candidates, key=lambda x: x[1])[0]

    return {
        "distance_to_long": _round(to_long),
        "distance_to_long_pct": _round(_pct(to_long, price)),
        "distance_to_short": _round(to_short),
        "distance_to_short_pct": _round(_pct(to_short, price)),
        "watch_target": _round(active_target),
        "watch_distance": _round(active_distance),
        "watch_distance_pct": _round(_pct(active_distance, price)),
        "watch_distance_atr": _round(None if active_distance is None or not atr else active_distance / atr),
        "nearest_side": nearest_side,
        "nearest_distance": _round(nearest_distance),
        "nearest_distance_pct": _round(_pct(nearest_distance, price)),
    }


def build_observation(report: dict[str, Any], *, mode_label: str) -> dict[str, Any] | None:
    if "error" in report:
        return {
            "symbol": report.get("symbol"),
            "mode": mode_label,
            "status": "ERROR",
            "error": str(report.get("error"))[:500],
        }
    signal = report.get("signal") or {}
    if not isinstance(signal, dict):
        return None
    watch_side, status, reason_class = _context(signal)
    distances = _distance_fields(signal, watch_side)
    checks = report.get("checks") or {}
    blockers = [str(k) for k, v in checks.items() if not bool(v)] if isinstance(checks, dict) else []
    atr = _to_float(signal.get("atr"))
    ema_fast = _to_float(signal.get("ema_fast"))
    ema_slow = _to_float(signal.get("ema_slow"))
    trend_gap_atr = None
    if atr and ema_fast is not None and ema_slow is not None:
        trend_gap_atr = abs(ema_fast - ema_slow) / atr
    current_price = _to_float(signal.get("current_price"))
    long_target = _to_float(signal.get("long_target"))
    short_target = _to_float(signal.get("short_target"))
    text = _human_text(
        symbol=str(report.get("symbol") or signal.get("symbol") or ""),
        status=status,
        watch_side=watch_side,
        current_price=current_price,
        long_target=long_target,
        short_target=short_target,
        watch_distance=distances.get("watch_distance"),
        watch_distance_pct=distances.get("watch_distance_pct"),
        reason=str(signal.get("reason") or ""),
    )
    return {
        "symbol": report.get("symbol") or signal.get("symbol"),
        "mode": mode_label,
        "status": status,
        "watch_side": watch_side,
        "reason_class": reason_class,
        "human": text,
        "signal": signal.get("signal"),
        "reason": signal.get("reason"),
        "trend_direction": _trend_direction(signal),
        "trend_mode": signal.get("trend_mode"),
        "data_quality": signal.get("data_quality"),
        "current_price": _round(current_price),
        "long_target": _round(long_target),
        "short_target": _round(short_target),
        "atr": _round(atr),
        "ema_fast": _round(ema_fast),
        "ema_slow": _round(ema_slow),
        "trend_gap_atr": _round(trend_gap_atr),
        **distances,
        "breakout_atr_distance": _round(_to_float(report.get("breakout_atr_distance"))),
        "survival_signal_score": _round(_to_float(report.get("survival_signal_score"))),
        "effective_size_multiplier": _round(_to_float(report.get("effective_size_multiplier"))),
        "effective_capital_ratio": _round(_to_float(report.get("effective_capital_ratio"))),
        "final_qty": (report.get("size_plan") or {}).get("final_qty") if isinstance(report.get("size_plan"), dict) else None,
        "notional_per_symbol": (report.get("size_plan") or {}).get("notional_per_symbol") if isinstance(report.get("size_plan"), dict) else None,
        "action_allowed": bool(report.get("action_allowed")),
        "blockers": blockers,
        "order_ready_if_signal": bool(report.get("order_payload_if_signal")),
        "order_executed_or_dry": report.get("order_result") is not None,
    }


def _human_text(*, symbol: str, status: str, watch_side: str, current_price: float | None, long_target: float | None, short_target: float | None, watch_distance: float | None, watch_distance_pct: float | None, reason: str) -> str:
    def fp(x: float | None) -> str:
        if x is None:
            return "-"
        return f"{x:,.2f}" if abs(x) >= 1000 else f"{x:.4f}"
    if status == "WAITING_TARGET" and watch_side in {"LONG", "SHORT"}:
        target = long_target if watch_side == "LONG" else short_target
        ko = "롱" if watch_side == "LONG" else "숏"
        return f"{symbol}: {ko} 기준 {fp(target)}까지 {fp(watch_distance)} ({fp(watch_distance_pct)}%) 남음"
    if status == "REJECTED_BREAKOUT":
        ko = "상방" if watch_side == "LONG" else "하방"
        return f"{symbol}: {ko} 돌파는 발생했지만 추세 필터로 거절됨"
    if status == "ACTIVE_SIGNAL":
        ko = "롱" if watch_side == "LONG" else "숏"
        return f"{symbol}: {ko} 신호 발생, 생존 필터 확인 중"
    if status == "DATA_BLOCKED":
        return f"{symbol}: 데이터 부족/외부 데이터 오류로 매매 금지"
    return f"{symbol}: HOLD / {reason[:120]}"


def persist_observations(settings: Any, reports: list[dict[str, Any]], *, mode_label: str, equity_guard: Any | None = None, global_open_before: int | None = None, survival_group_open_before: int | None = None) -> list[dict[str, Any]]:
    if not getattr(settings, "observation_enabled", True):
        return []
    observations = [o for o in (build_observation(r, mode_label=mode_label) for r in reports) if o is not None]
    now = datetime.now(timezone.utc).isoformat()
    snapshot = {
        "version": "1.6",
        "updated_at": now,
        "mode": mode_label,
        "dry_run": bool(getattr(settings, "dry_run", True)),
        "symbols": list(getattr(settings, "symbols", [])),
        "global_open_before": global_open_before,
        "survival_group_open_before": survival_group_open_before,
        "equity_guard": equity_guard.to_dict() if hasattr(equity_guard, "to_dict") else equity_guard,
        "observations": observations,
    }
    latest_path = _resolve_path(getattr(settings, "observation_latest_path", "data/market_observer.json"), "data/market_observer.json")
    tmp = latest_path.with_suffix(latest_path.suffix + ".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(latest_path)

    log_name = getattr(settings, "observation_jsonl", "signal_observer.jsonl") or "signal_observer.jsonl"
    log_dir = Path(getattr(settings, "log_dir", "logs") or "logs")
    if not log_dir.is_absolute():
        log_dir = ROOT / log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / log_name).open("a", encoding="utf-8") as f:
        for obs in observations:
            f.write(json.dumps({"ts": now, **obs}, ensure_ascii=False) + "\n")

    csv_name = getattr(settings, "observation_csv", "signal_distance.csv") or "signal_distance.csv"
    _append_distance_csv(log_dir / csv_name, now, observations)

    # Attach back to reports by symbol for downstream summaries.
    by_symbol = {o.get("symbol"): o for o in observations}
    for r in reports:
        sym = r.get("symbol")
        if sym in by_symbol:
            r["observation"] = by_symbol[sym]
    return observations


def _append_distance_csv(path: Path, ts: str, observations: list[dict[str, Any]]) -> None:
    columns = [
        "ts", "symbol", "mode", "status", "watch_side", "current_price", "long_target", "short_target",
        "watch_distance", "watch_distance_pct", "watch_distance_atr", "nearest_side", "nearest_distance_pct",
        "trend_direction", "trend_mode", "breakout_atr_distance", "survival_signal_score",
        "action_allowed", "blockers", "human",
    ]
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if not exists:
            writer.writeheader()
        for obs in observations:
            row = {c: obs.get(c, "") for c in columns}
            row["ts"] = ts
            row["blockers"] = ";".join(obs.get("blockers") or [])
            writer.writerow(row)


def observation_summary_line(report: dict[str, Any]) -> str:
    obs = report.get("observation") or {}
    if not isinstance(obs, dict) or not obs:
        return ""
    return str(obs.get("human") or "")
