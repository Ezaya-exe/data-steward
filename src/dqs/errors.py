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


class DatabaseError(DqsError):
    """The warehouse is unreachable, or init/reset/DDL failed."""
