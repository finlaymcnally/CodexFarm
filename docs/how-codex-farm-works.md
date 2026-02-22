---
summary: "In-depth explanation of codex-farm architecture, command flow, and runtime behavior."
read_when:
  - "When you want to understand exactly how codex-farm processes files end to end"
---

# How codex-farm works

This project is a local orchestrator around `codex exec`. It does not run a model itself. It coordinates three things:

1. Pipeline definitions (`pipelines/*.json`) that declare prompt/schema/runtime settings.
2. A SQLite-backed run/task queue (`codex_farm.sqlite3` in your data dir).
3. Worker loops that lease tasks, call Codex, validate output, and mark task state.

The result is a repeatable "folder in -> folder out" workflow with retries and progress tracking.

## 1) What the pipeline files control

Every pipeline JSON maps a stable `pipeline_id` to:

- prompt template path (`prompts/*.txt`)
- output schema path (`schemas/*.schema.json`)
- Codex runtime flags (model, sandbox, approval mode, web search, timeout)
- default input glob and output extension

`src/codex_farm/pipeline_spec.py` validates these files with Pydantic (`extra="forbid"`), resolves prompt/schema paths against the selected asset root, and refuses invalid references early.

At runtime, prompt rendering is simple string substitution:

- `{{INPUT_PATH}}` -> absolute input file path

So each task gets the same template plus a different input path.

## 2) Asset root and data dir resolution

`src/codex_farm/paths.py` resolves the asset root by locating folders `pipelines/`, `prompts/`, and `schemas/`.

Resolution order:

1. `--root` CLI option (if provided)
2. `CODEX_FARM_ROOT` env var
3. current working directory (and parents)
4. module file location (and parents)

The selected root must contain the three sentinel folders directly. If one is missing, codex-farm fails with a clear message.

Codex working directory is a separate setting: `--workspace-root` controls the `codex exec --cd` path. If omitted, it defaults to the resolved asset root.

Data dir is always resolved to an absolute path. DB path is:

- `<data_dir>/codex_farm.sqlite3`

## 3) Command architecture in `cli.py`

Main command groups:

- top-level: `doctor`, `init`, `one`, `worker`, `process`, `go`
- subcommands: `pipelines list`, `pipelines new`
- run lifecycle: `run create`, `run status`, `run tasks`, `run errors`

### `doctor`

Checks:

- Python >= 3.11
- `codex` exists on PATH
- non-interactive Codex smoke call works

The smoke check intentionally treats either of these as success:

- return code 0
- exact `OK` line in stdout

This guards against the known Codex behavior where warnings can cause non-zero exit even when usable output is produced.

### `init`

Creates:

- data dir
- `inbox/`
- `outbox/`
- SQLite schema

### `one`

Single file processing path:

1. load selected pipeline
2. render prompt with `{{INPUT_PATH}}`
3. call `run_codex_exec(...)` with `--workspace-root` (or asset root by default)
4. validate output JSON against schema
5. delete bad output and fail if validation fails

### `run create`

Run setup only (no workers):

1. enumerate input files by glob
2. create one run row
3. enqueue one task row per file

`runs.config_json` persists `farm_root` and `workspace_root` so resumed workers keep the same roots.

Output path strategy mirrors input structure:

- input `<in>/a/b/c.json`
- output `<out>/a/b/c.json` (or with pipeline extension override)

### `process`

Batch execution path:

1. create run + tasks (`run create` logic)
2. start `N` in-process worker threads (`ThreadPoolExecutor`)
3. each thread runs `worker_loop(..., once=True)` until queue empty
4. poll and print run status every second
5. exit non-zero if any worker failed or final run has errors

With `--json`, stdout is a single machine-readable JSON object; progress lines are sent to stderr.

### `run tasks` and `run errors`

These commands expose per-task status without querying SQLite directly.

- `run tasks --run-id ... [--status ...] --json` returns task objects with `input_path`, `rel_output_path`, `status`, `attempts`, `error`, and `output_path`.
- `run errors --run-id ... --json` returns only tasks in terminal `error` state.

