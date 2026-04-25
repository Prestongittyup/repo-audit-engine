from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = PROJECT_ROOT / "tools" / "repo_structure_audit.py"


def _run_audit(tmp_path: Path) -> Dict[str, Any]:
    output_json = tmp_path / "repo_audit_report.json"
    output_md = tmp_path / "repo_audit_report.md"

    command = [
        sys.executable,
        str(AUDIT_SCRIPT),
        "--repo",
        str(PROJECT_ROOT),
        "--output-json",
        str(output_json),
        "--output-md",
        str(output_md),
        "--bubble-mode",
        "true",
    ]

    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, (
        "Repository audit script failed.\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )

    assert output_json.exists(), "Expected JSON audit report was not created."
    assert output_md.exists(), "Expected markdown audit report was not created."

    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_e2e_pipeline_audit_contract(tmp_path: Path) -> None:
    report = _run_audit(tmp_path)

    required_top_level = {
        "structure_audit",
        "pipeline_execution_health",
        "runtime_validation",
        "truth_validation_layer",
        "determinism_check",
        "system_integrity_summary",
    }
    assert required_top_level.issubset(set(report.keys()))

    pipeline = report.get("pipeline_execution_health", {})
    assert bool(pipeline.get("pipeline_success", False)), "Pipeline did not complete all expected stages."
    assert bool(pipeline.get("stage_order_ok", False)), "Pipeline stage order deviated from expected deterministic sequence."

    artifact_presence = pipeline.get("artifact_presence", {})
    assert isinstance(artifact_presence, dict) and artifact_presence, "artifact_presence block is missing or empty."
    missing_artifacts = [name for name, present in artifact_presence.items() if not bool(present)]
    assert not missing_artifacts, f"Missing expected artifacts: {missing_artifacts}"

    schema_validation = pipeline.get("schema_validation", {})
    assert isinstance(schema_validation, dict) and schema_validation, "schema_validation block is missing or empty."
    invalid_schemas = [name for name, passed in schema_validation.items() if not bool(passed)]
    assert not invalid_schemas, f"Schema validation failed: {invalid_schemas}"

    assert bool(pipeline.get("no_null_critical_sections", False)), "Critical contract sections contain null or missing values."

    runtime = report.get("runtime_validation", {})
    assert bool(runtime.get("bubble_mode_executed", False)), "Bubble runtime mode was not executed."
    assert bool(runtime.get("runtime_event_stream_present", False)), "Runtime trace JSONL stream is empty."
    assert bool(runtime.get("execution_graph_generated", False)), "Execution flow graph JSON was not generated."
    assert int(runtime.get("runtime_event_count", 0) or 0) > 0, "Runtime event count should be positive when bubble mode is enabled."

    traced_entrypoints = runtime.get("traced_entrypoints", [])
    assert isinstance(traced_entrypoints, list) and traced_entrypoints, "No traced entrypoints recorded for runtime validation."

    truth = report.get("truth_validation_layer", {})
    assert isinstance(truth, dict) and truth, "truth_validation_layer block is missing or empty."

    required_truth_sections = {
        "passed",
        "thresholds",
        "runtime_meaningfulness",
        "runtime_static_reconciliation",
        "graph_sanity",
        "classification_quality",
        "critical_issues",
        "warnings",
    }
    assert required_truth_sections.issubset(set(truth.keys()))

    thresholds = truth.get("thresholds", {})
    assert isinstance(thresholds, dict) and thresholds, "Truth-validation thresholds are missing."
    for key in {
        "min_modules_executed",
        "min_local_modules_executed",
        "min_unique_functions_called",
        "min_unique_local_functions_called",
        "min_call_depth",
        "min_reachable_node_ratio",
        "max_isolated_node_ratio",
        "min_runtime_confirmed_edge_ratio",
    }:
        assert key in thresholds, f"Missing truth-validation threshold: {key}"

    runtime_truth = truth.get("runtime_meaningfulness", {})
    assert isinstance(runtime_truth, dict), "runtime_meaningfulness block is missing."
    assert int(runtime_truth.get("modules_executed", 0) or 0) >= 0
    assert int(runtime_truth.get("local_modules_executed", 0) or 0) >= 0
    assert int(runtime_truth.get("unique_functions_called", 0) or 0) >= 0
    assert int(runtime_truth.get("unique_local_functions_called", 0) or 0) >= 0
    assert int(runtime_truth.get("max_call_depth", 0) or 0) >= 0

    low_info_reasons = runtime_truth.get("low_information_reasons", [])
    assert isinstance(low_info_reasons, list), "low_information_reasons must be a list."
    if not bool(runtime_truth.get("passed", False)):
        assert low_info_reasons, "Failed runtime meaningfulness must include low-information reasons."

    reconciliation = truth.get("runtime_static_reconciliation", {})
    assert isinstance(reconciliation, dict), "runtime_static_reconciliation block is missing."
    static_edge_count = int(reconciliation.get("static_edge_count", 0) or 0)
    runtime_edge_count = int(reconciliation.get("runtime_edge_count", 0) or 0)
    shared_edge_count = int(reconciliation.get("shared_edge_count", 0) or 0)
    static_only_edge_count = int(reconciliation.get("static_only_edge_count", 0) or 0)
    runtime_only_edge_count = int(reconciliation.get("runtime_only_edge_count", 0) or 0)
    assert shared_edge_count + static_only_edge_count == static_edge_count
    assert shared_edge_count + runtime_only_edge_count == runtime_edge_count

    graph_sanity = truth.get("graph_sanity", {})
    assert isinstance(graph_sanity, dict), "graph_sanity block is missing."
    reachable_ratio = float(graph_sanity.get("reachable_node_ratio", 0.0) or 0.0)
    isolated_ratio = float(graph_sanity.get("isolated_node_ratio", 0.0) or 0.0)
    assert 0.0 <= reachable_ratio <= 1.0
    assert 0.0 <= isolated_ratio <= 1.0

    classification = truth.get("classification_quality", {})
    assert isinstance(classification, dict), "classification_quality block is missing."
    for key in ["dead_referenced_nodes_sample", "warm_unreachable_nodes_sample", "issues"]:
        assert isinstance(classification.get(key, []), list), f"classification_quality.{key} must be a list"

    summary_block = report.get("system_integrity_summary", {})
    critical_issues = summary_block.get("critical_issues", [])
    if not bool(truth.get("passed", False)):
        assert isinstance(critical_issues, list) and critical_issues, (
            "Failed truth validation must propagate into system_integrity_summary.critical_issues."
        )

    determinism = report.get("determinism_check", {})
    run1_hash = str(determinism.get("run1_hash", "")).strip()
    run2_hash = str(determinism.get("run2_hash", "")).strip()
    run1_semantic_hash = str(determinism.get("run1_semantic_hash", "")).strip()
    run2_semantic_hash = str(determinism.get("run2_semantic_hash", "")).strip()
    assert run1_hash and run2_hash, "Determinism hashes are missing."
    assert run1_semantic_hash and run2_semantic_hash, "Semantic determinism hashes are missing."

    deterministic = bool(determinism.get("deterministic", False))
    differences = determinism.get("differences", [])
    semantic_differences = determinism.get("semantic_differences", [])
    semantic_deterministic = bool(determinism.get("semantic_deterministic", False))
    assert deterministic or bool(differences) or bool(semantic_differences), (
        "Determinism check failed without providing explicit mismatch details."
    )
    assert semantic_deterministic or bool(semantic_differences), (
        "Semantic determinism check failed without providing semantic mismatch details."
    )
