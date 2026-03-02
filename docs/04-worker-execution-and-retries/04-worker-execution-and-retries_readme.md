---
summary: "How workers claim tasks, execute Codex, handle retries, and decide terminal failures."
read_when:
  - "When changing processing order, retries, lease handling, or worker failure behavior"
  - "When debugging stuck/running tasks, unexpected retries, or max-attempt outcomes"
---

# Worker Execution And Retries (Chunk 04)

This chunk owns everything that happens after tasks already exist in SQLite: claiming work, executing Codex, validating output, retrying failures, and deciding when a task becomes terminal `error`.

If you start with zero context, use this as the execution mental model for the runtime loop.

`process` worker slots run in-process via `ThreadPoolExecutor`; each worker loop uses its own SQLite connection, and `lease_one_task(...)` transaction semantics are the cross-thread concurrency guard.

## What This Chunk Owns

- Lease-based task claiming (`queued` and expired `running` tasks)
- Per-task execution (`codex exec` call + schema gate)
- Retry policy and terminal error policy
- Failed-attempt forensics capture (`task_forensics` index + bundle writes) before cleanup/requeue/error transitions
- Worker exit code semantics
- Task-level `--cd` selection behavior during worker execution

## Primary Files

- `src/codex_farm/worker.py`
- `src/codex_farm/db.py`
  - `lease_one_task`
  - `mark_task_done`
  - `mark_task_error`
  - `requeue_task`

Related boundaries:

- Queue shape/status inference: `docs/03-run-planning-and-queue-state/03-run-planning-and-queue-state_readme.md`
- Codex subprocess + schema contract: `docs/05-codex-exec-and-schema-gate/05-codex-exec-and-schema-gate_readme.md`

## End-To-End Worker Loop

`worker_loop(...)` in `src/codex_farm/worker.py` runs this cycle:

1. Claim one task via `lease_one_task(...)`.
2. If no task:
   - `once=True` -> return current worker exit code.
   - `once=False` -> sleep `poll_seconds` and continue.
3. Pre-check attempts:
   - if `effective_execution_attempts_before = execution_attempts - rate_limit_count` is already at budget, mark terminal `error` immediately.
4. Load run metadata (`get_run`) and parse `runs.config_json`.
5. Resolve run root:
   - run config `farm_root` wins
   - otherwise fallback to worker `--root` (if provided)
6. Resolve optional run-level `workspace_root` override (must exist as a directory).
7. Resolve execution contract:
   - if `runs.config_json.frozen_assets` exists, load and verify snapshot files from `<data_dir>/run_assets/<run_id>/` and use frozen settings (prompt/schema/model/effort/cd-mode/sandbox/approval/web-search/timeout)
   - otherwise load live pipeline map from resolved farm root (cached per root path) and use legacy run-config override precedence
8. If frozen assets exist but are missing/invalid/hash-mismatched, mark terminal `error` (non-retryable) and do not fall back to live pipeline files.
9. Resolve Heads Up config:
   - `runs.config_json.heads_up_enabled` controls prompt adaptation.
   - `runs.config_json.heads_up_max_tips` caps appended tip count.
10. Resolve task paths:
   - `input_path` from task row
   - canonical output path `run.output_dir / task.rel_output_path`
   - staged path `run.output_dir/.codex-farm-stage/<task_id>/<lease_token><ext>`
11. Resolve task `cd_dir`:
   - explicit run `workspace_root` override
   - else pipeline `codex_cd_mode` (`asset_root` / `input_dir` / `input_file_dir`)
12. Render prompt template and optionally append matching Heads Up tips.
13. Start heartbeat for the claimed lease (separate DB connection), then increment `execution_attempts` immediately before each real Codex start.
14. Execute Codex and validate staged output JSON against the resolved schema path.
15. Promote staged output to canonical output only inside an owner-checked transaction (`task_id + lease_token`).
16. On failure, capture failure forensics (best-effort) before staged-output cleanup.
17. Mark task `done`, or requeue/error on failure.
18. If Heads Up tips were applied and task reached terminal outcome (`done|error`), record usage to update tip scoring.

