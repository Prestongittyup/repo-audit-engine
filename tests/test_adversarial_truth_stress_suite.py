from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

import pytest

from repo_audit_engine.analysis.semantic_clusters import analyze_semantic_clusters
from repo_audit_engine.architecture.constraints import evaluate_architecture_constraints
from repo_audit_engine.classification.dead_code import build_dead_code_report
from repo_audit_engine.pipeline.validation import run_verification
from repo_audit_engine.runtime.causal_flow import analyze_causal_flow


def _cid(rel_path: str) -> str:
    return f"canonical://file/{rel_path}"


def _node(rel_path: str, entrypoint: bool = False) -> Dict[str, Any]:
    return {
        "id": _cid(rel_path),
        "path": rel_path,
        "metadata": {"is_entrypoint": bool(entrypoint)},
    }


def _edge(source_path: str, target_path: str, edge_type: str, confidence: float = 1.0) -> Dict[str, Any]:
    return {
        "from": _cid(source_path),
        "to": _cid(target_path),
        "type": str(edge_type).strip().upper(),
        "confidence": float(confidence),
    }


def _resolver_edge(source_path: str, target_path: str, edge_type: str, source: str) -> Dict[str, str]:
    return {
        "from": _cid(source_path),
        "to": _cid(target_path),
        "type": str(edge_type).strip().upper(),
        "source": str(source).strip().upper(),
    }


def _canonical_to_path(node_id: str) -> str:
    text = str(node_id or "").strip()
    if text.startswith("canonical://") and "/" in text[len("canonical://") :]:
        suffix = text[len("canonical://") :]
        return suffix.split("/", 1)[1]
    return text


def _base_adversarial_graph() -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    paths = {
        "entry": "apps/api/endpoints/checkout_router.py",
        "api_service": "apps/api/services/checkout_service.py",
        "orchestrator": "apps/api/orchestration/checkout_orchestrator.py",
        "domain_service": "apps/domain/services/pricing_service.py",
        "domain_policy": "apps/domain/policy/order_policy.py",
        "infra_adapter": "apps/infra/adapters/payment_gateway_adapter.py",
        "infra_repo": "apps/infra/repositories/order_repository.py",
        "contracts": "apps/shared/contracts/order_contracts.py",
        "shadow": "apps/api/helpers/pricing_shadow.py",
    }

    graph_data = {
        "nodes": [
            _node(paths["entry"], entrypoint=True),
            _node(paths["api_service"]),
            _node(paths["orchestrator"]),
            _node(paths["domain_service"]),
            _node(paths["domain_policy"]),
            _node(paths["infra_adapter"]),
            _node(paths["infra_repo"]),
            _node(paths["contracts"]),
            _node(paths["shadow"]),
        ],
        "edges": [
            _edge(paths["entry"], paths["api_service"], "DI"),
            _edge(paths["api_service"], paths["infra_repo"], "DI"),
            _edge(paths["domain_service"], paths["entry"], "IMPORT"),
            _edge(paths["api_service"], paths["infra_adapter"], "IMPORT"),
            _edge(paths["entry"], paths["contracts"], "IMPORT"),
            _edge(paths["orchestrator"], paths["domain_service"], "DI"),
            _edge(paths["orchestrator"], paths["infra_adapter"], "DI"),
            _edge(paths["shadow"], paths["domain_policy"], "DYNAMIC"),
            _edge(paths["domain_policy"], paths["shadow"], "IMPORT"),
            _edge(paths["domain_policy"], paths["infra_repo"], "DI"),
        ],
    }

    resolver_data = {
        "edges": [
            _resolver_edge(paths["entry"], paths["api_service"], "DI", "DI"),
            _resolver_edge(paths["api_service"], paths["infra_repo"], "DI", "DI"),
            _resolver_edge(paths["orchestrator"], paths["domain_service"], "DI", "DI"),
            _resolver_edge(paths["orchestrator"], paths["infra_adapter"], "DI", "DI"),
            _resolver_edge(paths["domain_policy"], paths["infra_repo"], "DI", "DI"),
            _resolver_edge(paths["domain_service"], paths["entry"], "IMPORT", "AST"),
            _resolver_edge(paths["api_service"], paths["infra_adapter"], "IMPORT", "AST"),
            _resolver_edge(paths["shadow"], paths["domain_policy"], "DYNAMIC", "AST"),
            _resolver_edge(paths["domain_policy"], paths["shadow"], "IMPORT", "AST"),
            _resolver_edge(paths["entry"], paths["contracts"], "IMPORT", "AST"),
        ]
    }

    entrypoints = [_cid(paths["entry"])]
    return graph_data, resolver_data, entrypoints


