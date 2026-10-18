"""Fault injectors F1–F10 with the spec-string parser (docs/ARCHITECTURE.md §6).

Why here: the benchmark must replay every fault exactly, so injecting is a set
of PURE functions over the generated batch frames — never touching the
generator, the DB, or the loader. Two exceptions exist by design:

- F8 (late_batch) returns an `arrived_at` override instead of a frame mutation;
  the loader records it (and it alone) in `dq.batches`.
- F9 (missing_table) removes a whole table from the batch dict — absence IS
  the fault.

Every kind is registered in `_INJECTORS`; a completeness test asserts the
registry covers exactly `FaultKind`, so a future fault kind cannot parse
successfully yet fail at application time.

Each injection returns a `GroundTruth` record (what should fire when detection
runs later) so benchmark scenarios can never drift from the actual mutation.

Spec format: `kind:table[.column][:k=v,k=v]`, e.g.
`column_rename:orders.customer_id:new_name=cust_id`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, SupportsFloat

import numpy as np
import pandas as pd

from dqs.errors import FaultSpecError
from dqs.models.enums import FaultKind, ResolutionType, RootCauseCategory
from dqs.models.faults import FaultSpec, GroundTruth
from dqs.sim import generator
from dqs.sim.loader import IDENTIFIER_RE, arrived_at_for

RAW_TABLES: Final = frozenset({"customers", "products", "orders", "order_items", "payments"})

# FK pairs that orphan_keys (F5) supports, mapping child column -> parent column.
# The injector generates orphan ids strictly ABOVE the parent's cumulative
# maximum, so the FK violation is structural, not probabilistic; any spec
# outside these pairs is rejected at PARSE time with a clear message.
FK_MAP: Final[dict[str, str]] = {
    "orders.customer_id": "raw.customers.customer_id",
    "order_items.order_id": "raw.orders.order_id",
    "payments.order_id": "raw.orders.order_id",
    "order_items.product_id": "raw.products.product_id",
}

# Bounds of the parent domains above, i.e. how far a "provably nonexistent" id
# must be pushed: every possible parent id of any batch up to ``business_date``
# is below its pair's bound (customers grow contiguously; the others use the
# per-date id slots, so the NEXT slot start is already beyond every parent).
_FK_BOUNDS: Final[dict[str, Callable[[int, dt.date], int]]] = {
    "orders.customer_id": generator.cumulative_customers,
    "order_items.order_id": lambda _seed, date: (
        generator.ORDER_ID_SLOT * ((date - generator.BASELINE_START_DATE).days + 1)
    ),
    "payments.order_id": lambda _seed, date: (
        generator.ORDER_ID_SLOT * ((date - generator.BASELINE_START_DATE).days + 1)
    ),
    "order_items.product_id": lambda _seed, _date: generator.INITIAL_PRODUCT_ROWS,
}

# Per-batch primary keys — F4 (null_spike) must target NON-key columns (§6)
_PRIMARY_KEYS: Final[dict[str, str]] = {
    "customers": "customer_id",
    "products": "product_id",
    "orders": "order_id",
    "order_items": "order_item_id",
    "payments": "payment_id",
}

Injection = Callable[
    [dict[str, pd.DataFrame], FaultSpec, np.random.Generator, dt.date, int],
    tuple[dict[str, pd.DataFrame], dt.datetime | None, GroundTruth],
]

_INJECTORS: Final[dict[FaultKind, Injection]] = {}


def register(kind: FaultKind) -> Callable[[Injection], Injection]:
    """Module-internal decorator: attach one pure injection to its fault kind."""

    def deco(fn: Injection) -> Injection:
        _INJECTORS[kind] = fn
        return fn

    return deco


# --- parsing -----------------------------------------------------------------------


def parse_fault_spec(spec: str) -> FaultSpec:
    """Parse one fault spec string; every problem raises a precise FaultSpecError."""
    text = spec.strip()
    if not text:
        raise FaultSpecError("empty fault spec")
    parts = text.split(":", 2)
    if len(parts) < 2:
        raise FaultSpecError(
            f"invalid fault spec {spec!r}: expected 'kind:table[.column][:k=v,...]'"
        )
    kind_part, target_part = parts[0], parts[1]
    params_part = parts[2] if len(parts) == 3 else None

    try:
        kind = FaultKind(kind_part)
    except ValueError as exc:
        allowed = ", ".join(k.value for k in FaultKind)
        raise FaultSpecError(
            f"unknown fault kind {kind_part!r} in {spec!r}; expected one of: {allowed}"
        ) from exc

    table, column = _parse_target(target_part, spec)
    if not column and kind in _COLUMN_REQUIRED:
        raise FaultSpecError(f"fault kind {kind.value!r} requires 'table.column' in {spec!r}")
    if kind is FaultKind.ORPHAN_KEYS:
        _require_fk_pair(table, column, spec)

    params = _parse_params(params_part, spec)
    _validate_params(table, kind, column, params, spec)
    return FaultSpec(kind=kind, table=table, column=column, params=params)


def parse_fault_specs(specs: list[str] | None) -> list[FaultSpec]:
    """Parse a whole repeatable --fault list, in order, stopping at the first error."""
    if not specs:
        return []
    return [parse_fault_spec(spec) for spec in specs]


def _parse_target(target_part: str, spec: str) -> tuple[str, str | None]:
    pieces = target_part.split(".", 1)
    table = pieces[0]
    if not table:
        raise FaultSpecError(
            f"invalid target {target_part!r} in {spec!r}: expected 'table' or 'table.column'"
        )
    if table not in RAW_TABLES:
        raise FaultSpecError(
            f"unknown table {table!r} in {spec!r}; expected one of: {', '.join(sorted(RAW_TABLES))}"
        )
    column = pieces[1] if len(pieces) == 2 else None
    if column is not None and not IDENTIFIER_RE.match(column):
        raise FaultSpecError(
            f"invalid column name {column!r} in {spec!r}: must match {IDENTIFIER_RE.pattern}"
        )
    return table, column


def _require_fk_pair(table: str, column: str | None, spec: str) -> None:
    """orphan_keys needs a KNOWN parent domain at parse time, not at inject time."""
    pair = f"{table}.{column}"
    if pair not in FK_MAP:
        raise FaultSpecError(
            f"orphan_keys is not supported for {pair!r} in {spec!r}: no known parent domain "
            f"(known pairs: {', '.join(sorted(FK_MAP))})"
        )


def _parse_params(params_part: str | None, spec: str) -> dict[str, str | int | float]:
    if params_part is None:
        return {}
    if params_part == "":
        # 'kind:table:' is always a spec error: the colon announces parameters
        # that never arrive (catches copy-paste truncation instead of silently
        # running the fault with all defaults)
        raise FaultSpecError(f"empty parameter list after ':' in {spec!r}")
    params: dict[str, str | int | float] = {}
    for pair in params_part.split(","):
        if "=" not in pair:
            raise FaultSpecError(
                f"malformed parameter {pair!r} in {spec!r}: expected k=v pairs separated by ','"
            )
        key, value = pair.split("=", 1)
        if not re.match(r"^[a-z_][a-z0-9_]*$", key):
            raise FaultSpecError(f"invalid parameter name {key!r} in {spec!r}")
        params[key] = _coerce_scalar(value)
    return params


def _coerce_scalar(value: str) -> str | int | float:
    """Ints and decimal floats become numbers, everything else stays text."""
    if re.match(r"^[+-]?\d+$", value):
        return int(value)
    if re.match(r"^[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?$", value):
        return float(value)
    return value


@dataclass(frozen=True)
class _ParamRule:
    """One declared fault parameter: type + optional default; absent means required."""

    name: str
    py_type: type  # int, float or str
    default: Any = None
    unit_interval: bool = False  # a fraction within (0, 1], when present


_PARAM_RULES: Final[dict[FaultKind, list[_ParamRule]]] = {
    FaultKind.COLUMN_RENAME: [_ParamRule("new_name", str)],
    FaultKind.COLUMN_REMOVED: [],
    FaultKind.TYPE_CHANGE: [_ParamRule("fraction", float, 1.0, unit_interval=True)],
    FaultKind.NULL_SPIKE: [_ParamRule("fraction", float, 0.35, unit_interval=True)],
    FaultKind.ORPHAN_KEYS: [_ParamRule("fraction", float, 0.35, unit_interval=True)],
    FaultKind.DUPLICATE_ROWS: [_ParamRule("fraction", float, 0.05, unit_interval=True)],
    FaultKind.SCALE_SHIFT: [_ParamRule("factor", float, 100.0)],
    FaultKind.LATE_BATCH: [_ParamRule("hours", int, 24)],
    FaultKind.MISSING_TABLE: [],
    # F10 adds one text column; the(VALUE-free default name depends on the table
    # and the coupon-like values (with nulls) are generated by the injector.
    FaultKind.COLUMN_ADDED: [_ParamRule("new_column", str)],
}

_COLUMN_REQUIRED: Final[frozenset[FaultKind]] = frozenset(
    {
        FaultKind.COLUMN_RENAME,
        FaultKind.COLUMN_REMOVED,
        FaultKind.TYPE_CHANGE,
        FaultKind.NULL_SPIKE,
        FaultKind.ORPHAN_KEYS,
        FaultKind.SCALE_SHIFT,
    }
)


def _validate_params(
    table: str,
    kind: FaultKind,
    column: str | None,
    params: dict[str, str | int | float],
    spec: str,
) -> None:
    """One central validation pass with the spec string in every error."""
    allowed_names = {rule.name for rule in _PARAM_RULES[kind]}
    unknown = set(params) - allowed_names
    if unknown:
        raise FaultSpecError(
            f"unknown parameter {sorted(unknown)[0]!r} for {kind.value!r} in {spec!r}; "
            f"expected: {', '.join(sorted(allowed_names)) or '(none)'}"
        )
    for rule in _PARAM_RULES[kind]:
        value = params.get(rule.name)
        if value is None:
            continue  # absent: the injector applies the documented default
        if rule.py_type is str:
            if not isinstance(value, str) or not IDENTIFIER_RE.match(value):
                raise FaultSpecError(
                    f"parameter {rule.name!r} for {kind.value!r} in {spec!r} must be an identifier"
                )
            continue
        if isinstance(value, str):
            raise FaultSpecError(
                f"parameter {rule.name!r} for {kind.value!r} in {spec!r} must be a number"
            )
        numeric = float(value)
        if rule.py_type is int and not float(value).is_integer():
            raise FaultSpecError(
                f"parameter {rule.name!r} for {kind.value!r} in {spec!r} must be an integer"
            )
        if rule.unit_interval and not 0.0 < numeric <= 1.0:
            raise FaultSpecError(
                f"parameter {rule.name!r} for {kind.value!r} in {spec!r} "
                "must be a fraction within (0, 1]"
            )
    if kind is FaultKind.NULL_SPIKE and column is not None and _PRIMARY_KEYS.get(table) == column:
        raise FaultSpecError(
            f"null_spike targets a non-key column, but {table}.{column} is that table's "
            f"primary key in {spec!r}"
        )


def _default(kind: FaultKind, name: str) -> Any:
    return next(rule.default for rule in _PARAM_RULES[kind] if rule.name == name)


# --- application -------------------------------------------------------------------


class FaultedBatch:
    """Result of applying fault specs to one generated batch."""

    __slots__ = ("tables", "arrived_at", "ground_truth")

    def __init__(
        self,
        tables: dict[str, pd.DataFrame],
        arrived_at: dt.datetime | None,
        ground_truth: list[GroundTruth],
    ) -> None:
        self.tables = tables
        self.arrived_at = arrived_at
        self.ground_truth = ground_truth


def apply_faults(
    tables: dict[str, pd.DataFrame],
    specs: list[FaultSpec],
    business_date: dt.date,
    seed: int,
) -> FaultedBatch:
    """Apply specs in order to a COPY of the clean batch.

    One RNG per fault, seeded ``[seed, date.toordinal(), 900 + order]``: the same
    seed + date + specs always inject an identical mutation, and one fault's
    draws never shift another fault's draws. F8 may override the arrival time;
    a later arrival-fault win is fine (the loader sees the last one).
    """
    mutable: dict[str, pd.DataFrame] = dict(tables)
    arrived_at: dt.datetime | None = None
    truth: list[GroundTruth] = []
    for order, spec in enumerate(specs):
        rng = np.random.default_rng([seed, business_date.toordinal(), 900 + order])
        if spec.table not in mutable:
            raise FaultSpecError(
                f"cannot inject {spec.format()}: table {spec.table!r} is not in the batch "
                "(a previous fault may have removed it)"
            )
        mutable, fault_arrival, record = _INJECTORS[spec.kind](
            mutable, spec, rng, business_date, seed
        )
        if fault_arrival is not None:
            arrived_at = fault_arrival
        truth.append(record)
    return FaultedBatch(mutable, arrived_at, truth)


# --- injections (all pure; the generator frames are never mutated) ------------------
# Each injection takes its RNG and uses it ONLY for choosing affected rows, so
# "which rows are hit" is replayable, and the values written are deterministic.
# Uniform signature: returns (frames, arrival override or None, ground truth).


@register(FaultKind.COLUMN_RENAME)
def _column_rename(
    tables: dict[str, pd.DataFrame],
    spec: FaultSpec,
    rng: np.random.Generator,
    business_date: dt.date,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], dt.datetime | None, GroundTruth]:
    """F1: rename one column of the batch header (schema drift)."""
    new_name = str(spec.params.get("new_name", f"x_{spec.column}"))
    frame = tables[spec.table].copy()
    frame = frame.rename(columns={spec.column: new_name})
    truth = GroundTruth(
        kind=spec.kind,
        table=spec.table,
        column=spec.column,
        parameters={"new_name": new_name},
        expected_checks=["schema.missing_column", "schema.unexpected_column"],
        expected_category=RootCauseCategory.SOURCE_COLUMN_RENAMED,
        expected_resolution=ResolutionType.PR,
        fix_shape="staging: coalesce(original, renamed) then drop the renamed column",
    )
    return {**tables, spec.table: frame}, None, truth


@register(FaultKind.COLUMN_REMOVED)
def _column_removed(
    tables: dict[str, pd.DataFrame],
    spec: FaultSpec,
    rng: np.random.Generator,
    business_date: dt.date,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], dt.datetime | None, GroundTruth]:
    """F2: drop a column from the batch header entirely."""
    frame = tables[spec.table].drop(columns=[spec.column])
    truth = GroundTruth(
        kind=spec.kind,
        table=spec.table,
        column=spec.column,
        expected_checks=["schema.missing_column"],
        expected_category=RootCauseCategory.SOURCE_COLUMN_REMOVED,
        expected_resolution=ResolutionType.ISSUE,
    )
    return {**tables, spec.table: frame}, None, truth


@register(FaultKind.TYPE_CHANGE)
def _type_change(
    tables: dict[str, pd.DataFrame],
    spec: FaultSpec,
    rng: np.random.Generator,
    business_date: dt.date,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], dt.datetime | None, GroundTruth]:
    """F3: numeric column becomes currency text like ``₹1,299.50`` (default: all rows)."""
    fraction = _fraction(spec, 1.0)
    frame = tables[spec.table].copy()
    values = frame[spec.column].to_numpy()
    hit = set(_sample_indices(len(values), _target_index_count(len(values), fraction), rng))
    formatted = [
        _format_currency(value, index) if index in hit else str(value)
        for index, value in enumerate(values)
    ]
    frame[spec.column] = pd.Series(formatted, dtype=object)
    truth = GroundTruth(
        kind=spec.kind,
        table=spec.table,
        column=spec.column,
        parameters={"fraction": fraction},
        expected_checks=["type.logical"],
        expected_category=RootCauseCategory.SOURCE_TYPE_CHANGED,
        expected_resolution=ResolutionType.PR,
        fix_shape="staging: regexp currency symbols/separators out before the cast",
    )
    return {**tables, spec.table: frame}, None, truth


@register(FaultKind.NULL_SPIKE)
def _null_spike(
    tables: dict[str, pd.DataFrame],
    spec: FaultSpec,
    rng: np.random.Generator,
    business_date: dt.date,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], dt.datetime | None, GroundTruth]:
    """F4: set X% (default 35%) of a non-key column to NULL."""
    fraction = _fraction(spec, 0.35)
    frame = tables[spec.table].copy()
    target = _sample_indices(len(frame), _target_index_count(len(frame), fraction), rng)
    nulls = frame[spec.column].astype(object)
    if target:
        nulls.loc[target] = None
    frame[spec.column] = nulls
    truth = GroundTruth(
        kind=spec.kind,
        table=spec.table,
        column=spec.column,
        parameters={"fraction": fraction},
        expected_checks=["nulls.rate"],
        expected_category=RootCauseCategory.UPSTREAM_NULLS_INTRODUCED,
        expected_resolution=ResolutionType.ISSUE,
    )
    return {**tables, spec.table: frame}, None, truth


@register(FaultKind.ORPHAN_KEYS)
def _orphan_keys(
    tables: dict[str, pd.DataFrame],
    spec: FaultSpec,
    rng: np.random.Generator,
    business_date: dt.date,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], dt.datetime | None, GroundTruth]:
    """F5: point X% of FK values at ids PROVABLY above the parent's cumulative max.

    The parent bound is known in closed form (see ``_FK_BOUNDS``): the first
    id after every id any parent batch up to ``business_date`` could contain.
    """
    fraction = _fraction(spec, 0.35)
    upper_bound = _FK_BOUNDS[f"{spec.table}.{spec.column}"](seed, business_date)
    frame = tables[spec.table].copy()
    target = _sample_indices(len(frame), _target_index_count(len(frame), fraction), rng)
    if target:
        orphans = frame[spec.column].astype(object)
        orphans.loc[target] = [int(upper_bound) + 1 + i for i in range(len(target))]
        frame[spec.column] = orphans
    truth = GroundTruth(
        kind=spec.kind,
        table=spec.table,
        column=spec.column,
        parameters={"fraction": fraction},
        expected_checks=["fk.integrity"],
        expected_category=RootCauseCategory.REFERENTIAL_INTEGRITY_BROKEN,
        expected_resolution=ResolutionType.ISSUE,
    )
    return {**tables, spec.table: frame}, None, truth


@register(FaultKind.DUPLICATE_ROWS)
def _duplicate_rows(
    tables: dict[str, pd.DataFrame],
    spec: FaultSpec,
    rng: np.random.Generator,
    business_date: dt.date,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], dt.datetime | None, GroundTruth]:
    """F6: replay X% of rows verbatim (same PK) as extra rows."""
    fraction = _fraction(spec, 0.05)
    frame = tables[spec.table]
    target = _sample_indices(len(frame), _target_index_count(len(frame), fraction), rng)
    duplicated = pd.concat([frame, frame.iloc[target]], ignore_index=True)
    truth = GroundTruth(
        kind=spec.kind,
        table=spec.table,
        parameters={"fraction": fraction},
        expected_checks=["unique.pk"],
        expected_category=RootCauseCategory.DUPLICATE_RECORDS,
        expected_resolution=ResolutionType.PR,
        fix_shape="staging: row_number() dedupe by PK keeping the latest _loaded_at",
    )
    return {**tables, spec.table: duplicated}, None, truth


@register(FaultKind.SCALE_SHIFT)
def _scale_shift(
    tables: dict[str, pd.DataFrame],
    spec: FaultSpec,
    rng: np.random.Generator,
    business_date: dt.date,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], dt.datetime | None, GroundTruth]:
    """F7: multiply a numeric column by a factor (default ×100: cents vs rupees)."""
    factor = float(spec.params.get("factor", _default(spec.kind, "factor")))
    frame = tables[spec.table].copy()
    frame[spec.column] = frame[spec.column].astype(float) * factor
    truth = GroundTruth(
        kind=spec.kind,
        table=spec.table,
        column=spec.column,
        parameters={"factor": factor},
        expected_checks=["range.bounds"],
        expected_category=RootCauseCategory.UNIT_OR_SCALE_CHANGE,
        expected_resolution=ResolutionType.ISSUE,
    )
    return {**tables, spec.table: frame}, None, truth


@register(FaultKind.LATE_BATCH)
def _late_batch(
    tables: dict[str, pd.DataFrame],
    spec: FaultSpec,
    rng: np.random.Generator,
    business_date: dt.date,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], dt.datetime | None, GroundTruth]:
    """F8: no frame change; return an arrival time pushed beyond the SLA.

    Default push is 24h on top of the normal +1d+02:00 arrival, i.e. ~46h
    after the business date — well past the 26h SLA freshness checks use.
    """
    hours = int(spec.params.get("hours", _default(spec.kind, "hours")))
    truth = GroundTruth(
        kind=spec.kind,
        table=spec.table,
        parameters={"hours": hours},
        expected_checks=["freshness.sla"],
        expected_category=RootCauseCategory.LATE_OR_MISSING_BATCH,
        expected_resolution=ResolutionType.ISSUE,
    )
    return tables, arrived_at_for(business_date) + dt.timedelta(hours=hours), truth


@register(FaultKind.MISSING_TABLE)
def _missing_table(
    tables: dict[str, pd.DataFrame],
    spec: FaultSpec,
    rng: np.random.Generator,
    business_date: dt.date,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], dt.datetime | None, GroundTruth]:
    """F9: remove a whole table from the batch — absence IS the fault."""
    truth = GroundTruth(
        kind=spec.kind,
        table=spec.table,
        expected_checks=["volume.rows"],
        expected_category=RootCauseCategory.LATE_OR_MISSING_BATCH,
        expected_resolution=ResolutionType.ISSUE,
    )
    return {name: frame for name, frame in tables.items() if name != spec.table}, None, truth


@register(FaultKind.COLUMN_ADDED)
def _column_added(
    tables: dict[str, pd.DataFrame],
    spec: FaultSpec,
    rng: np.random.Generator,
    business_date: dt.date,
    seed: int,
) -> tuple[dict[str, pd.DataFrame], dt.datetime | None, GroundTruth]:
    """F10: one extra TEXT column (unexpected-schema drift; warning-level).

    Shaped like a real upstream change: short coupon codes for orders
    (default ``orders.coupon_code``; other tables default ``extra_note``),
    ~30% of values NULL because a new optional field is not always populated.
    """
    candidate_default = "coupon_code" if spec.table == "orders" else "extra_note"
    new_column = str(spec.params.get("new_column", candidate_default))
    frame = tables[spec.table].copy()
    count = len(frame)
    values = [
        None if draw else _COUPON_CODES[int(i) % len(_COUPON_CODES)]
        for i, draw in enumerate(rng.random(max(count, 1)) < 0.3)
    ][:count]
    frame[new_column] = pd.Series(values, dtype=object)
    truth = GroundTruth(
        kind=spec.kind,
        table=spec.table,
        column=None,  # the fault invents a column that never existed in the source
        parameters={"new_column": new_column},
        expected_checks=["schema.unexpected_column"],
        expected_category=RootCauseCategory.SOURCE_COLUMN_ADDED,
        expected_resolution=ResolutionType.NONE,
    )
    return {**tables, spec.table: frame}, None, truth


_COUPON_CODES: Final = ("SAVE10", "SAVE20", "BUNDLE15", "FREESHIP")


def _fraction(spec: FaultSpec, default: float) -> float:
    value = spec.params.get("fraction")
    return default if value is None else float(value)


def _target_index_count(row_count: int, fraction: float) -> int:
    """Number of affected rows, clamped to the table (0 rows ⇒ no-op)."""
    if row_count == 0:
        return 0
    return max(1, min(row_count, int(round(row_count * fraction))))


def _sample_indices(count: int, k: int, rng: np.random.Generator) -> list[int]:
    """Choice-without-replacement that also works when k == count."""
    if count == 0 or k >= count:
        return list(range(count))
    return [int(i) for i in rng.choice(np.arange(count), size=k, replace=False)]


_CURRENCY_PREFIXES: Final = ("₹", "$", "€")


def _format_currency(value: SupportsFloat, index: int) -> str:
    """Human-visible currency text; prefix alternates by row (deterministic)."""
    prefix = _CURRENCY_PREFIXES[index % len(_CURRENCY_PREFIXES)]
    return f"{prefix}{float(value):,.2f}"
