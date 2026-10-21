# HANDOFF — read this first in a fresh session

Last updated: 2026-10-21 (Phase 6a complete) · Do not treat this file as a
spec; AGENTS.md is rules, docs/PHASES.md is order, docs/ARCHITECTURE.md is
design.

## Current state

- **Phase 6a (publish package: dry-run, live GitHub path, approval gate) is
  complete and ready for review.** Report: `docs/phase-notes/phase-6a.md`;
  review packet: `docs/review/phase-6a-review.md`. **Do not start Phase 6b**
  (LangGraph workflow, interrupt/resume approval, `dqs incident run|approve|
  reject`, `dqs demo`) until the owner says so.
- **Phase 6a deliverables**: `publish/base.py` (typed `ApprovalDecision` gate,
  failed-verification refusal, idempotent template method), `publish/store.py`
  (`dq.publications` + latest attempt), `publish/diff.py` (in-memory exact
  patch replay + `difflib`), `publish/body.py` (deterministic §12 body),
  `publish/sanitize.py`, `publish/dry_run.py` (`out/pr_previews/`,
  `out/issue_previews/`), `publish/github.py` (narrow client protocol, PyGithub
  2.10 adapter, fail-closed live mode, one PR, `data-quality` label), and
  `dqs publish preview <id>` (dry-run only, refuses live before opening the
  DB). DECISIONS 100–108. No workflow graph or real GitHub call in this phase.
- **Phase 5b (fixer agent, issue builder, `plan-fix`) remains complete.**
  Report: `docs/phase-notes/phase-5b.md`; packet:
  `docs/review/phase-5b-review.md`.
- **Phase 5b deliverables**: `resolve.py` (deterministic PR/ISSUE/NONE),
  `agents/fixer.py` + `agents/prompts/fixer.md` (no-tools planner with
  lineage-selected candidate SQL, native structured output, bounded retries,
  verifier feedback, per-attempt persistence), `agents/issue_builder.py`
  (deterministic Markdown + static category playbook), `dqs incident
  plan-fix <id>`, test-only `tests/helpers/oracle.py`, and DECISIONS 90–98.
  The fixer never executes SQL or edits files; deterministic code applies the
  validated plan and the Phase-5a verifier judges it.
- **Phase 5a (deterministic fix verification) remains complete.** Report:
  `docs/phase-notes/phase-5a.md`; packet: `docs/review/phase-5a-review.md`.
  Deliverables: `models/fix.py` (`Edit`, `FixPlan`, `CriterionResult`,
  `VerificationResult`, limits), `verify/patch.py` (Windows-hardened
  allowlist, exact-match application, relevance, temp copy, schema naming),
  `verify/sandbox.py` (isolated dbt build, minimal env, timeout, cleanup,
  `dq.fix_attempts`), `verify/criteria.py` (contract conformance, 95% row
  preservation, category targeted checks), `dqs verify <plan.json>` and
  `dqs verify cleanup`, DECISIONS 80–89.
- Phase 5a acceptance is real-dbt on faulted clones: three hand-written good
  patches (F1 coalesce, F3 regexp_replace, F6 row_number dedupe) pass; eleven
  bad/adversarial plans fail the asserted criterion (row preservation,
  contract conformance, dbt build/excerpt, targeted check, allowlist,
  relevance, exact match, size limit). The real manifest and dev layer counts
  are unchanged; killed/failing runs leave no `dqs_sandbox_*` schema.
- Phase 5b real-model evidence (2026-10-20): F1 `SOURCE_COLUMN_RENAMED` on
  `openai/gpt-oss-120b` passed on attempt 2 (7,451 fixer tokens); F3
  `SOURCE_TYPE_CHANGED` on 120b passed on attempt 1 (2,919 tokens); F6
  `DUPLICATE_RECORDS` on `openai/gpt-oss-20b` passed on attempt 2 (4,485
  tokens). All three oracle checks passed. F6 used 20b because the 120b key hit
  its daily token quota; a later 20b consistency run passed F3/F6 but exhausted
  F1, which is recorded honestly in the phase note. JSON in git-ignored
  `out/phase-5b-llm-*.json`.
