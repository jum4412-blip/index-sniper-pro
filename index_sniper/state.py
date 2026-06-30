from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class StrategyState:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = ROOT / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "daily_entries": {}, "orders": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("daily_entries", {})
                data.setdefault("orders", [])
                return data
        except Exception:
            pass
        return {"version": 1, "daily_entries": {}, "orders": []}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def entry_count(self, symbol: str, day: str | None = None) -> int:
        day = day or utc_day()
        return int(self.data.get("daily_entries", {}).get(day, {}).get(symbol, 0))

    def record_entry(self, symbol: str, order: dict[str, Any], *, day: str | None = None) -> None:
        day = day or utc_day()
        self.data.setdefault("daily_entries", {}).setdefault(day, {})
        self.data["daily_entries"][day][symbol] = int(self.data["daily_entries"][day].get(symbol, 0)) + 1
        self.data.setdefault("orders", []).append(order)
        self.save()
