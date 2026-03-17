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
- `lint`
- `one`
- `stats-dashboard`
- `worker`
- `process`
- `go`
- `pipelines list`
- `pipelines new`
- `models list`
- `heads-up list`
- `heads-up clear`
- `heads-up learn`
- `run create`
- `run progress`
- `run status`
- `run tasks`
- `run errors`
- `run forensics`
- `run telemetry`
- `run autotune`

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

- `_resolve_codex_home_override_or_die(codex_home)` / `_resolve_codex_home_path(...)`
- `--codex-home` is accepted by `one`, `run create`, `process`, and `go`.
- explicit `--codex-home` wins; otherwise CLI resolves `CODEX_FARM_CODEX_HOME_<PROFILE>` from the pipeline `codex_home_profile`.
- run-based commands persist the resolved absolute value in `runs.config_json.codex_home_path`.

- `_resolve_model_override_or_die(model)`
- returns `None` if not provided.
- otherwise trims and validates non-empty override text; raises `typer.BadParameter` on empty input.

- `_resolve_reasoning_effort_override_or_die(reasoning_effort)`
- accepts normalized Codex effort values: `none|minimal|low|medium|high|xhigh`.
- supports CLI aliases: `--effort`, `--reasoning-effort`, `--thinking-effort`, `--codex-reasoning-effort`, `--codex-thinking-effort`.

- `_ensure_codex_login_precheck_or_die(command_name, enabled, model, reasoning_effort, env_overrides)`
- skips when disabled or `CODEX_FARM_SKIP_LOGIN_PRECHECK` is truthy.
- otherwise runs `run_codex_execution_checks(...)`.
- `one`, `process`, and `go` pass the same resolved model/effort and resolved `CODEX_HOME` they will execute with, so the smoke check does not silently fall back to the default session.
- unscoped `worker` still prechecks generically; `worker --run-id` opens the run first and prechecks the persisted `codex_home_path`.

- `_resolve_one_cd_dir(...)`
- for `one`, `--workspace-root` wins.
- without override:
- `codex_cd_mode=asset_root` -> farm root.
- `codex_cd_mode=input_dir|input_file_dir` -> input file parent.
- requires computed directory to exist.
- if the selected pipeline has `codex_execution_context="scratch"`, this result is only the project-style base directory; the actual subprocess `--cd` becomes a scratch directory under `<data_dir>/execution_contexts/`.

- `_init_data_dir(data_dir)`
- creates `data_dir`, `inbox/`, `outbox/`.
- creates `run_assets/` for per-run frozen execution snapshots.
- scratch execution contexts are created later under `<data_dir>/execution_contexts/` when needed.
- opens DB at `<data_dir>/codex_farm.sqlite3` and runs `init_db(...)`.

- `_create_run_for_paths(...)`
- globs files and fails fast if no files matched.
- allocates run ID, freezes prompt/schema/pipeline assets under `<data_dir>/run_assets/<run_id>/`, then creates run row + enqueues one task per input.
- persists snapshot pointer in `runs.config_json.frozen_assets`:
  - `version`
  - `manifest_relpath`
- on DB write/enqueue failure after freezing, removes the snapshot best-effort and re-raises the original error.

- `_run_workers(...)`
- starts `workers` threads, each `worker_loop(... once=True)`.
- polls run status and prints progress lines.
- supports an optional progress snapshot callback so `process --progress-events` can emit machine-readable stderr events while preserving single-payload stdout JSON.
- progress polling waits on pending worker futures with `timeout=poll_seconds`, so fast-completing runs do not always pay an extra full poll-sleep before finishing.
- this `wait(..., return_when=FIRST_COMPLETED)` approach is intentional; prior fixed-sleep polling added avoidable latency in fast mocked/test runs.
- worker warnings are forwarded to stderr, including adaptive 429 cooldown/recovery transitions.
- combined exit code is `1` if any worker exit code is non-zero or final error count is non-zero.

# Command-by-command contract

## `doctor`

Purpose:

- Validate local runtime prerequisites before real runs.

Behavior:

- Calls `run_doctor_checks()`.
- Prints one line per check: `[OK]` or `[FAIL]` with check name and detail.
- When non-interactive Codex smoke fails with auth/session signatures (`401/403`, login-required text), detail is auth-specific and recommends running `codex` to sign in.
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

