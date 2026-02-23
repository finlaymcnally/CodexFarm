---
summary: "High-level evolution of root/workspace resolution and pipeline asset contracts."
read_when:
  - "When changing farm root precedence, workspace overrides, or pipeline asset validation"
---

# 02 Pipeline Assets And Root Resolution Log

## 2026-02-22_12.33.22 - Root/workspace persistence contract

- Source: `docs/understandings/2026-02-22_12.33.22_root-workspace-run-contract.md` (merged).
- Identified a durability requirement: `process` and `run create` must persist `farm_root` and `workspace_root` in `runs.config_json` so workers resumed from different shells do not drift.
- Established worker fallback priorities for root and workspace decisions so external pipeline packs run without requiring callers to `cd` into codex-farm first.

## 2026-02-22_13.30.00 - Explicit workspace override semantics

- Source: `docs/understandings/2026-02-22_13.30.00_cd-mode-and-workspace-override.md` (merged).
- Finalized that `--workspace-root` is explicit override only; omitting it should not silently persist a computed default.
- Formalized worker `--cd` resolution order: persisted workspace override first, then `codex_cd_mode` (`asset_root`, `input_dir`, `input_file_dir`).
- Marked missing computed `--cd` directory as terminal configuration error to avoid futile retries.

## 2026-02-22_14.34.00 - Root/asset validation and resume stability pass

- Source: `docs/understandings/2026-02-22_14.34.00_pipeline-assets-and-root-resolution-flow.md` (merged).
- Locked root precedence (`--root` > `CODEX_FARM_ROOT` > auto-discovery) and sentinel-folder validation (`pipelines/`, `prompts/`, `schemas/`).
- Reinforced strict pipeline JSON validation (`PipelineSpecModel`, `extra=forbid`) and repo-relative asset existence checks.
- Re-confirmed persisted `farm_root` and optional persisted `workspace_root` as the resume/retry stability mechanism.

