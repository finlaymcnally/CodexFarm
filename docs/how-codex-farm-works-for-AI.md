---
summary: "Deep technical reference for AI agents: command contracts, internals, state transitions, and debugging strategy."
read_when:
  - "When an AI agent must reason about codex-farm behavior without direct source-code access"
---

# codex-farm for AI agents (high-fidelity internal model)

This document is intentionally detailed. It is written for an AI assistant that cannot open the repository files, but still needs enough fidelity to:

- plan features
- debug failures
- reason about side effects and invariants
- avoid breaking hidden contracts

If you only need a quick overview, use `docs/how-codex-farm-works.md`. This file is the deep dive.

## 1) Product shape and hard boundaries

codex-farm is a **local CLI orchestrator** for running many `codex exec` tasks over files.

It is not:

- a web server
- a remote worker system
- a model runtime
- an API-key-based service

It shells out to an installed `codex` CLI and manages queueing + retries locally via SQLite.

Core design intent:

- Data-driven pipelines, not hard-coded transforms.
- Strong output contract via JSON Schema.
- Safe retries/resume with task leasing.
- Human-friendly mode (`go`) and script mode (`process`).

## 2) Runtime architecture in one mental model

Think in three layers:

1. **Pipeline config layer**
   Files in `pipelines/` define prompt path, schema path, model flags, and defaults.

2. **Run/task queue layer**
   SQLite tables represent runs and file-level tasks with leasing metadata.

3. **Execution layer**
   Worker loops lease tasks, run Codex, validate output, and transition task status.

Top-level flow for folder processing:

1. `process` creates run + task rows.
2. `process` starts N thread-based workers in the same process.
3. Each worker loops: lease -> execute -> validate -> mark done/requeue/error.
4. CLI polls run status and prints counts.
5. Exit code reflects worker failures or task errors.

## 3) Repository structure and functional responsibilities

Main implementation modules:

- `src/codex_farm/cli.py`
  CLI surface and orchestration glue.
- `src/codex_farm/pipeline_spec.py`
  Pipeline JSON validation/loading and prompt rendering.
- `src/codex_farm/paths.py`
  Repo root and data-dir path resolution.
- `src/codex_farm/db.py`
  SQLite schema, leasing, task/run state updates.
- `src/codex_farm/codex_exec.py`
  Subprocess wrapper for `codex exec`.
- `src/codex_farm/worker.py`
  Worker loop with retry/error handling.
- `src/codex_farm/schema_utils.py`
  Local JSON + JSON Schema validation.
- `src/codex_farm/doctor.py`
  Prerequisite checks.

Asset/config folders:

- `pipelines/` one JSON per operation
- `prompts/` prompt templates with `{{INPUT_PATH}}`
- `schemas/` JSON Schemas for Codex constrained output and local validation
- `examples/` sample fixtures

Persistent runtime data:

- default data dir: `./var` (resolved to absolute path)
- database: `<data_dir>/codex_farm.sqlite3`
- interactive input: `<data_dir>/inbox/`
- interactive output: `<data_dir>/outbox/<pipeline_id>/<timestamp>/`

## 4) CLI contract: commands, parameters, defaults, exit behavior

### `doctor`

Purpose: verify local prerequisites.

Checks:

- Python version >= 3.11
- `codex` exists on PATH and `codex --version` succeeds
- non-interactive smoke check using:
  - `codex --ask-for-approval never exec --skip-git-repo-check --sandbox read-only --model gpt-5.3-codex-spark "Reply with exactly: OK"`

Success rule for smoke check:

- return code 0 OR exact `OK` line in stdout

Why this rule exists:

- Codex can emit warning/telemetry errors after producing usable output.

Exit:

- 0 if all checks pass
- 1 if any check fails

### `init --data-dir ./var`

Creates directories and initializes DB schema:

- data dir
- `inbox/`
- `outbox/`
- SQLite tables/indexes

Exit:

- 0 on success

### `pipelines list [--root <pack>] [--json]`

Loads all pipeline specs and prints:

- `pipeline_id`
- `description`

`--json` prints a JSON array of objects.

### `pipelines new --pipeline-id <id> [--root <pack>]`

Scaffolds 3 files (fails if any exists):

