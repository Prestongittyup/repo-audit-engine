from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from repo_audit_engine.io.artifacts import load_json, write_json


def build_dead_code_report(
    heat_payload: Dict[str, Any],
    output_dir: Path,
) -> Dict[str, Any]:
    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    report_path = out_root / "dead_code_report.json"

    rows = heat_payload.get("nodes") if isinstance(heat_payload.get("nodes"), list) else []
    runtime_validation = heat_payload.get("runtime_validation") if isinstance(heat_payload.get("runtime_validation"), dict) else {}
    coverage_ratio = float(runtime_validation.get("coverage_ratio", 0.0) or 0.0)
    coverage_ratio = max(0.0, min(1.0, coverage_ratio))

    candidates: List[Dict[str, Any]] = []
    for item in rows:
        payload = item if isinstance(item, dict) else {}
        node_id = str(payload.get("node_id", "")).strip()
        if not node_id:
            continue

        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}

        runtime_hits = int(payload.get("runtime_hits", evidence.get("runtime_hits", 0)) or 0)
        inbound_edges = int(payload.get("inbound_edges", 0) or 0)
        executable_references = int(
            payload.get("executable_references", evidence.get("executable_references", 0)) or 0
        )
        non_executable_references = int(
            payload.get("non_executable_references", evidence.get("non_executable_references", 0)) or 0
        )
        ast_references = int(
            payload.get("ast_references", executable_references + non_executable_references) or 0
        )

        classification = str(payload.get("classification", payload.get("heat", ""))).strip().upper()
        guardrails: List[str] = []

        if classification == "DEAD" and inbound_edges > 0:
            classification = "COLD"
            guardrails.append("dead_reclassified_to_cold_due_to_inbound_edges")

        probability = 0.0
        chain: List[str] = []

        if runtime_hits == 0:
            probability += 0.6
            chain.append("Rule +0.6: no runtime hits observed in bubble execution.")

        if executable_references == 0:
            probability += 0.3
            chain.append("Rule +0.3: no executable CALL references.")

        if non_executable_references == 0:
            probability += 0.1
            chain.append("Rule +0.1: no non-executable references.")

        probability = round(min(1.0, probability), 3)
        confidence = round(probability * coverage_ratio, 3)

        chain.append(
            f"Coverage scale: confidence = probability * coverage_ratio ({probability:.3f} * {coverage_ratio:.3f})."
        )

        candidates.append(
            {
                "node_id": node_id,
                "classification": classification,
                "heat": classification,
                "probability": probability,
                "confidence": confidence,
                "justification_chain": chain,
                "runtime_hits": runtime_hits,
                "inbound_edges": inbound_edges,
                "executable_references": executable_references,
                "non_executable_references": non_executable_references,
                "ast_references": ast_references,
                "guardrails": guardrails,
            }
        )

    candidates.sort(key=lambda item: (-float(item.get("probability", 0.0)), str(item.get("node_id", ""))))

    dead_candidates = [item for item in candidates if str(item.get("classification", "")).upper() == "DEAD"]

    payload = {
        "rule_weights": {
            "no_runtime_hits": 0.6,
            "no_executable_references": 0.3,
            "no_non_executable_references": 0.1,
        },
        "summary": {
            "node_count": len(candidates),
            "dead_candidate_count": len(dead_candidates),
            "coverage_ratio": round(coverage_ratio, 3),
            "high_confidence_count": len([item for item in dead_candidates if float(item.get("confidence", 0.0)) >= 0.9]),
        },
        "candidates": candidates,
        "dead_candidates": dead_candidates,
    }

    write_json(report_path, payload, pretty=True)

    return {
        "report_path": str(report_path),
        "report": payload,
    }


def build_dead_code_report_from_artifact(
    heat_path: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    heat_payload = load_json(heat_path)
    return build_dead_code_report(
        heat_payload=heat_payload if isinstance(heat_payload, dict) else {},
        output_dir=output_dir,
    )
