"""Violation model — one failed check on one table/column (docs/ARCHITECTURE.md §7.3).

Why a dedicated model: violations are the hand-off between the deterministic
check engine (Phase 2b), incident persistence (`dq.incidents.violations` jsonb)
and every later agent prompt. A strict Pydantic model keeps that JSON shape
stable across all three, so a stored violation always re-validates.

Two fields extend §7.3's list, both for evidence/dedup reasons and logged in
docs/DECISIONS.md:

- `batch_id` — a violation is produced per batch; keeping the producing batch
  on the violation is what lets one de-duplicated incident show a growing batch
  list (§4.3 "append the new batch to it").
- `table` is optional because `freshness.sla` and `monitor.missing_batch` are
  batch-level checks with no table (`dq.incidents.table_name` is nullable too).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dqs.models.enums import Severity


class Violation(BaseModel):
    """One check that failed, with the evidence a human or agent needs (§7.3)."""

    model_config = ConfigDict(extra="forbid")

    check: str  # §7.3 check name, e.g. "nulls.rate"
    severity: Severity
    table: str | None = None  # qualified, e.g. "raw.orders"; None for batch-level
    column: str | None = None
    batch_id: str | None = None  # producing batch; None for batch-level checks
    message: str
    observed: dict[str, Any] = Field(default_factory=dict)
    expected: dict[str, Any] = Field(default_factory=dict)
    sample: list[str] = Field(default_factory=list)  # up to 5 offending values/rows
