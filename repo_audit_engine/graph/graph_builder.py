from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from repo_audit_engine.io.artifacts import write_json


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
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
                yield payload


def _canonical_id(node_kind: str, rel_path: str, name: str = "") -> str:
    normalized_path = rel_path.strip().replace("\\", "/")
    if name:
        normalized_name = name.strip().replace("/", "_")
        return f"canonical://{node_kind}/{normalized_path}/{normalized_name}"
    return f"canonical://{node_kind}/{normalized_path}"


def _node_id(node_kind: str, rel_path: str, name: str = "") -> str:
    normalized_path = str(rel_path or "").strip().replace("\\", "/")
    if normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]

    normalized_name = str(name or "").strip()
    if name:
        return f"{node_kind}:{normalized_path}:{normalized_name}"
    return f"{node_kind}:{normalized_path}"


def _edge_tuple(source: str, target: str, edge_type: str, evidence: str) -> Tuple[str, str, str, str]:
    return (source, target, edge_type.upper(), evidence)


def build_dependency_graph(
    manifest_path: Path,
    static_analysis_path: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    graph_path = out_root / "dependency_graph.json"
    summary_path = out_root / "dependency_graph_summary.json"

    file_nodes: Dict[str, Dict[str, Any]] = {}
    symbol_nodes: Dict[str, Dict[str, Any]] = {}
    edges: set[Tuple[str, str, str, str]] = set()

    for row in _iter_jsonl(manifest_path):
        rel_path = str(row.get("path", "")).strip()
        language = str(row.get("language", "")).strip()
        if not rel_path:
            continue
        if language != "python":
            continue

        node_id = _node_id("file", rel_path)
        file_nodes[node_id] = {
            "id": node_id,
            "kind": "file",
            "path": rel_path,
            "canonical_id": _canonical_id("file", rel_path),
        }

    symbols_by_file: Dict[str, set[str]] = {}

    for row in _iter_jsonl(static_analysis_path):
        rel_path = str(row.get("file_path", "")).strip()
        if not rel_path:
            continue

        symbols_for_file = symbols_by_file.setdefault(rel_path, set())

        for function in row.get("functions", []) or []:
            item = function if isinstance(function, dict) else {}
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            node_id = _node_id("function", rel_path, name)
            symbol_nodes[node_id] = {
                "id": node_id,
                "kind": "function",
                "path": rel_path,
                "name": name,
                "canonical_id": _canonical_id("function", rel_path, name),
            }
            symbols_for_file.add(name)

        for klass in row.get("classes", []) or []:
            item = klass if isinstance(klass, dict) else {}
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            node_id = _node_id("class", rel_path, name)
            symbol_nodes[node_id] = {
                "id": node_id,
                "kind": "class",
                "path": rel_path,
                "name": name,
                "canonical_id": _canonical_id("class", rel_path, name),
            }
            symbols_for_file.add(name)

    for row in _iter_jsonl(static_analysis_path):
        rel_path = str(row.get("file_path", "")).strip()
        if not rel_path:
            continue

        source_file_id = _node_id("file", rel_path)
        if source_file_id not in file_nodes:
            continue

        local_symbols = symbols_by_file.get(rel_path, set())

        for item in row.get("imports", []) or []:
            payload = item if isinstance(item, dict) else {}
            resolved_path = str(payload.get("resolved_path", "")).strip()
            if not resolved_path:
                continue
            target_file_id = _node_id("file", resolved_path)
            if target_file_id not in file_nodes:
                continue
            edges.add(_edge_tuple(source_file_id, target_file_id, "IMPORT", "static_import"))

        for item in row.get("calls", []) or []:
            payload = item if isinstance(item, dict) else {}
            caller = str(payload.get("caller", "<module>")).strip() or "<module>"
            callee = str(payload.get("callee", "")).strip()
            resolved_node_id = str(payload.get("resolved_node_id", "")).strip()

            if caller == "<module>":
                caller_node_id = source_file_id
            else:
                caller_name = caller.split(".")[-1]
                if caller_name in local_symbols:
                    candidate_function_id = _node_id("function", rel_path, caller_name)
                    candidate_class_id = _node_id("class", rel_path, caller_name)
                    if candidate_function_id in symbol_nodes:
                        caller_node_id = candidate_function_id
                    elif candidate_class_id in symbol_nodes:
                        caller_node_id = candidate_class_id
                    else:
                        caller_node_id = source_file_id
                else:
                    caller_node_id = source_file_id

            target_node_id = ""
            if resolved_node_id and resolved_node_id in symbol_nodes:
                target_node_id = resolved_node_id
            elif callee:
                callee_name = callee.split(".")[-1]
                local_function_id = _node_id("function", rel_path, callee_name)
                local_class_id = _node_id("class", rel_path, callee_name)
                if local_function_id in symbol_nodes:
                    target_node_id = local_function_id
                elif local_class_id in symbol_nodes:
                    target_node_id = local_class_id

            if target_node_id:
                edges.add(_edge_tuple(caller_node_id, target_node_id, "CALL", "static_call"))

        for raw_ref in row.get("config_references", []) or []:
            value = str(raw_ref).strip().replace("\\", "/")
            if not value:
                continue
            if value.startswith("./"):
                value = value[2:]
            target_file_id = _node_id("file", value)
            if target_file_id in file_nodes:
                edges.add(_edge_tuple(source_file_id, target_file_id, "CONFIG", "config_reference"))

    nodes = list(file_nodes.values()) + list(symbol_nodes.values())
    nodes.sort(key=lambda item: (str(item.get("kind", "")), str(item.get("id", ""))))

    edge_rows: List[Dict[str, Any]] = []
    for source, target, edge_type, evidence in sorted(edges, key=lambda item: (item[2], item[0], item[1], item[3])):
        edge_rows.append(
            {
                "source": source,
                "target": target,
                "type": edge_type,
                "evidence": evidence,
            }
        )

    canonical_node_map = {str(item.get("id", "")): str(item.get("canonical_id", "")) for item in nodes}

    validation_nodes = [
        {"id": str(item.get("canonical_id", ""))}
        for item in nodes
        if str(item.get("canonical_id", "")).startswith("canonical://")
    ]

    validation_edges: List[Dict[str, Any]] = []
    resolver_edges: List[Dict[str, Any]] = []
    for edge in edge_rows:
        source_id = str(edge.get("source", ""))
        target_id = str(edge.get("target", ""))
        source_canonical = canonical_node_map.get(source_id, "")
        target_canonical = canonical_node_map.get(target_id, "")
        edge_type = str(edge.get("type", "")).upper()

        if not source_canonical or not target_canonical:
            continue

        validation_edge_type = "DYNAMIC"
        resolver_source = "AST"

        if edge_type == "IMPORT":
            validation_edge_type = "IMPORT"
            resolver_source = "AST"
        elif edge_type == "CALL":
            validation_edge_type = "DI"
            resolver_source = "DI"
        elif edge_type == "CONFIG":
            validation_edge_type = "CONFIG"
            resolver_source = "CONFIG"

        validation_edges.append(
            {
                "from": source_canonical,
                "to": target_canonical,
                "type": validation_edge_type,
                "confidence": 1.0,
            }
        )

        resolver_edges.append(
            {
                "from": source_canonical,
                "to": target_canonical,
                "type": validation_edge_type,
                "source": resolver_source,
            }
        )

    validation_edges.sort(key=lambda item: (str(item.get("type", "")), str(item.get("from", "")), str(item.get("to", ""))))
    resolver_edges.sort(key=lambda item: (str(item.get("source", "")), str(item.get("from", "")), str(item.get("to", "")), str(item.get("type", ""))))

    graph_payload = {
        "nodes": nodes,
        "edges": edge_rows,
        "validation_graph": {
            "nodes": validation_nodes,
            "edges": validation_edges,
        },
        "resolver_data": {
            "edges": resolver_edges,
        },
        "summary": {
            "node_count": len(nodes),
            "file_node_count": len(file_nodes),
            "function_class_node_count": len(symbol_nodes),
            "edge_count": len(edge_rows),
            "import_edge_count": len([edge for edge in edge_rows if str(edge.get("type", "")) == "IMPORT"]),
            "call_edge_count": len([edge for edge in edge_rows if str(edge.get("type", "")) == "CALL"]),
            "config_edge_count": len([edge for edge in edge_rows if str(edge.get("type", "")) == "CONFIG"]),
        },
    }

    write_json(graph_path, graph_payload, pretty=True)
    write_json(summary_path, graph_payload["summary"], pretty=True)

    return {
        "graph_path": str(graph_path),
        "summary_path": str(summary_path),
        "graph": graph_payload,
    }