- **Phase 4 (4a + 4b) is complete.** Phase 4b report:
  `docs/phase-notes/phase-4b.md`; review packet:
  `docs/review/phase-4b-review.md`. The Phase-4 gate is met (F1/F4 step
  timelines, sample `RootCause`, token usage).
- **Phase 4b deliverables**: `models/root_cause.py`, `models/step.py`,
  `agents/steps.py` (memory + Postgres sinks, redaction, 4 KB cap),
  `agents/investigator.py` (explicit bounded loop, native structured output →
  `submit_root_cause` → UNKNOWN, evidence provenance filter, model-error
  recovery), `agents/prompts/investigator.md`, `incidents.save_root_cause`,
  `dqs incident investigate <id>`. Hardening: `ToolContext` role check, SQL
  function allowlist, LLM retry policy, contract regeneration guard.
  DECISIONS 70–79.
- **Phase 4a deliverables** (unchanged): `agents/llm.py` (only
  `init_chat_model` call; providers
  `anthropic|openai|google_genai|ollama|openai_compatible|fake`; per-call
  latency/tokens; budget tracker; redaction), `agents/smoke.py` (4 probes),
  `dqs llm smoke`, `tools/` (`base`, `sql_guard`, `readonly_sql`, `profiling`,
  `renames`, `dbt_tools`, `contracts_tool`, `lineage_tool`, `registry`),
  `dbt_run.latest_run`, DECISIONS 62–69.
- Tests: **521 unit + 90 integration (non-llm)** passing; Phase-5b real-LLM
  acceptance covers F1/F3/F6 (two models, see above); the earlier 3
  investigator smoke tests still pass with `RUN_LLM_TESTS=1`; ruff + format +
  mypy clean (93 source files). The one skipped unit test is real OS symlink
  creation (Windows privilege); junction + simulated-symlink branches pass.
- Real-model evidence (2026-10-20, Phase 4b, `openai/gpt-oss-120b` via
  `openai_compatible`): F1 rename 0.93/PR, F4 nulls 0.93/ISSUE, F6 duplicates
  0.95/PR; all schema-valid. Full timelines/JSON in git-ignored
  `out/phase-4b-llm-smoke-*.json`.
- LLM keys come only from `Settings` (`.env`), never the process environment:
  `GOOGLE_API_KEY` → `google_api_key`, and `get_llm` passes `api_key=`
  explicitly for every provider (DECISIONS 69). Do not print or commit keys.
- Phase 3 (lineage/impact) remains complete: `lineage/{manifest,graph,
  columns,impact}.py`, `dqs impact`, parsed dbt failures in
  `dq.pipeline_runs`; report `docs/phase-notes/phase-3.md`, packet
  `docs/review/phase-3-review.md`, DECISIONS 55–61.
- Faults today: `sim/faults.py` with ONE injection registry covering all ten
  kinds. Contracts: all 13 in `contracts/` are `status: approved`.
- Warehouse state right now: exactly the clean contract baseline — 30 batches
  (`batch_20260101`…`batch_20260130`) in `dq.batches`, max business_date
  2026-01-30, no 2026-01-31+ batches, 0 incidents, 0 `dq.agent_steps`,
  0 `dq.fix_attempts`, 0 `dq.publications`, no `dqs_sandbox_*` schemas.
  Integration tests clean up their throwaway databases; no `dqs_*` clone
  persists as of this update.
- Phase-3 notes (DECISIONS 55–61): failing tests are stored per-run (never
  read from live files); impact scope = column (F1/F2/F10) / table (volume,
  F9) / warehouse (freshness/missing batch); manifest staleness includes
  missing `compiled_code` (a failed `dbt build` leaves a torn manifest —
  `dbt compile` regenerates); score formula documented; `models_referencing_
  column` is precision-first (direct-from-source only).

