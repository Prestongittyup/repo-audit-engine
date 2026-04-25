from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from repo_audit_engine.pipeline import orchestrator as pipeline_orchestrator
from repo_audit_engine.pipeline.stages import FULL_STAGE_ORDER


ARCHITECTURAL_DOMAINS = [
    "core",
    "manifest",
    "analysis",
    "graph",
    "runtime",
    "classification",
    "diagnostics",
    "pipeline",
    "io",
    "utils",
    "tests",
]

SOURCE_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".tmp",
    "output",
    "state",
    "archive",
    ".idea",
    ".vscode",
}

TEXT_SCAN_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".tmp",
    "output",
    "state",
    "archive",
    ".idea",
    ".vscode",
}

LEGACY_SCAN_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".tmp",
    ".idea",
    ".vscode",
}

TEXT_EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".ps1",
}

VOLATILE_KEYS = {
    "timestamp",
    "runtime_seconds",
    "manifest_path",
    "analysis_path",
    "summary_path",
    "graph_path",
    "trace_path",
    "flow_graph_path",
    "validation_path",
    "report_path",
}

EXPECTED_STAGE_ORDER = [
    "manifest",
    "static",
    "graph",
    "bubble",
    "classification",
    "verification",
    "diagnostics",
    "report",
]

LOCAL_CODE_PATH_PREFIXES = (
    "repo_audit_engine/",
    "src/",
)

LOCAL_MODULE_PREFIXES = (
    "repo_audit_engine",
    "src",
)

TRUTH_VALIDATION_THRESHOLDS = {
    "min_modules_executed": 5,
    "min_local_modules_executed": 3,
    "min_unique_functions_called": 20,
    "min_unique_local_functions_called": 3,
    "min_call_depth": 3,
    "min_reachable_node_ratio": 0.15,
    "max_isolated_node_ratio": 0.35,
    "min_runtime_confirmed_edge_ratio": 0.01,
}


@dataclass
class PipelineRunCapture:
    payload: Dict[str, Any]
    stage_timings: Dict[str, float]
    stage_sequence: List[str]
    total_seconds: float
    output_dir: Path


def _to_posix(path: Path) -> str:
    return path.as_posix()


def _rel_path(path: Path, repo_root: Path) -> str:
    return _to_posix(path.resolve().relative_to(repo_root.resolve()))


def _iter_files(repo_root: Path, skip_dirs: Set[str]) -> Iterable[Path]:
    for root, dirs, files in os.walk(repo_root, topdown=True):
        dirs[:] = [name for name in sorted(dirs) if name not in skip_dirs]
        root_path = Path(root)
        for filename in sorted(files):
            yield root_path / filename


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _load_json(path: Path) -> Any:
    raw = _read_text(path).strip()
    if not raw:
        return {}
    return json.loads(raw)


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for raw_line in _read_text(path).splitlines():
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


def _hash_json_payload(payload: Any) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _extract_function_symbols(path: Path) -> Set[str]:
    source = _read_text(path)
    if not source.strip():
        return set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    symbols: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(str(node.name))
    return symbols


def _categorize_python_path(rel_path: str) -> Tuple[str, str]:
    parts = rel_path.split("/")
    if not parts:
        return ("orphan", "")

    if parts[0] == "repo_audit_engine":
        if len(parts) == 2 and parts[1] in {"__init__.py", "__main__.py", "cli.py"}:
            return ("package_root", "package_root")
        if len(parts) >= 2 and parts[1] in set(ARCHITECTURAL_DOMAINS):
            return ("domain", parts[1])
        return ("misplaced", "repo_audit_engine")

    if parts[0] == "tests":
        return ("domain", "tests")

    if parts[0] == "tools":
        return ("tooling", "tools")

    if parts[0] == "src":
        return ("orphan", "src")

    return ("orphan", "")


def _build_domain_coverage(repo_root: Path) -> Dict[str, Any]:
    domain_coverage: Dict[str, Dict[str, Any]] = {
        domain: {"file_count": 0, "sample_files": []} for domain in ARCHITECTURAL_DOMAINS
    }

    misplaced_files: List[str] = []
    orphaned_scripts: List[str] = []
    python_files: List[str] = []

    for file_path in _iter_files(repo_root, SOURCE_SKIP_DIRS):
        if file_path.suffix.lower() != ".py":
            continue
        rel_path = _rel_path(file_path, repo_root)
        python_files.append(rel_path)

        category, group = _categorize_python_path(rel_path)
        if category == "domain" and group in domain_coverage:
            coverage = domain_coverage[group]
            coverage["file_count"] = int(coverage["file_count"]) + 1
            if len(coverage["sample_files"]) < 20:
                coverage["sample_files"].append(rel_path)
        elif category == "misplaced":
            misplaced_files.append(rel_path)
        elif category == "orphan":
            orphaned_scripts.append(rel_path)

    for domain in domain_coverage.values():
        domain["sample_files"] = sorted(domain["sample_files"])

    return {
        "domain_coverage": domain_coverage,
        "misplaced_files": sorted(misplaced_files),
        "orphaned_scripts": sorted(orphaned_scripts),
        "python_files": sorted(python_files),
    }


def _detect_duplicate_functionality(repo_root: Path, python_files: Sequence[str]) -> List[Dict[str, Any]]:
    production_files = [
        rel for rel in python_files if rel.startswith("repo_audit_engine/") or rel.startswith("src/")
    ]

    basename_map: Dict[str, List[str]] = {}
    function_map: Dict[str, Set[str]] = {}

    for rel in production_files:
        path = repo_root / rel
        stem = path.stem
        basename_map.setdefault(stem, []).append(rel)
        function_map[rel] = _extract_function_symbols(path)

    findings: List[Dict[str, Any]] = []

    for stem, files in sorted(basename_map.items(), key=lambda item: item[0]):
        if stem in {"__init__", "__main__"}:
            continue
        if len(files) >= 2:
            findings.append(
                {
                    "type": "shared_basename",
                    "stem": stem,
                    "files": sorted(files),
                    "evidence": f"{len(files)} files share basename '{stem}'.",
                }
            )

    file_list = sorted(function_map.keys())
    for index, first in enumerate(file_list):
        first_symbols = {
            name for name in function_map.get(first, set()) if not name.startswith("_")
        }
        if not first_symbols:
            continue

        for second in file_list[index + 1 :]:
            second_symbols = {
                name for name in function_map.get(second, set()) if not name.startswith("_")
            }
            if not second_symbols:
                continue

            intersection = sorted(first_symbols.intersection(second_symbols))
            if len(intersection) < 5:
                continue

            union_size = len(first_symbols.union(second_symbols))
            if union_size == 0:
                continue

            jaccard = len(intersection) / float(union_size)
            if jaccard < 0.6:
                continue

            findings.append(
                {
                    "type": "function_overlap",
                    "files": [first, second],
                    "shared_function_count": len(intersection),
                    "jaccard": round(jaccard, 3),
                    "sample_shared_functions": intersection[:10],
                }
            )

    findings.sort(key=lambda item: (str(item.get("type", "")), json.dumps(item, sort_keys=True)))
    return findings[:25]


def _scan_legacy_remnants(repo_root: Path) -> Dict[str, Any]:
    ps1_files: List[str] = []
    command_refs: List[Dict[str, Any]] = []

    for path in _iter_files(repo_root, LEGACY_SCAN_SKIP_DIRS):
        rel_path = _rel_path(path, repo_root)
        if path.suffix.lower() == ".ps1":
            ps1_files.append(rel_path)

    for path in _iter_files(repo_root, TEXT_SCAN_SKIP_DIRS):
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue

        rel_path = _rel_path(path, repo_root)
        if rel_path.startswith("tools/"):
            continue
        content = _read_text(path)
        if not content:
            continue

        line_hits: List[int] = []
        for lineno, line in enumerate(content.splitlines(), start=1):
            normalized = line.lower()
            if "pwsh" in normalized or "run.ps1" in normalized:
                line_hits.append(lineno)

        if line_hits:
            command_refs.append(
                {
                    "file": rel_path,
                    "line_hits": line_hits[:20],
                    "hit_count": len(line_hits),
                }
            )

    return {
        "powershell_files": sorted(ps1_files),
        "legacy_command_references": sorted(command_refs, key=lambda item: str(item.get("file", ""))),
    }


def _module_name_from_rel(rel_path: str) -> str:
    if not rel_path.endswith(".py"):
        return ""
    module = rel_path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    return module


def _resolve_relative_import(current_module: str, level: int, module: str) -> str:
    package_parts = current_module.split(".")[:-1]
    if level > 0:
        trim = max(0, level - 1)
        if trim > len(package_parts):
            return module
        package_parts = package_parts[: len(package_parts) - trim]

    if module:
        return ".".join(package_parts + module.split("."))
    return ".".join(package_parts)


def _resolve_module_to_known(module_name: str, module_map: Dict[str, str]) -> str:
    probe = module_name
    while probe:
        if probe in module_map:
            return probe
        if "." not in probe:
            break
        probe = probe.rsplit(".", 1)[0]
    return ""


def _collect_import_edges(repo_root: Path) -> Tuple[Dict[str, Set[str]], Dict[str, str], List[Dict[str, Any]]]:
    module_map: Dict[str, str] = {}
    for path in _iter_files(repo_root / "repo_audit_engine", SOURCE_SKIP_DIRS):
        if path.suffix.lower() != ".py":
            continue
        rel_path = _rel_path(path, repo_root)
        module = _module_name_from_rel(rel_path)
        if module:
            module_map[module] = rel_path

    adjacency: Dict[str, Set[str]] = {module: set() for module in sorted(module_map.keys())}
    edge_rows: List[Dict[str, Any]] = []

    for module, rel_path in sorted(module_map.items(), key=lambda item: item[0]):
        source_path = repo_root / rel_path
        source_text = _read_text(source_path)
        if not source_text.strip():
            continue
        try:
            tree = ast.parse(source_text)
        except SyntaxError:
            continue

        imports: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(str(alias.name))
            elif isinstance(node, ast.ImportFrom):
                raw_module = str(node.module or "")
                resolved = _resolve_relative_import(module, int(node.level), raw_module)
                imports.add(resolved)

        for imported in sorted(imports):
            if not imported.startswith("repo_audit_engine"):
                continue
            resolved_module = _resolve_module_to_known(imported, module_map)
            if not resolved_module or resolved_module == module:
                continue
            adjacency.setdefault(module, set()).add(resolved_module)
            edge_rows.append(
                {
                    "source": module,
                    "target": resolved_module,
                    "source_path": rel_path,
                    "target_path": module_map.get(resolved_module, ""),
                }
            )

    for targets in adjacency.values():
        for value in list(targets):
            if value not in adjacency:
                targets.remove(value)

    edge_rows.sort(key=lambda item: (str(item.get("source", "")), str(item.get("target", ""))))
    return adjacency, module_map, edge_rows


