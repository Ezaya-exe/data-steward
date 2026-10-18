"""Unit tests for the deterministic batch generator (Phase 1 acceptance)."""

from __future__ import annotations

import datetime as dt

import pytest

from dqs.sim import generator


def _bytes(frame):  # helper: byte fingerprint of a batch table
    return frame.to_csv(index=False, lineterminator="\n").encode("utf-8")


def test_same_seed_and_date_reproduces_batch_byte_identically() -> None:
    a = generator.tables_for_date(dt.date(2026, 1, 7), 42)
    b = generator.tables_for_date(dt.date(2026, 1, 7), 42)
    assert set(a) == set(b) == {"customers", "products", "orders", "order_items", "payments"}
    for name in a:
        assert _bytes(a[name]) == _bytes(b[name]), f"{name} differs between identical seeds"


def test_day_three_after_skipping_day_two_is_identical() -> None:
    """The generator is a pure function of (seed, date): a skipped day leaves
    no state behind, so the missing-batch scenario replays later days exactly."""
    day_two, day_three = dt.date(2026, 1, 2), dt.date(2026, 1, 3)
    for seed in (42, 7):
        normal = generator.tables_for_date(day_three, seed)
        skipped_and_discarded = generator.tables_for_date(day_two, seed)
        after_skip = generator.tables_for_date(day_three, seed)
        assert len(skipped_and_discarded) == 5  # the skipped day really was generated
        for name in normal:
            assert _bytes(normal[name]) == _bytes(after_skip[name]), (seed, name)


def test_different_seed_changes_the_batch() -> None:
    a = generator.tables_for_date(dt.date(2026, 1, 7), 42)
    b = generator.tables_for_date(dt.date(2026, 1, 7), 43)
    assert _bytes(a["orders"]) != _bytes(b["orders"])
    assert _bytes(a["customers"]) != _bytes(b["customers"])


def test_day_one_is_the_initial_dimension_load() -> None:
    day1 = generator.tables_for_date(dt.date(2026, 1, 1), 42)
    assert len(day1["customers"]) == 2000
    assert len(day1["products"]) == 200
    assert day1["orders"]["order_id"].between(1, 1000).all()


def test_later_days_append_dimensions_only() -> None:
    later = generator.tables_for_date(dt.date(2026, 1, 15), 42)
    # ~35–45 new customers per day, contiguous after the previous day's max
    prev_max = generator.cumulative_customers(42, dt.date(2026, 1, 14))
    now_max = generator.cumulative_customers(42, dt.date(2026, 1, 15))
    assert prev_max < now_max
    ids = later["customers"]["customer_id"]
    assert ids.is_unique and ids.min() == prev_max + 1 and ids.max() == now_max
    ids_day14 = generator.tables_for_date(dt.date(2026, 1, 14), 42)["customers"]["customer_id"]
    assert set(ids).isdisjoint(set(ids_day14)), "customer ids must not repeat across days"
    # catalogue has no new rows after day 1, but the header is still present
    assert list(later["products"].columns) == ["product_id", "category", "price"]
    assert len(later["products"]) == 0


def test_orders_and_children_stay_within_documented_shapes() -> None:
    for date in (dt.date(2026, 1, 1), dt.date(2026, 1, 21), dt.date(2026, 2, 8)):
        batch = generator.tables_for_date(date, 42)
        n_orders = len(batch["orders"])
        assert 400 <= n_orders <= 600
        items_per_order = len(batch["order_items"]) / n_orders
        assert 1.0 <= items_per_order <= 4.0 + 100 / n_orders  # rounding headroom
        # PKs unique inside the batch
        for name, key in (
            ("orders", "order_id"),
            ("order_items", "order_item_id"),
            ("payments", "payment_id"),
            ("customers", "customer_id"),
        ):
            assert batch[name][key].is_unique, f"{name}.{key} duplicates on {date}"
        # FK targets exist within the same cumulative batch set
        assert (
            batch["orders"]["customer_id"]
            .between(1, generator.cumulative_customers(42, date))
            .all()
        )
        # products reference the cumulative catalogue (day 1 loaded all 200)
        assert batch["order_items"]["product_id"].between(1, 200).all()
        assert set(batch["payments"]["order_id"]) <= set(batch["orders"]["order_id"])
        # enum domains
        assert set(batch["orders"]["status"]) <= set(generator.ORDER_STATUSES)
        assert set(batch["payments"]["method"]) <= set(generator.PAYMENT_METHODS)


def test_payment_splits_sum_exactly_to_order_totals() -> None:
    batch = generator.tables_for_date(dt.date(2026, 1, 9), 42)
    totals = batch["orders"].set_index("order_id")["total_amount"]
    sums = batch["payments"].groupby("order_id")["amount"].sum()
    common = totals.index.intersection(sums.index)
    assert len(common) == len(totals)  # every order has ≥1 payment
    assert (sums.loc[common] - totals.loc[common]).abs().max() < 1e-9


def test_item_prices_match_the_product_catalogue() -> None:
    batch = generator.tables_for_date(dt.date(2026, 1, 12), 42)
    ids = batch["order_items"]["product_id"].unique()
    for pid in ids:
        assert batch["order_items"].query(f"product_id == {pid}")["unit_price"].nunique() == 1


def test_orders_reference_existing_customers_only_clean() -> None:
    for seed_value in (42, 7):
        for date in (dt.date(2026, 1, 2), dt.date(2026, 1, 30), dt.date(2026, 2, 1)):
            batch = generator.tables_for_date(date, seed_value)
            cumulative = generator.cumulative_customers(seed_value, date)
            assert batch["orders"]["customer_id"].between(1, cumulative).all(), (seed_value, date)


@pytest.mark.parametrize(
    "date,expected_id",
    [(dt.date(2026, 1, 1), "batch_20260101"), (dt.date(2026, 2, 14), "batch_20260214")],
)
def test_batch_id_format(date: dt.date, expected_id: str) -> None:
    from dqs.sim import loader

    assert loader.batch_id_for_date(date) == expected_id


def test_arrived_at_is_next_day_0200_utc() -> None:
    arrived = generator  # placeholder to fail loudly if refactored wrongly
    del arrived
    from dqs.sim import loader

    a = loader.arrived_at_for(dt.date(2026, 1, 30))
    assert (a.year, a.month, a.day) == (2026, 1, 31) and a.hour == 2 and a.minute == 0
    assert a.tzinfo == dt.UTC
