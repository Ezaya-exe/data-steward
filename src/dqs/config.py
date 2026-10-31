"""Application settings.

The single place where environment variables are read (AGENTS.md §6 forbids
scattered `os.environ` reads). Values come from the process environment and,
when present, from a `.env` file at the project root; the defaults below are
the documented ones from docs/ARCHITECTURE.md §3.

Aliases exist because the documented variable names have no underscore after
`PG` (`PGHOST`, `PGPORT`, ...) while Python field names are snake_case.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

LLM_PROVIDERS = frozenset(
    {"anthropic", "openai", "google_genai", "ollama", "openai_compatible", "fake"}
)
GITHUB_MODES = frozenset({"dry_run", "live"})

# Benchmark cost estimate, USD per million tokens (input, output), matched by a
# case-insensitive model-name prefix. These are planning prices, not invoices;
# `Settings` overrides them when a provider changes its list price. An unknown
# model costs 0.0 and is labelled as unpriced in the results metadata.
LLM_PRICE_TABLE: dict[str, tuple[float, float]] = {
    # Groq list price, checked 2026-10-25 against console.groq.com/docs/models
    # (the requested 2026-10-23 price is unchanged). Reports re-price stored
    # token counts with this table, so editing it also updates old results.
    "openai/gpt-oss-120b": (0.15, 0.60),
    # Owner's router list price for the model in LLM_MODEL, per 1M tokens
    # (provided 2026-10-26; the owner will confirm the final numbers). More
    # specific than the generic `deepseek` entry, so prefix matching picks it.
    "deepseek/deepseek-v4.1-flash": (0.07, 0.30),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
    "claude-opus": (15.0, 75.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-5": (1.25, 10.0),
    "o3": (2.0, 8.0),
    "deepseek": (0.27, 1.1),
    "gemini-2.5-flash": (0.3, 2.5),
    "gemini": (0.3, 2.5),
    "kimi": (0.6, 2.5),
    "glm": (0.6, 2.2),
}


def price_for_model(model: str) -> tuple[float, float] | None:
    """(input, output) USD per million tokens, or None when unpriced."""
    normalized = model.strip().lower()
    for prefix in sorted(LLM_PRICE_TABLE, key=len, reverse=True):
        if normalized.startswith(prefix):
            return LLM_PRICE_TABLE[prefix]
    return None


class Settings(BaseSettings):
    """All configuration, validated once at load time.

    Secrets (API keys, tokens, passwords) live here and are never logged or
    committed; they only travel to the components that need them.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Postgres (host port 5433 avoids a local instance)
    pg_host: str = Field("localhost", validation_alias=AliasChoices("PGHOST", "PG_HOST"))
    pg_port: int = Field(5433, validation_alias=AliasChoices("PGPORT", "PG_PORT"))
    pg_database: str = Field(
        "warehouse", validation_alias=AliasChoices("PGDATABASE", "PG_DATABASE")
    )
    pg_user: str = Field("dqs", validation_alias=AliasChoices("PGUSER", "PG_USER"))
    pg_password: str = Field("dqs", validation_alias=AliasChoices("PGPASSWORD", "PG_PASSWORD"))
    pg_ro_user: str = Field("dqs_ro", validation_alias=AliasChoices("PG_RO_USER"))
    pg_ro_password: str = Field("dqs_ro", validation_alias=AliasChoices("PG_RO_PASSWORD"))

    # LLM (provider/model names are validated, keys are not — Phase 4 builds them)
    llm_provider: str = "anthropic"
    llm_model: str = "claude-haiku-5-5"
    llm_model_bench: str = "claude-sonnet-5-5"
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    google_api_key: str = ""
    # OpenAI-compatible endpoints (DeepSeek, Kimi, GLM, OpenRouter, local Ollama).
    # Only `openai_compatible`/`ollama` read these; the field names match the
    # documented env variables exactly (no aliases needed).
    llm_base_url: str = ""
    llm_api_key: str = ""

    # GitHub publishing (Phase 6). dry_run writes PR previews to out/ only.
    github_mode: str = "dry_run"
    github_token: str = ""
    github_repo: str = ""
    github_base_branch: str = "main"

    # Bounded autonomy (AGENTS.md §7.7) and determinism (§8)
    max_tool_calls: int = 10
    max_fix_attempts: int = 3
    max_tokens_per_incident: int = 60000
    # Phase 6b raised the default: a real incident can include an LLM retry
    # cycle and one or more sandbox dbt builds (each a whole-warehouse build).
    incident_timeout_seconds: int = 900
    # Sandbox verification is a dbt build over a clone of the project; a hang
    # must end in a verdict, not an orphaned process or schema.
    fix_verify_timeout_seconds: int = 300
    # Per-call completion ceiling for the fixer. A reasoning model can spend the
    # whole budget on hidden reasoning and return an empty message (dev run 2),
    # so the ceiling is explicit, recorded in the results, and bounded.
    fixer_max_output_tokens: int = 8192
    dqs_seed: int = 42

    # Transient provider failures: retry only HTTP 429/5xx (AGENTS.md §7.7).
    # The total wait is a hard cap, so a huge Retry-After cannot stall an
    # incident; the loop escalates instead.
    llm_max_retries: int = 3
    llm_retry_max_wait_seconds: float = 30.0
    llm_retry_base_seconds: float = 1.0

    # Benchmark price overrides (USD per million tokens). Empty means "use
    # LLM_PRICE_TABLE"; both set and 0.0 is an explicit free/local model.
    llm_input_price_per_mtok: float | None = None
    llm_output_price_per_mtok: float | None = None

    # Observability (Phase 9, optional)
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = ""

    @field_validator("llm_provider")
    @classmethod
    def _check_provider(cls, value: str) -> str:
        if value not in LLM_PROVIDERS:
            allowed = ", ".join(sorted(LLM_PROVIDERS))
            raise ValueError(f"unsupported LLM_PROVIDER {value!r}; expected one of: {allowed}")
        return value

    @field_validator("github_mode")
    @classmethod
    def _check_github_mode(cls, value: str) -> str:
        if value not in GITHUB_MODES:
            allowed = ", ".join(sorted(GITHUB_MODES))
            raise ValueError(f"unsupported GITHUB_MODE {value!r}; expected one of: {allowed}")
        return value

    @property
    def rw_database_url(self) -> URL:
        """SQLAlchemy URL for the read/write app role (`dqs`)."""
        return URL.create(
            "postgresql+psycopg",
            username=self.pg_user,
            password=self.pg_password,
            host=self.pg_host,
            port=self.pg_port,
            database=self.pg_database,
        )

    @property
    def ro_database_url(self) -> URL:
        """SQLAlchemy URL for the read-only agent role (`dqs_ro`)."""
        return URL.create(
            "postgresql+psycopg",
            username=self.pg_ro_user,
            password=self.pg_ro_password,
            host=self.pg_host,
            port=self.pg_port,
            database=self.pg_database,
        )


def estimate_cost_usd(
    model: str, tokens_in: int, tokens_out: int, settings: Settings | None = None
) -> float:
    """Estimated USD cost from the price table (or explicit Settings overrides).

    Returns 0.0 for an unpriced model; the results metadata records whether the
    price was known so a 0.0 is never silently read as "free".
    """
    if settings is not None and settings.llm_input_price_per_mtok is not None:
        price_in = float(settings.llm_input_price_per_mtok)
        price_out = float(settings.llm_output_price_per_mtok or 0.0)
    else:
        tuple_price = price_for_model(model)
        if tuple_price is None:
            return 0.0
        price_in, price_out = tuple_price
    return (max(0, tokens_in) * price_in + max(0, tokens_out) * price_out) / 1_000_000.0


@lru_cache(maxsize=1)
def load_settings() -> Settings:
    """Read `.env` + environment once per process and validate.

    Returns a cached instance because the CLI is one-shot; tests that need a
    fresh object construct `Settings(...)` directly.
    """
    return Settings()