## Running the project (PowerShell, Windows)

```powershell
cd 'D:\study\python\data steward'

# one-time (venv is Python 3.12 — NOT the shell-default 3.13):
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env

# every session:
docker compose up -d --wait postgres        # or: .venv\Scripts\dqs dev up
.venv\Scripts\dqs db ping                    # rw + ro must both print PostgreSQL 16.x
.venv\Scripts\dqs db init                    # idempotent (roles/schemas/dq tables + 002)

# quality gates (equivalents: make lint / test / test-int / check)
.venv\Scripts\dqs dev lint                   # ruff check + ruff format --check + mypy src
.venv\Scripts\dqs dev test                   # unit only
.venv\Scripts\dqs dev test-int               # needs Postgres up
.venv\Scripts\dqs dev check                  # all three

# Phase 1 flow
.venv\Scripts\dqs db reset                   # wipes raw/staging/marts/dq!
.venv\Scripts\dqs sim seed --days 30         # ~3–4 min: 30 × (generate+load+dbt build)
.venv\Scripts\dqs dbt build --target dev     # standalone build
.venv\Scripts\dqs sim day --date 2026-01-31  # --date (not positional); --/--no-dbt flag
.venv\Scripts\dqs sim day --date 2026-01-31 --fault orders:row_count_drop:ratio=0.5

# Phase 2b flow — NEVER run the monitor against the dev warehouse (it flips
# batch status to 'checked'); use a clone:  $env:PGDATABASE='dqs_accept'.
# `incident investigate` also writes dq.incidents.root_cause and dq.agent_steps,
# so point it at a clone too.
.venv\Scripts\dqs contracts approve --all
.venv\Scripts\dqs check batch batch_20260131
.venv\Scripts\dqs monitor run                              # default --as-of = max date + 1d
.venv\Scripts\dqs monitor run --as-of 2026-02-03T00:00Z
.venv\Scripts\dqs incidents list
.venv\Scripts\dqs incidents show <uuid>

# Phase 3 flow (needs a faulted day first, then:)
.venv\Scripts\dqs impact <incident_uuid>     # deterministic blast radius from the manifest DAG
.venv\Scripts\python scripts\generate_manifest_fixture.py   # refresh the unit-test fixture

# Phase 4a flow (no key needed for the wiring probe)
$env:LLM_PROVIDER='fake'; .venv\Scripts\dqs llm smoke
# real endpoint: set LLM_PROVIDER=openai_compatible, LLM_MODEL, LLM_BASE_URL, LLM_API_KEY
# then run the same `dqs llm smoke` and record provider/model/date in the report.

# Phase 4b flow — investigate is a node, not the workflow. Point it at a CLONE.
$env:PGDATABASE='dqs_accept'                 # or any clone with an incident
.venv\Scripts\dqs incident investigate <incident-uuid>
.venv\Scripts\dqs incident investigate <incident-uuid> --provider fake --model fake-1

# real-model F1/F4/F6 smoke: seeds its own template, clones per scenario, drops all
$env:RUN_LLM_TESTS='1'
.venv\Scripts\python -m pytest tests\integration\test_investigator_llm.py -m llm -s -q
# results: out\phase-4b-llm-smoke-*.json (git-ignored)

# Phase 5a flow — deterministic, no LLM. Point verification at a CLONE too.
.venv\Scripts\dqs verify path\to\plan.json     # serialized FixPlan; temp dbt project/schemas
.venv\Scripts\dqs verify cleanup               # drop stale dqs_sandbox_* schemas
# acceptance (real dbt on F1/F3/F6 faulted clones; leaves dev warehouse untouched)
.venv\Scripts\python -m pytest tests\integration\test_verify_sandbox_db.py -m integration -s -q

# Phase 5b flow — manual node command; point it at a CLONE with a stored root cause/impact.
.venv\Scripts\dqs incident investigate <incident-uuid>   # real model, stores root cause
.venv\Scripts\dqs impact <incident-uuid>                 # stores impact report
.venv\Scripts\dqs incident plan-fix <incident-uuid>      # routing + attempts + plan + verdict
# cleanup / stale sandboxes from an interrupted run:
.venv\Scripts\dqs verify cleanup

# real-LLM chain: investigator -> fixer -> verifier -> clean-data oracle
$env:RUN_LLM_TESTS='1'
.venv\Scripts\python -m pytest tests\integration\test_fixer_llm.py -m llm -s -q
# results: out\phase-5b-llm-*.json (git-ignored); set LLM_MODEL to a model with quota
# oracle-only deterministic acceptance:
.venv\Scripts\python -m pytest tests\integration\test_verify_sandbox_db.py -m integration -q -k "oracle"

# Phase 6a flow — dry-run publishing only; never calls GitHub.
# Point it at a CLONE with a stored root cause, impact and passing fix attempt.
.venv\Scripts\python -m pytest tests\integration\test_publish_db.py tests\integration\test_publish_cli_e2e.py -m integration -q
.venv\Scripts\dqs publish preview <incident-uuid>
# writes out\pr_previews\<short>.md + .patch and one dq.publications row (mode=dry_run);
# a second run prints reused=True and makes no write. Live mode is exercised only
# through FakeGitHubClient in 6a (no CLI command, no real token/repo).
```

