from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

from repo_audit_engine.io.artifacts import write_json


_RULE_SAMPLE_LIMIT = 250

_LAYER_UNKNOWN = "unknown"

_GENERIC_CONTEXT_SEGMENTS = {
    "repo_audit_engine",
    "src",
    "apps",
    "tests",
    "archive",
    "scripts",
    "output",
    "state",
    "config",
}

_ALLOWED_LAYER_DEPENDENCIES = {
    "api": {"api", "orchestration", "domain", "shared", "unknown", "test"},
    "orchestration": {"orchestration", "domain", "infra", "shared", "unknown", "test", "api"},
    "domain": {"domain", "shared", "unknown", "test"},
    "infra": {"infra", "domain", "shared", "unknown", "test"},
    "shared": {"api", "orchestration", "domain", "infra", "shared", "unknown", "test"},
    "test": {"api", "orchestration", "domain", "infra", "shared", "unknown", "test"},
    "unknown": {"api", "orchestration", "domain", "infra", "shared", "unknown", "test"},
}

_RULES = [
    {
        "id": "LAYER_DIRECTION_VIOLATION",
        "description": "Layer dependency direction violates declared architectural boundaries.",
        "severity": "HIGH",
    },
    {
        "id": "SERVICE_IMPORTS_ADAPTER",
        "description": "Services should not directly depend on adapter implementations.",
        "severity": "MEDIUM",
    },
    {
        "id": "DIRECT_STATE_CHANGE_BYPASSES_ORCHESTRATOR",
        "description": "State/persistence dependencies from API layer should be mediated by orchestration/domain.",
        "severity": "MEDIUM",
    },
    {
        "id": "OWNERSHIP_BOUNDARY_CROSSING",
        "description": "Cross-owner dependencies should cross explicit contracts/events boundaries.",
        "severity": "LOW",
    },
]


def build_architecture_constraint_report(
    graph_payload: Mapping[str, Any],
    output_dir: Path,
    sample_limit: int = _RULE_SAMPLE_LIMIT,
) -> Dict[str, Any]:
    report = evaluate_architecture_constraints(graph_payload=graph_payload, sample_limit=sample_limit)

    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    report_path = out_root / "architecture_constraints.json"
    write_json(report_path, report, pretty=True)

    return {
        "report_path": str(report_path),
        "report": report,
    }


