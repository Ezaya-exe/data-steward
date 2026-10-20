"""Unit tests: LLM wrapper, call measurement, budget tracker, fake smoke.

No network and no API keys. The fake provider exists exactly for these wiring
tests (AGENTS.md §7.9); benchmark numbers are never taken from it.
"""

from __future__ import annotations

import pytest

from dqs.agents.llm import (
    BudgetTracker,
    FakeChatModel,
    LLMCallRecord,
    LLMClient,
    get_llm,
    redact_secrets,
)
from dqs.agents.smoke import run_smoke
from dqs.config import Settings
from dqs.errors import BudgetExceeded, ConfigError


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
