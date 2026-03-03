---
summary: "Caller-facing telemetry contract for codex_exec_activity.csv, including prompt-tuning and failure-analysis signals."
read_when:
  - "When an external caller consumes codex_exec_activity.csv to tune prompts or triage recurring failures"
  - "When building automation that correlates prompt hints, retries, and output quality"
---

# Telemetry Contracts

`codex_exec_activity.csv` is append-only and emits one row per `run_codex_exec(...)` call.

Preferred caller API (instead of parsing CSV directly):

- `codex-farm run telemetry --run-id <id> --data-dir ./var --json`
- `codex-farm run autotune --run-id <id> --data-dir ./var --json`
- `codex-farm process ... --json` now includes `telemetry_report` by default (toggle with `--no-telemetry-report`)

## Core IDs for joins

- `run_id`, `task_id`, `worker_id`, `pipeline_id`, `source`, `input_path`
- `attempt_index` (legacy lease-claim attempt index; kept for compatibility)
- `lease_claim_index` (explicit lease-claim index)
- `execution_attempt_index` (explicit real execution-start index)

Use `run_id` + `task_id` to correlate telemetry with:

- `codex-farm run tasks --json`
- `codex-farm run errors --json`

## Prompt and pass-forward signals

- `prompt_text`, `prompt_sha256`, `prompt_chars`
- `retry_context_applied`, `retry_previous_error`, `retry_previous_error_sha256`
- `heads_up_applied`, `heads_up_tip_count`, `heads_up_input_signature`
- `heads_up_tip_ids_json`, `heads_up_tip_texts_json`, `heads_up_tip_scores_json`

These fields are designed so callers can identify:

- repeated prior-error patterns that keep getting passed forward
- which Heads Up hints were active on successful/failed attempts

## Failure and runtime signals

- `status`, `exit_code`, `accepted_nonzero_exit`
- `failure_category` (`accepted_nonzero_exit`, `timeout`, `nonzero_exit_no_payload`, `zero_exit_no_payload`, or empty for normal exit-0 success)
- `rate_limit_suspected` (parsed from stderr/stdout tails)
- `stderr_tail`, `stdout_tail`

## Output-detail signals

- `output_payload_present`, `output_bytes`, `output_path`
- `output_sha256`
- `output_preview`, `output_preview_chars`, `output_preview_truncated`

`output_preview` is intentionally truncated and should be treated as diagnostic context, not canonical output. Use `output_path` for full payload retrieval.

## Codex event and token signals

- `tokens_input`, `tokens_cached_input`, `tokens_output`, `tokens_reasoning`, `tokens_total`, `usage_json`
- `codex_event_count`, `codex_event_types_json`
- `thread_id`

## Practical caller loop

1. Group rows by `pipeline_id` + `failure_category` + `retry_previous_error_sha256` to find recurring failure modes.
2. Compare success/failure rates when specific `heads_up_tip_ids_json` combinations are present.
3. Inspect `output_preview` and `output_sha256` deltas for fast quality checks before opening full outputs.
4. Apply prompt/template changes, then monitor the same groups over subsequent runs.

## `run telemetry --json` report shape

The report includes:

- `schema_version`: currently `2`.
- `summary`: status counts, retry/heads-up/rate-limit totals, duration+token aggregates.
  - Includes additive reasoning-token aggregates: `tokens_reasoning_total`, `tokens_reasoning_avg_per_call`.
- `failure_patterns`: grouped failure categories, schema paths/issue types, repeated retry errors.
- `heads_up_patterns`: per-tip effectiveness rows.
- `insights`: higher-level breakdowns for caller automation:
- `model_reasoning_breakdown` (success/tokens/duration by model+effort)
  - Includes additive `tokens_reasoning_avg_per_call` per model+effort row.
- `prompt_fingerprint_breakdown` (success by prompt hash)
- `input_failure_hotspots` (problematic input paths)
- `reasoning_signals` (Codex event stream/turn completion visibility)
- `pass_forward_effectiveness` (delta when retry/Heads Up context is applied)
- `terminal_errors`: run-level task errors (from SQLite) that can occur after Codex subprocess success.
- `recommendations`: evidence-backed suggestions in four categories:
- `prompt`
- `input_data`
- `output_schema`
- `runtime`
- `tuning_playbook`: concrete patch candidates grouped by execution surface:
- `prompt_edits`
- `input_prechecks`
- `schema_edits`
- `runtime_tuning`
- `model_tuning`

## `run autotune --json` contract

This command consumes telemetry report output and emits immediate remediation artifacts:

- `flag_overrides`: suggested `process` flags (`--workers`, `--model`, `--reasoning-effort`) with source evidence.
- `command_preview`: ready-to-run command string with suggested overrides.
- `prompt_template_diff`: unified diff for prompt template updates when pipeline context is resolvable.
- `pipeline_config_diff`: unified diff for pipeline JSON defaults (for example model/effort/timeout) when resolvable.

`run autotune` is non-mutating. Callers decide whether/how to apply emitted diffs.
