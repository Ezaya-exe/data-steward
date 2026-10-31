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
import subprocess
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
INVESTIGATOR_PROMPT: Final = REPO_ROOT / "src" / "dqs" / "agents" / "prompts" / "investigator.md"
FIXER_PROMPT: Final = REPO_ROOT / "src" / "dqs" / "agents" / "prompts" / "fixer.md"

# 8a.1: RootCause finish tool + failed_generation salvage + daily-quota guard.
# Phase 8b: fingerprint provenance, infra-error handling, token ledger, repeats.
# 8c.1: batch-scoped detection, non-coercing fix metrics, step summary, rescore.
# 8c.2: null-plan fix attempts, fixer output ceiling, finish_reason/reasoning log.
HARNESS_VERSION: Final = "8c.2"

# Files whose content can change a benchmark result. The hash is deliberately
# broader than `HARNESS_VERSION`: it covers every agent/tool/verification/workflow
# module, the contract logic, the injector, the scorer/catalogue/oracle, and every
# prompt file. A manual version bump is still useful for humans, but the
# fingerprint is what the runner compares across runs.
_FINGERPRINT_PACKAGES: Final = ("agents", "tools", "verify", "workflow", "checks", "contracts")
_FINGERPRINT_FILES: Final = (
    "resolve.py",
    "sim/faults.py",
    "bench/scoring.py",
    "bench/scenarios.py",
    "bench/oracle.py",
)


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


def _fingerprint_paths(root: Path) -> list[Path]:
    package_root = root / "src" / "dqs"
    paths: list[Path] = []
    for package in _FINGERPRINT_PACKAGES:
        paths.extend(sorted((package_root / package).rglob("*.py")))
    paths.extend(sorted((package_root / "agents" / "prompts").glob("*")))
    paths.extend(package_root / relative for relative in _FINGERPRINT_FILES)
    return [path for path in paths if path.is_file()]


def code_fingerprint(root: Path | None = None) -> str:
    """One SHA-256 over every source/prompt file that can affect results.

    Paths are included in the digest, so a rename or a new file changes the
    fingerprint even when the bytes are identical. The set is intentionally the
    same one documented in `docs/DECISIONS.md`; changing it is a harness change.
    """
    base = root or REPO_ROOT
    digest = hashlib.sha256()
    for path in _fingerprint_paths(base):
        relative = path.relative_to(base).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def git_dirty(cwd: Path | None = None) -> bool:
    """True when the working tree has uncommitted or untracked changes.

    A benchmark result from a dirty tree is still valid, but the dirty flag warns
    a reader that the recorded commit does not fully describe the code that ran.
    Outside a git checkout this returns False; `git_sha` still records `unknown`.
    """
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(cwd or REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def harness_metadata() -> dict[str, object]:
    """Everything a results file records about *how* the run was produced."""
    return {
        "harness_version": HARNESS_VERSION,
        "prompt_hashes": prompt_hashes(),
        "code_fingerprint": code_fingerprint(),
    }


__all__ = [
    "FIXER_PROMPT",
    "HARNESS_VERSION",
    "INVESTIGATOR_PROMPT",
    "code_fingerprint",
    "file_hash",
    "git_dirty",
    "harness_metadata",
    "prompt_hashes",
]
