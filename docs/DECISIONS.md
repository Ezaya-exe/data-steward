# DECISIONS

Log of every decision the docs leave open, with date and reason. One short
entry each; newest last.

---

## Phase 0 (2026-10-18)

1. **Repo root is this directory** (`D:\study\python\data steward`), not a new
   nested `dq-steward/` folder. Reason: `AGENTS.md` and `docs/` already live
   here; §17's `dq-steward/` prefix reads as the project name, not a required
   subdirectory.
2. **Python 3.12.10 for `.venv`** (`py -3.12 -m venv .venv`). Reason: AGENTS §4
   says "use the newest of 3.11/3.12"; the host default `python` is 3.13.5, and
   dbt/langgraph ecosystems were only documented for 3.11/3.12.
3. **`.gitattributes` pins LF** for text files. Reason: host git has
   `core.autocrlf=true`; future byte-identical-output requirements (contract
   YAML determinism, Phase 2) should not fight line-ending rewriting.
4. **`requirements.lock` = `pip freeze --exclude-editable`** of the full dev
   install (140 pins, transitive included). Reason: `uv` is not installed;
   AGENTS §4 accepts plain pip; exact versions after a working install.
5. **`sql/001_init.sql` creates schemas + `dq` tables only** — no `raw.*` DDL.
   Reason: ARCHITECTURE §4.2 says the loader creates raw tables on first sight
   (schema-evolving), so Phase 0 has nothing valid to say about their columns.
6. **`sql/000_roles.sql` also creates the four schemas** (deviation from the
   PHASES.md task wording, which puts schemas only in `001_init.sql`). Reason:
   the docker entrypoint runs files alphabetically — 000 before 001 — and the
   `GRANT`/`ALTER DEFAULT PRIVILEGES` statements reference the schemas, which
   would abort a first-boot init with "schema does not exist".
7. **`ALTER DEFAULT PRIVILEGES` grants `SELECT` to `dqs_ro` on future tables**
   in all four schemas. Reason: dbt-created `staging`/`marts` tables (Phase 1+)
   must stay readable by the read-only agent role without re-running grants.
8. **`dqs dev up|down|lint|test|test-int|check` added** (owner-directed).
   Reason: `make` is not installed on this Windows host; the project must not
   depend on it (AGENTS §5). These commands are make-equivalent wrappers and
   the Makefile keeps pointing at them. Noted as an addition to the ARCHITECTURE
   §15 CLI surface, not scope creep.
9. **Phase 0 acceptance evidence for `make install` / `make up` comes from the
   underlying commands** (`py -3.12 -m venv`, `pip install -e ".[dev]"`,
   `dqs dev up` = `docker compose up -d --wait postgres`). Reason as (8);
   Makefile can be exercised once make is installed — it only wraps these.
10. **Model names in `.env.example` (`claude-haiku-5-5`, `claude-sonnet-5-5`)
    kept exactly as documented but not validated** against installed provider
    catalogs. Reason: Phase 0 only stores strings (`Settings` validates the
    provider enum); real model IDs are verified in Phase 4 when the LLM client
    is built.
11. **`db init` runs each SQL file as one multi-statement round trip**
    (`Connection.exec_driver_sql`). Reason: psycopg3 allows multi-statement
    executes; splitting on `;` would break `DO $$ ... $$` blocks.
12. **mypy uses the pydantic plugin** (`plugins = ["pydantic.mypy"]`). Reason:
    without it, mypy treats `Settings()` as missing constructor arguments even
    though every field has a default.
13. **pytest marker expressions passed as a single argument**
    (`-m "not integration"`). Reason: pytest's `-m` takes one argument; the
    split form makes pytest treat `integration` as a test path.
14. **Resolved versions (2026-10-18, Python 3.12.10):** langgraph 1.2.16,
    langchain 1.4.6, langchain-core 1.6.12, dbt-core 1.12.6, dbt-postgres
    1.11.0, streamlit 1.66.0, SQLAlchemy 2.1.5, psycopg 3.3.6, pandas 2.3.3,
    numpy 2.5.4, pydantic 2.14.1, sqlglot 29.0.1, typer 0.27.3, ruff 0.17.1,
    mypy 1.20.2, pytest 8.4.2. Full transitive set in `requirements.lock`.

## Phase 1 (2026-10-18)

15. **New modules beyond the §17 layout:** `src/dqs/dbt_run.py` (dbt subprocess
    wrapper + `dq.pipeline_runs` recording) and `src/dqs/sim/pipeline.py`
    (seed/day orchestration: generate → load → build). Reason: §17 has no home
    for dbt invocation or the seed loop; keeping them out of `cli.py` makes the
    loop unit-testable and CLI presentation-only.
16. **dbt generic tests use the `arguments:` nesting** required since dbt
    1.11 (installed: 1.12.6); top-level args are only a deprecation warning
    today but will break later.
