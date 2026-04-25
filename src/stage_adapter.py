from __future__ import annotations

import ast
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple


def _normalize_path(value: str) -> str:
    return str(value).strip().replace("\\", "/").lstrip("./")


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)


def _parse_stage_args(tokens: Sequence[str]) -> Dict[str, List[str]]:
    parsed: Dict[str, List[str]] = {}
    i = 0
    while i < len(tokens):
        token = str(tokens[i])
        if token.startswith("-"):
            if ":" in token:
                flag, value = token.split(":", 1)
                parsed.setdefault(flag.lower(), []).append(value)
            elif i + 1 < len(tokens) and not str(tokens[i + 1]).startswith("-"):
                parsed.setdefault(token.lower(), []).append(str(tokens[i + 1]))
                i += 1
            else:
                parsed.setdefault(token.lower(), []).append("true")
        i += 1
    return parsed


def _require(parsed: Dict[str, List[str]], flag: str) -> str:
    values = parsed.get(flag.lower(), [])
    if not values:
        raise RuntimeError(f"Missing required argument: {flag}")
    return str(values[0])


def _optional(parsed: Dict[str, List[str]], flag: str, default: str = "") -> str:
    values = parsed.get(flag.lower(), [])
    if not values:
        return default
    return str(values[0])


def _many(parsed: Dict[str, List[str]], flag: str) -> List[str]:
    values = parsed.get(flag.lower(), [])
    out: List[str] = []
    for item in values:
        raw = str(item)
        if "," in raw:
            out.extend([part.strip() for part in raw.split(",") if part.strip()])
        elif raw.strip():
            out.append(raw.strip())
    return out


def _parse_bool(value: str) -> bool:
    lowered = str(value).strip().lower().replace("$", "")
    return lowered in {"1", "true", "yes", "on"}


def _canonical_id(rel_path: str) -> str:
    return f"canonical://repo/_root:{_normalize_path(rel_path)}"


def _node_component_map(node_ids: Sequence[str], edges: Sequence[Dict[str, Any]]) -> List[List[str]]:
    adjacency: Dict[str, Set[str]] = {str(node_id): set() for node_id in node_ids}
    for edge in edges:
        payload = edge if isinstance(edge, dict) else {}
        src = str(payload.get("from", "")).strip()
        dst = str(payload.get("to", "")).strip()
        if src in adjacency and dst in adjacency:
            adjacency[src].add(dst)
            adjacency[dst].add(src)

    components: List[List[str]] = []
    seen: Set[str] = set()
    for node_id in sorted(adjacency.keys()):
        if node_id in seen:
            continue
        queue: deque[str] = deque([node_id])
        seen.add(node_id)
        component: List[str] = []
        while queue:
            current = queue.popleft()
            component.append(current)
            for nxt in sorted(adjacency.get(current, set())):
                if nxt in seen:
                    continue
                seen.add(nxt)
                queue.append(nxt)
        components.append(sorted(component))
    return components


