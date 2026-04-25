from __future__ import annotations

from typing import List


FULL_STAGE_ORDER = [
    "manifest",
    "static",
    "graph",
    "bubble",
    "classification",
    "verification",
    "diagnostics",
    "report",
]


def mode_to_stages(mode: str) -> List[str]:
    normalized = str(mode or "").strip().lower()
    if normalized == "manifest-only":
        return ["manifest"]
    if normalized in {"static-only", "static-analysis"}:
        return ["manifest", "static", "graph"]
    if normalized == "bubble-run":
        return ["manifest", "static", "graph", "bubble"]
    return list(FULL_STAGE_ORDER)
