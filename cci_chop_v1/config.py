"""Sizing caps and account guards; no fixed take-profit or percentage stop."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from cci_chop_v1._compat.core import SafetyError

DEFAULT = {
    "symbols": ["BTCUSDT", "ETHUSDT"],
    "leverage": 5,
    "margin_cap_usdt": 300.0,
    "max_positions": 2,
    "risk_fraction": 0.01,
    "max_trade_risk_usdt": 12.0,
    "max_portfolio_risk_fraction": 0.02,
    "margin_reserve_usdt": 150.0,
    "daily_loss_fraction": 0.02,
    "weekly_loss_fraction": 0.05,
    "max_drawdown_fraction": 0.10,
    "entry_slippage_bps": 8.0,
    "exit_slippage_bps": 7.0,
    "max_spread_bps": 10.0,
    "max_mark_basis_bps": 50.0,
    "max_chase_bps": 15.0,
    "max_adverse_funding_rate": 0.0005,
    "protection_deadline_seconds": 10,
    "poll_seconds": 3,
    "summary_hours": 6,
    "shadow_days": 7,
    "shadow_equity_reference_usdt": 1042.19,
}


def validate(c):
    if not isinstance(c, dict) or set(c) != set(DEFAULT):
        raise SafetyError("CC_CONFIG_KEYS_INVALID")
    if c["symbols"] != DEFAULT["symbols"] or c["max_positions"] != 2:
        raise SafetyError("THIS_RELEASE_BTC_ETH_TWO_POSITIONS_ONLY")
    if type(c["leverage"]) is not int or not 1 <= c["leverage"] <= 5:
        raise SafetyError("LEVERAGE_MUST_BE_1_TO_5")
    for key in set(DEFAULT) - {"symbols"}:
        value = c[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise SafetyError("CC_CONFIG_NUMBER_INVALID:" + key)
    limits = {
        "margin_cap_usdt": 300, "risk_fraction": .01,
        "max_trade_risk_usdt": 12, "max_portfolio_risk_fraction": .02,
        "daily_loss_fraction": .02, "weekly_loss_fraction": .05,
        "max_drawdown_fraction": .10, "entry_slippage_bps": 8,
        "exit_slippage_bps": 7, "max_spread_bps": 10,
        "max_mark_basis_bps": 50, "max_chase_bps": 15,
        "max_adverse_funding_rate": .0005, "protection_deadline_seconds": 10,
    }
    if any(c[key] > ceiling for key, ceiling in limits.items()):
        raise SafetyError("CONFIG_EXCEEDS_RELEASE_RISK_CAPS")
    if c["margin_reserve_usdt"] < 150 or not 1 <= c["poll_seconds"] <= 5:
        raise SafetyError("RESERVE_OR_POLL_INVALID")
    if c["summary_hours"] < 6 or c["shadow_days"] != 7:
        raise SafetyError("SUMMARY_OR_SHADOW_DURATION_INVALID")
    return dict(c)


def load(root):
    path = Path(root) / "cci_chop_config.json"
    return validate(json.loads(path.read_text()) if path.exists() else DEFAULT)


def digest(c):
    return hashlib.sha256(json.dumps(validate(c), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def release_digest():
    """Bind authorization to actual executable sources, not a strategy label."""
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for path in sorted(list(root.rglob("*.py")) + [root/"entry_rule.json"]):
        if path.name.startswith("test_"):
            continue
        h.update(str(path.relative_to(root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()