Entry point: the `dqs` console script (`.venv\Scripts\dqs.exe`); it exists because
`pip install -e .` ran. If commands are missing, re-run the install line.

## Gotchas that cost real time in Phases 0–2

1. **dbt property files must live under `models/`** — `sources.yml`/`exposures.yml`
   at the project root silently don't parse in dbt 1.12 ("source named
   'raw.customers' which was not found"). They live in `models/` now.
2. **Generic test args need `arguments:` nesting** (dbt ≥1.11):
   `accepted_values: {arguments: {values: [...]}}`, `relationships: {arguments:
   {to: ref('x'), field: id}}`. Top-level args only warn today, break later.
3. **Customer IDs must be contiguous across days** (`cumulative(day-1)+1 …
   cumulative(day)`), NOT slot-per-date: the first slot-based version left id
   gaps and the dbt `relationships` test failed with 1901 orphans on *clean*
   data. Other tables keep per-date slots (safe for them).
4. **pytest `-m` takes ONE argument**: pass `-m "not integration"` as a single
   string (splitting makes pytest treat `integration` as a path).
5. **mypy needs the pydantic plugin** (`plugins = ["pydantic.mypy"]` in
   pyproject) or `Settings()` reports bogus missing-argument errors.
6. **Docker Desktop must actually be running** (the CLI error for a stopped
   daemon is cryptic: `%2F%2F.pipe%2FdockerDesktopLinuxEngine ... file not
   found`). Start Docker Desktop first, then `dqs dev up`. WSL/hosts: compose
   binds port 5433 (host) → 5432; a local real Postgres on 5432 is fine.
7. **`make` is not installed** on this host — use `dqs dev …` (identical
   behavior); do not add logic that only works via make.
8. **dbt executable resolution**: `dbt_run.dbt_executable()` looks next to the
   running interpreter — never assume PATH.
9. **PowerShell mangles UTF-8 in captured subprocess output** (rich glyphs show
   as `�`) — a console-encoding artifact only; redirect to a file or read the
   DB to check real content. Related: a stray non-UTF-8 byte (`cp1252` em-dash)
   in a Markdown file breaks `ruff format --check .` repo-wide.
10. **Integration tests must not touch the dev warehouse.** `monitor run` flips
    `dq.batches.status` to `checked`; the monitor tests clone a seeded template
    DB (`dqs_monitor_template`) with `CREATE DATABASE … TEMPLATE` and drop it.
11. **The detection matrix needs contiguous days** — skipping a business date
    makes clean FK values look orphaned (customer ids are date-slotted), which
    would produce false positives unrelated to the injected fault.


