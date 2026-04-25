from __future__ import annotations


def node_id(kind: str, rel_path: str, name: str = "") -> str:
    if name:
        return f"{kind}:{rel_path}:{name}"
    return f"{kind}:{rel_path}"


def canonical_id(kind: str, rel_path: str, name: str = "") -> str:
    normalized_path = rel_path.strip().replace("\\", "/")
    if name:
        normalized_name = name.strip().replace("/", "_")
        return f"canonical://{kind}/{normalized_path}/{normalized_name}"
    return f"canonical://{kind}/{normalized_path}"
