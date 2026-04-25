from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from repo_audit_engine.io.artifacts import write_json


DEFAULT_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "output",
    "state",
    ".tmp",
    ".idea",
    ".vscode",
}

DEFAULT_SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".dll",
    ".exe",
    ".so",
    ".dylib",
    ".class",
    ".jar",
    ".pdb",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
}


def _to_posix(path: Path) -> str:
    return path.as_posix()


def _safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _module_name_from_path(rel_path: str) -> str:
    if not rel_path.endswith(".py"):
        return ""

    module = rel_path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    return module


def _load_ignore_patterns(repo_path: Path) -> Tuple[set[str], set[str]]:
    skip_dirs = set(DEFAULT_SKIP_DIRS)
    skip_suffixes = set(DEFAULT_SKIP_SUFFIXES)

    config_path = repo_path / "config" / "ignore_patterns.txt"
    if not config_path.exists():
        return skip_dirs, skip_suffixes

    mode = "dirs"
    for raw_line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            lowered = line.lower()
            if "suffix" in lowered:
                mode = "suffix"
            elif "directory" in lowered:
                mode = "dirs"
            continue

        if mode == "suffix":
            skip_suffixes.add(line.lower())
        else:
            skip_dirs.add(line)

    return skip_dirs, skip_suffixes


def _entrypoint_heuristics(
    rel_path: str,
    module_name: str,
    symbols: Sequence[Dict[str, Any]],
    has_main_guard: bool,
    explicit_entrypoints: set[str],
) -> List[str]:
    reasons: List[str] = []

    if rel_path in explicit_entrypoints or module_name in explicit_entrypoints:
        reasons.append("explicit_config")

    filename = Path(rel_path).name.lower()
    if filename in {"main.py", "app.py", "cli.py", "run.py"}:
        reasons.append("filename_heuristic")

    if has_main_guard:
        reasons.append("main_guard")

    for symbol in symbols:
        if str(symbol.get("name", "")) == "main":
            reasons.append("main_symbol")
            break

    if rel_path.startswith("scripts/"):
        reasons.append("scripts_directory")

    # Keep deterministic ordering while removing duplicates.
    seen: set[str] = set()
    ordered: List[str] = []
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        ordered.append(reason)

    return ordered


def _extract_python_metadata(path: Path) -> Dict[str, Any]:
    source = _safe_read_text(path)
    imports: set[str] = set()
    symbols: List[Dict[str, Any]] = []
    has_main_guard = False

    if not source.strip():
        return {
            "imports": [],
            "symbols": [],
            "has_main_guard": False,
            "ast_error": "empty_file",
        }

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return {
            "imports": [],
            "symbols": [],
            "has_main_guard": False,
            "ast_error": f"syntax_error:{exc.lineno}:{exc.offset}",
        }

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in sorted(node.names, key=lambda item: item.name):
                imports.add(alias.name)

        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if base:
                imports.add(base)
            for alias in sorted(node.names, key=lambda item: item.name):
                if base:
                    imports.add(f"{base}.{alias.name}")
                else:
                    imports.add(alias.name)

        if isinstance(node, ast.FunctionDef):
            symbols.append({"kind": "function", "name": node.name, "lineno": int(node.lineno)})

        if isinstance(node, ast.AsyncFunctionDef):
            symbols.append({"kind": "function", "name": node.name, "lineno": int(node.lineno)})

        if isinstance(node, ast.ClassDef):
            symbols.append({"kind": "class", "name": node.name, "lineno": int(node.lineno)})

        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__":
                for comparator in test.comparators:
                    if isinstance(comparator, ast.Constant) and comparator.value == "__main__":
                        has_main_guard = True

    symbols.sort(key=lambda item: (str(item.get("kind", "")), str(item.get("name", "")), int(item.get("lineno", 0))))

    return {
        "imports": sorted(imports),
        "symbols": symbols,
        "has_main_guard": has_main_guard,
        "ast_error": "",
    }


