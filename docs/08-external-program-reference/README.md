---
summary: "Entry point for external-program integration docs (model/effort selection, output schema contracts, and telemetry fields)."
read_when:
  - "When building another program that calls codex-farm via CLI/JSON"
  - "When you need caller-facing references instead of internal implementation docs"
---

# External Program Reference

Use this folder when another app or script drives `codex-farm` via CLI and JSON contracts.

## Start here

1. `model-selection.md` for `models list --json`, `--model`, and `--reasoning-effort` usage.
2. `structured-output-contracts.md` for `--output-schema` behavior, schema template guidance, and pass/fail+retry semantics.
3. `lint-contracts.md` for read-only pack/schema preflight and finding-code contracts.
4. `incremental-runs.md` for `--incremental` / `--incremental-from` and reuse summary contracts.
5. `failure-forensics-contracts.md` for `run forensics --json`, bundle layout, and metadata fields.
6. `telemetry-contracts.md` for machine-readable telemetry fields used for prompt tuning and failure analysis.
7. `08-external-program-reference_log.md` for historical contract decisions and prior failure paths.

## Caller contract highlights

- Model picker:
  - `codex-farm models list --json` is the only supported caller contract for menu population.
  - rows are cache-backed when available; fallback row (`gpt-5.3-codex-spark`) is always present when cache metadata is missing.
- Execution overrides:
  - `--model` and effort aliases (`--effort`, `--reasoning-effort`, `--thinking-effort`, plus codex-prefixed forms) are accepted by `one`, `run create`, `process`, and `go`.
  - run-based commands persist explicit overrides so retries/resumes keep the same model/effort contract.
- Structured output:
  - `--output-schema` is supported on `one`, `run create`, `process`, and `go`.
  - run-based commands persist `output_schema_path_override` so queued worker retries use the same schema.
- Read-only linting:
  - `codex-farm lint --json` is the machine-facing preflight endpoint for pack/schema diagnostics.
  - `--strict` only changes exit behavior; finding severities stay unchanged.
- Incremental runs:
  - `run create`, `process`, and `go` accept `--incremental` and `--incremental-from <run_id>`.
  - JSON payloads include an additive `incremental` object with reuse counts and fallback reasons.
  - `run tasks --json` exposes reuse provenance per task (`reused`, `reused_from_run_id`, `reused_from_task_id`).
- Machine-safe outputs:
  - `process --json` keeps stdout parseable (single JSON payload).
  - lifecycle controls are machine-addressable: `run pause|resume|cancel|retry-errors --json`.
  - `run status --json` and `process --json` include `control_state` and `counts.canceled`.
  - inspect terminal failures with `run errors --json`; inspect per-task states with `run tasks --json`.
  - inspect failed-attempt evidence indexes with `run forensics --json`; `one` failure output may include `Forensics bundle: <abs path>` on stderr.

## Merged task docs from `docs/tasks`

The following task specs were merged into this folder's docs/log to preserve external-caller context:

- `2026-02-28_04.16.54 - caller-model-menu-contract.md`
- `2026-02-28_02.55.22 - model-effort-overrides-for-callers.md`
- `2026-02-28_02.47.41 - model-override-cli.md`
- `Plan-for-recipe-correction.md` (external pack integration baseline)
- `Plan-for-knowledge-correction.md` (`codex_cd_mode` and error-introspection refinements)

## Related deep docs

- `docs/01-cli-and-command-contracts/01-cli-and-command-contracts_readme.md`
- `docs/05-codex-exec-and-schema-gate/05-codex-exec-and-schema-gate_readme.md`
