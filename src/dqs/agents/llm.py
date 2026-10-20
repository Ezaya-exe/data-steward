"""The one place an LLM is constructed, called, measured, and budgeted.

Why here: AGENTS.md §7.1 says the LLM never executes anything; it is a
structured reasoner behind a typed wrapper. This module keeps that boundary
small and auditable:

* `get_llm(provider, model)` is the only `init_chat_model` call. Provider
  support is explicit (including OpenAI-compatible endpoints for DeepSeek,
  Kimi, GLM and OpenRouter) and the installed LangChain version's signature is
  the one used here (verified against langchain 1.4.6 / langchain-core 1.6.12).
* `LLMClient` records token usage and wall-clock latency for every call. A
  failed call is recorded too, with the provider error text redacted.
* `BudgetTracker` enforces the documented hard limits (tool calls, tokens,
  wall-clock) and raises `BudgetExceeded` instead of letting a loop run away.
* `FakeChatModel` exists for wiring tests only (AGENTS.md §7.9); it is
  deterministic and makes no network calls.

Secrets are read from `Settings` only and never logged. `redact_secrets` is
used on any provider error that may echo a key back.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from collections.abc import Callable, Sequence
from email.utils import parsedate_to_datetime
from typing import Any, Final, cast

from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from dqs.config import LLM_PROVIDERS, Settings, load_settings
from dqs.errors import BudgetExceeded, ConfigError, LLMError

logger = logging.getLogger(__name__)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{10,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|password)\s*[=:]\s*)\S+"),
)


def redact_secrets(text: str, *, secrets: Sequence[str] = ()) -> str:
    """Remove known secret values and common token shapes from log/error text.

    Explicit `secrets` (values from `Settings`) are replaced first; the
    patterns then cover provider errors that quote a key we did not know about.
    """
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "***redacted***")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda match: f"{match.group(1)}***redacted***", redacted)
        else:
            redacted = pattern.sub("***redacted***", redacted)
    return redacted


def settings_secrets(settings: Settings) -> list[str]:
    """Secret values to redact from logs/errors (never log this list itself)."""
    return [
        settings.anthropic_api_key,
        settings.google_api_key,
        settings.openai_api_key,
        settings.llm_api_key,
        settings.github_token,
    ]


# --- budget ---------------------------------------------------------------------------


class BudgetState(BaseModel):
    """A snapshot of the hard limits and current consumption."""

    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int
    max_tokens: int
    timeout_seconds: int
    tool_calls: int
    tokens: int
    elapsed_seconds: float
    tool_calls_remaining: int
    tokens_remaining: int
    seconds_remaining: float


class BudgetTracker:
    """Enforce `MAX_TOOL_CALLS`, token budget and wall-clock per incident.

    The clock is injectable so timeout tests are deterministic (AGENTS.md §8:
    tests seed all randomness/time where possible). `check()` is called after
    every recorded event and may also be called by the future investigator loop
    before each step.
    """

    def __init__(
        self,
        *,
        max_tool_calls: int = 10,
        max_tokens: int = 60000,
        timeout_seconds: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_tool_calls < 0 or max_tokens < 0 or timeout_seconds < 1:
            raise ValueError("budget limits must be non-negative (timeout > 0)")
        self.max_tool_calls = max_tool_calls
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._started_at = clock()
        self._tool_calls = 0
        self._tokens = 0
        self._llm_calls = 0

    @classmethod
    def from_settings(cls, settings: Settings) -> BudgetTracker:
        """Build from the documented env-backed limits (ARCHITECTURE §3)."""
        return cls(
            max_tool_calls=settings.max_tool_calls,
            max_tokens=settings.max_tokens_per_incident,
            timeout_seconds=settings.incident_timeout_seconds,
        )

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self._clock() - self._started_at)

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    @property
    def tokens(self) -> int:
        return self._tokens

    @property
    def llm_calls(self) -> int:
        return self._llm_calls

    def record_tool_call(self) -> None:
        """Count one tool invocation, then enforce all limits."""
        self._tool_calls += 1
        self.check()

    def record_llm_call(self, record: LLMCallRecord) -> None:
        """Count one model call's tokens, then enforce all limits."""
        self._llm_calls += 1
        self._tokens += max(0, record.total_tokens)
        self.check()

    def check(self) -> None:
        """Raise `BudgetExceeded` when any hard limit is exceeded."""
        if self._tool_calls > self.max_tool_calls:
            raise BudgetExceeded(
                f"tool-call budget exceeded: {self._tool_calls} > {self.max_tool_calls} "
                "(MAX_TOOL_CALLS)"
            )
        if self._tokens > self.max_tokens:
            raise BudgetExceeded(
                f"token budget exceeded: {self._tokens} > {self.max_tokens} "
                "(MAX_TOKENS_PER_INCIDENT)"
            )
        if self.elapsed_seconds > self.timeout_seconds:
            raise BudgetExceeded(
                f"wall-clock budget exceeded: {self.elapsed_seconds:.1f}s > "
                f"{self.timeout_seconds}s (INCIDENT_TIMEOUT_SECONDS)"
            )

    def state(self) -> BudgetState:
        """Immutable snapshot for logs / the phase report."""
        elapsed = self.elapsed_seconds
        return BudgetState(
            max_tool_calls=self.max_tool_calls,
            max_tokens=self.max_tokens,
            timeout_seconds=self.timeout_seconds,
            tool_calls=self._tool_calls,
            tokens=self._tokens,
            elapsed_seconds=round(elapsed, 3),
            tool_calls_remaining=max(0, self.max_tool_calls - self._tool_calls),
            tokens_remaining=max(0, self.max_tokens - self._tokens),
            seconds_remaining=round(max(0.0, self.timeout_seconds - elapsed), 3),
        )


