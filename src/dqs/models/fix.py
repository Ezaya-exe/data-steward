"""Fix-plan and verification models (docs/ARCHITECTURE.md §10.5, §11).

Why here: a fix is data before it is an action. The planner (Phase 5b) emits a
`FixPlan`; deterministic code validates and applies it; verification returns a
`VerificationResult`. Keeping both as strict Pydantic models means a plan from
an LLM and a plan read from disk go through exactly the same limits, and an
attempt can be persisted to `dq.fix_attempts` without ad-hoc JSON shaping.

The limits live on the model, not in the prompt: at most `MAX_EDITS` exact-match
edits and at most `MAX_NEW_BYTES` bytes of replacement text per plan. A plan
that breaks them cannot even be constructed, so no later code path has to
remember the rule.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dqs.models.enums import RootCauseCategory

# Product limits (owner decision, Phase 5a). Kept here so the model and the
# verifier cannot drift apart.
MAX_EDITS: int = 3
MAX_NEW_BYTES: int = 4096
MAX_LOG_EXCERPT_CHARS: int = 4096
IDENTIFIER_RE: str = r"^[a-z_][a-z0-9_]*$"


class _Strict(BaseModel):
    """Unknown keys are errors: a typo must not silently drop a field."""

    model_config = ConfigDict(extra="forbid")


class Edit(_Strict):
    """One exact-match replacement inside one dbt SQL model.

    `path` is project-relative and is re-validated by `dqs.verify.patch`
    (allowlist, traversal, symlinks). `old` must occur exactly once; `new` is
    the replacement text and may be empty (a deterministic deletion).
    """

    path: str = Field(min_length=1)
    old: str = Field(min_length=1)
    new: str = ""

    @field_validator("path")
    @classmethod
    def _no_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("edit path must not contain a NUL byte")
        if value != value.strip():
            raise ValueError("edit path must not have leading/trailing whitespace")
        return value

    @model_validator(mode="after")
    def _changes_something(self) -> Edit:
        if self.old == self.new:
            raise ValueError("edit is a no-op: 'old' and 'new' are identical")
        return self


class FixPlan(_Strict):
    """A proposed deterministic patch plus the context verification needs.

    The §10.5 fields (`summary`, `edits`, `rationale`, `expected_effect`,
    `risk`) are extended with the incident facts that cannot be guessed from
    the patch alone: the faulted batch, the incident's closed root-cause
    category, and the affected raw table/columns. Verification keys its
    targeted check and relevance rule off those fields.
    """

    incident_id: uuid.UUID | None = None
    batch_id: str | None = None
    category: RootCauseCategory
    affected_table: str
    affected_columns: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    edits: list[Edit] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    expected_effect: str = Field(min_length=1)
    risk: str = Field(min_length=1)

    @field_validator("batch_id")
    @classmethod
    def _batch_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("batch_") or not value[6:].isdigit() or len(value) != 14:
            raise ValueError("batch_id must look like 'batch_YYYYMMDD'")
        return value

    @field_validator("affected_table")
    @classmethod
    def _raw_table(cls, value: str) -> str:
        schema, _, table = value.partition(".")
        if schema != "raw" or not _is_identifier(table):
            raise ValueError("affected_table must be a raw table like 'raw.orders'")
        return value

    @field_validator("affected_columns")
    @classmethod
    def _columns(cls, value: list[str]) -> list[str]:
        for column in value:
            if not _is_identifier(column):
                raise ValueError(f"affected column {column!r} is not a safe identifier")
        return value

    @model_validator(mode="after")
    def _limits(self) -> FixPlan:
        if len(self.edits) > MAX_EDITS:
            raise ValueError(f"a plan may contain at most {MAX_EDITS} edits")
        new_bytes = sum(len(edit.new.encode("utf-8")) for edit in self.edits)
        if new_bytes > MAX_NEW_BYTES:
            raise ValueError(
                f"replacement text is {new_bytes} bytes; the plan limit is {MAX_NEW_BYTES}"
            )
        return self


class CriterionResult(_Strict):
    """One pass/fail check with the evidence needed to explain a rejection."""

    name: str = Field(min_length=1)
    passed: bool
    detail: str = ""
    observed: dict[str, Any] = Field(default_factory=dict)


class VerificationResult(_Strict):
    """The deterministic verdict for one applied fix (§11 step 6)."""

    passed: bool
    failed_criteria: list[str] = Field(default_factory=list)
    criteria: list[CriterionResult] = Field(default_factory=list)
    dbt_log_excerpt: str = ""
    row_counts: dict[str, int] = Field(default_factory=dict)
    duration_s: float = 0.0
    sandbox_schema: str = ""
    dbt_exit_code: int | None = None
    contract_tables: list[str] = Field(default_factory=list)

    @field_validator("failed_criteria")
    @classmethod
    def _unique_failures(cls, value: list[str]) -> list[str]:
        # Stable de-duplication: the UI shows one row per failed criterion.
        return list(dict.fromkeys(value))

    @field_validator("dbt_log_excerpt")
    @classmethod
    def _bounded_excerpt(cls, value: str) -> str:
        if len(value) <= MAX_LOG_EXCERPT_CHARS:
            return value
        return value[-MAX_LOG_EXCERPT_CHARS:]

    @field_validator("duration_s")
    @classmethod
    def _non_negative(cls, value: float) -> float:
        return max(0.0, value)

    @model_validator(mode="after")
    def _consistent(self) -> VerificationResult:
        if self.passed and self.failed_criteria:
            raise ValueError("a passed verification cannot have failed criteria")
        if not self.passed and not self.failed_criteria:
            raise ValueError("a failed verification must name at least one failed criterion")
        return self

    @classmethod
    def from_criteria(
        cls,
        criteria: list[CriterionResult],
        **kwargs: Any,
    ) -> VerificationResult:
        """Build the verdict from criteria, deriving `passed` and failures."""
        failed = [criterion.name for criterion in criteria if not criterion.passed]
        return cls(
            passed=not failed,
            failed_criteria=failed,
            criteria=criteria,
            **kwargs,
        )


def _is_identifier(value: str) -> bool:
    return re.match(IDENTIFIER_RE, value) is not None
