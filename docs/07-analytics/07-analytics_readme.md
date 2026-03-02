---
summary: "Codex execution telemetry CSV contract plus static dashboard generation flow."
read_when:
  - "When consuming codex_exec_activity.csv for token usage or runtime reporting"
  - "When changing telemetry fields in src/codex_farm/codex_exec.py"
  - "When changing stats-dashboard behavior in src/codex_farm/analytics_dashboard.py"
---

# Analytics (Chunk 07)

This chunk defines two analytics surfaces:

1. append-only telemetry CSV rows written by each Codex subprocess call
2. a static dashboard generated from that CSV via `codex-farm stats-dashboard`
3. a caller-facing telemetry report API via `codex-farm run telemetry --json`

## File location

- `worker`, `process`, `go`: `<data_dir>/codex_exec_activity.csv`
- `one`: `./var/codex_exec_activity.csv`

## Row model

One row is appended per `run_codex_exec(...)` call.

- `status`: `ok`, `failed`, or `timeout`
- `exit_code`: Codex subprocess exit code (empty for timeout)
- `accepted_nonzero_exit`: `true` when non-zero exit still produced usable payload
- `duration_ms`: wall-clock runtime for this Codex call
- `tokens_input`, `tokens_cached_input`, `tokens_output`, `tokens_total`: usage parsed from Codex JSONL event `turn.completed.usage` when present
- `codex_event_count`, `codex_event_types_json`: parsed Codex JSONL event volume/types for each invocation
- `prompt_text`: full prompt sent to Codex
- `prompt_sha256`, `prompt_chars`: prompt fingerprint and size
- `stderr_tail`, `stdout_tail`: recent non-JSON stderr/stdout text for diagnostics
- `output_payload_present`, `output_bytes`, `output_path`: payload presence and file details
- `output_sha256`, `output_preview`, `output_preview_chars`, `output_preview_truncated`: output payload fingerprint and preview for caller-side quality analysis
- `source`: caller path (`one`, `worker`, or `heads_up.learn`)
- `pipeline_id`, `run_id`, `task_id`, `worker_id`, `input_path`: execution context when available
- `heads_up_applied`, `heads_up_tip_count`, `heads_up_input_signature`: prompt-adaptation context when available
- `heads_up_tip_ids_json`, `heads_up_tip_texts_json`, `heads_up_tip_scores_json`: concrete pass-forward Heads Up hints applied to this prompt
- `attempt_index`, `lease_claim_index`, `execution_attempt_index`, `retry_context_applied`, `retry_previous_error`, `retry_previous_error_chars`, `retry_previous_error_sha256`: retry pass-forward context from previous task failures (`attempt_index` stays backward-compatible with lease-claim semantics)
- `failure_category`, `rate_limit_suspected`: normalized failure classification signals for external callers (`accepted_nonzero_exit`, `timeout`, `nonzero_exit_no_payload`, `zero_exit_no_payload`, or empty)
- `logged_at_utc`, `started_at_utc`, `finished_at_utc`: timestamps in UTC
- `model`, `sandbox`, `ask_for_approval`, `web_search`, `reasoning_effort`, `timeout_seconds`, `cd_dir`, `output_schema_path`, `thread_id`, `usage_json`: runtime settings and raw usage context

`output_schema_path` identity rule:

- For live runs, this is the runtime schema path.
- For frozen snapshot runs, worker may execute against a frozen copy, but telemetry keeps logical schema source identity when available so cross-run grouping stays stable.

## Dashboard command

CLI entrypoint:

- `codex-farm stats-dashboard [--data-dir ./var] [--csv <path>] [--out-dir <path>] [--recent-limit N]`

Command behavior:

- Reads telemetry CSV (default `<data_dir>/codex_exec_activity.csv`).
- Computes summary metrics (status mix, token totals, duration aggregates, source/pipeline/model breakdowns, daily trend, and recent events).
- Writes a static dashboard bundle (default `<data_dir>/analytics-dashboard`).
- Missing CSV is not fatal: command still writes an empty dashboard and prints warnings.

Primary implementation file:

- `src/codex_farm/analytics_dashboard.py`

## Telemetry report command

CLI entrypoint:

- `codex-farm run telemetry [--run-id <id>] [--pipeline <id>] [--source <source>] [--status ok|failed|timeout|other] [--limit N] [--recommendations-limit N] [--data-dir ./var] [--csv <path>] [--json]`

Command behavior:

- Reads telemetry CSV (same default as dashboard).
- Applies caller filters and limit window.
- Emits machine-readable summary, pattern clusters, recommendation categories (`prompt`, `input_data`, `output_schema`, `runtime`), and a schema-versioned tuning payload (`schema_version=2`).
- `insights` section includes model/reasoning-effort breakdown, prompt-fingerprint performance, input failure hotspots, pass-forward effectiveness deltas, and Codex event-stream signals.
- `tuning_playbook` section includes concrete caller actions grouped by `prompt_edits`, `input_prechecks`, `schema_edits`, `runtime_tuning`, and `model_tuning`.
- When `--run-id` is set, report also includes terminal task errors from SQLite so schema-gate failures that occur after Codex subprocess success are visible.

Primary implementation file:

- `src/codex_farm/telemetry_report.py`

## Autotune command

CLI entrypoint:

- `codex-farm run autotune [--run-id <id> | --pipeline <id>] [--source <source>] [--status ok|failed|timeout|other] [--limit N] [--recommendations-limit N] [--data-dir ./var] [--csv <path>] [--root <pack-root>] [--json]`

