from __future__ import annotations

from pathlib import Path

from repo_audit_engine.classification.heat_engine import classify_code_heat
from repo_audit_engine.classification.scoring import compute_heat_score


def test_compute_heat_score_bounds() -> None:
    assert compute_heat_score(1.0, 1.0, 1.0) == 1.0
    assert compute_heat_score(0.0, 0.0, 0.0) == 0.0


def test_heat_classification_smoke(tmp_path: Path) -> None:
    graph_payload = {
        "nodes": [
            {"id": "file:main.py", "kind": "file"},
            {"id": "function:main.py:main", "kind": "function"},
        ],
        "edges": [
            {"source": "file:main.py", "target": "function:main.py:main", "type": "CALL"},
        ],
    }
    runtime_payload = {
        "node_hits": {"function:main.py:main": 3},
    }
    manifest_summary = {"entrypoints": ["main.py"]}

    result = classify_code_heat(
        graph_payload=graph_payload,
        runtime_payload=runtime_payload,
        manifest_summary=manifest_summary,
        output_dir=tmp_path,
    )

    distribution = result.get("heat", {}).get("distribution", {})
    assert (tmp_path / "heat_classification.json").exists()
    assert int(distribution.get("HOT", 0)) >= 1
