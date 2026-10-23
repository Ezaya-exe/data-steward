"""The benchmark scenario catalogue (Phase 8a, `ARCHITECTURE.md` §14).

Why a code catalogue: the same seed + date + spec always produces an identical
mutation, so a scenario is a stable, reviewable record — not a mutable database
row. The ground truth is *derived* by applying the spec to the clean day-31
batch with the real injector, so the expected check/category/resolution can
never drift from the mutation.

Split integrity: ~30% of scenarios are `holdout`, all of F7 (`scale_shift`) and
F9 (`missing_table`), plus one variant of every other kind. `dev` is the only
split used while prompts/tools are being developed. Anything not in `dev` must
not influence prompt wording.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Final

import pandas as pd
from pydantic import BaseModel, ConfigDict

from dqs.models.enums import FaultKind
from dqs.models.faults import GroundTruth
from dqs.sim import faults as faults_mod
from dqs.sim import generator
from dqs.sim.faults import parse_fault_spec

FAULT_DATE: Final = dt.date(2026, 1, 31)
SEED: Final = 42
SPLIT_DEV: Final = "dev"
SPLIT_HOLDOUT: Final = "holdout"


class Split(StrEnum):
    """Which half of the catalogue a scenario belongs to."""

    DEV = "dev"
    HOLDOUT = "holdout"


class Scenario(BaseModel):
    """One benchmark case: a fault spec (or clean control) and its answer key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    split: Split
    table: str | None
    fault_spec: str | None = None
    expected: GroundTruth | None = None
    note: str = ""

    @property
    def is_control(self) -> bool:
        return self.fault_spec is None

    @property
    def kind(self) -> str:
        return self.expected.kind.value if self.expected is not None else "clean"

    @property
    def acceptable_columns(self) -> list[str]:
        return [] if self.expected is None else list(self.expected.acceptable_columns)


