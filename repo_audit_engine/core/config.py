from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class PipelineConfig:
    repo_path: str
    output_path: str
    mode: str = "full-pipeline"
    bubble_mode: bool = True
    entrypoints: Sequence[str] = field(default_factory=tuple)
    timeout_seconds: int = 30
    memory_cap_mb: int = 256
    max_events: int = 20000
    max_depth: int = 120
