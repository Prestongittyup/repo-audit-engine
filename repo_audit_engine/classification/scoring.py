from __future__ import annotations

RUNTIME_WEIGHT = 0.6
REACHABILITY_WEIGHT = 0.3
IMPORT_WEIGHT = 0.1


def compute_heat_score(runtime_component: float, reachable_component: float, import_component: float) -> float:
    score = (
        (float(runtime_component) * RUNTIME_WEIGHT)
        + (float(reachable_component) * REACHABILITY_WEIGHT)
        + (float(import_component) * IMPORT_WEIGHT)
    )
    return round(max(0.0, min(1.0, score)), 3)