def _semantic_rows_with_duplication() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Set[str]], Dict[str, str]]:
    manifest_rows = [
        {
            "path": "apps/security/token_gate.py",
            "module": "apps.security.token_gate",
            "imports": ["apps.identity.credential_router", "apps.auth.session_service"],
            "symbols": [{"kind": "class", "name": "SessionAccessService"}],
        },
        {
            "path": "apps/identity/credential_router.py",
            "module": "apps.identity.credential_router",
            "imports": ["apps.security.token_gate", "apps.auth.session_service"],
            "symbols": [{"kind": "class", "name": "SessionCredentialManager"}],
        },
        {
            "path": "apps/auth/session_service.py",
            "module": "apps.auth.session_service",
            "imports": ["apps.security.token_gate", "apps.identity.credential_router"],
            "symbols": [{"kind": "class", "name": "SessionAccessRouter"}],
        },
        {
            "path": "apps/orchestration/session_orchestrator.py",
            "module": "apps.orchestration.session_orchestrator",
            "imports": ["apps.auth.session_service"],
            "symbols": [{"kind": "class", "name": "SessionOrchestrator"}],
        },
        {
            "path": "apps/persistence/session_store.py",
            "module": "apps.persistence.session_store",
            "imports": [],
            "symbols": [{"kind": "class", "name": "SessionStore"}],
        },
        {
            "path": "apps/persistence/session_repository.py",
            "module": "apps.persistence.session_repository",
            "imports": [],
            "symbols": [{"kind": "class", "name": "SessionRepository"}],
        },
    ]

    static_rows = [
        {
            "file_path": "apps/security/token_gate.py",
            "imports": [
                {"module": "apps.identity.credential_router", "resolved_path": "apps/identity/credential_router.py"},
                {"module": "apps.auth.session_service", "resolved_path": "apps/auth/session_service.py"},
            ],
            "calls": [
                {"caller": "SessionAccessService.check", "callee": "credential_router.verify_session"},
                {"caller": "SessionAccessService.check", "callee": "session_service.open_session"},
            ],
            "functions": [{"name": "check_access_token"}],
            "classes": [{"name": "SessionAccessService"}],
        },
        {
            "file_path": "apps/identity/credential_router.py",
            "imports": [
                {"module": "apps.security.token_gate", "resolved_path": "apps/security/token_gate.py"},
                {"module": "apps.auth.session_service", "resolved_path": "apps/auth/session_service.py"},
            ],
            "calls": [{"caller": "SessionCredentialManager.verify", "callee": "session_service.open_session"}],
            "functions": [{"name": "verify_session_token"}],
            "classes": [{"name": "SessionCredentialManager"}],
        },
        {
            "file_path": "apps/auth/session_service.py",
            "imports": [
                {"module": "apps.security.token_gate", "resolved_path": "apps/security/token_gate.py"},
                {"module": "apps.identity.credential_router", "resolved_path": "apps/identity/credential_router.py"},
            ],
            "calls": [
                {"caller": "SessionAccessRouter.open", "callee": "token_gate.check_access_token"},
                {"caller": "SessionAccessRouter.open", "callee": "credential_router.verify_session_token"},
            ],
            "functions": [{"name": "open_session"}],
            "classes": [{"name": "SessionAccessRouter"}],
        },
        {
            "file_path": "apps/orchestration/session_orchestrator.py",
            "imports": [{"module": "apps.auth.session_service", "resolved_path": "apps/auth/session_service.py"}],
            "calls": [{"caller": "SessionOrchestrator.run", "callee": "session_service.open_session"}],
            "functions": [{"name": "run_session_flow"}],
            "classes": [{"name": "SessionOrchestrator"}],
        },
        {
            "file_path": "apps/persistence/session_store.py",
            "imports": [{"module": "apps.persistence.session_repository", "resolved_path": "apps/persistence/session_repository.py"}],
            "calls": [{"caller": "SessionStore.put", "callee": "session_repository.save_session"}],
            "functions": [{"name": "put_session"}],
            "classes": [{"name": "SessionStore"}],
        },
        {
            "file_path": "apps/persistence/session_repository.py",
            "imports": [],
            "calls": [{"caller": "SessionRepository.save", "callee": "db.commit"}],
            "functions": [{"name": "save_session"}],
            "classes": [{"name": "SessionRepository"}],
        },
    ]

    truth_groups = [
        {
            "apps/security/token_gate.py",
            "apps/identity/credential_router.py",
            "apps/auth/session_service.py",
        },
        {
            "apps/persistence/session_store.py",
            "apps/persistence/session_repository.py",
        },
    ]

    truth_domain_map = {
        "apps/security/token_gate.py": "authentication",
        "apps/identity/credential_router.py": "authentication",
        "apps/auth/session_service.py": "authentication",
        "apps/orchestration/session_orchestrator.py": "orchestration",
        "apps/persistence/session_store.py": "persistence",
        "apps/persistence/session_repository.py": "persistence",
    }

    return manifest_rows, static_rows, truth_groups, truth_domain_map


def _semantic_rows_with_misleading_naming() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Set[str]], Dict[str, str]]:
    manifest_rows = [
        {
            "path": "apps/auth/auth_helper.py",
            "module": "apps.auth.auth_helper",
            "imports": ["apps.persistence.auth_archive"],
            "symbols": [{"kind": "class", "name": "AuthSqlCacheRepositoryStore"}],
        },
        {
            "path": "apps/orchestration/payment_orchestrator.py",
            "module": "apps.orchestration.payment_orchestrator",
            "imports": ["apps.auth.auth_helper", "apps.persistence.auth_archive"],
            "symbols": [{"kind": "class", "name": "PaymentWorkflowStateTransitionPipelineOrchestrator"}],
        },
        {
            "path": "apps/persistence/auth_archive.py",
            "module": "apps.persistence.auth_archive",
            "imports": [],
            "symbols": [{"kind": "class", "name": "AuthSqlStorageRepositoryStore"}],
        },
        {
            "path": "apps/api/endpoints/login_router.py",
            "module": "apps.api.endpoints.login_router",
            "imports": ["apps.orchestration.payment_orchestrator"],
            "symbols": [{"kind": "class", "name": "LoginAuthRouter"}],
        },
        {
            "path": "apps/domain/policy/access_policy.py",
            "module": "apps.domain.policy.access_policy",
            "imports": ["apps.auth.auth_helper"],
            "symbols": [{"kind": "class", "name": "AccessPolicyEngine"}],
        },
    ]

    static_rows = [
        {
            "file_path": "apps/auth/auth_helper.py",
            "imports": [{"module": "apps.persistence.auth_archive", "resolved_path": "apps/persistence/auth_archive.py"}],
            "calls": [{"caller": "AuthSqlCacheRepositoryStore.persist", "callee": "auth_archive.save_sql_cache_storage"}],
            "functions": [{"name": "persist_sql_storage_cache"}, {"name": "issue_identity_token"}],
            "classes": [{"name": "AuthSqlCacheRepositoryStore"}],
        },
        {
            "file_path": "apps/orchestration/payment_orchestrator.py",
            "imports": [
                {"module": "apps.auth.auth_helper", "resolved_path": "apps/auth/auth_helper.py"},
                {"module": "apps.persistence.auth_archive", "resolved_path": "apps/persistence/auth_archive.py"},
            ],
            "calls": [
                {"caller": "PaymentWorkflowStateTransitionPipelineOrchestrator.execute", "callee": "auth_helper.issue_identity_token"},
                {"caller": "PaymentWorkflowStateTransitionPipelineOrchestrator.execute", "callee": "auth_archive.read_archive"},
            ],
            "functions": [{"name": "execute_payment_workflow_state_transition"}],
            "classes": [{"name": "PaymentWorkflowStateTransitionPipelineOrchestrator"}],
        },
        {
            "file_path": "apps/persistence/auth_archive.py",
            "imports": [],
            "calls": [{"caller": "AuthSqlStorageRepositoryStore.save", "callee": "sql_db.cache_commit"}],
            "functions": [{"name": "save_sql_cache_storage"}],
            "classes": [{"name": "AuthSqlStorageRepositoryStore"}],
        },
        {
            "file_path": "apps/api/endpoints/login_router.py",
            "imports": [{"module": "apps.orchestration.payment_orchestrator", "resolved_path": "apps/orchestration/payment_orchestrator.py"}],
            "calls": [{"caller": "LoginAuthRouter.login", "callee": "payment_orchestrator.route_login"}],
            "functions": [{"name": "login_with_identity_token"}],
            "classes": [{"name": "LoginAuthRouter"}],
        },
        {
            "file_path": "apps/domain/policy/access_policy.py",
            "imports": [{"module": "apps.auth.auth_helper", "resolved_path": "apps/auth/auth_helper.py"}],
            "calls": [{"caller": "AccessPolicyEngine.enforce", "callee": "auth_helper.issue_identity_token"}],
            "functions": [{"name": "enforce_access_policy"}],
            "classes": [{"name": "AccessPolicyEngine"}],
        },
    ]

    truth_groups = [
        {
            "apps/api/endpoints/login_router.py",
            "apps/domain/policy/access_policy.py",
        },
        {
            "apps/auth/auth_helper.py",
            "apps/persistence/auth_archive.py",
        },
    ]

    truth_domain_map = {
        "apps/auth/auth_helper.py": "persistence",
        "apps/orchestration/payment_orchestrator.py": "orchestration",
        "apps/persistence/auth_archive.py": "persistence",
        "apps/api/endpoints/login_router.py": "authentication",
        "apps/domain/policy/access_policy.py": "authentication",
    }

    return manifest_rows, static_rows, truth_groups, truth_domain_map