def _iter_repo_files(repo_path: Path, skip_dirs: Iterable[str], skip_suffixes: Iterable[str]) -> Iterable[Path]:
    skip_dir_set = set(skip_dirs)
    skip_suffix_set = {suffix.lower() for suffix in skip_suffixes}

    for root, dirs, files in os.walk(repo_path, topdown=True):
        root_path = Path(root)

        filtered_dirs = []
        for directory in sorted(dirs):
            if directory in skip_dir_set:
                continue
            filtered_dirs.append(directory)
        dirs[:] = filtered_dirs

        for filename in sorted(files):
            path = root_path / filename

            suffix = path.suffix.lower()
            if suffix in skip_suffix_set:
                continue

            yield path


def build_manifest(
    repo_path: Path,
    output_dir: Path,
    explicit_entrypoints: Sequence[str] | None = None,
    chunk_size: int = 200,
) -> Dict[str, Any]:
    repo_root = repo_path.resolve()
    output_root = output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    explicit = {str(item).strip() for item in (explicit_entrypoints or []) if str(item).strip()}

    skip_dirs, skip_suffixes = _load_ignore_patterns(repo_root)

    manifest_path = output_root / "manifest.jsonl"
    summary_path = output_root / "manifest_summary.json"

    file_count = 0
    python_file_count = 0
    total_bytes = 0
    entrypoints: List[str] = []
    language_counts: Dict[str, int] = {}

    with manifest_path.open("w", encoding="utf-8") as handle:
        for absolute_path in _iter_repo_files(repo_root, skip_dirs, skip_suffixes):
            if output_root in absolute_path.parents:
                continue

            rel_path = _to_posix(absolute_path.relative_to(repo_root))
            stat = absolute_path.stat()
            file_size = int(stat.st_size)
            total_bytes += file_size
            file_count += 1

            suffix = absolute_path.suffix.lower()
            language = "python" if suffix == ".py" else (suffix[1:] if suffix.startswith(".") else "unknown")
            language_counts[language] = language_counts.get(language, 0) + 1

            imports: List[str] = []
            symbols: List[Dict[str, Any]] = []
            has_main_guard = False
            ast_error = ""

            module_name = _module_name_from_path(rel_path)
            if language == "python":
                python_file_count += 1
                metadata = _extract_python_metadata(absolute_path)
                imports = list(metadata.get("imports", []))
                symbols = list(metadata.get("symbols", []))
                has_main_guard = bool(metadata.get("has_main_guard", False))
                ast_error = str(metadata.get("ast_error", "") or "")

            entrypoint_reasons = _entrypoint_heuristics(
                rel_path=rel_path,
                module_name=module_name,
                symbols=symbols,
                has_main_guard=has_main_guard,
                explicit_entrypoints=explicit,
            )

            if entrypoint_reasons:
                entrypoints.append(rel_path)

            record: Dict[str, Any] = {
                "path": rel_path,
                "size": file_size,
                "sha256": _file_sha256(absolute_path),
                "language": language,
                "module": module_name,
                "imports": imports,
                "symbols": symbols,
                "entrypoint_reasons": entrypoint_reasons,
                "ast_error": ast_error,
            }

            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
            if file_count % max(1, int(chunk_size)) == 0:
                handle.flush()

    unique_entrypoints = sorted(set(entrypoints))

    summary: Dict[str, Any] = {
        "file_count": file_count,
        "python_file_count": python_file_count,
        "total_bytes": total_bytes,
        "entrypoints": unique_entrypoints,
        "language_counts": dict(sorted(language_counts.items(), key=lambda item: item[0])),
        "manifest_path": str(manifest_path),
    }

    write_json(summary_path, summary, pretty=True)

    return {
        "manifest_path": str(manifest_path),
        "manifest_summary_path": str(summary_path),
        "summary": summary,
    }
