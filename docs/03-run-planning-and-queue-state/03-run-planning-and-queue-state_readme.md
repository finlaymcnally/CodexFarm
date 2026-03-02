---
summary: "Run creation, task queue records, and inferred run status state."
read_when:
  - "When changing input enumeration, task rows, run metadata, or status reporting"
---

# 03: Run planning and queue state

This chunk owns the durable planning layer for batch work:

- creating a run record
- turning matched input files into queued or reused-done task rows
- exposing stable introspection commands (`run status`, `run tasks`, `run errors`, `run forensics`)
- inferring canonical run status from task rows

If you are trying to understand "what exists in SQLite before/after workers run", start here.

## Ownership boundary

Owns:

- SQLite schema and lifecycle updates for `runs` and `tasks`
- SQLite schema index ownership for `task_forensics` rows used by `run forensics`
- deterministic input file enumeration for run creation
- input-to-output relative path mapping (`rel_output_path`)
- machine-readable run/task/error inspection output

Does not own:

- pipeline schema/prompt loading (`docs/02-pipeline-assets-and-root-resolution/02-pipeline-assets-and-root-resolution_readme.md`)
- task execution/retry behavior (`docs/04-worker-execution-and-retries/04-worker-execution-and-retries_readme.md`)
- Codex subprocess and schema acceptance gate (`docs/05-codex-exec-and-schema-gate/05-codex-exec-and-schema-gate_readme.md`)

## Primary code files

- `src/codex_farm/cli.py`
- `src/codex_farm/db.py`

Secondary seam file:

- `src/codex_farm/worker.py` (consumes persisted run/task rows, but execution policy belongs to chunk 04)

## Mental model

Think in two records:

1. `runs` row: "batch intent and configuration"
2. `tasks` rows: "one input file = one queue item"

`run create` does planning only. `process` does planning plus worker execution.  
Both planning paths use the same helper: `cli._create_run_for_paths`.

## Planning call path

`run create` and `process` both flow through:

1. `cli._enumerate_inputs(input_dir, glob_pattern)`
2. `run_assets.freeze_run_assets(...)` to persist per-run snapshot under `<data_dir>/run_assets/<run_id>/`
3. `incremental.build_execution_fingerprint(...)` and `incremental.plan_incremental_decisions(...)`
4. `db.create_run(...)` (caller-provided `run_id` + `execution_fingerprint`)
5. `db.insert_planned_tasks_for_run(...)`

Key implementation points:

- input enumeration is sorted and file-only (`Path.glob(...)` + `is_file()`)
- zero matches is a hard error (`typer.BadParameter`)
- all persisted run paths are absolute (`Path.resolve()`)
- run creation freezes effective prompt/schema/pipeline settings and stores `runs.config_json.frozen_assets` so workers can execute deterministically even if live pack files change later.
- incremental planning falls back per-task (reused -> queued) when prior outputs are missing or invalid under current schema, instead of aborting the whole run.

## SQLite schema and field semantics

Defined in `db.init_db`.

### `runs` table

Columns used by this chunk:

- `run_id` (PK, UUID hex)
- `pipeline_id`
- `execution_fingerprint` (compatibility key for safe incremental reuse)
- `status` (`queued|running|paused|done|error|canceled`)
- `control_state` (`active|paused|cancel_requested|canceled`)
- `input_dir`
- `glob_pattern`
- `output_dir`
- `config_json` (JSON string with run-time config that workers later consume)
- `created_at`, `updated_at` (UTC ISO timestamps)

### `tasks` table

Columns used by this chunk:

- `task_id` (PK, UUID hex)
- `run_id` (FK -> runs)
- `input_path` (absolute file path)
- `input_hash` (sha256 of input file at enqueue time)
- `rel_output_path` (POSIX relative path under run output dir)
- `status` (`queued|running|done|error`)
- `reused_from_run_id`, `reused_from_task_id` (nullable provenance for reused-done tasks)
- `attempts` (incremented when leased)
- `execution_attempts` (incremented only when Codex execution actually starts)
- `rate_limit_count` (internal counter incremented when provider throttling requeues a task)
- `leased_by`, `lease_until` (lease metadata)
- `last_heartbeat_at` (most recent owner heartbeat timestamp)
- `error` (latest error message, truncated to 2000 chars by db writers)
- `output_path` (absolute output path for completed tasks)
- `created_at`, `updated_at`

### `run_throttle_state` table

Internal adaptive 429 runtime state keyed by `run_id`:

- `desired_concurrency`
- `concurrency_limit`
- `cooldown_until`
- `last_cooldown_seconds`
- `consecutive_rate_limits`
- `success_streak`
- `last_rate_limit_error`
- `created_at`, `updated_at`

This table is runtime-only. Public run/task JSON contracts are unchanged.

