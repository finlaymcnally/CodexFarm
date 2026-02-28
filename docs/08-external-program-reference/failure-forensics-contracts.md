---
summary: "Caller-facing failure-forensics contract: run forensics JSON rows and on-disk bundle metadata."
read_when:
  - "When external callers need stable failed-attempt evidence lookup"
  - "When consuming run forensics --json or parsing metadata.json bundles"
---

# Failure Forensics Contracts

## Purpose

Failure forensics is an additive evidence surface for failed attempts.

- Queue/task state remains in `run status|tasks|errors`.
- Evidence lookup is in `run forensics`.
- Raw evidence files live under `<data_dir>/forensics/`.

## CLI contract

Command:

```bash
codex-farm run forensics --run-id <run_id> [--task-id <task_id>] [--data-dir ./var] [--json]
```

Behavior:

- Reads from SQLite `task_forensics` rows only.
- Does not scan directories recursively.
- Sort order is newest first (`created_at DESC`, then `forensics_id DESC`).

JSON row fields:

- `forensics_id`
- `source` (`worker` or `one`)
- `run_id`
- `task_id`
- `pipeline_id`
- `attempt_index`
- `terminal`
- `input_path`
- `rel_output_path`
- `failure_stage`
- `failure_category`
- `error_summary`
- `bundle_dir`
- `metadata_path`
- `raw_output_path`
- `created_at`

## Bundle layout

Worker/run path:

```text
<data_dir>/forensics/runs/<run_id>/<task_id>/attempt-<attempt_index>/
```

`one` command path:

```text
<data_dir>/forensics/one/<forensics_id>/
```

Common files:

- `metadata.json` (always)
- `prompt.txt` (when prompt text exists)
- `input.snapshot<suffix>` (when input file exists)
- `schema.json` (when resolved schema file exists)
- `output.raw.json` (when output file exists at capture time)
- `stderr_tail.txt` (when non-empty)
- `stdout_tail.txt` (when non-empty)

## `metadata.json` contract

Top-level fields include:

- `schema_version` (currently `1`)
- IDs and context: `forensics_id`, `source`, `run_id`, `task_id`, `pipeline_id`, `attempt_index`, `worker_id`
- classification: `terminal`, `failure_stage`, `failure_category`
- messages: `error_message_full`, `error_message_summary`, optional `previous_error`
- paths/hashes: `input_path`, `input_hash`, `rel_output_path`, `output_path`, `schema_path`
- `runtime_context` object (execution settings and branch details)
- `artifacts` object with relative artifact paths, byte counts, and SHA-256

`artifacts` keys are optional based on available files.

## Caller notes

- `run errors --json` remains unchanged; `run forensics --json` is the additive artifact index.
- `one` failures may print `Forensics bundle: <abs path>` to stderr when capture succeeds.
- Timeout failures may produce metadata-only bundles for raw output because codex timeout cleanup removes temporary payload files before capture.

