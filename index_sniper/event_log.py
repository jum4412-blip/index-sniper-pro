from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _resolve_dir(log_dir: str) -> Path:
    path = Path(log_dir)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_jsonl(log_dir: str, name: str, event: dict[str, Any]) -> None:
    path = _resolve_dir(log_dir) / name
    row = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_trade_csv(log_dir: str, event: dict[str, Any]) -> None:
    path = _resolve_dir(log_dir) / "trades.csv"
    columns = ["ts", "mode", "symbol", "signal", "side", "pos_side", "qty", "price", "stop_loss", "take_profit", "dry_run", "client_oid", "result_code", "result_msg"]
    exists = path.exists()
    row = {c: event.get(c, "") for c in columns}
    row["ts"] = row.get("ts") or datetime.now(timezone.utc).isoformat()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        if not exists:
            writer.writeheader()
        writer.writerow(row)
