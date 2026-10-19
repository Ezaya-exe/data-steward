"""Deterministic blast-radius builder (ARCHITECTURE §9, §10.4).

Why here: impact = graph reachability + a documented scoring formula + one
template-rendered summary. Nothing here may call an LLM; the single LLM seam
is `render_summary`, which Phase 4 replaces with one structured call (keeping
this template as the fallback). Everything is a pure function of (manifest
graph, incident, stored pipeline record), so two runs with the same inputs
produce identical reports.

Scope rule (DECISIONS 56):
- schema/column incidents (a specific column is named: F1/F2/F10) →
  column-level lineage; only models referencing that column are hit.
- table-scoped incidents without a column (volume, absent table) → all models
  downstream of that one raw source.
- batch-level lateness / missing batch (no table) → downstream of ALL raw
  sources (the whole reachable DAG).

Failing tests come ONLY from the stored record (`dq.pipeline_runs.failed_nodes`,
parsed at build time, DECISIONS 55) — the on-disk run_results.json is never
trusted here.
"""

from __future__ import annotations

import networkx as nx

from dqs.errors import LineageError
from dqs.lineage.columns import models_referencing_column
from dqs.lineage.graph import LineageGraph
from dqs.models.enums import ImpactScope, Severity
from dqs.models.impact import AffectedExposure, AffectedModel, ImpactReport
from dqs.models.incident import Incident
from dqs.models.pipeline import NodeResult, PipelineRunRecord

# --- scoring (DECISIONS 60) -----------------------------------------------------------
# score = 100 × Σ_exposure maturity_weight + 10 × Σ_affected_model 1/distance
# maturity_weight: high 1.0 > medium 0.6 > low 0.3 (unknown 0.5). Exposures
# weigh by count (they all sum) and by maturity; models weigh by graph
# distance — a one-hop model contributes more than a three-hop one, modeling
# "how close to the break the blast starts". Deterministic and cheap; the
# number is for ranking/communicating incidents, not an SLA.
_EXPOSURE_UNIT: float = 100.0
_MODEL_UNIT: float = 10.0
_MATURITY_WEIGHT: dict[str, float] = {"high": 1.0, "medium": 0.6, "low": 0.3}
_MATURITY_FALLBACK: float = 0.5


def _multi_source_distances(graph: nx.DiGraph, roots: list[str]) -> dict[str, int]:
    """BFS min-distance map from several roots (networkx ≥3.4 moved the
    multi_source helper into a submodule; a 10-line BFS is equivalent and
    version-proof — all edges have weight 1)."""
    from collections import deque

    dist: dict[str, int] = {r: 0 for r in roots}
    queue: deque[str] = deque(roots)
    while queue:
        node = queue.popleft()
        for child in graph.successors(node):
            if child not in dist:
                dist[child] = dist[node] + 1
                queue.append(child)
    return dist


def blast_radius_score(
    models: list[AffectedModel], exposures: list[AffectedExposure]
) -> float:
    """The documented, deterministic blast-radius score (DECISIONS 60)."""
    model_term = sum(1.0 / m.distance for m in models if m.distance >= 1)
    exposure_term = sum(
        _MATURITY_WEIGHT.get(e.maturity.lower(), _MATURITY_FALLBACK) for e in exposures
    )
    return round(_MODEL_UNIT * model_term + _EXPOSURE_UNIT * exposure_term, 2)


def _layer_of(node_payload: dict[str, object]) -> str:
    """staging | marts | unknown, from the model's path inside the dbt project."""
    path = str(node_payload.get("original_file_path", "") or "")
    parts = path.lower().replace("\\", "/").split("/")
    if "staging" in parts:
        return "staging"
    if "marts" in parts:
        return "marts"
    return "unknown"


def _affected_column(incident: Incident) -> str | None:
    """The column the incident names, deterministically.

    A *breaking* violation's column is authoritative (the check engine reports
    at most one breaking column per incident); otherwise the first sorted
    non-null column; else None.
    """
    breaking = [v for v in incident.violations if v.severity is Severity.BREAKING]
    pool = breaking or incident.violations
    columns = sorted({v.column for v in pool if v.column is not None})
    return columns[0] if columns else None