def _find_cycles(adjacency: Dict[str, Set[str]]) -> List[List[str]]:
    cycles: Set[Tuple[str, ...]] = set()
    visited: Set[str] = set()
    stack: List[str] = []
    on_stack: Set[str] = set()

    def dfs(node: str) -> None:
        visited.add(node)
        stack.append(node)
        on_stack.add(node)

        for target in sorted(adjacency.get(node, set())):
            if target not in visited:
                dfs(target)
                continue
            if target in on_stack:
                start_index = stack.index(target)
                cycle = stack[start_index:] + [target]
                cycle_tuple = tuple(cycle)
                rotations: List[Tuple[str, ...]] = []
                body = cycle_tuple[:-1]
                for index in range(len(body)):
                    rotated = body[index:] + body[:index] + (body[index],)
                    rotations.append(rotated)
                canonical = min(rotations)
                cycles.add(canonical)

        stack.pop()
        on_stack.remove(node)

    for node in sorted(adjacency.keys()):
        if node not in visited:
            dfs(node)

    return [list(cycle) for cycle in sorted(cycles)]


def _layer_for_module(module_name: str) -> str:
    if module_name in {"repo_audit_engine", "repo_audit_engine.cli", "repo_audit_engine.__main__"}:
        return "entrypoint"

    parts = module_name.split(".")
    if len(parts) < 3:
        return "entrypoint"

    domain = parts[1]
    if domain in {
        "core",
        "utils",
        "io",
        "manifest",
        "analysis",
        "graph",
        "runtime",
        "classification",
        "pipeline",
        "diagnostics",
    }:
        return domain

    return "entrypoint"


