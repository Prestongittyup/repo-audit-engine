from __future__ import annotations

import argparse
import builtins
import importlib
import json
import runpy
import sys
import tempfile
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Set

if __package__ is None or __package__ == "":
    # Support direct script execution from bubble subprocesses.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _frame_depth(frame) -> int:
    depth = 0
    cursor = frame
    while cursor is not None:
        depth += 1
        cursor = cursor.f_back
    return depth


def _normalize_repo_file(repo_path: Path, filename: str) -> str:
    raw = str(filename or "").strip()
    if not raw:
        return ""

    # CPython runtime internals use pseudo filenames (e.g. <frozen importlib._bootstrap>).
    # These are not real repo files and must be excluded from runtime graph reconciliation.
    if raw.startswith("<") and raw.endswith(">"):
        return ""

    candidate = Path(raw)
    # Avoid repeated and expensive realpath resolution on every trace event.
    if candidate.is_absolute():
        path = candidate
    else:
        path = (repo_path / candidate)

    try:
        if not path.exists() or not path.is_file():
            return ""
    except OSError:
        return ""

    try:
        return path.relative_to(repo_path).as_posix()
    except ValueError:
        return ""


def _node_id(rel_file: str, function_name: str) -> str:
    normalized_file = str(rel_file or "").strip().replace("\\", "/")
    normalized_function = str(function_name or "").strip()

    if not normalized_file:
        return ""
    if normalized_function and normalized_function != "<module>":
        return f"function:{normalized_file}:{normalized_function}"
    return f"file:{normalized_file}"


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _run_entrypoint(repo_path: Path, entrypoint: str) -> None:
    if str(entrypoint).startswith("scenario:auto:"):
        encoded_spec = str(entrypoint).split("scenario:auto:", 1)[1].strip()
        from repo_audit_engine.runtime.scenario_runner import run_encoded_scenario

        scenario_result = run_encoded_scenario(repo_path=repo_path, encoded_spec=encoded_spec)
        if not bool(scenario_result.get("ok", False)):
            scenario_id = str(scenario_result.get("scenario_id", "unknown")).strip() or "unknown"
            error = str(scenario_result.get("error", "scenario_execution_failed")).strip()
            raise RuntimeError(f"scenario_failed:{scenario_id}:{error}")
        return

    if str(entrypoint).startswith("scenario:"):
        scenario_name = str(entrypoint).split(":", 1)[1].strip().lower() or "core-flow"
        _run_scenario(repo_path, scenario_name)
        return

    if ":" in entrypoint:
        module_name, function_name = entrypoint.split(":", 1)
        module = importlib.import_module(module_name)
        target = getattr(module, function_name)
        if not callable(target):
            raise TypeError(f"Entrypoint target is not callable: {entrypoint}")
        target()
        return

    if entrypoint.endswith(".py"):
        script_path = (repo_path / entrypoint).resolve()
        runpy.run_path(str(script_path), run_name="__main__")
        return

    runpy.run_module(entrypoint, run_name="__main__")


def _run_core_flow_scenario(repo_path: Path) -> None:
    from repo_audit_engine.analysis.static_analyzer import run_static_analysis
    from repo_audit_engine.classification.dead_code import build_dead_code_report_from_artifact
    from repo_audit_engine.classification.heat_engine import classify_code_heat_from_artifacts
    from repo_audit_engine.diagnostics.reporter import run_diagnostics_from_artifacts
    from repo_audit_engine.graph.graph_builder import build_dependency_graph
    from repo_audit_engine.io.artifacts import write_json
    from repo_audit_engine.manifest.builder import build_manifest
    from repo_audit_engine.pipeline.validation import run_verification

    with tempfile.TemporaryDirectory(prefix="bubble_core_flow_") as temp_dir:
        output_dir = Path(temp_dir).resolve() / "artifacts"
        output_dir.mkdir(parents=True, exist_ok=True)

        manifest_result = build_manifest(repo_path=repo_path, output_dir=output_dir)
        manifest_path = Path(str(manifest_result.get("manifest_path", "")))
        manifest_summary_path = Path(str(manifest_result.get("manifest_summary_path", "")))

        static_result = run_static_analysis(
            repo_path=repo_path,
            manifest_path=manifest_path,
            output_dir=output_dir,
        )
        static_path = Path(str(static_result.get("analysis_path", "")))

        graph_result = build_dependency_graph(
            manifest_path=manifest_path,
            static_analysis_path=static_path,
            output_dir=output_dir,
        )
        graph_payload = graph_result.get("graph") if isinstance(graph_result.get("graph"), dict) else {}

        heat_result = classify_code_heat_from_artifacts(
            graph_path=Path(str(graph_result.get("graph_path", ""))),
            manifest_summary_path=manifest_summary_path,
            output_dir=output_dir,
            runtime_flow_graph_path=None,
        )
        dead_result = build_dead_code_report_from_artifact(
            heat_path=Path(str(heat_result.get("heat_path", ""))),
            output_dir=output_dir,
        )

        validation_graph = graph_payload.get("validation_graph") if isinstance(graph_payload.get("validation_graph"), dict) else {}
        resolver_data = graph_payload.get("resolver_data") if isinstance(graph_payload.get("resolver_data"), dict) else {}
        entrypoints = manifest_result.get("summary", {}).get("entrypoints", [])
        normalized_entrypoints = [str(item).strip() for item in entrypoints if str(item).strip()]

        validation_result = run_verification(
            graph_data=validation_graph,
            resolver_data=resolver_data,
            entrypoints=normalized_entrypoints,
            min_trust=0.40,
        )
        validation_path = output_dir / "validation_result.json"
        write_json(validation_path, validation_result, pretty=True)

        run_diagnostics_from_artifacts(
            validation_path=validation_path,
            graph_path=Path(str(graph_result.get("graph_path", ""))),
            resolver_path=None,
        )

        # Keep the result referenced so static analysis doesn't treat this as dead code.
        _ = dead_result


