"""Unit tests: LLM wrapper, call measurement, budget tracker, fake smoke.

No network and no API keys. The fake provider exists exactly for these wiring
tests (AGENTS.md §7.9); benchmark numbers are never taken from it.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from dqs.agents.llm import (
    BudgetTracker,
    FakeChatModel,
    LLMCallRecord,
    LLMClient,
    RetryRecord,
    _response_details,
    _supported_model_kwargs,
    get_llm,
    is_daily_quota_error,
    redact_secrets,
)
from dqs.agents.smoke import run_smoke
from dqs.config import Settings
from dqs.errors import BudgetExceeded, ConfigError, DailyQuotaExceeded


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _record(tokens: int) -> LLMCallRecord:
    return LLMCallRecord(
        provider="fake",
        model="fake-1",
        latency_ms=3,
        input_tokens=tokens,
        output_tokens=0,
        total_tokens=tokens,
    )


def test_get_llm_fake_is_deterministic(settings: Settings) -> None:
    model = get_llm("fake", "fake-1", settings=settings)
    assert isinstance(model, FakeChatModel)


def test_openai_compatible_requires_base_url(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="LLM_BASE_URL"):
        get_llm("openai_compatible", "deepseek-chat", settings=settings)


def test_unknown_provider_is_rejected(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="unsupported LLM provider"):
        get_llm("excel", "sheet-1", settings=settings)


@pytest.mark.parametrize(
    ("provider", "field", "key"),
    [
        ("anthropic", "anthropic_api_key", "test-anthropic-key"),
        ("openai", "openai_api_key", "test-openai-key"),
        ("google_genai", "google_api_key", "test-google-key"),
        ("openai_compatible", "llm_api_key", "test-compatible-key"),
    ],
)
def test_get_llm_passes_the_settings_key_explicitly(
    monkeypatch: pytest.MonkeyPatch, provider: str, field: str, key: str
) -> None:
    """A stub captures constructor kwargs; no provider client is built and no network is used."""
    settings = Settings(
        _env_file=None,
        llm_base_url="https://example.test/v1",
        **{field: key},
    )
    captured: dict[str, object] = {}

    def stub(model: str, **kwargs: object) -> object:
        captured["model"] = model
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("dqs.agents.llm.init_chat_model", stub)
    get_llm(provider, "test-model", settings=settings)
    assert captured["model"] == "test-model"
    assert captured["api_key"] == key


def test_get_llm_does_not_fall_back_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, google_api_key="settings-google-key")
    captured: dict[str, object] = {}

    def stub(model: str, **kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("GOOGLE_API_KEY", "environment-google-key")
    monkeypatch.setattr("dqs.agents.llm.init_chat_model", stub)
    get_llm("google_genai", "test-model", settings=settings)
    assert captured["api_key"] == "settings-google-key"


def test_secret_redaction() -> None:
    text = "Authorization: Bearer sk-ant-abcdefgh12345678 and api_key=supersecret"
    redacted = redact_secrets(text, secrets=["supersecret"])
    assert "sk-ant-abcdefgh12345678" not in redacted
    assert "supersecret" not in redacted
    assert "***redacted***" in redacted


def test_google_key_is_redacted_by_value_and_shape() -> None:
    key = "AIzaDummyKeyForRedaction1234567890"
    text = f"provider error: key={key}"
    assert key not in redact_secrets(text, secrets=[key])
    assert key not in redact_secrets(text)


def test_client_error_redacts_the_google_key_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "settings-google-key-without-a-known-shape"
    settings = Settings(_env_file=None, google_api_key=key)
    monkeypatch.setattr("dqs.agents.llm.load_settings", lambda: settings)

    class _Boom:
        def invoke(self, messages: object, **kwargs: object) -> object:
            raise RuntimeError(f"provider rejected key {key}")

    client = LLMClient(_Boom(), provider="google_genai", model="test-model")  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        client.invoke("hello")
    assert len(client.calls) == 1
    assert client.calls[0].error is not None
    assert key not in client.calls[0].error
    assert "***redacted***" in client.calls[0].error


def test_client_records_latency_and_tokens(settings: Settings) -> None:
    client = LLMClient(
        get_llm("fake", "fake-1", settings=settings),
        provider="fake",
        model="fake-1",
    )
    response = client.invoke("hello")
    assert response.content == "fake response"
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.latency_ms >= 0
    assert call.input_tokens > 0
    assert call.output_tokens > 0
    assert call.total_tokens == call.input_tokens + call.output_tokens
    assert call.error is None


def test_budget_tool_call_limit() -> None:
    clock = _Clock()
    tracker = BudgetTracker(max_tool_calls=1, max_tokens=100, timeout_seconds=10, clock=clock)
    tracker.record_tool_call()
    with pytest.raises(BudgetExceeded, match="tool-call budget"):
        tracker.record_tool_call()


def test_budget_token_limit() -> None:
    clock = _Clock()
    tracker = BudgetTracker(max_tool_calls=10, max_tokens=5, timeout_seconds=10, clock=clock)
    with pytest.raises(BudgetExceeded, match="token budget"):
        tracker.record_llm_call(_record(6))


def test_budget_wall_clock_timeout() -> None:
    clock = _Clock()
    tracker = BudgetTracker(max_tool_calls=10, max_tokens=100, timeout_seconds=5, clock=clock)
    clock.advance(6)
    with pytest.raises(BudgetExceeded, match="wall-clock budget"):
        tracker.check()


def test_budget_state_reports_remaining() -> None:
    clock = _Clock()
    tracker = BudgetTracker(max_tool_calls=3, max_tokens=100, timeout_seconds=10, clock=clock)
    tracker.record_tool_call()
    tracker.record_llm_call(_record(40))
    state = tracker.state()
    assert state.tool_calls == 1
    assert state.tool_calls_remaining == 2
    assert state.tokens == 40
    assert state.tokens_remaining == 60
    assert state.seconds_remaining == 10.0


def test_fake_provider_calls_are_counted_by_the_budget(settings: Settings) -> None:
    clock = _Clock()
    tracker = BudgetTracker(max_tool_calls=3, max_tokens=100, timeout_seconds=10, clock=clock)
    client = LLMClient(
        get_llm("fake", "fake-1", settings=settings),
        provider="fake",
        model="fake-1",
        budget=tracker,
    )
    client.invoke("hello")
    assert tracker.llm_calls == 1
    assert tracker.tokens == 17  # fake emits 12 input + 5 output tokens
    assert tracker.state().tokens_remaining == 83


def test_client_raises_when_a_call_exceeds_the_token_budget(settings: Settings) -> None:
    tracker = BudgetTracker(max_tool_calls=3, max_tokens=5, timeout_seconds=10, clock=_Clock())
    client = LLMClient(
        get_llm("fake", "fake-1", settings=settings),
        provider="fake",
        model="fake-1",
        budget=tracker,
    )
    with pytest.raises(BudgetExceeded, match="token budget"):
        client.invoke("hello")
    assert len(client.calls) == 1  # the call is still measured before the raise


class _Response:
    def __init__(self, retry_after: str | None = None) -> None:
        self.headers = {} if retry_after is None else {"retry-after": retry_after}
        self.status_code = 429


class _ProviderError(RuntimeError):
    """Stands in for a provider SDK error without importing the SDK."""

    def __init__(self, status: int, retry_after: str | None = None) -> None:
        super().__init__(f"provider error {status}")
        self.status_code = status
        self.response = _Response(retry_after)


class _FlakyRunnable:
    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = list(failures)
        self.calls = 0

    def invoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        _ = messages, kwargs
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        message = AIMessage(content="recovered")
        message.usage_metadata = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
        return message


def _flaky_client(runnable: _FlakyRunnable, **kwargs: Any) -> LLMClient:
    return LLMClient(
        cast(BaseChatModel, runnable),
        provider="fake",
        model="flaky",
        secret_values=[],
        **kwargs,
    )


def test_retry_on_429_then_success() -> None:
    runnable = _FlakyRunnable([_ProviderError(429)])
    slept: list[float] = []
    retries: list[RetryRecord] = []
    client = _flaky_client(
        runnable,
        max_retries=2,
        sleep=slept.append,
        on_retry=retries.append,
    )
    response = client.invoke("hello")
    assert response.content == "recovered"
    assert runnable.calls == 2
    assert slept == [1.0]
    assert len(retries) == 1
    assert retries[0].status_code == 429
    assert len(client.calls) == 1
    call = client.calls[0]
    assert (call.input_tokens, call.output_tokens, call.total_tokens) == (10, 2, 12)
    assert call.retries == 1
    assert call.error is None


def test_retry_honours_retry_after() -> None:
    runnable = _FlakyRunnable([_ProviderError(503, retry_after="0.25")])
    slept: list[float] = []
    client = _flaky_client(runnable, max_retries=1, sleep=slept.append)
    client.invoke("hello")
    assert slept == [0.25]
    assert client.calls[0].retries == 1


def test_retry_uses_exponential_backoff() -> None:
    runnable = _FlakyRunnable([_ProviderError(500), _ProviderError(502)])
    slept: list[float] = []
    client = _flaky_client(runnable, max_retries=2, sleep=slept.append)
    client.invoke("hello")
    assert slept == [1.0, 2.0]
    assert client.calls[0].retries == 2


def test_non_retryable_error_is_not_retried() -> None:
    runnable = _FlakyRunnable([_ProviderError(400)])
    slept: list[float] = []
    retries: list[RetryRecord] = []
    client = _flaky_client(runnable, max_retries=3, sleep=slept.append, on_retry=retries.append)
    with pytest.raises(_ProviderError):
        client.invoke("hello")
    assert runnable.calls == 1
    assert slept == []
    assert retries == []
    assert client.calls[0].retries == 0
    assert client.calls[0].error is not None


def test_retry_stops_at_max_retries() -> None:
    runnable = _FlakyRunnable([_ProviderError(429) for _ in range(10)])
    slept: list[float] = []
    client = _flaky_client(runnable, max_retries=3, sleep=slept.append)
    with pytest.raises(_ProviderError):
        client.invoke("hello")
    assert runnable.calls == 4  # initial + 3 retries
    assert len(slept) == 3
    assert client.calls[0].retries == 3


def test_retry_after_beyond_the_total_wait_budget_is_not_slept() -> None:
    runnable = _FlakyRunnable([_ProviderError(429, retry_after="120")])
    slept: list[float] = []
    client = _flaky_client(runnable, max_retries=3, max_retry_wait_seconds=30.0, sleep=slept.append)
    with pytest.raises(_ProviderError):
        client.invoke("hello")
    assert runnable.calls == 1
    assert slept == []
    assert client.calls[0].error is not None
    assert "retry budget" in client.calls[0].error


class _QuotaError(_ProviderError):
    """A daily-quota 429: same status as a rate limit, different meaning."""

    def __init__(self, message: str = "You exceeded your current quota, per day limit") -> None:
        super().__init__(429)
        self.args = (message,)


def test_daily_quota_429_stops_immediately_without_retry() -> None:
    runnable = _FlakyRunnable([_QuotaError()])
    slept: list[float] = []
    client = _flaky_client(runnable, max_retries=3, sleep=slept.append)
    with pytest.raises(DailyQuotaExceeded, match="daily LLM quota exhausted"):
        client.invoke("hello")
    assert runnable.calls == 1
    assert slept == []
    assert len(client.calls) == 1
    assert client.calls[0].error is not None
    assert "quota" in client.calls[0].error


def test_is_daily_quota_error_distinguishes_rate_limits() -> None:
    assert is_daily_quota_error(_QuotaError("insufficient_quota: quota exceeded"))
    assert not is_daily_quota_error(_ProviderError(429))
    assert not is_daily_quota_error(_QuotaError("rate limit reached for requests"))


def test_daily_quota_marker_is_not_retried_even_with_wait_room() -> None:
    runnable = _FlakyRunnable([_QuotaError("daily limit reached"), _QuotaError("daily limit")])
    slept: list[float] = []
    client = _flaky_client(runnable, max_retries=3, max_retry_wait_seconds=999, sleep=slept.append)
    with pytest.raises(DailyQuotaExceeded):
        client.invoke("hello")
    assert runnable.calls == 1
    assert slept == []


def test_smoke_runs_all_four_probes_on_fake() -> None:
    report = run_smoke("fake", "fake-1", settings=Settings(_env_file=None))
    assert report.passed, [result.detail for result in report.results]
    assert [result.name for result in report.results] == [
        "plain_completion",
        "typed_tool_call",
        "structured_output",
        "three_step_tool_loop",
    ]
    loop = report.results[-1]
    assert loop.input_tokens > 0
    assert "model_calls=3" in loop.detail


class _Clock:
    """Deterministic monotonic clock for budget tests."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# --- provider failure details and the output ceiling -----------------------------------