12. **A failed `dbt build` leaves a torn manifest** — children of broken
    nodes are skipped, so they carry no `compiled_code` (dbt 1.12, observed).
    The lineage loader treats that as *stale* and regenerates with
    `dbt compile` (needs Postgres up).
13. **dbt 1.12 writes `name: null` in `run_results.json`** — for readable
    node names use `unique_id.split(".")[2]`, never the hash tail.
14. **Windows console mojibake is a display artifact**, not file corruption:
    rich tables/Unicode print fine through pytest capture; `Get-Content` and
    `python -c` output look mangled in cp1252 consoles.

## File map — the 10 files that matter most

| File | What it does |
|---|---|
| `AGENTS.md` | Rules: protocol, guardrails (§7), testing rules (§8), phase-note template (§9) |
| `docs/PHASES.md` | Order of work + per-phase acceptance criteria (the checklist to satisfy) |
| `docs/ARCHITECTURE.md` | Design doc: §4 data model, §5 dbt, §6 faults, §7 contracts/checks, §10 agents, §11 verification, §14 benchmark |
| `docs/DECISIONS.md` | Open decisions I logged (108 entries); read the phase sections you extend |
| `src/dqs/sim/generator.py` | Deterministic 5-table generator; seeding + ID allocation invariants |
| `src/dqs/sim/loader.py` | Schema-evolving raw loader; atomic per-batch transaction; dq metadata |
| `src/dqs/dbt_run.py` | The ONE dbt entry point; records `dq.pipeline_runs` with 4 KB logs |
| `src/dqs/sim/pipeline.py` | seed/day orchestration: generate → load → dbt build |
| `src/dqs/config.py` | `Settings` (pydantic-settings; env aliases `PGHOST`-style) |
| `warehouse/dbt_project/models/staging/schema.yml` | Staging tests + relationships (clean-day detection baseline) |
| `src/dqs/cli.py` | Typer CLI: `db`, `dev`, `sim`, `dbt`, `contracts`, `check`, `monitor`, `incidents`, `incident`, `impact`, `llm`, `verify` |
| `src/dqs/db.py` | rw/ro engines, init/reset/ping |
| `src/dqs/checks/engine.py` | `CheckEngine.run(batch_id)` (raw batch scope) / `run_table` (staged); `from_directory` loads approved contracts |
| `src/dqs/incidents.py` | Violation → incident: `fingerprint`, `build_drafts`, `persist_incidents`, `_merge`, `list_incidents`, `get_incident` |
| `src/dqs/monitor.py` | The §8 loop: `run_monitor`, `default_as_of`, `unchecked_batches`, `parse_as_of` |

