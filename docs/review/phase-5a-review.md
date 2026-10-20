# Phase 5a review packet

Date: 2026-10-20 · Scope: Phase 5 tasks 1 and 4 — deterministic `FixPlan`,
hardened patch application, isolated dbt sandbox, pass criteria, persistence and
the `dqs verify` CLI. Companion: `docs/phase-notes/phase-5a.md`. No LLM code is
in this phase.

## What to review first

1. `src/dqs/verify/patch.py` — the allowlist and exact-match application. This
   is the file a malicious or mistaken plan hits first.
2. `src/dqs/verify/criteria.py` — contract conformance, row preservation and
   the three targeted checks. This is what decides pass/fail.
3. `src/dqs/verify/sandbox.py` — temp copy, child environment, timeout,
   always-drop cleanup and persistence.
4. `src/dqs/models/fix.py` — the limits and the extra incident-context fields.
5. `src/dqs/cli.py` — `verify <plan.json>` / `verify cleanup` and exit codes.
6. `tests/integration/test_verify_sandbox_db.py` — the real-dbt evidence behind
   every row below.

## Patch-by-patch verdicts

Every row below is asserted by a test; the "reason" column is the criterion or
error text the test checks.

### Known-good hand-written patches

| Patch | What it changes | Verdict | Why it passes |
|---|---|---|---|
| F1 rename | `stg_orders.customer_id` → `coalesce(customer_id::integer, cust_id::integer) as customer_id` | **PASS** | dbt build/tests pass; contract schema/type/null/unique/allowed values pass; row count preserved; no NULL `customer_id` in `batch_20260131` |
| F3 type change | `stg_orders.total_amount` → `regexp_replace(total_amount, '[^0-9.-]', '', 'g')::numeric(12,2)` | **PASS** | build/tests pass; numeric type and allowed/null/unique rules pass; row count preserved; batch values parse/fall inside the raw contract range |
| F6 duplicates | `stg_orders` wraps raw in `row_number() over (partition by order_id order by _loaded_at desc, _batch_id desc)` and keeps `_rn = 1` | **PASS** | build/tests pass; row count ≥ distinct raw PKs; staging `order_id` is unique with no NULL keys |

All three pass with `fix_attempts = (attempt_no=1, passed=True)` and leave no
`dqs_sandbox_*` schema.

### Known-bad and adversarial patches

| Bad patch / plan | Test | Verdict | Failure reason the test asserts |
|---|---|---|---|
| `where false` on `stg_orders` | delete-rows | **FAIL** | `row_preservation`: `0 staging rows ... minimum is 95%` (targeted F3 also has no batch rows) |
| Rename output `customer_id` → `customer_key` | renamed-column | **FAIL** | `contract_conformance`: missing `customer_id`, unexpected `customer_key`; the contract criterion is asserted |
| `select` → `seleckt` | syntax-error | **FAIL** | `dbt_build` non-zero exit; `dbt_log_excerpt` contains the compiler error; no staging table was produced |
| Keep the cast but filter currency rows: `where total_amount ~ '^[0-9]+([.][0-9]+)?$'` | plain-filter | **FAIL** | `targeted_check`: the faulted batch has no staging rows. The test asserts the targeted reason; `row_preservation` is not used as the justification |
| Edit `models/staging/schema.yml` | parameterized bad-path | **FAIL** | `allowlist`: only `.sql` model files may be edited |
| Edit `models/../macros/x.sql` | parameterized bad-path | **FAIL** | `allowlist`: path traversal is not allowed |
| Edit `macros/x.sql` | parameterized bad-path | **FAIL** | `allowlist`: path must be under `warehouse/dbt_project/models/` |
| Edit unrelated `stg_products` for a `raw.orders` incident | parameterized bad-path | **FAIL** | `relevance`: the model does not directly select from the affected raw table |
| `old` not present (`NO_SUCH_TEXT`) | parameterized bad-path | **FAIL** | `exact_match`: `found 0` |
| `old = order_id` (multiple occurrences) | parameterized bad-path | **FAIL** | `exact_match`: `found N` (must be exactly once) |
| Replacement text 5000 bytes | oversized | **FAIL (at model construction)** | Pydantic `ValidationError`: replacement text exceeds the 4096-byte plan limit; dbt never runs |
| Absolute / drive / UNC / NUL / ADS / reserved device path | unit allowlist | **FAIL (at validation)** | `FixPlanError` with the specific allowlist reason; no file copy |
| Real symlink / Windows junction component | unit allowlist | **FAIL (at validation)** | `FixPlanError`: symlinks are not allowed in edit paths. The real symlink test is skipped on this Windows account; a real junction and a monkeypatched `Path.is_symlink` test cover the branch |
| Run killed after creating its schema | killed-run | **FAIL** | `dbt_build` timeout; `sandbox_cleanup` / final assertion finds no `dqs_sandbox_*` schema |
| Plan missing required fields | CLI invalid-plan | **FAIL** | `dqs verify` exits non-zero with `invalid fix plan` |

