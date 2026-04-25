from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from repo_audit_engine.io.artifacts import load_json, write_json


_EXPECTED_ROLES = [
    "api",
    "validation",
    "orchestration",
    "domain_decision",
    "side_effect",
    "persistence",
]


def build_causal_flow_report(
    runtime_trace_path: Path,
    execution_flow_graph_path: Path,
    output_dir: Path,
    manifest_summary_path: Path | None = None,
) -> Dict[str, Any]:
    trace_rows = _load_trace_rows(runtime_trace_path)
    flow_payload = load_json(execution_flow_graph_path) if execution_flow_graph_path.exists() else {}
    manifest_summary = load_json(manifest_summary_path) if manifest_summary_path and manifest_summary_path.exists() else {}

    report = analyze_causal_flow(
        trace_rows=trace_rows,
        flow_payload=flow_payload if isinstance(flow_payload, Mapping) else {},
        manifest_summary=manifest_summary if isinstance(manifest_summary, Mapping) else {},
    )

    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    report_path = out_root / "causal_flow_report.json"
    write_json(report_path, report, pretty=True)

    return {
        "report_path": str(report_path),
        "report": report,
    }


def analyze_causal_flow(
    trace_rows: Sequence[Mapping[str, Any]],
    flow_payload: Mapping[str, Any],
    manifest_summary: Mapping[str, Any],
) -> Dict[str, Any]:
    call_rows = [
        row
        for row in trace_rows
        if isinstance(row, Mapping) and str(row.get("event", "")).strip().lower() == "call"
    ]

    run_sequences: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(call_rows):
        run_id = str(row.get("run_id", "")).strip() or "run_000"
        node_id = _event_node_id(row)
        role = _infer_role(
            node_id=node_id,
            file_path=str(row.get("file", "")).strip(),
            function_name=str(row.get("function", "")).strip(),
            module_name=str(row.get("module", "")).strip(),
        )

        run_sequences[run_id].append(
            {
                "seq": index + 1,
                "node_id": node_id,
                "role": role,
                "file": str(row.get("file", "")).strip(),
                "function": str(row.get("function", "")).strip(),
                "caller_node_id": str(row.get("caller_node_id", "")).strip(),
            }
        )

    workflow_rows: List[Dict[str, Any]] = []
    transition_counter: Counter[Tuple[str, str]] = Counter()
    role_counter: Counter[str] = Counter()
    direct_api_to_persistence_count = 0

    for run_id in sorted(run_sequences.keys()):
        events = run_sequences[run_id]
        role_sequence = _compress_role_sequence([str(item.get("role", "internal")) for item in events])
        if not role_sequence:
            continue

        for role in role_sequence:
            role_counter[role] += 1

        for source, target in zip(role_sequence, role_sequence[1:]):
            transition_counter[(source, target)] += 1
            if source == "api" and target == "persistence":
                direct_api_to_persistence_count += 1

        workflow_rows.append(
            {
                "run_id": run_id,
                "step_count": len(role_sequence),
                "role_sequence": role_sequence,
                "narrative": _workflow_narrative(role_sequence),
                "sample_nodes": _sample_nodes_for_workflow(events),
            }
        )

    flow_edges = _flow_role_edges(flow_payload)
    for edge in flow_edges:
        transition_counter[(str(edge[0]), str(edge[1]))] += int(edge[2])

    observed_roles = sorted(
        {
            role
            for role in role_counter.keys()
            if role in _EXPECTED_ROLES
        }
    )

    workflow_signatures = Counter(
        ">".join(row.get("role_sequence", [])) for row in workflow_rows if isinstance(row, Mapping)
    )
    top_workflow_templates = [
        {
            "signature": signature,
            "count": int(count),
            "roles": signature.split(">") if signature else [],
        }
        for signature, count in sorted(
            workflow_signatures.items(),
            key=lambda item: (-int(item[1]), str(item[0])),
        )[:12]
    ]

    role_coverage_ratio = _safe_divide(len(observed_roles), len(_EXPECTED_ROLES))
    workflow_count = len(workflow_rows)

    has_api = "api" in observed_roles
    has_decision = bool({"domain_decision", "orchestration", "validation"}.intersection(observed_roles))
    has_side_effect = bool({"side_effect", "persistence"}.intersection(observed_roles))

    issues: List[Dict[str, Any]] = []
    warnings: List[str] = []

    runtime_signal_present = len(call_rows) > 0
    analysis_enforced = bool(runtime_signal_present)

    if not runtime_signal_present:
        warnings.append("Runtime trace does not contain call events; causal flow checks were advisory only.")

    if runtime_signal_present and workflow_count == 0:
        issues.append(
            {
                "type": "NO_WORKFLOW_RECONSTRUCTION",
                "severity": "HIGH",
                "message": "Runtime call events were present but no causal workflow could be reconstructed.",
            }
        )

    if runtime_signal_present and not has_api:
        issues.append(
            {
                "type": "MISSING_API_BOUNDARY",
                "severity": "MEDIUM",
                "message": "No API boundary stage was detected in reconstructed workflows.",
            }
        )

    if runtime_signal_present and not has_decision:
        issues.append(
            {
                "type": "MISSING_DOMAIN_DECISION_STAGE",
                "severity": "MEDIUM",
                "message": "No validation/orchestration/domain decision stage was detected in reconstructed workflows.",
            }
        )

    if runtime_signal_present and not has_side_effect:
        issues.append(
            {
                "type": "MISSING_SIDE_EFFECT_OR_PERSISTENCE_STAGE",
                "severity": "MEDIUM",
                "message": "No side-effect or persistence stage was detected in reconstructed workflows.",
            }
        )

    if direct_api_to_persistence_count > 0:
        issues.append(
            {
                "type": "DIRECT_API_TO_PERSISTENCE_PATH",
                "severity": "LOW",
                "message": "Direct API-to-persistence transitions were detected and may bypass orchestration intent.",
            }
        )

    if runtime_signal_present and workflow_count > 0 and len(observed_roles) <= 2:
        warnings.append("Causal role diversity is low; runtime traces may be too shallow for full workflow intent reconstruction.")

    penalty = 0.0
    if runtime_signal_present:
        penalty += (1.0 - role_coverage_ratio) * 0.45
        if not has_decision:
            penalty += 0.20
        if not has_side_effect:
            penalty += 0.20
        if direct_api_to_persistence_count > 0:
            penalty += min(0.15, direct_api_to_persistence_count * 0.02)
        if workflow_count == 0:
            penalty += 0.25

    domain_score = round(max(0.0, min(1.0, 1.0 - min(1.0, penalty))), 3)

    summary = {
        "runtime_signal_present": bool(runtime_signal_present),
        "analysis_enforced": bool(analysis_enforced),
        "call_event_count": len(call_rows),
        "workflow_count": workflow_count,
        "role_coverage_ratio": round(role_coverage_ratio, 3),
        "observed_roles": observed_roles,
        "direct_api_to_persistence_count": int(direct_api_to_persistence_count),
        "entrypoint_count": len(_manifest_entrypoints(manifest_summary)),
        "domain_score": domain_score,
    }

    transition_rows = [
        {
            "from_role": source,
            "to_role": target,
            "count": int(count),
        }
        for (source, target), count in sorted(
            transition_counter.items(),
            key=lambda item: (-int(item[1]), str(item[0][0]), str(item[0][1])),
        )
    ]

    return {
        "schema_version": "1.0",
        "summary": summary,
        "workflows": sorted(
            workflow_rows,
            key=lambda item: (str(item.get("run_id", "")), str(item.get("narrative", ""))),
        )[:50],
        "workflow_templates": top_workflow_templates,
        "transitions": transition_rows[:80],
        "issues": issues,
        "warnings": sorted(set(warnings)),
    }


