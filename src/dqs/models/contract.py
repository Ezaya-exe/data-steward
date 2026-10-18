"""Data-contract models (docs/ARCHITECTURE.md §7.1) — the YAML schema as Pydantic.

Why here: contracts are the rules the check engine (Phase 2b) enforces and the
file format the store reads/writes. Strictness is a design requirement
("unknown keys rejected"): typos like `nullabel:` must fail loudly instead of
silently disabling a check, so every model sets ``extra="forbid"``.
"""

from __future__ import annotations

import datetime as dt
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from dqs.models.enums import ContractStatus, Layer, LogicalType

TABLE_NAME_RE: str = r"^[a-z_][a-z0-9_]*$"


class _Strict(BaseModel):
    """Every incoming field must be declared; unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")


class Baseline(_Strict):
    """The profiling window the contract was derived from (§7.1)."""

    batches: int
    first: str  # business date, YYYY-MM-DD
    last: str
    rows: int  # total rows profiled across all baseline batches

    @field_validator("first", "last")
    @classmethod
    def _is_date(cls, value: str) -> str:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            raise ValueError("baseline date must be YYYY-MM-DD")
        dt.date.fromisoformat(value)  # calendar validity (raises on 2026-02-30)
        return value


class ColumnReference(_Strict):
    """Parent table/column of an inferred FK (§7.2 `references`).

    Both parts end up inside a SQL anti-join identifier position, so both are
    validated with the same identifier regex the loader uses (not just trust).
    """

    table: str  # fully qualified, e.g. "raw.customers"
    column: str

    @field_validator("table")
    @classmethod
    def _qualified(cls, value: str) -> str:
        schema, _, name = value.partition(".")
        if not name or not re.match(TABLE_NAME_RE, schema) or not re.match(TABLE_NAME_RE, name):
            raise ValueError(f"reference table must be 'schema.table', got {value!r}")
        return value

    @field_validator("column")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not re.match(TABLE_NAME_RE, value):
            raise ValueError(f"reference column is not a valid identifier: {value!r}")
        return value


class NumericRange(_Strict):
    """Accepted min/max of a value or a batch mean (§7.2)."""

    min: float
    max: float


class ColumnSpec(_Strict):
    """One column's contract (§7.1)."""

    logical_type: LogicalType
    nullable: bool = True
    max_null_rate: float | None = None  # only when nullable
    unique: bool = False
    references: ColumnReference | None = None
    allowed_values: list[str] | None = None
    mean_bounds: NumericRange | None = None
    range: NumericRange | None = None

    @field_validator("max_null_rate")
    @classmethod
    def _rate_in_unit_interval(cls, value: float | None) -> float | None:
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError("max_null_rate must be within [0, 1]")
        return value


class Volume(_Strict):
    """Row-count window (§7.2), with the kind of alert it should raise.

    For raw tables `required_in_batch` means the table's header must appear in
    every batch (F9 whole-header absence is then a violation even at 0 rows);
    the generator derives it from how often the table appeared in the baseline.
    """

    min_rows: int
    max_rows: int
    required_in_batch: bool


class Freshness(_Strict):
    """SLA added to `business_date + 1 day` (§7.3 freshness.sla)."""

    max_delay_minutes: int


class Contract(_Strict):
    """One data contract = one YAML file in `contracts/` (§7.1)."""

    contract_version: int = 1
    status: ContractStatus = ContractStatus.DRAFT
    table: str  # fully qualified, e.g. "raw.orders"
    layer: Layer
    generated_at: dt.datetime
    baseline: Baseline
    primary_key: list[str] = Field(default_factory=list)
    columns: dict[str, ColumnSpec]
    volume: Volume
    freshness: Freshness

    @field_validator("table")
    @classmethod
    def _qualified(cls, value: str) -> str:
        try:
            schema, name = value.split(".", maxsplit=1)
        except ValueError as exc:
            raise ValueError(f"contract table must be 'schema.table', got {value!r}") from exc
        for part in (schema, name):
            if not re.match(TABLE_NAME_RE, part):
                raise ValueError(f"invalid table identifier part {part!r}")
        return value

    @field_validator("generated_at")
    @classmethod
    def _utc(cls, value: dt.datetime) -> dt.datetime:
        return value.replace(tzinfo=dt.UTC)
