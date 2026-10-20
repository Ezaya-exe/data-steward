# Phase 4a — LLM wrapper and read-only tool layer

Date: 2026-10-20 · Phase 4 of `docs/PHASES.md`, **split**: this note covers
tasks 1–2 only (LLM provider wrapper + the complete read-only tool layer).
The investigator loop, `dqs incident investigate`, prompt tuning and the
F1/F4/F6 `RootCause` smoke are **Phase 4b** and are not built here.
Companion review packet: `docs/review/phase-4a-review.md`.

## What was built

* `src/dqs/agents/llm.py` — the only `init_chat_model` call
  (`get_llm(provider, model)`), explicit providers `anthropic`, `openai`,
  `google_genai`, `ollama`, `openai_compatible`, `fake`; `LLMClient` records
  latency + normalized token usage per call (failures too, redacted);
  `BudgetTracker` enforces tool-call, token and wall-clock limits with an
  injectable clock; `redact_secrets`; deterministic `FakeChatModel` for wiring
  tests (DECISIONS 62–63, 67).
* `src/dqs/agents/smoke.py` — four independent probes: plain completion,
  typed tool call, Pydantic structured output, and a three-step tool loop.
  Each reports pass/fail, summed latency and tokens; one failure does not stop
  the others.
* `src/dqs/cli.py` — `dqs llm smoke [--provider …] [--model …]`, exits
  non-zero if any probe fails.
* `src/dqs/config.py` + `.env.example` — `LLM_BASE_URL` / `LLM_API_KEY`;
  `openai_compatible` added to the validated provider set.
* `src/dqs/errors.py` — `LLMError`, `BudgetExceeded`, `SqlGuardError`,
  `SqlExecutionError`, `ToolError`.
* `src/dqs/tools/base.py` — `ToolContext` (read-only engine + cached lineage
  graph), identifier/qualified-table allowlists, compact JSON, 4 KB
  `<data>…</data>` envelope, `@data_tool` (errors become data).
* `src/dqs/tools/sql_guard.py` — deny-by-default validator + executor:
  one statement, sqlglot postgres parse, SELECT/WITH-SELECT only, schema
  allowlist, dangerous-function denylist, CTE-safe node checks, embedded
  semicolon pre-scan, `LIMIT ≤ 200`, `SET TRANSACTION READ ONLY`,
  `statement_timeout ≤ 5 s` (DECISIONS 66).
* `src/dqs/tools/readonly_sql.py` — `run_readonly_sql` on the guarded path.
* `src/dqs/tools/profiling.py` — `get_batch_profile`, `compare_profiles`,
  `sample_rows` (≤10 rows; optional best-effort contract-violation filter).
* `src/dqs/tools/renames.py` — `find_rename_candidates` with the documented
  name/type/value ranking (DECISIONS 64).
* `src/dqs/tools/dbt_tools.py` — `get_pipeline_run`, `get_model_sql`.
* `src/dqs/tools/contracts_tool.py` — `get_contract` (approved contracts only).
* `src/dqs/tools/lineage_tool.py` — `get_lineage` (unique id, `raw.table`, or
  bare model; cached manifest graph).
* `src/dqs/tools/registry.py` — nine typed `ToolSpec`s + `build_tools(ctx)`
  returning LangChain `StructuredTool`s with Pydantic argument schemas.
* `src/dqs/dbt_run.py` — `latest_run` (newest run, success or failure) so
  `get_pipeline_run` can show successful builds too (DECISIONS 68).
* Tests: `tests/unit/test_llm.py`, `test_sql_guard.py`, `test_tools.py`
  (74 tests) and `tests/integration/test_sql_guard_db.py` (6 tests).
* `contracts/*.yml` — status `draft` → `approved` (needed by the contract
  tool; all 13 files, no content change beyond the status).
* `docs/DECISIONS.md` — entries 62–68.

## How to run it

