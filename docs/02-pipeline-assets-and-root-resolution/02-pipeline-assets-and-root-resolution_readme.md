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
- Read-only pack/schema lint scans (`codex-farm lint`) that reuse the same pipeline/schema path rules without fail-fast runtime exits.
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
  - Pipeline source-file provenance (`PipelineSpec.source_path`) used for run-asset freezing
  - Reusable single-file parsing (`parse_pipeline_model_file`)
  - Repo-relative asset resolution + existence checks
  - Prompt template substitution (`{{INPUT_PATH}}`)
- `src/codex_farm/pack_lint.py`
  - Read-only pack and schema lint orchestration
  - Finding-code classification (`error` vs `warning`)
  - Near-miss root diagnostics for explicit `lint --root` paths
- `src/codex_farm/cli.py`
  - Root/workspace option validation wrappers
  - Pipeline lookup and user-facing failures
  - Run config persistence (`farm_root`, optional `workspace_root`, optional model/effort overrides)
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

Lint exception:

- `lint --root <path>` does not call `resolve_farm_root(...)` when `--root` is explicitly provided.
- If that directory exists but is missing sentinels, lint reports `pack.missing_sentinel_dirs` as a finding so users can debug near-miss packs in one pass.
- Lint still reuses pipeline/schema validation seams (`parse_pipeline_model_file(...)`, repo-relative path checks) and then maps failures to finding codes, so lint diagnostics stay aligned with runtime validation without fail-fast CLI argument exits.

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
  - `codex_reasoning_effort`: `null` (optional; allowed: `none|minimal|low|medium|high|xhigh`)
  - `codex_timeout_seconds`: `180` (minimum `1`)
  - `codex_cd_mode`: `"asset_root"` with allowed values:
    - `"asset_root"`
    - `"input_dir"`
    - `"input_file_dir"`
  - `prompt_input_mode`: `"path"` with allowed values:
    - `"path"`
    - `"inline"`

After field validation, prompt/schema paths are resolved relative to `pipelines_dir.parent` and must exist on disk.

Duplicate `pipeline_id` across files is a hard error.

All validation/load errors are re-raised as:

- `ValueError("Invalid pipeline file <path>: <details>")`

Lint reuses the same field validation via `parse_pipeline_model_file(...)`, then separately classifies missing prompt/schema assets, outside-pack path escapes, and prompt-token contract mismatches as explicit finding codes.

## Prompt template rendering contract

`render_prompt_template(template_path, input_path)` supports two deterministic substitutions:

- Replaces every `{{INPUT_PATH}}` literal with the absolute resolved input file path.
- Replaces every `{{INPUT_TEXT}}` literal with full UTF-8 input file contents.

There is no general template engine. If you need more placeholders, code changes are required.

Pipeline prompt mode contract:

- `prompt_input_mode: "path"` requires `{{INPUT_PATH}}` in the prompt template.
- `prompt_input_mode: "inline"` requires `{{INPUT_TEXT}}` in the prompt template.
- `codex-farm lint` reports `pipeline.prompt_missing_required_token` when the prompt token does not match the configured mode.

Prompt-adjustment extension rule:

- Keep this function as the deterministic template baseline.
- Persist adaptive prompt toggles in `runs.config_json` and apply run-specific hint layering at worker execution time.
- Do not encode prompt-adaptation state in task rows; queue shape should stay stable.

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
- persisted `codex_model` / `codex_reasoning_effort` win when present
- persisted `output_schema_path_override` wins when present
- persisted `frozen_assets` (when present) tells workers to use frozen prompt/schema/pipeline files from `<data_dir>/run_assets/<run_id>/` instead of reopening live pack files

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
- `tests/test_pack_lint.py`
  - pack/schema lint findings for missing assets, duplicate IDs, invalid schema JSON/definition, and compatibility warnings
