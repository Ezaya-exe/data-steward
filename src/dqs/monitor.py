"""Monitor: the deterministic detection loop over the warehouse (§8).

Why here: `dqs monitor run` is the "Pipeline Monitor" + "Schema Drift Detector"
of the story, and it is pure code — no LLM. It (1) finds batches not yet
`checked`, (2) runs the check engine, (3) runs `monitor.missing_batch`,
(4) creates/updates incidents, (5) marks the batches `checked`. Everything is
driven off an `as_of` instant so a run can be replayed at a chosen time; when
omitted it defaults to the business day after the latest loaded batch (the
simulation's monitoring watermark), which keeps a contiguous clean warehouse at
zero incidents.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import Engine, text

from dqs.checks.base import CheckContext
from dqs.checks.engine import CheckEngine
from dqs.checks.freshness import MissingBatchCheck
from dqs.errors import CheckError
from dqs.incidents import build_drafts, persist_incidents
from dqs.models.contract import Contract

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MonitorResult:
    """What one `monitor run` did — the CLI summary and test evidence."""

    as_of: dt.datetime
    batches_checked: list[str]
    missing_batches: list[dt.date]
    created: list[uuid.UUID]
    updated: list[uuid.UUID]


def parse_as_of(value: str) -> dt.datetime:
    """Parse an `--as-of` ISO-8601 instant; naive values are treated as UTC."""
    try:
        moment = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise CheckError(f"--as-of must be ISO-8601 (e.g. 2026-02-03T00:00Z): {exc}") from exc
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.UTC)


def default_as_of(engine: Engine) -> dt.datetime:
    """Monitoring watermark: 00:00 UTC the day after the latest loaded batch.

    Falls back to "now" on an empty warehouse (nothing to monitor yet).
    """
    latest = _latest_business_date(engine)
    if latest is None:
        return dt.datetime.now(dt.UTC)
    return dt.datetime.combine(latest + dt.timedelta(days=1), dt.time(0, 0), tzinfo=dt.UTC)


def unchecked_batches(engine: Engine, as_of: dt.datetime) -> list[str]:
    """Batch ids still `loaded` and no later than `as_of`'s business date."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT batch_id FROM dq.batches "
                "WHERE status <> 'checked' AND business_date <= :d "
                "ORDER BY business_date, batch_id"
            ),
            {"d": as_of.date()},
        ).fetchall()
    return [str(row[0]) for row in rows]


def run_monitor(
    engine: Engine, contracts: Mapping[str, Contract], as_of: dt.datetime | None = None
) -> MonitorResult:
    """Run the §8 loop and return what changed (deterministic; persists to dq)."""
    moment = as_of if as_of is not None else default_as_of(engine)
    if not contracts:
        logger.warning(
            "no approved contracts - the engine will enforce no table rules; "
            "run 'dqs contracts approve --all' first"
        )
    check_engine = CheckEngine(engine, contracts)
    batch_ids = unchecked_batches(engine, moment)

    violations = []
    for batch_id in batch_ids:
        violations.extend(check_engine.run(batch_id))
    violations.extend(MissingBatchCheck().run(CheckContext(engine=engine, as_of=moment)))

    result = persist_incidents(engine, build_drafts(violations))
    _mark_checked(engine, batch_ids)
    missing = sorted(
        {
            dt.date.fromisoformat(v.observed["business_date"])
            for v in violations
            if v.check == MissingBatchCheck.name
        }
    )
    logger.info(
        "monitor as_of=%s checked=%d incidents_created=%d updated=%d missing=%d",
        moment.isoformat(),
        len(batch_ids),
        len(result.created),
        len(result.updated),
        len(missing),
    )
    return MonitorResult(
        as_of=moment,
        batches_checked=batch_ids,
        missing_batches=missing,
        created=result.created,
        updated=result.updated,
    )


def _mark_checked(engine: Engine, batch_ids: list[str]) -> None:
    if not batch_ids:
        return
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE dq.batches SET status = 'checked' WHERE batch_id = ANY(:ids)"),
            {"ids": batch_ids},
        )


def _latest_business_date(engine: Engine) -> dt.date | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT max(business_date) FROM dq.batches")).fetchone()
    return None if row is None or row[0] is None else row[0]