def _model_entry(
    graph: LineageGraph, unique_id: str, distance: int, referencing: bool
) -> AffectedModel:
    payload = graph.node(unique_id)
    return AffectedModel(
        unique_id=unique_id,
        name=str(payload.get("name", unique_id)),
        layer=_layer_of(payload),
        distance=distance,
        referencing_column=referencing,
    )


def _min_distance(graph: LineageGraph, source_ids: list[str], model_id: str) -> int:
    """Smallest hop count from any source; ≥1 (the model is never the source)."""
    distances = [graph.distance(s, model_id) for s in source_ids]
    positive = [d for d in distances if d > 0]
    return min(positive, default=1)


def _export_source_ids(graph: LineageGraph) -> list[str]:
    """All dbt source node ids, sorted (used for warehouse scope)."""
    if not graph.has_sources():
        raise LineageError("manifest defines no dbt sources; warehouse impact is undefined")
    return graph.source_ids()


def _qualified_name(graph: LineageGraph, source_id: str) -> str:
    payload = graph.node(source_id)
    schema = str(payload.get("schema", "") or "")
    identifier = str(payload.get("identifier", "") or "")
    if schema and identifier:
        return f"{schema}.{identifier}".lower()
    parts = source_id.split(".")
    return f"{parts[-2]}.{parts[-1]}".lower()


def _affected_models(
    graph: LineageGraph, source_ids: list[str], column: str | None, scope: ImpactScope
) -> tuple[list[AffectedModel], bool]:
    """(affected models, the blast was scoped by column-level lineage)."""
    if scope is ImpactScope.COLUMN and column:
        referenced: list[str] = []
        for source_id in source_ids:
            referenced.extend(
                models_referencing_column(
                    graph, _qualified_name(graph, source_id), column, graph.project_dir
                )
            )
        unique = sorted(set(referenced))
        if unique:
            entries = [
                _model_entry(graph, model_id, _min_distance(graph, source_ids, model_id), True)
                for model_id in unique
            ]
            entries.sort(key=lambda m: (m.distance, m.unique_id))
            return entries, True
        # The heuristic found no reference — honest fallback: the *table-level*
        # footprint. An empty report can never quietly hide a break; the
        # summary (see render_summary) is told the lineage found nothing.
        return _table_scope_models(graph, source_ids), False
    return _table_scope_models(graph, source_ids), False


def _table_scope_models(graph: LineageGraph, source_ids: list[str]) -> list[AffectedModel]:
    """All models downstream of the given sources, min-hop distance each."""
    walks = _multi_source_distances(graph.underlying_graph(), source_ids)
    entries: list[AffectedModel] = []
    for unique_id, distance in sorted(walks.items(), key=lambda kv: (kv[1], kv[0])):
        if graph.node_kind(unique_id) != "model":
            continue
        entries.append(_model_entry(graph, unique_id, distance, False))
    return entries


def _affected_exposures(
    graph: LineageGraph,
    source_ids: list[str],
    models: list[AffectedModel],
    restrict_to_models: bool,
) -> list[AffectedExposure]:
    """Exposures downstream of the models the blast already reached.

    COLUMN scope (restrict_to_models): the blast lives in the referencing
    models, so an exposure counts when it is reachable **through the chain**
    from one of them (a dashboard that never reads data carrying the broken
    column is not impacted). TABLE/WAREHOUSE scope: every exposure reachable
    from the affected sources counts.
    """
    roots: list[str] = (
        [m.unique_id for m in models] if restrict_to_models else source_ids
    )
    reach = _multi_source_distances(graph.underlying_graph(), roots)
    exposures: list[AffectedExposure] = []
    for unique_id, payload in graph.payloads():
        if graph.node_kind(unique_id) != "exposure":
            continue
        depends = payload.get("depends_on") or {}
        parents = [p for p in (depends.get("nodes") or []) if graph.has_node(p)]  # type: ignore[union-attr]
        # An exposure is impacted iff a parent is in `reach` (the roots sit at
        # distance 0, so an exposure fed directly by a source/referencing
        # model is covered too).
        if any(p in reach for p in parents):
            exposures.append(_exposure_entry(unique_id, payload))
    return sorted(exposures, key=lambda e: e.unique_id)


