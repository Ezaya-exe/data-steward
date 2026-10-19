"""Column-level lineage against the REAL dbt manifest (Phase 3, integration).

``target/`` is gitignored, so on a fresh checkout ``LineageGraph.from_manifest``
regenerates the manifest via ``dbt compile`` (needs Postgres — hence the
integration marker). The acceptance: ``models_referencing_column`` on
``raw.orders``/``customer_id`` returns ``stg_orders`` and never
``stg_products``, and the naive-table check matches the real compiled SQL.
"""

from __future__ import annotations

import pytest

from dqs.lineage.columns import models_referencing_column
from dqs.lineage.graph import LineageGraph

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def real_graph() -> LineageGraph:
    return LineageGraph.from_manifest()  # loads (or regenerates) the real manifest


def test_compiled_code_is_available_for_all_models(real_graph: LineageGraph) -> None:
    models = [u for u, _ in real_graph.payloads() if real_graph.node_kind(u) == "model"]
    missing = [u for u in models if not str(real_graph.node(u).get("compiled_code") or "").strip()]
    assert missing == [], "after dbt compile every model must carry compiled_code"


def test_column_reference_on_real_manifest(real_graph: LineageGraph) -> None:
    refs = models_referencing_column(real_graph, "raw.orders", "customer_id")
    assert "model.dq_warehouse.stg_orders" in refs
    assert "model.dq_warehouse.stg_products" not in refs


def test_column_reference_excludes_unrelated_sources(real_graph: LineageGraph) -> None:
    assert models_referencing_column(real_graph, "raw.products", "customer_id") == []


def test_real_dag_downstream_of_raw_orders(real_graph: LineageGraph) -> None:
    names = {
        u.split(".")[2]
        for u in real_graph.downstream(
            "source.dq_warehouse.raw.orders", types=("model", "exposure")
        )
    }
    assert {"stg_orders", "fct_orders", "fct_daily_revenue"} <= names
    assert {"revenue_dashboard", "ops_fulfilment_dashboard", "customer_360_dashboard"} <= names
