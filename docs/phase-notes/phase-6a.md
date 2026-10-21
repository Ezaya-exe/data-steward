# Phase 6a — Publish package: dry-run preview, live GitHub path, approval gate

Date: 2026-10-21 · Scope: Phase 6 task 3 only (`src/dqs/publish/` and
`dqs publish preview`). The LangGraph workflow, interrupt/resume approval and
`dqs demo` are **not** part of this phase and are not started; they remain
Phase 6b. Companion review packet: `docs/review/phase-6a-review.md`.

## What was built

* `src/dqs/errors.py` — `PublicationError` for refused, misconfigured or failed
  publications.
* `src/dqs/publish/base.py` — `ApprovalDecision` (approver, timezone-aware
  timestamp, `decision="approve"`), `PublicationKind`, `PublicationMode`,
  `PublicationResult`, and the `Publisher` base class. The base class enforces
  the approval gate, refuses a failed verification, rejects a plan from another
  incident, and returns an existing `dq.publications` row before any I/O.
* `src/dqs/publish/store.py` — `PublicationStore` protocol,
  `DbPublicationStore` (real `dq.publications` + `dq.fix_attempts` read),
  `MemoryPublicationStore` (unit-test double), and `load_latest_fix_attempt`.
* `src/dqs/publish/diff.py` — in-memory replay of a `FixPlan` against the real
  model files using the Phase-5a allowlist/exact-match helpers; `FilePatch` plus
  `difflib` unified diff. The project is read, never written.
* `src/dqs/publish/sanitize.py` — deterministic redaction and neutralization of
  untrusted text (secrets, mentions, issue refs, HTML, Markdown links, size
  caps, unclosable fences).
* `src/dqs/publish/body.py` — deterministic PR body with every §12 section and
  the fixed review footer; the same renderer feeds preview and live mode.
* `src/dqs/publish/dry_run.py` — `DryRunPublisher` and `run_dry_run`: write
  `out/pr_previews/<short>.md` + `.patch` (and `out/issue_previews/` for
  issues), record `mode=dry_run`, return a `file://` URL, no client call.
* `src/dqs/publish/github.py` — narrow `GitHubClient` protocol, PyGithub 2.10.0
  adapter, `GitHubPublisher` (validated `dqs/incident-<8 hex>` branch, commits
  only on that branch, one PR, `data-quality` label, issues), and
  `build_publisher` which fails closed in live mode.
* `src/dqs/cli.py` — `dqs publish preview <incident_id>`: dry-run only, from a
  persisted incident + root cause + impact + latest fix attempt.
* Tests: `tests/unit/test_publish_{sanitize,diff,body,base,github}.py`,
  `tests/unit/test_cli_publish_preview.py`,
  `tests/integration/test_publish_db.py`,
  `tests/integration/test_publish_cli_e2e.py`, plus
  `tests/helpers/{publish,fake_github}.py`.
* `docs/DECISIONS.md` 100–108.

## How to run it

```powershell
cd 'D:\study\python\data steward'

# quality gates
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m ruff format --check src tests
.venv\Scripts\python -m mypy src
.venv\Scripts\python -m pytest -m "not integration and not llm" -q

# publish-only acceptance (creates/drops dqs_publish_test and dqs_publish_cli)
.venv\Scripts\python -m pytest tests\integration\test_publish_db.py tests\integration\test_publish_cli_e2e.py -q

# full non-LLM integration suite
.venv\Scripts\python -m pytest -m "integration and not llm" -q

# manual preview against a CLONE that holds an investigated incident,
# a stored impact report and a passing fix attempt:
$env:PGDATABASE='dqs_accept'
.venv\Scripts\dqs publish preview <incident-uuid>
# writes out\pr_previews\<short>.md/.patch, then refuse a second write with reused=True
```

The command refuses `GITHUB_MODE=live` before opening a database connection and
never constructs a GitHub client.

## Acceptance results