# id -> (spec, split, acceptable columns). The split is explicit per row so a
# reviewer can see exactly which cases are held out.
_FAULT_ROWS: Final[tuple[tuple[str, str, Split, list[str]], ...]] = (
    # F1 column_rename
    (
        "f1-orders-customer-id",
        "column_rename:orders.customer_id:new_name=cust_id",
        Split.HOLDOUT,
        ["customer_id", "cust_id"],
    ),
    (
        "f1-orders-status",
        "column_rename:orders.status:new_name=order_status",
        Split.DEV,
        ["status", "order_status"],
    ),
    (
        "f1-payments-method",
        "column_rename:payments.method:new_name=payment_method",
        Split.DEV,
        ["method", "payment_method"],
    ),
    (
        "f1-customers-email",
        "column_rename:customers.email:new_name=email_address",
        Split.DEV,
        ["email", "email_address"],
    ),
    (
        "f1-order-items-quantity",
        "column_rename:order_items.quantity:new_name=qty",
        Split.DEV,
        ["quantity", "qty"],
    ),
    # F2 column_removed
    ("f2-orders-status", "column_removed:orders.status", Split.HOLDOUT, ["status"]),
    ("f2-customers-email", "column_removed:customers.email", Split.DEV, ["email"]),
    ("f2-payments-amount", "column_removed:payments.amount", Split.DEV, ["amount"]),
    (
        "f2-order-items-quantity",
        "column_removed:order_items.quantity",
        Split.DEV,
        ["quantity"],
    ),
    (
        "f2-order-items-unit-price",
        "column_removed:order_items.unit_price",
        Split.DEV,
        ["unit_price"],
    ),
    # F3 type_change
    (
        "f3-orders-total-amount",
        "type_change:orders.total_amount:fraction=1.0",
        Split.HOLDOUT,
        ["total_amount"],
    ),
    (
        "f3-payments-amount",
        "type_change:payments.amount:fraction=0.8",
        Split.DEV,
        ["amount"],
    ),
    (
        "f3-order-items-unit-price",
        "type_change:order_items.unit_price:fraction=0.5",
        Split.DEV,
        ["unit_price"],
    ),
    (
        "f3-order-items-quantity",
        "type_change:order_items.quantity:fraction=1.0",
        Split.DEV,
        ["quantity"],
    ),
    # F4 null_spike (20/35/60%)
    ("f4-orders-status-20", "null_spike:orders.status:fraction=0.2", Split.DEV, ["status"]),
    (
        "f4-orders-status-35",
        "null_spike:orders.status:fraction=0.35",
        Split.HOLDOUT,
        ["status"],
    ),
    (
        "f4-customers-email-60",
        "null_spike:customers.email:fraction=0.6",
        Split.DEV,
        ["email"],
    ),
    ("f4-customers-state-35", "null_spike:customers.state:fraction=0.35", Split.DEV, ["state"]),
    ("f4-payments-method-20", "null_spike:payments.method:fraction=0.2", Split.DEV, ["method"]),
    (
        "f4-order-items-quantity-60",
        "null_spike:order_items.quantity:fraction=0.6",
        Split.DEV,
        ["quantity"],
    ),
    (
        "f4-orders-customer-20",
        "null_spike:orders.customer_id:fraction=0.2",
        Split.DEV,
        ["customer_id"],
    ),
    (
        "f4-order-items-unit-price-35",
        "null_spike:order_items.unit_price:fraction=0.35",
        Split.DEV,
        ["unit_price"],
    ),
    # F5 orphan_keys (only the documented FK pairs)
    (
        "f5-orders-customer-35",
        "orphan_keys:orders.customer_id:fraction=0.35",
        Split.DEV,
        ["customer_id"],
    ),
    (
        "f5-orders-customer-15",
        "orphan_keys:orders.customer_id:fraction=0.15",
        Split.HOLDOUT,
        ["customer_id"],
    ),
    (
        "f5-order-items-order-35",
        "orphan_keys:order_items.order_id:fraction=0.35",
        Split.DEV,
        ["order_id"],
    ),
    (
        "f5-payments-order-20",
        "orphan_keys:payments.order_id:fraction=0.2",
        Split.DEV,
        ["order_id"],
    ),
    (
        "f5-payments-order-35",
        "orphan_keys:payments.order_id:fraction=0.35",
        Split.DEV,
        ["order_id"],
    ),
    (
        "f5-order-items-product-35",
        "orphan_keys:order_items.product_id:fraction=0.35",
        Split.DEV,
        ["product_id"],
    ),
    # F6 duplicate_rows (3/8/15%)
    ("f6-orders-03", "duplicate_rows:orders:fraction=0.03", Split.DEV, []),
    ("f6-orders-08", "duplicate_rows:orders:fraction=0.08", Split.HOLDOUT, []),
    ("f6-order-items-05", "duplicate_rows:order_items:fraction=0.05", Split.DEV, []),
    ("f6-order-items-03", "duplicate_rows:order_items:fraction=0.03", Split.DEV, []),
    ("f6-payments-15", "duplicate_rows:payments:fraction=0.15", Split.DEV, []),
    ("f6-customers-08", "duplicate_rows:customers:fraction=0.08", Split.DEV, []),
    ("f6-order-items-15", "duplicate_rows:order_items:fraction=0.15", Split.DEV, []),
    # F7 scale_shift (x100 / x1000) — every F7 case is holdout
    (
        "f7-orders-total-100",
        "scale_shift:orders.total_amount:factor=100",
        Split.HOLDOUT,
        ["total_amount"],
    ),
    (
        "f7-orders-total-1000",
        "scale_shift:orders.total_amount:factor=1000",
        Split.HOLDOUT,
        ["total_amount"],
    ),
    (
        "f7-payments-amount-100",
        "scale_shift:payments.amount:factor=100",
        Split.HOLDOUT,
        ["amount"],
    ),
    (
        "f7-order-items-unit-price-1000",
        "scale_shift:order_items.unit_price:factor=1000",
        Split.HOLDOUT,
        ["unit_price"],
    ),
    # F8 late_batch (table-scoped freshness)
    ("f8-orders-24", "late_batch:orders:hours=24", Split.HOLDOUT, []),
    ("f8-customers-30", "late_batch:customers:hours=30", Split.DEV, []),
    ("f8-payments-48", "late_batch:payments:hours=48", Split.DEV, []),
    ("f8-order-items-24", "late_batch:order_items:hours=24", Split.DEV, []),
    ("f8-order-items-30", "late_batch:order_items:hours=30", Split.DEV, []),
    # F9 missing_table — every F9 case is holdout
    ("f9-customers", "missing_table:customers", Split.HOLDOUT, []),
    ("f9-orders", "missing_table:orders", Split.HOLDOUT, []),
    ("f9-order-items", "missing_table:order_items", Split.HOLDOUT, []),
    ("f9-payments", "missing_table:payments", Split.HOLDOUT, []),
    # F10 column_added
    (
        "f10-orders-coupon",
        "column_added:orders:new_column=coupon_code",
        Split.HOLDOUT,
        ["coupon_code"],
    ),
    (
        "f10-customers-note",
        "column_added:customers:new_column=extra_note",
        Split.DEV,
        ["extra_note"],
    ),
    (
        "f10-payments-campaign",
        "column_added:payments:new_column=campaign_id",
        Split.DEV,
        ["campaign_id"],
    ),
    (
        "f10-order-items-note",
        "column_added:order_items:new_column=extra_note",
        Split.DEV,
        ["extra_note"],
    ),
    (
        "f10-orders-source",
        "column_added:orders:new_column=source_system",
        Split.DEV,
        ["source_system"],
    ),
)

