from __future__ import annotations

from repo_audit_engine.architecture.constraints import evaluate_architecture_constraints


def _graph_payload() -> dict:
    nodes = [
        {
            "id": "canonical://file/apps/api/endpoints/user_router.py",
            "kind": "file",
            "path": "apps/api/endpoints/user_router.py",
        },
        {
            "id": "canonical://file/apps/api/services/account_service.py",
            "kind": "file",
            "path": "apps/api/services/account_service.py",
        },
        {
            "id": "canonical://file/apps/domain/services/user_service.py",
            "kind": "file",
            "path": "apps/domain/services/user_service.py",
        },
        {
            "id": "canonical://file/apps/infra/adapters/user_store.py",
            "kind": "file",
            "path": "apps/infra/adapters/user_store.py",
        },
        {
            "id": "canonical://file/apps/api/orchestration/user_orchestrator.py",
            "kind": "file",
            "path": "apps/api/orchestration/user_orchestrator.py",
        },
        {
            "id": "canonical://file/apps/shared/contracts/user_contracts.py",
            "kind": "file",
            "path": "apps/shared/contracts/user_contracts.py",
        },
    ]

    edges = [
        {
            "from": "canonical://file/apps/domain/services/user_service.py",
            "to": "canonical://file/apps/api/endpoints/user_router.py",
            "type": "IMPORT",
        },
        {
            "from": "canonical://file/apps/api/services/account_service.py",
            "to": "canonical://file/apps/infra/adapters/user_store.py",
            "type": "IMPORT",
        },
        {
            "from": "canonical://file/apps/api/endpoints/user_router.py",
            "to": "canonical://file/apps/infra/adapters/user_store.py",
            "type": "DI",
        },
        {
            "from": "canonical://file/apps/api/endpoints/user_router.py",
            "to": "canonical://file/apps/shared/contracts/user_contracts.py",
            "type": "IMPORT",
        },
        {
            "from": "canonical://file/apps/api/orchestration/user_orchestrator.py",
            "to": "canonical://file/apps/infra/adapters/user_store.py",
            "type": "DI",
        },
    ]

    return {
        "nodes": nodes,
        "edges": edges,
    }


def test_constraint_engine_detects_layer_and_boundary_violations() -> None:
    report = evaluate_architecture_constraints(_graph_payload())

    summary = report.get("summary", {})
    violations = report.get("violations", [])

    assert int(summary.get("violation_count_total", 0) or 0) >= 3
    assert float(summary.get("domain_score", 1.0)) < 1.0

    rule_ids = {str(item.get("rule_id", "")) for item in violations if isinstance(item, dict)}

    assert "LAYER_DIRECTION_VIOLATION" in rule_ids
    assert "SERVICE_IMPORTS_ADAPTER" in rule_ids
    assert "DIRECT_STATE_CHANGE_BYPASSES_ORCHESTRATOR" in rule_ids


def test_constraint_engine_is_deterministic() -> None:
    first = evaluate_architecture_constraints(_graph_payload())
    second = evaluate_architecture_constraints(_graph_payload())

    assert first == second
