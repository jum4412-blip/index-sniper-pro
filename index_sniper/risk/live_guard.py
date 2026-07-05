from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
import json


@dataclass
class LiveGuardResult:
    ok: bool
    enabled: bool
    reason: str
    reasons: list[str]
    equity: float
    available: float
    peak_equity: float
    month_start_equity: float
    current_month: str
    drawdown_pct: float
    mdd_block_pct: float
    monthly_loss_pct: float
    monthly_loss_block_pct: float
    state_path: str

    def to_dict(self) -> dict:
        return asdict(self)


def _now_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _pct_loss(start: float, now: float) -> float:
    if start <= 0:
        return 0.0
    return max(0.0, (start - now) / start * 100.0)


def check_live_guard(settings, equity: float, available: float = 0.0) -> LiveGuardResult:
    enabled = bool(getattr(settings, "live_guard_enabled", False))
    state_path = Path(getattr(settings, "live_guard_state_path", "data/live_guard_v26.json"))

    mdd_block_pct = float(getattr(settings, "live_guard_mdd_block_pct", 25.0))
    monthly_loss_block_pct = float(getattr(settings, "live_guard_monthly_loss_block_pct", 15.0))

    equity = float(equity or 0.0)
    available = float(available or 0.0)

    current_month = _now_month()
    state = _load_state(state_path)

    if equity <= 0:
        return LiveGuardResult(
            ok=True,
            enabled=enabled,
            reason="NO_EQUITY",
            reasons=[],
            equity=equity,
            available=available,
            peak_equity=0.0,
            month_start_equity=0.0,
            current_month=current_month,
            drawdown_pct=0.0,
            mdd_block_pct=mdd_block_pct,
            monthly_loss_pct=0.0,
            monthly_loss_block_pct=monthly_loss_block_pct,
            state_path=str(state_path),
        )

    peak_equity = float(state.get("peak_equity") or equity)
    peak_equity = max(peak_equity, equity)

    saved_month = state.get("current_month")
    if saved_month != current_month:
        month_start_equity = equity
    else:
        month_start_equity = float(state.get("month_start_equity") or equity)

    drawdown_pct = _pct_loss(peak_equity, equity)
    monthly_loss_pct = _pct_loss(month_start_equity, equity)

    reasons = []
    if enabled and drawdown_pct >= mdd_block_pct:
        reasons.append(f"MDD_BLOCK {drawdown_pct:.2f}% >= {mdd_block_pct:.2f}%")
    if enabled and monthly_loss_pct >= monthly_loss_block_pct:
        reasons.append(f"MONTHLY_LOSS_BLOCK {monthly_loss_pct:.2f}% >= {monthly_loss_block_pct:.2f}%")

    ok = len(reasons) == 0
    reason = "OK" if ok else "; ".join(reasons)

    state.update({
        "peak_equity": peak_equity,
        "month_start_equity": month_start_equity,
        "current_month": current_month,
        "last_equity": equity,
        "last_available": available,
        "last_drawdown_pct": drawdown_pct,
        "last_monthly_loss_pct": monthly_loss_pct,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_state(state_path, state)

    return LiveGuardResult(
        ok=ok,
        enabled=enabled,
        reason=reason,
        reasons=reasons,
        equity=equity,
        available=available,
        peak_equity=peak_equity,
        month_start_equity=month_start_equity,
        current_month=current_month,
        drawdown_pct=drawdown_pct,
        mdd_block_pct=mdd_block_pct,
        monthly_loss_pct=monthly_loss_pct,
        monthly_loss_block_pct=monthly_loss_block_pct,
        state_path=str(state_path),
    )
