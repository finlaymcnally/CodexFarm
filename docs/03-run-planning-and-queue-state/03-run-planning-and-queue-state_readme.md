---
summary: "Run creation, task queue records, and inferred run status state."
read_when:
  - "When changing input enumeration, task rows, run metadata, or status reporting"
---

# 03: Run planning and queue state

This chunk owns the durable planning layer for batch work:

- creating a run record
- turning matched input files into queued task rows
- exposing stable introspection commands (`run status`, `run tasks`, `run errors`)
- inferring canonical run status from task rows

If you are trying to understand "what exists in SQLite before/after workers run", start here.

## Ownership boundary

Owns:

- SQLite schema and lifecycle updates for `runs` and `tasks`
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
2. `db.create_run(...)`
3. `db.enqueue_tasks_for_run(...)`

Key implementation points:

- input enumeration is sorted and file-only (`Path.glob(...)` + `is_file()`)
- zero matches is a hard error (`typer.BadParameter`)
- all persisted run paths are absolute (`Path.resolve()`)

## SQLite schema and field semantics

Defined in `db.init_db`.

### `runs` table

Columns used by this chunk:

- `run_id` (PK, UUID hex)
- `pipeline_id`
- `status` (`queued|running|done|error`)
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
- `attempts` (incremented when leased)
- `leased_by`, `lease_until` (lease metadata)
- `error` (latest error message, truncated to 2000 chars by db writers)
- `output_path` (absolute output path for completed tasks)
- `created_at`, `updated_at`

Indexes:

- `(run_id, status)` for status counts/filtering
- `(lease_until)` for lease scanning
- unique `(run_id, input_path)` to prevent duplicate task rows per input file in a run

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
- optional: `workspace_root` (only if explicitly provided)

Why this matters:

- workers prioritize persisted `farm_root`/`workspace_root` from `config_json`
- resumed runs keep the same pipeline-pack and `codex --cd` semantics even if caller environment changes

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

### `run status`

JSON output shape:

- `run_id`
- `pipeline_id`
- `status`
- `counts`: `queued`, `running`, `done`, `error`, `total`

### `run tasks`

Filter: optional `--status` (`queued|running|done|error`)  
JSON rows include:

- `input_path`
- `rel_output_path`
- `status`
- `attempts`
- `error`
- `output_path`

### `run errors`

Returns only terminal-error tasks (`status='error'`) with extra lease metadata:

- `task_id`
- `input_path`
- `rel_output_path`
- `attempts`
- `error`
- `leased_by`
- `lease_until`
- `updated_at`

## Common pitfalls

- Do not treat `runs.status` as independent state; it is derived from task rows.
- Do not mutate JSON output keys casually; integration tests assert these contracts.
- `attempts` counts leases, not only hard failures.
- `rel_output_path` must remain stable/path-safe; downstream worker join logic depends on it.
- `input_hash` is currently persisted but not used for dedupe or staleness checks yet.

## Fast debugging

Use CLI first (preferred machine contract), not direct SQLite ad-hoc queries:

- `codex-farm run status --run-id <id> --json`
- `codex-farm run tasks --run-id <id> --json`
- `codex-farm run errors --run-id <id> --json`

When debugging run creation issues:

1. validate input glob matched files
2. inspect `runs.config_json` for expected `farm_root` / `workspace_root`
3. confirm `rel_output_path` values reflect expected tree and extension replacement

## Tests that lock this chunk

- `tests/test_db.py`
- `tests/test_cli_integration_contracts.py` (`test_run_create_json_contract`, `test_run_errors_and_run_tasks_json`)

Cross-chunk confidence (planning + worker):

- `tests/test_process_smoke.py`
- `tests/test_worker.py`

## Change checklist for future AI coders

If you change anything in this chunk:

1. keep run/task JSON contracts backward compatible or update tests/docs together
2. re-check state inference rules in `db.run_status`
3. re-check `rel_output_path` mapping for nested paths and extension swaps
4. update this README and any affected chunk docs (especially 04 if worker-facing behavior changes)