def _trace_checkout_flow() -> List[Dict[str, Any]]:
    return [
        {
            "run_id": "run_001",
            "event": "call",
            "callee_node_id": "function:apps/api/endpoints/checkout_router.py:submit_order",
            "file": "apps/api/endpoints/checkout_router.py",
            "function": "submit_order",
            "module": "apps.api.endpoints.checkout_router",
        },
        {
            "run_id": "run_001",
            "event": "call",
            "callee_node_id": "function:apps/domain/policy/order_policy.py:validate_order",
            "file": "apps/domain/policy/order_policy.py",
            "function": "validate_order",
            "module": "apps.domain.policy.order_policy",
        },
        {
            "run_id": "run_001",
            "event": "call",
            "callee_node_id": "function:apps/api/orchestration/checkout_orchestrator.py:orchestrate_order",
            "file": "apps/api/orchestration/checkout_orchestrator.py",
            "function": "orchestrate_order",
            "module": "apps.api.orchestration.checkout_orchestrator",
        },
        {
            "run_id": "run_001",
            "event": "call",
            "callee_node_id": "function:apps/infra/adapters/payment_gateway_adapter.py:charge_card",
            "file": "apps/infra/adapters/payment_gateway_adapter.py",
            "function": "charge_card",
            "module": "apps.infra.adapters.payment_gateway_adapter",
        },
        {
            "run_id": "run_001",
            "event": "call",
            "callee_node_id": "function:apps/infra/repositories/order_repository.py:save_order",
            "file": "apps/infra/repositories/order_repository.py",
            "function": "save_order",
            "module": "apps.infra.repositories.order_repository",
        },
    ]


def _trace_runtime_noise() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for index in range(24):
        rows.append(
            {
                "run_id": "noise",
                "event": "call",
                "callee_node_id": f"function:apps/api/endpoints/health_router.py:pulse_{index}",
                "file": "apps/api/endpoints/health_router.py",
                "function": f"pulse_{index}",
                "module": "apps.api.endpoints.health_router",
            }
        )
        rows.append(
            {
                "run_id": "noise",
                "event": "call",
                "callee_node_id": f"function:apps/api/observability/logging_probe.py:flush_{index}",
                "file": "apps/api/observability/logging_probe.py",
                "function": f"flush_{index}",
                "module": "apps.api.observability.logging_probe",
            }
        )
    return rows


def _flow_payload_for_trace() -> Dict[str, Any]:
    return {
        "edges": [
            {
                "source": "function:apps/api/endpoints/checkout_router.py:submit_order",
                "target": "function:apps/infra/repositories/order_repository.py:save_order",
                "type": "RUNTIME_CALL",
            },
            {
                "source": "function:apps/domain/policy/order_policy.py:validate_order",
                "target": "function:apps/api/orchestration/checkout_orchestrator.py:orchestrate_order",
                "type": "RUNTIME_CALL",
            },
        ]
    }


def _dead_code_payload_with_dynamic_trap() -> Dict[str, Any]:
    return {
        "runtime_validation": {
            "coverage_ratio": 0.62,
        },
        "nodes": [
            {
                "node_id": "canonical://function/apps/domain/dynamic_dispatch.py/dispatch_event",
                "classification": "DEAD",
                "runtime_hits": 0,
                "inbound_edges": 3,
                "executable_references": 5,
                "non_executable_references": 2,
                "ast_references": 7,
            },
            {
                "node_id": "canonical://function/apps/domain/unused_module.py/dead_branch",
                "classification": "DEAD",
                "runtime_hits": 0,
                "inbound_edges": 0,
                "executable_references": 0,
                "non_executable_references": 0,
                "ast_references": 0,
            },
            {
                "node_id": "canonical://function/apps/domain/live_module.py/hot_path",
                "classification": "HOT",
                "runtime_hits": 40,
                "inbound_edges": 6,
                "executable_references": 5,
                "non_executable_references": 2,
                "ast_references": 7,
            },
        ],
    }


def _collect_issue_types(validation_result: Mapping[str, Any], architecture_report: Mapping[str, Any]) -> Set[str]:
    issue_types: Set[str] = set()

    detailed = validation_result.get("detailed_results")
    if isinstance(detailed, Mapping):
        for layer in detailed.values():
            payload = layer if isinstance(layer, Mapping) else {}
            issues = payload.get("issues") if isinstance(payload.get("issues"), list) else []
            for issue in issues:
                issue_payload = issue if isinstance(issue, Mapping) else {}
                issue_type = str(issue_payload.get("type", "")).strip().upper()
                if issue_type:
                    issue_types.add(issue_type)

    violations = architecture_report.get("violations") if isinstance(architecture_report.get("violations"), list) else []
    for violation in violations:
        payload = violation if isinstance(violation, Mapping) else {}
        rule_id = str(payload.get("rule_id", "")).strip().upper()
        if rule_id:
            issue_types.add(rule_id)

    return issue_types


def _pairs_from_group(group: Iterable[str]) -> Set[Tuple[str, str]]:
    normalized = sorted({str(item).strip() for item in group if str(item).strip()})
    return {(left, right) for left, right in itertools.combinations(normalized, 2)}


def _semantic_pairwise_f1(clusters: Sequence[Mapping[str, Any]], truth_groups: Sequence[Set[str]]) -> float:
    predicted_pairs: Set[Tuple[str, str]] = set()
    for cluster in clusters:
        payload = cluster if isinstance(cluster, Mapping) else {}
        members = payload.get("members") if isinstance(payload.get("members"), list) else []
        predicted_pairs.update(_pairs_from_group(members))

    truth_pairs: Set[Tuple[str, str]] = set()
    for group in truth_groups:
        truth_pairs.update(_pairs_from_group(group))

    if not truth_pairs and not predicted_pairs:
        return 1.0
    if not truth_pairs:
        return 0.0

    intersection = predicted_pairs.intersection(truth_pairs)
    precision = len(intersection) / max(1, len(predicted_pairs))
    recall = len(intersection) / max(1, len(truth_pairs))

    if precision + recall == 0.0:
        return 0.0
    return (2.0 * precision * recall) / (precision + recall)


