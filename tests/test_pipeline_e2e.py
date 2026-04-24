from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Configurable repo path requested by the harness requirements.
TARGET_REPO_PATH = Path(os.environ.get("PIPELINE_REPO_PATH", str(PROJECT_ROOT))).resolve()

# Configurable CLI path; defaults to root cli.py, then repo_audit_engine/cli.py.
CLI_PATH_OVERRIDE = os.environ.get("PIPELINE_CLI_PATH")
if CLI_PATH_OVERRIDE:
    PIPELINE_CLI_PATH = Path(CLI_PATH_OVERRIDE).resolve()
else:
    default_candidates = [
        PROJECT_ROOT / "cli.py",
        PROJECT_ROOT / "repo_audit_engine" / "cli.py",
    ]
    PIPELINE_CLI_PATH = next((path for path in default_candidates if path.exists()), default_candidates[0])

HARNESS_OUTPUT_DIR = Path(
    os.environ.get(
        "PIPELINE_TEST_OUTPUT_DIR",
        str(PROJECT_ROOT / "output" / "test_harness"),
    )
).resolve()

FORBIDDEN_LEGACY_KEYS = {"orphan_nodes", "disconnected_clusters"}
TIMESTAMP_KEY_PATTERN = re.compile(r"(timestamp|time|_at|date|utc|generated)", re.IGNORECASE)
GENERIC_ROOT_CAUSES = {
    "validation failed",
    "failed",
    "error",
    "unknown error",
    "pipeline failed",
}
ACTIONABLE_VERBS = {
    "fix",
    "review",
    "add",
    "remove",
    "update",
    "align",
    "reconcile",
    "connect",
    "mark",
    "ensure",
    "refactor",
}


@dataclass
class PipelineRunResult:
    label: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    runtime_seconds: float
    output_path: Path
    payload: dict[str, Any] | None


_CHECKS: dict[str, bool] = {}
_BLOCKING_ISSUES: list[str] = []
_BASELINE_RUN: PipelineRunResult | None = None
_DETERMINISM_RUNS: list[PipelineRunResult] = []


def _add_blocker(message: str) -> None:
    if message not in _BLOCKING_ISSUES:
        _BLOCKING_ISSUES.append(message)


def _record_check(name: str, passed: bool, blocker: str | None = None) -> None:
    _CHECKS[name] = bool(passed)
    if not passed and blocker:
        _add_blocker(blocker)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _pipeline_command(output_path: Path) -> list[str]:
    return [
        sys.executable,
        str(PIPELINE_CLI_PATH),
        "run-pipeline",
        "--repo",
        str(TARGET_REPO_PATH),
        "--output",
        str(output_path),
    ]


def _run_pipeline(label: str) -> PipelineRunResult:
    HARNESS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = HARNESS_OUTPUT_DIR / f"{label}.json"
    if output_path.exists():
        output_path.unlink()

    command = _pipeline_command(output_path)
    started = perf_counter()
    completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    runtime_seconds = perf_counter() - started

    payload: dict[str, Any] | None = None
    if output_path.exists():
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = None

    return PipelineRunResult(
        label=label,
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        runtime_seconds=runtime_seconds,
        output_path=output_path,
        payload=payload,
    )


def _get_baseline_run() -> PipelineRunResult:
    global _BASELINE_RUN
    if _BASELINE_RUN is None:
        _BASELINE_RUN = _run_pipeline("basic_execution")
    return _BASELINE_RUN


def _get_determinism_runs() -> list[PipelineRunResult]:
    global _DETERMINISM_RUNS
    if not _DETERMINISM_RUNS:
        _DETERMINISM_RUNS = [_run_pipeline(f"determinism_run_{index}") for index in range(1, 4)]
    return _DETERMINISM_RUNS


def _require_payload(run: PipelineRunResult) -> dict[str, Any]:
    if not run.output_path.exists():
        message = (
            f"Expected output JSON file was not created: {run.output_path}. "
            f"stderr={run.stderr.strip()}"
        )
        _add_blocker(message)
        pytest.fail(message)

    if run.payload is None:
        message = f"Output file exists but is not valid JSON: {run.output_path}"
        _add_blocker(message)
        pytest.fail(message)

    return run.payload


