import ast
import json
import sys


def parse_imports(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        tree = ast.parse(source, filename=path)
    except Exception:
        return {"imports": [], "from_imports": []}

    imports = []
    from_imports = []
    seen_i = set()
    seen_f = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name and alias.name not in seen_i:
                    seen_i.add(alias.name)
                    imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module not in seen_f:
                seen_f.add(node.module)
                from_imports.append(node.module)

    return {"imports": imports, "from_imports": from_imports}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"imports": [], "from_imports": []}))
        sys.exit(0)
    print(json.dumps(parse_imports(sys.argv[1])))