def _normalize_domain(domain: str) -> str:
    key = str(domain or "").strip().lower()
    mapping = {
        "auth": "authentication",
        "policy": "authentication",
        "orchestration": "orchestration",
        "runtime": "orchestration",
        "data": "persistence",
        "integration": "persistence",
        "observability": "orchestration",
        "general": "general",
    }
    return mapping.get(key, key or "general")


def _semantic_domain_accuracy(concept_domains: Sequence[Mapping[str, Any]], truth_domain_map: Mapping[str, str]) -> float:
    if not truth_domain_map:
        return 1.0

    predicted_map: Dict[str, str] = {}
    for row in concept_domains:
        payload = row if isinstance(row, Mapping) else {}
        path = str(payload.get("file_path", "")).strip()
        domain = _normalize_domain(str(payload.get("domain", "general")))
        if path:
            predicted_map[path] = domain

    matches = 0
    total = 0
    for file_path, expected_domain in sorted(truth_domain_map.items(), key=lambda item: item[0]):
        total += 1
        predicted = predicted_map.get(file_path, "general")
        if predicted == _normalize_domain(expected_domain):
            matches += 1

    return matches / max(1, total)


def _max_dead_confidence_for_nodes(dead_report: Mapping[str, Any], node_ids: Sequence[str]) -> float:
    targets = {str(item).strip() for item in node_ids if str(item).strip()}
    if not targets:
        return 0.0

    candidates = dead_report.get("dead_candidates") if isinstance(dead_report.get("dead_candidates"), list) else []
    max_confidence = 0.0
    for row in candidates:
        payload = row if isinstance(row, Mapping) else {}
        node_id = str(payload.get("node_id", "")).strip()
        if node_id not in targets:
            continue
        confidence = float(payload.get("confidence", 0.0) or 0.0)
        if confidence > max_confidence:
            max_confidence = confidence

    return max_confidence


