"""Provider smoke probes: does the chosen model do the four things Phase 4 needs?

Why: model selection is an empirical decision. Before an investigator prompt is
tuned, the owner needs to know that the configured endpoint can (1) answer at
all, (2) emit a typed tool call, (3) return structured output into a Pydantic
schema, and (4) carry a tool result into the next step of a small loop. The
same command runs against `fake` in CI to keep the wiring honest (AGENTS §7.9:
the fake model never produces benchmark numbers).

Each probe is independent and reports latency + tokens from `LLMClient`'s call
records. A failed probe never stops the others, so one run shows the full
picture; the CLI exits non-zero if any probe failed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field

from dqs.agents.llm import BudgetTracker, LLMClient, get_llm, redact_secrets, settings_secrets
from dqs.config import Settings, load_settings
from dqs.errors import DqsError

logger = logging.getLogger(__name__)


class SmokeAnswer(BaseModel):
    """Tiny schema for probe 3 (structured output)."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    confidence: float = Field(ge=0.0, le=1.0)


class ProbeResult(BaseModel):
    """One probe's verdict and cost."""

    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    latency_ms: int
    input_tokens: int
    output_tokens: int
    detail: str


class SmokeReport(BaseModel):
    """All four probe results for one provider/model."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    results: list[ProbeResult]

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)


@tool
def lookup(query: str) -> str:
    """Look up a deterministic fact by a short query (smoke-test tool)."""
    return f"fact for {query}"


def _text_of(response: Any) -> str:
    """Normalize LangChain message content (str or content-part list) to text."""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text", "")))
            else:
                parts.append(str(part))
        return " ".join(parts).strip()
    return str(content).strip()


def _metrics(client: LLMClient) -> tuple[int, int, int]:
    """(latency_ms, input_tokens, output_tokens) summed over the probe's calls."""
    return (
        sum(call.latency_ms for call in client.calls),
        sum(call.input_tokens for call in client.calls),
        sum(call.output_tokens for call in client.calls),
    )


def _new_client(provider: str, model: str, settings: Settings) -> LLMClient:
    # Smoke runs are deliberately outside the incident budget: they are a
    # provider check, not an investigation, and must not inherit a tiny local
    # MAX_TOOL_CALLS from the developer's shell.
    budget = BudgetTracker(max_tool_calls=50, max_tokens=2_000_000, timeout_seconds=600)
    return LLMClient(
        get_llm(provider, model, settings=settings),
        provider=provider,
        model=model,
        budget=budget,
    )


def _probe_plain(client: LLMClient) -> str:
    response = client.invoke([HumanMessage(content="Reply with the single word: ok")])
    text = _text_of(response)
    if not text:
        raise RuntimeError("plain completion returned an empty message")
    return f"content={text[:120]!r}"


def _probe_tool(client: LLMClient) -> str:
    bound = client.bind_tools([lookup])
    response = bound.invoke(
        [
            HumanMessage(
                content=(
                    "Call the lookup tool exactly once with query='alpha'. Do not answer in text."
                )
            )
        ]
    )
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        raise RuntimeError("model returned no tool call")
    call = tool_calls[0]
    if call.get("name") != lookup.name:
        raise RuntimeError(f"expected tool {lookup.name!r}, got {call.get('name')!r}")
    schema = lookup.args_schema
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        schema.model_validate(call.get("args") or {})
    return f"tool={call['name']} args={call.get('args')}"


def _probe_structured(client: LLMClient) -> str:
    structured = client.with_structured_output(SmokeAnswer)
    result = structured.invoke(
        [
            HumanMessage(
                content=(
                    "Return an answer to 'is this endpoint usable?' with a confidence "
                    "between 0 and 1."
                )
            )
        ]
    )
    if not isinstance(result, SmokeAnswer):
        raise RuntimeError(f"expected SmokeAnswer, got {type(result).__name__}")
    return f"answer={result.answer[:80]!r} confidence={result.confidence}"


def _probe_tool_loop(client: LLMClient) -> str:
    bound = client.bind_tools([lookup])
    messages: list[Any] = [
        HumanMessage(
            content=(
                "Use the lookup tool twice: first with query='alpha', then with the "
                "text the first result returned. After the second result, answer with "
                "one short sentence. Do not call the tool more than twice."
            )
        )
    ]
    tool_calls_seen = 0
    final_text = ""
    for _step in range(3):
        response = bound.invoke(messages)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            final_text = _text_of(response)
            break
        for call in tool_calls:
            tool_calls_seen += 1
            args = call.get("args") or {}
            result = lookup.invoke(args)
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
    if tool_calls_seen < 2:
        raise RuntimeError(f"expected 2 tool calls in the loop, saw {tool_calls_seen}")
    if not final_text:
        raise RuntimeError("loop ended without a final text answer")
    if len(client.calls) < 3:
        raise RuntimeError(f"expected 3 model calls, saw {len(client.calls)}")
    return f"tool_calls={tool_calls_seen} model_calls={len(client.calls)} final={final_text[:80]!r}"


_Probe = Callable[[LLMClient], str]

_PROBES: tuple[tuple[str, _Probe], ...] = (
    ("plain_completion", _probe_plain),
    ("typed_tool_call", _probe_tool),
    ("structured_output", _probe_structured),
    ("three_step_tool_loop", _probe_tool_loop),
)


def run_smoke(provider: str, model: str, *, settings: Settings | None = None) -> SmokeReport:
    """Run all probes, capturing failures as results instead of raising."""
    resolved = settings or load_settings()
    results: list[ProbeResult] = []
    for name, probe in _PROBES:
        client: LLMClient | None = None
        try:
            client = _new_client(provider, model, resolved)
            detail = probe(client)
            metrics = _metrics(client)
            results.append(
                ProbeResult(
                    name=name,
                    passed=True,
                    latency_ms=metrics[0],
                    input_tokens=metrics[1],
                    output_tokens=metrics[2],
                    detail=detail,
                )
            )
        except DqsError as exc:
            metrics = _metrics(client) if client is not None else (0, 0, 0)
            results.append(
                ProbeResult(
                    name=name,
                    passed=False,
                    latency_ms=metrics[0],
                    input_tokens=metrics[1],
                    output_tokens=metrics[2],
                    detail=redact_secrets(str(exc), secrets=settings_secrets(resolved)),
                )
            )
        except Exception as exc:  # provider/network errors are probe failures
            metrics = _metrics(client) if client is not None else (0, 0, 0)
            logger.warning("smoke probe %s failed: %s", name, exc)
            results.append(
                ProbeResult(
                    name=name,
                    passed=False,
                    latency_ms=metrics[0],
                    input_tokens=metrics[1],
                    output_tokens=metrics[2],
                    detail=redact_secrets(
                        f"{type(exc).__name__}: {exc}", secrets=settings_secrets(resolved)
                    ),
                )
            )
    return SmokeReport(provider=provider, model=model, results=results)