def _find_key_paths(payload: Any, target_keys: set[str], path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            next_path = f"{path}.{key}" if path else key
            if key in target_keys:
                found.append(next_path)
            found.extend(_find_key_paths(value, target_keys, next_path))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            next_path = f"{path}[{index}]"
            found.extend(_find_key_paths(value, target_keys, next_path))
    return found


def _find_raw_metric_only_blocks(payload: Any) -> list[str]:
    findings: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                next_path = f"{path}.{key}" if path else key
                if (
                    key.lower() in {"metrics", "counts", "stats"}
                    and isinstance(value, dict)
                    and value
                    and all(_is_number(inner_value) for inner_value in value.values())
                ):
                    findings.append(next_path)
                walk(value, next_path)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(payload, "")
    return findings


def _strip_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_timestamps(inner_value)
            for key, inner_value in value.items()
            if not TIMESTAMP_KEY_PATTERN.search(key)
        }
    if isinstance(value, list):
        return [_strip_timestamps(item) for item in value]
    return value


def _normalized_payload(payload: dict[str, Any]) -> str:
    normalized = _strip_timestamps(payload)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))


def _issues_are_ranked(top_issues: Any) -> bool:
    if not isinstance(top_issues, list) or not top_issues:
        return False
    for issue in top_issues:
        if not isinstance(issue, dict):
            return False
        if "rank" not in issue:
            return False
    return True


def _has_actionable_guidance(actions: Any) -> bool:
    if not isinstance(actions, list) or not actions:
        return False
    for action in actions:
        if not isinstance(action, str):
            continue
        text = action.strip().lower()
        if not text:
            continue
        if any(verb in text for verb in ACTIONABLE_VERBS):
            return True
    return False


def test_1_basic_execution() -> None:
    run = _get_baseline_run()

    exit_ok = run.returncode == 0
    output_file_exists = run.output_path.exists()
    passed = exit_ok and output_file_exists

    blocker = None
    if not exit_ok:
        blocker = (
            "Pipeline command failed. "
            f"command={' '.join(run.command)} stderr={run.stderr.strip()}"
        )
    elif not output_file_exists:
        blocker = f"Pipeline did not create expected output JSON file: {run.output_path}"

    _record_check("basic_execution", passed, blocker)

    assert exit_ok, f"Pipeline exited with non-zero code {run.returncode}. stderr={run.stderr.strip()}"
    assert output_file_exists, f"Expected output JSON file is missing: {run.output_path}"


def test_2_output_contract_validation() -> None:
    run = _get_baseline_run()
    payload = _require_payload(run)

    summary = payload.get("summary")
    diagnostics = payload.get("diagnostics")
    trust = payload.get("trust")

    conditions = {
        "summary_present": isinstance(summary, dict),
        "diagnostics_present": isinstance(diagnostics, dict),
        "trust_present": isinstance(trust, dict),
        "summary_status": isinstance((summary or {}).get("status"), str),
        "summary_root_cause": isinstance((summary or {}).get("root_cause"), str)
        and bool((summary or {}).get("root_cause", "").strip()),
        "summary_confidence": _is_number((summary or {}).get("confidence")),
        "diagnostics_top_issues": isinstance((diagnostics or {}).get("top_issues"), list),
        "diagnostics_failure_domains": isinstance((diagnostics or {}).get("failure_domains"), list),
        "trust_score": _is_number((trust or {}).get("score")),
        "trust_breakdown": isinstance((trust or {}).get("breakdown"), dict),
    }

    passed = all(conditions.values())
    failed_conditions = [name for name, is_ok in conditions.items() if not is_ok]

    blocker = None
    if not passed:
        blocker = f"Output contract validation failed for: {', '.join(failed_conditions)}"

    _record_check("output_contract", passed, blocker)

    assert passed, blocker


def test_3_no_legacy_fields() -> None:
    run = _get_baseline_run()
    payload = _require_payload(run)

    forbidden_paths = _find_key_paths(payload, FORBIDDEN_LEGACY_KEYS)

    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
    diagnostics = payload.get("diagnostics", {}) if isinstance(payload.get("diagnostics"), dict) else {}
    interpreted = bool(str(summary.get("root_cause", "")).strip()) and bool(diagnostics.get("top_issues"))

    raw_metric_blocks = _find_raw_metric_only_blocks(payload)
    raw_metric_without_interpretation = bool(raw_metric_blocks) and not interpreted

    passed = not forbidden_paths and not raw_metric_without_interpretation
    blocker_parts: list[str] = []
    if forbidden_paths:
        blocker_parts.append(f"Forbidden legacy fields found at: {forbidden_paths}")
    if raw_metric_without_interpretation:
        blocker_parts.append(
            "Raw metric-only fields detected without diagnostic interpretation at: "
            f"{raw_metric_blocks}"
        )

    blocker = "; ".join(blocker_parts) if blocker_parts else None
    _record_check("no_legacy_fields", passed, blocker)

    assert not forbidden_paths, f"Forbidden legacy fields found at: {forbidden_paths}"
    assert not raw_metric_without_interpretation, (
        "Raw metric-only fields found without interpretation: "
        f"{raw_metric_blocks}"
    )


