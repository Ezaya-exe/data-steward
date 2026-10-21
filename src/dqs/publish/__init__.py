"""Publishing package (docs/ARCHITECTURE.md §12): dry-run by default.

Public surface:
- `ApprovalDecision` / `Publisher` / `PublicationResult` (base)
- `DryRunPublisher`, `run_dry_run` (default mode; `out/pr_previews/`)
- `GitHubPublisher`, `PyGithubClient`, `build_publisher` (live mode)
- sanitization, deterministic diff and PR body helpers
"""

from __future__ import annotations

from dqs.publish.base import (
    ApprovalDecision,
    PublicationKind,
    PublicationMode,
    PublicationResult,
    Publisher,
)
from dqs.publish.body import REQUIRED_SECTIONS, REVIEW_FOOTER, render_pr_body
from dqs.publish.diff import FilePatch, build_file_patches, full_patch
from dqs.publish.dry_run import DryRunPublisher, run_dry_run
from dqs.publish.github import (
    GitHubPublisher,
    PyGithubClient,
    branch_name,
    build_publisher,
)
from dqs.publish.store import DbPublicationStore, MemoryPublicationStore

__all__ = [
    "REQUIRED_SECTIONS",
    "REVIEW_FOOTER",
    "ApprovalDecision",
    "DbPublicationStore",
    "DryRunPublisher",
    "FilePatch",
    "GitHubPublisher",
    "MemoryPublicationStore",
    "PublicationKind",
    "PublicationMode",
    "PublicationResult",
    "Publisher",
    "PyGithubClient",
    "branch_name",
    "build_file_patches",
    "build_publisher",
    "full_patch",
    "render_pr_body",
    "run_dry_run",
]
