"""Benchmark results-file and markdown report tests (Phase 8a).

The results JSON is evidence: it must round-trip through a strict model and the
generated markdown must show the same aggregates. A corrupt file must fail
loudly, never silently render an empty table.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dqs.bench.report import BenchMetadata, BenchRunFile, load_run_file, render_markdown
from dqs.bench.scoring import ScenarioRun


def _metadata(**overrides: object) -> BenchMetadata:
    payload: dict[str, object] = {
        "run_id": "20260101T000000Z",
        "model": "test-model",
        "provider": "anthropic",
        "split": "dev",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:10:00+00:00",
        "git_sha": "deadbeef",
        "harness_version": "8a.1",
        "prompt_hashes": {"investigator": "a" * 64, "fixer": "b" * 64},
        "daily_token_cap": 180_000,
        "tpm_limit": 8_000,
        "pause_seconds": 1.0,
        "llm_retry_max_wait_seconds": 90.0,
        "catalogue_size": 2,
        "fault_count": 1,
        "control_count": 1,
        "holdout_count": 0,
    }
    payload.update(overrides)
    return BenchMetadata.model_validate(payload)


def _run_file(**overrides: object) -> BenchRunFile:
    scenarios = [
        ScenarioRun(
            scenario_id="f1-orders-status",
            split="dev",
            kind="column_rename",
            clean=False,
            detected=True,
            rc_category_correct=False,
            rc_columns_correct=True,
            resolution_correct=True,
            verifier_passed=True,
            semantically_correct=False,
            verified_but_wrong=True,
            expected_category="SOURCE_COLUMN_RENAMED",
            actual_category="UNKNOWN",
            expected_resolution="PR",
            actual_resolution="PR",
            cost_usd=0.25,
            wall_s=12.0,
            tokens_in=1000,
            tokens_out=100,
        ),
        ScenarioRun(
            scenario_id="clean-01",
            split="dev",
            kind="clean",
            clean=True,
            false_positive=False,
            cost_usd=0.0,
            wall_s=3.0,
        ),
    ]
    return BenchRunFile(metadata=_metadata(**overrides), scenarios=scenarios)


def test_results_file_round_trips_through_the_strict_model() -> None:
    run = _run_file()
    payload = run.model_dump(mode="json")
    loaded = load_run_file(payload)
    assert loaded == run
    assert loaded.aggregates().overall.scenarios == 2
    assert loaded.aggregates().overall.verified_but_wrong == 1


def test_corrupt_results_file_is_rejected() -> None:
    payload = _run_file().model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        load_run_file(payload)
    with pytest.raises(ValidationError):
        load_run_file({"metadata": {}})


def test_markdown_contains_provenance_overall_and_per_kind_metrics() -> None:
    run = _run_file(
        stop_reason="daily token cap reached", discarded_scenarios=["f4-orders-status-20"]
    )
    markdown = render_markdown(run)
    assert "# Benchmark - test-model" in markdown
    assert "`deadbeef`" in markdown
    assert "**stopped early:** daily token cap reached" in markdown
    assert "discarded: f4-orders-status-20" in markdown
    assert "## Overall" in markdown
    assert "## By fault kind" in markdown
    assert "| column_rename |" in markdown
    assert "| clean |" in markdown
    # verified-but-wrong is visible through the semantic column and miss list.
    assert "0.0%" in markdown
    assert "`f1-orders-status`" in markdown
    assert "category=UNKNOWN (expected SOURCE_COLUMN_RENAMED)" in markdown
    assert markdown.endswith("\n")


def test_markdown_reports_prompt_change_and_resume_hint() -> None:
    run = _run_file(
        prompt_changed_since_last_dev=True,
        previous_dev_run="20260101T000000Z",
        resume_hint="dqs bench run --split dev --model test-model --resume out.json",
    )
    markdown = render_markdown(run)
    assert "prompts since last dev run: **changed**" in markdown
    assert "previous dev run: `20260101T000000Z`" in markdown
    assert "resume: `dqs bench run" in markdown


def test_markdown_handles_a_run_with_no_scenarios() -> None:
    run = BenchRunFile(metadata=_metadata(), scenarios=[])
    markdown = render_markdown(run)
    assert "| overall | 0 |" in markdown
    assert "n/a" in markdown


def test_markdown_is_ascii_safe_for_windows_consoles() -> None:
    """`dqs bench report` writes to a cp1252 console; no Unicode can crash it."""
    run = _run_file(stop_reason="daily quota", discarded_scenarios=["f6-orders-03"])
    markdown = render_markdown(run)
    markdown.encode("ascii")  # raises UnicodeEncodeError if a glyph sneaks back in