def _flow_role_edges(flow_payload: Mapping[str, Any]) -> List[Tuple[str, str, int]]:
    edges = flow_payload.get("edges") if isinstance(flow_payload.get("edges"), list) else []
    weighted: Counter[Tuple[str, str]] = Counter()

    for edge in edges:
        payload = edge if isinstance(edge, Mapping) else {}
        source_node = str(payload.get("source", "")).strip()
        target_node = str(payload.get("target", "")).strip()
        edge_type = str(payload.get("type", "")).strip().upper()
        if edge_type and edge_type != "RUNTIME_CALL":
            continue

        source_role = _infer_role(source_node, "", "", "")
        target_role = _infer_role(target_node, "", "", "")

        if not source_role or not target_role:
            continue
        weighted[(source_role, target_role)] += 1

    return [
        (source, target, int(count))
        for (source, target), count in sorted(
            weighted.items(),
            key=lambda item: (-int(item[1]), str(item[0][0]), str(item[0][1])),
        )
    ]


def _sample_nodes_for_workflow(events: Sequence[Mapping[str, Any]]) -> List[str]:
    values: List[str] = []
    seen: set[str] = set()
    for event in events:
        node_id = str(event.get("node_id", "")).strip()
        if not node_id:
            continue
        if node_id in seen:
            continue
        seen.add(node_id)
        values.append(node_id)
        if len(values) >= 10:
            break
    return values


