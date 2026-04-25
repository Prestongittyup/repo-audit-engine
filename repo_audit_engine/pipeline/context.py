from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class PipelineContext:
    repo_path: Path
    output_dir: Path
    mode: str
    bubble_mode: bool
    entrypoints: List[str] = field(default_factory=list)
    stage_outputs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