def _run_truth_stress_case(
    tmp_path: Path,
    scenario_id: str,
    category: str,
    summary: str,
    *,
    graph_data: Mapping[str, Any],
    resolver_data: Mapping[str, Any],
    entrypoints: Sequence[str],
    manifest_rows: Sequence[Mapping[str, Any]],
    static_rows: Sequence[Mapping[str, Any]],
    trace_rows: Sequence[Mapping[str, Any]],
    flow_payload: Mapping[str, Any],
    runtime_validation: Mapping[str, Any],
    runtime_static_reconciliation: Mapping[str, Any],
    distribution: Mapping[str, Any],
    heat_payload: Mapping[str, Any],
    truth_groups: Sequence[Set[str]],
    truth_domain_map: Mapping[str, str],
    dynamic_dispatch_nodes: Sequence[str],
    expected: Mapping[str, Any],
) -> Dict[str, Any]:
    architecture_report = evaluate_architecture_constraints(graph_data)
    semantic_report = analyze_semantic_clusters(
        manifest_rows=manifest_rows,
        static_rows=static_rows,
        similarity_threshold=0.22,
        min_shared_tokens=2,
    )
    causal_report = analyze_causal_flow(
        trace_rows=trace_rows,
        flow_payload=flow_payload,
        manifest_summary={"entrypoints": [_canonical_to_path(item) for item in entrypoints]},
    )

    execution_evidence = {
        "runtime_source": "runtime_trace",
        "runtime_validation": dict(runtime_validation),
        "runtime_static_reconciliation": dict(runtime_static_reconciliation),
        "distribution": dict(distribution),
        "architecture_constraints": architecture_report,
        "semantic_clusters": semantic_report,
        "causal_flow": causal_report,
    }

    validation_result = run_verification(
        graph_data=dict(graph_data),
        resolver_data=dict(resolver_data),
        entrypoints=list(entrypoints),
        min_trust=0.40,
        execution_evidence=execution_evidence,
    )

    dead_code_bundle = build_dead_code_report(
        heat_payload=dict(heat_payload),
        output_dir=tmp_path / f"{scenario_id}_dead_code",
    )
    dead_report = dead_code_bundle.get("report", {}) if isinstance(dead_code_bundle, Mapping) else {}

    issue_types = _collect_issue_types(validation_result, architecture_report)
    validation_failure_domains = (
        validation_result.get("failure_domains")
        if isinstance(validation_result.get("failure_domains"), list)
        else []
    )
    trust_breakdown = (
        validation_result.get("trust_breakdown")
        if isinstance(validation_result.get("trust_breakdown"), Mapping)
        else {}
    )

    architecture_summary = architecture_report.get("summary") if isinstance(architecture_report.get("summary"), Mapping) else {}
    semantic_summary = semantic_report.get("summary") if isinstance(semantic_report.get("summary"), Mapping) else {}

    detailed = validation_result.get("detailed_results") if isinstance(validation_result.get("detailed_results"), Mapping) else {}
    dependency_layer = detailed.get("dependency_consistency") if isinstance(detailed.get("dependency_consistency"), Mapping) else {}
    execution_layer = detailed.get("execution_confidence") if isinstance(detailed.get("execution_confidence"), Mapping) else {}

    dependency_metrics = dependency_layer.get("metrics") if isinstance(dependency_layer.get("metrics"), Mapping) else {}
    execution_metrics = execution_layer.get("metrics") if isinstance(execution_layer.get("metrics"), Mapping) else {}

    architecture_violation_count = int(architecture_summary.get("violation_count_total", 0) or 0)
    cycle_violation_count = int(
        (
            detailed.get("topology_validation", {}).get("metrics", {}).get("cycle_policy_violation_count", 0)
            if isinstance(detailed.get("topology_validation"), Mapping)
            else 0
        )
        or 0
    )

    duplicate_cluster_count = int(semantic_summary.get("duplicate_intent_cluster_count", 0) or 0)
    abstraction_collision_count = int(semantic_summary.get("abstraction_collision_count", 0) or 0)

    coverage_ratio = float(execution_metrics.get("coverage_ratio", 0.0) or 0.0)
    overlap_ratio = float(execution_metrics.get("overlap_ratio", 0.0) or 0.0)
    call_event_count = int(execution_metrics.get("call_event_count", 0) or 0)
    execution_confidence = float(execution_metrics.get("execution_confidence", 0.0) or 0.0)

    ast_di_divergence = float(dependency_metrics.get("ast_di_divergence_score", 0.0) or 0.0)

    semantic_pairwise_f1 = _semantic_pairwise_f1(
        clusters=semantic_report.get("clusters") if isinstance(semantic_report.get("clusters"), list) else [],
        truth_groups=truth_groups,
    )
    semantic_domain_accuracy = _semantic_domain_accuracy(
        concept_domains=semantic_report.get("concept_domains") if isinstance(semantic_report.get("concept_domains"), list) else [],
        truth_domain_map=truth_domain_map,
    )

    max_dynamic_false_positive_confidence = _max_dead_confidence_for_nodes(dead_report, dynamic_dispatch_nodes)

    structural_reasons: List[str] = []
    if architecture_violation_count > 0:
        structural_reasons.append(
            f"Detected {architecture_violation_count} architectural rule violations across layered and ownership boundaries."
        )
    if cycle_violation_count > 0:
        structural_reasons.append(
            f"Detected {cycle_violation_count} cycle-policy violations in topology validation."
        )

    semantic_reasons: List[str] = []
    if duplicate_cluster_count > 0:
        semantic_reasons.append(
            f"Detected {duplicate_cluster_count} duplicate-intent semantic clusters."
        )
    if abstraction_collision_count > 0:
        semantic_reasons.append(
            f"Detected {abstraction_collision_count} abstraction-collision concepts."
        )

    deceptive_runtime_pattern = bool(call_event_count >= 80 and coverage_ratio >= 0.70 and overlap_ratio <= 0.05)
    runtime_deception_reasons: List[str] = []
    if deceptive_runtime_pattern:
        runtime_deception_reasons.append(
            "Observed adversarial runtime signature: high coverage with very low static overlap."
        )
    if "LOW_EXECUTION_CONFIDENCE" in issue_types:
        runtime_deception_reasons.append(
            "Validation raised LOW_EXECUTION_CONFIDENCE under adversarial runtime signal."
        )

    reconciliation_reasons: List[str] = []
    if ast_di_divergence >= 0.20:
        reconciliation_reasons.append(
            f"AST/DI divergence score is {ast_di_divergence:.3f}, indicating substantial resolver disagreement."
        )
    drift_issue_detected = bool(
        "AST_DI_DIVERGENCE_ESCALATED" in issue_types
        or "AST_DI_DIVERGENCE_TRACKED" in issue_types
    )
    architecture_drift_detected = bool(
        "architecture_drift" in {str(item).strip().lower() for item in validation_failure_domains}
        or trust_breakdown.get("architecture_drift_triggered", False)
    )

    if drift_issue_detected:
        reconciliation_reasons.append(
            "Validation preserved AST/DI divergence signal (tracked or escalated) instead of reconciling falsely."
        )
    if architecture_drift_detected:
        reconciliation_reasons.append(
            "Validation promoted resolver divergence to architecture_drift first-class failure semantics."
        )

    min_architecture_violations = int(expected.get("min_architecture_violations", 1) or 1)
    min_semantic_f1 = float(expected.get("min_semantic_f1", 0.25) or 0.25)
    min_domain_accuracy = float(expected.get("min_domain_accuracy", 0.60) or 0.60)
    min_ast_di_divergence = float(expected.get("min_ast_di_divergence", 0.20) or 0.20)
    max_dead_confidence = float(expected.get("max_dead_false_positive_confidence", 0.45) or 0.45)

    structural_failure = bool(structural_reasons)
    semantic_failure = bool(semantic_reasons)
    runtime_deception_failure = deceptive_runtime_pattern and (
        "LOW_EXECUTION_CONFIDENCE" in issue_types or execution_confidence < 0.30
    )
    reconciliation_failure = bool(
        ast_di_divergence >= min_ast_di_divergence
        and drift_issue_detected
        and architecture_drift_detected
    )

    metric_exploitation_detected = bool(
        coverage_ratio >= 0.80 and overlap_ratio <= 0.05 and execution_confidence >= 0.30
    )

    require_runtime_deception = bool(expected.get("require_runtime_deception", False))
    require_semantic_redundancy = bool(expected.get("require_semantic_redundancy", False))

    architectural_ok = architecture_violation_count >= min_architecture_violations
    semantic_ok = semantic_pairwise_f1 >= min_semantic_f1 and semantic_domain_accuracy >= min_domain_accuracy
    if require_semantic_redundancy:
        semantic_ok = semantic_ok and semantic_failure

    dead_code_ok = max_dynamic_false_positive_confidence <= max_dead_confidence
    reconciliation_ok = bool(
        ast_di_divergence >= min_ast_di_divergence
        and drift_issue_detected
        and architecture_drift_detected
    )
    runtime_deception_ok = runtime_deception_failure if require_runtime_deception else True
    anti_gaming_ok = not metric_exploitation_detected

    cross_validation_ok = all(
        [
            architectural_ok,
            semantic_ok,
            dead_code_ok,
            reconciliation_ok,
            runtime_deception_ok,
            anti_gaming_ok,
        ]
    )

    pass_criteria = {
        "architectural_violations_identified": architectural_ok,
        "semantic_clustering_matches_ground_truth": semantic_ok,
        "dead_code_false_positive_threshold": dead_code_ok,
        "runtime_static_reconciliation_consistent": reconciliation_ok,
        "runtime_deception_detected": runtime_deception_ok,
        "metric_exploitation_not_detected": anti_gaming_ok,
        "cross_validation_guard": cross_validation_ok,
    }

    explanations: List[str] = []
    if not architectural_ok:
        explanations.append(
            "Structural criterion failed: expected architectural violations were not robustly detected."
        )
    if not semantic_ok:
        explanations.append(
            "Semantic criterion failed: cluster grouping/domain inference diverged from adversarial ground truth intent."
        )
    if not dead_code_ok:
        explanations.append(
            "Dead-code criterion failed: dynamic-dispatch trap was marked with excessive dead-code confidence."
        )
    if not reconciliation_ok:
        explanations.append(
            "Reconciliation criterion failed: AST/DI drift was not preserved as an explicit inconsistency signal."
        )
    if not runtime_deception_ok:
        explanations.append(
            "Runtime-deception criterion failed: adversarial high-coverage/low-overlap runtime pattern was not rejected."
        )
    if not anti_gaming_ok:
        explanations.append(
            "Anti-gaming criterion failed: coverage inflation produced acceptable execution confidence."
        )

    failure_classification = [
        {
            "type": "structural failure",
            "triggered": structural_failure,
            "why": structural_reasons,
        },
        {
            "type": "semantic failure",
            "triggered": semantic_failure,
            "why": semantic_reasons,
        },
        {
            "type": "runtime deception failure",
            "triggered": runtime_deception_failure,
            "why": runtime_deception_reasons,
        },
        {
            "type": "reconciliation failure",
            "triggered": reconciliation_failure,
            "why": reconciliation_reasons,
        },
    ]

    report = {
        "schema_version": "1.0",
        "scenario_id": scenario_id,
        "category": category,
        "adversarial_summary": {
            "summary": summary,
            "node_count": len(graph_data.get("nodes", [])) if isinstance(graph_data.get("nodes"), list) else 0,
            "edge_count": len(graph_data.get("edges", [])) if isinstance(graph_data.get("edges"), list) else 0,
            "hidden_dependency_paths": len(
                [
                    edge
                    for edge in (graph_data.get("edges") if isinstance(graph_data.get("edges"), list) else [])
                    if str((edge or {}).get("type", "")).strip().upper() == "DYNAMIC"
                ]
            ),
            "dead_code_traps": len([item for item in dynamic_dispatch_nodes if str(item).strip()]),
            "intentional_ambiguity_signals": {
                "misleading_names": bool(truth_domain_map),
                "duplicate_intent_groups": len(truth_groups),
            },
        },
        "analysis": {
            "architecture": architecture_report,
            "semantic": semantic_report,
            "causal_flow": causal_report,
            "validation": validation_result,
            "dead_code": dead_report,
            "semantic_ground_truth": {
                "pairwise_f1": round(float(semantic_pairwise_f1), 3),
                "domain_accuracy": round(float(semantic_domain_accuracy), 3),
                "truth_group_count": len(truth_groups),
                "evaluated_domain_count": len(truth_domain_map),
            },
            "metric_exploitation": {
                "detected": metric_exploitation_detected,
                "coverage_ratio": round(float(coverage_ratio), 3),
                "overlap_ratio": round(float(overlap_ratio), 3),
                "execution_confidence": round(float(execution_confidence), 3),
                "why": (
                    "Coverage increased while overlap collapsed without confidence penalty."
                    if metric_exploitation_detected
                    else "Cross-validation prevented coverage-only inflation from passing confidence gates."
                ),
            },
            "false_dead_code_guard": {
                "dynamic_dispatch_nodes": list(dynamic_dispatch_nodes),
                "max_dynamic_false_positive_confidence": round(float(max_dynamic_false_positive_confidence), 3),
                "threshold": round(float(max_dead_confidence), 3),
            },
        },
        "failure_classification": failure_classification,
        "pass_criteria": pass_criteria,
        "overall_pass": bool(all(pass_criteria.values())),
        "explanations": explanations,
    }

    report_path = tmp_path / f"{scenario_id}_truth_stress_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    report["report_path"] = str(report_path)

    return report


