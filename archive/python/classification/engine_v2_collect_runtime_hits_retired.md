# Retired Method Archive: EvidenceClassifier._collect_runtime_hits

Retired on 2026-04-25.

Reason:
- The method duplicated runtime hit collection logic already implemented and used in `EvidenceClassifier._analyze_runtime_trace_rows`.
- It had no call sites in the codebase and risked future divergence if maintained separately.

Archived implementation:

```python
def _collect_runtime_hits(
    self,
    runtime_trace_rows: Iterable[Mapping[str, Any]],
    canonical_lookup: Mapping[str, str],
) -> Dict[str, int]:
    hits: Dict[str, int] = {}

    for item in runtime_trace_rows:
        row = item if isinstance(item, Mapping) else {}
        if str(row.get("event", "")).strip().lower() != "call":
            continue

        node_id = self._node_id_from_runtime_row(row, canonical_lookup)
        if not node_id:
            continue

        hits[node_id] = int(hits.get(node_id, 0)) + 1

    return hits
```
