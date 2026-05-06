from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence

from repo_audit_engine.analysis.semantic_clusters import build_semantic_cluster_report
from repo_audit_engine.architecture.constraints import build_architecture_constraint_report
from repo_audit_engine.analysis.static_analyzer import run_static_analysis
from repo_audit_engine.classification.dead_code import build_dead_code_report_from_artifact
from repo_audit_engine.classification.heat_engine import classify_code_heat_from_artifacts
from repo_audit_engine.diagnostics.reporter import run_diagnostics_from_artifacts
from repo_audit_engine.graph.graph_builder import build_dependency_graph
from repo_audit_engine.io.artifacts import append_stage_event, build_final_report, write_json
from repo_audit_engine.manifest.builder import build_manifest
from repo_audit_engine.pipeline.stages import mode_to_stages
from repo_audit_engine.pipeline.validation import run_verification
from repo_audit_engine.runtime.bubble_executor import execute_runtime_bubble
from repo_audit_engine.runtime.causal_flow import build_causal_flow_report
from repo_audit_engine.runtime.scenario_runner import build_runtime_scenario_plan


class PipelineExecutionError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(f"[{stage}] {message}")
        self.stage = stage


def _apply_diagnostics_to_trust(
    trust_payload: Dict[str, Any],
    diagnostics_payload: Dict[str, Any],
) -> Dict[str, Any]:
    # Diagnostics are explanatory only. They annotate trust context and never alter score.
    base_score = float(trust_payload.get("score", 0.0) or 0.0)
    breakdown = trust_payload.get("breakdown")
    trust_breakdown = dict(breakdown) if isinstance(breakdown, dict) else {}

    root_causes = diagnostics_payload.get("root_causes")
    causes = root_causes if isinstance(root_causes, list) else []
    severity_values = [
        float(item.get("severity", 0.0) or 0.0)
        for item in causes
        if isinstance(item, dict)
    ]

    trust_breakdown["diagnostic_context"] = {
        "max_root_cause_severity": round(max(severity_values) if severity_values else 0.0, 3),
        "diagnostics_status": str(diagnostics_payload.get("status", "UNKNOWN")),
        "score_adjustment_applied": False,
    }

    return {
        "score": round(max(0.0, min(1.0, base_score)), 3),
        "breakdown": trust_breakdown,
    }


def _stage_failure_payload(error: PipelineExecutionError) -> Dict[str, Any]:
    message = str(error)

    return {
        "summary": {
            "status": "FAILED",
            "root_cause": message,
            "confidence": 0.0,
        },
        "diagnostics": {
            "status": "FAIL",
            "root_causes": [
                {
                    "id": f"rc-01-{error.stage.lower()}",
                    "type": "STRUCTURAL",
                    "severity": 1.0,
                    "confidence": 1.0,
                    "description": message,
                    "affected_nodes": [],
                    "evidence": [
                        f"pipeline_stage={error.stage}",
                        "diagnostic_synthesis=stage_failure",
                    ],
                    "propagation_path": [
                        "Manifest build",
                        "Static analysis",
                        "Pipeline termination",
                    ],
                }
            ],
            "causal_chain": [
                {
                    "step": 1,
                    "cause": f"Stage '{error.stage}' failed deterministically.",
                    "effect": "Pipeline report is incomplete and marked FAILED.",
                }
            ],
            "system_health": {
                "structural_health": 0.0,
                "graph_connectivity": 0.0,
                "dependency_integrity": 0.0,
                "semantic_consistency": 0.0,
            },
            "summary": {
                "primary_failure_mode": message,
                "secondary_failure_modes": [],
                "stability_class": "BROKEN",
            },
            "top_issues": [
                {
                    "rank": 1,
                    "type": f"stage_failure:{error.stage}",
                    "domain": "pipeline_orchestration",
                    "severity": "HIGH",
                    "impact_score": 1.0,
                    "message": message,
                    "sample_nodes": [],
                }
            ],
            "failure_domains": ["pipeline_orchestration"],
            "recommended_actions": [
                f"Fix deterministic failure in stage '{error.stage}'.",
                "Re-run the staged pipeline after remediation.",
            ],
            "validation_sections": {
                "structural_validation": {"status": "FAIL", "score": 0.0, "summary": "Not executed due to stage failure."},
                "resolver_consistency": {"status": "FAIL", "score": 0.0, "summary": "Not executed due to stage failure."},
                "reachability_analysis": {"status": "FAIL", "score": 0.0, "summary": "Not executed due to stage failure."},
                "semantic_validation": {"status": "FAIL", "score": 0.0, "summary": "Not executed due to stage failure."},
            },
        },
        "trust": {
            "score": 0.0,
            "breakdown": {
                "reason": "Pipeline terminated before trust evaluation.",
            },
        },
        "artifacts": {},
        "system_valid": False,
    }


