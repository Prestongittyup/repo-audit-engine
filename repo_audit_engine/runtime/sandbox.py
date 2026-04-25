from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable, List


def copy_repo_to_sandbox(repo_path: Path, sandbox_root: Path, skip_dirs: Iterable[str]) -> Path:
    target = sandbox_root / "repo"
    skip = set(skip_dirs)

    def _ignore(_: str, names: List[str]) -> List[str]:
        return [name for name in names if name in skip]

    shutil.copytree(repo_path, target, ignore=_ignore)
    return target