def evaluate_architecture_constraints(
    graph_payload: Mapping[str, Any],
    sample_limit: int = _RULE_SAMPLE_LIMIT,
) -> Dict[str, Any]:
    graph = _unwrap_graph_payload(graph_payload)

    node_map, alias_map = _build_node_map(graph.get("nodes", []))
    edge_rows = _normalize_edges(graph.get("edges", []), alias_map)

    layer_counts: Dict[str, int] = {}
    context_counts: Dict[str, int] = {}
    owner_counts: Dict[str, int] = {}

    for node in sorted(node_map.values(), key=lambda item: str(item.get("node_id", ""))):
        layer = str(node.get("layer", _LAYER_UNKNOWN))
        context = str(node.get("context", "global"))
        owner = str(node.get("owner", "global"))

        layer_counts[layer] = int(layer_counts.get(layer, 0)) + 1
        context_counts[context] = int(context_counts.get(context, 0)) + 1
        owner_counts[owner] = int(owner_counts.get(owner, 0)) + 1

    violations: List[Dict[str, Any]] = []
    seen_violation_keys: set[Tuple[str, str, str]] = set()
    violation_count_total = 0

    violation_counts_by_rule: Dict[str, int] = {}
    layered_edge_count = 0
    boundary_crossing_count = 0

    for edge in edge_rows:
        source = node_map.get(str(edge.get("source", "")), {})
        target = node_map.get(str(edge.get("target", "")), {})

        source_layer = str(source.get("layer", _LAYER_UNKNOWN))
        target_layer = str(target.get("layer", _LAYER_UNKNOWN))

        source_path = str(source.get("path", ""))
        target_path = str(target.get("path", ""))

        if source_layer != _LAYER_UNKNOWN and target_layer != _LAYER_UNKNOWN:
            layered_edge_count += 1

            allowed_targets = _ALLOWED_LAYER_DEPENDENCIES.get(source_layer, _ALLOWED_LAYER_DEPENDENCIES[_LAYER_UNKNOWN])
            if target_layer not in allowed_targets:
                violation_count_total += 1
                violation_counts_by_rule["LAYER_DIRECTION_VIOLATION"] = int(
                    violation_counts_by_rule.get("LAYER_DIRECTION_VIOLATION", 0)
                ) + 1
                _record_violation(
                    violations=violations,
                    seen_keys=seen_violation_keys,
                    sample_limit=max(1, int(sample_limit)),
                    rule_id="LAYER_DIRECTION_VIOLATION",
                    severity="HIGH",
                    reason=(
                        f"{source_layer} depends on {target_layer}, which violates the architectural dependency direction."
                    ),
                    edge=edge,
                    source=source,
                    target=target,
                )

        if _is_service_path(source_path) and _is_adapter_path(target_path):
            violation_count_total += 1
            violation_counts_by_rule["SERVICE_IMPORTS_ADAPTER"] = int(
                violation_counts_by_rule.get("SERVICE_IMPORTS_ADAPTER", 0)
            ) + 1
            _record_violation(
                violations=violations,
                seen_keys=seen_violation_keys,
                sample_limit=max(1, int(sample_limit)),
                rule_id="SERVICE_IMPORTS_ADAPTER",
                severity="MEDIUM",
                reason="Service-level module depends directly on adapter implementation.",
                edge=edge,
                source=source,
                target=target,
            )

        if (
            source_layer == "api"
            and _is_persistence_path(target_path)
            and ("orchestrator" not in _path_segments(source_path))
        ):
            violation_count_total += 1
            violation_counts_by_rule["DIRECT_STATE_CHANGE_BYPASSES_ORCHESTRATOR"] = int(
                violation_counts_by_rule.get("DIRECT_STATE_CHANGE_BYPASSES_ORCHESTRATOR", 0)
            ) + 1
            _record_violation(
                violations=violations,
                seen_keys=seen_violation_keys,
                sample_limit=max(1, int(sample_limit)),
                rule_id="DIRECT_STATE_CHANGE_BYPASSES_ORCHESTRATOR",
                severity="MEDIUM",
                reason="API-layer dependency appears to bypass orchestration and touches persistence concerns directly.",
                edge=edge,
                source=source,
                target=target,
            )

        source_owner = str(source.get("owner", ""))
        target_owner = str(target.get("owner", ""))
        if (
            source_owner
            and target_owner
            and source_owner != target_owner
            and not _is_contract_boundary_edge(source_path, target_path)
        ):
            boundary_crossing_count += 1
            violation_count_total += 1
            violation_counts_by_rule["OWNERSHIP_BOUNDARY_CROSSING"] = int(
                violation_counts_by_rule.get("OWNERSHIP_BOUNDARY_CROSSING", 0)
            ) + 1
            _record_violation(
                violations=violations,
                seen_keys=seen_violation_keys,
                sample_limit=max(1, int(sample_limit)),
                rule_id="OWNERSHIP_BOUNDARY_CROSSING",
                severity="LOW",
                reason="Cross-owner dependency is not mediated by an explicit contract/event boundary.",
                edge=edge,
                source=source,
                target=target,
            )

    total_nodes = len(node_map)
    classified_nodes = len([node for node in node_map.values() if str(node.get("layer", _LAYER_UNKNOWN)) != _LAYER_UNKNOWN])
    total_edges = len(edge_rows)

    constraint_coverage_ratio = _safe_divide(classified_nodes, max(1, total_nodes))
    violation_ratio = _safe_divide(violation_count_total, max(1, layered_edge_count if layered_edge_count > 0 else total_edges))
    boundary_crossing_ratio = _safe_divide(boundary_crossing_count, max(1, total_edges))

    penalty = min(
        1.0,
        (violation_ratio * 0.65)
        + ((1.0 - constraint_coverage_ratio) * 0.20)
        + (boundary_crossing_ratio * 0.15),
    )
    domain_score = _round3(_clamp01(1.0 - penalty))

    warnings: List[str] = []
    if total_nodes > 0 and classified_nodes == 0:
        warnings.append("No nodes could be mapped to architectural layers; constraint checks were informational only.")
    if boundary_crossing_count > 0:
        warnings.append("Cross-owner dependencies were detected outside explicit contract/event boundaries.")

    intent_model = {
        "layer_counts": dict(sorted(layer_counts.items(), key=lambda item: item[0])),
        "context_counts": dict(sorted(context_counts.items(), key=lambda item: item[0])),
        "owner_counts": dict(sorted(owner_counts.items(), key=lambda item: item[0])),
        "classified_nodes": classified_nodes,
        "total_nodes": total_nodes,
        "constraint_coverage_ratio": _round3(constraint_coverage_ratio),
    }

    summary = {
        "node_count": total_nodes,
        "edge_count": total_edges,
        "classified_node_count": classified_nodes,
        "layered_edge_count": layered_edge_count,
        "violation_count_total": int(violation_count_total),
        "violation_count_sampled": len(violations),
        "boundary_crossing_count": int(boundary_crossing_count),
        "constraint_coverage_ratio": _round3(constraint_coverage_ratio),
        "violation_ratio": _round3(violation_ratio),
        "boundary_crossing_ratio": _round3(boundary_crossing_ratio),
        "domain_score": domain_score,
        "rule_violation_counts": dict(sorted(violation_counts_by_rule.items(), key=lambda item: item[0])),
    }

    return {
        "schema_version": "1.0",
        "intent_model": intent_model,
        "rules": list(_RULES),
        "violations": sorted(
            violations,
            key=lambda item: (
                _severity_rank(str(item.get("severity", ""))),
                str(item.get("rule_id", "")),
                str(item.get("source_node_id", "")),
                str(item.get("target_node_id", "")),
            ),
        ),
        "warnings": sorted(set(warnings)),
        "summary": summary,
    }