| # | Criterion | Verdict | Evidence |
|---|---|---|---|
| 1 | Approval is a typed runtime gate | PASS | `test_create_pr_without_an_approval_argument_raises_type_error`, `test_create_pr_with_a_plain_dict_is_refused`, `test_approval_decision_requires_approve_and_a_timezone`; a reject/naive timestamp cannot be constructed. |
| 2 | PR refused unless verification passed | PASS | `test_create_pr_refuses_a_failed_verification` — no file, no store row. |
| 3 | Dry-run writes `.md` + `.patch`, row with `mode=dry_run`, `file://` URL | PASS | `test_create_pr_writes_once_and_returns_the_stored_row`; `test_dry_run_writes_a_preview_and_records_one_publication` against a real throwaway DB. |
| 4 | Dry-run never calls GitHub | PASS | `test_dry_run_build_never_touches_the_github_client` — fake client `calls == []`. |
| 5 | Live: branch pattern, never base, one PR, label, changed files only | PASS | `test_live_pr_uses_the_validated_incident_branch_and_never_the_base`: one `create_branch`, one `put_file` on `dqs/incident-12345678`, `head=incident branch`, `base=main`, one `create_pull`, `data-quality` ensured/applied. |
| 6 | Live idempotency (same process and a new publisher) | PASS | `test_live_pr_is_idempotent_across_a_second_run` and `test_live_publication_idempotency_survives_a_new_publisher` (DB-backed, zero extra client calls, one row). |
| 7 | Crash recovery: existing branch + PR is reused | PASS | `test_an_existing_branch_and_pr_are_reused_after_a_crash` — no branch/commit/PR call. |
| 8 | Fail closed when live config is incomplete | PASS | `test_live_mode_without_token_or_repo_fails_closed`, `test_live_mode_rejects_a_malformed_repo_slug`, `test_refuses_when_the_base_branch_is_the_incident_branch`; all before any client call. |
| 9 | Branch names validated before any API call | PASS | `test_branch_names_are_validated_before_any_call` (`main`, uppercase hex, 9 hex, path traversal all rejected). |
| 10 | Sanitization: `@octocat`, `#1`, `<script>`, links, 100 KB, `ghp_` | PASS | `tests/unit/test_publish_sanitize.py` — 8 tests; huge 100,000-char input capped at `MAX_BLOCK_CHARS` with `…[truncated]`; hostile body test also asserts the PR renderer. |
| 11 | Every PR section + review footer | PASS | `test_body_contains_every_required_section` covers all `REQUIRED_SECTIONS`; `REVIEW_FOOTER` asserted. |
| 12 | Deterministic body | PASS | `test_same_inputs_produce_identical_markdown`. |
| 13 | Exact F1 patch | PASS | `test_publish_diff.py` asserts the unified diff and `+    coalesce(customer_id::integer, cust_id::integer) as customer_id,`; sample `out/pr_previews/12345678.patch` matches. |
| 14 | Token/secret redaction inside wrapped errors | PASS | `test_github_errors_are_wrapped_with_the_token_redacted` — `ghp_` absent, `***redacted***` present, no store row; `test_settings_secrets_are_redacted_from_the_body`. |
| 15 | Issue path sanitized, labelled, idempotent | PASS | `test_live_issue_is_idempotent_and_sanitizes_hostile_markdown`; `test_create_issue_requires_approval_and_is_idempotent`; `test_create_issue_refuses_an_empty_body`. |
| 16 | `dqs publish preview` from persisted artifacts | PASS | `tests/unit/test_cli_publish_preview.py` — **5 passed**; real E2E in `test_publish_cli_e2e.py` seeds `dq.incidents`/`dq.fix_attempts`, runs the actual CLI, asserts `mode=dry_run kind=pr reused=False`, both files, one DB row, then `reused=True` with one row and unchanged bytes. |
| 17 | CLI refuses live mode before the DB | PASS | `test_preview_refuses_live_mode` makes `make_rw_engine` raise if called. |
| 18 | Lint / format / mypy | PASS | `ruff check src tests` and `ruff format --check src tests` clean; `mypy src` clean (93 source files). |
| 19 | Publish unit + integration tests | PASS | **45 publish unit tests + 3 integration tests passed** (publish files); full unit **521 passed, 1 skipped** (real Windows symlink privilege). |
| 20 | Full non-LLM integration suite | PASS | **90 passed, 528 deselected in 431.91s (0:07:11)** (`pytest -m "integration and not llm" -q`, final tree including the new CLI E2E). |
| 21 | No real GitHub call | PASS | Every live/issue test injects `FakeGitHubClient`; dry-run builds with a fake client and asserts zero calls. No token/repo was configured for this phase. |
| 22 | Dev warehouse unchanged | PASS | 30 batches, max batch `batch_20260130` (max business_date 2026-01-30); 0 incidents, 0 `dq.agent_steps`, 0 `dq.fix_attempts`, 0 `dq.publications`; no `dqs_*` schemas. |
| 23 | No leftover temp databases/schemas | PASS | `pg_database` contains no `dqs_%` databases after all runs; `information_schema.schemata` contains no `dqs_*` schemas. |

Sample real output (generated by the publisher, not hand-written):
`out/pr_previews/12345678.md` and `.patch` (git-ignored; reproduced in the
review packet).

## Design choices and why

* **The approval gate is a runtime check, not just a type hint.** JSON from a
  workflow or a UI can always be a plain dict, so `Publisher` re-checks
  `isinstance(approval, ApprovalDecision)` and re-checks
  `VerificationResult.passed` before touching the backend (DECISIONS 100).
* **Idempotency belongs to the base class.** Both modes ask the same store for
  `(incident_id, kind)` first; dry-run and live cannot diverge in their
  duplicate behaviour. `dq.publications` has no PK by spec, so this is
  application-level (DECISIONS 102).