def _cross_layer_violations(edges: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rank = {
        "core": 0,
        "utils": 0,
        "io": 0,
        "manifest": 1,
        "analysis": 1,
        "graph": 1,
        "runtime": 2,
        "classification": 3,
        "pipeline": 4,
        "diagnostics": 5,
        "entrypoint": 6,
    }

    allowed = {
        ("repo_audit_engine.pipeline.orchestrator", "diagnostics"),
    }

    violations: List[Dict[str, Any]] = []

    for edge in edges:
        source_module = str(edge.get("source", ""))
        target_module = str(edge.get("target", ""))

        source_layer = _layer_for_module(source_module)
        target_layer = _layer_for_module(target_module)

        if (source_module, target_layer) in allowed:
            continue

        if source_layer == "diagnostics" and target_layer == "pipeline":
            violations.append(
                {
                    "source": source_module,
                    "target": target_module,
                    "reason": "diagnostics layer should not import pipeline orchestration modules",
                }
            )
            continue

        source_rank = rank.get(source_layer, -1)
        target_rank = rank.get(target_layer, -1)

        if source_layer == "entrypoint" or target_layer == "entrypoint":
            continue

        if source_rank < target_rank:
            violations.append(
                {
                    "source": source_module,
                    "target": target_module,
                    "reason": f"layer escalation from {source_layer} to {target_layer}",
                }
            )

    violations.sort(key=lambda item: (str(item.get("source", "")), str(item.get("target", ""))))
    return violations


def _dependency_sanity(repo_root: Path) -> Dict[str, Any]:
    adjacency, module_map, edges = _collect_import_edges(repo_root)
    cycles = _find_cycles(adjacency)
    violations = _cross_layer_violations(edges)

    return {
        "module_count": len(module_map),
        "import_edge_count": len(edges),
        "circular_imports": cycles,
        "cross_layer_violations": violations,
    }


def _check_diagnostics_are_observational(repo_root: Path) -> Dict[str, Any]:
    issues: List[str] = []
    orchestrator_path = repo_root / "repo_audit_engine" / "pipeline" / "orchestrator.py"
    source = _read_text(orchestrator_path)

    if not source.strip():
        return {"passed": False, "issues": ["orchestrator.py is missing or unreadable."]}

    required_assignment = 'system_valid = bool(validation_result.get("system_valid", False))'
    if required_assignment not in source:
        issues.append("system_valid is not sourced directly from verification output.")

    if '"score_adjustment_applied": False' not in source:
        issues.append("Diagnostics trust annotation no longer guarantees score_adjustment_applied=False.")

    try:
        tree = ast.parse(source)
    except SyntaxError:
        issues.append("orchestrator.py has syntax errors and could not be parsed.")
        return {"passed": False, "issues": issues}

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue

        for target in node.targets:
            if not isinstance(target, ast.Name) or target.id != "system_valid":
                continue
            value_repr = ast.unparse(node.value).lower()
            if "diagnostic" in value_repr:
                issues.append("system_valid assignment references diagnostics data.")

    return {"passed": len(issues) == 0, "issues": sorted(set(issues))}


def _check_runtime_graph_isolation(repo_root: Path) -> Dict[str, Any]:
    runtime_root = repo_root / "repo_audit_engine" / "runtime"
    issues: List[str] = []
    forbidden_tokens = [
        "dependency_graph.json",
        "validation_graph",
        "resolver_data",
        "build_dependency_graph(",
    ]

    for path in sorted(runtime_root.rglob("*.py")):
        rel_path = _rel_path(path, repo_root)
        source = _read_text(path)
        lowered = source.lower()
        for token in forbidden_tokens:
            if token.lower() in lowered:
                issues.append(f"{rel_path}: found forbidden token '{token}'")

    return {"passed": len(issues) == 0, "issues": sorted(set(issues))}


def _is_mutable_literal(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"list", "dict", "set"}:
        return True
    return False


def _check_pipeline_hidden_state(repo_root: Path) -> Dict[str, Any]:
    pipeline_root = repo_root / "repo_audit_engine" / "pipeline"
    issues: List[str] = []

    for path in sorted(pipeline_root.rglob("*.py")):
        rel_path = _rel_path(path, repo_root)
        source = _read_text(path)
        if not source.strip():
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            issues.append(f"{rel_path}: syntax_error")
            continue

        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if not isinstance(target, ast.Name):
                        continue
                    if target.id.isupper() or target.id == "__all__":
                        continue
                    if _is_mutable_literal(node.value):
                        issues.append(f"{rel_path}: mutable global '{target.id}'")
            elif isinstance(node, ast.AnnAssign):
                if not isinstance(node.target, ast.Name):
                    continue
                if node.target.id.isupper() or node.target.id == "__all__":
                    continue
                if node.value is not None and _is_mutable_literal(node.value):
                    issues.append(f"{rel_path}: mutable global '{node.target.id}'")

    return {"passed": len(issues) == 0, "issues": sorted(set(issues))}


def _check_stage_order() -> Dict[str, Any]:
    is_expected = list(FULL_STAGE_ORDER) == list(EXPECTED_STAGE_ORDER)
    issues: List[str] = []
    if not is_expected:
        issues.append(
            "FULL_STAGE_ORDER mismatch. "
            f"expected={EXPECTED_STAGE_ORDER}, actual={list(FULL_STAGE_ORDER)}"
        )
    return {"passed": is_expected, "issues": issues}


def _system_integrity_checks(repo_root: Path) -> Dict[str, Any]:
    diagnostics_check = _check_diagnostics_are_observational(repo_root)
    runtime_check = _check_runtime_graph_isolation(repo_root)
    hidden_state_check = _check_pipeline_hidden_state(repo_root)
    stage_order_check = _check_stage_order()

    checks = {
        "diagnostics_observational_only": diagnostics_check,
        "runtime_graph_isolation": runtime_check,
        "pipeline_hidden_state_free": hidden_state_check,
        "deterministic_stage_order": stage_order_check,
    }

    issues: List[str] = []
    for name, result in checks.items():
        if not bool(result.get("passed", False)):
            for issue in result.get("issues", []):
                issues.append(f"{name}: {issue}")

    return {
        "checks": checks,
        "passed": len(issues) == 0,
        "issues": sorted(issues),
    }


def _ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _run_pipeline_capture(repo_root: Path, output_dir: Path, bubble_mode: bool) -> PipelineRunCapture:
    _ensure_clean_dir(output_dir)

    stage_timings: Dict[str, float] = {}
    stage_sequence: List[str] = []

    original_append = pipeline_orchestrator.append_stage_event
    last_marker = time.perf_counter()

    def append_with_timing(log_path: Path, stage: str, status: str, details: Dict[str, Any]) -> None:
        nonlocal last_marker
        now = time.perf_counter()
        elapsed = round(max(0.0, now - last_marker), 4)
        stage_timings[str(stage)] = elapsed
        stage_sequence.append(str(stage))
        last_marker = now
        original_append(log_path, stage, status, details)

    pipeline_orchestrator.append_stage_event = append_with_timing
    started = time.perf_counter()
    try:
        payload = pipeline_orchestrator.run_staged_pipeline(
            repo_path=repo_root,
            output_dir=output_dir,
            bubble_mode=bool(bubble_mode),
            mode="full-pipeline",
        )
    finally:
        pipeline_orchestrator.append_stage_event = original_append

    total_seconds = round(max(0.0, time.perf_counter() - started), 4)
    stage_timings["total"] = total_seconds

    return PipelineRunCapture(
        payload=payload,
        stage_timings=stage_timings,
        stage_sequence=stage_sequence,
        total_seconds=total_seconds,
        output_dir=output_dir,
    )


def _validate_json_schema(payload: Dict[str, Any], required_keys: Sequence[str]) -> bool:
    return isinstance(payload, dict) and all(key in payload for key in required_keys)


def _contains_scoring_fields(payload: Any) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = str(key).strip().lower()
            if normalized in {"score", "domain_score"}:
                return True
            if _contains_scoring_fields(value):
                return True
        return False
    if isinstance(payload, list):
        return any(_contains_scoring_fields(item) for item in payload)
    return False


def _validate_artifact_contract(payload: Dict[str, Any]) -> Dict[str, Any]:
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}

    artifact_presence: Dict[str, bool] = {}
    for key, value in sorted(artifacts.items(), key=lambda item: item[0]):
        path = Path(str(value)) if str(value).strip() else None
        artifact_presence[key] = bool(path and path.exists())

    schema_validation: Dict[str, bool] = {}

    manifest_summary_path = Path(str(artifacts.get("manifest_summary_json", "")))
    if manifest_summary_path.exists():
        manifest_summary = _load_json(manifest_summary_path)
        schema_validation["manifest_summary_json"] = _validate_json_schema(
            manifest_summary,
            ["file_count", "python_file_count", "entrypoints", "language_counts"],
        )
    else:
        schema_validation["manifest_summary_json"] = False

    manifest_jsonl_path = Path(str(artifacts.get("manifest_jsonl", "")))
    if manifest_jsonl_path.exists():
        manifest_rows = _load_jsonl(manifest_jsonl_path)
        row_ok = bool(manifest_rows) and _validate_json_schema(
            manifest_rows[0],
            ["path", "sha256", "language", "imports", "symbols"],
        )
        schema_validation["manifest_jsonl"] = row_ok
    else:
        schema_validation["manifest_jsonl"] = False

    static_jsonl_path = Path(str(artifacts.get("static_analysis_jsonl", "")))
    if static_jsonl_path.exists():
        static_rows = _load_jsonl(static_jsonl_path)
        row_ok = bool(static_rows) and _validate_json_schema(
            static_rows[0],
            ["file_path", "functions", "classes", "imports", "calls"],
        )
        schema_validation["static_analysis_jsonl"] = row_ok
    else:
        schema_validation["static_analysis_jsonl"] = False

    dependency_graph_path = Path(str(artifacts.get("dependency_graph_json", "")))
    if dependency_graph_path.exists():
        graph_payload = _load_json(dependency_graph_path)
        schema_validation["dependency_graph_json"] = _validate_json_schema(
            graph_payload,
            ["nodes", "edges", "validation_graph", "resolver_data", "summary"],
        )
    else:
        schema_validation["dependency_graph_json"] = False

    runtime_flow_path = Path(str(artifacts.get("execution_flow_graph_json", "")))
    if runtime_flow_path.exists():
        runtime_payload = _load_json(runtime_flow_path)
        schema_validation["execution_flow_graph_json"] = _validate_json_schema(
            runtime_payload,
            ["bubble_mode", "entrypoint_runs", "nodes", "edges", "summary", "node_hits"],
        )
    else:
        schema_validation["execution_flow_graph_json"] = False

    heat_path = Path(str(artifacts.get("heat_classification_json", "")))
    if heat_path.exists():
        heat_payload = _load_json(heat_path)
        base_schema_ok = _validate_json_schema(
            heat_payload,
            ["distribution", "nodes"],
        )
        node_rows = heat_payload.get("nodes") if isinstance(heat_payload.get("nodes"), list) else []
        first_node = node_rows[0] if node_rows and isinstance(node_rows[0], dict) else {}
        has_node_identity = bool(str(first_node.get("node_id", first_node.get("id", ""))).strip()) if first_node else True
        has_classification = bool(
            str(first_node.get("classification", first_node.get("heat", ""))).strip()
        ) if first_node else True
        has_evidence = isinstance(first_node.get("evidence"), dict) if first_node else True

        schema_validation["heat_classification_json"] = bool(
            base_schema_ok and has_node_identity and has_classification and has_evidence
        )
    else:
        schema_validation["heat_classification_json"] = False

    dead_path = Path(str(artifacts.get("dead_code_report_json", "")))
    if dead_path.exists():
        dead_payload = _load_json(dead_path)
        schema_validation["dead_code_report_json"] = _validate_json_schema(
            dead_payload,
            ["rule_weights", "summary", "candidates", "dead_candidates"],
        )
    else:
        schema_validation["dead_code_report_json"] = False

    validation_path = Path(str(artifacts.get("validation_result_json", "")))
    if validation_path.exists():
        validation_payload = _load_json(validation_path)
        schema_validation["validation_result_json"] = _validate_json_schema(
            validation_payload,
            ["status", "system_valid", "trust_score", "trust_breakdown"],
        )
    else:
        schema_validation["validation_result_json"] = False

    final_report_path = Path(str(artifacts.get("final_report_json", "")))
    if final_report_path.exists():
        final_payload = _load_json(final_report_path)
        required_keys = [
            "summary",
            "diagnostics",
            "trust",
            "stats",
            "runtime_execution_coverage",
            "structural_audit",
            "behavioral_audit",
            "redundancy_overlap_audit",
            "architectural_quality_audit",
            "architect_auditor",
            "design_quality_signals",
            "audit_layers",
        ]
        base_schema_ok = _validate_json_schema(final_payload, required_keys)

        layer_keys = [
            "structural_audit",
            "behavioral_audit",
            "redundancy_overlap_audit",
            "architectural_quality_audit",
        ]
        layer_schema_ok = all(
            isinstance(final_payload.get(layer_name), dict)
            and bool(str((final_payload.get(layer_name) or {}).get("title", "")).strip())
            for layer_name in layer_keys
        )

        design_quality = final_payload.get("design_quality_signals") if isinstance(final_payload.get("design_quality_signals"), dict) else {}
        principle_enforcement = (
            design_quality.get("principle_enforcement")
            if isinstance(design_quality.get("principle_enforcement"), dict)
            else {}
        )
        required_principles = [
            "intent_over_execution",
            "structure_over_runtime_alone",
            "multi_signal_confirmation_over_single_metric",
        ]
        principles_ok = all(
            isinstance(principle_enforcement.get(principle), dict)
            and bool((principle_enforcement.get(principle) or {}).get("enforced", False))
            and isinstance((principle_enforcement.get(principle) or {}).get("status"), str)
            for principle in required_principles
        )

        architect_auditor = final_payload.get("architect_auditor") if isinstance(final_payload.get("architect_auditor"), dict) else {}
        questions = architect_auditor.get("questions") if isinstance(architect_auditor.get("questions"), dict) else {}
        required_questions = [
            "structure_matches_intended_architecture",
            "responsibility_clean_or_duplicated",
            "behavior_aligns_with_structure",
        ]
        questions_ok = all(
            isinstance(questions.get(name), dict)
            and isinstance((questions.get(name) or {}).get("status"), str)
            and isinstance((questions.get(name) or {}).get("violations"), list)
            for name in required_questions
        )

        hard_constraints = (
            architect_auditor.get("hard_constraints")
            if isinstance(architect_auditor.get("hard_constraints"), dict)
            else {}
        )
        constraints_ok = all(
            bool(hard_constraints.get(name, False))
            for name in [
                "no_new_scoring_systems",
                "no_upstream_artifact_mutation",
                "no_feedback_loops",
                "deterministic_outputs",
            ]
        )

        taxonomy = architect_auditor.get("violation_taxonomy") if isinstance(architect_auditor.get("violation_taxonomy"), list) else []
        taxonomy_ok = taxonomy == [
            "layer_violation",
            "circular_dependency",
            "redundant_domain",
            "orphan_module",
            "overcoupled_node",
        ]

        no_scoring_fields_ok = not _contains_scoring_fields(architect_auditor)

        schema_validation["final_report_json"] = bool(
            base_schema_ok
            and layer_schema_ok
            and principles_ok
            and questions_ok
            and constraints_ok
            and taxonomy_ok
            and no_scoring_fields_ok
        )
    else:
        schema_validation["final_report_json"] = False

    events_path = Path(str(artifacts.get("pipeline_events_jsonl", "")))
    if events_path.exists():
        event_rows = _load_jsonl(events_path)
        row_ok = bool(event_rows) and _validate_json_schema(
            event_rows[0],
            ["stage", "status", "details"],
        )
        expected_stages = set(EXPECTED_STAGE_ORDER)
        present_stages = {str(row.get("stage", "")) for row in event_rows}
        schema_validation["pipeline_events_jsonl"] = row_ok and expected_stages.issubset(present_stages)
    else:
        schema_validation["pipeline_events_jsonl"] = False

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    trust = payload.get("trust") if isinstance(payload.get("trust"), dict) else {}
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}

    no_null_critical_sections = all(
        [
            isinstance(summary.get("status"), str) and bool(str(summary.get("status")).strip()),
            isinstance(summary.get("root_cause"), str),
            isinstance(summary.get("confidence"), (int, float)),
            isinstance(diagnostics.get("status"), str),
            isinstance(diagnostics.get("top_issues"), list),
            isinstance(trust.get("score"), (int, float)),
            isinstance(trust.get("breakdown"), dict),
            isinstance(validation.get("status"), str),
            "trust_score" in validation,
        ]
    )

    return {
        "artifact_presence": artifact_presence,
        "schema_validation": schema_validation,
        "no_null_critical_sections": bool(no_null_critical_sections),
    }


def _runtime_validation(payload: Dict[str, Any]) -> Dict[str, Any]:
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    trace_path = Path(str(artifacts.get("runtime_trace_jsonl", "")))
    flow_path = Path(str(artifacts.get("execution_flow_graph_json", "")))

    trace_rows = _load_jsonl(trace_path) if trace_path.exists() else []
    flow_payload = _load_json(flow_path) if flow_path.exists() else {}

    summary = flow_payload.get("summary") if isinstance(flow_payload.get("summary"), dict) else {}
    entrypoint_runs = flow_payload.get("entrypoint_runs") if isinstance(flow_payload.get("entrypoint_runs"), list) else []

    traced_entrypoints = sorted(
        {
            str(item.get("entrypoint", "")).strip()
            for item in entrypoint_runs
            if isinstance(item, dict) and str(item.get("entrypoint", "")).strip()
        }
    )

    bubble_mode_executed = bool(flow_payload.get("bubble_mode", False)) and int(summary.get("run_count", 0) or 0) >= 1
    runtime_event_stream_present = len(trace_rows) > 0
    execution_graph_generated = isinstance(flow_payload.get("nodes"), list) and isinstance(flow_payload.get("edges"), list)

    module_hits = flow_payload.get("module_hits") if isinstance(flow_payload.get("module_hits"), dict) else {}
    local_module_count = len([name for name in module_hits.keys() if _is_local_module_name(str(name))])

    return {
        "bubble_mode_executed": bubble_mode_executed,
        "runtime_event_stream_present": runtime_event_stream_present,
        "execution_graph_generated": execution_graph_generated,
        "runtime_event_count": len(trace_rows),
        "traced_entrypoints": traced_entrypoints,
        "module_count": len(module_hits),
        "local_module_count": local_module_count,
        "runtime_summary": {
            "run_count": int(summary.get("run_count", 0) or 0),
            "call_event_count": int(summary.get("call_event_count", 0) or 0),
            "import_event_count": int(summary.get("import_event_count", 0) or 0),
            "line_event_count": int(summary.get("line_event_count", 0) or 0),
            "timeout_count": int(summary.get("timeout_count", 0) or 0),
        },
    }


