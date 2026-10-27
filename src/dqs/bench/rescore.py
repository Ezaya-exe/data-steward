"""Offline re-scoring of a stored results file (`dqs bench rescore`, Phase 8c).

Why: scoring rules can be corrected after a run (for example the "not attempted
became failed" bug). Re-running the model to see the corrected numbers would be
expensive and would produce a different sample. This module recomputes only the
fields that are a pure function of stored records, writes a *new* file, and
never touches the source or its markdown report.

What it cannot recover: the incident list was not stored, so detection is
preserved as recorded. Legacy records also do not store whether a fix was
attempted; a `None` `fix_attempted` is kept as unknown rather than guessed.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from dqs.bench.report import BenchRunFile, load_run_file
from dqs.bench.scoring import ScenarioRun
from dqs.errors import DqsError
from dqs.models.enums import ResolutionType

RESCORE_NOTE = (
    "Recomputed from stored records by `dqs bench rescore`; this is not a new "
    "measurement. Detection and per-record `detected` values are preserved "
    "because the incident list is not stored. A null `fix_attempted` means the "
    "source record did not record attempt provenance and is excluded from the "
    "attempt-conditioned denominator."
)


class RescoreError(DqsError):
    """A results file cannot be rescored or the output would overwrite data."""


def rescore_scenario(run: ScenarioRun) -> ScenarioRun:
    """Recompute the fields implied by the corrected scoring rules."""
    update: dict[str, object] = {}
    if run.expected_resolution == ResolutionType.PR.value:
        # A verifier pass is logically impossible without an attempt, so this
        # one inference is safe even for a legacy record. A false/None verifier
        # result stays unknown: a failed attempt and "never attempted" look the
        # same in an old file.
        if run.fix_attempted is None and run.verifier_passed is True:
            update["fix_attempted"] = True
        update["fix_delivered_valid"] = (
            run.actual_resolution == ResolutionType.PR.value and run.verifier_passed is True
        )
        update["verified_but_wrong"] = (
            run.verifier_passed is True and run.semantically_correct is False
        )
    else:
        update["fix_attempted"] = None
        update["fix_delivered_valid"] = None
        update["verified_but_wrong"] = False
    return run.model_copy(update=update)


def rescore_run(run: BenchRunFile, *, now: dt.datetime | None = None) -> BenchRunFile:
    """Return a new run document with corrected metrics; the input is unchanged."""
    moment = now or dt.datetime.now(dt.UTC)
    metadata = run.metadata.model_copy(
        update={
            "run_id": f"{run.metadata.run_id}-rescore",
            "rescore_of": run.metadata.run_id,
            "rescore_at": moment.isoformat(timespec="seconds"),
            "rescore_note": RESCORE_NOTE,
            # A derived document is never benchmark evidence, even when its
            # source was a clean run.
            "diagnostic": True,
            "diagnostic_note": "rescored document; not a new measurement",
        }
    )
    return BenchRunFile(
        metadata=metadata,
        scenarios=[rescore_scenario(result) for result in run.scenarios],
    )


def rescore_file(
    source: Path,
    destination: Path,
    *,
    now: dt.datetime | None = None,
) -> BenchRunFile:
    """Rescore `source` into `destination`; refuse to overwrite either file."""
    if destination.resolve() == source.resolve():
        raise RescoreError("refusing to overwrite the source results file")
    if destination.exists():
        raise RescoreError(f"refusing to overwrite existing results file: {destination}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RescoreError(f"could not read results file {source}: {exc}") from exc
    run = load_run_file(payload)
    rescored = rescore_run(run, now=now)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        destination.write_text(
            json.dumps(rescored.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise RescoreError(f"could not write rescored file {destination}: {exc}") from exc
    return rescored


def default_destination(source: Path) -> Path:
    """`<stem>_rescore.json`, next to the source (never the same name)."""
    return source.with_name(f"{source.stem}_rescore.json")


__all__ = [
    "RESCORE_NOTE",
    "RescoreError",
    "default_destination",
    "rescore_file",
    "rescore_run",
    "rescore_scenario",
]
