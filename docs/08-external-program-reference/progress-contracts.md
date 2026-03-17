---
summary: "Caller-facing progress/snapshot contracts for spinner-style integrations."
read_when:
  - "When an external caller needs live or polled run progress for UI spinners/progress trackers"
  - "When integrating CodexFarm process output streams with machine parsing"
---

# Progress Contracts

This document defines the stable machine-facing progress surfaces for external programs.

## `run progress --json`

Primary polling endpoint for spinner/progress UIs:

- `codex-farm run progress --run-id <id> --json`
- Optional continuous stream: `codex-farm run progress --run-id <id> --watch --json`

Snapshot fields:

- `run_id`, `pipeline_id`, `status`, `control_state`
- `counts`: `queued`, `running`, `done`, `error`, `canceled`, `total`
- `snapshot_at_utc`
- `progress`:
  - `completed` (`done + error + canceled`)
  - `remaining`
  - `percent_complete`
- `running_tasks`: bounded list of active tasks (task id/input path/lease fields)
- `recent_errors`: bounded list of latest terminal error tasks

Watch behavior:

- emits one JSON object per poll interval
- stops automatically when run state becomes terminal (`done`, `error`, or canceled)

## `process --progress-events`

Opt-in event stream for callers already using `process`:

- `codex-farm process ... --json --progress-events`

Contract guarantees:

- stdout stays a single final JSON payload (same `process --json` contract).
- progress events are emitted on stderr only.
- each event line starts with: `__codex_farm_progress__ `
- suffix after prefix is one JSON object.

Event types:

- `run_started`: initial snapshot plus `workers`.
- `run_progress`: periodic snapshot during worker polling.
- `run_finished`: terminal snapshot plus `exit_code` and `worker_exit_codes`.

Common event fields:

- `event`
- `schema_version` (currently `1`)
- `emitted_at_utc`
- run snapshot payload fields (`run_id`, `status`, `counts`, `progress`, task snippets)
- additive `runtime_mode`
- additive `session_summary` object with `active_sessions`, `sessions_started`, `sessions_finished`, `current_session_task_count`, `session_count`, `fresh_session_count`, `session_turn_count_total`, `session_failures`, and `tasks_per_session_summary`

## Integration guidance

- Use `run progress --watch --json` when your caller orchestrates `run create` + workers separately.
- Use `process --progress-events` when your caller already runs `process` and can parse stderr lines.
- Treat unknown additive fields as forward-compatible extensions.
