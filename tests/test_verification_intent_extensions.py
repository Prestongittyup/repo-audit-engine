from __future__ import annotations

from repo_audit_engine.pipeline.validation import run_verification


def _graph_payload() -> dict:
    return {
        "nodes": [
            {"id": "canonical://file/apps/api/main.py", "metadata": {"is_entrypoint": True}},
            {"id": "canonical://function/apps/api/main.py/main", "metadata": {}},
            {"id": "canonical://function/apps/domain/service.py/execute", "metadata": {}},
        ],
        "edges": [
            {
                "from": "canonical://file/apps/api/main.py",
                "to": "canonical://function/apps/api/main.py/main",
                "type": "DI",
                "confidence": 1.0,
            },
            {
                "from": "canonical://function/apps/api/main.py/main",
                "to": "canonical://function/apps/domain/service.py/execute",
                "type": "DI",
                "confidence": 1.0,
            },
        ],
    }


def _resolver_payload() -> dict:
    return {
        "edges": [
            {
                "from": "canonical://file/apps/api/main.py",
                "to": "canonical://function/apps/api/main.py/main",
                "type": "DI",
                "source": "DI",
            },
            {
                "from": "canonical://function/apps/api/main.py/main",
                "to": "canonical://function/apps/domain/service.py/execute",
                "type": "DI",
                "source": "DI",
            },
        ]
    }


def _resolver_payload_with_drift() -> dict:
    return {
        "edges": [
            {
                "from": "canonical://file/apps/api/main.py",
                "to": "canonical://function/apps/api/main.py/main",
                "type": "DI",
                "source": "DI",
            },
            {
                "from": "canonical://function/apps/api/main.py/main",
                "to": "canonical://function/apps/domain/service.py/execute",
                "type": "IMPORT",
                "source": "AST",
            },
        ]
    }


def test_verification_includes_architecture_semantic_and_causal_domains() -> None:
    result = run_verification(
        graph_data=_graph_payload(),
        resolver_data=_resolver_payload(),
        entrypoints=["canonical://file/apps/api/main.py"],
        min_trust=0.40,
        execution_evidence={
            "runtime_source": "runtime_trace",
            "runtime_validation": {
                "call_event_count": 200,
                "coverage_ratio": 0.25,
                "entrypoint_count": 1,
                "executed_entrypoint_count": 1,
            },
            "runtime_static_reconciliation": {
                "overlap_ratio": 0.25,
            },
            "distribution": {
                "HOT": 10,
                "WARM": 8,
                "COLD": 2,
                "DEAD": 0,
            },
            "architecture_constraints": {
                "summary": {
                    "violation_count_total": 3,
                    "violation_ratio": 0.30,
                    "constraint_coverage_ratio": 0.90,
                    "domain_score": 0.45,
                    "rule_violation_counts": {
                        "LAYER_DIRECTION_VIOLATION": 2,
                    },
                },
                "intent_model": {
                    "layer_counts": {"api": 2, "domain": 1},
                },
                "violations": [
                    {
                        "rule_id": "LAYER_DIRECTION_VIOLATION",
                        "source_node_id": "canonical://function/apps/domain/service.py/execute",
                        "target_node_id": "canonical://file/apps/api/main.py",
                    }
                ],
            },
            "semantic_clusters": {
                "summary": {
                    "cluster_count": 2,
                    "cross_context_cluster_count": 1,
                    "duplicate_intent_cluster_count": 1,
                    "abstraction_collision_count": 2,
                    "domain_score": 0.50,
                },
                "duplicate_intent_clusters": [
                    {
                        "id": "cluster-001",
                        "members": [
                            "apps/api/main.py",
                            "apps/domain/service.py",
                        ],
                    }
                ],
                "abstraction_collisions": [
                    {
                        "concept_key": "auth",
                    }
                ],
            },
            "causal_flow": {
                "summary": {
                    "runtime_signal_present": True,
                    "analysis_enforced": True,
                    "workflow_count": 1,
                    "role_coverage_ratio": 0.30,
                    "direct_api_to_persistence_count": 1,
                    "domain_score": 0.35,
                    "observed_roles": ["api", "persistence"],
                },
                "issues": [
                    {
                        "type": "DIRECT_API_TO_PERSISTENCE_PATH",
                        "severity": "LOW",
                        "message": "API calls persistence directly.",
                    }
                ],
                "warnings": ["Role diversity is low."],
                "workflows": [
                    {
                        "run_id": "run_001",
                        "role_sequence": ["api", "persistence"],
                    }
                ],
            },
        },
    )

    detailed = result.get("detailed_results", {})
    policy = result.get("policy_decision", {})
    policy_metrics = policy.get("policy_metrics", {}) if isinstance(policy, dict) else {}

    assert "architectural_intent" in detailed
    assert "semantic_consistency" in detailed
    assert "causal_flow" in detailed

    assert "architecture_intent_score" in policy_metrics
    assert "semantic_consistency_score" in policy_metrics
    assert "causal_flow_score" in policy_metrics

    failure_domains = result.get("failure_domains", [])
    assert "architectural_intent" in failure_domains
    assert "semantic_consistency" in failure_domains
    assert "causal_flow" in failure_domains

    soft_fail_reasons = policy.get("soft_fail_reasons", []) if isinstance(policy, dict) else []
    assert any("Architectural intent score" in str(item) for item in soft_fail_reasons)
    assert any("Semantic consistency score" in str(item) for item in soft_fail_reasons)
    assert any("Causal flow score" in str(item) for item in soft_fail_reasons)


def test_ast_di_divergence_promotes_architecture_drift_failure_domain() -> None:
    result = run_verification(
        graph_data=_graph_payload(),
        resolver_data=_resolver_payload_with_drift(),
        entrypoints=["canonical://file/apps/api/main.py"],
        min_trust=0.40,
        execution_evidence={
            "runtime_source": "runtime_trace",
            "runtime_validation": {
                "call_event_count": 260,
                "coverage_ratio": 0.84,
                "entrypoint_count": 1,
                "executed_entrypoint_count": 1,
            },
            "runtime_static_reconciliation": {
                "overlap_ratio": 0.40,
            },
            "distribution": {
                "HOT": 8,
                "WARM": 4,
                "COLD": 1,
                "DEAD": 0,
            },
        },
    )

    trust_breakdown = result.get("trust_breakdown", {}) if isinstance(result.get("trust_breakdown"), dict) else {}
    assert bool(trust_breakdown.get("architecture_drift_triggered", False))
    assert float(trust_breakdown.get("architecture_drift_penalty", 0.0) or 0.0) == 0.2

    failure_domains = result.get("failure_domains", [])
    assert "architecture_drift" in failure_domains

    policy = result.get("policy_decision", {}) if isinstance(result.get("policy_decision"), dict) else {}
    policy_metrics = policy.get("policy_metrics", {}) if isinstance(policy.get("policy_metrics"), dict) else {}
    assert bool(policy_metrics.get("architecture_drift_triggered", False))

    soft_fail_reasons = policy.get("soft_fail_reasons", []) if isinstance(policy, dict) else []
    assert any("Architecture drift detected" in str(item) for item in soft_fail_reasons)