## `lint`

Purpose:

- Run a local, read-only preflight over a pack (`--root`) or one schema file (`--schema`).

Behavior:

- Pack mode: `codex-farm lint [--root <pack-root>] [--pipeline <pipeline-id>]`.
- Schema mode: `codex-farm lint --schema <schema-path>`.
- `--schema` and `--pipeline` are mutually exclusive.
- `--strict` only changes exit behavior:
  - default: exit `0` when no errors exist (warnings allowed).
  - strict: exit `1` when warnings exist.
- `--json` emits one stdout object with `target`, `ok`, `error_count`, `warning_count`, `scanned`, and `findings`.
- Explicit near-miss roots are allowed for linting:
  - if `--root` points at an existing directory missing sentinels, lint reports `pack.missing_sentinel_dirs` instead of failing argument parsing.
- Linting is filesystem-only:
  - no Codex subprocess calls
  - no SQLite access
  - no file writes

Exit codes:

- `0`: no errors (`--strict` off) or fully clean (`--strict` on).
- `1`: one or more errors, or warnings when `--strict` is enabled.

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

## `models list`

Purpose:

- Expose caller-facing model choices from local Codex metadata.

Behavior:

- Reads visible model rows from local `models_cache.json` files under Codex home directories.
- Includes visibility values `list` and `default`; ignores hidden/private rows.
- Deduplicates by model `slug`.
- Normalizes optional supported reasoning-effort metadata when present.
- If no cache rows are available, returns one fallback model row for `gpt-5.3-codex-spark`.

Output:

- text mode: one line per model with optional description and effort hints.
- JSON mode: array of objects:
  - `slug`
  - `display_name`
  - `description`
  - optional `supported_reasoning_efforts`

## `heads-up list`

Purpose:

- Inspect learned Heads Up tips for one pipeline.

Behavior:

- Reads rows from `heads_up_tips` for `--pipeline`.
- Supports `--json` machine output.

## `heads-up clear`

Purpose:

- Delete learned Heads Up tips for one pipeline.

Behavior:

- Requires confirmation unless `--yes` is passed.
- Returns deleted row count in JSON mode.

## `heads-up learn`

Purpose:

- Backfill or rerun post-run tip distillation for an existing run.

Behavior:

- Accepts `--run-id` plus optional model/effort overrides.
- Requires run status to be terminal (`done` or `error`); non-terminal runs return warning output and add zero tips.
- Executes one distiller call and upserts normalized tips.
- Warning text is non-fatal and is returned in JSON payload when present.

## `one`

Purpose:

- Process a single input file through one pipeline.

Behavior:

- Resolves farm root and optional workspace override.
- Loads pipeline by ID.
- Optional `--model`/`--codex-model` overrides pipeline `codex_model` for this invocation.
- Optional reasoning-effort aliases override pipeline `codex_reasoning_effort`.
- Optional `--output-schema` overrides pipeline `output_schema_path` for Codex structured output and local validation.
- Optional `--heads-up` enables prompt augmentation from learned tips in local SQLite.
- Optional `--heads-up-max-tips` caps appended tips (default `3`, min `1`, max `8`).
- Runs execution precheck by default (`codex login status` plus a non-interactive `codex exec` smoke check) before execution; bypass with `--no-login-precheck` or `CODEX_FARM_SKIP_LOGIN_PRECHECK=1`.
- Resolves Codex `--cd` via `_resolve_one_cd_dir`.
- Renders prompt from template (`{{INPUT_PATH}}` and `{{INPUT_TEXT}}` substitutions are supported; required token is selected by pipeline `prompt_input_mode`).
- When `--heads-up` is enabled, computes an input signature and appends matching `Heads up` tips before execution.
- Runs Codex wrapper.
- Validates output against schema.
- Deletes output file if schema validation fails.
- For auth/session failures, emits an auth-specific message and warning, then exits `1` (no retry path in `one`).
- On failure, captures a best-effort forensics bundle under `<data_dir>/forensics/one/<forensics_id>/`.

Exit codes:

- `0` success and valid output written.
- `1` timeout, codex failure, or schema validation failure.

Output:

