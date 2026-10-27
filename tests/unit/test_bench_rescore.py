"""Offline rescore tests (`dqs bench rescore`, Phase 8c).

The source file must never be modified, the output must be a new file, and a
legacy `verifier_passed=False` must stay unknown when attempt provenance was
never recorded.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from dqs.bench.report import BenchMetadata, BenchRunFile
from dqs.bench.rescore import (
    RescoreError,
    default_destination,
    rescore_file,
    rescore_run,
    rescore_scenario,
)
from dqs.bench.scoring import ScenarioRun, aggregate

NOW = dt.datetime(2026, 10, 27, 12, 0, tzinfo=dt.UTC)


def _metadata(**overrides: object) -> BenchMetadata:
    payload: dict[str, object] = {
        "run_id": "20261026T181316Z",
        "model": "test-model",
        "provider": "openai_compatible",
        "split": "dev",
        "started_at": "2026-10-26T18:13:16+00:00",
        "finished_at": "2026-10-26T19:14:32+00:00",
        "git_sha": "deadbeef",
        "harness_version": "8b.1",
        "prompt_hashes": {},
        "daily_token_cap": 180_000,
        "tpm_limit": 8_000,
        "pause_seconds": 1.0,
        "llm_retry_max_wait_seconds": 90.0,
        "catalogue_size": 13,
        "fault_count": 13,
        "control_count": 0,
        "holdout_count": 0,
    }
    payload.update(overrides)
    return BenchMetadata.model_validate(payload)


def _run(
    scenario_id: str,
    *,
    verifier: bool | None,
    resolution: str = "ISSUE",
    semantic: bool | None = None,
    fix_attempted: bool | None = None,
) -> ScenarioRun:
    return ScenarioRun(
        scenario_id=scenario_id,
        split="dev",
        kind="duplicate_rows",
        clean=False,
        detected=True,
        rc_category_correct=True,
        rc_columns_correct=None,
        resolution_correct=resolution == "PR",
        verifier_passed=verifier,
        semantically_correct=semantic,
        expected_resolution="PR",
        actual_resolution=resolution,
        fix_attempted=fix_attempted,
        verified_but_wrong=False,
    )


def _run_file(scenarios: list[ScenarioRun]) -> BenchRunFile:
    return BenchRunFile(metadata=_metadata(), scenarios=scenarios)


def test_verified_legacy_record_gets_an_attempt_and_a_delivered_fix() -> None:
    rescored = rescore_scenario(
        _run("f6-order-items-03", verifier=True, resolution="PR", semantic=True)
    )
    assert rescored.fix_attempted is True
    assert rescored.fix_delivered_valid is True
    assert rescored.detected is True  # never recomputed without incidents


def test_unknown_legacy_attempt_is_not_coerced_to_false() -> None:
    rescored = rescore_scenario(_run("f6-orders-03", verifier=False, resolution="ISSUE"))
    assert rescored.fix_attempted is None
    assert rescored.fix_delivered_valid is False
    overall = aggregate([rescored]).overall
    assert overall.fix_attempted == 0
    assert overall.verifier_pass_given_attempt is None
    assert overall.fix_delivered_valid_rate == 0.0


def test_rescore_run_marks_itself_derived_and_keeps_the_source_id() -> None:
    run = _run_file([_run("f6-order-items-03", verifier=True, resolution="PR", semantic=True)])
    rescored = rescore_run(run, now=NOW)
    assert rescored.metadata.run_id == "20261026T181316Z-rescore"
    assert rescored.metadata.rescore_of == "20261026T181316Z"
    assert rescored.metadata.rescore_at == NOW.isoformat(timespec="seconds")
    assert rescored.metadata.diagnostic is True
    assert rescored.metadata.rescore_note
    assert run.metadata.run_id == "20261026T181316Z"  # input untouched


def test_rescore_file_writes_a_new_file_and_never_overwrites(
    tmp_path: Path,
) -> None:
    source = tmp_path / "20261026T181316Z_test-model.json"
    source.write_text(
        json.dumps(
            _run_file(
                [_run("f6-order-items-03", verifier=True, resolution="PR", semantic=True)]
            ).model_dump(mode="json")
        ),
        encoding="utf-8",
    )
    before = source.read_text(encoding="utf-8")
    destination = default_destination(source)
    rescored = rescore_file(source, destination, now=NOW)

    assert source.read_text(encoding="utf-8") == before
    assert destination.exists()
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["metadata"]["rescore_of"] == "20261026T181316Z"
    assert payload["scenarios"][0]["fix_delivered_valid"] is True
    assert rescored.metadata.diagnostic is True

    with pytest.raises(RescoreError, match="overwrite"):
        rescore_file(source, destination, now=NOW)


def test_rescore_refuses_to_write_over_the_source(tmp_path: Path) -> None:
    source = tmp_path / "run.json"
    source.write_text(json.dumps(_run_file([]).model_dump(mode="json")), encoding="utf-8")
    with pytest.raises(RescoreError, match="source"):
        rescore_file(source, source, now=NOW)


def test_rescore_reports_a_missing_or_corrupt_source(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(RescoreError, match="could not read"):
        rescore_file(missing, tmp_path / "out.json", now=NOW)

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    with pytest.raises(RescoreError, match="could not read"):
        rescore_file(corrupt, tmp_path / "out.json", now=NOW)