- `pipelines/<id>.json`
- `prompts/<id with dots -> underscores>.txt`
- `schemas/<id with dots -> underscores>.schema.json`

Generated defaults include model/sandbox/timeout, `codex_cd_mode: "asset_root"`, and a permissive placeholder schema.

### `one --pipeline <id> --in <file> --out <file> [--root <pack>] [--workspace-root <dir>]`

Single-file processing path:

1. resolve pipeline
2. render prompt from template (`{{INPUT_PATH}}`)
3. resolve `--cd`:
   - if `--workspace-root` is set, use it
   - else use pipeline `codex_cd_mode`
   - for `one`, both `input_dir` and `input_file_dir` map to the input file parent
4. call Codex wrapper with the resolved `--cd`
5. local schema validate
6. delete output and fail if invalid

Exit:

- 0 if output written and valid
- 1 on timeout/Codex error/schema failure

### `run create --pipeline <id> --in <dir> --out <dir> [--glob "**/*.json"] [--data-dir ./var] [--root <pack>] [--workspace-root <dir>] [--json]`

Creates run + enqueues one task per matching file. Does not run workers. Persisted `config_json` always includes absolute `farm_root` and includes `workspace_root` only when explicitly provided.

If no files match, raises parameter error.

Output path mapping preserves relative tree:

- input `<in>/nested/a.json` -> task rel output `nested/a.json` (or replaced suffix if pipeline extension differs)

### `run status --run-id <id> [--data-dir ./var] [--json]`

Computes counts from task rows and inferred run state:

- JSON shape is `{run_id, pipeline_id, status, counts:{queued,running,done,error,total}}`

### `run tasks --run-id <id> [--status queued|running|done|error] [--data-dir ./var] [--json]`

Lists task rows for a run. JSON includes:

- `input_path`
- `rel_output_path`
- `status`
- `attempts`
- `error`
- `output_path`

### `run errors --run-id <id> [--data-dir ./var] [--json]`

Returns only error tasks with fields:

- `task_id`
- `input_path`
- `rel_output_path`
- `attempts`
- `error`
- `leased_by`
- `lease_until`
- `updated_at`

### `worker [--data-dir ./var] [--worker-id ""] [--run-id <id>] [--lease-seconds 300] [--max-attempts 3] [--poll-seconds 1.0] [--once] [--root <pack>]`

Runs worker loop directly.

Defaults:

- random worker id when omitted
- lease 300s
- max attempts 3
- poll interval 1s
- endless loop unless `--once`

Behavior:

- with `--once`, exits when no task is available
- without `--once`, sleeps/polls forever

Exit:

- 0 if no terminal errors hit
- 1 if any task is marked terminal error during this worker’s run

### `process --pipeline <id> --in <dir> --out <dir> [--workers 8] [--data-dir ./var] [--glob ""] [--lease-seconds 300] [--max-attempts 3] [--root <pack>] [--workspace-root <dir>] [--json]`

End-to-end batch mode:

1. create run + tasks
2. start N worker threads (`ThreadPoolExecutor`) each with `once=True`
3. poll status every second
4. wait for all workers
5. print summary (JSON/text)
6. exit non-zero if any worker exit !=0 or run has errors

`--glob ""` means use pipeline default glob.

`--json` contract:

- stdout: one JSON object `{run_id, pipeline_id, status, counts, input_dir, output_dir, farm_root, workspace_root, worker_exit_codes, exit_code}`
- `workspace_root` is `null` unless explicitly set via `--workspace-root`
- stderr: progress lines

### `go [--data-dir ./var] [--root <pack>] [--workspace-root <dir>]`

Interactive inbox/outbox mode:

1. initialize data dir and DB
2. list pipelines
3. prompt for pipeline index
4. prompt for worker count (default 8)
5. input dir fixed to `<data_dir>/inbox`
6. output dir set to `<data_dir>/outbox/<pipeline_id>/<timestamp>`
7. run same worker orchestration as `process`

Timestamp format:

- `%Y-%m-%d_%H.%M.%S`

## 5) Pipeline configuration contract (exact fields)

On-disk pipeline JSON must provide fields accepted by `PipelineSpecModel`.

Effective schema:

- `pipeline_id: str` (required, non-empty)
- `description: str` (required, non-empty)
- `prompt_template_path: str` (required, repo-relative path to existing file)
- `output_schema_path: str` (required, repo-relative path to existing file)
- `input_glob_default: str` (default `"**/*.json"`)
- `output_ext: str` (default `".json"`, must start with `.`)
- `codex_model: str` (default `"gpt-5.3-codex-spark"`)
- `codex_sandbox: str` (default `"read-only"`)
- `codex_ask_for_approval: str` (default `"never"`)
- `codex_web_search: str` (default `"disabled"`)
- `codex_timeout_seconds: int` (default `180`, must be >=1)
- `codex_cd_mode: Literal["asset_root", "input_dir", "input_file_dir"]` (default `"asset_root"`)

Validation behavior:

- extra keys are rejected (`extra="forbid"`)
- missing referenced prompt/schema files cause loader failure
- duplicate `pipeline_id` across files causes loader failure

Runtime object:

- dataclass `PipelineSpec` with the same fields, but prompt/schema are resolved absolute `Path` values.

## 6) Prompt rendering behavior

Prompt templates are plain text files.

Substitution rule:

- replace every literal `{{INPUT_PATH}}` with absolute resolved input path string

No other templating exists (no conditional logic, no escaping, no variable map).

AI implication:

- if you add new placeholders, code must be changed; currently only `INPUT_PATH` is supported.

## 7) Path resolution and root discovery

Asset root detection relies on the existence of all sentinel folders:

- `pipelines`
- `prompts`
- `schemas`

Resolution algorithm:

1. if `--root` is provided, use it
2. else if `CODEX_FARM_ROOT` is set, use it
3. else search from cwd upward
4. else search from module file path upward
5. else raise `FileNotFoundError`

Important consequences:

- missing/renamed sentinel folder breaks most commands even if code exists.
- worker resume behavior depends on persisted run config:
  - `farm_root` is always read from `runs.config_json` first when present
  - `workspace_root` overrides pipeline `codex_cd_mode` only when it exists in `runs.config_json`

## 8) SQLite schema and queue mechanics

DB settings on connect:

- WAL mode enabled
- foreign keys enabled
- busy timeout 5000ms
- row factory: dict-like rows (`sqlite3.Row`)

### `runs` table (logical model)

- `run_id TEXT PRIMARY KEY`
- `pipeline_id TEXT NOT NULL`
- `created_at TEXT NOT NULL` (UTC ISO string)
- `updated_at TEXT NOT NULL` (UTC ISO string)
- `status TEXT NOT NULL`
- `input_dir TEXT NOT NULL`
- `glob_pattern TEXT NOT NULL`
- `output_dir TEXT NOT NULL`
- `config_json TEXT NOT NULL` (serialized request config)

### `tasks` table (logical model)

