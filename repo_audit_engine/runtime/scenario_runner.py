from __future__ import annotations

import base64
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple, get_args, get_origin

from repo_audit_engine.io.artifacts import load_json, write_json

DEFAULT_MAX_ENTRYPOINTS = 5
DEFAULT_MAX_SCENARIOS = 100
DEFAULT_MAX_SEED_SCENARIOS = 40
DEFAULT_COVERAGE_STOP_THRESHOLD = 0.25
DEFAULT_MAX_RUNTIME_SECONDS = 180
DEFAULT_MAX_EVENTS_PER_SCENARIO = 600
DEFAULT_MAX_ENTRYPOINT_SECONDS = 3
DEFAULT_MAX_EXPANSION_DEPTH = 3
DEFAULT_MAX_FORCED_PROBES = 60
DEFAULT_MAX_FOLLOWUPS_PER_NODE = 2

PREFERRED_CLASS_METHODS: Tuple[str, ...] = ("run", "execute", "process", "handle", "main")

_WEB_IMPORT_TOKENS: Tuple[str, ...] = (
    "fastapi",
    "flask",
    "starlette",
    "django",
    "sanic",
    "quart",
    "aiohttp",
)
_CLI_IMPORT_TOKENS: Tuple[str, ...] = (
    "argparse",
    "click",
    "typer",
    "fire",
    "docopt",
    "cmd",
)

_CATEGORY_WEIGHTS: Dict[str, float] = {
    "explicit_config": 220.0,
    "main_guard": 200.0,
    "manifest_entrypoint": 185.0,
    "web_router": 170.0,
    "cli_definition": 160.0,
    "script_file": 90.0,
    "test_file": 30.0,
    "filename_heuristic": 100.0,
}

_NON_FATAL_INVOKE_ERRORS: Set[str] = {
    "unsafe_signature",
    "signature_unavailable",
    "target_not_callable",
    "target_not_class",
}


