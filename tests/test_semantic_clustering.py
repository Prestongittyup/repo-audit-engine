from __future__ import annotations

from repo_audit_engine.analysis.semantic_clusters import analyze_semantic_clusters


def _manifest_rows() -> list[dict]:
    return [
        {
            "path": "apps/api/auth_router.py",
            "module": "apps.api.auth_router",
            "imports": ["apps.core.auth_service", "apps.security.auth_manager"],
            "symbols": [{"kind": "class", "name": "AuthRouter"}],
        },
        {
            "path": "apps/core/auth_service.py",
            "module": "apps.core.auth_service",
            "imports": ["apps.security.auth_manager"],
            "symbols": [{"kind": "class", "name": "AuthService"}],
        },
        {
            "path": "apps/security/auth_manager.py",
            "module": "apps.security.auth_manager",
            "imports": ["apps.core.auth_service"],
            "symbols": [{"kind": "class", "name": "AuthManager"}],
        },
        {
            "path": "apps/billing/invoice_service.py",
            "module": "apps.billing.invoice_service",
            "imports": ["apps.billing.tax_engine"],
            "symbols": [{"kind": "class", "name": "InvoiceService"}],
        },
    ]


def _static_rows() -> list[dict]:
    return [
        {
            "file_path": "apps/api/auth_router.py",
            "imports": [
                {"module": "apps.core.auth_service", "resolved_path": "apps/core/auth_service.py"},
                {"module": "apps.security.auth_manager", "resolved_path": "apps/security/auth_manager.py"},
            ],
            "calls": [
                {"caller": "AuthRouter.handle", "callee": "auth_service.verify_token"},
                {"caller": "AuthRouter.handle", "callee": "auth_manager.open_session"},
            ],
            "functions": [{"name": "handle_auth"}],
            "classes": [{"name": "AuthRouter"}],
        },
        {
            "file_path": "apps/core/auth_service.py",
            "imports": [{"module": "apps.security.auth_manager", "resolved_path": "apps/security/auth_manager.py"}],
            "calls": [{"caller": "AuthService.verify", "callee": "auth_manager.validate_policy"}],
            "functions": [{"name": "verify_token"}],
            "classes": [{"name": "AuthService"}],
        },
        {
            "file_path": "apps/security/auth_manager.py",
            "imports": [{"module": "apps.core.auth_service", "resolved_path": "apps/core/auth_service.py"}],
            "calls": [{"caller": "AuthManager.open", "callee": "auth_service.verify_token"}],
            "functions": [{"name": "open_session"}],
            "classes": [{"name": "AuthManager"}],
        },
        {
            "file_path": "apps/billing/invoice_service.py",
            "imports": [{"module": "apps.billing.tax_engine", "resolved_path": "apps/billing/tax_engine.py"}],
            "calls": [{"caller": "InvoiceService.calculate", "callee": "tax_engine.compute_tax"}],
            "functions": [{"name": "calculate_invoice"}],
            "classes": [{"name": "InvoiceService"}],
        },
    ]


def test_semantic_clusters_detect_duplicate_intent_and_abstraction_collisions() -> None:
    report = analyze_semantic_clusters(
        manifest_rows=_manifest_rows(),
        static_rows=_static_rows(),
        similarity_threshold=0.25,
        min_shared_tokens=2,
    )

    summary = report.get("summary", {})
    duplicate_clusters = report.get("duplicate_intent_clusters", [])
    abstraction_collisions = report.get("abstraction_collisions", [])

    assert int(summary.get("cluster_count", 0) or 0) >= 1
    assert int(summary.get("duplicate_intent_cluster_count", 0) or 0) >= 1
    assert isinstance(duplicate_clusters, list) and duplicate_clusters

    assert int(summary.get("abstraction_collision_count", 0) or 0) >= 1
    assert isinstance(abstraction_collisions, list) and abstraction_collisions

    concept_keys = {str(item.get("concept_key", "")) for item in abstraction_collisions if isinstance(item, dict)}
    assert any("auth" in key for key in concept_keys)


def test_semantic_clustering_is_deterministic() -> None:
    first = analyze_semantic_clusters(_manifest_rows(), _static_rows(), similarity_threshold=0.25, min_shared_tokens=2)
    second = analyze_semantic_clusters(_manifest_rows(), _static_rows(), similarity_threshold=0.25, min_shared_tokens=2)

    assert first == second
