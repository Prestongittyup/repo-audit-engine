from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Sequence, Set, Tuple


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


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


def _round3(value: float) -> float:
    return round(float(value), 3)


def _avg(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / float(len(values))


def _score_status(score: float, pass_floor: float = 0.75, degraded_floor: float = 0.5) -> str:
    normalized = _clamp01(_to_float(score))
    if normalized >= pass_floor:
        return "PASS"
    if normalized >= degraded_floor:
        return "DEGRADED"
    return "FAIL"


def _numeric_score(score_map: Dict[str, Any], keys: Sequence[str], default: float = 0.0) -> float:
    for key in keys:
        if key not in score_map:
            continue
        value = score_map.get(key)
        if isinstance(value, (int, float)):
            return _clamp01(_to_float(value))
    return _clamp01(_to_float(default))


def _append_finding(findings: List[str], condition: bool, message: str) -> None:
    if condition and message not in findings:
        findings.append(message)


def _top_root_cause_descriptions(root_causes: Sequence[Any], limit: int = 3) -> List[str]:
    rows: List[str] = []
    for item in root_causes:
        payload = item if isinstance(item, dict) else {}
        description = str(payload.get("description", "")).strip()
        if not description:
            continue
        rows.append(description)
        if len(rows) >= max(1, int(limit)):
            break
    return rows


def _build_architect_auditor(
    graph_payload: Dict[str, Any],
    architecture_payload: Dict[str, Any],
    semantic_payload: Dict[str, Any],
    causal_payload: Dict[str, Any],
    runtime_summary: Dict[str, Any],
    execution_gate_applied: bool,
) -> Dict[str, Any]:
    taxonomy = [
        "layer_violation",
        "circular_dependency",
        "redundant_domain",
        "orphan_module",
        "overcoupled_node",
    ]

    layer_violations = _extract_layer_violations(architecture_payload)
    circular_violations = _extract_circular_dependency_violations(graph_payload)
    orphan_module_violations = _extract_orphan_module_violations(graph_payload)
    overcoupled_node_violations = _extract_overcoupled_node_violations(graph_payload)
    redundant_domain_violations = _extract_redundant_domain_violations(semantic_payload)

    structure_violations = sorted(
        layer_violations + circular_violations + orphan_module_violations,
        key=lambda item: (
            str(item.get("type", "")),
            str(item.get("rule_id", "")),
            str(item.get("path", item.get("source_path", ""))),
            str(item.get("message", "")),
        ),
    )
    responsibility_violations = sorted(
        redundant_domain_violations + overcoupled_node_violations,
        key=lambda item: (
            str(item.get("type", "")),
            str(item.get("cluster_id", item.get("concept_key", ""))),
            str(item.get("path", "")),
            str(item.get("message", "")),
        ),
    )

    run_count = _to_int(runtime_summary.get("run_count", 0))
    behavior_violations = _extract_behavior_alignment_violations(causal_payload)

    structure_status = "PASS" if not structure_violations else "FAIL"
    responsibility_status = "PASS" if not responsibility_violations else "FAIL"

    if run_count <= 0:
        behavior_status = "INSUFFICIENT_EVIDENCE"
        behavior_answer = "insufficient_evidence"
    else:
        behavior_status = "PASS" if not behavior_violations else "FAIL"
        behavior_answer = "yes" if behavior_status == "PASS" else "no"

    return {
        "contract_version": "1.0",
        "violation_taxonomy": taxonomy,
        "questions": {
            "structure_matches_intended_architecture": {
                "question": "Does the structure match an intended architecture?",
                "status": structure_status,
                "answer": "yes" if structure_status == "PASS" else "no",
                "violation_count": len(structure_violations),
                "violation_types": sorted({str(item.get("type", "")) for item in structure_violations if str(item.get("type", ""))}),
                "violations": structure_violations[:60],
                "evidence_sources": ["architecture_constraints", "dependency_graph"],
            },
            "responsibility_clean_or_duplicated": {
                "question": "Is responsibility clean or duplicated?",
                "status": responsibility_status,
                "answer": "yes" if responsibility_status == "PASS" else "no",
                "violation_count": len(responsibility_violations),
                "violation_types": sorted({str(item.get("type", "")) for item in responsibility_violations if str(item.get("type", ""))}),
                "violations": responsibility_violations[:60],
                "evidence_sources": ["semantic_clusters", "dependency_graph"],
            },
            "behavior_aligns_with_structure": {
                "question": "Does behavior align with structure?",
                "status": behavior_status,
                "answer": behavior_answer,
                "violation_count": len(behavior_violations),
                "violation_types": sorted({str(item.get("type", "")) for item in behavior_violations if str(item.get("type", ""))}),
                "violations": behavior_violations[:60],
                "evidence_sources": ["causal_flow", "runtime_execution"],
                "runtime_observation": {
                    "run_count": run_count,
                    "call_event_count": _to_int(runtime_summary.get("call_event_count", 0)),
                    "execution_gate_applied": bool(execution_gate_applied),
                },
            },
        },
        "hard_constraints": {
            "no_new_scoring_systems": True,
            "no_upstream_artifact_mutation": True,
            "no_feedback_loops": True,
            "deterministic_outputs": True,
        },
    }


def _extract_layer_violations(architecture_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    violations = architecture_payload.get("violations") if isinstance(architecture_payload.get("violations"), list) else []
    allowed_rule_ids = {
        "LAYER_DIRECTION_VIOLATION",
        "SERVICE_IMPORTS_ADAPTER",
        "DIRECT_STATE_CHANGE_BYPASSES_ORCHESTRATOR",
    }

    rows: List[Dict[str, Any]] = []
    for item in violations:
        payload = item if isinstance(item, dict) else {}
        rule_id = str(payload.get("rule_id", "")).strip()
        if rule_id and rule_id not in allowed_rule_ids:
            continue
        source_path = str(payload.get("source_path", "")).strip()
        target_path = str(payload.get("target_path", "")).strip()
        message = str(payload.get("reason", "")).strip() or "Layer dependency violated intended architecture."
        rows.append(
            {
                "type": "layer_violation",
                "rule_id": rule_id,
                "source_node_id": str(payload.get("source_node_id", "")).strip(),
                "target_node_id": str(payload.get("target_node_id", "")).strip(),
                "source_path": source_path,
                "target_path": target_path,
                "message": message,
            }
        )

    rows.sort(
        key=lambda item: (
            str(item.get("rule_id", "")),
            str(item.get("source_node_id", "")),
            str(item.get("target_node_id", "")),
            str(item.get("message", "")),
        )
    )
    return rows


def _extract_redundant_domain_violations(semantic_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    duplicate_clusters = (
        semantic_payload.get("duplicate_intent_clusters")
        if isinstance(semantic_payload.get("duplicate_intent_clusters"), list)
        else []
    )
    for item in duplicate_clusters:
        payload = item if isinstance(item, dict) else {}
        members = payload.get("members") if isinstance(payload.get("members"), list) else []
        member_paths = sorted({str(path).strip() for path in members if str(path).strip()})
        if not member_paths:
            continue
        rows.append(
            {
                "type": "redundant_domain",
                "cluster_id": str(payload.get("id", "")).strip(),
                "paths": member_paths,
                "message": (
                    f"Duplicate-intent cluster spans {len(member_paths)} module(s), which indicates overlapping responsibility."
                ),
            }
        )

    abstraction_collisions = (
        semantic_payload.get("abstraction_collisions")
        if isinstance(semantic_payload.get("abstraction_collisions"), list)
        else []
    )
    for item in abstraction_collisions:
        payload = item if isinstance(item, dict) else {}
        files = payload.get("files") if isinstance(payload.get("files"), list) else []
        abstractions = (
            payload.get("abstraction_types")
            if isinstance(payload.get("abstraction_types"), list)
            else []
        )
        file_paths = sorted({str(path).strip() for path in files if str(path).strip()})
        abstraction_types = sorted({str(value).strip() for value in abstractions if str(value).strip()})
        if not file_paths:
            continue
        rows.append(
            {
                "type": "redundant_domain",
                "concept_key": str(payload.get("concept_key", "")).strip(),
                "paths": file_paths,
                "abstraction_types": abstraction_types,
                "message": (
                    "Same concept appears under multiple abstraction types, which indicates duplicated domain responsibility."
                ),
            }
        )

    rows.sort(
        key=lambda item: (
            str(item.get("cluster_id", item.get("concept_key", ""))),
            str(item.get("message", "")),
        )
    )
    return rows


def _extract_behavior_alignment_violations(causal_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    summary = causal_payload.get("summary") if isinstance(causal_payload.get("summary"), dict) else {}
    direct_count = _to_int(summary.get("direct_api_to_persistence_count", 0))
    if direct_count > 0:
        rows.append(
            {
                "type": "layer_violation",
                "source": "causal_flow",
                "message": (
                    f"Observed {direct_count} direct API-to-persistence runtime path(s), which violates intended layered behavior."
                ),
            }
        )

    issues = causal_payload.get("issues") if isinstance(causal_payload.get("issues"), list) else []
    for item in issues:
        payload = item if isinstance(item, dict) else {}
        issue_type = str(payload.get("type", "")).strip().upper()
        if issue_type not in {"DIRECT_API_TO_PERSISTENCE_PATH", "DIRECT_STATE_CHANGE_BYPASSES_ORCHESTRATOR"}:
            continue
        message = str(payload.get("message", "")).strip() or "Runtime behavior bypasses intended orchestration boundaries."
        rows.append(
            {
                "type": "layer_violation",
                "source": "causal_flow_issue",
                "issue_type": issue_type,
                "message": message,
            }
        )

    rows.sort(
        key=lambda item: (
            str(item.get("issue_type", "")),
            str(item.get("message", "")),
        )
    )
    deduped: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()
    for item in rows:
        key = (str(item.get("issue_type", "")), str(item.get("message", "")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped


def _extract_orphan_module_violations(graph_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    node_map = _normalize_graph_nodes(graph_payload)
    edges = _normalize_graph_edges(graph_payload)

    file_node_ids = {
        node_id
        for node_id, payload in node_map.items()
        if _is_python_file_node(node_id, payload)
    }
    if not file_node_ids:
        return []

    degree_by_node: Dict[str, int] = {node_id: 0 for node_id in sorted(file_node_ids)}
    for source_id, target_id, _edge_type in edges:
        if source_id in degree_by_node:
            degree_by_node[source_id] = int(degree_by_node.get(source_id, 0)) + 1
        if target_id in degree_by_node:
            degree_by_node[target_id] = int(degree_by_node.get(target_id, 0)) + 1

    rows: List[Dict[str, Any]] = []
    for node_id in sorted(degree_by_node.keys()):
        if int(degree_by_node.get(node_id, 0)) != 0:
            continue
        path = str((node_map.get(node_id) or {}).get("path", "")).strip()
        rows.append(
            {
                "type": "orphan_module",
                "node_id": node_id,
                "path": path,
                "message": "Module is isolated in dependency structure (no incoming or outgoing edges).",
            }
        )

    return rows[:60]


def _extract_overcoupled_node_violations(graph_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    node_map = _normalize_graph_nodes(graph_payload)
    edges = _normalize_graph_edges(graph_payload)

    file_node_ids = {
        node_id
        for node_id, payload in node_map.items()
        if _is_python_file_node(node_id, payload)
    }
    if not file_node_ids:
        return []

    degree_by_node: Dict[str, int] = {node_id: 0 for node_id in sorted(file_node_ids)}
    for source_id, target_id, _edge_type in edges:
        if source_id in degree_by_node:
            degree_by_node[source_id] = int(degree_by_node.get(source_id, 0)) + 1
        if target_id in degree_by_node:
            degree_by_node[target_id] = int(degree_by_node.get(target_id, 0)) + 1

    degree_values = sorted(int(value) for value in degree_by_node.values())
    if not degree_values:
        return []
    median = degree_values[len(degree_values) // 2]
    threshold = max(8, int(median) * 2)

    rows: List[Dict[str, Any]] = []
    for node_id, degree in sorted(
        degree_by_node.items(),
        key=lambda item: (-int(item[1]), str((node_map.get(item[0]) or {}).get("path", "")), str(item[0])),
    ):
        if int(degree) < threshold:
            continue
        path = str((node_map.get(node_id) or {}).get("path", "")).strip()
        rows.append(
            {
                "type": "overcoupled_node",
                "node_id": node_id,
                "path": path,
                "dependency_degree": int(degree),
                "message": f"Module has dependency degree {int(degree)}, which exceeds over-coupling threshold {threshold}.",
            }
        )

    return rows[:60]


def _extract_circular_dependency_violations(graph_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    node_map = _normalize_graph_nodes(graph_payload)
    edges = _normalize_graph_edges(graph_payload)

    file_node_ids = {
        node_id
        for node_id, payload in node_map.items()
        if _is_python_file_node(node_id, payload)
    }
    if not file_node_ids:
        return []

    adjacency: Dict[str, Set[str]] = {node_id: set() for node_id in sorted(file_node_ids)}
    for source_id, target_id, _edge_type in edges:
        if source_id in adjacency and target_id in adjacency:
            adjacency[source_id].add(target_id)

    components = _strongly_connected_components(sorted(file_node_ids), adjacency)

    rows: List[Dict[str, Any]] = []
    for component in components:
        if len(component) <= 1:
            node_id = component[0] if component else ""
            if not node_id or node_id not in adjacency or node_id not in adjacency.get(node_id, set()):
                continue

        paths = sorted(
            {
                str((node_map.get(node_id) or {}).get("path", "")).strip()
                for node_id in component
                if str((node_map.get(node_id) or {}).get("path", "")).strip()
            }
        )
        rows.append(
            {
                "type": "circular_dependency",
                "node_ids": sorted(component),
                "paths": paths,
                "message": f"Circular dependency detected across {len(component)} module node(s).",
            }
        )

    rows.sort(
        key=lambda item: (
            str((item.get("paths") or [""])[0]),
            int(len(item.get("node_ids", []))),
        )
    )
    return rows[:60]


def _strongly_connected_components(nodes: Sequence[str], adjacency: Dict[str, Set[str]]) -> List[List[str]]:
    index = 0
    indices: Dict[str, int] = {}
    lowlinks: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: Set[str] = set()
    components: List[List[str]] = []

    def strongconnect(node_id: str) -> None:
        nonlocal index
        indices[node_id] = index
        lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)

        for next_id in sorted(adjacency.get(node_id, set())):
            if next_id not in indices:
                strongconnect(next_id)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[next_id])
            elif next_id in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indices[next_id])

        if lowlinks[node_id] == indices[node_id]:
            component: List[str] = []
            while stack:
                current = stack.pop()
                on_stack.discard(current)
                component.append(current)
                if current == node_id:
                    break
            components.append(sorted(component))

    for node_id in sorted(nodes):
        if node_id not in indices:
            strongconnect(node_id)

    return components


def _normalize_graph_nodes(graph_payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    node_rows = graph_payload.get("nodes") if isinstance(graph_payload.get("nodes"), list) else []
    node_map: Dict[str, Dict[str, Any]] = {}

    for row in sorted(node_rows, key=lambda item: str((item if isinstance(item, dict) else {}).get("id", ""))):
        payload = row if isinstance(row, dict) else {}
        node_id = str(payload.get("id", payload.get("node_id", ""))).strip()
        if not node_id:
            continue
        path = str(payload.get("path", "")).strip().replace("\\", "/")
        if not path:
            path = _path_from_node_id(node_id)
        node_map[node_id] = {
            "id": node_id,
            "kind": str(payload.get("kind", "")).strip().lower(),
            "path": path,
        }

    return node_map


def _normalize_graph_edges(graph_payload: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    edge_rows = graph_payload.get("edges") if isinstance(graph_payload.get("edges"), list) else []
    edges: List[Tuple[str, str, str]] = []

    for row in edge_rows:
        payload = row if isinstance(row, dict) else {}
        source_id = str(payload.get("source", payload.get("from", ""))).strip()
        target_id = str(payload.get("target", payload.get("to", ""))).strip()
        edge_type = str(payload.get("type", "")).strip().upper() or "UNKNOWN"
        if not source_id or not target_id:
            continue
        edges.append((source_id, target_id, edge_type))

    edges.sort(key=lambda item: (item[0], item[1], item[2]))
    return edges


def _is_python_file_node(node_id: str, payload: Dict[str, Any]) -> bool:
    kind = str(payload.get("kind", "")).strip().lower()
    path = str(payload.get("path", "")).strip()

    if not path.endswith(".py"):
        return False

    if kind == "file":
        return True
    if str(node_id).startswith("file:"):
        return True
    if str(node_id).startswith("canonical://file/"):
        return True

    return False


def _path_from_node_id(node_id: str) -> str:
    text = str(node_id or "").strip().replace("\\", "/")
    if not text:
        return ""

    if text.startswith("canonical://"):
        suffix = text[len("canonical://") :]
        if "/" not in suffix:
            return ""
        kind, remainder = suffix.split("/", 1)
        if kind == "file":
            return remainder
        if "/" in remainder:
            return remainder.rsplit("/", 1)[0]
        return remainder

    if ":" in text:
        kind, payload = text.split(":", 1)
        kind = kind.strip().lower()
        payload = payload.strip()
        if kind == "file":
            return payload
        if kind in {"function", "class"} and ":" in payload:
            return payload.rsplit(":", 1)[0]
        return payload

    return text


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
    architecture_result: Dict[str, Any] | None = None,
    semantic_result: Dict[str, Any] | None = None,
    causal_flow_result: Dict[str, Any] | None = None,
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

    architecture_payload = {}
    if isinstance(architecture_result, dict):
        architecture_payload = (
            architecture_result.get("report")
            if isinstance(architecture_result.get("report"), dict)
            else {}
        )

    semantic_payload = {}
    if isinstance(semantic_result, dict):
        semantic_payload = (
            semantic_result.get("report")
            if isinstance(semantic_result.get("report"), dict)
            else {}
        )

    causal_payload = {}
    if isinstance(causal_flow_result, dict):
        causal_payload = (
            causal_flow_result.get("report")
            if isinstance(causal_flow_result.get("report"), dict)
            else {}
        )

    diagnostics_payload = diagnostics_result.get("diagnostics") if isinstance(diagnostics_result.get("diagnostics"), dict) else {}
    diagnostics_summary = diagnostics_payload.get("summary") if isinstance(diagnostics_payload.get("summary"), dict) else {}
    diagnostics_sections = diagnostics_payload.get("validation_sections") if isinstance(diagnostics_payload.get("validation_sections"), dict) else {}
    diagnostics_root_causes = diagnostics_payload.get("root_causes") if isinstance(diagnostics_payload.get("root_causes"), list) else []
    diagnostics_actions = diagnostics_payload.get("recommended_actions") if isinstance(diagnostics_payload.get("recommended_actions"), list) else []
    diagnostics_domains = diagnostics_payload.get("failure_domains") if isinstance(diagnostics_payload.get("failure_domains"), list) else []

    architecture_summary = architecture_payload.get("summary") if isinstance(architecture_payload.get("summary"), dict) else {}
    semantic_summary = semantic_payload.get("summary") if isinstance(semantic_payload.get("summary"), dict) else {}
    causal_summary = causal_payload.get("summary") if isinstance(causal_payload.get("summary"), dict) else {}

    trust_breakdown = trust_payload.get("breakdown") if isinstance(trust_payload.get("breakdown"), dict) else {}
    trust_scores = trust_breakdown.get("scores") if isinstance(trust_breakdown.get("scores"), dict) else {}
    trust_domain_scores = trust_breakdown.get("domain_scores") if isinstance(trust_breakdown.get("domain_scores"), dict) else {}

    score_map: Dict[str, Any] = dict(trust_domain_scores)
    score_map.update(trust_scores)

    structural_integrity_score = _numeric_score(score_map, ("structural_integrity", "structural"), 0.0)
    dependency_consistency_score = _numeric_score(score_map, ("dependency_consistency", "resolver"), 0.0)
    topology_validation_score = _numeric_score(score_map, ("topology_validation", "reachability"), 0.0)
    semantic_observation_score = _numeric_score(score_map, ("semantic_observations", "semantic"), 0.0)
    execution_confidence_score = _numeric_score(
        score_map,
        ("execution_confidence", "execution"),
        _to_float(runtime_summary.get("coverage_ratio", 0.0)),
    )

    architecture_intent_score = _clamp01(_to_float(architecture_summary.get("domain_score", 0.0)))
    semantic_consistency_score = _clamp01(_to_float(semantic_summary.get("domain_score", semantic_observation_score)))
    causal_flow_score = _clamp01(_to_float(causal_summary.get("domain_score", 0.0)))

    structure_score = _clamp01(
        _avg(
            [
                structural_integrity_score,
                dependency_consistency_score,
                topology_validation_score,
            ]
        )
    )
    intent_score = _clamp01(
        _avg(
            [
                architecture_intent_score,
                semantic_consistency_score,
                causal_flow_score,
            ]
        )
    )
    behavior_score = _clamp01(
        _avg(
            [
                execution_confidence_score,
                _clamp01(_to_float(runtime_summary.get("coverage_ratio", 0.0))),
                _clamp01(_to_float(causal_summary.get("role_coverage_ratio", 0.0))),
            ]
        )
    )

    architecture_violation_count = _to_int(architecture_summary.get("violation_count_total", 0))
    boundary_crossing_count = _to_int(architecture_summary.get("boundary_crossing_count", 0))
    violation_ratio = _clamp01(_to_float(architecture_summary.get("violation_ratio", 0.0)))
    boundary_crossing_ratio = _clamp01(_to_float(architecture_summary.get("boundary_crossing_ratio", 0.0)))

    duplicate_intent_cluster_count = _to_int(semantic_summary.get("duplicate_intent_cluster_count", 0))
    abstraction_collision_count = _to_int(semantic_summary.get("abstraction_collision_count", 0))
    cross_context_cluster_count = _to_int(semantic_summary.get("cross_context_cluster_count", 0))
    high_overlap_cluster_count = _to_int(semantic_summary.get("high_overlap_cluster_count", 0))

    direct_api_to_persistence_count = _to_int(causal_summary.get("direct_api_to_persistence_count", 0))
    workflow_count = _to_int(causal_summary.get("workflow_count", 0))

    run_count = _to_int(runtime_summary.get("run_count", 0))
    call_event_count = _to_int(runtime_summary.get("call_event_count", 0))
    runtime_coverage_ratio = _clamp01(_to_float(runtime_summary.get("coverage_ratio", 0.0)))
    min_execution_confidence = _clamp01(_to_float(trust_breakdown.get("min_execution_confidence", 0.3), 0.3))
    execution_gate_applied = bool(trust_breakdown.get("execution_gate_applied", False))

    redundancy_penalty = min(
        1.0,
        (duplicate_intent_cluster_count * 0.12)
        + (abstraction_collision_count * 0.12)
        + (cross_context_cluster_count * 0.08)
        + (high_overlap_cluster_count * 0.05),
    )
    redundancy_score = _clamp01(1.0 - redundancy_penalty)

    architecture_penalty = min(
        1.0,
        (violation_ratio * 0.55)
        + (boundary_crossing_ratio * 0.30)
        + min(0.15, direct_api_to_persistence_count * 0.03),
    )
    architectural_quality_score = _clamp01((intent_score * 0.60) + (structure_score * 0.40) - architecture_penalty)

    structural_findings: List[str] = []
    _append_finding(
        structural_findings,
        _to_int(manifest_summary.get("file_count", 0)) == 0,
        "No source files were indexed; structural inventory is incomplete.",
    )
    _append_finding(
        structural_findings,
        _to_int(graph_summary.get("node_count", 0)) == 0,
        "Dependency graph contains zero nodes, so topology evidence is missing.",
    )
    _append_finding(
        structural_findings,
        architecture_violation_count > 0,
        f"Detected {architecture_violation_count} architectural constraint violations in declared dependency structure.",
    )
    _append_finding(
        structural_findings,
        _to_float(architecture_summary.get("constraint_coverage_ratio", 0.0)) < 0.6
        and _to_int(architecture_summary.get("classified_node_count", 0)) > 0,
        "Architectural intent coverage is below 0.60, so a large share of nodes remains weakly classified.",
    )
    if not structural_findings:
        structural_findings.append("Structural inventory, graph topology, and architecture mapping were produced without major coverage gaps.")

    behavioral_findings: List[str] = []
    _append_finding(
        behavioral_findings,
        run_count <= 0,
        "No runtime executions were captured, so behavior could not be confirmed empirically.",
    )
    _append_finding(
        behavioral_findings,
        run_count > 0 and execution_confidence_score < min_execution_confidence,
        (
            f"Execution confidence {execution_confidence_score:.3f} is below minimum threshold "
            f"{min_execution_confidence:.3f}; runtime coverage is treated as advisory."
        ),
    )
    _append_finding(
        behavioral_findings,
        run_count > 0 and workflow_count == 0,
        "Runtime traces did not reconstruct end-to-end workflows despite observed execution events.",
    )
    _append_finding(
        behavioral_findings,
        direct_api_to_persistence_count > 0,
        (
            f"Observed {direct_api_to_persistence_count} direct API-to-persistence path(s), "
            "which bypass expected orchestration/decision stages."
        ),
    )
    if not behavioral_findings:
        behavioral_findings.append("Runtime traces and causal-flow signals align with expected behavioral pathways.")

    redundancy_findings: List[str] = []
    _append_finding(
        redundancy_findings,
        duplicate_intent_cluster_count > 0,
        f"Detected {duplicate_intent_cluster_count} duplicate intent cluster(s) that indicate overlapping responsibilities.",
    )
    _append_finding(
        redundancy_findings,
        abstraction_collision_count > 0,
        f"Detected {abstraction_collision_count} abstraction collision(s) where concept roots map to inconsistent abstraction styles.",
    )
    _append_finding(
        redundancy_findings,
        cross_context_cluster_count > 0,
        f"Detected {cross_context_cluster_count} cross-context semantic overlap cluster(s), suggesting bounded-context leakage.",
    )
    _append_finding(
        redundancy_findings,
        high_overlap_cluster_count > 0,
        f"Detected {high_overlap_cluster_count} high-overlap semantic cluster(s) with strong token-level duplication.",
    )
    _append_finding(
        redundancy_findings,
        _to_int(dead_summary.get("dead_candidate_count", 0)) > 0,
        (
            f"Dead-code classifier surfaced {_to_int(dead_summary.get('dead_candidate_count', 0))} candidate(s); "
            "review for stale duplicates before deletion."
        ),
    )
    if not redundancy_findings:
        redundancy_findings.append("No material semantic duplication or overlap pressure was detected.")

    causal_issues = causal_payload.get("issues") if isinstance(causal_payload.get("issues"), list) else []
    missing_expectations = sorted(
        {
            str((item if isinstance(item, dict) else {}).get("type", "")).strip()
            for item in causal_issues
            if str((item if isinstance(item, dict) else {}).get("type", "")).strip().startswith("MISSING_")
        }
    )

    architectural_findings: List[str] = []
    _append_finding(
        architectural_findings,
        architecture_violation_count > 0,
        f"Architectural intent is violated by {architecture_violation_count} constraint breach(es).",
    )
    _append_finding(
        architectural_findings,
        boundary_crossing_count > 0,
        f"Detected {boundary_crossing_count} cross-owner boundary dependency crossing(s) outside explicit contracts.",
    )
    _append_finding(
        architectural_findings,
        direct_api_to_persistence_count > 0,
        "Causal flow indicates direct API-to-persistence transitions that should be mediated by domain orchestration.",
    )
    _append_finding(
        architectural_findings,
        bool(str(diagnostics_summary.get("primary_failure_mode", "")).strip()),
        f"Primary failure mode: {str(diagnostics_summary.get('primary_failure_mode', '')).strip()}",
    )
    if not architectural_findings:
        architectural_findings.append("Architectural quality signals are currently consistent with declared intent boundaries.")

    intent_sources: List[str] = []
    if architecture_payload:
        intent_sources.append("architecture_constraints")
    if semantic_payload:
        intent_sources.append("semantic_clusters")
    if causal_payload:
        intent_sources.append("causal_flow")

    structure_sources: List[str] = []
    if manifest_summary:
        structure_sources.append("manifest_inventory")
    if static_summary:
        structure_sources.append("static_analysis")
    if graph_summary:
        structure_sources.append("dependency_graph")
    if trust_breakdown:
        structure_sources.append("trust_model")

    runtime_sources: List[str] = []
    if runtime_summary:
        runtime_sources.append("runtime_execution")
    if run_count > 0:
        runtime_sources.append("runtime_trace_events")

    signal_sources = sorted(set(intent_sources + structure_sources + runtime_sources))
    signal_count = len(signal_sources)

    multi_signal_confirmed = bool(
        signal_count >= 4
        and len(intent_sources) >= 2
        and len(structure_sources) >= 2
        and len(runtime_sources) >= 1
    )

    intent_over_execution_score = _clamp01((intent_score * 0.70) + (execution_confidence_score * 0.30))
    structure_over_runtime_score = _clamp01((structure_score * 0.65) + (execution_confidence_score * 0.35))
    multi_signal_score = 1.0 if multi_signal_confirmed else _clamp01(signal_count / 4.0)
    design_quality_score = _clamp01(
        (intent_over_execution_score * 0.40)
        + (structure_over_runtime_score * 0.35)
        + (multi_signal_score * 0.25)
    )

    decision_authority = "single_metric_fallback"
    if multi_signal_confirmed:
        decision_authority = "multi_signal_intent_first"
    elif len(intent_sources) >= 1 and len(structure_sources) >= 1:
        decision_authority = "intent_and_structure_with_limited_runtime"

    structural_audit = {
        "title": "Structural Audit (what exists)",
        "status": _score_status(structure_score),
        "score": _round3(structure_score),
        "signals": {
            "file_count": _to_int(manifest_summary.get("file_count", 0)),
            "python_file_count": _to_int(manifest_summary.get("python_file_count", 0)),
            "entrypoint_count": len(manifest_summary.get("entrypoints", [])) if isinstance(manifest_summary.get("entrypoints"), list) else 0,
            "graph_node_count": _to_int(graph_summary.get("node_count", 0)),
            "graph_edge_count": _to_int(graph_summary.get("edge_count", 0)),
            "architecture_constraint_coverage_ratio": _round3(_clamp01(_to_float(architecture_summary.get("constraint_coverage_ratio", 0.0)))),
            "architecture_violation_count": architecture_violation_count,
            "structural_integrity_score": _round3(structural_integrity_score),
            "dependency_consistency_score": _round3(dependency_consistency_score),
            "topology_validation_score": _round3(topology_validation_score),
        },
        "findings": structural_findings,
    }

    behavioral_audit = {
        "title": "Behavioral Audit (what it does)",
        "status": _score_status(behavior_score, pass_floor=0.7, degraded_floor=0.45),
        "score": _round3(behavior_score),
        "signals": {
            "run_count": run_count,
            "call_event_count": call_event_count,
            "coverage_ratio": _round3(runtime_coverage_ratio),
            "workflow_count": workflow_count,
            "role_coverage_ratio": _round3(_clamp01(_to_float(causal_summary.get("role_coverage_ratio", 0.0)))),
            "execution_confidence": _round3(execution_confidence_score),
            "min_execution_confidence": _round3(min_execution_confidence),
            "execution_gate_applied": execution_gate_applied,
        },
        "findings": behavioral_findings,
    }

    redundancy_overlap_audit = {
        "title": "Redundancy & overlap audit (what is duplicated)",
        "status": _score_status(redundancy_score, pass_floor=0.8, degraded_floor=0.6),
        "score": _round3(redundancy_score),
        "signals": {
            "duplicate_intent_cluster_count": duplicate_intent_cluster_count,
            "cross_context_cluster_count": cross_context_cluster_count,
            "high_overlap_cluster_count": high_overlap_cluster_count,
            "abstraction_collision_count": abstraction_collision_count,
            "dead_candidate_count": _to_int(dead_summary.get("dead_candidate_count", 0)),
        },
        "findings": redundancy_findings,
    }

    architectural_quality_audit = {
        "title": "Architectural quality audit (what should exist)",
        "status": _score_status(architectural_quality_score, pass_floor=0.72, degraded_floor=0.5),
        "score": _round3(architectural_quality_score),
        "signals": {
            "intent_score": _round3(intent_score),
            "structure_score": _round3(structure_score),
            "violation_ratio": _round3(violation_ratio),
            "boundary_crossing_ratio": _round3(boundary_crossing_ratio),
            "direct_api_to_persistence_count": direct_api_to_persistence_count,
            "diagnostic_status": str(diagnostics_payload.get("status", "UNKNOWN")),
            "failure_domains": [str(item) for item in diagnostics_domains[:10]],
            "degraded_validation_sections": sorted(
                [
                    str(name)
                    for name, section in diagnostics_sections.items()
                    if isinstance(section, dict)
                    and str(section.get("status", "")).upper() in {"DEGRADED", "FAIL"}
                ]
            ),
        },
        "expected_missing_capabilities": missing_expectations,
        "root_cause_evidence": _top_root_cause_descriptions(diagnostics_root_causes, limit=3),
        "recommended_actions": [str(item) for item in diagnostics_actions[:5]],
        "findings": architectural_findings,
    }

    principle_enforcement = {
        "intent_over_execution": {
            "enforced": True,
            "status": _score_status(intent_over_execution_score, pass_floor=0.72, degraded_floor=0.48),
            "intent_score": _round3(intent_score),
            "execution_score": _round3(execution_confidence_score),
            "weighting": {"intent": 0.70, "execution": 0.30},
            "decision_score": _round3(intent_over_execution_score),
        },
        "structure_over_runtime_alone": {
            "enforced": True,
            "status": _score_status(structure_over_runtime_score, pass_floor=0.72, degraded_floor=0.48),
            "structure_score": _round3(structure_score),
            "runtime_score": _round3(execution_confidence_score),
            "weighting": {"structure": 0.65, "runtime": 0.35},
            "decision_score": _round3(structure_over_runtime_score),
        },
        "multi_signal_confirmation_over_single_metric": {
            "enforced": True,
            "status": "PASS" if multi_signal_confirmed else "FAIL",
            "signal_count": signal_count,
            "intent_signal_count": len(intent_sources),
            "structure_signal_count": len(structure_sources),
            "runtime_signal_count": len(runtime_sources),
            "signal_sources": signal_sources,
            "single_metric_override_blocked": bool(multi_signal_confirmed),
            "decision_score": _round3(multi_signal_score),
        },
    }

    design_quality_signals = {
        "status": _score_status(design_quality_score, pass_floor=0.75, degraded_floor=0.52),
        "score": _round3(design_quality_score),
        "decision_authority": decision_authority,
        "intent_score": _round3(intent_score),
        "structure_score": _round3(structure_score),
        "behavior_score": _round3(behavior_score),
        "principle_enforcement": principle_enforcement,
    }

    architect_auditor = _build_architect_auditor(
        graph_payload=graph_payload,
        architecture_payload=architecture_payload,
        semantic_payload=semantic_payload,
        causal_payload=causal_payload,
        runtime_summary=runtime_summary,
        execution_gate_applied=execution_gate_applied,
    )

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
        "architecture_constraints": architecture_payload,
        "semantic_clusters": semantic_payload,
        "causal_flow": causal_payload,
        "diagnostics": diagnostics_payload,
        "trust": trust_payload,
        "audit_principles": {
            "intent_over_execution": True,
            "structure_over_runtime_alone": True,
            "multi_signal_confirmation_over_single_metric": True,
        },
        "structural_audit": structural_audit,
        "behavioral_audit": behavioral_audit,
        "redundancy_overlap_audit": redundancy_overlap_audit,
        "architectural_quality_audit": architectural_quality_audit,
        "audit_layers": {
            "structural": structural_audit,
            "behavioral": behavioral_audit,
            "redundancy_overlap": redundancy_overlap_audit,
            "architectural_quality": architectural_quality_audit,
        },
        "architect_auditor": architect_auditor,
        "design_quality_signals": design_quality_signals,
        "system_valid": bool(system_valid),
        "summary": {
            "status": "PASSED" if bool(system_valid) else "FAILED",
            "primary_failure_mode": str(
                (diagnostics_summary
                 .get("primary_failure_mode", diagnostics_result.get("root_cause", "none")))
            ),
            "dead_candidate_count": int(dead_summary.get("dead_candidate_count", 0) or 0),
            "design_quality_score": _round3(design_quality_score),
            "decision_authority": decision_authority,
            "evidence_signal_count": signal_count,
        },
    }

    write_json(final_report_path, report, pretty=True)

    return {
        "report_path": str(final_report_path),
        "report": report,
    }
