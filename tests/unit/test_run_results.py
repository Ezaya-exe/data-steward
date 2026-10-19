"""Unit tests for run_results.json parsing (Phase 3, DECISIONS 55).

The parser is pure: fixtures stand in for real dbt output; no dbt, no DB.
Negative cases cover unreadable/malformed files (a torn file after a crashed
dbt run must not lose the run record itself).
"""

from __future__ import annotations

import json
from pathlib import Path

from dqs.dbt_run import parse_run_results

RUN_RESULTS = {
    "metadata": {"dbt_version": "1.12.6"},
    "results": [
        {
            "unique_id": "model.dq_warehouse.stg_orders",
            "name": "stg_orders",
            "status": "error",
            "message": "column customer_id does not exist",
        },
        {
            "unique_id": "test.dq_warehouse.unique_stg_orders_order_id.e3b841c71a",
            "name": "unique_stg_orders_order_id",
            "status": "fail",
            "message": "Got 3 duplicate rows",
        },
        {
            "unique_id": "test.dq_warehouse.unique_stg_payments_payment_id.3744",
            "name": "unique_stg_payments_payment_id",
            "status": "pass",
            "message": None,
        },
        {
            "unique_id": "model.dq_warehouse.stg_payments",
            "name": "stg_payments",
            "status": "success",
            "message": None,
        },
    ],
}


def test_parses_only_failed_and_errored_nodes(tmp_path: Path) -> None:
    path = tmp_path / "run_results.json"
    path.write_text(json.dumps(RUN_RESULTS), encoding="utf-8")
    nodes = parse_run_results(path, batch_id="batch_20260205")
    assert [n.name for n in nodes] == ["stg_orders", "unique_stg_orders_order_id"]
    statuses = {n.name: n.status for n in nodes}
    assert statuses == {"stg_orders": "error", "unique_stg_orders_order_id": "fail"}
    assert all(n.batch_id == "batch_20260205" for n in nodes)


def test_output_is_sorted_deterministically(tmp_path: Path) -> None:
    path = tmp_path / "run_results.json"
    shuffled = {**RUN_RESULTS, "results": list(reversed(RUN_RESULTS["results"]))}
    path.write_text(json.dumps(shuffled), encoding="utf-8")
    nodes = parse_run_results(path)
    assert [n.unique_id for n in nodes] == sorted(n.unique_id for n in nodes)


def test_missing_file_is_not_an_error(tmp_path: Path) -> None:
    assert parse_run_results(tmp_path / "nope.json") == []


def test_torn_or_invalid_file_is_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "run_results.json"
    path.write_text('{"results": [ partial', encoding="utf-8")
    assert parse_run_results(path) == []


def test_malformed_entries_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "run_results.json"
    payload = {"results": ["oops", {"status": "fail"}, {"unique_id": "test.a.b", "name": "a", "status": "fail"}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    nodes = parse_run_results(path)
    assert [n.unique_id for n in nodes] == ["test.a.b"]