def test_4_determinism_check() -> None:
    runs = _get_determinism_runs()

    all_success = all(run.returncode == 0 and run.payload is not None for run in runs)
    if not all_success:
        failures = [
            {
                "label": run.label,
                "returncode": run.returncode,
                "output_exists": run.output_path.exists(),
                "stderr": run.stderr.strip(),
            }
            for run in runs
            if run.returncode != 0 or run.payload is None
        ]
        _record_check("determinism", False, f"Determinism runs did not complete successfully: {failures}")
        pytest.fail(f"Determinism runs failed: {failures}")

    payloads = [run.payload for run in runs if run.payload is not None]
    trust_scores = [payload.get("trust", {}).get("score") for payload in payloads]
    failure_domains = [payload.get("diagnostics", {}).get("failure_domains") for payload in payloads]
    root_causes = [payload.get("summary", {}).get("root_cause") for payload in payloads]
    normalized_payloads = [_normalized_payload(payload) for payload in payloads]

    same_trust = all(score == trust_scores[0] for score in trust_scores[1:])
    same_domains = all(domains == failure_domains[0] for domains in failure_domains[1:])
    same_root_cause = all(root == root_causes[0] for root in root_causes[1:])
    same_normalized_output = all(value == normalized_payloads[0] for value in normalized_payloads[1:])

    deterministic = same_trust and same_domains and same_root_cause and same_normalized_output

    blocker = None
    if not deterministic:
        blocker = (
            "Determinism violation detected across three runs. "
            f"trust_scores={trust_scores} failure_domains={failure_domains} root_causes={root_causes}"
        )

    _record_check("determinism", deterministic, blocker)

    assert same_trust, f"trust.score changed across runs: {trust_scores}"
    assert same_domains, f"diagnostics.failure_domains changed across runs: {failure_domains}"
    assert same_root_cause, f"summary.root_cause changed across runs: {root_causes}"
    assert same_normalized_output, "Output changed across runs in non-timestamp fields."


def test_5_signal_quality_check() -> None:
    run = _get_baseline_run()
    payload = _require_payload(run)

    looks_like_stage_failure_only = (
        isinstance(payload, dict)
        and set(payload.keys()) <= {"status", "message", "failed_stage"}
        and "failed_stage" in payload
    )

    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
    diagnostics = payload.get("diagnostics", {}) if isinstance(payload.get("diagnostics"), dict) else {}

    root_cause = str(summary.get("root_cause", "")).strip()
    top_issues = diagnostics.get("top_issues")
    actions = diagnostics.get("recommended_actions")

    has_explanation = bool(root_cause)
    has_ranked_issues = _issues_are_ranked(top_issues)
    has_actions = isinstance(actions, list) and bool(actions)

    passed = not looks_like_stage_failure_only and has_explanation and has_ranked_issues and has_actions
    blocker = None
    if not passed:
        blocker = (
            "Diagnostics signal quality is insufficient. "
            f"stage_only={looks_like_stage_failure_only} has_explanation={has_explanation} "
            f"has_ranked_issues={has_ranked_issues} has_actions={has_actions}"
        )

    _record_check("signal_quality", passed, blocker)

    assert not looks_like_stage_failure_only, "Output collapsed to stage-level failure only payload."
    assert has_explanation, "No explanation of why failure/degradation happened was found."
    assert has_ranked_issues, "Diagnostics top_issues are missing ranking information."
    assert has_actions, "No recommended_actions were produced."


