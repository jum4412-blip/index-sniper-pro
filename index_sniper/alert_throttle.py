from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _load_state(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        tmp.replace(path)
    except Exception:
        # Alert throttling must never break trading logic.
        return


def should_emit(key: str, cooldown_seconds: int | float, state_path: str | Path) -> bool:
    """Return True when an alert key may be emitted.

    This is intentionally tiny and file-backed so it works across loop restarts/screen sessions.
    If the state file cannot be read or written, fail open and allow the alert.
    """
    if not key:
        return True
    try:
        cooldown = float(cooldown_seconds or 0)
    except Exception:
        cooldown = 0.0
    if cooldown <= 0:
        return True

    path = Path(state_path)
    now = time.time()
    state = _load_state(path)
    last = 0.0
    try:
        last = float(state.get(key, 0.0) or 0.0)
    except Exception:
        last = 0.0
    if now - last < cooldown:
        return False
    state[key] = now
    _save_state(path, state)
    return True