- `tests/test_cli_integration_contracts.py`
  - `pipelines list --root` precedence
  - `process --workspace-root` JSON contract and cd behavior
  - `lint --json` contracts for clean packs, broken packs, schema mode, and near-miss roots
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
  - update docs for this boundary (`docs/02-..._readme.md`) and adjacent runtime boundaries (`docs/03-..._readme.md`, `docs/04-..._readme.md`) if resume/runtime behavior changes
- If changing `codex_cd_mode` semantics:
  - update CLI one-file resolver and worker resolver together
  - update tests in `tests/test_worker.py`
  - update chunk docs 02 and 04 so they stay aligned

## Task doc merges from `docs/tasks`

Historical task docs merged into this chunk to preserve root/asset decision context:

- `Initial-Build.md` (`2026-02-20_12.45.00` revision note):
  - set the foundational rule that pipeline behavior is data-driven via `pipelines/*.json`, prompt templates, and schemas, not hard-coded orchestration classes.
  - documented strict schema/prompt file coupling as part of pipeline integrity.
- `Plan-for-recipe-correction.md` (`2026-02-22_12.36.41`):
  - introduced explicit root precedence (`--root` > `CODEX_FARM_ROOT` > discovery) for external pipeline packs.
  - introduced persisted run roots (`farm_root`, optional `workspace_root`) so resumed workers do not drift with caller cwd/env.
  - codified `process --json` external-caller usage where root/workspace selections must remain reproducible.
- `Plan-for-knowledge-correction.md` (`2026-02-22_13.07.23`):
  - added pipeline `codex_cd_mode` (`asset_root`, `input_dir`, `input_file_dir`) and enforced explicit override precedence for `--workspace-root`.
  - locked terminal behavior for missing computed `--cd` directories (configuration errors are not retried).
  - reinforced deterministic fake-Codex integration coverage for root/cd semantics.
- `idea1-2.md` (`2026-02-28_13.20.00`):
  - added run-assets freezing at run creation (`<data_dir>/run_assets/<run_id>/`) for pipeline JSON, effective pipeline contract, prompt template, and schema.
  - documented snapshot-first worker expectation for snapshot-bearing runs and explicit no-live-fallback behavior on missing/corrupt snapshot data.
  - captured scope boundary: this freeze contract does not freeze input files or guarantee `farm_root`/workspace availability at execution time.
- `idea1-7.md` (`2026-02-28_18.32.00`):
  - added read-only pack/schema lint contract (`codex-farm lint`) with deterministic finding codes and strict JSON output shape.
  - locked near-miss root rule: explicit `lint --root <existing-dir>` must report sentinel-folder findings instead of failing argument parsing.
  - preserved compatibility rule that missing Heads Up distiller assets are warning-only, and lint remains offline/local (no network `$ref` fetch).

## Merged discoveries from `docs/understandings`

These points were originally captured as separate timestamped exploration notes and are now merged here:

- `2026-02-22_12.33.22`: Run creation must persist `farm_root` and `workspace_root` decisions in `runs.config_json` so resumed workers do not depend on caller cwd/env.
- `2026-02-22_12.33.22`: Worker root fallback order is persisted `farm_root` first, then worker `--root`, then normal auto-resolution; workspace fallback is persisted `workspace_root` first, then farm root behavior.
- `2026-02-22_13.30.00`: `--workspace-root` is an explicit override only. It should not be silently backfilled when omitted.
- `2026-02-22_13.30.00`: Worker `--cd` resolution order is strict: persisted workspace override, else `codex_cd_mode` (`asset_root` -> run farm root, `input_dir` -> run input dir, `input_file_dir` -> task input parent).
- `2026-02-22_13.30.00`: Missing computed `--cd` directory is terminal configuration error, not retryable runtime noise.
- `2026-02-22_14.34.00`: Root validation must require sentinel directories (`pipelines/`, `prompts/`, `schemas/`) and use precedence `--root` > `CODEX_FARM_ROOT` > auto-discovery.
- `2026-02-22_14.34.00`: Pipeline JSON validation (`PipelineSpecModel`, `extra=forbid`) plus repo-relative asset existence checks are the critical guard before tasks are queued.
- `2026-02-28_02.55.22`: Pipeline assets may set optional `codex_reasoning_effort`; CLI run overrides persist as optional run-config keys (`codex_model`, `codex_reasoning_effort`) so worker resume behavior stays deterministic.
- `2026-02-28_09.31.02`: Caller programs may set `--output-schema`; run-based flows persist optional `output_schema_path_override` so worker resume and retries keep the same validation contract.
- `2026-02-28_09.32.28`: Prompt-adaptation work should layer run-config state plus worker-time rendering; keep `render_prompt_template(...)` deterministic and avoid task-schema expansion.
- `2026-02-28_12.30.33`: Lint root handling is intentionally asymmetric: execution commands require fully valid pack roots, but explicit `lint --root` near-miss directories should surface accumulated finding codes (including sentinel misses) instead of short-circuiting with argument failures.
- `2026-02-28_12.30.33`: To avoid drift, lint diagnostics should keep reusing pipeline parsing/root-relative helpers and only translate resulting failures into lint severities/codes.

Known bad paths to avoid repeating:

- Allowing implicit workspace defaults made resume behavior drift across shells and caused hard-to-reproduce `--cd` bugs.
- Letting workers recompute roots from ambient cwd instead of persisted run config caused external pipeline-pack runs to pick the wrong assets.

## Merged understanding notes (`docs/understandings`)

### 2026-03-02_16.00.00 - Duplicate `prompt_input_mode` key regression in benchmark prompt asset
- Found a temporary duplicate JSON key in `pipelines/recipeimport.benchmark.line_label.v1.json`: both `"prompt_input_mode": "inline"` and later `"prompt_input_mode": "path"`.
- JSON parser behavior keeps the last duplicate value, which effectively forced `path` mode and broke intended inline behavior during inline-migration checks.
- Resolution is to remove the duplicate and keep a single `prompt_input_mode` to preserve lint/runtime expectations for migrated inline benchmark pipelines.

## Task-driven context: inline prompt payload mode (`docs/tasks/2026-03-02_08.18.23`)

Why this work exists:

- External callers needed prompt payloads to be self-contained so model tasks do not rely on file-path-based reads.
- This makes a single task input source available in the rendered prompt body for robust orchestration.

Current design and why:

- `render_prompt_template(template_path, input_path)` now substitutes both `{{INPUT_PATH}}` and `{{INPUT_TEXT}}`.
- Pipeline JSON supports `prompt_input_mode` with default `path`; `inline` is explicit and opt-in.
- `pack_lint.py` validates required placeholder mode:
  - `path` requires `{{INPUT_PATH}}`
  - `inline` requires `{{INPUT_TEXT}}`
- Backward compatibility for legacy packs is preserved by keeping `{{INPUT_PATH}}` behavior intact while adding inline support.

How it is implemented:

- Runtime rendering is in `src/codex_farm/pipeline_spec.py`.
- Lint enforcement is in `src/codex_farm/pack_lint.py`.
- Most contract tests are in `tests/test_pipeline_spec.py` and `tests/test_pack_lint.py`; worker-level inline-proofing is in `tests/test_worker.py`.
- Migrated prompt templates live in:
  - `prompts/recipe_schemaorg_normalize_v1.txt`
  - `prompts/recipe_schemaorg_to_proprietary_v1.txt`
  - `prompts/recipeimport_benchmark_line_label_v1.txt`

Known issues / avoid-list:

- JSON key collisions in pipeline assets (for example duplicated `prompt_input_mode`) can silently produce wrong mode behavior due to overwrite order.
- For this reason, pipeline JSON mode entries should be single-source and reviewed during migration to avoid ambiguous renders.
