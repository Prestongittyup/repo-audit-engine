from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence

if __package__ is None or __package__ == "":
    # Allows "python repo_audit_engine/cli.py ..." from repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repo_audit_engine.diagnostics.reporter import run_diagnostics
from repo_audit_engine.pipeline.orchestrator import PipelineExecutionError, run_staged_pipeline
from repo_audit_engine.pipeline.validation import run_verification
from repo_audit_engine.io.artifacts import load_json, write_json


class PipelineStageError(RuntimeError):
    def __init__(self, stage_name: str, message: str):
        super().__init__(f"[{stage_name}] {message}")
        self.stage_name = stage_name


_EXTERNAL_STAGE_MODULES: Dict[str, str] = {
    "inventory": "repo_audit_engine.pipeline.inventory",
    "canonical": "repo_audit_engine.pipeline.canonical",
    "resolver": "repo_audit_engine.pipeline.resolver",
    "graph": "repo_audit_engine.pipeline.graph",
    "trust": "repo_audit_engine.pipeline.trust",
    "policy": "repo_audit_engine.pipeline.policy",
}

_EXTERNAL_STAGE_CALLABLES: Dict[str, Sequence[str]] = {
    "inventory": ("run_inventory", "inventory", "run"),
    "canonical": ("run_canonical", "canonicalize", "run"),
    "resolver": ("run_resolver", "resolve", "run"),
    "graph": ("run_graph", "build_graph", "run"),
    "trust": ("run_trust", "compute_trust", "run"),
    "policy": ("run_policy", "evaluate_policy", "run"),
}


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--graph-path", required=True, help="Path to unified graph JSON document.")
    parser.add_argument("--resolver-path", help="Path to resolver/edges JSON document.")
    parser.add_argument(
        "--entrypoint",
        action="append",
        default=[],
        help="Canonical entrypoint ID (repeat the flag for multiple values).",
    )
    parser.add_argument("--min-trust", type=float, default=0.40, help="Minimum trust threshold.")
    parser.add_argument("--output", help="Optional output JSON path.")
    parser.add_argument("--pretty", action="store_true", help="Write pretty-formatted JSON output.")


def _load_inputs(args: argparse.Namespace) -> Dict[str, Any]:
    graph_data = load_json(args.graph_path)
    resolver_data: Dict[str, Any] = {}
    if args.resolver_path:
        resolver_data = load_json(args.resolver_path)

    return {
        "graph_data": graph_data,
        "resolver_data": resolver_data,
    }


def _build_summary_status(validation_result: Dict[str, Any]) -> str:
    if bool(validation_result.get("policy_critical_failure")):
        return "FAILED"
    if bool(validation_result.get("system_valid")):
        return "PASS"
    return "DEGRADED"


def _emit_output(payload: Dict[str, Any], output_path: str | None, pretty: bool) -> None:
    if output_path:
        write_json(output_path, payload, pretty=pretty)

    if pretty:
        print(json.dumps(payload, indent=2, ensure_ascii=True))
    else:
        print(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))


def _parse_bool_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value

    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False

    raise ValueError(f"Invalid boolean flag value: {value}")