# --- call records ---------------------------------------------------------------------


class LLMCallRecord(BaseModel):
    """One model invocation, whether it succeeded or failed."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    tool_calls: int = 0
    retries: int = 0
    error: str | None = None


class RetryRecord(BaseModel):
    """One retry decision, emitted to the investigator's step log."""

    model_config = ConfigDict(extra="forbid")

    attempt: int  # 1-based retry number
    status_code: int
    delay_seconds: float
    total_wait_seconds: float
    error: str


# Only transient provider conditions are retried. A malformed request, an
# invalid key or a validation error is deterministic: retrying it wastes time
# and can hide the real problem (AGENTS.md §7.7).
RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})


def _as_int(value: object) -> int | None:
    """An integer status code, excluding bool (which is an int in Python)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def exception_status_code(exc: BaseException) -> int | None:
    """Best-effort HTTP status of a provider exception, without provider imports.

    Different SDKs expose the status differently (`status_code`,
    `response.status_code`, gRPC-style `code`). Class-name fallbacks cover the
    common Google exceptions, whose `code` is not an HTTP number.
    """
    for attribute in ("status_code", "http_status", "status"):
        status = _as_int(getattr(exc, attribute, None))
        if status is not None:
            return status
    response = getattr(exc, "response", None)
    if response is not None:
        status = _as_int(getattr(response, "status_code", None))
        if status is not None:
            return status
    code = getattr(exc, "code", None)
    status = _as_int(code)
    if status is not None:
        return status
    # google.api_core exceptions: code is a gRPC StatusCode enum whose value is
    # a (number, name) tuple. The numbers (8/14) are not HTTP statuses.
    name = type(exc).__name__.lower()
    if "resourceexhausted" in name or "ratelimit" in name:
        return 429
    if "unavailable" in name:
        return 503
    if "deadlineexceeded" in name:
        return 504
    if "internalserver" in name:
        return 500
    return None


def retry_after_seconds(exc: BaseException) -> float | None:
    """Parse `Retry-After` (seconds or HTTP-date) from the provider response."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter("retry-after") or getter("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        moment = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    return max(0.0, (moment - dt.datetime.now(dt.UTC)).total_seconds())


def _usage_counts(response: Any) -> tuple[int, int, int]:
    """(input, output, total) tokens from LangChain's normalized metadata."""
    usage = getattr(response, "usage_metadata", None) or {}
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    if total_tokens == 0 and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens
    if total_tokens == 0:
        metadata = getattr(response, "response_metadata", None) or {}
        for key in ("token_usage", "usage"):
            legacy = metadata.get(key) or {}
            if not isinstance(legacy, dict):
                continue
            input_tokens = int(legacy.get("input_tokens", legacy.get("prompt_tokens", 0)) or 0)
            output_tokens = int(
                legacy.get("output_tokens", legacy.get("completion_tokens", 0)) or 0
            )
            total_tokens = int(legacy.get("total_tokens", 0) or 0)
            if total_tokens == 0:
                total_tokens = input_tokens + output_tokens
            break
    return input_tokens, output_tokens, total_tokens


