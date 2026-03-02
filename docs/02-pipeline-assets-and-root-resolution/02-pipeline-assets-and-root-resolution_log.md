---
summary: "High-level evolution of root/workspace resolution and pipeline asset contracts."
read_when:
  - "When changing farm root precedence, workspace overrides, or pipeline asset validation"
---

# 02 Pipeline Assets And Root Resolution Log

## 2026-02-28_18.32.00 - Lint contract formalized at the root/asset boundary

- Source: merged task doc `docs/tasks/idea1-7.md`.
- Added read-only `codex-farm lint` contract across pack mode and standalone schema mode with deterministic findings (`code`, `severity`, `path`, `message`, optional `hint`).
- Locked near-miss behavior for explicit roots: if `--root` points at an existing directory missing sentinels, lint must emit `pack.missing_sentinel_dirs` instead of short-circuiting as an argument error.
- Preserved important safety boundaries from the task history:
  - lint collects findings instead of fail-fast runtime exits,
  - lint stays local/offline (no network ref resolution),
  - compatibility warnings (for example missing Heads Up distiller assets) stay warning-only rather than hard errors.
- Failed path worth avoiding: trying to force lint into existing execution-path argument validation removed useful diagnostics for almost-valid external packs and was rejected.

## 2026-02-28_13.20.00 - Frozen run-assets snapshot seam

- Source: merged task doc `docs/tasks/idea1-2.md`.
- Introduced per-run frozen asset snapshots under `<data_dir>/run_assets/<run_id>/` with manifest-pointer persistence in `runs.config_json.frozen_assets`.
- Locked deterministic source-of-truth rule: snapshot-bearing runs must execute from frozen pipeline/prompt/schema assets and must not silently fall back to live files when snapshots are missing/tampered.
- Preserved telemetry/operational readability requirement: execution may use frozen schema copies while logical schema identity remains tied to the original source path for reporting.
- Scope decisions carried forward from task history:
  - freeze effective pipeline assets only,
  - do not freeze input files or broader filesystem/workspace state in this seam.

## 2026-02-28_12.30.33 - Lint near-miss root diagnostics seam

- Source: merged understanding note.
- Locked intentional asymmetry: execution commands still require a fully valid pack root, while explicit `lint --root <existing-dir>` reports near-miss sentinel failures as findings (`pack.missing_sentinel_dirs`) instead of argument errors.
- Preserved implementation anti-drift rule: lint reuses `parse_pipeline_model_file(...)` and repo-relative path helpers, then classifies resulting failures into finding severities/codes.

## 2026-02-28_09.32.28 - Prompt-adjustment seam for deterministic template layering

- Source: merged historical notes.
- Recorded that prompt construction currently hinges on `render_prompt_template(...)` and should stay deterministic at asset-render time.
- Captured extension contract: adaptive prompt knobs should persist in run config and be consumed during worker execution, not encoded into queue/task schema.

## 2026-02-28_09.31.02 - Persisted output-schema override for run determinism

- Source: merged historical notes.
- Added optional run-config key `output_schema_path_override` when caller passes `--output-schema`.
- Extended persisted override contract so resumed workers keep the same validation schema and do not drift back to pipeline defaults.

## 2026-02-28_02.55.22 - Pipeline/model-effort override shape update

- Source: merged historical notes.
- Added optional pipeline field `codex_reasoning_effort` with normalized allowed values.
- Documented run-config persistence for optional model/effort overrides so external callers can deterministically drive worker execution.

## 2026-02-22_12.33.22 - Root/workspace persistence contract

- Source: merged historical notes (merged).
- Identified a durability requirement: `process` and `run create` must persist `farm_root` and `workspace_root` in `runs.config_json` so workers resumed from different shells do not drift.
- Established worker fallback priorities for root and workspace decisions so external pipeline packs run without requiring callers to `cd` into codex-farm first.

## 2026-02-22_13.30.00 - Explicit workspace override semantics

- Source: merged historical notes (merged).
- Finalized that `--workspace-root` is explicit override only; omitting it should not silently persist a computed default.
- Formalized worker `--cd` resolution order: persisted workspace override first, then `codex_cd_mode` (`asset_root`, `input_dir`, `input_file_dir`).
- Marked missing computed `--cd` directory as terminal configuration error to avoid futile retries.

## 2026-02-22_14.34.00 - Root/asset validation and resume stability pass

- Source: merged historical notes (merged).
- Locked root precedence (`--root` > `CODEX_FARM_ROOT` > auto-discovery) and sentinel-folder validation (`pipelines/`, `prompts/`, `schemas/`).
- Reinforced strict pipeline JSON validation (`PipelineSpecModel`, `extra=forbid`) and repo-relative asset existence checks.
- Re-confirmed persisted `farm_root` and optional persisted `workspace_root` as the resume/retry stability mechanism.

## 2026-02-22_13.07.23 - `codex_cd_mode` rollout for external pack workers

- Source: merged historical notes (merged).
- Added pipeline field `codex_cd_mode` with deterministic values (`asset_root`, `input_dir`, `input_file_dir`) and default `asset_root`.
- Preserved explicit override contract where `--workspace-root` wins, but only when passed.
- Recorded that missing computed `--cd` directories are terminal configuration errors, not retry candidates.

## 2026-02-22_12.36.41 - External pipeline pack root/workspace baseline

- Source: merged historical notes (merged).
- Established first-class external-pack root behavior (`--root` precedence + persisted run roots for resume safety).
- Captured run-level persistence requirement for workspace override so worker execution remains reproducible across shells.
- Recorded machine-caller motivation: external orchestrators should not need to rely on repository cwd assumptions.

## 2026-02-20_12.45.00 - Data-driven pipeline asset baseline

- Source: merged historical notes (merged).
- Established V1 direction that pipeline behavior is file-driven (`pipelines/`, `prompts/`, `schemas/`) rather than hard-coded per operation.
- Logged early guardrail that missing prompt/schema files should fail immediately at load time with actionable errors.

## 2026-03-02_16.00.00 - Duplicate inline prompt key collision fixed in benchmark prompt asset
- Source: imported pipeline review revealed duplicate `prompt_input_mode` in inline benchmark JSON during migration review.
- Decision: treat last-write duplicates as a correctness risk and require single-source prompt mode definitions in pack assets.
- Outcome: remove duplicate key, preserving intended `inline` mode and keeping pipeline assets/lint contracts consistent for benchmark paths.

## 2026-03-02_08.18.23 - Inline prompt payload support (`prompt_input_mode`) added

- Source: `docs/tasks/2026-03-02_08.18.23-codexfarm-self-contained-inline-prompts.md`.
- Goal/decision: keep existing `{{INPUT_PATH}}` path mode while adding `{{INPUT_TEXT}}` inline mode so prompts can be self-contained.
- Architecture choice: implement inline rendering directly in `render_prompt_template(...)` and gate mode correctness in `pack_lint` instead of introducing a new template engine.
- Contract choice: add explicit `prompt_input_mode` (`path|inline`) and fail fast with `pipeline.prompt_missing_required_token` when template and mode diverge.
- Migration outcome:
  - migrated prompt templates now carry inline payload blocks.
  - tests were updated to lock both default-path and inline behavior.
  - worker/task evidence confirms inline payload is present in prompt logs when enabled.
- Major failure correction to remember:
  - benchmark pipeline asset briefly carried duplicate `prompt_input_mode` keys; last-write behavior made the intended mode ambiguous and was fixed by removing duplicate keys.
- Critical acceptance reminder: do not change mode rules without updating lint + docs together, or callers can pass invalid assets that fail late in worker execution.
