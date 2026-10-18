"""Developer convenience commands (owner-requested addition).

Equivalent to the Makefile targets so the project does not depend on `make`
being installed on this Windows host (logged in docs/DECISIONS.md). They are
wrappers only — they run the same underlying tools (docker compose, pytest,
ruff, mypy) and propagate exit codes; no logic lives here that does not also
exist as a direct command.

Every command list is a pure function so unit tests can assert exactly what
would run without invoking subprocess. `run` is the only side-effecting
piece.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

logger = logging.getLogger(__name__)


def compose_cmd(*args: str) -> list[str]:
    """A docker compose command in project scope."""
    return ["docker", "compose", *args]


def pytest_cmd(*args: str) -> list[str]:
    """A pytest invocation on the current interpreter."""
    return [sys.executable, "-m", "pytest", *args]


def ruff_cmd(*args: str) -> list[str]:
    """A ruff invocation on the current interpreter."""
    return [sys.executable, "-m", "ruff", *args]


def mypy_cmd(*args: str) -> list[str]:
    """A mypy invocation on the current interpreter."""
    return [sys.executable, "-m", "mypy", *args]


def run(cmd: Sequence[str], cwd: Path = PROJECT_ROOT) -> int:
    """Run a command from the project root, logging it; return the exit code."""
    printable = " ".join(str(part) for part in cmd)
    logger.info("run: %s", printable)
    return subprocess.run(list(cmd), cwd=cwd).returncode


def up() -> int:
    """Start Postgres and wait until healthy (`docker compose up --wait`)."""
    return run(compose_cmd("up", "-d", "--wait", "postgres"))


def down() -> int:
    """Stop Postgres."""
    return run(compose_cmd("down"))


def lint() -> int:
    """ruff check + ruff format --check + mypy, stopping at first failure."""
    for cmd in (
        ruff_cmd("check", "."),
        ruff_cmd("format", "--check", "."),
        mypy_cmd("src"),
    ):
        code = run(cmd)
        if code:
            return code
    return 0


def test() -> int:
    """Fast unit tests only (no network, no Docker, no LLM)."""
    # pytest's -m takes one argument, so the marker expression is a single token
    return run(pytest_cmd("-m", "not integration"))


def test_int() -> int:
    """Integration tests (need `dqs dev up`)."""
    return run(pytest_cmd("-m", "integration"))


def check() -> int:
    """The full quality gate: lint + test + test-int."""
    for func in (lint, test, test_int):
        code = func()
        if code:
            return code
    return 0
