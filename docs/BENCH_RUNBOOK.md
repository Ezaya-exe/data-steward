# Benchmark runbook (Phase 8b)

This is the operator guide for running the fault-injection benchmark. It is
written for PowerShell on Windows; every command is copy-pasteable from the
repository root. Read the whole page once before starting a real run.

The benchmark spends real model quota. A run is resumable and writes its results
after every scenario, so a stop is never a catastrophe - but an unattended run
can still burn a day's budget. Check the stop messages below.

## 0. What you need

- Docker Desktop is running and Postgres is healthy (`dqs db ping`).
- `.env` has the model provider key and `LLM_MODEL_BENCH` (or pass `--model`).
- Approved contracts (`contracts/*.yml` all `approved`).
- At least 1 GiB free disk, and the machine will stay awake for the whole run.
- **`GITHUB_MODE` does not matter.** The benchmark always forces `dry_run`:
  issues/PRs are written to `out/issue_previews/` and `out/pr_previews/`, never
  to GitHub. A live `GITHUB_MODE` in `.env` cannot override this (it is recorded
  in the results metadata as `github_mode_env=live`, `github_mode=dry_run`).
  `dqs demo` forces the same mode.

## 1. Preflight (always, before spending quota)

```powershell
.venv\Scripts\dqs dev up
.venv\Scripts\dqs bench preflight --model openai/gpt-oss-120b
```

Preflight is read-only and makes **no LLM calls**. It checks, with PASS/FAIL:

| check | what PASS means |
|---|---|
| postgres healthy | the read/write and read-only roles both connect |
| dqs_template cached/current | the cached clean baseline exists and its content hash/seed/day count match this checkout |
| contracts approved | at least one contract exists and none are still `draft` |
| GITHUB_MODE effective | the benchmark's forced `dry_run` mode (a live `.env` is called out) |
| benchmark model | `--model` or `LLM_MODEL_BENCH` is non-empty |
| harness frozen | `HARNESS_VERSION` and both prompt hashes are present |
| free disk space | at least 1.0 GiB free |
| LLM API key | a key is configured for the provider (the value is never printed) |

Fix every FAIL before continuing. A **stale** template (hash/seed/day mismatch)
is a real FAIL; a **missing** template is reported as PASS because the first
run builds it once (~10 s), so it cannot corrupt a measurement.

## 2. Pilot run (4 scenarios, first real check)

The pilot is deliberately small and covers the three resolutions plus a clean
control. It is the first real-model run and is not a benchmark result to quote.

| id | fault | expected | why it is in the pilot |
|---|---|---|---|
| `f1-orders-status` | column rename (F1) | PR | the canonical fix path |
| `f4-orders-status-20` | 20% null spike (F4) | ISSUE | a non-code fix must route to an issue |
| `f6-order-items-15` | 15% duplicate rows (F6) | PR | a harder dedupe fix on a child table |
| `clean-01` | none | control | proves a clean batch produces no false positive |

```powershell
.venv\Scripts\dqs bench run --split dev --model openai/gpt-oss-120b --ids f1-orders-status,f4-orders-status-20,f6-order-items-15,clean-01
```

Review the console summary and the generated report before starting the full dev
split:

```powershell
.venv\Scripts\dqs bench report
```

Expected shape: 4 scenarios, detection 3/3 faults, one clean control, no
infrastructure errors. Category/resolution accuracy may be below 100%; that is a
measurement, not a bug to fix by tuning prompts on the spot.

## 3. First full dev run

The dev split is 43 scenarios. The runner is sequential by design. Results are
rewritten after every scenario to
`benchmarks\results\<UTC run id>_<safe model name>.json`, with `latest.md` next
to it.

```powershell
.venv\Scripts\dqs bench run --split dev --model openai/gpt-oss-120b
```

Useful options:

- `--daily-token-cap 180000` - local per-UTC-day budget across invocations
  (default 180,000). The ledger is `data\token_ledger.json` (git-ignored).
- `--tpm-limit 8000` - client-side one-minute throttle.
- `--repeats 3` - run every selected scenario three times (see §6).
- `--no-dbt` - skip the dbt build after loading the fault. Do **not** use this
  for a benchmark number; it removes the failing dbt run record from the
  scenario.
