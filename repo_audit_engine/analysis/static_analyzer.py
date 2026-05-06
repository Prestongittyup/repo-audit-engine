from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from repo_audit_engine.io.artifacts import write_json


class _CallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope_stack: List[str] = []
        self.functions: List[Dict[str, Any]] = []
        self.classes: List[Dict[str, Any]] = []
        self.calls: List[Dict[str, Any]] = []
        self.imports: List[Dict[str, Any]] = []
        self.config_refs: List[str] = []

    def visit_Import(self, node: ast.Import) -> Any:  # noqa: N802
        for alias in sorted(node.names, key=lambda item: item.name):
            self.imports.append(
                {
                    "module": alias.name,
                    "alias": alias.asname or "",
                    "lineno": int(node.lineno),
                }
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:  # noqa: N802
        base = node.module or ""
        for alias in sorted(node.names, key=lambda item: item.name):
            full_name = f"{base}.{alias.name}" if base else alias.name
            self.imports.append(
                {
                    "module": full_name,
                    "alias": alias.asname or "",
                    "lineno": int(node.lineno),
                }
            )
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:  # noqa: N802
        qualname = ".".join(self.scope_stack + [node.name]) if self.scope_stack else node.name
        self.functions.append(
            {
                "name": node.name,
                "qualname": qualname,
                "lineno": int(node.lineno),
            }
        )
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:  # noqa: N802
        qualname = ".".join(self.scope_stack + [node.name]) if self.scope_stack else node.name
        self.functions.append(
            {
                "name": node.name,
                "qualname": qualname,
                "lineno": int(node.lineno),
            }
        )
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:  # noqa: N802
        qualname = ".".join(self.scope_stack + [node.name]) if self.scope_stack else node.name
        self.classes.append(
            {
                "name": node.name,
                "qualname": qualname,
                "lineno": int(node.lineno),
            }
        )
        self.scope_stack.append(node.name)
        self.generic_visit(node)
        self.scope_stack.pop()

    def visit_Call(self, node: ast.Call) -> Any:  # noqa: N802
        callee = self._call_name(node.func)
        caller = ".".join(self.scope_stack) if self.scope_stack else "<module>"
        self.calls.append(
            {
                "caller": caller,
                "callee": callee,
                "lineno": int(node.lineno),
            }
        )

        if callee.endswith("open"):
            for arg in node.args[:1]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    self.config_refs.append(str(arg.value))

        self.generic_visit(node)

    def _call_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id

        if isinstance(node, ast.Attribute):
            left = self._call_name(node.value)
            if left:
                return f"{left}.{node.attr}"
            return node.attr

        return "<unknown>"


def _iter_manifest_records(manifest_path: Path) -> Iterable[Dict[str, Any]]:
    with manifest_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                yield item


def _build_manifest_indexes(manifest_path: Path) -> tuple[Dict[str, str], Dict[str, List[Dict[str, str]]]]:
    module_index: Dict[str, str] = {}
    symbol_index: Dict[str, List[Dict[str, str]]] = {}

    for record in _iter_manifest_records(manifest_path):
        path = str(record.get("path", "")).strip()
        module = str(record.get("module", "")).strip()
        language = str(record.get("language", "")).strip()

        if language == "python" and path and module:
            module_index[module] = path

        path = str(record.get("path", ""))
        symbols = record.get("symbols") if isinstance(record.get("symbols"), list) else []
        for symbol in symbols:
            payload = symbol if isinstance(symbol, dict) else {}
            name = str(payload.get("name", "")).strip()
            kind = str(payload.get("kind", "")).strip() or "symbol"
            if not name or not path:
                continue
            node_id = f"{kind}:{path}:{name}"
            symbol_index.setdefault(name, []).append({"node_id": node_id, "path": path, "kind": kind})

    for key in symbol_index:
        symbol_index[key].sort(key=lambda item: (str(item.get("kind")), str(item.get("path")), str(item.get("node_id"))))

    return dict(sorted(module_index.items(), key=lambda item: item[0])), symbol_index


def _resolve_module(module_name: str, module_index: Dict[str, str]) -> str:
    if module_name in module_index:
        return module_index[module_name]

    pieces = module_name.split(".")
    while pieces:
        probe = ".".join(pieces)
        if probe in module_index:
            return module_index[probe]
        pieces.pop()

    return ""


def _build_import_resolution_context(imports_with_resolution: List[Dict[str, Any]]) -> Dict[str, Dict[str, set[str]]]:
    prefix_to_paths: Dict[str, set[str]] = {}
    symbol_to_paths: Dict[str, set[str]] = {}

    for item in imports_with_resolution:
        payload = item if isinstance(item, dict) else {}
        module_name = str(payload.get("module", "")).strip()
        alias_name = str(payload.get("alias", "")).strip()
        resolved_path = str(payload.get("resolved_path", "")).strip()
        if not resolved_path:
            continue

        module_parts = [part.strip() for part in module_name.split(".") if part.strip()]
        if module_parts:
            module_root = module_parts[0]
            module_leaf = module_parts[-1]
            prefix_to_paths.setdefault(module_root, set()).add(resolved_path)
            prefix_to_paths.setdefault(module_leaf, set()).add(resolved_path)
            symbol_to_paths.setdefault(module_leaf, set()).add(resolved_path)

            if len(module_parts) >= 2:
                prefix_to_paths.setdefault(module_parts[-2], set()).add(resolved_path)

        if alias_name:
            prefix_to_paths.setdefault(alias_name, set()).add(resolved_path)
            symbol_to_paths.setdefault(alias_name, set()).add(resolved_path)

    return {
        "prefix_to_paths": prefix_to_paths,
        "symbol_to_paths": symbol_to_paths,
    }


def _choose_unique_symbol_match(matches: List[Dict[str, str]]) -> str:
    if not matches:
        return ""

    if len(matches) == 1:
        return str(matches[0].get("node_id", "")).strip()

    function_matches = [item for item in matches if str(item.get("kind", "")).strip() == "function"]
    if len(function_matches) == 1:
        return str(function_matches[0].get("node_id", "")).strip()

    class_matches = [item for item in matches if str(item.get("kind", "")).strip() == "class"]
    if len(class_matches) == 1:
        return str(class_matches[0].get("node_id", "")).strip()

    return ""


def _resolve_call_target(
    *,
    file_path: str,
    callee: str,
    symbol_name: str,
    matches: List[Dict[str, str]],
    import_context: Dict[str, Dict[str, set[str]]],
) -> str:
    if not matches:
        return ""

    direct_choice = _choose_unique_symbol_match(matches)
    if direct_choice:
        return direct_choice

    local_matches = [item for item in matches if str(item.get("path", "")).strip() == file_path]
    local_choice = _choose_unique_symbol_match(local_matches)
    if local_choice:
        return local_choice

    prefix_to_paths = import_context.get("prefix_to_paths", {})
    symbol_to_paths = import_context.get("symbol_to_paths", {})

    call_prefix = ""
    if "." in callee:
        call_prefix = callee.split(".", 1)[0].strip()

    if call_prefix:
        candidate_paths = prefix_to_paths.get(call_prefix, set())
        narrowed = [item for item in matches if str(item.get("path", "")).strip() in candidate_paths]
        narrowed_choice = _choose_unique_symbol_match(narrowed)
        if narrowed_choice:
            return narrowed_choice

    candidate_paths = symbol_to_paths.get(symbol_name, set()) if symbol_name else set()
    if candidate_paths:
        narrowed = [item for item in matches if str(item.get("path", "")).strip() in candidate_paths]
        narrowed_choice = _choose_unique_symbol_match(narrowed)
        if narrowed_choice:
            return narrowed_choice

    return ""


def _analyze_python_file(file_path: Path) -> Dict[str, Any]:
    source = file_path.read_text(encoding="utf-8", errors="replace")
    if not source.strip():
        return {
            "functions": [],
            "classes": [],
            "imports": [],
            "calls": [],
            "config_references": [],
            "ast_error": "empty_file",
        }

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {
            "functions": [],
            "classes": [],
            "imports": [],
            "calls": [],
            "config_references": [],
            "ast_error": f"syntax_error:{exc.lineno}:{exc.offset}",
        }

    visitor = _CallVisitor()
    visitor.visit(tree)

    visitor.functions.sort(key=lambda item: (str(item.get("qualname")), int(item.get("lineno", 0))))
    visitor.classes.sort(key=lambda item: (str(item.get("qualname")), int(item.get("lineno", 0))))
    visitor.imports.sort(key=lambda item: (str(item.get("module")), int(item.get("lineno", 0))))
    visitor.calls.sort(key=lambda item: (int(item.get("lineno", 0)), str(item.get("caller")), str(item.get("callee"))))

    config_refs = sorted({str(item).strip() for item in visitor.config_refs if str(item).strip()})

    return {
        "functions": visitor.functions,
        "classes": visitor.classes,
        "imports": visitor.imports,
        "calls": visitor.calls,
        "config_references": config_refs,
        "ast_error": "",
    }


def run_static_analysis(
    repo_path: Path,
    manifest_path: Path,
    output_dir: Path,
) -> Dict[str, Any]:
    repo_root = repo_path.resolve()
    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    analysis_path = out_root / "static_analysis.jsonl"
    summary_path = out_root / "static_analysis_summary.json"

    module_index, symbol_index = _build_manifest_indexes(manifest_path)

    file_count = 0
    call_count = 0
    import_count = 0
    function_count = 0
    class_count = 0

    with analysis_path.open("w", encoding="utf-8") as handle:
        for manifest_item in _iter_manifest_records(manifest_path):
            if str(manifest_item.get("language", "")).strip() != "python":
                continue

            rel_path = str(manifest_item.get("path", "")).strip()
            if not rel_path:
                continue

            absolute_path = repo_root / rel_path
            if not absolute_path.exists():
                continue

            file_count += 1
            analysis = _analyze_python_file(absolute_path)

            imports_with_resolution: List[Dict[str, Any]] = []
            for item in analysis["imports"]:
                payload = item if isinstance(item, dict) else {}
                module_name = str(payload.get("module", "")).strip()
                resolved_path = _resolve_module(module_name, module_index)
                imports_with_resolution.append(
                    {
                        "module": module_name,
                        "alias": str(payload.get("alias", "")),
                        "lineno": int(payload.get("lineno", 0) or 0),
                        "resolved_path": resolved_path,
                    }
                )

            import_context = _build_import_resolution_context(imports_with_resolution)

            calls_with_resolution: List[Dict[str, Any]] = []
            for item in analysis["calls"]:
                payload = item if isinstance(item, dict) else {}
                callee = str(payload.get("callee", "")).strip()
                caller = str(payload.get("caller", "<module>")).strip() or "<module>"
                symbol_name = callee.split(".")[-1] if callee else ""
                matches = list(symbol_index.get(symbol_name, [])) if symbol_name else []
                resolved_node_id = _resolve_call_target(
                    file_path=rel_path,
                    callee=callee,
                    symbol_name=symbol_name,
                    matches=matches,
                    import_context=import_context,
                )

                calls_with_resolution.append(
                    {
                        "caller": caller,
                        "callee": callee,
                        "lineno": int(payload.get("lineno", 0) or 0),
                        "resolved_node_id": resolved_node_id,
                    }
                )

            function_count += len(analysis["functions"])
            class_count += len(analysis["classes"])
            import_count += len(imports_with_resolution)
            call_count += len(calls_with_resolution)

            record: Dict[str, Any] = {
                "file_path": rel_path,
                "module": str(manifest_item.get("module", "")),
                "functions": analysis["functions"],
                "classes": analysis["classes"],
                "imports": imports_with_resolution,
                "calls": calls_with_resolution,
                "config_references": analysis["config_references"],
                "ast_reference_count": len(imports_with_resolution) + len(calls_with_resolution),
                "ast_error": analysis["ast_error"],
            }

            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")

    summary = {
        "python_files_analyzed": file_count,
        "function_count": function_count,
        "class_count": class_count,
        "import_count": import_count,
        "call_count": call_count,
        "analysis_path": str(analysis_path),
    }

    write_json(summary_path, summary, pretty=True)

    return {
        "analysis_path": str(analysis_path),
        "summary_path": str(summary_path),
        "summary": summary,
    }
