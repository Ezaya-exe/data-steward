# Phase 7 — Streamlit UI

Date: 2026-10-25 · Scope: `docs/PHASES.md` Phase 7 and
`docs/ARCHITECTURE.md` §13. A thin review console over the existing service
layer. No benchmark scenario was run, no `dqs demo` was re-run, and no LLM or
GitHub call was made while building or testing this phase.

## What was built

* `src/dqs/services.py` — the UI-facing service layer:
  * typed view models (`IncidentRow`, `StepView`, `ChecklistItem`,
    `FixAttemptView`, `PublicationView`, `IncidentDetail`, `ContractView`,
    `BenchmarkView`, `DecisionResult`);
  * pure builders (`filter_incident_rows`, `database_names`,
    `build_attempt_view`, `build_incident_detail`, `benchmark_notes`,
    `build_benchmark_view`, `require_awaiting_approval`);
  * read functions over the existing public APIs (`list_incidents`,
    `get_incident`, `load_contracts`, `render_markdown`) plus parameterized reads
    for `dq.agent_steps`, `dq.fix_attempts` and `dq.publications` (no public list
    functions existed);
  * `decide_incident`, the only write, which gates on `awaiting_approval` and
    calls `IncidentWorkflow.resume` on a r/w engine.
* `ui/common.py` — page config, sidebar database selector, UTC formatting,
  publish-mode banner, and the AppTest injection seam.
* `ui/app.py` — Incidents table with status/severity/table/search filters.
* `ui/pages/incident_detail.py` — violations, agent timeline, root-cause card
  (“model-reported, uncalibrated”), impact, every fix attempt with its
  verification checklist and observed values, a diff viewer, publications, and
  the approve/reject gate (live mode requires an extra checkbox).
* `ui/pages/contracts.py` — contract table, status counts, deterministic YAML.
* `ui/pages/benchmark.py` — results-file chooser, provenance, pilot/partial/infra
  warnings, the existing report Markdown (overall + per-kind tables with 95%
  Wilson intervals).
* `tests/unit/test_services.py` — 30 tests over the pure builders and guards.
* `tests/unit/test_ui_pages.py` — 13 AppTest tests, each page empty and
  populated, plus approval-gating and no-raw-SQL/no-unsafe-HTML guards.
* `tests/integration/test_ui_services_db.py` — seeds every artifact into a
  `dqs_ui_it_*` clone of `dqs_template`, reads it back through the service layer,
  and drops only that database.
* `docs/img/README.md` — the manual screenshot instructions listed below.

## How to run the UI

```powershell
cd 'D:\study\python\data steward'
docker compose up -d --wait postgres        # or: .venv\Scripts\dqs dev up
.venv\Scripts\dqs db ping                    # rw + ro must both connect
.venv\Scripts\python -m streamlit run ui/app.py
```

Open the Local URL Streamlit prints (default `http://localhost:8501`). The
sidebar defaults to `dqs_demo` when it exists, otherwise `warehouse`; benchmark
scenario databases and `dqs_template` are never listed. The console is read-only
except Approve/Reject, which is only shown for `awaiting_approval` incidents.
For a populated demo, first run `dqs demo --yes --reset` (that is a Phase 6b
command; it was not run while building this phase). For the approval screenshot,
the incident must be paused at `awaiting_approval`: run `dqs demo` interactively
and leave it at the prompt, or run `dqs incident run <uuid>` on a faulted
incident (it stops at the gate).

Quality gates:

```powershell
.venv\Scripts\dqs dev lint
.venv\Scripts\dqs dev test
.venv\Scripts\dqs dev test-int
```

## Acceptance results

