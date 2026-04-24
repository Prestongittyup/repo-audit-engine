from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

Issue = Dict[str, Any]


class DiagnosticSynthesisLayer:
    """Synthesizes concise root-cause diagnostics from validation output."""

    _SEVERITY_RANK = {
        "HIGH": 0,
        "MEDIUM": 1,
        "LOW": 2,
        "INFO": 3,
    }

    _SEVERITY_BASE_IMPACT = {
        "HIGH": 1.00,
        "MEDIUM": 0.70,
        "LOW": 0.40,
        "INFO": 0.20,
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
        "UNREACHABLE_NODES": "Connect unreachable nodes to supported entrypoint flows or mark them isolated.",
        "UNEXPECTED_ORPHAN_SUBGRAPH": "Mark intentional isolation explicitly or reconnect orphan subgraphs.",
        "CYCLE_POLICY_VIOLATION": "Add explicit cycle policy annotations or refactor cyclic ownership boundaries.",
    }

    def run(
        self,
        validation_result: Dict[str, Any],
        graph_data: Optional[Dict[str, Any]] = None,
        resolver_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        del graph_data
        del resolver_data

        trust_score = float(validation_result.get("trust_score", 0.0) or 0.0)
        ranked_domains = list(validation_result.get("failure_domains_ranked", []) or [])

        issues = self._collect_domain_issues(validation_result)
        top_issues = self._rank_issues(issues, ranked_domains)

        if ranked_domains:
            primary_domain = str(ranked_domains[0].get("domain", "")).strip()
            primary_reason = str(ranked_domains[0].get("reason", "")).strip() or "signal_detected"
            root_cause = f"{primary_domain}:{primary_reason}" if primary_domain else primary_reason
            root_domain = primary_domain or "unknown"
        elif top_issues:
            first = top_issues[0]
            root_domain = str(first.get("domain", "unknown"))
            root_cause = f"{root_domain}:{first.get('type', 'signal_detected')}"
        else:
            root_domain = "none"
            root_cause = "none"

        top_impact = float(top_issues[0].get("impact_score", 0.0) if top_issues else 0.0)
        confidence = self._compute_confidence(trust_score, top_impact, bool(top_issues))

        recommended_actions = self._build_recommendations(top_issues)
        example_nodes = self._collect_example_nodes(top_issues)

        summary_text = self._build_summary_text(root_domain, trust_score, top_issues)

        return {
            "root_cause": root_cause,
            "root_domain": root_domain,
            "confidence": confidence,
            "top_issues": top_issues,
            "failure_domains": [str(item.get("domain", "")) for item in ranked_domains if str(item.get("domain", "")).strip()],
            "example_nodes": example_nodes,
            "recommended_actions": recommended_actions,
            "diagnostic_summary": summary_text,
        }

    def _collect_domain_issues(self, validation_result: Dict[str, Any]) -> List[Issue]:
        detailed = validation_result.get("detailed_results", {})
        if not isinstance(detailed, dict):
            detailed = {}

        collected: List[Issue] = []
        for domain, layer in sorted(detailed.items(), key=lambda item: item[0]):
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

    def _rank_issues(
        self,
        issues: Sequence[Issue],
        ranked_domains: Sequence[Dict[str, Any]],
        limit: int = 8,
    ) -> List[Dict[str, Any]]:
        domain_impact = {
            str(item.get("domain", "")): float(item.get("impact_score", 0.0) or 0.0)
            for item in ranked_domains
            if str(item.get("domain", "")).strip()
        }

        ranked: List[Dict[str, Any]] = []
        for issue in issues:
            severity = str(issue.get("severity", "LOW")).upper()
            count = int(issue.get("count", 0) or 0)
            domain = str(issue.get("domain", "unknown")).strip() or "unknown"

            severity_base = self._SEVERITY_BASE_IMPACT.get(severity, 0.20)
            count_factor = 1.0 + min(count, 50) / 50.0
            domain_factor = 1.0 + min(domain_impact.get(domain, 0.0), 0.50)

            impact_score = min(1.0, severity_base * count_factor * domain_factor)

            ranked.append(
                {
                    "domain": domain,
                    "type": str(issue.get("type", "UNKNOWN")),
                    "severity": severity,
                    "message": str(issue.get("message", "")).strip(),
                    "count": count,
                    "impact_score": round(impact_score, 3),
                    "sample_nodes": list(issue.get("sample_nodes") or issue.get("sample") or [])[:25],
                }
            )

        ranked.sort(
            key=lambda item: (
                -float(item.get("impact_score", 0.0)),
                self._SEVERITY_RANK.get(str(item.get("severity", "")).upper(), 99),
                str(item.get("domain", "")),
                str(item.get("type", "")),
            )
        )

        top = ranked[:limit]
        for index, item in enumerate(top, start=1):
            item["rank"] = index

        return top

    def _compute_confidence(self, trust_score: float, top_impact: float, has_issues: bool) -> float:
        if not has_issues:
            return round(min(0.99, 0.60 + (trust_score * 0.35)), 3)

        confidence = 0.50 + (min(top_impact, 1.0) * 0.30) + ((1.0 - trust_score) * 0.20)
        return round(min(0.98, max(0.30, confidence)), 3)

    def _build_recommendations(self, top_issues: Sequence[Dict[str, Any]]) -> List[str]:
        recommendations: List[str] = []

        for issue in top_issues:
            issue_type = str(issue.get("type", "")).strip().upper()
            action = self._ACTION_BY_ISSUE.get(issue_type)
            if action and action not in recommendations:
                recommendations.append(action)

        if not recommendations:
            recommendations.append("No blocking issues detected; continue monitoring structural and resolver drift trends.")

        return recommendations

    def _collect_example_nodes(self, top_issues: Iterable[Dict[str, Any]]) -> List[str]:
        collected: List[str] = []

        for issue in top_issues:
            for node in issue.get("sample_nodes", []) or []:
                value = str(node).strip()
                if not value:
                    continue
                if value not in collected:
                    collected.append(value)
                if len(collected) >= 20:
                    return collected

        return collected

    def _build_summary_text(
        self,
        root_domain: str,
        trust_score: float,
        top_issues: Sequence[Dict[str, Any]],
    ) -> str:
        if not top_issues:
            return "No material validation issues detected."

        issue_types = [str(issue.get("type", "")).strip() for issue in top_issues[:3]]
        joined_types = ", ".join(issue for issue in issue_types if issue)

        return (
            f"Primary risk domain is {root_domain}; top issue types: {joined_types}. "
            f"Current trust score is {trust_score:.3f}."
        )


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
