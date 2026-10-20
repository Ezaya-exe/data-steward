"""Table-driven tests for the pure resolution router (Phase 5b).

Every category is pinned here; changing the routing rule must change this
table on purpose, not by accident.
"""

from __future__ import annotations

import pytest

from dqs.models.enums import ResolutionType, RootCauseCategory
from dqs.resolve import MIN_PR_CONFIDENCE, PR_ELIGIBLE_CATEGORIES, plan_resolution

# All ten closed categories. At high confidence + a candidate model, only the
# three PR-eligible categories may produce a PR.
EXPECTED_AT_HIGH_CONFIDENCE = {
    RootCauseCategory.SOURCE_COLUMN_RENAMED: ResolutionType.PR,
    RootCauseCategory.SOURCE_COLUMN_REMOVED: ResolutionType.ISSUE,
    RootCauseCategory.SOURCE_TYPE_CHANGED: ResolutionType.PR,
    RootCauseCategory.UPSTREAM_NULLS_INTRODUCED: ResolutionType.ISSUE,
    RootCauseCategory.REFERENTIAL_INTEGRITY_BROKEN: ResolutionType.ISSUE,
    RootCauseCategory.DUPLICATE_RECORDS: ResolutionType.PR,
    RootCauseCategory.UNIT_OR_SCALE_CHANGE: ResolutionType.ISSUE,
    RootCauseCategory.LATE_OR_MISSING_BATCH: ResolutionType.ISSUE,
    RootCauseCategory.SOURCE_COLUMN_ADDED: ResolutionType.NONE,
    RootCauseCategory.UNKNOWN: ResolutionType.ISSUE,
}


@pytest.mark.parametrize(("category", "expected"), EXPECTED_AT_HIGH_CONFIDENCE.items())
def test_every_category_routes_as_documented(
    category: RootCauseCategory, expected: ResolutionType
) -> None:
    assert plan_resolution(category, 0.9, has_candidate_model=True) is expected


def test_the_pr_eligible_set_is_exactly_three_categories() -> None:
    assert PR_ELIGIBLE_CATEGORIES == {
        RootCauseCategory.SOURCE_COLUMN_RENAMED,
        RootCauseCategory.SOURCE_TYPE_CHANGED,
        RootCauseCategory.DUPLICATE_RECORDS,
    }


@pytest.mark.parametrize("category", sorted(PR_ELIGIBLE_CATEGORIES))
def test_confidence_boundary_is_inclusive_at_six_tenths(category: RootCauseCategory) -> None:
    assert MIN_PR_CONFIDENCE == 0.6
    assert plan_resolution(category, 0.6, has_candidate_model=True) is ResolutionType.PR
    assert plan_resolution(category, 0.599, has_candidate_model=True) is ResolutionType.ISSUE


@pytest.mark.parametrize("category", sorted(PR_ELIGIBLE_CATEGORIES))
def test_missing_candidate_model_forces_an_issue(category: RootCauseCategory) -> None:
    assert plan_resolution(category, 1.0, has_candidate_model=False) is ResolutionType.ISSUE


@pytest.mark.parametrize("category", sorted(PR_ELIGIBLE_CATEGORIES))
def test_zero_confidence_is_an_issue(category: RootCauseCategory) -> None:
    assert plan_resolution(category, 0.0, has_candidate_model=True) is ResolutionType.ISSUE


def test_source_column_added_is_none_even_at_full_confidence_with_a_model() -> None:
    assert (
        plan_resolution(RootCauseCategory.SOURCE_COLUMN_ADDED, 1.0, has_candidate_model=True)
        is ResolutionType.NONE
    )


def test_unknown_never_becomes_a_pr() -> None:
    assert (
        plan_resolution(RootCauseCategory.UNKNOWN, 1.0, has_candidate_model=True)
        is ResolutionType.ISSUE
    )


def test_out_of_range_confidence_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="confidence"):
        plan_resolution(RootCauseCategory.SOURCE_TYPE_CHANGED, 1.01, True)
    with pytest.raises(ValueError, match="confidence"):
        plan_resolution(RootCauseCategory.SOURCE_TYPE_CHANGED, -0.01, True)