| Criterion | Result | Evidence |
|---|---|---|
| `streamlit run ui/app.py` starts | PASS | Headless run on port 8599: `GET /_stcore/health` → `200 ok`; `GET /` → `200` (6467 bytes). Log: `You can now view your Streamlit app in your browser.` |
| Pages load with data | PASS | AppTest: 13/13 page tests pass; real-service smoke rendered all four pages with no `st.exception`, including an incident from `dqs_demo` and the pilot results file. |
| Pages load without data | PASS | AppTest empty-state tests for Incidents, Incident detail, Contracts and Benchmark; each shows an explicit `st.info`, not an exception. |
| Detail shows the approval payload | PASS | AppTest asserts violation text, timeline tool name, root-cause category and confidence label, impact exposure/owner, checklist criteria and observed values, and the rendered diff. |
| Approve/Reject only when `awaiting_approval` | PASS | AppTest: zero buttons for a `resolved` incident; both buttons present for `awaiting_approval` (Reject disabled until a note is typed). `require_awaiting_approval` has its own unit tests. |
| Approve resumes the paused workflow | PASS (service layer) | `test_decide_incident_resumes_an_awaiting_incident` and the DB integration test verify the service calls `resume(decision, approver, note)` only after the status gate. The real LLM workflow was not re-run in this phase. |
| Live mode needs explicit confirmation | PASS | AppTest with `github_mode=live`: Approve is disabled until the `live_confirm` checkbox is checked; the banner says LIVE. |
| Reject requires a note | PASS | Service guard + disabled button + AppTest that types a note and asserts `decision="reject"` reached the fake workflow. |
| No raw SQL in `ui/` | PASS | Static test scans every `ui/**/*.py` for `SELECT `, `FROM dq.`, `.connect()` and `unsafe_allow_html=True`; none present. |
| No secrets displayed | PASS (code review) | The UI reads only `pg_database`/`github_mode` from `Settings`; it never accesses tokens and publishes only publication URLs/paths. Screenshot review is still on the manual checklist. |
| Lint + type check | PASS | `dqs dev lint`: `All checks passed!`, `208 files already formatted`, mypy `Success: no issues found in 102 source files`. |
| Unit tests | PASS | `dqs dev test`: **704 passed, 1 skipped** (the existing Windows symlink skip), 106 deselected. |
| Integration tests | PASS | `dqs dev test-int`: **100 passed, 6 skipped** (LLM-gated), 705 deselected, 6:47. The new DB test is included. |
| Benchmark fingerprint unchanged | PASS | `dqs bench preflight` all PASS before and after. `code_fingerprint()` was `17ac403b512ffd73d0b07df91047d55df846aa0e72c1c787d35315b91f1c6a26` both times. |

The integration test clones `dqs_template` and drops `dqs_ui_it_<8hex>` in
`finally`; after the suite, `list_steward_databases()` was exactly
`['dqs_demo', 'dqs_template']` (no `dqs_bench_*` or test leftovers).

## Screenshots to take manually

Save them in `docs/img/` (the directory and filename list are in
`docs/img/README.md`). Take them at a ~1440 px window width, light theme,
with the sidebar visible and no `.env`/token content on screen.

1. `docs/img/01-incidents.png` — Incidents page on `dqs_demo`, showing the
   filters and a table with at least two statuses.
2. `docs/img/02-incident-detail.png` — Incident detail, top half: status
   metrics, violations table, timeline, root-cause card (category, evidence,
   “model-reported, uncalibrated”), impact models/exposures.
3. `docs/img/03-incident-approval.png` — Incident detail, bottom half: fix
   attempt with the PASS/FAIL checklist and observed values, diff viewer,
   publication row, and the dry-run approval banner with Approve/Reject.
4. `docs/img/04-benchmark-pilot.png` — Benchmark page with
   `20261023T135123Z_openai-gpt-oss-120b.json` selected: visible pilot/partial
   warnings, provenance table (model, date, git SHA, fingerprint, harness and
   prompt hashes), infra-error metric, and the overall/per-kind tables.

`docs/img/05-contracts.png` is optional (contract table + YAML + status).

## Design choices and why

* **Service layer owns all SQL and engines.** Pages call typed functions; the
  `ui/` static guard proves there is no query or connection there. This is the
  §13 requirement and keeps future UI changes from re-implementing checks.
* **Reads use `dqs_ro`; the only write is `resume`.** The sidebar builds a
  read-only engine, and `decide_incident` creates a r/w engine only for the
  resume path. The status gate lives in the service, so a stale page cannot
  approve an incident that already moved on.