def _ensure_mapping(stage_name: str, value: Any, context: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise PipelineStageError(stage_name, f"{context} must be an object/dict payload.")
    return value


def _load_stage_callable(stage_name: str):
    module_name = _EXTERNAL_STAGE_MODULES[stage_name]
    callable_names = _EXTERNAL_STAGE_CALLABLES[stage_name]

    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            candidates = ", ".join(callable_names)
            raise PipelineStageError(
                stage_name,
                f"Missing stage module '{module_name}'. Add this module with one of: {candidates}.",
            ) from exc
        raise

    for callable_name in callable_names:
        candidate = getattr(module, callable_name, None)
        if callable(candidate):
            return candidate

    candidates = ", ".join(callable_names)
    raise PipelineStageError(
        stage_name,
        f"Stage module '{module_name}' does not expose a supported callable. Expected one of: {candidates}.",
    )


def _build_stage_input(
    repo_path: Path,
    entrypoints: Sequence[str],
    stage_outputs: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "repo_path": str(repo_path),
        "entrypoints": list(entrypoints),
        "stage_outputs": dict(stage_outputs),
    }


def _run_external_stage(
    stage_name: str,
    repo_path: Path,
    entrypoints: Sequence[str],
    stage_outputs: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    stage_callable = _load_stage_callable(stage_name)
    stage_input = _build_stage_input(repo_path, entrypoints, stage_outputs)

    try:
        result = stage_callable(stage_input)
    except TypeError:
        # Support stage signatures that accept keyword arguments directly.
        result = stage_callable(**stage_input)
    except Exception as exc:  # noqa: BLE001
        raise PipelineStageError(stage_name, f"Stage execution failed: {exc}") from exc

    return _ensure_mapping(stage_name, result, "Stage result")


def _extract_graph_data(graph_stage_output: Dict[str, Any]) -> Dict[str, Any]:
    graph_output = _ensure_mapping("graph", graph_stage_output, "Graph stage result")

    for key in ("graph_data", "graph"):
        candidate = graph_output.get(key)
        if isinstance(candidate, dict):
            return candidate

    if "nodes" in graph_output or "edges" in graph_output:
        return graph_output

    raise PipelineStageError(
        "graph",
        "Graph stage output does not include graph payload. Expected keys 'graph_data' or 'graph'.",
    )


def _extract_resolver_data(resolver_stage_output: Dict[str, Any]) -> Dict[str, Any]:
    resolver_output = _ensure_mapping("resolver", resolver_stage_output, "Resolver stage result")

    for key in ("resolver_data", "resolver"):
        candidate = resolver_output.get(key)
        if isinstance(candidate, dict):
            return candidate

    return resolver_output


def _extract_trust_payload(
    trust_result: Dict[str, Any],
    validation_result: Dict[str, Any],
) -> Dict[str, Any]:
    trust_payload = _ensure_mapping("trust", trust_result, "Trust stage result")

    score = trust_payload.get("score", trust_payload.get("trust_score"))
    if not isinstance(score, (int, float)):
        score = validation_result.get("trust_score")
    if not isinstance(score, (int, float)):
        raise PipelineStageError("trust", "Trust stage output must contain numeric 'score' or 'trust_score'.")

    breakdown = trust_payload.get("breakdown", trust_payload.get("trust_breakdown"))
    if not isinstance(breakdown, dict):
        breakdown = validation_result.get("trust_breakdown")
    if not isinstance(breakdown, dict):
        raise PipelineStageError("trust", "Trust stage output must contain object 'breakdown' or 'trust_breakdown'.")

    return {
        "score": float(score),
        "breakdown": breakdown,
    }


def _extract_policy_valid(policy_result: Dict[str, Any], validation_result: Dict[str, Any]) -> bool:
    policy_payload = _ensure_mapping("policy", policy_result, "Policy stage result")

    if isinstance(policy_payload.get("system_valid"), bool):
        return bool(policy_payload["system_valid"])

    if isinstance(validation_result.get("system_valid"), bool):
        return bool(validation_result["system_valid"])

    raise PipelineStageError("policy", "Policy stage output must contain boolean 'system_valid'.")


def _apply_diagnostics_to_trust(
    trust_payload: Dict[str, Any],
    diagnostics_payload: Dict[str, Any],
) -> Dict[str, Any]:
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
    max_severity = max(severity_values) if severity_values else 0.0

    trust_breakdown["diagnostic_context"] = {
        "max_root_cause_severity": round(max_severity, 3),
        "base_score": round(base_score, 3),
        "score_adjustment_applied": False,
        "diagnostics_status": str(diagnostics_payload.get("status", "UNKNOWN")),
    }

    return {
        "score": round(max(0.0, min(1.0, base_score)), 3),
        "breakdown": trust_breakdown,
    }


def _build_pipeline_payload(
    diagnostics_result: Dict[str, Any],
    trust_payload: Dict[str, Any],
    system_valid: bool,
) -> Dict[str, Any]:
    diagnostics_result_payload = _ensure_mapping("diagnostics", diagnostics_result, "Diagnostics stage result")
    diagnostics_payload = diagnostics_result_payload.get("diagnostics")
    if not isinstance(diagnostics_payload, dict):
        diagnostics_payload = diagnostics_result_payload

    root_cause_raw = diagnostics_result_payload.get("root_cause")
    if root_cause_raw is None and isinstance(diagnostics_payload.get("summary"), dict):
        root_cause_raw = diagnostics_payload.get("summary", {}).get("primary_failure_mode")
    root_cause = str(root_cause_raw).strip() if root_cause_raw is not None else "unknown"

    confidence_raw = diagnostics_result_payload.get("confidence", 0.0)
    confidence: float
    if isinstance(confidence_raw, (int, float)):
        confidence = float(confidence_raw)
    else:
        confidence = 0.0

    trust_with_diagnostics = _apply_diagnostics_to_trust(trust_payload, diagnostics_payload)

    return {
        "summary": {
            "status": "PASSED" if system_valid else "FAILED",
            "root_cause": root_cause,
            "confidence": confidence,
        },
        "diagnostics": diagnostics_payload,
        "trust": {
            "score": trust_with_diagnostics["score"],
            "breakdown": trust_with_diagnostics["breakdown"],
        },
    }


def _build_stage_failure_payload(error: PipelineStageError) -> Dict[str, Any]:
    message = str(error)
    missing_stage = "missing stage module" in message.lower() or "does not expose a supported callable" in message.lower()
    issue_type = "MISSING_STAGE" if missing_stage else "STAGE_EXECUTION_FAILURE"

    stage_file = f"repo_audit_engine/pipeline/{error.stage_name}.py"
    recommended_actions = [
        f"Implement or fix stage '{error.stage_name}' in {stage_file} using a supported callable signature.",
        "Re-run run-pipeline after stage implementation is available and returns structured output.",
    ]

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
                    "id": f"rc-01-{issue_type.lower()}",
                    "type": "STRUCTURAL",
                    "severity": 1.0,
                    "confidence": 1.0,
                    "description": message,
                    "affected_nodes": [],
                    "evidence": [
                        f"pipeline_stage={error.stage_name}",
                        "diagnostic_synthesis=stage_failure",
                    ],
                    "propagation_path": [
                        "Pipeline orchestration",
                        "Validation execution",
                        "Final verdict",
                    ],
                }
            ],
            "causal_chain": [
                {
                    "step": 1,
                    "cause": f"Stage '{error.stage_name}' could not execute correctly.",
                    "effect": "Pipeline terminated before complete validation synthesis.",
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
                    "domain": "pipeline_orchestration",
                    "type": issue_type,
                    "severity": "HIGH",
                    "message": message,
                    "rank": 1,
                }
            ],
            "failure_domains": ["pipeline_orchestration"],
            "recommended_actions": recommended_actions,
        },
        "trust": {
            "score": 0.0,
            "breakdown": {
                "reason": "Pipeline terminated before trust evaluation completed.",
            },
        },
    }


