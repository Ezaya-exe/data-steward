"""Rename candidates: which new column probably replaced a missing contract one?

Why: F1 (`column_rename`) is not provable from names alone. The deterministic
ranking combines three independent signals (ARCHITECTURE §10.3):

* name similarity — `difflib.SequenceMatcher` between the missing and the new
  column name;
* type match — the share of new values that parse as the missing column's
  contract logical type, using the SAME parser as detection (`parsing.py`);
* value overlap — the share of new values also present in the missing column's
  values in the previous batch (order ids/customer ids survive a rename).

Weights (0.45/0.35/0.20) are logged in DECISIONS.md. The tool is evidence for
the model, never a verdict: the investigator still has to justify the rename.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import Engine, text

from dqs.contracts.parsing import INFERENCE_THRESHOLD, parse_rate
from dqs.contracts.store import load_approved
from dqs.errors import ToolError
from dqs.models.enums import LogicalType
from dqs.tools.base import (
    SYSTEM_COLUMNS,
    ToolContext,
    data_tool,
    validate_qualified_table,
    validate_raw_table,
)
from dqs.tools.profiling import batch_header, batch_ref, batch_values

NAME_WEIGHT = 0.45
TYPE_WEIGHT = 0.35
OVERLAP_WEIGHT = 0.20


def _text_values(values: Sequence[Any]) -> list[str | None]:
    return [None if value is None else str(value) for value in values]


def rank_rename_candidates(
    missing_column: str,
    logical_type: LogicalType,
    header: Sequence[str],
    contract_columns: Sequence[str],
    values_by_column: Mapping[str, Sequence[Any]],
    expected_values: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    """Rank header columns not in the contract for one missing column (pure).

    Deterministic: ties break on the candidate name. Empty candidate columns
    are not given a free type-match point (no values means no evidence).
    """
    contract_set = set(contract_columns)
    expected = {str(value) for value in expected_values if value is not None}
    candidates: list[dict[str, Any]] = []
    for candidate in sorted(set(header) - contract_set - SYSTEM_COLUMNS):
        name_similarity = SequenceMatcher(None, missing_column.lower(), candidate.lower()).ratio()
        values = _text_values(values_by_column.get(candidate, ()))
        rate, non_null = parse_rate(values, logical_type)
        type_match = 1.0 if non_null > 0 and rate >= INFERENCE_THRESHOLD else 0.0
        observed = {value for value in values if value is not None}
        overlap = len(observed & expected) / len(observed) if observed and expected else 0.0
        score = NAME_WEIGHT * name_similarity + TYPE_WEIGHT * type_match + OVERLAP_WEIGHT * overlap
        candidates.append(
            {
                "missing_column": missing_column,
                "candidate": candidate,
                "score": round(score, 6),
                "name_similarity": round(name_similarity, 6),
                "type_match": type_match,
                "value_overlap": round(overlap, 6),
            }
        )
    return sorted(
        candidates,
        key=lambda item: (-float(item["score"]), str(item["candidate"])),
    )


def _previous_batch(engine: Engine, batch_id: str) -> str | None:
    current = batch_ref(engine, batch_id)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT batch_id FROM dq.batches WHERE business_date < :d "
                "ORDER BY business_date DESC, batch_id DESC LIMIT 1"
            ),
            {"d": current.business_date},
        ).fetchone()
    return None if row is None else str(row[0])


@data_tool
def find_rename_candidates(ctx: ToolContext, table: str, batch_id: str) -> dict[str, Any]:
    """Rank new columns for every contract column missing from the batch header."""
    schema, name = validate_raw_table(table)
    qualified = validate_qualified_table(table, schemas=frozenset({"raw"}))
    contract = load_approved(ctx.contracts_dir).get(qualified)
    if contract is None:
        raise ToolError(f"no approved contract for {qualified}")
    header = batch_header(ctx.engine, schema, name, batch_id)
    if header is None:
        return {
            "table": qualified,
            "batch_id": batch_id,
            "present": False,
            "missing_columns": [],
            "candidates": [],
        }
    contract_columns = list(contract.columns)
    missing = [column for column in contract_columns if column not in header]
    new_columns = [column for column in header if column not in contract.columns]
    if not missing or not new_columns:
        return {
            "table": qualified,
            "batch_id": batch_id,
            "present": True,
            "missing_columns": missing,
            "new_columns": new_columns,
            "candidates": [],
        }
    current_values = batch_values(ctx.engine, table, batch_id, new_columns)
    previous_id = _previous_batch(ctx.engine, batch_id)
    previous_values: dict[str, list[Any]] = {}
    if previous_id is not None:
        previous_values = batch_values(ctx.engine, table, previous_id, missing)
    candidates: list[dict[str, Any]] = []
    for missing_column in missing:
        spec = contract.columns[missing_column]
        candidates.extend(
            rank_rename_candidates(
                missing_column,
                spec.logical_type,
                header,
                contract_columns,
                current_values,
                previous_values.get(missing_column, ()),
            )
        )
    return {
        "table": qualified,
        "batch_id": batch_id,
        "present": True,
        "missing_columns": missing,
        "new_columns": new_columns,
        "previous_batch": previous_id,
        "candidates": candidates,
    }