- `task_id TEXT PRIMARY KEY`
- `run_id TEXT NOT NULL` FK -> `runs(run_id)` cascade delete
- `input_path TEXT NOT NULL` absolute path
- `input_hash TEXT NOT NULL` SHA-256 of input bytes at enqueue time
- `rel_output_path TEXT NOT NULL` output path relative to run output root
- `status TEXT NOT NULL` (`queued|running|done|error`)
- `attempts INTEGER NOT NULL DEFAULT 0`
- `leased_by TEXT NULL`
- `lease_until REAL NULL` (epoch seconds)
- `error TEXT NULL` truncated to 2000 chars on write
- `output_path TEXT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

Indexes:

- `(run_id, status)`
- `(lease_until)`
- unique `(run_id, input_path)`

### Task enqueue logic

For each input file:

1. compute `rel_output_path` via `input_file.relative_to(input_root).with_suffix(output_ext)`
2. hash file contents (SHA-256)
3. insert queued task with attempts 0

Note:

- output root is passed into enqueue but not currently used in row generation.

## 9) Leasing algorithm and concurrency guarantees

Leasing function:

- `lease_one_task(conn, worker_id, lease_seconds, run_id|None)`

Algorithm:

1. `BEGIN IMMEDIATE` transaction
2. select one task where:
   - status is `queued`, OR
   - status is `running` with expired lease (`lease_until < now`)
   - optionally filtered by `run_id`
3. priority:
   - queued before expired-running
   - oldest `updated_at` first
4. update selected task:
   - `status='running'`
   - `attempts = attempts + 1`
   - `leased_by = worker_id`
   - `lease_until = now + lease_seconds`
   - `error = NULL`
5. update parent run status to `running`
6. commit and return claimed row

If no task eligible:

- commit and return `None`

Concurrency property:

- `BEGIN IMMEDIATE` ensures only one writer acquires claim/update at a time on that DB.
- claim is atomic with attempt increment.

## 10) Task and run state machines

### Task states

Nominal transitions:

- `queued -> running` (lease claim)
- `running -> done` (successful Codex + schema validation)
- `running -> queued` (recoverable failure and attempts below limit)
- `running -> error` (failure at/over max attempts)

Lease-expiry recovery:

- `running (expired lease) -> running` by another worker claim
- this increments attempts again

Important nuance:

- attempts count increases on claim, not on completion. A stuck worker that lets leases expire can consume attempt budget.

### Run states (inferred)

Run status is not only "set once"; it is recomputed from tasks by `run_status()`:

- if `total == 0`: `queued`
- else if all tasks queued: `queued`
- else if queued == 0 and running == 0:
  - if any errors: `error`
  - else: `done`
- else: `running`

When inferred status changes, it is persisted back to runs table.

## 11) Worker loop internals (exact behavior)

Worker pre-loads:

- pipeline cache keyed by resolved `farm_root`
- SQLite connection

Loop body:

1. lease one task
2. if none:
   - if `once=True`: return current exit code
   - else sleep `poll_seconds` and continue
3. if `task.attempts > max_attempts`: mark task error immediately
4. fetch run row and parse `runs.config_json`
5. resolve `farm_root`:
   - use config `farm_root` when present
   - else use worker `--root` (if provided)
   - else fallback discovery
6. load pipeline map from `<farm_root>/pipelines` (cached per root)
7. resolve optional explicit `workspace_root` override from config
8. look up pipeline by `run.pipeline_id`
9. build:
   - `input_path = task.input_path`
   - `output_path = run.output_dir / task.rel_output_path`
10. resolve Codex `--cd`:
    - explicit `workspace_root` override when present
    - otherwise pipeline `codex_cd_mode` (`asset_root`, `input_dir`, `input_file_dir`)
11. if computed `--cd` does not exist, mark task `error` immediately (no retry)
12. render prompt with template + input path
13. run Codex wrapper
14. if wrapper says failure -> raise runtime error
15. local schema validate output
16. mark task done with output path

Error handling classes:

- `CodexExecTimeoutError`
- `SchemaValidationError`
- `RuntimeError` (including non-OK Codex returns)
- generic `Exception` fallback

On handled error:

1. delete output file if present
2. trim error text to <= 1800 chars
3. if attempts >= max_attempts: mark `error`, set worker exit_code=1
4. else: `requeue_task`

Returned worker exit code semantics:

- 1 means at least one terminal task failure observed by this worker instance
- 0 otherwise

## 12) Codex subprocess wrapper: exact command and accept/reject logic

Wrapper function signature:

- `run_codex_exec(cd_dir, prompt, model, sandbox, ask_for_approval, web_search, output_schema, output_path, timeout_seconds)`

Generated command:

1. `codex`
2. `--ask-for-approval <value>` (global, before `exec`)
3. `exec`
4. `--cd <resolved cd_dir>`
5. `--skip-git-repo-check`
6. `--model <model>`
7. `--sandbox <sandbox>`
8. `--config web_search=<value>`
9. `--output-schema <absolute schema path>`
10. `--output-last-message <temp output file>`
11. `<prompt text>`

I/O strategy:

- create temp file in output directory
- run subprocess with captured stdout/stderr and timeout
- check whether temp output exists and is non-empty

Result decision:

- if return code !=0 and no payload file: failure
- if payload file missing/empty even with return code 0: failure
- otherwise success (including non-zero exit with non-empty payload)

Success write:

- atomic `os.replace(temp_output, final_output)`

Timeout:

- delete temp output if present
- raise `CodexExecTimeoutError("codex exec timed out after Xs")`

Why this design matters:

- avoids partial output files
- tolerates known Codex shutdown-noise failure mode
- delegates semantic correctness to local schema validation

## 13) Schema validation layer

`validate_json_file_against_schema(json_path, schema_path)`:

1. parse both JSON files
2. validate with `jsonschema.Draft202012Validator`
3. collect and sort errors
4. raise on first error with path-aware message

Typical failure message shape:

- `Schema validation failed at <path>: <message>`

If JSON is malformed:

- `Invalid JSON at <path>: <decode error>`

Use in pipeline:

- called after Codex output for both `one` and worker flow
- invalid output is deleted before failure/retry

## 14) Process orchestration and parallelism details

`process` uses `ThreadPoolExecutor` inside one Python process.

Each thread runs `worker_loop(... once=True)` and opens its own SQLite connection.

Main thread behavior:

1. while any worker futures unfinished:
   - query `run_status`
   - print one progress line per second
2. after completion:
   - collect worker exit codes
   - query final run status
   - compute combined exit:
     - non-zero if any worker non-zero OR final error count >0

Implications:

- No daemon is required.
- No subprocess worker management overhead in v1.
- DB is the shared coordination boundary.

## 15) Debugging playbook (symptom -> likely root cause -> checks)

### Symptom: "Unknown pipeline '<id>'"

Likely cause:

- typo or missing pipeline JSON

Checks:

1. `codex-farm pipelines list`
2. verify pipeline file under `pipelines/`
3. validate JSON fields and references

### Symptom: pipeline load fails with file reference error

Likely cause:

- `prompt_template_path` or `output_schema_path` points to non-existent file

Checks:

1. ensure path is repo-relative
2. verify target file exists

### Symptom: `process` creates run then tasks go `error`

Likely causes:

- Codex command failure
- schema mismatch
- max attempts reached after retries

Checks:

1. `codex-farm run status --run-id <id> --json`
2. inspect `tasks.error` in SQLite for task messages
3. run one failed file through `codex-farm one` for faster repro
4. verify schema matches expected Codex output shape

### Symptom: non-zero Codex exit but some outputs still produced

Expected behavior in this project:

- non-zero exits can still be accepted if payload file exists and is non-empty

Check:

- ensure downstream local schema validation passed

### Symptom: tasks stuck as `running`

Possible causes:

- worker process/thread crashed
- lease timeout too long relative to failure pattern

Checks:

1. compare `lease_until` vs current time
2. wait for lease expiry; tasks should become eligible again
3. adjust `--lease-seconds` if needed

### Symptom: all tasks fail with schema errors after a schema edit

Likely cause:

- Codex structured output constraints or prompt/schema mismatch

Important known rule:

- Codex `--output-schema` currently expects every key in `properties` to also appear in `required`.
- represent optional fields as nullable required fields.

### Symptom: `doctor` fails in non-git directory

Fix:

- ensure `--skip-git-repo-check` is present in Codex command path (it is required for this environment)

### Symptom: repo root not found

Likely cause:

- sentinel folders missing/renamed

Fix:

- restore `pipelines/`, `prompts/`, `schemas/`
- or set `CODEX_FARM_ROOT` correctly

## 16) SQL-level forensic queries (manual debugging)

When direct DB inspection is needed, useful query patterns:

1. Run overview:
   - select run metadata + status from `runs` by `run_id`
2. Task counts:
   - group tasks by `status` for a run
3. Failures:
   - select `task_id,input_path,attempts,error` where status=`error`
4. Lease visibility:
   - select `task_id,status,leased_by,lease_until,updated_at` where status=`running`

Interpretation rules:

- expired `lease_until` on running task means task can be reclaimed.
- high attempts with repeated schema errors points to prompt/schema incompatibility, not transient runtime.

## 17) Test coverage map and current confidence boundaries

Automated tests currently cover:

- pipeline loading and prompt placeholder replacement
- DB lifecycle (run create, enqueue, lease, done, status counts)
- worker loop with mocked Codex call
- `process` command smoke with multiple mocked workers
- CLI pipeline scaffold generation
- recipeimport schema examples validation

High-confidence areas:

- queue/lease/retry mechanics
- CLI orchestration shape
- schema validation plumbing

Lower-confidence / integration-sensitive areas:

- real Codex subprocess behavior across versions
- interactive `go` input paths under unusual shells
- long-running lease-expiry behavior under heavy failures

## 18) Hidden invariants and "do not break" rules

These are critical for behavior fidelity:

- approval mode must be global flag:
  - `codex --ask-for-approval ... exec ...`
- always include:
  - `--skip-git-repo-check`
- never mark task done before atomic output replace + schema validation
- run/task logic assumes output files are disposable on failed attempts
- pipeline behavior should stay data-driven via `pipelines/*.json`
- `recipe.schemaorg.normalize.v1` expects `recipeInstructions` as HowToStep objects

## 19) Feature-planning decision framework for future AI agents

When asked to add a feature, first classify scope:

1. **Pipeline-only feature**
   Add/change files in `pipelines/`, `prompts/`, `schemas/`.
   No queue/orchestrator code needed.

2. **Execution-policy feature**
   Modify worker loop, retry policy, lease policy, or Codex wrapper.
   Requires careful state-machine reasoning and docs updates.

3. **CLI UX feature**
   Add command/options in `cli.py`.
   Keep existing command contracts stable unless intentionally versioned.

4. **Data model feature**
   Add DB columns/tables with migration-safe logic.
   Must preserve existing run/task semantics.

Recommended planning sequence:

1. define user-facing behavior and CLI/API contract
2. map affected state transitions
3. decide whether failure paths should requeue or mark error
4. define test plan with mocked Codex where possible
5. update docs + conventions if hidden rules change

## 20) Common extension patterns and pitfalls

### Adding a new pipeline

Safe pattern:

1. create schema first
2. write prompt tightly to schema
3. add pipeline JSON
4. run `pipelines list`
5. smoke with `one`, then batch with `process`

Pitfall:

- adding optional schema properties without nullable+required pattern can break Codex structured output.

### Changing retry/attempt behavior

Safe pattern:

1. model transitions explicitly (`queued/running/done/error`)
2. preserve atomic lease claim semantics
3. verify no branch leaves task indefinitely `running` without lease refresh path

Pitfall:

- moving attempt increment to wrong phase can break max-attempt guarantees.

### Changing output path strategy

Safe pattern:

1. update task `rel_output_path` generation
2. ensure deterministic mapping from input path
3. ensure worker and tests align

Pitfall:

- non-deterministic mapping breaks idempotent reasoning and output lookup.

### Introducing subprocess workers instead of threads

Safe pattern:

1. keep DB leasing as source of truth
2. preserve `once=True` semantics in orchestration
3. keep final exit aggregation equivalent

Pitfall:

- duplicating scheduling logic outside DB can reintroduce double-processing races.

## 21) Known quality debt and opportunities (for planners)

Potential improvements an AI planner might prioritize:

- Add tests for `codex_exec.py` edge cases (non-zero with payload, empty payload, timeout cleanup).
- Add tests for `doctor` behavior and OK-line fallback.
- Add richer status/progress output options (quiet mode, JSON streaming, interval config).
- Add structured error codes for failure classification.
- Add optional stale-task recovery tooling/reporting command.
- Add explicit DB migrations/versioning if schema evolves.

These are opportunities, not current blockers.

## 22) Minimal reproducible scenario templates

### Fast single-file repro

Use when debugging prompt/schema/Codex interaction only:

- run `one` on one known input file
- inspect output and schema errors

### Queue/retry repro

Use when debugging worker/state transitions:

1. create small input dir with 2-3 files
2. run `process --workers 2 --max-attempts 2`
3. inspect run status + task rows
4. intentionally force failure (bad schema) to observe retry -> error path

### Interactive path repro

Use when debugging UX:

1. `init`
2. place files into `inbox`
3. run `go`
4. verify output dir timestamping and run completion

## 23) If you are an AI agent proposing code changes

Before touching code, preserve these assumptions unless explicitly changing them:

- data-driven pipeline model
- thread-based process orchestration in v1
- SQLite lease as concurrency guard
- atomic output file finalization
- two-step output contract (Codex constrained generation + local JSON Schema validation)

When you intentionally change one of these, mark it as an architectural change and update convention docs accordingly.
