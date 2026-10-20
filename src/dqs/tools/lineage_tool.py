"""`get_lineage` — typed upstream/downstream traversal of the dbt DAG.

The graph comes from `ToolContext.graph()`, built once per investigation
(Phase-3 review note). No SQL and no LLM: dbt's manifest is the source of
truth, and unknown nodes are reported as a clear tool error.
"""

from __future__ import annotations

from typing import Any

from dqs.errors import LineageError
from dqs.lineage.graph import LineageGraph
from dqs.tools.base import ToolContext, data_tool


def resolve_node(graph: LineageGraph, node: str) -> str:
    """Accept a unique_id, a raw table (`raw.orders`), or a bare model name."""
    candidate = node.strip()
    if graph.has_node(candidate):
        return candidate
    if "." in candidate:
        try:
            return graph.nodes_for_raw_table(candidate)
        except LineageError:
            pass
    matches = [
        unique_id
        for unique_id, payload in graph.payloads()
        if str(payload.get("name", "")) == candidate
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise LineageError(
            f"node name {candidate!r} is ambiguous; use a unique_id: {', '.join(matches)}"
        )
    raise LineageError(
        f"unknown lineage node {candidate!r}; use a unique_id, raw.table, or model name"
    )


@data_tool
def get_lineage(ctx: ToolContext, node: str, direction: str = "downstream") -> dict[str, Any]:
    """Names upstream or downstream of a node, nearest first."""
    normalized = direction.strip().lower()
    if normalized not in ("upstream", "downstream"):
        raise LineageError("direction must be 'upstream' or 'downstream'")
    graph = ctx.graph()
    unique_id = resolve_node(graph, node)
    if normalized == "downstream":
        ids = graph.downstream(unique_id)
        distances = {other: graph.distance(unique_id, other) for other in ids}
    else:
        ids = graph.upstream(unique_id)
        distances = {other: graph.distance(other, unique_id) for other in ids}
    payload = graph.node(unique_id)
    entries = [
        {
            "unique_id": other,
            "name": str(graph.node(other).get("name", other)),
            "kind": graph.kind(other),
            "distance": distances[other],
        }
        for other in ids
    ]
    return {
        "node": unique_id,
        "name": str(payload.get("name", unique_id)),
        "kind": graph.kind(unique_id),
        "direction": normalized,
        "count": len(entries),
        "nodes": entries,
    }