def _workflow_narrative(role_sequence: Sequence[str]) -> str:
    if not role_sequence:
        return "No causal steps recorded."

    labels = {
        "api": "API boundary",
        "validation": "validation",
        "orchestration": "orchestration",
        "domain_decision": "domain decision",
        "side_effect": "side effect",
        "persistence": "persistence",
        "internal": "internal computation",
    }

    parts = [labels.get(str(role), str(role)) for role in role_sequence]
    return " -> ".join(parts)


def _compress_role_sequence(roles: Sequence[str]) -> List[str]:
    compressed: List[str] = []
    for role in roles:
        normalized = str(role or "internal").strip().lower() or "internal"
        if not compressed or compressed[-1] != normalized:
            compressed.append(normalized)
    return compressed


def _event_node_id(row: Mapping[str, Any]) -> str:
    for key in ("callee_node_id", "node"):
        value = str(row.get(key, "")).strip()
        if value:
            return value

    rel_file = str(row.get("file", "")).strip().replace("\\", "/")
    function = str(row.get("function", "")).strip()
    if rel_file:
        if function and function != "<module>":
            return f"function:{rel_file}:{function}"
        return f"file:{rel_file}"

    return ""


def _infer_role(node_id: str, file_path: str, function_name: str, module_name: str) -> str:
    text_parts = [
        str(node_id or "").strip().lower(),
        str(file_path or "").strip().lower(),
        str(function_name or "").strip().lower(),
        str(module_name or "").strip().lower(),
    ]
    haystack = " ".join(part for part in text_parts if part)

    if not haystack:
        return "internal"

    if any(token in haystack for token in ["repository", "store", "storage", "database", "sql", "cache", "idempotency"]):
        return "persistence"

    if any(token in haystack for token in ["adapter", "integration", "bridge", "publisher", "client", "queue", "event_log", "email", "calendar"]):
        return "side_effect"

    if any(token in haystack for token in ["orchestrator", "workflow", "state_machine", "pipeline", "runtime_router"]):
        return "orchestration"

    if any(token in haystack for token in ["api", "endpoint", "router", "controller", "asgi", "http", "ui_bootstrap"]):
        return "api"

    if any(token in haystack for token in ["validate", "validation", "schema", "guard", "auth", "admission", "policy"]):
        return "validation"

    if any(token in haystack for token in ["domain", "decision", "engine", "planner", "rules", "service", "intent"]):
        return "domain_decision"

    return "internal"


def _manifest_entrypoints(manifest_summary: Mapping[str, Any]) -> List[str]:
    entrypoints = manifest_summary.get("entrypoints") if isinstance(manifest_summary.get("entrypoints"), list) else []
    values = [str(item).strip() for item in entrypoints if str(item).strip()]
    return sorted(set(values))


def _load_trace_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []

    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)

    return rows


def _safe_divide(numerator: float, denominator: float) -> float:
    if float(denominator) == 0.0:
        return 0.0
    return float(numerator) / float(denominator)
