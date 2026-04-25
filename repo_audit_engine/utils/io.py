from __future__ import annotations

from pathlib import Path
from typing import Any

from repo_audit_engine.io.artifacts import load_json as _load_json
from repo_audit_engine.io.artifacts import write_json as _write_json


def load_json(path: str | Path) -> Any:
    return _load_json(path)


def write_json(path: str | Path, payload: Any, pretty: bool = True) -> Path:
    return _write_json(path, payload, pretty=pretty)
