"""Deterministic resolution routing (docs/ARCHITECTURE.md §6, §10.2).

Why here: whether an incident becomes a PR, an issue, or nothing is a product
rule, not a judgement call. Keeping it a pure function means the same inputs
always produce the same publication type, the rule is exhaustively testable,
and no LLM can talk the workflow into opening a code change for a category
that has no code fix.

The PR rule is deliberately narrow: only the three categories whose fix lives
in a staging model, only at confidence >= 0.6, and only when lineage actually
found a candidate staging model. Everything else falls back to ISSUE; warning
drift (`SOURCE_COLUMN_ADDED`) needs no action at all.
"""

from __future__ import annotations

from dqs.models.enums import ResolutionType, RootCauseCategory

MIN_PR_CONFIDENCE: float = 0.6

# Categories a staging-model edit can genuinely fix. The investigator prompt
# names the same set; this constant is the enforcement point.
PR_ELIGIBLE_CATEGORIES: frozenset[RootCauseCategory] = frozenset(
    {
        RootCauseCategory.SOURCE_COLUMN_RENAMED,
        RootCauseCategory.SOURCE_TYPE_CHANGED,
        RootCauseCategory.DUPLICATE_RECORDS,
    }
)


def plan_resolution(
    category: RootCauseCategory,
    confidence: float,
    has_candidate_model: bool,
) -> ResolutionType:
    """Choose PR / ISSUE / NONE for one root cause.

    Boundary: confidence exactly ``0.6`` is sufficient (the rule says "at
    least 0.6"). A missing candidate model always means ISSUE: a PR without a
    file to edit would be an empty promise.
    """
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be within [0, 1]")
    if category is RootCauseCategory.SOURCE_COLUMN_ADDED:
        return ResolutionType.NONE
    if (
        category in PR_ELIGIBLE_CATEGORIES
        and confidence >= MIN_PR_CONFIDENCE
        and has_candidate_model
    ):
        return ResolutionType.PR
    return ResolutionType.ISSUE
