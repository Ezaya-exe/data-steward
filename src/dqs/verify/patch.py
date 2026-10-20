"""Hardened allowlist and exact-match application for fix plans (§11 steps 1–2).

Why this module exists: a patch is untrusted input even when a human wrote it.
The only writable files are SQL models inside `warehouse/dbt_project/models/`;
everything else (tests, contracts, macros, `profiles.yml`, the verification
code itself) is out of bounds. The checks are deliberately fail-closed and
written for Windows, where `\\` and `/` are both separators, case is ignored, and
drive/UNC/8.3 forms can otherwise smuggle a path outside the allowlist.

Application is exact-match and sequential: `old` must occur exactly once in the
file at the moment it is applied. There is no regex, no fuzzy matching, and no
"apply anyway" mode.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath
from typing import Final

from dqs.errors import FixPlanError, LineageError
from dqs.lineage.graph import LineageGraph
from dqs.models.fix import Edit, FixPlan

PROJECT_DIR: Final = Path(__file__).resolve().parents[3] / "warehouse" / "dbt_project"
MODELS_DIR: Final = "models"
PROJECT_PREFIX: Final = ("warehouse", "dbt_project")
SQL_SUFFIX: Final = ".sql"
# Windows devices are reserved regardless of extension: `CON.sql` is not a file
# a reviewer can ever open, and it is a classic traversal/steering trick.
_RESERVED_NAMES: Final = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_PART_RE: Final = re.compile(r"^[A-Za-z0-9_.-]+$")
_DRIVE_RE: Final = re.compile(r"^[A-Za-z]:")
SANDBOX_SCHEMA_RE: Final = re.compile(r"^dqs_sandbox_[a-z0-9_]{1,40}$")
IGNORED_TOP_LEVEL: Final = frozenset({"target", "dbt_packages", "logs", "__pycache__"})
COPY_IGNORE: Final = shutil.ignore_patterns(*sorted(IGNORED_TOP_LEVEL))


def normalize_edit_path(raw: str) -> str:
    """Validate separators/traversal and return a project-relative POSIX path.

    This is syntax/shape checking only; `validate_edit_paths` proves the file
    exists and resolves inside `models/`.
    """
    if not raw:
        raise FixPlanError("edit path is empty")
    if "\x00" in raw:
        raise FixPlanError("edit path contains a NUL byte")
    if raw != raw.strip():
        raise FixPlanError(f"edit path {raw!r} has leading/trailing whitespace")

    # Check absolute forms before normalizing separators: a UNC path starts
    # with two backslashes and `PureWindowsPath` catches both it and `C:\...`.
    windows = PureWindowsPath(raw)
    if windows.is_absolute() or windows.drive:
        raise FixPlanError(f"absolute or drive-qualified edit path is not allowed: {raw!r}")
    if _DRIVE_RE.match(raw):
        raise FixPlanError(f"drive-relative edit path is not allowed: {raw!r}")

    normalized = raw.replace("\\", "/")
    if normalized.startswith("/"):
        raise FixPlanError(f"absolute edit path is not allowed: {raw!r}")
    if "//" in normalized:
        raise FixPlanError(f"empty path segment is not allowed: {raw!r}")

    parts = [part for part in normalized.split("/") if part != "."]
    if not parts:
        raise FixPlanError("edit path is empty")
    while len(parts) >= 2 and tuple(p.lower() for p in parts[:2]) == PROJECT_PREFIX:
        parts = parts[2:]
    if len(parts) < 2 or parts[0].lower() != MODELS_DIR:
        raise FixPlanError(f"edit path must be under warehouse/dbt_project/models/: {raw!r}")

    for part in parts:
        if part in ("..", "."):
            raise FixPlanError(f"path traversal is not allowed: {raw!r}")
        if not _PART_RE.match(part):
            raise FixPlanError(f"unsafe path segment {part!r} in {raw!r}")
        if part.endswith((".", " ")):
            raise FixPlanError(f"path segment {part!r} ends with a dot/space")
        if part.split(".", maxsplit=1)[0].upper() in _RESERVED_NAMES:
            raise FixPlanError(f"reserved Windows device name in path: {raw!r}")

    if not parts[-1].lower().endswith(SQL_SUFFIX):
        raise FixPlanError(f"only .sql model files may be edited: {raw!r}")
    return "/".join(parts)


def validate_edit_paths(edits: Sequence[Edit], project_dir: Path = PROJECT_DIR) -> list[str]:
    """Canonicalize every path and prove it is a real, non-symlinked model file.

    Returns project-relative POSIX paths in edit order. Any failure raises
    `FixPlanError` before a temp copy is created.
    """
    models_root = (project_dir / MODELS_DIR).resolve()
    canonical: list[str] = []
    for edit in edits:
        relative = normalize_edit_path(edit.path)
        candidate = project_dir.joinpath(*relative.split("/"))
        if not candidate.exists():
            raise FixPlanError(f"edit target does not exist: {edit.path!r}")
        if not candidate.is_file():
            raise FixPlanError(f"edit target is not a file: {edit.path!r}")

        current = models_root
        for part in relative.split("/")[1:]:
            current = current / part
            if _is_link(current):
                raise FixPlanError(f"symlinks are not allowed in edit paths: {edit.path!r}")

        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(models_root):
            raise FixPlanError(f"edit target escapes models/: {edit.path!r}")
        canonical.append(relative)
    return canonical


def model_file_for_path(path: str, graph: LineageGraph) -> str:
    """Map a canonical `models/...sql` path to its dbt model unique_id."""
    wanted = path.replace("\\", "/").lower()
    for unique_id, payload in graph.payloads():
        if payload.get("resource_type") != "model":
            continue
        original = _manifest_relative(payload.get("original_file_path", ""))
        if original.lower() == wanted:
            return unique_id
    raise FixPlanError(f"no dbt model corresponds to {path!r} (is the manifest current?)")


def validate_relevance(
    canonical_paths: Sequence[str],
    graph: LineageGraph,
    affected_table: str,
) -> dict[str, str]:
    """Require every edited model to be a staging model directly over the raw table.

    The rule is intentionally narrow: a fix for one incident must not wander
    into unrelated models. Returns {canonical path: model unique_id}.
    """
    try:
        source_id = graph.nodes_for_raw_table(affected_table)
    except LineageError as exc:
        raise FixPlanError(f"cannot resolve affected raw table {affected_table!r}: {exc}") from exc

    resolved: dict[str, str] = {}
    for path in canonical_paths:
        unique_id = model_file_for_path(path, graph)
        payload = graph.node(unique_id)
        original = _manifest_relative(payload.get("original_file_path", ""))
        if not original.lower().startswith(f"{MODELS_DIR}/staging/"):
            raise FixPlanError(
                f"edited model {unique_id} is not a staging model "
                f"(path {original!r}); fixes may only edit models/staging/*.sql"
            )
        depends_on = payload.get("depends_on") or {}
        direct = depends_on.get("nodes") if isinstance(depends_on, dict) else None
        if not isinstance(direct, list) or source_id not in direct:
            raise FixPlanError(
                f"edited model {unique_id} does not directly select from {affected_table}; "
                "the edit is unrelated to this incident"
            )
        resolved[path] = unique_id
    return resolved


def model_names(unique_ids: Sequence[str]) -> list[str]:
    """dbt model names (the last unique_id segment), stable and de-duplicated."""
    names: list[str] = []
    for unique_id in unique_ids:
        name = unique_id.rsplit(".", 1)[-1]
        if name not in names:
            names.append(name)
    return names


def copy_project(source: Path, destination: Path) -> Path:
    """Copy a dbt project without build artifacts or installed packages."""
    source = source.resolve()
    if not source.is_dir():
        raise FixPlanError(f"dbt project directory does not exist: {source}")
    if destination.exists():
        raise FixPlanError(f"destination already exists: {destination}")
    shutil.copytree(source, destination, ignore=COPY_IGNORE)
    return destination


def apply_edits(edits: Sequence[Edit], project_dir: Path) -> list[str]:
    """Apply edits sequentially to `project_dir`; return the touched paths.

    Bytes are read/written without newline translation so untouched text stays
    byte-identical (important for reviewable diffs and deterministic PRs).
    """
    touched: list[str] = []
    for index, edit in enumerate(edits, start=1):
        relative = normalize_edit_path(edit.path)
        path = project_dir.joinpath(*relative.split("/"))
        try:
            text = path.read_bytes().decode("utf-8")
        except FileNotFoundError as exc:
            raise FixPlanError(
                f"edit {index}: file disappeared from the temp copy: {relative}"
            ) from exc
        except UnicodeDecodeError as exc:
            raise FixPlanError(f"edit {index}: {relative} is not valid UTF-8") from exc

        matches = text.count(edit.old)
        if matches != 1:
            raise FixPlanError(
                f"edit {index}: 'old' must occur exactly once in {relative}, found {matches}"
            )
        text = text.replace(edit.old, edit.new, 1)
        path.write_bytes(text.encode("utf-8"))
        touched.append(relative)
    return touched


def sandbox_schema_name(plan: FixPlan) -> str:
    """`dqs_sandbox_<short_id>`: incident-derived, else plan-hash derived.

    The base schema is namespaced further by the project macro
    (`_staging`/`_marts`); cleanup drops all three.
    """
    if plan.incident_id is not None:
        short = plan.incident_id.hex[:8]
    else:
        digest = hashlib.sha256(plan.model_dump_json().encode("utf-8")).hexdigest()
        short = digest[:8]
    name = f"dqs_sandbox_{short}"
    if not SANDBOX_SCHEMA_RE.match(name):
        raise FixPlanError(f"refusing unsafe sandbox schema name {name!r}")
    return name


def is_sandbox_schema(name: str) -> bool:
    """Exactly the schemas this project owns and may drop."""
    return SANDBOX_SCHEMA_RE.match(name) is not None


def validate_plan(plan: FixPlan, project_dir: Path = PROJECT_DIR) -> list[str]:
    """Allowlist + shape validation; returns canonical edit paths."""
    return validate_edit_paths(plan.edits, project_dir)


def ignored_at_top_level(name: str) -> bool:
    """Public form of the copy ignore rule (unit-testable)."""
    return name.lower() in IGNORED_TOP_LEVEL


def replace_once(text: str, old: str, new: str) -> str:
    """Pure exact-match helper shared by tests and `apply_edits`."""
    matches = text.count(old)
    if matches != 1:
        raise FixPlanError(f"'old' must occur exactly once, found {matches}")
    return text.replace(old, new, 1)


def _manifest_relative(path: object) -> str:
    normalized = str(path).replace("\\", "/")
    return normalized[2:] if normalized.startswith("./") else normalized


def _is_link(path: Path) -> bool:
    """True for symlinks and Windows junctions/reparse points we must not follow."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if callable(is_junction) else False


__all__ = [
    "COPY_IGNORE",
    "is_sandbox_schema",
    "model_names",
    "normalize_edit_path",
    "sandbox_schema_name",
    "validate_edit_paths",
    "validate_relevance",
]
