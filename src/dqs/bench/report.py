"""Benchmark result files and the markdown report (Phase 8a).

Why a typed file model: a results JSON is evidence that may be quoted in the
README. Loading it through a strict Pydantic model means a corrupted or
hand-edited file fails loudly instead of producing a misleading number.

Markdown is generated from the same aggregates the CLI prints, so `latest.md`
and the console can never disagree.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dqs.bench.scoring import (
    Aggregates,
    MetricBlock,
    ScenarioRun,
    aggregate,
    wilson_interval,
)
from dqs.config import estimate_cost_usd, price_for_model

# Every dollar figure in the report is a list-price estimate, never an invoice:
# benchmark runs used provider free tiers. Keep the exact wording in one place
# so a new cost column cannot be added without the disclaimer.
_COST_LABEL = "estimated at list price; free tier used, nothing was billed"


class BenchMetadata(BaseModel):
    """Provenance for one run (`ARCHITECTURE.md` §14 integrity rule)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    model: str
    provider: str
    split: str
    started_at: str
    finished_at: str
    git_sha: str
    git_dirty: bool = False
    code_fingerprint: str = ""
    acknowledged_changes: str | None = None
    harness_version: str
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    repeats: int = 1
    dbt_after_load: bool = True
    daily_token_cap: int
    tpm_limit: int
    pause_seconds: float
    llm_retry_max_wait_seconds: float
    holdout_confirmed: bool = False
    prompt_changed_since_last_dev: bool | None = None
    previous_dev_run: str | None = None
    stop_reason: str | None = None
    resume_hint: str | None = None
    # A diagnostic run (for example a four-scenario `--ids` investigation) is
    # kept in the same file format but is never benchmark evidence.
    diagnostic: bool = False
    diagnostic_note: str | None = None
    # A rescore is derived from stored records, not a new measurement. The
    # source run id is kept so the two files can never be confused.
    rescore_of: str | None = None
    rescore_at: str | None = None
    rescore_note: str | None = None
    catalogue_size: int
    fault_count: int
    control_count: int
    holdout_count: int
    tokens_total: int = 0
    cost_usd_total: float = 0.0
    wall_s_total: float = 0.0
    discarded_scenarios: list[str] = Field(default_factory=list)


class BenchRunFile(BaseModel):
    """The complete JSON document written after every scenario."""

    model_config = ConfigDict(extra="forbid")

    metadata: BenchMetadata
    scenarios: list[ScenarioRun] = Field(default_factory=list)

    def aggregates(self) -> Aggregates:
        return aggregate(self.scenarios)


def _proportion(successes: int, n: int) -> str:
    """`rate (k/n) [95% Wilson low-high]`; n is always visible, n=0 is n/a."""
    interval = wilson_interval(successes, n)
    if interval is None:
        return "n/a (0/0)"
    low, high = interval
    return f"{successes / n * 100:.1f}% ({successes}/{n}) [{low * 100:.1f}-{high * 100:.1f}]"


def _money(value: float) -> str:
    return f"${value:.4f}"


def _rate(value: float) -> str:
    """A list price without trailing zeros (`0.15`, `0.6`, `3`)."""
    return f"${value:g}"


def _repriced_scenarios(model: str, scenarios: list[ScenarioRun]) -> list[ScenarioRun]:
    """Re-price every record from its stored tokens with the current price table.

    Costs are deliberately not trusted from the results JSON: a price-table edit
    must also update files produced before the edit (the pilot run stored 0.0
    because it used a free tier). Scoring itself is untouched; only the cost
    field on a copy is replaced.
    """
    return [
        run.model_copy(
            update={
                "cost_usd": round(
                    estimate_cost_usd(model, run.tokens_in, run.tokens_out),
                    6,
                )
            }
        )
        for run in scenarios
    ]


def _cost_totals(scenarios: list[ScenarioRun]) -> tuple[int, float]:
    """(tokens_in + tokens_out, estimated cost) over the records being reported."""
    tokens = sum(run.tokens_in + run.tokens_out for run in scenarios)
    cost = sum(run.cost_usd for run in scenarios)
    return tokens, cost


