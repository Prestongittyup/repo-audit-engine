from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from repo_audit_engine.classification.engine_v2 import ClassificationError, EvidenceClassifier


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")


def _classify_from_artifacts(
    tmp_path: Path,
    dependency_payload: Dict[str, Any],
    flow_payload: Dict[str, Any],
    trace_rows: List[Dict[str, Any]],
    manifest_payload: Dict[str, Any] | None = None,
    enforce_runtime_signal: bool = False,
) -> Dict[str, Any]:
    dependency_path = tmp_path / "dependency_graph.json"
    flow_path = tmp_path / "execution_flow_graph.json"
    trace_path = tmp_path / "runtime_trace.jsonl"
    manifest_path = tmp_path / "manifest.json"

    _write_json(dependency_path, dependency_payload)
    _write_json(flow_path, flow_payload)
    _write_jsonl(trace_path, trace_rows)
    _write_json(manifest_path, manifest_payload or {"entrypoints": ["src/app.py"]})

    classifier = EvidenceClassifier()
    result = classifier.classify_from_artifacts(
        dependency_graph_path=dependency_path,
        execution_flow_graph_path=flow_path,
        runtime_trace_path=trace_path,
        manifest_path=manifest_path,
        output_dir=tmp_path,
        enforce_runtime_signal=bool(enforce_runtime_signal),
    )

    assert (tmp_path / "heat_classification.json").exists()
    return result.get("heat", {})


def _base_dependency_graph() -> Dict[str, Any]:
    nodes = [
        {"id": "file:src/app.py", "kind": "file"},
        {"id": "function:src/app.py:main", "kind": "function"},
        {"id": "function:src/app.py:runtime_root", "kind": "function"},
        {"id": "function:src/app.py:config_only", "kind": "function"},
        {"id": "function:src/app.py:call_only", "kind": "function"},
        {"id": "function:src/app.py:hot_without_runtime", "kind": "function"},
    ]

    edges = [
        {"source": "file:src/app.py", "target": "function:src/app.py:config_only", "type": "CONFIG"},
        {"source": "function:src/app.py:main", "target": "function:src/app.py:call_only", "type": "CALL"},
        {
            "source": "function:src/app.py:runtime_root",
            "target": "function:src/app.py:hot_without_runtime",
            "type": "CALL",
        },
        {"source": "file:src/app.py", "target": "function:src/app.py:hot_without_runtime", "type": "IMPORT"},
    ]

    return {"nodes": nodes, "edges": edges}


def _base_flow_graph() -> Dict[str, Any]:
    return {
        "bubble_mode": True,
        "entrypoint_runs": [],
        "nodes": [
            {"id": "function:src/app.py:runtime_root"},
            {"id": "function:src/app.py:hot_without_runtime"},
        ],
        "edges": [
            {
                "source": "function:src/app.py:runtime_root",
                "target": "function:src/app.py:hot_without_runtime",
                "type": "RUNTIME_CALL",
            }
        ],
        "summary": {"run_count": 1, "call_event_count": 1, "import_event_count": 0, "timeout_count": 0},
        "node_hits": {"function:src/app.py:runtime_root": 1},
    }


