"""Benchmark provenance: prompt hashes and the harness version (Phase 8a).

Why this exists: a benchmark number is only meaningful together with the exact
prompts and harness that produced it (`ARCHITECTURE.md` §14 integrity rule).
Every results file records this module's values so a later prompt edit is
visible as a diff in the metadata instead of silently changing the score.

`HARNESS_VERSION` is bumped whenever the *plumbing* changes in a way that can
affect a scenario (investigator loop, salvage path, runner), not for a comment.
The benchmark-frozen baseline at the end of Phase 8a is recorded in
`docs/DECISIONS.md`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
INVESTIGATOR_PROMPT: Final = REPO_ROOT / "src" / "dqs" / "agents" / "prompts" / "investigator.md"
FIXER_PROMPT: Final = REPO_ROOT / "src" / "dqs" / "agents" / "prompts" / "fixer.md"

# 8a.1: RootCause finish tool + failed_generation salvage + daily-quota guard.
HARNESS_VERSION: Final = "8a.1"


def file_hash(path: Path) -> str:
    """SHA-256 of a file's bytes, or an empty string when it does not exist."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def prompt_hashes() -> dict[str, str]:
    """The two agent prompts whose wording the benchmark freezes."""
    return {
        "investigator": file_hash(INVESTIGATOR_PROMPT),
        "fixer": file_hash(FIXER_PROMPT),
    }


def harness_metadata() -> dict[str, object]:
    """Everything a results file records about *how* the run was produced."""
    return {
        "harness_version": HARNESS_VERSION,
        "prompt_hashes": prompt_hashes(),
    }


__all__ = [
    "FIXER_PROMPT",
    "HARNESS_VERSION",
    "INVESTIGATOR_PROMPT",
    "file_hash",
    "harness_metadata",
    "prompt_hashes",
]
