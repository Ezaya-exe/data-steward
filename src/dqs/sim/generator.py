"""Deterministic synthetic batch generator for the simulated e-commerce store.

Produces the 5 raw business tables for one business date (docs/ARCHITECTURE.md
§4.2). Why here: acceptance criteria and benchmark scenarios all depend on
"same seed + same date ⇒ identical batch", so seeding and ID allocation are the
most important design constraints in the whole simulator.

Conventions:
- One independent numpy RNG per table, seeded as ``[seed, date.toordinal(), k]``
  with a distinct ``k`` per table, so refactoring one table never silently
  changes the others.
- Frames carry business columns only (stable order); the loader appends
  ``_batch_id``/``_loaded_at``.
- Across-date PK uniqueness is structural, not probabilistic: each date gets a
  fixed ID slot per table, and daily output is bounded well below the slot.
- FK integrity on clean days is structural: orders/items/payments reference
  only IDs that exist cumulatively up to (and including) that date.
"""

from __future__ import annotations

import datetime as dt
from typing import Final

import numpy as np
import pandas as pd

BASELINE_START_DATE: Final = dt.date(2026, 1, 1)
INITIAL_CUSTOMER_ROWS: Final = 2000
INITIAL_PRODUCT_ROWS: Final = 200

# ID slots per calendar day for the high-volume tables; daily output is bounded
# well below the slot. Customers use CONTIGUOUS ids (see _customers) so clean
# data can never have order -> customer orphans.
ORDER_ID_SLOT: Final = 1000
ORDER_ITEM_ID_SLOT: Final = 5000
PAYMENT_ID_SLOT: Final = 2000

ORDER_STATUSES: Final = ("placed", "paid", "shipped", "delivered", "cancelled")
STATUS_WEIGHTS: Final = (0.12, 0.15, 0.15, 0.40, 0.18)
PRODUCT_CATEGORIES: Final = ("electronics", "apparel", "home", "beauty", "toys")
PAYMENT_METHODS: Final = ("card", "upi", "netbanking", "cod")
STATES: Final = (
    "karnataka",
    "maharashtra",
    "delhi",
    "tamil_nadu",
    "telangana",
    "gujarat",
    "kerala",
    "west_bengal",
    "uttar_pradesh",
    "rajasthan",
)
FIRST_NAMES: Final = (
    "priya",
    "arun",
    "meera",
    "vikram",
    "isha",
    "rahul",
    "sneha",
    "dev",
    "tara",
    "omar",
)
LAST_NAMES: Final = (
    "sharma",
    "patel",
    "rao",
    "gupta",
    "iyer",
    "khan",
    "das",
    "menon",
    "joshi",
    "bose",
)

# RNG salt per table so tables stay independent across refactors
_RNG_K_NEW_CUSTOMERS: Final = 1
_RNG_K_CUSTOMERS: Final = 11
_RNG_K_PRODUCTS: Final = 22
_RNG_K_ORDERS: Final = 33
_RNG_K_ITEMS_ROWS: Final = 55
_RNG_K_PAYMENTS_ROWS: Final = 77

_TABLE_COLUMNS: Final = {
    "customers": ["customer_id", "email", "state", "signup_date"],
    "products": ["product_id", "category", "price"],
    "orders": ["order_id", "customer_id", "order_ts", "status", "total_amount"],
    "order_items": ["order_item_id", "order_id", "product_id", "quantity", "unit_price"],
    "payments": ["payment_id", "order_id", "method", "amount"],
}


def tables_for_date(business_date: dt.date, seed: int) -> dict[str, pd.DataFrame]:
    """Generate the 5 raw batch tables for one business date (pure, deterministic).

    Children (items/payments) are derived from the generated ``orders`` frame so
    their row counts, order ids and money match exactly — clean data must satisfy
    every FK/sum invariant structurally, not probabilistically.
    """
    max_customer_id = cumulative_customers(seed, business_date)
    orders = _orders(business_date, seed, max_customer_id)
    return {
        "customers": _customers(business_date, seed),
        "products": _products(business_date, seed),
        "orders": orders,
        "order_items": _order_items(business_date, seed, orders),
        "payments": _payments(business_date, seed, orders),
    }


def columns_of_table(table: str) -> list[str]:
    """Business columns of a raw table, in stable order."""
    return list(_TABLE_COLUMNS[table])