def _parse_import_modules(file_path: Path) -> Set[str]:
    source = file_path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    modules: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(str(alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(str(node.module).split(".")[0])
    return modules


def _handle_layer1(parsed: Dict[str, List[str]]) -> int:
    repo_path = Path(_require(parsed, "-RepoPath")).resolve()
    output_path = Path(_require(parsed, "-OutputPath")).resolve()

    rows: List[Dict[str, Any]] = []
    for file_path in sorted(repo_path.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() != ".py":
            continue
        relative = _normalize_path(file_path.relative_to(repo_path).as_posix())
        rows.append(
            {
                "file_id": relative,
                "absolute_path": str(file_path),
                "extension": file_path.suffix.lower(),
            }
        )

    payload = {
        "repo_path": str(repo_path),
        "files": rows,
        "stats": {
            "total_files": len(rows),
            "python_files": len(rows),
        },
    }
    _write_json(output_path, payload)
    return 0


def _handle_layer2(parsed: Dict[str, List[str]]) -> int:
    inventory_path = Path(_require(parsed, "-InventoryPath")).resolve()
    output_path = Path(_require(parsed, "-OutputPath")).resolve()

    inventory = _load_json(inventory_path)
    files = inventory.get("files") if isinstance(inventory.get("files"), list) else []

    nodes: List[Dict[str, Any]] = []
    for item in files:
        payload = item if isinstance(item, dict) else {}
        rel_path = _normalize_path(str(payload.get("file_id") or payload.get("path") or ""))
        if not rel_path:
            continue
        module_path = rel_path[:-3].replace("/", ".") if rel_path.endswith(".py") else rel_path.replace("/", ".")
        nodes.append(
            {
                "id": _canonical_id(rel_path),
                "file_path": rel_path,
                "module_path": module_path,
                "type": "FILE",
            }
        )

    nodes.sort(key=lambda item: str(item.get("id", "")))
    _write_json(
        output_path,
        {
            "nodes": nodes,
            "stats": {
                "node_count": len(nodes),
            },
        },
    )
    return 0


def _handle_layer3(parsed: Dict[str, List[str]]) -> int:
    inventory_path = Path(_require(parsed, "-InventoryPath")).resolve()
    canonical_path = Path(_require(parsed, "-CanonicalPath")).resolve()
    output_path = Path(_require(parsed, "-OutputPath")).resolve()

    inventory = _load_json(inventory_path)
    canonical = _load_json(canonical_path)

    files = inventory.get("files") if isinstance(inventory.get("files"), list) else []
    nodes = canonical.get("nodes") if isinstance(canonical.get("nodes"), list) else []

    canonical_by_path: Dict[str, str] = {}
    for node in nodes:
        payload = node if isinstance(node, dict) else {}
        rel_path = _normalize_path(str(payload.get("file_path", "")))
        node_id = str(payload.get("id", "")).strip()
        if rel_path and node_id:
            canonical_by_path[rel_path] = node_id

    edges: List[Dict[str, Any]] = []
    for item in files:
        payload = item if isinstance(item, dict) else {}
        rel_path = _normalize_path(str(payload.get("file_id") or payload.get("path") or ""))
        absolute_path = Path(str(payload.get("absolute_path", "")).strip())
        if not rel_path or not absolute_path.exists():
            continue

        source_id = canonical_by_path.get(rel_path, "")
        if not source_id:
            continue

        import_roots = _parse_import_modules(absolute_path)
        for root in sorted(import_roots):
            candidate_rel = _normalize_path(f"{root}.py")
            target_id = canonical_by_path.get(candidate_rel, "")
            if not target_id:
                continue
            edges.append(
                {
                    "from": source_id,
                    "to": target_id,
                    "type": "IMPORT",
                    "confidence": 1.0,
                    "source": "AST",
                }
            )

    dedup: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    for edge in edges:
        key = (
            str(edge.get("from", "")),
            str(edge.get("to", "")),
            str(edge.get("type", "")),
            str(edge.get("source", "")),
        )
        dedup[key] = edge

    edge_rows = sorted(dedup.values(), key=lambda item: (str(item.get("from", "")), str(item.get("to", "")), str(item.get("type", "")), str(item.get("source", ""))))
    _write_json(
        output_path,
        {
            "edges": edge_rows,
            "stats": {
                "edge_count": len(edge_rows),
            },
        },
    )
    return 0


def _handle_layer4(parsed: Dict[str, List[str]]) -> int:
    canonical_path = Path(_require(parsed, "-CanonicalPath")).resolve()
    edges_path = Path(_require(parsed, "-EdgesPath")).resolve()
    output_path = Path(_require(parsed, "-OutputPath")).resolve()

    canonical = _load_json(canonical_path)
    edges_doc = _load_json(edges_path)

    nodes = [node for node in (canonical.get("nodes") if isinstance(canonical.get("nodes"), list) else []) if isinstance(node, dict)]
    edges = [edge for edge in (edges_doc.get("edges") if isinstance(edges_doc.get("edges"), list) else []) if isinstance(edge, dict)]

    node_ids = {str(node.get("id", "")).strip() for node in nodes if str(node.get("id", "")).strip()}

    unresolved = []
    for edge in edges:
        src = str(edge.get("from", "")).strip()
        dst = str(edge.get("to", "")).strip()
        if src not in node_ids or dst not in node_ids:
            unresolved.append(edge)

    if unresolved:
        print("UNRESOLVED_EDGE_REFERENCE", file=sys.stderr)
        return 1

    ast_pairs = {
        (str(edge.get("from", "")).strip(), str(edge.get("to", "")).strip())
        for edge in edges
        if str(edge.get("source", "")).strip().upper() == "AST"
    }
    di_pairs = {
        (str(edge.get("from", "")).strip(), str(edge.get("to", "")).strip())
        for edge in edges
        if str(edge.get("source", "")).strip().upper() == "DI" or str(edge.get("type", "")).strip().upper() == "DI"
    }

    if any(pair not in ast_pairs for pair in di_pairs):
        print("DI_NOT_DERIVED_FROM_AST", file=sys.stderr)
        return 1

    graph_edges: List[Dict[str, Any]] = []
    for edge in edges:
        source = str(edge.get("source", "")).strip().upper() or "AST"
        graph_edges.append(
            {
                "from": str(edge.get("from", "")).strip(),
                "to": str(edge.get("to", "")).strip(),
                "type": str(edge.get("type", "")).strip().upper() or "IMPORT",
                "confidence": float(edge.get("confidence", 1.0) or 1.0),
                "source": source,
                "source_metadata": [source],
            }
        )

    graph_edges.sort(key=lambda item: (str(item.get("from", "")), str(item.get("to", "")), str(item.get("type", "")), str(item.get("source", ""))))
    nodes_sorted = sorted(nodes, key=lambda item: str(item.get("id", "")))

    _write_json(
        output_path,
        {
            "graph": {
                "nodes": nodes_sorted,
                "edges": graph_edges,
            },
            "stats": {
                "node_count": len(nodes_sorted),
                "edge_count": len(graph_edges),
            },
        },
    )
    return 0


def _handle_layer5(parsed: Dict[str, List[str]]) -> int:
    graph_path = Path(_require(parsed, "-GraphPath")).resolve()
    output_path = Path(_require(parsed, "-OutputPath")).resolve()

    graph_doc = _load_json(graph_path)
    graph_payload = graph_doc.get("graph") if isinstance(graph_doc.get("graph"), dict) else graph_doc

    nodes = [node for node in (graph_payload.get("nodes") if isinstance(graph_payload.get("nodes"), list) else []) if isinstance(node, dict)]
    edges = [edge for edge in (graph_payload.get("edges") if isinstance(graph_payload.get("edges"), list) else []) if isinstance(edge, dict)]

    node_ids = {str(node.get("id", "")).strip() for node in nodes if str(node.get("id", "")).strip()}

    issues: List[Dict[str, Any]] = []

    unresolved_count = 0
    for edge in edges:
        src = str(edge.get("from", "")).strip()
        dst = str(edge.get("to", "")).strip()
        if src not in node_ids or dst not in node_ids:
            unresolved_count += 1

    if unresolved_count > 0:
        issues.append(
            {
                "type": "UNRESOLVED_EDGE_REFERENCE",
                "severity": "HIGH",
                "message": "Edges reference nodes that are not present in canonical nodes.",
                "count": unresolved_count,
                "sample_nodes": [],
            }
        )

    components = _node_component_map(sorted(node_ids), edges)
    disconnected_clusters = max(0, len(components) - 1)
    orphan_nodes = sum(len(component) for component in components[1:]) if disconnected_clusters > 0 else 0
    if disconnected_clusters > 0:
        sample = components[1][:25] if len(components) > 1 else []
        issues.append(
            {
                "type": "DISCONNECTED_SUBGRAPHS",
                "severity": "HIGH",
                "message": "Graph contains disconnected components not reachable from the primary cluster.",
                "count": disconnected_clusters,
                "sample_nodes": sample,
            }
        )

    by_file_path: Dict[str, Set[str]] = defaultdict(set)
    for node in nodes:
        rel = _normalize_path(str(node.get("file_path", "")))
        node_id = str(node.get("id", "")).strip()
        if rel and node_id:
            by_file_path[rel].add(node_id)

    collision_paths = [path for path, ids in by_file_path.items() if len(ids) > 1]
    if collision_paths:
        issues.append(
            {
                "type": "IDENTITY_COLLISION_NAMESPACE_FILEPATH",
                "severity": "HIGH",
                "message": "Multiple canonical node IDs map to the same file_path.",
                "count": len(collision_paths),
                "sample_nodes": sorted(collision_paths)[:25],
            }
        )

    status = "VALID" if not issues else "INVALID"
    payload = {
        "status": status,
        "critical_failure": status != "VALID",
        "system_valid": status == "VALID",
        "trust_score": 1.0 if status == "VALID" else 0.0,
        "issues": issues,
        "warnings": [],
        "metrics": {
            "orphan_nodes": orphan_nodes,
            "disconnected_clusters": disconnected_clusters,
            "di_nodes_missing_edges": unresolved_count,
        },
    }
    _write_json(output_path, payload)

    fail_on_invalid = _parse_bool(_optional(parsed, "-FailOnInvalid", "false"))
    if fail_on_invalid and status != "VALID":
        return 1
    return 0


def _handle_validate_graph_structure(parsed: Dict[str, List[str]]) -> int:
    graph_path = Path(_require(parsed, "-GraphPath")).resolve()
    inventory_path = Path(_require(parsed, "-InventoryPath")).resolve()
    output_path = Path(_require(parsed, "-OutputPath")).resolve()

    graph_doc = _load_json(graph_path)
    graph_payload = graph_doc.get("graph") if isinstance(graph_doc.get("graph"), dict) else graph_doc
    inventory_doc = _load_json(inventory_path)

    files = inventory_doc.get("files") if isinstance(inventory_doc.get("files"), list) else []
    known_files = {
        _normalize_path(str(item.get("file_id") or item.get("path") or ""))
        for item in files
        if isinstance(item, dict)
    }

    nodes = [node for node in (graph_payload.get("nodes") if isinstance(graph_payload.get("nodes"), list) else []) if isinstance(node, dict)]
    missing = []
    for node in nodes:
        rel = _normalize_path(str(node.get("file_path", "")))
        if rel and rel not in known_files:
            missing.append(rel)

    status = "PASS" if not missing else "FAIL"
    payload = {
        "status": status,
        "issues": [] if not missing else [
            {
                "type": "MISSING_INVENTORY_FILEPATH",
                "severity": "HIGH",
                "message": "Graph node file_path is missing from inventory.",
                "count": len(set(missing)),
                "sample_nodes": sorted(set(missing))[:25],
            }
        ],
        "metrics": {
            "missing_inventory_paths": len(set(missing)),
        },
    }
    _write_json(output_path, payload)
    return 0 if status == "PASS" else 1


def _edge_pairs_from_resolver(edges_doc: Dict[str, Any], source: str) -> Set[Tuple[str, str]]:
    rows = edges_doc.get("edges") if isinstance(edges_doc.get("edges"), list) else []
    out: Set[Tuple[str, str]] = set()
    for row in rows:
        payload = row if isinstance(row, dict) else {}
        row_source = str(payload.get("source", "")).strip().upper()
        row_type = str(payload.get("type", "")).strip().upper()
        src = str(payload.get("from", "")).strip()
        dst = str(payload.get("to", "")).strip()
        if not src or not dst:
            continue
        if source == "AST" and row_source == "AST":
            out.add((src, dst))
        elif source == "DI" and (row_source == "DI" or row_type == "DI"):
            out.add((src, dst))
    return out


def _handle_compare_resolvers(parsed: Dict[str, List[str]]) -> int:
    graph_path = Path(_require(parsed, "-GraphPath")).resolve()
    edges_path = Path(_require(parsed, "-EdgesPath")).resolve()
    output_path = Path(_require(parsed, "-OutputPath")).resolve()

    graph_doc = _load_json(graph_path)
    graph_payload = graph_doc.get("graph") if isinstance(graph_doc.get("graph"), dict) else graph_doc
    edges_doc = _load_json(edges_path)

    graph_edges = [edge for edge in (graph_payload.get("edges") if isinstance(graph_payload.get("edges"), list) else []) if isinstance(edge, dict)]
    graph_import_pairs = {
        (str(edge.get("from", "")).strip(), str(edge.get("to", "")).strip())
        for edge in graph_edges
        if str(edge.get("type", "")).strip().upper() == "IMPORT"
    }

    ast_pairs = _edge_pairs_from_resolver(edges_doc, "AST")
    di_pairs = _edge_pairs_from_resolver(edges_doc, "DI")

    missing_ast_edges = sorted(ast_pairs - graph_import_pairs)
    di_not_from_ast = sorted(di_pairs - ast_pairs)

    disagreements: List[Dict[str, Any]] = []
    for src, dst in di_not_from_ast:
        disagreements.append(
            {
                "issue": "DI_NOT_DERIVED_FROM_AST",
                "severity": "HIGH",
                "from": src,
                "to": dst,
            }
        )
    for src, dst in missing_ast_edges:
        disagreements.append(
            {
                "issue": "AST_EDGE_MISSING_FROM_GRAPH",
                "severity": "HIGH",
                "from": src,
                "to": dst,
            }
        )

    denominator = max(1, len(ast_pairs | di_pairs | set(missing_ast_edges)))
    drift_score = round(float(len(set(di_not_from_ast) | set(missing_ast_edges))) / float(denominator), 3)

    payload = {
        "drift_score": drift_score,
        "missing_ast_edges": [
            {
                "from": src,
                "to": dst,
            }
            for src, dst in missing_ast_edges
        ],
        "disagreements": disagreements,
    }
    _write_json(output_path, payload)

    threshold = float(_optional(parsed, "-DriftThreshold", "0") or 0)
    if drift_score > threshold or any(str(item.get("severity", "")).upper() == "HIGH" for item in disagreements):
        return 1
    return 0


def _directed_reachable(entrypoints: Sequence[str], edges: Sequence[Dict[str, Any]]) -> Set[str]:
    adjacency: Dict[str, Set[str]] = defaultdict(set)
    for edge in edges:
        payload = edge if isinstance(edge, dict) else {}
        src = str(payload.get("from", "")).strip()
        dst = str(payload.get("to", "")).strip()
        if src and dst:
            adjacency[src].add(dst)

    reachable: Set[str] = set()
    queue: deque[str] = deque([entry for entry in entrypoints if entry])
    for entry in entrypoints:
        if entry:
            reachable.add(entry)

    while queue:
        current = queue.popleft()
        for nxt in sorted(adjacency.get(current, set())):
            if nxt in reachable:
                continue
            reachable.add(nxt)
            queue.append(nxt)

    return reachable


def _handle_semantic_validate(parsed: Dict[str, List[str]]) -> int:
    graph_path = Path(_require(parsed, "-GraphPath")).resolve()
    output_path = Path(_require(parsed, "-OutputPath")).resolve()

    graph_doc = _load_json(graph_path)
    graph_payload = graph_doc.get("graph") if isinstance(graph_doc.get("graph"), dict) else graph_doc

    nodes = [node for node in (graph_payload.get("nodes") if isinstance(graph_payload.get("nodes"), list) else []) if isinstance(node, dict)]
    edges = [edge for edge in (graph_payload.get("edges") if isinstance(graph_payload.get("edges"), list) else []) if isinstance(edge, dict)]

    node_ids = {str(node.get("id", "")).strip() for node in nodes if str(node.get("id", "")).strip()}

    entrypoints_raw = _many(parsed, "-Entrypoints")
    entrypoints = [entry for entry in entrypoints_raw if entry in node_ids]

    components = _node_component_map(sorted(node_ids), edges)
    components_with_entrypoint = [component for component in components if any(node in set(entrypoints) for node in component)]
    disconnected_islands = [component for component in components if component not in components_with_entrypoint]

    reachable = _directed_reachable(entrypoints, edges)

    anomalies: List[Dict[str, Any]] = []
    if disconnected_islands:
        anomalies.append(
            {
                "type": "DISCONNECTED_ISLANDS",
                "severity": "HIGH",
                "message": "Graph contains disconnected island components.",
                "count": len(disconnected_islands),
            }
        )
        anomalies.append(
            {
                "type": "FALSE_HEALTHY_SUBGRAPHS",
                "severity": "HIGH",
                "message": "Disconnected components can appear healthy without entrypoint reachability.",
                "count": len(disconnected_islands),
            }
        )

    payload = {
        "status": "FAIL" if anomalies else "PASS",
        "anomalies": anomalies,
        "entrypoints_used": entrypoints,
        "reachable_nodes": sorted(reachable),
        "disconnected_islands": [node for component in disconnected_islands for node in component],
        "false_healthy_subgraphs": [component[0] for component in disconnected_islands if component],
    }
    _write_json(output_path, payload)
    return 1 if anomalies else 0


def _handle_aggregate_trust(parsed: Dict[str, List[str]]) -> int:
    structural_path = Path(_require(parsed, "-StructuralValidationPath")).resolve()
    reachability_path = Path(_require(parsed, "-ReachabilityValidationPath")).resolve()
    resolver_path = Path(_require(parsed, "-ResolverConsistencyPath")).resolve()
    semantic_path = Path(_require(parsed, "-SemanticValidationPath")).resolve()
    output_path = Path(_require(parsed, "-OutputPath")).resolve()

    structural = _load_json(structural_path)
    reachability = _load_json(reachability_path)
    resolver = _load_json(resolver_path)
    semantic = _load_json(semantic_path)

    structural_fail = str(structural.get("status", "")).upper() not in {"PASS", "VALID"}
    resolver_fail = float(resolver.get("drift_score", 0.0) or 0.0) > 0.0 or bool(resolver.get("disagreements"))
    semantic_fail = bool(semantic.get("anomalies"))
    reachability_fail = bool(reachability.get("false_dead_nodes"))

    trust_score = 0.0 if any([structural_fail, resolver_fail, semantic_fail, reachability_fail]) else 1.0

    payload = {
        "status": "FAIL" if trust_score == 0.0 else "PASS",
        "trust_score": trust_score,
        "breakdown": {
            "structural_validation": 0.0 if structural_fail else 1.0,
            "resolver_consistency": 0.0 if resolver_fail else 1.0,
            "semantic_validation": 0.0 if semantic_fail else 1.0,
            "reachability_validation": 0.0 if reachability_fail else 1.0,
        },
    }
    _write_json(output_path, payload)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print("Missing stage command.", file=sys.stderr)
        return 2

    command = str(args[0]).strip().lower()
    parsed = _parse_stage_args(args[1:])

    handlers = {
        "layer1-inventory": _handle_layer1,
        "layer2-canonical": _handle_layer2,
        "layer3-resolve": _handle_layer3,
        "layer4-graph": _handle_layer4,
        "layer5-validate": _handle_layer5,
        "validate-graph-structure": _handle_validate_graph_structure,
        "compare-resolvers": _handle_compare_resolvers,
        "semantic-validate": _handle_semantic_validate,
        "aggregate-trust": _handle_aggregate_trust,
    }

    handler = handlers.get(command)
    if handler is None:
        print(f"Unsupported stage command: {command}", file=sys.stderr)
        return 2

    try:
        return int(handler(parsed))
    except Exception as exc:  # noqa: BLE001
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
