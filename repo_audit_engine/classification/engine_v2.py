from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Sequence, Set

from repo_audit_engine.graph.graph_utils import node_id as build_node_id
from repo_audit_engine.io.artifacts import load_json, write_json


class EvidenceClassifier:
    HOT_THRESHOLD = 0.8
    WARM_THRESHOLD = 0.3
    COLD_THRESHOLD = 0.1

    def classify(
        self,
        dependency_graph: Mapping[str, Any],
        execution_flow_graph: Mapping[str, Any],
        runtime_trace_rows: Iterable[Mapping[str, Any]],
        manifest: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        dependency_payload = self._unwrap_payload(dependency_graph, "graph")
        flow_payload = self._unwrap_payload(execution_flow_graph, "flow_graph")
        manifest_payload = manifest if isinstance(manifest, Mapping) else {}

        runtime_hits = self._collect_runtime_hits(runtime_trace_rows)
        runtime_reachable = self._collect_runtime_reachable_nodes(flow_payload, runtime_hits)

        evidence = self._collect_reference_evidence(dependency_payload)

        all_nodes = self._collect_all_nodes(
            dependency_payload=dependency_payload,
            flow_payload=flow_payload,
            runtime_hits=runtime_hits,
            manifest_payload=manifest_payload,
        )

        rows: List[Dict[str, Any]] = []
        distribution = {"HOT": 0, "WARM": 0, "COLD": 0, "DEAD": 0}

        for node_id in sorted(all_nodes):
            runtime_hit_count = int(runtime_hits.get(node_id, 0))
            reachable_from_runtime = bool(node_id in runtime_reachable)
            executable_references = int(evidence["executable_references"].get(node_id, 0))
            non_executable_references = int(evidence["non_executable_references"].get(node_id, 0))
            inbound_edges = int(evidence["inbound_edges"].get(node_id, 0))
            outbound_edges = int(evidence["outbound_edges"].get(node_id, 0))

            score = 0.0
            if runtime_hit_count > 0:
                score += 1.0
            if reachable_from_runtime:
                score += 0.7
            if executable_references > 0:
                score += 0.5
            if non_executable_references > 0:
                score += 0.2
            score = round(max(0.0, min(1.0, score)), 3)

            classification = self._classification_from_score(score)
            adjustments: List[str] = []

            if runtime_hit_count <= 0 and not reachable_from_runtime:
                classification = "DEAD"
                adjustments.append("no_runtime_evidence_authority_dead")

            if classification == "DEAD" and executable_references > 0:
                classification = "COLD"
                adjustments.append("dead_with_executable_references_reclassified_to_cold")

            if classification == "HOT" and runtime_hit_count <= 0:
                classification = "WARM"
                adjustments.append("hot_without_runtime_hits_downgraded_to_warm")

            weak_dead_signal = bool(classification == "DEAD" and non_executable_references > 0)

            distribution[classification] = int(distribution.get(classification, 0)) + 1

            row = {
                "node_id": node_id,
                "id": node_id,
                "classification": classification,
                "heat": classification,
                "score": score,
                "evidence": {
                    "runtime_hits": runtime_hit_count,
                    "reachable_from_runtime": reachable_from_runtime,
                    "executable_references": executable_references,
                    "non_executable_references": non_executable_references,
                },
                # Compatibility fields consumed by existing reporting/validation paths.
                "runtime_hits": runtime_hit_count,
                "inbound_edges": inbound_edges,
                "outbound_edges": outbound_edges,
                "executable_references": executable_references,
                "non_executable_references": non_executable_references,
                "ast_references": executable_references + non_executable_references,
            }
            if adjustments:
                row["adjustments"] = adjustments
            if weak_dead_signal:
                row["weak_dead_signal"] = True

            rows.append(row)

        return {
            "classifier": "EvidenceClassifier",
            "schema_version": "2.0",
            "nodes": rows,
            "distribution": distribution,
        }

    def classify_from_artifacts(
        self,
        dependency_graph_path: Path,
        output_dir: Path,
        execution_flow_graph_path: Path | None = None,
        runtime_trace_path: Path | None = None,
        manifest_path: Path | None = None,
    ) -> Dict[str, Any]:
        dependency_payload = load_json(dependency_graph_path) if dependency_graph_path.exists() else {}

        flow_payload: Dict[str, Any] = {}
        if execution_flow_graph_path and execution_flow_graph_path.exists():
            flow_payload = load_json(execution_flow_graph_path)

        manifest_payload = self._load_manifest_payload(manifest_path)

        trace_rows: Iterable[Dict[str, Any]] = []
        if runtime_trace_path and runtime_trace_path.exists():
            trace_rows = self._iter_jsonl(runtime_trace_path)

        heat_payload = self.classify(
            dependency_graph=dependency_payload if isinstance(dependency_payload, Mapping) else {},
            execution_flow_graph=flow_payload if isinstance(flow_payload, Mapping) else {},
            runtime_trace_rows=trace_rows,
            manifest=manifest_payload,
        )

        out_root = output_dir.resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        heat_path = out_root / "heat_classification.json"
        write_json(heat_path, heat_payload, pretty=True)

        return {
            "heat_path": str(heat_path),
            "heat": heat_payload,
        }

    def _collect_runtime_hits(self, runtime_trace_rows: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
        hits: Dict[str, int] = {}

        for item in runtime_trace_rows:
            row = item if isinstance(item, Mapping) else {}
            if str(row.get("event", "")).strip().lower() != "call":
                continue

            node_id = self._node_id_from_runtime_row(row)
            if not node_id:
                continue

            hits[node_id] = int(hits.get(node_id, 0)) + 1

        return hits

    def _collect_runtime_reachable_nodes(
        self,
        execution_flow_graph: Mapping[str, Any],
        runtime_hits: Mapping[str, int],
    ) -> Set[str]:
        nodes = execution_flow_graph.get("nodes") if isinstance(execution_flow_graph.get("nodes"), list) else []
        edges = execution_flow_graph.get("edges") if isinstance(execution_flow_graph.get("edges"), list) else []

        runtime_nodes: Set[str] = set()
        adjacency: Dict[str, Set[str]] = {}
        inbound_count: Dict[str, int] = {}

        for item in nodes:
            payload = item if isinstance(item, Mapping) else {}
            node_id = self._normalize_node_id(payload.get("id", ""))
            if node_id:
                runtime_nodes.add(node_id)

        for edge in edges:
            payload = edge if isinstance(edge, Mapping) else {}
            edge_type = str(payload.get("type", "")).strip().upper()
            if edge_type != "RUNTIME_CALL":
                continue

            source = self._normalize_node_id(payload.get("source", ""))
            target = self._normalize_node_id(payload.get("target", ""))
            if not source or not target:
                continue

            runtime_nodes.add(source)
            runtime_nodes.add(target)

            adjacency.setdefault(source, set()).add(target)
            inbound_count[target] = int(inbound_count.get(target, 0)) + 1

        for node_id, count in runtime_hits.items():
            if int(count) > 0:
                runtime_nodes.add(str(node_id))

        if not runtime_nodes:
            return set()

        roots = sorted(node for node in runtime_nodes if int(inbound_count.get(node, 0)) == 0)
        if not roots:
            roots = sorted(runtime_nodes)

        visited: Set[str] = set()
        queue: deque[str] = deque(roots)
        visited.update(roots)

        while queue:
            current = queue.popleft()
            for target in sorted(adjacency.get(current, set())):
                if target in visited:
                    continue
                visited.add(target)
                queue.append(target)

        return visited

    def _collect_reference_evidence(self, dependency_graph: Mapping[str, Any]) -> Dict[str, Dict[str, int]]:
        edges = dependency_graph.get("edges") if isinstance(dependency_graph.get("edges"), list) else []

        inbound_edges: Dict[str, int] = {}
        outbound_edges: Dict[str, int] = {}
        executable_references: Dict[str, int] = {}
        non_executable_references: Dict[str, int] = {}

        for edge in edges:
            payload = edge if isinstance(edge, Mapping) else {}
            source = self._normalize_node_id(payload.get("source", payload.get("from", "")))
            target = self._normalize_node_id(payload.get("target", payload.get("to", "")))
            if not source or not target:
                continue

            outbound_edges[source] = int(outbound_edges.get(source, 0)) + 1
            inbound_edges[target] = int(inbound_edges.get(target, 0)) + 1

            edge_type = str(payload.get("type", "")).strip().upper()
            if edge_type == "CALL":
                executable_references[target] = int(executable_references.get(target, 0)) + 1
            else:
                non_executable_references[target] = int(non_executable_references.get(target, 0)) + 1

        return {
            "inbound_edges": inbound_edges,
            "outbound_edges": outbound_edges,
            "executable_references": executable_references,
            "non_executable_references": non_executable_references,
        }

    def _collect_all_nodes(
        self,
        dependency_payload: Mapping[str, Any],
        flow_payload: Mapping[str, Any],
        runtime_hits: Mapping[str, int],
        manifest_payload: Mapping[str, Any],
    ) -> Set[str]:
        node_ids: Set[str] = set()

        dependency_nodes = dependency_payload.get("nodes") if isinstance(dependency_payload.get("nodes"), list) else []
        dependency_edges = dependency_payload.get("edges") if isinstance(dependency_payload.get("edges"), list) else []

        for item in dependency_nodes:
            payload = item if isinstance(item, Mapping) else {}
            normalized = self._normalize_node_id(payload.get("id", ""))
            if normalized:
                node_ids.add(normalized)

        for edge in dependency_edges:
            payload = edge if isinstance(edge, Mapping) else {}
            source = self._normalize_node_id(payload.get("source", payload.get("from", "")))
            target = self._normalize_node_id(payload.get("target", payload.get("to", "")))
            if source:
                node_ids.add(source)
            if target:
                node_ids.add(target)

        flow_nodes = flow_payload.get("nodes") if isinstance(flow_payload.get("nodes"), list) else []
        flow_edges = flow_payload.get("edges") if isinstance(flow_payload.get("edges"), list) else []

        for item in flow_nodes:
            payload = item if isinstance(item, Mapping) else {}
            normalized = self._normalize_node_id(payload.get("id", ""))
            if normalized:
                node_ids.add(normalized)

        for edge in flow_edges:
            payload = edge if isinstance(edge, Mapping) else {}
            source = self._normalize_node_id(payload.get("source", ""))
            target = self._normalize_node_id(payload.get("target", ""))
            if source:
                node_ids.add(source)
            if target:
                node_ids.add(target)

        for node_id in runtime_hits.keys():
            normalized = self._normalize_node_id(node_id)
            if normalized:
                node_ids.add(normalized)

        entrypoints = manifest_payload.get("entrypoints") if isinstance(manifest_payload.get("entrypoints"), list) else []
        for entrypoint in entrypoints:
            normalized_path = self._normalize_path(entrypoint)
            if not normalized_path:
                continue
            node_ids.add(build_node_id("file", normalized_path))

        return node_ids

    def _node_id_from_runtime_row(self, row: Mapping[str, Any]) -> str:
        direct_node_id = self._normalize_node_id(row.get("callee_node_id", row.get("node", "")))
        if direct_node_id:
            return direct_node_id

        file_path = self._normalize_path(row.get("file", ""))
        if not file_path or file_path.startswith("<"):
            return ""

        function_name = str(row.get("function", "")).strip()
        if function_name and function_name not in {"<module>", "<import>"}:
            return build_node_id("function", file_path, function_name)
        return build_node_id("file", file_path)

    def _classification_from_score(self, score: float) -> str:
        if score >= self.HOT_THRESHOLD:
            return "HOT"
        if score >= self.WARM_THRESHOLD:
            return "WARM"
        if score >= self.COLD_THRESHOLD:
            return "COLD"
        return "DEAD"

    def _normalize_node_id(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw or raw.startswith("canonical://"):
            return ""
        if ":" not in raw:
            return ""

        kind, payload = raw.split(":", 1)
        node_kind = str(kind).strip().lower()
        node_payload = str(payload).strip()

        if node_kind == "file":
            path_text = self._normalize_path(node_payload)
            if not path_text:
                return ""
            return build_node_id("file", path_text)

        if node_kind in {"function", "class"}:
            rel_path, separator, name = node_payload.rpartition(":")
            if not separator:
                return ""
            normalized_path = self._normalize_path(rel_path)
            normalized_name = str(name).strip()
            if not normalized_path or not normalized_name:
                return ""
            return build_node_id(node_kind, normalized_path, normalized_name)

        return ""

    def _normalize_path(self, value: Any) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    def _unwrap_payload(self, payload: Mapping[str, Any], nested_key: str) -> Mapping[str, Any]:
        if isinstance(payload.get(nested_key), Mapping):
            nested = payload.get(nested_key)
            if isinstance(nested, Mapping):
                return nested
        return payload

    def _load_manifest_payload(self, manifest_path: Path | None) -> Dict[str, Any]:
        if not manifest_path or not manifest_path.exists():
            return {}

        if manifest_path.suffix.lower() == ".jsonl":
            entrypoints: Set[str] = set()
            for row in self._iter_jsonl(manifest_path):
                reasons = row.get("entrypoint_reasons") if isinstance(row.get("entrypoint_reasons"), list) else []
                path_text = self._normalize_path(row.get("path", ""))
                if path_text and reasons:
                    entrypoints.add(path_text)
            return {"entrypoints": sorted(entrypoints)}

        payload = load_json(manifest_path)
        return payload if isinstance(payload, dict) else {}

    def _iter_jsonl(self, path: Path) -> Iterator[Dict[str, Any]]:
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