def _unwrap_graph_payload(graph_payload: Mapping[str, Any]) -> Dict[str, Any]:
    payload = graph_payload if isinstance(graph_payload, Mapping) else {}
    if isinstance(payload.get("graph"), Mapping):
        payload = payload.get("graph", {})

    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
    edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
    return {"nodes": nodes, "edges": edges}


def _build_node_map(nodes: Sequence[Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    node_map: Dict[str, Dict[str, Any]] = {}
    alias_map: Dict[str, str] = {}

    for raw in sorted(nodes, key=lambda item: str((item or {}).get("id", ""))):
        payload = raw if isinstance(raw, Mapping) else {}

        node_id = str(payload.get("id", "")).strip()
        canonical_id = str(payload.get("canonical_id", "")).strip()
        normalized_id = _normalize_node_id(node_id or canonical_id)
        if not normalized_id:
            continue

        path = _normalize_path(payload.get("path", _path_from_node_id(normalized_id)))
        kind = str(payload.get("kind", _kind_from_node_id(normalized_id))).strip().lower() or "symbol"

        layer = _infer_layer(path)
        context = _infer_context(path)
        owner = _infer_owner(path)

        node_map[normalized_id] = {
            "node_id": normalized_id,
            "canonical_id": canonical_id,
            "kind": kind,
            "path": path,
            "layer": layer,
            "context": context,
            "owner": owner,
        }

        for alias in {
            node_id,
            canonical_id,
            _canonicalize_legacy_node_id(node_id),
            _canonicalize_legacy_node_id(canonical_id),
            normalized_id,
        }:
            text = _normalize_node_id(alias)
            if text:
                alias_map[text] = normalized_id

    return node_map, alias_map


def _normalize_edges(edges: Sequence[Any], alias_map: Mapping[str, str]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for raw in sorted(
        edges,
        key=lambda item: (
            str((item or {}).get("source", (item or {}).get("from", ""))),
            str((item or {}).get("target", (item or {}).get("to", ""))),
            str((item or {}).get("type", "")),
        ),
    ):
        payload = raw if isinstance(raw, Mapping) else {}

        source = _resolve_node_reference(payload.get("source", payload.get("from", "")), alias_map)
        target = _resolve_node_reference(payload.get("target", payload.get("to", "")), alias_map)
        edge_type = str(payload.get("type", "")).strip().upper() or "UNKNOWN"

        if not source or not target:
            continue

        normalized.append({"source": source, "target": target, "type": edge_type})

    return normalized


def _resolve_node_reference(value: Any, alias_map: Mapping[str, str]) -> str:
    normalized = _normalize_node_id(value)
    if not normalized:
        return ""

    if normalized in alias_map:
        return str(alias_map.get(normalized, ""))

    canonical_candidate = _canonicalize_legacy_node_id(normalized)
    if canonical_candidate in alias_map:
        return str(alias_map.get(canonical_candidate, ""))

    return normalized


def _record_violation(
    violations: List[Dict[str, Any]],
    seen_keys: set[Tuple[str, str, str]],
    sample_limit: int,
    rule_id: str,
    severity: str,
    reason: str,
    edge: Mapping[str, Any],
    source: Mapping[str, Any],
    target: Mapping[str, Any],
) -> None:
    key = (
        str(rule_id),
        str(source.get("node_id", "")),
        str(target.get("node_id", "")),
    )
    if key in seen_keys:
        return
    seen_keys.add(key)

    if len(violations) >= max(1, int(sample_limit)):
        return

    violations.append(
        {
            "rule_id": str(rule_id),
            "severity": str(severity).upper(),
            "reason": str(reason),
            "edge_type": str(edge.get("type", "")),
            "source_node_id": str(source.get("node_id", "")),
            "target_node_id": str(target.get("node_id", "")),
            "source_path": str(source.get("path", "")),
            "target_path": str(target.get("path", "")),
            "source_layer": str(source.get("layer", _LAYER_UNKNOWN)),
            "target_layer": str(target.get("layer", _LAYER_UNKNOWN)),
            "source_context": str(source.get("context", "global")),
            "target_context": str(target.get("context", "global")),
            "source_owner": str(source.get("owner", "global")),
            "target_owner": str(target.get("owner", "global")),
        }
    )


def _normalize_node_id(value: Any) -> str:
    return str(value or "").strip()


def _normalize_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _kind_from_node_id(node_id: str) -> str:
    text = str(node_id or "").strip()
    if text.startswith("canonical://"):
        suffix = text[len("canonical://") :]
        return suffix.split("/", 1)[0].strip().lower() or "symbol"
    if ":" in text:
        return text.split(":", 1)[0].strip().lower() or "symbol"
    return "symbol"


def _path_from_node_id(node_id: str) -> str:
    text = str(node_id or "").strip()
    if text.startswith("canonical://"):
        suffix = text[len("canonical://") :]
        if "/" not in suffix:
            return ""
        remainder = suffix.split("/", 1)[1]
        if "/" in remainder:
            return remainder.rsplit("/", 1)[0]
        return remainder

    if ":" in text:
        pieces = text.split(":")
        if len(pieces) >= 3:
            return _normalize_path(":".join(pieces[1:-1]))
        if len(pieces) == 2:
            return _normalize_path(pieces[1])

    return ""


def _canonicalize_legacy_node_id(value: str) -> str:
    text = _normalize_node_id(value)
    if not text or text.startswith("canonical://"):
        return text

    if ":" not in text:
        return text

    kind, payload = text.split(":", 1)
    kind = kind.strip().lower()
    payload = payload.strip()
    if not kind or not payload:
        return text

    if kind in {"function", "class"} and ":" in payload:
        rel_path, _, name = payload.rpartition(":")
        rel_path = _normalize_path(rel_path)
        if rel_path and name:
            return f"canonical://{kind}/{rel_path}/{name.strip()}"

    if kind == "file":
        rel_path = _normalize_path(payload)
        if rel_path:
            return f"canonical://file/{rel_path}"

    return text


def _path_segments(path: str) -> List[str]:
    return [segment for segment in _normalize_path(path).lower().split("/") if segment]


def _infer_layer(path: str) -> str:
    segments = _path_segments(path)
    if not segments:
        return _LAYER_UNKNOWN

    if "tests" in segments or "test" in segments:
        return "test"

    if any(token in segments for token in {"orchestrator", "orchestration", "workflow", "pipeline"}):
        return "orchestration"

    if any(
        token in segments
        for token in {
            "infra",
            "infrastructure",
            "adapter",
            "adapters",
            "integration",
            "gateway",
            "repository",
            "repositories",
            "storage",
            "db",
            "database",
            "sql",
            "cache",
            "runtime",
        }
    ):
        return "infra"

    if any(token in segments for token in {"api", "endpoints", "endpoint", "router", "routes", "controller", "presentation", "ui"}):
        return "api"

    if any(token in segments for token in {"domain", "core", "models", "model", "policy_engine", "rules", "services"}):
        return "domain"

    if any(token in segments for token in {"common", "shared", "utils", "util"}):
        return "shared"

    return _LAYER_UNKNOWN


def _infer_context(path: str) -> str:
    segments = [segment for segment in _path_segments(path) if segment not in _GENERIC_CONTEXT_SEGMENTS]
    if not segments:
        return "global"
    if len(segments) == 1:
        return segments[0]
    return f"{segments[0]}/{segments[1]}"


def _infer_owner(path: str) -> str:
    segments = _path_segments(path)
    if not segments:
        return "global"

    if segments[0] in {"repo_audit_engine", "apps", "src", "tests"} and len(segments) >= 2:
        return f"{segments[0]}/{segments[1]}"

    return segments[0]


def _is_service_path(path: str) -> bool:
    segments = _path_segments(path)
    if "services" in segments:
        return True
    filename = segments[-1] if segments else ""
    return filename.endswith("service.py")


def _is_adapter_path(path: str) -> bool:
    segments = _path_segments(path)
    if "adapters" in segments or "adapter" in segments:
        return True
    filename = segments[-1] if segments else ""
    return filename.endswith("adapter.py")


def _is_persistence_path(path: str) -> bool:
    segments = _path_segments(path)
    if any(token in segments for token in {"repository", "repositories", "store", "storage", "db", "database", "sql", "cache"}):
        return True
    filename = segments[-1] if segments else ""
    return any(
        filename.endswith(suffix)
        for suffix in {
            "repository.py",
            "store.py",
            "storage.py",
            "db.py",
        }
    )


def _is_contract_boundary_edge(source_path: str, target_path: str) -> bool:
    source_segments = _path_segments(source_path)
    target_segments = _path_segments(target_path)
    boundary_tokens = {"contract", "contracts", "interface", "interfaces", "event", "events", "schema", "schemas"}
    return bool(boundary_tokens.intersection(source_segments) or boundary_tokens.intersection(target_segments))


def _severity_rank(value: str) -> int:
    normalized = str(value or "").strip().upper()
    if normalized == "HIGH":
        return 0
    if normalized == "MEDIUM":
        return 1
    if normalized == "LOW":
        return 2
    return 3


def _safe_divide(numerator: float, denominator: float) -> float:
    if float(denominator) == 0.0:
        return 0.0
    return float(numerator) / float(denominator)


def _clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return float(value)


def _round3(value: float) -> float:
    return round(float(value), 3)