def cumulative_customers(seed: int, upto: dt.date) -> int:
    """Number of customers that would exist after loading batches up to ``upto``.

    A cheap pure loop over the fixed baseline window: recomputing a 30-day
    history costs microseconds and avoids any module-level state.
    """
    delta = (upto - BASELINE_START_DATE).days
    if delta < 0:
        return 0
    if delta == 0:
        return INITIAL_CUSTOMER_ROWS
    total = INITIAL_CUSTOMER_ROWS
    day = BASELINE_START_DATE + dt.timedelta(days=1)
    while day <= upto:
        total += _new_customers_count(seed, day)
        day += dt.timedelta(days=1)
    return total


def _new_customers_count(seed: int, business_date: dt.date) -> int:
    rng = np.random.default_rng([seed, business_date.toordinal(), _RNG_K_NEW_CUSTOMERS])
    return int(rng.integers(35, 46))


def _customers(business_date: dt.date, seed: int) -> pd.DataFrame:
    """Day 1: initial dimension load (2000 rows). Later days: ~35–45 new rows.

    IDs stay contiguous over days (``cumulative(day-1) + 1 .. cumulative(day)``):
    every order references ``1..cumulative(day)``, so clean days can never break
    the FK to customers — reference integrity is a structural property of the
    design, not a hope about the RNG.
    """
    delta = (business_date - BASELINE_START_DATE).days
    if delta == 0:
        count, start_id = INITIAL_CUSTOMER_ROWS, 1
    else:
        count = _new_customers_count(seed, business_date)
        start_id = cumulative_customers(seed, business_date) - count + 1

    rng = np.random.default_rng([seed, business_date.toordinal(), _RNG_K_CUSTOMERS])
    ids = np.arange(start_id, start_id + count)
    firsts = _choice_str(rng, FIRST_NAMES, count)
    lasts = _choice_str(rng, LAST_NAMES, count)
    states = _choice_str(rng, STATES, count)
    # day-1 signups spread back over ~2 years; later days are recent signups
    span = 731 if delta == 0 else 0
    back_days = rng.integers(0, span + 1, size=count)
    signups = [str(business_date - dt.timedelta(days=int(d))) for d in back_days]
    emails = [
        f"{fn}.{ln}{int(i)}@example.com" for fn, ln, i in zip(firsts, lasts, ids, strict=True)
    ]
    return pd.DataFrame(
        {
            "customer_id": [int(i) for i in ids],
            "email": emails,
            "state": states,
            "signup_date": signups,
        },
        columns=_TABLE_COLUMNS["customers"],
    )


def _products(business_date: dt.date, seed: int) -> pd.DataFrame:
    """Day 1 (and only day 1): the 200-product catalogue. Other days: header, 0 rows.

    A present-but-empty batch keeps ``missing table`` (whole header absent,
    fault F9) unambiguous versus ``no new rows``.
    """
    if (business_date - BASELINE_START_DATE).days != 0:
        return pd.DataFrame(columns=_TABLE_COLUMNS["products"])
    rng = np.random.default_rng([seed, business_date.toordinal(), _RNG_K_PRODUCTS])
    ids = np.arange(1, INITIAL_PRODUCT_ROWS + 1)
    categories = _choice_str(rng, PRODUCT_CATEGORIES, len(ids))
    return pd.DataFrame(
        {
            "product_id": [int(i) for i in ids],
            "category": categories,
            "price": [_product_price(int(i)) for i in ids],
        },
        columns=_TABLE_COLUMNS["products"],
    )


def _product_price(product_id: int) -> float:
    """Stable hash-derived price per product id.

    Pure function, not RNG: order item prices must equal catalogue prices for
    any date/seed without regenerating the day-1 catalogue.
    """
    return round(20.0 + ((int(product_id) * 2654435761) % 400000) / 100.0, 2)


def _orders(business_date: dt.date, seed: int, max_customer_id: int) -> pd.DataFrame:
    delta = (business_date - BASELINE_START_DATE).days
    rng = np.random.default_rng([seed, business_date.toordinal(), _RNG_K_ORDERS])
    count = int(rng.integers(400, 601))
    ids = ORDER_ID_SLOT * delta + np.arange(1, count + 1)
    customer_ids = 1 + rng.integers(0, max(1, max_customer_id), size=count)
    statuses = _choice_weighted_str(rng, ORDER_STATUSES, STATUS_WEIGHTS, count)
    amounts = np.round(rng.uniform(100.0, 5000.0, size=count), 2)
    seconds = rng.integers(0, 86400, size=count)
    return pd.DataFrame(
        {
            "order_id": [int(i) for i in ids],
            "customer_id": [int(c) for c in customer_ids],
            "order_ts": [_format_ts(business_date, int(s)) for s in seconds],
            "status": statuses,
            "total_amount": [float(a) for a in amounts],
        },
        columns=_TABLE_COLUMNS["orders"],
    )


