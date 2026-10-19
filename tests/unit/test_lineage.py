"""Unit tests for the lineage DAG over the checked-in manifest fixture.

The fixture (tests/fixtures/manifest_fixture.json) is generated from the REAL
dbt manifest by scripts/generate_manifest_fixture.py and mirrors the actual
DAG, so none of these tests need dbt or Postgres (DECISIONS 59). The real DAG:
raw.orders â†’ stg_orders â†’ fct_orders â†’ fct_daily_revenue â†’ revenue_dashboard,
with dim_customers downstream of raw.orders via fct_orders (its other parent,
stg_customers, does NOT carry the orders blast radius on its own).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dqs.errors import LineageError
from dqs.lineage.graph import LineageGraph

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "manifest_fixture.json"

P = "model.dq_warehouse"
SRC = "source.dq_warehouse"
E = "exposure.dq_warehouse"


def name_of(unique_id: str) -> str:
    """Readable node name: tests keep theirs, sources keep `schema.table`."""
    parts = unique_id.split(".")
    return f"{parts[2]}.{parts[3]}" if parts[0] == "source" else parts[2]


@pytest.fixture(scope="module")
def graph() -> LineageGraph:
    return LineageGraph.from_manifest(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_fixture_reflects_the_real_dag_shape(graph: LineageGraph) -> None:
    # Nothing was lost in trimming: 5 sources, 8 models, 26 tests, 3 exposures.
    assert len(graph.source_ids()) == 5
    assert sum(1 for u, _ in graph.payloads() if graph.node_kind(u) == "model") == 8
    assert sum(1 for u, _ in graph.payloads() if graph.node_kind(u) == "exposure") == 3


def test_downstream_of_raw_orders_matches_the_real_dag(graph: LineageGraph) -> None:
    names = {name_of(u) for u in graph.downstream(f"{SRC}.raw.orders")}
    assert names == {
        # models
        "stg_orders",
        "fct_orders",
        "fct_daily_revenue",
        "dim_customers",  # via fct_orders (real DAG), not via stg_customers
        # the three exposures
        "revenue_dashboard",
        "customer_360_dashboard",
        "ops_fulfilment_dashboard",
        # dbt tests on stg_orders (incl. relationship tests back to customers)
        "not_null_stg_orders_customer_id",
        "not_null_stg_orders_order_id",
        "not_null_stg_orders_order_ts",
        "not_null_stg_orders_total_amount",
        "unique_stg_orders_order_id",
        "relationships_stg_order_items_order_id__order_id__ref_stg_orders_",
        "relationships_stg_orders_customer_id__customer_id__ref_stg_customers_",
        "relationships_stg_payments_order_id__order_id__ref_stg_orders_",
        "accepted_values_stg_orders_status__placed__paid__shipped__delivered__cancelled",
    }


def test_downstream_models_nearest_first_excludes_exposures_and_tests(
    graph: LineageGraph,
) -> None:
    models = graph.downstream(f"{SRC}.raw.orders", types=("model",))
    assert all(u.startswith("model.") for u in models)
    assert models[0] == f"{P}.stg_orders"
    assert [name_of(u) for u in models[:3]] == [
        "stg_orders",
        "fct_orders",
        "dim_customers",  # same distance as fct_daily_revenue; name-sorted
    ]


def test_downstream_includes_dbt_tests(graph: LineageGraph) -> None:
    tests = graph.downstream(f"{SRC}.raw.orders", types=("test",))
    assert any("not_null_stg_orders_customer_id" in t for t in tests)
    assert any("unique_stg_orders_order_id" in t for t in tests)


def test_types_filter_supports_exposures(graph: LineageGraph) -> None:
    exposures = graph.downstream(f"{SRC}.raw.orders", types=("exposure",))
    assert set(exposures) == {
        f"{E}.revenue_dashboard",
        f"{E}.customer_360_dashboard",
        f"{E}.ops_fulfilment_dashboard",
    }


def test_upstream_of_fct_orders(graph: LineageGraph) -> None:
    ups = graph.upstream(f"{P}.fct_orders")
    names = {name_of(u) for u in ups}
    assert names == {
        "stg_order_items",
        "stg_payments",
        "stg_orders",
        "raw.order_items",
        "raw.payments",
        "raw.orders",
    }


def test_upstream_nearest_first_orders_models(graph: LineageGraph) -> None:
    models = graph.upstream(f"{P}.fct_orders", types=("model",))
    # all three are one hop away; the order is the (distance, name) tiebreak
    assert models == [f"{P}.stg_order_items", f"{P}.stg_orders", f"{P}.stg_payments"]


def test_upstream_of_exposure_hits_the_raw_source(graph: LineageGraph) -> None:
    assert f"{SRC}.raw.orders" in graph.upstream(f"{E}.revenue_dashboard")


def test_nodes_for_raw_table_maps_by_schema_identifier(graph: LineageGraph) -> None:
    assert graph.nodes_for_raw_table("raw.orders") == f"{SRC}.raw.orders"
    assert graph.nodes_for_raw_table("raw.payments") == f"{SRC}.raw.payments"


def test_nodes_for_raw_table_unknown_table_is_a_clear_error(graph: LineageGraph) -> None:
    with pytest.raises(LineageError) as exc:
        graph.nodes_for_raw_table("raw.invoices")
    assert "no dbt source" in str(exc.value)


def test_unknown_node_downstream_is_a_clear_error(graph: LineageGraph) -> None:
    with pytest.raises(LineageError) as exc:
        graph.downstream(f"{P}.does_not_exist")
    assert "unknown lineage node" in str(exc.value)


def test_unknown_node_upstream_is_a_clear_error(graph: LineageGraph) -> None:
    with pytest.raises(LineageError):
        graph.upstream("exposure.dq_warehouse.huh")


def test_distances_increase_with_the_graph(graph: LineageGraph) -> None:
    source = f"{SRC}.raw.orders"
    assert graph.distance(source, f"{P}.stg_orders") == 1
    assert graph.distance(source, f"{P}.fct_orders") == 2
    assert graph.distance(source, f"{P}.fct_daily_revenue") == 3
    assert graph.distance(source, f"{E}.revenue_dashboard") == 4


def test_downstream_of_unrelated_source_excludes_the_orders_chain(
    graph: LineageGraph,
) -> None:
    names = {name_of(u) for u in graph.downstream(f"{SRC}.raw.customers")}
    assert "stg_orders" not in names
    assert {"stg_customers", "dim_customers"} <= names