### Isolation and safety checks (not patches)

| Check | Result |
|---|---|
| Real `warehouse/dbt_project/target/manifest.json` SHA-256 before/after | unchanged |
| Dev `staging.*` and `marts.*` row counts before/after | unchanged |
| dbt child env in a polluted parent (`LLM_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`) | none of those values present; PG* built from `Settings` |
| dbt child invocation | `shell=False`, explicit timeout, captured stdout/stderr |
| `dq.fix_attempts` | one passing row for the verified incident |
| `dqs verify cleanup` | drops a seeded stale `dqs_sandbox_*` schema |
| Dev warehouse after the full integration run | 30 batches, 0 incidents, 0 agent steps, 0 fix attempts, no sandbox schemas, no stray `dqs_%` databases |

## Weakest points

1. **Verification trusts the plan's incident context.** `FixPlan` carries
   `incident_id`, `batch_id`, `category`, `affected_table` and
   `affected_columns`, and the verifier uses them directly. A hand-written plan
   can label a different batch or table than the persisted incident. Phase 5b
   must construct these fields from the stored `RootCause`/incident, and a
   future check should cross-validate `(incident_id, batch_id, category,
   affected_table)` against `dq.incidents`/`dq.root_cause` before verifying.
2. **The sandbox builds every staging model and all marts.** `staging.*+` is
   deliberate (sibling refs and relationship tests need it), but it is broader
   and slower than the §11 selector, and an unrelated pre-existing broken model
   can fail a fix that would be correct in isolation. Phase 5b should decide
   whether to keep this strict "whole warehouse still builds" gate or scope the
   build while adding the missing sibling models explicitly.
3. **Only the changed staging table is contract-checked.** Row preservation and
   the targeted check are staging-level too. A patch could preserve staging
   rows and satisfy its contract while changing the meaning of a downstream
   mart (for example by altering a join key). Phase 5b/5c should add published
   mart-contract conformance for downstream models selected by the build.

Secondary risks: sandbox schema names are incident-derived, so concurrent
verifications of the same incident collide; `dq.fix_attempts` has no unique
constraint on `(incident_id, attempt_no)`; the real symlink test is skipped on
this Windows account; exact-match strings are byte-sensitive (LF project).

## Risks and questions for 5b (fixer agent)

* The fixer must produce a plan whose `old` strings are copied from the current
  model bytes, not retyped from a prompt summary. A mismatch now fails
  `exact_match`; the retry feedback needs the precise `found N` message.
* Retry loop must carry `failed_criteria` plus the redacted dbt excerpt; the
  `VerificationResult` already exposes both. Decide the retry budget and whether
  a failed attempt is persisted before the next plan (recommended: yes, one row
  per attempt).
* The fixer must not be allowed to choose incident context freely. Supply it
  from the stored root cause and validate the returned plan's fields.
* A fix that legitimately needs more than 3 edits or >4 KB is out of scope for
  v1. The prompt should say so, and the model validation will reject it.
* `staging.*+` means verification is a whole-warehouse build. Budget for the
  runtime and surface it to the fixer/UI; do not silently shorten the timeout.
* The fixer's prompt should include the published staging contract and the
  current SQL, as §10.5 says. It must never see the verification source code or
  a "make this pass" hint that bypasses row preservation.
