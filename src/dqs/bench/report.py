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

from dqs.bench.scoring import Aggregates, MetricBlock, ScenarioRun, aggregate


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
    harness_version: str
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
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


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _money(value: float) -> str:
    return f"${value:.4f}"


def _seconds(value: float) -> str:
    return f"{value:.1f}s"


_METRIC_HEADER = (
    "| scope | n | detection | RC category | RC columns | resolution | "
    "fix valid | semantic | FP rate | errors | median cost | p90 cost | median wall | p90 wall |"
)
_METRIC_RULE = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"


def _metric_row(scope: str, block: MetricBlock) -> str:
    return (
        f"| {scope} | {block.scenarios} | {_percent(block.detection_recall)} | "
        f"{_percent(block.rc_category_accuracy)} | {_percent(block.rc_columns_accuracy)} | "
        f"{_percent(block.resolution_accuracy)} | {_percent(block.fix_validity_rate)} | "
        f"{_percent(block.semantic_rate)} | {_percent(block.false_positive_rate)} | "
        f"{block.errors} | {_money(block.median_cost_usd)} | {_money(block.p90_cost_usd)} | "
        f"{_seconds(block.median_wall_s)} | {_seconds(block.p90_wall_s)} |"
    )


def render_markdown(run: BenchRunFile) -> str:
    """The `latest.md` report: provenance, overall/per-kind metrics, failures."""
    meta = run.metadata
    aggregates = run.aggregates()
    lines = [
        f"# Benchmark — {meta.model}",
        "",
        f"- run: `{meta.run_id}` ({meta.split})",
        f"- provider/model: `{meta.provider}` / `{meta.model}`",
        f"- started/finished: {meta.started_at} → {meta.finished_at}",
        f"- git: `{meta.git_sha}`",
        f"- harness: `{meta.harness_version}`",
        "- prompt hashes: "
        + ", ".join(f"`{name}={sha[:12]}`" for name, sha in sorted(meta.prompt_hashes.items())),
        f"- catalogue: {meta.catalogue_size} scenarios "
        f"({meta.fault_count} faults, {meta.control_count} controls, "
        f"{meta.holdout_count} holdout)",
        f"- dbt after load: {meta.dbt_after_load}",
        f"- limits: daily cap {meta.daily_token_cap} tokens, TPM {meta.tpm_limit}, "
        f"retry max wait {meta.llm_retry_max_wait_seconds:.0f}s",
        f"- totals: {meta.tokens_total} tokens, {_money(meta.cost_usd_total)} (estimate), "
        f"{_seconds(meta.wall_s_total)}",
    ]
    if meta.prompt_changed_since_last_dev is not None:
        changed = "changed" if meta.prompt_changed_since_last_dev else "unchanged"
        previous = (
            f" (previous dev run: `{meta.previous_dev_run}`)" if meta.previous_dev_run else ""
        )
        lines.append(f"- prompts since last dev run: **{changed}**{previous}")
    if meta.stop_reason:
        lines.append(f"- **stopped early:** {meta.stop_reason}")
    if meta.resume_hint:
        lines.append(f"- resume: `{meta.resume_hint}`")
    if meta.discarded_scenarios:
        lines.append(f"- discarded: {', '.join(meta.discarded_scenarios)}")
    lines += [
        "",
        "All percentages are deterministic comparisons against the scenario answer key; "
        "cost is a token-price estimate, not a provider invoice.",
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

    failures = [run for run in run.scenarios if run.error is not None]
    misses = [
        run
        for run in run.scenarios
        if not run.clean
        and (
            run.detected is not True
            or run.rc_category_correct is not True
            or run.resolution_correct is not True
        )
    ]
    if failures or misses:
        lines += ["", "## Scenarios to look at", ""]
        for result in failures:
            lines.append(f"- `{result.scenario_id}` — error: {result.error}")
        for result in misses[:20]:
            lines.append(
                f"- `{result.scenario_id}` — detected={result.detected} "
                f"category={result.actual_category} (expected {result.expected_category}) "
                f"resolution={result.actual_resolution} (expected {result.expected_resolution})"
            )
    return "\n".join(lines) + "\n"


def load_run_file(payload: dict[str, Any]) -> BenchRunFile:
    """Validate a results JSON document (raises a Pydantic error when corrupt)."""
    return BenchRunFile.model_validate(payload)


__all__ = ["BenchMetadata", "BenchRunFile", "load_run_file", "render_markdown"]
