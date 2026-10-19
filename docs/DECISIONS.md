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
