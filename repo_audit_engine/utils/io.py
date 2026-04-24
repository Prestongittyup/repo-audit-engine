from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(path: str | Path) -> Any:
    file_path = Path(path)
    raw = file_path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    return json.loads(raw)


def write_json(path: str | Path, payload: Any, pretty: bool = True) -> Path:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    if pretty:
        serialized = json.dumps(payload, indent=2, ensure_ascii=True)
    else:
        serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)

    file_path.write_text(serialized + "\n", encoding="utf-8")
    return file_path
