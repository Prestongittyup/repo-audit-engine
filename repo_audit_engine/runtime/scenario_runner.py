from __future__ import annotations

import base64
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple, get_args, get_origin

from repo_audit_engine.io.artifacts import load_json, write_json

DEFAULT_MAX_SCENARIOS = 100
DEFAULT_COVERAGE_STOP_THRESHOLD = 0.25
DEFAULT_MAX_RUNTIME_SECONDS = 45
DEFAULT_MAX_EVENTS_PER_SCENARIO = 1500

PREFERRED_CLASS_METHODS: Tuple[str, ...] = ("run", "execute", "process", "handle")


def build_runtime_scenario_plan(
    dependency_graph_path: Path,
    manifest_summary_path: Path,
    output_dir: Path,
    execution_flow_graph_path: Path | None = None,
    max_scenarios: int = DEFAULT_MAX_SCENARIOS,
    coverage_stop_threshold: float = DEFAULT_COVERAGE_STOP_THRESHOLD,
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
    max_events_per_scenario: int = DEFAULT_MAX_EVENTS_PER_SCENARIO,
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

    entrypoint_paths = _entrypoint_paths(manifest_summary)

    selected_specs: List[Dict[str, Any]] = []
    for node in normalized_nodes:
        kind = str(node.get("kind", "")).strip().lower()
        if kind not in {"function", "class"}:
            continue

        rel_path = str(node.get("path", "")).strip()
        if _is_excluded_path(rel_path):
            continue

        module_name = _module_name_from_path(rel_path)
        if not module_name:
            continue

        node_id = str(node.get("node_id", "")).strip()
        if not node_id:
            continue

        node_name = str(node.get("name", "")).strip()
        if not node_name:
            continue

        is_entrypoint = rel_path in entrypoint_paths
        is_unexecuted = node_id not in runtime_hit_nodes

        inbound_count = int(inbound_edges.get(node_id, 0))
        outbound_count = int(outbound_edges.get(node_id, 0))

        priority_score = (
            (inbound_count * 0.5)
            + (outbound_count * 0.2)
            + (1.0 if is_unexecuted else 0.0)
            + (2.0 if is_entrypoint else 0.0)
        )

        selected_specs.append(
            {
                "node_id": node_id,
                "kind": kind,
                "path": rel_path,
                "name": node_name,
                "module": module_name,
                "inbound_edges": inbound_count,
                "outbound_edges": outbound_count,
                "is_unexecuted": bool(is_unexecuted),
                "is_entrypoint": bool(is_entrypoint),
                "priority_score": round(float(priority_score), 3),
            }
        )

    selected_specs.sort(
        key=lambda item: (
            -float(item.get("priority_score", 0.0) or 0.0),
            str(item.get("node_id", "")),
        )
    )

    scenario_limit = max(1, int(max_scenarios))
    selected_specs = selected_specs[:scenario_limit]

    for index, spec in enumerate(selected_specs, start=1):
        spec["scenario_id"] = f"scenario-{index:04d}"

    entrypoints = ["scenario:depth-probe"]
    for spec in selected_specs:
        entrypoints.append(encode_scenario_entrypoint(spec))

    total_node_count = max(1, len(normalized_nodes))
    baseline_runtime_hit_count = len(runtime_hit_nodes)
    baseline_coverage_ratio = _safe_divide(baseline_runtime_hit_count, total_node_count)

    shared_edges = static_call_edges.intersection(runtime_edges)
    baseline_overlap_ratio = _safe_divide(len(shared_edges), max(1, len(static_call_edges)))

    summary = {
        "total_node_count": len(normalized_nodes),
        "eligible_node_count": len(selected_specs),
        "selected_node_count": len(selected_specs),
        "entrypoint_count": len(entrypoints),
        "baseline_runtime_hit_nodes": baseline_runtime_hit_count,
        "baseline_coverage_ratio": round(float(baseline_coverage_ratio), 6),
        "baseline_overlap_ratio": round(float(baseline_overlap_ratio), 6),
        "max_scenarios": scenario_limit,
        "coverage_stop_threshold": round(float(_clamp01(coverage_stop_threshold)), 3),
        "max_runtime_seconds": max(10, int(max_runtime_seconds)),
        "max_events_per_scenario": max(100, int(max_events_per_scenario)),
    }

    payload = {
        "summary": summary,
        "entrypoints": entrypoints,
        "scenarios": selected_specs,
        "entrypoint_paths": sorted(entrypoint_paths),
        "baseline_runtime_hit_nodes": sorted(runtime_hit_nodes),
    }

    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    plan_path = out_root / "runtime_scenario_plan.json"
    write_json(plan_path, payload, pretty=True)

    return {
        "plan_path": str(plan_path),
        "entrypoints": entrypoints,
        "summary": summary,
        "scenarios": selected_specs,
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
            }
        return {
            "ok": False,
            "scenario_id": scenario_id,
            "error": str(execution.get("error", "scenario_execution_failed")),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "scenario_id": scenario_id,
            "error": f"{type(exc).__name__}:{exc}",
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
        return {"ok": False, "error": f"unsupported_kind:{kind or 'unknown'}"}

    module = _import_repo_module(repo_path=repo_path, module_name=module_name, rel_path=rel_path)
    if module is None:
        return {"ok": False, "error": f"module_import_failed:{module_name or rel_path}"}

    if kind == "module":
        return {"ok": True, "action": "imported_module"}

    target_name = str(spec.get("name", "")).strip()
    if not target_name:
        return {"ok": False, "error": "target_name_missing"}

    target = getattr(module, target_name, None)
    if target is None:
        return {"ok": False, "error": f"target_not_found:{target_name}"}

    if kind == "function":
        if not callable(target):
            return {"ok": False, "error": f"target_not_callable:{target_name}"}
        invoke = _invoke_callable(target)
        if not bool(invoke.get("ok", False)):
            return invoke
        return {"ok": True, "action": "called_function"}

    if not inspect.isclass(target):
        return {"ok": False, "error": f"target_not_class:{target_name}"}

    ctor_result = _invoke_callable(target)
    if not bool(ctor_result.get("ok", False)):
        return ctor_result

    instance = ctor_result.get("result")
    method = _select_instance_method(instance)
    if method is None:
        return {"ok": True, "action": "instantiated_class_only"}

    invoke = _invoke_callable(method)
    if not bool(invoke.get("ok", False)):
        return invoke

    return {"ok": True, "action": "instantiated_class_and_called_method"}


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


def _select_instance_method(instance):
    for method_name in PREFERRED_CLASS_METHODS:
        method = _safe_getattr(instance, method_name)
        if callable(method):
            return method

    for name in sorted(dir(instance)):
        if name.startswith("_"):
            continue
        method = _safe_getattr(instance, name)
        if callable(method):
            return method

    return None


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


def _is_excluded_path(path: str) -> bool:
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
