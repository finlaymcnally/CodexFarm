---
summary: "User-facing CLI entrypoints, command contracts, JSON payloads, and orchestration glue in cli.py."
read_when:
  - "When changing commands, options, JSON payloads, or go mode prompts"
  - "When debugging behavior that starts at codex-farm CLI and fans out into DB/worker/codex code"
---

# Scope

This chunk owns the user-facing `codex-farm` command surface.

Primary ownership:

- Command names and option shapes.
- Text vs JSON output contracts.
- Exit code policy at the CLI boundary.
- Top-level orchestration for `process` and `go`.
- Analytics export command surface (`stats-dashboard`).

Primary file:

- `src/codex_farm/cli.py`

This chunk is mostly a contract/orchestration layer. It delegates deeper behavior to other chunks:

- pipeline/root loading: `src/codex_farm/pipeline_spec.py`, `src/codex_farm/paths.py`
- queue state: `src/codex_farm/db.py`
- worker retry/runtime logic: `src/codex_farm/worker.py`
- codex subprocess + schema gate: `src/codex_farm/codex_exec.py`, `src/codex_farm/schema_utils.py`

# 30-second mental model

`codex-farm` is a Typer app with this command tree:

- `doctor`
- `init`
- `one`
- `stats-dashboard`
- `worker`
- `process`
- `go`
- `pipelines list`
- `pipelines new`
- `run create`
- `run status`
- `run tasks`
- `run errors`

Most commands do 3 things:

1. Resolve roots and validate flags.
2. Call lower-level modules.
3. Normalize user-visible output and exit code behavior.

# Shared helper contracts in `cli.py`

These helpers shape behavior across multiple commands:

- `_resolve_farm_root_or_die(root)`
- calls `resolve_farm_root(...)` and rewrites `FileNotFoundError` to `typer.BadParameter`.
- result: root problems show up as CLI argument errors, not stack traces.

- `_resolve_workspace_root_override_or_die(workspace_root)`
- returns `None` if not provided.
- otherwise requires an existing directory; else raises `typer.BadParameter`.

- `_resolve_one_cd_dir(...)`
- for `one`, `--workspace-root` wins.
- without override:
- `codex_cd_mode=asset_root` -> farm root.
- `codex_cd_mode=input_dir|input_file_dir` -> input file parent.
- requires computed directory to exist.

- `_init_data_dir(data_dir)`
- creates `data_dir`, `inbox/`, `outbox/`.
- opens DB at `<data_dir>/codex_farm.sqlite3` and runs `init_db(...)`.

- `_create_run_for_paths(...)`
- globs files and fails fast if no files matched.
- creates run row and enqueues one task per input.

- `_run_workers(...)`
- starts `workers` threads, each `worker_loop(... once=True)`.
- polls run status and prints progress lines.
- combined exit code is `1` if any worker exit code is non-zero or final error count is non-zero.

# Command-by-command contract

## `doctor`

Purpose:

- Validate local runtime prerequisites before real runs.

Behavior:

- Calls `run_doctor_checks()`.
- Prints one line per check: `[OK]` or `[FAIL]` with check name and detail.
- If any check fails, prints a remediation hint and exits `1`.

Exit codes:

- `0` all checks pass.
- `1` one or more checks fail.

## `init`

Purpose:

- Initialize local runtime directories and DB.

Behavior:

- Resolves `--data-dir` (default `./var`) to absolute path.
- Creates required directories and DB schema.
- Prints resolved data dir path.

Exit codes:

- `0` on success.

## `stats-dashboard`

Purpose:

- Build a static analytics dashboard from Codex telemetry CSV.

Behavior:

- Resolves `--data-dir` (default `./var`) to absolute path.
- Chooses telemetry source CSV:
  - `--csv` when provided
  - otherwise `<data_dir>/codex_exec_activity.csv`
- Chooses output directory:
  - `--out-dir` when provided
  - otherwise `<data_dir>/analytics-dashboard`
- Calls dashboard builder in `src/codex_farm/analytics_dashboard.py`.
- Always writes dashboard artifacts even when CSV is missing (warnings printed to stdout).

Output:

- `Wrote dashboard: <abs path to index.html>`
- `Rows analyzed: <count>`
- optional `warning: ...` lines

Exit codes:

- `0` on success.

## `pipelines list`

Purpose:

- Discover available pipelines and show ID + description.

Behavior:

- Resolves pipeline pack root (`--root`, env, or auto-discovery).
- Loads all pipeline specs.
- Sorts by `pipeline_id`.

Output:

- text mode: one line per pipeline, `<pipeline_id>: <description>`.
- JSON mode: array of objects with `pipeline_id`, `description`.

## `pipelines new`

Purpose:

- Scaffold a new pipeline JSON, prompt template, and placeholder schema.

Behavior:

- Requires `--pipeline-id`.
- Computes slug by replacing `.` with `_`.
- Refuses to overwrite if any target file already exists.
- Writes:
- `pipelines/<pipeline_id>.json`
- `prompts/<slug>.txt`
- `schemas/<slug>.schema.json`

Non-obvious defaults written into new pipeline JSON:

- model: `gpt-5.3-codex-spark`
- sandbox: `read-only`
- approval: `never`
- web search: `disabled`
- timeout: `180`
- cd mode: `asset_root`

## `one`

Purpose:

- Process a single input file through one pipeline.

Behavior:

- Resolves farm root and optional workspace override.
- Loads pipeline by ID.
- Resolves Codex `--cd` via `_resolve_one_cd_dir`.
- Renders prompt from template (`{{INPUT_PATH}}` substitution).
- Runs Codex wrapper.
- Validates output against schema.
- Deletes output file if schema validation fails.

Exit codes:

- `0` success and valid output written.
- `1` timeout, codex failure, or schema validation failure.

Output:

- success: `Wrote output: <abs path>`.
- failure: specific error message from timeout/codex/schema path.

## `run create`

Purpose:

- Create run + queued task rows only (no worker execution).

Behavior:

- Resolves roots and pipeline.
- Ensures data dir + DB exist.
- Uses explicit `--glob` value; default is `"**/*.json"`.
- Persists run config JSON with absolute paths and `farm_root`.
- Adds `workspace_root` only when user passed `--workspace-root`.

Output:

- text mode: `Created run <run_id> with <task_count> tasks`.
- JSON mode object:

```json
{
  "run_id": "string",
  "pipeline_id": "string",
  "input_dir": "absolute path",
  "output_dir": "absolute path",
  "total": 1,
  "farm_root": "absolute path",
  "workspace_root": "absolute path or null"
}
```

Failure mode to remember:

- No matching files -> `BadParameter` with clear glob/input message.

## `run status`

Purpose:

- Return inferred run state and task counts.

Output:

- text mode: one summary line like `run_id=... status=... queued=...`.
- JSON mode object:

```json
{
  "run_id": "string",
  "pipeline_id": "string",
  "status": "queued|running|done|error",
  "counts": {
    "queued": 0,
    "running": 0,
    "done": 0,
    "error": 0,
    "total": 0
  }
}
```

## `run tasks`

Purpose:

- Inspect tasks for a run (optionally by status).

Behavior:

- Validates `--status` against `queued|running|done|error`.

Output:

- JSON mode: array of task rows with:
- `input_path`, `rel_output_path`, `status`, `attempts`, `error`, `output_path`
- text mode:
- `No tasks found.` when empty
- otherwise one line per task + optional indented error line.

## `run errors`

Purpose:

- Inspect only terminal error tasks.

Output:

- JSON mode: array of rows with:
- `task_id`, `input_path`, `rel_output_path`, `attempts`, `error`, `leased_by`, `lease_until`, `updated_at`
- text mode:
- `No error tasks.` when empty
- otherwise `<input_path>: <error message>`.

## `worker`

Purpose:

- Run worker loop directly (daemon-like or one-pass).

Behavior:

- Optional `--root` is validated only when passed.
- If `--worker-id` not provided, generates `worker-<8 hex chars>`.
- Calls `worker_loop(...)` and exits with that exact code.

Exit codes:

- passthrough from `worker_loop`.

## `process`

Purpose:

- End-to-end batch mode: create run + execute workers until queue is drained.

Behavior:

- Resolves root, workspace override, and pipeline.
- Glob selection rule:
- if `--glob` provided and non-empty: use it.
- if omitted/empty string: use pipeline `input_glob_default`.
- Creates run+tasks (same internal path as `run create`).
- Starts `N` worker threads with deterministic IDs `worker-1...worker-N`.
- Polls status every second and prints progress.
- Collects worker exit codes and computes combined exit code.

Output contract:

- text mode:
- prints creation line, progress lines, final summary.
- JSON mode:
- stdout is a single final JSON object.
- creation/progress lines go to stderr.

`process --json` payload:

```json
{
  "run_id": "string",
  "pipeline_id": "string",
  "status": "queued|running|done|error",
  "counts": {
    "queued": 0,
    "running": 0,
    "done": 0,
    "error": 0,
    "total": 0
  },
  "input_dir": "absolute path",
  "output_dir": "absolute path",
  "farm_root": "absolute path",
  "workspace_root": "absolute path or null",
  "worker_exit_codes": [0, 0],
  "exit_code": 0
}
```

Exit codes:

- `0` if all workers returned `0` and final run has zero error tasks.
- `1` otherwise.

## `go`

Purpose:

- Interactive inbox/outbox wrapper for quick manual usage.

Flow:

- Initializes data dir.
- Loads and lists available pipelines.
- Prompts for pipeline number and worker count.
- Input dir is fixed to `<data_dir>/inbox`.
- Output dir is `<data_dir>/outbox/<pipeline_id>/<timestamp>`.
- Timestamp format: `%Y-%m-%d_%H.%M.%S`.
- Creates run and executes workers similarly to `process`.

Exit codes:

- `1` if no pipelines exist.
- otherwise same combined success/failure semantics as `process`.

# Non-obvious rules future AI coders should preserve

- `process --json` is machine-facing; keep stdout JSON-only.
- `run create` default glob is always `"**/*.json"`, while `process` defaults to pipeline `input_glob_default` when `--glob` is omitted.
- Persisted run config must include absolute `farm_root`; include `workspace_root` only when explicitly provided.
- `--workspace-root` is an override, not a fallback default.
- `stats-dashboard` is read-only over telemetry input CSV and should continue writing a fully static bundle (`index.html` + `assets/` files).
- `one` handles `input_dir` and `input_file_dir` the same way (input file parent) because there is no run-wide input root.
- CLI should keep raising `typer.BadParameter` for bad user inputs so errors remain actionable.

# How to change this chunk safely

1. If you add/rename a command or option, update tests in `tests/test_cli_integration_contracts.py` and related smoke tests.
2. If you change any JSON shape, treat it as a contract change and update this doc plus callers/tests.
3. If you move behavior into/out of CLI helpers, verify command output/exit behavior is unchanged unless intentionally versioning it.
