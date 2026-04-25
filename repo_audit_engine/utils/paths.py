from __future__ import annotations

from pathlib import Path

from .filesystem import package_root as _package_root
from .filesystem import project_root as _project_root


def package_root() -> Path:
    return _package_root()


def project_root() -> Path:
    return _project_root()
