from __future__ import annotations

import json
from typing import Any


def short_json(data: Any, limit: int = 1200) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    return text[:limit] + ("..." if len(text) > limit else "")


def ok_mark(data: dict) -> str:
    return "✅" if str(data.get("code")) in {"00000", "0"} else "⚠️"