def build_runtime_scenario_plan(
    dependency_graph_path: Path,
    manifest_summary_path: Path,
    output_dir: Path,
    execution_flow_graph_path: Path | None = None,
    max_scenarios: int = DEFAULT_MAX_SCENARIOS,
    coverage_stop_threshold: float = DEFAULT_COVERAGE_STOP_THRESHOLD,
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
    max_events_per_scenario: int = DEFAULT_MAX_EVENTS_PER_SCENARIO,
    manifest_path: Path | None = None,
    max_entrypoints: int = DEFAULT_MAX_ENTRYPOINTS,
    max_seed_scenarios: int | None = None,
    max_entrypoint_seconds: int = DEFAULT_MAX_ENTRYPOINT_SECONDS,
) -> Dict[str, Any]:
    dependency_payload = _unwrap_payload(_safe_load_json(dependency_graph_path), "graph")
    manifest_summary = _safe_load_json(manifest_summary_path)
    existing_flow = _safe_load_json(execution_flow_graph_path) if execution_flow_graph_path else {}

    node_rows = dependency_payload.get("nodes") if isinstance(dependency_payload.get("nodes"), list) else []
    edge_rows = dependency_payload.get("edges") if isinstance(dependency_payload.get("edges"), list) else []

    normalized_nodes: List[Dict[str, Any]] = []
    alias_to_primary: Dict[str, str] = {}

    for item in node_rows:
        payload = item if isinstance(item, Mapping) else {}
        parsed = _parse_node(payload)
        if not parsed:
            continue

        primary_node_id = parsed["node_id"]
        normalized_nodes.append(parsed)

        aliases = {
            str(payload.get("id", "")).strip(),
            str(payload.get("canonical_id", "")).strip(),
            _canonicalize_legacy_node_id(str(payload.get("id", "")).strip()),
        }
        for alias in aliases:
            normalized_alias = str(alias or "").strip()
            if not normalized_alias:
                continue
            alias_to_primary[normalized_alias] = primary_node_id

    inbound_edges: Dict[str, int] = {}
    outbound_edges: Dict[str, int] = {}
    static_call_edges: Set[Tuple[str, str]] = set()

    for edge in edge_rows:
        payload = edge if isinstance(edge, Mapping) else {}
        source = _normalize_node_reference(payload.get("source", payload.get("from", "")), alias_to_primary)
        target = _normalize_node_reference(payload.get("target", payload.get("to", "")), alias_to_primary)
        if not source or not target:
            continue

        inbound_edges[target] = int(inbound_edges.get(target, 0)) + 1
        outbound_edges[source] = int(outbound_edges.get(source, 0)) + 1

        edge_type = str(payload.get("type", "")).strip().upper()
        if edge_type == "CALL":
            static_call_edges.add((source, target))

    runtime_hit_nodes: Set[str] = _extract_runtime_hit_nodes(existing_flow, alias_to_primary)
    runtime_edges = _extract_runtime_edges(existing_flow, alias_to_primary)

    manifest_rows = _load_manifest_rows(manifest_path)
    discovery_candidates = _discover_entrypoint_candidates(
        manifest_rows=manifest_rows,
        manifest_summary=manifest_summary,
        normalized_nodes=normalized_nodes,
    )

    entrypoint_limit = _focused_entrypoint_limit(max_entrypoints)
    selected_entrypoint_candidates = _select_focused_entrypoints(
        candidates=discovery_candidates,
        entrypoint_limit=entrypoint_limit,
    )
    selected_entrypoints = [
        str(item.get("entrypoint", "")).strip()
        for item in selected_entrypoint_candidates
        if str(item.get("entrypoint", "")).strip()
    ]

    seed_limit = max(1, int(max_seed_scenarios if max_seed_scenarios is not None else max_scenarios))
    selected_seed_specs, eligible_seed_specs = _select_seed_specs(
        normalized_nodes=normalized_nodes,
        inbound_edges=inbound_edges,
        outbound_edges=outbound_edges,
        preferred_paths={str(item.get("path", "")).strip() for item in selected_entrypoint_candidates},
        runtime_hit_nodes=runtime_hit_nodes,
        seed_limit=seed_limit,
    )

    for index, spec in enumerate(selected_seed_specs, start=1):
        spec["scenario_id"] = f"seed-{index:04d}"

    call_adjacency = _build_call_adjacency(
        static_call_edges=static_call_edges,
        outbound_edges=outbound_edges,
        max_neighbors_per_node=8,
    )

    expansion_context = _build_expansion_context(
        normalized_nodes=normalized_nodes,
        inbound_edges=inbound_edges,
        outbound_edges=outbound_edges,
        runtime_hit_nodes=runtime_hit_nodes,
        preferred_paths={str(item.get("path", "")).strip() for item in selected_entrypoint_candidates},
        call_adjacency=call_adjacency,
        max_forced_probes=DEFAULT_MAX_FORCED_PROBES,
    )

    entrypoints = list(selected_entrypoints)

    if not entrypoints:
        fallback_entrypoints = sorted(_entrypoint_paths(manifest_summary))
        entrypoints.extend(fallback_entrypoints[:entrypoint_limit])

    if not entrypoints:
        # Last-resort deterministic probe when discovery could not find executable targets.
        entrypoints.append("scenario:depth-probe")

    total_node_count = max(1, len(normalized_nodes))
    baseline_runtime_hit_count = len(runtime_hit_nodes)
    baseline_coverage_ratio = _safe_divide(baseline_runtime_hit_count, total_node_count)

    shared_edges = static_call_edges.intersection(runtime_edges)
    baseline_overlap_ratio = _safe_divide(len(shared_edges), max(1, len(static_call_edges)))

    category_counts: Dict[str, int] = {}
    for item in selected_entrypoint_candidates:
        category = str(item.get("primary_category", "unknown")).strip() or "unknown"
        category_counts[category] = int(category_counts.get(category, 0)) + 1

    summary = {
        "strategy": "focused_expansion",
        "total_node_count": len(normalized_nodes),
        "eligible_node_count": len(eligible_seed_specs),
        "selected_node_count": len(selected_seed_specs),
        "discovered_entrypoint_count": len(discovery_candidates),
        "selected_entrypoint_count": len(selected_entrypoint_candidates),
        "seed_entrypoint_count": len(entrypoints),
        "selected_seed_count": len(selected_seed_specs),
        "entrypoint_count": len(entrypoints),
        "forced_probe_count": len(expansion_context.get("forced_probe_entrypoints", [])),
        "entrypoint_category_counts": dict(sorted(category_counts.items(), key=lambda item: item[0])),
        "baseline_runtime_hit_nodes": baseline_runtime_hit_count,
        "baseline_coverage_ratio": round(float(baseline_coverage_ratio), 6),
        "baseline_overlap_ratio": round(float(baseline_overlap_ratio), 6),
        "max_scenarios": seed_limit,
        "max_entrypoints": entrypoint_limit,
        "max_expansion_depth": DEFAULT_MAX_EXPANSION_DEPTH,
        "max_followups_per_node": DEFAULT_MAX_FOLLOWUPS_PER_NODE,
        "coverage_stop_threshold": round(float(_clamp01(coverage_stop_threshold)), 3),
        "max_runtime_seconds": max(30, int(max_runtime_seconds)),
        "max_events_per_scenario": max(100, int(max_events_per_scenario)),
        "max_entrypoint_seconds": max(1, int(max_entrypoint_seconds)),
        "scheduling_policy": {
            "type": "coverage_driven_priority_queue",
            "priority_formula": "no_runtime_hits*HIGH + graph_centrality + low_inbound_edges + unexplored_neighbors",
        },
    }

    payload = {
        "summary": summary,
        "entrypoints": entrypoints,
        "seed_entrypoints": entrypoints,
        "discovered_entrypoints": selected_entrypoint_candidates,
        "scenarios": selected_seed_specs,
        "forced_probes": expansion_context.get("forced_probe_entrypoints", []),
        "node_probe_map": expansion_context.get("node_probe_map", {}),
        "path_probe_map": expansion_context.get("path_probe_map", {}),
        "module_to_paths": expansion_context.get("module_to_paths", {}),
        "node_metrics": expansion_context.get("node_metrics", {}),
        "call_adjacency": call_adjacency,
        "entrypoint_paths": sorted(_entrypoint_paths(manifest_summary)),
        "baseline_runtime_hit_nodes": sorted(runtime_hit_nodes),
    }

    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    plan_path = out_root / "runtime_scenario_plan.json"
    write_json(plan_path, payload, pretty=True)

    return {
        "plan_path": str(plan_path),
        "entrypoints": entrypoints,
        "seed_entrypoints": entrypoints,
        "summary": summary,
        "discovered_entrypoints": selected_entrypoint_candidates,
        "scenarios": selected_seed_specs,
        "forced_probes": expansion_context.get("forced_probe_entrypoints", []),
        "node_probe_map": expansion_context.get("node_probe_map", {}),
        "path_probe_map": expansion_context.get("path_probe_map", {}),
        "module_to_paths": expansion_context.get("module_to_paths", {}),
        "node_metrics": expansion_context.get("node_metrics", {}),
        "call_adjacency": call_adjacency,
        "baseline_runtime_hit_nodes": sorted(runtime_hit_nodes),
    }


