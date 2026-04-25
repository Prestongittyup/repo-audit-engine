from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable, List


def copy_repo_to_sandbox(repo_path: Path, sandbox_root: Path, skip_dirs: Iterable[str]) -> Path:
    target = sandbox_root / "repo"
    skip = set(skip_dirs)

    def _ignore(current_dir: str, names: List[str]) -> List[str]:
        ignored: List[str] = []
        base = Path(current_dir)

        for name in names:
            if name in skip:
                ignored.append(name)
                continue

            child = base / name
            try:
                if child.is_symlink():
                    ignored.append(name)
                    continue
                is_junction = getattr(child, "is_junction", None)
                if callable(is_junction) and bool(is_junction()):
                    ignored.append(name)
                    continue
            except OSError:
                ignored.append(name)

        return ignored

    shutil.copytree(repo_path, target, ignore=_ignore)
    return target