def _pricing_line(model: str) -> str:
    price = price_for_model(model)
    if price is None:
        return "- pricing: no list price configured for this model; cost columns show $0.0000"
    return f"- pricing: {_rate(price[0])}/1M input, {_rate(price[1])}/1M output - {_COST_LABEL}"


def _seconds(value: float) -> str:
    return f"{value:.1f}s"


_METRIC_HEADER = (
    "| scope | n | detection | RC category | RC columns | resolution | "
    "fix delivered (all PR) | verifier pass (attempted) | semantic (verified) | "
    "verified wrong (verified) | FP rate | infra | errors | "
    "median est. cost | p90 est. cost | median wall | p90 wall |"
)
_METRIC_RULE = (
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
)


def _metric_row(scope: str, block: MetricBlock) -> str:
    return (
        f"| {scope} | {block.scenarios} | "
        f"{_proportion(block.detection_success, block.faults)} | "
        f"{_proportion(block.category_success, block.faults)} | "
        f"{_proportion(block.columns_success, block.column_scoped)} | "
        f"{_proportion(block.resolution_success, block.faults)} | "
        f"{_proportion(block.fix_delivered_valid, block.pr_expected)} | "
        f"{_proportion(block.verified, block.fix_attempted)} | "
        f"{_proportion(block.semantic_correct, block.verified)} | "
        f"{_proportion(block.verified_but_wrong, block.verified)} | "
        f"{_proportion(block.false_positive_success, block.controls)} | "
        f"{block.infra_errors} | {block.errors} | {_money(block.median_cost_usd)} | "
        f"{_money(block.p90_cost_usd)} | {_seconds(block.median_wall_s)} | "
        f"{_seconds(block.p90_wall_s)} |"
    )