# --- the wrapper ----------------------------------------------------------------------


class BoundLLMClient:
    """A model with tools bound; calls still go through the parent's recorder."""

    def __init__(self, parent: LLMClient, runnable: Runnable[Any, Any]) -> None:
        self._parent = parent
        self._runnable = runnable

    def invoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        return cast(AIMessage, self._parent._run(self._runnable, messages, **kwargs))


class StructuredLLMClient:
    """A structured-output runnable that records the underlying raw call."""

    def __init__(self, parent: LLMClient, runnable: Runnable[Any, Any]) -> None:
        self._parent = parent
        self._runnable = runnable

    def invoke(self, messages: Any, **kwargs: Any) -> Any:
        result = self._parent._run(self._runnable, messages, **kwargs)
        if not isinstance(result, dict):
            return result
        parsing_error = result.get("parsing_error")
        if parsing_error is not None:
            raise LLMError(f"structured output could not be parsed: {parsing_error}")
        return result.get("parsed")


class LLMClient:
    """Typed facade over a chat model with per-call measurement and budgets."""

    def __init__(
        self,
        chat: BaseChatModel,
        *,
        provider: str,
        model: str,
        budget: BudgetTracker | None = None,
        max_retries: int = 3,
        max_retry_wait_seconds: float = 30.0,
        retry_base_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: Callable[[RetryRecord], None] | None = None,
        secret_values: Sequence[str] | None = None,
    ) -> None:
        self.chat = chat
        self.provider = provider
        self.model = model
        self.budget = budget
        self.max_retries = max(0, int(max_retries))
        self.max_retry_wait_seconds = max(0.0, float(max_retry_wait_seconds))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self._sleep = sleep
        self.on_retry = on_retry
        self._secret_values = list(secret_values) if secret_values is not None else None
        self.calls: list[LLMCallRecord] = []

    @classmethod
    def from_provider(
        cls,
        provider: str,
        model: str,
        *,
        settings: Settings | None = None,
        budget: BudgetTracker | None = None,
    ) -> LLMClient:
        resolved = settings or load_settings()
        return cls(
            get_llm(provider, model, settings=resolved),
            provider=provider,
            model=model,
            budget=budget or BudgetTracker.from_settings(resolved),
            max_retries=resolved.llm_max_retries,
            max_retry_wait_seconds=resolved.llm_retry_max_wait_seconds,
            retry_base_seconds=resolved.llm_retry_base_seconds,
            secret_values=settings_secrets(resolved),
        )

    def invoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        """Call the model once; record latency, tokens, and any error."""
        return cast(AIMessage, self._run(self.chat, messages, **kwargs))

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> BoundLLMClient:
        return BoundLLMClient(self, self.chat.bind_tools(tools, **kwargs))

    def with_structured_output(self, schema: type[BaseModel]) -> StructuredLLMClient:
        runnable = self.chat.with_structured_output(schema, include_raw=True)
        return StructuredLLMClient(self, runnable)

    def _secrets(self) -> Sequence[str]:
        if self._secret_values is not None:
            return self._secret_values
        return settings_secrets(load_settings())

    def _record_failure(self, started: float, error: str, retries: int) -> None:
        latency_ms = int((time.perf_counter() - started) * 1000)
        self.calls.append(
            LLMCallRecord(
                provider=self.provider,
                model=self.model,
                latency_ms=latency_ms,
                retries=retries,
                error=error,
            )
        )
        logger.warning(
            "llm call failed provider=%s model=%s latency_ms=%s retries=%s error=%s",
            self.provider,
            self.model,
            latency_ms,
            retries,
            error,
        )

    def _run(self, runnable: Runnable[Any, Any], messages: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        retries = 0
        total_wait = 0.0
        while True:
            try:
                response = runnable.invoke(messages, **kwargs)
                break
            except Exception as exc:
                error = redact_secrets(str(exc), secrets=self._secrets())
                status = exception_status_code(exc)
                if (
                    status is None
                    or status not in RETRYABLE_STATUS_CODES
                    or retries >= self.max_retries
                ):
                    self._record_failure(started, error, retries)
                    raise
                backoff = self.retry_base_seconds * (2**retries)
                requested = retry_after_seconds(exc)
                delay = requested if requested is not None else backoff
                if total_wait + delay > self.max_retry_wait_seconds:
                    reason = (
                        f"retry not attempted: waiting {delay:.1f}s would exceed the "
                        f"{self.max_retry_wait_seconds:.1f}s total retry budget"
                    )
                    self._record_failure(started, f"{error} ({reason})", retries)
                    raise
                retries += 1
                total_wait += delay
                retry_record = RetryRecord(
                    attempt=retries,
                    status_code=status,
                    delay_seconds=round(delay, 3),
                    total_wait_seconds=round(total_wait, 3),
                    error=error,
                )
                logger.warning(
                    "llm retry provider=%s model=%s attempt=%s status=%s delay=%.3fs",
                    self.provider,
                    self.model,
                    retries,
                    status,
                    delay,
                )
                if self.on_retry is not None:
                    try:
                        self.on_retry(retry_record)
                    except Exception as hook_exc:  # observability must not break the call
                        logger.warning("retry step-log hook failed: %s", hook_exc)
                self._sleep(delay)

        latency_ms = int((time.perf_counter() - started) * 1000)
        # Structured-output runnables return {"raw": AIMessage, "parsed": ...};
        # measure the raw message, not the dict wrapper.
        measured = response
        if isinstance(response, dict) and response.get("raw") is not None:
            measured = response["raw"]
        input_tokens, output_tokens, total_tokens = _usage_counts(measured)
        response_tool_calls = getattr(measured, "tool_calls", None) or []
        record = LLMCallRecord(
            provider=self.provider,
            model=self.model,
            latency_ms=latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            tool_calls=len(response_tool_calls),
            retries=retries,
        )
        self.calls.append(record)
        if self.budget is not None:
            self.budget.record_llm_call(record)
        logger.debug(
            "llm call provider=%s model=%s latency_ms=%s tokens=%s tool_calls=%s retries=%s",
            self.provider,
            self.model,
            latency_ms,
            total_tokens,
            len(response_tool_calls),
            retries,
        )
        return response


# --- provider construction ------------------------------------------------------------


def get_llm(provider: str, model: str, *, settings: Settings | None = None) -> BaseChatModel:
    """Construct the configured chat model (the only `init_chat_model` call).

    Every provider receives `api_key` explicitly from `Settings`; the process
    environment is never used as a fallback. This keeps `.env` the single
    source of truth and prevents a stray shell variable from selecting a
    different account.

    `openai_compatible` is the escape hatch for any OpenAI-style endpoint
    (DeepSeek, Kimi, GLM, OpenRouter). `ollama` uses its OpenAI-compatible
    `/v1` endpoint, so no extra integration package is required. `fake` is
    deterministic and for wiring tests only.
    """
    normalized = provider.strip().lower()
    if normalized not in LLM_PROVIDERS:
        allowed = ", ".join(sorted(LLM_PROVIDERS))
        raise ConfigError(f"unsupported LLM provider {provider!r}; expected one of: {allowed}")
    if not model.strip():
        raise ConfigError("LLM model name is empty")

    if normalized == "fake":
        return FakeChatModel()

    resolved = settings or load_settings()
    try:
        if normalized == "openai_compatible":
            if not resolved.llm_base_url.strip():
                raise ConfigError(
                    "LLM_BASE_URL is required for LLM_PROVIDER=openai_compatible "
                    "(e.g. https://api.deepseek.com/v1)"
                )
            return init_chat_model(
                model,
                model_provider="openai",
                base_url=resolved.llm_base_url,
                api_key=resolved.llm_api_key or "not-needed",
                temperature=0,
            )
        if normalized == "ollama":
            return init_chat_model(
                model,
                model_provider="openai",
                base_url=resolved.llm_base_url or "http://localhost:11434/v1",
                api_key=resolved.llm_api_key or "ollama",
                temperature=0,
            )
        if normalized == "anthropic":
            return init_chat_model(
                model,
                model_provider="anthropic",
                api_key=resolved.anthropic_api_key,
                temperature=0,
            )
        if normalized == "openai":
            return init_chat_model(
                model,
                model_provider="openai",
                api_key=resolved.openai_api_key,
                temperature=0,
            )
        # google_genai: always pass the key from Settings. Passing it explicitly
        # means a process-level GOOGLE_API_KEY cannot silently override the
        # configured environment (and makes missing-key failures deterministic).
        return init_chat_model(
            model,
            model_provider="google_genai",
            api_key=resolved.google_api_key,
            temperature=0,
        )
    except ConfigError:
        raise
    except ImportError as exc:
        package = {
            "anthropic": "langchain-anthropic",
            "openai": "langchain-openai",
            "google_genai": "langchain-google-genai",
        }.get(normalized, "the provider integration package")
        raise LLMError(
            f"provider {normalized!r} needs {package}; install it in the venv ({exc})"
        ) from exc
    except Exception as exc:
        raise LLMError(
            redact_secrets(
                f"could not initialise {normalized}/{model}: {exc}",
                secrets=settings_secrets(resolved),
            )
        ) from exc


# --- deterministic fake ----------------------------------------------------------------


def _example_value(annotation: Any) -> Any:
    """A deterministic, schema-valid example used by the fake model only."""
    origin = getattr(annotation, "__origin__", None)
    if annotation is str:
        return "example"
    if annotation is bool:
        return True
    if annotation is int:
        return 1
    if annotation is float:
        return 0.9
    if origin in (list, tuple):
        return []
    if origin is dict:
        return {}
    return "example"


def _example_args(schema: Any) -> dict[str, Any]:
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return {
            name: _example_value(field.annotation) for name, field in schema.model_fields.items()
        }
    return {}


def _tool_name(tool: Any) -> str:
    if isinstance(tool, BaseTool):
        return tool.name
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "tool")
        return str(tool.get("name") or "tool")
    return str(getattr(tool, "__name__", "tool"))


