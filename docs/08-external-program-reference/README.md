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
6. `progress-contracts.md` for spinner/progress snapshot/event contracts (`run progress`, `process --progress-events`).
7. `telemetry-contracts.md` for machine-readable telemetry fields used for prompt tuning and failure analysis.
8. `benchmark-runtime-contracts.md` for recipeimport benchmark mode flags and artifact layout.
9. `08-external-program-reference_log.md` for historical contract decisions and prior failure paths.

## Caller contract highlights

- Model picker:
  - `codex-farm models list --json` is the only supported caller contract for menu population.
  - rows are cache-backed when available; fallback row (`gpt-5.3-codex-spark`) is always present when cache metadata is missing.
- Execution overrides:
  - `run create`, `process`, and `go` accept `--runtime-mode`; supported values are `classic_task_farm_v1` and `structured_loop_agentic_v1`.
  - `structured_loop_agentic_v1` defaults to one worker when `--workers` is omitted and rejects `--workers > 1`.
  - `--model` and effort aliases (`--effort`, `--reasoning-effort`, `--thinking-effort`, plus codex-prefixed forms) are accepted by `one`, `run create`, `process`, and `go`.
  - run-based commands persist explicit overrides so retries/resumes keep the same model/effort contract.
  - `--codex-home` is accepted by `one`, `run create`, `process`, and `go`; run-based commands persist the resolved absolute `codex_home_path`.
  - pipelines may also declare `codex_home_profile`, which resolves `CODEX_FARM_CODEX_HOME_<PROFILE>` during run creation.
- Structured output:
  - `--output-schema` is supported on `one`, `run create`, `process`, and `go`.
  - run-based commands persist `output_schema_path_override` so queued worker retries use the same schema.
- Benchmark mode:
  - run-based commands accept `--recipeimport-benchmark-mode line_label_v1` and optional `--recipeimport-benchmark-debug`.
  - completed benchmark tasks emit deterministic artifacts under `<run output>/.recipeimport-benchmark/<task_id>/`.
- Read-only linting:
  - `codex-farm lint --json` is the machine-facing preflight endpoint for pack/schema diagnostics.
  - `--strict` only changes exit behavior; finding severities stay unchanged.
- Incremental runs:
  - `run create`, `process`, and `go` accept `--incremental` and `--incremental-from <run_id>`.
  - JSON payloads include an additive `incremental` object with reuse counts and fallback reasons.
  - `run tasks --json` exposes reuse provenance per task (`reused`, `reused_from_run_id`, `reused_from_task_id`).
- Machine-safe outputs:
- execution commands (`one`, `worker`, `process`, `go`) run execution precheck by default (`codex login status` plus a non-interactive `codex exec` smoke check).
  - `run create --json` and `process --json` include additive `runtime_mode`; `process --json` also includes `effective_workers` plus session counters (`session_count`, `fresh_session_count`, `tasks_per_session_summary`, `session_turn_count_total`, `session_failures`).
  - `run create --json` and `process --json` include additive `codex_execution_context` and `codex_home_path` fields.
  - recipe-style pipelines may run from scratch `--cd` directories under `<data_dir>/execution_contexts/` even when `codex_cd_mode` or `workspace_root` would otherwise point at the pack root.
  - callers can bypass precheck intentionally with `--no-login-precheck` or env `CODEX_FARM_SKIP_LOGIN_PRECHECK=1`.
  - `process --json` keeps stdout parseable (single JSON payload).
  - `process --progress-events` adds machine-readable stderr event lines prefixed with `__codex_farm_progress__ `.
  - lifecycle controls are machine-addressable: `run pause|resume|cancel|retry-errors --json`.
  - `run status --json` and `process --json` include `control_state` and `counts.canceled`.
  - `run progress --json` is the spinner-friendly snapshot endpoint (with optional `--watch` polling stream).
  - inspect terminal failures with `run errors --json`; inspect per-task states with `run tasks --json`.
  - inspect failed-attempt evidence indexes with `run forensics --json`; `one` failure output may include `Forensics bundle: <abs path>` on stderr.
  - invocation trace artifacts now include a normalized `captured_reasoning` block so callers can distinguish stdout reasoning, rollout summary text, empty-summary encrypted rollout metadata, and missing-rollout cases without reverse-engineering raw Codex events.

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

## Merged understanding notes (`docs/understandings`)

### 2026-03-01_20.40.25 - RecipeImport progress integration notes
- `codex-farm run` and process callers should treat external helper failures as contract-bearing events and avoid relying on mutable local paths.

### 2026-03-01_20.40.25 - Spinner/progress integration for RecipeImport callers
- RecipeImport currently uses blocking `subprocess.run(..., capture_output=True)` and cannot stream live CodexFarm state.
- Recommended caller-facing event pattern is `run progress --json` polling and/or stderr-prefixed progress events to keep machine parsing stable.
- `process --json` must remain a single JSON payload on stdout.

### 2026-03-02_14.50.12 - Oracle manual-login timeout pathology in browser mode
- `/home/mcnal/.local/bin/oracle-browser-headless` is configured to force manual login; runs can appear stuck in pending state with empty model logs.
- Useful repro command: `--browser-timeout 45s --browser-input-timeout 15s`; watch for `Manual login mode enabled`, `waiting for session to appear`, and timeout diagnostics.
- Keep this in the caller troubleshooting section because it is mostly external login-behavior behavior.

### 2026-03-02_15.03.51 - Browser profile artifacts and GitHub push protection
- Push protection checks history (`origin/master..HEAD`) not current tree state.
- A prior commit with Chromium profile cache bytes containing secrets blocked future pushes even after cleanup.
- Preventive control: ignore browser profile/cache artifacts in `.gitignore` to avoid rediscovering this failure mode in external integration runs.

## Task archives merged from `docs/tasks`

### 2026-03-01_20.36.00 - `run progress` + `process --progress-events`

This task introduced external-runner-friendly progress surfaces:

- machine snapshot endpoint: `run progress --run-id <id> --json`
- optional polling stream: `run progress --run-id <id> --watch --json`
- process stderr event stream: `process --json --progress-events`
- invariant: `process --json` keeps stdout as one stable machine object while progress events are stderr-only with prefix `__codex_farm_progress__ `.

### 2026-03-02_09.37.43 - recipeimport benchmark-native mode

This task added a dedicated benchmark path for recipeimport line-label runs:

- opt-in run flags: `--recipeimport-benchmark-mode line_label_v1`, `--recipeimport-benchmark-debug`
- deterministic canonical payload contract in task output and per-task `.recipeimport-benchmark/<task_id>/` artifacts.
- explicit terminalization rule for benchmark contract failures (`benchmark_contract_error`), so contract failures are not treated as retriable transient errors.

If an external caller needs both features, combine `run create/process/go` lifecycle commands with these contracts and read the per-task artifact layout from `benchmark-runtime-contracts.md`.