def encode_scenario_entrypoint(spec: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"scenario:auto:{encoded}"


def run_encoded_scenario(repo_path: Path, encoded_spec: str) -> Dict[str, Any]:
    spec = _decode_scenario_spec(encoded_spec)
    scenario_id = str(spec.get("scenario_id", "unknown")).strip() or "unknown"

    try:
        execution = _execute_scenario(repo_path=repo_path, spec=spec)
        if bool(execution.get("ok", False)):
            return {
                "ok": True,
                "scenario_id": scenario_id,
                "action": execution.get("action", "executed"),
                "scenario_result": execution.get("scenario_result", "NO_CALL_ACTIVITY"),
            }
        return {
            "ok": False,
            "scenario_id": scenario_id,
            "error": str(execution.get("error", "scenario_execution_failed")),
            "scenario_result": execution.get("scenario_result", "CRASHED"),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "scenario_id": scenario_id,
            "error": f"{type(exc).__name__}:{exc}",
            "scenario_result": "CRASHED",
        }


def _decode_scenario_spec(encoded_spec: str) -> Dict[str, Any]:
    token = str(encoded_spec or "").strip()
    if not token:
        return {}

    padding = "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode((token + padding).encode("ascii"))
        payload = json.loads(decoded.decode("utf-8", errors="replace"))
    except (ValueError, json.JSONDecodeError):
        return {}

    return payload if isinstance(payload, dict) else {}


def _execute_scenario(repo_path: Path, spec: Mapping[str, Any]) -> Dict[str, Any]:
    kind = str(spec.get("kind", "")).strip().lower()
    module_name = str(spec.get("module", "")).strip()
    rel_path = str(spec.get("path", "")).strip()

    if kind not in {"function", "class", "module"}:
        return {"ok": False, "error": f"unsupported_kind:{kind or 'unknown'}", "scenario_result": "CRASHED"}

    module = _import_repo_module(repo_path=repo_path, module_name=module_name, rel_path=rel_path)
    if module is None:
        return {
            "ok": False,
            "error": f"module_import_failed:{module_name or rel_path}",
            "scenario_result": "CRASHED",
        }

    if kind == "module":
        return {"ok": True, "action": "imported_module", "scenario_result": "IMPORT_ONLY"}

    target_name = str(spec.get("name", "")).strip()
    if not target_name:
        return {"ok": False, "error": "target_name_missing", "scenario_result": "CRASHED"}

    target = getattr(module, target_name, None)
    if target is None:
        return {"ok": False, "error": f"target_not_found:{target_name}", "scenario_result": "CRASHED"}

    if kind == "function":
        if not callable(target):
            return {"ok": False, "error": f"target_not_callable:{target_name}", "scenario_result": "CRASHED"}

        invoke = _invoke_callable(target)
        if bool(invoke.get("ok", False)):
            return {"ok": True, "action": "called_function", "scenario_result": "CALL_ACTIVITY"}

        invoke_error = str(invoke.get("error", "")).strip()
        if invoke_error in _NON_FATAL_INVOKE_ERRORS:
            return {
                "ok": True,
                "action": "skipped_function_seed",
                "scenario_result": "NO_CALL_ACTIVITY",
            }

        return {"ok": False, "error": invoke_error or "function_invoke_failed", "scenario_result": "CRASHED"}

    if not inspect.isclass(target):
        return {"ok": False, "error": f"target_not_class:{target_name}", "scenario_result": "CRASHED"}

    ctor_result = _invoke_callable(target)
    if not bool(ctor_result.get("ok", False)):
        ctor_error = str(ctor_result.get("error", "")).strip()
        if ctor_error in _NON_FATAL_INVOKE_ERRORS:
            return {
                "ok": True,
                "action": "skipped_class_seed",
                "scenario_result": "NO_CALL_ACTIVITY",
            }
        return {"ok": False, "error": ctor_error or "class_constructor_failed", "scenario_result": "CRASHED"}

    instance = ctor_result.get("result")
    methods = _select_instance_methods(instance)
    if not methods:
        return {"ok": True, "action": "instantiated_class_only", "scenario_result": "NO_CALL_ACTIVITY"}

    invoked_count = 0
    attempted_count = 0
    for method in methods[:4]:
        attempted_count += 1
        invoke = _invoke_callable(method)
        if bool(invoke.get("ok", False)):
            invoked_count += 1

    if invoked_count > 0:
        return {
            "ok": True,
            "action": f"instantiated_class_and_called_{invoked_count}_methods",
            "scenario_result": "CALL_ACTIVITY",
        }

    if attempted_count > 0:
        return {
            "ok": True,
            "action": "instantiated_class_no_methods_invoked",
            "scenario_result": "NO_CALL_ACTIVITY",
        }

    return {"ok": True, "action": "instantiated_class_only", "scenario_result": "NO_CALL_ACTIVITY"}


def _import_repo_module(repo_path: Path, module_name: str, rel_path: str):
    if module_name:
        try:
            return importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            pass

    if not rel_path:
        return None

    file_path = (repo_path / rel_path).resolve()
    if not file_path.exists() or not file_path.is_file():
        return None

    fallback_name = f"_bubble_runtime_{_safe_module_token(module_name or rel_path)}"
    try:
        spec = importlib.util.spec_from_file_location(fallback_name, str(file_path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001
        return None


def _invoke_callable(callable_obj) -> Dict[str, Any]:
    signature = _safe_signature(callable_obj)
    if signature is None:
        return {"ok": False, "error": "signature_unavailable"}

    arguments = _build_safe_arguments(signature)
    if arguments is None:
        return {"ok": False, "error": "unsafe_signature"}

    args, kwargs = arguments
    try:
        result = callable_obj(*args, **kwargs)
        return {"ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}:{exc}"}


def _select_instance_methods(instance) -> List[Any]:
    selected: List[Any] = []
    seen: Set[str] = set()

    for method_name in PREFERRED_CLASS_METHODS:
        method = _safe_getattr(instance, method_name)
        if callable(method) and method_name not in seen:
            seen.add(method_name)
            selected.append(method)

    for name in sorted(dir(instance)):
        if name.startswith("_"):
            continue
        if name in seen:
            continue
        method = _safe_getattr(instance, name)
        if callable(method):
            seen.add(name)
            selected.append(method)

    return selected


def _safe_signature(callable_obj):
    try:
        return inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return None


def _build_safe_arguments(signature: inspect.Signature) -> Tuple[List[Any], Dict[str, Any]] | None:
    args: List[Any] = []
    kwargs: Dict[str, Any] = {}

    required_params = [
        param
        for param in signature.parameters.values()
        if param.default is inspect._empty
        and param.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
    ]
    if len(required_params) > 4:
        return None

    for param in signature.parameters.values():
        if param.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            continue

        if param.default is not inspect._empty:
            continue

        value = _safe_value_for_parameter(param)
        if value is _UNSAFE_PARAMETER:
            return None

        if param.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}:
            args.append(value)
        else:
            kwargs[param.name] = value

    return args, kwargs


_UNSAFE_PARAMETER = object()


def _safe_value_for_parameter(param: inspect.Parameter) -> Any:
    name = str(param.name or "").strip().lower()

    unsafe_tokens = {"session", "socket", "connection", "engine", "db", "cursor"}
    if any(token in name for token in unsafe_tokens):
        return _UNSAFE_PARAMETER

    annotation = param.annotation
    if annotation is not inspect._empty:
        resolved = _safe_value_for_annotation(annotation)
        if resolved is not _UNSAFE_PARAMETER:
            return resolved

    if any(token in name for token in {"id", "count", "index", "size", "port", "timeout", "retries"}):
        return 0
    if any(token in name for token in {"name", "path", "file", "text", "message", "query", "url"}):
        return ""
    if any(token in name for token in {"enabled", "flag", "strict", "verify", "dry"}):
        return False
    if any(token in name for token in {"data", "payload", "config", "options", "context", "params", "metadata"}):
        return {}
    if any(token in name for token in {"items", "values", "records", "nodes", "edges"}):
        return []

    return None


def _safe_value_for_annotation(annotation: Any) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)

    if annotation in {int, float}:
        return 0
    if annotation is bool:
        return False
    if annotation is str:
        return ""
    if annotation is bytes:
        return b""

    if origin in {list, List, Sequence, tuple, set, Set}:
        return []
    if origin in {dict, Dict, Mapping}:
        return {}

    if origin is None and hasattr(annotation, "__name__"):
        annotation_name = str(getattr(annotation, "__name__", "")).lower()
        if annotation_name in {"path", "posixpath", "windowspath"}:
            return ""

    optional_args = [item for item in args if item is not type(None)]  # noqa: E721
    if optional_args:
        return _safe_value_for_annotation(optional_args[0])

    return _UNSAFE_PARAMETER


def _safe_getattr(target: Any, name: str):
    try:
        return getattr(target, name)
    except Exception:  # noqa: BLE001
        return None


def _load_manifest_rows(manifest_path: Path | None) -> List[Dict[str, Any]]:
    if not manifest_path or not manifest_path.exists() or manifest_path.suffix.lower() != ".jsonl":
        return []

    rows: List[Dict[str, Any]] = []
    with manifest_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)

    return rows