17. **`sources.yml` / `exposures.yml` live under `models/`**, not the project
    root. Reason: dbt 1.12 did not parse root-level property files ("source
    named 'raw.customers' which was not found"); model-path properties always
    parse. Per AGENCY docstring rule: adapt to the installed version.
18. **Customer ids are contiguous across days** (`cumulative(day-1)+1 …
    cumulative(day)`); orders reference `1..cumulative(date)`. Reason: FK
    integrity on clean days is a structural guarantee, not probabilistic — the
    earlier slot-based allocation left unassigned id ranges, which the
    `relationships` dbt test exposed (1901 orphans fixed).
19. **High-volume ids (orders, items, payments) use per-date slots**
    (1000/5000/2000/day). Reason: keeps cross-date PK uniqueness structural
    while within-batch allocation stays simple; orphan risk does not exist for
    them because children are generated from the actual orders frame.
20. **Children derive from the generated `orders` frame** (row counts, order
    ids, totals). Reason: payment sums equal order totals exactly and child
    inconsistencies cannot occur on clean data.
21. **Products: header present in every batch, 0 rows after day 1.** Reason:
    keeps `missing table` (F9, whole header absent) unambiguous versus "no new
    rows"; the catalogue persists in `raw.products` for FK joins.
22. **Loader rejects a duplicate batch id** (`batch_YYYYMMDD` already in
    `dq.batches` → error). Reason: silently appending would double rows and
    break determinism promises; re-runs require the explicit `dqs db reset`.
23. **`dq.batch_tables.columns` records the observed batch header without
    `_batch_id`/`_loaded_at`.** Reason: those columns are loader-owned metadata
    (§4.1) rather than source-delivered columns; schema checks already ignore
    them, so making them absent from the header avoids double-ignoring.
24. **dbt executable resolution: the console script next to the running
    interpreter** (`.venv\Scripts\dbt.exe`). Reason: no PATH dependence; works
    from any shell without activation.
25. **`sim day` assumes the baseline is seeded** (order → customer
    relationships span all historical days). Documented in `--help`; no
    defensive workaround, since faults must stay attributable.
26. **`--seed` defaults to `DQS_SEED` from `Settings`** when the flag is
    omitted, keeping env-config and CLI flag consistent.

## Phase 2a (2026-10-18)

27. **All ten fault kinds share one injection registry** — uniform signature
    `(tables, spec, rng, date, seed) -> (frames, arrival_override | None,
    ground_truth)`; F8/F9 are registered injectors, not special cases in
    `apply_faults`. Reason: a registry-completeness test
    (`set(_INJECTORS) == set(FaultKind)`) makes it impossible for a kind the
    parser accepts to fail at apply time (the original `KeyError` bug).
28. **`FK_MAP` restricts orphan_keys at PARSE time** to the four structural FK
    pairs (`orders.customer_id → customers`,

    `order_items.order_id → orders`, `payments.order_id → orders`,
    `order_items.product_id → products`); injected ids are strictly above the
    parent's cumulative maximum (closed-form bound, e.g. next slot start), so
    the FK violation is provable, not probabilistic. Rejected pairs fail in
    the parser with the supported pairs listed.
29. **null_spike rejects only the target table's primary-key column.** FK
    columns stay valid targets: NULLing a nullable FK column is a realistic
    upstream-nulls fault, and NULL is not an orphan (fk.integrity stays quiet
    while nulls.rate fires).
30. **`kind:table:` with an empty parameter list is a parse error.** A colon
    announces parameters that never arrived — silent default behavior would
    hide copy-paste truncation.
31. **F10 `column_added` shape: one extra TEXT column of short coupon-like
    codes (`SAVE10`, `SAVE20`, …) with ~30 % NULLs; default name
    `orders.coupon_code`, `extra_note` elsewhere, overridable via
    `new_column=`.** Reason: mirrors real upstream drift (new optional field,
    partially populated) and stays warning-level; replaces the earlier draft
    `new_name/value` parameters.
32. **Fault-path integration tests use a throwaway database** (`dqs_test`,
    created and dropped by the test fixture). Reason: a renamed header
    ALTERs `raw.orders` — running that on the dev warehouse would pollute
    exactly the schema evolution state contracts must be generated from; the
    `dqs` role has CREATEDB, `WITH (FORCE)` drops need no extra setup.

## Phase 2a — task 2: contracts (2026-10-18)

33. **Contracts are generated from loaded DB rows, never from the simulator.**
    `contracts/profiler.py` reads `raw.*` (TEXT) through `contracts/parsing.py`
    and staging/marts straight from their typed columns; the simulator's
    "intent" is only ever ground truth for benchmark scoring. Reason: the check
    engine will see exactly the parsed DB reality, so generation must share
    that view (and the SAME parsing module, so type inference cannot drift).
34. **Loader-owned system columns (`_batch_id`, `_loaded_at`) never enter
    contracts** (consistency with DECISION 23): schema checks compare the
    observed business header against `dq.batch_tables.columns`, which also
    never carries them.
35. **Range/mean bounds exist on measure columns only** (name contains
    amount/price/quantity/qty/total/revenue/spend/collected) AND only when the
    column is not `*_id`, not a date/timestamp, and not a known FK target. Keys
    and temporal columns get uniqueness/nullable/allowed_values rules instead.
36. **Dimension volume excludes the day-1 initial load**; `products` (empty
    histogram after day 1) gets an explicit `min_rows: 0`,
    `max_rows: ceil(0.1 × initial)`; `required_in_batch` is true only when the
    table appeared in 100% of baseline batches (`dq.batch_tables` presence).
37. **Freshness is batch-level via `dq.batches.arrived_at`** — per-table
    freshness is guarded by `required_in_batch` + volume only. The
    `late_batch:<table>` table part of the F8 spec stays parseable but
    per-table value is vestigial; the SLA is a single contract-wide
    `max_delay_minutes: 360`.
38. **`generated_at` is derived from the baseline window** (last business date
    00:00 UTC), never wall-clock — this is what makes two regeneration runs
    byte-identical (acceptance criterion), verified by the byte-identity test.
39. **Staging/marts contracts are whole-table snapshots** (`batches: 1`,
    `required_in_batch: false`, no FK references): dbt rebuilds them whole, so
    there is no per-batch semantic to model. Raw contracts keep batch facts.
40. **Mean-bounds sigma floor (`_STD_EPS = 1e-9`)**: with =2 identical batch
    means, s collapses to 0 and MEAN_STD_MULT collapses the window onto the
    mean; below the floor the MEAN_FALLBACK_SHARE (±25% of mean) fallback
    applies (real bug found while writing the tests).
41. **Fit thresholds are exported from `contracts/fitting.py` in one copy**
    (`FK_ORPHAN_MAX 0.005`, `TYPE_PARSE_MIN 0.99`, `NEAR_BOUND_SHARE 0.10`) so
    generation-time acceptance and the Phase 2b check engine read the same
    numbers. `fitting.py` is a test/acceptance harness, NOT the check engine
    (no Violation model, no severity, no fingerprinting).
42. **`generate_staged_contracts` returns `[]` before the first dbt build.**
    A warehouse with raw batches but no staging/marts tables is legitimate
    (contract tests seed `dqs_test` with `run_dbt=False`); the old code raised
    a misleading "no rows in dq.batches" ContractError.
43. **F8 (late_batch) is invisible to contract stat bounds by design.** The
    fault changes only arrival time; the integration test documents that stat
    findings stay empty while `arrived_at - scheduled` exceeds
    `freshness.max_delay_minutes` — detection belongs to the Phase 2b
    freshness check, which reads `dq.batches.arrived_at`.

## Phase 2b — task 3/4: checks, incidents, monitor (2026-10-19)

44. **Contracts are approved in place, and the determinism test compares every
    byte except the `status:` line.** Generation always writes `draft`; the
    owner approves with `dqs contracts approve --all` (the human step, §7.2).
    So `test_regenerated_contracts_are_byte_identical_to_committed` normalizes
    `status:` on both sides (a guard asserts the committed catalog really is
    `approved` everywhere) and still fails on any other difference.
45. **Value-level checks never cascade off a missing column, and schema checks
    never cascade off an absent table.** `type.logical`, `nulls.rate`,
    `unique.pk`, `fk.integrity`, `range.bounds` and `volume.rows` iterate only
    `present_columns ∩ contract.columns`, so a column absent from the batch
    header (F1's renamed side, F2) is reported *solely* by
    `schema.missing_column`. A wholly absent table (F9) is owned by
    `volume.rows` when `required_in_batch: true`; `schema.missing_column`/
    `schema.unexpected_column` stay silent for it, so F9 is one violation, not
    one per contract column.
46. **`Violation` extends §7.3 with `batch_id` and an optional `table`**:
    `batch_id` is the producing batch (it is what makes an incident's batch list
    grow across days) and `table=None` marks the batch-level checks
    (`freshness.sla`, `monitor.missing_batch`). Every other field keeps the
    documented meaning.
47. **`IncidentStatus` is a `StrEnum`** (`open | investigating |
    awaiting_approval | resolved | rejected | failed`) in `models/enums.py`, so
    incident status is a typed value that matches `dq.incidents.status` exactly.
48. **Fingerprint = `sha1(table | check | column)`** with `""` for a missing
    part, taken from a deterministic *representative* violation (max severity,
    then sorted check/column/message). Violations group by `(batch_id, table)`
    into one incident (severity = max of the group). A fault recurring on
    consecutive days therefore maps to the same fingerprint and merges into the
    one open incident instead of creating a second; the merge de-duplicates by
    `(batch_id, check, column, message)`.
49. **`volume.initial_load_excluded` makes DECISION 36 enforceable.** The
    generator omits the day-1 initial load when it fits dimension volume bounds
    and now records that on the contract; `volume.rows` exempts exactly the
    batch whose business date equals `baseline.first`. Reason: enforcement must
    mirror generation or a clean baseline fails on its own first day — a real
    false positive the acceptance probe surfaced (customers day-1 = 2000 vs
    bounds [21, 68]; products day-1 = 200 vs [0, 20]).
50. **`range.bounds` ignores the batch-mean signal for a sample smaller than
    `_MIN_MEAN_SAMPLE = 2` non-null values.** With one value the "mean" *is*
    that value, so mean bounds cannot distinguish a systematic shift (F7) from a
    single odd row; the isolated-value `range` signal still applies. Real false
    positive found by the edge-case unit tests.
51. **The monitor checks raw batches only (batch scope).** `CheckEngine.run(batch_id)`
    is the §7.3 batch path; `CheckEngine.run_table(table)` enforces a staged/marts
    snapshot (the scope Phase 5 §11 fix-verification reuses). `dqs monitor run`
    does not run staged snapshots yet — documented limitation until Phase 5
    wires the staged path.
52. **The monitor is driven by an explicit `as_of` watermark.** `dqs monitor run`
    defaults `--as-of` to `max(business_date) + 1 day 00:00 UTC`, falling back to
    now(UTC) on an empty warehouse; `unchecked_batches` selects
    `status <> 'checked' AND business_date <= as_of.date`. This is why a
    contiguous clean warehouse yields zero incidents without a wall-clock
    argument. `monitor.missing_batch` walks every business date from the first
    present row to `as_of.date − 1` and flags the ones with no `dq.batches` row;
    it runs even when there are zero approved contracts (a missing date is a
    fact, not a table rule).
53. **Phase 2b acceptance uses a template database.** `test_monitor_e2e.py`
    seeds the 30-day baseline once into `dqs_monitor_template` and clones it per
    test with `CREATE DATABASE … TEMPLATE` (clones dropped afterwards; the dev
    warehouse is never touched). Fault days start the day after a shared clean
    window and every scenario loads days **contiguously**, because customer ids
    are date-slotted (DECISION 18): a skipped day makes clean FK values look
    orphaned — an artifact, not a defect.
54. **Repaired six non-UTF-8 (cp1252) bytes in `docs/DECISIONS.md`** (em dash,
    ×, ±). They predated this phase and made `ruff format --check .` fail
    repo-wide ("stream did not contain valid UTF-8"); the file is now UTF-8.

## Phase 3 — lineage and impact (2026-10-19)

55. **Failing dbt nodes are parsed from `target/run_results.json` immediately
    after every build and stored on the same `dq.pipeline_runs` row** (new
    idempotent migration `sql/002_pipeline_run_failures.sql`: `failed_nodes`
    jsonb + `run_results_parsed_at`). The impact report reads ONLY the stored
    record; the live file is never trusted (the next build overwrites it — a
    torn manifest/run_results after failed builds was actually observed).
    Pre-002 rows keep `failed_nodes = NULL` and honestly mean "no parsed
    record exists".
56. **Impact scope rules.** Incidents naming a column (F1/F2/F10) scope to
    column-level lineage (only models referencing that column, plus their
    downstream exposures). Table-scoped incidents without a column (volume,
    missing table) take the full downstream of that raw source. Batch-level
    incidents (freshness, missing batch — `table = NULL`) take the downstream
    of ALL raw sources. When the column heuristic finds nothing, the builder
    falls back to the table footprint and says so in the summary — an empty
    blast radius must never silently hide a break.
57. **Manifest staleness = missing, unparseable, older than the newest
    `models/**` file, or containing a model without `compiled_code`.**
    Verified against installed dbt 1.12.6: `compiled_code` IS present after
    `dbt compile`, and after a FAILED `dbt build` the manifest is torn
    (children of broken nodes are skipped, so they carry no compiled_code —
    observed live). Regeneration runs `dbt compile` and fails loudly with the
    dbt excerpt (a wrong DAG is worse than no DAG). Target/compiled/** exists
    as a read fallback only.
58. **`models_referencing_column` is a best-effort heuristic**, documented in
    `lineage/columns.py`: sqlglot (postgres dialect) over each downstream
    model's compiled SQL; a model counts only when it selects directly FROM
    the raw source (so chained models like fct_orders do not count — limiting,
    but precise) AND the column appears as a column reference. No join/alias
    scoping: a column with the same name in another joined table is an
    over-count risk; dbt-visible `SELECT *` stays unexpanded and is missed;
    comments/strings invisible. Precision over recall, stated everywhere it
    matters.
59. **Lineage unit tests use a checked-in fixture generated from the REAL
    manifest** (`scripts/generate_manifest_fixture.py` →
    `tests/fixtures/manifest_fixture.json`), trimmed to the keys the lineage
    code reads but keeping the complete real DAG (5 sources, 8 models, 26
    tests, 3 exposures with owners/maturities, consistent child/parent maps)
    — so graph/column tests need no dbt, and the fixture cannot drift
    silently (regenerating is one command).
60. **`blast_radius_score`** (deterministic, in DECISIONS for review):
    `score = 100 × Σ_exposure maturity_weight + 10 × Σ_affected_model
    1/distance`, maturity_weight high 1.0 / medium 0.6 / low 0.3 (unknown
    0.5), distance = min graph hops from the affected raw source(s). Rationale:
    exposures weigh by count (they sum) *and* business criticality
    (maturity); models weigh by distance like a decay — the blast is worse
    the closer it starts. The number ranks incidents; it is not an SLA.
61. **The summary is a template with a Phase-4 hook**: `render_summary()` in
    `lineage/impact.py` is the single LLM seam — Phase 4 replaces its body
    with ONE structured LLM call (no tools), falling back to the template on
    any failure. Phase 3 has no LLM anywhere; unit tests assert the template
    output. The CLI also persists the report into `dq.incidents.impact` and
    the incident reader now carries `root_cause`/`impact`/`resolution` jsonb.

## Phase 4a (2026-10-20)

62. **Provider set is `anthropic | openai | google_genai | ollama |
    openai_compatible | fake`.** `openai_compatible` uses `LLM_BASE_URL` +
    `LLM_API_KEY` (DeepSeek, Kimi, GLM, OpenRouter); `ollama` is routed
    through its OpenAI-compatible `/v1` endpoint with `langchain-openai`, so
    no new `langchain-ollama` dependency is needed. `google_genai` stays
    optional: `init_chat_model` raises a clear `ImportError`, which we wrap
    with install guidance; `langchain-google-genai` is not installed now.
63. **`FakeChatModel` is a wiring fixture, never a benchmark model.** It is
    deterministic and bounded: no bound tools → one text answer; a bound
    Pydantic schema → one schema-valid tool call; ordinary bound tools → two
    tool calls then a final text answer (exactly enough to exercise a
    three-step loop). It makes no network calls and synthesizes token counts
    so recorder/budget tests can assert real plumbing. AGENTS §7.9 still
    forbids quoting fake results as benchmark evidence.
64. **Rename ranking weights: name similarity 0.45, type match 0.35, value
    overlap 0.20.** Name/type dominate because a rename keeps the column's
    meaning; value overlap is evidence, not proof (a clean column can share
    values with the missing one). Type match requires a successfully inferred
    logical type, so an all-NULL new column never gets free type points. The
    F1 fixture (`customer_id` → `cust_id`) ranks `cust_id` first.
65. **Every tool result crosses the model boundary as data**: compact JSON
    (sorted keys) inside `<data>…</data>`, at most 4 KB. Truncation keeps
    *valid JSON* by returning `{"truncated":true,"preview":"…"}` instead of a
    cut string. Tool errors (`DqsError`, validation `ValueError`, DB errors)
    are returned as data so the investigator can correct itself; programming
    errors still raise. This is the §7.6 prompt-injection boundary.
66. **SQL guard is a positive allowlist, with an independent DB backstop.**
    Reject before parsing when a real (non-quoted, non-comment) semicolon
    appears before the end; then exactly one sqlglot postgres statement,
    top-level `SELECT`/`WITH … SELECT`, no DML/DDL/COPY/MERGE/SET/transaction
    nodes anywhere (including CTEs), no `SELECT INTO`, no locking clauses, no
    catalog-qualified or unqualified relations outside `raw/staging/marts/dq`,
    and no `pg_*`, `lo_*`, `dblink*`, `set_config`, `current_setting` or XML
    admin functions. Execution uses `SET TRANSACTION READ ONLY`,
    `statement_timeout ≤ 5000 ms`, and a hard `LIMIT` cap of 200. False
    rejections are accepted; silent acceptance is not.
67. **Budget boundaries are strict (`>`), not `≥`**: exactly `MAX_TOOL_CALLS`
    calls and exactly the token budget are allowed; the next event raises
    `BudgetExceeded`. The clock is injectable for deterministic timeout tests,
    and a future loop must call `check()` before each step (Phase 4b wires the
    escalation/`UNKNOWN` result).
68. **`dbt_run.latest_run` reads the newest run whether or not it failed.**
    The existing `latest_run_for_batch` intentionally requires parsed failing
    nodes (Phase 3 impact); `get_pipeline_run` also needs successful runs and
    their log excerpt. Ordering is by `run_id DESC` (the table's bigserial),
    not by a file on disk.
69. **API keys come from `Settings`, never from the process environment.**
    `get_llm` passes `api_key=` explicitly for anthropic, openai,
    google_genai, openai_compatible and ollama; no provider client is allowed
    to fall back to `os.environ`. `GOOGLE_API_KEY` now maps to
    `Settings.google_api_key` (`.env.example` updated). Reason: a stray shell
    variable silently selected a different account and produced the opaque
    "API key required for Gemini Developer API" error even though `.env` had
    a key. A unit test stubs `init_chat_model` and asserts the constructor
    receives the Settings value for every provider, including when
    `GOOGLE_API_KEY` is set differently in the environment. Keys are redacted
    by value in logs/errors (`settings_secrets`) and by Google key shape
    (`AIza…`) when a provider quotes an unknown key. `langchain-google-genai`
    is installed (4.4.2), so the earlier optional-package caveat in entry 62
    is superseded.

## Phase 4b — investigator and hardening (2026-10-20)

70. **`ToolContext` verifies the live database role (`SELECT current_user =
    dqs_ro`) in `__post_init__`, and `from_settings` is the only factory that
    builds an engine (`make_ro_engine`).** Reason: Phase-4a review weak point 3
    — a caller could pass the read/write engine; the READ ONLY transaction was
    the only protection. The check is on the actual session, not the URL, so a
    mislabelled engine cannot pass. File-only unit tests use a tiny stub engine
    whose `current_user` is `dqs_ro`; the integration test also proves the real
    rw engine is rejected.
71. **SQL functions are a positive allowlist keyed by sqlglot's canonical
    name** (`Anonymous.name` or `Func.sql_name()`, lower-cased), replacing the
    `pg_*`/`lo_*`/`dblink*` denylist. Reason: a denylist can only reject names
    someone thought of; an allowlist rejects `query_to_xml`, `table_to_xml`,
    `ts_stat`, `pg_advisory_lock`, `current_setting`, `pg_read_binary_file` and
    any future admin helper by construction. Qualified/quoted forms
    (`pg_catalog.pg_sleep`, `"pg_sleep"`) normalize to the same name. False
    rejections of harmless unlisted helpers (`version()`) are accepted
    (DECISIONS 66's stance).
72. **LLM retries are limited to HTTP 429 / 500 / 502 / 503 / 504**, with
    exponential backoff (`LLM_RETRY_BASE_SECONDS`, default 1 s), `Retry-After`
    honoured when present (seconds or HTTP-date), `LLM_MAX_RETRIES` (default 3)
    and a hard cumulative `LLM_RETRY_MAX_WAIT_SECONDS` (default 30 s). If a
    requested wait would exceed the cumulative cap, the call fails instead of
    stalling. Other errors are never retried. Each retry emits a `RetryRecord`
    through an optional hook so the investigator can write it to
    `dq.agent_steps`; a failing hook is logged, never allowed to mask the
    provider error (`RetryRecord.retries` also lands on `LLMCallRecord`).
73. **`contracts generate` preserves `status: approved` when the regenerated
    content is identical (status normalized away) and refuses changed content
    unless `--force`.** Reason: approval is a human decision, and regeneration
    must not silently demote a reviewed rule set to a draft. `--force` writes
    the newly generated **draft**, so a changed rule set has to be re-approved.
    The store validates every conflict before writing, so a refused run leaves
    the catalogue untouched.
74. **The investigator loop is hand-written, and budgets are checked before
    every model turn.** Exactly `MAX_TOOL_CALLS` tool calls are allowed, then
    the loop stops without another model call and returns
    `UNKNOWN`/`ISSUE`/confidence 0 with the partial findings in `summary` and
    evidence only from calls that actually ran. Token/time overruns follow the
    same escalation. Reason: a framework's hidden iteration cannot guarantee
    "never loop forever" or that the expensive final call is skipped once the
    budget is exhausted.
75. **Final `RootCause` channels, in order: native structured output, then a
    `submit_root_cause` tool, then the UNKNOWN escalation.** The provider smoke
    (`dqs llm smoke`) passed native structured output, so it is primary; the
    tool fallback covers OpenAI-compatible endpoints whose tool-call validation
    rejects a native schema call. Both channels were observed on the real
    F1/F4/F6 smoke, and every answer re-validates as `RootCause`. The chosen
    channel is written to `dq.agent_steps` (`final_channel`).
76. **A provider error after at least one tool call is recovered through the
    final-answer channel instead of aborting the investigation.** Reason: the
    configured OpenAI-compatible model sometimes tries to call a tool named
    `RootCause`/`json` when it decides it is finished; the gateway rejects that
    request with HTTP 400 before LangChain returns a message. Aborting would
    throw away valid evidence. The recovery keeps the evidence and asks for the
    structured answer again; if that also fails, the result is `UNKNOWN` +
    `ISSUE` with partial findings. Before any evidence exists, the original
    `InvestigatorError` still surfaces (there is nothing honest to report).
77. **Evidence provenance is enforced deterministically.** After the answer is
    parsed, any `EvidenceItem` whose `tool` was not called in this run is
    dropped and an `evidence_dropped` system step records the fabricated item
    and the called-tool set. An empty evidence list is preferred to a plausible
    invention. A model that cites a failed tool call is not dropped (the call
    happened); the finding is still the model's interpretation of the error.
78. **Every LLM/tool step is written through a `StepSink` that owns `seq`.**
    The DB sink starts after `MAX(seq)` for the same incident+node, so a rerun
    appends a new timeline instead of colliding. Payloads are recursively
    redacted (`settings_secrets` + token shapes) and capped at 4 KB with a
    valid `{"truncated":true,"preview":…}` object. The LLM hook records
    provider/model, a compact prompt/response summary, tokens and latency for
    every call including the final structured one; retries write a separate
    `llm_retry` system step.
79. **The manual node command is `dqs incident investigate <id>` (singular
    group).** `docs/PHASES.md` names this exact command; inspection stays under
    the existing `dqs incidents list|show`. The command persists only
    `root_cause` and the step log — it does not change the incident status,
    because lifecycle transitions belong to the Phase 6 workflow. It accepts
    `--provider/--model` so the fake provider can exercise the CLI without a
    real key.

## Phase 5a — sandbox verification (2026-10-20)

80. **`FixPlan` carries the incident facts verification needs, not only the
    §10.5 prose fields.** Added `incident_id`, `batch_id`, `category`,
    `affected_table` and `affected_columns` alongside `summary/edits/rationale/
    expected_effect/risk`. Reason: relevance, the targeted check and the row
    denominator are deterministic SQL decisions; guessing them from a patch
    would make verification ambiguous and easy to game.
81. **Plan limits live on the Pydantic model, not only in the prompt:** at most
    3 edits and at most 4096 UTF-8 bytes of replacement text per plan, with
    empty paths, NUL bytes, padded paths, no-op edits and non-`raw.*` affected
    tables rejected. A plan that breaks a limit cannot be constructed, so the
    verifier never has a half-valid plan.
82. **The edit allowlist is fail-closed and Windows-aware.** Paths must be
    relative, use `/` or `\`, resolve after `resolve()` inside
    `warehouse/dbt_project/models/`, end in `.sql`, and contain no `..`,
    absolute/drive/UNC prefix, NUL, alternate-data-stream colon, empty segment,
    reserved device name, trailing dot/space or symlink/junction component.
    Case and separators are normalized; an optional `warehouse/dbt_project/`
    prefix is stripped. The exact-match `old` string must occur once.
83. **Relevance uses a read-only manifest.** An edited model must be a
    `models/staging/*.sql` dbt model whose direct `depends_on` includes the
    source node for the incident's `raw.<table>`. `load_manifest(...,
    regenerate_when_stale=False)` is deliberate: verification must never run
    `dbt compile` in the real project and overwrite `target/manifest.json` (the
    Phase-3 torn-manifest lesson).
84. **Edits are applied to bytes, sequentially.** The temp copy's file is read
    as UTF-8 bytes, `old` must occur exactly once at that point, and untouched
    CRLF/LF text stays byte-identical. This makes reviewable diffs and avoids
    Python newline translation changing an unrelated line.
85. **The sandbox is a temp copy with isolated dbt paths and a minimal child
    environment.** `target/`, `dbt_packages/`, `logs/` and `__pycache__` are
    excluded; `--target-path`/`--log-path` point inside the temp project;
    dbt runs with `shell=False`, a timeout and only PATH/OS basics plus PG*
    values built from `Settings`. LLM/GitHub API keys are never copied to the
    child. Sandbox schemas are dropped in `finish()` and again in an outer
    `finally` for unexpected exceptions.
86. **The dbt selector is `staging.*+`, a documented superset of the §11
    `<changed>+`.** Downstream marts reference sibling staging models, and dbt
    relationship tests live on those siblings; selecting only the changed
    branch leaves relations missing and fails for an unrelated reason. The
    sandbox therefore rebuilds every staging model, their tests and all
    downstream marts in the throwaway schemas.
87. **Sandbox schema naming follows the project macro.** The base is
    `dqs_sandbox_<8 hex>` (incident id if present, otherwise plan hash); the
    macro creates `<base>_staging` and `<base>_marts`. Cleanup validates every
    name against `^dqs_sandbox_[a-z0-9_]{1,40}$` before `DROP ... CASCADE`, and
    `dqs verify cleanup` drops every stale `dqs_sandbox_*` schema. The staged
    profiler accepts this steward-owned schema pattern in addition to the dev
    `staging`/`marts`.
88. **Pass criteria are all-of, deterministic and deliberately narrow:** dbt
    build + its tests exit 0; conformance to the published staging contract
    using only schema/type/null/unique/allowed-values (explicit Postgres type
    mapping included; volume/freshness/range excluded because contracts are
    one-snapshot); row preservation ≥ 95% (distinct-PK denominator for
    DUPLICATE_RECORDS); and the category-specific check (no batch nulls for F1,
    numeric cast + raw-contract range for F3, primary-key uniqueness for F6).
    An unsupported category cannot pass.
89. **`dqs verify <plan.json>` and `dqs verify cleanup` are one Typer command
    with the literal `cleanup` argument.** Typer consumes a group's positional
    argument before resolving a subcommand, so a group + `cleanup` command
    would make `dqs verify cleanup` parse `cleanup` as a plan path. The
    single-command form keeps both required surfaces exact. A failed verdict
    exits 1; a passing attempt is appended to `dq.fix_attempts` with the next
    `attempt_no` for its incident.

## Phase 5b - fixer, issue builder, plan-fix (2026-10-20)

90. **The fixer's structured output is `FixPlanDraft`, not `FixPlan`.** The
    model returns only the five §10.5 fields (`summary`, `edits`, `rationale`,
    `expected_effect`, `risk`). Code fills `incident_id`, `batch_id`,
    `category`, `affected_table` and `affected_columns` from the stored
    incident and root cause. Making those fields absent from the schema is a
    structural guarantee that the model cannot relabel the incident or point
    verification at a different batch.
91. **Candidate SQL is selected by lineage, never by the model.** The fixer
    sees only `models/staging/*.sql` files whose manifest `depends_on` includes
    the source node for the root cause's `raw.<table>`. This is the same
    relevance rule the verifier enforces, applied before the prompt, so the
    model cannot be tempted into editing an unrelated file.
92. **Every planner round trip is persisted through one payload path.** A
    schema-invalid draft and a draft that fails plan validation are written to
    `dq.fix_attempts` with a synthetic failed `VerificationResult`; a plan that
    reaches dbt is written with the real verdict. One sequential attempt number
    is allocated per incident (`next_attempt_number`) so invalid and valid
    attempts cannot diverge in the timeline.
93. **The fixer has no tools and no submit-tool fallback.** Native structured
    output is the only channel. A provider that cannot emit the schema yields a
    failed attempt (`fixer_output`), not a tool loop or a hand-written parser.
    This follows the §10.5 "pre-supplied context" design and AGENTS §7.1.
94. **`SOURCE_COLUMN_ADDED` routes to NONE before any confidence check.**
    Warning-level source drift needs no PR or issue action. Every other
    non-PR-eligible category routes to ISSUE, and a PR additionally requires
    confidence >= 0.6 and at least one candidate staging model
    (`dqs.resolve.plan_resolution`).
95. **`dqs incident plan-fix` reads the stored root cause and impact, prints the
    deterministic routing, and refuses to plan when routing is not PR.** It is
    a manual Phase-5b entry point; lifecycle/status changes remain Phase 6. A
    failed or budget-exhausted run exits 1 after printing every attempt and the
    final criterion table.
96. **The issue builder is a template, not an agent.** `build_issue` takes the
    incident, root cause, impact, the already-decided resolution and any failed
    attempts. It renders one stable Markdown document with a static
    per-category playbook (`NEXT_STEPS`), rejects a mismatched impact or a
    PR/NONE-routed incident (`IssueBuildError`), and collapses multiline
    model/tool text so hostile content cannot create headings or fake sections.
97. **The clean-data oracle is test-only.** `tests/helpers/oracle.py` rebuilds
    the patched staging model with the production patch/build helpers, reads
    the fault batch from the sandbox, and compares it with
    `generator.tables_for_date(fault_date, seed)`. It is never imported by
    `src/dqs` (asserted by a test). A verifier pass with an oracle mismatch is
    a test failure and is recorded in the LLM result JSON, not hidden.
98. **The real-LLM fixer test runs the whole chain and records, not tunes.**
    Each F1/F3/F6 scenario seeds a throwaway template, clones it, runs the real
    investigator, stores the root cause, builds impact, then runs the fixer and
    verifier. Results (attempts, failed criteria, tokens, retries, oracle
    verdict) are written to `out/phase-5b-llm-*.json`. A provider quota error
    is recorded as a failed attempt; reruns after quota recovery use the same
    scenario and do not change prompts or expected outcomes.
99. **The pytest config puts the repo root on `sys.path`
    (`pythonpath = ["."]`).** Without it, the bare `pytest` entry point did not
    make the repository root importable, so `tests.helpers.oracle` failed in
    `test_fixer_llm.py` and `test_oracle.py`. `python -m pytest` happened to add
    the working directory, which is why the two invocations differed. The
    config makes both behave the same; test imports are unchanged.

## Phase 6a — publish package (2026-10-21)

100. **Publishing is gated by a typed `ApprovalDecision` checked at runtime.**
     `Publisher.create_pr`/`create_issue` require `approver`, a timezone-aware
     `timestamp`, and `decision="approve"`; a dict or a missing argument is
     refused before any I/O. `create_pr` additionally refuses unless
     `VerificationResult.passed` is true. The dry-run CLI preview is **not** a
     publication decision and therefore takes no approval payload — it still
     refuses a failed verification and still records `mode=dry_run`
     (DECISIONS 107).
101. **Dry-run issues get `out/issue_previews/<short>.md`.** §12 only specifies
     `out/pr_previews/` for PRs; the issue path mirrors it so a Phase-6b ISSUE
     route can be previewed without inventing a GitHub call. The issue document
     is sanitized as a whole (headings/tables preserved, HTML/mentions/refs
     neutralized, secrets redacted).
102. **`dq.publications` idempotency lives in application code.** The table has
     no primary key/unique constraint by spec (§4.3). `PublicationStore.find`
     returns the newest row for `(incident_id, kind)` and the base publisher
     returns it before delegating any I/O. The row is inserted only after a
     successful external write, so a process crash in that narrow window can
     still allow a duplicate on retry; concurrent writers are unsupported
     (single-writer assumption, same as `dq.fix_attempts`).
103. **Diffs are computed in memory against the current model files.** The
     Phase-5a allowlist and exact-match rules are reused; the real project is
     read but never written. The same `FilePatch` objects produce the preview
     `.patch` and the live commit content, so the reviewed patch and the
     committed change cannot drift. Unified diffs use LF control lines.
104. **Live GitHub access is a small `GitHubClient` protocol plus a PyGithub
     2.10.0 adapter.** The fake client in tests records every call, so branch
     validation, target-branch-only commits, one PR, the label step and
     idempotency are asserted without a network call. The protocol deliberately
     has no merge/force/base-branch write method; live mode additionally
     refuses a branch equal to `GITHUB_BASE_BRANCH`.
105. **Live mode fails closed.** `GITHUB_MODE=live` requires both a non-empty
     token and a `owner/repo`-shaped repo; otherwise `build_publisher` raises a
     clear `PublicationError` before constructing a client. Client construction
     and every GitHub call are wrapped in an error whose message is redacted by
     value (`Settings` secrets) and by token shape.
106. **Sanitization neutralizes structure, not just characters.** Mentions and
     `#123` refs get a zero-width space (visually identical, not parseable),
     HTML is escaped, Markdown link boundaries (`](`, brackets) are escaped,
     secrets are redacted first, and every field/document has a hard character
     cap. Evidence, diffs and dbt excerpts are fenced and cannot close their
     fence. The zero-width choice avoids showing literal `&#64;` entities
     inside code blocks.
107. **`dqs publish preview <id>` is dry-run only.** It refuses
     `GITHUB_MODE=live` before opening a DB connection; it requires a stored
     root cause, impact and at least one `dq.fix_attempts` row; the latest
     attempt must have a valid plan and a passing verification. It renders
     through the same `render_pr_body`/`build_file_patches` code and records
     the row with `mode=dry_run`.
108. **Live commit messages and PR titles use code-only fields.** Category
     (closed enum), `raw.<table>` (validated identifier), short incident id and
     fixed label text only — no model prose enters git history or the title
     line. The body still carries the sanitized prose.

## Phase 6b — workflow, approval gate, demo (2026-10-22)

109. **Workflow state is a TypedDict; services are injected, never
     checkpointed.** `WorkflowState` holds only validated, JSON-safe values
     (incidents, root cause, impact, plans, verification results, budget
     snapshot). Engines, LLM clients, publishers and tool contexts live in
     `WorkflowDeps`, so `data/checkpoints.sqlite` can be resumed in a fresh
     process without a connection or a secret. A node exception marks the
     incident failed and keeps the checkpoint that proves where it stopped.
110. **All nodes share one `BudgetTracker`; exhaustion is a routing outcome,
     not an exception loop.** `investigate`, `plan_fix` and `verify_fix` report
     `budget_exceeded`; the graph routes that to `build_issue` with the partial
     findings. The wall-clock limit (`INCIDENT_TIMEOUT_SECONDS`) is enforced by
     the same tracker. The default was raised from 300 s to 900 s because a
     real incident can include an LLM retry plus one or more whole-warehouse
     sandbox builds (DECISIONS 76 still applies to failed-call tokens).
111. **The approval gate is a LangGraph `interrupt` with a typed resume
     payload.** The gate writes `awaiting_approval`, interrupts, and persists
     only after `Command(resume={"decision", "approver", "note"})` returns.
     `ApprovalDecision` now models approve *and* reject; a rejection requires a
     note, auto-approval can only approve, and the publisher still refuses
     anything but `approve`. Auto-approval is never a default: it requires the
     explicit `--auto-approve`/`--yes` flag, uses approver `auto`, and is
     labeled in the rendered artifact.
112. **Status transitions live in `dq.incidents`; reasons live in
     `resolution.status_reason`.** There is no new history table. Each
     transition updates `status`, `updated_at` and a short reason; the
     `resolution` JSON is merged rather than replaced so a routing reason or an
     issue title survives approval and publication updates.
113. **Publication is a pending → done lifecycle with a real unique key.**
     `sql/003_publication_status.sql` dedupes legacy rows, adds `status` and a
     unique index on `(incident_id, kind)`. The publisher inserts `pending`
     before any external call and flips it to `done` after. A retry that sees a
     pending row looks up the deterministic branch/issue marker, so a crash in
     the window before `done` cannot create a second artifact. `find` returns
     completed rows only.
114. **Diffs and commits use repository-root-relative paths, and live mode
     checks the base branch.** Patches name
     `warehouse/dbt_project/models/...`, and the live adapter compares each
     base-branch target file's blob hash with the local pre-patch content
     before writing. A mismatch refuses with "push local commits first"
     instead of committing on top of an unseen base change. The fake-client
     test and `git apply --check` run from the repository root.
115. **The demo owns `dqs_demo`, and dbt receives PG* only from
     `Settings`.** The database is created from `template0` through the
     `postgres` maintenance connection; `--reset` drops it `WITH (FORCE)`, and
     a reused demo database has its four schemas reset in place. `dbt_env`
     allowlists the child environment and overrides ambient `PGDATABASE`, so a
     demo build cannot fall back to the warehouse. The demo follows
     `GITHUB_MODE` and is dry-run by default.
116. **The demo uses the production approval path.** Without `--yes` the CLI
     prompts with the same artifact summary a human reviewer sees; with
     `--yes` it sets the workflow's explicit auto-approve flag (approver
     `auto`, visibly labeled). There is no implicit approval and no separate
     demo-only publisher.
117. **`langgraph-checkpoint-sqlite` is pinned to `>=3.1,<4` (3.1.2).** The
     installed `langgraph-checkpoint` 4.3 removed
     `JsonPlusSerializer.dumps`, which broke 2.0.10 at runtime. The installed
     LangGraph 1.2 API was verified directly: `interrupt(value, *,
     response_schema=...)`, `Command(*, resume=...)`, `SqliteSaver(conn)` and
     `compiled.get_graph().draw_mermaid()`.
118. **LangGraph node registration goes through one typed helper.** LangGraph
     1.2's `StateLike` bound is not accepted for a `TypedDict` by mypy, so
     `_add_node` carries one commented `# type: ignore[call-overload]`; the
     runtime contract and every edge are covered by the workflow tests.
119. **`investigate` takes either a `ToolContext` or an explicit tool
     registry.** Workflow tests inject scripted read-only tools without a
     database; production still builds the real context. Supplying neither is
     an `InvestigatorError` rather than silently running without tools.
