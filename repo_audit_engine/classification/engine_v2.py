from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Set

from repo_audit_engine.classification.scoring import compute_heat_score
from repo_audit_engine.graph.graph_utils import canonical_id as build_canonical_id, node_id as build_node_id
from repo_audit_engine.io.artifacts import load_json, write_json


class ClassificationError(RuntimeError):
    pass


class EvidenceClassifier:
    HOT_THRESHOLD = 0.8
    WARM_THRESHOLD = 0.3
    COLD_THRESHOLD = 0.1
    MIN_RUNTIME_CALL_EVENTS = 50
    MIN_RUNTIME_UNIQUE_CALLEES = 10
    MIN_RUNTIME_MAX_CALL_DEPTH = 3
    MIN_RUNTIME_LOCAL_CALLS = 1
    MIN_COVERAGE_RATIO = 0.02
    MIN_REACHABLE_RATIO = 0.05
    MAX_DEAD_RATIO_WITH_RUNTIME = 0.70

    def classify(
        self,
        dependency_graph: Mapping[str, Any],
        execution_flow_graph: Mapping[str, Any],
        runtime_trace_rows: Iterable[Mapping[str, Any]],
        manifest: Mapping[str, Any] | None = None,
        runtime_source: str = "runtime_trace",
        enforce_runtime_signal: bool = True,
        runtime_requirements: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        dependency_payload = self._unwrap_payload(dependency_graph, "graph")
        flow_payload = self._unwrap_payload(execution_flow_graph, "flow_graph")
        manifest_payload = manifest if isinstance(manifest, Mapping) else {}
        trace_rows = [item for item in runtime_trace_rows if isinstance(item, Mapping)]

        canonical_lookup = self._build_canonical_lookup(dependency_payload, flow_payload)

        runtime_analysis = self._analyze_runtime_trace_rows(trace_rows, canonical_lookup)
        runtime_hits = dict(runtime_analysis.get("runtime_hits", {}))
        runtime_reachable = self._collect_runtime_reachable_nodes(flow_payload, runtime_hits, canonical_lookup)

        evidence = self._collect_reference_evidence(dependency_payload, canonical_lookup)
        reconciliation = self._runtime_static_reconciliation(dependency_payload, flow_payload, canonical_lookup)

        all_nodes = self._collect_all_nodes(
            dependency_payload=dependency_payload,
            flow_payload=flow_payload,
            runtime_hits=runtime_hits,
            manifest_payload=manifest_payload,
            canonical_lookup=canonical_lookup,
        )

        runtime_validation = self._validate_runtime_signal(
            runtime_analysis=runtime_analysis,
            runtime_hits=runtime_hits,
            runtime_reachable=runtime_reachable,
            all_nodes=all_nodes,
            manifest_payload=manifest_payload,
            runtime_source=runtime_source,
            reconciliation=reconciliation,
            runtime_requirements=runtime_requirements,
            enforce_runtime_signal=enforce_runtime_signal,
        )

        max_runtime_hits = max((int(value) for value in runtime_hits.values()), default=0)
        max_reference_count = max(
            (
                int(evidence["executable_references"].get(node_id, 0))
                + int(evidence["non_executable_references"].get(node_id, 0))
                + int(evidence["inbound_edges"].get(node_id, 0))
            )
            for node_id in all_nodes
        ) if all_nodes else 0

        rows: List[Dict[str, Any]] = []
        distribution = {"HOT": 0, "WARM": 0, "COLD": 0, "DEAD": 0}

        for node_id in sorted(all_nodes):
            runtime_hit_count = int(runtime_hits.get(node_id, 0))
            reachable_from_runtime = bool(node_id in runtime_reachable)
            executable_references = int(evidence["executable_references"].get(node_id, 0))
            non_executable_references = int(evidence["non_executable_references"].get(node_id, 0))
            inbound_edges = int(evidence["inbound_edges"].get(node_id, 0))
            outbound_edges = int(evidence["outbound_edges"].get(node_id, 0))

            runtime_signal = self._normalized_count(runtime_hit_count, max_runtime_hits)
            reachability_signal = 1.0 if reachable_from_runtime else 0.0
            reference_total = executable_references + non_executable_references + inbound_edges
            reference_signal = self._normalized_count(reference_total, max_reference_count)
            score = compute_heat_score(runtime_signal, reachability_signal, reference_signal)

            classification = self._classification_from_score(score)
            adjustments: List[str] = []

            if runtime_hit_count == 0 and executable_references == 0 and inbound_edges == 0:
                classification = "DEAD"
                adjustments.append("no_runtime_executable_or_inbound_evidence_dead")

            if classification == "HOT" and runtime_hit_count <= 0:
                classification = "WARM"
                adjustments.append("hot_without_runtime_hits_downgraded_to_warm")

            if classification == "DEAD" and (
                runtime_hit_count > 0
                or executable_references > 0
                or reachable_from_runtime
                or inbound_edges > 0
            ):
                classification = "COLD"
                adjustments.append("dead_conflict_downgraded_to_cold")

            if classification == "DEAD" and inbound_edges > 0:
                classification = "COLD"
                adjustments.append("dead_inbound_edge_hard_guardrail")

            weak_dead_signal = bool(classification == "DEAD" and non_executable_references > 0)

            graph_strength = self._clamp01((0.7 if reachable_from_runtime else 0.0) + (0.3 if inbound_edges > 0 else 0.0))
            static_strength = self._clamp01((0.7 if executable_references > 0 else 0.0) + (0.3 if non_executable_references > 0 else 0.0))
            confidence = self._clamp01((runtime_signal * 0.5) + (graph_strength * 0.25) + (static_strength * 0.25))

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
                "confidence": round(confidence, 3),
                "evidence_strength": {
                    "runtime": round(runtime_signal, 3),
                    "graph": round(graph_strength, 3),
                    "static": round(static_strength, 3),
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

        runtime_hit_total = sum(int(value) for value in runtime_hits.values())
        hot_count = int(distribution.get("HOT", 0))
        warm_count = int(distribution.get("WARM", 0))
        if hot_count == 0 and warm_count == 0:
            raise ClassificationError(
                "No active code detected - runtime signal invalid or mapping broken"
            )

        if runtime_hit_total > 0 and hot_count == 0 and warm_count == 0:
            raise ClassificationError(
                "Classification sanity check failed: runtime evidence exists but no HOT/WARM nodes were produced."
            )

        dead_ratio = self._safe_divide(int(distribution.get("DEAD", 0)), max(1, len(rows)))
        if bool(runtime_validation.get("passed", False)) and dead_ratio > self.MAX_DEAD_RATIO_WITH_RUNTIME:
            raise ClassificationError(
                "Classification sanity check failed: DEAD ratio exceeds threshold despite valid runtime signal."
            )

        return {
            "classifier": "EvidenceClassifier",
            "schema_version": "2.1",
            "runtime_source": str(runtime_source or "unknown"),
            "runtime_validation": runtime_validation,
            "runtime_static_reconciliation": reconciliation,
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
        runtime_source: str | None = None,
        enforce_runtime_signal: bool = False,
        runtime_requirements: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        dependency_payload = load_json(dependency_graph_path) if dependency_graph_path.exists() else {}

        flow_payload: Dict[str, Any] = {}
        if execution_flow_graph_path and execution_flow_graph_path.exists():
            flow_payload = load_json(execution_flow_graph_path)

        manifest_payload = self._load_manifest_payload(manifest_path)

        trace_rows: Iterable[Dict[str, Any]] = []
        if runtime_trace_path and runtime_trace_path.exists():
            trace_rows = list(self._iter_jsonl(runtime_trace_path))

        resolved_runtime_source = str(runtime_source or "").strip().lower()
        if not resolved_runtime_source:
            if runtime_trace_path and runtime_trace_path.exists():
                resolved_runtime_source = "runtime_trace" if list(trace_rows) else "runtime_trace_empty"
            else:
                resolved_runtime_source = "none"

        heat_payload = self.classify(
            dependency_graph=dependency_payload if isinstance(dependency_payload, Mapping) else {},
            execution_flow_graph=flow_payload if isinstance(flow_payload, Mapping) else {},
            runtime_trace_rows=trace_rows,
            manifest=manifest_payload,
            runtime_source=resolved_runtime_source,
            enforce_runtime_signal=bool(enforce_runtime_signal),
            runtime_requirements=runtime_requirements,
        )

        out_root = output_dir.resolve()
        out_root.mkdir(parents=True, exist_ok=True)
        heat_path = out_root / "heat_classification.json"
        write_json(heat_path, heat_payload, pretty=True)

        return {
            "heat_path": str(heat_path),
            "heat": heat_payload,
        }

    def _analyze_runtime_trace_rows(
        self,
        runtime_trace_rows: Iterable[Mapping[str, Any]],
        canonical_lookup: Mapping[str, str],
    ) -> Dict[str, Any]:
        runtime_hits: Dict[str, int] = {}
        raw_unique_callees: Set[str] = set()
        mapped_unique_callees: Set[str] = set()
        unmatched_call_event_count = 0
        call_event_count = 0
        import_event_count = 0
        local_call_event_count = 0
        max_call_depth = 0
        depth_by_node: Dict[str, int] = {}

        for item in runtime_trace_rows:
            row = item if isinstance(item, Mapping) else {}
            event_type = str(row.get("event", "")).strip().lower()
            if event_type == "import":
                import_event_count += 1
                continue

            if event_type != "call":
                continue

            call_event_count += 1

            raw_callee = self._normalize_node_id(row.get("callee_node_id", row.get("node", "")))
            if raw_callee:
                raw_unique_callees.add(raw_callee)

            node_id = self._node_id_from_runtime_row(row, canonical_lookup)
            if node_id:
                mapped_unique_callees.add(node_id)
                runtime_hits[node_id] = int(runtime_hits.get(node_id, 0)) + 1
            else:
                unmatched_call_event_count += 1

            normalized_file = self._normalize_path(row.get("file", ""))
            if normalized_file and not normalized_file.startswith("<"):
                local_call_event_count += 1

            depth_value = self._extract_runtime_depth(row)
            if depth_value <= 0:
                caller_node_id = self._to_canonical_node_id(row.get("caller_node_id", ""), canonical_lookup)
                if caller_node_id:
                    depth_value = int(depth_by_node.get(caller_node_id, 1)) + 1
                else:
                    depth_value = 1

            if node_id and depth_value > int(depth_by_node.get(node_id, 0)):
                depth_by_node[node_id] = depth_value

            if depth_value > max_call_depth:
                max_call_depth = depth_value

        return {
            "runtime_hits": runtime_hits,
            "call_event_count": call_event_count,
            "import_event_count": import_event_count,
            "unique_raw_callee_count": len(raw_unique_callees),
            "unique_mapped_callee_count": len(mapped_unique_callees),
            "unmatched_call_event_count": unmatched_call_event_count,
            "local_call_event_count": local_call_event_count,
            "max_call_depth": max_call_depth,
        }

    def _runtime_static_reconciliation(
        self,
        dependency_graph: Mapping[str, Any],
        execution_flow_graph: Mapping[str, Any],
        canonical_lookup: Mapping[str, str],
    ) -> Dict[str, Any]:
        static_edges: Set[tuple[str, str]] = set()
        runtime_edges: Set[tuple[str, str]] = set()

        dependency_edges = self._ensure_list(dependency_graph.get("edges"))
        flow_edges = self._ensure_list(execution_flow_graph.get("edges"))

        for edge in dependency_edges:
            payload = edge if isinstance(edge, Mapping) else {}
            edge_type = str(payload.get("type", "")).strip().upper()
            if edge_type != "CALL":
                continue

            source = self._to_canonical_node_id(payload.get("source", payload.get("from", "")), canonical_lookup)
            target = self._to_canonical_node_id(payload.get("target", payload.get("to", "")), canonical_lookup)
            if source and target:
                static_edges.add((source, target))

        for edge in flow_edges:
            payload = edge if isinstance(edge, Mapping) else {}
            edge_type = str(payload.get("type", "")).strip().upper()
            if edge_type != "RUNTIME_CALL":
                continue

            source = self._to_canonical_node_id(payload.get("source", ""), canonical_lookup)
            target = self._to_canonical_node_id(payload.get("target", ""), canonical_lookup)
            if source and target:
                runtime_edges.add((source, target))

        overlap = static_edges.intersection(runtime_edges)
        overlap_ratio = self._safe_divide(len(overlap), len(static_edges))

        return {
            "static_call_edge_count": len(static_edges),
            "runtime_call_edge_count": len(runtime_edges),
            "shared_call_edge_count": len(overlap),
            "overlap_ratio": round(overlap_ratio, 6),
        }

    def _validate_runtime_signal(
        self,
        runtime_analysis: Mapping[str, Any],
        runtime_hits: Mapping[str, int],
        runtime_reachable: Set[str],
        all_nodes: Set[str],
        manifest_payload: Mapping[str, Any],
        runtime_source: str,
        reconciliation: Mapping[str, Any],
        runtime_requirements: Mapping[str, Any] | None,
        enforce_runtime_signal: bool,
    ) -> Dict[str, Any]:
        call_event_count = int(runtime_analysis.get("call_event_count", 0) or 0)
        import_event_count = int(runtime_analysis.get("import_event_count", 0) or 0)
        unique_mapped_callee_count = int(runtime_analysis.get("unique_mapped_callee_count", 0) or 0)
        unmatched_call_event_count = int(runtime_analysis.get("unmatched_call_event_count", 0) or 0)
        local_call_event_count = int(runtime_analysis.get("local_call_event_count", 0) or 0)
        max_call_depth = int(runtime_analysis.get("max_call_depth", 0) or 0)

        total_nodes = max(1, len(all_nodes))
        nodes_with_runtime_hits = len([node for node, value in runtime_hits.items() if int(value) > 0])
        coverage_ratio = self._safe_divide(nodes_with_runtime_hits, total_nodes)
        reachable_ratio = self._safe_divide(len(runtime_reachable), total_nodes)

        defaults: Dict[str, float] = {
            "min_call_events": float(self.MIN_RUNTIME_CALL_EVENTS),
            "min_unique_callees": float(self.MIN_RUNTIME_UNIQUE_CALLEES),
            "min_call_depth": float(self.MIN_RUNTIME_MAX_CALL_DEPTH),
            "min_local_calls": float(self.MIN_RUNTIME_LOCAL_CALLS),
            "min_coverage_ratio": float(self.MIN_COVERAGE_RATIO),
            "min_reachable_ratio": float(self.MIN_REACHABLE_RATIO),
        }
        if isinstance(runtime_requirements, Mapping):
            for key in defaults:
                candidate = runtime_requirements.get(key)
                if isinstance(candidate, (int, float)):
                    defaults[key] = float(candidate)

        effective_min_unique_callees = int(
            min(
                defaults["min_unique_callees"],
                max(3, int(math.ceil(float(total_nodes) * 0.03))),
            )
        )
        effective_min_call_events = int(
            min(
                defaults["min_call_events"],
                max(10, unique_mapped_callee_count),
            )
        )
        effective_min_call_depth = int(defaults["min_call_depth"])
        effective_min_local_calls = int(defaults["min_local_calls"])
        effective_min_coverage_ratio = float(defaults["min_coverage_ratio"])
        effective_min_reachable_ratio = float(defaults["min_reachable_ratio"])

        issues: List[str] = []

        runtime_source_text = str(runtime_source or "unknown").strip().lower()
        if runtime_source_text == "synthetic":
            issues.append("synthetic_runtime_trace_not_allowed")

        if call_event_count <= 0 and import_event_count > 0:
            issues.append("runtime_trace_contains_only_import_or_bootstrap_events")

        if call_event_count < effective_min_call_events:
            issues.append("runtime_call_events_below_threshold")

        if unique_mapped_callee_count < effective_min_unique_callees:
            issues.append("runtime_unique_callee_nodes_below_threshold")

        if max_call_depth < effective_min_call_depth:
            issues.append("runtime_call_depth_below_threshold")

        if local_call_event_count < effective_min_local_calls:
            issues.append("runtime_local_call_count_below_threshold")

        if call_event_count > 0 and nodes_with_runtime_hits == 0:
            issues.append("runtime_calls_exist_but_no_hits_mapped_to_graph_nodes")

        if coverage_ratio < effective_min_coverage_ratio:
            issues.append("runtime_coverage_ratio_below_threshold")

        if reachable_ratio < effective_min_reachable_ratio:
            issues.append("runtime_reachable_ratio_below_threshold")

        static_call_edge_count = int(reconciliation.get("static_call_edge_count", 0) or 0)
        runtime_call_edge_count = int(reconciliation.get("runtime_call_edge_count", 0) or 0)
        shared_call_edge_count = int(reconciliation.get("shared_call_edge_count", 0) or 0)
        if static_call_edge_count > 0 and runtime_call_edge_count > 0 and shared_call_edge_count == 0:
            issues.append("runtime_static_call_overlap_is_zero")

        raw_entrypoints = self._ensure_list(manifest_payload.get("entrypoints"))
        entrypoints = sorted(
            {
                normalized
                for normalized in (self._normalize_path(item) for item in raw_entrypoints)
                if normalized
            }
        )

        executed_entrypoints: List[str] = []
        for entrypoint in entrypoints:
            if self._entrypoint_has_runtime_evidence(entrypoint, runtime_hits, runtime_reachable):
                executed_entrypoints.append(entrypoint)

        if entrypoints and not executed_entrypoints:
            issues.append("manifest_entrypoints_not_observed_in_runtime")

        if unmatched_call_event_count > 0 and unique_mapped_callee_count == 0:
            issues.append("runtime_identity_mapping_failed")

        if enforce_runtime_signal and issues:
            detail = {
                "runtime_source": runtime_source_text,
                "call_event_count": call_event_count,
                "unique_mapped_callee_count": unique_mapped_callee_count,
                "max_call_depth": max_call_depth,
                "local_call_event_count": local_call_event_count,
                "coverage_ratio": round(coverage_ratio, 6),
                "reachable_ratio": round(reachable_ratio, 6),
                "issues": issues,
            }
            raise ClassificationError(
                "Runtime signal validation failed: " + json.dumps(detail, sort_keys=True)
            )

        return {
            "passed": len(issues) == 0,
            "enforced": bool(enforce_runtime_signal),
            "runtime_source": runtime_source_text,
            "call_event_count": call_event_count,
            "import_event_count": import_event_count,
            "unique_mapped_callee_count": unique_mapped_callee_count,
            "unmatched_call_event_count": unmatched_call_event_count,
            "local_call_event_count": local_call_event_count,
            "max_call_depth": max_call_depth,
            "nodes_with_runtime_hits": nodes_with_runtime_hits,
            "total_nodes": total_nodes,
            "coverage_ratio": round(coverage_ratio, 6),
            "reachable_ratio": round(reachable_ratio, 6),
            "entrypoint_count": len(entrypoints),
            "executed_entrypoint_count": len(executed_entrypoints),
            "entrypoints": entrypoints,
            "executed_entrypoints": executed_entrypoints,
            "requirements": {
                "min_call_events": effective_min_call_events,
                "min_unique_callees": effective_min_unique_callees,
                "min_call_depth": effective_min_call_depth,
                "min_local_calls": effective_min_local_calls,
                "min_coverage_ratio": round(effective_min_coverage_ratio, 6),
                "min_reachable_ratio": round(effective_min_reachable_ratio, 6),
            },
            "issues": issues,
        }

    def _entrypoint_has_runtime_evidence(
        self,
        entrypoint: Any,
        runtime_hits: Mapping[str, int],
        runtime_reachable: Set[str],
    ) -> bool:
        normalized_path = self._normalize_path(entrypoint)
        if not normalized_path:
            return False

        file_prefix = f"canonical://file/{normalized_path}"
        function_prefix = f"canonical://function/{normalized_path}/"
        class_prefix = f"canonical://class/{normalized_path}/"

        def has_match(node_id: str) -> bool:
            return node_id == file_prefix or node_id.startswith(function_prefix) or node_id.startswith(class_prefix)

        for node_id, hit_count in runtime_hits.items():
            if int(hit_count) <= 0:
                continue
            if has_match(str(node_id)):
                return True

        for node_id in runtime_reachable:
            if has_match(str(node_id)):
                return True

        return False

    def _extract_runtime_depth(self, row: Mapping[str, Any]) -> int:
        for key in ("depth", "stack_depth", "call_depth"):
            value = row.get(key)
            if isinstance(value, (int, float)):
                return max(0, int(value))
            if isinstance(value, str) and value.strip().isdigit():
                return max(0, int(value.strip()))
        return 0

    def _collect_runtime_hits(
        self,
        runtime_trace_rows: Iterable[Mapping[str, Any]],
        canonical_lookup: Mapping[str, str],
    ) -> Dict[str, int]:
        hits: Dict[str, int] = {}

        for item in runtime_trace_rows:
            row = item if isinstance(item, Mapping) else {}
            if str(row.get("event", "")).strip().lower() != "call":
                continue

            node_id = self._node_id_from_runtime_row(row, canonical_lookup)
            if not node_id:
                continue

            hits[node_id] = int(hits.get(node_id, 0)) + 1

        return hits

    def _normalized_count(self, value: int, maximum: int) -> float:
        numerator = max(0, int(value))
        denominator = max(0, int(maximum))
        if numerator <= 0 or denominator <= 0:
            return 0.0
        return self._clamp01(self._safe_divide(math.log1p(float(numerator)), math.log1p(float(denominator))))

    def _safe_divide(self, numerator: float, denominator: float) -> float:
        if denominator == 0:
            return 0.0
        return float(numerator) / float(denominator)

    def _clamp01(self, value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    def _collect_runtime_reachable_nodes(
        self,
        execution_flow_graph: Mapping[str, Any],
        runtime_hits: Mapping[str, int],
        canonical_lookup: Mapping[str, str],
    ) -> Set[str]:
        nodes = self._ensure_list(execution_flow_graph.get("nodes"))
        edges = self._ensure_list(execution_flow_graph.get("edges"))

        runtime_nodes: Set[str] = set()
        adjacency: Dict[str, Set[str]] = {}
        inbound_count: Dict[str, int] = {}

        for item in nodes:
            payload = item if isinstance(item, Mapping) else {}
            node_id = self._to_canonical_node_id(payload.get("id", ""), canonical_lookup)
            if node_id:
                runtime_nodes.add(node_id)

        for edge in edges:
            payload = edge if isinstance(edge, Mapping) else {}
            edge_type = str(payload.get("type", "")).strip().upper()
            if edge_type != "RUNTIME_CALL":
                continue

            source = self._to_canonical_node_id(payload.get("source", ""), canonical_lookup)
            target = self._to_canonical_node_id(payload.get("target", ""), canonical_lookup)
            if not source or not target:
                continue

            runtime_nodes.add(source)
            runtime_nodes.add(target)

            adjacency.setdefault(source, set()).add(target)
            inbound_count[target] = int(inbound_count.get(target, 0)) + 1

        for node_id, count in runtime_hits.items():
            if int(count) > 0:
                canonical_node_id = self._to_canonical_node_id(node_id, canonical_lookup)
                if canonical_node_id:
                    runtime_nodes.add(canonical_node_id)

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

    def _collect_reference_evidence(
        self,
        dependency_graph: Mapping[str, Any],
        canonical_lookup: Mapping[str, str],
    ) -> Dict[str, Dict[str, int]]:
        edges = self._ensure_list(dependency_graph.get("edges"))

        inbound_edges: Dict[str, int] = {}
        outbound_edges: Dict[str, int] = {}
        executable_references: Dict[str, int] = {}
        non_executable_references: Dict[str, int] = {}

        for edge in edges:
            payload = edge if isinstance(edge, Mapping) else {}
            source = self._to_canonical_node_id(payload.get("source", payload.get("from", "")), canonical_lookup)
            target = self._to_canonical_node_id(payload.get("target", payload.get("to", "")), canonical_lookup)
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
        canonical_lookup: Mapping[str, str],
    ) -> Set[str]:
        node_ids: Set[str] = set()

        dependency_nodes = self._ensure_list(dependency_payload.get("nodes"))
        dependency_edges = self._ensure_list(dependency_payload.get("edges"))

        for item in dependency_nodes:
            payload = item if isinstance(item, Mapping) else {}
            normalized = self._to_canonical_node_id(payload.get("id", ""), canonical_lookup)
            if normalized:
                node_ids.add(normalized)

        for edge in dependency_edges:
            payload = edge if isinstance(edge, Mapping) else {}
            source = self._to_canonical_node_id(payload.get("source", payload.get("from", "")), canonical_lookup)
            target = self._to_canonical_node_id(payload.get("target", payload.get("to", "")), canonical_lookup)
            if source:
                node_ids.add(source)
            if target:
                node_ids.add(target)

        flow_nodes = self._ensure_list(flow_payload.get("nodes"))
        flow_edges = self._ensure_list(flow_payload.get("edges"))

        for item in flow_nodes:
            payload = item if isinstance(item, Mapping) else {}
            normalized = self._to_canonical_node_id(payload.get("id", ""), canonical_lookup)
            if normalized:
                node_ids.add(normalized)

        for edge in flow_edges:
            payload = edge if isinstance(edge, Mapping) else {}
            source = self._to_canonical_node_id(payload.get("source", ""), canonical_lookup)
            target = self._to_canonical_node_id(payload.get("target", ""), canonical_lookup)
            if source:
                node_ids.add(source)
            if target:
                node_ids.add(target)

        for node_id in runtime_hits.keys():
            normalized = self._to_canonical_node_id(node_id, canonical_lookup)
            if normalized:
                node_ids.add(normalized)

        entrypoints = self._ensure_list(manifest_payload.get("entrypoints"))
        for entrypoint in entrypoints:
            normalized_path = self._normalize_path(entrypoint)
            if not normalized_path:
                continue
            entrypoint_node = self._to_canonical_node_id(build_node_id("file", normalized_path), canonical_lookup)
            if entrypoint_node:
                node_ids.add(entrypoint_node)

        return node_ids

    def _node_id_from_runtime_row(self, row: Mapping[str, Any], canonical_lookup: Mapping[str, str]) -> str:
        direct_node_id = self._to_canonical_node_id(row.get("callee_node_id", row.get("node", "")), canonical_lookup)
        if direct_node_id:
            return direct_node_id

        file_path = self._normalize_path(row.get("file", ""))
        if not file_path or file_path.startswith("<"):
            return ""

        function_name = str(row.get("function", "")).strip()
        if function_name and function_name not in {"<module>", "<import>"}:
            function_candidate = build_node_id("function", file_path, function_name)
            canonical_function_candidate = self._to_canonical_node_id(function_candidate, canonical_lookup)
            if canonical_function_candidate:
                return canonical_function_candidate

        file_candidate = build_node_id("file", file_path)
        return self._to_canonical_node_id(file_candidate, canonical_lookup)

    def _classification_from_score(self, score: float) -> str:
        if score >= self.HOT_THRESHOLD:
            return "HOT"
        if score >= self.WARM_THRESHOLD:
            return "WARM"
        if score >= self.COLD_THRESHOLD:
            return "COLD"
        return "DEAD"

    def _build_canonical_lookup(
        self,
        dependency_payload: Mapping[str, Any],
        flow_payload: Mapping[str, Any],
    ) -> Dict[str, str]:
        lookup: Dict[str, str] = {}

        def register(alias: Any, canonical_value: Any) -> None:
            alias_id = self._normalize_node_id(alias)
            canonical_id = self._normalize_node_id(canonical_value)
            if not alias_id or not canonical_id:
                return
            lookup.setdefault(alias_id, canonical_id)
            lookup.setdefault(canonical_id, canonical_id)

        dependency_nodes = self._ensure_list(dependency_payload.get("nodes"))
        dependency_edges = self._ensure_list(dependency_payload.get("edges"))
        flow_nodes = self._ensure_list(flow_payload.get("nodes"))
        flow_edges = self._ensure_list(flow_payload.get("edges"))

        for item in dependency_nodes:
            payload = item if isinstance(item, Mapping) else {}
            node_id = self._normalize_node_id(payload.get("id", ""))
            if not node_id:
                continue
            canonical_id = self._normalize_node_id(payload.get("canonical_id", ""))
            if not canonical_id:
                canonical_id = self._canonicalize_legacy_node_id(node_id) or node_id
            register(node_id, canonical_id)

        for edge in dependency_edges:
            payload = edge if isinstance(edge, Mapping) else {}
            source = self._normalize_node_id(payload.get("source", payload.get("from", "")))
            target = self._normalize_node_id(payload.get("target", payload.get("to", "")))
            if source and source not in lookup:
                register(source, self._canonicalize_legacy_node_id(source) or source)
            if target and target not in lookup:
                register(target, self._canonicalize_legacy_node_id(target) or target)

        for item in flow_nodes:
            payload = item if isinstance(item, Mapping) else {}
            node_id = self._normalize_node_id(payload.get("id", ""))
            if node_id and node_id not in lookup:
                register(node_id, self._canonicalize_legacy_node_id(node_id) or node_id)

        for edge in flow_edges:
            payload = edge if isinstance(edge, Mapping) else {}
            source = self._normalize_node_id(payload.get("source", ""))
            target = self._normalize_node_id(payload.get("target", ""))
            if source and source not in lookup:
                register(source, self._canonicalize_legacy_node_id(source) or source)
            if target and target not in lookup:
                register(target, self._canonicalize_legacy_node_id(target) or target)

        return lookup

    def _to_canonical_node_id(self, value: Any, canonical_lookup: Mapping[str, str]) -> str:
        normalized = self._normalize_node_id(value)
        if not normalized:
            return ""

        lookup_value = self._normalize_node_id(canonical_lookup.get(normalized, ""))
        if lookup_value:
            return lookup_value

        if normalized.startswith("canonical://"):
            return normalized

        return self._canonicalize_legacy_node_id(normalized)

    def _canonicalize_legacy_node_id(self, value: Any) -> str:
        normalized = self._normalize_node_id(value)
        if not normalized:
            return ""
        if normalized.startswith("canonical://"):
            return normalized
        if ":" not in normalized:
            return ""

        kind, payload = normalized.split(":", 1)
        node_kind = str(kind).strip().lower()
        node_payload = str(payload).strip()

        if node_kind == "file":
            normalized_path = self._normalize_path(node_payload)
            if not normalized_path:
                return ""
            return self._normalize_node_id(build_canonical_id("file", normalized_path))

        if node_kind in {"function", "class"}:
            rel_path, separator, name = node_payload.rpartition(":")
            if not separator:
                return ""
            normalized_path = self._normalize_path(rel_path)
            normalized_name = str(name).strip()
            if not normalized_path or not normalized_name:
                return ""
            return self._normalize_node_id(build_canonical_id(node_kind, normalized_path, normalized_name))

        return ""

    def _normalize_node_id(self, value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""

        if raw.startswith("canonical://"):
            payload = raw[len("canonical://") :]
            node_kind, separator, node_payload = payload.partition("/")
            if not separator:
                return ""

            canonical_kind = str(node_kind).strip().lower()
            canonical_payload = str(node_payload).strip()
            if not canonical_payload:
                return ""

            if canonical_kind == "file":
                normalized_path = self._normalize_path(canonical_payload)
                if not normalized_path:
                    return ""
                return f"canonical://file/{normalized_path}"

            if canonical_kind in {"function", "class"}:
                rel_path, rel_separator, name = canonical_payload.rpartition("/")
                if not rel_separator:
                    return ""
                normalized_path = self._normalize_path(rel_path)
                normalized_name = str(name).strip().replace("/", "_")
                if not normalized_path or not normalized_name:
                    return ""
                return f"canonical://{canonical_kind}/{normalized_path}/{normalized_name}"

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

    def _ensure_list(self, value: Any) -> List[Any]:
        return value if isinstance(value, list) else []

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
