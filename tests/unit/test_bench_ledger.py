"""Per-UTC-day token ledger tests (Phase 8b).

The ledger exists so `--daily-token-cap` is a *day* budget, not a per-process
budget. Tests pin the date and the provider/model key so they never depend on
the machine clock or the real repository file.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path

from dqs.bench.ledger import TokenLedger


def _today(day: dt.date) -> Callable[[], dt.date]:
    return lambda: day


def test_totals_accumulate_by_day_provider_and_model(tmp_path: Path) -> None:
    path = tmp_path / "token_ledger.json"
    day_one = dt.date(2026, 1, 1)
    day_two = dt.date(2026, 1, 2)

    first = TokenLedger(path, today=_today(day_one))
    assert first.add("anthropic", "model-a", 100) == 100
    assert first.add("anthropic", "model-a", 25) == 125
    assert first.add("anthropic", "model-b", 10) == 10
    assert first.add("openai", "model-a", 7) == 7

    # A new process (new instance) must see the same persisted total.
    second = TokenLedger(path, today=_today(day_one))
    assert second.total("anthropic", "model-a") == 125
    assert second.total("anthropic", "model-b") == 10
    assert second.total("openai", "model-a") == 7

    third = TokenLedger(path, today=_today(day_two))
    assert third.total("anthropic", "model-a") == 0
    assert third.add("anthropic", "model-a", 5) == 5
    assert third.total("anthropic", "model-a") == 5


def test_malformed_ledger_is_ignored_instead_of_crashing(tmp_path: Path) -> None:
    path = tmp_path / "token_ledger.json"
    path.write_text("not json", encoding="utf-8")
    ledger = TokenLedger(path, today=_today(dt.date(2026, 1, 1)))
    assert ledger.total("anthropic", "model-a") == 0
    assert ledger.add("anthropic", "model-a", 3) == 3
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["days"]["2026-01-01"]["anthropic/model-a"] == 3


def test_negative_tokens_are_clamped_to_zero(tmp_path: Path) -> None:
    path = tmp_path / "token_ledger.json"
    ledger = TokenLedger(path, today=_today(dt.date(2026, 1, 1)))
    assert ledger.add("anthropic", "model-a", -50) == 0
    assert ledger.total("anthropic", "model-a") == 0


def test_a_corrupt_day_entry_is_skipped_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "token_ledger.json"
    path.write_text(
        json.dumps(
            {
                "days": {
                    "2026-01-01": {"anthropic/model-a": 12, "bad": "nope"},
                    "not-a-day": "nope",
                }
            }
        ),
        encoding="utf-8",
    )
    ledger = TokenLedger(path, today=lambda: dt.date(2026, 1, 1))
    assert ledger.total("anthropic", "model-a") == 12
    assert ledger.total("anthropic", "bad") == 0
