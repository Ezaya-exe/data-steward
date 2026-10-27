# Phase 8c — scoring corrections, budget-cap diagnosis, investigator finalization

## What was built

- `src/dqs/bench/scoring.py` — batch-level detection (`BATCH_LEVEL_CHECKS`) and
  three explicit fix scopes (`pr_expected`, `fix_attempted`, `verified`); no
  `is True` coercion of a missing verification.
- `src/dqs/bench/summary.py` — `StepSummary` (ordered tools, counts, tokens per
  node, events, fix attempts, failed criteria) plus read-only loaders for
  `dq.agent_steps` and `dq.fix_attempts`.
- `src/dqs/bench/rescore.py` — offline rescore into a new, self-labelled file;
  refuses to overwrite the source or an existing destination.
- `src/dqs/bench/report.py` — new metric columns, explicit denominator text,
  diagnostic/rescore metadata and notes.
- `src/dqs/bench/runner.py` — writes the step summary and fix history into every
  result record; `--diagnostic` option and metadata.
- `src/dqs/bench/harness.py` — `HARNESS_VERSION = "8c.1"`.
- `src/dqs/cli.py` — `bench run --diagnostic` and `dqs bench rescore`.
- `src/dqs/agents/investigator.py` — `calls_remaining` on every tool result;
  one tools-disabled `RootCause` synthesis when the tool ceiling stops the loop.
- `src/dqs/agents/prompts/investigator.md` — generic budget/finishing wording;
  removed the stale "no RootCause tool" claim. No fault names or hints.
- `src/dqs/workflow/nodes.py` — budget-exhaustion error text matches the new
  path.
- Tests: `tests/unit/test_bench_scoring.py`, `test_bench_report.py`,
  `test_bench_summary.py`, `test_bench_rescore.py`, `test_investigator.py`.
- `docs/review/budget-cap-diagnosis.md` — the four-scenario diagnosis and the
  one-scenario confirmation after the fix.

## How to run it

```powershell
# unit + integration (Postgres up), same as the project gates
.venv\Scripts\dqs dev lint
.venv\Scripts\dqs dev test
.venv\Scripts\dqs dev test-int

# rescore an existing (git-ignored) results file into a NEW file
.venv\Scripts\dqs bench rescore benchmarks\results\20261026T181316Z_deepseek-deepseek-v4.1-flash.json

# a diagnostic subset; labelled in the file and report, never evidence
.venv\Scripts\dqs bench run --split dev --model deepseek/deepseek-v4.1-flash `
  --ids f5-orders-customer-35,f4-customers-state-35,f6-orders-03,f3-order-items-unit-price `
  --diagnostic
```

No holdout run, no `--confirm-holdout`, no GitHub call. `GITHUB_MODE` stayed
`dry_run`; the benchmark forces it regardless.

## Acceptance results

### A1 — batch-level detection: PASS

`BATCH_LEVEL_CHECKS = {freshness.sla, monitor.missing_batch}` match on the check
name; table-level faults still require the scenario table. Tests cover a
batch-level incident with `table=None`, a different table carrying the right
batch check, the wrong batch check, and the wrong table for a table-level fault.
`pytest tests/unit/test_bench_scoring.py` passes.

### A2 — honest fix metrics: PASS

The rescored dev-run-1 file reports:

| scope | value |
|---|---|
| PR-expected | 13 |
| attempted | 7 |
| delivered valid (PR + verifier pass) | 7/13 = 53.8% |
| verifier pass given attempt | 7/7 = 100% |
| semantic given verified | 7/7 = 100% |
| verified but wrong | 0/7 |
| false positives | 0/6 |

The six dev-run-1 scenarios with zero `dq.fix_attempts` rows are unit-tested as
`fix_attempted=False`, `verifier_passed=None`, excluded from the
attempt-conditioned denominator. A legacy record without attempt provenance is
kept `None`, never guessed.

### A3 — compact step summary: PASS

Every 2026-10-27 result record contains `step_summary` with the ordered
`tool_sequence`, `tool_calls`, `llm_calls`, `tokens_by_node`, `events`,
`fix_attempts` and `failed_criteria`. The diagnostic files record
`['budget_exhausted']` and, after the fix,
`['budget_exhausted', 'budget_synthesis=native_structured',
'final_channel=budget_synthesis:native_structured']`.

### A4 — offline rescore: PASS

`dqs bench rescore` wrote
`benchmarks/results/20261026T181316Z_deepseek-deepseek-v4.1-flash_rescore.json`
with `run_id=...-rescore`, `rescore_of`, `rescore_at`, `rescore_note`,
`diagnostic=true`; the source file was unchanged. Tests assert a second run
refuses to overwrite and a source==destination write is refused.

Detection remains 33/37 (89.2%), category 21/37 (56.8%), columns 27/27,
resolution 31/37 (83.8%). Detection cannot improve offline because the incident
list was never stored (see Limitations).

### B — budget-cap diagnosis: PASS

Four scenarios, one per capped family from dev-run-1, same model and limits:
all four hit exactly 10 tool calls after 4 model turns, with
`budget_exhausted` as the only event and no finish attempt. The decisive fact
was already observed in each run; at least 6/40 calls were guard/SQL errors.
`docs/review/budget-cap-diagnosis.md` records sequences, evidence sufficiency,
repeats, truncation, rejected finish attempts and the plain cause.
Results file: `20261027T125219Z_...` (diagnostic).

