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