_CONTROL_IDS: Final[tuple[str, ...]] = tuple(f"clean-{index:02d}" for index in range(1, 7))


def _base_batch() -> dict[str, pd.DataFrame]:
    """The clean day-31 frames, generated once (pure; never mutated by injectors)."""
    return dict(generator.tables_for_date(FAULT_DATE, SEED))


def _fault_scenario(
    scenario_id: str,
    spec_text: str,
    split: Split,
    acceptable_columns: list[str],
    base: dict[str, pd.DataFrame],
) -> Scenario:
    spec = parse_fault_spec(spec_text)
    faulted = faults_mod.apply_faults(base, [spec], FAULT_DATE, SEED)
    truth = faulted.ground_truth[0].model_copy(
        update={"acceptable_columns": list(acceptable_columns)}
    )
    return Scenario(
        id=scenario_id,
        split=split,
        table=spec.table,
        fault_spec=spec_text,
        expected=truth,
    )


def _build() -> tuple[Scenario, ...]:
    base = _base_batch()
    faults = tuple(
        _fault_scenario(scenario_id, spec, split, columns, base)
        for scenario_id, spec, split, columns in _FAULT_ROWS
    )
    controls = tuple(
        Scenario(id=scenario_id, split=Split.DEV, table=None) for scenario_id in _CONTROL_IDS
    )
    return faults + controls


SCENARIOS: Final[tuple[Scenario, ...]] = _build()
SCENARIO_IDS: Final[tuple[str, ...]] = tuple(scenario.id for scenario in SCENARIOS)


def scenarios_for(split: str = "all") -> list[Scenario]:
    """The catalogue, optionally filtered to `dev` or `holdout` (stable order)."""
    value = split.strip().lower()
    if value == "all":
        return list(SCENARIOS)
    if value not in {SPLIT_DEV, SPLIT_HOLDOUT}:
        raise ValueError(f"split must be 'dev', 'holdout' or 'all' (got {split!r})")
    return [scenario for scenario in SCENARIOS if scenario.split.value == value]


def get_scenario(scenario_id: str) -> Scenario:
    """Look up one scenario by its stable id."""
    for scenario in SCENARIOS:
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(scenario_id)


def catalogue_problems() -> list[str]:
    """Structural checks for the §14 acceptance criteria (empty means valid)."""
    problems: list[str] = []
    faults = [scenario for scenario in SCENARIOS if not scenario.is_control]
    controls = [scenario for scenario in SCENARIOS if scenario.is_control]
    if len(faults) < 40:
        problems.append(f"only {len(faults)} fault scenarios (need >= 40)")
    if len(controls) < 6:
        problems.append(f"only {len(controls)} clean controls (need >= 6)")
    if len({scenario.id for scenario in SCENARIOS}) != len(SCENARIOS):
        problems.append("scenario ids are not unique")
    kinds = {scenario.kind for scenario in faults}
    expected_kinds = {kind.value for kind in FaultKind}
    if kinds != expected_kinds:
        problems.append(f"fault kinds {sorted(kinds)} != {sorted(expected_kinds)}")
    for scenario in SCENARIOS:
        if len(scenario.id) > 80 or not scenario.id.replace("-", "").isalnum():
            problems.append(f"unstable/unsafe id {scenario.id!r}")
        if scenario.is_control:
            if scenario.expected is not None:
                problems.append(f"control {scenario.id} carries a ground truth")
            continue
        expected = scenario.expected
        if expected is None or not expected.expected_checks:
            problems.append(f"{scenario.id} has no expected checks")
    holdout = [scenario for scenario in faults if scenario.split is Split.HOLDOUT]
    holdout_kinds = {scenario.kind for scenario in holdout}
    for kind in ("scale_shift", "missing_table"):
        if kind not in holdout_kinds:
            problems.append(f"holdout does not contain every {kind} scenario")
        if any(s.kind == kind and s.split is not Split.HOLDOUT for s in faults):
            problems.append(f"holdout must contain ALL {kind} scenarios")
    for kind in expected_kinds - {"scale_shift", "missing_table"}:
        if kind not in holdout_kinds:
            problems.append(f"holdout has no variant of {kind}")
    share = len(holdout) / max(1, len(faults))
    if not 0.2 <= share <= 0.4:
        problems.append(f"holdout share {share:.2f} is outside ~30% (20-40%)")
    return problems


__all__ = [
    "FAULT_DATE",
    "SCENARIO_IDS",
    "SCENARIOS",
    "SEED",
    "SPLIT_DEV",
    "SPLIT_HOLDOUT",
    "Scenario",
    "Split",
    "catalogue_problems",
    "get_scenario",
    "scenarios_for",
]
