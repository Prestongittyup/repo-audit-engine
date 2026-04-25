from __future__ import annotations

import json
from pathlib import Path

from repo_audit_engine.graph.graph_builder import build_dependency_graph


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def test_build_dependency_graph_smoke(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    static_path = tmp_path / "static.jsonl"
    output_dir = tmp_path / "out"

    _write_jsonl(
        manifest_path,
        [
            {
                "path": "app.py",
                "language": "python",
                "module": "app",
                "symbols": [{"kind": "function", "name": "main", "lineno": 1}],
            }
        ],
    )

    _write_jsonl(
        static_path,
        [
            {
                "file_path": "app.py",
                "functions": [{"name": "main", "qualname": "main", "lineno": 1}],
                "classes": [],
                "imports": [],
                "calls": [],
                "config_references": [],
            }
        ],
    )

    result = build_dependency_graph(manifest_path=manifest_path, static_analysis_path=static_path, output_dir=output_dir)
    summary = result.get("graph", {}).get("summary", {})

    assert (output_dir / "dependency_graph.json").exists()
    assert int(summary.get("node_count", 0)) >= 1
