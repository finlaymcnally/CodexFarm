---
summary: "How codex-farm finds a pipeline pack, validates pipeline JSON assets, and resolves stable run/workspace roots."
read_when:
  - "When changing pipeline JSON fields/defaults, prompt/schema path behavior, or root precedence"
  - "When debugging 'Unknown pipeline', missing asset files, or unexpected codex --cd directories"
---

# 02: Pipeline Assets and Root Resolution

This chunk is the configuration gate that runs before any useful work can happen.

If this layer resolves the wrong root, loads the wrong pack, or accepts a bad pipeline file, everything downstream looks broken even when worker logic is fine.

## What this chunk owns

- Finding the active pipeline pack root (`pipelines/`, `prompts/`, `schemas/`).
- Validating each `pipelines/*.json` file against allowed fields and defaults.
- Resolving prompt/schema paths from repo-relative strings to absolute filesystem paths.
- Enforcing pipeline ID uniqueness.
- Resolving and validating optional workspace override paths.
- Persisting root/workspace decisions into run config so resumed workers behave consistently.

## Fast mental model

```text
CLI command
  -> resolve_farm_root(...)
  -> load_pipelines(<farm_root>/pipelines)
  -> pick one PipelineSpec by pipeline_id
  -> create run with config_json { farm_root, optional workspace_root, ... }
  -> worker later re-reads config_json and uses the same roots
```

## Primary code map

- `src/codex_farm/paths.py`
  - Root discovery (`resolve_farm_root`)
  - Root validation against sentinel folders
  - Data-dir/db path helpers
- `src/codex_farm/pipeline_spec.py`
  - On-disk pipeline schema (`PipelineSpecModel`)
  - Runtime immutable spec (`PipelineSpec`)
  - Repo-relative asset resolution + existence checks
  - Prompt template substitution (`{{INPUT_PATH}}`)
- `src/codex_farm/cli.py`
  - Root/workspace option validation wrappers
  - Pipeline lookup and user-facing failures
  - Run config persistence (`farm_root`, optional `workspace_root`)
- `src/codex_farm/worker.py`
  - Re-resolves `farm_root` from persisted run config first
  - Re-resolves `workspace_root` override from persisted config
  - Computes final Codex `--cd` directory from override or `codex_cd_mode`

## Root discovery contract (`resolve_farm_root`)

The active pack root must directly contain all three folders:

- `pipelines/`
- `prompts/`
- `schemas/`

Resolution precedence is strict:

1. Explicit CLI override (`--root`) if provided.
2. `CODEX_FARM_ROOT` environment variable.
3. Upward search from a start path/current working directory/module path.

If a candidate root exists but misses required folders, it fails immediately with a `FileNotFoundError` naming missing folders.

If no root can be found at all, it fails with guidance to pass `--root` or set `CODEX_FARM_ROOT`.

### Why this matters

- `--root` always wins over environment configuration.
- A typo in one sentinel folder name can make the repo appear "not found."
- Worker resume behavior depends on this being deterministic (see run config section below).

## Pipeline file loading contract (`load_pipelines`)

`load_pipelines(pipelines_dir)` loads every `*.json` file and returns:

- `dict[pipeline_id, PipelineSpec]`

Each JSON file is parsed through `PipelineSpecModel`:

- unknown keys are rejected (`extra="forbid"`).
- strings are whitespace-stripped.
- required non-empty fields:
  - `pipeline_id`
  - `description`
  - `prompt_template_path`
  - `output_schema_path`
- defaults:
  - `input_glob_default`: `"**/*.json"`
  - `output_ext`: `".json"` (must start with `.`)
  - `codex_model`: `"gpt-5.3-codex-spark"`
  - `codex_sandbox`: `"read-only"`
  - `codex_ask_for_approval`: `"never"`
  - `codex_web_search`: `"disabled"`
  - `codex_timeout_seconds`: `180` (minimum `1`)
  - `codex_cd_mode`: `"asset_root"` with allowed values:
    - `"asset_root"`
    - `"input_dir"`
    - `"input_file_dir"`

After field validation, prompt/schema paths are resolved relative to `pipelines_dir.parent` and must exist on disk.

Duplicate `pipeline_id` across files is a hard error.

