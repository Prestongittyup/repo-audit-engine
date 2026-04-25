from __future__ import annotations

import json
import re
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

from repo_audit_engine.io.artifacts import write_json


_STOPWORDS = {
    "the",
    "and",
    "for",
    "from",
    "with",
    "that",
    "this",
    "into",
    "true",
    "false",
    "none",
    "main",
    "init",
    "base",
    "core",
    "util",
    "utils",
    "helper",
    "helpers",
    "module",
    "class",
    "function",
    "file",
    "data",
    "value",
    "values",
    "item",
    "items",
    "node",
    "nodes",
    "edge",
    "edges",
    "test",
    "tests",
}

_ABSTRACTION_SUFFIXES = {
    "service",
    "manager",
    "engine",
    "router",
    "controller",
    "handler",
    "gateway",
    "orchestrator",
    "repository",
    "store",
    "builder",
    "adapter",
}

_DOMAIN_KEYWORDS = {
    "auth": {"auth", "token", "identity", "session", "jwt"},
    "policy": {"policy", "rules", "guard", "compliance", "risk"},
    "runtime": {"runtime", "trace", "event", "executor", "scheduler"},
    "orchestration": {"orchestrator", "workflow", "pipeline", "state", "transition"},
    "integration": {"integration", "adapter", "bridge", "client", "provider"},
    "observability": {"metrics", "logging", "diagnostic", "telemetry", "alert"},
    "data": {"repository", "store", "storage", "db", "sql", "cache"},
}


def build_semantic_cluster_report(
    manifest_path: Path,
    static_analysis_path: Path,
    output_dir: Path,
    similarity_threshold: float = 0.45,
    min_shared_tokens: int = 4,
) -> Dict[str, Any]:
    report = analyze_semantic_clusters(
        manifest_rows=_load_jsonl_rows(manifest_path),
        static_rows=_load_jsonl_rows(static_analysis_path),
        similarity_threshold=similarity_threshold,
        min_shared_tokens=min_shared_tokens,
    )

    out_root = output_dir.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    report_path = out_root / "semantic_clusters.json"
    write_json(report_path, report, pretty=True)

    return {
        "report_path": str(report_path),
        "report": report,
    }


