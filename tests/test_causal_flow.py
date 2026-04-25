from __future__ import annotations

from repo_audit_engine.runtime.causal_flow import analyze_causal_flow


def _trace_rows() -> list[dict]:
    return [
        {
            "run_id": "run_001",
            "event": "call",
            "callee_node_id": "function:apps/api/endpoints/task_router.py:submit_task",
            "caller_node_id": "",
            "file": "apps/api/endpoints/task_router.py",
            "function": "submit_task",
            "module": "apps.api.endpoints.task_router",
        },
        {
            "run_id": "run_001",
            "event": "call",
            "callee_node_id": "function:apps/api/policy_engine/rules.py:validate_request",
            "caller_node_id": "function:apps/api/endpoints/task_router.py:submit_task",
            "file": "apps/api/policy_engine/rules.py",
            "function": "validate_request",
            "module": "apps.api.policy_engine.rules",
        },
        {
            "run_id": "run_001",
            "event": "call",
            "callee_node_id": "function:household_os/runtime/orchestrator.py:orchestrate",
            "caller_node_id": "function:apps/api/policy_engine/rules.py:validate_request",
            "file": "household_os/runtime/orchestrator.py",
            "function": "orchestrate",
            "module": "household_os.runtime.orchestrator",
        },
        {
            "run_id": "run_001",
            "event": "call",
            "callee_node_id": "function:household_os/core/decision_engine.py:decide",
            "caller_node_id": "function:household_os/runtime/orchestrator.py:orchestrate",
            "file": "household_os/core/decision_engine.py",
            "function": "decide",
            "module": "household_os.core.decision_engine",
        },
        {
            "run_id": "run_001",
            "event": "call",
            "callee_node_id": "function:apps/api/integration_core/event_bridge.py:publish",
            "caller_node_id": "function:household_os/core/decision_engine.py:decide",
            "file": "apps/api/integration_core/event_bridge.py",
            "function": "publish",
            "module": "apps.api.integration_core.event_bridge",
        },
        {
            "run_id": "run_001",
            "event": "call",
            "callee_node_id": "function:apps/api/identity/sqlalchemy_repository.py:save_session",
            "caller_node_id": "function:apps/api/integration_core/event_bridge.py:publish",
            "file": "apps/api/identity/sqlalchemy_repository.py",
            "function": "save_session",
            "module": "apps.api.identity.sqlalchemy_repository",
        },
    ]


def test_causal_flow_reconstructs_workflow_roles() -> None:
    report = analyze_causal_flow(
        trace_rows=_trace_rows(),
        flow_payload={"edges": []},
        manifest_summary={"entrypoints": ["apps/api/main.py"]},
    )

    summary = report.get("summary", {})
    observed_roles = summary.get("observed_roles", [])
    issues = report.get("issues", [])

    assert bool(summary.get("runtime_signal_present", False))
    assert int(summary.get("workflow_count", 0) or 0) >= 1
    assert float(summary.get("role_coverage_ratio", 0.0) or 0.0) > 0.30

    assert "api" in observed_roles
    assert "persistence" in observed_roles

    issue_types = {str(item.get("type", "")) for item in issues if isinstance(item, dict)}
    assert "NO_WORKFLOW_RECONSTRUCTION" not in issue_types


def test_causal_flow_is_advisory_without_runtime_calls() -> None:
    report = analyze_causal_flow(trace_rows=[], flow_payload={"edges": []}, manifest_summary={})

    summary = report.get("summary", {})
    warnings = report.get("warnings", [])

    assert not bool(summary.get("runtime_signal_present", True))
    assert bool(warnings)
    assert float(summary.get("domain_score", 0.0) or 0.0) == 1.0