def _default_expected_overrides() -> Dict[str, Any]:
    return {
        "min_architecture_violations": 2,
        "min_semantic_f1": 0.25,
        "min_domain_accuracy": 0.60,
        "min_ast_di_divergence": 0.20,
        "max_dead_false_positive_confidence": 0.45,
        "require_runtime_deception": False,
        "require_semantic_redundancy": False,
    }


def test_adversarial_architecture_boundary_violations(tmp_path: Path) -> None:
    graph_data, resolver_data, entrypoints = _base_adversarial_graph()
    manifest_rows, static_rows, truth_groups, truth_domain_map = _semantic_rows_with_duplication()

    expected = _default_expected_overrides()
    expected["min_architecture_violations"] = 3

    report = _run_truth_stress_case(
        tmp_path,
        "architecture-boundary-violation",
        "architecture-boundary-violation",
        "API layer bypasses orchestration, service imports adapter directly, and hidden circular links create deceptive structural ambiguity.",
        graph_data=graph_data,
        resolver_data=resolver_data,
        entrypoints=entrypoints,
        manifest_rows=manifest_rows,
        static_rows=static_rows,
        trace_rows=_trace_checkout_flow(),
        flow_payload=_flow_payload_for_trace(),
        runtime_validation={
            "call_event_count": 180,
            "coverage_ratio": 0.58,
            "entrypoint_count": 1,
            "executed_entrypoint_count": 1,
        },
        runtime_static_reconciliation={
            "overlap_ratio": 0.22,
        },
        distribution={
            "HOT": 31,
            "WARM": 18,
            "COLD": 7,
            "DEAD": 3,
        },
        heat_payload=_dead_code_payload_with_dynamic_trap(),
        truth_groups=truth_groups,
        truth_domain_map=truth_domain_map,
        dynamic_dispatch_nodes=["canonical://function/apps/domain/dynamic_dispatch.py/dispatch_event"],
        expected=expected,
    )

    assert report["overall_pass"], json.dumps(report, indent=2)

    failure_map = {
        str(item.get("type", "")).strip().lower(): bool(item.get("triggered", False))
        for item in report.get("failure_classification", [])
        if isinstance(item, Mapping)
    }
    assert failure_map.get("structural failure", False)

    architecture_summary = report.get("analysis", {}).get("architecture", {}).get("summary", {})
    assert int(architecture_summary.get("violation_count_total", 0) or 0) >= 3


def test_adversarial_semantic_duplication_detection(tmp_path: Path) -> None:
    graph_data, resolver_data, entrypoints = _base_adversarial_graph()
    manifest_rows, static_rows, truth_groups, truth_domain_map = _semantic_rows_with_duplication()

    expected = _default_expected_overrides()
    expected["require_semantic_redundancy"] = True

    report = _run_truth_stress_case(
        tmp_path,
        "semantic-duplication",
        "semantic-duplication",
        "Conceptually identical authentication responsibilities are spread across differently named modules with overlapping but non-identical abstractions.",
        graph_data=graph_data,
        resolver_data=resolver_data,
        entrypoints=entrypoints,
        manifest_rows=manifest_rows,
        static_rows=static_rows,
        trace_rows=_trace_checkout_flow(),
        flow_payload=_flow_payload_for_trace(),
        runtime_validation={
            "call_event_count": 160,
            "coverage_ratio": 0.54,
            "entrypoint_count": 1,
            "executed_entrypoint_count": 1,
        },
        runtime_static_reconciliation={
            "overlap_ratio": 0.20,
        },
        distribution={
            "HOT": 24,
            "WARM": 16,
            "COLD": 11,
            "DEAD": 5,
        },
        heat_payload=_dead_code_payload_with_dynamic_trap(),
        truth_groups=truth_groups,
        truth_domain_map=truth_domain_map,
        dynamic_dispatch_nodes=["canonical://function/apps/domain/dynamic_dispatch.py/dispatch_event"],
        expected=expected,
    )

    assert report["overall_pass"], json.dumps(report, indent=2)

    semantic_summary = report.get("analysis", {}).get("semantic", {}).get("summary", {})
    assert int(semantic_summary.get("duplicate_intent_cluster_count", 0) or 0) >= 1


