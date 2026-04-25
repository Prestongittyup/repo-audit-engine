from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .jsonl_writer import append_jsonl_line


def stream_stage_event(log_path: Path, stage: str, status: str, details: Dict[str, Any]) -> Path:
    payload = {
        "stage": str(stage),
        "status": str(status),
        "details": details if isinstance(details, dict) else {},
    }
    return append_jsonl_line(log_path, payload)
