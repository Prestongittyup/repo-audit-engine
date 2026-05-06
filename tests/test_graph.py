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


def test_call_edges_remain_call_type_in_validation_and_resolver(tmp_path: Path) -> None:
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
            },
            {
                "path": "service.py",
                "language": "python",
                "module": "service",
                "symbols": [{"kind": "function", "name": "run", "lineno": 1}],
            },
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
                "calls": [
                    {
                        "caller": "main",
                        "callee": "run",
                        "lineno": 2,
                        "resolved_node_id": "function:service.py:run",
                    }
                ],
                "config_references": [],
            },
            {
                "file_path": "service.py",
                "functions": [{"name": "run", "qualname": "run", "lineno": 1}],
                "classes": [],
                "imports": [],
                "calls": [],
                "config_references": [],
            },
        ],
    )

    result = build_dependency_graph(manifest_path=manifest_path, static_analysis_path=static_path, output_dir=output_dir)
    graph = result.get("graph", {})

    validation_edges = graph.get("validation_graph", {}).get("edges", [])
    resolver_edges = graph.get("resolver_data", {}).get("edges", [])

    call_validation_edges = [
        edge
        for edge in validation_edges
        if isinstance(edge, dict)
        and str(edge.get("from", "")) == "canonical://function/app.py/main"
        and str(edge.get("to", "")) == "canonical://function/service.py/run"
    ]
    assert call_validation_edges
    assert all(str(edge.get("type", "")) == "CALL" for edge in call_validation_edges)

    call_resolver_edges = [
        edge
        for edge in resolver_edges
        if isinstance(edge, dict)
        and str(edge.get("from", "")) == "canonical://function/app.py/main"
        and str(edge.get("to", "")) == "canonical://function/service.py/run"
    ]
    assert call_resolver_edges
    assert all(str(edge.get("type", "")) == "CALL" for edge in call_resolver_edges)
    assert all(str(edge.get("source", "")) == "AST" for edge in call_resolver_edges)
