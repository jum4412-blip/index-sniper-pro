from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from index_sniper.config import Settings, ROOT
from index_sniper.state import utc_day


@dataclass(frozen=True)
class EquityGuardResult:
    enabled: bool
    day: str
    baseline_equity: float
    current_equity: float
    current_available: float
    max_daily_loss_pct: float
    max_daily_loss_usdt: float
    loss_usdt: float
    loss_pct: float
    ok: bool
    reason: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _write(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def reset_equity_guard(settings: Settings) -> Path:
    path = _resolve(settings.risk_state_path)
    if path.exists():
        path.unlink()
    return path


def check_daily_equity_guard(settings: Settings, *, equity: float, available: float) -> EquityGuardResult:
    day = utc_day()
    path = _resolve(settings.risk_state_path)
    enabled = bool(settings.daily_loss_guard_enabled)
    current_equity = float(equity)
    current_available = float(available)
    max_loss_pct = abs(float(settings.max_daily_loss_pct))
    max_loss_usdt = max(0.0, float(settings.max_daily_loss_usdt))

    if not enabled:
        return EquityGuardResult(False, day, current_equity, current_equity, current_available, max_loss_pct, max_loss_usdt, 0.0, 0.0, True, "disabled", str(path))

    state = _read(path)
    if state.get("day") != day or float(state.get("baseline_equity") or 0) <= 0:
        state = {
            "version": 1,
            "day": day,
            "baseline_equity": current_equity,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "halted": False,
        }

    baseline = float(state.get("baseline_equity") or current_equity or 0)
    if baseline <= 0:
        result = EquityGuardResult(True, day, baseline, current_equity, current_available, max_loss_pct, max_loss_usdt, 0.0, 0.0, False, "baseline <= 0", str(path))
        return result

    loss_usdt = max(0.0, baseline - current_equity)
    loss_pct = loss_usdt / baseline * 100.0
    ok_pct = True if max_loss_pct <= 0 else loss_pct < max_loss_pct
    ok_usdt = True if max_loss_usdt <= 0 else loss_usdt < max_loss_usdt
    ok = ok_pct and ok_usdt and not bool(state.get("halted") and state.get("day") == day)
    reason = "ok" if ok else f"daily loss guard block: loss {loss_usdt:.4f} USDT / {loss_pct:.3f}%"

    if not ok:
        state["halted"] = True
        state.setdefault("halted_at", datetime.now(timezone.utc).isoformat())
    state.update({
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "current_equity": current_equity,
        "current_available": current_available,
        "loss_usdt": loss_usdt,
        "loss_pct": loss_pct,
        "ok": ok,
        "reason": reason,
        "max_daily_loss_pct": max_loss_pct,
        "max_daily_loss_usdt": max_loss_usdt,
    })
    _write(path, state)
    return EquityGuardResult(True, day, baseline, current_equity, current_available, max_loss_pct, max_loss_usdt, loss_usdt, loss_pct, ok, reason, str(path))
