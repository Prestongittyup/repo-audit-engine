from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

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
        "topology_validation": 3,
        "semantic_observations": 4,
    }

    _DOMAIN_WEIGHTS = {
        "structural_integrity": 0.35,
        "topology_validation": 0.25,
        "dependency_consistency": 0.25,
        "semantic_observations": 0.15,
    }

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

        trust_model = self._compute_trust_model(
            structural_layer=structural_layer,
            dependency_layer=dependency_layer,
            topology_layer=topology_layer,
            semantic_layer=semantic_layer,
        )

        policy_decision = self._apply_policy(
            structural_layer=structural_layer,
            dependency_layer=dependency_layer,
            topology_layer=topology_layer,
            trust_model=trust_model,
            min_trust=min_trust,
        )

        failure_analysis = self._build_failure_analysis(
            structural_layer=structural_layer,
            dependency_layer=dependency_layer,
            topology_layer=topology_layer,
            semantic_layer=semantic_layer,
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
            issues.append(
                self._issue(
                    domain="dependency_consistency",
                    issue_type="AST_DI_DIVERGENCE_TRACKED",
                    severity="MEDIUM",
                    message="AST and DI pair sets diverge; this is tracked as architecture drift.",
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

    def _compute_trust_model(
        self,
        structural_layer: LayerResult,
        dependency_layer: LayerResult,
        topology_layer: LayerResult,
        semantic_layer: LayerResult,
    ) -> Dict[str, Any]:
        domain_scores = {
            "structural_integrity": self._domain_score(structural_layer),
            "topology_validation": self._domain_score(topology_layer),
            "dependency_consistency": self._domain_score(dependency_layer),
            "semantic_observations": self._domain_score(semantic_layer),
        }

        weighted_contributions = {
            domain: self._round3(domain_scores[domain] * self._DOMAIN_WEIGHTS[domain])
            for domain in domain_scores
        }

        trust_score = self._round3(sum(weighted_contributions.values()))

        penalties = {
            f"{domain}_penalty": self._round3(1.0 - score)
            for domain, score in domain_scores.items()
        }

        return {
            "formula": "trust_score = structural*0.35 + topology*0.25 + resolver*0.25 + semantic*0.15",
            "trust_score": trust_score,
            "weights": dict(self._DOMAIN_WEIGHTS),
            "domain_scores": domain_scores,
            "weighted_contributions": weighted_contributions,
            "penalties": penalties,
            "scores": {
                "structural_integrity": domain_scores["structural_integrity"],
                "dependency_consistency": domain_scores["dependency_consistency"],
                "topology_validation": domain_scores["topology_validation"],
                "semantic_observations": domain_scores["semantic_observations"],
                "structural": domain_scores["structural_integrity"],
                "reachability": domain_scores["topology_validation"],
                "resolver": domain_scores["dependency_consistency"],
                "semantic": domain_scores["semantic_observations"],
                "trust": trust_score,
            },
        }

    def _apply_policy(
        self,
        structural_layer: LayerResult,
        dependency_layer: LayerResult,
        topology_layer: LayerResult,
        trust_model: Dict[str, Any],
        min_trust: float,
    ) -> Dict[str, Any]:
        structural_metrics = structural_layer.get("metrics", {})
        dependency_metrics = dependency_layer.get("metrics", {})
        topology_metrics = topology_layer.get("metrics", {})

        trust_score = float(trust_model.get("trust_score", 0.0) or 0.0)

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

        if trust_score < min_trust:
            soft_fail_reasons.append(
                f"Trust score {trust_score:.3f} is below minimum threshold {min_trust:.3f}."
            )

        if int(topology_metrics.get("cycle_policy_violation_count", 0) or 0) > 0:
            soft_fail_reasons.append("Cycle policy violations detected (warning-level unless threshold escalates).")

        critical_failure = len(hard_fail_reasons) > 0
        system_valid = (not critical_failure) and (trust_score >= min_trust)

        return {
            "system_valid": system_valid,
            "critical_failure": critical_failure,
            "hard_fail_reasons": sorted(set(hard_fail_reasons)),
            "soft_fail_reasons": sorted(set(soft_fail_reasons)),
            "thresholds": {
                "min_trust": self._round3(min_trust),
                "max_missing_di_node_ratio": 0.50,
                "max_unexpected_island_ratio": 0.40,
            },
            "policy_metrics": {
                "trust_score": self._round3(trust_score),
                "missing_di_node_ratio": self._round3(missing_di_node_ratio),
                "unexpected_island_ratio": self._round3(unexpected_island_ratio),
            },
        }

    def _build_failure_analysis(
        self,
        structural_layer: LayerResult,
        dependency_layer: LayerResult,
        topology_layer: LayerResult,
        semantic_layer: LayerResult,
        trust_model: Dict[str, Any],
    ) -> Dict[str, Any]:
        layers = {
            "structural_integrity": structural_layer,
            "dependency_consistency": dependency_layer,
            "topology_validation": topology_layer,
            "semantic_observations": semantic_layer,
        }

        records: List[Dict[str, Any]] = []
        for domain, layer in layers.items():
            score = self._domain_score(layer)
            impact = self._round3((1.0 - score) * self._DOMAIN_WEIGHTS[domain])
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
) -> Dict[str, Any]:
    runner = VerificationRunner(graph_data=graph_data, resolver_data=resolver_data)
    return runner.run(entrypoints=entrypoints, min_trust=min_trust)
