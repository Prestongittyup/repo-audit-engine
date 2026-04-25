from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable

from repo_audit_engine.io.artifacts import write_json


def write_manifest_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> Path:
    output_path = path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            payload = row if isinstance(row, dict) else {}
            handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
    return output_path


def write_manifest_summary(path: Path, summary: Dict[str, Any]) -> Path:
    return write_json(path, summary, pretty=True)
