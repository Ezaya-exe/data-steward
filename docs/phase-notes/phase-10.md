# Phase 10 — Polish: README, claims, CI, honest status

Date: 2026-10-27 · Scope: `docs/PHASES.md` Phase 10, with the owner's
instructions (README content, `docs/CLAIMS.md`, CI, dead-code cleanup, no
fingerprint changes, no benchmark/demo/LLM/GitHub calls). Phase 9 (optional
Chroma/Langfuse) was not requested and was not started.

## What was built

- `README.md` — problem statement in plain language, Mermaid architecture
  diagram, an eight-command quickstart, the design principle with a
  who-does-what table, a `TBD`-only benchmark section, Limitations (the nine
  requested items), screenshot placeholders, repository map, honest status.
- `docs/CLAIMS.md` — 18 rows mapping a resume-safe claim to evidence and a
  reproduce command; benchmark-dependent rows are `PENDING`; live GitHub,
  production scale and real-customer data are explicitly "DO NOT CLAIM".
- `.github/workflows/ci.yml` — Ubuntu + Python 3.12, pip install `.[dev]`,
  ruff check, ruff format check, mypy, unit tests. No services, no secrets,
  `permissions: contents: read`, job timeout.
- `src/dqs/bench/preflight.py` — the "harness frozen" line now prints the full
  `code_fingerprint()` (`fp=…`), so an unchanged benchmark fingerprint is
  visible from `dqs bench preflight`. `tests/unit/test_bench_preflight.py`
  asserts the field.
- Dead code: removed `dbt_run.last_runs` and `bench.runner.ScenarioFailure`
  (zero references; outside the fingerprint). Three other unused helpers
  (`tools.registry.registry_names`, `verify.patch.validate_plan`,
  `verify.patch.ignored_at_top_level`) were restored after they changed
  `code_fingerprint()`; removing them would have invalidated prior benchmark
  provenance. DECISIONS 143.
- `docs/DECISIONS.md` 140–144; `docs/HANDOFF.md` current state, test counts,
  file map and limitations updated.

## How to run it

```powershell
# README quickstart (8 commands): clone, venv, install, .env, compose up,
# db init, sim seed --days 30  (no LLM key needed)
.venv\Scripts\dqs dev lint
.venv\Scripts\dqs dev test
.venv\Scripts\dqs dev test-int
dqs bench preflight          # prints the code fingerprint
```

CI runs the same lint/mypy/unit commands on a pull request; integration tests
stay local because they need Docker Postgres + dbt.

## Acceptance results

| Criterion | Result | Evidence |
|---|---|---|
| Fresh clone → README quickstart works start to finish | PASS | Cloned the local repo at `49c8309` into `%TEMP%\opencode\dqs-clean` (no remote configured). All eight commands exited 0. `dqs db ping` → PostgreSQL 16.15 for `rw` and `ro`; seed ended `ok baseline seeded`; `dq.batches` = 30 rows, max `business_date` 2026-01-30. |
| Quickstart environment is usable | PASS | In that fresh clone: `dqs dev lint` → all checks passed, 210 files formatted, mypy clean (102 files); unit suite → 705 passed, 1 skipped. |
| `make check` equivalent green | PASS | Main checkout: `dqs dev lint` → ruff clean, 211 files formatted, mypy clean (102 files); unit → 705 passed, 1 skipped, 106 deselected; integration → 100 passed, 6 skipped, 706 deselected (8:19). `make` is not installed; these are its `dqs dev` equivalents. |
| README benchmark numbers are real or `TBD` | PASS | Every metric row is `TBD`; the section names `benchmarks/results/<file>` as the only source and forbids pilot/fake/dirty fills. No benchmark run was made. |
| `docs/CLAIMS.md` complete | PASS | 18 rows, each with evidence and a reproduce command; rows 14–16 are `PENDING: needs final results file`; rows 17–18 are do-not-claim. |
| CI workflow exists and matches local commands | PASS (not executed on GitHub) | `.github/workflows/ci.yml` parses (7 steps); the same ruff/mypy/pytest commands pass locally. This checkout has no remote, so no Actions run exists. |
| Benchmark code fingerprint unchanged | PASS | `dqs bench preflight` before: `fp=17ac403b512ffd73d0b07df91047d55df846aa0e72c1c787d35315b91f1c6a26`; after: same full value, all checks PASS. |
| Dead code removed only when clearly unused | PASS with a constraint | 2 zero-reference helpers removed outside fingerprint inputs; 3 inside them restored (DECISIONS 143). |
| No benchmark / demo / real LLM / GitHub call | PASS | Only `bench preflight` (read-only), local DB/integration tests, and a deterministic seed were run. |