def _order_items(business_date: dt.date, seed: int, orders: pd.DataFrame) -> pd.DataFrame:
    """1–4 items per order; quantities 1–3; unit price from the product catalogue.

    ``orders`` is the already-generated orders frame: children must reference
    exactly its ids (independent RNG streams would break FK/sum invariants).
    """
    delta = (business_date - BASELINE_START_DATE).days
    rows_rng = np.random.default_rng([seed, business_date.toordinal(), _RNG_K_ITEMS_ROWS])

    order_ids = orders["order_id"].to_numpy()
    items_per_order = rows_rng.integers(1, 5, size=len(orders))
    total_items = int(items_per_order.sum())
    assert total_items <= ORDER_ITEM_ID_SLOT, "daily guard: items exceed id slot"

    repeated_order_ids = np.repeat(order_ids, items_per_order)
    product_ids = 1 + rows_rng.integers(0, INITIAL_PRODUCT_ROWS, size=total_items)
    quantities = 1 + rows_rng.integers(0, 3, size=total_items)
    item_ids = ORDER_ITEM_ID_SLOT * delta + np.arange(1, total_items + 1)
    return pd.DataFrame(
        {
            "order_item_id": [int(i) for i in item_ids],
            "order_id": [int(o) for o in repeated_order_ids],
            "product_id": [int(p) for p in product_ids],
            "quantity": [int(q) for q in quantities],
            "unit_price": [_product_price(int(p)) for p in product_ids],
        },
        columns=_TABLE_COLUMNS["order_items"],
    )


def _payments(business_date: dt.date, seed: int, orders: pd.DataFrame) -> pd.DataFrame:
    """1 payment per order; ~5% of orders get a second payment (split 60/40).

    Payment amounts are derived from the generated order totals so the sum of
    payments for an order equals its ``total_amount`` exactly.
    """
    delta = (business_date - BASELINE_START_DATE).days
    pay_rng = np.random.default_rng([seed, business_date.toordinal(), _RNG_K_PAYMENTS_ROWS])

    order_ids = orders["order_id"].to_numpy()
    totals = orders["total_amount"].to_numpy()
    has_second = pay_rng.random(len(orders)) < 0.05
    payment_count = int(len(orders) + int(has_second.sum()))
    assert payment_count <= PAYMENT_ID_SLOT, "daily guard: payments exceed id slot"

    payment_ids = PAYMENT_ID_SLOT * delta + np.arange(1, payment_count + 1)
    methods = _choice_str(pay_rng, PAYMENT_METHODS, payment_count)

    order_ids_out: list[int] = []
    amounts_out: list[float] = []
    for order_id, total, second in zip(order_ids, totals, has_second, strict=True):
        if second:
            # the split sums to exactly the order total by rounding the remainder
            first_paid = round(float(total) * 0.6, 2)
            order_ids_out += [int(order_id), int(order_id)]
            amounts_out += [first_paid, round(float(total) - first_paid, 2)]
        else:
            order_ids_out.append(int(order_id))
            amounts_out.append(round(float(total), 2))
    return pd.DataFrame(
        {
            "payment_id": [int(p) for p in payment_ids],
            "order_id": order_ids_out,
            "method": methods,
            "amount": amounts_out,
        },
        columns=_TABLE_COLUMNS["payments"],
    )


def _format_ts(business_date: dt.date, seconds_since_midnight: int) -> str:
    """``YYYY-MM-DD HH:MM:SS`` — plainly castable to timestamptz (UTC assumed)."""
    hours, rem = divmod(seconds_since_midnight, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{business_date.isoformat()} {hours:02d}:{minutes:02d}:{secs:02d}"


def _choice_str(rng: np.random.Generator, choices: tuple[str, ...], size: int) -> list[str]:
    return [str(c) for c in rng.choice(np.array(choices), size=size)]


def _choice_weighted_str(
    rng: np.random.Generator,
    choices: tuple[str, ...],
    weights: tuple[float, ...],
    size: int,
) -> list[str]:
    return [str(c) for c in rng.choice(np.array(choices), size=size, p=np.array(weights))]
