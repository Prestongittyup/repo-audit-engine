from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from repo_audit_engine.io.artifacts import write_json
from repo_audit_engine.manifest.builder import DEFAULT_SKIP_DIRS
from repo_audit_engine.runtime.scenario_runner import encode_scenario_entrypoint
from repo_audit_engine.runtime.sandbox import copy_repo_to_sandbox


def _node_id_from_event(file_path: str, function_name: str) -> str:
    normalized_file = str(file_path or "").strip().replace("\\", "/")
    if normalized_file.startswith("./"):
        normalized_file = normalized_file[2:]
    normalized_function = str(function_name or "").strip()

    if not normalized_file or normalized_file.startswith("<"):
        return ""
    if normalized_function and normalized_function != "<module>":
        return f"function:{normalized_file}:{normalized_function}"
    return f"file:{normalized_file}"


def _node_id_from_label(label: str) -> str:
    raw = str(label or "").strip()
    if not raw or raw == "<entrypoint>":
        return ""

    if raw.startswith(("file:", "function:", "class:")):
        return raw

    if ":" not in raw:
        return ""

    path, _, function = raw.rpartition(":")
    if not path:
        return ""

    return _node_id_from_event(path, function)


def _is_scenario_entrypoint(entrypoint: str) -> bool:
    normalized = str(entrypoint or "").strip()
    return normalized.startswith("scenario:")


def _normalize_path(value: Any) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _module_name_from_path(path: str) -> str:
    normalized = _normalize_path(path)
    if not normalized.endswith(".py"):
        return ""

    without_suffix = normalized[:-3]
    if without_suffix.endswith("/__init__"):
        without_suffix = without_suffix[: -len("/__init__")]

    return without_suffix.replace("/", ".").strip(".")


def _decode_auto_scenario_entrypoint(entrypoint: str) -> Dict[str, Any]:
    token = str(entrypoint or "").strip()
    if not token.startswith("scenario:auto:"):
        return {}

    encoded = token.split("scenario:auto:", 1)[1].strip()
    if not encoded:
        return {}

    padding = "=" * (-len(encoded) % 4)
    try:
        raw = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, json.JSONDecodeError):
        return {}

    return payload if isinstance(payload, dict) else {}


def _entrypoint_path_hint(entrypoint: str) -> str:
    value = str(entrypoint or "").strip()
    if not value:
        return ""
    if value.endswith(".py"):
        return _normalize_path(value)

    scenario_spec = _decode_auto_scenario_entrypoint(value)
    return _normalize_path(scenario_spec.get("path", ""))


def _classify_run_result(status: str, call_count: int, import_count: int, line_count: int) -> str:
    normalized_status = str(status or "").strip().lower()

    if normalized_status == "timeout":
        return "TIMEOUT"
    if normalized_status != "ok":
        return "CRASHED"
    if int(call_count) > 0:
        return "CALL_ACTIVITY"
    if int(import_count) > 0:
        return "IMPORT_ONLY"
    if int(line_count) > 0:
        return "NO_OP"
    return "NO_CALL_ACTIVITY"


