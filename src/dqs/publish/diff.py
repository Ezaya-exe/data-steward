"""Deterministic unified diffs for a verified fix plan (§12).

Why: the dry-run preview must show exactly what live mode would commit, and
live mode must commit exactly what the verifier approved. The real model files
are never written in this phase: the edits are replayed in memory against the
current file contents on disk, then rendered with `difflib.unified_diff`. The
same `FilePatch` objects feed both the `.patch` preview and the GitHub commit,
so the two cannot drift.

Applying edits here reuses the Phase-5a allowlist and exact-match rules
(`dqs.verify.patch`): project-relative paths under `models/`, no traversal or
symlinks, and `old` must occur exactly once at the moment it is applied.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from dqs.errors import FixPlanError
from dqs.models.fix import Edit
from dqs.verify.patch import PROJECT_DIR, replace_once, validate_edit_paths


class FilePatch(BaseModel):
    """One file's old/new content plus its reviewable unified diff."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, description="Project-relative POSIX path.")
    old_text: str
    new_text: str
    patch: str = Field(min_length=1)
    added: int = Field(ge=0)
    removed: int = Field(ge=0)


def make_patch(path: str, old_text: str, new_text: str) -> FilePatch:
    """Render one exact unified diff with stable LF control lines."""
    diff_lines = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="\n",
        )
    )
    if not diff_lines:
        raise FixPlanError(f"edit produced no diff for {path!r}")
    # A file without a trailing newline makes difflib emit a final line without
    # one; terminating it keeps multi-file patches concatenable.
    patch = "".join(line if line.endswith("\n") else f"{line}\n" for line in diff_lines)
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    return FilePatch(
        path=path,
        old_text=old_text,
        new_text=new_text,
        patch=patch,
        added=added,
        removed=removed,
    )


def build_file_patches(edits: Sequence[Edit], project_dir: Path = PROJECT_DIR) -> list[FilePatch]:
    """Apply edits in order in memory and return one patch per touched file.

    Raises `FixPlanError` before anything is written if a path is outside the
    allowlist, the file is unreadable/non-UTF-8, or an `old` string does not
    occur exactly once at its turn. The project on disk is never modified.
    """
    canonical = validate_edit_paths(edits, project_dir)
    originals: dict[str, str] = {}
    current: dict[str, str] = {}
    order: list[str] = []

    for index, (edit, relative) in enumerate(zip(edits, canonical, strict=True), start=1):
        if relative not in current:
            target = project_dir.joinpath(*relative.split("/"))
            try:
                text = target.read_bytes().decode("utf-8")
            except FileNotFoundError as exc:
                raise FixPlanError(f"edit {index}: file disappeared: {relative}") from exc
            except UnicodeDecodeError as exc:
                raise FixPlanError(f"edit {index}: {relative} is not valid UTF-8") from exc
            originals[relative] = text
            current[relative] = text
            order.append(relative)
        try:
            current[relative] = replace_once(current[relative], edit.old, edit.new)
        except FixPlanError as exc:
            raise FixPlanError(f"edit {index} in {relative}: {exc}") from exc

    return [make_patch(path, originals[path], current[path]) for path in order]


def full_patch(patches: Sequence[FilePatch]) -> str:
    """Concatenate per-file diffs into one unified patch (git-apply friendly)."""
    return "".join(patch.patch for patch in patches)


__all__ = ["FilePatch", "build_file_patches", "full_patch", "make_patch"]