All validation/load errors are re-raised as:

- `ValueError("Invalid pipeline file <path>: <details>")`

## Prompt template rendering contract

`render_prompt_template(template_path, input_path)` does one substitution only:

- Replaces every `{{INPUT_PATH}}` literal with the absolute resolved input file path.

There is no general template engine. If you need more placeholders, code changes are required.

## Workspace override and cd-mode rules

`--workspace-root` is treated as an explicit override, not a fallback default.

Validation rules:

- In CLI entrypoints, `--workspace-root` must exist and be a directory.
- In workers, persisted `workspace_root` from run config is revalidated before task execution.

Final Codex `--cd` resolution:

- If `workspace_root` override exists: use it.
- Else if pipeline `codex_cd_mode == asset_root`: use resolved farm root.
- Else if `codex_cd_mode == input_dir`: use run-level input directory.
- Else (`input_file_dir`): use the task input file parent.

Special case in `one` command:

- There is no run-level input root, so `input_dir` and `input_file_dir` both resolve to the input file parent.

If computed `--cd` does not exist, task fails with a clear configuration error.

## Run-config persistence contract (important for resume/retry)

When runs are created (`run create`, `process`, `go`), config JSON always stores:

- absolute `farm_root`

And stores `workspace_root` only when explicitly provided.

Workers later prefer persisted config over worker CLI defaults:

- persisted `farm_root` wins
- persisted `workspace_root` wins when present

This prevents resumed tasks from silently switching pipeline packs or cd behavior.

## Known failure signatures and where to look

- Error mentions missing `pipelines/prompts/schemas`:
  - Root candidate is invalid.
  - Check `--root`, `CODEX_FARM_ROOT`, and folder layout.
- `Unknown pipeline '<id>'`:
  - Pipeline ID not found in loaded map for selected farm root.
  - Check selected root and `pipelines/*.json` IDs.
- `Invalid pipeline file ...`:
  - Broken JSON, unknown field, invalid `codex_cd_mode`, bad `output_ext`, or missing referenced asset file.
- `workspace_root does not exist or is not a directory`:
  - Persisted override in run config points to stale/moved path.
- `Computed codex --cd directory does not exist`:
  - cd-mode resolved to a path missing at execution time.

## Tests that lock this chunk's behavior

- `tests/test_paths.py`
  - `--root` precedence over `CODEX_FARM_ROOT`
  - invalid root/env rejection
- `tests/test_pipeline_spec.py`
  - pipeline loading, prompt substitution, cd-mode validation
- `tests/test_cli_integration_contracts.py`
  - `pipelines list --root` precedence
  - `process --workspace-root` JSON contract and cd behavior
- `tests/test_worker.py`
  - worker cd selection for each `codex_cd_mode`
  - persisted workspace override wins in worker flow
- `tests/test_fake_codex_pipeline_pack_demo.py`
  - real CLI flow with external demo pack under `examples/pipeline_pack_demo`

## How this chunk hands off to the next ones

- To chunk 03 (`docs/03-run-planning-and-queue-state/03-run-planning-and-queue-state_readme.md`):
  - provides stable pipeline metadata needed to enqueue tasks.
- To chunk 04 (`docs/04-worker-execution-and-retries/04-worker-execution-and-retries_readme.md`):
  - provides `farm_root`, optional `workspace_root`, and `codex_cd_mode` that drive per-task execution context.
- To chunk 05 (`docs/05-codex-exec-and-schema-gate/05-codex-exec-and-schema-gate_readme.md`):
  - provides resolved prompt/schema assets used by the Codex subprocess and local schema gate.

## Safe change checklist for future AI coders

- If adding a pipeline field:
  - update `PipelineSpecModel`
  - update `PipelineSpec`
  - thread field through `_to_spec`
  - add/adjust tests in `tests/test_pipeline_spec.py`
- If changing root precedence:
  - update `resolve_farm_root`
  - add tests in `tests/test_paths.py` and CLI integration coverage
  - update `docs/IMPORTANT CONVENTIONS.md`
- If changing `codex_cd_mode` semantics:
  - update CLI one-file resolver and worker resolver together
  - update tests in `tests/test_worker.py`
  - update chunk docs 02 and 04 so they stay aligned