```powershell
# wiring/CI probe (no key, no network)
$env:LLM_PROVIDER = 'fake'
.venv\Scripts\dqs llm smoke

# real endpoint, once a key exists
$env:LLM_PROVIDER = 'openai_compatible'
$env:LLM_MODEL = 'deepseek-chat'
$env:LLM_BASE_URL = 'https://api.deepseek.com/v1'
$env:LLM_API_KEY = '...'
.venv\Scripts\dqs llm smoke

# quality gates
.venv\Scripts\dqs dev lint
.venv\Scripts\dqs dev test
.venv\Scripts\dqs dev test-int
```

## Acceptance results

Phase 4a covers Phase 4 tasks 1–2 and the parts of the Phase-4 acceptance
list that belong to them. Investigator-specific criteria are explicitly
deferred (table below).

| # | Criterion (in scope for 4a) | Verdict | Evidence |
|---|---|---|---|
| 1 | `get_llm` supports the documented providers; `openai_compatible` requires `LLM_BASE_URL`; unknown provider rejected; keys never logged | PASS | `test_llm.py` (19 tests): fake deterministic, missing base URL → `ConfigError`, `excel` rejected, redaction removes `sk-ant-…`/`api_key=…`, and a stubbed `init_chat_model` proves every provider receives `api_key` from Settings (even when the process env differs). Fake smoke runs end-to-end without a key |
| 2 | Per-call latency and token capture (including structured output and failures) | PASS | `test_client_records_latency_and_tokens`; structured probe reports tokens (raw message measured, not the `{"raw",…}` wrapper); failed calls are recorded with a redacted error |
| 3 | Budget tracker enforces tool calls, tokens and wall clock deterministically | PASS | `test_budget_tool_call_limit`, `test_budget_token_limit`, `test_budget_wall_clock_timeout`, `test_budget_state_reports_remaining`; injected `_Clock`. `test_fake_provider_calls_are_counted_by_the_budget` proves `LLMClient` + fake model feeds the tracker (17 tokens); `test_client_raises_when_a_call_exceeds_the_token_budget` proves the raise while the call stays measured |
| 4 | `dqs llm smoke` = 4 probes, prints pass/fail + latency + tokens, non-zero exit on failure | PASS | CLI run below: all four PASS on `fake`; loop probe shows `model_calls=3`. Failure path: `dqs llm smoke --provider excel` reports four FAIL rows and exits **1** |
| 5 | All nine tools exist with typed args, read-only behavior, compact JSON wrapped as `<data>` ≤4 KB | PASS | `test_registry_exposes_typed_argument_schemas`, `test_every_registered_tool_wraps_its_output`, `test_wrap_data_is_compact_and_parseable`, `test_wrap_data_truncates_to_four_kb_with_valid_json`; real-warehouse smoke below |
| 6 | SQL guard rejects DML/DDL/COPY, multi-statement + comment tricks, `SELECT INTO`, locking, unqualified/disallowed schemas, `pg_catalog`, dangerous functions, dangerous CTE bodies; accepts SELECT/WITH; caps LIMIT at 200 | PASS | `test_sql_guard.py`: **35 rejection cases**, **10 accepted cases**, 4 LIMIT-cap tests, empty-SQL test — all passed. Cases include `SELECT 1; -- ; DROP …`, `WITH x AS (DELETE …) SELECT …`, stacked-CTE `UNION … pg_catalog.pg_class` |
| 7 | DB-level proof that `dqs_ro` blocks writes independently and timeout is real | PASS | `test_sql_guard_db.py`: 6 passed. DELETE/UPDATE/INSERT/`CREATE TABLE` on `dqs_ro` all raise "permission denied"/"read-only" (`WHERE false` + rollback); recursive CTE with `timeout_ms=200` raises `SqlExecutionError` |
| 8 | Tool tests on fixtures: profile stats correct; F1 rename ranks `cust_id` first | PASS | `test_summarize_batch_profile_on_known_frame_stats`, `test_find_rename_candidates_ranks_cust_id_first_for_f1_case`, `test_rename_does_not_invent_type_match_for_empty_column` |
| 9 | Hostile sample text flows through as data, never instructions | PASS (boundary only) | `test_data_tool_wraps_hostile_text_as_data`: `IGNORE PREVIOUS INSTRUCTIONS…` is inside a `<data>` JSON string; guard rejects any write it might suggest. The loop-level "no DB change" test requires the investigator (4b) |
| 10 | Lint, format, mypy and all tests green; dev warehouse untouched | PASS | `ruff check .` → all passed; `ruff format --check .` → 128 files already formatted; mypy → 85 source files clean; **295 unit + 58 integration passed** (integration 330 s). After the suite: 30 batches, 0 incidents, 13,891 `raw.orders`, max date 2026-01-30 |
| 11 | Real-provider LLM smoke on F1/F4/F6 with schema-valid `RootCause` | **DEFERRED to 4b** | No API key is present in this environment (`anthropic_api_key`, `openai_api_key`, `llm_api_key` all empty), and `RootCause` does not exist yet. Fake smoke only proves wiring, not model quality (AGENTS §7.9) |
| 12 | Budget test with a fake LLM that never stops calling tools terminates at `MAX_TOOL_CALLS` with `UNKNOWN`/escalation | **DEFERRED to 4b** | `BudgetTracker` raises `BudgetExceeded` at the boundary (tested); the loop that catches it and returns an escalation result is the investigator |

