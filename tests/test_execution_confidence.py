from __future__ import annotations

from pathlib import Path

import pytest

from repo_audit_engine.classification.dead_code import build_dead_code_report
from repo_audit_engine.pipeline.validation import run_verification


def _graph_payload() -> dict:
    return {
        "nodes": [
            {"id": "canonical://file/app/main.py", "metadata": {"is_entrypoint": True}},
            {"id": "canonical://function/app/main.py/start", "metadata": {}},
        ],
        "edges": [
            {
                "from": "canonical://file/app/main.py",
                "to": "canonical://function/app/main.py/start",
                "type": "DI",
                "confidence": 1.0,
            }
        ],
    }


def _resolver_payload() -> dict:
    return {
        "edges": [
            {
                "from": "canonical://file/app/main.py",
                "to": "canonical://function/app/main.py/start",
                "type": "DI",
                "source": "DI",
            },
            {
                "from": "canonical://file/app/main.py",
                "to": "canonical://function/app/main.py/start",
                "type": "IMPORT",
                "source": "AST",
            },
        ]
    }


def test_low_execution_confidence_penalizes_trust_and_validity() -> None:
    result = run_verification(
        graph_data=_graph_payload(),
        resolver_data=_resolver_payload(),
        entrypoints=["canonical://file/app/main.py"],
        min_trust=0.40,
        execution_evidence={
            "runtime_source": "runtime_trace",
            "runtime_validation": {
                "call_event_count": 279,
                "coverage_ratio": 0.05122,
                "entrypoint_count": 49,
                "executed_entrypoint_count": 1,
            },
            "runtime_static_reconciliation": {
                "overlap_ratio": 0.001,
            },
            "distribution": {
                "HOT": 2,
                "WARM": 254,
                "COLD": 2246,
                "DEAD": 2496,
            },
        },
    )

    trust_breakdown = result.get("trust_breakdown", {})
    scores = trust_breakdown.get("scores", {}) if isinstance(trust_breakdown, dict) else {}
    execution_confidence = float(scores.get("execution_confidence", 0.0) or 0.0)

    assert execution_confidence == pytest.approx(0.02, abs=1e-6)
    assert bool(trust_breakdown.get("execution_gate_applied", False))
    assert bool(trust_breakdown.get("coverage_hard_gate_applied", False))
    assert float(result.get("trust_score", 1.0) or 1.0) < 0.40
    assert not bool(result.get("system_valid", True))

    policy_metrics = result.get("policy_decision", {}).get("policy_metrics", {})
    assert float(policy_metrics.get("execution_confidence", 1.0) or 1.0) < 0.30
    assert bool(policy_metrics.get("coverage_hard_gate_applied", False))


def test_high_execution_confidence_keeps_system_valid() -> None:
    result = run_verification(
        graph_data=_graph_payload(),
        resolver_data=_resolver_payload(),
        entrypoints=["canonical://file/app/main.py"],
        min_trust=0.40,
        execution_evidence={
            "runtime_source": "runtime_trace",
            "runtime_validation": {
                "call_event_count": 120,
                "coverage_ratio": 0.40,
                "entrypoint_count": 1,
                "executed_entrypoint_count": 1,
            },
            "runtime_static_reconciliation": {
                "overlap_ratio": 0.20,
            },
            "distribution": {
                "HOT": 20,
                "WARM": 10,
                "COLD": 2,
                "DEAD": 0,
            },
        },
    )

    trust_breakdown = result.get("trust_breakdown", {})
    scores = trust_breakdown.get("scores", {}) if isinstance(trust_breakdown, dict) else {}
    execution_confidence = float(scores.get("execution_confidence", 0.0) or 0.0)

    assert execution_confidence == pytest.approx(1.0, abs=1e-6)
    assert float(scores.get("runtime_authority", 0.0) or 0.0) >= 0.95
    assert not bool(trust_breakdown.get("execution_gate_applied", True))
    assert not bool(trust_breakdown.get("coverage_hard_gate_applied", True))
    assert float(result.get("trust_score", 0.0) or 0.0) >= 0.90
    assert bool(result.get("system_valid", False))


def test_coverage_hard_gate_invalidates_even_with_high_execution_confidence() -> None:
    result = run_verification(
        graph_data=_graph_payload(),
        resolver_data=_resolver_payload(),
        entrypoints=["canonical://file/app/main.py"],
        min_trust=0.40,
        execution_evidence={
            "runtime_source": "runtime_trace",
            "runtime_validation": {
                "call_event_count": 150,
                "coverage_ratio": 0.20,
                "entrypoint_count": 1,
                "executed_entrypoint_count": 1,
            },
            "runtime_static_reconciliation": {
                "overlap_ratio": 0.20,
            },
            "distribution": {
                "HOT": 12,
                "WARM": 4,
                "COLD": 1,
                "DEAD": 0,
            },
        },
    )

    trust_breakdown = result.get("trust_breakdown", {})
    scores = trust_breakdown.get("scores", {}) if isinstance(trust_breakdown, dict) else {}
    assert float(scores.get("execution_confidence", 0.0) or 0.0) == pytest.approx(1.0, abs=1e-6)
    assert bool(trust_breakdown.get("coverage_hard_gate_applied", False))
    assert not bool(result.get("system_valid", True))

    policy = result.get("policy_decision", {})
    hard_fail_reasons = policy.get("hard_fail_reasons", []) if isinstance(policy, dict) else []
    assert any("Runtime coverage ratio fell below hard floor" in str(item) for item in hard_fail_reasons)


