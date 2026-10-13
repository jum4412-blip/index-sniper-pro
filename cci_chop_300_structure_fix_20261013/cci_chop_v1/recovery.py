"""Operator-requested flat-account migration; all exchange access is read-only.

No record is erased to make a safety check pass. Unique exchange history settles
owned closed trades; a confirmed flat account is required to clear this specific
known halt. New entries stay paused until a separate explicit arm command.
"""
from __future__ import annotations

import copy
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config
from ._compat import account
from ._compat.core import SafetyError, atomic, now_ms
from ._compat.store import Store
from .api import CCAPI
from .engine import CCEngine, NAMESPACE

RECOVERABLE_HALTS = {None, "NATIVE_CHILD_OWNERSHIP_MISMATCH",
                     "CLOSED_POSITION_ACCOUNTING_UNRESOLVED"}


def check_flat(root, api=None):
    api = api or CCAPI(root=root, write=False)
    if getattr(api, "write", False):
        raise SafetyError("RECOVERY_REQUIRES_READ_ONLY_EXCHANGE_API")
    snap = api.inventory()
    account.require_flat(snap)
    return snap


def recover_flat(root, api=None, clock=now_ms):
    # This command shares the same process lock as the live runner. An active
    # service cannot race a migration, even if its last local tick is old.
    from .cli import single_runner
    root = Path(root).resolve()
    directory = root / "data" / "cci_chop"
    path = directory / "live.sqlite"
    if not path.is_file():
        raise SafetyError("RECOVERY_EXISTING_LIVE_STATE_REQUIRED")
    api = api or CCAPI(root=root, write=False)
    if getattr(api, "write", False):
        raise SafetyError("RECOVERY_REQUIRES_READ_ONLY_EXCHANGE_API")
    with single_runner(root):
        store = Store(path)
        try:
            state = store.get(NAMESPACE)
            if not isinstance(state, dict):
                raise SafetyError("RECOVERY_EXISTING_ENGINE_STATE_REQUIRED")
            if state.get("pending") or state.get("modifications"):
                raise SafetyError("RECOVERY_LOCAL_ORDER_OR_MODIFICATION_UNRESOLVED")
            if state.get("halt") not in RECOVERABLE_HALTS:
                raise SafetyError("RECOVERY_UNRECOGNIZED_HALT: " + str(state.get("halt")))
            first = check_flat(root, api)
            uid = account.identity(api, store)
            bound_uid = (store.get("cci_chop_runtime", {}).get("binding") or {}).get("account_uid")
            if bound_uid is not None and str(bound_uid) != uid:
                raise SafetyError("RECOVERY_RUNTIME_ACCOUNT_CHANGED")
            old_config = config.load(root)
            next_config = config.validate({**old_config, "sizing_policy": "fixed_margin",
                                           "target_margin_usdt": 300.0,
                                           "margin_cap_usdt": 300.0, "leverage": 5})
            stamp = datetime.fromtimestamp(clock()/1000, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_dir = directory / "backups" / (stamp + "_" + uuid.uuid4().hex[:8])
            backup_dir.mkdir(parents=True, mode=0o700)
            with sqlite3.connect(backup_dir / "live.sqlite") as backup:
                store.db.backup(backup)
            os.chmod(backup_dir / "live.sqlite", 0o600)
            atomic(backup_dir / "config_before.json", old_config)
            atomic(directory / "PAUSE", {"at": clock(), "reason": "FLAT_MIGRATION"})
            (directory / "ARM.json").unlink(missing_ok=True)
            engine = CCEngine(api, store, {**old_config, "mode": "live"},
                              authorized=lambda: False, clock=clock)
            settled = []
            for p in list(engine.state["positions"].values()):
                if any(row.get("symbol") == p["symbol"] for row in first["positions"]):
                    raise SafetyError("RECOVERY_POSITION_REAPPEARED")
                p["position_verification"] = {"at": clock(), "status": "EXCHANGE_FLAT_ACCOUNTING_PENDING"}
                engine._finish(p, clock())
                if p["symbol"] in engine.state["positions"]:
                    raise SafetyError("RECOVERY_CLOSED_ACCOUNTING_UNRESOLVED:" + p["symbol"])
                trade = store.db.execute("SELECT json FROM trades WHERE id=?", (p["id"],)).fetchone()
                if not trade:
                    raise SafetyError("RECOVERY_DURABLE_TRADE_MISSING")
                import json
                record = json.loads(trade[0])
                settled.append({"symbol": record["symbol"], "closed": record["closed"],
                                "net_usdt": record["net_usdt"],
                                "funding_usdt": record.get("funding_usdt")})
            second = check_flat(root, api)
            if any(engine.state.get(k) for k in ("positions", "pending", "modifications")):
                raise SafetyError("RECOVERY_LOCAL_STATE_STILL_UNRESOLVED")
            prior_halt = engine.state.get("halt")
            engine._risk_blocks(second, clock())
            engine.state["halt"] = None
            engine.state["latest_management_error"] = None
            engine.state["exchange_positions"] = {"checked_at": clock(), "positions": [],
                                                   "source": "BITGET_POSITIONS_READ"}
            engine.state["last_tick"] = clock()
            engine.save()
            runtime = store.get("cci_chop_runtime", {})
            store.set("cci_chop_runtime", {**runtime, "running": False,
                                          "migration_requires_arm": True})
            # Config/source rebinding is performed by arm only after it repeats
            # account-wide flat checks. Recovery never grants entry authority.
            atomic(root / "cci_chop_config.json", next_config)
            result = {"account_uid": uid, "settled": settled,
                      "exchange_positions": 0, "exchange_open_orders": 0,
                      "exchange_strategies": 0, "previous_halt": prior_halt,
                      "sizing_policy": "fixed_margin", "target_margin_usdt": 300,
                      "leverage": 5, "entry_enabled": False,
                      "risk_records_preserved": True, "backup_directory": str(backup_dir),
                      "real_orders_sent": 0}
            store.event("CC_FLAT_RECOVERY_VERIFIED", copy.deepcopy(result))
            return result
        finally:
            store.close()
