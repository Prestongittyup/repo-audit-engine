from __future__ import annotations

from typing import Any, Dict, List


def rank_root_causes(causes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked = [item for item in causes if isinstance(item, dict)]
    ranked.sort(
        key=lambda item: (
            -float(item.get("severity", 0.0) or 0.0),
            -float(item.get("confidence", 0.0) or 0.0),
            str(item.get("description", "")),
        )
    )
    for index, cause in enumerate(ranked, start=1):
        cause.setdefault("rank", index)
    return ranked