def test_adversarial_runtime_deception_detection(tmp_path: Path) -> None:
    graph_data, resolver_data, entrypoints = _base_adversarial_graph()
    manifest_rows, static_rows, truth_groups, truth_domain_map = _semantic_rows_with_duplication()

    expected = _default_expected_overrides()
    expected["require_runtime_deception"] = True

    report = _run_truth_stress_case(
        tmp_path,
        "runtime-deception",
        "runtime-deception",
        "Runtime trace executes many noisy functions with high apparent coverage but negligible overlap with architectural dependency edges.",
        graph_data=graph_data,
        resolver_data=resolver_data,
        entrypoints=entrypoints,
        manifest_rows=manifest_rows,
        static_rows=static_rows,
        trace_rows=_trace_runtime_noise(),
        flow_payload={"edges": []},
        runtime_validation={
            "call_event_count": 320,
            "coverage_ratio": 0.93,
            "entrypoint_count": 1,
            "executed_entrypoint_count": 1,
        },
        runtime_static_reconciliation={
            "overlap_ratio": 0.01,
        },
        distribution={
            "HOT": 140,
            "WARM": 90,
            "COLD": 30,
            "DEAD": 5,
        },
        heat_payload=_dead_code_payload_with_dynamic_trap(),
        truth_groups=truth_groups,
        truth_domain_map=truth_domain_map,
        dynamic_dispatch_nodes=["canonical://function/apps/domain/dynamic_dispatch.py/dispatch_event"],
        expected=expected,
    )

    assert report["overall_pass"], json.dumps(report, indent=2)

    failure_map = {
        str(item.get("type", "")).strip().lower(): bool(item.get("triggered", False))
        for item in report.get("failure_classification", [])
        if isinstance(item, Mapping)
    }
    assert failure_map.get("runtime deception failure", False)


def test_adversarial_coverage_gaming_resistance(tmp_path: Path) -> None:
    graph_data, resolver_data, entrypoints = _base_adversarial_graph()
    manifest_rows, static_rows, truth_groups, truth_domain_map = _semantic_rows_with_duplication()

    baseline_expected = _default_expected_overrides()

    baseline = _run_truth_stress_case(
        tmp_path,
        "coverage-baseline",
        "coverage-gaming-resistance",
        "Baseline execution has balanced runtime coverage and static overlap.",
        graph_data=graph_data,
        resolver_data=resolver_data,
        entrypoints=entrypoints,
        manifest_rows=manifest_rows,
        static_rows=static_rows,
        trace_rows=_trace_checkout_flow(),
        flow_payload=_flow_payload_for_trace(),
        runtime_validation={
            "call_event_count": 155,
            "coverage_ratio": 0.55,
            "entrypoint_count": 1,
            "executed_entrypoint_count": 1,
        },
        runtime_static_reconciliation={
            "overlap_ratio": 0.25,
        },
        distribution={
            "HOT": 27,
            "WARM": 21,
            "COLD": 9,
            "DEAD": 4,
        },
        heat_payload=_dead_code_payload_with_dynamic_trap(),
        truth_groups=truth_groups,
        truth_domain_map=truth_domain_map,
        dynamic_dispatch_nodes=["canonical://function/apps/domain/dynamic_dispatch.py/dispatch_event"],
        expected=baseline_expected,
    )

    gamed_expected = _default_expected_overrides()
    gamed_expected["require_runtime_deception"] = True

    gamed = _run_truth_stress_case(
        tmp_path,
        "coverage-gamed",
        "coverage-gaming-resistance",
        "Adversary inflates runtime coverage while minimizing meaningful static-overlap reconciliation.",
        graph_data=graph_data,
        resolver_data=resolver_data,
        entrypoints=entrypoints,
        manifest_rows=manifest_rows,
        static_rows=static_rows,
        trace_rows=_trace_runtime_noise(),
        flow_payload={"edges": []},
        runtime_validation={
            "call_event_count": 350,
            "coverage_ratio": 0.97,
            "entrypoint_count": 1,
            "executed_entrypoint_count": 1,
        },
        runtime_static_reconciliation={
            "overlap_ratio": 0.01,
        },
        distribution={
            "HOT": 160,
            "WARM": 120,
            "COLD": 35,
            "DEAD": 5,
        },
        heat_payload=_dead_code_payload_with_dynamic_trap(),
        truth_groups=truth_groups,
        truth_domain_map=truth_domain_map,
        dynamic_dispatch_nodes=["canonical://function/apps/domain/dynamic_dispatch.py/dispatch_event"],
        expected=gamed_expected,
    )

    assert baseline["overall_pass"], json.dumps(baseline, indent=2)
    assert gamed["overall_pass"], json.dumps(gamed, indent=2)

    baseline_exec = (
        baseline.get("analysis", {})
        .get("validation", {})
        .get("detailed_results", {})
        .get("execution_confidence", {})
        .get("metrics", {})
        .get("execution_confidence", 0.0)
    )
    gamed_exec = (
        gamed.get("analysis", {})
        .get("validation", {})
        .get("detailed_results", {})
        .get("execution_confidence", {})
        .get("metrics", {})
        .get("execution_confidence", 0.0)
    )

    baseline_coverage = (
        baseline.get("analysis", {})
        .get("validation", {})
        .get("detailed_results", {})
        .get("execution_confidence", {})
        .get("metrics", {})
        .get("coverage_ratio", 0.0)
    )
    gamed_coverage = (
        gamed.get("analysis", {})
        .get("validation", {})
        .get("detailed_results", {})
        .get("execution_confidence", {})
        .get("metrics", {})
        .get("coverage_ratio", 0.0)
    )

    assert float(gamed_coverage) > float(baseline_coverage)
    assert float(gamed_exec) < float(baseline_exec)

    assert not bool(baseline.get("analysis", {}).get("metric_exploitation", {}).get("detected", True))
    assert not bool(gamed.get("analysis", {}).get("metric_exploitation", {}).get("detected", True))


