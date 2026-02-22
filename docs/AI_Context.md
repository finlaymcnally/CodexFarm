---
summary: "AI onboarding context for codex-farm: architecture, contracts, workflow, and debugging map."
read_when:
  - "When an AI agent or new developer needs a fast technical model of codex-farm"
  - "When planning code changes that touch CLI contracts, run/task state, workers, or Codex execution"
---

# AI Context: codex-farm

This project is a local orchestration tool for running many `codex exec` tasks over files with retries, status tracking, and schema validation.

codex-farm does not host models or serve an API. It shells out to an installed `codex` CLI, manages queue state in SQLite, and enforces output contracts.

## What it does (in one model)

codex-farm is built from three layers:

1. Pipeline assets: `pipelines/`, `prompts/`, `schemas/`
2. Queue state: `runs` and `tasks` in SQLite
3. Execution: worker loops that lease tasks, call Codex, validate output, and mark status

Main batch path:

1. `process` creates a run and task rows from an input directory.
2. Worker threads claim tasks via DB leasing.
3. Each task runs Codex with pipeline settings and a rendered prompt.
4. Output is schema-validated locally.
5. Task becomes `done`, requeued, or terminal `error`.

## Core modules and ownership

- `src/codex_farm/cli.py`: command surface and orchestration entrypoints
- `src/codex_farm/pipeline_spec.py`: pipeline JSON validation + prompt rendering
- `src/codex_farm/paths.py`: farm root and data-dir resolution rules
- `src/codex_farm/db.py`: SQLite schema, leasing, task/run state transitions
- `src/codex_farm/worker.py`: worker loop, retry policy, terminal error handling
- `src/codex_farm/codex_exec.py`: Codex subprocess contract + atomic output writes + telemetry
- `src/codex_farm/schema_utils.py`: local JSON/JSON Schema validation
- `src/codex_farm/doctor.py`: environment and Codex smoke checks

## Important runtime contracts

### Root and workspace selection

- Asset root precedence: `--root` -> `CODEX_FARM_ROOT` -> upward auto-discovery.
- Chosen root must contain `pipelines/`, `prompts/`, and `schemas/`.
- `--workspace-root` explicitly overrides Codex `--cd`.
- If omitted, pipeline `codex_cd_mode` chooses `--cd` (`asset_root`, `input_dir`, `input_file_dir`).

### Run persistence and resumability

- `run create` stores absolute `farm_root` and optional explicit `workspace_root` in `runs.config_json`.
- Workers should use persisted run config so resumed work keeps the same pipeline pack and `--cd` behavior.

### Task leasing and retries

- Workers lease one task at a time inside a DB transaction (`BEGIN IMMEDIATE`).
- Claiming increments `attempts` when lease is taken.
- Failures requeue until `attempts >= max_attempts`, then task is marked `error`.
- Expired leases allow another worker to reclaim a previously running task.

### Output acceptance

- Codex receives `--output-schema` and writes to a temp output file.
- codex-farm only promotes temp -> final path via atomic rename.
- Local schema validation is final authority; invalid outputs are deleted.
- Non-zero Codex exit can still be accepted if non-empty output was produced; local validation decides final pass/fail.

### JSON machine contracts

- `process --json` prints a single JSON object to stdout (progress goes to stderr).
- `run status --json`, `run tasks --json`, and `run errors --json` are stable inspection APIs.

## CLI modes you will see most

- `one`: single input file to single output file
- `run create`: queue-only setup for a batch
- `worker`: explicit worker loop execution
- `process`: create run + execute workers end-to-end
- `go`: interactive inbox/outbox flow
- `pipelines list/new`: discover or scaffold pipeline assets
- `doctor`: prerequisites and Codex smoke test

## Data locations

- Default data dir: `./var` (resolved to absolute path)
- DB: `<data_dir>/codex_farm.sqlite3`
- Interactive input: `<data_dir>/inbox/`
- Interactive output: `<data_dir>/outbox/<pipeline_id>/<timestamp>/`
- Codex telemetry CSV: `<data_dir>/codex_exec_activity.csv` (or `./var/...` for `one`)

## What to read before editing specific areas

- CLI contract changes: `docs/01-cli-and-command-contracts/01-cli-and-command-contracts_readme.md`
- Pipeline fields/root behavior: `docs/02-pipeline-assets-and-root-resolution/02-pipeline-assets-and-root-resolution_readme.md`
- Run/task state: `docs/03-run-planning-and-queue-state/03-run-planning-and-queue-state_readme.md`
- Worker/retry behavior: `docs/04-worker-execution-and-retries/04-worker-execution-and-retries_readme.md`
- Codex subprocess/schema gate: `docs/05-codex-exec-and-schema-gate/05-codex-exec-and-schema-gate_readme.md`
- Cross-boundary tests: `docs/06-integration-contracts-and-fixtures/06-integration-contracts-and-fixtures_readme.md`
- Global hidden rules: `docs/IMPORTANT CONVENTIONS.md`
- Full deep dive: `docs/how-codex-farm-works-for-AI.md`

## Local development defaults

- Python: 3.11+
- Standard setup:
  - `python3 -m venv .venv`
  - `source .venv/bin/activate`
  - `pip install -e ".[dev]"`
- Test command: `pytest` (inside `.venv`)

## Fast debugging checklist

1. Confirm chosen root (`--root`/env/discovery) actually contains the three asset folders.
2. Verify pipeline prompt/schema paths resolve and files exist.
3. Check `run status --json` counts and `run errors --json` details.
4. Confirm workspace selection (`--workspace-root` vs `codex_cd_mode`) matches intent.
5. If Codex exits non-zero, inspect whether output file still exists and whether local schema validation rejected it.
6. Check `<data_dir>/codex_exec_activity.csv` for per-call timing/token/error context.
