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


class DatabaseError(DqsError):
    """The warehouse is unreachable, or init/reset/DDL failed."""
