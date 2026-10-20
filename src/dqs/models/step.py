"""One row of an agent's step timeline (`dq.agent_steps`, §4.3).

Why a model: the step log crosses the DB, the CLI printer and (in Phase 9) an
exporter. A typed record keeps `kind`, counters and JSON payloads consistent
everywhere; the sink owns `seq` so callers cannot write a non-monotonic
timeline.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StepKind = Literal["llm", "tool", "system"]


class StepRecord(BaseModel):
    """One logged LLM call, tool call or system event."""

    model_config = ConfigDict(extra="forbid")

    incident_id: uuid.UUID | None = None
    seq: int = Field(ge=1)
    node: str = Field(min_length=1)
    kind: StepKind
    name: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