- success: `Wrote output: <abs path>`.
- failure: specific error message from timeout/codex/schema path.
- failure with captured bundle: one additional stderr line `Forensics bundle: <abs path>`.

## `run create`

Purpose:

- Create run + queued task rows only (no worker execution).

Behavior:

- Resolves roots and pipeline.
- Ensures data dir + DB exist.
- Uses explicit `--glob` value; default is `"**/*.json"`.
- Optional `--incremental` enables planning-time reuse from a compatible prior run.
- Optional `--incremental-from <run_id>` forces one source run and fails if it is missing, non-terminal, or incompatible.
- Persists run config JSON with absolute paths and `farm_root`.
- Persists explicit `runtime_mode` (`classic_task_farm_v1` default, `structured_loop_agentic_v1` opt-in).
- Adds `workspace_root` only when user passed `--workspace-root`.
- Adds `codex_home_path` when CLI resolved one from `--codex-home` or `CODEX_FARM_CODEX_HOME_<PROFILE>`.
- Adds `codex_model` only when user passed `--model`.
- Adds `codex_reasoning_effort` only when user passed an effort override.
- Adds `output_schema_path_override` only when user passed `--output-schema`.
- Adds `recipeimport_benchmark_mode` only when user passed `--recipeimport-benchmark-mode`.
- Adds `recipeimport_benchmark_debug` only when user passed `--recipeimport-benchmark-debug`.
- When `recipeimport_benchmark_mode=line_label_v1` is set, run creation dispatches to pipeline `recipeimport.benchmark.line_label.v1`.
- Always persists `heads_up_enabled` and `heads_up_max_tips` for worker determinism.
- Always persists session reset defaults (`session_task_budget`, `max_turns_per_task`, `session_reset_on_error`) so session-aware runs stay reproducible.
- Always persists `incremental_enabled` and `incremental_source_run_id` in run config for reproducibility.
- Computes and stores `runs.execution_fingerprint` and can pre-materialize reused outputs before worker start.

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
  "runtime_mode": "classic_task_farm_v1|structured_loop_agentic_v1",
  "workspace_root": "absolute path or null",
  "codex_execution_context": "project|scratch",
  "codex_home_path": "absolute path or null",
  "codex_model": "resolved model string",
  "codex_reasoning_effort": "resolved effort string or null",
  "output_schema_path": "resolved schema path",
  "recipeimport_benchmark_mode": "line_label_v1 or null",
  "recipeimport_benchmark_debug": false,
  "heads_up_enabled": false,
  "heads_up_max_tips": 3,
  "incremental": {
    "enabled": false,
    "source_run_id": null,
    "reused": 0,
    "queued": 0,
    "fallback_counts": {
      "no_prior_success": 0,
      "hash_changed": 0,
      "source_output_missing": 0,
      "source_output_invalid": 0
    }
  }
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

## `run progress`

Purpose:

- Return spinner-friendly progress snapshots for a run.

Behavior:

- Includes the same run status/count fields as `run status`.
- Adds `progress.completed`, `progress.remaining`, and `progress.percent_complete`.
- Adds bounded `running_tasks` and `recent_errors` arrays for active-state UI rendering.
- Adds additive `session_summary` counters so callers can see whether one active worker session is serving multiple tasks.
- `--watch` mode keeps polling until terminal run state and emits one snapshot per poll.

Output:

- text mode: compact run/count/percent summary plus running/error detail snippets.
- JSON mode object:

```json
{
  "run_id": "string",
  "pipeline_id": "string",
  "status": "queued|running|done|error|canceled",
  "control_state": "active|paused|cancel_requested|canceled",
  "counts": {
    "queued": 0,
    "running": 0,
    "done": 0,
    "error": 0,
    "canceled": 0,
    "total": 0
  },
  "snapshot_at_utc": "ISO-8601 timestamp",
  "runtime_mode": "classic_task_farm_v1|structured_loop_agentic_v1",
  "progress": {
    "completed": 0,
    "remaining": 0,
    "percent_complete": 0.0
  },
  "session_summary": {
    "active_sessions": 0,
    "sessions_started": 0,
    "sessions_finished": 0,
    "current_session_task_count": 0,
    "session_count": 0,
    "fresh_session_count": 0,
    "session_turn_count_total": 0,
    "session_failures": 0,
    "tasks_per_session_summary": {}
  },
  "running_tasks": [],
  "recent_errors": []
}
```