def render_markdown(run: BenchRunFile) -> str:
    """The `latest.md` report: provenance, overall/per-kind metrics, failures."""
    meta = run.metadata
    # Costs are recomputed here, not read from the file, so editing
    # `LLM_PRICE_TABLE` also reprices results written before the edit.
    priced = _repriced_scenarios(meta.model, run.scenarios)
    aggregates = aggregate(priced)
    tokens_total, cost_total = _cost_totals(priced)
    lines = [
        f"# Benchmark - {meta.model}",
        "",
        f"- run: `{meta.run_id}` ({meta.split})",
        f"- provider/model: `{meta.provider}` / `{meta.model}`",
        f"- started/finished: {meta.started_at} -> {meta.finished_at}",
        f"- git: `{meta.git_sha}`" + (" (**dirty worktree**)" if meta.git_dirty else ""),
        f"- code fingerprint: `{meta.code_fingerprint[:16] or 'unknown'}`",
        f"- harness: `{meta.harness_version}`",
        "- prompt hashes: "
        + ", ".join(f"`{name}={sha[:12]}`" for name, sha in sorted(meta.prompt_hashes.items())),
        f"- repeats: {meta.repeats}",
        f"- catalogue: {meta.catalogue_size} scenarios "
        f"({meta.fault_count} faults, {meta.control_count} controls, "
        f"{meta.holdout_count} holdout)",
        f"- dbt after load: {meta.dbt_after_load}",
        f"- limits: daily cap {meta.daily_token_cap} tokens, TPM {meta.tpm_limit}, "
        f"retry max wait {meta.llm_retry_max_wait_seconds:.0f}s",
        _pricing_line(meta.model),
        f"- totals: {tokens_total} tokens, {_money(cost_total)} - {_COST_LABEL}, "
        f"{_seconds(meta.wall_s_total)}",
    ]
    if meta.prompt_changed_since_last_dev is not None:
        changed = "changed" if meta.prompt_changed_since_last_dev else "unchanged"
        previous = (
            f" (previous dev run: `{meta.previous_dev_run}`)" if meta.previous_dev_run else ""
        )
        lines.append(f"- prompts since last dev run: **{changed}**{previous}")
    if meta.acknowledged_changes:
        lines.append(f"- acknowledged code change: {meta.acknowledged_changes}")
    if meta.stop_reason:
        lines.append(f"- **stopped early:** {meta.stop_reason}")
    if meta.resume_hint:
        lines.append(f"- resume: `{meta.resume_hint}`")
    if meta.discarded_scenarios:
        lines.append(f"- discarded: {', '.join(meta.discarded_scenarios)}")
    if meta.diagnostic:
        lines.append(
            "- **diagnostic only:** this run is not benchmark evidence"
            + (f" - {meta.diagnostic_note}" if meta.diagnostic_note else "")
        )
    if meta.rescore_of:
        lines.append(
            f"- **rescored:** derived from `{meta.rescore_of}`"
            + (f" at {meta.rescore_at}" if meta.rescore_at else "")
        )
        if meta.rescore_note:
            lines.append(f"- rescore note: {meta.rescore_note}")
    lines += [
        "",
        "All percentages are deterministic comparisons against the scenario answer key. "
        f"Every cost figure is recomputed from each record's stored input/output tokens and is "
        f"{_COST_LABEL}.",
        "Fix metrics use three explicit scopes: delivered over all PR-expected scenarios, "
        "verifier passes over attempted fixes, and semantic correctness over verified fixes. "
        "`n/a (0/0)` means the scope is empty, not zero.",
        "",
        "## Overall",
        "",
        _METRIC_HEADER,
        _METRIC_RULE,
        _metric_row("overall", aggregates.overall),
        "",
        "## By fault kind",
        "",
        _METRIC_HEADER,
        _METRIC_RULE,
        *(_metric_row(kind, block) for kind, block in aggregates.by_kind.items()),
    ]

    infra = [run for run in run.scenarios if run.infra_error is not None]
    if infra:
        lines += [
            "",
            f"## Infrastructure errors ({len(infra)})",
            "",
            "Provider rate-limit/quota failures. They are excluded from every score "
            "denominator and `--resume` re-runs them automatically.",
            "",
        ]
        for result in infra:
            lines.append(
                f"- `{result.scenario_id}` repeat {result.repeat + 1}: {result.infra_error}"
            )

    repeated = [run for run in run.scenarios if run.repeat > 0]
    if repeated:
        lines += [
            "",
            "## Repeat stability",
            "",
            "| scenario | repeats | stability |",
            "|---|---:|---|",
        ]
        by_scenario: dict[str, list[ScenarioRun]] = {}
        for result in run.scenarios:
            by_scenario.setdefault(result.scenario_id, []).append(result)
        for scenario_id, results in sorted(by_scenario.items()):
            if max(result.repeat for result in results) < 1:
                continue
            scored = [result for result in results if result.infra_error is None]
            if len(scored) < 2:
                stability = "n/a (infra errors)"
            else:
                outcomes = {_outcome_key(result) for result in scored}
                stability = "stable" if len(outcomes) == 1 else "flaky"
            lines.append(f"| `{scenario_id}` | {len(results)} | {stability} |")

    failures = [run for run in run.scenarios if run.error is not None]
    misses = [
        run
        for run in run.scenarios
        if not run.clean
        and run.infra_error is None
        and (
            run.detected is not True
            or run.rc_category_correct is not True
            or run.resolution_correct is not True
        )
    ]
    if failures or misses:
        lines += ["", "## Scenarios to look at", ""]
        for result in failures:
            lines.append(f"- `{result.scenario_id}` - error: {result.error}")
        for result in misses[:20]:
            lines.append(
                f"- `{result.scenario_id}` - detected={result.detected} "
                f"category={result.actual_category} (expected {result.expected_category}) "
                f"resolution={result.actual_resolution} (expected {result.expected_resolution})"
            )
    return "\n".join(lines) + "\n"


def _outcome_key(result: ScenarioRun) -> tuple[object, ...]:
    """The scored outcome of one repeat; identical keys mean a stable scenario."""
    return (
        result.detected,
        result.rc_category_correct,
        result.rc_columns_correct,
        result.resolution_correct,
        result.verifier_passed,
        result.fix_attempted,
        result.fix_delivered_valid,
        result.semantically_correct,
        result.false_positive,
        result.error is not None,
    )


def load_run_file(payload: dict[str, Any]) -> BenchRunFile:
    """Validate a results JSON document (raises a Pydantic error when corrupt)."""
    return BenchRunFile.model_validate(payload)


__all__ = ["BenchMetadata", "BenchRunFile", "load_run_file", "render_markdown"]
