from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class StageExecutionRecord:
    stage: str
    status: str
    details: Dict[str, Any] = field(default_factory=dict)