## `run tasks`

Purpose:

- Inspect tasks for a run (optionally by status).

Behavior:

- Validates `--status` against `queued|running|done|error|canceled`.

Output:

- JSON mode: array of task rows with:
- `input_path`, `rel_output_path`, `status`, `attempts`, `lease_claims`, `execution_attempts`, `last_heartbeat_at`, `error`, `output_path`
- additive reuse fields: `reused`, `reused_from_run_id`, `reused_from_task_id`
- additive session fields: `session_row_id`, `session_task_index`, `session_turn_index`, `fresh_session_started`
- text mode:
- `No tasks found.` when empty
- otherwise one line per task + optional `[reused]` marker + optional indented error line.
- text mode shows both lease claims and execution attempts when they diverge (`attempts=N exec_attempts=M`).

## `run errors`

Purpose:

- Inspect only terminal error tasks.

Output:

- JSON mode: array of rows with:
- `task_id`, `input_path`, `rel_output_path`, `attempts`, `lease_claims`, `execution_attempts`, `last_heartbeat_at`, `error`, `leased_by`, `lease_until`, `updated_at`
- text mode:
- `No error tasks.` when empty
- otherwise `<input_path>: <error message>`.

## `run forensics`

Purpose:

- Inspect preserved failed-attempt evidence bundles for one run.

Behavior:

- Reads only from SQLite `task_forensics` index rows for `--run-id` (optional `--task-id` filter).
- Does not scan telemetry CSV and does not recurse filesystem directories.

Output:

- JSON mode: array of rows with:
- `forensics_id`, `source`, `run_id`, `task_id`, `pipeline_id`
- `attempt_index`, `terminal`, `input_path`, `rel_output_path`
- `failure_stage`, `failure_category`, `error_summary`
- `bundle_dir`, `metadata_path`, `raw_output_path`, `created_at`
- text mode:
- `No forensics bundles.` when empty
- otherwise one row per bundle with `task_id`, `attempt`, `stage`, `category`, and `bundle` path.

## `run telemetry`

Purpose:

- Build a machine-usable telemetry report for prompt/data/schema refinement loops.

Behavior:

- Reads telemetry CSV from `--csv` or default `<data_dir>/codex_exec_activity.csv`.
- Supports filters: `--run-id`, `--pipeline`, `--source`, `--status`, `--limit`.
- Uses `list_error_tasks` when `--run-id` is provided so report includes terminal task errors that can occur after Codex subprocess success (for example local schema-gate failures).
- Emits recommendation categories with evidence rows:
- `prompt`
- `input_data`
- `output_schema`
- `runtime`

Output:

- JSON mode: one report object with `summary`, `failure_patterns`, `heads_up_patterns`, `insights`, `recommendations`, `tuning_playbook`, `terminal_errors`, and `recent_rows`.
- text mode: compact summary plus recommendation lines.

## `run autotune`

Purpose:

- Convert telemetry tuning output into immediate caller actions (flag overrides and file diffs).

Behavior:

- Builds telemetry report using the same filters as `run telemetry`.
- Resolves run/pipeline context from SQLite run metadata (`--run-id`) and pipeline assets (`farm_root` from run config or `--root` override).
- Emits non-mutating autotune payload with:
- `flag_overrides` (for example `--workers`, `--model`, `--reasoning-effort`)
- `command_preview` for rerun commands
- `prompt_template_diff` and `pipeline_config_diff` unified diff text when paths are resolvable
- Requires at least one context selector: `--run-id` or `--pipeline`.

Output:

- JSON mode: one payload with schema version, context, overrides, preview command, diff blocks, and warning list.
- text mode: compact summary, command preview, override lines, and any generated diff text.

## `worker`

Purpose:

- Run worker loop directly (daemon-like or one-pass).

Behavior:

- Optional `--root` is validated only when passed.
- Runs execution precheck by default (`codex login status` plus a non-interactive `codex exec` smoke check) before leasing tasks; bypass with `--no-login-precheck` or `CODEX_FARM_SKIP_LOGIN_PRECHECK=1`.
- `worker --run-id <id>` reads that run first and prechecks its persisted `codex_home_path`; unscoped worker precheck stays generic.
- When the scoped run persisted `runtime_mode=structured_loop_agentic_v1`, CLI dispatches to the session-aware worker loop instead of the classic per-task loop.
- If `--worker-id` not provided, generates `worker-<8 hex chars>`.
- Calls `worker_loop(...)` and exits with that exact code.

Exit codes:

- passthrough from `worker_loop`.

## `process`

Purpose:

- End-to-end batch mode: create run + execute workers until completion or early-stop conditions.

Behavior:

- Resolves root, workspace override, optional model override, optional effort override, and pipeline.
- Resolves optional `--codex-home` override or profile-derived `CODEX_FARM_CODEX_HOME_<PROFILE>`.
- Resolves optional output-schema override (`--output-schema`).
- Resolves optional recipeimport benchmark mode (`--recipeimport-benchmark-mode line_label_v1`) and debug capture toggle (`--recipeimport-benchmark-debug`).
- Resolves `--runtime-mode` and persists it with the run.
- When benchmark mode is enabled, process dispatches to pipeline `recipeimport.benchmark.line_label.v1`.
- Runs execution precheck by default (`codex login status` plus a non-interactive `codex exec` smoke check) before run creation; bypass with `--no-login-precheck` or `CODEX_FARM_SKIP_LOGIN_PRECHECK=1`.
- The precheck receives the same resolved `CODEX_HOME` override the real run will use.
- Optional `--incremental` enables planning-time reuse from the latest compatible prior run.
- Optional `--incremental-from <run_id>` forces one source run and fails on incompatibility.
- Glob selection rule:
- if `--glob` provided and non-empty: use it.
- if omitted/empty string: use pipeline `input_glob_default`.
- Creates run+tasks (same internal path as `run create`).
- `classic_task_farm_v1` starts `N` classic worker threads with deterministic IDs `worker-1...worker-N`.
- `structured_loop_agentic_v1` defaults to one worker when `--workers` is omitted and rejects `--workers > 1`; that one worker runs the session-aware loop.
- Polls status every second and prints progress.
- If a worker hits codex rate-limit (`429` / rate-limit text), it requeues the interrupted task, stores run-level cooldown state, and temporarily reduces effective concurrency.
- Temporary throttling auto-recovers in the same invocation; persistent throttling eventually exits non-zero with queued work preserved for resume.
- Collects worker exit codes and computes combined exit code.
- If `--heads-up` is enabled, workers append matching tips to prompts and, after the run reaches terminal state, CLI performs one best-effort distillation call to learn new tips from outcomes.
- Distiller failures are warning-only and do not change run/task status semantics.
- `--telemetry-report/--no-telemetry-report` controls whether `process --json` includes an embedded telemetry report (enabled by default).
- `--telemetry-limit` and `--telemetry-recommendations-limit` bound report size in `process --json`.
- `--progress-events` emits machine-readable stderr events prefixed with `__codex_farm_progress__ `:
  - `run_started` (initial snapshot + worker count)
  - `run_progress` (periodic snapshots from worker polling loop)
  - `run_finished` (terminal snapshot + exit metadata)

Output contract:

- text mode:
- prints creation line, progress lines, final summary.
- JSON mode:
- stdout is a single final JSON object.
- creation/progress lines go to stderr.
- with `--progress-events`, stderr additionally includes prefixed JSON event lines for caller-side spinner/progress adapters.

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
  "runtime_mode": "classic_task_farm_v1|structured_loop_agentic_v1",
  "effective_workers": 1,
  "workspace_root": "absolute path or null",
  "codex_execution_context": "project|scratch",
  "codex_home_path": "absolute path or null",
  "codex_model": "resolved model string",
  "codex_reasoning_effort": "resolved effort string or null",
  "output_schema_path": "resolved schema path",
  "recipeimport_benchmark_mode": "line_label_v1 or null",
  "recipeimport_benchmark_debug": false,
  "heads_up_enabled": false,
  "heads_up_max_tips": 3,
  "heads_up_tips_applied": 0,
  "heads_up_tips_added": 0,
  "progress_events_enabled": false,
  "session_count": 0,
  "fresh_session_count": 0,
  "tasks_per_session_summary": {},
  "session_turn_count_total": 0,
  "session_failures": 0,
  "incremental": {
    "enabled": true,
    "source_run_id": "string or null",
    "reused": 0,
    "queued": 0,
    "fallback_counts": {
      "no_prior_success": 0,
      "hash_changed": 0,
      "source_output_missing": 0,
      "source_output_invalid": 0
    }
  },
  "telemetry_report": {
    "schema_version": 2,
    "matched_rows": 0,
    "insights": {
      "model_reasoning_breakdown": [],
      "prompt_fingerprint_breakdown": [],
      "input_failure_hotspots": [],
      "reasoning_signals": {},
      "pass_forward_effectiveness": {}
    },
    "recommendations": {
      "prompt": [],
      "input_data": [],
      "output_schema": [],
      "runtime": []
    },
    "tuning_playbook": {
      "prompt_edits": [],
      "input_prechecks": [],
      "schema_edits": [],
      "runtime_tuning": [],
      "model_tuning": []
    }
  },
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
- Optional `--model` persists run-level `codex_model` override for worker execution.
- Optional effort aliases persist run-level `codex_reasoning_effort` override for worker execution.
- Optional `--output-schema` persists run-level `output_schema_path_override` for worker execution.
- Optional `--recipeimport-benchmark-mode line_label_v1` persists run-level benchmark mode for worker-side benchmark artifacts.
- Optional `--recipeimport-benchmark-debug` persists run-level debug capture for benchmark artifacts.
- When benchmark mode is enabled, go dispatches execution to pipeline `recipeimport.benchmark.line_label.v1`.
- Optional `--heads-up`/`--heads-up-max-tips` persist run-level prompt-adaptation settings for worker execution.
- Optional `--incremental` and `--incremental-from` persist incremental planning intent for run reproducibility.
- Runs execution precheck by default (`codex login status` plus a non-interactive `codex exec` smoke check) before run creation; bypass with `--no-login-precheck` or `CODEX_FARM_SKIP_LOGIN_PRECHECK=1`.
- Creates run and executes workers similarly to `process`.
- When `--heads-up` is enabled and run reaches terminal status, performs one best-effort post-run learning call and prints `Heads Up tips added: <n>`.

Exit codes:

- `1` if no pipelines exist.
- otherwise same combined success/failure semantics as `process`.

# Non-obvious rules future AI coders should preserve

- `process --json` is machine-facing; keep stdout JSON-only.
- `lint --json` is machine-facing; keep stdout JSON-only.
- `run create` default glob is always `"**/*.json"`, while `process` defaults to pipeline `input_glob_default` when `--glob` is omitted.
- `process` now uses adaptive run-level 429 handling (cooldown + reduced concurrency + recovery) instead of hard-stop-on-first-hit behavior.
- Execution precheck defaults to enabled on `one`, `worker`, `process`, and `go`; it checks both `codex login status` and a non-interactive `codex exec` smoke call. `--no-login-precheck` and env `CODEX_FARM_SKIP_LOGIN_PRECHECK=1` remain the explicit bypass controls.
- Persisted run config must include absolute `farm_root`; include `workspace_root` only when explicitly provided.
- Persisted run config includes `codex_model` only when the user passes `--model`; workers honor this override instead of pipeline default.
- Persisted run config includes `codex_reasoning_effort` only when the user passes effort aliases; workers honor this override instead of pipeline default.
- Persisted run config includes `output_schema_path_override` only when the user passes `--output-schema`; workers honor this override instead of pipeline default schema.
- Persisted run config includes `recipeimport_benchmark_mode` only when the user passes `--recipeimport-benchmark-mode`; workers use this to enable benchmark artifact generation.
- Persisted run config includes `recipeimport_benchmark_debug` only when the user passes `--recipeimport-benchmark-debug`; workers use this to include raw prompt/response debug files in benchmark artifacts.
- Benchmark mode `line_label_v1` is not just metadata: CLI run creation/processing routes to the benchmark pipeline ID `recipeimport.benchmark.line_label.v1`.
- Persisted run config always includes `heads_up_enabled` and `heads_up_max_tips`; workers use those values to keep resume behavior deterministic.
- Incremental reuse is planning-time only: workers still lease only queued/running tasks, and reused tasks start in `done`.
- Reuse safety requires both `input_hash` equality and matching `runs.execution_fingerprint`; hash-only reuse is forbidden.
- Heads Up post-run learning is warning-safe in both `process` and `go`; learner failures must not change run/task exit semantics.
- Heads Up learning is terminal-run only; non-terminal `heads-up learn` calls return warning output and add zero tips.
- `models list --json` is the machine-facing contract for external callers that need model-menu choices.
- `run telemetry --json` is the machine-facing contract for aggregated telemetry recommendations; callers should prefer it over parsing raw CSV directly.
- `run telemetry --json` schema version `2` includes caller-ready `insights` and `tuning_playbook` sections for automatic prompt/data/schema/runtime adjustments.
- `run autotune --json` is non-mutating by contract; it emits patch suggestions and flag overrides but does not modify files.
- `run forensics --json` is the machine-facing failed-attempt evidence index; callers should prefer it over directory scans.
- `run errors --json` remains task-state introspection, while `run forensics --json` is additive artifact/evidence introspection.
- `--workspace-root` is an override, not a fallback default.
- Lint root handling is intentionally asymmetric:
  - normal commands still require a fully valid pack root.
  - `lint --root <existing-dir>` may run on near-miss roots to surface diagnostics.
