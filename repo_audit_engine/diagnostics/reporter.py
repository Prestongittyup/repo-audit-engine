from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from repo_audit_engine.io.artifacts import load_json

Issue = Dict[str, Any]
RootCause = Dict[str, Any]


class DiagnosticSynthesisLayer:
    """
    Synthesizes ranked, causal diagnostics from validation artifacts.

    This layer does not re-run validation. It consumes existing outputs from
    structural, resolver, reachability, and semantic validations and produces
    decision-grade diagnostics.
    """

    _SEVERITY_WEIGHT = {
        "HIGH": 1.00,
        "MEDIUM": 0.70,
        "LOW": 0.40,
        "INFO": 0.20,
    }

    _ISSUE_TYPE = {
        "structural_integrity": "STRUCTURAL",
        "dependency_consistency": "RESOLVER",
        "topology_validation": "REACHABILITY",
        "semantic_observations": "SEMANTIC",
    }

    _ACTION_BY_ISSUE = {
        "MALFORMED_NODE_ID": "Normalize canonical node IDs before graph materialization.",
        "DUPLICATE_NODE_ID": "Deduplicate canonical nodes earlier in identity synthesis.",
        "INVALID_NODE_NAMESPACE": "Ensure node IDs follow canonical://<namespace>/<path> format.",
        "MALFORMED_EDGE_SCHEMA": "Repair edge schema generation for from/to/type/confidence fields.",
        "INVALID_EDGE_TYPE": "Restrict edge types to IMPORT, DI, CONFIG, or DYNAMIC.",
        "UNRESOLVED_EDGE_REFERENCE": "Fix resolver outputs so all edges point to known canonical nodes.",
        "MISSING_DI_NODES": "Align DI resolver references with canonical node IDs.",
        "AST_DI_DIVERGENCE_TRACKED": "Review AST versus DI drift and update resolver policies.",
        "GRAPH_DI_DRIFT": "Reconcile unified graph DI edges with resolver DI evidence.",
        "MISSING_ENTRYPOINTS": "Validate entrypoint configuration against canonical node IDs.",
        "UNREACHABLE_NODES": "Restore missing bridge dependencies for isolated nodes or mark allowed isolation explicitly.",
        "UNEXPECTED_ORPHAN_SUBGRAPH": "Resolve namespace partitioning by introducing explicit bridge dependencies.",
        "CYCLE_POLICY_VIOLATION": "Add explicit cycle policy annotations or refactor cyclic ownership boundaries.",
    }

    _SECTION_DOMAIN = {
        "structural_validation": "structural_integrity",
        "resolver_consistency": "dependency_consistency",
        "reachability_analysis": "topology_validation",
        "semantic_validation": "semantic_observations",
    }

    def run(
        self,
        validation_result: Dict[str, Any],
        graph_data: Optional[Dict[str, Any]] = None,
        resolver_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        _ = resolver_data

        validation = validation_result if isinstance(validation_result, dict) else {}
        detailed = validation.get("detailed_results")
        detailed_results = detailed if isinstance(detailed, dict) else {}

        domain_impact = self._domain_impact_map(validation)
        counts = self._collect_issue_counts(validation)
        node_centrality, max_degree = self._graph_centrality(graph_data)

        raw_issues = self._collect_domain_issues(validation)
        grouped_issues = self._collapse_duplicate_issues(raw_issues)
        ranked_root_causes = self._rank_root_causes(grouped_issues, counts, domain_impact, node_centrality, max_degree)

        system_health = self._build_system_health(validation, detailed_results)
        status = self._diagnostic_status(validation, ranked_root_causes, system_health)

        causal_chain = self._build_causal_chain(status, ranked_root_causes)
        summary = self._build_summary(status, ranked_root_causes)
        validation_sections = self._build_validation_sections(detailed_results)

        top_issues = self._to_top_issues(ranked_root_causes)
        failure_domains = self._failure_domains(ranked_root_causes)
        recommended_actions = self._build_recommendations(ranked_root_causes)

        primary_root_cause = ranked_root_causes[0]["description"] if ranked_root_causes else "No material failures detected."
        confidence = round(float(ranked_root_causes[0]["confidence"]), 3) if ranked_root_causes else self._health_confidence(system_health)

        diagnostics_payload = {
            "status": status,
            "root_causes": ranked_root_causes,
            "causal_chain": causal_chain,
            "system_health": system_health,
            "summary": summary,
            "validation_sections": validation_sections,
            # Compatibility fields for existing consumers/harnesses.
            "top_issues": top_issues,
            "failure_domains": failure_domains,
            "recommended_actions": recommended_actions,
        }

        return {
            "diagnostics": diagnostics_payload,
            # Compatibility fields for existing CLI composition.
            "root_cause": primary_root_cause,
            "confidence": confidence,
            "top_issues": top_issues,
            "failure_domains": failure_domains,
            "recommended_actions": recommended_actions,
        }

    def _collect_domain_issues(self, validation_result: Dict[str, Any]) -> List[Issue]:
        detailed = validation_result.get("detailed_results")
        detailed_results = detailed if isinstance(detailed, dict) else {}

        collected: List[Issue] = []
        for domain, layer in sorted(detailed_results.items(), key=lambda item: item[0]):
            payload = layer if isinstance(layer, dict) else {}
            for raw_issue in payload.get("issues", []) or []:
                issue = raw_issue if isinstance(raw_issue, dict) else {}
                merged = dict(issue)
                merged["domain"] = str(issue.get("domain") or domain)
                collected.append(merged)

        if not collected:
            for raw_issue in validation_result.get("issues", []) or []:
                issue = raw_issue if isinstance(raw_issue, dict) else {}
                merged = dict(issue)
                merged["domain"] = str(issue.get("domain") or "structural_integrity")
                collected.append(merged)

        return collected

    def _collapse_duplicate_issues(self, issues: Sequence[Issue]) -> List[Issue]:
        grouped: Dict[Tuple[str, str, str], Issue] = {}

        for issue in issues:
            domain = str(issue.get("domain", "unknown")).strip() or "unknown"
            issue_type = str(issue.get("type", "UNKNOWN")).strip().upper() or "UNKNOWN"
            message = self._normalize_text(str(issue.get("message", "")).strip())

            key = (domain, issue_type, message)
            if key not in grouped:
                grouped[key] = {
                    "domain": domain,
                    "type": issue_type,
                    "severity": str(issue.get("severity", "LOW")).upper(),
                    "message": str(issue.get("message", "")).strip(),
                    "count": int(issue.get("count", 0) or 0),
                    "sample_nodes": list(issue.get("sample_nodes") or issue.get("sample") or [])[:25],
                    "evidence": [
                        f"issue_type={issue_type}",
                        f"domain={domain}",
                        f"message={str(issue.get('message', '')).strip()}",
                    ],
                }
                continue

            current = grouped[key]
            current["count"] = int(current.get("count", 0)) + int(issue.get("count", 0) or 0)
            current["sample_nodes"] = self._merge_unique(
                list(current.get("sample_nodes", [])),
                list(issue.get("sample_nodes") or issue.get("sample") or []),
                limit=25,
            )

            current_sev = str(current.get("severity", "LOW")).upper()
            incoming_sev = str(issue.get("severity", "LOW")).upper()
            if self._SEVERITY_WEIGHT.get(incoming_sev, 0.0) > self._SEVERITY_WEIGHT.get(current_sev, 0.0):
                current["severity"] = incoming_sev

        return sorted(grouped.values(), key=lambda item: (str(item.get("domain")), str(item.get("type"))))

    def _rank_root_causes(
        self,
        grouped_issues: Sequence[Issue],
        all_issue_counts: Sequence[int],
        domain_impact: Dict[str, float],
        node_centrality: Dict[str, float],
        max_degree: float,
    ) -> List[RootCause]:
        max_count = max(all_issue_counts) if all_issue_counts else 1

        ranked: List[RootCause] = []
        for issue in grouped_issues:
            domain = str(issue.get("domain", "unknown")).strip() or "unknown"
            issue_type = str(issue.get("type", "UNKNOWN")).strip().upper() or "UNKNOWN"
            issue_count = int(issue.get("count", 0) or 0)
            sample_nodes = [str(node).strip() for node in (issue.get("sample_nodes") or []) if str(node).strip()]

            impact_weight = self._impact_weight(issue, domain_impact)
            centrality_impact = self._centrality_impact(sample_nodes, node_centrality, max_degree)
            failure_frequency = self._failure_frequency(issue_count, max_count)

            severity_score = round(
                (impact_weight * 0.5) + (centrality_impact * 0.3) + (failure_frequency * 0.2),
                3,
            )
            confidence_score = self._confidence_score(severity_score, len(sample_nodes), issue_count)

            description = self._root_cause_description(issue, sample_nodes)
            propagation_path = self._propagation_path(issue)
            evidence = self._issue_evidence(issue, impact_weight, centrality_impact, failure_frequency)

            ranked.append(
                {
                    "id": "",
                    "type": self._ISSUE_TYPE.get(domain, "SEMANTIC"),
                    "severity": severity_score,
                    "confidence": confidence_score,
                    "description": description,
                    "affected_nodes": sample_nodes[:25],
                    "evidence": evidence,
                    "propagation_path": propagation_path,
                    "_domain": domain,
                    "_issue_type": issue_type,
                    "_count": issue_count,
                }
            )

        ranked.sort(
            key=lambda item: (
                -float(item.get("severity", 0.0)),
                -float(item.get("confidence", 0.0)),
                str(item.get("_domain", "")),
                str(item.get("_issue_type", "")),
            )
        )

        for index, root_cause in enumerate(ranked, start=1):
            issue_type = str(root_cause.get("_issue_type", "unknown")).lower()
            root_cause["id"] = f"rc-{index:02d}-{issue_type}"

        for item in ranked:
            item.pop("_domain", None)
            item.pop("_issue_type", None)
            item.pop("_count", None)

        return ranked

    def _domain_impact_map(self, validation_result: Dict[str, Any]) -> Dict[str, float]:
        ranked_domains = validation_result.get("failure_domains_ranked")
        rows = ranked_domains if isinstance(ranked_domains, list) else []

        impact_map: Dict[str, float] = {}
        for row in rows:
            item = row if isinstance(row, dict) else {}
            domain = str(item.get("domain", "")).strip()
            if not domain:
                continue
            impact_map[domain] = self._clamp01(float(item.get("impact_score", 0.0) or 0.0))

        return impact_map

    def _collect_issue_counts(self, validation_result: Dict[str, Any]) -> List[int]:
        counts: List[int] = []
        for issue in self._collect_domain_issues(validation_result):
            counts.append(max(1, int(issue.get("count", 0) or 0)))
        return counts

    def _graph_centrality(self, graph_data: Optional[Dict[str, Any]]) -> Tuple[Dict[str, float], float]:
        doc = graph_data if isinstance(graph_data, dict) else {}
        graph = doc.get("graph") if isinstance(doc.get("graph"), dict) else doc
        edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []

        degree: Dict[str, int] = defaultdict(int)
        for edge in edges:
            payload = edge if isinstance(edge, dict) else {}
            src = str(payload.get("from") or payload.get("source") or "").strip()
            dst = str(payload.get("to") or payload.get("target") or "").strip()
            if src:
                degree[src] += 1
            if dst:
                degree[dst] += 1

        max_degree = float(max(degree.values())) if degree else 0.0
        centrality = {node: float(value) for node, value in degree.items()}
        return centrality, max_degree

    def _impact_weight(self, issue: Issue, domain_impact: Dict[str, float]) -> float:
        severity = str(issue.get("severity", "LOW")).upper()
        base = self._SEVERITY_WEIGHT.get(severity, 0.20)
        domain = str(issue.get("domain", "")).strip()
        impact_bonus = self._clamp01(domain_impact.get(domain, 0.0) * 0.25)
        return self._clamp01(base + impact_bonus)

    def _centrality_impact(self, affected_nodes: Sequence[str], node_centrality: Dict[str, float], max_degree: float) -> float:
        if not affected_nodes:
            return 0.30
        if max_degree <= 0.0:
            return 0.30

        values: List[float] = []
        for node in affected_nodes[:25]:
            values.append(self._clamp01(float(node_centrality.get(node, 0.0)) / max_degree))

        if not values:
            return 0.30
        return round(sum(values) / len(values), 3)

    def _failure_frequency(self, count: int, max_count: int) -> float:
        denominator = max(1, int(max_count))
        return round(self._clamp01(float(max(1, count)) / float(denominator)), 3)

    def _confidence_score(self, severity_score: float, evidence_count: int, issue_count: int) -> float:
        evidence_strength = self._clamp01((min(5, evidence_count) * 0.1) + (min(20, issue_count) / 40.0))
        confidence = (severity_score * 0.65) + (evidence_strength * 0.35)
        return round(self._clamp01(confidence), 3)

    def _root_cause_description(self, issue: Issue, sample_nodes: Sequence[str]) -> str:
        issue_type = str(issue.get("type", "UNKNOWN")).upper()
        domain = str(issue.get("domain", "unknown"))
        message = str(issue.get("message", "")).strip()

        if issue_type == "UNREACHABLE_NODES":
            return (
                "Nodes are orphaned from active entrypoints, typically due to missing inbound DI/IMPORT edges "
                "introduced by resolver coverage gaps."
            )

        if issue_type == "UNEXPECTED_ORPHAN_SUBGRAPH":
            return (
                "Disconnected clusters are caused by namespace partitioning plus missing bridge dependencies "
                "between otherwise related subgraphs."
            )

        if issue_type == "MISSING_DI_NODES":
            return (
                "Resolver produced DI references for canonical nodes that do not exist, breaking dependency integrity "
                "and downstream reachability."
            )

        if message:
            return f"{domain}::{issue_type} - {message}"

        if sample_nodes:
            return f"{domain}::{issue_type} impacts sampled nodes and downstream graph behavior."

        return f"{domain}::{issue_type} produced a validation degradation signal."

    def _propagation_path(self, issue: Issue) -> List[str]:
        issue_type = str(issue.get("type", "UNKNOWN")).upper()
        domain = str(issue.get("domain", "unknown"))

        if domain == "dependency_consistency" or issue_type in {"MISSING_DI_NODES", "GRAPH_DI_DRIFT"}:
            return [
                "AST extraction",
                "Resolver consistency",
                "Graph build",
                "Reachability analysis",
                "Validation failure",
            ]

        if domain == "topology_validation":
            return [
                "Resolver consistency",
                "Graph build",
                "Reachability analysis",
                "Semantic validation",
                "Validation failure",
            ]

        if domain == "structural_integrity":
            return [
                "Canonical synthesis",
                "Graph build",
                "Reachability analysis",
                "Validation failure",
            ]

        return [
            "AST extraction",
            "Resolver consistency",
            "Graph build",
            "Semantic validation",
            "Validation failure",
        ]

    def _issue_evidence(
        self,
        issue: Issue,
        impact_weight: float,
        centrality_impact: float,
        failure_frequency: float,
    ) -> List[str]:
        issue_type = str(issue.get("type", "UNKNOWN")).upper()
        domain = str(issue.get("domain", "unknown"))
        issue_count = int(issue.get("count", 0) or 0)

        evidence = list(issue.get("evidence") or [])
        evidence.append(f"formula_inputs: impact_weight={impact_weight:.3f}")
        evidence.append(f"formula_inputs: graph_centrality_impact={centrality_impact:.3f}")
        evidence.append(f"formula_inputs: failure_frequency={failure_frequency:.3f}")
        evidence.append(f"observed_count={issue_count}")
        evidence.append(f"domain={domain}")
        evidence.append(f"issue_type={issue_type}")

        unique: List[str] = []
        for item in evidence:
            value = str(item).strip()
            if value and value not in unique:
                unique.append(value)

        return unique[:20]

    def _build_system_health(
        self,
        validation_result: Dict[str, Any],
        detailed_results: Dict[str, Any],
    ) -> Dict[str, float]:
        has_any_validation = bool(detailed_results) or bool(validation_result.get("trust_breakdown"))

        structural = self._domain_score(detailed_results.get("structural_integrity"), validation_result, "structural_integrity", has_any_validation)
        dependency = self._domain_score(detailed_results.get("dependency_consistency"), validation_result, "dependency_consistency", has_any_validation)
        connectivity = self._domain_score(detailed_results.get("topology_validation"), validation_result, "topology_validation", has_any_validation)
        semantic = self._domain_score(detailed_results.get("semantic_observations"), validation_result, "semantic_observations", has_any_validation)

        return {
            "structural_health": structural,
            "graph_connectivity": connectivity,
            "dependency_integrity": dependency,
            "semantic_consistency": semantic,
        }

    def _domain_score(
        self,
        layer: Any,
        validation_result: Dict[str, Any],
        score_key: str,
        has_any_validation: bool,
    ) -> float:
        payload = layer if isinstance(layer, dict) else {}
        metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}

        if isinstance(metrics.get("domain_score"), (int, float)):
            return round(self._clamp01(float(metrics.get("domain_score") or 0.0)), 3)

        trust_breakdown = validation_result.get("trust_breakdown")
        if isinstance(trust_breakdown, dict):
            scores = trust_breakdown.get("scores") if isinstance(trust_breakdown.get("scores"), dict) else {}
            if isinstance(scores.get(score_key), (int, float)):
                return round(self._clamp01(float(scores.get(score_key) or 0.0)), 3)

        if has_any_validation:
            return 0.5

        return 0.0

    def _diagnostic_status(
        self,
        validation_result: Dict[str, Any],
        root_causes: Sequence[RootCause],
        system_health: Dict[str, float],
    ) -> str:
        if bool(validation_result.get("policy_critical_failure")):
            return "FAIL"

        if root_causes:
            max_severity = max(float(item.get("severity", 0.0) or 0.0) for item in root_causes)
            min_health = min(system_health.values()) if system_health else 1.0
            if max_severity >= 0.85 or min_health < 0.40:
                return "FAIL"
            return "DEGRADED"

        return "PASS"

    def _build_causal_chain(self, status: str, root_causes: Sequence[RootCause]) -> List[Dict[str, Any]]:
        if status == "PASS":
            return []

        if not root_causes:
            return [
                {
                    "step": 1,
                    "cause": "Validation produced a non-pass verdict without an explicit issue payload.",
                    "effect": "Diagnostics marked the system as degraded due to missing causal evidence.",
                }
            ]

        primary = root_causes[0]
        cause_text = str(primary.get("description", "primary failure"))
        issue_type = str(primary.get("type", "SEMANTIC"))

        return [
            {
                "step": 1,
                "cause": f"AST evidence introduced {issue_type.lower()} instability.",
                "effect": "Resolver consistency confidence was reduced.",
            },
            {
                "step": 2,
                "cause": "Resolver inconsistency propagated into graph assembly.",
                "effect": "Graph build contained disconnected or unresolved dependency paths.",
            },
            {
                "step": 3,
                "cause": "Graph anomalies reduced reachability coverage and semantic coherence.",
                "effect": "Validation layers emitted ranked failure signals.",
            },
            {
                "step": 4,
                "cause": cause_text,
                "effect": "Authority/policy verdict receives a failure-grade diagnostic signal.",
            },
        ]

    def _build_summary(self, status: str, root_causes: Sequence[RootCause]) -> Dict[str, Any]:
        if not root_causes:
            return {
                "primary_failure_mode": "No material failure mode detected.",
                "secondary_failure_modes": [],
                "stability_class": "STABLE",
            }

        primary = root_causes[0]
        secondary = [str(item.get("description", "")) for item in root_causes[1:4] if str(item.get("description", "")).strip()]

        severity_values = [float(item.get("severity", 0.0) or 0.0) for item in root_causes]
        max_severity = max(severity_values) if severity_values else 0.0
        interacting = len(root_causes) >= 3 and len({str(item.get("type", "")) for item in root_causes}) >= 2

        stability_class = "STABLE"
        if any(str(item.get("type", "")).upper() in {"STRUCTURAL", "RESOLVER"} and float(item.get("severity", 0.0) or 0.0) >= 0.85 for item in root_causes):
            stability_class = "BROKEN"
        elif interacting:
            stability_class = "FRAGILE"
        elif max_severity > 0.2:
            stability_class = "DEGRADED"

        if status == "FAIL" and stability_class == "STABLE":
            stability_class = "DEGRADED"

        return {
            "primary_failure_mode": str(primary.get("description", "Unknown primary failure mode.")),
            "secondary_failure_modes": secondary,
            "stability_class": stability_class,
        }

    def _build_validation_sections(self, detailed_results: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        sections: Dict[str, Dict[str, Any]] = {}

        for section_name, domain_key in self._SECTION_DOMAIN.items():
            layer = detailed_results.get(domain_key)
            payload = layer if isinstance(layer, dict) else {}
            metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
            issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []

            score = metrics.get("domain_score")
            if not isinstance(score, (int, float)):
                score = 0.5
            score_value = round(self._clamp01(float(score)), 3)

            status = "PASS"
            if score_value < 0.65:
                status = "FAIL"
            elif score_value < 0.90:
                status = "DEGRADED"

            summary = "No significant issues detected."
            if issues:
                top_issue = issues[0] if isinstance(issues[0], dict) else {}
                issue_type = str(top_issue.get("type", "signal_detected"))
                message = str(top_issue.get("message", "")).strip()
                summary = f"{issue_type}: {message}" if message else issue_type

            sections[section_name] = {
                "status": status,
                "score": score_value,
                "summary": summary,
            }

        return sections

    def _to_top_issues(self, root_causes: Sequence[RootCause]) -> List[Dict[str, Any]]:
        top_issues: List[Dict[str, Any]] = []
        for rank, cause in enumerate(root_causes, start=1):
            severity = float(cause.get("severity", 0.0) or 0.0)
            severity_label = "LOW"
            if severity >= 0.8:
                severity_label = "HIGH"
            elif severity >= 0.5:
                severity_label = "MEDIUM"

            top_issues.append(
                {
                    "rank": rank,
                    "type": str(cause.get("id", "unknown")),
                    "domain": str(cause.get("type", "SEMANTIC")).lower(),
                    "severity": severity_label,
                    "impact_score": round(severity, 3),
                    "message": str(cause.get("description", "")),
                    "sample_nodes": list(cause.get("affected_nodes") or [])[:25],
                }
            )

        return top_issues

    def _failure_domains(self, root_causes: Sequence[RootCause]) -> List[str]:
        ordered: List[str] = []
        for cause in root_causes:
            domain = str(cause.get("type", "SEMANTIC")).lower()
            if domain and domain not in ordered:
                ordered.append(domain)
        return ordered

    def _build_recommendations(self, root_causes: Sequence[RootCause]) -> List[str]:
        recommendations: List[str] = []

        for cause in root_causes:
            cause_id = str(cause.get("id", "")).upper()
            issue_type = cause_id.split("-", 2)[-1].upper() if cause_id else ""
            action = self._ACTION_BY_ISSUE.get(issue_type)
            if action and action not in recommendations:
                recommendations.append(action)

        if not recommendations and root_causes:
            recommendations.append("Address the primary root cause first, then re-validate dependency and reachability flows.")

        if not recommendations:
            recommendations.append("No corrective action required; continue drift monitoring.")

        return recommendations

    def _health_confidence(self, system_health: Dict[str, float]) -> float:
        if not system_health:
            return 0.5
        avg = sum(float(value) for value in system_health.values()) / max(1, len(system_health))
        return round(self._clamp01(0.45 + (avg * 0.4)), 3)

    def _merge_unique(self, left: Sequence[str], right: Sequence[str], limit: int) -> List[str]:
        merged: List[str] = []
        for item in list(left) + list(right):
            value = str(item).strip()
            if not value or value in merged:
                continue
            merged.append(value)
            if len(merged) >= limit:
                break
        return merged

    def _normalize_text(self, text: str) -> str:
        return " ".join(text.strip().lower().split())

    def _clamp01(self, value: float) -> float:
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return float(value)


def run_diagnostics(
    validation_result: Dict[str, Any],
    graph_data: Optional[Dict[str, Any]] = None,
    resolver_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    layer = DiagnosticSynthesisLayer()
    return layer.run(
        validation_result=validation_result,
        graph_data=graph_data,
        resolver_data=resolver_data,
    )


def run_diagnostics_from_artifacts(
    validation_path: Path,
    graph_path: Path | None = None,
    resolver_path: Path | None = None,
) -> Dict[str, Any]:
    validation_payload = load_json(validation_path)

    graph_payload: Optional[Dict[str, Any]] = None
    if graph_path and graph_path.exists():
        loaded_graph = load_json(graph_path)
        if isinstance(loaded_graph, dict):
            graph_payload = loaded_graph

    resolver_payload: Optional[Dict[str, Any]] = None
    if resolver_path and resolver_path.exists():
        loaded_resolver = load_json(resolver_path)
        if isinstance(loaded_resolver, dict):
            resolver_payload = loaded_resolver

    return run_diagnostics(
        validation_result=validation_payload if isinstance(validation_payload, dict) else {},
        graph_data=graph_payload,
        resolver_data=resolver_payload,
    )
