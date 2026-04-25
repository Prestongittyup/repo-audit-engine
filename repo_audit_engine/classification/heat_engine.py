from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping

from repo_audit_engine.io.artifacts import write_json
from repo_audit_engine.classification.engine_v2 import ClassificationError, EvidenceClassifier


def _synthetic_trace_rows_from_node_hits(node_hits: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    for raw_node_id, raw_count in sorted(node_hits.items(), key=lambda item: str(item[0])):
        node_id = str(raw_node_id).strip()
        if not node_id:
            continue

        try:
            count = max(0, int(raw_count))
        except (TypeError, ValueError):
            count = 0

        for _ in range(count):
            rows.append(
                {
                    "event": "call",
                    "callee_node_id": node_id,
                }
            )

    return rows


def classify_code_heat(
    graph_payload: Dict[str, Any],
    runtime_payload: Dict[str, Any],
    manifest_summary: Dict[str, Any],
    output_dir: Path,
    allow_synthetic_runtime: bool = False,
    static_only_mode: bool = False,
    enforce_runtime_signal: bool = True,
    runtime_requirements: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    heat_path = out_root / "heat_classification.json"

    runtime_doc = runtime_payload if isinstance(runtime_payload, dict) else {}
    runtime_trace_rows: List[Dict[str, Any]] | None = None
    runtime_source = "runtime_payload_trace"

    for key in ("runtime_trace_rows", "trace_rows", "trace"):
        trace_candidate = runtime_doc.get(key)
        if not isinstance(trace_candidate, list):
            continue
        runtime_trace_rows = [row for row in trace_candidate if isinstance(row, dict)]
        break

    if runtime_trace_rows is None:
        if not bool(allow_synthetic_runtime) or not bool(static_only_mode):
            raise ClassificationError(
                "Runtime trace rows are missing and synthetic runtime fallback is disabled."
            )
        node_hits = runtime_doc.get("node_hits") if isinstance(runtime_doc.get("node_hits"), dict) else {}
        runtime_trace_rows = _synthetic_trace_rows_from_node_hits(node_hits)
        runtime_source = "synthetic"

    classifier = EvidenceClassifier()
    heat_payload = classifier.classify(
        dependency_graph=graph_payload if isinstance(graph_payload, dict) else {},
        execution_flow_graph=runtime_doc,
        runtime_trace_rows=runtime_trace_rows,
        manifest=manifest_summary if isinstance(manifest_summary, dict) else {},
        runtime_source=runtime_source,
        enforce_runtime_signal=bool(enforce_runtime_signal),
        runtime_requirements=runtime_requirements,
    )

    write_json(heat_path, heat_payload, pretty=True)

    return {
        "heat_path": str(heat_path),
        "heat": heat_payload,
    }


def classify_code_heat_from_artifacts(
    graph_path: Path,
    manifest_summary_path: Path,
    output_dir: Path,
    runtime_flow_graph_path: Path | None = None,
    runtime_trace_path: Path | None = None,
    enforce_runtime_signal: bool = False,
    runtime_requirements: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    classifier = EvidenceClassifier()

    trace_path = runtime_trace_path
    if trace_path is None and runtime_flow_graph_path is not None:
        candidate = runtime_flow_graph_path.with_name("runtime_trace.jsonl")
        if candidate.exists():
            trace_path = candidate

    return classifier.classify_from_artifacts(
        dependency_graph_path=graph_path,
        output_dir=output_dir,
        execution_flow_graph_path=runtime_flow_graph_path,
        runtime_trace_path=trace_path,
        manifest_path=manifest_summary_path,
        runtime_source="runtime_trace" if trace_path else "none",
        enforce_runtime_signal=bool(enforce_runtime_signal),
        runtime_requirements=runtime_requirements,
    )