def test_adversarial_false_dead_code_dynamic_dispatch_guard(tmp_path: Path) -> None:
    graph_data, resolver_data, entrypoints = _base_adversarial_graph()
    manifest_rows, static_rows, truth_groups, truth_domain_map = _semantic_rows_with_duplication()

    expected = _default_expected_overrides()
    expected["max_dead_false_positive_confidence"] = 0.45

    dynamic_node = "canonical://function/apps/domain/dynamic_dispatch.py/dispatch_event"

    report = _run_truth_stress_case(
        tmp_path,
        "false-dead-code",
        "false-dead-code",
        "Dynamic dispatch keeps logic live without straightforward call edges, creating a dead-code false-positive trap.",
        graph_data=graph_data,
        resolver_data=resolver_data,
        entrypoints=entrypoints,
        manifest_rows=manifest_rows,
        static_rows=static_rows,
        trace_rows=_trace_checkout_flow(),
        flow_payload=_flow_payload_for_trace(),
        runtime_validation={
            "call_event_count": 172,
            "coverage_ratio": 0.61,
            "entrypoint_count": 1,
            "executed_entrypoint_count": 1,
        },
        runtime_static_reconciliation={
            "overlap_ratio": 0.23,
        },
        distribution={
            "HOT": 30,
            "WARM": 20,
            "COLD": 10,
            "DEAD": 4,
        },
        heat_payload=_dead_code_payload_with_dynamic_trap(),
        truth_groups=truth_groups,
        truth_domain_map=truth_domain_map,
        dynamic_dispatch_nodes=[dynamic_node],
        expected=expected,
    )

    assert report["overall_pass"], json.dumps(report, indent=2)

    false_dead_guard = report.get("analysis", {}).get("false_dead_code_guard", {})
    assert float(false_dead_guard.get("max_dynamic_false_positive_confidence", 1.0)) <= 0.45


def test_adversarial_intent_model_validation_with_misleading_names(tmp_path: Path) -> None:
    graph_data, resolver_data, entrypoints = _base_adversarial_graph()
    manifest_rows, static_rows, truth_groups, truth_domain_map = _semantic_rows_with_misleading_naming()

    expected = _default_expected_overrides()
    expected["min_domain_accuracy"] = 0.80

    report = _run_truth_stress_case(
        tmp_path,
        "intent-model-misleading-names",
        "intent-model-validation",
        "Modules intentionally use misleading auth-oriented naming while implementing persistence and billing responsibilities.",
        graph_data=graph_data,
        resolver_data=resolver_data,
        entrypoints=entrypoints,
        manifest_rows=manifest_rows,
        static_rows=static_rows,
        trace_rows=_trace_checkout_flow(),
        flow_payload=_flow_payload_for_trace(),
        runtime_validation={
            "call_event_count": 165,
            "coverage_ratio": 0.57,
            "entrypoint_count": 1,
            "executed_entrypoint_count": 1,
        },
        runtime_static_reconciliation={
            "overlap_ratio": 0.21,
        },
        distribution={
            "HOT": 26,
            "WARM": 21,
            "COLD": 9,
            "DEAD": 4,
        },
        heat_payload=_dead_code_payload_with_dynamic_trap(),
        truth_groups=truth_groups,
        truth_domain_map=truth_domain_map,
        dynamic_dispatch_nodes=["canonical://function/apps/domain/dynamic_dispatch.py/dispatch_event"],
        expected=expected,
    )

    assert report["overall_pass"], json.dumps(report, indent=2)

    semantic_ground_truth = report.get("analysis", {}).get("semantic_ground_truth", {})
    assert float(semantic_ground_truth.get("domain_accuracy", 0.0) or 0.0) >= 0.80


def test_adversarial_stability_under_permuted_inputs(tmp_path: Path) -> None:
    graph_data, resolver_data, entrypoints = _base_adversarial_graph()
    manifest_rows, static_rows, truth_groups, truth_domain_map = _semantic_rows_with_duplication()

    expected = _default_expected_overrides()

    canonical_report = _run_truth_stress_case(
        tmp_path,
        "stability-canonical",
        "stability",
        "Canonical ordering for adversarial scenario evaluation.",
        graph_data=graph_data,
        resolver_data=resolver_data,
        entrypoints=entrypoints,
        manifest_rows=manifest_rows,
        static_rows=static_rows,
        trace_rows=_trace_checkout_flow(),
        flow_payload=_flow_payload_for_trace(),
        runtime_validation={
            "call_event_count": 170,
            "coverage_ratio": 0.59,
            "entrypoint_count": 1,
            "executed_entrypoint_count": 1,
        },
        runtime_static_reconciliation={
            "overlap_ratio": 0.24,
        },
        distribution={
            "HOT": 29,
            "WARM": 22,
            "COLD": 8,
            "DEAD": 4,
        },
        heat_payload=_dead_code_payload_with_dynamic_trap(),
        truth_groups=truth_groups,
        truth_domain_map=truth_domain_map,
        dynamic_dispatch_nodes=["canonical://function/apps/domain/dynamic_dispatch.py/dispatch_event"],
        expected=expected,
    )

    permuted_graph = {
        "nodes": list(reversed(graph_data.get("nodes", []))),
        "edges": list(reversed(graph_data.get("edges", []))),
    }
    permuted_resolver = {
        "edges": list(reversed(resolver_data.get("edges", []))),
    }

    permuted_report = _run_truth_stress_case(
        tmp_path,
        "stability-permuted",
        "stability",
        "Permutation ordering for adversarial scenario evaluation.",
        graph_data=permuted_graph,
        resolver_data=permuted_resolver,
        entrypoints=list(reversed(entrypoints)),
        manifest_rows=list(reversed(manifest_rows)),
        static_rows=list(reversed(static_rows)),
        trace_rows=list(reversed(_trace_checkout_flow())),
        flow_payload={"edges": list(reversed(_flow_payload_for_trace().get("edges", [])) )},
        runtime_validation={
            "call_event_count": 170,
            "coverage_ratio": 0.59,
            "entrypoint_count": 1,
            "executed_entrypoint_count": 1,
        },
        runtime_static_reconciliation={
            "overlap_ratio": 0.24,
        },
        distribution={
            "HOT": 29,
            "WARM": 22,
            "COLD": 8,
            "DEAD": 4,
        },
        heat_payload=_dead_code_payload_with_dynamic_trap(),
        truth_groups=truth_groups,
        truth_domain_map=truth_domain_map,
        dynamic_dispatch_nodes=["canonical://function/apps/domain/dynamic_dispatch.py/dispatch_event"],
        expected=expected,
    )

    assert canonical_report["overall_pass"], json.dumps(canonical_report, indent=2)
    assert permuted_report["overall_pass"], json.dumps(permuted_report, indent=2)

    canonical_failure_flags = {
        str(item.get("type", "")).strip().lower(): bool(item.get("triggered", False))
        for item in canonical_report.get("failure_classification", [])
        if isinstance(item, Mapping)
    }
    permuted_failure_flags = {
        str(item.get("type", "")).strip().lower(): bool(item.get("triggered", False))
        for item in permuted_report.get("failure_classification", [])
        if isinstance(item, Mapping)
    }

    assert canonical_failure_flags == permuted_failure_flags

    canonical_trust = float(canonical_report.get("analysis", {}).get("validation", {}).get("trust_score", 0.0) or 0.0)
    permuted_trust = float(permuted_report.get("analysis", {}).get("validation", {}).get("trust_score", 0.0) or 0.0)
    assert canonical_trust == pytest.approx(permuted_trust, abs=1e-9)
