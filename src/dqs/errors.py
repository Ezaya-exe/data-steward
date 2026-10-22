"""Exception hierarchy for dqs.

Every module raises one of these instead of bare exceptions so the CLI can
print a precise message and exit non-zero without swallowing errors
(AGENTS.md §6). Third-party failures (SQLAlchemy, subprocess, filesystem)
are wrapped at module boundaries with their original cause attached.
"""

from __future__ import annotations


class DqsError(Exception):
    """Base class for every error dqs raises."""


class ConfigError(DqsError):
    """Configuration or environment is invalid/incomplete."""


class FaultSpecError(DqsError):
    """A fault spec string (or its parameters) is malformed or out of range."""


class ContractError(DqsError):
    """A data-contract file is missing, malformed, or fails strict validation."""


class CheckError(DqsError):
    """The check engine was asked for a batch/snapshot it cannot evaluate."""


class DatabaseError(DqsError):
    """The warehouse is unreachable, or init/reset/DDL failed."""


class LineageError(DqsError):
    """(dbt) lineage cannot be read/built: missing manifest, unknown node, bad SQL."""


class LLMError(DqsError):
    """The LLM provider is unavailable, misconfigured, or returned unusable output."""


class InvestigatorError(DqsError):
    """The investigator could not run or could not produce a valid RootCause."""


class BudgetExceeded(DqsError):
    """An incident investigation exceeded a hard budget (calls, tokens, or time)."""


class SqlGuardError(DqsError):
    """A SQL string was rejected by the read-only guard (AGENTS.md §7.2)."""


class SqlExecutionError(DqsError):
    """A guarded SQL statement failed while running on the read-only role."""


class ToolError(DqsError):
    """An agent tool could not answer (bad input, missing row, unsafe identifier)."""


class FixPlanError(DqsError):
    """A fix plan violated the allowlist, relevance rule, size limit, or exact match."""


class FixerError(DqsError):
    """The fixer could not build its context or produce a valid FixPlan."""


class IssueBuildError(DqsError):
    """An issue cannot be rendered from the supplied incident artifacts."""


class SandboxError(DqsError):
    """The sandbox could not be prepared, built, or cleaned up."""


class PublicationError(DqsError):
    """A publication was refused, misconfigured, or failed at the GitHub boundary."""


class WorkflowError(DqsError):
    """The incident workflow is misconfigured or cannot continue from its checkpoint."""