- `--results-dir PATH` - write somewhere other than `benchmarks\results\`.

Before the first scenario the runner prints the tokens already recorded for
today and this provider/model. That number matters: **`dqs demo`, the real-LLM
tests, and any other terminal using the same API key share the provider's
quota**. The ledger only sees benchmark runs.

## 4. Daily resume

A run can stop for three different reasons (see §7). The stop message prints the
exact `--resume` path. In PowerShell, the newest file for a model is:

```powershell
$results = Get-ChildItem benchmarks\results\*_openai-gpt-oss-120b.json |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName
.venv\Scripts\dqs bench run --split dev --model openai/gpt-oss-120b --resume $results
```

The path pattern is:

```
benchmarks\results\20261023T101500Z_openai-gpt-oss-120b.json
```

Rules:

- `--resume` refuses a different model, provider, split, or `--repeats` value.
- Completed scenarios are skipped; infrastructure failures and scored errors
  are re-run automatically.
- Check `benchmarks\results\latest.md` after each resume; it lists the stop
  reason, infrastructure errors and remaining resume command.

## 5. Re-run specific scenarios

Use `--ids` to run an explicit subset. This is the right tool after reviewing a
report and deciding that specific scenarios need a second look:

```powershell
.venv\Scripts\dqs bench run --split dev --model openai/gpt-oss-120b --ids f1-orders-status,f4-orders-status-20,f6-order-items-15
```

- `--ids` accepts comma-separated ids and validates the split.
- Without `--resume` it starts a **new** results file; do not mix that file with
  the main run when reporting.
- With `--resume` the same file is updated, but completed scenarios are skipped.
  To force a scored error to run again, resume the file (errors are not
  "completed") or use a fresh `--ids` run.

## 6. Repeats (variance / flakiness)

`--repeats N` runs every selected scenario N times with keys `(scenario id,
repeat)`. Each repeat is a separate run, and the report adds a
**Repeat stability** table (`stable` / `flaky`).

A practical variance check is about 5 scenarios x 3 repeats (15 paid runs), not
the whole catalogue:

```powershell
.venv\Scripts\dqs bench run --split dev --model openai/gpt-oss-120b --ids f1-orders-status,f4-orders-status-20,f6-order-items-15,f6-orders-03,clean-01 --repeats 3
```

Do not compare results files with different `--repeats` values; the resume guard
will refuse a mismatch, and the report treats each repeat as a separate record.

## 7. Holdout (reporting only)

The holdout split is 16 scenarios. It must not be used to tune prompts or
choose fixes (AGENTS.md §7.9). Run it once, after the dev run and after any code
or prompt changes are frozen:

```powershell
.venv\Scripts\dqs bench run --split holdout --model openai/gpt-oss-120b --confirm-holdout
```

`--confirm-holdout` is mandatory. If the code/prompt fingerprint differs from
the newest dev results file, the runner refuses to start unless you record why:

```powershell
.venv\Scripts\dqs bench run --split holdout --model openai/gpt-oss-120b --confirm-holdout --acknowledge-changes "fixed the tool-result truncation bug"
```

The acknowledgement is written into the results metadata. It is an audit trail,
not a rubber stamp: if the change could affect answers, re-run dev first.

## 8. Read the report

```powershell
.venv\Scripts\dqs bench report
# or a specific file:
.venv\Scripts\dqs bench report benchmarks\results\20261023T101500Z_openai-gpt-oss-120b.json
```

Every proportion is printed as `rate (k/n) [95% Wilson low-high]`. The interval
is wide on small samples; do not quote a pilot (n=4) as a benchmark result.
`n/a (0/0)` means the denominator was zero (for example, no PR-expected
scenario or no clean control), not 0%.

The report also shows:

- provenance: git commit, **dirty worktree** flag, code fingerprint, harness
  version, prompt hashes, model/provider, limits;
- infrastructure errors separately, with the count and message;
- repeat stability when `--repeats` was used;
- scenarios to look at (scored misses and errors).

## 9. Stop messages: what they mean

| message | meaning | what to do |
|---|---|---|
| `daily token cap reached before <id> ... (used >= cap tokens today)` | the local ledger says today's benchmark usage has reached `--daily-token-cap`. The scenario named has **not** run. | Resume tomorrow (UTC date changes), or raise the cap deliberately. The ledger is `data\token_ledger.json`. |
| `daily provider quota exhausted: ...` | the provider returned a daily-quota error. The attempt is recorded as `infra_error`, not scored, and the run stops immediately. | Wait for the provider quota reset, then resume. Do not retry in a loop. |
| `circuit breaker: 2 consecutive infrastructure errors ...` | two scenarios in a row failed for provider/network reasons (429/rate limit/quota/timeout-style messages). The run stops cleanly. | Wait a few minutes, check the provider status, then resume. Infra records re-run automatically. |
| `INFRA <message>` on a scenario line | that one attempt is an infrastructure failure. It is excluded from every denominator. | Resume later; no action needed now. |
| `ERROR <message>` on a scenario line | a real scenario/agent failure. It is recorded and counted as a miss, and resume will retry it. | Inspect the error; fix code if it is a bug, otherwise let resume retry. |
| `stopped early: ...` in `latest.md` | the run ended before the full split; the file is still valid. | Use the printed `resume:` command. |

A results file is written after every scenario, so you can close the terminal at
any point. The only unsafe state is starting a **new** run while an old one is
still resuming - do not run two benchmarks at once against one API key.

## 10. When the model, prompts or code change

The result file records a **code fingerprint**: one SHA-256 over the agent,
tool, verification, workflow, check and contract packages, the resolver,
injector, scorer/catalogue/oracle, and both prompt files. `HARNESS_VERSION` is
still recorded for humans, but the fingerprint is the real comparison.

Procedure:

1. Finish any run in progress and note its results path.
2. Make the change (model name, prompt, code) and run the pilot again
   (`--ids ...`); confirm the wiring and no new errors.
3. Re-run the dev split. This becomes the new reference dev result.
4. Run holdout only after dev is frozen. If the fingerprint differs from the
   newest dev file, pass `--acknowledge-changes "reason"`; the reason is stored
   in the result metadata.
5. Update `docs/DECISIONS.md` with the change and the date, and bump
   `HARNESS_VERSION` in `src/dqs/bench/harness.py` when the plumbing (not just a
   comment) changes.
6. Never compare numbers from different fingerprints as if they measured the
   same system. The report shows the fingerprint, dirty flag and harness version
   precisely so a reader can tell.

Changing only the model name is a change of measurement, not of the harness:
start a new results file (the runner refuses to mix models in one file) and
record the model in the report.

## 11. Keep Windows awake

A sleeping machine pauses the run and can leave a scenario database behind; the
next run drops it, but the interruption wastes a paid attempt. Before a long run:

```powershell
# Plug in, keep the machine awake on AC, keep the display timeout short.
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 15

