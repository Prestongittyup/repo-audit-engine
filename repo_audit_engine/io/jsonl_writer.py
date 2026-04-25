from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> Path:
    output_path = path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            payload = row if isinstance(row, dict) else {}
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    return output_path


def append_jsonl_line(path: Path, row: Dict[str, Any]) -> Path:
    output_path = path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = row if isinstance(row, dict) else {}
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    return output_path
