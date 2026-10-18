"""Cross-component fault models (docs/ARCHITECTURE.md §6).

Why here: fault specs travel CLI → pipeline → injectors, and ground-truth
records travel the injectors → benchmark (Phase 8). Both are structured, so
Pydantic models instead of bare dicts keeps CLI parsing errors precise and lets
the benchmark load/persist records without re-documenting their shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from dqs.models.enums import FaultKind, ResolutionType, RootCauseCategory

IDENTIFIER_MAX = 63  # Postgres identifier length cap


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FaultSpec(_Strict):
    """Parsed fault-spec string `kind:table[.column][:k=v,k=v]` (§6)."""

    kind: FaultKind
    table: str
    column: str | None = None
    params: dict[str, str | int | float] = Field(default_factory=dict)

    def format(self) -> str:
        """Back into the canonical spec string (used in logs/reports)."""
        target = self.table if self.column is None else f"{self.table}.{self.column}"
        if self.params:
            joined = ",".join(f"{k}={v}" for k, v in self.params.items())
            return f"{self.kind.value}:{target}:{joined}"
        return f"{self.kind.value}:{target}"


class GroundTruth(_Strict):
    """What a fault injection did and what should detect it (§6, benchmark §14).

    The benchmark reuses these records as expected outcomes; they are produced
    by the injectors themselves so they can never drift from the mutation.
    """

    kind: FaultKind
    table: str
    column: str | None = None
    parameters: dict[str, str | int | float] = Field(default_factory=dict)
    expected_checks: list[str]  # §7.3 check names, e.g. "nulls.rate"
    expected_category: RootCauseCategory
    expected_resolution: ResolutionType
    fix_shape: str | None = None  # §6 "expected fix shape" (PR faults only)

    @field_validator("column")
    @classmethod
    def _identifier(cls, value: str | None) -> str | None:
        if value is not None and len(value) > IDENTIFIER_MAX:
            raise ValueError("column identifier too long")
        return value
