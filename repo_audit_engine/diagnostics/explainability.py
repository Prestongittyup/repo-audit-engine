from __future__ import annotations

from typing import Any, Dict, Iterable, List


def actionable_recommendations(issues: Iterable[Dict[str, Any]]) -> List[str]:
    actions: List[str] = []
    for issue in issues:
        payload = issue if isinstance(issue, dict) else {}
        message = str(payload.get("message", "")).strip()
        if not message:
            continue
        actions.append(f"Review and remediate: {message}")

    deduped = sorted(set(actions))
    return deduped
