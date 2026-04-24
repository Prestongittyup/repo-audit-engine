from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_verification_runner(script_dir: Path):
    module_path = script_dir / "verification_runner.py"
    spec = importlib.util.spec_from_file_location("verification_runner", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load verification_runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run_verification


def _build_resolver_data(edges_doc: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    edges = edges_doc.get("edges", [])
    if not isinstance(edges, list):
        edges = []

    normalized_edges: List[Dict[str, Any]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue

        src = edge.get("from")
        dst = edge.get("to")
        edge_type = edge.get("type")
        source = edge.get("source")

        if not isinstance(src, str) or not isinstance(dst, str):
            continue

        normalized_edges.append(
            {
                "from": src.strip(),
                "to": dst.strip(),
                "type": str(edge_type or "").strip(),
                "source": str(source or "").strip(),
            }
        )

    normalized_edges.sort(key=lambda item: (item["from"], item["to"], item["type"], item["source"]))
    return {"edges": normalized_edges}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed:  # NaN guard
        return default
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Run policy authority decision from VerificationRunner output.")
    parser.add_argument("--graph-path", required=True)
    parser.add_argument("--edges-path", required=True)
    parser.add_argument("--validation-path", required=False)
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--min-trust", required=False, type=float, default=0.55)
    parser.add_argument("--entrypoint", action="append", default=[])
    args = parser.parse_args()

    graph_path = Path(args.graph_path).resolve()
    edges_path = Path(args.edges_path).resolve()
    output_path = Path(args.output_path).resolve()

    graph_doc = _load_json(graph_path)
    edges_doc = _load_json(edges_path)

    graph_payload = graph_doc.get("graph", graph_doc)
    if not isinstance(graph_payload, dict):
        raise RuntimeError("Invalid graph document: expected object with graph payload")

    entrypoints = [ep for ep in (args.entrypoint or []) if isinstance(ep, str) and ep.strip()]
    entrypoints = sorted(set(ep.strip() for ep in entrypoints))
    if not entrypoints:
        raise RuntimeError("Authority gate requires explicit --entrypoint arguments.")

    min_trust = max(0.0, min(1.0, float(args.min_trust)))

    resolver_data = _build_resolver_data(edges_doc)

    script_dir = Path(__file__).resolve().parent
    run_verification = _load_verification_runner(script_dir)

    verification_result = run_verification(
        graph_data={"graph": graph_payload},
        resolver_data=resolver_data,
        entrypoints=entrypoints,
    )

    trust_block = verification_result.get("trust", {})
    if not isinstance(trust_block, dict):
        trust_block = {}

    policy_decision = verification_result.get("policy_decision", {})
    if not isinstance(policy_decision, dict):
        policy_decision = {}

    trust_score = _safe_float(
        verification_result.get("trust_score", trust_block.get("trust_score", 0.0)),
        default=0.0,
    )

    critical_failure = bool(
        policy_decision.get("critical_failure", verification_result.get("critical_failure", False))
    )

    authority_valid = bool((not critical_failure) and trust_score >= min_trust)

    output_doc: Dict[str, Any] = {
        "authority": "VERIFICATION_RUNNER",
        "authority_valid": authority_valid,
        "policy": {
            "min_trust": min_trust,
            "require_no_critical_failure": True,
            "decision_model": "fact_policy_split",
            "evaluated_utc": datetime.now(timezone.utc).isoformat(),
        },
        "entrypoints_used": entrypoints,
        "trust_score": trust_score,
        "critical_failure": critical_failure,
        "failure_domains": verification_result.get("failure_domains", []),
        "issues": verification_result.get("issues", []),
        "warnings": verification_result.get("warnings", []),
        "recommendations": verification_result.get("recommendations", []),
        "penalties": trust_block.get("penalties", {}),
        "failure_analysis": verification_result.get("failure_analysis", {}),
        "policy_decision": policy_decision,
        "verification": verification_result,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output_doc, f, indent=2)

    return 0 if authority_valid else 1


if __name__ == "__main__":
    sys.exit(main())