Indexes:

- `(run_id, status)` for status counts/filtering
- `(lease_until)` for lease scanning
- unique `(run_id, input_path)` to prevent duplicate task rows per input file in a run

### `task_forensics` table (evidence index, not queue state)

This table indexes bundle metadata for failed attempts. It does not drive task transitions.

Columns:

- `forensics_id` (PK)
- `source` (`worker|one`)
- `run_id`, `task_id`, `pipeline_id`
- `attempt_index`
- `terminal` (0/1)
- `input_path`, `rel_output_path`
- `failure_stage`, `failure_category`
- `error_summary`
- `bundle_dir`, `metadata_path`, `raw_output_path`
- `created_at`

Indexes:

- `(run_id, created_at)` for run-level listing
- `(run_id, task_id, created_at)` for optional task filter listing

Contract boundary:

- `task_forensics` rows are additive evidence indexes.
- run/task lifecycle remains owned by `runs` + `tasks` state and `run_status` inference.

## Input -> output mapping contract

Task output location is built in two phases:

1. At enqueue time, `db._rel_output_path(...)` stores:
- input path relative to run input root
- with suffix replaced by pipeline `output_ext`
2. At execution time (chunk 04), worker joins:
- `Path(run.output_dir) / task.rel_output_path`

Example:

- input root: `/data/in`
- input file: `/data/in/nested/a.json`
- `output_ext`: `.normalized.json`
- stored `rel_output_path`: `nested/a.normalized.json`

## Run config contract (`runs.config_json`)

Planning writes config in `cli.run_create_command` / `cli.process_command`.

Current keys:

- always: `pipeline`, `in`, `out`, `glob`, `farm_root`
- `process` also includes: `workers`
- always: `heads_up_enabled`, `heads_up_max_tips`
- always for new runs: `frozen_assets` object with:
  - `version`
  - `manifest_relpath` (relative path to `<data_dir>/run_assets/<run_id>/manifest.json`)
- optional: `workspace_root` (only if explicitly provided)
- optional: `codex_model` (only if `--model` override was provided)
- optional: `codex_reasoning_effort` (only if effort override was provided)
- optional: `output_schema_path_override` (only if `--output-schema` override was provided)
- always: `incremental_enabled` (bool)
- always: `incremental_source_run_id` (string or null)

Why this matters:

- workers prioritize persisted `farm_root`/`workspace_root` from `config_json`
- workers also prioritize persisted `codex_model` when present
- workers also prioritize persisted `codex_reasoning_effort` when present
- workers also prioritize persisted `output_schema_path_override` when present
- workers load frozen prompt/schema/pipeline settings from `frozen_assets` when present
- workers use persisted `heads_up_enabled` / `heads_up_max_tips` to keep prompt adaptation deterministic across resumes
- resumed runs keep the same pipeline-pack and `codex --cd` semantics even if caller environment changes

## Prompt-adjustment extension seam

- Prompt text construction still starts at `pipeline_spec.render_prompt_template(...)`, which substitutes `{{INPUT_PATH}}` and `{{INPUT_TEXT}}` deterministically.
- New prompt-adaptation behavior should stay execution-time scoped: persist run-level toggles/hint sources in `runs.config_json` and consume them in worker prompt rendering.
- Heads Up adaptation is intentionally cross-run: workers use persisted run config plus current tips at lease time, but they do not activate tips learned mid-run for the same run.
- Tip scoring stays local and explainable: rank by `(wins + 1) / (uses + 2)` and suppress repeatedly bad tips once `uses >= 8` and `score < 0.25`.
- Post-run learning remains terminal-run only and warning-safe; learner failures must not change run/task exit semantics.
- Keep prompt adaptation out of `tasks` schema; queue identity fields already provide stable task mapping.
- Same-run adaptive hints are non-deterministic under concurrent `process` workers (lease-order dependent), so deterministic adaptation should use prior completed runs.

## State machine (inferred, not blindly trusted)

Source of truth for run status is task rows, computed by `db.run_status`.

### Task-level transitions

Planning:

- enqueue -> `queued`, `attempts=0`

Worker interaction (seam with chunk 04):

- lease -> `running`, `attempts += 1`, `lease_*` set, previous `error` cleared
- success -> `done`, `output_path` set, lease cleared
- retryable failure -> `queued`, lease cleared, `error` set
- terminal failure -> `error`, lease cleared, `error` set

### Run-level inferred status

`db.run_status` computes counts and then infers:

- `queued` when total is 0, or all tasks are queued
- `done` when no queued/running tasks and error count is 0
- `error` when no queued/running tasks and error count > 0
- otherwise `running`

If inferred value differs from stored `runs.status`, DB is updated in place.

Design note:

