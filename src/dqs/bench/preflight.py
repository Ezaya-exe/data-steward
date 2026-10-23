"""`dqs bench preflight`: read-only readiness checks before spending quota.

Every check is cheap and side-effect free: it pings Postgres, inspects the
cached template row, reads contract status, computes file hashes, looks at the
configured model and disk, and checks that a key exists (without ever printing
it). It makes **no LLM calls and no benchmark runs**, so it is safe to execute
before every session.

Why PASS/FAIL rather than exceptions: an operator should see every problem in
one screen, not discover the next one after fixing the first.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict

from dqs import db as dbmod
from dqs import fast_reset
from dqs.bench.harness import HARNESS_VERSION, prompt_hashes
from dqs.bench.runner import force_bench_dry_run
from dqs.config import Settings
from dqs.contracts import store as contracts_store
from dqs.errors import DqsError
from dqs.models.enums import ContractStatus

CONTRACTS_DIR: Final = Path(__file__).resolve().parents[3] / "contracts"
MIN_FREE_DISK_BYTES: Final = 1 * 1024**3  # 1 GiB


class DiskUsage(Protocol):
    """The one attribute preflight needs from `shutil.disk_usage`."""

    free: int


def _default_disk_usage(path: Path) -> DiskUsage:
    usage = shutil.disk_usage(path)
    return SimpleNamespace(free=usage.free)


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class PreflightCheck(BaseModel):
    """One named readiness check and its human-readable evidence."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: CheckStatus
    detail: str


class PreflightReport(BaseModel):
    """All checks in display order; `ok` is false when any check failed."""

    model_config = ConfigDict(extra="forbid")

    checks: list[PreflightCheck]

    @property
    def ok(self) -> bool:
        return all(check.status is CheckStatus.PASS for check in self.checks)

    def render(self) -> str:
        width = max(len(check.name) for check in self.checks)
        return "\n".join(
            f"{check.status.value:<4} {check.name:<{width}}  {check.detail}"
            for check in self.checks
        )


def _provider_key(settings: Settings) -> str:
    """The provider's configured key value; never placed in a detail string."""
    return {
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
        "google_genai": settings.google_api_key,
        "openai_compatible": settings.llm_api_key,
        "ollama": settings.llm_api_key,
        "fake": "",
    }.get(settings.llm_provider, settings.llm_api_key)


def _provider_key_name(settings: Settings) -> str:
    return {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google_genai": "GOOGLE_API_KEY",
        "openai_compatible": "LLM_API_KEY",
        "ollama": "LLM_API_KEY (optional for a local server)",
        "fake": "not required (fake provider)",
    }.get(settings.llm_provider, "LLM_API_KEY")


