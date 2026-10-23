"""Answer-key isolation test (AGENTS.md §7.9, Phase 8a).

The benchmark oracle can rebuild clean data for a fault date. If any
agent/verifier/workflow/publishing/tool/check module could import it, a model or
a fix could special-case the answer instead of diagnosing. This test is a
structural guard: those packages must never reference `dqs.bench`.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "src" / "dqs"

ISOLATED_PACKAGES = (
    "agents",
    "verify",
    "workflow",
    "publish",
    "tools",
    "checks",
    "sim",
    "contracts",
    "lineage",
)

BENCH_REFERENCE = re.compile(r"\bdqs\.bench\b|\bfrom\s+dqs\s+import\s+bench\b")


def test_agent_and_verification_packages_never_reference_the_benchmark() -> None:
    offenders: list[str] = []
    for package in ISOLATED_PACKAGES:
        directory = SOURCE / package
        assert directory.is_dir(), directory
        for path in directory.rglob("*.py"):
            if BENCH_REFERENCE.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == []


def test_only_bench_modules_import_the_oracle() -> None:
    offenders: list[str] = []
    for path in SOURCE.rglob("*.py"):
        if path.parent.name == "bench":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\bbench\.oracle\b|\bfrom\s+dqs\.bench\s+import\s+oracle\b", text):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == []


def test_oracle_module_does_not_import_agent_or_publish_code() -> None:
    text = (SOURCE / "bench" / "oracle.py").read_text(encoding="utf-8")
    for forbidden in ("dqs.agents", "dqs.workflow", "dqs.publish", "dqs.monitor"):
        assert forbidden not in text
