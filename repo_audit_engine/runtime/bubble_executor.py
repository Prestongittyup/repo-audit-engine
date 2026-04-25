from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from repo_audit_engine.io.artifacts import write_json
from repo_audit_engine.manifest.builder import DEFAULT_SKIP_DIRS
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


def execute_runtime_bubble(
    repo_path: Path,
    output_dir: Path,
    entrypoints: Sequence[str],
    bubble_mode: bool,
    timeout_seconds: int = 30,
    memory_cap_mb: int = 256,
    max_events: int = 20000,
    max_depth: int = 120,
) -> Dict[str, Any]:
    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    trace_jsonl_path = out_root / "runtime_trace.jsonl"
    flow_graph_path = out_root / "execution_flow_graph.json"

    normalized_entrypoints = sorted({str(item).strip() for item in entrypoints if str(item).strip()})

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
            },
        }
        write_json(flow_graph_path, flow_payload, pretty=True)
        return {
            "trace_path": str(trace_jsonl_path),
            "flow_graph_path": str(flow_graph_path),
            "flow_graph": flow_payload,
        }

    node_hits: Dict[str, int] = {}
    module_hits: Dict[str, int] = {}
    edge_set: set[Tuple[str, str, str]] = set()

    run_records: List[Dict[str, Any]] = []
    call_event_count = 0
    import_event_count = 0
    line_event_count = 0
    timeout_count = 0

    with trace_jsonl_path.open("w", encoding="utf-8") as trace_handle:
        for run_index, entrypoint in enumerate(normalized_entrypoints, start=1):
            run_id = f"run_{run_index:03d}"
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
            run_import_count = 0
            run_line_count = 0
            events_file: Path | None = None
            persisted_events_file: Path | None = None

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
                    str(int(max_events)),
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
                        timeout=max(1, int(timeout_seconds)),
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
                        "runtime_seconds": float(timeout_seconds),
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

                        rel_file = str(payload.get("file", "")).strip().replace("\\", "/")
                        function_name = str(payload.get("function", "")).strip()
                        module_name = str(payload.get("module", "")).strip()
                        caller_label = str(payload.get("caller", "")).strip()
                        caller_node_id = str(payload.get("caller_node_id", "")).strip()
                        callee_node_id = str(payload.get("callee_node_id", payload.get("node", ""))).strip()

                        trace_row = {
                            "run_id": run_id,
                            "entrypoint": entrypoint,
                            "event": event_type,
                            "timestamp": str(payload.get("timestamp", "")),
                            "module": module_name,
                            "file": rel_file,
                            "function": function_name,
                            "line": int(payload.get("line", 0) or 0),
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

                        node_id = callee_node_id or _node_id_from_event(rel_file, function_name)
                        if node_id:
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

            run_records.append(
                {
                    "run_id": run_id,
                    "entrypoint": entrypoint,
                    "status": str(trace_summary.get("status", "error")),
                    "error": str(trace_summary.get("error", "")),
                    "runtime_seconds": float(trace_summary.get("runtime_seconds", 0.0) or 0.0),
                    "import_count": run_import_count,
                    "call_count": run_call_count,
                    "line_count": run_line_count,
                }
            )

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
        },
    }

    write_json(flow_graph_path, flow_payload, pretty=True)

    return {
        "trace_path": str(trace_jsonl_path),
        "flow_graph_path": str(flow_graph_path),
        "flow_graph": flow_payload,
    }