| `src/dqs/lineage/graph.py` | dbt manifest → networkx DAG; upstream/downstream/nodes_for_raw_table (Phase 3) |
| `src/dqs/lineage/impact.py` | deterministic ImpactReport builder + `render_summary` Phase-4 hook (Phase 3) |
| `src/dqs/agents/llm.py` | the ONLY provider construction; per-call tokens/latency; budgets; retries; fake model (Phase 4a/b) |
| `src/dqs/tools/sql_guard.py` | deny-by-default SQL guard + positive function allowlist + read-only executor (Phase 4a/b) |
| `src/dqs/tools/registry.py` | the nine typed tools the investigator sees (Phase 4a) |
| `src/dqs/agents/investigator.py` | explicit bounded tool loop; final channels; provenance; recovery (Phase 4b) |
| `src/dqs/verify/patch.py` | hardened edit allowlist, exact-match application, relevance rule, temp-project copy (Phase 5a) |
| `src/dqs/verify/sandbox.py` | isolated dbt build, minimal env, timeout, cleanup, `dq.fix_attempts` persistence (Phase 5a) |
| `src/dqs/verify/criteria.py` | contract conformance, row preservation, category targeted checks (Phase 5a) |
| `src/dqs/agents/fixer.py` | no-tools planner: lineage candidates, `FixPlanDraft` -> `FixPlan`, bounded retry, attempt persistence (Phase 5b) |
| `src/dqs/agents/prompts/fixer.md` | fixer system prompt: minimal edit, preserve outputs, never hide rows, data-not-instructions (Phase 5b) |
| `src/dqs/agents/issue_builder.py` | deterministic incident issue + static category playbook (Phase 5b) |
| `src/dqs/resolve.py` | deterministic PR / ISSUE / NONE routing (Phase 5b) |
| `src/dqs/publish/base.py` | typed approval gate, failed-verification refusal, idempotent publisher (Phase 6a) |
| `src/dqs/publish/github.py` | narrow GitHub protocol, PyGithub adapter, branch/PR safety, fail-closed live mode (Phase 6a) |
| `src/dqs/publish/dry_run.py` | `out/pr_previews/` + `out/issue_previews/`, `mode=dry_run` publication (Phase 6a) |
| `src/dqs/publish/body.py` / `diff.py` / `sanitize.py` | deterministic PR body, exact shared patch, structural neutralization (Phase 6a) |
| `tests/helpers/oracle.py` | test-only clean-data answer key; never imported by `src/dqs` (Phase 5b) |
| `src/dqs/agents/steps.py` | `dq.agent_steps` sinks: sink-owned seq, redaction, 4 KB cap (Phase 4b) |
| `src/dqs/agents/prompts/investigator.md` | investigator system prompt (Phase 4b) |
| `src/dqs/models/root_cause.py` / `step.py` | `RootCause`/`EvidenceItem` and `StepRecord` (Phase 4b) |

(There are also: `src/dqs/errors.py`, `src/dqs/dev.py`, `src/dqs/checks/{schema,types,nulls,unique,fk,ranges,volume,freshness}.py`,
`src/dqs/contracts/{store,generator,profiler,fitting,parsing,generation}.py`,
`src/dqs/models/{contract,violation,incident,enums}.py`, `tests/conftest.py`,
`tests/unit/test_*.py`, `tests/integration/test_*.py`, dbt `models/*.sql`,
`contracts/*.yml`, `docs/phase-notes/phase-{0,1,2a,2b,3,4a,4b,5a,5b,6a}.md`,
`docs/review/phase-{2b,3,4a,4b,5a,5b,6a}-review.md`.)

## DECISIONS.md entries Phase 2 must build on

- **5**: `sql/001_init.sql` creates schemas + `dq.*` only (no raw DDL) — the
  loader owns raw table creation.
- **18–19**: customer ids contiguous; orders/items/payments use per-date slots
  (1000/5000/2000). Any contract/uniqueness/FK check must respect this.