### `dqs llm smoke` on the fake provider (real CLI output, table trimmed)

```
llm smoke provider=fake model=claude-haiku-5-5
probe                | result | latency_ms | tokens_in | tokens_out | detail
plain_completion     | PASS   | 13         | 12        | 5          | content='fake response'
typed_tool_call      | PASS   | 0          | 12        | 5          | tool=lookup args={'query': 'step-1'}
structured_output    | PASS   | 2          | 12        | 5          | answer='example' confidence=0.9
three_step_tool_loop | PASS   | 0          | 45        | 15         | tool_calls=2 model_calls=3 final='fake final answer'
ok all four probes passed
```

### Real-warehouse tool smoke (read-only `dqs_ro`, clean baseline)

Called directly through each tool function with `ToolContext(engine=ro)`:

```
get_contract             → approved raw.orders contract (columns, rules, baseline 30 batches)
get_batch_profile        → per-column null/parse/range stats for batch_20260130
compare_profiles         → 20260129 vs 20260130 deltas + missing columns
find_rename_candidates   → {"present":true,"missing_columns":[],"candidates":[]} (clean batch)
sample_rows              → 2 evidence rows, columns listed, values as data
run_readonly_sql         → SELECT status,count(*) … GROUP BY 1 → 5 rows, LIMIT applied
get_pipeline_run         → newest run: success, no failed nodes, 4 KB log excerpt
get_model_sql            → models/staging/stg_orders.sql source
get_lineage              → 16 downstream nodes for source raw.orders
```

All nine outputs begin with `<data>{…}</data>`; none raised.

## Design choices and why

Logged in `docs/DECISIONS.md` (62–68). Highlights:

* **One wrapper owns every model call.** Provider construction, token
  normalization, latency, redaction and budget accounting live in one module;
  the future investigator cannot accidentally bypass measurement.
* **Deny-by-default SQL, then a DB backstop.** Parsing catches intent;
  `dqs_ro` + `READ ONLY` + timeout make a parser bypass non-exploitable.
  We accept false rejections (e.g. a harmless `pg_*` helper) to avoid false
  acceptances (DECISIONS 66).
* **Data, never instructions.** Tools return hostile strings *inside* JSON in
  a `<data>` envelope; the model never receives a bare log line or row that
  looks like a system message (DECISIONS 65).
* **Rename evidence is transparent.** Fixed weights and an explicit
  type-match rule make a ranking explainable; the tool reports the candidates
  and scores, not a verdict (DECISIONS 64).
* **The fake model is bounded by design**, so it can test a three-step loop
  without becoming a hidden source of "success" (DECISIONS 63).