def _normalize_path_text(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip()


def _is_local_path(path_text: str) -> bool:
    normalized = _normalize_path_text(path_text).lstrip("./")
    if not normalized:
        return False
    return any(normalized.startswith(prefix) for prefix in LOCAL_CODE_PATH_PREFIXES)


def _is_local_module_name(module_name: str) -> bool:
    normalized = str(module_name or "").strip()
    if not normalized:
        return False
    for prefix in LOCAL_MODULE_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "."):
            return True
    return False


def _is_local_node_id(node_id: str) -> bool:
    raw = str(node_id or "").strip()
    if not raw or ":" not in raw:
        return False
    _, payload = raw.split(":", 1)
    payload = _normalize_path_text(payload)
    if "#" in payload:
        payload = payload.split("#", 1)[0]
    return _is_local_path(payload)


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _estimate_graph_depth(adjacency: Dict[str, Set[str]]) -> int:
    node_set: Set[str] = set(adjacency.keys())
    for targets in adjacency.values():
        node_set.update(targets)

    if not node_set:
        return 0

    depth = {node: 1 for node in node_set}
    max_iterations = min(len(node_set), 1024)

    for _ in range(max_iterations):
        changed = False
        for source, targets in adjacency.items():
            source_depth = depth.get(source, 1)
            for target in targets:
                candidate = min(source_depth + 1, len(node_set))
                if candidate > depth.get(target, 1):
                    depth[target] = candidate
                    changed = True
        if not changed:
            break

    return max(depth.values())