- **20**: children derive from the generated orders frame — payment sums equal
  order totals exactly; a fault touching one table will desync its children by
  design (that's what the checks must detect).
- **21**: products ship 0 rows after day 1 but the header stays in every batch
  (this is what makes F9 `missing_table` unambiguous).
- **22**: duplicate `batch_YYYYMMDD` in `dq.batches` → load error; re-seeds must
  go through `dqs db reset`.
- **23**: `dq.batch_tables.columns` records the observed batch header WITHOUT
  `_batch_id`/`_loaded_at` — contract schema checks compare against this list.
- **16–17**: dbt quirks (arguments nesting, yml under models/) — keep in mind
  when staging contract generation touches manifest/schema.yml.

## Phase 2 invariants (now enforced — read before Phase 3+)

- **Generator stays a pure function of `(seed, date)`** — it must never read the
  DB or count loaded rows, so *skipped days* (e.g. the missing-batch scenario,
  skip 2026-02-01) still work and history can be replayed deterministically.
- **Fault injection happens AFTER generation, BEFORE loading** — faults are pure
  functions over the generated frames (arrival-time faults like F8 are the
  exception; they adjust metadata). ✅ resolved in 2a: `loader.load_batch` takes
  an optional `arrived_at` override and `pipeline.load_day(specs=…)` applies the
  faults; F8 returns that override (`dq.batches.arrived_at` + `_loaded_at` move,
  frames don't). Tested e2e (task 1).
- **Zero false positives is an acceptance criterion** — clean days
  2026-01-31…02-04 must produce 0 incidents. That's why clean-day generation
  (not the checks!) carries the burden: ranges/means/volume bounds come from the
  30-day baseline with headroom (§7.2 rules).
- **Contract generation must be byte-identical across runs** (sort keys, stable
  float formatting) — this is enforced by `.gitattributes` LF normalization (see
  DECISIONS 3) and tested in Phase 2 acceptance (now enforced by `test_regenerated_contracts_are_byte_identical_to_committed`).
- **`volume` for dimension tables excludes the initial-load batch** (customers
  day-1=2000 must not enter bounds); for products there are no non-initial
  batches — handle explicitly (don't crash on an empty histogram).
- **Detection-matrix discipline**: for each of F1–F10 the expected check fires
  on the expected table/column and NO unrelated breaking violation fires
  (warning-level F10 aside). Don't hand-code fault names anywhere (AGENTS §7.9).
- **Fingerprint** = `sha1(table | check_type | column)`; consecutive same-fault
  days must update ONE open incident, not two. ✅ `tests/unit/test_incidents.py`
  + `test_monitor_e2e.py::test_same_fault_on_two_days_keeps_one_open_incident…`.
- **CLI surface per §15 is complete through Phase 2**: `dqs contracts
  generate|show|approve`, `dqs check batch`, `dqs monitor run [--as-of]`,
  `dqs incidents list|show`, and `sim day --fault SPEC …`.
- **Unknown-key rejection is tested** (`test_contracts_e2e.py`).
- **Value checks never cascade** — a missing column is reported once
  (`schema.missing_column`); an absent `required_in_batch` table is owned by
  `volume.rows`. Keep this when adding checks in later phases.
- **`monitor run` mutates batch status** — never point it at the dev warehouse.
  Use a throwaway clone (`CREATE DATABASE … TEMPLATE`) as the integration tests do.

## Known limitations / half-done

- Phase 5a verification uses the `sandbox` target, but the dbt selector is
  `staging.*+` (a superset of the §11 `<changed>+`); it rebuilds all staging
  models and marts in throwaway schemas (DECISIONS 86). Expect a whole-warehouse
  build per verification.
- Sandbox schema names are incident-derived (`dqs_sandbox_<8 hex>`); concurrent
  verifications of the same incident collide, and `verify cleanup` is a manual
  recovery tool, not a concurrency primitive.
- `dq.fix_attempts` has no unique constraint on `(incident_id, attempt_no)`;
  sequential single-writer use is safe, concurrent writers are not.
- Verification checks the changed staging table's contract only; downstream
  mart contracts are not separately checked. The plan's incident context
  (`batch_id`, `category`, `affected_table`) is trusted — `plan_fix` now fills
  it from the stored root cause/incident; `dqs verify` still trusts a serialized
  plan. The fixer only sees staging models that directly select from the
  affected raw source; mart/macro fixes route to ISSUE.
- The clean-data oracle is test-only and covers `stg_orders` (F1/F3/F6). It is
  not a runtime guard: a production verification can still accept a
  verifier-passing patch that writes wrong values. The oracle isolation test
  ensures no `src/dqs` import.
- `dq.fix_attempts` attempt numbers are allocated with `MAX(attempt_no)+1` and
  have no unique constraint; concurrent fixers for one incident can collide.
  The fixer has no tool fallback: a provider that cannot emit `FixPlanDraft`
  produces a failed `fixer_output` attempt.
- Exact-match patch strings are byte-sensitive; the dbt project is LF-normalized.
  The real OS symlink creation test is skipped on this Windows account;
  junction and simulated-symlink coverage pass.
- `dqs sim day` requires the baseline to be seeded first (documented; no
  defensive workaround — intentional).
- `sim seed` always rebuilds all dbt models per day (30 × full build ≈ 3–4 min);
  benchmark resets may prefer a snapshot/restore strategy in Phase 8.
- `dqs sim day --fault`, contracts, check engine / monitor / incidents,
  `dqs impact`, the Phase-4a LLM/tool layer, the Phase-4b investigator, the
  Phase-5a deterministic sandbox verification, the Phase-5b fixer/issue/
  plan-fix path and the Phase-6a publish package all work and are tested.
  **Phase 6b** (workflow graph, interrupt/resume approval gate, `dqs demo`) is
  next but NOT started.
- Publishing is **application-level idempotent only** (DECISIONS 102, 105):
  `dq.publications` has no unique key, the row is written after the external
  write, and concurrent writers are unsupported. A crash between the GitHub
  call and the insert can duplicate on retry; live mode reuses an existing
  branch/PR to reduce the window, not eliminate it.
- The PyGithub adapter is not network-tested. Its signatures were checked
  against installed PyGithub 2.10.0 and the publisher logic is covered through
  the fake client; the optional real PR in PHASES still needs an owner test
  repo/token.
- An existing open PR is returned without re-checking that its diff still
  matches the current plan. Phase 6b should make an incident immutable after
  approval (or compare a stored patch hash) so the reviewed diff cannot go
  stale.
- `dqs publish preview` is a preview, not an approval: it takes no approval
  payload and records `mode=dry_run`. It needs persisted root cause, impact and
  a passing attempt. The ISSUE path has a publisher + preview file but no CLI
  command yet.
- **The real-model smoke is 3 scenarios × 1 run** (`openai/gpt-oss-120b`,
  2026-10-20): F1 0.93/PR, F4 0.93/ISSUE, F6 0.95/PR. It is not benchmark-grade
  evidence (no repeats/variance, `run_dbt=False` so no failing run record) and
  the prompt was not tuned on it. Do not quote it as a benchmark (AGENTS §7.9).
- The investigator recovers from a provider rejecting a model-invented
  `RootCause`/`json` tool call **after evidence exists** by trying the final
  channel; before any evidence the error still raises. Failed calls record
  latency/error but zero tokens, so token budgeting under-counts failures
  (DECISIONS 76).
- `dqs incident investigate` leaves the incident status unchanged: lifecycle
  transitions belong to the Phase 6b workflow. It persists `root_cause` + steps.
- `dq.agent_steps` has no unique constraint on `(incident_id, node, seq)`; the
  sink reads `MAX(seq)` first, which is safe for one sequential run per
  incident, not for concurrent writers (Phase 6b workflow assumption).
- SQL guard is parser + curated tests, not fuzzed. The positive function
  allowlist deliberately rejects harmless unlisted helpers (false rejection >
  false acceptance). See the phase-4b review for the remaining gaps.
- `dqs monitor run` enforces raw contracts only; staged/marts contracts are
  reachable via `CheckEngine.run_table` but not yet run by the monitor (future
  phase).
- `test_monitor_e2e.py` seeds the 30-day baseline into a template DB once
  (~2.5 min); `test_lineage_e2e.py` seeds its own module DB (~1 min) —
  acceptable now, revisit if scenarios multiply.
- Column-level lineage (`models_referencing_column`) is precision-first:
  only models selecting directly FROM the raw source count; no join scoping
  (DECISIONS 58). Chained models and dbt-visible `SELECT *` are missed.
- Pre-002 `dq.pipeline_runs` rows have `failed_nodes = NULL` — their
  incidents honestly report "no parsed dbt failure record".
- `dqs impact` may trigger a `dbt compile` manifest regeneration (torn
  manifest after a failed build); Postgres must be up.
- `dq.publications` has no primary key by spec (§4.3) — idempotency is
  enforced in application code (Phase 6a).
- Windows console shows mojibake for some rich Unicode glyphs — cosmetic only.
- No git remote; push instructions live with the owner.