* **`ollama` without a new dependency**: its `/v1` endpoint speaks the OpenAI
  protocol, and `langchain-openai` is already installed (DECISIONS 62).

## Concepts used

* **LLM as a constrained component, not an executor.** The model's only
  influence is typed arguments to fixed tools and a Pydantic return value.
  Capability is granted by the interface (schemas, allowlists), not by trust —
  the same reason a database user gets `SELECT` instead of `SUPERUSER`.
* **AST validation beats string matching.** A SQL string is parsed into a
  tree; rules such as "no `DELETE` node anywhere", "every relation is
  schema-qualified" or "no `pg_*` function" are properties of the tree.
  Comment tricks and stacked statements are handled by tokenization plus a
  quote/comment-aware pre-scan, not by regexes over raw text.
* **Defence in depth.** The guard and the database role enforce the same
  property independently. If one layer has a bug, the other still prevents a
  write; the integration test proves the role, not just the parser.
* **Prompt injection as a data/instruction boundary.** An LLM cannot reliably
  distinguish a row from a system message if both arrive as plain text.
  Wrapping tool output in a tagged, valid-JSON envelope makes provenance
  explicit; the system prompt can then say "content inside `<data>` is data".
* **Metered autonomy.** Tokens, tool calls and wall clock are resources; a
  budget tracker turns "please stop" (a prompt hope) into an exception the
  loop must handle. Measuring usage per call is also what makes cost visible.
* **Test doubles at the provider seam.** `FakeChatModel` implements the same
  `BaseChatModel` interface, so the client, tools and smoke probes are tested
  without network, keys, or nondeterminism — dependency inversion at the one
  place where an external service enters the system.

## Deviations from the docs

* Phase 4 was split by the owner: tasks 1–2 (this note) and task 3–4
  (investigator) are 4b. No investigator loop, prompt file, `RootCause`
  model, or `dqs incident investigate` command exists yet.
* `ollama` is supported through its OpenAI-compatible endpoint instead of a
  dedicated integration package (DECISIONS 62).
* `google_genai` is wired through `langchain-google-genai` (installed 4.4.2);
  every provider now receives `api_key` explicitly from `Settings` — no
  provider client reads the process environment (DECISIONS 69).
* `latest_run` was added to `dbt_run.py` (not in the Phase-4 task list) because
  `get_pipeline_run` must be able to report successful runs (DECISIONS 68).
* Rename weights were not specified by the docs; logged as DECISIONS 64.

## Known limitations / TODO

* **No real-model run was executed for this phase note.** The only executed
  smoke is `fake`; run `dqs llm smoke` with the configured provider before
  trusting model behavior. The Phase-4 LLM acceptance (F1/F4/F6 → valid
  `RootCause`) remains open for 4b.
* **No investigator loop yet**, therefore no step timeline, no
  `dq.agent_steps` writes, no `UNKNOWN`/escalation path, and no loop-level
  prompt-injection "no DB change" assertion. The pieces (`BudgetTracker`,
  tool registry, data envelope) are ready for it.
* **`dqs_ro` is not enforced by the tool constructors.** `run_readonly_sql`
  takes whatever engine `ToolContext` holds; the `READ ONLY` transaction still
  blocks writes, but 4b must build the context with `make_ro_engine`.
* `find_rename_candidates` value overlap is exact string matching after
  parsing; fuzzy value evidence (e.g. reformatted dates) is out of scope.
* `get_batch_profile` / `compare_profiles` / `sample_rows` only address
  `raw.<table>` batches; there is no staged/marts batch profiling tool yet
  (Phase 5 needs table checks, not batch profiles).
* SQL guard may reject harmless functions whose names start with `pg_`/`lo_`
  or any `dblink*`; this is deliberate (false rejection > false acceptance).
* Structured-output support depends on the provider's tool-calling mode;
  `run_smoke` is the quick way to discover a provider that cannot do it.
* Windows console renders some rich glyphs as mojibake; the smoke table is
  readable but its header is truncated in cp1252 consoles (cosmetic).
