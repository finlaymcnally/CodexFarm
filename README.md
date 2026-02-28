# codex-farm

`codex-farm` is a local CLI tool that runs many `codex exec` jobs over files, validates outputs against JSON Schema, retries failures, and tracks progress in SQLite.

Programmatic callers: see [CONNECTION_INSTRUCTIONS.md](CONNECTION_INSTRUCTIONS.md).

## What You Can Do With It

If you have "a folder of input files" and want "a folder of structured output files", codex-farm gives you:

- Pipeline-driven execution (prompt + schema + model settings in files).
- Batch processing with worker concurrency.
- Resume/retry behavior with run/task status tracking.
- Strict output validation before files are accepted.

## 5-Minute Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
codex-farm doctor
```

`doctor` checks Python, your `codex` CLI install, and a non-interactive Codex smoke run.

## First Run (Copy/Paste)

This processes the sample recipe inputs in this repo:

```bash
codex-farm process \
  --pipeline recipe.schemaorg.normalize.v1 \
  --in examples/schemaorg_recipes_in \
  --out var/demo_out \
  --workers 4 \
  --data-dir ./var \
  --json
```

Then inspect results:

```bash
codex-farm run status --run-id <run_id> --data-dir ./var --json
codex-farm run errors --run-id <run_id> --data-dir ./var --json
```

Outputs are written under `var/demo_out`.

## Day-To-Day Workflows

### 1) Scripted Batch (`process`)

Use this for automation and shell scripts:

```bash
codex-farm process \
  --pipeline recipe.schemaorg.normalize.v1 \
  --in /abs/path/to/inputs \
  --out /abs/path/to/outputs \
  --workers 8 \
  --data-dir ./var
```

Optional runtime overrides on `one`, `run create`, `process`, and `go`:

- `--model` / `--codex-model`
- `--reasoning-effort` (aliases: `--effort`, `--thinking-effort`, `--codex-reasoning-effort`, `--codex-thinking-effort`)
- `--output-schema`
- `--workspace-root`

On run-based flows, these selections are saved with the run and reused by workers.

### 2) Interactive Inbox/Outbox (`go`)

Use this when you want a guided flow:

```bash
codex-farm init --data-dir ./var
cp examples/schemaorg_recipes_in/*.json ./var/inbox/
codex-farm go --data-dir ./var
```

`go` asks you to pick a pipeline and worker count, reads from `var/inbox`, and writes to:

- `var/outbox/<pipeline_id>/<YYYY-MM-DD_HH.MM.SS>/`

### 3) Queue First, Worker Later

If you want explicit control:

```bash
codex-farm run create --pipeline recipe.schemaorg.normalize.v1 --in ./in --out ./out --data-dir ./var
codex-farm worker --data-dir ./var --run-id <run_id>
```

## Pipeline Pack Basics

A pipeline pack is just three folders:

- `pipelines/` JSON pipeline specs
- `prompts/` prompt templates
- `schemas/` JSON Schemas

You can use the built-in pack in this repo, or an external pack via `--root`:

```bash
codex-farm pipelines list --root /abs/path/to/pack --json
codex-farm lint --root /abs/path/to/pack --json
codex-farm models list --json
codex-farm process \
  --root /abs/path/to/pack \
  --pipeline demo.echo.v1 \
  --in /abs/path/to/inputs \
  --out /abs/path/to/outputs \
  --json
```

Codex working directory is chosen by:

1. `--workspace-root` when provided.
2. Otherwise pipeline `codex_cd_mode` (`asset_root`, `input_dir`, `input_file_dir`).

## How codex-farm Works (Merged Overview)

codex-farm coordinates three layers:

1. Pipeline definitions (`pipelines/*.json`) with prompt/schema/runtime settings.
2. SQLite run/task queue (`<data_dir>/codex_farm.sqlite3`).
3. Worker loops that lease tasks, call Codex, validate output, and set task state.

End-to-end flow:

1. `process` (or `run create`) creates a run and enqueues file tasks.
2. Workers lease one task at a time in a DB transaction.
3. Each task runs `codex exec` with the pipeline prompt + schema.
4. Output is validated locally with `jsonschema`.
5. Task becomes `done`, requeued, or terminal `error`.

Important behavior:

- Leasing uses an atomic claim/update to avoid duplicate processing.
- Attempts increment when a task is claimed.
- Expired leases allow stuck `running` tasks to be reclaimed.
- Codex output is written to a temp file, then atomically moved into place.
- Non-zero Codex exits can still be accepted if output exists and passes local schema validation.
- Invalid output files are deleted before retry/error.

## Command Reference

- `doctor`: verify Python + Codex prerequisites.
- `init`: create data dir + inbox/outbox + DB schema.
- `lint`: read-only pack/schema preflight (`--root` or `--schema`).
- `pipelines list`: list available pipelines.
- `pipelines new`: scaffold new pipeline/prompt/schema files.
- `one`: process one file.
- `run create`: enqueue tasks only.
- `run status`: run-level counts/status.
- `run tasks`: list task rows.
- `run errors`: list terminal task failures.
- `worker`: run worker loop directly.
- `process`: create run + process tasks with N workers.
- `go`: interactive inbox/outbox workflow.
- `stats-dashboard`: build static telemetry dashboard from CSV.
- `models list`: list visible Codex models from local metadata.

## Troubleshooting

- `Unknown pipeline '<id>'`
  - Run `codex-farm pipelines list` and verify the pipeline file exists.
- Pipeline load errors about missing prompt/schema files
  - Ensure paths in pipeline JSON are valid relative paths inside the selected root.
- Tasks ending in `error`
  - Inspect `run errors --json`; then repro a single file with `one` for faster iteration.
- Tasks stuck in `running`
  - Wait for lease expiry or reduce `--lease-seconds`; expired leases are reclaimable.
- Root detection fails
  - Ensure root contains `pipelines/`, `prompts/`, and `schemas/`, or pass `--root`.

## Folder Notes

- `pipelines/`: pipeline config JSON files.
- `prompts/`: prompt templates used by workers.
- `schemas/`: JSON Schemas enforced by Codex and validated locally.
- `examples/`: sample inputs and structural examples.
- `examples/pipeline_pack_demo/`: tiny external pack for `--root` smoke tests.

## Deeper Docs

- Human-readable internals: [docs/how-codex-farm-works.md](docs/how-codex-farm-works.md)
- AI deep-dive internals: [docs/how-codex-farm-works-for-AI.md](docs/how-codex-farm-works-for-AI.md)
- Fast onboarding context: [docs/AI_Context.md](docs/AI_Context.md)
- Oracle troubleshooting + stable run commands: [oracle/README.md](oracle/README.md)