def _check_postgres(
    settings: Settings, ping: Callable[[Settings], dict[str, str]]
) -> PreflightCheck:
    try:
        versions = ping(settings)
    except DqsError as exc:
        return PreflightCheck(name="postgres healthy", status=CheckStatus.FAIL, detail=str(exc))
    except Exception as exc:  # a driver/socket error is still a FAIL, not a crash
        return PreflightCheck(
            name="postgres healthy",
            status=CheckStatus.FAIL,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return PreflightCheck(
        name="postgres healthy",
        status=CheckStatus.PASS,
        detail=f"rw + ro connected ({str(versions.get('rw', '')).split(',')[0][:40]})",
    )


def _check_template(
    settings: Settings,
    *,
    exists: Callable[[Settings], bool],
    meta: Callable[[Settings], dict[str, object] | None],
) -> PreflightCheck:
    try:
        if not exists(settings):
            # A missing cache is not a blocker: the runner builds it once (~10 s)
            # before the first paid scenario. A *stale* cache is a blocker.
            return PreflightCheck(
                name="dqs_template cached/current",
                status=CheckStatus.PASS,
                detail="not cached yet; the first benchmark run will build it (~10 s)",
            )
        row = meta(settings.model_copy(update={"pg_database": fast_reset.TEMPLATE_DB}))
    except DqsError as exc:
        return PreflightCheck(
            name="dqs_template cached/current", status=CheckStatus.FAIL, detail=str(exc)
        )
    if row is None:
        return PreflightCheck(
            name="dqs_template cached/current",
            status=CheckStatus.FAIL,
            detail="database exists but dq.template_meta is missing; rebuild it",
        )
    expected = fast_reset.content_hash()
    stored = str(row.get("content_hash") or "")
    seed = _row_int(row, "seed")
    days = _row_int(row, "baseline_days")
    if stored != expected or seed != fast_reset.SEED or days != fast_reset.BASELINE_DAYS:
        return PreflightCheck(
            name="dqs_template cached/current",
            status=CheckStatus.FAIL,
            detail=(
                f"stale (hash {stored[:12] or 'unknown'} != {expected[:12]}, "
                f"seed {seed}, days {days}); drop dqs_template or run the benchmark once"
            ),
        )
    return PreflightCheck(
        name="dqs_template cached/current",
        status=CheckStatus.PASS,
        detail=f"hash {expected[:12]}, seed {seed}, {days} days",
    )


def _row_int(row: dict[str, object], key: str) -> int:
    """Read an int from a DB row without trusting its runtime type."""
    value = row.get(key, -1)
    return value if isinstance(value, int) and not isinstance(value, bool) else -1


def _check_contracts(directory: Path) -> PreflightCheck:
    try:
        contracts = contracts_store.load_contracts(directory)
    except DqsError as exc:
        return PreflightCheck(name="contracts approved", status=CheckStatus.FAIL, detail=str(exc))
    if not contracts:
        return PreflightCheck(
            name="contracts approved",
            status=CheckStatus.FAIL,
            detail=f"no contracts in {directory}; run `dqs contracts approve --all`",
        )
    drafts = sorted(
        table
        for table, contract in contracts.items()
        if contract.status is not ContractStatus.APPROVED
    )
    if drafts:
        return PreflightCheck(
            name="contracts approved",
            status=CheckStatus.FAIL,
            detail=f"{len(drafts)} not approved: {', '.join(drafts)}",
        )
    return PreflightCheck(
        name="contracts approved",
        status=CheckStatus.PASS,
        detail=f"{len(contracts)} approved",
    )


def _check_github_mode(settings: Settings) -> PreflightCheck:
    effective = force_bench_dry_run(settings).github_mode
    if settings.github_mode == "live":
        detail = "dry_run (forced by the benchmark; GITHUB_MODE=live in .env is ignored)"
    else:
        detail = f"{effective} (no GitHub writes)"
    return PreflightCheck(name="GITHUB_MODE effective", status=CheckStatus.PASS, detail=detail)


def _check_model(settings: Settings, model: str | None) -> PreflightCheck:
    configured = (model or settings.llm_model_bench).strip()
    if not configured:
        return PreflightCheck(
            name="benchmark model",
            status=CheckStatus.FAIL,
            detail="LLM_MODEL_BENCH is empty; pass --model or set it in .env",
        )
    return PreflightCheck(
        name="benchmark model",
        status=CheckStatus.PASS,
        detail=f"{configured} (provider {settings.llm_provider})",
    )


def _check_harness() -> PreflightCheck:
    hashes = prompt_hashes()
    missing = [name for name, value in hashes.items() if not value]
    if missing or not HARNESS_VERSION:
        return PreflightCheck(
            name="harness frozen",
            status=CheckStatus.FAIL,
            detail=f"missing prompt hash/harness version: {', '.join(missing) or 'version'}",
        )
    short = ", ".join(f"{name}={value[:12]}" for name, value in sorted(hashes.items()))
    return PreflightCheck(
        name="harness frozen",
        status=CheckStatus.PASS,
        detail=f"{HARNESS_VERSION} ({short})",
    )


def _check_disk(usage: Callable[[Path], DiskUsage]) -> PreflightCheck:
    try:
        free = int(usage(fast_reset.REPO_ROOT).free)
    except OSError as exc:
        return PreflightCheck(
            name="free disk space", status=CheckStatus.FAIL, detail=f"cannot read: {exc}"
        )
    if free < MIN_FREE_DISK_BYTES:
        return PreflightCheck(
            name="free disk space",
            status=CheckStatus.FAIL,
            detail=f"{free / 1024**3:.1f} GiB free; need at least 1.0 GiB",
        )
    return PreflightCheck(
        name="free disk space",
        status=CheckStatus.PASS,
        detail=f"{free / 1024**3:.1f} GiB free",
    )


def _check_api_key(settings: Settings) -> PreflightCheck:
    if settings.llm_provider == "fake":
        return PreflightCheck(
            name="LLM API key",
            status=CheckStatus.PASS,
            detail="not required (fake provider)",
        )
    if _provider_key(settings).strip():
        return PreflightCheck(
            name="LLM API key",
            status=CheckStatus.PASS,
            detail=(
                f"set for {settings.llm_provider} "
                f"({_provider_key_name(settings)}; value not printed)"
            ),
        )
    return PreflightCheck(
        name="LLM API key",
        status=CheckStatus.FAIL,
        detail=f"{_provider_key_name(settings)} is not set for {settings.llm_provider}",
    )


def run_preflight(
    settings: Settings,
    *,
    model: str | None = None,
    contracts_dir: Path = CONTRACTS_DIR,
    ping: Callable[[Settings], dict[str, str]] = dbmod.ping,
    template_exists: Callable[[Settings], bool] = fast_reset.template_exists,
    template_meta: Callable[[Settings], dict[str, object] | None] = fast_reset.template_meta,
    disk_usage: Callable[[Path], DiskUsage] = _default_disk_usage,
) -> PreflightReport:
    """Run every readiness check; no LLM/GitHub call, no database writes."""
    return PreflightReport(
        checks=[
            _check_postgres(settings, ping),
            _check_template(settings, exists=template_exists, meta=template_meta),
            _check_contracts(contracts_dir),
            _check_github_mode(settings),
            _check_model(settings, model),
            _check_harness(),
            _check_disk(disk_usage),
            _check_api_key(settings),
        ]
    )


__all__ = [
    "CheckStatus",
    "MIN_FREE_DISK_BYTES",
    "PreflightCheck",
    "PreflightReport",
    "run_preflight",
]
