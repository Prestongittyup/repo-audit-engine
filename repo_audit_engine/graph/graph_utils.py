from __future__ import annotations


def _normalize_path(rel_path: str) -> str:
    normalized = str(rel_path or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def node_id(kind: str, rel_path: str, name: str = "") -> str:
    normalized_path = _normalize_path(rel_path)
    normalized_name = str(name or "").strip()
    if name:
        return f"{kind}:{normalized_path}:{normalized_name}"
    return f"{kind}:{normalized_path}"


def canonical_id(kind: str, rel_path: str, name: str = "") -> str:
    normalized_path = _normalize_path(rel_path)
    if name:
        normalized_name = name.strip().replace("/", "_")
        return f"canonical://{kind}/{normalized_path}/{normalized_name}"
    return f"canonical://{kind}/{normalized_path}"