### C1 — `calls_remaining` on every tool result: PASS

Unit tests assert the tags `calls_remaining="1"` and `"0"` on the two tool
messages the model sees, and the same numbers in the step log. The value comes
from `BudgetTracker`, never the model.

### C2 — one final tools-disabled answer: PASS

Tests cover: a model that never finishes → synthesis yields a valid
`RootCause`; invalid final output → `UNKNOWN` + `ISSUE` with partial findings;
provenance filter still drops a tool that was not called. A token/wall-clock
ceiling (and a zero tool-call budget) does **not** spend another call, preserving
the hard limits.

Real-model confirmation: the same orphan-keys scenario that previously returned
`UNKNOWN`/`ISSUE` returned `REFERENTIAL_INTEGRITY_BROKEN` on
`raw.orders.customer_id`, confidence 0.65, `ISSUE`, with seven evidence items
all citing called tools. The loop still spent 10 calls
(`20261027T131407Z_...`, diagnostic, n=1).

### C3 — routing unchanged: PASS

`plan_resolution` still returns `ISSUE` at 0.59 and `PR` at 0.6 for a
PR-eligible category; the investigator test asserts this on the synthesized
`RootCause`. No change to `resolve.py`, tools, contracts, scenarios, oracle or
injectors.

### Project gates: PASS

- `dqs dev lint` — ruff check, ruff format check and `mypy src` clean
  (104 source files, 219 formatted).
- Unit suite — **722 passed, 1 skipped** (Windows symlink capability), 106
  deselected.
- Integration suite — **100 passed, 6 skipped** (LLM-gated), run 2026-10-27
  against Docker Postgres.

## Design choices and why

- **Batch-level checks are structural, not per-fault.** `freshness.sla` and
  `monitor.missing_batch` carry no table; matching them by name is the only
  stable rule and cannot special-case a fault (DECISIONS 145).
- **Three fix scopes keep "not attempted" visible.** A single
  "fix validity" rate mixed three different questions; the scopes make the
  denominator explicit and print `n/a (0/0)` instead of a fake 0% (146).
- **Legacy records are not guessed.** A verifier pass implies an attempt; a
  failure or unknown does not justify inferring "no attempt" (147).
- **Rescore is derived data.** It writes a new file and marks itself diagnostic
  so a corrected number can never be mistaken for a new measurement (149).
- **Finalization is safer than a bigger budget.** The diagnosis showed evidence
  was sufficient at the ceiling; one tools-disabled call recovers the answer
  without paying for more exploratory calls, and the token/time limits remain
  hard (152–155).
- **The budget tag lives outside `<data>`.** Re-serializing the payload to add a
  field could truncate a 4 KB tool result a second time; a sibling tag leaves
  the data byte-for-byte intact.

## Concepts used

- **Conditional denominators.** A rate is only meaningful over the cases that
  could enter it: verifier passes over attempted fixes, semantics over verified
  fixes. Printing the numerator/denominator pair prevents a missing attempt from
  looking like a failure.
- **Structural vs. semantic matching.** A late batch has no table, so a
  table-based match is structurally impossible; matching the check name is a
  property of the data model, not a hint about the answer.
- **Bounded autonomy with a finalization step.** The tool/token/time limits stop
  runaway loops, but a limit should end exploration, not the ability to report
  what was already observed. One no-tools call is the smallest change that
  preserves both properties.
- **Provenance filtering.** Model citations are untrusted input; keeping only
  tools that actually ran turns a fabricated citation into a detectable,
  droppable event instead of a published claim.
- **Derived evidence.** A rescore recomputes functions of stored data; it is not
  a new experiment. Labelling it `diagnostic`/`rescore_of` keeps measurement and
  re-analysis separate.

## Deviations from the docs

- **A1 cannot be applied retroactively to dev-run-1.** The results file does not
  store incidents, so rescoring preserves `detected`. The corrected rule is
  tested and will apply to the next run; the limitation is printed in every
  rescored file's `rescore_note`.
- **The real-model confirmation is n=1.** The phase's C evidence is the unit
  suite plus one diagnostic run, not a benchmark measurement.
- **The prompt changed, so the fingerprint changed.** `HARNESS_VERSION` stays
  `8c.1` (the task named that value); the code fingerprint moved from
  `e56a6651...` to `9104f193...` because the prompt and investigator are
  fingerprint inputs. A future run will record the new hash.

## Known limitations / TODO

- Rescore cannot recover detection, and legacy `fix_attempted=None` records stay
  out of the attempt denominator.
- The 2026-10-26 `catalogue_size=4` label bug is still unfixed; the rescored
  sibling preserves the label. Fix it from a clean run, never by hand-editing
  results.
- The tool ceiling itself is unchanged: the confirmation still spent 10 calls
  (30,955 tokens). Synthesis is skipped after a token/wall-clock stop or a
  zero-call budget.
- The 6/40 guard/SQL errors are not fixed: `_batch_id` vs `batch_id`, the
  `AND` rejection and `information_schema` are tool-usability findings for a
  later change.
- Synthesis success and confidence calibration need a clean benchmark; one
  scenario cannot estimate either.
- The two 2026-10-27 files are diagnostic, not benchmark evidence. README and
  CLAIMS stay `TBD`/PENDING until a clean final run.