def _dedupe_preserve_order(values: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for item in values:
        normalized = str(item).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _parse_node_id(node_id: str) -> Tuple[str, str, str]:
    raw = str(node_id or "").strip()
    if not raw:
        return "", "", ""

    if raw.startswith("canonical://"):
        payload = raw[len("canonical://") :]
        kind, sep, remainder = payload.partition("/")
        if not sep:
            return "", "", ""

        normalized_kind = str(kind).strip().lower()
        if normalized_kind == "file":
            return "file", _normalize_path(remainder), ""

        rel_path, rel_sep, name = remainder.rpartition("/")
        if not rel_sep:
            return normalized_kind, _normalize_path(remainder), ""

        return normalized_kind, _normalize_path(rel_path), str(name).strip()

    if raw.startswith(("function:", "class:", "file:")):
        kind, payload = raw.split(":", 1)
        normalized_kind = str(kind).strip().lower()

        if normalized_kind == "file":
            return "file", _normalize_path(payload), ""

        rel_path, rel_sep, name = str(payload).rpartition(":")
        if not rel_sep:
            return normalized_kind, _normalize_path(payload), ""

        return normalized_kind, _normalize_path(rel_path), str(name).strip()

    return "", "", ""


def _safe_probe_token(value: str) -> str:
    token = str(value or "").strip().replace("canonical://", "").replace("/", "_").replace(":", "_").replace(".", "_")
    return token or "probe"


def _build_probe_entrypoint_from_node(node_id: str) -> str:
    kind, path, name = _parse_node_id(node_id)
    if kind not in {"file", "module", "function", "class"}:
        return ""

    module_name = _module_name_from_path(path)
    if not module_name:
        return ""

    spec: Dict[str, Any] = {
        "scenario_id": f"followup-{_safe_probe_token(node_id)}",
        "kind": "module" if kind in {"file", "module"} else kind,
        "path": path,
        "module": module_name,
        "seed_reason": "runtime_continuation",
        "node_id": str(node_id).strip(),
    }
    if kind in {"function", "class"} and name:
        spec["name"] = name

    return encode_scenario_entrypoint(spec)


def _priority_for_node(
    node_id: str,
    node_hits: Mapping[str, int],
    node_metrics: Mapping[str, Mapping[str, Any]],
    depth: int,
) -> float:
    metric_payload = node_metrics.get(str(node_id).strip()) if isinstance(node_metrics, Mapping) else {}
    metric = metric_payload if isinstance(metric_payload, Mapping) else {}

    inbound_edges = int(metric.get("inbound_edges", 0) or 0)
    outbound_edges = int(metric.get("outbound_edges", 0) or 0)
    graph_centrality = float(metric.get("graph_centrality", float(inbound_edges + outbound_edges)) or float(inbound_edges + outbound_edges))
    low_inbound_score = float(metric.get("low_inbound_score", 1.0 / (1.0 + float(inbound_edges))) or (1.0 / (1.0 + float(inbound_edges))))
    unexplored_neighbors = int(metric.get("unexplored_neighbors", 0) or 0)

    no_runtime_hits = 1 if int(node_hits.get(str(node_id).strip(), 0) or 0) <= 0 else 0

    priority = (
        (float(no_runtime_hits) * 120.0)
        + (graph_centrality * 6.0)
        + (low_inbound_score * 25.0)
        + (float(unexplored_neighbors) * 12.0)
        - (max(0, int(depth) - 1) * 4.0)
    )

    return round(float(priority), 3)


def _is_callable_node_id(node_id: str) -> bool:
    normalized = str(node_id or "").strip()
    if not normalized:
        return False
    return normalized.startswith((
        "function:",
        "class:",
        "canonical://function/",
        "canonical://class/",
    ))


def _enqueue_run(
    run_queue: List[Dict[str, Any]],
    queued_entrypoints: Set[str],
    attempted_entrypoints: Set[str],
    entrypoint: str,
    priority: float,
    depth: int,
    source: str,
    source_node: str,
    reason: str,
    max_pending_queue: int,
) -> bool:
    normalized_entrypoint = str(entrypoint or "").strip()
    if not normalized_entrypoint:
        return False

    if normalized_entrypoint in queued_entrypoints:
        return False
    if normalized_entrypoint in attempted_entrypoints:
        return False
    if len(run_queue) >= max_pending_queue:
        return False

    run_queue.append(
        {
            "entrypoint": normalized_entrypoint,
            "priority": float(priority),
            "depth": max(1, int(depth)),
            "source": str(source or "unknown").strip() or "unknown",
            "source_node": str(source_node or "").strip(),
            "reason": str(reason or "").strip(),
        }
    )
    queued_entrypoints.add(normalized_entrypoint)
    return True


def _pop_next_run(run_queue: List[Dict[str, Any]], queued_entrypoints: Set[str]) -> Dict[str, Any] | None:
    if not run_queue:
        return None

    run_queue.sort(
        key=lambda item: (
            -float(item.get("priority", 0.0) or 0.0),
            int(item.get("depth", 1) or 1),
            str(item.get("entrypoint", "")),
        )
    )
    selected = run_queue.pop(0)
    queued_entrypoints.discard(str(selected.get("entrypoint", "")).strip())
    return selected


def _probe_row_for_node(node_probe_map: Mapping[str, Any], node_id: str) -> Dict[str, Any]:
    key = str(node_id or "").strip()
    if not key:
        return {}

    payload = node_probe_map.get(key) if isinstance(node_probe_map, Mapping) else None
    if isinstance(payload, str):
        normalized = str(payload).strip()
        if not normalized:
            return {}
        return {"entrypoint": normalized, "node_id": key, "priority_score": 0.0}

    if isinstance(payload, Mapping):
        row = dict(payload)
        row["node_id"] = key
        row["entrypoint"] = str(row.get("entrypoint", "")).strip()
        return row

    return {}


def _timeout_for_depth(
    depth: int,
    timeout_seconds: int,
    max_entrypoint_seconds: int,
) -> int:
    normalized_depth = max(1, int(depth))
    max_timeout = max(1, int(timeout_seconds))
    base_timeout = max(1, int(max_entrypoint_seconds))

    if normalized_depth <= 1:
        return min(max_timeout, base_timeout)
    if normalized_depth == 2:
        return min(max_timeout, max(1, base_timeout))
    return min(max_timeout, base_timeout)


def execute_runtime_bubble(
    repo_path: Path,
    output_dir: Path,
    entrypoints: Sequence[str],
    bubble_mode: bool,
    timeout_seconds: int = 30,
    memory_cap_mb: int = 256,
    max_events: int = 20000,
    max_depth: int = 120,
    total_node_count: int = 0,
    baseline_runtime_hit_nodes: Sequence[str] | None = None,
    coverage_stop_threshold: float = 0.25,
    max_runtime_seconds: int = 45,
    max_events_per_scenario: int = 1500,
    max_entrypoint_seconds: int = 3,
    runtime_plan: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    trace_jsonl_path = out_root / "runtime_trace.jsonl"
    flow_graph_path = out_root / "execution_flow_graph.json"

    normalized_entrypoints = _dedupe_preserve_order([str(item).strip() for item in entrypoints if str(item).strip()])

    if not bubble_mode:
        trace_jsonl_path.write_text("", encoding="utf-8")
        flow_payload = {
            "bubble_mode": False,
            "entrypoint_runs": [],
            "nodes": [],
            "edges": [],
            "node_hits": {},
            "module_hits": {},
            "summary": {
                "run_count": 0,
                "call_event_count": 0,
                "import_event_count": 0,
                "timeout_count": 0,
                "line_event_count": 0,
                "call_activity_runs": 0,
                "import_only_runs": 0,
                "no_op_runs": 0,
                "no_call_activity_runs": 0,
                "crashed_runs": 0,
                "partial_success_runs": 0,
                "scenarios_executed": 0,
                "successful_scenarios": 0,
                "failed_scenarios": 0,
                "continuation_runs": 0,
                "import_followup_runs": 0,
                "forced_probe_runs": 0,
                "continuation_scheduled_targets": 0,
                "import_followup_scheduled_targets": 0,
                "forced_probe_scheduled_targets": 0,
                "new_nodes_covered": 0,
                "coverage_delta": 0.0,
                "coverage_ratio": 0.0,
                "baseline_coverage_ratio": 0.0,
                "coverage_stop_threshold": 0.0,
                "max_entrypoint_seconds": 0,
                "stop_reason": "bubble_mode_disabled",
            },
        }
        write_json(flow_graph_path, flow_payload, pretty=True)
        return {
            "trace_path": str(trace_jsonl_path),
            "flow_graph_path": str(flow_graph_path),
            "flow_graph": flow_payload,
        }

    plan_payload = runtime_plan if isinstance(runtime_plan, Mapping) else {}
    plan_summary_payload = plan_payload.get("summary") if isinstance(plan_payload.get("summary"), Mapping) else {}

    seed_entrypoints = plan_payload.get("seed_entrypoints") if isinstance(plan_payload.get("seed_entrypoints"), list) else []
    normalized_seed_entrypoints = _dedupe_preserve_order(
        [str(item).strip() for item in seed_entrypoints if str(item).strip()]
    )
    if not normalized_seed_entrypoints:
        normalized_seed_entrypoints = list(normalized_entrypoints)

    forced_probe_rows_payload = plan_payload.get("forced_probe_rows")
    forced_probe_rows: List[Dict[str, Any]] = []
    if isinstance(forced_probe_rows_payload, list):
        for item in forced_probe_rows_payload:
            if not isinstance(item, Mapping):
                continue
            row = dict(item)
            row["entrypoint"] = str(row.get("entrypoint", "")).strip()
            row["node_id"] = str(row.get("node_id", "")).strip()
            forced_probe_rows.append(row)
    else:
        forced_probe_entrypoints = plan_payload.get("forced_probes") if isinstance(plan_payload.get("forced_probes"), list) else []
        for entrypoint in forced_probe_entrypoints:
            normalized = str(entrypoint).strip()
            if not normalized:
                continue
            forced_probe_rows.append({"entrypoint": normalized, "node_id": "", "priority_score": 0.0})

    node_probe_map = plan_payload.get("node_probe_map") if isinstance(plan_payload.get("node_probe_map"), Mapping) else {}
    path_probe_map_payload = plan_payload.get("path_probe_map") if isinstance(plan_payload.get("path_probe_map"), Mapping) else {}
    module_to_paths_payload = plan_payload.get("module_to_paths") if isinstance(plan_payload.get("module_to_paths"), Mapping) else {}
    call_adjacency_payload = plan_payload.get("call_adjacency") if isinstance(plan_payload.get("call_adjacency"), Mapping) else {}
    node_metrics_payload = plan_payload.get("node_metrics") if isinstance(plan_payload.get("node_metrics"), Mapping) else {}

    path_probe_map: Dict[str, List[Dict[str, Any]]] = {}
    for key, value in path_probe_map_payload.items():
        path = _normalize_path(key)
        if not path:
            continue
        rows: List[Dict[str, Any]] = []
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, Mapping):
                    continue
                row = dict(item)
                row["entrypoint"] = str(row.get("entrypoint", "")).strip()
                row["node_id"] = str(row.get("node_id", "")).strip()
                rows.append(row)
        path_probe_map[path] = rows

    module_to_paths: Dict[str, List[str]] = {}
    for module_name, paths in module_to_paths_payload.items():
        normalized_module = str(module_name).strip()
        if not normalized_module:
            continue
        if isinstance(paths, list):
            normalized_paths = _dedupe_preserve_order([_normalize_path(item) for item in paths if _normalize_path(item)])
            module_to_paths[normalized_module] = normalized_paths

    call_adjacency: Dict[str, List[str]] = {}
    for source, targets in call_adjacency_payload.items():
        source_id = str(source).strip()
        if not source_id:
            continue
        if isinstance(targets, list):
            call_adjacency[source_id] = _dedupe_preserve_order([str(item).strip() for item in targets if str(item).strip()])

    node_metrics: Dict[str, Dict[str, Any]] = {}
    for node_id, metric in node_metrics_payload.items():
        normalized_node = str(node_id).strip()
        if not normalized_node or not isinstance(metric, Mapping):
            continue
        node_metrics[normalized_node] = dict(metric)

    node_hits: Dict[str, int] = {}
    module_hits: Dict[str, int] = {}
    edge_set: Set[Tuple[str, str, str]] = set()

    run_records: List[Dict[str, Any]] = []
    call_event_count = 0
    import_event_count = 0
    line_event_count = 0
    timeout_count = 0

    baseline_hit_nodes = {
        str(item).strip()
        for item in (baseline_runtime_hit_nodes or [])
        if str(item).strip()
    }
    baseline_hit_node_count = len(baseline_hit_nodes)

    normalized_total_node_count = max(0, int(total_node_count))
    normalized_coverage_stop_threshold = max(0.0, min(1.0, float(coverage_stop_threshold)))
    normalized_max_runtime_seconds = max(10, int(max_runtime_seconds))
    normalized_max_events_per_scenario = max(100, min(int(max_events), int(max_events_per_scenario)))
    normalized_max_entrypoint_seconds = max(1, min(int(timeout_seconds), int(max_entrypoint_seconds)))

    max_runs_by_budget = max(
        1,
        int(normalized_max_runtime_seconds // normalized_max_entrypoint_seconds),
    )

    max_expansion_depth = max(2, min(4, int(plan_summary_payload.get("max_expansion_depth", 3) or 3)))
    max_followups_per_node = max(1, min(4, int(plan_summary_payload.get("max_followups_per_node", 2) or 2)))
    max_pending_queue = max(8, int(max_runs_by_budget) * 4)
    max_forced_probe_schedules = max(
        0,
        min(len(forced_probe_rows), int(plan_summary_payload.get("max_forced_probe_runs", max_runs_by_budget) or max_runs_by_budget)),
    )

    scenarios_executed = 0
    successful_scenarios = 0
    failed_scenarios = 0
    call_activity_runs = 0
    import_only_runs = 0
    no_op_runs = 0
    no_call_activity_runs = 0
    crashed_runs = 0
    partial_success_runs = 0
    import_heavy_runs = 0

    continuation_runs = 0
    import_followup_runs = 0
    forced_probe_runs = 0

    continuation_scheduled_targets = 0
    import_followup_scheduled_targets = 0
    forced_probe_scheduled_targets = 0

    stop_reason = "queue_exhausted"

    run_queue: List[Dict[str, Any]] = []
    queued_entrypoints: Set[str] = set()
    attempted_entrypoints: Set[str] = set()

    for index, entrypoint in enumerate(normalized_seed_entrypoints):
        _enqueue_run(
            run_queue=run_queue,
            queued_entrypoints=queued_entrypoints,
            attempted_entrypoints=attempted_entrypoints,
            entrypoint=entrypoint,
            priority=1000.0 - float(index),
            depth=1,
            source="seed_entrypoint",
            source_node="",
            reason="focused_seed",
            max_pending_queue=max_pending_queue,
        )

    forced_probe_cursor = 0
    node_followup_counts: Dict[str, int] = {}

    run_started_at = perf_counter()

    with trace_jsonl_path.open("w", encoding="utf-8") as trace_handle:
        while True:
            elapsed_seconds = perf_counter() - run_started_at
            if elapsed_seconds >= float(normalized_max_runtime_seconds):
                stop_reason = "max_runtime_seconds_exceeded"
                break

            if len(run_records) >= max_runs_by_budget:
                stop_reason = "runtime_budget_reached"
                break

            while (
                len(run_queue) < 3
                and forced_probe_cursor < len(forced_probe_rows)
                and forced_probe_scheduled_targets < max_forced_probe_schedules
            ):
                probe_row = forced_probe_rows[forced_probe_cursor]
                forced_probe_cursor += 1
                probe_entrypoint = str(probe_row.get("entrypoint", "")).strip()
                probe_node_id = str(probe_row.get("node_id", "")).strip()
                if not probe_entrypoint:
                    continue

                probe_priority = max(
                    60.0,
                    float(probe_row.get("priority_score", 0.0) or 0.0),
                )
                if probe_node_id:
                    probe_priority = max(
                        probe_priority,
                        _priority_for_node(
                            node_id=probe_node_id,
                            node_hits=node_hits,
                            node_metrics=node_metrics,
                            depth=2,
                        ) + 10.0,
                    )

                queued = _enqueue_run(
                    run_queue=run_queue,
                    queued_entrypoints=queued_entrypoints,
                    attempted_entrypoints=attempted_entrypoints,
                    entrypoint=probe_entrypoint,
                    priority=probe_priority,
                    depth=min(max_expansion_depth, 2),
                    source="forced_probe",
                    source_node=probe_node_id,
                    reason="coverage_gap_probe",
                    max_pending_queue=max_pending_queue,
                )
                if queued:
                    forced_probe_scheduled_targets += 1

            selected_run = _pop_next_run(run_queue, queued_entrypoints)
            if not selected_run:
                stop_reason = "queue_exhausted"
                break

            entrypoint = str(selected_run.get("entrypoint", "")).strip()
            if not entrypoint:
                continue
            if entrypoint in attempted_entrypoints:
                continue

            attempted_entrypoints.add(entrypoint)

            schedule_depth = max(1, int(selected_run.get("depth", 1) or 1))
            schedule_source = str(selected_run.get("source", "unknown")).strip() or "unknown"
            schedule_source_node = str(selected_run.get("source_node", "")).strip()
            schedule_priority = float(selected_run.get("priority", 0.0) or 0.0)
            schedule_reason = str(selected_run.get("reason", "")).strip()

            if schedule_source == "continuation":
                continuation_runs += 1
            elif schedule_source == "import_followup":
                import_followup_runs += 1
            elif schedule_source == "forced_probe":
                forced_probe_runs += 1

            run_id = f"run_{len(run_records) + 1:03d}"
            is_scenario = _is_scenario_entrypoint(entrypoint)

            if is_scenario:
                scenarios_executed += 1

            trace_summary: Dict[str, Any] = {
                "entrypoint": entrypoint,
                "status": "error",
                "error": "trace_summary_missing",
                "imports": [],
                "executed_modules": [],
                "event_counts": {"call": 0, "import": 0, "return": 0, "line": 0},
                "runtime_seconds": 0.0,
            }

            run_call_count = 0
            run_module_call_count = 0
            run_import_count = 0
            run_line_count = 0
            run_nodes_seen: Set[str] = set()
            run_paths_seen: Set[str] = set()
            run_modules_seen: Set[str] = set()
            events_file: Path | None = None
            persisted_events_file: Path | None = None
            run_timeout_seconds = _timeout_for_depth(
                depth=schedule_depth,
                timeout_seconds=int(timeout_seconds),
                max_entrypoint_seconds=normalized_max_entrypoint_seconds,
            )

            with tempfile.TemporaryDirectory(prefix="bubble_", dir=str(out_root)) as temp_dir:
                sandbox_root = Path(temp_dir).resolve()
                ignore_dirs = set(DEFAULT_SKIP_DIRS)
                ignore_dirs.add("__pycache__")

                sandbox_repo = copy_repo_to_sandbox(repo_path.resolve(), sandbox_root, sorted(ignore_dirs))
                summary_file = sandbox_root / "trace_summary.json"
                events_file = sandbox_root / "trace_events.jsonl"
                tracer_script = Path(__file__).resolve().with_name("tracer.py")

                command = [
                    sys.executable,
                    str(tracer_script),
                    "--repo",
                    str(sandbox_repo),
                    "--entrypoint",
                    entrypoint,
                    "--output",
                    str(summary_file),
                    "--events-output",
                    str(events_file),
                    "--memory-cap-mb",
                    str(int(memory_cap_mb)),
                    "--max-events",
                    str(
                        int(normalized_max_events_per_scenario)
                        if is_scenario
                        else int(max_events)
                    ),
                    "--max-depth",
                    str(int(max_depth)),
                ]

                try:
                    completed = subprocess.run(
                        command,
                        cwd=str(sandbox_repo),
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=run_timeout_seconds,
                    )

                    if summary_file.exists():
                        try:
                            loaded = json.loads(summary_file.read_text(encoding="utf-8", errors="replace"))
                            if isinstance(loaded, dict):
                                trace_summary = loaded
                        except json.JSONDecodeError:
                            trace_summary["error"] = "invalid_trace_json"

                    if completed.returncode != 0 and not str(trace_summary.get("error", "")).strip():
                        trace_summary["error"] = str(completed.stderr or completed.stdout or "bubble_worker_failed").strip()
                        trace_summary["status"] = "error"

                except subprocess.TimeoutExpired:
                    timeout_count += 1
                    trace_summary = {
                        "entrypoint": entrypoint,
                        "status": "timeout",
                        "error": "timeout_exceeded",
                        "imports": [],
                        "executed_modules": [],
                        "event_counts": {"call": 0, "import": 0, "return": 0, "line": 0},
                        "runtime_seconds": float(run_timeout_seconds),
                    }

                if events_file.exists():
                    persisted_events_file = out_root / f"{run_id}_trace_events.jsonl"
                    shutil.copyfile(events_file, persisted_events_file)

            executed_modules = trace_summary.get("executed_modules") if isinstance(trace_summary.get("executed_modules"), list) else []
            imports = trace_summary.get("imports") if isinstance(trace_summary.get("imports"), list) else []

            for module_name in executed_modules:
                module_text = str(module_name).strip()
                if not module_text:
                    continue
                module_hits[module_text] = module_hits.get(module_text, 0) + 1
                run_modules_seen.add(module_text)

            for module_name in imports:
                module_text = str(module_name).strip()
                if module_text:
                    run_modules_seen.add(module_text)

            if persisted_events_file and persisted_events_file.exists():
                with persisted_events_file.open("r", encoding="utf-8", errors="replace") as handle:
                    for raw_line in handle:
                        line = raw_line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = event if isinstance(event, dict) else {}
                        event_type = str(payload.get("event", "")).strip().lower()

                        rel_file = _normalize_path(payload.get("file", ""))
                        function_name = str(payload.get("function", "")).strip()
                        module_name = str(payload.get("module", "")).strip()
                        caller_label = str(payload.get("caller", "")).strip()
                        caller_node_id = str(payload.get("caller_node_id", "")).strip()
                        callee_node_id = str(payload.get("callee_node_id", payload.get("node", ""))).strip()

                        if rel_file:
                            run_paths_seen.add(rel_file)
                        if module_name:
                            run_modules_seen.add(module_name)

                        trace_row = {
                            "run_id": run_id,
                            "entrypoint": entrypoint,
                            "event": event_type,
                            "timestamp": str(payload.get("timestamp", "")),
                            "module": module_name,
                            "file": rel_file,
                            "function": function_name,
                            "line": int(payload.get("line", 0) or 0),
                            "depth": int(payload.get("depth", 0) or 0),
                            "caller": caller_label,
                            "node": str(payload.get("node", "")),
                            "caller_node_id": caller_node_id,
                            "callee_node_id": callee_node_id,
                        }
                        trace_handle.write(json.dumps(trace_row, ensure_ascii=True, sort_keys=True) + "\n")

                        if event_type == "import":
                            import_event_count += 1
                            run_import_count += 1
                            continue

                        if event_type == "line":
                            line_event_count += 1
                            run_line_count += 1
                            continue

                        if event_type != "call":
                            continue

                        if function_name in {"<module>", "<import>"}:
                            run_module_call_count += 1

                        node_id = callee_node_id or _node_id_from_event(rel_file, function_name)
                        if node_id:
                            run_nodes_seen.add(node_id)
                            node_hits[node_id] = node_hits.get(node_id, 0) + 1

                        source_id = caller_node_id or _node_id_from_label(caller_label)
                        if source_id and node_id:
                            edge_set.add((source_id, node_id, "RUNTIME_CALL"))

                        call_event_count += 1
                        run_call_count += 1

            event_counts = trace_summary.get("event_counts") if isinstance(trace_summary.get("event_counts"), dict) else {}
            if run_call_count == 0:
                run_call_count = int(event_counts.get("call", 0) or 0)
                call_event_count += run_call_count
            if run_import_count == 0:
                run_import_count = int(event_counts.get("import", 0) or 0)
                import_event_count += run_import_count
            if run_line_count == 0:
                run_line_count = int(event_counts.get("line", 0) or 0)
                line_event_count += run_line_count

            run_effective_call_count = max(0, int(run_call_count) - int(run_module_call_count))
            partial_trace_committed = bool(
                str(trace_summary.get("status", "")).strip().lower() == "timeout"
                and persisted_events_file is not None
                and persisted_events_file.exists()
            )

            if not imports and run_import_count > 0:
                trace_handle.write(
                    json.dumps(
                        {
                            "run_id": run_id,
                            "entrypoint": entrypoint,
                            "event": "import",
                            "module": "<aggregated>",
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                    )
                    + "\n"
                )

            run_status = str(trace_summary.get("status", "error"))
            run_result = _classify_run_result(
                status=run_status,
                call_count=run_effective_call_count,
                import_count=run_import_count,
                line_count=run_line_count,
            )
            if run_result == "TIMEOUT" and (run_effective_call_count > 0 or run_import_count > 0 or run_line_count > 0):
                run_result = "PARTIAL_SUCCESS"

            is_import_heavy = bool(run_effective_call_count <= 0 and run_import_count > 0)
            if is_import_heavy:
                import_heavy_runs += 1

            scheduled_followups = 0

            if schedule_depth < max_expansion_depth and run_nodes_seen:
                next_depth = min(max_expansion_depth, schedule_depth + 1)
                for source_node in sorted(run_nodes_seen):
                    if not _is_callable_node_id(source_node):
                        continue

                    followup_attempts = int(node_followup_counts.get(source_node, 0) or 0)
                    if followup_attempts >= max_followups_per_node:
                        continue

                    neighbor_nodes = [str(item).strip() for item in call_adjacency.get(source_node, []) if str(item).strip()]
                    if not neighbor_nodes:
                        neighbor_nodes = [source_node]

                    for target_node in neighbor_nodes:
                        if followup_attempts >= max_followups_per_node:
                            break

                        probe_row = _probe_row_for_node(node_probe_map, target_node)
                        probe_entrypoint = str(probe_row.get("entrypoint", "")).strip() if probe_row else ""
                        if not probe_entrypoint:
                            probe_entrypoint = _build_probe_entrypoint_from_node(target_node)
                        if not probe_entrypoint:
                            continue

                        probe_priority = _priority_for_node(
                            node_id=target_node,
                            node_hits=node_hits,
                            node_metrics=node_metrics,
                            depth=next_depth,
                        )

                        queued = _enqueue_run(
                            run_queue=run_queue,
                            queued_entrypoints=queued_entrypoints,
                            attempted_entrypoints=attempted_entrypoints,
                            entrypoint=probe_entrypoint,
                            priority=probe_priority,
                            depth=next_depth,
                            source="continuation",
                            source_node=source_node,
                            reason="node_hit_not_fully_explored",
                            max_pending_queue=max_pending_queue,
                        )
                        if queued:
                            scheduled_followups += 1
                            continuation_scheduled_targets += 1
                            followup_attempts += 1

                    node_followup_counts[source_node] = followup_attempts

            if (
                run_result in {"IMPORT_ONLY", "NO_CALL_ACTIVITY", "PARTIAL_SUCCESS"}
                and run_effective_call_count <= 0
                and is_import_heavy
            ):
                candidate_paths: Set[str] = set(run_paths_seen)
                hinted_path = _entrypoint_path_hint(entrypoint)
                if hinted_path:
                    candidate_paths.add(hinted_path)

                for module_name in run_modules_seen:
                    for rel_path in module_to_paths.get(module_name, []):
                        normalized_path = _normalize_path(rel_path)
                        if normalized_path:
                            candidate_paths.add(normalized_path)

                for rel_path in sorted(candidate_paths):
                    probe_rows = path_probe_map.get(rel_path, [])
                    if not isinstance(probe_rows, list) or not probe_rows:
                        continue

                    ordered_probe_rows = sorted(
                        probe_rows,
                        key=lambda row: (
                            0 if str(row.get("kind", "")).strip() in {"function", "class"} else 1,
                            -float(row.get("priority_score", 0.0) or 0.0),
                            str(row.get("entrypoint", "")),
                        ),
                    )

                    scheduled_for_path = 0
                    for probe_row in ordered_probe_rows:
                        if scheduled_for_path >= 2:
                            break

                        probe_entrypoint = str(probe_row.get("entrypoint", "")).strip()
                        if not probe_entrypoint:
                            continue
                        probe_node_id = str(probe_row.get("node_id", "")).strip()
                        probe_priority = _priority_for_node(
                            node_id=probe_node_id,
                            node_hits=node_hits,
                            node_metrics=node_metrics,
                            depth=min(max_expansion_depth, schedule_depth + 1),
                        ) + 15.0

                        queued = _enqueue_run(
                            run_queue=run_queue,
                            queued_entrypoints=queued_entrypoints,
                            attempted_entrypoints=attempted_entrypoints,
                            entrypoint=probe_entrypoint,
                            priority=probe_priority,
                            depth=min(max_expansion_depth, schedule_depth + 1),
                            source="import_followup",
                            source_node=probe_node_id,
                            reason="import_only_probe",
                            max_pending_queue=max_pending_queue,
                        )
                        if queued:
                            scheduled_followups += 1
                            import_followup_scheduled_targets += 1
                            scheduled_for_path += 1

            if (
                call_activity_runs <= 0
                and forced_probe_cursor < len(forced_probe_rows)
                and forced_probe_scheduled_targets < max_forced_probe_schedules
            ):
                probe_row = forced_probe_rows[forced_probe_cursor]
                forced_probe_cursor += 1
                probe_entrypoint = str(probe_row.get("entrypoint", "")).strip()
                probe_node_id = str(probe_row.get("node_id", "")).strip()
                if probe_entrypoint:
                    probe_priority = max(
                        220.0,
                        _priority_for_node(
                            node_id=probe_node_id,
                            node_hits=node_hits,
                            node_metrics=node_metrics,
                            depth=min(max_expansion_depth, schedule_depth + 1),
                        ),
                    )
                    queued = _enqueue_run(
                        run_queue=run_queue,
                        queued_entrypoints=queued_entrypoints,
                        attempted_entrypoints=attempted_entrypoints,
                        entrypoint=probe_entrypoint,
                        priority=probe_priority,
                        depth=min(max_expansion_depth, schedule_depth + 1),
                        source="forced_probe",
                        source_node=probe_node_id,
                        reason="import_heavy_forced_probe",
                        max_pending_queue=max_pending_queue,
                    )
                    if queued:
                        forced_probe_scheduled_targets += 1
                        scheduled_followups += 1

            run_records.append(
                {
                    "run_id": run_id,
                    "entrypoint": entrypoint,
                    "status": run_status,
                    "error": str(trace_summary.get("error", "")),
                    "runtime_seconds": float(trace_summary.get("runtime_seconds", 0.0) or 0.0),
                    "import_count": run_import_count,
                    "call_count": run_call_count,
                    "effective_call_count": run_effective_call_count,
                    "module_call_count": run_module_call_count,
                    "line_count": run_line_count,
                    "scenario_result": run_result,
                    "is_seed_scenario": bool(is_scenario),
                    "schedule_source": schedule_source,
                    "schedule_source_node": schedule_source_node,
                    "schedule_priority": round(schedule_priority, 3),
                    "schedule_depth": schedule_depth,
                    "schedule_reason": schedule_reason,
                    "partial_trace_committed": partial_trace_committed,
                    "is_import_heavy": is_import_heavy,
                    "scheduled_followups": scheduled_followups,
                }
            )

            if run_result == "CALL_ACTIVITY":
                call_activity_runs += 1
            elif run_result == "IMPORT_ONLY":
                import_only_runs += 1
            elif run_result == "NO_OP":
                no_op_runs += 1
            elif run_result == "NO_CALL_ACTIVITY":
                no_call_activity_runs += 1
            elif run_result == "CRASHED":
                crashed_runs += 1
            elif run_result == "PARTIAL_SUCCESS":
                partial_success_runs += 1

            if is_scenario:
                if run_result not in {"CRASHED", "TIMEOUT"}:
                    successful_scenarios += 1
                else:
                    failed_scenarios += 1

            if normalized_total_node_count > 0 and normalized_coverage_stop_threshold > 0:
                coverage_ratio = float(len(node_hits)) / float(max(1, normalized_total_node_count))
                if coverage_ratio >= normalized_coverage_stop_threshold:
                    stop_reason = "coverage_target_reached"
                    break

    final_coverage_ratio = 0.0
    baseline_coverage_ratio = 0.0
    if normalized_total_node_count > 0:
        final_coverage_ratio = float(len(node_hits)) / float(max(1, normalized_total_node_count))
        baseline_coverage_ratio = float(baseline_hit_node_count) / float(max(1, normalized_total_node_count))

    new_nodes_covered = len({node for node in node_hits if node not in baseline_hit_nodes})
    coverage_delta = final_coverage_ratio - baseline_coverage_ratio

    failed_run_count = len([row for row in run_records if str(row.get("scenario_result", "")) in {"CRASHED", "TIMEOUT"}])
    if failed_run_count > failed_scenarios:
        failed_scenarios = failed_run_count
    successful_scenarios = max(0, len(run_records) - failed_scenarios)

    scenario_warnings: List[str] = []
    if len(run_records) <= 0:
        scenario_warnings.append("No runtime entrypoints were executed.")
    if len(run_records) > 0 and coverage_delta <= 0.0:
        scenario_warnings.append("Runtime coverage did not increase versus baseline.")
    if len(run_records) > 0 and failed_scenarios > 0:
        scenario_warnings.append("One or more runtime entrypoint runs failed.")
    if len(run_records) > 0 and call_activity_runs <= 0:
        scenario_warnings.append("Runtime entrypoint runs produced no call activity.")
    if import_heavy_runs > max(0, call_activity_runs):
        scenario_warnings.append("Import-only runs dominate runtime execution; synthetic probes were scheduled.")
    if partial_success_runs > 0:
        scenario_warnings.append("Timeout traces were retained as partial success evidence.")
    if (
        len(run_records) > 0
        and normalized_coverage_stop_threshold > 0
        and final_coverage_ratio < normalized_coverage_stop_threshold
    ):
        scenario_warnings.append("Coverage target was not reached before runtime limits were hit.")

    node_rows = [
        {"id": node_id, "kind": "runtime_observed", "hit_count": count}
        for node_id, count in sorted(node_hits.items(), key=lambda item: item[0])
    ]

    edge_rows = [
        {"source": source, "target": target, "type": edge_type}
        for source, target, edge_type in sorted(edge_set, key=lambda item: (item[2], item[0], item[1]))
    ]

    flow_payload = {
        "bubble_mode": True,
        "entrypoint_runs": run_records,
        "nodes": node_rows,
        "edges": edge_rows,
        "node_hits": dict(sorted(node_hits.items(), key=lambda item: item[0])),
        "module_hits": dict(sorted(module_hits.items(), key=lambda item: item[0])),
        "summary": {
            "run_count": len(run_records),
            "call_event_count": call_event_count,
            "import_event_count": import_event_count,
            "line_event_count": line_event_count,
            "timeout_count": timeout_count,
            "call_activity_runs": call_activity_runs,
            "import_only_runs": import_only_runs,
            "import_heavy_runs": import_heavy_runs,
            "no_op_runs": no_op_runs,
            "no_call_activity_runs": no_call_activity_runs,
            "crashed_runs": crashed_runs,
            "partial_success_runs": partial_success_runs,
            "scenarios_executed": scenarios_executed,
            "successful_scenarios": successful_scenarios,
            "failed_scenarios": failed_scenarios,
            "continuation_runs": continuation_runs,
            "import_followup_runs": import_followup_runs,
            "forced_probe_runs": forced_probe_runs,
            "continuation_scheduled_targets": continuation_scheduled_targets,
            "import_followup_scheduled_targets": import_followup_scheduled_targets,
            "forced_probe_scheduled_targets": forced_probe_scheduled_targets,
            "new_nodes_covered": new_nodes_covered,
            "coverage_delta": round(float(coverage_delta), 6),
            "coverage_ratio": round(float(final_coverage_ratio), 6),
            "baseline_coverage_ratio": round(float(baseline_coverage_ratio), 6),
            "coverage_stop_threshold": round(float(normalized_coverage_stop_threshold), 3),
            "max_runtime_seconds": normalized_max_runtime_seconds,
            "max_events_per_scenario": normalized_max_events_per_scenario,
            "max_entrypoint_seconds": normalized_max_entrypoint_seconds,
            "max_runs_by_budget": max_runs_by_budget,
            "max_scenario_runs_by_budget": max_runs_by_budget,
            "max_expansion_depth": max_expansion_depth,
            "max_followups_per_node": max_followups_per_node,
            "stop_reason": stop_reason,
            "warnings": scenario_warnings,
        },
    }

    write_json(flow_graph_path, flow_payload, pretty=True)

    return {
        "trace_path": str(trace_jsonl_path),
        "flow_graph_path": str(flow_graph_path),
        "flow_graph": flow_payload,
    }
