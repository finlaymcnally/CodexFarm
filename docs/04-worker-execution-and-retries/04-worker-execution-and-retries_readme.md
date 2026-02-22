---
summary: "How workers claim tasks, execute Codex, handle retries, and decide terminal failures."
read_when:
  - "When changing processing order, retries, lease handling, or worker failure behavior"
  - "When debugging stuck/running tasks, unexpected retries, or max-attempt outcomes"
---

# Worker Execution And Retries (Chunk 04)

This chunk owns everything that happens after tasks already exist in SQLite: claiming work, executing Codex, validating output, retrying failures, and deciding when a task becomes terminal `error`.

If you start with zero context, use this as the execution mental model for the runtime loop.

## What This Chunk Owns

- Lease-based task claiming (`queued` and expired `running` tasks)
- Per-task execution (`codex exec` call + schema gate)
- Retry policy and terminal error policy
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
   - if `task["attempts"] > max_attempts`, mark terminal `error` immediately.
4. Load run metadata (`get_run`) and parse `runs.config_json`.
5. Resolve run root and pipeline map:
   - run config `farm_root` wins
   - otherwise fallback to worker `--root` (if provided)
   - pipeline specs are cached per resolved root path.
6. Resolve optional run-level `workspace_root` override (must exist as a directory).
7. Resolve pipeline for `run["pipeline_id"]`; unknown pipeline is terminal `error`.
8. Resolve task paths:
   - `input_path` from task row
   - `output_path = run.output_dir / task.rel_output_path`
9. Resolve task `cd_dir`:
   - explicit run `workspace_root` override
   - else pipeline `codex_cd_mode` (`asset_root` / `input_dir` / `input_file_dir`)
10. Render prompt template and execute Codex.
11. Validate output JSON against schema.
12. Mark task `done`, or requeue/error on failure.

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
  - `lease_until = now + lease_seconds`
  - `error = NULL`
  - run row status to `running`

Key implication: attempts are consumed on claim, not on completion.

## Retry And Terminal-Failure Policy

Handled inside `worker_loop`:

- Retryable failure classes:
  - `CodexExecTimeoutError`
  - `SchemaValidationError`
  - `RuntimeError` raised when Codex subprocess result is not OK
  - unexpected exceptions (fallback branch)
- For retryable failures:
  - worker deletes output file (`unlink(missing_ok=True)`)
  - if `attempts >= max_attempts`: mark terminal `error`
  - else: `requeue_task(...)` to `status='queued'`
- Terminal without retry (configuration/setup failures before execution):
  - invalid/unknown farm root
  - invalid `workspace_root`
  - unknown `pipeline_id` in run metadata
  - computed `cd_dir` does not exist
  - attempts already beyond budget (`attempts > max_attempts`) before processing

Error text truncation:

- Worker trims to 1800 chars (`_trim_error`)
- DB update functions hard-cap to 2000 chars

## Attempt Budget Semantics (Easy To Misread)

Because attempts increment on lease:

- `max_attempts=3` allows up to 3 actual execution attempts under normal retry flow.
- A lease-expired reclaim can still increase attempts even if prior worker crashed before completion.
- Guard `if task["attempts"] > max_attempts` catches over-budget reclaimed tasks and marks terminal error immediately.

This is why stale `running` rows with frequent lease expiry can consume retry budget.

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

Run status is inferred elsewhere (`run_status`), not directly finalized by worker.

## Debugging Checklist

When tasks appear stuck or repeatedly fail:

1. Inspect `run errors --run-id <id> --json` for terminal failures and attempts.
2. Inspect `run tasks --run-id <id> --status running --json` for stale leases.
3. Compare current time vs `lease_until` to confirm reclaim eligibility.
4. Validate run config values in `runs.config_json`:
   - `farm_root`
   - optional `workspace_root`
5. Verify pipeline still exists for `run.pipeline_id`.
6. Verify computed `cd_dir` exists for chosen `codex_cd_mode`.
7. For schema failures, check output was deleted and error contains validation details.

## Tests That Define This Contract

- `tests/test_worker.py`
  - worker success path with mocked Codex
  - `codex_cd_mode` routing (`asset_root`, `input_dir`, `input_file_dir`)
- `tests/test_db.py`
  - lease claim behavior (`attempts` increment, running status)
  - task listing and error row fields
- `tests/test_process_smoke.py`
  - multi-worker process flow over queued tasks
- `tests/test_fake_codex_pipeline_pack_demo.py`
  - schema failure path reaching terminal `error` via `process`

## Safe Change Checklist For Future Edits

When modifying this chunk:

1. Preserve atomic lease claim semantics (`BEGIN IMMEDIATE` + update in same transaction).
2. Keep attempts semantics intentional (claim-time increment).
3. Ensure every failure branch clears/handles output file consistently.
4. Keep terminal-vs-retry behavior explicit; do not silently strand tasks in `running`.
5. Re-run worker/db/process tests together, not in isolation.
