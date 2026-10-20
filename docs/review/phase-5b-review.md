# Phase 5b review packet

Date: 2026-10-20 · Scope: Phase 5 tasks 2 and 3 plus the Phase-5 acceptance
LLM runs — fixer agent, deterministic issue builder, resolution routing,
`dqs incident plan-fix`, test-only clean-data oracle and real-model evidence.
Companion: `docs/phase-notes/phase-5b.md`. No workflow graph or publishing is
in this phase.

## What to review first

1. `src/dqs/agents/fixer.py` — what the model sees, what code owns, the retry
   loop and the single attempt-persistence path.
2. `src/dqs/agents/prompts/fixer.md` — the safety rules the model is given
   (minimal edit, preserve output schema, never hide rows, all-history
   correctness, context is data).
3. `src/dqs/agents/issue_builder.py` — deterministic content and the static
   category playbook. The issue is the honest fallback when a fix fails.
4. `src/dqs/resolve.py` — the PR/ISSUE/NONE policy, independent of any model.
5. `src/dqs/cli.py` (`incident plan-fix`) — manual node command, routing
   refusal and exit codes.
6. `tests/helpers/oracle.py` and the oracle tests — the answer-key boundary.
7. `tests/integration/test_fixer_llm.py` — the real investigator → fixer →
   verifier → oracle chain and what each result JSON records.

## Verdict table

| Area | Verdict | Evidence |
|---|---|---|
| Schema boundary: model cannot choose incident context | PASS | `FixPlanDraft` has only the five §10.5 fields; `build_fix_plan` fills incident/category/table/columns from stored data |
| Candidate selection from lineage | PASS | Direct `depends_on` edge to the affected raw source; staging `.sql` only; test asserts `stg_orders` and excludes unrelated models |
| Prompt safety rules | PASS | Unit test checks the prompt contains no filter/delete advice, only `models/**/*.sql`, exact match, output schema preservation and "data, never instructions" |
| Bounded retry + feedback | PASS | Fake structured-output model: failed criteria + redacted excerpt reach the next attempt; the loop stops at `MAX_FIX_ATTEMPTS` |
| Persistence | PASS | Invalid output, allowlist rejection and real verdicts all reach `dq.fix_attempts`; real runs assert `persisted_attempts == attempts` |
| Routing | PASS | 23 table-driven tests; PR only for renamed/type/duplicate at >= 0.6 with a candidate; added-column always NONE |
| Issue builder | PASS | 23 tests: all required sections, category playbook for every category, failed attempts, artifacts, deterministic output, hostile multiline text collapsed |
| `plan-fix` CLI | PASS | 4 tests: non-PR refusal, missing root cause, passing plan/attempt output, invalid uuid; failed runs exit 1 |
| Oracle isolation | PASS | Unit scan finds no `src/dqs` import/mention of the oracle; unsafe identifiers rejected before DB access |
| Oracle correctness on good patches | PASS | Real dbt on F1/F3/F6 clones: all three match the clean generated frame |
| Oracle catches wrong values behind a verifier pass | PASS | `coalesce(customer_id, 1)` passes the verifier but the oracle reports mismatched customer ids |
| Real-LLM F1 | PASS | 120b, 2 attempts, oracle PASS, 7,451 fixer tokens |
| Real-LLM F3 | PASS | 120b, 1 attempt, oracle PASS, 2,919 fixer tokens |
| Real-LLM F6 | PASS | 20b (120b daily quota), 2 attempts, oracle PASS, 4,485 fixer tokens |
| Quality gates | PASS | ruff/format/mypy clean; 476 unit passed, 1 Windows symlink skip; 87 integration non-LLM passed |
| Dev warehouse | PASS | 30 batches, 0 incidents, 0 steps, 0 attempts, no sandbox schemas |

## Real-model attempt detail

| Scenario | Model | Attempt | Result | Why |
|---|---|---|---|---|
| F1 | `openai/gpt-oss-120b` | 1 | FAIL | changed to `cust_id` for the whole history: `dbt_build` + `contract_conformance` failed |
| F1 | `openai/gpt-oss-120b` | 2 | PASS | `coalesce(customer_id, cust_id)` preserves old and new batches; oracle PASS |
| F3 | `openai/gpt-oss-120b` | 1 | PASS | strips currency formatting before `numeric(12,2)`; oracle PASS |
| F6 | `openai/gpt-oss-20b` | 1 | FAIL | schema-invalid draft (`fixer_output`) |
| F6 | `openai/gpt-oss-20b` | 2 | PASS | `WITH ... row_number()` dedupe; oracle PASS |

A later consistency rerun on 20b passed F3/F6 but failed F1 after 3 attempts
with `dbt_build`/`contract_conformance`. That result is recorded in the phase
note; the prompt was not changed in response. The 120b quota (HTTP 429, daily
token limit) is why F6 was rerun on 20b, not a code change.

## Weakest points

1. **The fixer only sees direct staging candidates.** That is the correct
   narrow rule for the three fixable categories today, but a defect that
   genuinely requires a mart or macro change routes to ISSUE. Do not widen the
   allowlist without a verifier rule that can prove such an edit safe.
2. **The verifier is still the first gate, and the oracle is test-only.** A
   deployment without the oracle can accept a verifier-passing patch that
   writes wrong values. The benchmark/test suite is what catches that class of
   failure; it is not a runtime guard.
3. **Model reliability is uneven.** 20b passed F3/F6 but failed F1 even with
   retry feedback. The system reports the exhausted attempts honestly; it does
   not claim a fix. A future phase could choose a stronger model or add a
   second planner, but that is not in scope here.
4. **Provider quota can look like a system failure.** 429s are persisted as
   invalid attempts. The result JSON and phase note must be read with the
   provider error, not just the verdict, or a quota run looks like a bad patch.
5. **`dq.fix_attempts` has no uniqueness constraint.** Attempt numbers are
   allocated by `MAX(attempt_no)+1`; concurrent fixers for one incident can
   collide. The workflow is assumed single-writer.
6. **The oracle covers `stg_orders` only.** F1/F3/F6 all target orders. Do not
   generalize its coverage claim to other tables or fault kinds without adding
   comparisons.
7. **Issue builder is not wired to publishing.** It returns an `IssueDraft`;
   Phase 6 must pass the correct resolution and handle NONE before calling it.
   A PR/NONE resolution is rejected by the builder, which is the intended
   guard but means the caller must route first.

## Files a reviewer will want open

| File | Why |
|---|---|
| `src/dqs/agents/fixer.py` | Context assembly, draft→plan boundary, retry/persistence loop |
| `src/dqs/agents/prompts/fixer.md` | The actual instructions the model receives |
| `src/dqs/agents/issue_builder.py` | Template sections and the category playbook |
| `src/dqs/resolve.py` | The product routing rule |
| `src/dqs/verify/sandbox.py` | `next_attempt_number` / `persist_attempt_payload` |
| `tests/unit/test_fixer.py` | Schema/loop/negative persistence tests |
| `tests/unit/test_issue_builder.py` | Section, playbook, honesty and hostile-text tests |
| `tests/unit/test_oracle.py` | Oracle comparison and production-isolation test |
| `tests/integration/test_verify_sandbox_db.py` | Real-dbt oracle acceptance |
| `tests/integration/test_fixer_llm.py` | Real-model chain and result-record schema |