## Lease Claiming Contract (Critical Concurrency Logic)

`lease_one_task` in `src/codex_farm/db.py` is the concurrency guard:

- Uses `BEGIN IMMEDIATE` transaction.
- Eligible tasks:
  - `status='queued'`
  - or `status='running'` with expired lease (`lease_until < now`)
- Optional run scoping: `run_id` filter when provided.
- Ordering:
  - queued tasks first
  - then expired-running tasks
  - oldest `updated_at` first
- On claim, atomically updates:
  - `status='running'`
  - `attempts = attempts + 1`
  - `leased_by = worker_id`
  - `lease_until = now + lease_seconds + 2s jitter grace`
  - `error = NULL`
  - run row status to `running`

Key implication: attempts are consumed on claim, not on completion.

Heartbeat contract:

- Workers run a per-task heartbeat session after claim to extend `lease_until` while execution is healthy (same `+2s` grace).
- `lease_one_task(...)` now performs a read pre-check and skips `BEGIN IMMEDIATE` entirely when there is no queued/expired candidate, reducing write-lock churn during contention.
- Heartbeat writes are owner-checked by `lease_token`.
- On owner mismatch heartbeat stops and marks `lost_ownership`, so stale workers skip finalization safely.

## Retry And Terminal-Failure Policy

Handled inside `worker_loop`:

- Retryable failure classes:
  - `CodexExecTimeoutError`
  - `SchemaValidationError`
  - `RuntimeError` raised when Codex subprocess result is not OK (except detected rate limits or auth failures)
  - unexpected exceptions (fallback branch)
- Retry context is attempt-aware:
  - queue leasing now carries a `previous_error` snapshot from the prior task row.
  - on effective execution attempts `>1` (`execution_attempts - rate_limit_count`), worker prompt appends a compact retry block with that failure text so any worker can correct the prior failure mode.
- For retryable failures:
  - worker captures a forensics bundle first (when possible), then deletes staged output file (`unlink(missing_ok=True)`)
  - if `effective_execution_attempts >= max_attempts`: mark terminal `error`
  - else: `requeue_task(...)` to `status='queued'`
- Rate-limit failures (`CodexExecRateLimitError`) use adaptive handling:
  - interrupted task is requeued with `error=NULL`
  - `tasks.rate_limit_count` increments
  - run-level cooldown/concurrency state is written to `run_throttle_state`
  - workers stop claiming while cooldown is active and resume automatically
  - worker exits non-zero only after max consecutive rate-limit events (default 6), leaving queued work resumable
- Terminal without retry (configuration/setup failures before execution):
  - invalid/unknown farm root
  - invalid `workspace_root`
  - unknown `pipeline_id` in run metadata
  - invalid/missing/tampered `frozen_assets` snapshot for snapshot-bearing runs
  - computed `cd_dir` does not exist
  - execution-attempt budget already exhausted before processing (`effective_execution_attempts_before >= max_attempts`)
- Terminal without retry (execution-time auth failure):
  - Codex stderr/stdout matches auth/session failure signatures (`401/403`, `backend-api/codex/responses`, login-required text)
  - worker records `failure_category="auth_failure"` and marks the task `error` immediately with remediation text

Forensics capture contract:

- Every meaningful worker failure branch calls `capture_failure_forensics(...)` best-effort.
- Schema/invalid-json paths capture before staged output cleanup so rejected payload evidence is preserved outside normal output dirs.
- Timeout branches are currently metadata-only for raw output because `run_codex_exec(...)` removes temp output on timeout; tails/runtime context are still captured.
- Forensics write failures never replace the original task failure handling.

Error text truncation:

- Worker trims to 1800 chars (`_trim_error`)
- DB update functions hard-cap to 2000 chars

## Attempt Budget Semantics (Easy To Misread)

