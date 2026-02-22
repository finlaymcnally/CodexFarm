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
- `prompt_text`: full prompt sent to Codex
- `prompt_sha256`, `prompt_chars`: prompt fingerprint and size
- `stderr_tail`, `stdout_tail`: recent non-JSON stderr/stdout text for diagnostics
- `output_payload_present`, `output_bytes`, `output_path`: payload presence and file details
- `source`: caller path (`one` or `worker`)
- `pipeline_id`, `run_id`, `task_id`, `worker_id`, `input_path`: execution context when available
- `logged_at_utc`, `started_at_utc`, `finished_at_utc`: timestamps in UTC
- `model`, `sandbox`, `ask_for_approval`, `web_search`, `timeout_seconds`, `cd_dir`, `output_schema_path`, `thread_id`, `usage_json`: runtime settings and raw usage context

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