def cmd_validate(args: argparse.Namespace) -> int:
    loaded = _load_inputs(args)
    validation_result = run_verification(
        graph_data=loaded["graph_data"],
        resolver_data=loaded["resolver_data"],
        entrypoints=args.entrypoint,
        min_trust=float(args.min_trust),
    )

    _emit_output(validation_result, args.output, pretty=bool(args.pretty))

    if getattr(args, "fail_on_invalid", False) and not bool(validation_result.get("system_valid")):
        return 1

    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    loaded = _load_inputs(args)

    validation_result = run_verification(
        graph_data=loaded["graph_data"],
        resolver_data=loaded["resolver_data"],
        entrypoints=args.entrypoint,
        min_trust=float(args.min_trust),
    )

    diagnostics_result = run_diagnostics(
        validation_result=validation_result,
        graph_data=loaded["graph_data"],
        resolver_data=loaded["resolver_data"],
    )

    diagnostics_payload = diagnostics_result.get("diagnostics")
    if not isinstance(diagnostics_payload, dict):
        diagnostics_payload = diagnostics_result

    trust_with_diagnostics = _apply_diagnostics_to_trust(
        {
            "score": validation_result.get("trust_score", 0.0),
            "breakdown": validation_result.get("trust_breakdown", {}),
        },
        diagnostics_payload,
    )

    payload: Dict[str, Any] = {
        "summary": {
            "status": _build_summary_status(validation_result),
            "confidence": diagnostics_result.get("confidence", 0.0),
            "root_cause": diagnostics_result.get("root_cause", "none"),
        },
        "diagnostics": diagnostics_payload,
        "trust": {
            "score": trust_with_diagnostics["score"],
            "breakdown": trust_with_diagnostics["breakdown"],
        },
        "validation": validation_result if bool(args.include_validation) else {
            "status": validation_result.get("status"),
            "critical_failure": validation_result.get("critical_failure"),
            "system_valid": validation_result.get("system_valid"),
            "policy_critical_failure": validation_result.get("policy_critical_failure"),
        },
    }

    _emit_output(payload, args.output, pretty=bool(args.pretty))

    if getattr(args, "fail_on_invalid", False) and not bool(validation_result.get("system_valid")):
        return 1

    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    examples_dir = Path(__file__).resolve().parent / "examples"
    graph_path = examples_dir / "mock_graph.json"
    resolver_path = examples_dir / "mock_resolver.json"

    namespace = argparse.Namespace(
        graph_path=str(graph_path),
        resolver_path=str(resolver_path),
        entrypoint=["canonical://service/App"],
        min_trust=0.40,
        output=args.output,
        pretty=args.pretty,
        include_validation=True,
        fail_on_invalid=False,
    )

    return cmd_analyze(namespace)