def _run_cli_smoke_scenario(repo_path: Path) -> None:
    from repo_audit_engine.cli import main as cli_main

    with tempfile.TemporaryDirectory(prefix="bubble_cli_smoke_") as temp_dir:
        temp_root = Path(temp_dir).resolve()
        output_file = temp_root / "contract.json"

        exit_code = int(
            cli_main(
                [
                    "run-pipeline",
                    "--repo",
                    str(repo_path),
                    "--output",
                    str(output_file),
                    "--bubble-mode",
                    "false",
                ]
            )
        )

        if exit_code not in {0, 1}:
            raise RuntimeError(f"cli_smoke_failed:{exit_code}")


def _scenario_depth_probe() -> None:
    # Emit a deterministic local call chain so runtime depth checks can validate
    # that nested local execution is observed in bubble traces.
    _scenario_depth_probe_level1()


def _scenario_depth_probe_level1() -> None:
    _scenario_depth_probe_level2()


def _scenario_depth_probe_level2() -> None:
    _scenario_depth_probe_level3()


def _scenario_depth_probe_level3() -> None:
    return


def _run_scenario(repo_path: Path, scenario_name: str) -> None:
    normalized = str(scenario_name or "").strip().lower()

    if normalized in {"depth-probe", "depth_probe"}:
        _scenario_depth_probe()
        return

    _scenario_depth_probe()

    if normalized in {"core-flow", "default"}:
        _run_core_flow_scenario(repo_path)
        return

    if normalized == "cli-smoke":
        _run_cli_smoke_scenario(repo_path)
        return

    if normalized in {"comprehensive", "full-smoke"}:
        _run_core_flow_scenario(repo_path)
        _run_cli_smoke_scenario(repo_path)
        return

    raise ValueError(f"unknown_runtime_scenario:{scenario_name}")


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bubble_worker")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--events-output", required=True)
    parser.add_argument("--memory-cap-mb", type=int, default=256)
    parser.add_argument("--max-events", type=int, default=20000)
    parser.add_argument("--max-depth", type=int, default=120)
    parser.add_argument("--capture-lines", action="store_true", help="Capture line events in addition to call/return/import.")
    args = parser.parse_args(argv)

    repo_path = Path(args.repo).resolve()
    output_path = Path(args.output).resolve()
    events_output_path = Path(args.events_output).resolve()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    events_output_path.parent.mkdir(parents=True, exist_ok=True)

    imported_modules: Set[str] = set()
    executed_modules: Set[str] = set()
    call_stack: List[str] = []
    file_cache: Dict[str, str] = {}

    memory_cap_mb = max(32, int(args.memory_cap_mb))
    max_events = max(100, int(args.max_events))
    max_depth = max(20, int(args.max_depth))
    capture_lines = bool(args.capture_lines)

    status = "ok"
    error = ""

    event_counts = {
        "import": 0,
        "call": 0,
        "return": 0,
        "line": 0,
    }

    original_import = builtins.__import__

    sequence = 0

    with events_output_path.open("w", encoding="utf-8", buffering=1) as events_handle:
        def emit_event(payload: Dict[str, Any]) -> None:
            nonlocal sequence
            sequence += 1
            row = dict(payload)
            row.setdefault("timestamp", _utc_timestamp())
            row.setdefault("seq", sequence)
            events_handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")

        def tracing_import(name, globals_obj=None, locals_obj=None, fromlist=(), level=0):
            module = original_import(name, globals_obj, locals_obj, fromlist, level)
            normalized = str(name or "").strip()
            if normalized:
                imported_modules.add(normalized)
                event_counts["import"] += 1
                emit_event(
                    {
                        "event": "import",
                        "module": normalized,
                        "function": "<import>",
                        "file": "",
                        "line": 0,
                    }
                )
            return module

        max_line_events = max_events * 5

        def trace_callback(frame, event, arg):
            del arg

            if event not in {"call", "return", "line"}:
                return trace_callback

            if event == "line" and not capture_lines:
                return trace_callback

            filename = str(frame.f_code.co_filename or "")
            rel_file = file_cache.get(filename)
            if rel_file is None:
                rel_file = _normalize_repo_file(repo_path, filename)
                file_cache[filename] = rel_file
            if not rel_file:
                return trace_callback

            lineno = int(frame.f_lineno or 0)
            function_name = str(frame.f_code.co_name or "")
            module_name = str(frame.f_globals.get("__name__", "") or "")
            executed_modules.add(module_name)

            if event == "return":
                node_label = _node_id(rel_file, function_name)
                caller = call_stack[-2] if len(call_stack) >= 2 else "<entrypoint>"
                depth = len(call_stack)
                if call_stack:
                    call_stack.pop()
                event_counts["return"] += 1
                emit_event(
                    {
                        "event": "return",
                        "module": module_name,
                        "function": function_name,
                        "file": rel_file,
                        "line": lineno,
                        "caller": caller,
                        "caller_node_id": caller if caller != "<entrypoint>" else "",
                        "node": node_label,
                        "callee_node_id": node_label,
                        "depth": depth,
                    }
                )
                return trace_callback

            if event == "line":
                if event_counts["line"] >= max_line_events:
                    return trace_callback
                event_counts["line"] += 1
                emit_event(
                    {
                        "event": "line",
                        "module": module_name,
                        "function": function_name,
                        "file": rel_file,
                        "line": lineno,
                        "caller": call_stack[-1] if call_stack else "<entrypoint>",
                        "caller_node_id": call_stack[-1] if call_stack else "",
                        "depth": len(call_stack),
                    }
                )
                return trace_callback

            if event_counts["call"] >= max_events:
                raise RuntimeError("trace_event_limit_exceeded")

            depth = _frame_depth(frame)
            if depth > max_depth:
                raise RuntimeError("trace_depth_limit_exceeded")

            current_bytes, _ = tracemalloc.get_traced_memory()
            current_mb = current_bytes / (1024.0 * 1024.0)
            if current_mb > float(memory_cap_mb):
                raise MemoryError("trace_memory_limit_exceeded")

            node_label = _node_id(rel_file, function_name)
            caller_node_id = call_stack[-1] if call_stack else ""
            caller = caller_node_id if caller_node_id else "<entrypoint>"
            call_depth = len(call_stack) + 1

            event_counts["call"] += 1
            emit_event(
                {
                    "event": "call",
                    "module": module_name,
                    "function": function_name,
                    "file": rel_file,
                    "line": lineno,
                    "caller": caller,
                    "node": node_label,
                    "caller_node_id": caller_node_id,
                    "callee_node_id": node_label,
                    "depth": call_depth,
                }
            )
            if node_label:
                call_stack.append(node_label)

            return trace_callback

        started = perf_counter()

        try:
            if not repo_path.exists():
                raise FileNotFoundError(f"Repo path does not exist for bubble execution: {repo_path}")

            sys.path.insert(0, str(repo_path))
            builtins.__import__ = tracing_import

            tracemalloc.start()
            sys.settrace(trace_callback)

            _run_entrypoint(repo_path=repo_path, entrypoint=str(args.entrypoint))
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, SystemExit):
                code = exc.code
                if code is None:
                    exit_code = 0
                elif isinstance(code, int):
                    exit_code = int(code)
                else:
                    exit_code = 1

                if exit_code == 0:
                    status = "ok"
                    error = ""
                else:
                    status = "error"
                    error = f"system_exit:{exit_code}"
            else:
                status = "error"
                error = str(exc)
        finally:
            sys.settrace(None)
            builtins.__import__ = original_import
            if tracemalloc.is_tracing():
                tracemalloc.stop()

    duration = round(perf_counter() - started, 6)

    payload = {
        "entrypoint": str(args.entrypoint),
        "status": status,
        "error": error,
        "runtime_seconds": duration,
        "imports": sorted(imported_modules),
        "executed_modules": sorted(module for module in executed_modules if module),
        "event_counts": event_counts,
        "events_output": str(events_output_path),
        "capture_lines": capture_lines,
    }

    output_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")

    if status == "ok":
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