def _discover_entrypoint_candidates(
    manifest_rows: Sequence[Mapping[str, Any]],
    manifest_summary: Mapping[str, Any],
    normalized_nodes: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    candidates_by_path: Dict[str, Dict[str, Any]] = {}
    summary_entrypoints = _entrypoint_paths(manifest_summary)

    for row in manifest_rows:
        payload = row if isinstance(row, Mapping) else {}
        if str(payload.get("language", "")).strip().lower() not in {"", "python"}:
            continue

        path = _normalize_path(payload.get("path", ""))
        if not path.endswith(".py"):
            continue

        module_name = str(payload.get("module", "")).strip() or _module_name_from_path(path)
        imports = {
            str(item).strip().lower()
            for item in (payload.get("imports") if isinstance(payload.get("imports"), list) else [])
            if str(item).strip()
        }
        entrypoint_reasons = {
            str(item).strip().lower()
            for item in (payload.get("entrypoint_reasons") if isinstance(payload.get("entrypoint_reasons"), list) else [])
            if str(item).strip()
        }

        categories: Set[str] = set()
        if "explicit_config" in entrypoint_reasons:
            categories.add("explicit_config")
        if "main_guard" in entrypoint_reasons or Path(path).name.lower() == "__main__.py":
            categories.add("main_guard")
        if path in summary_entrypoints or entrypoint_reasons:
            categories.add("manifest_entrypoint")
        if Path(path).name.lower() in {"main.py", "app.py", "cli.py", "run.py"}:
            categories.add("filename_heuristic")
        if _looks_like_web_router(path, imports):
            categories.add("web_router")
        if _looks_like_cli_module(path, imports):
            categories.add("cli_definition")
        if path.startswith("scripts/"):
            categories.add("script_file")
        if _is_test_path(path):
            categories.add("test_file")

        if not categories:
            continue

        priority_score = _entrypoint_priority(categories, path)
        existing = candidates_by_path.get(path)
        if existing is None:
            candidates_by_path[path] = {
                "entrypoint": path,
                "path": path,
                "module": module_name,
                "categories": sorted(categories),
                "primary_category": _primary_category(categories),
                "priority_score": round(priority_score, 3),
            }
            continue

        merged_categories = set(existing.get("categories", [])) | categories
        existing["categories"] = sorted(merged_categories)
        existing["primary_category"] = _primary_category(merged_categories)
        existing["priority_score"] = round(max(float(existing.get("priority_score", 0.0) or 0.0), priority_score), 3)

    # Ensure summary-declared entrypoints are always represented.
    for path in sorted(summary_entrypoints):
        if path in candidates_by_path:
            continue
        module_name = _module_name_from_path(path)
        categories = {"manifest_entrypoint"}
        if Path(path).name.lower() in {"main.py", "app.py", "cli.py", "run.py", "__main__.py"}:
            categories.add("filename_heuristic")
        candidates_by_path[path] = {
            "entrypoint": path,
            "path": path,
            "module": module_name,
            "categories": sorted(categories),
            "primary_category": _primary_category(categories),
            "priority_score": round(_entrypoint_priority(categories, path), 3),
        }

    # Fallback from graph nodes when manifest rows are sparse.
    for node in normalized_nodes:
        payload = node if isinstance(node, Mapping) else {}
        if str(payload.get("kind", "")).strip().lower() != "file":
            continue
        path = _normalize_path(payload.get("path", ""))
        if not path.endswith(".py") or path in candidates_by_path:
            continue

        categories: Set[str] = set()
        if path.startswith("scripts/"):
            categories.add("script_file")
        if _is_test_path(path):
            categories.add("test_file")
        if Path(path).name.lower() in {"main.py", "app.py", "cli.py", "run.py", "__main__.py"}:
            categories.add("filename_heuristic")

        if not categories:
            continue

        candidates_by_path[path] = {
            "entrypoint": path,
            "path": path,
            "module": _module_name_from_path(path),
            "categories": sorted(categories),
            "primary_category": _primary_category(categories),
            "priority_score": round(_entrypoint_priority(categories, path), 3),
        }

    candidates = list(candidates_by_path.values())
    candidates.sort(
        key=lambda item: (
            -float(item.get("priority_score", 0.0) or 0.0),
            str(item.get("path", "")),
        )
    )

    return candidates


def _entrypoint_priority(categories: Set[str], path: str) -> float:
    score = 0.0
    for category in categories:
        score += float(_CATEGORY_WEIGHTS.get(category, 0.0))

    normalized_path = _normalize_path(path).lower()
    if normalized_path.startswith("apps/api/"):
        score += 8.0
    if normalized_path.startswith("scripts/"):
        score += 4.0
    if normalized_path.startswith("tests/"):
        score += 1.0

    return score


def _primary_category(categories: Set[str]) -> str:
    if not categories:
        return "unknown"
    return sorted(categories, key=lambda item: (-float(_CATEGORY_WEIGHTS.get(item, 0.0)), item))[0]


def _looks_like_web_router(path: str, imports: Set[str]) -> bool:
    lowered_path = _normalize_path(path).lower()
    if any(token in lowered_path for token in ("router", "routes", "endpoint", "api/")):
        if imports:
            if any(any(imp == token or imp.startswith(token + ".") for token in _WEB_IMPORT_TOKENS) for imp in imports):
                return True

    if any(any(imp == token or imp.startswith(token + ".") for token in _WEB_IMPORT_TOKENS) for imp in imports):
        return True

    return False


def _looks_like_cli_module(path: str, imports: Set[str]) -> bool:
    lowered_path = _normalize_path(path).lower()
    if "cli" in Path(lowered_path).name or lowered_path.startswith("scripts/"):
        return True

    return any(any(imp == token or imp.startswith(token + ".") for token in _CLI_IMPORT_TOKENS) for imp in imports)


def _is_test_path(path: str) -> bool:
    normalized = _normalize_path(path).lower()
    filename = Path(normalized).name
    if normalized.startswith("tests/"):
        return True
    if filename.startswith("test_"):
        return True
    if filename.endswith("_test.py"):
        return True
    return False


def _select_seed_specs(
    normalized_nodes: Sequence[Mapping[str, Any]],
    inbound_edges: Mapping[str, int],
    outbound_edges: Mapping[str, int],
    preferred_paths: Set[str],
    runtime_hit_nodes: Set[str],
    seed_limit: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    candidate_specs: List[Dict[str, Any]] = []

    for node in normalized_nodes:
        payload = node if isinstance(node, Mapping) else {}
        kind = str(payload.get("kind", "")).strip().lower()
        if kind not in {"function", "class"}:
            continue

        rel_path = _normalize_path(payload.get("path", ""))
        name = str(payload.get("name", "")).strip()
        if not rel_path or not name:
            continue
        if _is_excluded_seed_path(rel_path):
            continue
        if _is_private_symbol(name):
            continue

        module_name = _module_name_from_path(rel_path)
        if not module_name:
            continue

        node_id = str(payload.get("node_id", "")).strip()
        if not node_id:
            continue

        inbound_count = int(inbound_edges.get(node_id, 0) or 0)
        outbound_count = int(outbound_edges.get(node_id, 0) or 0)
        is_unexecuted = node_id not in runtime_hit_nodes
        preferred_path = rel_path in preferred_paths

        score = (
            (inbound_count * 0.5)
            + (outbound_count * 0.2)
            + (2.0 if preferred_path else 0.0)
            + (1.0 if is_unexecuted else 0.0)
            + (0.25 if kind == "class" else 0.0)
        )

        candidate_specs.append(
            {
                "node_id": node_id,
                "kind": kind,
                "path": rel_path,
                "name": name,
                "module": module_name,
                "seed_reason": "runtime_seeding",
                "inbound_edges": inbound_count,
                "outbound_edges": outbound_count,
                "is_unexecuted": bool(is_unexecuted),
                "is_preferred_path": bool(preferred_path),
                "priority_score": round(float(score), 3),
            }
        )

    candidate_specs.sort(
        key=lambda item: (
            -float(item.get("priority_score", 0.0) or 0.0),
            str(item.get("node_id", "")),
        )
    )

    selected = [dict(item) for item in candidate_specs[: max(0, int(seed_limit))]]
    return selected, candidate_specs


def _focused_entrypoint_limit(max_entrypoints: int) -> int:
    requested = max(1, int(max_entrypoints))
    return max(3, min(5, requested))


def _build_call_adjacency(
    static_call_edges: Set[Tuple[str, str]],
    outbound_edges: Mapping[str, int],
    max_neighbors_per_node: int,
) -> Dict[str, List[str]]:
    adjacency_sets: Dict[str, Set[str]] = {}
    for source, target in static_call_edges:
        source_id = str(source).strip()
        target_id = str(target).strip()
        if not source_id or not target_id:
            continue
        adjacency_sets.setdefault(source_id, set()).add(target_id)

    adjacency: Dict[str, List[str]] = {}
    for source, targets in adjacency_sets.items():
        ordered_targets = sorted(
            targets,
            key=lambda target: (
                -int(outbound_edges.get(target, 0) or 0),
                str(target),
            ),
        )
        adjacency[source] = ordered_targets[: max(1, int(max_neighbors_per_node))]

    return adjacency


def _select_focused_entrypoints(
    candidates: Sequence[Mapping[str, Any]],
    entrypoint_limit: int,
) -> List[Dict[str, Any]]:
    normalized_limit = max(1, int(entrypoint_limit))
    rows = [dict(item) for item in candidates if isinstance(item, Mapping)]
    if len(rows) <= normalized_limit:
        return rows

    by_category: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        category = str(row.get("primary_category", "unknown")).strip() or "unknown"
        by_category.setdefault(category, []).append(row)

    for category in by_category:
        by_category[category].sort(
            key=lambda item: (
                -float(item.get("priority_score", 0.0) or 0.0),
                str(item.get("path", "")),
            )
        )

    selected: List[Dict[str, Any]] = []
    selected_paths: Set[str] = set()

    preferred_category_order = [
        "explicit_config",
        "web_router",
        "main_guard",
        "manifest_entrypoint",
        "cli_definition",
        "filename_heuristic",
        "script_file",
    ]

    for category in preferred_category_order:
        if len(selected) >= normalized_limit:
            break
        rows_for_category = by_category.get(category, [])
        for row in rows_for_category:
            path = str(row.get("path", "")).strip()
            if not path or path in selected_paths:
                continue
            if _is_test_path(path):
                continue
            if category == "script_file":
                selected_script_count = len(
                    [
                        item
                        for item in selected
                        if str(item.get("primary_category", "")).strip() == "script_file"
                    ]
                )
                if selected_script_count >= 2:
                    continue
            selected.append(dict(row))
            selected_paths.add(path)
            break

    if len(selected) < normalized_limit:
        for row in rows:
            path = str(row.get("path", "")).strip()
            if not path or path in selected_paths:
                continue
            category = str(row.get("primary_category", "")).strip()
            if category == "script_file":
                selected_script_count = len(
                    [
                        item
                        for item in selected
                        if str(item.get("primary_category", "")).strip() == "script_file"
                    ]
                )
                if selected_script_count >= 2:
                    continue
            if category == "test_file" and len(selected) >= 3:
                continue
            selected.append(dict(row))
            selected_paths.add(path)
            if len(selected) >= normalized_limit:
                break

    return selected[:normalized_limit]


def _build_expansion_context(
    normalized_nodes: Sequence[Mapping[str, Any]],
    inbound_edges: Mapping[str, int],
    outbound_edges: Mapping[str, int],
    runtime_hit_nodes: Set[str],
    preferred_paths: Set[str],
    call_adjacency: Mapping[str, Sequence[str]],
    max_forced_probes: int,
) -> Dict[str, Any]:
    runtime_hits = {str(item).strip() for item in runtime_hit_nodes if str(item).strip()}

    node_probe_map: Dict[str, Dict[str, Any]] = {}
    path_probe_map: Dict[str, List[Dict[str, Any]]] = {}
    module_to_paths: Dict[str, Set[str]] = {}
    node_metrics: Dict[str, Dict[str, Any]] = {}
    force_probe_candidates: List[Dict[str, Any]] = []

    for node in normalized_nodes:
        payload = node if isinstance(node, Mapping) else {}
        node_id = str(payload.get("node_id", "")).strip()
        kind = str(payload.get("kind", "")).strip().lower()
        rel_path = _normalize_path(payload.get("path", ""))
        symbol_name = str(payload.get("name", "")).strip()

        if not node_id or not rel_path:
            continue
        if _is_excluded_seed_path(rel_path):
            continue

        probe_kind = kind
        module_name = _module_name_from_path(rel_path)
        probe_name = symbol_name

        if kind == "file":
            probe_kind = "module"
            probe_name = ""
        elif kind == "module":
            probe_kind = "module"
            probe_name = ""
        elif kind in {"function", "class"}:
            if _is_private_symbol(symbol_name):
                continue
        else:
            continue

        if not module_name:
            continue

        inbound_count = int(inbound_edges.get(node_id, 0) or 0)
        outbound_count = int(outbound_edges.get(node_id, 0) or 0)
        graph_centrality = float(inbound_count + outbound_count)

        neighbors = [str(item).strip() for item in call_adjacency.get(node_id, []) if str(item).strip()]
        unexplored_neighbors = len([item for item in neighbors if item not in runtime_hits])
        no_runtime_hits = 1 if node_id not in runtime_hits else 0
        low_inbound_score = 1.0 / (1.0 + float(inbound_count))

        priority_score = (
            (float(no_runtime_hits) * 120.0)
            + (graph_centrality * 6.0)
            + (low_inbound_score * 25.0)
            + (float(unexplored_neighbors) * 12.0)
            + (18.0 if rel_path in preferred_paths else 0.0)
            + (10.0 if probe_kind == "function" else 0.0)
            + (4.0 if probe_kind == "class" else 0.0)
            + (2.0 if probe_kind == "module" else 0.0)
            + (10.0 if (inbound_count + outbound_count) <= 1 else 0.0)
        )

        spec: Dict[str, Any] = {
            "scenario_id": f"probe-{_safe_module_token(node_id)}",
            "kind": probe_kind,
            "path": rel_path,
            "module": module_name,
            "seed_reason": "forced_probe",
            "node_id": node_id,
        }
        if probe_kind in {"function", "class"} and probe_name:
            spec["name"] = probe_name

        encoded_entrypoint = encode_scenario_entrypoint(spec)
        row = {
            "entrypoint": encoded_entrypoint,
            "node_id": node_id,
            "path": rel_path,
            "kind": probe_kind,
            "priority_score": round(float(priority_score), 3),
            "inbound_edges": inbound_count,
            "outbound_edges": outbound_count,
            "graph_centrality": round(graph_centrality, 3),
            "unexplored_neighbors": int(unexplored_neighbors),
            "no_runtime_hits": bool(no_runtime_hits),
        }

        existing_probe = node_probe_map.get(node_id)
        if existing_probe is None or float(existing_probe.get("priority_score", 0.0) or 0.0) < float(priority_score):
            node_probe_map[node_id] = dict(row)

        path_probe_map.setdefault(rel_path, []).append(dict(row))
        module_to_paths.setdefault(module_name, set()).add(rel_path)

        node_metrics[node_id] = {
            "inbound_edges": inbound_count,
            "outbound_edges": outbound_count,
            "graph_centrality": round(graph_centrality, 3),
            "low_inbound_score": round(float(low_inbound_score), 6),
            "unexplored_neighbors": int(unexplored_neighbors),
            "no_runtime_hits": bool(no_runtime_hits),
            "path": rel_path,
            "kind": probe_kind,
        }

        is_low_degree = (inbound_count + outbound_count) <= 2
        should_force_probe = (
            (probe_kind == "function" and bool(no_runtime_hits) and is_low_degree)
            or (probe_kind == "module" and is_low_degree)
            or (probe_kind == "class" and bool(no_runtime_hits) and (inbound_count + outbound_count) <= 1)
        )
        if should_force_probe:
            force_probe_candidates.append(dict(row))

    normalized_path_probe_map: Dict[str, List[Dict[str, Any]]] = {}
    for rel_path, rows in path_probe_map.items():
        ordered_rows = sorted(
            rows,
            key=lambda item: (
                -float(item.get("priority_score", 0.0) or 0.0),
                str(item.get("entrypoint", "")),
            ),
        )
        normalized_path_probe_map[rel_path] = ordered_rows[:8]

    forced_rows = sorted(
        force_probe_candidates,
        key=lambda item: (
            0 if str(item.get("kind", "")).strip() == "function" else (1 if str(item.get("kind", "")).strip() == "class" else 2),
            -float(item.get("priority_score", 0.0) or 0.0),
            str(item.get("entrypoint", "")),
        ),
    )

    seen_entrypoints: Set[str] = set()
    forced_probe_entrypoints: List[str] = []
    forced_probe_rows: List[Dict[str, Any]] = []
    for item in forced_rows:
        entrypoint = str(item.get("entrypoint", "")).strip()
        if not entrypoint or entrypoint in seen_entrypoints:
            continue
        seen_entrypoints.add(entrypoint)
        forced_probe_entrypoints.append(entrypoint)
        forced_probe_rows.append(dict(item))
        if len(forced_probe_entrypoints) >= max(1, int(max_forced_probes)):
            break

    serialized_module_to_paths = {
        module_name: sorted(paths)
        for module_name, paths in module_to_paths.items()
    }

    return {
        "forced_probe_entrypoints": forced_probe_entrypoints,
        "forced_probe_rows": forced_probe_rows,
        "node_probe_map": node_probe_map,
        "path_probe_map": normalized_path_probe_map,
        "module_to_paths": dict(sorted(serialized_module_to_paths.items(), key=lambda item: item[0])),
        "node_metrics": node_metrics,
    }


def _extract_runtime_hit_nodes(
    execution_flow_graph: Mapping[str, Any],
    alias_to_primary: Mapping[str, str],
) -> Set[str]:
    hit_nodes: Set[str] = set()

    node_hits = execution_flow_graph.get("node_hits") if isinstance(execution_flow_graph.get("node_hits"), Mapping) else {}
    for key, value in node_hits.items():
        if int(value or 0) <= 0:
            continue
        normalized = _normalize_node_reference(key, alias_to_primary)
        if normalized:
            hit_nodes.add(normalized)

    node_rows = execution_flow_graph.get("nodes") if isinstance(execution_flow_graph.get("nodes"), list) else []
    for item in node_rows:
        payload = item if isinstance(item, Mapping) else {}
        hit_count = int(payload.get("hit_count", 0) or 0)
        if hit_count <= 0:
            continue
        normalized = _normalize_node_reference(payload.get("id", ""), alias_to_primary)
        if normalized:
            hit_nodes.add(normalized)

    return hit_nodes


def _extract_runtime_edges(
    execution_flow_graph: Mapping[str, Any],
    alias_to_primary: Mapping[str, str],
) -> Set[Tuple[str, str]]:
    result: Set[Tuple[str, str]] = set()
    edge_rows = execution_flow_graph.get("edges") if isinstance(execution_flow_graph.get("edges"), list) else []

    for item in edge_rows:
        payload = item if isinstance(item, Mapping) else {}
        edge_type = str(payload.get("type", "")).strip().upper()
        if edge_type != "RUNTIME_CALL":
            continue

        source = _normalize_node_reference(payload.get("source", payload.get("from", "")), alias_to_primary)
        target = _normalize_node_reference(payload.get("target", payload.get("to", "")), alias_to_primary)
        if source and target:
            result.add((source, target))

    return result


def _entrypoint_paths(manifest_summary: Mapping[str, Any]) -> Set[str]:
    entrypoints = manifest_summary.get("entrypoints") if isinstance(manifest_summary.get("entrypoints"), list) else []
    result: Set[str] = set()
    for item in entrypoints:
        normalized = _normalize_path(item)
        if normalized:
            result.add(normalized)
    return result


def _parse_node(payload: Mapping[str, Any]) -> Dict[str, Any] | None:
    raw_kind = str(payload.get("kind", "")).strip().lower()
    raw_id = str(payload.get("id", "")).strip()
    canonical_id = str(payload.get("canonical_id", "")).strip()

    kind = raw_kind
    path = _normalize_path(payload.get("path", ""))
    name = str(payload.get("name", "")).strip()

    canonical_from_id = canonical_id or _canonicalize_legacy_node_id(raw_id)
    if canonical_from_id and canonical_from_id.startswith("canonical://"):
        parsed_kind, parsed_path, parsed_name = _parse_canonical_node_id(canonical_from_id)
        if not kind:
            kind = parsed_kind
        if not path:
            path = parsed_path
        if not name:
            name = parsed_name

    if not kind and raw_id:
        parsed_kind, parsed_path, parsed_name = _parse_legacy_node_id(raw_id)
        kind = parsed_kind
        if not path:
            path = parsed_path
        if not name:
            name = parsed_name

    if kind not in {"file", "function", "class", "module"}:
        return None

    if not path:
        return None

    if kind in {"function", "class"} and not name:
        return None

    node_id = canonical_from_id if canonical_from_id else raw_id
    if not node_id:
        if kind == "file":
            node_id = f"canonical://file/{path}"
        else:
            node_id = f"canonical://{kind}/{path}/{name}"

    return {
        "node_id": str(node_id).strip(),
        "kind": kind,
        "path": path,
        "name": name,
    }


def _parse_canonical_node_id(node_id: str) -> Tuple[str, str, str]:
    raw = str(node_id or "").strip()
    if not raw.startswith("canonical://"):
        return "", "", ""

    payload = raw[len("canonical://") :]
    kind, sep, remainder = payload.partition("/")
    if not sep:
        return "", "", ""

    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind == "file":
        return "file", _normalize_path(remainder), ""

    rel_path, rel_sep, name = remainder.rpartition("/")
    if not rel_sep:
        return normalized_kind, _normalize_path(remainder), ""

    return normalized_kind, _normalize_path(rel_path), str(name).strip()


def _parse_legacy_node_id(node_id: str) -> Tuple[str, str, str]:
    raw = str(node_id or "").strip()
    if ":" not in raw:
        return "", "", ""

    kind, payload = raw.split(":", 1)
    normalized_kind = str(kind or "").strip().lower()
    body = str(payload or "").strip()

    if normalized_kind == "file":
        return "file", _normalize_path(body), ""

    if normalized_kind in {"function", "class"}:
        rel_path, sep, name = body.rpartition(":")
        if not sep:
            return normalized_kind, _normalize_path(body), ""
        return normalized_kind, _normalize_path(rel_path), str(name).strip()

    return "", "", ""


def _normalize_node_reference(value: Any, alias_to_primary: Mapping[str, str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    if raw in alias_to_primary:
        return str(alias_to_primary[raw]).strip()

    canonical = _canonicalize_legacy_node_id(raw)
    if canonical and canonical in alias_to_primary:
        return str(alias_to_primary[canonical]).strip()

    return canonical or raw


def _canonicalize_legacy_node_id(node_id: str) -> str:
    kind, path, name = _parse_legacy_node_id(node_id)
    if not kind:
        return ""
    if kind == "file":
        return f"canonical://file/{path}"
    if kind in {"function", "class"} and name:
        return f"canonical://{kind}/{path}/{name}"
    return ""


def _module_name_from_path(path: str) -> str:
    normalized = _normalize_path(path)
    if not normalized.endswith(".py"):
        return ""

    without_suffix = normalized[:-3]
    if without_suffix.endswith("/__init__"):
        without_suffix = without_suffix[: -len("/__init__")]

    module_name = without_suffix.replace("/", ".")
    while module_name.startswith("."):
        module_name = module_name[1:]

    return module_name


def _is_excluded_seed_path(path: str) -> bool:
    normalized = _normalize_path(path).lower()
    if not normalized:
        return True

    if normalized.startswith("<"):
        return True
    if "site-packages/" in normalized or "dist-packages/" in normalized:
        return True
    if normalized.endswith("/__init__.py"):
        return True
    if normalized.startswith("tests/") or "/tests/" in f"/{normalized}":
        return True
    if normalized.startswith("test_") or "/test_" in normalized:
        return True
    if normalized.endswith("_test.py"):
        return True

    return False


def _is_private_symbol(name: str) -> bool:
    normalized = str(name or "").strip()
    if not normalized:
        return True
    if normalized.startswith("_"):
        return True
    if normalized.startswith("test"):
        return True
    return False


def _normalize_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _safe_load_json(path: Path | None) -> Dict[str, Any]:
    if not path:
        return {}
    if not path.exists():
        return {}

    payload = load_json(path)
    return payload if isinstance(payload, dict) else {}


def _unwrap_payload(payload: Mapping[str, Any], nested_key: str) -> Mapping[str, Any]:
    nested = payload.get(nested_key)
    if isinstance(nested, Mapping):
        return nested
    return payload


def _safe_module_token(value: str) -> str:
    token = str(value or "").strip().replace("\\", "/").replace("/", "_").replace(".", "_")
    return token or "module"


def _safe_divide(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return float(numerator) / float(denominator)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
