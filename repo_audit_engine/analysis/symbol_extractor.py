from __future__ import annotations

import ast
from typing import Dict, List


def extract_top_level_symbols(tree: ast.AST) -> List[Dict[str, str]]:
    symbols: List[Dict[str, str]] = []
    module_body = getattr(tree, "body", [])
    for node in module_body:
        if isinstance(node, ast.FunctionDef):
            symbols.append({"kind": "function", "name": node.name})
        elif isinstance(node, ast.AsyncFunctionDef):
            symbols.append({"kind": "function", "name": node.name})
        elif isinstance(node, ast.ClassDef):
            symbols.append({"kind": "class", "name": node.name})
    symbols.sort(key=lambda item: (item["kind"], item["name"]))
    return symbols
