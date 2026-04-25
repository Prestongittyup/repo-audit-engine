from __future__ import annotations

from pathlib import Path


def ensure_directory(path: Path) -> Path:
    target = path.resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def package_root() -> Path:
    return Path(__file__).resolve().parents[1]


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]