def _exposure_entry(unique_id: str, payload: dict[str, object]) -> AffectedExposure:
    owner = payload.get("owner")
    owner_text = ""
    if isinstance(owner, dict):
        owner_text = str(owner.get("name") or owner.get("email") or "")
    elif isinstance(owner, str):
        owner_text = owner
    return AffectedExposure(
        unique_id=unique_id,
        name=str(payload.get("name", unique_id)),
        owner=owner_text,
        maturity=str(payload.get("maturity", "unknown") or "unknown"),
    )


def _stored_failing_tests(
    record: PipelineRunRecord | None, batch_id: str | None
) -> list[NodeResult]:
    """Failed/errored nodes from the STORED record only (never live files)."""
    if record is None:
        return []
    nodes = [n for n in record.failed_nodes if batch_id in (None, n.batch_id)]
    return sorted(nodes, key=lambda n: (n.unique_id, n.message))


def build_impact_report(
    graph: LineageGraph,
    incident: Incident,
    record: PipelineRunRecord | None = None,
) -> ImpactReport:
    """Compute the complete ImpactReport deterministically (no LLM anywhere)."""
    column = _affected_column(incident)
    if incident.table is None:
        scope = ImpactScope.WAREHOUSE
    elif column is not None:
        scope = ImpactScope.COLUMN
    else:
        scope = ImpactScope.TABLE

    source_ids = _sources_of_table(
        graph, None if scope is ImpactScope.WAREHOUSE else incident.table
    )
    models, blasted_by_column = _affected_models(graph, source_ids, column, scope)
    restrict = scope is ImpactScope.COLUMN
    exposures = _affected_exposures(graph, source_ids, models, restrict)

    report = ImpactReport(
        incident_id=incident.incident_id,
        source_table=incident.table,
        column=column,
        scope=scope,
        affected_models=models,
        failing_tests=_stored_failing_tests(record, incident.batch_id),
        affected_exposures=exposures,
        blast_radius_score=blast_radius_score(models, exposures),
    )
    report.summary = render_summary(report, column_lineage_used=blasted_by_column)
    return report


def _sources_of_table(graph: LineageGraph, table: str | None) -> list[str]:
    if table is None:
        exporters = _export_source_ids(graph)
        return exporters
    return [graph.nodes_for_raw_table(table or "")]


# --- the Phase-4 hook (DECISIONS 61) ---------------------------------------------------


def render_summary(report: ImpactReport, column_lineage_used: bool = False) -> str:
    """Render the 1–3 sentence summary from the structured report.

    ***PHASE-4 HOOK***: replace the body of this function with ONE LLM call
    that rewrites the summary from `report.model_dump()` — a single attempt,
    no tools, bounded tokens. If the LLM call fails, times out, or returns an
    empty string, keep THIS template (deterministic, honest, and what the
    unit tests assert by default in Phase 3).
    """
    table = report.source_table or "batch-level fault across all raw sources"
    column_bit = f" (column {report.column})" if report.column else ""
    lineage_bit = " [column-level lineage applied]" if column_lineage_used else ""
    count = len(report.affected_models)
    names = [m.name for m in report.affected_models[:3]]
    more = f" (+{count - 3} more)" if count > 3 else ""
    names_text = ", ".join(names)
    affected_text = (
        f"affected models: {names_text}{more}" if names else "affected models: none downstream"
    )
    exposures = ", ".join(
        f"{e.name} ({e.owner or 'no owner'})" for e in report.affected_exposures
    )
    exposure_text = f"exposures impacted: {exposures}" if exposures else "no exposures impacted"
    tests = len(report.failing_tests)
    tests_text = (
        f"stored dbt record shows {tests} failed/errored node(s)"
        if tests
        else "no parsed dbt failure record was stored for this batch"
    )
    return (
        f"Blast radius for {table}{column_bit}{lineage_bit}: {affected_text}; "
        f"{exposure_text}; {tests_text}; blast radius score {report.blast_radius_score}."
    )
