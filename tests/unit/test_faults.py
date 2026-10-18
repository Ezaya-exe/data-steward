"""Unit tests for F1–F10 injectors + spec parser (Phase 2a acceptance).

Focus: (a) the mutation is EXACTLY what the spec says (affected fraction,
renamed header, currency text, ×100 …), (b) determinism for the same seed,
(c) malformed specs fail with clear errors — at PARSE time, including an
empty ``:params`` suffix and unsupported FK pairs, (d) injection never
mutates the generator's frames (faults live "after generation, before
loading"), (e) the injection registry is complete: every FaultKind has one.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from dqs.errors import FaultSpecError
from dqs.models.enums import FaultKind, ResolutionType, RootCauseCategory
from dqs.sim import faults as F
from dqs.sim import generator

DATE = dt.date(2026, 2, 1)
SEED = 42


def _batch() -> dict[str, pd.DataFrame]:
    return generator.tables_for_date(DATE, SEED)


def _spec(spec_str: str) -> F.FaultSpec:
    return F.parse_fault_spec(spec_str)


def _bytes(frame: pd.DataFrame) -> bytes:
    return frame.to_csv(index=False, lineterminator="\n").encode("utf-8")


# --- parser -------------------------------------------------------------------------


def test_parser_happy_paths() -> None:
    assert _spec("column_rename:orders.customer_id:new_name=cust_id") == (
        F.FaultSpec(
            kind=FaultKind.COLUMN_RENAME,
            table="orders",
            column="customer_id",
            params={"new_name": "cust_id"},
        )
    )
    assert _spec("missing_table:payments") == F.FaultSpec(
        kind=FaultKind.MISSING_TABLE, table="payments"
    )
    assert _spec("null_spike:customers.state:fraction=0.5").params == {"fraction": 0.5}
    assert _spec("late_batch:orders").kind is FaultKind.LATE_BATCH
    # numbers coerce; text stays text
    assert _spec("scale_shift:products.price:factor=25.5").params == {"factor": 25.5}
    assert _spec("column_added:orders:new_column=note").params == {"new_column": "note"}


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "nonsense",
        "nonsense:orders",
        "notakind:orders.order_id",
        "null_spike:unknown_table.col",
        "null_spike:orders",  # kind requires table.column
        "null_spike:orders.order_ts:",  # empty parameter list after ':'
        "late_batch:orders:",  # same, batch-level kind
        "null_spike:orders.order_ts:a=1",  # unknown parameter
        "null_spike:orders.order_ts:fraction=0",  # out of (0, 1]
        "null_spike:orders.order_ts:fraction=1.5",
        "null_spike:orders.order_ts:fraction=abc",  # text where a number is needed
        "null_spike:orders.order_id",  # primary key column rejected
        "null_spike:orders.order_ts:new_name=x",  # parameter belongs to another kind
        "orphan_keys:orders.order_ts",  # not a known FK pair
        "orphan_keys:customers.state:fraction=0.2",  # ditto, with params
        "column_rename:orders.customer_id:new_name=Bad Col",  # not an identifier
        "column_added:orders:new_column=SAVE 10",  # not an identifier
        "scale_shift:orders.total_amount:factor",  # malformed k=v pair
    ],
)
def test_parser_rejects_malformed_specs_with_clear_errors(bad: str) -> None:
    with pytest.raises(FaultSpecError):
        F.parse_fault_spec(bad)


def test_parser_error_messages_mention_the_spec() -> None:
    with pytest.raises(FaultSpecError) as err:
        F.parse_fault_spec("tipe_change:orders.total_amount")
    assert "tipe_change" in str(err.value) and "unknown fault kind" in str(err.value)


def test_parser_error_for_unsupported_fk_pair_names_the_known_pairs() -> None:
    with pytest.raises(FaultSpecError) as err:
        F.parse_fault_spec("orphan_keys:order_items.quantity")
    message = str(err.value)
    assert "no known parent domain" in message
    assert "orders.customer_id" in message  # an example of a supported pair


def test_fk_map_documents_exactly_the_four_known_pairs() -> None:
    assert F.FK_MAP == {
        "orders.customer_id": "raw.customers.customer_id",
        "order_items.order_id": "raw.orders.order_id",
        "payments.order_id": "raw.orders.order_id",
        "order_items.product_id": "raw.products.product_id",
    }


def test_every_fault_kind_has_a_registered_injection() -> None:
    assert set(F._INJECTORS) == set(FaultKind)


# --- injection behaviour --------------------------------------------------------------


def test_f1_column_rename_is_exactly_a_header_rename() -> None:
    clean = _batch()
    faulted = F.apply_faults(
        clean, [_spec("column_rename:orders.customer_id:new_name=cust_id")], DATE, SEED
    )
    renamed = faulted.tables["orders"]
    assert list(renamed.columns) == [
        "order_id",
        "cust_id",
        "order_ts",
        "status",
        "total_amount",
    ]
    # values move with the column: renamed frame == clean frame with new header
    expected = clean["orders"].rename(columns={"customer_id": "cust_id"})
    assert _bytes(renamed) == _bytes(expected)
    truth = faulted.ground_truth[0]
    assert truth.expected_checks == ["schema.missing_column", "schema.unexpected_column"]
    assert truth.expected_category is RootCauseCategory.SOURCE_COLUMN_RENAMED
    assert truth.expected_resolution is ResolutionType.PR


def test_f2_column_removed_drops_the_column() -> None:
    clean = _batch()
    faulted = F.apply_faults(clean, [_spec("column_removed:orders.order_ts")], DATE, SEED)
    assert "order_ts" not in faulted.tables["orders"].columns
    truth = faulted.ground_truth[0]
    assert truth.expected_checks == ["schema.missing_column"]


def test_f3_type_change_formats_exactly_the_fraction_as_currency() -> None:
    clean = _batch()
    amounts = clean["orders"]["total_amount"]
    faulted = F.apply_faults(
        clean, [_spec("type_change:orders.total_amount:fraction=0.25")], DATE, SEED
    )
    out = faulted.tables["orders"]["total_amount"].astype(str)
    hit = out.str.match(r"^[₹$€][0-9,]+\.[0-9]{2}$")
    expected_hits = int(round(len(amounts) * 0.25))
    assert int(hit.sum()) == expected_hits
    # untouched values are unchanged; formatted ones parse back to the same amount
    not_hit = out[~hit]
    assert not_hit.tolist() == [str(v) for v in amounts[~hit]]
    symbols = out[hit].str[0]
    assert set(symbols) <= {"₹", "$", "€"}
    truth = faulted.ground_truth[0]
    assert truth.expected_checks == ["type.logical"]


def test_f4_null_spike_hits_exactly_the_fraction_and_is_replayable() -> None:
    clean = _batch()
    state = clean["customers"]["state"]
    faulted = F.apply_faults(clean, [_spec("null_spike:customers.state:fraction=0.5")], DATE, SEED)
    out = faulted.tables["customers"]["state"]
    expected_nulls = int(round(len(state) * 0.5))
    assert out.isna().sum() == expected_nulls  # exactly the spec's fraction
    # the SAME rows are null when re-applied — the affected subset is replayable
    again = F.apply_faults(
        generator.tables_for_date(DATE, SEED),
        [_spec("null_spike:customers.state:fraction=0.5")],
        DATE,
        SEED,
    )
    assert out.isna().to_list() == again.tables["customers"]["state"].isna().to_list()
    # untouchd positions keep their original values
    mask = ~out.isna()
    assert (out[mask].to_numpy() == state.to_numpy()[mask]).all()
    truth = faulted.ground_truth[0]
    assert truth.expected_checks == ["nulls.rate"]


def test_f5_orphan_ids_are_strictly_above_the_cumulative_parent_max() -> None:
    clean = _batch()
    faulted = F.apply_faults(
        clean, [_spec("orphan_keys:orders.customer_id:fraction=0.2")], DATE, SEED
    )
    parent_max = generator.cumulative_customers(SEED, DATE)
    out = faulted.tables["orders"]["customer_id"]
    orphans_mask = out > parent_max
    # the number hit is exactly the fraction, and they lie beyond any parent id
    assert int(orphans_mask.sum()) == int(round(len(out) * 0.2))
    assert out[orphans_mask].min() == parent_max + 1
    # at the same positions, non-orphan values equal the clean values (mask compare)
    clean_values = clean["orders"]["customer_id"].to_numpy()[~orphans_mask]
    assert (out[~orphans_mask].to_numpy() == clean_values).all()
    truth = faulted.ground_truth[0]
    assert truth.expected_checks == ["fk.integrity"]


def test_f5_order_items_orphans_are_above_every_possible_parent_order_id() -> None:
    clean = _batch()
    faulted = F.apply_faults(
        clean, [_spec("orphan_keys:order_items.order_id:fraction=0.1")], DATE, SEED
    )
    bound = generator.ORDER_ID_SLOT * ((DATE - generator.BASELINE_START_DATE).days + 1)
    items = faulted.tables["order_items"]["order_id"]
    assert int((items > bound).sum()) == int(round(len(items) * 0.1))
    # the injected ids form the dense block above the bound: provably nonexistent
    assert int(items.max()) == bound + int(round(len(items) * 0.1))


def test_f6_duplicate_rows_replay_exactly_the_fraction() -> None:
    clean = _batch()
    orders = clean["orders"]
    faulted = F.apply_faults(clean, [_spec("duplicate_rows:orders:fraction=0.1")], DATE, SEED)
    out = faulted.tables["orders"]
    extra = int(round(len(orders) * 0.1))
    assert len(out) == len(orders) + extra
    assert len(out) - out["order_id"].nunique() == extra
    # duplicates are exact copies (values, order irrelevant)
    merged = out.merge(orders, on=list(orders.columns), how="left", indicator=True)
    assert (merged["_merge"] == "both").all()
    truth = faulted.ground_truth[0]
    assert truth.expected_checks == ["unique.pk"]
    assert truth.expected_category is RootCauseCategory.DUPLICATE_RECORDS


def test_f7_scale_shift_multiplies_exactly_by_the_factor() -> None:
    clean = _batch()
    faulted = F.apply_faults(
        clean, [_spec("scale_shift:orders.total_amount:factor=100")], DATE, SEED
    )
    assert np.allclose(
        faulted.tables["orders"]["total_amount"],
        100.0 * clean["orders"]["total_amount"].to_numpy(),
    )
    truth = faulted.ground_truth[0]
    assert truth.expected_checks == ["range.bounds"]
    assert truth.expected_category is RootCauseCategory.UNIT_OR_SCALE_CHANGE


def test_f8_late_batch_shifts_only_arrival_time() -> None:
    clean = _batch()
    faulted = F.apply_faults(clean, [_spec("late_batch:orders:hours=12")], DATE, SEED)
    from dqs.sim.loader import arrived_at_for

    assert faulted.tables["orders"] is clean["orders"]  # frames untouched
    assert faulted.arrived_at == arrived_at_for(DATE) + dt.timedelta(hours=12)
    truth = faulted.ground_truth[0]
    assert truth.expected_checks == ["freshness.sla"]
    assert truth.expected_category is RootCauseCategory.LATE_OR_MISSING_BATCH
    # default is the documented 24h push, which is definitely past the SLA
    default = F.apply_faults(clean, [_spec("late_batch:orders")], DATE, SEED)
    assert default.arrived_at == arrived_at_for(DATE) + dt.timedelta(hours=24)


def test_f9_missing_table_removes_the_whole_table() -> None:
    clean = _batch()
    faulted = F.apply_faults(clean, [_spec("missing_table:payments")], DATE, SEED)
    assert set(faulted.tables) == set(clean) - {"payments"}
    truth = faulted.ground_truth[0]
    assert truth.expected_checks == ["volume.rows"]


def test_f10_column_added_appends_short_text_column_with_some_nulls() -> None:
    clean = _batch()
    faulted = F.apply_faults(clean, [_spec("column_added:orders")], DATE, SEED)
    out = faulted.tables["orders"]
    # default shape: one extra text column, coupon-like short codes + ~30% nulls
    assert list(out.columns)[:5] == list(clean["orders"].columns)
    assert list(out.columns)[5] == "coupon_code"
    values = out["coupon_code"]
    assert set(values.dropna()) <= {"SAVE10", "SAVE20", "BUNDLE15", "FREESHIP"}
    nulls = int(values.isna().sum())
    assert 0 < nulls < len(values)  # some nulls, not all
    assert len(out) == len(clean["orders"])
    truth = faulted.ground_truth[0]
    assert truth.expected_checks == ["schema.unexpected_column"]
    assert truth.expected_category is RootCauseCategory.SOURCE_COLUMN_ADDED
    assert truth.expected_resolution is ResolutionType.NONE
    # an alternative name is injected via the parameter
    named = F.apply_faults(clean, [_spec("column_added:customers:new_column=segment")], DATE, SEED)
    assert list(named.tables["customers"].columns)[-1] == "segment"
    # non-orders tables fall back to a neutral default name
    other = F.apply_faults(clean, [_spec("column_added:payments")], DATE, SEED)
    assert list(other.tables["payments"].columns)[-1] == "extra_note"


def test_injection_leaves_the_generated_frames_untouched() -> None:
    clean = _batch()
    before = {name: _bytes(frame) for name, frame in clean.items()}
    specs = F.parse_fault_specs(
        [
            "null_spike:customers.state:fraction=0.5",
            "duplicate_rows:orders",
            "scale_shift:payments.amount",
            "column_rename:payments.payment_id:new_name=pid",
            "column_removed:payments.method",
            "column_added:payments",
            "orphan_keys:payments.order_id",
            "type_change:customers.customer_id:fraction=0.5",
            "missing_table:order_items",
            "late_batch:orders",
        ]
    )
    F.apply_faults(clean, specs, DATE, SEED)
    assert {name: _bytes(frame) for name, frame in clean.items()} == before


def test_multiple_faults_apply_in_spec_order_and_are_deterministic() -> None:
    specs = [
        "duplicate_rows:orders:fraction=0.1",
        "null_spike:orders.status:fraction=0.3",
        "orphan_keys:orders.customer_id:fraction=0.2",
    ]
    a = F.apply_faults(
        generator.tables_for_date(DATE, SEED), F.parse_fault_specs(specs), DATE, SEED
    )
    b = F.apply_faults(
        generator.tables_for_date(DATE, SEED), F.parse_fault_specs(specs), DATE, SEED
    )
    for name in a.tables:
        assert _bytes(a.tables[name]) == _bytes(b.tables[name]), name
    assert a.ground_truth == b.ground_truth
    assert [record.kind for record in a.ground_truth] == [
        FaultKind.DUPLICATE_ROWS,
        FaultKind.NULL_SPIKE,
        FaultKind.ORPHAN_KEYS,
    ]


def test_ground_truth_records_carry_all_expected_fields() -> None:
    mapping = {
        "column_rename:orders.customer_id:new_name=cust_id": (
            RootCauseCategory.SOURCE_COLUMN_RENAMED,
            ResolutionType.PR,
        ),
        "column_removed:orders.order_ts": (
            RootCauseCategory.SOURCE_COLUMN_REMOVED,
            ResolutionType.ISSUE,
        ),
        "type_change:orders.total_amount": (
            RootCauseCategory.SOURCE_TYPE_CHANGED,
            ResolutionType.PR,
        ),
        "null_spike:customers.state": (
            RootCauseCategory.UPSTREAM_NULLS_INTRODUCED,
            ResolutionType.ISSUE,
        ),
        "orphan_keys:payments.order_id": (
            RootCauseCategory.REFERENTIAL_INTEGRITY_BROKEN,
            ResolutionType.ISSUE,
        ),
        "duplicate_rows:orders": (RootCauseCategory.DUPLICATE_RECORDS, ResolutionType.PR),
        "scale_shift:payments.amount": (
            RootCauseCategory.UNIT_OR_SCALE_CHANGE,
            ResolutionType.ISSUE,
        ),
        "late_batch:orders": (RootCauseCategory.LATE_OR_MISSING_BATCH, ResolutionType.ISSUE),
        "missing_table:payments": (
            RootCauseCategory.LATE_OR_MISSING_BATCH,
            ResolutionType.ISSUE,
        ),
        "column_added:orders": (RootCauseCategory.SOURCE_COLUMN_ADDED, ResolutionType.NONE),
    }
    for spec_str, (category, resolution) in mapping.items():
        result = F.apply_faults(
            generator.tables_for_date(DATE, SEED), F.parse_fault_specs([spec_str]), DATE, SEED
        )
        record = result.ground_truth[0]
        assert record.expected_category is category, spec_str
        assert record.expected_resolution is resolution, spec_str
        assert record.expected_checks, spec_str
        assert record.parameters or record.kind in (
            FaultKind.COLUMN_REMOVED,
            FaultKind.MISSING_TABLE,
        )


def test_faulted_batch_rejects_injecting_a_table_removed_earlier() -> None:
    result = F.apply_faults(
        generator.tables_for_date(DATE, SEED), [_spec("missing_table:payments")], DATE, SEED
    )
    assert "payments" not in result.tables
    with pytest.raises(FaultSpecError, match="not in the batch"):
        F.apply_faults(
            generator.tables_for_date(DATE, SEED),
            [_spec("missing_table:payments"), _spec("scale_shift:payments.amount")],
            DATE,
            SEED,
        )