- `status` and `control_state` intentionally model different concerns: task-derived progress reality vs operator intent (`active|paused|cancel_requested|canceled`).
- Stale-worker safety depends on lease-token ownership checks in `mark_task_done` / `mark_task_error` / `requeue_task`; queue state should treat token mismatches as non-authoritative writes.

## CLI contracts owned by this chunk

### `run create`

CLI: `codex-farm run create --pipeline ... --in ... --out ... [--glob ...] [--json]`  
Contract: creates run + tasks only (no workers started).

JSON output fields:

- `run_id`
- `pipeline_id`
- `input_dir`
- `output_dir`
- `total` (task count)
- `farm_root`
- `workspace_root` (string or `null`)
- `codex_model` (resolved model string)
- `codex_reasoning_effort` (resolved effort string or `null`)
- `output_schema_path` (resolved schema path)
- `heads_up_enabled` (`true|false`)
- `heads_up_max_tips` (int)
- `incremental` object:
  - `enabled`, `source_run_id`, `reused`, `queued`
  - `fallback_counts`: `no_prior_success`, `hash_changed`, `source_output_missing`, `source_output_invalid`

### `run status`

JSON output shape:

- `run_id`
- `pipeline_id`
- `status`
- `control_state`
- `counts`: `queued`, `running`, `done`, `error`, `canceled`, `total`

### `run tasks`

Filter: optional `--status` (`queued|running|done|error|canceled`)  
JSON rows include:

- `input_path`
- `rel_output_path`
- `status`
- `attempts`
- `lease_claims` (explicit alias of `attempts`)
- `execution_attempts`
- `last_heartbeat_at`
- `error`
- `output_path`
- `reused`
- `reused_from_run_id`
- `reused_from_task_id`

### `run errors`

Returns only terminal-error tasks (`status='error'`) with extra lease metadata:

- `task_id`
- `input_path`
- `rel_output_path`
- `attempts`
- `lease_claims`
- `execution_attempts`
- `last_heartbeat_at`
- `error`
- `leased_by`
- `lease_until`
- `updated_at`

### `run forensics`

Filter: required `--run-id`, optional `--task-id`  
JSON rows include:

- `forensics_id`
- `source`
- `run_id`
- `task_id`
- `pipeline_id`
- `attempt_index`
- `terminal`
- `input_path`
- `rel_output_path`
- `failure_stage`
- `failure_category`
- `error_summary`
- `bundle_dir`
- `metadata_path`
- `raw_output_path`
- `created_at`

## Common pitfalls

- Do not treat `runs.status` as independent state; it is derived from task rows.
- Do not mutate JSON output keys casually; integration tests assert these contracts.
- `attempts`/`lease_claims` count lease claims, not only hard failures.
- `execution_attempts` counts real Codex starts; retry budget uses `execution_attempts - rate_limit_count`.
- `rel_output_path` must remain stable/path-safe; downstream worker join logic depends on it.
- `input_hash` alone is not a safe cache key; incremental reuse also requires matching `runs.execution_fingerprint`.
- Do not treat `task_forensics` as queue truth; it is evidence history and must not be used to infer run/task status.

## Fast debugging

Use CLI first (preferred machine contract), not direct SQLite ad-hoc queries:

- `codex-farm run status --run-id <id> --json`
- `codex-farm run tasks --run-id <id> --json`
- `codex-farm run errors --run-id <id> --json`
- `codex-farm run forensics --run-id <id> --json`

When debugging run creation issues:

1. validate input glob matched files
2. inspect `runs.config_json` for expected `farm_root` / `workspace_root`
3. confirm `rel_output_path` values reflect expected tree and extension replacement

## Tests that lock this chunk

- `tests/test_db.py` (`test_task_forensics_insert_and_list` and other planning/state assertions)
- `tests/test_cli_integration_contracts.py` (`test_run_create_json_contract`, `test_run_errors_and_run_tasks_json`, `test_run_forensics_json_contract`)

Cross-chunk confidence (planning + worker):

- `tests/test_process_smoke.py`
- `tests/test_worker.py`

## Change checklist for future AI coders

If you change anything in this chunk:

1. keep run/task JSON contracts backward compatible or update tests/docs together
2. re-check state inference rules in `db.run_status`
3. re-check `rel_output_path` mapping for nested paths and extension swaps
4. update this README and any affected chunk docs (especially 04 if worker-facing behavior changes)

## Task doc merges from `docs/tasks`

Historical task docs merged into this chunk to preserve planning-contract context:

- `Initial-Build.md` (`2026-02-20_12.45.00` revision note):
  - established the core runs/tasks schema shape and lease-aware queue model used in current SQLite planning.
  - established mirror-path output mapping (`<in>/a/b/c.json` -> `<out>/a/b/c.<ext>`), still the basis of `rel_output_path`.