def _mode_order(mode: str) -> List[str]:
    return mode_to_stages(mode)


def _default_bubble_entrypoints(repo_root: Path, manifest_summary: Dict[str, Any]) -> List[str]:
    candidates: List[str] = []

    inferred_entrypoints = manifest_summary.get("entrypoints") if isinstance(manifest_summary, dict) else []
    if isinstance(inferred_entrypoints, list):
        preferred_filenames = ("__main__.py", "main.py", "app.py", "cli.py", "run.py")
        prioritized = sorted(
            [str(item).strip() for item in inferred_entrypoints if str(item).strip()],
            key=lambda item: (
                0
                if Path(item).name.lower() in preferred_filenames
                else 1,
                str(item).lower(),
            ),
        )

        for entrypoint in prioritized:
            candidate_path = (repo_root / entrypoint).resolve()
            if candidate_path.exists() and candidate_path.is_file():
                candidates.append(entrypoint)

    if not candidates:
        for filename in ("__main__.py", "main.py", "app.py", "cli.py", "run.py"):
            candidate_path = (repo_root / filename).resolve()
            if candidate_path.exists() and candidate_path.is_file():
                candidates.append(filename)

    if not candidates:
        # Last-resort deterministic probe when no plausible target entrypoint exists.
        candidates.append("scenario:depth-probe")

    # Keep deterministic ordering and de-duplication.
    ordered: List[str] = []
    seen: set[str] = set()
    for value in candidates:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)

    return ordered


def _runtime_plan_existing_flow_path(output_root: Path, min_mtime: float | None = None) -> Path | None:
    candidate = output_root / "execution_flow_graph.json"
    if candidate.exists() and candidate.is_file():
        if min_mtime is not None:
            try:
                if float(candidate.stat().st_mtime) < float(min_mtime):
                    return None
            except OSError:
                return None
        return candidate
    return None


def _default_diagnostics_result() -> Dict[str, Any]:
    return {
        "diagnostics": {
            "status": "PASS",
            "root_causes": [],
            "causal_chain": [],
            "system_health": {
                "structural_health": 1.0,
                "graph_connectivity": 1.0,
                "dependency_integrity": 1.0,
                "semantic_consistency": 1.0,
            },
            "summary": {
                "primary_failure_mode": "No validation issues detected.",
                "secondary_failure_modes": [],
                "stability_class": "STABLE",
            },
            "top_issues": [],
            "failure_domains": [],
            "recommended_actions": ["Continue deterministic monitoring."],
            "validation_sections": {
                "structural_validation": {"status": "PASS", "score": 1.0, "summary": "Not executed in selected mode."},
                "resolver_consistency": {"status": "PASS", "score": 1.0, "summary": "Not executed in selected mode."},
                "reachability_analysis": {"status": "PASS", "score": 1.0, "summary": "Not executed in selected mode."},
                "semantic_validation": {"status": "PASS", "score": 1.0, "summary": "Not executed in selected mode."},
            },
        },
        "root_cause": "none",
        "confidence": 0.6,
        "top_issues": [],
        "failure_domains": [],
        "recommended_actions": ["Continue deterministic monitoring."],
    }


