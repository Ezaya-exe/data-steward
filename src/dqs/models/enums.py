"""Closed enums shared across components (docs/ARCHITECTURE.md §6, §7).

Why here: violations, incidents, contracts and ground-truth records all carry
one of these values; a single str-Enum source of truth keeps YAML/DB/Pydantic
representations identical (str subclass so YAML and JSON dump the value text).
"""

from __future__ import annotations

from enum import StrEnum


class Layer(StrEnum):
    """Warehouse layer a contract describes (§7.1)."""

    RAW = "raw"
    STAGING = "staging"
    MARTS = "marts"


class LogicalType(StrEnum):
    """Contract logical types (§7.1); raw profiling infers, staging copies PG types."""

    INTEGER = "integer"
    NUMERIC = "numeric"
    TIMESTAMP = "timestamp"
    DATE = "date"
    STRING = "string"
    BOOLEAN = "boolean"


class ContractStatus(StrEnum):
    """Checks enforce approved contracts only (§7.1)."""

    DRAFT = "draft"
    APPROVED = "approved"


class Severity(StrEnum):
    """Violation/incident severity (§7.3)."""

    BREAKING = "breaking"
    WARNING = "warning"


class IncidentStatus(StrEnum):
    """Incident lifecycle (§4.3). Only the open family is de-duplicated into."""

    OPEN = "open"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    REJECTED = "rejected"
    FAILED = "failed"


class FaultKind(StrEnum):
    """The 10 fault kinds of the catalog (§6). Values are the spec-string heads."""

    COLUMN_RENAME = "column_rename"
    COLUMN_REMOVED = "column_removed"
    TYPE_CHANGE = "type_change"
    NULL_SPIKE = "null_spike"
    ORPHAN_KEYS = "orphan_keys"
    DUPLICATE_ROWS = "duplicate_rows"
    SCALE_SHIFT = "scale_shift"
    LATE_BATCH = "late_batch"
    MISSING_TABLE = "missing_table"
    COLUMN_ADDED = "column_added"


class RootCauseCategory(StrEnum):
    """Closed diagnosis categories (§6)."""

    SOURCE_COLUMN_RENAMED = "SOURCE_COLUMN_RENAMED"
    SOURCE_COLUMN_REMOVED = "SOURCE_COLUMN_REMOVED"
    SOURCE_TYPE_CHANGED = "SOURCE_TYPE_CHANGED"
    UPSTREAM_NULLS_INTRODUCED = "UPSTREAM_NULLS_INTRODUCED"
    REFERENTIAL_INTEGRITY_BROKEN = "REFERENTIAL_INTEGRITY_BROKEN"
    DUPLICATE_RECORDS = "DUPLICATE_RECORDS"
    UNIT_OR_SCALE_CHANGE = "UNIT_OR_SCALE_CHANGE"
    LATE_OR_MISSING_BATCH = "LATE_OR_MISSING_BATCH"
    SOURCE_COLUMN_ADDED = "SOURCE_COLUMN_ADDED"
    UNKNOWN = "UNKNOWN"


class ResolutionType(StrEnum):
    """What the workflow should publish after investigation (§6)."""

    PR = "PR"
    ISSUE = "ISSUE"
    NONE = "NONE"