def test_scenario_coverage_completeness_gates_policy() -> None:
    result = run_verification(
        graph_data=_graph_payload(),
        resolver_data=_resolver_payload(),
        entrypoints=["canonical://file/app/main.py"],
        min_trust=0.40,
        execution_evidence={
            "runtime_source": "runtime_trace",
            "runtime_validation": {
                "call_event_count": 240,
                "coverage_ratio": 0.78,
                "entrypoint_count": 4,
                "executed_entrypoint_count": 1,
                "entrypoints": [
                    "apps/api/main.py",
                    "apps/cli/main.py",
                    "apps/jobs/scheduler.py",
                    "apps/jobs/worker.py",
                ],
                "executed_entrypoints": [
                    "apps/api/main.py",
                ],
            },
            "runtime_static_reconciliation": {
                "overlap_ratio": 0.40,
            },
            "distribution": {
                "HOT": 10,
                "WARM": 4,
                "COLD": 2,
                "DEAD": 0,
            },
            "runtime_scenarios": {
                "summary": {
                    "selected_node_count": 8,
                    "max_scenarios": 20,
                },
                "scenarios": [
                    {"path": "apps/api/main.py", "priority_score": 4.0},
                    {"path": "apps/cli/main.py", "priority_score": 3.0},
                    {"path": "apps/jobs/scheduler.py", "priority_score": 2.0},
                    {"path": "apps/jobs/worker.py", "priority_score": 1.0},
                ],
            },
        },
    )

    policy = result.get("policy_decision", {})
    assert isinstance(policy, dict)
    assert not bool(result.get("system_valid", True))

    policy_metrics = policy.get("policy_metrics", {}) if isinstance(policy.get("policy_metrics"), dict) else {}
    assert float(policy_metrics.get("entrypoint_coverage_completeness", 1.0) or 1.0) < 0.30
    assert float(policy_metrics.get("domain_coverage_completeness", 1.0) or 1.0) < 0.34
    assert float(policy_metrics.get("scenario_coverage_completeness", 1.0) or 1.0) < 0.55


def test_dead_code_confidence_scales_with_runtime_coverage(tmp_path: Path) -> None:
    payload = {
        "runtime_validation": {
            "coverage_ratio": 0.05,
        },
        "nodes": [
            {
                "node_id": "canonical://function/app/main.py/start",
                "classification": "DEAD",
                "runtime_hits": 0,
                "inbound_edges": 0,
                "executable_references": 0,
                "non_executable_references": 0,
                "ast_references": 0,
            }
        ],
    }

    result = build_dead_code_report(heat_payload=payload, output_dir=tmp_path)
    report = result.get("report", {})
    dead_candidates = report.get("dead_candidates", [])

    assert isinstance(dead_candidates, list) and dead_candidates

    first = dead_candidates[0]
    assert float(first.get("probability", 0.0) or 0.0) == pytest.approx(1.0, abs=1e-6)
    assert float(first.get("confidence", 0.0) or 0.0) == pytest.approx(0.05, abs=1e-6)

    summary = report.get("summary", {})
    assert float(summary.get("coverage_ratio", 0.0) or 0.0) == pytest.approx(0.05, abs=1e-6)
    assert int(summary.get("high_confidence_count", 1)) == 0


def test_dead_code_report_reclassifies_inbound_dead_candidate(tmp_path: Path) -> None:
    payload = {
        "runtime_validation": {
            "coverage_ratio": 0.90,
        },
        "nodes": [
            {
                "node_id": "canonical://function/app/main.py/background_job",
                "classification": "DEAD",
                "runtime_hits": 0,
                "inbound_edges": 2,
                "executable_references": 0,
                "non_executable_references": 0,
                "ast_references": 0,
            }
        ],
    }

    result = build_dead_code_report(heat_payload=payload, output_dir=tmp_path)
    report = result.get("report", {})

    candidates = report.get("candidates", []) if isinstance(report.get("candidates"), list) else []
    assert candidates
    assert str(candidates[0].get("classification", "")).upper() == "COLD"
    guardrails = candidates[0].get("guardrails", [])
    assert any("inbound_edges" in str(item) for item in guardrails)

    dead_candidates = report.get("dead_candidates", [])
    assert isinstance(dead_candidates, list)
    assert dead_candidates == []
