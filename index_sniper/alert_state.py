from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class AlertState:
    """Small JSON-backed state used to prevent repeated Telegram spam in loops."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def changed(self, key: str, signature: str) -> bool:
        """Return True only when a signature differs from the last saved value."""
        old = self.data.get(key)
        if old == signature:
            return False
        self.data[key] = signature
        self.save()
        return True

    def clear(self, key: str) -> None:
        if key in self.data:
            del self.data[key]
            self.save()
