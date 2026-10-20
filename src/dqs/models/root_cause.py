"""Root-cause output of the investigator (docs/ARCHITECTURE.md §10.3).

Why a dedicated model: this is the hand-off between the reasoning node and the
deterministic rest of the workflow (impact, fix planning, publishing). A strict
Pydantic schema means a model response either becomes a typed `RootCause` or
is rejected, and every field the later nodes rely on has one fixed shape.

`EvidenceItem.tool` is deliberately a tool name, not free text: the
investigator is required to cite only tools it actually called in this run, so
a fabricated citation is detectable and can be dropped before anything is
stored or published.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from dqs.models.enums import ResolutionType, RootCauseCategory


class EvidenceItem(BaseModel):
    """One observed fact, attributed to the tool call that produced it."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1, description="Exact name of a tool called in this run.")
    finding: str = Field(min_length=1, description="What that tool result showed.")


class RootCause(BaseModel):
    """The investigator's structured conclusion (§10.3)."""

    model_config = ConfigDict(extra="forbid")

    category: RootCauseCategory
    summary: str = Field(min_length=1)
    affected_table: str = ""
    affected_columns: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_resolution: ResolutionType
    hypotheses_rejected: list[str] = Field(default_factory=list)


def unknown_root_cause(summary: str) -> RootCause:
    """The safe escalation used when a budget runs out or evidence is unusable."""
    return RootCause(
        category=RootCauseCategory.UNKNOWN,
        summary=summary,
        affected_table="",
        affected_columns=[],
        evidence=[],
        confidence=0.0,
        suggested_resolution=ResolutionType.ISSUE,
        hypotheses_rejected=[],
    )