# Optional: see what currently holds a power request.
powercfg /requests
```

Keep the lid open and do not run on battery. Restore your normal values
afterwards, for example:

```powershell
powercfg /change standby-timeout-ac 30
powercfg /change hibernate-timeout-ac 60
powercfg /change monitor-timeout-ac 10
```

Use `powercfg /query` first if you want to restore the exact previous values.

## 12. Scenario ids by fault kind and split

All ids below are from the frozen catalogue. `dev` is the tuning/measurement
split; `holdout` is reporting-only and requires `--confirm-holdout`.

| fault kind | dev ids | holdout ids |
|---|---|---|
| `column_rename` (F1) | `f1-orders-status`, `f1-payments-method`, `f1-customers-email`, `f1-order-items-quantity` | `f1-orders-customer-id` |
| `column_removed` (F2) | `f2-customers-email`, `f2-payments-amount`, `f2-order-items-quantity`, `f2-order-items-unit-price` | `f2-orders-status` |
| `type_change` (F3) | `f3-payments-amount`, `f3-order-items-unit-price`, `f3-order-items-quantity` | `f3-orders-total-amount` |
| `null_spike` (F4) | `f4-orders-status-20`, `f4-customers-email-60`, `f4-customers-state-35`, `f4-payments-method-20`, `f4-order-items-quantity-60`, `f4-orders-customer-20`, `f4-order-items-unit-price-35` | `f4-orders-status-35` |
| `orphan_keys` (F5) | `f5-orders-customer-35`, `f5-order-items-order-35`, `f5-payments-order-20`, `f5-payments-order-35`, `f5-order-items-product-35` | `f5-orders-customer-15` |
| `duplicate_rows` (F6) | `f6-orders-03`, `f6-order-items-05`, `f6-order-items-03`, `f6-payments-15`, `f6-customers-08`, `f6-order-items-15` | `f6-orders-08` |
| `scale_shift` (F7) | - | `f7-orders-total-100`, `f7-orders-total-1000`, `f7-payments-amount-100`, `f7-order-items-unit-price-1000` |
| `late_batch` (F8) | `f8-customers-30`, `f8-payments-48`, `f8-order-items-24`, `f8-order-items-30` | `f8-orders-24` |
| `missing_table` (F9) | - | `f9-customers`, `f9-orders`, `f9-order-items`, `f9-payments` |
| `column_added` (F10) | `f10-customers-note`, `f10-payments-campaign`, `f10-order-items-note`, `f10-orders-source` | `f10-orders-coupon` |
| `clean` (control) | `clean-01`, `clean-02`, `clean-03`, `clean-04`, `clean-05`, `clean-06` | - |

## 13. Honest reporting checklist

Before quoting a number:

- [ ] It came from a real provider/model, not the `fake` provider.
- [ ] The report shows the model, date, git commit and fingerprint.
- [ ] The worktree was clean (or the dirty flag is disclosed).
- [ ] `n` and the 95% Wilson interval are shown next to the proportion.
- [ ] Infrastructure errors are excluded and listed, not counted as agent
      failures.
- [ ] No holdout scenario was used to choose a prompt or fix.
- [ ] The number is not a 4-scenario pilot presented as the full dev split.
