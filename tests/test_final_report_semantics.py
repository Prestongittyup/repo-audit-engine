from __future__ import annotations

from pathlib import Path
from typing import Any

from repo_audit_engine.io.artifacts import build_final_report


def _contains_score_key(payload: Any) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in {"score", "domain_score"}:
                return True
            if _contains_score_key(value):
                return True
        return False
    if isinstance(payload, list):
        return any(_contains_score_key(item) for item in payload)
    return False


def test_build_final_report_emits_layered_audits_and_guardrails(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"

    result = build_final_report(
        output_dir=output_dir,
        manifest_result={
            "summary": {
                "file_count": 12,
                "python_file_count": 9,
                "entrypoints": ["app.py", "cli.py"],
            }
        },
        static_result={"summary": {"function_count": 34, "class_count": 4}},
        graph_result={
            "graph": {
                "summary": {
                    "node_count": 42,
                    "edge_count": 68,
                    "import_edge_count": 18,
                    "call_edge_count": 50,
                }
            }
        },
        runtime_result={
            "flow_graph": {
                "summary": {
                    "run_count": 3,
                    "call_event_count": 120,
                    "import_event_count": 24,
                    "timeout_count": 0,
                    "coverage_ratio": 0.41,
                }
            }
        },
        heat_result={"heat": {"distribution": {"HOT": 4, "WARM": 8, "COLD": 10, "DEAD": 20}}},
        dead_code_result={
            "report": {
                "summary": {"dead_candidate_count": 7},
                "dead_candidates": [{"node_id": "file:legacy.py", "probability": 0.92}],
            }
        },
        diagnostics_result={
            "root_cause": "Architectural intent mismatch",
            "diagnostics": {
                "status": "FAIL",
                "summary": {
                    "primary_failure_mode": "Constraint violation dominates runtime-only confidence.",
                },
                "validation_sections": {
                    "structural_validation": {"status": "DEGRADED"},
                    "semantic_validation": {"status": "FAIL"},
                },
                "root_causes": [
                    {
                        "description": "Cross-owner coupling bypasses orchestration boundaries.",
                        "severity": 0.86,
                    }
                ],
                "failure_domains": ["architecture", "semantic"],
                "recommended_actions": [
                    "Enforce owner boundaries via explicit orchestration contracts.",
                    "Merge duplicated semantic intent clusters.",
                ],
            },
        },
        trust_payload={
            "score": 0.23,
            "breakdown": {
                "execution_gate_applied": True,
                "min_execution_confidence": 0.30,
                "domain_scores": {
                    "structural_integrity": 0.88,
                    "dependency_consistency": 0.84,
                    "topology_validation": 0.79,
                    "semantic_observations": 0.68,
                },
                "scores": {
                    "structural_integrity": 0.88,
                    "dependency_consistency": 0.84,
                    "topology_validation": 0.79,
                    "semantic_observations": 0.68,
                    "execution_confidence": 0.24,
                },
            },
        },
        system_valid=False,
        architecture_result={
            "report": {
                "summary": {
                    "domain_score": 0.73,
                    "violation_count_total": 3,
                    "boundary_crossing_count": 2,
                    "constraint_coverage_ratio": 0.81,
                    "violation_ratio": 0.22,
                    "boundary_crossing_ratio": 0.14,
                    "classified_node_count": 38,
                }
            }
        },
        semantic_result={
            "report": {
                "summary": {
                    "domain_score": 0.61,
                    "duplicate_intent_cluster_count": 2,
                    "cross_context_cluster_count": 1,
                    "high_overlap_cluster_count": 1,
                    "abstraction_collision_count": 1,
                }
            }
        },
        causal_flow_result={
            "report": {
                "summary": {
                    "domain_score": 0.56,
                    "workflow_count": 1,
                    "role_coverage_ratio": 0.5,
                    "direct_api_to_persistence_count": 1,
                },
                "issues": [
                    {"type": "MISSING_DOMAIN_DECISION_STAGE"},
                ],
            }
        },
    )

    report = result.get("report", {})
    report_path = Path(str(result.get("report_path", "")))

    assert report_path.exists(), "final_report.json was not written to disk"

    for key in [
        "structural_audit",
        "behavioral_audit",
        "redundancy_overlap_audit",
        "architectural_quality_audit",
        "architect_auditor",
        "design_quality_signals",
        "audit_layers",
    ]:
        assert isinstance(report.get(key), dict), f"Expected report['{key}'] to be a dict"

    assert report.get("structural_audit", {}).get("title") == "Structural Audit (what exists)"
    assert report.get("behavioral_audit", {}).get("title") == "Behavioral Audit (what it does)"
    assert report.get("redundancy_overlap_audit", {}).get("title") == "Redundancy & overlap audit (what is duplicated)"
    assert report.get("architectural_quality_audit", {}).get("title") == "Architectural quality audit (what should exist)"

    principles = report.get("design_quality_signals", {}).get("principle_enforcement", {})
    assert isinstance(principles, dict)
    for principle_name in [
        "intent_over_execution",
        "structure_over_runtime_alone",
        "multi_signal_confirmation_over_single_metric",
    ]:
        block = principles.get(principle_name)
        assert isinstance(block, dict), f"Missing principle block: {principle_name}"
        assert bool(block.get("enforced", False)) is True

    multi_signal = principles.get("multi_signal_confirmation_over_single_metric", {})
    assert int(multi_signal.get("signal_count", 0) or 0) >= 4
    assert bool(multi_signal.get("single_metric_override_blocked", False)) is True

    architect = report.get("architect_auditor", {})
    assert isinstance(architect, dict)
    assert architect.get("violation_taxonomy") == [
        "layer_violation",
        "circular_dependency",
        "redundant_domain",
        "orphan_module",
        "overcoupled_node",
    ]

    questions = architect.get("questions", {})
    assert isinstance(questions, dict)
    for question_key in [
        "structure_matches_intended_architecture",
        "responsibility_clean_or_duplicated",
        "behavior_aligns_with_structure",
    ]:
        block = questions.get(question_key)
        assert isinstance(block, dict), f"Missing architect auditor question: {question_key}"
        assert isinstance(block.get("status"), str)
        assert isinstance(block.get("violations"), list)

    hard_constraints = architect.get("hard_constraints", {})
    assert isinstance(hard_constraints, dict)
    for name in [
        "no_new_scoring_systems",
        "no_upstream_artifact_mutation",
        "no_feedback_loops",
        "deterministic_outputs",
    ]:
        assert bool(hard_constraints.get(name, False)) is True

    assert _contains_score_key(architect) is False



def test_build_final_report_flags_single_metric_mode(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"

    result = build_final_report(
        output_dir=output_dir,
        manifest_result={"summary": {}},
        static_result={"summary": {}},
        graph_result={"graph": {"summary": {}}},
        runtime_result={"flow_graph": {"summary": {}}},
        heat_result={"heat": {"distribution": {}}},
        dead_code_result={"report": {"summary": {}, "dead_candidates": []}},
        diagnostics_result={"diagnostics": {}},
        trust_payload={
            "score": 0.05,
            "breakdown": {
                "scores": {"execution_confidence": 0.2},
            },
        },
        system_valid=False,
        architecture_result=None,
        semantic_result=None,
        causal_flow_result=None,
    )

    report = result.get("report", {})
    principles = report.get("design_quality_signals", {}).get("principle_enforcement", {})
    multi_signal = principles.get("multi_signal_confirmation_over_single_metric", {})

    assert multi_signal.get("status") == "FAIL"
    assert bool(multi_signal.get("single_metric_override_blocked", True)) is False

    for key in [
        "structural_audit",
        "behavioral_audit",
        "redundancy_overlap_audit",
        "architectural_quality_audit",
        "architect_auditor",
    ]:
        section = report.get(key, {})
        assert isinstance(section, dict)

    for key in [
        "structural_audit",
        "behavioral_audit",
        "redundancy_overlap_audit",
        "architectural_quality_audit",
    ]:
        section = report.get(key, {})
        assert isinstance(section.get("findings"), list)
        assert section.get("findings"), f"Expected non-empty findings in section: {key}"

    architect = report.get("architect_auditor", {})
    behavior_block = (
        architect.get("questions", {}).get("behavior_aligns_with_structure", {})
        if isinstance(architect.get("questions"), dict)
        else {}
    )
    assert behavior_block.get("status") == "INSUFFICIENT_EVIDENCE"
    assert behavior_block.get("answer") == "insufficient_evidence"
