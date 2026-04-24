from __future__ import annotations

from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


class VerificationRunner:
    """
    Deterministic verification runner with strict fact/policy separation.

    Validation layers emit facts only:
      1) structural_integrity
      2) dependency_consistency
      3) topology_validation
      + semantic_observations (informational)

    Policy consumes facts and trust penalties to produce gating decisions.
    """

    _VALID_EDGE_TYPES = {
        "CALL",
        "CALLS",
        "IMPORT",
        "IMPORTS",
        "INSTANTIATE",
        "INSTANTIATES",
        "DI",
        "DYNAMIC",
        "CONFIG",
        "HEURISTIC",
        "USES",
    }
    _ISSUE_SAMPLE_LIMIT = 50

    # Policy thresholds
    _POLICY_MIN_TRUST = 0.40
    _POLICY_MAX_UNEXPECTED_ISLAND_RATIO = 0.85
    _POLICY_MAX_MISSING_DI_NODE_RATIO = 0.10

    _DOMAIN_STAGE_ORDER = {
        "structural_integrity": 0,
        "dependency_consistency": 1,
        "topology_validation": 2,
        "semantic_observations": 3,
    }

    _SEVERITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}

    def __init__(self, graph_data: Dict[str, Any], resolver_data: Optional[Dict[str, Any]] = None) -> None:
        graph = (graph_data or {}).get("graph")
        if isinstance(graph, dict):
            self._nodes = graph.get("nodes", []) or []
            self._edges = graph.get("edges", []) or []
        else:
            self._nodes = []
            self._edges = []

        self._resolver_edges = (resolver_data or {}).get("edges", []) or []

    def run(self, entrypoints: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        entrypoints = entrypoints or []

        structural_layer = self._evaluate_structural_integrity()

        node_ids: List[str] = structural_layer["context"]["node_ids"]
        node_map: Dict[str, Dict[str, Any]] = structural_layer["context"]["node_map"]
        adjacency: Dict[str, Set[str]] = structural_layer["context"]["adjacency"]
        valid_edges: List[Tuple[str, str, str, str]] = structural_layer["context"]["valid_edges"]
        valid_edge_tuples: Set[Tuple[str, str, str]] = structural_layer["context"]["valid_edge_tuples"]

        dependency_layer = self._evaluate_dependency_consistency(node_ids, valid_edge_tuples)
        topology_layer = self._evaluate_topology_validation(
            node_ids,
            node_map,
            adjacency,
            valid_edges,
            entrypoints,
        )
        semantic_layer = self._evaluate_semantic_observations(topology_layer, dependency_layer)

        trust_model = self._compute_trust_penalties(structural_layer, dependency_layer, topology_layer)
        policy_decision = self._apply_policy(structural_layer, dependency_layer, topology_layer, trust_model)

        issues = self._sort_issues(
            structural_layer["violations"]
            + dependency_layer["violations"]
            + topology_layer["violations"]
        )

        warnings = sorted(
            {
                *structural_layer["warnings"],
                *dependency_layer["warnings"],
                *topology_layer["warnings"],
                *semantic_layer["warnings"],
            }
        )

        recommendations = self._build_recommendations(issues, warnings, policy_decision)

        failure_analysis = self._build_failure_analysis(
            structural_layer,
            dependency_layer,
            topology_layer,
            semantic_layer,
            trust_model,
        )

        ranked_causes = failure_analysis.get("ranked_causes", [])
        failure_domains = sorted(
            {
                str(cause.get("domain", "")).strip()
                for cause in ranked_causes
                if str(cause.get("domain", "")).strip()
            }
        )

        metrics = self._build_metrics(structural_layer, dependency_layer, topology_layer, semantic_layer)
        scores = trust_model["scores"]

        validation_facts = {
            "layers": {
                "structural_integrity": self._strip_context(structural_layer),
                "dependency_consistency": dependency_layer,
                "topology_validation": topology_layer,
                "semantic_observations": semantic_layer,
            },
            "metrics": metrics,
            "issues": issues,
            "warnings": warnings,
        }

        compatibility_results = {
            # Canonical
            "structural_integrity": self._strip_context(structural_layer).get("details", {}),
            "dependency_consistency": dependency_layer.get("details", {}),
            "topology_validation": topology_layer.get("details", {}),
            "semantic_observations": semantic_layer.get("details", {}),
            # Legacy aliases
            "structural": self._strip_context(structural_layer).get("details", {}),
            "reachability": topology_layer.get("details", {}).get("reachability", {}),
            "resolver": dependency_layer.get("details", {}),
            "semantic": semantic_layer.get("details", {}),
        }

        return {
            # Legacy envelope (preserved)
            "metrics": metrics,
            "scores": scores,
            "issues": issues,
            "critical_failure": bool(policy_decision.get("critical_failure", False)),
            "warnings": warnings,
            "recommendations": recommendations,
            "system_valid": bool(policy_decision.get("system_valid", False)),
            "failure_domains": failure_domains,
            "trust_score": float(trust_model["trust_score"]),
            "results": compatibility_results,
            # New architecture
            "validation_facts": validation_facts,
            "policy_decision": policy_decision,
            "trust": trust_model,
            "failure_analysis": failure_analysis,
            "canonical_layers": [
                "structural_integrity",
                "dependency_consistency",
                "topology_validation",
                "semantic_observations",
            ],
            "ast_di_divergence_score": float(dependency_layer["metrics"].get("ast_di_divergence_score", 0.0)),
        }

    def _evaluate_structural_integrity(self) -> Dict[str, Any]:
        node_map: Dict[str, Dict[str, Any]] = {}
        node_ids: List[str] = []

        malformed_node_ids = 0
        duplicate_node_ids = 0
        invalid_namespace_count = 0

        issues: List[Dict[str, Any]] = []
        warnings: List[str] = []

        for raw_node in self._nodes:
            node = raw_node if isinstance(raw_node, dict) else {}
            node_id = self._normalize_node_id(node.get("id"))

            if not node_id:
                malformed_node_ids += 1
                continue

            if node_id in node_map:
                duplicate_node_ids += 1
                continue

            node_map[node_id] = node
            node_ids.append(node_id)

            if not self._is_valid_namespace(node_id):
                invalid_namespace_count += 1

        valid_node_count = len(node_ids)

        adjacency: Dict[str, Set[str]] = {node_id: set() for node_id in node_ids}
        valid_edges: List[Tuple[str, str, str, str]] = []
        valid_edge_tuples: Set[Tuple[str, str, str]] = set()

        malformed_edges = 0
        unresolved_edges = 0
        invalid_type_edges = 0

        for raw_edge in self._edges:
            edge = raw_edge if isinstance(raw_edge, dict) else {}
            from_id = self._normalize_node_id(edge.get("from"))
            to_id = self._normalize_node_id(edge.get("to"))
            edge_type = str(edge.get("type") or "").strip().upper()
            source = str(edge.get("source") or "").strip().upper()

            if not from_id or not to_id or not edge_type:
                malformed_edges += 1
                continue

            if edge_type not in self._VALID_EDGE_TYPES:
                invalid_type_edges += 1
                continue

            if from_id not in node_map or to_id not in node_map:
                unresolved_edges += 1
                continue

            adjacency[from_id].add(to_id)
            valid_edges.append((from_id, to_id, edge_type, source))
            valid_edge_tuples.add((from_id, to_id, edge_type))

        invalid_edge_count = malformed_edges + unresolved_edges + invalid_type_edges

        if malformed_node_ids:
            issues.append(
                self._issue(
                    domain="structural_integrity",
                    issue_type="MALFORMED_NODE_IDS",
                    severity="HIGH",
                    message="Nodes with null or empty IDs were detected.",
                    count=malformed_node_ids,
                )
            )

        if duplicate_node_ids:
            issues.append(
                self._issue(
                    domain="structural_integrity",
                    issue_type="DUPLICATE_NODE_IDS",
                    severity="HIGH",
                    message="Duplicate canonical node IDs were detected.",
                    count=duplicate_node_ids,
                )
            )

        if invalid_namespace_count:
            issues.append(
                self._issue(
                    domain="structural_integrity",
                    issue_type="INVALID_NODE_NAMESPACE",
                    severity="MEDIUM",
                    message="Node IDs contain invalid namespace segments.",
                    count=invalid_namespace_count,
                )
            )

        if invalid_edge_count:
            issues.append(
                self._issue(
                    domain="structural_integrity",
                    issue_type="INVALID_EDGES",
                    severity="HIGH",
                    message="Edges contain malformed, unresolved, or unsupported references.",
                    count=invalid_edge_count,
                )
            )

        metrics = {
            "node_count": len(self._nodes),
            "valid_node_count": valid_node_count,
            "edge_count": len(self._edges),
            "valid_edge_count": len(valid_edges),
            "malformed_node_count": malformed_node_ids,
            "duplicate_node_count": duplicate_node_ids,
            "invalid_namespace_count": invalid_namespace_count,
            "malformed_edge_count": malformed_edges,
            "unresolved_edge_count": unresolved_edges,
            "invalid_type_edge_count": invalid_type_edges,
            "invalid_edge_count": invalid_edge_count,
        }

        if valid_node_count == 0:
            warnings.append("No valid nodes were resolved from the graph payload.")

        return {
            "domain": "structural_integrity",
            "metrics": metrics,
            "violations": issues,
            "warnings": sorted(set(warnings)),
            "details": {
                "node_count_mismatch": max(0, len(self._nodes) - valid_node_count),
                "invalid_edges": {
                    "malformed": malformed_edges,
                    "unresolved": unresolved_edges,
                    "invalid_type": invalid_type_edges,
                },
            },
            "context": {
                "node_map": node_map,
                "node_ids": sorted(node_ids),
                "adjacency": adjacency,
                "valid_edges": valid_edges,
                "valid_edge_tuples": valid_edge_tuples,
            },
        }

    def _evaluate_dependency_consistency(
        self,
        node_ids: Sequence[str],
        valid_edge_tuples: Set[Tuple[str, str, str]],
    ) -> Dict[str, Any]:
        node_set = set(node_ids)

        represented_edge_counts = {"AST": 0, "DI": 0, "CONFIG": 0, "HEURISTIC": 0}

        for _, _, edge_type, source in self._iter_sorted_valid_edges(valid_edge_tuples):
            if source in represented_edge_counts:
                represented_edge_counts[source] += 1
            if edge_type in represented_edge_counts:
                represented_edge_counts[edge_type] += 1

        resolver_edges = self._normalize_resolver_edges()

        resolver_by_source = {"AST": [], "DI": [], "CONFIG": [], "HEURISTIC": []}
        for edge in resolver_edges:
            source = edge["source"]
            if source in resolver_by_source:
                resolver_by_source[source].append(edge)

        total_by_source = {source: len(edges) for source, edges in resolver_by_source.items()}

        missing_di_nodes: Set[str] = set()
        di_nodes_referenced: Set[str] = set()
        inferred_dependency_pairs: Set[Tuple[str, str]] = set()

        ast_pairs = self._pair_set(resolver_by_source["AST"])
        di_pairs = self._pair_set(resolver_by_source["DI"])
        config_pairs = self._pair_set(resolver_by_source["CONFIG"])
        heuristic_pairs = self._pair_set(resolver_by_source["HEURISTIC"])

        for edge in resolver_by_source["DI"]:
            src = edge["from"]
            dst = edge["to"]
            di_nodes_referenced.add(src)
            di_nodes_referenced.add(dst)
            if src not in node_set:
                missing_di_nodes.add(src)
            if dst not in node_set:
                missing_di_nodes.add(dst)

            if (src, dst) not in ast_pairs:
                inferred_dependency_pairs.add((src, dst))

        ast_di_divergence_score = self._pair_divergence(ast_pairs, di_pairs)
        config_heuristic_alignment = self._pair_overlap(config_pairs, heuristic_pairs)

        issues: List[Dict[str, Any]] = []
        warnings: List[str] = []

        if missing_di_nodes:
            issues.append(
                self._issue(
                    domain="dependency_consistency",
                    issue_type="MISSING_DI_NODES",
                    severity="HIGH",
                    message="DI dependencies reference nodes that are missing from canonical graph nodes.",
                    count=len(missing_di_nodes),
                    sample_nodes=sorted(missing_di_nodes),
                )
            )

        if ast_di_divergence_score > 0.0:
            issues.append(
                self._issue(
                    domain="dependency_consistency",
                    issue_type="AST_DI_DIVERGENCE_TRACKED",
                    severity="MEDIUM",
                    message="AST-to-DI divergence is tracked as a consistency signal (non-fatal by itself).",
                    count=len(ast_pairs.symmetric_difference(di_pairs)),
                )
            )

        if inferred_dependency_pairs:
            issues.append(
                self._issue(
                    domain="dependency_consistency",
                    issue_type="NON_AST_INFERRED_DI_EDGES",
                    severity="LOW",
                    message="DI contains inferred edges without direct AST derivation; retained as non-fatal evidence.",
                    count=len(inferred_dependency_pairs),
                )
            )

        ast_coverage = self._safe_divide(represented_edge_counts.get("AST", 0), max(1, total_by_source.get("AST", 0)))
        di_coverage = self._safe_divide(represented_edge_counts.get("DI", 0), max(1, total_by_source.get("DI", 0)))

        if total_by_source.get("AST", 0) > 0 and ast_coverage < 0.7:
            warnings.append("AST edge coverage in canonical graph is below preferred baseline (0.70).")
        if total_by_source.get("DI", 0) > 0 and di_coverage < 0.7:
            warnings.append("DI edge coverage in canonical graph is below preferred baseline (0.70).")

        metrics = {
            "resolver_edge_count": len(resolver_edges),
            "missing_di_nodes_count": len(missing_di_nodes),
            "di_nodes_referenced_count": len(di_nodes_referenced),
            "ast_edge_count": total_by_source.get("AST", 0),
            "di_edge_count": total_by_source.get("DI", 0),
            "config_edge_count": total_by_source.get("CONFIG", 0),
            "heuristic_edge_count": total_by_source.get("HEURISTIC", 0),
            "ast_represented_count": represented_edge_counts.get("AST", 0),
            "di_represented_count": represented_edge_counts.get("DI", 0),
            "config_represented_count": represented_edge_counts.get("CONFIG", 0),
            "heuristic_represented_count": represented_edge_counts.get("HEURISTIC", 0),
            "ast_di_divergence_score": ast_di_divergence_score,
            "config_heuristic_alignment_score": config_heuristic_alignment,
            "inferred_dependency_pair_count": len(inferred_dependency_pairs),
        }

        details = {
            "missing_di_nodes": sorted(missing_di_nodes),
            "inferred_dependency_pairs": [
                {"from": src, "to": dst} for src, dst in sorted(inferred_dependency_pairs)
            ],
            "coverage": {
                "ast": {
                    "represented": represented_edge_counts.get("AST", 0),
                    "total": total_by_source.get("AST", 0),
                    "ratio": ast_coverage,
                },
                "di": {
                    "represented": represented_edge_counts.get("DI", 0),
                    "total": total_by_source.get("DI", 0),
                    "ratio": di_coverage,
                },
                "config": {
                    "represented": represented_edge_counts.get("CONFIG", 0),
                    "total": total_by_source.get("CONFIG", 0),
                    "ratio": self._safe_divide(
                        represented_edge_counts.get("CONFIG", 0),
                        max(1, total_by_source.get("CONFIG", 0)),
                    ),
                },
                "heuristic": {
                    "represented": represented_edge_counts.get("HEURISTIC", 0),
                    "total": total_by_source.get("HEURISTIC", 0),
                    "ratio": self._safe_divide(
                        represented_edge_counts.get("HEURISTIC", 0),
                        max(1, total_by_source.get("HEURISTIC", 0)),
                    ),
                },
            },
            "divergence": {
                "ast_di_divergence_score": ast_di_divergence_score,
                "config_heuristic_alignment_score": config_heuristic_alignment,
            },
        }

        return {
            "domain": "dependency_consistency",
            "metrics": metrics,
            "violations": self._sort_issues(issues),
            "warnings": sorted(set(warnings)),
            "details": details,
        }

    def _evaluate_topology_validation(
        self,
        node_ids: Sequence[str],
        node_map: Dict[str, Dict[str, Any]],
        adjacency: Dict[str, Set[str]],
        valid_edges: Sequence[Tuple[str, str, str, str]],
        entrypoints: Sequence[str],
    ) -> Dict[str, Any]:
        node_set = set(node_ids)
        normalized_entrypoints = [self._normalize_node_id(ep) for ep in entrypoints if self._normalize_node_id(ep)]
        entrypoints_present = sorted({ep for ep in normalized_entrypoints if ep in node_set})
        entrypoints_missing = sorted({ep for ep in normalized_entrypoints if ep not in node_set})

        reachable = self._bfs_reachable(adjacency, entrypoints_present)
        unreachable = sorted(node_set.difference(reachable))

        di_edges = [edge for edge in valid_edges if edge[2] == "DI"]
        unreachable_set = set(unreachable)
        di_unreachable_edges = [
            (src, dst, edge_type)
            for src, dst, edge_type, _source in di_edges
            if src in unreachable_set or dst in unreachable_set
        ]

        cycle_nodes = self._find_cycle_nodes(node_ids, adjacency)
        cycle_policy_violations = [
            node_id
            for node_id in cycle_nodes
            if not self._is_cycle_allowed(node_map.get(node_id, {}))
        ]

        components = self._undirected_components(node_ids, adjacency)
        isolated_reports: List[Dict[str, Any]] = []

        isolated_module_count = 0
        orphan_subgraph_count = 0
        cyclic_island_count = 0
        isolated_module_node_count = 0
        orphan_subgraph_node_count = 0
        cyclic_island_node_count = 0

        for component in components:
            if not component:
                continue

            if not set(component).issubset(unreachable_set):
                continue

            classification = self._classify_island(component, node_map, set(cycle_nodes))
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
                    "contains_cycle": any(n in cycle_nodes for n in component),
                    "explicit_isolation": all(
                        self._is_explicitly_isolated(node_map.get(n, {})) for n in component
                    ),
                }
            )

        disconnected_island_count = len(isolated_reports)
        unexpected_island_count = orphan_subgraph_count
        unexpected_island_node_count = orphan_subgraph_node_count

        issues: List[Dict[str, Any]] = []
        warnings: List[str] = []

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

        if cycle_policy_violations:
            issues.append(
                self._issue(
                    domain="topology_validation",
                    issue_type="CYCLE_POLICY_VIOLATION",
                    severity="MEDIUM",
                    message="Cycle nodes violate cycle allowance policy flags.",
                    count=len(cycle_policy_violations),
                    sample_nodes=cycle_policy_violations,
                )
            )

        if cyclic_island_count > 0:
            warnings.append("Cyclic disconnected islands were detected and tracked as topology risk.")
        if disconnected_island_count > 0:
            warnings.append("Disconnected islands were detected and classified (isolated_module/orphan_subgraph/cyclic_island).")

        unreachable_ratio = self._safe_divide(len(unreachable), max(1, len(node_ids)))

        metrics = {
            "entrypoint_count": len(normalized_entrypoints),
            "entrypoint_present_count": len(entrypoints_present),
            "entrypoint_missing_count": len(entrypoints_missing),
            "reachable_node_count": len(reachable),
            "unreachable_node_count": len(unreachable),
            "unreachable_nodes_ratio": unreachable_ratio,
            "di_edge_count": len(di_edges),
            "di_unreachable_edge_count": len(di_unreachable_edges),
            "cycle_node_count": len(cycle_nodes),
            "cycle_policy_violation_count": len(cycle_policy_violations),
            "disconnected_island_count": disconnected_island_count,
            "isolated_module_count": isolated_module_count,
            "orphan_subgraph_count": orphan_subgraph_count,
            "cyclic_island_count": cyclic_island_count,
            "unexpected_island_count": unexpected_island_count,
            "isolated_module_node_count": isolated_module_node_count,
            "orphan_subgraph_node_count": orphan_subgraph_node_count,
            "cyclic_island_node_count": cyclic_island_node_count,
            "unexpected_island_node_count": unexpected_island_node_count,
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
                "cycle_policy_violations": sorted(cycle_policy_violations)[: self._ISSUE_SAMPLE_LIMIT],
                "isolation_report": sorted(
                    isolated_reports,
                    key=lambda item: (-int(item.get("size", 0)), str(item.get("classification", ""))),
                )[: self._ISSUE_SAMPLE_LIMIT],
            },
        }

        return {
            "domain": "topology_validation",
            "metrics": metrics,
            "violations": self._sort_issues(issues),
            "warnings": sorted(set(warnings)),
            "details": details,
        }

    def _evaluate_semantic_observations(
        self,
        topology_layer: Dict[str, Any],
        dependency_layer: Dict[str, Any],
    ) -> Dict[str, Any]:
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

        details = {
            "notes": sorted(set(notes)),
            "island_classification_summary": {
                "isolated_module": int(topology_metrics.get("isolated_module_count", 0) or 0),
                "orphan_subgraph": int(topology_metrics.get("orphan_subgraph_count", 0) or 0),
                "cyclic_island": int(topology_metrics.get("cyclic_island_count", 0) or 0),
            },
            "ast_di_divergence_score": ast_di_divergence,
        }

        metrics = {
            "semantic_note_count": len(details["notes"]),
            "semantic_warning_count": len(warnings),
            "ast_di_divergence_score": ast_di_divergence,
        }

        return {
            "domain": "semantic_observations",
            "metrics": metrics,
            "violations": [],
            "warnings": sorted(set(warnings)),
            "details": details,
        }

    def _compute_trust_penalties(
        self,
        structural_layer: Dict[str, Any],
        dependency_layer: Dict[str, Any],
        topology_layer: Dict[str, Any],
    ) -> Dict[str, Any]:
        structural_metrics = structural_layer.get("metrics", {})
        dependency_metrics = dependency_layer.get("metrics", {})
        topology_metrics = topology_layer.get("metrics", {})

        node_count = int(structural_metrics.get("valid_node_count", 0) or 0)
        edge_count = int(structural_metrics.get("valid_edge_count", 0) or 0)

        invalid_edge_count = int(structural_metrics.get("invalid_edge_count", 0) or 0)
        malformed_nodes = int(structural_metrics.get("malformed_node_count", 0) or 0) + int(
            structural_metrics.get("duplicate_node_count", 0) or 0
        )

        unreachable_ratio = float(topology_metrics.get("unreachable_nodes_ratio", 0.0) or 0.0)
        di_unreachable_count = int(topology_metrics.get("di_unreachable_edge_count", 0) or 0)
        di_edge_count = int(topology_metrics.get("di_edge_count", 0) or 0)

        missing_di_nodes_count = int(dependency_metrics.get("missing_di_nodes_count", 0) or 0)
        di_nodes_referenced_count = int(dependency_metrics.get("di_nodes_referenced_count", 0) or 0)
        ast_di_divergence = float(dependency_metrics.get("ast_di_divergence_score", 0.0) or 0.0)

        disconnected_island_count = int(topology_metrics.get("disconnected_island_count", 0) or 0)
        unexpected_island_count = int(topology_metrics.get("unexpected_island_count", 0) or 0)
        unexpected_island_node_count = int(topology_metrics.get("unexpected_island_node_count", 0) or 0)
        cycle_policy_violation_count = int(topology_metrics.get("cycle_policy_violation_count", 0) or 0)

        invalid_edge_ratio = self._clamp01(self._safe_divide(invalid_edge_count, max(1, edge_count)))
        malformed_node_ratio = self._clamp01(self._safe_divide(malformed_nodes, max(1, node_count)))

        di_unreachable_ratio = self._clamp01(self._safe_divide(di_unreachable_count, max(1, di_edge_count)))
        missing_di_node_ratio = self._clamp01(
            self._safe_divide(missing_di_nodes_count, max(1, di_nodes_referenced_count))
        )

        disconnected_island_ratio = self._clamp01(
            self._safe_divide(unexpected_island_node_count, max(1, node_count))
        )
        cycle_violation_ratio = self._clamp01(
            self._safe_divide(cycle_policy_violation_count, max(1, node_count))
        )

        # Upgrade 1 formula (deterministic weighted penalties)
        structural_penalty = self._round3(
            min(0.25, invalid_edge_ratio * 0.25) + min(0.10, malformed_node_ratio * 0.10)
        )

        reachability_penalty = self._round3(
            min(0.30, self._clamp01(unreachable_ratio) * 0.30)
            + min(0.20, di_unreachable_ratio * 0.20)
        )

        resolver_penalty = self._round3(
            min(0.25, missing_di_node_ratio * 0.25)
            + min(0.25, self._clamp01(ast_di_divergence) * 0.25)
        )

        semantic_penalty = self._round3(
            min(0.10, disconnected_island_ratio * 0.10)
            + min(0.10, cycle_violation_ratio * 0.10)
        )

        total_penalty = self._round3(
            structural_penalty + reachability_penalty + resolver_penalty + semantic_penalty
        )

        trust_score = self._round3(
            self._clamp01(
                1.0
                - structural_penalty
                - reachability_penalty
                - resolver_penalty
                - semantic_penalty
            )
        )

        structural_score = self._penalty_to_score(structural_penalty, 0.35)
        reachability_score = self._penalty_to_score(reachability_penalty, 0.50)
        resolver_score = self._penalty_to_score(resolver_penalty, 0.50)
        semantic_score = self._penalty_to_score(semantic_penalty, 0.20)
        topology_score = self._penalty_to_score(reachability_penalty + semantic_penalty, 0.70)

        return {
            "formula": "trust_score = clamp(1.0 - structural_penalty - reachability_penalty - resolver_penalty - semantic_penalty, 0.0, 1.0)",
            "trust_score": trust_score,
            "penalties": {
                "structural_penalty": structural_penalty,
                "reachability_penalty": reachability_penalty,
                "resolver_penalty": resolver_penalty,
                "semantic_penalty": semantic_penalty,
                "total_penalty": total_penalty,
            },
            "ratios": {
                "invalid_edge_ratio": self._round3(invalid_edge_ratio),
                "malformed_node_ratio": self._round3(malformed_node_ratio),
                "unreachable_nodes_ratio": self._round3(self._clamp01(unreachable_ratio)),
                "di_unreachable_ratio": self._round3(di_unreachable_ratio),
                "missing_di_node_ratio": self._round3(missing_di_node_ratio),
                "ast_di_divergence_score": self._round3(self._clamp01(ast_di_divergence)),
                "disconnected_island_ratio": self._round3(disconnected_island_ratio),
                "cycle_violation_ratio": self._round3(cycle_violation_ratio),
            },
            "scores": {
                # Canonical
                "structural_integrity": structural_score,
                "dependency_consistency": resolver_score,
                "topology_validation": topology_score,
                "semantic_observations": semantic_score,
                # Legacy aliases
                "structural": structural_score,
                "reachability": reachability_score,
                "resolver": resolver_score,
                "semantic": semantic_score,
                "trust": trust_score,
            },
        }

    def _apply_policy(
        self,
        structural_layer: Dict[str, Any],
        dependency_layer: Dict[str, Any],
        topology_layer: Dict[str, Any],
        trust_model: Dict[str, Any],
    ) -> Dict[str, Any]:
        structural_metrics = structural_layer.get("metrics", {})
        dependency_metrics = dependency_layer.get("metrics", {})
        topology_metrics = topology_layer.get("metrics", {})
        ratios = trust_model.get("ratios", {})

        trust_score = float(trust_model.get("trust_score", 0.0) or 0.0)
        min_trust = self._POLICY_MIN_TRUST

        hard_fail_reasons: List[str] = []
        soft_fail_reasons: List[str] = []

        valid_nodes = int(structural_metrics.get("valid_node_count", 0) or 0)
        if valid_nodes <= 0:
            hard_fail_reasons.append("No valid canonical nodes available (catastrophic graph corruption).")

        missing_di_node_ratio = float(ratios.get("missing_di_node_ratio", 0.0) or 0.0)
        if missing_di_node_ratio > self._POLICY_MAX_MISSING_DI_NODE_RATIO:
            hard_fail_reasons.append(
                "Dependency rule violation threshold exceeded for missing DI node references."
            )

        unexpected_island_ratio = self._safe_divide(
            int(topology_metrics.get("unexpected_island_node_count", 0) or 0),
            max(1, int(structural_metrics.get("valid_node_count", 0) or 0)),
        )
        if unexpected_island_ratio > self._POLICY_MAX_UNEXPECTED_ISLAND_RATIO:
            hard_fail_reasons.append(
                "Unapproved isolated/orphan subgraph ratio exceeded policy threshold."
            )

        if trust_score < min_trust:
            soft_fail_reasons.append(
                f"Trust score {trust_score:.3f} is below minimum threshold {min_trust:.3f}."
            )

        cycle_violations = int(topology_metrics.get("cycle_policy_violation_count", 0) or 0)
        if cycle_violations > 0:
            soft_fail_reasons.append("Cycle policy violations detected (warning-level unless threshold escalates).")

        divergence = float(dependency_metrics.get("ast_di_divergence_score", 0.0) or 0.0)
        if divergence > 0.0:
            soft_fail_reasons.append("AST-vs-DI divergence detected and tracked as non-fatal consistency evidence.")

        critical_failure = len(hard_fail_reasons) > 0
        system_valid = (not critical_failure) and (trust_score >= min_trust)

        return {
            "system_valid": system_valid,
            "critical_failure": critical_failure,
            "hard_fail_reasons": sorted(set(hard_fail_reasons)),
            "soft_fail_reasons": sorted(set(soft_fail_reasons)),
            "thresholds": {
                "min_trust": min_trust,
                "max_missing_di_node_ratio": self._POLICY_MAX_MISSING_DI_NODE_RATIO,
                "max_unexpected_island_ratio": self._POLICY_MAX_UNEXPECTED_ISLAND_RATIO,
            },
            "policy_metrics": {
                "trust_score": trust_score,
                "missing_di_node_ratio": self._round3(missing_di_node_ratio),
                "unexpected_island_ratio": self._round3(unexpected_island_ratio),
            },
        }

    def _build_failure_analysis(
        self,
        structural_layer: Dict[str, Any],
        dependency_layer: Dict[str, Any],
        topology_layer: Dict[str, Any],
        semantic_layer: Dict[str, Any],
        trust_model: Dict[str, Any],
    ) -> Dict[str, Any]:
        penalties = trust_model.get("penalties", {})

        layers = {
            "structural_integrity": structural_layer,
            "dependency_consistency": dependency_layer,
            "topology_validation": topology_layer,
            "semantic_observations": semantic_layer,
        }

        domain_impact = {
            "structural_integrity": float(penalties.get("structural_penalty", 0.0) or 0.0),
            "dependency_consistency": float(penalties.get("resolver_penalty", 0.0) or 0.0),
            "topology_validation": float(penalties.get("reachability_penalty", 0.0) or 0.0),
            "semantic_observations": float(penalties.get("semantic_penalty", 0.0) or 0.0),
        }

        records: List[Dict[str, Any]] = []
        for domain in [
            "structural_integrity",
            "dependency_consistency",
            "topology_validation",
            "semantic_observations",
        ]:
            layer = layers[domain]
            reason = self._derive_domain_reason(layer)
            impact = self._round3(domain_impact.get(domain, 0.0))
            has_signal = impact > 0.0 or bool(layer.get("violations"))
            if not has_signal:
                continue

            records.append(
                {
                    "domain": domain,
                    "reason": reason,
                    "impact_score": impact,
                    "stage_index": self._DOMAIN_STAGE_ORDER.get(domain, 999),
                }
            )

        ranked_causes = sorted(
            records,
            key=lambda item: (
                -float(item.get("impact_score", 0.0)),
                int(item.get("stage_index", 999)),
                str(item.get("domain", "")),
            ),
        )

        first_failure_candidates = sorted(
            records,
            key=lambda item: (
                int(item.get("stage_index", 999)),
                -float(item.get("impact_score", 0.0)),
                str(item.get("domain", "")),
            ),
        )

        primary_cause = "none"
        primary_domain = ""
        if first_failure_candidates:
            first = first_failure_candidates[0]
            primary_domain = str(first.get("domain", ""))
            primary_cause = f"{primary_domain}:{first.get('reason', 'signal detected')}"

        causal_chain: List[Dict[str, Any]] = []
        if primary_domain:
            for item in sorted(records, key=lambda rec: int(rec.get("stage_index", 999))):
                stage = str(item.get("domain", ""))
                reason = str(item.get("reason", "signal detected"))
                impact = float(item.get("impact_score", 0.0) or 0.0)

                if stage == primary_domain:
                    dependency = "source_graph"
                    failure = reason
                else:
                    dependency = primary_domain
                    failure = f"{reason} (propagated from {primary_domain})"

                causal_chain.append(
                    {
                        "stage": stage,
                        "dependency": dependency,
                        "failure": failure,
                        "impact_score": self._round3(impact),
                    }
                )

        return {
            "primary_cause": primary_cause,
            "ranked_causes": [
                {
                    "domain": item["domain"],
                    "reason": item["reason"],
                    "impact_score": self._round3(float(item.get("impact_score", 0.0) or 0.0)),
                }
                for item in ranked_causes
            ],
            "causal_chain": causal_chain,
            "first_failure_dominance": {
                "enabled": True,
                "first_originating_domain": primary_domain or None,
            },
        }

    def _build_metrics(
        self,
        structural_layer: Dict[str, Any],
        dependency_layer: Dict[str, Any],
        topology_layer: Dict[str, Any],
        semantic_layer: Dict[str, Any],
    ) -> Dict[str, Any]:
        structural_metrics = structural_layer.get("metrics", {})
        dependency_metrics = dependency_layer.get("metrics", {})
        topology_metrics = topology_layer.get("metrics", {})
        semantic_metrics = semantic_layer.get("metrics", {})

        metrics: Dict[str, Any] = {}

        for key, value in structural_metrics.items():
            metrics[f"structural_integrity.{key}"] = value
        for key, value in dependency_metrics.items():
            metrics[f"dependency_consistency.{key}"] = value
        for key, value in topology_metrics.items():
            metrics[f"topology_validation.{key}"] = value
        for key, value in semantic_metrics.items():
            metrics[f"semantic_observations.{key}"] = value

        # Legacy flattened keys
        metrics["node_count"] = structural_metrics.get("valid_node_count", 0)
        metrics["edge_count"] = structural_metrics.get("valid_edge_count", 0)
        metrics["unreachable_node_count"] = topology_metrics.get("unreachable_node_count", 0)
        metrics["missing_dependency_count"] = dependency_metrics.get("missing_di_nodes_count", 0)
        metrics["ast_di_divergence_score"] = dependency_metrics.get("ast_di_divergence_score", 0.0)
        metrics["cycle_policy_violation_count"] = topology_metrics.get("cycle_policy_violation_count", 0)
        metrics["disconnected_island_count"] = topology_metrics.get("disconnected_island_count", 0)
        metrics["orphan_subgraph_count"] = topology_metrics.get("orphan_subgraph_count", 0)
        metrics["cyclic_island_count"] = topology_metrics.get("cyclic_island_count", 0)

        return metrics

    def _build_recommendations(
        self,
        issues: Sequence[Dict[str, Any]],
        warnings: Sequence[str],
        policy_decision: Dict[str, Any],
    ) -> List[str]:
        recommendations: List[str] = []

        issue_types = {str(issue.get("type", "")).strip().upper() for issue in issues}

        if "INVALID_EDGES" in issue_types:
            recommendations.append(
                "Repair malformed/unresolved edges before topology and dependency interpretation."
            )

        if "MISSING_DI_NODES" in issue_types:
            recommendations.append(
                "Align DI dependency references with canonical node IDs or add missing nodes into canonical graph generation."
            )

        if "AST_DI_DIVERGENCE_TRACKED" in issue_types:
            recommendations.append(
                "Review AST-vs-DI divergence trends; treat as architecture drift signal rather than immediate rejection."
            )

        if "UNEXPECTED_ORPHAN_SUBGRAPH" in issue_types:
            recommendations.append(
                "Mark intentional isolation explicitly or connect orphan_subgraph components to supported entrypoint flows."
            )

        if "CYCLE_POLICY_VIOLATION" in issue_types:
            recommendations.append(
                "Annotate permitted cycles with explicit policy flags or refactor cycle ownership boundaries."
            )

        if policy_decision.get("critical_failure"):
            recommendations.append(
                "Resolve policy hard-fail conditions first; trust scoring remains continuous but policy gate blocks validity."
            )

        for warning in warnings:
            if "coverage" in warning.lower():
                recommendations.append("Improve resolver source coverage in canonical graph synthesis.")
                break

        if not recommendations:
            recommendations.append("No blocking recommendations. Continue monitoring drift and topology telemetry.")

        return sorted(set(recommendations))

    def _normalize_resolver_edges(self) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []

        for raw_edge in self._resolver_edges:
            edge = raw_edge if isinstance(raw_edge, dict) else {}
            src = self._normalize_node_id(edge.get("from"))
            dst = self._normalize_node_id(edge.get("to"))
            source = str(edge.get("source") or "").strip().upper()
            edge_type = str(edge.get("type") or "").strip().upper()

            if not src or not dst:
                continue
            if source not in {"AST", "DI", "CONFIG", "HEURISTIC"}:
                continue

            normalized.append(
                {
                    "from": src,
                    "to": dst,
                    "source": source,
                    "type": edge_type,
                }
            )

        normalized.sort(key=lambda item: (item["source"], item["from"], item["to"], item["type"]))
        return normalized

    def _iter_sorted_valid_edges(
        self,
        valid_edge_tuples: Iterable[Tuple[str, str, str]],
    ) -> Iterable[Tuple[str, str, str, str]]:
        edge_set = {
            (
                self._normalize_node_id(edge.get("from")),
                self._normalize_node_id(edge.get("to")),
                str(edge.get("type") or "").strip().upper(),
                str(edge.get("source") or "").strip().upper(),
            )
            for edge in self._edges
            if isinstance(edge, dict)
        }

        accepted = []
        for src, dst, edge_type, source in edge_set:
            if not src or not dst or not edge_type:
                continue
            if (src, dst, edge_type) not in valid_edge_tuples:
                continue
            accepted.append((src, dst, edge_type, source))

        accepted.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        return accepted

    def _pair_set(self, edges: Sequence[Dict[str, str]]) -> Set[Tuple[str, str]]:
        return {(edge["from"], edge["to"]) for edge in edges}

    def _pair_divergence(
        self,
        left_pairs: Set[Tuple[str, str]],
        right_pairs: Set[Tuple[str, str]],
    ) -> float:
        if not left_pairs and not right_pairs:
            return 0.0

        union = left_pairs.union(right_pairs)
        symmetric_delta = left_pairs.symmetric_difference(right_pairs)
        raw_divergence = self._safe_divide(len(symmetric_delta), max(1, len(union)))

        # AST is a preferred signal, not an absolute authority.
        # When AST evidence is sparse, reduce divergence impact accordingly.
        ast_evidence_confidence = self._clamp01(self._safe_divide(len(left_pairs), max(1, len(right_pairs))))
        adjusted_divergence = raw_divergence * ast_evidence_confidence

        return self._round3(adjusted_divergence)

    def _pair_overlap(
        self,
        left_pairs: Set[Tuple[str, str]],
        right_pairs: Set[Tuple[str, str]],
    ) -> float:
        if not left_pairs and not right_pairs:
            return 1.0
        if not left_pairs or not right_pairs:
            return 0.0

        overlap = left_pairs.intersection(right_pairs)
        baseline = min(len(left_pairs), len(right_pairs))
        return self._round3(self._safe_divide(len(overlap), max(1, baseline)))

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

        queue: deque[str] = deque(sorted([node_id for node_id, deg in in_degree.items() if deg == 0]))
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

        cycle_nodes = sorted(node_id for node_id in node_ids if node_id not in processed)
        return cycle_nodes

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

            component: List[str] = []
            queue: deque[str] = deque([start])
            visited.add(start)

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
            self._is_explicitly_isolated(node_map.get(node_id, {})) for node_id in component_nodes
        ):
            return "isolated_module"

        if any(node_id in cycle_nodes for node_id in component_nodes):
            return "cyclic_island"

        return "orphan_subgraph"

    def _is_valid_namespace(self, node_id: str) -> bool:
        if not node_id:
            return False

        if " " in node_id:
            return False

        separators = [":", "#", "/", "\\", "."]
        return any(sep in node_id for sep in separators)

    def _is_explicitly_isolated(self, node: Dict[str, Any]) -> bool:
        if not isinstance(node, dict):
            return False

        for key in ("allow_isolated", "isolated_allowed", "expected_isolation"):
            val = node.get(key)
            if isinstance(val, bool) and val:
                return True

        metadata = node.get("metadata")
        if isinstance(metadata, dict):
            for key in ("allow_isolated", "isolated_allowed", "expected_isolation"):
                val = metadata.get(key)
                if isinstance(val, bool) and val:
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
            val = node.get(key)
            if isinstance(val, bool) and val:
                return True

        metadata = node.get("metadata")
        if isinstance(metadata, dict):
            for key in ("allow_cycle", "cycle_allowed"):
                val = metadata.get(key)
                if isinstance(val, bool) and val:
                    return True

        return False

    def _derive_domain_reason(self, layer: Dict[str, Any]) -> str:
        violations = layer.get("violations", [])
        if violations:
            top = self._sort_issues(list(violations))[0]
            issue_type = str(top.get("type", "signal_detected")).strip()
            message = str(top.get("message", "")).strip()
            if message:
                return f"{issue_type}: {message}"
            return issue_type

        warnings = layer.get("warnings", [])
        if warnings:
            return str(warnings[0])

        return "signal_detected"

    def _strip_context(self, layer: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in layer.items() if k != "context"}

    def _issue(
        self,
        domain: str,
        issue_type: str,
        severity: str,
        message: str,
        count: Optional[int] = None,
        sample_nodes: Optional[Sequence[str]] = None,
        sample_edges: Optional[Sequence[Tuple[str, str, str]]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "domain": domain,
            "layer": domain,
            "type": issue_type,
            "severity": severity.upper(),
            "message": message,
            "count": int(count or 0),
        }

        if sample_nodes:
            payload["sample_nodes"] = list(sample_nodes)[: self._ISSUE_SAMPLE_LIMIT]

        if sample_edges:
            payload["sample_edges"] = [
                {"from": src, "to": dst, "type": edge_type}
                for src, dst, edge_type in list(sample_edges)[: self._ISSUE_SAMPLE_LIMIT]
            ]

        return payload

    def _sort_issues(self, issues: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            list(issues),
            key=lambda issue: (
                self._SEVERITY_RANK.get(str(issue.get("severity", "")).upper(), 99),
                self._DOMAIN_STAGE_ORDER.get(str(issue.get("domain", "")), 999),
                str(issue.get("type", "")),
                str(issue.get("message", "")),
            ),
        )

    def _normalize_node_id(self, value: Any) -> str:
        text = str(value or "").strip()
        return text

    def _safe_divide(self, numerator: float, denominator: float) -> float:
        if denominator == 0:
            return 0.0
        return numerator / denominator

    def _clamp01(self, value: float) -> float:
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return float(value)

    def _round3(self, value: float) -> float:
        return round(float(value), 3)

    def _penalty_to_score(self, penalty: float, max_penalty: float) -> float:
        if max_penalty <= 0:
            return 1.0
        normalized = self._safe_divide(penalty, max_penalty)
        return self._round3(self._clamp01(1.0 - normalized))


def run_verification(
    graph_data: Dict[str, Any],
    resolver_data: Optional[Dict[str, Any]] = None,
    entrypoints: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    runner = VerificationRunner(graph_data, resolver_data)
    return runner.run(entrypoints=entrypoints)