def test_6_trust_score_sanity() -> None:
    runs = _get_determinism_runs()
    payloads = [run.payload for run in runs if run.payload is not None]

    if len(payloads) != 3:
        _record_check("trust_sanity", False, "Trust score sanity check could not run on three valid outputs.")
        pytest.fail("Trust score sanity check requires three successful outputs.")

    scores: list[float] = []
    for payload in payloads:
        trust = payload.get("trust", {}) if isinstance(payload.get("trust"), dict) else {}
        score = trust.get("score")
        if not _is_number(score):
            _record_check("trust_sanity", False, "trust.score is missing or non-numeric.")
            pytest.fail(f"trust.score must be numeric; found {score!r}")
        scores.append(float(score))

    all_boundary_scores = all(score in (0.0, 1.0) for score in scores)

    first_breakdown = (
        payloads[0].get("trust", {}).get("breakdown")
        if isinstance(payloads[0].get("trust"), dict)
        else None
    )
    has_breakdown = isinstance(first_breakdown, dict) and bool(first_breakdown)

    has_per_domain_contribution = False
    if has_breakdown:
        for key in ("weighted_contributions", "domain_scores", "scores"):
            block = first_breakdown.get(key)
            if isinstance(block, dict) and len(block) >= 2 and any(_is_number(value) for value in block.values()):
                has_per_domain_contribution = True
                break

    passed = (not all_boundary_scores) and has_breakdown and has_per_domain_contribution
    blocker = None
    if not passed:
        blocker = (
            "Trust model sanity check failed. "
            f"scores={scores} has_breakdown={has_breakdown} "
            f"has_per_domain_contribution={has_per_domain_contribution}"
        )

    _record_check("trust_sanity", passed, blocker)

    assert not all_boundary_scores, f"trust.score stayed at only boundary values (0.0/1.0): {scores}"
    assert has_breakdown, "trust.breakdown is missing or empty."
    assert has_per_domain_contribution, "trust.breakdown does not show per-domain contribution details."


def test_7_performance_check() -> None:
    runs = _get_determinism_runs()
    total_runtime = float(sum(run.runtime_seconds for run in runs))

    print(json.dumps({"runtime_seconds": round(total_runtime, 3)}, sort_keys=True))

    if total_runtime > 120.0:
        warnings.warn(
            f"Runtime warning: total runtime exceeded 120 seconds ({total_runtime:.3f}s)",
            RuntimeWarning,
        )

    _record_check("performance", True)


def test_8_failure_explainability() -> None:
    run = _get_baseline_run()
    payload = _require_payload(run)

    summary = payload.get("summary", {}) if isinstance(payload.get("summary"), dict) else {}
    diagnostics = payload.get("diagnostics", {}) if isinstance(payload.get("diagnostics"), dict) else {}

    root_cause = str(summary.get("root_cause", "")).strip()
    top_issues = diagnostics.get("top_issues") if isinstance(diagnostics.get("top_issues"), list) else []
    top_issue = top_issues[0] if top_issues else None
    actions = diagnostics.get("recommended_actions")

    generic_root = root_cause.lower() in GENERIC_ROOT_CAUSES or root_cause.lower().startswith("validation failed")
    has_top_issue = isinstance(top_issue, dict) and bool(
        str(top_issue.get("message") or top_issue.get("reason") or "").strip()
    )
    actionable = _has_actionable_guidance(actions)

    passed = (not generic_root) and has_top_issue and actionable
    blocker = None
    if not passed:
        blocker = (
            "Failure explainability is insufficient. "
            f"generic_root={generic_root} has_top_issue={has_top_issue} actionable={actionable}"
        )

    _record_check("failure_explainability", passed, blocker)

    assert not generic_root, f"root_cause is generic and non-actionable: {root_cause!r}"
    assert has_top_issue, "top_issues[0] is missing or has no meaningful message/reason."
    assert actionable, "recommended_actions are missing actionable guidance."


def test_9_print_final_structured_report() -> None:
    pipeline_valid = all(
        _CHECKS.get(name, False)
        for name in (
            "basic_execution",
            "output_contract",
            "no_legacy_fields",
            "signal_quality",
            "failure_explainability",
        )
    )
    deterministic = _CHECKS.get("determinism", False)
    trust_model_valid = _CHECKS.get("trust_sanity", False)

    diagnostics_points = sum(
        int(_CHECKS.get(name, False))
        for name in (
            "output_contract",
            "no_legacy_fields",
            "signal_quality",
            "failure_explainability",
        )
    )
    if diagnostics_points >= 4:
        diagnostics_quality = "HIGH"
    elif diagnostics_points >= 2:
        diagnostics_quality = "MEDIUM"
    else:
        diagnostics_quality = "LOW"

    ready_for_powershell_removal = (
        pipeline_valid
        and deterministic
        and trust_model_valid
        and len(_BLOCKING_ISSUES) == 0
    )

    report = {
        "pipeline_valid": pipeline_valid,
        "deterministic": deterministic,
        "diagnostics_quality": diagnostics_quality,
        "trust_model_valid": trust_model_valid,
        "ready_for_powershell_removal": ready_for_powershell_removal,
        "blocking_issues": _BLOCKING_ISSUES,
    }

    print(json.dumps(report, indent=2, sort_keys=True))
