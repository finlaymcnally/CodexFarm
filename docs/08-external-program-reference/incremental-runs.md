---
summary: "Caller-facing contract for incremental run reuse flags and JSON payloads."
read_when:
  - "When automating reruns and you want to skip unchanged inputs safely"
  - "When consuming run create/process/task JSON to understand reused vs queued work"
---

# Incremental Runs Contract

Incremental mode is a planning-time feature for run-based commands. It reuses prior successful outputs for unchanged inputs and creates those tasks directly in `done` status before workers start.

Supported commands:

- `codex-farm run create ... --incremental`
- `codex-farm run create ... --incremental-from <run_id>`
- `codex-farm process ... --incremental`
- `codex-farm process ... --incremental-from <run_id>`
- `codex-farm go ... --incremental`
- `codex-farm go ... --incremental-from <run_id>`

`one` does not support incremental mode.

## Safety Contract

Reuse requires both:

1. matching input bytes (`tasks.input_hash`)
2. matching run execution compatibility (`runs.execution_fingerprint`)

This avoids unsafe hash-only reuse when prompt/schema/model/path context changed.

If no compatible prior run exists in auto mode, tasks are queued normally.

If `--incremental-from <run_id>` is provided, the command fails unless that run exists, is terminal (`done` or `error`), and is fingerprint-compatible.

## JSON Contract

`run create --json` and `process --json` include an additive `incremental` object:

```json
{
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
  }
}
```

When incremental mode is off, shape remains stable with `enabled=false` and zero counters.

## Task Inspection Contract

`run tasks --json` includes additive reuse fields:

- `reused` (`true|false`)
- `reused_from_run_id` (`string|null`)
- `reused_from_task_id` (`string|null`)

Text mode shows `[reused]` markers on reused task rows.

## Telemetry Note

Reused tasks do not execute Codex subprocesses, so no new telemetry CSV rows are generated for them. Reuse visibility lives in run/task JSON payloads.
