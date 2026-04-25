from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Sequence


def scan_repository_files(
    repo_path: Path,
    skip_dirs: Sequence[str] = (),
    skip_suffixes: Sequence[str] = (),
) -> Iterable[Path]:
    root = repo_path.resolve()
    skip_dir_set = set(skip_dirs)
    skip_suffix_set = {item.lower() for item in skip_suffixes}

    for cwd, dirs, files in os.walk(root, topdown=True):
        dirs[:] = [name for name in sorted(dirs) if name not in skip_dir_set]

        for name in sorted(files):
            file_path = Path(cwd) / name
            if file_path.suffix.lower() in skip_suffix_set:
                continue
            yield file_path
