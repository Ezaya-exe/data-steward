"""Unit tests for the `dqs dev` wrappers (pure command builders; no subprocess)."""

from __future__ import annotations

import sys
from pathlib import Path

from dqs import dev


def test_compose_up_waits_for_health() -> None:
    assert dev.compose_cmd("up", "-d", "--wait", "postgres") == [
        "docker",
        "compose",
        "up",
        "-d",
        "--wait",
        "postgres",
    ]


def test_pytest_marker_selection() -> None:
    unit = dev.pytest_cmd("-m", "not integration")
    integration = dev.pytest_cmd("-m", "integration")
    assert unit[:3] == [sys.executable, "-m", "pytest"]
    assert unit[-2:] == ["-m", "not integration"]
    assert integration[-2:] == ["-m", "integration"]


def test_lint_tool_cmds_are_current_interpreter() -> None:
    assert dev.ruff_cmd("check", ".")[:3] == [sys.executable, "-m", "ruff"]
    assert dev.mypy_cmd("src")[:3] == [sys.executable, "-m", "mypy"]
    assert dev.mypy_cmd("src")[-1] == "src"


def test_run_propagates_exit_code(tmp_path: Path) -> None:
    code = dev.run([sys.executable, "-c", "raise SystemExit(3)"], cwd=tmp_path)
    assert code == 3