Command behavior:

- Builds telemetry report first, then maps `tuning_playbook` actions into immediate caller artifacts.
- Emits candidate `process` flag overrides and unified diffs for prompt/pipeline files when context is available from run metadata + pipeline assets.
- Does not mutate files; output is suggestion-only.

Primary implementation file:

- `src/codex_farm/autotune.py`

## Dashboard artifacts

Output files:

- `<out_dir>/index.html`
- `<out_dir>/assets/dashboard_data.json`
- `<out_dir>/assets/dashboard.js`
- `<out_dir>/assets/style.css`

`index.html` embeds an inline copy of `dashboard_data.json` and dashboard JS tries inline data first, then `fetch("assets/dashboard_data.json")`. This keeps the dashboard usable when opened from `file://` in environments where local fetch is restricted.

## Notes

- CSV writes are append-only and header-safe (header is written only when file is empty).
- Logging is best-effort; telemetry write failures do not fail task execution.
- Dashboard generation is read-only against telemetry input files.
- Incremental reused tasks do not emit new telemetry rows because no Codex subprocess runs; inspect reuse via `run create --json`, `process --json`, and `run tasks --json`.

## Task doc merges from `docs/tasks`

Historical task docs merged into this chunk to preserve telemetry/autotune contract context:

- `2026-02-28_15.02.31-telemetry-reporting-api.md`:
  - introduced caller-facing `run telemetry --json` report contract and embedded `telemetry_report` in `process --json`.
  - locked schema-version strategy (`schema_version=2`) with additive automation surfaces (`insights`, `tuning_playbook`) instead of expanding only free-form text recommendations.
  - documented legacy-row compatibility rule: missing newer CSV columns (for example `output_payload_present`) require fallback handling to avoid false recommendation spikes.
  - recorded known gap from task history: recommendation synthesis is deterministic/rule-based today and not yet ranked by longer-horizon outcome deltas.
- `2026-02-28_10.29.20-autotune-cli-diff-emitter.md`:
  - added `run autotune --json` translation layer from telemetry `tuning_playbook` to actionable artifacts (flag overrides, command preview, prompt/pipeline unified diffs).
  - preserved non-mutating contract: command emits reviewable suggestions only; callers choose if/how to apply patches.
  - captured practical boundary from task history: timeout tuning currently maps best to pipeline diff output because `process` has no direct `--timeout` flag.

## Merged discoveries from `docs/understandings`

- `2026-02-22_14.47.53`: `codex_exec_activity.csv` already carries enough context for static analytics without DB joins (status, durations, tokens, source, pipeline, run/task metadata).
- `2026-02-22_14.47.53`: Dashboard output should stay read-only over telemetry input and keep inline JSON plus fetch fallback so `file://` loads still work.
- `2026-02-22_14.47.53`: Primary paths to preserve are CLI `codex-farm stats-dashboard` and implementation in `src/codex_farm/analytics_dashboard.py`.
- `2026-02-22_19.40.00`: All real Codex subprocess execution paths converge at `src/codex_farm/codex_exec.py::run_codex_exec`, so token telemetry should stay centralized there.
- `2026-02-22_19.40.00`: Worker-based modes (`process`, `go`, `worker`) call Codex through `worker_loop`; `one` is the only non-worker caller and still uses `run_codex_exec`.
- `2026-02-22_19.40.00`: Per-call token usage comes from Codex JSONL `turn.completed.usage` and should be persisted once per invocation row.
- `2026-02-28_02.55.22`: Telemetry rows now include optional `reasoning_effort` so model+effort choices can be audited for external caller runs.
- `2026-02-28_09.21.54`: `codex_exec_activity.csv` is part of the output-verification visibility contract and should remain sufficient to explain per-call acceptance/rejection outcomes alongside `run tasks --json` and `run errors --json`.
- `2026-02-28_09.33.49`: Telemetry rows now include Heads Up prompt-adaptation context and distiller calls (`source=heads_up.learn`) so adaptation effects are auditable per invocation.
- `2026-02-28_14.50.39`: Telemetry rows now include structured pass-forward context (`heads_up_tip_*`, retry context fields), failure classification (`failure_category`, `rate_limit_suspected`), Codex event summaries, and output fingerprint/preview fields so external callers can tune prompts from one CSV stream.
- `2026-02-28_15.02.31`: `run telemetry --json` now provides a first-class caller contract for recommendation-ready aggregation over telemetry rows, including terminal task error context when scoped by run.
- `2026-02-28_15.20.27`: Telemetry report schema version `2` adds caller-automation surfaces (`insights`, `tuning_playbook`) so external programs can auto-select model/effort shifts, tighten prompt pass-forward strategies, and target schema/input fixes from one report payload.
- `2026-02-28_10.29.20`: `run autotune --json` now translates telemetry playbook entries into concrete override and diff suggestions, so callers can apply remediation quickly without writing their own mapping layer.

Known trap:

- Duplicating telemetry writes outside `run_codex_exec` creates double-counting and inconsistent context coverage across command paths.

## Merged understanding notes (`docs/understandings`)

### 2026-03-02_01.26.15 - Handle mixed-schema telemetry rows robustly
- `codex_exec_activity.csv` contains mixed historical schemas while header can remain older.
- Prompt/text extraction should parse with positional/csv-reader logic and row-length handling, not strict `DictReader` by name.
- Current payload alignment in this workspace follows 56-column schema shape from `src/codex_farm/codex_exec.py::_USAGE_LOG_FIELDS`.