class _ReasoningRun:
    """Returns one provider message with a finish reason and reasoning usage."""

    def __init__(self, message: AIMessage) -> None:
        self.message = message

    def invoke(self, messages: object, **kwargs: object) -> AIMessage:
        _ = messages, kwargs
        return self.message


def _reasoning_message(metadata: dict[str, Any], usage: dict[str, Any]) -> AIMessage:
    message = AIMessage(content="")
    message.response_metadata = metadata
    message.usage_metadata = usage
    return message


def test_client_records_the_finish_reason_and_reasoning_tokens(settings: Settings) -> None:
    """An empty all-reasoning reply must be visible in the call record."""
    message = _reasoning_message(
        {"finish_reason": "error", "token_usage": {"completion_tokens_details": {}}},
        {
            "input_tokens": 120,
            "output_tokens": 3948,
            "total_tokens": 4068,
            "output_token_details": {"reasoning": 3948},
        },
    )
    client = LLMClient(_ReasoningRun(message), provider="fake", model="reasoning-fake")  # type: ignore[arg-type]

    client.invoke("hello")

    call = client.calls[0]
    assert call.finish_reason == "error"
    assert call.reasoning_tokens == 3948
    assert call.output_tokens == 3948


def test_response_details_reads_the_openai_compatible_reasoning_counter() -> None:
    message = _reasoning_message(
        {
            "finish_reason": "length",
            "token_usage": {"completion_tokens_details": {"reasoning_tokens": 512}},
        },
        {"input_tokens": 10, "output_tokens": 512, "total_tokens": 522},
    )

    assert _response_details(message) == ("length", 512)


def test_response_details_is_empty_for_a_plain_message() -> None:
    assert _response_details(AIMessage(content="plain")) == (None, None)


def test_unsupported_structured_output_options_are_dropped_not_raised() -> None:
    """`max_tokens` is optional: a model without that field must still answer."""

    class _NoLimit(FakeChatModel):
        pass

    applied = _supported_model_kwargs(_NoLimit(), {"max_tokens": 8192})
    assert applied == {}
    # And the structured call itself still works with an unsupported option.
    client = LLMClient(_NoLimit(), provider="fake", model="fake-1")
    result = client.with_structured_output(_Example, max_tokens=8192).invoke("hello")
    assert isinstance(result, _Example)


def test_supported_structured_output_options_are_bound() -> None:
    class _DeclaresLimit(FakeChatModel):
        max_tokens: int | None = None

    assert _supported_model_kwargs(_DeclaresLimit(), {"max_tokens": 8192}) == {"max_tokens": 8192}


class _Example(BaseModel):
    """Minimal structured-output schema used by the ceiling tests."""

    label: str
