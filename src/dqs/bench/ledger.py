"""Per-day token ledger shared by every benchmark invocation (Phase 8b).

Why a file: `--daily-token-cap` used to be a session limit, so two short sessions
on the same day could double the intended spend. The ledger persists UTC day ->
provider/model -> tokens (in + out) in a small git-ignored JSON file, and the
runner consults it before every scenario.

The ledger records benchmark usage only. A note printed at the start of each run
reminds the operator that `dqs demo`, the real-LLM tests and any other use of the
same API key share the provider's quota; the ledger cannot see those calls.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
DEFAULT_LEDGER_PATH: Final = REPO_ROOT / "data" / "token_ledger.json"


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


class TokenLedger:
    """A tiny JSON-backed counter keyed by UTC date, provider and model."""

    def __init__(
        self,
        path: Path = DEFAULT_LEDGER_PATH,
        *,
        today: Callable[[], dt.date] = _utc_today,
    ) -> None:
        self.path = Path(path)
        self._today = today

    def _read(self) -> dict[str, dict[str, int]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        days = payload.get("days") if isinstance(payload, dict) else None
        if not isinstance(days, dict):
            return {}
        cleaned: dict[str, dict[str, int]] = {}
        for day, entries in days.items():
            if not isinstance(day, str) or not isinstance(entries, dict):
                continue
            cleaned[day] = {
                str(key): max(0, int(value))
                for key, value in entries.items()
                if isinstance(value, int) and not isinstance(value, bool)
            }
        return cleaned

    def _write(self, days: dict[str, dict[str, int]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-replace keeps the file readable if the process is killed
        # mid-write; the data is only a budget hint, never benchmark evidence.
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"days": days}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @staticmethod
    def key(provider: str, model: str) -> str:
        return f"{provider}/{model}"

    def total(self, provider: str, model: str, *, day: dt.date | None = None) -> int:
        """Tokens already recorded for this UTC day and provider/model."""
        date_key = (day or self._today()).isoformat()
        return self._read().get(date_key, {}).get(self.key(provider, model), 0)

    def add(
        self,
        provider: str,
        model: str,
        tokens: int,
        *,
        day: dt.date | None = None,
    ) -> int:
        """Add `tokens` and return the new day/provider/model total."""
        amount = max(0, int(tokens))
        date_key = (day or self._today()).isoformat()
        days = self._read()
        entries = days.setdefault(date_key, {})
        key = self.key(provider, model)
        entries[key] = entries.get(key, 0) + amount
        self._write(days)
        return entries[key]


__all__ = ["DEFAULT_LEDGER_PATH", "TokenLedger"]