## Design choices and why

- **The README treats documentation as a claim ledger.** Any performance number
  is a liability if it cannot be traced. The benchmark table is therefore
  `TBD`-only and points at the results file; CLAIMS.md gives each non-benchmark
  claim a command or file.
- **The quickstart is eight commands, not PHASES' six.** `clone` and `cd` are
  separate so the block can be pasted literally; the six substantive steps are
  venv, install, `.env`, compose, `db init`, `seed`. No LLM key is required.
- **CI is deliberately narrow.** Lint/mypy/unit need no services or secrets and
  catch most defects in minutes. Integration and real-LLM tests need Docker, a
  seeded template, quota and keys; making them required in CI would either leak
  secrets or produce flaky red builds.
- **A fingerprint is provenance, not a version label.** Removing unused
  functions from `tools/`/`verify/` changed the hash even though behaviour did
  not. Restoring them keeps prior benchmark files comparable; cleanup does not
  get to rewrite history silently.
- **The local 2026-10-26 dev results file is not evidence.** It has
  `git_dirty=true` and `catalogue_size=4` metadata above an n=43 table. README
  and CLAIMS stay `TBD`/`PENDING` rather than quote a file that cannot be
  reproduced byte-for-byte.

DECISIONS entries added: 140 (preflight fingerprint), 141 (eight-command
quickstart + no invented numbers), 142 (CI scope), 143 (dead-code/fingerprint
constraint), 144 (why the existing dev results file is not quoted).

## Concepts used

1. **Clean-room acceptance.** A clone in a temporary directory has no `.env`,
  no `.venv`, no cached database volume and no build artifacts. Running the
  documented commands there is the only honest test of "a new user can start".
2. **Claim → evidence → reproduce.** A resume claim is a hypothesis; a test
  path plus a command is its falsification procedure. Keeping the three in one
  table prevents a number from drifting away from its source.
3. **Content fingerprints for benchmark provenance.** Hashing the source paths
  and bytes that can change an answer makes a result file reproducible in
  spirit: if the hash matches, the code that produced it is unchanged. This is
  stronger than a human-maintained version string.
4. **Fail-fast CI gates.** Lint and unit tests are cheap and deterministic;
  running them on every push catches style, typing and logic regressions before
  expensive Docker/LLM work. Integration stays a local gate because its
  dependencies are not portable to a secret-free hosted runner.
5. **Mermaid as executable-ish documentation.** The diagram names the same
  modules the code map names; a box that no longer exists is easy to spot in
  review, unlike a prose paragraph.
6. **Statistical honesty at small n.** Wilson intervals are reported with the
  exact numerator and denominator; the README does not turn a four-scenario
  pilot into a percentage headline.

## Deviations from the docs

- PHASES Phase 10 says a quickstart of at most six commands; the owner asked for
  at most eight, and eight are used (DECISIONS 141).
- PHASES says "demo GIF/video placeholder"; the README uses static screenshot
  placeholders plus `docs/img/README.md`, because no recording exists and none
  can be generated without a real LLM run.
- The Phase 9 optional memory/tracing work is not built; the README says so.

## Known limitations / TODO

- Screenshots are still missing (`docs/img/01…05`); the README marks them `TBD`.
- The CI workflow has never run on GitHub (no remote in this checkout).
- No clean final benchmark exists. The only local full-dev file is dirty and
  has an inconsistent `catalogue_size` label; do not quote it.
- Live GitHub publishing is not network-tested.
- The quickstart resolves `pyproject.toml` ranges, not `requirements.lock`;
  the clean clone picked slightly newer minors and still passed lint/unit tests.
- Root-cause confidence is model-reported and uncalibrated; the UI labels it as
  such.
- Verification rebuilds the whole staging/marts layer per attempt; there is no
  concurrency control for two verifications of the same incident.
