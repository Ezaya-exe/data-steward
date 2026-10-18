"""Unit tests for the contract store: strictness and byte determinism.

Why negative-first: the store is the boundary through which every contract
rule reaches the check engine. A typo'd key must fail loudly (§7.1), and a
second write must reproduce the exact bytes (Phase 2 acceptance).
"""

from __future__ import annotations

import datetime as dt

import pytest
import yaml
from pydantic import ValidationError

from dqs.contracts import store
from dqs.errors import ContractError
from dqs.models.contract import Contract
from dqs.models.enums import ContractStatus


def _contract(table: str = "raw.orders") -> Contract:
    return Contract(
        table=table,
        layer="raw",
        generated_at=dt.datetime(2026, 1, 30, 0, 0, tzinfo=dt.UTC),
        baseline={"batches": 30, "first": "2026-01-01", "last": "2026-01-30", "rows": 13891},
        primary_key=["order_id"],
        columns={
            "order_id": {"logical_type": "integer", "nullable": False, "unique": True},
            "status": {
                "logical_type": "string",
                "nullable": False,
                "allowed_values": ["placed", "paid"],
            },
        },
        volume={"min_rows": 242, "max_rows": 867, "required_in_batch": True},
        freshness={"max_delay_minutes": 360},
    )


def test_save_and_load_round_trip(tmp_path) -> None:
    path = store.save(_contract(), tmp_path)
    assert path.name == "raw.orders.yml"
    loaded = store.load_contract(path)
    assert loaded == _contract()


def test_save_is_byte_identical_for_two_runs(tmp_path) -> None:
    first = store.save(_contract(), tmp_path / "a")
    second = store.save(_contract(), tmp_path / "b")
    assert first.read_bytes() == second.read_bytes()


def test_yaml_has_no_anchors_and_sorted_keys(tmp_path) -> None:
    text = store.to_yaml(_contract())
    assert "&id" not in text
    lines = text.splitlines()
    # top-level keys sorted: baseline, columns, contract_version, freshness…
    top = [line for line in lines if not line.startswith(" ")]
    assert top[:2] == ["baseline:", "columns:"]


def test_unknown_top_level_key_rejected(tmp_path) -> None:
    document = yaml.safe_load(store.to_yaml(_contract()))
    document["volume"]["typod_key"] = 10  # nudge into the strict-submodel case
    path = tmp_path / "raw.orders.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ContractError, match="validation failed"):
        store.load_contract(path)


def test_unknown_column_key_rejected(tmp_path) -> None:
    document = yaml.safe_load(store.to_yaml(_contract()))
    document["columns"]["order_id"]["nullabel"] = False  # the classic typo
    path = tmp_path / "raw.orders.yml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ContractError, match="nullabel"):
        store.load_contract(path)


def test_bad_table_identifier_rejected(tmp_path) -> None:
    with pytest.raises(ValidationError):
        Contract.model_validate(
            {
                **_contract("raw.9orders").model_dump(mode="json", exclude_none=True),
            }
        )


def test_fk_reference_block_forbids_nonidentifiers() -> None:
    with pytest.raises(ValidationError):
        Contract.model_validate(
            {
                **_contract().model_dump(mode="json", exclude_none=True),
                "columns": {
                    "customer_id": {
                        "logical_type": "integer",
                        "nullable": False,
                        "references": {"table": "raw..customers", "column": "customer_id"},
                    }
                },
            }
        )


def test_unparsable_yaml_reports_contract_error(tmp_path) -> None:
    path = tmp_path / "raw.orders.yml"
    path.write_text("columns: [unclosed", encoding="utf-8")
    with pytest.raises(ContractError, match="YAML parse failed"):
        store.load_contract(path)


def test_approve_all_flips_every_status(tmp_path) -> None:
    store.save(_contract("raw.orders"), tmp_path)
    store.save(_contract("raw.payments"), tmp_path)
    rewritten = store.approve(tmp_path, None)
    assert len(rewritten) == 2
    for contract in store.load_contracts(tmp_path).values():
        assert contract.status is ContractStatus.APPROVED


def test_approve_flips_only_requested_table(tmp_path) -> None:
    store.save(_contract("raw.orders"), tmp_path)
    store.save(_contract("raw.payments"), tmp_path)
    store.approve(tmp_path, ["raw.orders"])
    loaded = store.load_contracts(tmp_path)
    assert loaded["raw.orders"].status is ContractStatus.APPROVED
    assert loaded["raw.payments"].status is ContractStatus.DRAFT