- `Plan-for-recipe-correction.md` (`2026-02-22_12.36.41`):
  - introduced machine-facing run introspection for external callers (`run tasks`, `run errors`) and formalized JSON contract expectations.
  - reinforced persistent run metadata (`farm_root`, optional `workspace_root`) as reproducibility requirements, not optional logging.
- `Plan-for-knowledge-correction.md` (`2026-02-22_13.07.23`):
  - refined error introspection to dedicated error-task payloads so caller diagnostics are stable and operationally useful.
  - reinforced run-config-driven worker determinism for external pack execution.
- `idea1-1.md` (`2026-02-28_20.36.00`):
  - introduced explicit run lifecycle control plane (`pause`, `resume`, `cancel`, `retry-errors`) with distinct `control_state` semantics alongside task-derived `status`.
  - added queue-state extensions (`tasks.status='canceled'`, `runs.control_state`, `tasks.lease_token`) and locked stale-write protection as part of run-state correctness.
  - preserved key operational decision: lifecycle actions are graceful claim gating, not hard-kill subprocess control.
- `idea1-4.md` (`2026-02-28_18.23.00`):
  - added planning-time incremental reuse contract requiring both `input_hash` match and `runs.execution_fingerprint` compatibility.
  - documented mixed-run planning path where reused tasks can be inserted as `done` before worker startup while non-reusable tasks stay `queued`.
  - captured per-task fallback behavior (missing/invalid source outputs downgrade to queued execution) to avoid whole-run aborts.

## Merged discoveries from `docs/understandings`

- `2026-02-22_14.34.04`: `run create` and `process` share one planning seam (`_create_run_for_paths`) that resolves absolute paths, expands sorted file inputs, creates one run row, and enqueues one task row per input.
- `2026-02-22_14.34.04`: The real run state contract is task-derived. `runs.status` is synchronized to inferred task counts; it is not independent truth.
- `2026-02-22_14.34.04`: `runs.config_json` is a queue-planning seam, not just metadata storage. It intentionally persists `farm_root` always and `workspace_root` only when explicitly passed so worker resume semantics stay deterministic.
- `2026-02-28_02.47.41`: `runs.config_json.codex_model` is intentionally optional; when present it makes worker model selection deterministic for resumed runs without mutating pipeline JSON.
- `2026-02-28_02.55.22`: `runs.config_json.codex_reasoning_effort` is intentionally optional; when present it keeps Codex effort selection deterministic for resumed runs.
- `2026-02-28_09.31.02`: `runs.config_json.output_schema_path_override` is intentionally optional; when present it keeps schema validation deterministic for resumed runs without mutating pipeline assets.
- `2026-02-28_09.32.28`: Prompt-adaptation changes should be layered through run config and worker-time rendering, not by mutating queue schema.
- `2026-02-28_09.32.28`: Adaptive hints derived within the same run are lease-order dependent under concurrent workers; deterministic behavior should key off prior completed runs.
- `2026-02-28_09.33.49`: Heads Up adaptation persists as stable run-config fields (`heads_up_enabled`, `heads_up_max_tips`) so queued/resumed runs keep identical prompt-augmentation policy without task-schema changes.
- `2026-02-28_10.12.00`: Heads Up determinism is cross-run by contract; workers read persisted run-level toggles, and tip scoring/hide thresholds stay local (`(wins + 1)/(uses + 2)`, hide when `uses >= 8` and `score < 0.25`).
- `2026-02-28_10.12.00`: Post-run learning remains terminal-only and warning-safe so partial queue states do not contaminate learned guidance.
- `2026-02-28_12.34.52`: run lifecycle now has operator control-state (`active|paused|cancel_requested|canceled`) separate from task-derived effective status; task state adds `canceled`; and stale finalization is guarded by per-lease `lease_token`.
- `2026-02-28_13.18.06`: deterministic runs require snapshot-first execution for snapshot-bearing runs: planning must freeze effective assets at run creation and persist the snapshot pointer, while telemetry may still preserve logical schema identity.
- `2026-02-28_13.22.04`: safe incremental reuse requires both matching `input_hash` and matching `runs.execution_fingerprint`; hash-only reuse is unsafe and must fall back to queued work.
- `2026-02-28_18.46.00`: `task_forensics` is intentionally a side index for bundle lookup and is excluded from run/task state inference so evidence retention changes do not alter queue semantics.
- `2026-02-28_20.38.15`: run status reporting must remain a two-axis contract (task-derived status + control intent), and lease-token ownership checks are the stale-write boundary that keeps reclaimed/canceled tasks from being overwritten by old workers.

Known trap:

- Edits that only touch worker transitions can still alter user-visible run status because `run_status` inference depends on task counts and transition timing.
