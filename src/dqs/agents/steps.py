"""Step log for one agent node (`dq.agent_steps`, §4.3).

Why: an LLM investigation is otherwise a black box. Every model call and tool
call is written with its arguments, a truncated output, tokens and latency, so
a human can reconstruct the timeline after the fact (and Phase 9 can export
it). The sink is an injected dependency: unit tests use the in-memory sink,
the CLI uses the Postgres sink with the read/write engine.

The sink assigns `seq` itself so callers cannot accidentally write an
out-of-order timeline. Secrets are redacted recursively before persistence;
tool outputs are already ≤4 KB by the data envelope, and any other oversized
payload is replaced with a valid `{"truncated": true, "preview": …}` object.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import Engine, text

from dqs.agents.llm import redact_secrets, settings_secrets
from dqs.config import Settings, load_settings
from dqs.models.step import StepKind, StepRecord
from dqs.tools.base import to_jsonable

logger = logging.getLogger(__name__)

MAX_STEP_BYTES = 4096


def _redact(value: Any, secrets: list[str]) -> Any:
    """Redact strings inside a JSON-like structure (keys are structural)."""
    if isinstance(value, str):
        return redact_secrets(value, secrets=secrets)
    if isinstance(value, dict):
        return {str(key): _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    return value


def prepare_payload(payload: Any, *, secrets: list[str]) -> Any:
    """Make a step payload JSON-safe, redacted and bounded."""
    cleaned = _redact(to_jsonable(payload), secrets)
    encoded = json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
    if len(encoded.encode("utf-8")) <= MAX_STEP_BYTES:
        return cleaned
    preview = encoded[: MAX_STEP_BYTES - 64]
    return {"truncated": True, "preview": preview}


class StepSink:
    """Base sink: assigns sequence numbers, retains records, delegates writes."""

    def __init__(
        self,
        *,
        node: str,
        incident_id: uuid.UUID | None = None,
        start_seq: int = 1,
        settings: Settings | None = None,
    ) -> None:
        self.node = node
        self.incident_id = incident_id
        self.records: list[StepRecord] = []
        self._next_seq = max(1, int(start_seq))
        resolved = settings or load_settings()
        self._secrets = settings_secrets(resolved)

    def record(
        self,
        *,
        kind: StepKind,
        name: str,
        input: Any = None,  # noqa: A002 - matches the DB column name
        output: Any = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: int = 0,
    ) -> StepRecord:
        """Record one step and return the stored object."""
        step = StepRecord(
            incident_id=self.incident_id,
            seq=self._next_seq,
            node=self.node,
            kind=kind,
            name=name,
            input=prepare_payload(input or {}, secrets=self._secrets),
            output=prepare_payload(output or {}, secrets=self._secrets),
            tokens_in=max(0, int(tokens_in)),
            tokens_out=max(0, int(tokens_out)),
            latency_ms=max(0, int(latency_ms)),
        )
        self._next_seq += 1
        self.records.append(step)
        self.write(step)
        return step

    def write(self, step: StepRecord) -> None:
        """Persist one step (overridden by the DB sink)."""
        raise NotImplementedError


class MemoryStepSink(StepSink):
    """In-memory sink for unit tests and dry runs; writes nothing."""

    def write(self, step: StepRecord) -> None:
        _ = step


class DbStepSink(StepSink):
    """Postgres sink for `dq.agent_steps` (writes on the read/write engine)."""

    def __init__(
        self,
        engine: Engine,
        *,
        node: str,
        incident_id: uuid.UUID | None,
        settings: Settings | None = None,
    ) -> None:
        self.engine = engine
        start_seq = self._latest_seq(engine, incident_id, node)
        super().__init__(
            node=node,
            incident_id=incident_id,
            start_seq=start_seq + 1,
            settings=settings,
        )

    @staticmethod
    def _latest_seq(engine: Engine, incident_id: uuid.UUID | None, node: str) -> int:
        if incident_id is None:
            return 0
        with engine.connect() as conn:
            value: int | None = conn.execute(
                text(
                    "SELECT COALESCE(MAX(seq), 0) FROM dq.agent_steps "
                    "WHERE incident_id = :id AND node = :node"
                ),
                {"id": incident_id, "node": node},
            ).scalar_one()
        return int(value or 0)

    def write(self, step: StepRecord) -> None:
        payload = {
            "id": self.incident_id,
            "seq": step.seq,
            "node": step.node,
            "kind": step.kind,
            "name": step.name,
            "input": json.dumps(step.input, ensure_ascii=False, sort_keys=True),
            "output": json.dumps(step.output, ensure_ascii=False, sort_keys=True),
            "tokens_in": step.tokens_in,
            "tokens_out": step.tokens_out,
            "latency_ms": step.latency_ms,
        }
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO dq.agent_steps "
                    "(incident_id, seq, node, kind, name, input, output, "
                    "tokens_in, tokens_out, latency_ms) VALUES "
                    "(:id, :seq, :node, :kind, :name, CAST(:input AS jsonb), "
                    "CAST(:output AS jsonb), :tokens_in, :tokens_out, :latency_ms)"
                ),
                payload,
            )
        logger.debug(
            "agent step incident=%s seq=%s kind=%s name=%s",
            self.incident_id,
            step.seq,
            step.kind,
            step.name,
        )
