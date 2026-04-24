from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

if __package__ is None or __package__ == "":
    # Allows "python repo_audit_engine/cli.py ..." from repository root.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repo_audit_engine.pipeline.diagnostics import run_diagnostics
from repo_audit_engine.pipeline.validation import run_verification
from repo_audit_engine.utils.io import load_json, write_json


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

    payload: Dict[str, Any] = {
        "summary": {
            "status": _build_summary_status(validation_result),
            "confidence": diagnostics_result.get("confidence", 0.0),
            "root_cause": diagnostics_result.get("root_cause", "none"),
        },
        "diagnostics": {
            "top_issues": diagnostics_result.get("top_issues", []),
            "failure_domains": diagnostics_result.get("failure_domains", []),
            "example_nodes": diagnostics_result.get("example_nodes", []),
            "recommended_actions": diagnostics_result.get("recommended_actions", []),
        },
        "trust": {
            "score": validation_result.get("trust_score", 0.0),
            "breakdown": validation_result.get("trust_breakdown", {}),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repo_audit_engine.cli",
        description="Phase 1 Python validation and diagnostics entrypoint.",
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