def analyze_semantic_clusters(
    manifest_rows: Sequence[Mapping[str, Any]],
    static_rows: Sequence[Mapping[str, Any]],
    similarity_threshold: float = 0.45,
    min_shared_tokens: int = 4,
) -> Dict[str, Any]:
    normalized_similarity_threshold = max(0.05, min(0.95, float(similarity_threshold)))
    normalized_min_shared_tokens = max(1, int(min_shared_tokens))

    file_records: Dict[str, Dict[str, Any]] = {}

    for row in manifest_rows:
        payload = row if isinstance(row, Mapping) else {}
        rel_path = _normalize_path(payload.get("path", ""))
        if not rel_path:
            continue

        record = file_records.setdefault(rel_path, _empty_file_record(rel_path))
        record["module"] = str(payload.get("module", "")).strip()

        tokens = set(record.get("tokens", set()))
        tokens.update(_tokenize(rel_path))
        tokens.update(_tokenize(record.get("module", "")))

        imports = payload.get("imports") if isinstance(payload.get("imports"), list) else []
        for item in imports:
            tokens.update(_tokenize(item))

        symbols = payload.get("symbols") if isinstance(payload.get("symbols"), list) else []
        for symbol in symbols:
            symbol_payload = symbol if isinstance(symbol, Mapping) else {}
            symbol_name = str(symbol_payload.get("name", "")).strip()
            kind = str(symbol_payload.get("kind", "")).strip().lower() or "symbol"
            if symbol_name:
                record["symbols"].append({"name": symbol_name, "kind": kind})
                tokens.update(_tokenize(symbol_name))

        record["tokens"] = tokens

    for row in static_rows:
        payload = row if isinstance(row, Mapping) else {}
        rel_path = _normalize_path(payload.get("file_path", ""))
        if not rel_path:
            continue

        record = file_records.setdefault(rel_path, _empty_file_record(rel_path))
        tokens = set(record.get("tokens", set()))
        tokens.update(_tokenize(rel_path))

        imports = payload.get("imports") if isinstance(payload.get("imports"), list) else []
        for item in imports:
            item_payload = item if isinstance(item, Mapping) else {}
            tokens.update(_tokenize(item_payload.get("module", "")))
            tokens.update(_tokenize(item_payload.get("resolved_path", "")))

        calls = payload.get("calls") if isinstance(payload.get("calls"), list) else []
        for call in calls:
            call_payload = call if isinstance(call, Mapping) else {}
            tokens.update(_tokenize(call_payload.get("callee", "")))
            tokens.update(_tokenize(call_payload.get("caller", "")))

        functions = payload.get("functions") if isinstance(payload.get("functions"), list) else []
        for item in functions:
            item_payload = item if isinstance(item, Mapping) else {}
            name = str(item_payload.get("name", "")).strip()
            if name:
                record["symbols"].append({"name": name, "kind": "function"})
                tokens.update(_tokenize(name))

        classes = payload.get("classes") if isinstance(payload.get("classes"), list) else []
        for item in classes:
            item_payload = item if isinstance(item, Mapping) else {}
            name = str(item_payload.get("name", "")).strip()
            if name:
                record["symbols"].append({"name": name, "kind": "class"})
                tokens.update(_tokenize(name))

        record["tokens"] = tokens

    ordered_paths = sorted(file_records.keys())

    pair_similarity: Dict[Tuple[str, str], float] = {}
    adjacency: Dict[str, Set[str]] = {path: set() for path in ordered_paths}

    for index, left_path in enumerate(ordered_paths):
        left_tokens = set(file_records[left_path].get("tokens", set()))
        if not left_tokens:
            continue

        for right_path in ordered_paths[index + 1 :]:
            right_tokens = set(file_records[right_path].get("tokens", set()))
            if not right_tokens:
                continue

            shared = left_tokens.intersection(right_tokens)
            if len(shared) < normalized_min_shared_tokens:
                continue

            similarity = _safe_divide(len(shared), len(left_tokens.union(right_tokens)))
            if similarity < normalized_similarity_threshold:
                continue

            key = (left_path, right_path)
            pair_similarity[key] = round(float(similarity), 6)
            adjacency[left_path].add(right_path)
            adjacency[right_path].add(left_path)

    clusters = _connected_components(adjacency)

    cluster_rows: List[Dict[str, Any]] = []
    duplicate_intent_clusters: List[Dict[str, Any]] = []

    for cluster_index, members in enumerate(clusters, start=1):
        if len(members) < 2:
            continue

        cluster_tokens = Counter()
        contexts: Set[str] = set()
        domains: Counter[str] = Counter()

        for member in members:
            member_tokens = set(file_records[member].get("tokens", set()))
            for token in member_tokens:
                cluster_tokens[token] += 1

            context = _infer_context(member)
            if context:
                contexts.add(context)

            domain = _infer_domain(member_tokens, member)
            domains[domain] += 1

        pair_values = []
        for i, left in enumerate(members):
            for right in members[i + 1 :]:
                key = (left, right) if (left, right) in pair_similarity else (right, left)
                if key in pair_similarity:
                    pair_values.append(float(pair_similarity[key]))

        average_similarity = round(sum(pair_values) / max(1, len(pair_values)), 3)
        cross_context = len(contexts) > 1

        top_terms = [
            item[0]
            for item in sorted(cluster_tokens.items(), key=lambda value: (-int(value[1]), str(value[0])))[:12]
        ]

        dominant_domain = sorted(domains.items(), key=lambda value: (-int(value[1]), str(value[0])))[0][0] if domains else "general"

        row = {
            "id": f"cluster-{cluster_index:03d}",
            "member_count": len(members),
            "members": sorted(members),
            "contexts": sorted(contexts),
            "cross_context": bool(cross_context),
            "average_similarity": average_similarity,
            "top_terms": top_terms,
            "dominant_domain": dominant_domain,
            "duplicate_intent_risk": bool(cross_context and average_similarity >= 0.55),
        }
        cluster_rows.append(row)

        if bool(row["duplicate_intent_risk"]):
            duplicate_intent_clusters.append(row)

    abstraction_collisions = _detect_abstraction_collisions(file_records)

    concept_domains: List[Dict[str, Any]] = []
    for path in ordered_paths:
        tokens = set(file_records[path].get("tokens", set()))
        domain = _infer_domain(tokens, path)
        top_terms = sorted(tokens)[:8]
        concept_domains.append(
            {
                "file_path": path,
                "context": _infer_context(path),
                "domain": domain,
                "top_terms": top_terms,
            }
        )

    high_overlap_cluster_count = len(
        [item for item in cluster_rows if float(item.get("average_similarity", 0.0) or 0.0) >= 0.70]
    )
    cross_context_cluster_count = len([item for item in cluster_rows if bool(item.get("cross_context", False))])

    file_count = len(ordered_paths)
    cluster_count = len(cluster_rows)
    abstraction_collision_count = len(abstraction_collisions)

    cross_context_ratio = _safe_divide(cross_context_cluster_count, max(1, cluster_count))
    overlap_ratio = _safe_divide(high_overlap_cluster_count, max(1, cluster_count))
    collision_ratio = _safe_divide(abstraction_collision_count, max(1, max(1, file_count // 8)))

    penalty = min(1.0, (cross_context_ratio * 0.45) + (overlap_ratio * 0.25) + (collision_ratio * 0.30))
    domain_score = round(max(0.0, min(1.0, 1.0 - penalty)), 3)

    notes: List[str] = []
    if cluster_count == 0:
        notes.append("No semantic overlap clusters met the configured similarity thresholds.")
    if abstraction_collision_count > 0:
        notes.append("Abstraction collisions detected: same concept roots use multiple abstraction types.")

    summary = {
        "file_count": file_count,
        "cluster_count": cluster_count,
        "cross_context_cluster_count": cross_context_cluster_count,
        "high_overlap_cluster_count": high_overlap_cluster_count,
        "duplicate_intent_cluster_count": len(duplicate_intent_clusters),
        "abstraction_collision_count": abstraction_collision_count,
        "similarity_threshold": round(normalized_similarity_threshold, 3),
        "min_shared_tokens": normalized_min_shared_tokens,
        "domain_score": domain_score,
    }

    return {
        "schema_version": "1.0",
        "summary": summary,
        "clusters": sorted(cluster_rows, key=lambda item: (-int(item.get("member_count", 0)), str(item.get("id", "")))),
        "duplicate_intent_clusters": sorted(
            duplicate_intent_clusters,
            key=lambda item: (-float(item.get("average_similarity", 0.0)), str(item.get("id", ""))),
        ),
        "abstraction_collisions": abstraction_collisions,
        "concept_domains": concept_domains,
        "notes": sorted(set(notes)),
    }


def _detect_abstraction_collisions(file_records: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, Any]]:
    concept_map: Dict[str, Dict[str, Any]] = {}

    for path in sorted(file_records.keys()):
        record = file_records[path] if isinstance(file_records[path], Mapping) else {}
        symbols = record.get("symbols") if isinstance(record.get("symbols"), list) else []

        for symbol in symbols:
            payload = symbol if isinstance(symbol, Mapping) else {}
            name = str(payload.get("name", "")).strip()
            if not name:
                continue

            tokens = _tokenize_ordered(name)
            if not tokens:
                continue

            abstraction = ""
            for token in reversed(tokens):
                if token in _ABSTRACTION_SUFFIXES:
                    abstraction = token
                    break

            if not abstraction:
                continue

            root_tokens = [token for token in tokens if token != abstraction]
            deduped_root_tokens: List[str] = []
            for token in root_tokens:
                if token not in deduped_root_tokens:
                    deduped_root_tokens.append(token)
            if not root_tokens:
                continue
            concept_key = "_".join(deduped_root_tokens[:3])

            entry = concept_map.setdefault(
                concept_key,
                {
                    "abstraction_types": set(),
                    "symbols": [],
                    "files": set(),
                },
            )
            entry["abstraction_types"].add(abstraction)
            entry["files"].add(path)
            entry["symbols"].append(
                {
                    "symbol": name,
                    "abstraction": abstraction,
                    "file_path": path,
                }
            )

    collisions: List[Dict[str, Any]] = []
    for concept_key, item in sorted(concept_map.items(), key=lambda value: value[0]):
        abstractions = sorted({str(value) for value in item.get("abstraction_types", set()) if str(value)})
        files = sorted({str(value) for value in item.get("files", set()) if str(value)})
        symbols = item.get("symbols") if isinstance(item.get("symbols"), list) else []

        if len(abstractions) < 3:
            continue
        if len(files) < 2:
            continue

        collisions.append(
            {
                "concept_key": concept_key,
                "abstraction_types": abstractions,
                "file_count": len(files),
                "files": files,
                "sample_symbols": sorted(
                    [symbol for symbol in symbols if isinstance(symbol, Mapping)],
                    key=lambda value: (str(value.get("file_path", "")), str(value.get("symbol", ""))),
                )[:20],
            }
        )

    return collisions


def _connected_components(adjacency: Mapping[str, Set[str]]) -> List[List[str]]:
    visited: Set[str] = set()
    components: List[List[str]] = []

    for start in sorted(adjacency.keys()):
        if start in visited:
            continue

        queue: deque[str] = deque([start])
        visited.add(start)
        component: List[str] = []

        while queue:
            current = queue.popleft()
            component.append(current)

            for nxt in sorted(adjacency.get(current, set())):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)

        components.append(sorted(component))

    return components


def _empty_file_record(rel_path: str) -> Dict[str, Any]:
    return {
        "path": rel_path,
        "module": "",
        "tokens": set(),
        "symbols": [],
    }


def _infer_context(path: str) -> str:
    normalized = _normalize_path(path)
    parts = [segment for segment in normalized.split("/") if segment]
    if not parts:
        return "global"

    ignored = {"repo_audit_engine", "apps", "src", "tests", "archive", "scripts"}
    filtered = [segment for segment in parts[:-1] if segment not in ignored]
    if not filtered:
        return parts[0]

    if len(filtered) == 1:
        return filtered[0]

    return f"{filtered[0]}/{filtered[1]}"


def _infer_domain(tokens: Set[str], path: str) -> str:
    token_set = set(tokens)
    token_set.update(_tokenize(path))

    scored: List[Tuple[str, int]] = []
    for domain, keywords in sorted(_DOMAIN_KEYWORDS.items(), key=lambda item: item[0]):
        score = len(token_set.intersection(keywords))
        if score > 0:
            scored.append((domain, score))

    if not scored:
        return "general"

    scored.sort(key=lambda item: (-int(item[1]), str(item[0])))
    return scored[0][0]


def _tokenize(value: Any) -> Set[str]:
    text = str(value or "").strip()
    if not text:
        return set()

    snake = text.replace("-", "_").replace("/", "_").replace(".", "_").replace(":", "_")
    camel = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake)

    tokens: Set[str] = set()
    for part in re.split(r"[^A-Za-z0-9_]+", camel):
        candidate = part.strip("_").lower()
        if not candidate:
            continue
        for sub in candidate.split("_"):
            token = sub.strip().lower()
            if not token:
                continue
            if len(token) < 3:
                continue
            if token in _STOPWORDS:
                continue
            tokens.add(token)

    return tokens


def _tokenize_ordered(value: Any) -> List[str]:
    text = str(value or "").strip()
    if not text:
        return []

    snake = text.replace("-", "_").replace("/", "_").replace(".", "_").replace(":", "_")
    camel = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", snake)

    ordered: List[str] = []
    for part in re.split(r"[^A-Za-z0-9_]+", camel):
        candidate = part.strip("_").lower()
        if not candidate:
            continue
        for sub in candidate.split("_"):
            token = sub.strip().lower()
            if not token:
                continue
            if len(token) < 3:
                continue
            if token in _STOPWORDS:
                continue
            ordered.append(token)

    return ordered


def _load_jsonl_rows(path: Path) -> List[Dict[str, Any]]:
    if not path.exists() or not path.is_file():
        return []

    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
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


def _normalize_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _safe_divide(numerator: float, denominator: float) -> float:
    if float(denominator) == 0.0:
        return 0.0
    return float(numerator) / float(denominator)