- `stats-dashboard` is read-only over telemetry input CSV and should continue writing a fully static bundle (`index.html` + `assets/` files).
- `process --json` now includes embedded `telemetry_report` by default; it remains stdout-only JSON even with report warnings.
- `one` handles `input_dir` and `input_file_dir` the same way (input file parent) because there is no run-wide input root.
- `one` failure output can include an extra stderr line (`Forensics bundle: ...`) after the main failure message; this is additive and should not replace existing error text.
- CLI should keep raising `typer.BadParameter` for bad user inputs so errors remain actionable.

# How to change this chunk safely

1. If you add/rename a command or option, update tests in `tests/test_cli_integration_contracts.py` and related smoke tests.
2. If you change any JSON shape, treat it as a contract change and update this doc plus callers/tests.
3. If you move behavior into/out of CLI helpers, verify command output/exit behavior is unchanged unless intentionally versioning it.

## Task doc merges from `docs/tasks`

Historical task docs merged into this chunk to preserve caller-contract context:

- `Initial-Build.md` (`2026-02-20_12.45.00` revision note):
  - established CLI-first split between scripted `process` and interactive `go`.
  - established local-only operation and non-interactive Codex defaults for worker safety.
  - locked the "one input file -> one output file" batch contract and non-zero exit on terminal task errors.
- `Plan-for-recipe-correction.md` (`2026-02-22_12.36.41`):
  - added external pack caller contract (`--root`, `--workspace-root`, stable JSON payloads).
  - added `run tasks` and `run errors` machine endpoints so callers do not scrape logs or SQLite.
  - enforced machine-clean `process --json` stdout (progress on stderr).
- `Plan-for-knowledge-correction.md` (`2026-02-22_13.07.23`):
  - extended caller-facing config with pipeline `codex_cd_mode` and explicit workspace override precedence.
  - refined `run errors --json` to return focused error-task rows rather than generic task rows.
- `2026-02-28_02.47.41 - model-override-cli.md`:
  - locked `--model` support on `one`, `run create`, `process`, and `go`.
  - kept override persistence optional (`codex_model` only when caller explicitly passes an override).
- `2026-02-28_02.55.22 - model-effort-overrides-for-callers.md`:
  - locked effort alias vocabulary and normalized value domain.
  - persisted `codex_reasoning_effort` only when explicitly set so queued runs remain deterministic without altering default pipeline behavior.
- `2026-02-28_04.16.54 - caller-model-menu-contract.md`:
  - added `models list --json` as the stable caller model-picker contract.
  - locked deterministic fallback row when Codex cache metadata is missing.

## Merged discoveries from `docs/understandings`

Chronological details that were previously split across short exploration notes:

- `2026-02-22_14.34.46`: `process --json` is a strict machine contract. Stdout must contain only the final JSON object; creation/progress lines belong on stderr.
- `2026-02-22_14.34.46`: `run create` and `process` intentionally use different glob-default semantics. `run create` defaults to `"**/*.json"` while `process` falls back to the pipeline's `input_glob_default` when `--glob` is omitted/empty.
- `2026-02-22_14.34.46`: Run config persistence is part of the CLI contract. `farm_root` is always persisted as an absolute path; `workspace_root` is persisted only when explicitly provided.
- `2026-02-22_14.34.46`: In `one`, `codex_cd_mode=input_dir` and `input_file_dir` intentionally collapse to the same directory (`Path(--in).parent`) because no run-wide input root exists.
- `2026-02-28_02.47.41`: `--model` is a run-level override for `one`, `run create`, `process`, and `go`; queued runs persist `codex_model` in `config_json` so worker execution stays deterministic across resumes.
- `2026-02-28_02.55.22`: effort aliases (`--effort|--reasoning-effort|--thinking-effort` and codex-prefixed forms) map to Codex `model_reasoning_effort`; run-based flows persist `codex_reasoning_effort` so worker/resume behavior stays deterministic.
- `2026-02-28_09.31.02`: caller-supplied `--output-schema` is a run-level validation-contract override for `one`, `run create`, `process`, and `go`; run-based flows persist `output_schema_path_override`, and JSON payloads expose resolved `output_schema_path`.
- `2026-02-28_09.33.49`: Heads Up adaptation is an explicit opt-in contract (`--heads-up`). Run-based flows persist `heads_up_enabled` and `heads_up_max_tips`; worker prompt injection and post-run learning remain warning-safe and deterministic across resumes.
- `2026-02-28_09.58.11`: Heads Up learning safety applies to both `process --heads-up` and `go --heads-up`; non-terminal learn calls stay warning-only with zero added tips so run/task exit semantics remain tied to worker outcomes.
- `2026-02-28_04.16.54`: `models list` is the caller contract for model-picker menus; it sources visible Codex cache rows and emits a stable fallback (`gpt-5.3-codex-spark`) when local cache metadata is absent.
- `2026-02-28_12.34.52`: run lifecycle control is now explicit in CLI contracts: `run pause`, `run resume`, `run cancel`, and `run retry-errors` exist; `run status --json` and `process --json` include `control_state` plus `counts.canceled`; and `run tasks --status canceled` is supported.
- `2026-02-28_13.43.43`: `_run_workers(...)` switched from unconditional `time.sleep(poll_seconds)` loops to `concurrent.futures.wait(..., return_when=FIRST_COMPLETED)` so progress polling stays periodic but finishes immediately when work is already done.
- `2026-02-28_18.46.00`: failure-forensics inspection is a separate caller contract (`run forensics --json`) keyed by the `task_forensics` index; existing `run errors --json` payload fields remain unchanged.

Known rough edges to preserve context:

- When stdout cleanliness breaks in `process --json`, downstream automation fails fast with JSON parse errors even if core processing still works.
- Seemingly harmless "unification" of glob defaults across commands changes user-visible behavior and has broken expectations before.
- Reverting worker orchestration to unconditional poll sleeps reintroduces synthetic latency that hides real performance regressions in `process`/`go` tests.

## Merged understanding notes (`docs/understandings`)

### 2026-03-02_00.45.47 - Auth context is inherited; service callers must preserve Codex home
- `run_codex_exec` uses inherited process environment for Codex credentials; callers that run under a different user/home must explicitly set `CODEX_HOME` or `HOME` to the profile where `codex login` was completed.
- `codex-farm doctor` validates both CLI availability and a non-interactive smoke execution path so integrators get fast feedback before starting queueing work.
- For external orchestrators: align Codex credential context before running `doctor`, `process`, `go`, or `worker`.

### 2026-03-02_00.52.58 - Login preflight before queueing/starting execution
- Missing Codex login used to fail later in worker attempts with noisy retry churn.
- Execution entrypoints now preflight via `codex login status` plus a non-interactive `codex exec` smoke check before run creation or worker leasing on `one`, `worker`, `process`, and `go`.
- Explicit bypass controls: `--no-login-precheck` / `CODEX_FARM_SKIP_LOGIN_PRECHECK=1` for intentional non-interactive environments.