def _tool_args_schema(tool: Any) -> Any:
    if isinstance(tool, BaseTool):
        return tool.args_schema
    if isinstance(tool, dict):
        return tool.get("function", {}).get("parameters") if tool.get("function") else None
    return None


def _schema_example_args(schema: Any) -> dict[str, Any]:
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return {}
        return {
            str(name): _example_value(spec.get("type") if isinstance(spec, dict) else None)
            for name, spec in properties.items()
        }
    return _example_args(schema)


class FakeChatModel(BaseChatModel):
    """Deterministic scripted model for wiring tests (never a benchmark model).

    Behaviour is intentionally simple and inspectable:

    * no bound tools -> a plain text answer;
    * a bound Pydantic schema (structured output) -> one tool call whose args
      satisfy that schema;
    * bound tools -> one tool call per step, and a final text answer after two
      tool results (enough to exercise a three-step loop).
    """

    bound_tools: list[Any] = Field(default_factory=list)
    _step: int = PrivateAttr(default=0)

    @property
    def _llm_type(self) -> str:
        return "dqs-fake"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        _ = tool_choice, kwargs
        return self.model_copy(update={"bound_tools": list(tools)})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Any:
        _ = stop, run_manager, kwargs
        from langchain_core.outputs import ChatGeneration, ChatResult

        tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
        structured = next(
            (
                tool
                for tool in self.bound_tools
                if isinstance(tool, type) and issubclass(tool, BaseModel)
            ),
            None,
        )
        if structured is not None:
            message = self._tool_call_message(
                structured.__name__, _example_args(structured), "structured"
            )
        elif self.bound_tools:
            tool = self.bound_tools[0]
            name = _tool_name(tool)
            args = _schema_example_args(_tool_args_schema(tool))
            if len(tool_messages) >= 2:
                message = AIMessage(content="fake final answer")
            else:
                # Vary the argument so a test can prove each tool result feeds
                # the next model call (not a replayed constant).
                if "query" in args:
                    args["query"] = f"step-{len(tool_messages) + 1}"
                message = self._tool_call_message(name, args, f"step-{len(tool_messages) + 1}")
        else:
            message = AIMessage(content="fake response")

        input_tokens = 12 + 3 * len(tool_messages)
        output_tokens = 5
        message.usage_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        message.response_metadata = {"model_name": "fake"}
        self._step += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    @staticmethod
    def _tool_call_message(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": name,
                    "args": args,
                    "id": f"call_fake_{call_id}",
                    "type": "tool_call",
                }
            ],
        )
