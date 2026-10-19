"""Unit tests for models_referencing_column (sqlglot heuristic on the fixture).

The fixture carries each model's compiled_code exactly as dbt produced it, so
the heuristic is exercised against real compiled SQL without needing dbt.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dqs.lineage.columns import models_referencing_column
from dqs.lineage.graph import LineageGraph

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "manifest_fixture.json"


@pytest.fixture(scope="module")
def graph() -> LineageGraph:
    return LineageGraph.from_manifest(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_orders_customer_id_is_found_in_stg_orders(graph: LineageGraph) -> None:
    refs = models_referencing_column(graph, "raw.orders", "customer_id")
    # Only stg_orders selects directly FROM raw.orders in compiled SQL
    # (fct_orders reads stg_orders); the acceptance asks for stg_orders being
    # present and stg_products absent.
    assert refs == ["model.dq_warehouse.stg_orders"]
    assert "model.dq_warehouse.stg_products" not in refs


def test_unrelated_table_column_returns_nothing(graph: LineageGraph) -> None:
    # products has no customer_id; stg_products never selects it
    refs = models_referencing_column(graph, "raw.products", "customer_id")
    assert refs == []


def test_orders_order_id_reaches_direct_referencing_models(graph: LineageGraph) -> None:
    refs = models_referencing_column(graph, "raw.orders", "order_id")
    assert refs == ["model.dq_warehouse.stg_orders"]


def test_result_is_sorted_deterministically(graph: LineageGraph) -> None:
    refs = models_referencing_column(graph, "raw.orders", "order_id")
    assert refs == sorted(refs)
    assert refs == models_referencing_column(graph, "raw.orders", "order_id")


def test_unknown_raw_table_raises_a_clear_error(graph: LineageGraph) -> None:
    from dqs.errors import LineageError

    with pytest.raises(LineageError) as exc:
        models_referencing_column(graph, "raw.invoices", "customer_id")
    assert "no dbt source" in str(exc.value)


def test_empty_column_string_is_rejected(graph: LineageGraph) -> None:
    from dqs.errors import LineageError

    with pytest.raises(LineageError):
        models_referencing_column(graph, "raw.orders", "")