def _extract_local_edge_set(edges: Sequence[Dict[str, Any]], edge_type: str | None = None) -> Set[Tuple[str, str]]:
    edge_set: Set[Tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if edge_type is not None and str(edge.get("type", "")).strip() != edge_type:
            continue

        source = str(edge.get("source", "")).strip()
        target = str(edge.get("target", "")).strip()
        if not source or not target:
            continue
        if not _is_local_node_id(source) or not _is_local_node_id(target):
            continue

        edge_set.add((source, target))

    return edge_set


def _collect_runtime_call_metrics(trace_rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    adjacency: Dict[str, Set[str]] = {}
    unique_functions: Set[str] = set()
    unique_local_functions: Set[str] = set()
    local_call_event_count = 0
    total_call_event_count = 0

    for row in trace_rows:
        if str(row.get("event", "")).strip() != "call":
            continue

        total_call_event_count += 1

        file_path = _normalize_path_text(row.get("file", ""))
        module_name = str(row.get("module", "")).strip()
        function_name = str(row.get("function", "")).strip() or "<unknown>"
        caller_node_id = str(row.get("caller_node_id", "")).strip()
        callee_node_id = str(row.get("callee_node_id", "")).strip()

        callee_base = file_path or module_name
        callee_label = f"{callee_base}:{function_name}" if callee_base else ""
        if callee_label:
            unique_functions.add(callee_label)

        if _is_local_path(file_path):
            local_call_event_count += 1
            if function_name not in {"<module>", "<import>", "<unknown>"}:
                unique_local_functions.add(f"{file_path}:{function_name}")

        if caller_node_id and callee_node_id:
            adjacency.setdefault(caller_node_id, set()).add(callee_node_id)
            continue

        caller = str(row.get("caller", "")).strip()
        if caller and callee_label:
            adjacency.setdefault(caller, set()).add(callee_label)

    return {
        "total_call_event_count": total_call_event_count,
        "local_call_event_count": local_call_event_count,
        "unique_functions_called": len(unique_functions),
        "unique_local_functions_called": len(unique_local_functions),
        "max_call_depth": _estimate_graph_depth(adjacency),
    }


def _build_runtime_meaningfulness(
    trace_rows: Sequence[Dict[str, Any]],
    flow_payload: Dict[str, Any],
    thresholds: Dict[str, Any],
) -> Dict[str, Any]:
    summary = flow_payload.get("summary") if isinstance(flow_payload.get("summary"), dict) else {}
    module_hits = flow_payload.get("module_hits") if isinstance(flow_payload.get("module_hits"), dict) else {}

    call_metrics = _collect_runtime_call_metrics(trace_rows)

    modules_executed = len(module_hits)
    local_modules_executed = len([name for name in module_hits.keys() if _is_local_module_name(str(name))])

    call_event_count = _safe_int(summary.get("call_event_count", call_metrics.get("total_call_event_count", 0)))
    import_event_count = _safe_int(summary.get("import_event_count", 0))

    low_information_reasons: List[str] = []
    if modules_executed < _safe_int(thresholds.get("min_modules_executed", 0)):
        low_information_reasons.append(
            f"modules_executed={modules_executed} below min_modules_executed={_safe_int(thresholds.get('min_modules_executed', 0))}"
        )

    if local_modules_executed < _safe_int(thresholds.get("min_local_modules_executed", 0)):
        low_information_reasons.append(
            "local_modules_executed="
            f"{local_modules_executed} below min_local_modules_executed={_safe_int(thresholds.get('min_local_modules_executed', 0))}"
        )

    if _safe_int(call_metrics.get("unique_functions_called", 0)) < _safe_int(
        thresholds.get("min_unique_functions_called", 0)
    ):
        low_information_reasons.append(
            "unique_functions_called="
            f"{_safe_int(call_metrics.get('unique_functions_called', 0))} "
            f"below min_unique_functions_called={_safe_int(thresholds.get('min_unique_functions_called', 0))}"
        )

    if _safe_int(call_metrics.get("unique_local_functions_called", 0)) < _safe_int(
        thresholds.get("min_unique_local_functions_called", 0)
    ):
        low_information_reasons.append(
            "unique_local_functions_called="
            f"{_safe_int(call_metrics.get('unique_local_functions_called', 0))} "
            "below min_unique_local_functions_called="
            f"{_safe_int(thresholds.get('min_unique_local_functions_called', 0))}"
        )

    if _safe_int(call_metrics.get("max_call_depth", 0)) < _safe_int(thresholds.get("min_call_depth", 0)):
        low_information_reasons.append(
            f"max_call_depth={_safe_int(call_metrics.get('max_call_depth', 0))} below min_call_depth={_safe_int(thresholds.get('min_call_depth', 0))}"
        )

    if call_event_count <= import_event_count:
        low_information_reasons.append(
            f"call_event_count={call_event_count} is not greater than import_event_count={import_event_count}"
        )

    passed = len(low_information_reasons) == 0

    return {
        "passed": passed,
        "modules_executed": modules_executed,
        "local_modules_executed": local_modules_executed,
        "call_event_count": call_event_count,
        "import_event_count": import_event_count,
        "unique_functions_called": _safe_int(call_metrics.get("unique_functions_called", 0)),
        "unique_local_functions_called": _safe_int(call_metrics.get("unique_local_functions_called", 0)),
        "local_call_event_count": _safe_int(call_metrics.get("local_call_event_count", 0)),
        "max_call_depth": _safe_int(call_metrics.get("max_call_depth", 0)),
        "low_information_reasons": low_information_reasons,
        "issues": list(low_information_reasons),
    }


def _sample_edges(edges: Set[Tuple[str, str]], limit: int = 20) -> List[Dict[str, str]]:
    sampled: List[Dict[str, str]] = []
    for source, target in sorted(edges)[: max(0, int(limit))]:
        sampled.append({"source": source, "target": target})
    return sampled


def _build_runtime_static_reconciliation(
    dependency_payload: Dict[str, Any],
    flow_payload: Dict[str, Any],
    thresholds: Dict[str, Any],
) -> Dict[str, Any]:
    dependency_edges = dependency_payload.get("edges") if isinstance(dependency_payload.get("edges"), list) else []
    runtime_edges = flow_payload.get("edges") if isinstance(flow_payload.get("edges"), list) else []

    static_call_edges = _extract_local_edge_set(dependency_edges, edge_type="CALL")
    runtime_call_edges = _extract_local_edge_set(runtime_edges, edge_type="RUNTIME_CALL")

    shared_edges = static_call_edges.intersection(runtime_call_edges)
    runtime_only_edges = runtime_call_edges.difference(static_call_edges)
    static_only_edges = static_call_edges.difference(runtime_call_edges)

    union_count = len(static_call_edges.union(runtime_call_edges))
    overlap_ratio = len(shared_edges) / float(max(1, union_count))
    static_confirmation_ratio = len(shared_edges) / float(max(1, len(static_call_edges)))
    runtime_alignment_ratio = len(shared_edges) / float(max(1, len(runtime_call_edges)))

    issues: List[str] = []
    if not static_call_edges:
        issues.append("Static graph does not contain local CALL edges for reconciliation.")

    if not runtime_call_edges:
        issues.append("Runtime graph does not contain local RUNTIME_CALL edges for reconciliation.")

    min_confirmed_ratio = _safe_float(thresholds.get("min_runtime_confirmed_edge_ratio", 0.0))
    if static_confirmation_ratio < min_confirmed_ratio:
        issues.append(
            "runtime_confirmed_edge_ratio="
            f"{static_confirmation_ratio:.4f} below min_runtime_confirmed_edge_ratio={min_confirmed_ratio:.4f}"
        )

    passed = len(issues) == 0

    return {
        "passed": passed,
        "static_edge_count": len(static_call_edges),
        "runtime_edge_count": len(runtime_call_edges),
        "shared_edge_count": len(shared_edges),
        "runtime_only_edge_count": len(runtime_only_edges),
        "static_only_edge_count": len(static_only_edges),
        "overlap_ratio": round(overlap_ratio, 6),
        "runtime_alignment_ratio": round(runtime_alignment_ratio, 6),
        "static_confirmation_ratio": round(static_confirmation_ratio, 6),
        "shared_edges_sample": _sample_edges(shared_edges),
        "runtime_only_edges_sample": _sample_edges(runtime_only_edges),
        "static_only_edges_sample": _sample_edges(static_only_edges),
        "issues": issues,
    }


def _build_graph_sanity(
    dependency_payload: Dict[str, Any],
    manifest_payload: Dict[str, Any],
    runtime_static_reconciliation: Dict[str, Any],
    thresholds: Dict[str, Any],
) -> Dict[str, Any]:
    node_rows = dependency_payload.get("nodes") if isinstance(dependency_payload.get("nodes"), list) else []
    edge_rows = dependency_payload.get("edges") if isinstance(dependency_payload.get("edges"), list) else []

    local_nodes = {
        str(node.get("id", "")).strip()
        for node in node_rows
        if isinstance(node, dict) and _is_local_node_id(str(node.get("id", "")))
    }

    local_edges = _extract_local_edge_set(edge_rows)

    adjacency: Dict[str, Set[str]] = {}
    inbound: Dict[str, Set[str]] = {}
    for source, target in local_edges:
        adjacency.setdefault(source, set()).add(target)
        inbound.setdefault(target, set()).add(source)

    entrypoint_paths = (
        manifest_payload.get("entrypoints") if isinstance(manifest_payload.get("entrypoints"), list) else []
    )
    entrypoint_nodes = {
        f"file:{_normalize_path_text(path)}"
        for path in entrypoint_paths
        if _is_local_path(str(path))
    }
    entrypoint_nodes = {node_id for node_id in entrypoint_nodes if node_id in local_nodes}

    if not entrypoint_nodes:
        for fallback in ("file:repo_audit_engine/__main__.py", "file:repo_audit_engine/cli.py"):
            if fallback in local_nodes:
                entrypoint_nodes.add(fallback)

    reachable_nodes: Set[str] = set(entrypoint_nodes)
    queue = list(entrypoint_nodes)
    while queue:
        current = queue.pop(0)
        for target in sorted(adjacency.get(current, set())):
            if target in reachable_nodes:
                continue
            reachable_nodes.add(target)
            queue.append(target)

    isolated_count = sum(
        1
        for node in local_nodes
        if not adjacency.get(node, set()) and not inbound.get(node, set())
    )

    local_node_count = len(local_nodes)
    reachable_ratio = len(reachable_nodes) / float(max(1, local_node_count))
    isolated_ratio = isolated_count / float(max(1, local_node_count))
    runtime_confirmed_ratio = _safe_float(runtime_static_reconciliation.get("static_confirmation_ratio", 0.0))

    issues: List[str] = []
    if not entrypoint_nodes:
        issues.append("No local entrypoint nodes were found in the static dependency graph.")

    min_reachable = _safe_float(thresholds.get("min_reachable_node_ratio", 0.0))
    if reachable_ratio < min_reachable:
        issues.append(
            f"reachable_node_ratio={reachable_ratio:.4f} below min_reachable_node_ratio={min_reachable:.4f}"
        )

    max_isolated = _safe_float(thresholds.get("max_isolated_node_ratio", 1.0))
    if isolated_ratio > max_isolated:
        issues.append(
            f"isolated_node_ratio={isolated_ratio:.4f} above max_isolated_node_ratio={max_isolated:.4f}"
        )

    min_confirmed_ratio = _safe_float(thresholds.get("min_runtime_confirmed_edge_ratio", 0.0))
    if runtime_confirmed_ratio < min_confirmed_ratio:
        issues.append(
            "runtime_confirmed_edge_ratio="
            f"{runtime_confirmed_ratio:.4f} below min_runtime_confirmed_edge_ratio={min_confirmed_ratio:.4f}"
        )

    passed = len(issues) == 0

    return {
        "passed": passed,
        "local_node_count": local_node_count,
        "local_edge_count": len(local_edges),
        "entrypoint_node_count": len(entrypoint_nodes),
        "reachable_node_count": len(reachable_nodes),
        "reachable_node_ratio": round(reachable_ratio, 6),
        "isolated_node_count": isolated_count,
        "isolated_node_ratio": round(isolated_ratio, 6),
        "runtime_confirmed_edge_ratio": round(runtime_confirmed_ratio, 6),
        "entrypoint_nodes": sorted(entrypoint_nodes),
        "issues": issues,
    }


def _build_classification_quality(
    heat_payload: Dict[str, Any],
    dependency_payload: Dict[str, Any],
    runtime_validation: Dict[str, Any],
) -> Dict[str, Any]:
    heat_nodes = heat_payload.get("nodes") if isinstance(heat_payload.get("nodes"), list) else []
    distribution = heat_payload.get("distribution") if isinstance(heat_payload.get("distribution"), dict) else {}
    heat_runtime_validation = (
        heat_payload.get("runtime_validation") if isinstance(heat_payload.get("runtime_validation"), dict) else {}
    )

    edge_rows = dependency_payload.get("edges") if isinstance(dependency_payload.get("edges"), list) else []
    local_edges = _extract_local_edge_set(edge_rows)

    adjacency: Dict[str, Set[str]] = {}
    for source, target in local_edges:
        adjacency.setdefault(source, set()).add(target)

    hot_nodes: Set[str] = set()
    warm_nodes: Set[str] = set()
    dead_conflict_nodes: List[str] = []
    dead_non_executable_nodes: List[str] = []

    for node in heat_nodes:
        if not isinstance(node, dict):
            continue

        node_id = str(node.get("id", node.get("node_id", ""))).strip()
        if not _is_local_node_id(node_id):
            continue

        heat_label = str(node.get("classification", node.get("heat", ""))).strip().upper()
        evidence = node.get("evidence") if isinstance(node.get("evidence"), dict) else {}

        if heat_label == "HOT":
            hot_nodes.add(node_id)
        elif heat_label == "WARM":
            warm_nodes.add(node_id)

        if heat_label == "DEAD":
            runtime_hits = _safe_int(node.get("runtime_hits", evidence.get("runtime_hits", 0)))
            executable_references = _safe_int(
                node.get("executable_references", evidence.get("executable_references", 0))
            )
            inbound_edges = _safe_int(node.get("inbound_edges", evidence.get("inbound_edges", 0)))
            reachable_from_runtime = bool(
                node.get("reachable_from_runtime", evidence.get("reachable_from_runtime", False))
            )
            non_executable_references = _safe_int(
                node.get("non_executable_references", evidence.get("non_executable_references", 0))
            )

            # Under v2 rules, DEAD may keep non-executable references but cannot keep
            # runtime hits, executable CALL references, runtime reachability, or inbound edges.
            if runtime_hits > 0 or executable_references > 0 or reachable_from_runtime or inbound_edges > 0:
                dead_conflict_nodes.append(node_id)
            if non_executable_references > 0:
                dead_non_executable_nodes.append(node_id)

    reachable_from_hot: Set[str] = set(hot_nodes)
    queue = list(hot_nodes)
    while queue:
        current = queue.pop(0)
        for target in sorted(adjacency.get(current, set())):
            if target in reachable_from_hot:
                continue
            reachable_from_hot.add(target)
            queue.append(target)

    warm_unreachable_nodes = sorted(node for node in warm_nodes if node not in reachable_from_hot)

    runtime_summary = runtime_validation.get("runtime_summary") if isinstance(runtime_validation.get("runtime_summary"), dict) else {}
    runtime_present = bool(runtime_validation.get("runtime_event_stream_present", False)) and _safe_int(
        runtime_summary.get("call_event_count", 0)
    ) > 0
    classifier_runtime_validation_passed = bool(heat_runtime_validation.get("passed", True))

    issues: List[str] = []
    if runtime_present and len(hot_nodes) == 0 and len(warm_nodes) == 0:
        issues.append("Runtime events exist but no HOT/WARM nodes were classified.")

    if runtime_present and not classifier_runtime_validation_passed:
        issues.append("Classifier runtime validation failed in heat payload.")

    if dead_conflict_nodes:
        issues.append(
            "DEAD nodes still have runtime hits or executable references: "
            f"{len(dead_conflict_nodes)}"
        )

    dead_ratio = _safe_int(distribution.get("DEAD", 0)) / float(max(1, len(heat_nodes)))
    if runtime_present and dead_ratio > 0.70:
        issues.append(f"DEAD ratio remains high under runtime evidence: {dead_ratio:.3f}")

    if warm_unreachable_nodes:
        issues.append(
            "WARM nodes are not reachable from HOT nodes in the static graph: "
            f"{len(warm_unreachable_nodes)}"
        )

    passed = len(issues) == 0

    return {
        "passed": passed,
        "runtime_present": runtime_present,
        "distribution": {str(key): _safe_int(value) for key, value in sorted(distribution.items())},
        "hot_node_count": len(hot_nodes),
        "warm_node_count": len(warm_nodes),
        "dead_ratio": round(dead_ratio, 6),
        "classifier_runtime_validation_passed": classifier_runtime_validation_passed,
        "dead_referenced_count": len(dead_conflict_nodes),
        "dead_non_executable_count": len(dead_non_executable_nodes),
        "warm_unreachable_count": len(warm_unreachable_nodes),
        "dead_referenced_nodes_sample": sorted(dead_conflict_nodes)[:20],
        "dead_non_executable_nodes_sample": sorted(dead_non_executable_nodes)[:20],
        "warm_unreachable_nodes_sample": warm_unreachable_nodes[:20],
        "issues": issues,
    }


def _truth_validation_layer(
    payload: Dict[str, Any],
    runtime_validation: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}

    trace_path = Path(str(artifacts.get("runtime_trace_jsonl", "")))
    flow_path = Path(str(artifacts.get("execution_flow_graph_json", "")))
    dependency_path = Path(str(artifacts.get("dependency_graph_json", "")))
    manifest_summary_path = Path(str(artifacts.get("manifest_summary_json", "")))
    heat_path = Path(str(artifacts.get("heat_classification_json", "")))

    trace_rows = _load_jsonl(trace_path) if trace_path.exists() else []
    flow_payload = _load_json(flow_path) if flow_path.exists() else {}
    dependency_payload = _load_json(dependency_path) if dependency_path.exists() else {}
    manifest_payload = _load_json(manifest_summary_path) if manifest_summary_path.exists() else {}
    heat_payload = _load_json(heat_path) if heat_path.exists() else {}

    runtime_state = runtime_validation or _runtime_validation(payload)
    thresholds = dict(TRUTH_VALIDATION_THRESHOLDS)

    runtime_meaningfulness = _build_runtime_meaningfulness(
        trace_rows=trace_rows,
        flow_payload=flow_payload,
        thresholds=thresholds,
    )
    runtime_static_reconciliation = _build_runtime_static_reconciliation(
        dependency_payload=dependency_payload,
        flow_payload=flow_payload,
        thresholds=thresholds,
    )
    graph_sanity = _build_graph_sanity(
        dependency_payload=dependency_payload,
        manifest_payload=manifest_payload,
        runtime_static_reconciliation=runtime_static_reconciliation,
        thresholds=thresholds,
    )
    classification_quality = _build_classification_quality(
        heat_payload=heat_payload,
        dependency_payload=dependency_payload,
        runtime_validation=runtime_state,
    )

    critical_issues: List[str] = []
    warnings: List[str] = []

    for section_name, section in (
        ("runtime_meaningfulness", runtime_meaningfulness),
        ("runtime_static_reconciliation", runtime_static_reconciliation),
        ("graph_sanity", graph_sanity),
        ("classification_quality", classification_quality),
    ):
        if not bool(section.get("passed", False)):
            section_issues = section.get("issues") if isinstance(section.get("issues"), list) else []
            if section_issues:
                critical_issues.extend([f"{section_name}: {item}" for item in section_issues])
            else:
                critical_issues.append(f"{section_name}: failed without detailed issues")

    if runtime_meaningfulness.get("passed", False) and runtime_static_reconciliation.get("shared_edge_count", 0) == 0:
        warnings.append("Runtime trace is non-empty but no static CALL edges were confirmed by runtime edges.")

    passed = len(critical_issues) == 0

    return {
        "passed": passed,
        "thresholds": thresholds,
        "runtime_meaningfulness": runtime_meaningfulness,
        "runtime_static_reconciliation": runtime_static_reconciliation,
        "graph_sanity": graph_sanity,
        "classification_quality": classification_quality,
        "critical_issues": critical_issues,
        "warnings": warnings,
    }


def _semantic_truth_profile(capture: PipelineRunCapture) -> Dict[str, Any]:
    payload = capture.payload
    runtime_validation = _runtime_validation(payload)
    truth_validation = _truth_validation_layer(payload, runtime_validation=runtime_validation)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    validation = payload.get("validation") if isinstance(payload.get("validation"), dict) else {}

    runtime_summary = runtime_validation.get("runtime_summary") if isinstance(runtime_validation.get("runtime_summary"), dict) else {}
    runtime_meaningfulness = (
        truth_validation.get("runtime_meaningfulness")
        if isinstance(truth_validation.get("runtime_meaningfulness"), dict)
        else {}
    )
    runtime_reconciliation = (
        truth_validation.get("runtime_static_reconciliation")
        if isinstance(truth_validation.get("runtime_static_reconciliation"), dict)
        else {}
    )
    graph_sanity = truth_validation.get("graph_sanity") if isinstance(truth_validation.get("graph_sanity"), dict) else {}
    classification_quality = (
        truth_validation.get("classification_quality")
        if isinstance(truth_validation.get("classification_quality"), dict)
        else {}
    )

    return {
        "stage_sequence": list(capture.stage_sequence),
        "summary_status": str(summary.get("status", "UNKNOWN")),
        "system_valid": bool(payload.get("system_valid", False)),
        "validation_status": str(validation.get("status", "UNKNOWN")),
        "validation_trust_score": _safe_float(validation.get("trust_score", 0.0)),
        "runtime": {
            "runtime_event_count": _safe_int(runtime_validation.get("runtime_event_count", 0)),
            "module_count": _safe_int(runtime_validation.get("module_count", 0)),
            "local_module_count": _safe_int(runtime_validation.get("local_module_count", 0)),
            "call_event_count": _safe_int(runtime_summary.get("call_event_count", 0)),
            "import_event_count": _safe_int(runtime_summary.get("import_event_count", 0)),
            "unique_functions_called": _safe_int(runtime_meaningfulness.get("unique_functions_called", 0)),
            "unique_local_functions_called": _safe_int(
                runtime_meaningfulness.get("unique_local_functions_called", 0)
            ),
            "max_call_depth": _safe_int(runtime_meaningfulness.get("max_call_depth", 0)),
        },
        "graph": {
            "local_node_count": _safe_int(graph_sanity.get("local_node_count", 0)),
            "local_edge_count": _safe_int(graph_sanity.get("local_edge_count", 0)),
            "reachable_node_ratio": round(_safe_float(graph_sanity.get("reachable_node_ratio", 0.0)), 6),
            "isolated_node_ratio": round(_safe_float(graph_sanity.get("isolated_node_ratio", 0.0)), 6),
            "runtime_confirmed_edge_ratio": round(
                _safe_float(runtime_reconciliation.get("static_confirmation_ratio", 0.0)), 6
            ),
        },
        "classification": {
            "distribution": (
                classification_quality.get("distribution")
                if isinstance(classification_quality.get("distribution"), dict)
                else {}
            ),
            "dead_referenced_count": _safe_int(classification_quality.get("dead_referenced_count", 0)),
            "warm_unreachable_count": _safe_int(classification_quality.get("warm_unreachable_count", 0)),
        },
        "truth_validation_passed": bool(truth_validation.get("passed", False)),
    }


def _collect_value_differences(left: Any, right: Any, max_items: int = 50) -> List[str]:
    differences: List[str] = []

    def _walk(lhs: Any, rhs: Any, path: str) -> None:
        if len(differences) >= max_items:
            return

        if type(lhs) is not type(rhs):
            differences.append(path or "<root>")
            return

        if isinstance(lhs, dict):
            lhs_keys = set(lhs.keys())
            rhs_keys = set(rhs.keys())
            for key in sorted(lhs_keys.union(rhs_keys)):
                next_path = f"{path}.{key}" if path else str(key)
                if key not in lhs or key not in rhs:
                    differences.append(next_path)
                    if len(differences) >= max_items:
                        return
                    continue
                _walk(lhs[key], rhs[key], next_path)
            return

        if isinstance(lhs, list):
            if lhs != rhs:
                differences.append(path or "<root>")
            return

        if lhs != rhs:
            differences.append(path or "<root>")

    _walk(left, right, "")
    return differences


def _normalize_for_hash(value: Any, output_marker: str) -> Any:
    if isinstance(value, dict):
        normalized: Dict[str, Any] = {}
        for key in sorted(value.keys()):
            if key in VOLATILE_KEYS:
                continue
            normalized[key] = _normalize_for_hash(value[key], output_marker)
        return normalized

    if isinstance(value, list):
        return [_normalize_for_hash(item, output_marker) for item in value]

    if isinstance(value, str):
        text = value.replace("\\", "/")
        if output_marker and output_marker in text:
            text = text.replace(output_marker, "<RUN_OUTPUT>")
        return text

    return value


def _hash_artifact(path: Path, output_marker: str) -> str:
    suffix = path.suffix.lower()

    if suffix == ".json":
        payload = _load_json(path)
        normalized = _normalize_for_hash(payload, output_marker)
        return _hash_json_payload(normalized)

    if suffix == ".jsonl":
        rows = _load_jsonl(path)
        normalized_rows = [_normalize_for_hash(row, output_marker) for row in rows]
        return _hash_json_payload(normalized_rows)

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _run_fingerprint(capture: PipelineRunCapture) -> Dict[str, Any]:
    payload = capture.payload
    artifacts = payload.get("artifacts") if isinstance(payload.get("artifacts"), dict) else {}
    output_marker = str(capture.output_dir.resolve()).replace("\\", "/")

    artifact_hashes: Dict[str, str] = {}
    for key, value in sorted(artifacts.items(), key=lambda item: item[0]):
        path_text = str(value).strip()
        if not path_text:
            continue
        path = Path(path_text)
        if not path.exists():
            continue
        artifact_hashes[key] = _hash_artifact(path, output_marker)

    normalized_payload = _normalize_for_hash(payload, output_marker)
    contract_hash = _hash_json_payload(normalized_payload)

    bundle = {
        "contract_hash": contract_hash,
        "artifact_hashes": artifact_hashes,
    }
    bundle_hash = _hash_json_payload(bundle)

    return {
        "bundle_hash": bundle_hash,
        "contract_hash": contract_hash,
        "artifact_hashes": artifact_hashes,
    }


def _determinism_check(first: PipelineRunCapture, second: PipelineRunCapture) -> Dict[str, Any]:
    first_fp = _run_fingerprint(first)
    second_fp = _run_fingerprint(second)

    artifact_differences: List[str] = []
    first_hashes = first_fp.get("artifact_hashes", {})
    second_hashes = second_fp.get("artifact_hashes", {})

    for key in sorted(set(first_hashes.keys()).union(second_hashes.keys())):
        if first_hashes.get(key) != second_hashes.get(key):
            artifact_differences.append(key)

    if first_fp.get("contract_hash") != second_fp.get("contract_hash"):
        artifact_differences.append("pipeline_contract")

    artifact_deterministic = (
        first_fp.get("bundle_hash") == second_fp.get("bundle_hash") and len(artifact_differences) == 0
    )

    first_semantic_profile = _semantic_truth_profile(first)
    second_semantic_profile = _semantic_truth_profile(second)

    semantic_differences = _collect_value_differences(first_semantic_profile, second_semantic_profile)
    run1_semantic_hash = _hash_json_payload(first_semantic_profile)
    run2_semantic_hash = _hash_json_payload(second_semantic_profile)
    semantic_deterministic = run1_semantic_hash == run2_semantic_hash and len(semantic_differences) == 0

    differences = list(artifact_differences)
    if not semantic_deterministic:
        differences.append("semantic_profile")

    deterministic = artifact_deterministic and semantic_deterministic

    return {
        "run1_hash": str(first_fp.get("bundle_hash", "")),
        "run2_hash": str(second_fp.get("bundle_hash", "")),
        "run1_semantic_hash": run1_semantic_hash,
        "run2_semantic_hash": run2_semantic_hash,
        "deterministic": deterministic,
        "artifact_deterministic": artifact_deterministic,
        "semantic_deterministic": semantic_deterministic,
        "differences": differences,
        "artifact_differences": artifact_differences,
        "semantic_differences": semantic_differences,
    }


def _pipeline_execution_health(capture: PipelineRunCapture) -> Dict[str, Any]:
    contract_checks = _validate_artifact_contract(capture.payload)
    summary = capture.payload.get("summary") if isinstance(capture.payload.get("summary"), dict) else {}

    stage_sequence = list(capture.stage_sequence)
    expected_set = set(EXPECTED_STAGE_ORDER)
    observed_set = set(stage_sequence)
    stage_order_ok = stage_sequence == EXPECTED_STAGE_ORDER

    pipeline_success = expected_set.issubset(observed_set)

    return {
        "pipeline_success": bool(pipeline_success),
        "stage_order_ok": bool(stage_order_ok),
        "stage_sequence": stage_sequence,
        "stage_timings": capture.stage_timings,
        "artifact_presence": contract_checks["artifact_presence"],
        "schema_validation": contract_checks["schema_validation"],
        "no_null_critical_sections": contract_checks["no_null_critical_sections"],
        "system_valid": bool(capture.payload.get("system_valid", False)),
        "summary_status": str(summary.get("status", "UNKNOWN")),
    }


def _build_structure_audit(repo_root: Path) -> Dict[str, Any]:
    coverage = _build_domain_coverage(repo_root)
    duplicate_functionality = _detect_duplicate_functionality(repo_root, coverage["python_files"])
    legacy = _scan_legacy_remnants(repo_root)
    dependency = _dependency_sanity(repo_root)

    return {
        "domain_coverage": coverage["domain_coverage"],
        "misplaced_files": coverage["misplaced_files"],
        "orphaned_scripts": coverage["orphaned_scripts"],
        "duplicate_functionality": duplicate_functionality,
        "legacy_remnants": legacy,
        "dependency_sanity": dependency,
    }


def _build_system_integrity_summary(
    structure_audit: Dict[str, Any],
    pipeline_health: Dict[str, Any],
    runtime_validation: Dict[str, Any],
    truth_validation: Dict[str, Any],
    determinism: Dict[str, Any],
    integrity_checks: Dict[str, Any],
) -> Dict[str, Any]:
    critical_issues: List[str] = []
    warnings: List[str] = []

    if not bool(pipeline_health.get("pipeline_success", False)):
        critical_issues.append("Pipeline did not complete all required stages.")

    schema_validation = pipeline_health.get("schema_validation") if isinstance(pipeline_health.get("schema_validation"), dict) else {}
    failed_schema = [name for name, passed in sorted(schema_validation.items()) if not bool(passed)]
    if failed_schema:
        critical_issues.append(f"Schema validation failed for artifacts: {failed_schema}")

    if not bool(pipeline_health.get("no_null_critical_sections", False)):
        critical_issues.append("Critical sections contain null or missing values in contract payload.")

    if not bool(runtime_validation.get("bubble_mode_executed", False)):
        critical_issues.append("Runtime bubble mode did not execute at least one entrypoint.")

    if not bool(runtime_validation.get("runtime_event_stream_present", False)):
        critical_issues.append("Runtime trace stream is empty.")

    if not bool(runtime_validation.get("execution_graph_generated", False)):
        critical_issues.append("Execution flow graph was not generated.")

    if not bool(truth_validation.get("passed", False)):
        truth_issues = (
            truth_validation.get("critical_issues")
            if isinstance(truth_validation.get("critical_issues"), list)
            else []
        )
        if truth_issues:
            critical_issues.extend(truth_issues)
        else:
            critical_issues.append("Truth validation layer failed without explicit issue details.")

    if not bool(determinism.get("deterministic", False)):
        critical_issues.append(
            "Determinism mismatch between runs: "
            f"{determinism.get('differences', [])}"
        )

    if not bool(determinism.get("semantic_deterministic", True)):
        critical_issues.append(
            "Semantic determinism mismatch between runs: "
            f"{determinism.get('semantic_differences', [])}"
        )

    if not bool(integrity_checks.get("passed", False)):
        critical_issues.extend(integrity_checks.get("issues", []))

    dependency = structure_audit.get("dependency_sanity") if isinstance(structure_audit.get("dependency_sanity"), dict) else {}
    cycles = dependency.get("circular_imports") if isinstance(dependency.get("circular_imports"), list) else []
    violations = dependency.get("cross_layer_violations") if isinstance(dependency.get("cross_layer_violations"), list) else []

    if cycles:
        critical_issues.append(f"Circular imports detected: {len(cycles)}")

    if violations:
        critical_issues.append(f"Cross-layer import violations detected: {len(violations)}")

    orphaned = structure_audit.get("orphaned_scripts") if isinstance(structure_audit.get("orphaned_scripts"), list) else []
    if orphaned:
        warnings.append(f"Orphaned scripts detected: {len(orphaned)}")

    misplaced = structure_audit.get("misplaced_files") if isinstance(structure_audit.get("misplaced_files"), list) else []
    if misplaced:
        warnings.append(f"Misplaced files detected: {len(misplaced)}")

    duplicates = structure_audit.get("duplicate_functionality") if isinstance(structure_audit.get("duplicate_functionality"), list) else []
    if duplicates:
        warnings.append(f"Potential duplicate functionality findings: {len(duplicates)}")

    legacy = structure_audit.get("legacy_remnants") if isinstance(structure_audit.get("legacy_remnants"), dict) else {}
    ps1_files = legacy.get("powershell_files") if isinstance(legacy.get("powershell_files"), list) else []
    if ps1_files:
        warnings.append(f"Legacy PowerShell files present: {len(ps1_files)}")

    command_refs = legacy.get("legacy_command_references") if isinstance(legacy.get("legacy_command_references"), list) else []
    if command_refs:
        warnings.append(f"Legacy command references (pwsh/run.ps1) present: {len(command_refs)}")

    truth_warnings = truth_validation.get("warnings") if isinstance(truth_validation.get("warnings"), list) else []
    for item in truth_warnings:
        warnings.append(f"truth_validation: {item}")

    passes = len(critical_issues) == 0

    return {
        "passes": passes,
        "critical_issues": critical_issues,
        "warnings": warnings,
        "integrity_checks": integrity_checks,
    }


def run_repository_audit(
    repo_root: Path,
    output_json_path: Path,
    output_md_path: Path,
    bubble_mode: bool = True,
) -> Dict[str, Any]:
    repo_root = repo_root.resolve()

    run_root = output_json_path.resolve().parent / "repo_audit_runs"
    first_run_dir = run_root / "run_1"
    second_run_dir = run_root / "run_2"

    first_capture = _run_pipeline_capture(repo_root, first_run_dir, bubble_mode=bool(bubble_mode))
    second_capture = _run_pipeline_capture(repo_root, second_run_dir, bubble_mode=bool(bubble_mode))

    structure_audit = _build_structure_audit(repo_root)
    pipeline_health = _pipeline_execution_health(first_capture)
    runtime_validation = _runtime_validation(first_capture.payload)
    truth_validation = _truth_validation_layer(first_capture.payload, runtime_validation=runtime_validation)
    determinism = _determinism_check(first_capture, second_capture)
    integrity_checks = _system_integrity_checks(repo_root)

    report = {
        "structure_audit": structure_audit,
        "pipeline_execution_health": pipeline_health,
        "runtime_validation": runtime_validation,
        "truth_validation_layer": truth_validation,
        "determinism_check": determinism,
        "system_integrity_summary": _build_system_integrity_summary(
            structure_audit=structure_audit,
            pipeline_health=pipeline_health,
            runtime_validation=runtime_validation,
            truth_validation=truth_validation,
            determinism=determinism,
            integrity_checks=integrity_checks,
        ),
    }

    output_json_path.parent.mkdir(parents=True, exist_ok=True)
    output_md_path.parent.mkdir(parents=True, exist_ok=True)

    output_json_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    output_md_path.write_text(_render_markdown_report(report), encoding="utf-8")

    return report


def _render_bool(flag: bool) -> str:
    return "PASS" if flag else "FAIL"


def _render_markdown_report(report: Dict[str, Any]) -> str:
    structure = report.get("structure_audit") if isinstance(report.get("structure_audit"), dict) else {}
    pipeline_health = report.get("pipeline_execution_health") if isinstance(report.get("pipeline_execution_health"), dict) else {}
    runtime = report.get("runtime_validation") if isinstance(report.get("runtime_validation"), dict) else {}
    truth = report.get("truth_validation_layer") if isinstance(report.get("truth_validation_layer"), dict) else {}
    determinism = report.get("determinism_check") if isinstance(report.get("determinism_check"), dict) else {}
    summary = report.get("system_integrity_summary") if isinstance(report.get("system_integrity_summary"), dict) else {}

    lines: List[str] = []
    lines.append("# Repository Audit Report")
    lines.append("")

    lines.append("## System Integrity")
    lines.append(f"- Overall: {_render_bool(bool(summary.get('passes', False)))}")
    lines.append(f"- Critical issues: {len(summary.get('critical_issues', []))}")
    lines.append(f"- Warnings: {len(summary.get('warnings', []))}")
    lines.append("")

    lines.append("## Structure Audit")
    lines.append(f"- Misplaced files: {len(structure.get('misplaced_files', []))}")
    lines.append(f"- Orphaned scripts: {len(structure.get('orphaned_scripts', []))}")
    lines.append(f"- Duplicate functionality findings: {len(structure.get('duplicate_functionality', []))}")

    legacy = structure.get("legacy_remnants") if isinstance(structure.get("legacy_remnants"), dict) else {}
    lines.append(f"- Legacy PowerShell files: {len(legacy.get('powershell_files', []))}")
    lines.append(
        f"- Legacy command references (pwsh/run.ps1): {len(legacy.get('legacy_command_references', []))}"
    )

    dependency = structure.get("dependency_sanity") if isinstance(structure.get("dependency_sanity"), dict) else {}
    lines.append(f"- Circular imports: {len(dependency.get('circular_imports', []))}")
    lines.append(f"- Cross-layer violations: {len(dependency.get('cross_layer_violations', []))}")
    lines.append("")

    lines.append("## Pipeline Execution Health")
    lines.append(f"- Pipeline success: {_render_bool(bool(pipeline_health.get('pipeline_success', False)))}")
    lines.append(f"- Stage order exact match: {_render_bool(bool(pipeline_health.get('stage_order_ok', False)))}")
    lines.append(
        f"- Critical sections non-null: {_render_bool(bool(pipeline_health.get('no_null_critical_sections', False)))}"
    )

    stage_timings = pipeline_health.get("stage_timings") if isinstance(pipeline_health.get("stage_timings"), dict) else {}
    if stage_timings:
        lines.append("- Stage timings (seconds):")
        for stage, elapsed in sorted(stage_timings.items(), key=lambda item: item[0]):
            lines.append(f"  - {stage}: {float(elapsed):.4f}")

    schema_validation = pipeline_health.get("schema_validation") if isinstance(pipeline_health.get("schema_validation"), dict) else {}
    if schema_validation:
        lines.append("- Schema validation:")
        for name, passed in sorted(schema_validation.items(), key=lambda item: item[0]):
            lines.append(f"  - {name}: {_render_bool(bool(passed))}")

    lines.append("")
    lines.append("## Runtime Validation")
    lines.append(f"- Bubble mode executed: {_render_bool(bool(runtime.get('bubble_mode_executed', False)))}")
    lines.append(
        f"- Runtime event stream present: {_render_bool(bool(runtime.get('runtime_event_stream_present', False)))}"
    )
    lines.append(
        f"- Execution graph generated: {_render_bool(bool(runtime.get('execution_graph_generated', False)))}"
    )
    lines.append(f"- Runtime event count: {int(runtime.get('runtime_event_count', 0) or 0)}")
    entrypoints = runtime.get("traced_entrypoints") if isinstance(runtime.get("traced_entrypoints"), list) else []
    lines.append(f"- Traced entrypoints: {entrypoints}")

    runtime_meaningfulness = truth.get("runtime_meaningfulness") if isinstance(truth.get("runtime_meaningfulness"), dict) else {}
    reconciliation = (
        truth.get("runtime_static_reconciliation")
        if isinstance(truth.get("runtime_static_reconciliation"), dict)
        else {}
    )
    graph_sanity = truth.get("graph_sanity") if isinstance(truth.get("graph_sanity"), dict) else {}
    classification = (
        truth.get("classification_quality") if isinstance(truth.get("classification_quality"), dict) else {}
    )

    lines.append("")
    lines.append("## Truth Validation Layer")
    lines.append(f"- Truth validation passed: {_render_bool(bool(truth.get('passed', False)))}")
    lines.append(
        "- Runtime meaningfulness passed: "
        f"{_render_bool(bool(runtime_meaningfulness.get('passed', False)))}"
    )
    lines.append(
        "- Runtime/static reconciliation passed: "
        f"{_render_bool(bool(reconciliation.get('passed', False)))}"
    )
    lines.append(f"- Graph sanity passed: {_render_bool(bool(graph_sanity.get('passed', False)))}")
    lines.append(
        "- Classification quality passed: "
        f"{_render_bool(bool(classification.get('passed', False)))}"
    )
    lines.append(
        "- Runtime richness metrics: "
        "modules="
        f"{_safe_int(runtime_meaningfulness.get('modules_executed', 0))}, "
        "local_modules="
        f"{_safe_int(runtime_meaningfulness.get('local_modules_executed', 0))}, "
        "unique_functions="
        f"{_safe_int(runtime_meaningfulness.get('unique_functions_called', 0))}, "
        "unique_local_functions="
        f"{_safe_int(runtime_meaningfulness.get('unique_local_functions_called', 0))}, "
        "max_call_depth="
        f"{_safe_int(runtime_meaningfulness.get('max_call_depth', 0))}"
    )
    lines.append(
        "- Runtime/static edge overlap: "
        "shared="
        f"{_safe_int(reconciliation.get('shared_edge_count', 0))}, "
        "runtime_only="
        f"{_safe_int(reconciliation.get('runtime_only_edge_count', 0))}, "
        "static_only="
        f"{_safe_int(reconciliation.get('static_only_edge_count', 0))}, "
        "overlap_ratio="
        f"{_safe_float(reconciliation.get('overlap_ratio', 0.0)):.4f}"
    )
    lines.append(
        "- Graph sanity metrics: "
        "reachable_ratio="
        f"{_safe_float(graph_sanity.get('reachable_node_ratio', 0.0)):.4f}, "
        "isolated_ratio="
        f"{_safe_float(graph_sanity.get('isolated_node_ratio', 0.0)):.4f}, "
        "runtime_confirmed_edge_ratio="
        f"{_safe_float(graph_sanity.get('runtime_confirmed_edge_ratio', 0.0)):.4f}"
    )
    lines.append(
        "- Classification metrics: "
        "HOT="
        f"{_safe_int(classification.get('hot_node_count', 0))}, "
        "WARM="
        f"{_safe_int(classification.get('warm_node_count', 0))}, "
        "dead_referenced="
        f"{_safe_int(classification.get('dead_referenced_count', 0))}, "
        "warm_unreachable="
        f"{_safe_int(classification.get('warm_unreachable_count', 0))}"
    )

    truth_critical = truth.get("critical_issues") if isinstance(truth.get("critical_issues"), list) else []
    if truth_critical:
        lines.append("- Truth critical issues:")
        for item in truth_critical:
            lines.append(f"  - {item}")

    low_info_reasons = (
        runtime_meaningfulness.get("low_information_reasons")
        if isinstance(runtime_meaningfulness.get("low_information_reasons"), list)
        else []
    )
    if low_info_reasons:
        lines.append("- Low-information runtime reasons:")
        for item in low_info_reasons:
            lines.append(f"  - {item}")

    lines.append("")
    lines.append("## Determinism")
    lines.append(f"- Deterministic: {_render_bool(bool(determinism.get('deterministic', False)))}")
    lines.append(f"- Run 1 hash: {str(determinism.get('run1_hash', ''))}")
    lines.append(f"- Run 2 hash: {str(determinism.get('run2_hash', ''))}")
    lines.append(f"- Semantic deterministic: {_render_bool(bool(determinism.get('semantic_deterministic', False)))}")
    lines.append(f"- Run 1 semantic hash: {str(determinism.get('run1_semantic_hash', ''))}")
    lines.append(f"- Run 2 semantic hash: {str(determinism.get('run2_semantic_hash', ''))}")
    lines.append(f"- Differences: {determinism.get('differences', [])}")
    lines.append(f"- Semantic differences: {determinism.get('semantic_differences', [])}")

    critical = summary.get("critical_issues") if isinstance(summary.get("critical_issues"), list) else []
    warnings = summary.get("warnings") if isinstance(summary.get("warnings"), list) else []

    lines.append("")
    lines.append("## Issues")
    if critical:
        lines.append("### Critical")
        for item in critical:
            lines.append(f"- {item}")
    else:
        lines.append("- No critical issues detected.")

    if warnings:
        lines.append("### Warnings")
        for item in warnings:
            lines.append(f"- {item}")

    lines.append("")
    return "\n".join(lines)


def _print_cli_summary(report: Dict[str, Any], output_json_path: Path, output_md_path: Path) -> None:
    structure = report.get("structure_audit") if isinstance(report.get("structure_audit"), dict) else {}
    pipeline_health = report.get("pipeline_execution_health") if isinstance(report.get("pipeline_execution_health"), dict) else {}
    runtime = report.get("runtime_validation") if isinstance(report.get("runtime_validation"), dict) else {}
    truth = report.get("truth_validation_layer") if isinstance(report.get("truth_validation_layer"), dict) else {}
    determinism = report.get("determinism_check") if isinstance(report.get("determinism_check"), dict) else {}
    summary = report.get("system_integrity_summary") if isinstance(report.get("system_integrity_summary"), dict) else {}

    print("[repo-audit] Summary")
    print(f"[repo-audit] overall={_render_bool(bool(summary.get('passes', False)))}")
    print(f"[repo-audit] pipeline_success={_render_bool(bool(pipeline_health.get('pipeline_success', False)))}")
    print(f"[repo-audit] runtime_stream={_render_bool(bool(runtime.get('runtime_event_stream_present', False)))}")
    print(f"[repo-audit] truth_validation={_render_bool(bool(truth.get('passed', False)))}")
    print(f"[repo-audit] deterministic={_render_bool(bool(determinism.get('deterministic', False)))}")
    print(f"[repo-audit] semantic_deterministic={_render_bool(bool(determinism.get('semantic_deterministic', False)))}")

    stage_timings = pipeline_health.get("stage_timings") if isinstance(pipeline_health.get("stage_timings"), dict) else {}
    if stage_timings:
        timing_summary = ", ".join(
            f"{stage}={float(value):.4f}s" for stage, value in sorted(stage_timings.items(), key=lambda item: item[0])
        )
        print(f"[repo-audit] stage_timings: {timing_summary}")

    differences = determinism.get("differences") if isinstance(determinism.get("differences"), list) else []
    print(f"[repo-audit] determinism_differences={differences}")

    truth_runtime = truth.get("runtime_meaningfulness") if isinstance(truth.get("runtime_meaningfulness"), dict) else {}
    low_info_reasons = (
        truth_runtime.get("low_information_reasons")
        if isinstance(truth_runtime.get("low_information_reasons"), list)
        else []
    )
    if low_info_reasons:
        print(f"[repo-audit] low_information_runtime_reasons={low_info_reasons}")

    print(
        "[repo-audit] structure_counts="
        f"misplaced:{len(structure.get('misplaced_files', []))},"
        f"orphaned:{len(structure.get('orphaned_scripts', []))},"
        f"duplicates:{len(structure.get('duplicate_functionality', []))}"
    )

    print(f"[repo-audit] report_json={output_json_path}")
    print(f"[repo-audit] report_md={output_md_path}")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deterministic repository structure and pipeline audit.")
    parser.add_argument("--repo", default=str(PROJECT_ROOT), help="Repository root to audit.")
    parser.add_argument(
        "--output-json",
        default=str(PROJECT_ROOT / "output" / "repo_audit_report.json"),
        help="Output path for JSON report.",
    )
    parser.add_argument(
        "--output-md",
        default=str(PROJECT_ROOT / "output" / "repo_audit_report.md"),
        help="Output path for markdown report.",
    )
    parser.add_argument(
        "--bubble-mode",
        default="true",
        choices=["true", "false"],
        help="Whether runtime bubble execution should be enabled.",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    repo_root = Path(str(args.repo)).resolve()
    output_json_path = Path(str(args.output_json)).resolve()
    output_md_path = Path(str(args.output_md)).resolve()
    bubble_mode = str(args.bubble_mode).strip().lower() == "true"

    report = run_repository_audit(
        repo_root=repo_root,
        output_json_path=output_json_path,
        output_md_path=output_md_path,
        bubble_mode=bubble_mode,
    )

    _print_cli_summary(report, output_json_path=output_json_path, output_md_path=output_md_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