def _node_map(heat_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    rows = heat_payload.get("nodes") if isinstance(heat_payload.get("nodes"), list) else []
    result: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        payload = row if isinstance(row, dict) else {}
        node_id = str(payload.get("node_id", "")).strip()
        if node_id:
            result[node_id] = payload
    return result


def _canonicalize_test_node_id(node_id: str) -> str:
    if node_id.startswith("canonical://"):
        return node_id
    if ":" not in node_id:
        return node_id

    kind, payload = node_id.split(":", 1)
    if kind == "file":
        return f"canonical://file/{payload}"
    if kind in {"function", "class"} and ":" in payload:
        rel_path, _, name = payload.rpartition(":")
        return f"canonical://{kind}/{rel_path}/{name}"
    return node_id


def _lookup_node(heat_payload: Dict[str, Any], node_id: str) -> Dict[str, Any]:
    node_rows = _node_map(heat_payload)
    direct = node_rows.get(node_id)
    if isinstance(direct, dict):
        return direct

    canonical = _canonicalize_test_node_id(node_id)
    resolved = node_rows.get(canonical)
    if isinstance(resolved, dict):
        return resolved

    raise KeyError(node_id)


def test_node_with_only_config_reference_is_cold_not_dead(tmp_path: Path) -> None:
    heat = _classify_from_artifacts(
        tmp_path=tmp_path,
        dependency_payload=_base_dependency_graph(),
        flow_payload=_base_flow_graph(),
        trace_rows=[
            {
                "event": "call",
                "callee_node_id": "function:src/app.py:runtime_root",
                "caller_node_id": "",
            }
        ],
    )

    config_node = _lookup_node(heat, "function:src/app.py:config_only")
    assert config_node.get("classification") == "COLD"
    evidence = config_node.get("evidence", {})
    assert int(evidence.get("non_executable_references", 0) or 0) > 0


def test_node_with_call_reference_is_cold_under_weighted_model(tmp_path: Path) -> None:
    heat = _classify_from_artifacts(
        tmp_path=tmp_path,
        dependency_payload=_base_dependency_graph(),
        flow_payload=_base_flow_graph(),
        trace_rows=[
            {
                "event": "call",
                "callee_node_id": "function:src/app.py:runtime_root",
                "caller_node_id": "",
            }
        ],
    )

    call_node = _lookup_node(heat, "function:src/app.py:call_only")
    assert call_node.get("classification") == "COLD"
    evidence = call_node.get("evidence", {})
    assert int(evidence.get("executable_references", 0) or 0) > 0


def test_reachable_node_without_runtime_and_with_references_is_warm(tmp_path: Path) -> None:
    heat = _classify_from_artifacts(
        tmp_path=tmp_path,
        dependency_payload=_base_dependency_graph(),
        flow_payload=_base_flow_graph(),
        trace_rows=[
            {
                "event": "call",
                "callee_node_id": "function:src/app.py:runtime_root",
                "caller_node_id": "",
            }
        ],
    )

    target = _lookup_node(heat, "function:src/app.py:hot_without_runtime")
    assert target.get("classification") == "WARM"
    evidence = target.get("evidence", {})
    assert bool(evidence.get("reachable_from_runtime", False))
    assert int(evidence.get("runtime_hits", 0) or 0) == 0


def test_deterministic_output_across_three_runs(tmp_path: Path) -> None:
    dependency_payload = _base_dependency_graph()
    flow_payload = _base_flow_graph()
    trace_rows = [
        {
            "event": "call",
            "callee_node_id": "function:src/app.py:runtime_root",
            "caller_node_id": "",
        }
    ]

    run_payloads: List[Dict[str, Any]] = []
    file_texts: List[str] = []

    for _ in range(3):
        heat = _classify_from_artifacts(
            tmp_path=tmp_path,
            dependency_payload=dependency_payload,
            flow_payload=flow_payload,
            trace_rows=trace_rows,
        )
        run_payloads.append(heat)
        file_texts.append((tmp_path / "heat_classification.json").read_text(encoding="utf-8"))

    assert run_payloads[0] == run_payloads[1] == run_payloads[2]
    assert file_texts[0] == file_texts[1] == file_texts[2]


def test_no_dead_node_has_executable_references(tmp_path: Path) -> None:
    heat = _classify_from_artifacts(
        tmp_path=tmp_path,
        dependency_payload=_base_dependency_graph(),
        flow_payload=_base_flow_graph(),
        trace_rows=[
            {
                "event": "call",
                "callee_node_id": "function:src/app.py:runtime_root",
                "caller_node_id": "",
            }
        ],
    )

    rows = heat.get("nodes") if isinstance(heat.get("nodes"), list) else []
    for row in rows:
        payload = row if isinstance(row, dict) else {}
        if str(payload.get("classification", "")).upper() != "DEAD":
            continue
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        assert int(evidence.get("executable_references", 0) or 0) == 0


def test_no_dead_node_has_inbound_edges(tmp_path: Path) -> None:
    heat = _classify_from_artifacts(
        tmp_path=tmp_path,
        dependency_payload=_base_dependency_graph(),
        flow_payload=_base_flow_graph(),
        trace_rows=[
            {
                "event": "call",
                "callee_node_id": "function:src/app.py:runtime_root",
                "caller_node_id": "",
            }
        ],
    )

    rows = heat.get("nodes") if isinstance(heat.get("nodes"), list) else []
    for row in rows:
        payload = row if isinstance(row, dict) else {}
        inbound_edges = int(payload.get("inbound_edges", 0) or 0)
        if inbound_edges <= 0:
            continue
        assert str(payload.get("classification", "")).upper() != "DEAD"


def test_runtime_signal_gate_fails_when_runtime_is_weak(tmp_path: Path) -> None:
    dependency_path = tmp_path / "dependency_graph.json"
    flow_path = tmp_path / "execution_flow_graph.json"
    trace_path = tmp_path / "runtime_trace.jsonl"
    manifest_path = tmp_path / "manifest.json"

    _write_json(dependency_path, _base_dependency_graph())
    _write_json(flow_path, _base_flow_graph())
    _write_jsonl(
        trace_path,
        [
            {
                "event": "call",
                "callee_node_id": "function:src/app.py:runtime_root",
                "caller_node_id": "",
                "file": "src/app.py",
                "function": "runtime_root",
                "depth": 1,
            }
        ],
    )
    _write_json(manifest_path, {"entrypoints": ["src/app.py"]})

    classifier = EvidenceClassifier()
    with pytest.raises(ClassificationError, match="Runtime signal validation failed"):
        classifier.classify_from_artifacts(
            dependency_graph_path=dependency_path,
            execution_flow_graph_path=flow_path,
            runtime_trace_path=trace_path,
            manifest_path=manifest_path,
            output_dir=tmp_path,
            enforce_runtime_signal=True,
        )


def test_node_rows_include_confidence_and_evidence_strength(tmp_path: Path) -> None:
    heat = _classify_from_artifacts(
        tmp_path=tmp_path,
        dependency_payload=_base_dependency_graph(),
        flow_payload=_base_flow_graph(),
        trace_rows=[
            {
                "event": "call",
                "callee_node_id": "function:src/app.py:runtime_root",
                "caller_node_id": "",
                "file": "src/app.py",
                "function": "runtime_root",
                "depth": 2,
            }
        ],
    )

    rows = heat.get("nodes") if isinstance(heat.get("nodes"), list) else []
    assert rows
    for row in rows:
        payload = row if isinstance(row, dict) else {}
        confidence = payload.get("confidence")
        evidence_strength = payload.get("evidence_strength")
        assert isinstance(confidence, (int, float))
        assert 0.0 <= float(confidence) <= 1.0
        assert isinstance(evidence_strength, dict)
        assert set(evidence_strength.keys()) == {"runtime", "graph", "static"}
