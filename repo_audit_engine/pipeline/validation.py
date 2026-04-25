from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from repo_audit_engine.architecture.constraints import evaluate_architecture_constraints

Issue = Dict[str, Any]
LayerResult = Dict[str, Any]
EdgeTuple = Tuple[str, str, str]


@dataclass
class GraphPrimitives:
    total_node_count: int
    total_edge_count: int
    node_map: Dict[str, Dict[str, Any]]
    valid_node_ids: List[str]
    malformed_node_ids: List[str]
    duplicate_node_ids: List[str]
    invalid_namespace_ids: List[str]
    valid_edges: List[EdgeTuple]
    malformed_edges: List[str]
    invalid_edge_types: List[str]
    unresolved_edges: List[str]
    cross_namespace_edges: List[str]
    adjacency: Dict[str, Set[str]]


class VerificationRunner:
    """
    Deterministic validation engine used by Phase 1 Python migration.

    This runner preserves the legacy layer5 structural contract while also
    producing richer domain-level outputs for later migration phases.
    """

    _ALLOWED_EDGE_TYPES = {"IMPORT", "DI", "CONFIG", "DYNAMIC"}
    _ALLOWED_RESOLVER_SOURCES = {"AST", "DI", "CONFIG", "HEURISTIC", "DYNAMIC"}

    _SEVERITY_RANK = {
        "HIGH": 0,
        "MEDIUM": 1,
        "LOW": 2,
        "INFO": 3,
    }

    _DOMAIN_ORDER = {
        "structural_integrity": 1,
        "dependency_consistency": 2,
        "architecture_drift": 2,
        "topology_validation": 3,
        "semantic_observations": 4,
        "execution_confidence": 5,
        "architectural_intent": 6,
        "semantic_consistency": 7,
        "causal_flow": 8,
    }

    _DOMAIN_WEIGHTS = {
        "structural_integrity": 0.35,
        "topology_validation": 0.25,
        "dependency_consistency": 0.25,
        "semantic_observations": 0.15,
    }

    _FAILURE_IMPACT_WEIGHTS = {
        "structural_integrity": 0.30,
        "topology_validation": 0.20,
        "dependency_consistency": 0.20,
        "architecture_drift": 0.20,
        "semantic_observations": 0.10,
        "execution_confidence": 0.20,
        "architectural_intent": 0.12,
        "semantic_consistency": 0.08,
        "causal_flow": 0.10,
    }

    _MIN_EXECUTION_CONFIDENCE = 0.30
    _COVERAGE_HARD_FLOOR = 0.30
    _COVERAGE_HARD_FLOOR_TRUST_MULTIPLIER = 0.50
    _AST_DI_DIVERGENCE_HARD_THRESHOLD = 0.50
    _ARCHITECTURE_DRIFT_TRUST_PENALTY = 0.20
    _MIN_ENTRYPOINT_COVERAGE_COMPLETENESS = 0.30
    _MIN_SCENARIO_COVERAGE_COMPLETENESS = 0.55
    _MIN_DOMAIN_COVERAGE_COMPLETENESS = 0.34

    _ISSUE_SAMPLE_LIMIT = 25

    def __init__(
        self,
        graph_data: Dict[str, Any],
        resolver_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._graph_data = graph_data if isinstance(graph_data, dict) else {}
        self._resolver_data = resolver_data if isinstance(resolver_data, dict) else {}

    def run(
        self,
        entrypoints: Optional[Sequence[str]] = None,
        min_trust: float = 0.40,
        execution_evidence: Optional[Mapping[str, Any]] = None,
        min_execution_confidence: float = _MIN_EXECUTION_CONFIDENCE,
    ) -> Dict[str, Any]:
        graph = self._extract_graph(self._graph_data)
        primitives = self._build_graph_primitives(
            nodes=graph.get("nodes", []),
            edges=graph.get("edges", []),
        )

        structural_layer = self._evaluate_structural_integrity(primitives)

        resolver_edges = self._normalize_resolver_edges(self._resolver_data)
        dependency_layer = self._evaluate_dependency_consistency(primitives, resolver_edges)

        topology_layer = self._evaluate_topology_validation(
            primitives=primitives,
            entrypoints=entrypoints or [],
        )

        semantic_layer = self._evaluate_semantic_observations(
            topology_layer=topology_layer,
            dependency_layer=dependency_layer,
        )

        execution_layer = self._evaluate_execution_confidence(
            execution_evidence=execution_evidence,
            min_execution_confidence=min_execution_confidence,
            entrypoints=entrypoints or [],
        )

        architecture_layer = self._evaluate_architectural_intent(
            primitives=primitives,
            execution_evidence=execution_evidence,
        )

        semantic_consistency_layer = self._evaluate_semantic_consistency(
            execution_evidence=execution_evidence,
        )

        causal_flow_layer = self._evaluate_causal_flow(
            execution_evidence=execution_evidence,
            runtime_signal_present=int(execution_layer.get("metrics", {}).get("call_event_count", 0) or 0) > 0,
        )

        trust_model = self._compute_trust_model(
            structural_layer=structural_layer,
            dependency_layer=dependency_layer,
            topology_layer=topology_layer,
            semantic_layer=semantic_layer,
            execution_layer=execution_layer,
            min_execution_confidence=min_execution_confidence,
        )

        policy_decision = self._apply_policy(
            structural_layer=structural_layer,
            dependency_layer=dependency_layer,
            topology_layer=topology_layer,
            execution_layer=execution_layer,
            architecture_layer=architecture_layer,
            semantic_consistency_layer=semantic_consistency_layer,
            causal_flow_layer=causal_flow_layer,
            trust_model=trust_model,
            min_trust=min_trust,
            min_execution_confidence=min_execution_confidence,
        )

        failure_analysis = self._build_failure_analysis(
            structural_layer=structural_layer,
            dependency_layer=dependency_layer,
            topology_layer=topology_layer,
            semantic_layer=semantic_layer,
            execution_layer=execution_layer,
            architecture_layer=architecture_layer,
            semantic_consistency_layer=semantic_consistency_layer,
            causal_flow_layer=causal_flow_layer,
            trust_model=trust_model,
        )

        legacy_status = self._build_legacy_status(structural_layer)

        compatibility_metrics = {
            "node_count": primitives.total_node_count,
            "edge_count": primitives.total_edge_count,
            "valid_node_count": len(primitives.valid_node_ids),
            "malformed_node_count": len(primitives.malformed_node_ids),
            "duplicate_node_count": len(primitives.duplicate_node_ids),
            "invalid_namespace_count": len(primitives.invalid_namespace_ids),
            "malformed_edge_count": len(primitives.malformed_edges),
            "invalid_edge_type_count": len(primitives.invalid_edge_types),
            "unresolved_edge_count": len(primitives.unresolved_edges),
            "cross_namespace_edge_count": len(primitives.cross_namespace_edges),
            "invalid_edge_count": (
                len(primitives.malformed_edges)
                + len(primitives.invalid_edge_types)
                + len(primitives.unresolved_edges)
            ),
        }

        return {
            "status": legacy_status["status"],
            "critical_failure": legacy_status["critical_failure"],
            "system_valid": policy_decision["system_valid"],
            "policy_critical_failure": policy_decision["critical_failure"],
            "trust_score": trust_model["trust_score"],
            "trust_breakdown": trust_model,
            "failure_domains": failure_analysis["failure_domains"],
            "failure_domains_ranked": failure_analysis["ranked_domains"],
            "failure_analysis": failure_analysis,
            "policy_decision": policy_decision,
            "metrics": compatibility_metrics,
            "issues": self._sort_issues(structural_layer.get("issues", [])),
            "warnings": sorted(set(structural_layer.get("warnings", []))),
            "detailed_results": {
                "structural_integrity": self._strip_context(structural_layer),
                "dependency_consistency": self._strip_context(dependency_layer),
                "topology_validation": self._strip_context(topology_layer),
                "semantic_observations": self._strip_context(semantic_layer),
                "execution_confidence": self._strip_context(execution_layer),
                "architectural_intent": self._strip_context(architecture_layer),
                "semantic_consistency": self._strip_context(semantic_consistency_layer),
                "causal_flow": self._strip_context(causal_flow_layer),
            },
        }

    def _extract_graph(self, graph_data: Dict[str, Any]) -> Dict[str, Any]:
        doc = graph_data if isinstance(graph_data, dict) else {}

        if isinstance(doc.get("graph"), dict):
            doc = doc.get("graph", {})

        nodes = doc.get("nodes", [])
        edges = doc.get("edges", [])

        if not isinstance(nodes, list):
            nodes = []
        if not isinstance(edges, list):
            edges = []

        return {
            "nodes": nodes,
            "edges": edges,
        }

    def _build_graph_primitives(
        self,
        nodes: Sequence[Dict[str, Any]],
        edges: Sequence[Dict[str, Any]],
    ) -> GraphPrimitives:
        node_map: Dict[str, Dict[str, Any]] = {}
        malformed_node_ids: List[str] = []
        duplicate_node_ids: List[str] = []
        invalid_namespace_ids: List[str] = []

        for node in sorted(nodes, key=lambda item: str((item or {}).get("id", ""))):
            payload = node if isinstance(node, dict) else {}
            node_id = self._normalize_node_id(payload.get("id"))
            if not node_id:
                malformed_node_ids.append(str(payload.get("id", "")))
                continue

            if node_id in node_map:
                duplicate_node_ids.append(node_id)
                continue

            node_map[node_id] = payload
            if not self._is_valid_namespace(node_id):
                invalid_namespace_ids.append(node_id)

        malformed_edges: List[str] = []
        invalid_edge_types: List[str] = []
        unresolved_edges: List[str] = []
        cross_namespace_edges: List[str] = []

        valid_edges_set: Set[EdgeTuple] = set()
        adjacency: Dict[str, Set[str]] = {node_id: set() for node_id in node_map}

        def edge_sort_key(edge: Any) -> Tuple[str, str, str]:
            payload = edge if isinstance(edge, dict) else {}
            return (
                self._normalize_node_id(payload.get("from")),
                self._normalize_node_id(payload.get("to")),
                str(payload.get("type") or "").strip().upper(),
            )

        for edge in sorted(edges, key=edge_sort_key):
            payload = edge if isinstance(edge, dict) else {}

            src = self._normalize_node_id(payload.get("from"))
            dst = self._normalize_node_id(payload.get("to"))
            edge_type = str(payload.get("type") or "").strip().upper()
            signature = f"{src}|{dst}|{edge_type}"

            if not src or not dst or not edge_type:
                malformed_edges.append(signature)
                continue

            if edge_type not in self._ALLOWED_EDGE_TYPES:
                invalid_edge_types.append(signature)
                continue

            confidence = payload.get("confidence")
            if not isinstance(confidence, (int, float)):
                malformed_edges.append(signature)
                continue

            if src not in node_map or dst not in node_map:
                unresolved_edges.append(signature)
                continue

            valid_edges_set.add((src, dst, edge_type))
            adjacency[src].add(dst)

            src_ns = self._get_namespace(src)
            dst_ns = self._get_namespace(dst)
            if src_ns and dst_ns and src_ns != dst_ns:
                cross_namespace_edges.append(signature)

        return GraphPrimitives(
            total_node_count=len(nodes),
            total_edge_count=len(edges),
            node_map=node_map,
            valid_node_ids=sorted(node_map.keys()),
            malformed_node_ids=sorted(set(malformed_node_ids)),
            duplicate_node_ids=sorted(set(duplicate_node_ids)),
            invalid_namespace_ids=sorted(set(invalid_namespace_ids)),
            valid_edges=sorted(valid_edges_set),
            malformed_edges=sorted(set(malformed_edges)),
            invalid_edge_types=sorted(set(invalid_edge_types)),
            unresolved_edges=sorted(set(unresolved_edges)),
            cross_namespace_edges=sorted(set(cross_namespace_edges)),
            adjacency=adjacency,
        )

    def _evaluate_structural_integrity(self, primitives: GraphPrimitives) -> LayerResult:
        issues: List[Issue] = []
        warnings: List[str] = []

        if primitives.malformed_node_ids:
            issues.append(
                self._issue(
                    domain="structural_integrity",
                    issue_type="MALFORMED_NODE_ID",
                    severity="HIGH",
                    message="Node ids must be non-empty strings.",
                    count=len(primitives.malformed_node_ids),
                    sample_nodes=primitives.malformed_node_ids,
                )
            )

        if primitives.duplicate_node_ids:
            issues.append(
                self._issue(
                    domain="structural_integrity",
                    issue_type="DUPLICATE_NODE_ID",
                    severity="HIGH",
                    message="Duplicate node ids detected.",
                    count=len(primitives.duplicate_node_ids),
                    sample_nodes=primitives.duplicate_node_ids,
                )
            )

        if primitives.invalid_namespace_ids:
            issues.append(
                self._issue(
                    domain="structural_integrity",
                    issue_type="INVALID_NODE_NAMESPACE",
                    severity="MEDIUM",
                    message="Node ids should use canonical namespace format.",
                    count=len(primitives.invalid_namespace_ids),
                    sample_nodes=primitives.invalid_namespace_ids,
                )
            )

        if primitives.malformed_edges:
            issues.append(
                self._issue(
                    domain="structural_integrity",
                    issue_type="MALFORMED_EDGE_SCHEMA",
                    severity="HIGH",
                    message="Edges must include valid from/to/type/confidence values.",
                    count=len(primitives.malformed_edges),
                    sample_nodes=primitives.malformed_edges,
                )
            )

        if primitives.invalid_edge_types:
            issues.append(
                self._issue(
                    domain="structural_integrity",
                    issue_type="INVALID_EDGE_TYPE",
                    severity="MEDIUM",
                    message="Edge type is not supported.",
                    count=len(primitives.invalid_edge_types),
                    sample_nodes=primitives.invalid_edge_types,
                )
            )

        if primitives.unresolved_edges:
            issues.append(
                self._issue(
                    domain="structural_integrity",
                    issue_type="UNRESOLVED_EDGE_REFERENCE",
                    severity="HIGH",
                    message="Edges must reference known nodes.",
                    count=len(primitives.unresolved_edges),
                    sample_nodes=primitives.unresolved_edges,
                )
            )

        if primitives.cross_namespace_edges:
            warnings.append(
                "Cross-namespace edges detected. This is allowed but should be reviewed for ownership boundaries."
            )

        malformed_node_ratio = self._safe_divide(
            len(primitives.malformed_node_ids) + len(primitives.duplicate_node_ids),
            max(1, primitives.total_node_count),
        )

        invalid_edge_ratio = self._safe_divide(
            len(primitives.malformed_edges) + len(primitives.invalid_edge_types) + len(primitives.unresolved_edges),
            max(1, primitives.total_edge_count),
        )

        domain_penalty = min(1.0, (invalid_edge_ratio * 0.8) + (malformed_node_ratio * 0.2))
        domain_score = self._round3(self._clamp01(1.0 - domain_penalty))

        metrics = {
            "node_count": primitives.total_node_count,
            "edge_count": primitives.total_edge_count,
            "valid_node_count": len(primitives.valid_node_ids),
            "malformed_node_count": len(primitives.malformed_node_ids),
            "duplicate_node_count": len(primitives.duplicate_node_ids),
            "invalid_namespace_count": len(primitives.invalid_namespace_ids),
            "malformed_edge_count": len(primitives.malformed_edges),
            "invalid_edge_type_count": len(primitives.invalid_edge_types),
            "unresolved_edge_count": len(primitives.unresolved_edges),
            "cross_namespace_edge_count": len(primitives.cross_namespace_edges),
            "invalid_edge_ratio": self._round3(invalid_edge_ratio),
            "malformed_node_ratio": self._round3(malformed_node_ratio),
            "domain_score": domain_score,
        }

        return {
            "domain": "structural_integrity",
            "metrics": metrics,
            "issues": self._sort_issues(issues),
            "warnings": sorted(set(warnings)),
            "context": {
                "node_map": primitives.node_map,
                "valid_node_ids": primitives.valid_node_ids,
                "valid_edges": primitives.valid_edges,
                "adjacency": primitives.adjacency,
            },
        }

    def _normalize_resolver_edges(self, resolver_data: Dict[str, Any]) -> List[Dict[str, str]]:
        candidates: List[Any] = []

        for key in ("edges", "resolver_edges"):
            value = resolver_data.get(key)
            if isinstance(value, list):
                candidates.extend(value)

        graph_obj = resolver_data.get("graph")
        if isinstance(graph_obj, dict):
            value = graph_obj.get("edges")
            if isinstance(value, list):
                candidates.extend(value)

        normalized_set: Set[Tuple[str, str, str, str]] = set()

        for raw_edge in candidates:
            edge = raw_edge if isinstance(raw_edge, dict) else {}
            src = self._normalize_node_id(edge.get("from"))
            dst = self._normalize_node_id(edge.get("to"))
            edge_type = str(edge.get("type") or "").strip().upper()
            source = str(edge.get("source") or "").strip().upper()

            if not src or not dst:
                continue

            if not source:
                source_meta = edge.get("source_metadata")
                if isinstance(source_meta, list) and source_meta:
                    source = str(source_meta[0]).strip().upper()

            if source not in self._ALLOWED_RESOLVER_SOURCES:
                source = "UNKNOWN"

            if not edge_type:
                edge_type = "UNKNOWN"

            normalized_set.add((src, dst, edge_type, source))

        return [
            {"from": src, "to": dst, "type": edge_type, "source": source}
            for src, dst, edge_type, source in sorted(normalized_set, key=lambda item: (item[3], item[0], item[1], item[2]))
        ]

    def _evaluate_dependency_consistency(
        self,
        primitives: GraphPrimitives,
        resolver_edges: Sequence[Dict[str, str]],
    ) -> LayerResult:
        issues: List[Issue] = []
        warnings: List[str] = []

        graph_di_pairs = {(src, dst) for src, dst, edge_type in primitives.valid_edges if edge_type == "DI"}
        graph_all_pairs = {(src, dst) for src, dst, _ in primitives.valid_edges}

        resolver_di_pairs = {
            (edge["from"], edge["to"])
            for edge in resolver_edges
            if edge.get("source") == "DI"
        }
        resolver_ast_pairs = {
            (edge["from"], edge["to"])
            for edge in resolver_edges
            if edge.get("source") == "AST"
        }
        resolver_all_pairs = {(edge["from"], edge["to"]) for edge in resolver_edges}

        di_nodes_referenced = {node for pair in resolver_di_pairs for node in pair}
        missing_di_nodes = sorted(node for node in di_nodes_referenced if node not in primitives.node_map)

        missing_di_ratio = self._safe_divide(len(missing_di_nodes), max(1, len(di_nodes_referenced)))
        ast_di_divergence = self._pair_divergence(resolver_ast_pairs, resolver_di_pairs)
        ast_di_overlap = self._pair_overlap(resolver_ast_pairs, resolver_di_pairs)
        graph_di_drift = self._pair_divergence(graph_di_pairs, resolver_di_pairs)
        resolver_coverage = self._pair_overlap(graph_all_pairs, resolver_all_pairs)

        if missing_di_nodes:
            issues.append(
                self._issue(
                    domain="dependency_consistency",
                    issue_type="MISSING_DI_NODES",
                    severity="HIGH",
                    message="Resolver DI edges reference nodes not present in the canonical graph.",
                    count=len(missing_di_nodes),
                    sample_nodes=missing_di_nodes,
                )
            )

        if ast_di_divergence > 0.0:
            escalated = ast_di_divergence > self._AST_DI_DIVERGENCE_HARD_THRESHOLD
            issues.append(
                self._issue(
                    domain="dependency_consistency",
                    issue_type=(
                        "AST_DI_DIVERGENCE_ESCALATED"
                        if escalated
                        else "AST_DI_DIVERGENCE_TRACKED"
                    ),
                    severity="HIGH" if escalated else "MEDIUM",
                    message=(
                        "AST and DI pair sets diverge and exceed architecture drift threshold."
                        if escalated
                        else "AST and DI pair sets diverge; this is tracked as architecture drift."
                    ),
                    count=len(resolver_di_pairs.symmetric_difference(resolver_ast_pairs)),
                )
            )

        if graph_di_drift > 0.35:
            issues.append(
                self._issue(
                    domain="dependency_consistency",
                    issue_type="GRAPH_DI_DRIFT",
                    severity="MEDIUM",
                    message="Unified graph DI edges diverge significantly from resolver DI edges.",
                    count=int(round(graph_di_drift * max(1, len(graph_di_pairs) + len(resolver_di_pairs)))),
                )
            )

        if not resolver_edges:
            warnings.append("Resolver edge payload is empty; dependency consistency confidence is reduced.")
        elif resolver_coverage < 0.40:
            warnings.append("Resolver edge coverage is low compared to unified graph edges.")

        penalty = min(
            1.0,
            (self._clamp01(missing_di_ratio) * 0.65)
            + (self._clamp01(ast_di_divergence) * 0.20)
            + (self._clamp01(graph_di_drift) * 0.15),
        )

        metrics = {
            "resolver_edge_count": len(resolver_edges),
            "graph_di_edge_count": len(graph_di_pairs),
            "resolver_di_edge_count": len(resolver_di_pairs),
            "resolver_ast_edge_count": len(resolver_ast_pairs),
            "missing_di_nodes_count": len(missing_di_nodes),
            "di_nodes_referenced_count": len(di_nodes_referenced),
            "missing_di_node_ratio": self._round3(missing_di_ratio),
            "ast_di_divergence_score": self._round3(ast_di_divergence),
            "ast_di_overlap_score": self._round3(ast_di_overlap),
            "graph_di_drift_score": self._round3(graph_di_drift),
            "resolver_coverage_score": self._round3(resolver_coverage),
            "domain_score": self._round3(self._clamp01(1.0 - penalty)),
        }

        details = {
            "missing_di_nodes": missing_di_nodes[: self._ISSUE_SAMPLE_LIMIT],
            "resolver_di_pairs_sample": [
                {"from": src, "to": dst}
                for src, dst in sorted(resolver_di_pairs)[: self._ISSUE_SAMPLE_LIMIT]
            ],
            "resolver_ast_pairs_sample": [
                {"from": src, "to": dst}
                for src, dst in sorted(resolver_ast_pairs)[: self._ISSUE_SAMPLE_LIMIT]
            ],
        }

        return {
            "domain": "dependency_consistency",
            "metrics": metrics,
            "issues": self._sort_issues(issues),
            "warnings": sorted(set(warnings)),
            "details": details,
            "context": {
                "graph_di_pairs": sorted(graph_di_pairs),
                "resolver_di_pairs": sorted(resolver_di_pairs),
                "resolver_ast_pairs": sorted(resolver_ast_pairs),
            },
        }

    def _evaluate_topology_validation(
        self,
        primitives: GraphPrimitives,
        entrypoints: Sequence[str],
    ) -> LayerResult:
        issues: List[Issue] = []
        warnings: List[str] = []

        explicit_entrypoints = sorted(
            {
                self._normalize_node_id(item)
                for item in entrypoints
                if self._normalize_node_id(item)
            }
        )

        inferred_entrypoints = self._infer_entrypoints(primitives.node_map)
        effective_entrypoints = explicit_entrypoints if explicit_entrypoints else inferred_entrypoints

        entrypoints_present = [item for item in effective_entrypoints if item in primitives.node_map]
        entrypoints_missing = [item for item in effective_entrypoints if item not in primitives.node_map]

        if entrypoints_present:
            reachable = self._bfs_reachable(primitives.adjacency, entrypoints_present)
        else:
            # Preserve deterministic behavior without collapsing trust when entrypoints are unknown.
            reachable = set(primitives.valid_node_ids)
            warnings.append("No active entrypoints available; reachability checks were skipped.")

        unreachable = sorted(set(primitives.valid_node_ids) - set(reachable))
        unreachable_set = set(unreachable)

        di_unreachable_edges = [
            (src, dst, edge_type)
            for src, dst, edge_type in primitives.valid_edges
            if edge_type == "DI" and (src in unreachable_set or dst in unreachable_set)
        ]

        cycle_nodes = self._find_cycle_nodes(primitives.valid_node_ids, primitives.adjacency)
        cycle_violation_nodes = sorted(
            node_id
            for node_id in cycle_nodes
            if not self._is_cycle_allowed(primitives.node_map.get(node_id, {}))
        )

        isolated_reports: List[Dict[str, Any]] = []
        isolated_module_count = 0
        isolated_module_node_count = 0
        orphan_subgraph_count = 0
        orphan_subgraph_node_count = 0
        cyclic_island_count = 0
        cyclic_island_node_count = 0

        components = self._undirected_components(unreachable, primitives.adjacency)
        cycle_node_set = set(cycle_nodes)

        for component in components:
            if not component:
                continue

            classification = self._classify_island(component, primitives.node_map, cycle_node_set)
            if classification == "isolated_module":
                isolated_module_count += 1
                isolated_module_node_count += len(component)
            elif classification == "cyclic_island":
                cyclic_island_count += 1
                cyclic_island_node_count += len(component)
            else:
                orphan_subgraph_count += 1
                orphan_subgraph_node_count += len(component)

            isolated_reports.append(
                {
                    "size": len(component),
                    "classification": classification,
                    "nodes": sorted(component)[: self._ISSUE_SAMPLE_LIMIT],
                    "contains_cycle": any(node in cycle_node_set for node in component),
                    "explicit_isolation": all(
                        self._is_explicitly_isolated(primitives.node_map.get(node, {}))
                        for node in component
                    ),
                }
            )

        if entrypoints_missing:
            issues.append(
                self._issue(
                    domain="topology_validation",
                    issue_type="MISSING_ENTRYPOINTS",
                    severity="MEDIUM",
                    message="Some configured entrypoints were not found in canonical nodes.",
                    count=len(entrypoints_missing),
                    sample_nodes=entrypoints_missing,
                )
            )

        if unreachable:
            issues.append(
                self._issue(
                    domain="topology_validation",
                    issue_type="UNREACHABLE_NODES",
                    severity="LOW",
                    message="Unreachable nodes were detected from active entrypoints.",
                    count=len(unreachable),
                    sample_nodes=unreachable,
                )
            )

        if orphan_subgraph_count > 0:
            issues.append(
                self._issue(
                    domain="topology_validation",
                    issue_type="UNEXPECTED_ORPHAN_SUBGRAPH",
                    severity="MEDIUM",
                    message="Disconnected subgraphs are not explicitly marked as allowed isolation.",
                    count=orphan_subgraph_count,
                )
            )

        if cycle_violation_nodes:
            issues.append(
                self._issue(
                    domain="topology_validation",
                    issue_type="CYCLE_POLICY_VIOLATION",
                    severity="MEDIUM",
                    message="Cycle nodes violate cycle allowance policy flags.",
                    count=len(cycle_violation_nodes),
                    sample_nodes=cycle_violation_nodes,
                )
            )

        if cyclic_island_count > 0:
            warnings.append("Cyclic disconnected islands were detected and tracked as topology risk.")

        if isolated_reports:
            warnings.append(
                "Disconnected islands were detected and classified (isolated_module/orphan_subgraph/cyclic_island)."
            )

        unreachable_ratio = self._safe_divide(len(unreachable), max(1, len(primitives.valid_node_ids)))
        di_unreachable_ratio = self._safe_divide(
            len(di_unreachable_edges),
            max(1, len([edge for edge in primitives.valid_edges if edge[2] == "DI"])),
        )
        unexpected_island_ratio = self._safe_divide(
            orphan_subgraph_node_count,
            max(1, len(primitives.valid_node_ids)),
        )
        cycle_violation_ratio = self._safe_divide(
            len(cycle_violation_nodes),
            max(1, len(primitives.valid_node_ids)),
        )

        domain_penalty = min(
            1.0,
            (self._clamp01(unreachable_ratio) * 0.60)
            + (self._clamp01(di_unreachable_ratio) * 0.20)
            + (self._clamp01(unexpected_island_ratio) * 0.15)
            + (self._clamp01(cycle_violation_ratio) * 0.15),
        )

        metrics = {
            "entrypoint_count": len(effective_entrypoints),
            "entrypoint_present_count": len(entrypoints_present),
            "entrypoint_missing_count": len(entrypoints_missing),
            "reachable_node_count": len(reachable),
            "unreachable_node_count": len(unreachable),
            "unreachable_nodes_ratio": self._round3(unreachable_ratio),
            "di_edge_count": len([edge for edge in primitives.valid_edges if edge[2] == "DI"]),
            "di_unreachable_edge_count": len(di_unreachable_edges),
            "di_unreachable_ratio": self._round3(di_unreachable_ratio),
            "cycle_node_count": len(cycle_nodes),
            "cycle_policy_violation_count": len(cycle_violation_nodes),
            "disconnected_island_count": len(isolated_reports),
            "isolated_module_count": isolated_module_count,
            "orphan_subgraph_count": orphan_subgraph_count,
            "cyclic_island_count": cyclic_island_count,
            "unexpected_island_count": orphan_subgraph_count,
            "isolated_module_node_count": isolated_module_node_count,
            "orphan_subgraph_node_count": orphan_subgraph_node_count,
            "cyclic_island_node_count": cyclic_island_node_count,
            "unexpected_island_node_count": orphan_subgraph_node_count,
            "unexpected_island_ratio": self._round3(unexpected_island_ratio),
            "cycle_violation_ratio": self._round3(cycle_violation_ratio),
            "domain_score": self._round3(self._clamp01(1.0 - domain_penalty)),
        }

        details = {
            "reachability": {
                "entrypoints_used": entrypoints_present,
                "entrypoints_missing": entrypoints_missing,
                "reachable_nodes": sorted(reachable)[: self._ISSUE_SAMPLE_LIMIT],
                "unreachable_nodes": unreachable[: self._ISSUE_SAMPLE_LIMIT],
                "di_unreachable_edges": [
                    {"from": src, "to": dst, "type": edge_type}
                    for src, dst, edge_type in sorted(di_unreachable_edges)[: self._ISSUE_SAMPLE_LIMIT]
                ],
            },
            "topology": {
                "cycle_nodes": sorted(cycle_nodes)[: self._ISSUE_SAMPLE_LIMIT],
                "cycle_policy_violations": cycle_violation_nodes[: self._ISSUE_SAMPLE_LIMIT],
                "isolation_report": sorted(
                    isolated_reports,
                    key=lambda item: (-int(item.get("size", 0)), str(item.get("classification", ""))),
                )[: self._ISSUE_SAMPLE_LIMIT],
            },
        }

        return {
            "domain": "topology_validation",
            "metrics": metrics,
            "issues": self._sort_issues(issues),
            "warnings": sorted(set(warnings)),
            "details": details,
        }

    def _evaluate_semantic_observations(
        self,
        topology_layer: LayerResult,
        dependency_layer: LayerResult,
    ) -> LayerResult:
        topology_metrics = topology_layer.get("metrics", {})
        dependency_metrics = dependency_layer.get("metrics", {})

        notes: List[str] = []
        warnings: List[str] = []

        ast_di_divergence = float(dependency_metrics.get("ast_di_divergence_score", 0.0) or 0.0)
        if ast_di_divergence > 0.0:
            if ast_di_divergence > self._AST_DI_DIVERGENCE_HARD_THRESHOLD:
                warnings.append(
                    "AST-vs-DI divergence exceeded architecture drift threshold and is treated as a first-class failure signal."
                )
            else:
                notes.append(
                    "AST-vs-DI divergence is present and tracked for explainability; it does not directly invalidate the system."
                )

        if int(topology_metrics.get("isolated_module_count", 0) or 0) > 0:
            notes.append("At least one disconnected island is explicitly marked as isolated_module (expected isolation).")

        if int(topology_metrics.get("orphan_subgraph_count", 0) or 0) > 0:
            warnings.append("Unexpected orphan_subgraph islands were observed and should be reviewed by policy.")

        if int(topology_metrics.get("cyclic_island_count", 0) or 0) > 0:
            warnings.append("cyclic_island classifications were observed; they are policy-dependent warnings.")

        warning_penalty = min(0.6, (len(warnings) * 0.18) + (self._clamp01(ast_di_divergence) * 0.10))
        domain_score = self._round3(self._clamp01(1.0 - warning_penalty))

        metrics = {
            "semantic_note_count": len(notes),
            "semantic_warning_count": len(warnings),
            "ast_di_divergence_score": self._round3(ast_di_divergence),
            "domain_score": domain_score,
        }

        details = {
            "notes": sorted(set(notes)),
            "island_classification_summary": {
                "isolated_module": int(topology_metrics.get("isolated_module_count", 0) or 0),
                "orphan_subgraph": int(topology_metrics.get("orphan_subgraph_count", 0) or 0),
                "cyclic_island": int(topology_metrics.get("cyclic_island_count", 0) or 0),
            },
            "ast_di_divergence_score": self._round3(ast_di_divergence),
        }

        return {
            "domain": "semantic_observations",
            "metrics": metrics,
            "issues": [],
            "warnings": sorted(set(warnings)),
            "details": details,
        }

    def _evaluate_execution_confidence(
        self,
        execution_evidence: Optional[Mapping[str, Any]],
        min_execution_confidence: float,
        entrypoints: Sequence[str],
    ) -> LayerResult:
        evidence = execution_evidence if isinstance(execution_evidence, Mapping) else {}

        runtime_validation_raw = evidence.get("runtime_validation")
        runtime_validation = runtime_validation_raw if isinstance(runtime_validation_raw, Mapping) else {}

        reconciliation_raw = evidence.get("runtime_static_reconciliation")
        reconciliation = reconciliation_raw if isinstance(reconciliation_raw, Mapping) else {}

        distribution_raw = evidence.get("distribution")
        distribution = distribution_raw if isinstance(distribution_raw, Mapping) else {}

        runtime_scenarios_raw = evidence.get("runtime_scenarios")
        runtime_scenarios = runtime_scenarios_raw if isinstance(runtime_scenarios_raw, Mapping) else {}

        scenario_validation_raw = evidence.get("scenario_validation")
        scenario_validation = scenario_validation_raw if isinstance(scenario_validation_raw, Mapping) else {}

        scenario_validation_warnings_raw = scenario_validation.get("warnings")
        scenario_validation_warnings = [
            str(item).strip()
            for item in (scenario_validation_warnings_raw if isinstance(scenario_validation_warnings_raw, list) else [])
            if str(item).strip()
        ]

        call_event_count = int(runtime_validation.get("call_event_count", 0) or 0)
        coverage_ratio = self._clamp01(float(runtime_validation.get("coverage_ratio", 0.0) or 0.0))
        reachable_ratio = self._clamp01(float(runtime_validation.get("reachable_ratio", 0.0) or 0.0))
        overlap_ratio = self._clamp01(float(reconciliation.get("overlap_ratio", 0.0) or 0.0))
        unique_mapped_callee_count = int(runtime_validation.get("unique_mapped_callee_count", 0) or 0)

        entrypoint_count = int(runtime_validation.get("entrypoint_count", 0) or 0)
        executed_entrypoint_count = int(runtime_validation.get("executed_entrypoint_count", 0) or 0)

        declared_entrypoints: List[str] = []
        runtime_entrypoints_raw = runtime_validation.get("entrypoints")
        if isinstance(runtime_entrypoints_raw, list):
            declared_entrypoints = self._normalize_entrypoints(runtime_entrypoints_raw)

        if not declared_entrypoints:
            scenario_entrypoints_raw = runtime_scenarios.get("entrypoint_paths")
            if isinstance(scenario_entrypoints_raw, list):
                declared_entrypoints = self._normalize_entrypoints(scenario_entrypoints_raw)

        if not declared_entrypoints:
            declared_entrypoints = self._normalize_entrypoints(entrypoints)

        executed_entrypoints: List[str] = []
        executed_entrypoints_known = False
        runtime_executed_entrypoints_raw = runtime_validation.get("executed_entrypoints")
        if isinstance(runtime_executed_entrypoints_raw, list):
            executed_entrypoints = self._normalize_entrypoints(runtime_executed_entrypoints_raw)
            executed_entrypoints_known = True

        if declared_entrypoints:
            entrypoint_count = len(declared_entrypoints)

        if executed_entrypoints_known:
            executed_entrypoint_count = len(executed_entrypoints)
        else:
            executed_entrypoint_count = min(entrypoint_count, executed_entrypoint_count)

        entrypoint_execution_ratio = self._safe_divide(executed_entrypoint_count, max(1, entrypoint_count))
        entrypoint_coverage_completeness = self._clamp01(entrypoint_execution_ratio if entrypoint_count > 0 else 1.0)

        scenario_rows_raw = runtime_scenarios.get("scenarios")
        scenario_rows = scenario_rows_raw if isinstance(scenario_rows_raw, list) else []
        scenario_paths: List[str] = []
        for item in scenario_rows:
            payload = item if isinstance(item, Mapping) else {}
            path_value = str(payload.get("path", "")).strip()
            if path_value:
                scenario_paths.append(path_value)

        expected_domains = sorted(self._domain_set_from_paths(declared_entrypoints + scenario_paths))
        covered_domains: List[str] = []
        if expected_domains:
            if executed_entrypoints_known:
                covered_domains = sorted(self._domain_set_from_paths(executed_entrypoints))
                domain_coverage_completeness = self._safe_divide(len(covered_domains), len(expected_domains))
            else:
                domain_coverage_completeness = entrypoint_coverage_completeness
                if domain_coverage_completeness >= 1.0:
                    covered_domains = list(expected_domains)
                elif domain_coverage_completeness > 0.0:
                    estimated = max(1, int(round(len(expected_domains) * domain_coverage_completeness)))
                    covered_domains = list(expected_domains[:estimated])
        else:
            domain_coverage_completeness = 1.0

        domain_coverage_completeness = self._clamp01(domain_coverage_completeness)

        runtime_source = str(
            runtime_validation.get("runtime_source", evidence.get("runtime_source", "unknown"))
        ).strip().lower()

        runtime_signal_present = call_event_count > 0

        runtime_confidence = self._clamp01(coverage_ratio * 10.0)
        edge_confidence = self._clamp01(overlap_ratio * 20.0)
        execution_confidence = self._clamp01(min(runtime_confidence, edge_confidence))

        call_frequency_score = self._call_frequency_score(
            call_event_count=call_event_count,
            unique_mapped_callee_count=unique_mapped_callee_count,
            runtime_validation=runtime_validation,
        )
        path_centrality_score = self._path_centrality_score(
            overlap_ratio=overlap_ratio,
            reachable_ratio=reachable_ratio,
        )
        scenario_importance_score = self._scenario_importance_score(
            runtime_scenarios=runtime_scenarios,
            entrypoint_coverage_completeness=entrypoint_coverage_completeness,
            scenario_warning_count=len(scenario_validation_warnings),
        )
        runtime_authority_score = self._clamp01(
            (call_frequency_score * 0.5)
            + (path_centrality_score * 0.3)
            + (scenario_importance_score * 0.2)
        )

        authority_adjusted_execution_confidence = self._clamp01(
            min(execution_confidence, runtime_authority_score)
        )

        scenario_coverage_completeness = self._clamp01(
            (entrypoint_coverage_completeness * 0.45)
            + (domain_coverage_completeness * 0.35)
            + (scenario_importance_score * 0.20)
        )

        if scenario_validation_warnings:
            scenario_coverage_completeness = self._clamp01(
                scenario_coverage_completeness
                * (1.0 - min(0.30, len(scenario_validation_warnings) * 0.08))
            )

        issues: List[Issue] = []
        warnings: List[str] = []

        if not runtime_signal_present:
            # Static-only verification can still run, but execution confidence cannot be assessed.
            runtime_confidence = 1.0
            edge_confidence = 1.0
            execution_confidence = 1.0
            authority_adjusted_execution_confidence = 1.0
            runtime_authority_score = 1.0
            call_frequency_score = 1.0
            path_centrality_score = 1.0
            scenario_importance_score = 1.0
            entrypoint_coverage_completeness = 1.0
            domain_coverage_completeness = 1.0
            scenario_coverage_completeness = 1.0
            warnings.append("Runtime execution evidence is unavailable; execution confidence gate was not applied.")
        else:
            if authority_adjusted_execution_confidence < min_execution_confidence:
                issues.append(
                    self._issue(
                        domain="execution_confidence",
                        issue_type="LOW_EXECUTION_CONFIDENCE",
                        severity="HIGH",
                        message=(
                            "Runtime execution coverage, edge overlap, and authority-weighted confidence "
                            "are insufficient for trustworthy system validation."
                        ),
                        count=1,
                    )
                )

            if entrypoint_count > 0 and executed_entrypoint_count < entrypoint_count:
                warnings.append(
                    "Only a subset of configured entrypoints was observed in runtime execution."
                )

            if entrypoint_coverage_completeness < self._MIN_ENTRYPOINT_COVERAGE_COMPLETENESS:
                issues.append(
                    self._issue(
                        domain="execution_confidence",
                        issue_type="ENTRYPOINT_COVERAGE_INCOMPLETE",
                        severity="HIGH",
                        message=(
                            "Observed runtime execution does not cover enough declared entrypoints for "
                            "architectural-confidence validation."
                        ),
                        count=max(0, entrypoint_count - executed_entrypoint_count),
                    )
                )

            if expected_domains and domain_coverage_completeness < self._MIN_DOMAIN_COVERAGE_COMPLETENESS:
                issues.append(
                    self._issue(
                        domain="execution_confidence",
                        issue_type="DOMAIN_COVERAGE_INCOMPLETE",
                        severity="MEDIUM",
                        message=(
                            "Runtime coverage misses key execution domains (API/CLI/background) and should be expanded."
                        ),
                        count=max(0, len(expected_domains) - len(covered_domains)),
                    )
                )

            if scenario_coverage_completeness < self._MIN_SCENARIO_COVERAGE_COMPLETENESS:
                issues.append(
                    self._issue(
                        domain="execution_confidence",
                        issue_type="SCENARIO_COVERAGE_INCOMPLETE",
                        severity="HIGH",
                        message=(
                            "Scenario coverage completeness is below minimum confidence threshold for runtime authority."
                        ),
                        count=1,
                    )
                )

            warnings.extend(scenario_validation_warnings)

        metrics = {
            "runtime_source": runtime_source,
            "runtime_signal_present": bool(runtime_signal_present),
            "call_event_count": call_event_count,
            "coverage_ratio": self._round3(coverage_ratio),
            "reachable_ratio": self._round3(reachable_ratio),
            "overlap_ratio": self._round3(overlap_ratio),
            "unique_mapped_callee_count": unique_mapped_callee_count,
            "runtime_confidence": self._round3(runtime_confidence),
            "edge_confidence": self._round3(edge_confidence),
            "execution_confidence": self._round3(execution_confidence),
            "authority_adjusted_execution_confidence": self._round3(authority_adjusted_execution_confidence),
            "runtime_authority_score": self._round3(runtime_authority_score),
            "call_frequency_score": self._round3(call_frequency_score),
            "path_centrality_score": self._round3(path_centrality_score),
            "scenario_importance_score": self._round3(scenario_importance_score),
            "entrypoint_count": entrypoint_count,
            "executed_entrypoint_count": executed_entrypoint_count,
            "entrypoint_execution_ratio": self._round3(entrypoint_execution_ratio),
            "entrypoint_coverage_completeness": self._round3(entrypoint_coverage_completeness),
            "domain_coverage_completeness": self._round3(domain_coverage_completeness),
            "scenario_coverage_completeness": self._round3(scenario_coverage_completeness),
            "expected_domain_count": len(expected_domains),
            "covered_domain_count": len(covered_domains),
            "expected_domains": expected_domains,
            "covered_domains": covered_domains,
            "scenario_warning_count": len(scenario_validation_warnings),
            "hot_count": int(distribution.get("HOT", 0) or 0),
            "warm_count": int(distribution.get("WARM", 0) or 0),
            "cold_count": int(distribution.get("COLD", 0) or 0),
            "dead_count": int(distribution.get("DEAD", 0) or 0),
            "min_execution_confidence": self._round3(min_execution_confidence),
            "domain_score": self._round3(authority_adjusted_execution_confidence),
        }

        details = {
            "formula": {
                "runtime_confidence": "min(1.0, coverage_ratio * 10.0)",
                "edge_confidence": "min(1.0, overlap_ratio * 20.0)",
                "execution_confidence": "min(runtime_confidence, edge_confidence)",
                "runtime_authority": "call_frequency*0.5 + path_centrality*0.3 + scenario_importance*0.2",
                "authority_adjusted_execution": "min(execution_confidence, runtime_authority)",
                "scenario_coverage": (
                    "entrypoint_coverage*0.45 + domain_coverage*0.35 + scenario_importance*0.20"
                ),
            },
            "gate": {
                "threshold": self._round3(min_execution_confidence),
                "applied": bool(
                    runtime_signal_present
                    and authority_adjusted_execution_confidence < min_execution_confidence
                ),
            },
            "entrypoints": {
                "declared": declared_entrypoints[: self._ISSUE_SAMPLE_LIMIT],
                "executed": executed_entrypoints[: self._ISSUE_SAMPLE_LIMIT],
            },
            "domain_coverage": {
                "expected_domains": expected_domains,
                "covered_domains": covered_domains,
                "executed_entrypoints_known": executed_entrypoints_known,
            },
        }

        return {
            "domain": "execution_confidence",
            "metrics": metrics,
            "issues": self._sort_issues(issues),
            "warnings": sorted(set(warnings)),
            "details": details,
        }

    def _evaluate_architectural_intent(
        self,
        primitives: GraphPrimitives,
        execution_evidence: Optional[Mapping[str, Any]],
    ) -> LayerResult:
        evidence = execution_evidence if isinstance(execution_evidence, Mapping) else {}
        architecture_raw = evidence.get("architecture_constraints")
        architecture_report = architecture_raw if isinstance(architecture_raw, Mapping) else {}

        if not architecture_report:
            fallback_payload = {
                "nodes": [{"id": node_id} for node_id in primitives.valid_node_ids],
                "edges": [
                    {"from": src, "to": dst, "type": edge_type}
                    for src, dst, edge_type in primitives.valid_edges
                ],
            }
            architecture_report = evaluate_architecture_constraints(fallback_payload)

        summary = (
            architecture_report.get("summary")
            if isinstance(architecture_report.get("summary"), Mapping)
            else {}
        )
        intent_model = (
            architecture_report.get("intent_model")
            if isinstance(architecture_report.get("intent_model"), Mapping)
            else {}
        )
        violations = (
            architecture_report.get("violations")
            if isinstance(architecture_report.get("violations"), list)
            else []
        )

        violation_count = int(summary.get("violation_count_total", summary.get("violation_count", 0)) or 0)
        violation_ratio = float(summary.get("violation_ratio", 0.0) or 0.0)
        coverage_ratio = float(
            summary.get(
                "constraint_coverage_ratio",
                intent_model.get("constraint_coverage_ratio", 0.0),
            )
            or 0.0
        )
        domain_score = self._round3(
            self._clamp01(float(summary.get("domain_score", 1.0) or 0.0))
        )

        issues: List[Issue] = []
        warnings: List[str] = []

        if violation_count > 0:
            severity = "LOW"
            if violation_ratio >= 0.25:
                severity = "HIGH"
            elif violation_ratio >= 0.10:
                severity = "MEDIUM"

            issues.append(
                self._issue(
                    domain="architectural_intent",
                    issue_type="ARCHITECTURE_CONSTRAINT_VIOLATION",
                    severity=severity,
                    message=(
                        "Architecture intent constraints were violated. "
                        "Review layer direction, ownership boundaries, and orchestrator mediation rules."
                    ),
                    count=violation_count,
                    sample_nodes=[
                        str(item.get("source_node_id", "")).strip()
                        for item in violations
                        if isinstance(item, Mapping)
                    ],
                )
            )

        report_warnings = architecture_report.get("warnings")
        if isinstance(report_warnings, list):
            warnings.extend(str(item).strip() for item in report_warnings if str(item).strip())

        if coverage_ratio <= 0.0:
            warnings.append("Architecture constraints had no layer coverage and were treated as informational.")

        metrics = {
            "violation_count": violation_count,
            "violation_ratio": self._round3(self._clamp01(violation_ratio)),
            "constraint_coverage_ratio": self._round3(self._clamp01(coverage_ratio)),
            "boundary_crossing_count": int(summary.get("boundary_crossing_count", 0) or 0),
            "domain_score": domain_score,
        }

        details = {
            "intent_model": intent_model,
            "rule_violation_counts": summary.get("rule_violation_counts", {}),
            "top_violations": [
                item
                for item in violations[: self._ISSUE_SAMPLE_LIMIT]
                if isinstance(item, Mapping)
            ],
        }

        return {
            "domain": "architectural_intent",
            "metrics": metrics,
            "issues": self._sort_issues(issues),
            "warnings": sorted(set(warnings)),
            "details": details,
        }

    def _evaluate_semantic_consistency(
        self,
        execution_evidence: Optional[Mapping[str, Any]],
    ) -> LayerResult:
        evidence = execution_evidence if isinstance(execution_evidence, Mapping) else {}
        semantic_raw = evidence.get("semantic_clusters")
        semantic_report = semantic_raw if isinstance(semantic_raw, Mapping) else {}

        summary = (
            semantic_report.get("summary")
            if isinstance(semantic_report.get("summary"), Mapping)
            else {}
        )
        clusters = semantic_report.get("clusters") if isinstance(semantic_report.get("clusters"), list) else []
        duplicate_clusters = (
            semantic_report.get("duplicate_intent_clusters")
            if isinstance(semantic_report.get("duplicate_intent_clusters"), list)
            else []
        )
        abstraction_collisions = (
            semantic_report.get("abstraction_collisions")
            if isinstance(semantic_report.get("abstraction_collisions"), list)
            else []
        )

        domain_score = self._round3(
            self._clamp01(float(summary.get("domain_score", 1.0) or 0.0))
        )

        duplicate_count = int(
            summary.get("duplicate_intent_cluster_count", len(duplicate_clusters))
            or 0
        )
        abstraction_collision_count = int(
            summary.get("abstraction_collision_count", len(abstraction_collisions))
            or 0
        )

        issues: List[Issue] = []
        warnings: List[str] = []

        if duplicate_count > 0:
            issues.append(
                self._issue(
                    domain="semantic_consistency",
                    issue_type="DUPLICATE_INTENT_CLUSTERS",
                    severity="MEDIUM",
                    message="Conceptually overlapping cross-context clusters suggest duplicated or fragmented domain intent.",
                    count=duplicate_count,
                    sample_nodes=[
                        str(item)
                        for cluster in duplicate_clusters[: self._ISSUE_SAMPLE_LIMIT]
                        for item in (cluster.get("members") if isinstance(cluster, Mapping) else [])[:2]
                    ],
                )
            )

        if abstraction_collision_count > 0:
            issues.append(
                self._issue(
                    domain="semantic_consistency",
                    issue_type="FAKE_ABSTRACTION_COLLISION",
                    severity="MEDIUM",
                    message="Multiple abstraction styles represent the same concept root, indicating semantic inconsistency.",
                    count=abstraction_collision_count,
                    sample_nodes=[
                        str(item.get("concept_key", "")).strip()
                        for item in abstraction_collisions
                        if isinstance(item, Mapping)
                    ],
                )
            )

        report_notes = semantic_report.get("notes")
        if isinstance(report_notes, list):
            warnings.extend(str(item).strip() for item in report_notes if str(item).strip())

        if not semantic_report:
            warnings.append("Semantic clustering report was unavailable; semantic consistency checks were informational.")

        metrics = {
            "cluster_count": int(summary.get("cluster_count", len(clusters)) or 0),
            "cross_context_cluster_count": int(summary.get("cross_context_cluster_count", 0) or 0),
            "duplicate_intent_cluster_count": duplicate_count,
            "abstraction_collision_count": abstraction_collision_count,
            "domain_score": domain_score,
        }

        details = {
            "duplicate_intent_clusters": [
                item
                for item in duplicate_clusters[: self._ISSUE_SAMPLE_LIMIT]
                if isinstance(item, Mapping)
            ],
            "abstraction_collisions": [
                item
                for item in abstraction_collisions[: self._ISSUE_SAMPLE_LIMIT]
                if isinstance(item, Mapping)
            ],
        }

        return {
            "domain": "semantic_consistency",
            "metrics": metrics,
            "issues": self._sort_issues(issues),
            "warnings": sorted(set(warnings)),
            "details": details,
        }

    def _evaluate_causal_flow(
        self,
        execution_evidence: Optional[Mapping[str, Any]],
        runtime_signal_present: bool,
    ) -> LayerResult:
        evidence = execution_evidence if isinstance(execution_evidence, Mapping) else {}
        causal_raw = evidence.get("causal_flow")
        causal_report = causal_raw if isinstance(causal_raw, Mapping) else {}

        summary = (
            causal_report.get("summary")
            if isinstance(causal_report.get("summary"), Mapping)
            else {}
        )
        workflows = (
            causal_report.get("workflows")
            if isinstance(causal_report.get("workflows"), list)
            else []
        )

        domain_score = self._round3(
            self._clamp01(float(summary.get("domain_score", 1.0) or 0.0))
        )

        workflow_count = int(summary.get("workflow_count", len(workflows)) or 0)
        role_coverage_ratio = float(summary.get("role_coverage_ratio", 0.0) or 0.0)
        direct_api_to_persistence_count = int(summary.get("direct_api_to_persistence_count", 0) or 0)
        analysis_enforced = bool(summary.get("analysis_enforced", runtime_signal_present))

        issues: List[Issue] = []
        warnings: List[str] = []

        raw_issues = causal_report.get("issues")
        if isinstance(raw_issues, list):
            for item in raw_issues:
                payload = item if isinstance(item, Mapping) else {}
                issue_type = str(payload.get("type", "CAUSAL_FLOW_SIGNAL")).strip().upper() or "CAUSAL_FLOW_SIGNAL"
                severity = str(payload.get("severity", "LOW")).strip().upper() or "LOW"
                message = str(payload.get("message", "Causal flow issue detected.")).strip() or "Causal flow issue detected."
                issues.append(
                    self._issue(
                        domain="causal_flow",
                        issue_type=issue_type,
                        severity=severity,
                        message=message,
                        count=1,
                    )
                )

        raw_warnings = causal_report.get("warnings")
        if isinstance(raw_warnings, list):
            warnings.extend(str(item).strip() for item in raw_warnings if str(item).strip())

        if runtime_signal_present and analysis_enforced and workflow_count <= 0:
            issues.append(
                self._issue(
                    domain="causal_flow",
                    issue_type="NO_WORKFLOW_RECONSTRUCTION",
                    severity="HIGH",
                    message="Runtime evidence is present but no causal workflow chain was reconstructed.",
                    count=1,
                )
            )

        if not runtime_signal_present:
            warnings.append("Runtime call signal is unavailable; causal flow checks were not enforced.")

        metrics = {
            "runtime_signal_present": bool(runtime_signal_present),
            "analysis_enforced": bool(analysis_enforced),
            "workflow_count": workflow_count,
            "role_coverage_ratio": self._round3(self._clamp01(role_coverage_ratio)),
            "direct_api_to_persistence_count": direct_api_to_persistence_count,
            "domain_score": domain_score,
        }

        details = {
            "observed_roles": summary.get("observed_roles", []),
            "workflow_templates": (
                causal_report.get("workflow_templates")
                if isinstance(causal_report.get("workflow_templates"), list)
                else []
            ),
            "sample_workflows": [
                item
                for item in workflows[: self._ISSUE_SAMPLE_LIMIT]
                if isinstance(item, Mapping)
            ],
        }

        return {
            "domain": "causal_flow",
            "metrics": metrics,
            "issues": self._sort_issues(issues),
            "warnings": sorted(set(warnings)),
            "details": details,
        }

    def _compute_trust_model(
        self,
        structural_layer: LayerResult,
        dependency_layer: LayerResult,
        topology_layer: LayerResult,
        semantic_layer: LayerResult,
        execution_layer: LayerResult,
        min_execution_confidence: float,
    ) -> Dict[str, Any]:
        base_domain_scores = {
            "structural_integrity": self._domain_score(structural_layer),
            "topology_validation": self._domain_score(topology_layer),
            "dependency_consistency": self._domain_score(dependency_layer),
            "semantic_observations": self._domain_score(semantic_layer),
        }

        execution_confidence = self._domain_score(execution_layer)
        execution_metrics = execution_layer.get("metrics", {})
        dependency_metrics = dependency_layer.get("metrics", {})

        runtime_signal_present = bool(execution_metrics.get("runtime_signal_present", False))
        runtime_authority_score = self._clamp01(
            float(execution_metrics.get("runtime_authority_score", 1.0) or 0.0)
        )
        coverage_ratio = self._clamp01(float(execution_metrics.get("coverage_ratio", 0.0) or 0.0))
        ast_di_divergence = self._clamp01(float(dependency_metrics.get("ast_di_divergence_score", 0.0) or 0.0))

        domain_scores = dict(base_domain_scores)
        domain_scores["execution_confidence"] = execution_confidence

        weighted_contributions = {
            domain: self._round3(base_domain_scores[domain] * self._DOMAIN_WEIGHTS[domain])
            for domain in base_domain_scores
        }

        base_trust_score = self._round3(sum(weighted_contributions.values()))
        execution_gate_applied = execution_confidence < min_execution_confidence
        execution_adjustment = execution_confidence if execution_gate_applied else 1.0

        runtime_authority_adjustment = 1.0
        if runtime_signal_present:
            runtime_authority_adjustment = self._clamp01(0.5 + (runtime_authority_score * 0.5))

        coverage_hard_gate_applied = coverage_ratio < self._COVERAGE_HARD_FLOOR
        coverage_penalty_multiplier = (
            self._COVERAGE_HARD_FLOOR_TRUST_MULTIPLIER if coverage_hard_gate_applied else 1.0
        )

        architecture_drift_triggered = ast_di_divergence > self._AST_DI_DIVERGENCE_HARD_THRESHOLD
        architecture_drift_penalty = (
            self._ARCHITECTURE_DRIFT_TRUST_PENALTY if architecture_drift_triggered else 0.0
        )

        trust_before_architecture_drift = self._round3(
            base_trust_score
            * execution_adjustment
            * runtime_authority_adjustment
            * coverage_penalty_multiplier
        )
        trust_score = self._round3(
            self._clamp01(trust_before_architecture_drift - architecture_drift_penalty)
        )

        penalties = {
            f"{domain}_penalty": self._round3(1.0 - score)
            for domain, score in domain_scores.items()
        }
        penalties["runtime_authority_penalty"] = self._round3(1.0 - runtime_authority_score)
        penalties["coverage_hard_gate_penalty"] = self._round3(
            1.0 - float(coverage_penalty_multiplier)
        )
        penalties["architecture_drift_penalty"] = self._round3(architecture_drift_penalty)

        return {
            "formula": (
                "base_trust = structural*0.35 + topology*0.25 + resolver*0.25 + semantic*0.15; "
                "if execution_confidence < min_execution_confidence then execution_adjustment = execution_confidence; "
                "if runtime signal exists then runtime_authority_adjustment = 0.5 + runtime_authority*0.5; "
                "if coverage_ratio < coverage_hard_floor then coverage_multiplier = 0.5; "
                "if ast_di_divergence > 0.5 then subtract architecture_drift_penalty(0.2)"
            ),
            "base_trust_score": base_trust_score,
            "trust_score": trust_score,
            "weights": dict(self._DOMAIN_WEIGHTS),
            "domain_scores": domain_scores,
            "weighted_contributions": weighted_contributions,
            "execution_adjustment": self._round3(execution_adjustment),
            "execution_gate_applied": execution_gate_applied,
            "runtime_signal_present": runtime_signal_present,
            "runtime_authority_score": self._round3(runtime_authority_score),
            "runtime_authority_adjustment": self._round3(runtime_authority_adjustment),
            "coverage_ratio": self._round3(coverage_ratio),
            "coverage_hard_floor": self._round3(self._COVERAGE_HARD_FLOOR),
            "coverage_hard_gate_applied": coverage_hard_gate_applied,
            "coverage_penalty_multiplier": self._round3(coverage_penalty_multiplier),
            "ast_di_divergence_score": self._round3(ast_di_divergence),
            "architecture_drift_threshold": self._round3(self._AST_DI_DIVERGENCE_HARD_THRESHOLD),
            "architecture_drift_triggered": architecture_drift_triggered,
            "architecture_drift_penalty": self._round3(architecture_drift_penalty),
            "trust_before_architecture_drift": trust_before_architecture_drift,
            "min_execution_confidence": self._round3(min_execution_confidence),
            "penalties": penalties,
            "scores": {
                "structural_integrity": domain_scores["structural_integrity"],
                "dependency_consistency": domain_scores["dependency_consistency"],
                "topology_validation": domain_scores["topology_validation"],
                "semantic_observations": domain_scores["semantic_observations"],
                "execution_confidence": execution_confidence,
                "runtime_authority": self._round3(runtime_authority_score),
                "structural": domain_scores["structural_integrity"],
                "reachability": domain_scores["topology_validation"],
                "resolver": domain_scores["dependency_consistency"],
                "semantic": domain_scores["semantic_observations"],
                "execution": execution_confidence,
                "trust": trust_score,
            },
        }

    def _apply_policy(
        self,
        structural_layer: LayerResult,
        dependency_layer: LayerResult,
        topology_layer: LayerResult,
        execution_layer: LayerResult,
        architecture_layer: LayerResult,
        semantic_consistency_layer: LayerResult,
        causal_flow_layer: LayerResult,
        trust_model: Dict[str, Any],
        min_trust: float,
        min_execution_confidence: float,
    ) -> Dict[str, Any]:
        structural_metrics = structural_layer.get("metrics", {})
        dependency_metrics = dependency_layer.get("metrics", {})
        topology_metrics = topology_layer.get("metrics", {})
        execution_metrics = execution_layer.get("metrics", {})
        architecture_metrics = architecture_layer.get("metrics", {})
        semantic_consistency_metrics = semantic_consistency_layer.get("metrics", {})
        causal_flow_metrics = causal_flow_layer.get("metrics", {})

        trust_score = float(trust_model.get("trust_score", 0.0) or 0.0)
        execution_confidence = float(
            execution_metrics.get(
                "execution_confidence",
                (trust_model.get("scores") if isinstance(trust_model.get("scores"), dict) else {}).get(
                    "execution_confidence",
                    1.0,
                ),
            )
            or 0.0
        )
        runtime_authority_score = float(execution_metrics.get("runtime_authority_score", 1.0) or 0.0)
        coverage_ratio = float(execution_metrics.get("coverage_ratio", 0.0) or 0.0)
        runtime_signal_present = bool(execution_metrics.get("runtime_signal_present", False))
        entrypoint_coverage_completeness = float(
            execution_metrics.get("entrypoint_coverage_completeness", 1.0) or 0.0
        )
        domain_coverage_completeness = float(
            execution_metrics.get("domain_coverage_completeness", 1.0) or 0.0
        )
        scenario_coverage_completeness = float(
            execution_metrics.get("scenario_coverage_completeness", 1.0) or 0.0
        )
        expected_domain_count = int(execution_metrics.get("expected_domain_count", 0) or 0)

        ast_di_divergence = float(dependency_metrics.get("ast_di_divergence_score", 0.0) or 0.0)
        architecture_drift_triggered = bool(
            trust_model.get(
                "architecture_drift_triggered",
                ast_di_divergence > self._AST_DI_DIVERGENCE_HARD_THRESHOLD,
            )
        )
        coverage_hard_gate_applied = bool(
            trust_model.get("coverage_hard_gate_applied", coverage_ratio < self._COVERAGE_HARD_FLOOR)
        )

        hard_fail_reasons: List[str] = []
        soft_fail_reasons: List[str] = []

        if int(structural_metrics.get("valid_node_count", 0) or 0) <= 0:
            hard_fail_reasons.append("No valid canonical nodes available (catastrophic graph corruption).")

        missing_di_node_ratio = float(dependency_metrics.get("missing_di_node_ratio", 0.0) or 0.0)
        if missing_di_node_ratio > 0.50:
            hard_fail_reasons.append("Dependency rule violation threshold exceeded for missing DI node references.")

        unexpected_island_ratio = float(topology_metrics.get("unexpected_island_ratio", 0.0) or 0.0)
        if unexpected_island_ratio > 0.40:
            hard_fail_reasons.append("Unapproved isolated/orphan subgraph ratio exceeded policy threshold.")

        architecture_violation_ratio = float(architecture_metrics.get("violation_ratio", 0.0) or 0.0)
        architecture_coverage_ratio = float(architecture_metrics.get("constraint_coverage_ratio", 0.0) or 0.0)
        if architecture_coverage_ratio >= 0.20 and architecture_violation_ratio > 0.60:
            hard_fail_reasons.append("Architecture constraint violation ratio exceeded hard threshold.")

        if coverage_hard_gate_applied:
            hard_fail_reasons.append(
                "Runtime coverage ratio fell below hard floor 0.300; system cannot be accepted as valid."
            )

        if runtime_signal_present and entrypoint_coverage_completeness < self._MIN_ENTRYPOINT_COVERAGE_COMPLETENESS:
            hard_fail_reasons.append(
                "Entrypoint coverage completeness is below hard threshold 0.300."
            )

        if trust_score < min_trust:
            soft_fail_reasons.append(
                f"Trust score {trust_score:.3f} is below minimum threshold {min_trust:.3f}."
            )

        if execution_confidence < min_execution_confidence:
            soft_fail_reasons.append(
                "Execution confidence "
                f"{execution_confidence:.3f} is below minimum threshold {min_execution_confidence:.3f}."
            )

        if runtime_signal_present and expected_domain_count > 0 and (
            domain_coverage_completeness < self._MIN_DOMAIN_COVERAGE_COMPLETENESS
        ):
            soft_fail_reasons.append(
                "Domain coverage completeness (API/CLI/background) is below minimum threshold 0.340."
            )

        if runtime_signal_present and (
            scenario_coverage_completeness < self._MIN_SCENARIO_COVERAGE_COMPLETENESS
        ):
            soft_fail_reasons.append(
                "Scenario coverage completeness is below minimum threshold 0.550."
            )

        if architecture_drift_triggered:
            soft_fail_reasons.append(
                "Architecture drift detected: AST/DI divergence exceeded threshold 0.500."
            )

        if int(topology_metrics.get("cycle_policy_violation_count", 0) or 0) > 0:
            soft_fail_reasons.append("Cycle policy violations detected (warning-level unless threshold escalates).")

        architecture_score = float(architecture_metrics.get("domain_score", 1.0) or 0.0)
        if architecture_coverage_ratio >= 0.20 and architecture_score < 0.60:
            soft_fail_reasons.append(
                "Architectural intent score is below minimum threshold 0.600."
            )

        semantic_consistency_score = float(semantic_consistency_metrics.get("domain_score", 1.0) or 0.0)
        abstraction_collision_count = int(semantic_consistency_metrics.get("abstraction_collision_count", 0) or 0)
        if semantic_consistency_score < 0.55 and abstraction_collision_count > 0:
            soft_fail_reasons.append(
                "Semantic consistency score is below minimum threshold 0.550 with abstraction collisions present."
            )

        causal_flow_score = float(causal_flow_metrics.get("domain_score", 1.0) or 0.0)
        causal_flow_enforced = bool(causal_flow_metrics.get("analysis_enforced", False))
        if causal_flow_enforced and causal_flow_score < 0.40:
            soft_fail_reasons.append(
                "Causal flow score is below minimum threshold 0.400 under enforced runtime analysis."
            )

        critical_failure = len(hard_fail_reasons) > 0
        system_valid = (
            (not critical_failure)
            and (trust_score >= min_trust)
            and (execution_confidence >= min_execution_confidence)
        )

        return {
            "system_valid": system_valid,
            "critical_failure": critical_failure,
            "hard_fail_reasons": sorted(set(hard_fail_reasons)),
            "soft_fail_reasons": sorted(set(soft_fail_reasons)),
            "thresholds": {
                "min_trust": self._round3(min_trust),
                "min_execution_confidence": self._round3(min_execution_confidence),
                "min_runtime_coverage_hard_floor": self._round3(self._COVERAGE_HARD_FLOOR),
                "min_entrypoint_coverage_completeness": self._round3(self._MIN_ENTRYPOINT_COVERAGE_COMPLETENESS),
                "min_domain_coverage_completeness": self._round3(self._MIN_DOMAIN_COVERAGE_COMPLETENESS),
                "min_scenario_coverage_completeness": self._round3(self._MIN_SCENARIO_COVERAGE_COMPLETENESS),
                "max_ast_di_divergence_before_drift": self._round3(self._AST_DI_DIVERGENCE_HARD_THRESHOLD),
                "max_missing_di_node_ratio": 0.50,
                "max_unexpected_island_ratio": 0.40,
                "max_architecture_violation_ratio": 0.60,
                "min_architecture_intent": 0.60,
                "min_semantic_consistency": 0.55,
                "min_causal_flow": 0.40,
            },
            "policy_metrics": {
                "trust_score": self._round3(trust_score),
                "execution_confidence": self._round3(execution_confidence),
                "runtime_authority_score": self._round3(runtime_authority_score),
                "coverage_ratio": self._round3(coverage_ratio),
                "coverage_hard_gate_applied": bool(coverage_hard_gate_applied),
                "entrypoint_coverage_completeness": self._round3(entrypoint_coverage_completeness),
                "domain_coverage_completeness": self._round3(domain_coverage_completeness),
                "scenario_coverage_completeness": self._round3(scenario_coverage_completeness),
                "expected_domain_count": expected_domain_count,
                "ast_di_divergence_score": self._round3(ast_di_divergence),
                "architecture_drift_triggered": bool(architecture_drift_triggered),
                "missing_di_node_ratio": self._round3(missing_di_node_ratio),
                "unexpected_island_ratio": self._round3(unexpected_island_ratio),
                "architecture_violation_ratio": self._round3(architecture_violation_ratio),
                "architecture_intent_score": self._round3(architecture_score),
                "semantic_consistency_score": self._round3(semantic_consistency_score),
                "causal_flow_score": self._round3(causal_flow_score),
            },
        }

    def _build_failure_analysis(
        self,
        structural_layer: LayerResult,
        dependency_layer: LayerResult,
        topology_layer: LayerResult,
        semantic_layer: LayerResult,
        execution_layer: LayerResult,
        architecture_layer: LayerResult,
        semantic_consistency_layer: LayerResult,
        causal_flow_layer: LayerResult,
        trust_model: Dict[str, Any],
    ) -> Dict[str, Any]:
        layers = {
            "structural_integrity": structural_layer,
            "dependency_consistency": dependency_layer,
            "topology_validation": topology_layer,
            "semantic_observations": semantic_layer,
            "execution_confidence": execution_layer,
            "architectural_intent": architecture_layer,
            "semantic_consistency": semantic_consistency_layer,
            "causal_flow": causal_flow_layer,
        }

        records: List[Dict[str, Any]] = []
        for domain, layer in layers.items():
            score = self._domain_score(layer)
            impact_weight = float(self._FAILURE_IMPACT_WEIGHTS.get(domain, 0.0) or 0.0)
            impact = self._round3((1.0 - score) * impact_weight)
            has_signal = impact > 0.0 or bool(layer.get("issues")) or bool(layer.get("warnings"))
            if not has_signal:
                continue

            records.append(
                {
                    "domain": domain,
                    "reason": self._derive_domain_reason(layer),
                    "impact_score": impact,
                    "stage_index": self._DOMAIN_ORDER.get(domain, 999),
                }
            )

        if bool(trust_model.get("architecture_drift_triggered", False)):
            ast_di_divergence = float(trust_model.get("ast_di_divergence_score", 0.0) or 0.0)
            drift_penalty = float(trust_model.get("architecture_drift_penalty", 0.0) or 0.0)
            records.append(
                {
                    "domain": "architecture_drift",
                    "reason": (
                        "AST_DI_DIVERGENCE_ESCALATED: "
                        f"score {ast_di_divergence:.3f} exceeded threshold "
                        f"{self._AST_DI_DIVERGENCE_HARD_THRESHOLD:.3f}."
                    ),
                    "impact_score": self._round3(max(drift_penalty, 0.12)),
                    "stage_index": self._DOMAIN_ORDER.get("architecture_drift", 2),
                }
            )

        ranked = sorted(
            records,
            key=lambda item: (
                -float(item.get("impact_score", 0.0)),
                int(item.get("stage_index", 999)),
                str(item.get("domain", "")),
            ),
        )

        ranked_domains: List[Dict[str, Any]] = []
        for index, item in enumerate(ranked, start=1):
            ranked_domains.append(
                {
                    "rank": index,
                    "domain": item["domain"],
                    "reason": item["reason"],
                    "impact_score": self._round3(float(item.get("impact_score", 0.0) or 0.0)),
                }
            )

        primary = ranked_domains[0] if ranked_domains else None
        primary_cause = "none"
        if primary:
            primary_cause = f"{primary['domain']}:{primary['reason']}"

        causal_chain = [
            {
                "stage": item["domain"],
                "failure": item["reason"],
                "impact_score": self._round3(float(item.get("impact_score", 0.0) or 0.0)),
            }
            for item in sorted(records, key=lambda value: int(value.get("stage_index", 999)))
        ]

        return {
            "primary_cause": primary_cause,
            "ranked_domains": ranked_domains,
            "failure_domains": [item["domain"] for item in ranked_domains],
            "causal_chain": causal_chain,
            "scores": trust_model.get("scores", {}),
        }

    def _build_legacy_status(self, structural_layer: LayerResult) -> Dict[str, Any]:
        metrics = structural_layer.get("metrics", {})
        structural_critical = int(metrics.get("valid_node_count", 0) or 0) <= 0

        if structural_critical:
            status = "INVALID_STRUCTURAL"
        elif structural_layer.get("issues"):
            status = "DEGRADED_STRUCTURAL"
        else:
            status = "VALID"

        return {
            "status": status,
            "critical_failure": structural_critical,
        }

    def _infer_entrypoints(self, node_map: Dict[str, Dict[str, Any]]) -> List[str]:
        inferred: List[str] = []

        for node_id, node in sorted(node_map.items(), key=lambda item: item[0]):
            if self._is_entrypoint(node):
                inferred.append(node_id)

        return inferred

    def _is_entrypoint(self, node: Dict[str, Any]) -> bool:
        if not isinstance(node, dict):
            return False

        for key in ("is_entrypoint", "entrypoint"):
            value = node.get(key)
            if isinstance(value, bool) and value:
                return True

        role = str(node.get("role") or "").strip().upper()
        if role in {"ENTRYPOINT", "ROOT"}:
            return True

        metadata = node.get("metadata")
        if isinstance(metadata, dict):
            for key in ("is_entrypoint", "entrypoint"):
                value = metadata.get(key)
                if isinstance(value, bool) and value:
                    return True

        tags = node.get("tags")
        if isinstance(tags, list):
            normalized_tags = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
            if normalized_tags.intersection({"entrypoint", "root", "startup"}):
                return True

        return False

    def _is_valid_namespace(self, node_id: str) -> bool:
        if not node_id or not node_id.startswith("canonical://"):
            return False

        suffix = node_id[len("canonical://") :]
        if not suffix:
            return False

        slash_index = suffix.find("/")
        return slash_index > 0

    def _get_namespace(self, node_id: str) -> str:
        if not self._is_valid_namespace(node_id):
            return ""

        suffix = node_id[len("canonical://") :]
        slash_index = suffix.find("/")
        if slash_index <= 0:
            return ""

        return suffix[:slash_index]

    def _is_explicitly_isolated(self, node: Dict[str, Any]) -> bool:
        if not isinstance(node, dict):
            return False

        for key in ("allow_isolated", "isolated_allowed", "expected_isolation"):
            value = node.get(key)
            if isinstance(value, bool) and value:
                return True

        metadata = node.get("metadata")
        if isinstance(metadata, dict):
            for key in ("allow_isolated", "isolated_allowed", "expected_isolation"):
                value = metadata.get(key)
                if isinstance(value, bool) and value:
                    return True

        tags = node.get("tags")
        if isinstance(tags, list):
            normalized_tags = {str(tag).strip().lower() for tag in tags if str(tag).strip()}
            if "isolated_module" in normalized_tags or "expected_isolation" in normalized_tags:
                return True

        return False

    def _is_cycle_allowed(self, node: Dict[str, Any]) -> bool:
        if not isinstance(node, dict):
            return False

        for key in ("allow_cycle", "cycle_allowed"):
            value = node.get(key)
            if isinstance(value, bool) and value:
                return True

        metadata = node.get("metadata")
        if isinstance(metadata, dict):
            for key in ("allow_cycle", "cycle_allowed"):
                value = metadata.get(key)
                if isinstance(value, bool) and value:
                    return True

        return False

    def _bfs_reachable(self, adjacency: Dict[str, Set[str]], starts: Sequence[str]) -> Set[str]:
        visited: Set[str] = set()
        queue: deque[str] = deque()

        for start in sorted(set(starts)):
            if start in adjacency and start not in visited:
                visited.add(start)
                queue.append(start)

        while queue:
            current = queue.popleft()
            for nxt in sorted(adjacency.get(current, set())):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)

        return visited

    def _find_cycle_nodes(self, node_ids: Sequence[str], adjacency: Dict[str, Set[str]]) -> List[str]:
        in_degree = {node_id: 0 for node_id in node_ids}

        for src in node_ids:
            for dst in adjacency.get(src, set()):
                if dst in in_degree:
                    in_degree[dst] += 1

        queue: deque[str] = deque(sorted(node_id for node_id, deg in in_degree.items() if deg == 0))
        processed: Set[str] = set()

        while queue:
            current = queue.popleft()
            processed.add(current)

            for dst in sorted(adjacency.get(current, set())):
                if dst not in in_degree:
                    continue
                in_degree[dst] -= 1
                if in_degree[dst] == 0:
                    queue.append(dst)

        return sorted(node_id for node_id in node_ids if node_id not in processed)

    def _undirected_components(
        self,
        node_ids: Sequence[str],
        adjacency: Dict[str, Set[str]],
    ) -> List[List[str]]:
        undirected: Dict[str, Set[str]] = {node_id: set() for node_id in node_ids}

        for src in node_ids:
            for dst in adjacency.get(src, set()):
                if dst not in undirected:
                    continue
                undirected[src].add(dst)
                undirected[dst].add(src)

        visited: Set[str] = set()
        components: List[List[str]] = []

        for start in sorted(node_ids):
            if start in visited:
                continue

            queue: deque[str] = deque([start])
            visited.add(start)
            component: List[str] = []

            while queue:
                current = queue.popleft()
                component.append(current)

                for nxt in sorted(undirected.get(current, set())):
                    if nxt not in visited:
                        visited.add(nxt)
                        queue.append(nxt)

            components.append(sorted(component))

        return components

    def _classify_island(
        self,
        component_nodes: Sequence[str],
        node_map: Dict[str, Dict[str, Any]],
        cycle_nodes: Set[str],
    ) -> str:
        if component_nodes and all(
            self._is_explicitly_isolated(node_map.get(node_id, {}))
            for node_id in component_nodes
        ):
            return "isolated_module"

        if any(node_id in cycle_nodes for node_id in component_nodes):
            return "cyclic_island"

        return "orphan_subgraph"

    def _normalize_entrypoints(self, values: Sequence[Any]) -> List[str]:
        normalized: List[str] = []
        seen: Set[str] = set()

        for item in values:
            value = str(item or "").strip()
            if not value:
                continue
            candidate = value.replace("\\", "/")
            if candidate in seen:
                continue
            seen.add(candidate)
            normalized.append(candidate)

        return sorted(normalized)

    def _domain_set_from_paths(self, values: Sequence[str]) -> Set[str]:
        domains: Set[str] = set()
        for item in values:
            domain = self._entrypoint_domain(item)
            if domain:
                domains.add(domain)
        return domains

    def _entrypoint_domain(self, value: str) -> str:
        normalized = str(value or "").strip().lower().replace("\\", "/")
        if not normalized:
            return ""

        normalized = normalized.replace("canonical://", "")
        tokens = [token for token in normalized.replace(":", "/").split("/") if token]
        token_set = set(tokens)

        if token_set.intersection({"api", "rest", "http", "web"}):
            return "api"
        if token_set.intersection({"cli", "command", "commands", "cmd"}):
            return "cli"
        if token_set.intersection(
            {
                "job",
                "jobs",
                "worker",
                "workers",
                "scheduler",
                "cron",
                "background",
                "queue",
                "queues",
                "task",
                "tasks",
            }
        ):
            return "background"

        return ""

    def _call_frequency_score(
        self,
        call_event_count: int,
        unique_mapped_callee_count: int,
        runtime_validation: Mapping[str, Any],
    ) -> float:
        requirements_raw = runtime_validation.get("requirements")
        requirements = requirements_raw if isinstance(requirements_raw, Mapping) else {}

        min_call_events = int(requirements.get("min_call_events", 50) or 50)
        min_unique_callees = int(requirements.get("min_unique_callees", 10) or 10)

        call_score = self._clamp01(self._safe_divide(call_event_count, max(1, min_call_events)))

        if unique_mapped_callee_count > 0:
            unique_score = self._clamp01(
                self._safe_divide(unique_mapped_callee_count, max(1, min_unique_callees))
            )
        else:
            unique_score = call_score

        return self._clamp01((call_score * 0.70) + (unique_score * 0.30))

    def _path_centrality_score(self, overlap_ratio: float, reachable_ratio: float) -> float:
        overlap_score = self._clamp01(overlap_ratio * 5.0)
        reachable_score = self._clamp01(reachable_ratio * 3.0)
        return self._clamp01(max(overlap_score, reachable_score))

    def _scenario_importance_score(
        self,
        runtime_scenarios: Mapping[str, Any],
        entrypoint_coverage_completeness: float,
        scenario_warning_count: int,
    ) -> float:
        summary_raw = runtime_scenarios.get("summary")
        summary = summary_raw if isinstance(summary_raw, Mapping) else {}

        scenarios_raw = runtime_scenarios.get("scenarios")
        scenarios = scenarios_raw if isinstance(scenarios_raw, list) else []

        priority_scores: List[float] = []
        for item in scenarios:
            payload = item if isinstance(item, Mapping) else {}
            candidate = payload.get("priority_score")
            if isinstance(candidate, (int, float)):
                priority_scores.append(max(0.0, float(candidate)))

        priority_coverage = 0.0
        if priority_scores:
            max_priority = max(priority_scores)
            if max_priority > 0.0:
                normalized = [self._clamp01(score / max_priority) for score in priority_scores]
                priority_coverage = self._safe_divide(sum(normalized), max(1, len(normalized)))
        else:
            selected_node_count = int(summary.get("selected_node_count", 0) or 0)
            max_scenarios = int(summary.get("max_scenarios", 0) or 0)
            if max_scenarios > 0:
                priority_coverage = self._safe_divide(selected_node_count, max_scenarios)
            else:
                priority_coverage = self._clamp01(entrypoint_coverage_completeness)

        score = self._clamp01(
            (self._clamp01(priority_coverage) * 0.70)
            + (self._clamp01(entrypoint_coverage_completeness) * 0.30)
        )

        if scenario_warning_count > 0:
            score = self._clamp01(score * (1.0 - min(0.30, scenario_warning_count * 0.08)))

        return score

    def _derive_domain_reason(self, layer: LayerResult) -> str:
        issues = layer.get("issues", [])
        if issues:
            top_issue = self._sort_issues(issues)[0]
            issue_type = str(top_issue.get("type", "signal_detected")).strip()
            message = str(top_issue.get("message", "")).strip()
            if message:
                return f"{issue_type}: {message}"
            return issue_type

        warnings = layer.get("warnings", [])
        if warnings:
            return str(warnings[0])

        return "signal_detected"

    def _domain_score(self, layer: LayerResult) -> float:
        metrics = layer.get("metrics", {})
        return self._round3(self._clamp01(float(metrics.get("domain_score", 1.0) or 0.0)))

    def _pair_divergence(self, left_pairs: Set[Tuple[str, str]], right_pairs: Set[Tuple[str, str]]) -> float:
        if not left_pairs and not right_pairs:
            return 0.0

        union = left_pairs.union(right_pairs)
        symmetric_delta = left_pairs.symmetric_difference(right_pairs)
        raw_divergence = self._safe_divide(len(symmetric_delta), max(1, len(union)))

        evidence_confidence = self._clamp01(self._safe_divide(min(len(left_pairs), len(right_pairs)), max(1, len(union))))
        adjusted_divergence = raw_divergence * (0.70 + (0.30 * evidence_confidence))

        return self._round3(adjusted_divergence)

    def _pair_overlap(self, left_pairs: Set[Tuple[str, str]], right_pairs: Set[Tuple[str, str]]) -> float:
        if not left_pairs and not right_pairs:
            return 1.0
        if not left_pairs or not right_pairs:
            return 0.0

        overlap = left_pairs.intersection(right_pairs)
        baseline = min(len(left_pairs), len(right_pairs))
        return self._round3(self._safe_divide(len(overlap), max(1, baseline)))

    def _issue(
        self,
        domain: str,
        issue_type: str,
        severity: str,
        message: str,
        count: int = 0,
        sample_nodes: Optional[Sequence[str]] = None,
    ) -> Issue:
        sample = list(sample_nodes or [])[: self._ISSUE_SAMPLE_LIMIT]

        return {
            "domain": domain,
            "layer": domain,
            "type": issue_type,
            "severity": severity.upper(),
            "message": message,
            "count": int(count),
            "sample": sample,
            "sample_nodes": sample,
        }

    def _sort_issues(self, issues: Iterable[Issue]) -> List[Issue]:
        return sorted(
            list(issues),
            key=lambda issue: (
                self._SEVERITY_RANK.get(str(issue.get("severity", "")).upper(), 99),
                self._DOMAIN_ORDER.get(str(issue.get("domain", "")), 999),
                str(issue.get("type", "")),
                str(issue.get("message", "")),
            ),
        )

    def _strip_context(self, layer: LayerResult) -> LayerResult:
        return {key: value for key, value in layer.items() if key != "context"}

    def _normalize_node_id(self, value: Any) -> str:
        return str(value or "").strip()

    def _safe_divide(self, numerator: float, denominator: float) -> float:
        if denominator == 0:
            return 0.0
        return float(numerator) / float(denominator)

    def _clamp01(self, value: float) -> float:
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return float(value)

    def _round3(self, value: float) -> float:
        return round(float(value), 3)


def run_verification(
    graph_data: Dict[str, Any],
    resolver_data: Optional[Dict[str, Any]] = None,
    entrypoints: Optional[Sequence[str]] = None,
    min_trust: float = 0.40,
    execution_evidence: Optional[Mapping[str, Any]] = None,
    min_execution_confidence: float = VerificationRunner._MIN_EXECUTION_CONFIDENCE,
) -> Dict[str, Any]:
    runner = VerificationRunner(graph_data=graph_data, resolver_data=resolver_data)
    return runner.run(
        entrypoints=entrypoints,
        min_trust=min_trust,
        execution_evidence=execution_evidence,
        min_execution_confidence=min_execution_confidence,
    )
