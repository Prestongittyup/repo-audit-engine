from __future__ import annotations

import ast
from pathlib import Path


def parse_python_source(source: str) -> ast.AST:
    return ast.parse(source)


def parse_python_file(path: Path) -> ast.AST:
    source = path.read_text(encoding="utf-8", errors="replace")
    return parse_python_source(source)
