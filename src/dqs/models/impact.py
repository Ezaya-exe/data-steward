"""ImpactReport model — the deterministic blast-radius analysis (§9, §10.4).

Why: it is the artifact every later phase consumes (`assess_impact` in the
LangGraph workflow attaches it to the incident; the UI prints it), and it must
be byte-stable for the same graph + incident + stored run record. Two fields
come from outside the deterministic computation, both by documented decision:

- ``summary`` — template-rendered in Phase 3; Phase 4 replaces the renderer
  with ONE LLM call that may fail, falling back to the same template.
- ``failing_tests`` — comes from the stored `dq.pipeline_runs.failed_nodes`
  record (parsed at build time, DECISIONS 55); never from the live
  run_results.json file, which later builds overwrite.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from dqs.models.enums import ImpactScope
from dqs.models.pipeline import NodeResult


class AffectedModel(BaseModel):
    """One downstream model hit by the incident."""

    model_config = ConfigDict(extra="forbid")

    unique_id: str
    name: str
    layer: str  # staging | marts | unknown (from original_file_path)
    distance: int  # graph hops from the affected raw source (≥1)
    referencing_column: bool = False  # column-level lineage hit (heuristic)


class AffectedExposure(BaseModel):
    """One downstream exposure (dashboard) hit by the incident."""

    model_config = ConfigDict(extra="forbid")

    unique_id: str
    name: str
    owner: str
    maturity: str  # high | medium | low | unknown


class ImpactReport(BaseModel):
    """Deterministic blast radius of one incident (§9)."""

    model_config = ConfigDict(extra="forbid")

    incident_id: uuid.UUID
    source_table: str | None = None  # affected raw source; None = warehouse scope
    column: str | None = None
    scope: ImpactScope
    affected_models: list[AffectedModel] = Field(default_factory=list)
    failing_tests: list[NodeResult] = Field(default_factory=list)
    affected_exposures: list[AffectedExposure] = Field(default_factory=list)
    blast_radius_score: float = 0.0
    summary: str = ""
