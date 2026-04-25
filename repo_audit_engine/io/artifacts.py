from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


def load_json(path: str | Path) -> Any:
    file_path = Path(path)
    raw = file_path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    return json.loads(raw)


def write_json(path: str | Path, payload: Any, pretty: bool = True) -> Path:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    if pretty:
        serialized = json.dumps(payload, indent=2, ensure_ascii=True)
    else:
        serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)

    file_path.write_text(serialized + "\n", encoding="utf-8")
    return file_path


def append_stage_event(log_path: Path, stage: str, status: str, details: Dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stage": str(stage),
        "status": str(status),
        "details": details if isinstance(details, dict) else {},
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def build_final_report(
    output_dir: Path,
    manifest_result: Dict[str, Any],
    static_result: Dict[str, Any],
    graph_result: Dict[str, Any],
    runtime_result: Dict[str, Any],
    heat_result: Dict[str, Any],
    dead_code_result: Dict[str, Any],
    diagnostics_result: Dict[str, Any],
    trust_payload: Dict[str, Any],
    system_valid: bool,
) -> Dict[str, Any]:
    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    final_report_path = out_root / "final_report.json"

    manifest_summary = manifest_result.get("summary") if isinstance(manifest_result.get("summary"), dict) else {}
    static_summary = static_result.get("summary") if isinstance(static_result.get("summary"), dict) else {}

    graph_payload = graph_result.get("graph") if isinstance(graph_result.get("graph"), dict) else {}
    graph_summary = graph_payload.get("summary") if isinstance(graph_payload.get("summary"), dict) else {}

    runtime_payload = runtime_result.get("flow_graph") if isinstance(runtime_result.get("flow_graph"), dict) else {}
    runtime_summary = runtime_payload.get("summary") if isinstance(runtime_payload.get("summary"), dict) else {}

    heat_payload = heat_result.get("heat") if isinstance(heat_result.get("heat"), dict) else {}
    heat_distribution = heat_payload.get("distribution") if isinstance(heat_payload.get("distribution"), dict) else {}

    dead_payload = dead_code_result.get("report") if isinstance(dead_code_result.get("report"), dict) else {}
    dead_summary = dead_payload.get("summary") if isinstance(dead_payload.get("summary"), dict) else {}
    dead_candidates = dead_payload.get("dead_candidates") if isinstance(dead_payload.get("dead_candidates"), list) else []

    diagnostics_payload = diagnostics_result.get("diagnostics") if isinstance(diagnostics_result.get("diagnostics"), dict) else {}

    report = {
        "stats": {
            "file_count": int(manifest_summary.get("file_count", 0) or 0),
            "python_file_count": int(manifest_summary.get("python_file_count", 0) or 0),
            "function_count": int(static_summary.get("function_count", 0) or 0),
            "class_count": int(static_summary.get("class_count", 0) or 0),
            "graph_node_count": int(graph_summary.get("node_count", 0) or 0),
            "graph_edge_count": int(graph_summary.get("edge_count", 0) or 0),
        },
        "graph_summary": graph_summary,
        "heat_distribution": heat_distribution,
        "dead_code_candidates": dead_candidates[:100],
        "runtime_execution_coverage": {
            "run_count": int(runtime_summary.get("run_count", 0) or 0),
            "call_event_count": int(runtime_summary.get("call_event_count", 0) or 0),
            "import_event_count": int(runtime_summary.get("import_event_count", 0) or 0),
            "timeout_count": int(runtime_summary.get("timeout_count", 0) or 0),
        },
        "diagnostics": diagnostics_payload,
        "trust": trust_payload,
        "system_valid": bool(system_valid),
        "summary": {
            "status": "PASSED" if bool(system_valid) else "FAILED",
            "primary_failure_mode": str(
                ((diagnostics_payload.get("summary") if isinstance(diagnostics_payload.get("summary"), dict) else {})
                 .get("primary_failure_mode", diagnostics_result.get("root_cause", "none")))
            ),
            "dead_candidate_count": int(dead_summary.get("dead_candidate_count", 0) or 0),
        },
    }

    write_json(final_report_path, report, pretty=True)

    return {
        "report_path": str(final_report_path),
        "report": report,
    }
