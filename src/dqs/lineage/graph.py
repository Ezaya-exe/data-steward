"""DAG over the dbt manifest (networkx) — ARCHITECTURE §9.

Why here: impact analysis is a graph reachability question ("what is
downstream of this raw source?") that must never go through an LLM. Edges come
from the manifest's ``child_map``/``parent_map`` when present (dbt's own view,
including tests' parents), falling back to ``depends_on.nodes`` derivation so
hand-written fixtures stay easy; both agree on the real DAG (asserted in
unit tests).

Node kinds (unique_id prefixes): ``source``, ``model``, ``test``, ``exposure``
(dbt also emits ``seed``/``snapshot``/``unit`` — passed through with their
prefix so nothing is silently dropped). Raw- table names like ``raw.orders``
map to source nodes via the source's ``schema`` + ``identifier``.
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx

from dqs.errors import LineageError
from dqs.lineage.manifest import PROJECT_DIR, load_manifest

_NODE_KINDS: tuple[str, ...] = ("model", "test", "source", "exposure", "seed", "snapshot", "unit")


def _kind(unique_id: str) -> str:
    prefix = unique_id.split(".", 1)[0]
    if prefix not in _NODE_KINDS:
        raise LineageError(f"unknown dbt node kind in unique_id {unique_id!r}")
    return prefix


class LineageGraph:
    """Typed view over the manifest DAG with upstream/downstream queries."""

    def __init__(
        self,
        graph: nx.DiGraph,
        nodes: dict[str, dict[str, object]],
        project_dir: Path = PROJECT_DIR,
    ) -> None:
        # _nodes keeps node payload dicts (resource_type, depends_on, compiled_code,
        # owner, ...) alongside the graph exactly as the manifest named them.
        # project_dir anchors the target/compiled fallback for compiled SQL.
        self._graph = graph
        self._nodes = nodes
        self.project_dir = project_dir

    @classmethod
    def from_manifest(
        cls,
        manifest: dict[str, object] | None = None,
        *,
        project_dir: Path = PROJECT_DIR,
    ) -> LineageGraph:
        """Build from a manifest dict (unit-test fixture) or load the real one.

        Without ``manifest`` the real file is loaded (and regenerated when
        stale) via `dqs.lineage.manifest.load_manifest`.
        """
        if manifest is None:
            manifest = load_manifest(project_dir)

        # Collect every node payload keyed by unique_id (models/tests live in
        # `nodes`; sources and exposures have their own maps).
        collected: dict[str, dict[str, object]] = {}
        for bucket in ("nodes", "sources", "exposures"):
            layer = manifest.get(bucket)
            if not isinstance(layer, dict):
                continue
            for unique_id, payload in layer.items():
                if not isinstance(payload, dict):
                    raise LineageError(f"manifest {bucket}.{unique_id} is not an object")
                collected[unique_id] = dict(payload)

        # Edges: prefer dbt's own parent_map/child_map (complete, includes
        # dangling refs); derive from depends_on when either side is absent.
        parents: dict[str, list[str]] = {}
        parent_map = manifest.get("parent_map")
        if isinstance(parent_map, dict):
            for key, parent_list in parent_map.items():
                children_of = list(parent_list) if isinstance(parent_list, list) else []
                parents[str(key)] = [p for p in children_of if p in collected]
        else:
            for unique_id, payload in collected.items():
                deps = payload.get("depends_on") or {}
                if isinstance(deps, dict):
                    parents[unique_id] = [p for p in (deps.get("nodes") or []) if p in collected]

        graph = nx.DiGraph()
        for unique_id in collected:
            graph.add_node(unique_id, kind=_kind(unique_id))
        for child, parent_list in parents.items():
            if child not in graph:
                continue
            for parent in parent_list:
                if parent in graph:
                    graph.add_edge(parent, child)

        directory = project_dir if isinstance(project_dir, Path) else PROJECT_DIR
        return cls(graph, collected, directory)

    def node(self, unique_id: str) -> dict[str, object]:
        """Get one node's payload with a clear unknown-node error (acceptance)."""
        if unique_id not in self._nodes:
            known = "\n".join(sorted(self._nodes)[:20])
            raise LineageError(
                f"unknown lineage node {unique_id!r}; known ids (first 20):\n{known}"
            )
        return self._nodes[unique_id]

    def _reachable(self, unique_id: str, reverse: bool) -> list[str]:
        """Reachable ids (never including the start), nearest-first, name-tiebreak."""
        self.node(unique_id)
        if reverse:
            found = nx.ancestors(self._graph, unique_id)
            walk = self._graph.reverse()
        else:
            found = nx.descendants(self._graph, unique_id)
            walk = self._graph
        distances = nx.single_source_shortest_path_length(walk, unique_id, cutoff=None)
        return sorted(found, key=lambda n: (distances[n], n))

    def distance(self, start: str, target: str) -> int:
        """Shortest directed distance start→target (−1 when unreachable)."""
        self.node(start)
        self.node(target)
        dist = nx.single_source_shortest_path_length(self._graph, start, cutoff=None)
        return dist.get(target, -1)

    def downstream(self, unique_id: str, types: tuple[str, ...] | None = None) -> list[str]:
        """Every reachable node downstream of ``unique_id``, nearest first.

        ``types`` filters by node kind (e.g. ``("model", "exposure")``); the
        start node itself is never included.
        """
        found = self._reachable(unique_id, reverse=False)
        if types is None:
            return found
        return [n for n in found if _kind(n) in set(types)]

    def upstream(self, unique_id: str, types: tuple[str, ...] | None = None) -> list[str]:
        """Every node upstream of ``unique_id``, nearest first."""
        found = self._reachable(unique_id, reverse=True)
        if types is None:
            return found
        return [n for n in found if _kind(n) in set(types)]

    def nodes_for_raw_table(self, table: str) -> str:
        """Map a qualified raw-table name like ``raw.orders`` to its source node.

        Matches on the source's ``schema`` + ``identifier`` payload, which is
        what dbt itself resolved (fixtures set both too). Raises LineageError
        when no source matches — the caller reports the table name back to the
        user instead of silently returning an empty blast radius.
        """
        wanted = table.lower().replace(" ", "")
        schema_name, _, identifier = wanted.rpartition(".")
        for unique_id, payload in self._nodes.items():
            if _kind(unique_id) != "source":
                continue
            if (
                str(payload.get("schema", "")).lower() == schema_name
                and str(payload.get("identifier", "")).lower() == identifier
            ):
                return unique_id
        raise LineageError(f"no dbt source maps to table {table!r}")

    def kind(self, unique_id: str) -> str:
        """The node kind ('source' | 'model' | 'test' | 'exposure' | …)."""
        self.node(unique_id)
        return _kind(unique_id)

    def node_kind(self, unique_id: str) -> str:
        """`kind` without the existence check (internal iteration paths)."""
        return _kind(unique_id)

    def has_node(self, unique_id: str) -> bool:
        """True when the unique_id exists in this manifest."""
        return unique_id in self._nodes

    def payloads(self) -> list[tuple[str, dict[str, object]]]:
        """All (unique_id, payload) pairs, sorted for deterministic iteration."""
        return sorted(self._nodes.items())

    def source_ids(self) -> list[str]:
        """Every source node's unique_id, sorted."""
        return sorted(u for u in self._nodes if _kind(u) == "source")

    def has_sources(self) -> bool:
        return any(_kind(u) == "source" for u in self._nodes)

    def underlying_graph(self) -> nx.DiGraph:
        """The networkx DiGraph itself (reachability helpers elsewhere)."""
        return self._graph