### `go`

Interactive wrapper around `process`:

1. list pipelines
2. prompt pipeline selection + worker count
3. use `<data_dir>/inbox` as input
4. write output to `<data_dir>/outbox/<pipeline_id>/<timestamp>/`
5. run workers to completion

## 4) SQLite data model and task leasing

`src/codex_farm/db.py` owns run/task state.

### `runs` table

Stores run metadata:

- `run_id`, `pipeline_id`, timestamps, status
- input dir, glob pattern, output dir
- serialized config JSON for reproducibility (`farm_root`, `workspace_root`, and run options)

### `tasks` table

One row per input file:

- `task_id`, `run_id`
- `input_path`, `input_hash`
- `rel_output_path`
- `status`, `attempts`
- lease metadata (`leased_by`, `lease_until`)
- error/output path

### Leasing strategy

`lease_one_task(...)` uses `BEGIN IMMEDIATE` and then:

1. picks one eligible task:
   - queued, or
   - running with expired lease
2. atomically sets:
   - `status='running'`
   - `attempts = attempts + 1`
   - lease metadata
3. updates run status to `running`

This transaction boundary is the concurrency guard that prevents duplicate claims across workers.

## 5) Worker lifecycle

`src/codex_farm/worker.py` loops:

1. lease task
2. load run + pipeline using persisted `farm_root` (or worker fallback root)
3. derive absolute input/output paths
4. render prompt
5. call Codex wrapper using persisted `workspace_root` (or `farm_root` fallback)
6. schema-validate output
7. mark done, or requeue/error

Retry behavior:

- if failure and `attempts < max_attempts`: task is requeued
- if failure and `attempts >= max_attempts`: task marked `error`

Any failed/invalid output file is deleted before retry/error marking.

## 6) Codex invocation contract

`src/codex_farm/codex_exec.py` builds this shape:

- `codex --ask-for-approval <mode> exec ...`
- `--skip-git-repo-check`
- `--sandbox <pipeline setting>`
- `--config web_search=<pipeline setting>`
- `--output-schema <schema>`
- `--output-last-message <temp file>`
- `--cd <workspace_root>`

Important behavior:

- output is written to a temporary file in destination directory
- success path does atomic `os.replace(temp, final)`
- timeout raises `CodexExecTimeoutError`
- non-zero exit is accepted if temp output exists and is non-empty
- final accept/reject still depends on local schema validation

This is why codex-farm remains resilient to non-fatal Codex exit noise while still enforcing strict output contracts.

## 7) Run status inference

`run_status(...)` computes counts from tasks (`queued/running/done/error`) and infers run status:

- all queued -> `queued`
- no queued and no running:
  - if any error -> `error`
  - else -> `done`
- otherwise -> `running`

So run status is derived from current task truth, not only from prior write-time intent.

## 8) Validation layers

There are two schema gates:

1. Codex constrained output (`--output-schema`) during generation.
2. Local validation with `jsonschema` after generation.

Local validation is the final authority; if it fails, output is removed and task is retried/fails by attempt policy.

## 9) Testing strategy

Tests focus on orchestration logic without requiring live Codex calls:

- `test_pipeline_spec.py`: pipeline loading + prompt render
- `test_db.py`: run/task lifecycle + leasing basics
- `test_worker.py`: worker task processing with mocked Codex
- `test_process_smoke.py`: multi-worker process flow with mocked Codex
- `test_recipeimport_schemas.py`: validates real example payloads against recipeimport schemas
- `test_cli_scaffold.py`: pipeline scaffold command output files
- `test_cli_integration_contracts.py`: `--root`, `--workspace-root`, JSON output contracts, and run task/error exports

This keeps unit/smoke tests deterministic while still exercising core flow.

## 10) Practical extension points

To add a new operation, you usually only need:

1. new pipeline JSON in `pipelines/`
2. prompt template in `prompts/`
3. schema in `schemas/`

Because queue/worker/CLI execution is pipeline-driven, no new orchestration code is required for most transforms.