Because lease claims and real execution starts are tracked separately:

- `attempts` (alias `lease_claims`) counts lease claims.
- `execution_attempts` counts real Codex starts.
- retry budget uses `effective_execution_attempts = execution_attempts - rate_limit_count`.
- `max_attempts=3` allows up to 3 effective execution attempts; 429 retries still do not consume budget.

## `--cd` Resolution During Worker Execution

Worker `cd_dir` decision in `_resolve_task_cd_dir(...)`:

1. `runs.config_json.workspace_root` if present and valid directory.
2. Else by pipeline `codex_cd_mode`:
   - `asset_root` -> resolved farm root
   - `input_dir` -> run input root (`run["input_dir"]`)
   - `input_file_dir` -> task input file parent

If computed `cd_dir` does not exist, task is terminal `error` (no retry).

## Worker Exit Code Semantics

`worker_loop` returns:

- `0` if this worker instance never marks a task terminal `error`.
- `1` if it marks any task terminal `error` during its lifetime.

In `process`, the final command exit is non-zero when:

- any worker exit code is non-zero, or
- final run error count is non-zero.

## State Transitions Owned Here

Common transitions for a task:

- `queued -> running` (lease claim)
- `running -> done` (successful Codex + schema validation)
- `running -> queued` (retryable failure with attempts still under budget)
- `running -> error` (terminal failure)
- `running (expired lease) -> running` by a different worker on reclaim
- Heads Up score side-effect on terminal outcomes:
  - `heads_up_tips.uses += 1` when an applied tip reaches terminal task outcome
  - `heads_up_tips.wins += 1` for terminal `done`
  - `heads_up_tips.score = (wins + 1) / (uses + 2)` after update

Run status is inferred elsewhere (`run_status`), not directly finalized by worker.

## Debugging Checklist

When tasks appear stuck or repeatedly fail:

1. Inspect `run errors --run-id <id> --json` for terminal failures and attempts.
   - Compare `lease_claims` vs `execution_attempts` to detect lease churn versus real retries.
2. Inspect `run tasks --run-id <id> --status running --json` for stale leases.
3. Compare current time vs `lease_until` to confirm reclaim eligibility.
4. Validate run config values in `runs.config_json`:
   - `farm_root`
   - optional `workspace_root`
   - optional `codex_model`
   - optional `codex_reasoning_effort`
   - optional `output_schema_path_override`
   - optional `frozen_assets` (`version`, `manifest_relpath`)
   - `heads_up_enabled`, `heads_up_max_tips`
5. For snapshot-bearing runs, verify manifest/files still exist under `<data_dir>/run_assets/<run_id>/`.
6. For older runs with no snapshot metadata, verify pipeline still exists for `run.pipeline_id`.
7. Verify computed `cd_dir` exists for chosen `codex_cd_mode`.
8. For schema failures, check output was deleted, error contains validation details, and `run forensics --run-id <id> --json` includes a bundle row.

## Tests That Define This Contract

- `tests/test_worker.py`
  - worker success path with mocked Codex
  - `codex_cd_mode` routing (`asset_root`, `input_dir`, `input_file_dir`)
  - frozen prompt/schema/pipeline drift prevention and corrupt-snapshot rejection
  - Heads Up prompt injection + score update when enabled
  - forensics capture-before-cleanup assertion on schema failure
- `tests/test_db.py`
  - lease claim behavior (`attempts` increment, running status)
  - task listing/error row fields plus Heads Up tip/usage table helpers
- `tests/test_process_smoke.py`
  - multi-worker process flow over queued tasks
- `tests/test_heads_up_integration.py`
  - run-A learning writes tips and run-B prompt receives appended Heads Up block
- `tests/test_fake_codex_pipeline_pack_demo.py`
  - schema failure path reaching terminal `error` via `process`
  - preserved forensics bundle contents while normal output path stays clean

## Safe Change Checklist For Future Edits

When modifying this chunk:

1. Preserve atomic lease claim semantics (`BEGIN IMMEDIATE` + update in same transaction).
2. Keep attempts semantics intentional (claim-time increment).
3. Ensure every failure branch clears/handles output file consistently.
4. Keep terminal-vs-retry behavior explicit; do not silently strand tasks in `running`.
5. Re-run worker/db/process tests together, not in isolation.

## Task doc merges from `docs/tasks`

Historical task docs merged into this chunk to preserve retry/failure context:

- `Initial-Build.md` (`2026-02-20_12.45.00` revision note):
  - set the baseline leasing/retry design (`queued -> running -> done|queued|error`) with per-task attempt budgets.
  - recorded deliberate in-process worker fan-out for `process` as the V1 simplicity/performance choice.
- `Plan-for-knowledge-correction.md` (`2026-02-22_13.07.23`):
  - added pipeline `codex_cd_mode` routing into worker execution context and codified explicit override precedence.
  - locked terminal handling for invalid computed `--cd` paths to prevent pointless retry loops on configuration errors.
  - reinforced deterministic fake-Codex coverage for worker cd resolution and error introspection paths.
- `2026-02-23_00.24.39 - stop-on-rate-limit-429.md`:
  - locked terminal-on-first-hit behavior for Codex `429`/rate-limit failures.
  - locked process-level stop-event propagation so sibling workers stop claiming new tasks after a rate-limit hit.
  - task evidence called out regression tests `test_worker_loop_stops_immediately_on_rate_limit` and `test_process_command_stops_after_first_rate_limit`.
- `2026-02-28_09.33.49-heads-up-adaptive-prompts.md`:
  - added cross-run Heads Up prompt augmentation in worker execution with deterministic input-signature matching and wildcard fallback.
  - locked outcome-based tip scoring path (`uses`, `wins`, smoothed score) plus terminal-only learning constraints.
  - captured warning-safe learner behavior (`process`/`go` exit semantics stay tied to worker outcomes, not learner failures).
- `idea1-3.md` (`2026-02-28_13.50.27`):
  - split lease claims from real execution starts (`attempts` vs `execution_attempts`) and added heartbeat recency (`last_heartbeat_at`).
  - documented the stale-owner file race and resulting staged-output promotion contract (owner-checked promotion, stale cleanup only on staged files).
  - preserved additive compatibility surfaces (`lease_claims`, `execution_attempts`, heartbeat fields) without breaking legacy `attempts` semantics.
- `idea1-5.md` (`2026-02-28_13.24.13`):
  - replaced stop-on-first-429 with adaptive cooldown/concurrency recovery via durable `run_throttle_state`.
  - preserved retry-budget correctness by treating provider throttling as internal accounting (`rate_limit_count`) rather than terminal task failure.
  - captured important dead-end from task history: warning dedupe in `_run_workers` masked adaptive transition visibility and was removed for persistent-429 clarity.

## Merged discoveries from `docs/understandings`

These worker-flow notes are now folded into the chunk doc:

- `2026-02-20_12.50.00`: `process` uses in-process thread fan-out, not nested `codex-farm worker` subprocesses; safety comes from SQLite leasing, not thread-local locks.
- `2026-02-20_12.50.00`: Lease claim is atomic (`BEGIN IMMEDIATE` + `attempts += 1`) and prevents duplicate task claims under concurrency.
- `2026-02-20_13.24.12`: End-to-end success path is lease -> codex execution -> local schema validation -> task transition -> run-status inference.
- `2026-02-20_13.24.12`: End-to-end failure path accepts non-zero codex exits only when payload exists; local schema gate still determines done/error routing.
- `2026-02-22_14.34.10`: Attempt budget is consumed when a lease is claimed, including expired-lease reclaims after crashes.
- `2026-02-22_14.34.10`: Guard branch `attempts > max_attempts` is required to terminate over-budget reclaimed tasks before execution.
- `2026-02-23_00.24.39`: Hard-stop-on-429 was an intermediate policy; it reduced pressure quickly but stranded queued work and was later superseded by adaptive cooldown/concurrency handling.
- `2026-02-28_02.47.41`: Worker model selection is run-config aware: `codex_model` override in `runs.config_json` takes precedence over pipeline defaults so resumed runs keep model intent.
- `2026-02-28_02.55.22`: Worker effort selection is run-config aware: `codex_reasoning_effort` override in `runs.config_json` takes precedence over pipeline defaults so resumed runs keep effort intent.
- `2026-02-28_09.31.02`: Worker schema selection is run-config aware: `output_schema_path_override` in `runs.config_json` takes precedence over pipeline `output_schema_path` so resume/retry validation remains deterministic.
- `2026-02-28_09.33.49`: Worker prompt adaptation is run-config aware: `heads_up_enabled` + `heads_up_max_tips` gate tip retrieval by input signature and append deterministic `Heads up` blocks without changing task queue schema.
- `2026-02-28_09.39.25`: Failure lifecycle is retry-then-error: timeout/runtime/schema paths requeue while `attempts < max_attempts`, then flip to terminal `error` at attempt budget.
- `2026-02-28_09.39.25`: Output cleanup is part of the failure contract (`unlink` before requeue/error); later adaptive 429 work changed only rate-limit policy, not cleanup ordering.
- `2026-02-28_09.39.25`: Operational inspection contract is `run tasks --json` plus `run errors --json`; run-level `error` state still derives from task counts, not worker return code alone.
- `2026-02-28_09.47.44`: Retry guidance is queue-state based, not worker-local: `lease_one_task` surfaces `previous_error`, and attempts `>1` append it to prompt text so cross-worker retries get actionable failure context.
- `2026-02-28_12.34.52`: lifecycle-aware workers now lease only from active runs, keep polling through pause for run-scoped continuous loops, suppress retry requeue during `cancel_requested` by marking retryable failures `canceled`, and reject stale final writes via lease-token checks (including stale output cleanup).
- `2026-02-28_13.20.16`: Lease-token DB guards already protected `mark_task_done` / `mark_task_error` / `requeue_task`, but direct canonical output writes still allowed stale-owner unlink races; staged promotion was identified as the required seam.
- `2026-02-28_13.24.13`: Adaptive 429 handling required transactional claim-time gating (cooldown/concurrency checks inside lease transaction) plus retry-budget accounting via `execution_attempts - rate_limit_count`.
- `2026-02-28_13.27.21`: Claim-time gating already spans both lifecycle control (`runs.control_state`) and throttle state; heartbeat/promotion refactors must preserve that combined contract and keep forensics capture paths intact.
- `2026-02-28_13.49.48`: Forensics capture must happen before staged/canonical cleanup on schema/runtime failures; timeout failures remain metadata/tail-based unless codex timeout cleanup semantics change.
- `2026-02-28_13.50.27`: Heartbeats and staged promotion solve different failure modes and must ship together: heartbeat prevents false reclaim churn, while owner-checked promotion prevents stale-worker canonical-output deletion.
- `2026-02-28_14.03.46`: Short-lease reclaim stability depends on jitter tolerance and lock-pressure control: claims/heartbeats write `lease_until` with `+2s` grace and claim probing avoids unnecessary `BEGIN IMMEDIATE` when no candidate exists.
- `2026-02-28_18.46.00`: failed-attempt forensics capture is best-effort and must execute before staged-output cleanup on schema/runtime paths so evidence survives while normal output directories stay clean.
- `2026-02-28_18.46.00`: timeout bundles are metadata-only for raw payload because codex-exec timeout cleanup removes temp output before worker recovery logic runs.

Known bad path to avoid:

- Interpreting `attempts` as "number of completed runs" causes retry-budget math bugs; in this system `attempts` means "number of lease claims."
- Treating heartbeat as a complete stale-safety fix while still writing directly to canonical output paths leaves stale-owner delete races unresolved.