def run_staged_pipeline(
    repo_path: Path,
    output_dir: Path,
    entrypoints: Sequence[str] | None = None,
    bubble_mode: bool = True,
    mode: str = "full-pipeline",
    timeout_seconds: int = 30,
    memory_cap_mb: int = 256,
    max_events: int = 5000,
    max_depth: int = 120,
) -> Dict[str, Any]:
    repo_root = repo_path.resolve()
    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    events_path = out_root / "pipeline_events.jsonl"
    events_path.write_text("", encoding="utf-8")

    selected_stages = _mode_order(mode)

    stage_data: Dict[str, Dict[str, Any]] = {}

    try:
        explicit_entrypoints = [str(item).strip() for item in (entrypoints or []) if str(item).strip()]

        if "manifest" in selected_stages:
            manifest_result = build_manifest(
                repo_path=repo_root,
                output_dir=out_root,
                explicit_entrypoints=explicit_entrypoints,
            )
            stage_data["manifest"] = manifest_result
            append_stage_event(events_path, "manifest", "ok", manifest_result.get("summary", {}))

        if "static" in selected_stages:
            manifest_path = Path(stage_data["manifest"]["manifest_path"])
            static_result = run_static_analysis(
                repo_path=repo_root,
                manifest_path=manifest_path,
                output_dir=out_root,
            )
            stage_data["static"] = static_result
            append_stage_event(events_path, "static", "ok", static_result.get("summary", {}))

        if "graph" in selected_stages:
            manifest_path = Path(stage_data["manifest"]["manifest_path"])
            analysis_path = Path(stage_data["static"]["analysis_path"])
            graph_result = build_dependency_graph(
                manifest_path=manifest_path,
                static_analysis_path=analysis_path,
                output_dir=out_root,
            )
            stage_data["graph"] = graph_result

            architecture_result = build_architecture_constraint_report(
                graph_payload=graph_result.get("graph", {}),
                output_dir=out_root,
            )
            stage_data["architecture"] = architecture_result

            semantic_cluster_result = build_semantic_cluster_report(
                manifest_path=manifest_path,
                static_analysis_path=analysis_path,
                output_dir=out_root,
            )
            stage_data["semantic_clusters"] = semantic_cluster_result

            graph_details = graph_result.get("graph", {}).get("summary", {})
            graph_event_details = dict(graph_details) if isinstance(graph_details, dict) else {}

            architecture_summary = (
                architecture_result.get("report", {}).get("summary", {})
                if isinstance(architecture_result.get("report"), dict)
                else {}
            )
            if architecture_summary:
                graph_event_details["architecture_constraints"] = {
                    "violation_count_total": int(architecture_summary.get("violation_count_total", 0) or 0),
                    "constraint_coverage_ratio": float(architecture_summary.get("constraint_coverage_ratio", 0.0) or 0.0),
                    "domain_score": float(architecture_summary.get("domain_score", 1.0) or 1.0),
                }

            semantic_summary = (
                semantic_cluster_result.get("report", {}).get("summary", {})
                if isinstance(semantic_cluster_result.get("report"), dict)
                else {}
            )
            if semantic_summary:
                graph_event_details["semantic_clusters"] = {
                    "cluster_count": int(semantic_summary.get("cluster_count", 0) or 0),
                    "cross_context_cluster_count": int(semantic_summary.get("cross_context_cluster_count", 0) or 0),
                    "abstraction_collision_count": int(semantic_summary.get("abstraction_collision_count", 0) or 0),
                    "domain_score": float(semantic_summary.get("domain_score", 1.0) or 1.0),
                }

            append_stage_event(events_path, "graph", "ok", graph_event_details)

        if "bubble" in selected_stages:
            manifest_summary = stage_data.get("manifest", {}).get("summary", {})
            inferred_entrypoints = manifest_summary.get("entrypoints") if isinstance(manifest_summary, dict) else []

            run_entrypoints = explicit_entrypoints or []
            scenario_plan: Dict[str, Any] = {}

            if not run_entrypoints:
                graph_path = Path(stage_data["graph"]["graph_path"]) if "graph" in stage_data else None
                manifest_summary_path = (
                    Path(stage_data["manifest"]["manifest_summary_path"])
                    if "manifest" in stage_data
                    else None
                )

                if graph_path and manifest_summary_path:
                    run_reference_mtime = None
                    try:
                        run_reference_mtime = float(events_path.stat().st_mtime)
                    except OSError:
                        run_reference_mtime = None

                    scenario_plan = build_runtime_scenario_plan(
                        dependency_graph_path=graph_path,
                        manifest_summary_path=manifest_summary_path,
                        manifest_path=Path(stage_data["manifest"]["manifest_path"]),
                        output_dir=out_root,
                        execution_flow_graph_path=_runtime_plan_existing_flow_path(out_root, min_mtime=run_reference_mtime),
                        max_scenarios=100,
                        max_entrypoints=5,
                        max_seed_scenarios=40,
                        coverage_stop_threshold=0.25,
                        max_runtime_seconds=max(120, min(300, int(timeout_seconds) * 6)),
                        max_events_per_scenario=min(int(max_events), 600),
                        max_entrypoint_seconds=max(1, min(int(timeout_seconds), 3)),
                    )
                    run_entrypoints = [
                        str(item).strip()
                        for item in scenario_plan.get("entrypoints", [])
                        if str(item).strip()
                    ]

            if not run_entrypoints and isinstance(inferred_entrypoints, list):
                run_entrypoints = [str(item).strip() for item in inferred_entrypoints if str(item).strip()][:5]
            if not run_entrypoints:
                run_entrypoints = _default_bubble_entrypoints(repo_root, manifest_summary if isinstance(manifest_summary, dict) else {})

            stage_data["runtime_scenarios"] = scenario_plan

            scenario_summary = scenario_plan.get("summary") if isinstance(scenario_plan.get("summary"), dict) else {}

            runtime_result = execute_runtime_bubble(
                repo_path=repo_root,
                output_dir=out_root,
                entrypoints=run_entrypoints,
                bubble_mode=bool(bubble_mode),
                timeout_seconds=int(timeout_seconds),
                memory_cap_mb=int(memory_cap_mb),
                max_events=int(max_events),
                max_depth=int(max_depth),
                total_node_count=int(scenario_summary.get("total_node_count", 0) or 0),
                baseline_runtime_hit_nodes=scenario_plan.get("baseline_runtime_hit_nodes", []),
                coverage_stop_threshold=float(scenario_summary.get("coverage_stop_threshold", 0.25) or 0.25),
                max_runtime_seconds=int(scenario_summary.get("max_runtime_seconds", max(120, min(300, int(timeout_seconds) * 6))) or max(120, min(300, int(timeout_seconds) * 6))),
                max_events_per_scenario=int(scenario_summary.get("max_events_per_scenario", min(int(max_events), 600)) or min(int(max_events), 600)),
                max_entrypoint_seconds=int(scenario_summary.get("max_entrypoint_seconds", max(1, min(int(timeout_seconds), 3))) or max(1, min(int(timeout_seconds), 3))),
                runtime_plan=scenario_plan,
            )
            stage_data["bubble"] = runtime_result

            causal_flow_result = build_causal_flow_report(
                runtime_trace_path=Path(str(runtime_result.get("trace_path", ""))),
                execution_flow_graph_path=Path(str(runtime_result.get("flow_graph_path", ""))),
                output_dir=out_root,
                manifest_summary_path=(
                    Path(stage_data["manifest"]["manifest_summary_path"])
                    if "manifest" in stage_data
                    else None
                ),
            )
            stage_data["causal_flow"] = causal_flow_result

            bubble_summary = runtime_result.get("flow_graph", {}).get("summary", {})
            details = dict(bubble_summary) if isinstance(bubble_summary, dict) else {}
            if scenario_summary:
                details["scenario_plan"] = {
                    "selected_node_count": int(scenario_summary.get("selected_node_count", 0) or 0),
                    "entrypoint_count": int(scenario_summary.get("entrypoint_count", 0) or 0),
                    "baseline_coverage_ratio": float(scenario_summary.get("baseline_coverage_ratio", 0.0) or 0.0),
                }

            causal_summary = (
                causal_flow_result.get("report", {}).get("summary", {})
                if isinstance(causal_flow_result.get("report"), dict)
                else {}
            )
            if causal_summary:
                details["causal_flow"] = {
                    "workflow_count": int(causal_summary.get("workflow_count", 0) or 0),
                    "role_coverage_ratio": float(causal_summary.get("role_coverage_ratio", 0.0) or 0.0),
                    "domain_score": float(causal_summary.get("domain_score", 1.0) or 1.0),
                }

            append_stage_event(events_path, "bubble", "ok", details)

        if "classification" in selected_stages:
            if "graph" not in stage_data:
                raise PipelineExecutionError("classification", "Graph stage output is required before classification.")
            if "manifest" not in stage_data:
                raise PipelineExecutionError("classification", "Manifest stage output is required before classification.")

            runtime_graph_path: Path | None = None
            runtime_graph_ref = str(stage_data.get("bubble", {}).get("flow_graph_path", "")).strip()
            if runtime_graph_ref:
                runtime_graph_path = Path(runtime_graph_ref)

            runtime_trace_path: Path | None = None
            runtime_trace_ref = str(stage_data.get("bubble", {}).get("trace_path", "")).strip()
            if runtime_trace_ref:
                runtime_trace_path = Path(runtime_trace_ref)

            bubble_summary = (
                stage_data.get("bubble", {}).get("flow_graph", {}).get("summary", {})
                if isinstance(stage_data.get("bubble"), dict)
                else {}
            )
            # Runtime is evidence and should never hard-fail classification fusion.
            enforce_runtime_signal = False

            try:
                heat_result = classify_code_heat_from_artifacts(
                    graph_path=Path(stage_data["graph"]["graph_path"]),
                    manifest_summary_path=Path(stage_data["manifest"]["manifest_summary_path"]),
                    output_dir=out_root,
                    runtime_flow_graph_path=runtime_graph_path,
                    runtime_trace_path=runtime_trace_path,
                    enforce_runtime_signal=enforce_runtime_signal,
                )
            except Exception as exc:  # noqa: BLE001
                raise PipelineExecutionError("classification", str(exc)) from exc
            stage_data["heat"] = heat_result

            dead_result = build_dead_code_report_from_artifact(
                heat_path=Path(heat_result["heat_path"]),
                output_dir=out_root,
            )
            stage_data["dead"] = dead_result

            scenario_validation_warnings: List[str] = []
            heat_payload = heat_result.get("heat") if isinstance(heat_result.get("heat"), dict) else {}
            heat_distribution = heat_payload.get("distribution") if isinstance(heat_payload.get("distribution"), dict) else {}
            heat_runtime_validation = (
                heat_payload.get("runtime_validation")
                if isinstance(heat_payload.get("runtime_validation"), dict)
                else {}
            )
            heat_reconciliation = (
                heat_payload.get("runtime_static_reconciliation")
                if isinstance(heat_payload.get("runtime_static_reconciliation"), dict)
                else {}
            )

            scenario_summary = (
                stage_data.get("runtime_scenarios", {}).get("summary")
                if isinstance(stage_data.get("runtime_scenarios"), dict)
                else {}
            )
            if isinstance(scenario_summary, dict) and scenario_summary:
                baseline_coverage_ratio = float(scenario_summary.get("baseline_coverage_ratio", 0.0) or 0.0)
                current_coverage_ratio = float(heat_runtime_validation.get("coverage_ratio", 0.0) or 0.0)
                if current_coverage_ratio <= baseline_coverage_ratio:
                    scenario_validation_warnings.append(
                        "Runtime coverage ratio did not improve versus scenario baseline."
                    )

                baseline_overlap_ratio = float(scenario_summary.get("baseline_overlap_ratio", 0.0) or 0.0)
                current_overlap_ratio = float(heat_reconciliation.get("overlap_ratio", 0.0) or 0.0)
                if current_overlap_ratio <= baseline_overlap_ratio:
                    scenario_validation_warnings.append(
                        "Runtime/static CALL overlap ratio did not improve versus scenario baseline."
                    )

                baseline_hot_warm = int(scenario_summary.get("baseline_runtime_hit_nodes", 0) or 0)
                current_hot_warm = int(heat_distribution.get("HOT", 0) or 0) + int(heat_distribution.get("WARM", 0) or 0)
                if current_hot_warm <= baseline_hot_warm:
                    scenario_validation_warnings.append(
                        "HOT/WARM active-node count did not improve versus baseline runtime hit nodes."
                    )

            stage_data["scenario_validation"] = {
                "warnings": scenario_validation_warnings,
            }

            append_stage_event(
                events_path,
                "classification",
                "ok",
                {
                    "heat_distribution": heat_distribution,
                    "dead_summary": dead_result.get("report", {}).get("summary", {}),
                    "scenario_validation_warnings": scenario_validation_warnings,
                },
            )

        trust_payload = {
            "score": 1.0,
            "breakdown": {
                "scores": {
                    "structural_integrity": 1.0,
                    "dependency_consistency": 1.0,
                    "topology_validation": 1.0,
                    "semantic_observations": 1.0,
                },
                "domain_scores": {
                    "structural_integrity": 1.0,
                    "dependency_consistency": 1.0,
                    "topology_validation": 1.0,
                    "semantic_observations": 1.0,
                },
                "weighted_contributions": {
                    "structural_integrity": 0.35,
                    "dependency_consistency": 0.25,
                    "topology_validation": 0.25,
                    "semantic_observations": 0.15,
                },
            },
        }

        system_valid = True
        validation_result: Dict[str, Any] = {}

        if "verification" in selected_stages:
            if "graph" not in stage_data:
                raise PipelineExecutionError("verification", "Graph stage output is required before verification.")

            graph_payload = stage_data["graph"].get("graph", {})
            validation_graph = graph_payload.get("validation_graph") if isinstance(graph_payload.get("validation_graph"), dict) else {}
            resolver_data = graph_payload.get("resolver_data") if isinstance(graph_payload.get("resolver_data"), dict) else {}

            if not validation_graph:
                raise PipelineExecutionError("verification", "Missing validation graph payload from graph stage.")

            active_entrypoints = explicit_entrypoints
            if not active_entrypoints:
                inferred = stage_data.get("manifest", {}).get("summary", {}).get("entrypoints", [])
                if isinstance(inferred, list):
                    active_entrypoints = [str(item).strip() for item in inferred if str(item).strip()]

            execution_evidence: Dict[str, Any] = {}
            heat_payload = stage_data.get("heat", {}).get("heat")
            if isinstance(heat_payload, dict):
                runtime_validation = heat_payload.get("runtime_validation")
                runtime_reconciliation = heat_payload.get("runtime_static_reconciliation")
                distribution = heat_payload.get("distribution")

                execution_evidence = {
                    "runtime_source": str(heat_payload.get("runtime_source", "unknown")),
                    "runtime_validation": runtime_validation if isinstance(runtime_validation, dict) else {},
                    "runtime_static_reconciliation": runtime_reconciliation if isinstance(runtime_reconciliation, dict) else {},
                    "distribution": distribution if isinstance(distribution, dict) else {},
                }

            runtime_scenarios_payload = stage_data.get("runtime_scenarios")
            if isinstance(runtime_scenarios_payload, dict) and runtime_scenarios_payload:
                execution_evidence["runtime_scenarios"] = runtime_scenarios_payload

            scenario_validation_payload = stage_data.get("scenario_validation")
            if isinstance(scenario_validation_payload, dict) and scenario_validation_payload:
                execution_evidence["scenario_validation"] = scenario_validation_payload

            architecture_report = stage_data.get("architecture", {}).get("report")
            if isinstance(architecture_report, dict):
                execution_evidence["architecture_constraints"] = architecture_report

            semantic_report = stage_data.get("semantic_clusters", {}).get("report")
            if isinstance(semantic_report, dict):
                execution_evidence["semantic_clusters"] = semantic_report

            causal_report = stage_data.get("causal_flow", {}).get("report")
            if isinstance(causal_report, dict):
                execution_evidence["causal_flow"] = causal_report

            validation_result = run_verification(
                graph_data=validation_graph,
                resolver_data=resolver_data,
                entrypoints=active_entrypoints,
                min_trust=0.40,
                execution_evidence=execution_evidence,
            )

            validation_path = out_root / "validation_result.json"
            write_json(validation_path, validation_result, pretty=True)
            stage_data["verification"] = {
                "validation": validation_result,
                "validation_path": str(validation_path),
            }

            trust_payload = {
                "score": float(validation_result.get("trust_score", 0.0) or 0.0),
                "breakdown": validation_result.get("trust_breakdown") if isinstance(validation_result.get("trust_breakdown"), dict) else {},
            }
            system_valid = bool(validation_result.get("system_valid", False))

            append_stage_event(
                events_path,
                "verification",
                "ok",
                {
                    "system_valid": system_valid,
                    "trust_score": trust_payload.get("score", 0.0),
                    "execution_confidence": (
                        validation_result.get("trust_breakdown", {}).get("scores", {}).get("execution_confidence", 1.0)
                        if isinstance(validation_result.get("trust_breakdown"), dict)
                        else 1.0
                    ),
                },
            )

        diagnostics_result: Dict[str, Any] = _default_diagnostics_result()

        if "diagnostics" in selected_stages:
            if "verification" in stage_data:
                diagnostics_result = run_diagnostics_from_artifacts(
                    validation_path=Path(stage_data["verification"]["validation_path"]),
                    graph_path=Path(stage_data["graph"]["graph_path"]) if "graph" in stage_data else None,
                    resolver_path=None,
                )

            diagnostics_payload = diagnostics_result.get("diagnostics") if isinstance(diagnostics_result.get("diagnostics"), dict) else {}
            trust_payload = _apply_diagnostics_to_trust(trust_payload, diagnostics_payload)

            append_stage_event(
                events_path,
                "diagnostics",
                "ok",
                {
                    "status": diagnostics_payload.get("status", "PASS"),
                    "root_cause": diagnostics_result.get("root_cause", "none"),
                },
            )

        if "report" in selected_stages:
            report_result = build_final_report(
                output_dir=out_root,
                manifest_result=stage_data.get("manifest", {}),
                static_result=stage_data.get("static", {}),
                graph_result=stage_data.get("graph", {}),
                runtime_result=stage_data.get("bubble", {}),
                heat_result=stage_data.get("heat", {}),
                dead_code_result=stage_data.get("dead", {}),
                architecture_result=stage_data.get("architecture", {}),
                semantic_result=stage_data.get("semantic_clusters", {}),
                causal_flow_result=stage_data.get("causal_flow", {}),
                diagnostics_result=diagnostics_result,
                trust_payload=trust_payload,
                system_valid=system_valid,
            )
            stage_data["report"] = report_result
            append_stage_event(events_path, "report", "ok", report_result.get("report", {}).get("summary", {}))

        diagnostics_payload = diagnostics_result.get("diagnostics") if isinstance(diagnostics_result.get("diagnostics"), dict) else diagnostics_result

        payload = {
            "summary": {
                "status": "PASSED" if system_valid else "FAILED",
                "root_cause": str(diagnostics_result.get("root_cause", "none")),
                "confidence": float(diagnostics_result.get("confidence", 0.0) or 0.0),
            },
            "diagnostics": diagnostics_payload,
            "trust": trust_payload,
            "system_valid": bool(system_valid),
            "artifacts": {
                "manifest_jsonl": stage_data.get("manifest", {}).get("manifest_path", ""),
                "manifest_summary_json": stage_data.get("manifest", {}).get("manifest_summary_path", ""),
                "static_analysis_jsonl": stage_data.get("static", {}).get("analysis_path", ""),
                "dependency_graph_json": stage_data.get("graph", {}).get("graph_path", ""),
                "runtime_trace_jsonl": stage_data.get("bubble", {}).get("trace_path", ""),
                "execution_flow_graph_json": stage_data.get("bubble", {}).get("flow_graph_path", ""),
                "heat_classification_json": stage_data.get("heat", {}).get("heat_path", ""),
                "dead_code_report_json": stage_data.get("dead", {}).get("report_path", ""),
                "architecture_constraints_json": stage_data.get("architecture", {}).get("report_path", ""),
                "semantic_clusters_json": stage_data.get("semantic_clusters", {}).get("report_path", ""),
                "causal_flow_report_json": stage_data.get("causal_flow", {}).get("report_path", ""),
                "validation_result_json": stage_data.get("verification", {}).get("validation_path", ""),
                "final_report_json": stage_data.get("report", {}).get("report_path", str((out_root / "final_report.json"))),
                "pipeline_events_jsonl": str(events_path),
            },
            "validation": validation_result,
        }

        contract_output_path = out_root / "pipeline_contract.json"
        write_json(contract_output_path, payload, pretty=True)

        return payload

    except PipelineExecutionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PipelineExecutionError("pipeline", str(exc)) from exc