def cmd_run(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).resolve()
    output_path = Path(args.output).resolve()

    # `run` treats --output as an output directory.
    output_dir = output_path
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        payload = run_staged_pipeline(
            repo_path=repo_path,
            output_dir=output_dir,
            entrypoints=list(args.entrypoints or []),
            bubble_mode=_parse_bool_flag(args.bubble_mode),
            mode=str(args.mode),
            timeout_seconds=int(args.timeout_seconds),
            memory_cap_mb=int(args.memory_cap_mb),
            max_events=int(args.max_events),
            max_depth=int(args.max_depth),
        )
    except PipelineExecutionError as exc:
        detail = str(exc)
        prefix = f"[{exc.stage}] "
        if detail.startswith(prefix):
            detail = detail[len(prefix) :]
        payload = _build_stage_failure_payload(PipelineStageError(exc.stage, detail))

    print(json.dumps(payload, indent=2 if bool(args.pretty) else None, ensure_ascii=True))
    return 0 if bool(payload.get("system_valid", False)) else 1


def cmd_run_pipeline(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).resolve()
    output_file = Path(args.output).resolve()
    artifacts_dir = output_file.parent / f"{output_file.stem}_artifacts"

    try:
        payload = run_staged_pipeline(
            repo_path=repo_path,
            output_dir=artifacts_dir,
            entrypoints=list(args.entrypoints or []),
            bubble_mode=_parse_bool_flag(getattr(args, "bubble_mode", True)),
            mode="full-pipeline",
            timeout_seconds=int(getattr(args, "timeout_seconds", 30)),
            memory_cap_mb=int(getattr(args, "memory_cap_mb", 256)),
            max_events=int(getattr(args, "max_events", 5000)),
            max_depth=int(getattr(args, "max_depth", 120)),
        )
    except PipelineExecutionError as exc:
        detail = str(exc)
        prefix = f"[{exc.stage}] "
        if detail.startswith(prefix):
            detail = detail[len(prefix) :]
        payload = _build_stage_failure_payload(PipelineStageError(exc.stage, detail))

    contract_payload: Dict[str, Any] = {
        "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
        "diagnostics": payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {},
        "trust": payload.get("trust") if isinstance(payload.get("trust"), dict) else {},
        "system_valid": bool(payload.get("system_valid", False)),
    }

    _emit_output(contract_payload, str(output_file), pretty=bool(args.pretty))
    return 0 if bool(contract_payload.get("system_valid", False)) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repo_audit_engine.cli",
        description="Python repo analysis CLI.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Run validation and emit layer5-compatible output.",
    )
    _add_common_arguments(validate_parser)
    validate_parser.add_argument(
        "--fail-on-invalid",
        action="store_true",
        help="Return non-zero exit code when system_valid=false.",
    )
    validate_parser.set_defaults(handler=cmd_validate)

    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Run validation + diagnostics and emit combined report.",
    )
    _add_common_arguments(analyze_parser)
    analyze_parser.add_argument(
        "--include-validation",
        action="store_true",
        help="Include full validation payload in output JSON.",
    )
    analyze_parser.add_argument(
        "--fail-on-invalid",
        action="store_true",
        help="Return non-zero exit code when system_valid=false.",
    )
    analyze_parser.set_defaults(handler=cmd_analyze)

    demo_parser = subparsers.add_parser(
        "demo",
        help="Run analysis on bundled mock graph inputs.",
    )
    demo_parser.add_argument("--output", help="Optional output JSON path.")
    demo_parser.add_argument("--pretty", action="store_true", help="Write pretty-formatted JSON output.")
    demo_parser.set_defaults(handler=cmd_demo)

    run_parser = subparsers.add_parser(
        "run",
        help="Run deterministic staged pipeline (manifest/static/graph/bubble/heat/dead/report).",
    )
    run_parser.add_argument("--repo", required=True, help="Path to repository root to analyze.")
    run_parser.add_argument("--output", required=True, help="Output directory for staged artifacts and final report.")
    run_parser.add_argument(
        "--bubble-mode",
        nargs="?",
        const="true",
        default="true",
        help="Enable sandboxed runtime bubble execution (true/false).",
    )
    run_parser.add_argument(
        "--entrypoints",
        nargs="*",
        default=[],
        help="Optional entrypoints (module, module:function, or relative .py script).",
    )
    run_parser.add_argument(
        "--mode",
        choices=["manifest-only", "static-only", "static-analysis", "bubble-run", "full-pipeline"],
        default="full-pipeline",
        help="Select staged execution depth.",
    )
    run_parser.add_argument("--timeout-seconds", type=int, default=30, help="Timeout per bubble subprocess run.")
    run_parser.add_argument("--memory-cap-mb", type=int, default=256, help="Memory cap for bubble subprocess tracing.")
    run_parser.add_argument("--max-events", type=int, default=5000, help="Maximum traced call events per bubble run.")
    run_parser.add_argument("--max-depth", type=int, default=120, help="Maximum tracing recursion depth per bubble run.")
    run_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output to stdout.")
    run_parser.set_defaults(handler=cmd_run)

    run_pipeline_parser = subparsers.add_parser(
        "run-pipeline",
        help="Run full Python pipeline: inventory->canonical->resolver->graph->validation->trust->diagnostics->policy.",
    )
    run_pipeline_parser.add_argument("--repo", required=True, help="Path to repository root to analyze.")
    run_pipeline_parser.add_argument("--output", required=True, help="Path to write final JSON output.")
    run_pipeline_parser.add_argument(
        "--entrypoints",
        nargs="*",
        default=[],
        help="Optional list of canonical entrypoint IDs.",
    )
    run_pipeline_parser.add_argument("--pretty", action="store_true", help="Write pretty-formatted JSON output.")
    run_pipeline_parser.add_argument(
        "--bubble-mode",
        nargs="?",
        const="true",
        default="true",
        help="Enable sandboxed runtime bubble execution during run-pipeline (true/false).",
    )
    run_pipeline_parser.add_argument("--timeout-seconds", type=int, default=30, help="Timeout per bubble subprocess run.")
    run_pipeline_parser.add_argument("--memory-cap-mb", type=int, default=256, help="Memory cap for bubble subprocess tracing.")
    run_pipeline_parser.add_argument("--max-events", type=int, default=5000, help="Maximum traced call events per bubble run.")
    run_pipeline_parser.add_argument("--max-depth", type=int, default=120, help="Maximum tracing recursion depth per bubble run.")
    run_pipeline_parser.set_defaults(handler=cmd_run_pipeline)

    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2

    try:
        return int(handler(args))
    except FileNotFoundError as exc:
        print(json.dumps({"error": str(exc), "code": "FILE_NOT_FOUND"}, ensure_ascii=True), file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"Invalid JSON input: {exc}", "code": "INVALID_JSON"}, ensure_ascii=True), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc), "code": "UNHANDLED_EXCEPTION"}, ensure_ascii=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
