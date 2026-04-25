from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GraphNode:
    id: str
    kind: str


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    type: str