* **View models instead of raw dicts.** `build_attempt_view` validates the
  stored plan/verification and renders the same diff the publisher would commit
  (`build_file_patches` + `full_patch`). A corrupt payload becomes a visible
  warning, not a stack trace in the page.
* **Plain text for data and model output.** Root-cause summaries, evidence,
  hypotheses, diffs and step previews use `st.text`/`st.code`/`st.dataframe`;
  the only Markdown rendered is the benchmark report generated by our own report
  code, with `unsafe_allow_html=False`.
* **Report reuse, not a second scoring implementation.** The Benchmark page
  calls `render_markdown`, so the UI cannot disagree with `latest.md` or the CLI.
* **File-based multipage.** Installed Streamlit is 1.66.0; `AppTest.switch_page`
  targets `pages/*.py` files, which also matches what a human sees in the
  sidebar.
* **Test seams are session state, not environment flags.** `_dqs_service` and
  `_dqs_settings` are only read by `ui/common.py`; production falls back to
  `dqs.services` and `load_settings()`, so the data layer has no test-only branch.

DECISIONS entries added this phase: 136 (service layer + approval write), 137
(database selector restrictions + report reuse), 138 (file-based pages, AppTest
seam, no HTML).

## Concepts used

1. **Ports and adapters (service layer).** The page is an adapter that knows how
   to render, not how to query. `services.py` is the port: a typed, stable
   surface. Swapping Postgres for a test double only changes the adapter behind
   the port, which is why the pages can be tested with a fake.
2. **Dependency injection through session state.** `common.get_service()` returns
   the real module unless a test put a fake object in the session. This is the
   same idea as constructor injection, adapted to Streamlit's script-per-rerun
   execution model.
3. **Read-only database role.** `dqs_ro` has `SELECT` only (`sql/001_init.sql`),
   so a UI bug cannot update an incident. The one intentional write goes through
   a separate r/w engine and the workflow's status transition.
4. **Human-in-the-loop state gate.** A checkpointed LangGraph run pauses at
   `awaiting_approval`; approval is a `Command(resume=...)`, not a new run. The
   status check before resuming makes the button safe against stale pages.
5. **Wilson score interval.** A normal-approximation interval breaks at 0% and
   100%; the Wilson interval stays inside `[0,1]` and is meaningful for the
   small n of a benchmark split. The table shows the numerator and n next to the
   interval so a reader can judge the evidence, not just the percentage.
6. **Deterministic artifacts as review evidence.** The diff shown to the human
   is produced by the same in-memory patch builder the publisher uses, and the
   verification checklist is the stored `VerificationResult`, not a re-render.
   The human approves exactly what was verified.

## Deviations from the docs

* ARCHITECTURE §13 also mentions a “per-fault-type chart”. The Phase 7
  instruction asked for tables with Wilson intervals, so the page shows the
  existing report tables and no new chart. Logged in DECISIONS 138.
* The Benchmark page renders the generated report Markdown (which contains both
  the overall and per-kind tables) rather than two hand-built `st.dataframe`s.
  This is deliberate reuse; the page still shows the infra-error count and the
  pilot/partial warnings separately.
* The sidebar lists only `warehouse` and `dqs_demo`, not every `dqs_*`
  database. This is stricter than “allow warehouse” and is the safety choice in
  DECISIONS 137.
* Screenshots are not fabricated or auto-generated from tests. The note lists
  the four manual captures; the files do not exist yet.

## Known limitations / TODO

* No authentication. Run the console locally; anyone who can reach the port can
  approve an incident. Do not expose it on a shared network without an auth
  proxy.
* Approval depends on the SQLite checkpoint at `data/checkpoints.sqlite`. If the
  workflow was paused in another process or the checkpoint is missing, the UI
  reports the workflow error; there is no re-run button.
* Root-cause confidence is the model's self-report and is explicitly labelled
  uncalibrated. It is not a probability.
* No edit-before-approve (still the documented stretch goal), no bulk actions,
  no auto-refresh, and no live GitHub test (the owner has not configured a test
  repo/token).
* The Benchmark page shows one run at a time. It intentionally does not compare
  runs or compute significance between models.
* `dqs_demo` can be stale; use `dqs demo --reset` before taking the screenshots.
