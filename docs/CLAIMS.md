# CLAIMS — what can honestly go on a resume

One row per claim. **Evidence** is a file, command, or results path that is in
this repository (or explicitly marked local). **Reproduce** is the command a
reviewer can run. "Verified" means it was run on 2026-10-27 on this checkout;
"PENDING" means it must not be claimed yet.

Rules:

- Do not quote a benchmark rate, cost, latency, or token count until it is copied
  from a final `benchmarks/results/*.json` file with a recorded model, date, and
  clean worktree. The README benchmark table is `TBD` for that reason.
- A pilot, a fake-provider wiring test, or a dirty-worktree run is not benchmark
  evidence (AGENTS.md §7.9).
- Live GitHub publishing is **not** claimed: the PyGithub adapter has not been
  exercised against a real repository/token.

## Claims

| # | Claim (resume-safe wording) | Evidence | Reproduce | Status |
|---:|---|---|---|---|
| 1 | Built an end-to-end data-quality steward for Postgres + dbt: contract generation, deterministic checks, incident de-duplication, root-cause investigation, lineage/impact, fix planning, sandbox verification, human approval, and dry-run publishing. | `src/dqs/`; `docs/ARCHITECTURE.md`; `docs/phase-notes/phase-2a.md` … `phase-7.md` | `dqs dev test`; `dqs dev test-int` (Docker) | Verified: 705 unit passed / 1 skipped; 100 integration passed / 6 skipped |
| 2 | Auto-generated 13 approved data contracts from a clean 30-day baseline, with byte-identical regeneration (status normalized). | `contracts/*.yml`; `src/dqs/contracts/generator.py`; `tests/unit/test_contracts_generator.py`; `tests/integration/test_contracts_e2e.py` | `dqs contracts generate`; `pytest tests/unit/test_contracts_generator.py tests/integration/test_contracts_e2e.py -q` | Verified (contracts are `approved`; determinism test passes) |
| 3 | Modelled 10 fault kinds and a deterministic benchmark catalogue of 53 fault scenarios + 6 clean controls, split dev/holdout, with the answer key produced by the real injector. | `src/dqs/sim/faults.py`; `src/dqs/bench/scenarios.py`; `tests/unit/test_faults.py`; `tests/unit/test_bench_scenarios.py`; DECISIONS 27, 122 | `pytest tests/unit/test_faults.py tests/unit/test_bench_scenarios.py -q` | Verified (catalogue structural tests pass) |
| 4 | Built a read-only SQL tool enforced by a parser allowlist, read-only role/transaction, hard `LIMIT` and statement timeout; negative tests cover DML/DDL, catalog access, multiple statements, and admin functions. | `src/dqs/tools/sql_guard.py`; `src/dqs/tools/readonly_sql.py`; `tests/unit/test_sql_guard.py`; `tests/integration/test_sql_guard_db.py`; DECISIONS 66, 71 | `pytest tests/unit/test_sql_guard.py -q`; integration needs Docker | Verified |
| 5 | Built a bounded investigator (max tool calls, tokens, clock) that returns a schema-validated `RootCause`, filters evidence to tools that actually ran, and salvages provider tool-call failures. | `src/dqs/agents/investigator.py`; `src/dqs/models/root_cause.py`; `tests/unit/test_investigator.py`; DECISIONS 74–78, 126 | `pytest tests/unit/test_investigator.py -q` | Verified (unit; real-model path is a 3-scenario smoke, row 12) |
| 6 | Built deterministic lineage/impact from the real dbt manifest: upstream/downstream traversal, column-scoped impact, exposure maturity weighting, and a scored blast radius. | `src/dqs/lineage/`; `tests/unit/test_lineage.py`; `tests/unit/test_columns.py`; `tests/unit/test_impact.py`; `tests/unit/test_manifest.py`; DECISIONS 55–61 | `pytest tests/unit/test_lineage.py tests/unit/test_columns.py tests/unit/test_impact.py tests/unit/test_manifest.py -q` | Verified (real-manifest fixture) |
| 7 | Built deterministic fix verification: edit allowlist, exact-match patch application, isolated dbt build, published-contract conformance, ≥95% row preservation, and a category-specific check. A data-deleting "fix" fails. | `src/dqs/verify/`; `src/dqs/models/fix.py`; `tests/unit/test_verify_patch.py`; `tests/unit/test_verify_criteria.py`; `tests/integration/test_verify_sandbox_db.py`; DECISIONS 80–89 | `pytest tests/unit/test_verify_patch.py tests/unit/test_verify_criteria.py -q`; `pytest tests/integration/test_verify_sandbox_db.py -m integration -q` | Verified (unit; real dbt integration passes) |
| 8 | Built a human approval gate with LangGraph `interrupt`/`Command(resume=...)`, a typed approval decision, crash-safe pending/done publication rows, and a default `dry_run` mode that never writes to GitHub. | `src/dqs/workflow/`; `src/dqs/publish/`; `tests/unit/test_workflow.py`; `tests/unit/test_publish_base.py`; `tests/integration/test_publish_db.py`; DECISIONS 100–119, 128 | `pytest tests/unit/test_workflow.py tests/unit/test_publish_base.py -q` | Verified (fake GitHub client + dry-run integration; live mode not claimed) |
| 9 | Built a four-page Streamlit review console that reads through a typed service layer, uses the read-only DB role, and only writes through approve/reject; 43 unit tests (30 service + 13 AppTest page tests). | `ui/`; `src/dqs/services.py`; `tests/unit/test_services.py`; `tests/unit/test_ui_pages.py`; `tests/integration/test_ui_services_db.py`; DECISIONS 136–138 | `pytest tests/unit/test_services.py tests/unit/test_ui_pages.py -q` | Verified (43 collected/passed) |
| 10 | Built benchmark provenance and reporting: code fingerprint over agents/tools/verify/workflow/checks/contracts/prompts, preflight PASS/FAIL screen, per-UTC-day token ledger, Wilson intervals, infra-error exclusion, circuit breaker, `--repeats`, resume. | `src/dqs/bench/{preflight,harness,ledger,scoring,report,runner}.py`; `tests/unit/test_bench_*.py`; `docs/BENCH_RUNBOOK.md`; DECISIONS 129–135 | `dqs bench preflight`; `pytest tests/unit/test_bench_preflight.py tests/unit/test_bench_ledger.py tests/unit/test_bench_report.py tests/unit/test_bench_scoring.py tests/unit/test_bench_runner.py -q` | Verified (preflight all PASS; no benchmark run) |
| 11 | Verified the README quickstart (eight commands) on a clean clone in a temporary directory on Windows. | `README.md` → "Clean-clone verification"; this checkout has no git remote, so the clone was made from the local repository | Follow the eight commands in `README.md` | Verified 2026-10-27 |
| 12 | Ran a real-model smoke of the full chain (investigator → fixer → verifier → clean-data oracle) on 3 scenarios, 1 run each; recorded, not tuned. **Not benchmark-grade** (no repeats/variance). | `docs/phase-notes/phase-5b.md`; local, git-ignored `out/phase-5b-llm-*.json`; model `openai/gpt-oss-120b`, 2026-10-20 | `RUN_LLM_TESTS=1 pytest tests/integration/test_fixer_llm.py -m llm -q` (needs a key/quota) | Local evidence only — do not quote as a benchmark |
| 13 | Added a GitHub Actions CI workflow that runs lint, mypy, and unit tests only, with no secrets and no Docker services. | `.github/workflows/ci.yml`; this table's row 1 commands | Push the repo and open a pull request; the workflow runs the same commands | Local commands pass; **workflow not yet executed on GitHub** (no remote here) |
| 14 | Benchmark detection accuracy, root-cause accuracy, resolution accuracy, fix validity, semantic correctness, verified-but-wrong rate, and false-positive rate. | Final `benchmarks/results/*.json` and `benchmarks/results/latest.md` | `dqs bench run --split dev` then `dqs bench report` | **PENDING: needs final results file** |
| 15 | Benchmark cost, token use, and wall-clock time per scenario. | Final `benchmarks/results/*.json` (stored tokens are re-priced by `dqs bench report`) | Same as row 14 | **PENDING: needs final results file** |
| 16 | Holdout/generalization numbers. | A final holdout results file (runs require `--confirm-holdout`; never tune on it) | `dqs bench run --split holdout --confirm-holdout` | **PENDING: needs final results file** |
| 17 | "Opened real pull requests / issues on GitHub from the workflow." | — | — | **DO NOT CLAIM.** Only dry-run previews and a fake GitHub client are tested here; live mode was never configured against a real repo |
| 18 | "Production-scale / real-customer data." | — | — | **DO NOT CLAIM.** The data is synthetic and the checks are demo-scale (see README Limitations) |

## Evidence index

- Unit suite (no Docker/LLM): `dqs dev test` → 705 passed, 1 skipped, 106 deselected
  (2026-10-27; the skip is real OS symlink creation on this Windows account).
- Integration suite (Docker Postgres + dbt): `dqs dev test-int` → 100 passed,
  6 skipped (the LLM-gated suites), 2026-10-27.
- Lint/type gate: `dqs dev lint` → ruff check clean, ruff format clean, mypy
  clean (102 source files).
- Benchmark readiness: `dqs bench preflight` → all PASS, code fingerprint
  `17ac403b512ffd73…` (printed by the command).
- Architecture and decisions: `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`.
- Phase reports: `docs/phase-notes/phase-*.md`.
- Benchmark runbook: `docs/BENCH_RUNBOOK.md`.
