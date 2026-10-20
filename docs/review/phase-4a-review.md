# Phase 4a review packet

Date: 2026-10-20 · Scope: Phase 4 tasks 1–2 (LLM wrapper + read-only tools).
Companion: `docs/phase-notes/phase-4a.md`.

## What to review first

1. `src/dqs/tools/sql_guard.py` — the security boundary. Read the docstring,
   then `validate_sql` and `execute_readonly`.
2. `src/dqs/tools/base.py` — the data/instruction boundary and identifier
   allowlists.
3. `src/dqs/agents/llm.py` — the only provider construction and the budget
   semantics (`>` boundaries).
4. `src/dqs/tools/registry.py` — what the model will actually be offered.

## Acceptance table

| # | Phase-4 criterion relevant to 4a | Verdict | Evidence |
|---|---|---|---|
| 1 | Provider wrapper via `init_chat_model`; explicit providers; fake provider; token/latency capture; budget tracker | PASS | `test_llm.py` 11/11; fake CLI smoke below |
| 2 | SQL guard rejects DROP/DELETE/UPDATE/INSERT, multi-statement, comment tricks, `pg_sleep`, `pg_catalog`/other schemas, unqualified names; LIMIT auto-capped ≤200; accepts SELECT/WITH | PASS | 35 negative + 10 positive parametrized cases, 4 LIMIT tests; all pass |
| 3 | ro role independently blocks writes; timeout enforced | PASS | `test_sql_guard_db.py` 6/6: permission denied on DELETE/UPDATE/INSERT/DDL; recursive CTE cancelled at 200 ms |
| 4 | Tool tests on fixtures: correct profiles; F1 rename ranks `cust_id` first | PASS | `test_tools.py`: known-frame stats, F1 ranking, no free type match for empty column |
| 5 | Tool outputs truncated and wrapped as data; prompt text stays data | PASS | 4 KB envelope tests + hostile-string test; all nine real-warehouse outputs started with `<data>` |
| 6 | Budget test with an endless fake tool loop terminates at `MAX_TOOL_CALLS` → `UNKNOWN`/escalation | DEFERRED (4b) | Tracker raises `BudgetExceeded` at the boundary (tested); no loop exists yet |
| 7 | LLM smoke on F1/F4/F6 produces schema-valid `RootCause` with category/confidence | DEFERRED (4b) | No API key present; `RootCause` is part of 4b |
| 8 | Lint/format/mypy/tests green; dev warehouse untouched | PASS | ruff + format + mypy clean; 288 unit + 58 integration passed; 30 batches, 0 incidents, 13,891 orders after the run |

## Verification commands (all run for this packet)

```powershell
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
.venv\Scripts\python -m mypy src
.venv\Scripts\python -m pytest -q -m "not integration"
.venv\Scripts\python -m pytest -q -m integration
$env:LLM_PROVIDER='fake'; .venv\Scripts\dqs llm smoke
```

Results: clean, clean, clean, 288 passed, 58 passed (330 s), all four probes
PASS.

## Three weakest points

1. **The guard has no adversarial fuzzing, only curated cases.** Thirty-five
   hand-written rejections cover every rule in AGENTS §7.2, but a determined
   attacker could still find a sqlglot construct the allowlist misses (e.g. a
   function name sqlglot resolves to a typed node rather than `Anonymous`).
   The DB backstop limits the damage to information disclosure, not writes —
   and `dqs_ro` currently has broad `SELECT`, so schema/table visibility is
   not restricted beyond the parser. Before 4b: consider a read-only role
   limited to the four schemas and a small generative/property test corpus.
2. **No real model has executed any of this.** The fake provider proves the
   plumbing, not that Anthropic/DeepSeek/Kimi emit the expected tool calls or
   survive the 4 KB data envelope. Structured output in particular varies by
   provider. The first 4b task should be `dqs llm smoke` with the owner's
   chosen key, before any prompt work.
3. **The tool layer trusts its `ToolContext`.** Nothing prevents a caller from
   passing the read/write engine; only the `READ ONLY` transaction protects
   writes. `get_pipeline_run`/`get_model_sql`/`get_lineage` also read the
   filesystem/manifest, so "read-only" means "no writes", not "no side
   effects" (`dbt compile` regeneration is a Phase-3 behavior). 4b must
   construct the context in one factory and test that it is the ro engine.

## Risks for Phase 4b

* **Loop budget semantics.** The tracker raises on the event *after* the
  limit; the investigator must catch `BudgetExceeded` and emit a structured
  escalation (`UNKNOWN`) rather than a traceback. Also decide whether the
  final answer call is allowed after the last tool call (recommend: reserve
  one model call).
* **Tool-call argument validation.** LangChain validates `args_schema` before
  calling the tool; a malformed provider response may bypass it. The
  investigator should treat a tool exception as data and continue, not crash.
* **Prompt-injection test must assert DB state.** The current test proves the
  envelope; 4b needs an agent-level test that a hostile row produces no write
  (monitor `dq.batches`/row counts before/after) and that the guard is the
  thing that rejected the SQL.
* **Context size.** Nine tools with verbose descriptions + 4 KB outputs can
  exceed a small model's context. The investigator should cap message history
  and prefer compact summaries; measure tokens per incident.
* **Manifest/DB availability.** `get_lineage` may trigger `dbt compile`
  (Phase 3 rule). An investigation that starts while dbt is running can block
  or see a torn manifest; the tool currently surfaces the error as data, which
  is acceptable but should be surfaced in the final report.
* **`RootCause` schema is not written yet.** The F1/F4/F6 smoke cannot be
  judged until the schema has category, confidence, evidence references and
  fixability, and the prompt tells the model how to use tool output.
* **No incident-memory or tracing yet** (Phase 9); do not add Chroma/Langfuse
  to make 4b easier without a DECISIONS entry.
