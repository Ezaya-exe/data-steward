# Phase 4b review packet

Date: 2026-10-20 · Scope: Phase 4 tasks 3–4 (investigator loop, `RootCause`,
`dqs incident investigate`, prompt, real-model smoke, and the task-0
hardening). Companion: `docs/phase-notes/phase-4b.md`.

## What to review first

1. `src/dqs/agents/investigator.py` — the loop, budget boundaries, recovery,
   final-answer channels and provenance filter. This is the safety-critical
   file.
2. `src/dqs/agents/steps.py` — sink-owned `seq`, redaction and 4 KB payload
   cap; then the `DbStepSink` SQL in the same file.
3. `src/dqs/agents/prompts/investigator.md` — closed categories, falsification,
   data/instruction rule and finishing rule.
4. `src/dqs/tools/base.py` (`ToolContext`) and `src/dqs/tools/sql_guard.py`
   (allowlist) — the two task-0 boundaries.
5. `src/dqs/agents/llm.py` — retry policy and the `on_call`/`on_retry` hooks.
6. `src/dqs/cli.py` — `incident investigate` wiring (ro context, rw sink,
   persistence, printing).

## Acceptance table

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Non-`dqs_ro` engine refused; all tools use the ro engine | PASS | Unit role stub + real-rw-engine integration test; `from_settings` is the only factory |
| 2 | SQL function allowlist with the six requested bypasses rejected | PASS | 73 collected guard cases, including qualified/quoted forms and message names the function |
| 3 | LLM retries 429/5xx with backoff/`Retry-After`, bounded; others not retried | PASS | 6 retry tests + retry step-log test |
| 4 | Contract regeneration preserves approval / refuses changed content | PASS | Store unit tests + e2e `--force` behavior |
| 5 | Endless fake tool loop stops at `MAX_TOOL_CALLS` → UNKNOWN/ISSUE/0 with partial findings | PASS | 3 tool calls, 3 model calls, `budget_exhausted` step; token variant too |
| 6 | Fabricated evidence dropped and logged | PASS | Unit test; also observed in the real F1/F4 runs (`evidence_dropped`) |
| 7 | Every LLM/tool call logged with args/output/tokens/latency; secrets redacted | PASS | Unit metrics test + Postgres ordering test + DB redaction test |
| 8 | Prompt injection through `sample_rows`; no DB change; guard rejects the write | PASS | Hostile row + requested `DROP TABLE`; guard error, row count/value unchanged |
| 9 | Final `RootCause` schema-valid | PASS | Pydantic validation tests + 3/3 real runs re-validated |
| 10 | `dqs incident investigate <id>` runs only this node, prints JSON + timeline, persists | PASS | CLI integration test (exit 0, `dq.incidents.root_cause`, `dq.agent_steps`) |
| 11 | Real LLM on F1/F4/F6 | PASS | 3/3 valid: rename 0.93/PR, nulls 0.93/ISSUE, duplicates 0.95/PR |
| 12 | Lint/format/mypy/tests green; dev warehouse untouched | PASS | 345 unit + 64 integration (non-llm) + 3 llm passed; warehouse still 30 batches, 0 incidents, 0 steps |

## Verification commands (all run for this packet)

```powershell
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m ruff format --check src tests
.venv\Scripts\python -m mypy src
.venv\Scripts\python -m pytest -m "not integration" -q
.venv\Scripts\python -m pytest -m "integration and not llm" -q
$env:RUN_LLM_TESTS='1'
.venv\Scripts\python -m pytest tests\integration\test_investigator_llm.py -m llm -s -q
```

Results: clean, clean, clean (88 files), 345 passed, 64 passed (~314 s),
3 passed (~279 s). Real model `openai/gpt-oss-120b` via `openai_compatible`;
run date 2026-10-20. Outputs are in git-ignored
`out/phase-4b-llm-smoke-*.json`.

## Three weakest points

1. **The real-model evidence is three scenarios, one run each.** The diagnoses
   are correct and confidence is high, but there is no variance measurement, no
   holdout, and no comparison against a second model. The smoke also loads the
   fault with `run_dbt=False`, so the model sees raw profiles/contracts/rows but
   not a failing dbt run record. Do not describe this as benchmark quality
   (AGENTS §7.9); Phase 8 owns repeats/scoring.
2. **The final-answer recovery is provider-shaped by observation.** The
   configured gateway rejects a model-generated tool call to a non-existent
   `RootCause`/`json` tool with HTTP 400. We recover generically ("after any
   evidence, try the final channel"), which worked for all three scenarios, but
   a provider that instead returns malformed arguments to a *real* tool, or
   that cannot do native structured output *and* has no tool-call channel, will
   end in `UNKNOWN`. That is safe, but it means diagnosis quality is partly a
   provider capability, not just prompt quality. Failed calls also report zero
   tokens, so the token budget under-counts failed attempts (latency/error are
   recorded).
3. **`dq.agent_steps` ordering is only safe for sequential runs.** `DbStepSink`
   reads `MAX(seq)` then inserts; there is no unique constraint on
   `(incident_id, node, seq)`, so two concurrent investigations of the same
   incident could collide. Phase 6's one-workflow-per-incident design avoids
   this in practice, but the invariant lives in application code, not the
   schema.

Honorable mentions: the provenance check proves a cited tool *ran*, not that
the finding is *true*; the model can still misinterpret a real tool result. The
CLI/Rich table truncates names in narrow consoles (full payloads are in the
DB). Tool outputs consume context; there is no history compaction yet.

## Risks for Phase 5 (fix planner + sandbox verification)

* **Do not let the fixer inherit the investigator's read-only context.** The
  fixer edits a temp copy through the deterministic patch layer; it must never
  receive the live warehouse engine or a write-capable tool. Reuse the
  `ToolContext` role pattern for any DB access, but keep edits on the
  path-allowlist code path.
* **`RootCause` confidence is a model number, not a calibrated probability.**
  The Phase-6 routing threshold (`>= 0.6`, PR-eligible) will trust it. The real
  runs produced 0.93–0.95; do not tune the threshold on three examples.
* **Provider finishing quirks will repeat in `plan_fix`.** Use the same
  structured-output-first / submit-tool-fallback / safe-escalation pattern and
  keep the provider-error recovery explicit.
* **Evidence is a list of strings.** Verification must not rely on the
  investigator's evidence to decide pass/fail; it must re-run the checks,
  published-contract conformance and row preservation (§11). The root cause is
  an input to planning, not proof.
* **Step-log schema has no unique constraint.** If Phase 5 writes attempts or
  verification steps, either add a constraint/migration or keep the sink
  single-writer per node. `dq.fix_attempts` already has its own `attempt_no`.
* **Budget accounting across nodes.** `BudgetTracker` is per investigation
  today. Phase 6 needs one budget/timeout spanning the whole incident; do not
  silently create a fresh tracker per node without logging that decision.