* **The preview and the commit share one diff.** `build_file_patches` replays
  the exact Phase-5a allowlist/exact-match logic in memory. The `.patch` the
  human reviews is byte-for-byte what live mode writes, so review and commit
  cannot drift (DECISIONS 103).
* **The PR body is a template fed by stored facts.** Root cause, evidence,
  impact and verification are typed records; the only prose is sanitized. The
  model never writes the document (DECISIONS 108 for the title/commit
  message).
* **Sanitization is structural.** GitHub interprets mentions, issue refs, HTML
  and link syntax even inside otherwise plain text. Neutralizing the
  interpreters (zero-width separators, escaped tags/brackets) is more robust
  than a blocklist of payloads (DECISIONS 106).
* **The GitHub surface is deliberately smaller than PyGithub.** The protocol
  has no merge, force-push or base-branch write method; live mode additionally
  rejects a branch equal to the base. Tests can then assert the absence of
  dangerous operations by construction (DECISIONS 104).
* **Live mode fails closed.** Missing token, malformed repo, or a client
  construction error raises before any write (DECISIONS 105).
* **`publish preview` is a preview, not an approval.** It takes no approval
  payload and records `mode=dry_run`; the human still approves in Phase 6b.
  It requires the stored root cause, impact and a passing attempt so it cannot
  render an unverified patch (DECISIONS 107).

## Concepts used

* **Capability gating.** Publishing is modelled as an operation that requires a
  capability token (`ApprovalDecision`), checked at the boundary. This is the
  same idea as an OS capability or a signed approval: possession of the object
  is the permission, and the callee verifies it rather than trusting the
  caller's intent.
* **Idempotency without a unique constraint.** The spec's table has no key, so
  the system performs a read-before-write on `(incident_id, kind)`. This is
  application-level idempotency; it is safe for a single writer but cannot
  replace a database constraint under concurrency.
* **Unified diff as a review artifact.** A diff is a compact edit script with
  context lines. `difflib.unified_diff` produces the same format `git apply`
  understands, which is why the reviewed `.patch` can also be the committed
  content.
* **Markdown as an interpreter boundary.** A PR body is not inert text: GitHub
  parses mentions, references, links and HTML. Prompt-injection hygiene here is
  output encoding, not input filtering — escape/neutralize at the point where
  untrusted text becomes Markdown.
* **Fail-closed configuration.** When a safety-critical mode is requested but
  its prerequisites are missing, the system refuses to start rather than
  falling back silently. A missing token becomes a clear error, not a dry-run
  surprise.
* **Deterministic artifacts over model prose.** The body, title, branch and
  commit message are computed from typed facts; the same input must produce the
  same bytes. Reproducibility is what makes the human review meaningful.

## Deviations from the docs

* **Only Phase 6 task 3 was implemented.** `workflow/graph.py`, the
  interrupt/resume approval gate, `dqs incident run|approve|reject` and
  `dqs demo` are Phase 6b. `dqs publish preview` is a manual dry-run entry point,
  not the workflow.
* **Issue previews use `out/issue_previews/`.** §12 specifies only
  `out/pr_previews/`; the mirror directory was added so the ISSUE route can be
  previewed without inventing a GitHub write (DECISIONS 101).
* **No real live PR/issue was opened.** The phase explicitly allows a fake
  client; no owner test repo/token was configured, so the live path is proven
  against the fake and the PyGithub adapter is not network-tested (optional in
  PHASES).
* **`dq.publications` has no database uniqueness.** Application-level
  idempotency plus a documented crash window (DECISIONS 102).

## Known limitations / TODO

* **Crash window.** The row is inserted after the successful write. A process
  crash between the GitHub write and the insert can duplicate on retry. Live
  mode mitigates the common case by reusing an existing branch/PR, but the
  window is not zero.
* **Single-writer assumption.** Concurrent publications for one incident are
  unsupported; `find` + `record` is not atomic and the table has no key.
* **PyGithub adapter is not exercised against the real API.** Its method
  signatures were checked against installed PyGithub 2.10.0, and the publisher
  logic is tested through the protocol, but a real token/repo run remains
  optional Phase 6 acceptance.
* **`dqs publish preview` requires persisted artifacts.** It cannot preview an
  in-memory workflow state; Phase 6b must persist the root cause, impact and
  attempt before calling it (or call the publisher directly).
* **Issue CLI is not wired.** `Publisher.create_issue` and
  `DryRunPublisher`'s issue path exist and are tested, but no CLI command
  renders an `IssueDraft` yet; that belongs to the Phase 6b ISSUE branch.
* **The body caps lists at 20 items and the diff at 8,000 characters.** This is
  deliberate review ergonomics; full evidence remains in the database.
* **No approval UI.** The gate is an API; the LangGraph interrupt and
  Streamlit buttons are later phases.
