---
summary: "High-level change history and major contract decisions for chunk 01 CLI behavior."
read_when:
  - "When CLI behavior feels inconsistent across commands or JSON output consumers"
  - "When deciding whether to change command defaults or persisted run config fields"
---

# 01 CLI And Command Contracts Log

## 2026-02-28_15.20.27 - Telemetry report schema v2 insights + tuning playbook

- Source: merged historical notes.
- Expanded `run telemetry --json` contract to schema version `2` with `insights` and `tuning_playbook` sections for direct caller automation.
- Locked `process --json` embedded `telemetry_report` parity with `run telemetry` so callers can consume model/reasoning breakdowns, pass-forward effectiveness deltas, and caller-ready override suggestions without a follow-up command.

## 2026-02-28_15.02.31 - Caller telemetry report command + process payload integration

- Source: merged historical notes.
- Added `run telemetry` as machine-facing report command over `codex_exec_activity.csv` with filters (`run_id`, `pipeline`, `source`, `status`) and recommendation categories (`prompt`, `input_data`, `output_schema`, `runtime`).
- Extended `process --json` payload with embedded `telemetry_report` (default-on, bounded by `--telemetry-limit` and `--telemetry-recommendations-limit`) so callers can act on recommendations without a second CLI call.
- Locked warning-safe behavior: missing/unreadable telemetry CSV yields warnings in report payload but does not change run/task exit semantics.

## 2026-02-28_13.43.43 - Process worker-poll latency hardening

- Source: merged understanding note.
- Removed fixed poll-sleep overhead in CLI orchestration: `_run_workers(...)` now waits on futures with `return_when=FIRST_COMPLETED` while still honoring the poll timeout for progress updates.
- Preserved command contract and JSON behavior while eliminating avoidable ~1 second tail-latency in fast mocked `process`/`go` paths.

## 2026-02-28_10.29.20 - Autotune diff emitter command

- Source: merged historical notes.
- Added `run autotune` command that converts telemetry `tuning_playbook` output into caller-ready `process` flag overrides plus unified diffs for prompt/pipeline files when context paths are available.
- Locked non-mutating behavior: command emits suggestions only and never writes project files.

## 2026-02-28_09.58.11 - Heads Up learning safety + parity hardening

- Source: merged historical notes.
- Hardened post-run learning path to be warning-safe even on unexpected learner exceptions.
- Extended automatic Heads Up post-run learning from `process --heads-up` to `go --heads-up`.
- Locked terminal-run precondition for learning (`done|error`) so non-terminal `heads-up learn` requests return warning output with zero added tips.

## 2026-02-28_09.31.02 - Caller-provided output-schema override contract

- Source: merged historical notes.
- Added `--output-schema` to `one`, `run create`, `process`, and `go` so callers can enforce their own output contract without editing pipeline assets.
- Updated run-config contract: `output_schema_path_override` persists only when explicitly set so resumed runs keep deterministic validation behavior.
- Extended JSON payload contracts for `run create --json` and `process --json` with resolved `output_schema_path`.

## 2026-02-28_04.16.54 - Caller-facing model picker contract

- Source: merged historical notes.
- Added `models list` command so external programs can fetch model-picker options without parsing Codex cache files directly.
- Contract: `models list --json` returns visible model rows (`slug`, `display_name`, `description`, optional `supported_reasoning_efforts`).
- Added deterministic fallback row (`gpt-5.3-codex-spark`) when local `models_cache.json` metadata is unavailable.
- Task-source evidence (merged historical notes): targeted suites were `tests/test_model_catalog.py` and `tests/test_cli_integration_contracts.py`; full `pytest -q` was also recorded green after implementation.

## 2026-02-28_02.55.22 - Reasoning-effort override contract

- Source: merged historical notes.
- Added effort override aliases across execution commands: `--effort`, `--reasoning-effort`, `--thinking-effort`, `--codex-reasoning-effort`, `--codex-thinking-effort`.
- Locked normalized effort values to `none|minimal|low|medium|high|xhigh`.
- Extended run-config and JSON payload contracts with `codex_reasoning_effort` (optional override, persisted for deterministic worker resume behavior).
- Task-source evidence (merged historical notes): fail-before symptoms included unknown effort flags and missing worker pass-through; targeted suite `tests/test_codex_exec.py tests/test_cli_integration_contracts.py tests/test_worker.py -q` and full `pytest -q` were recorded passing.

## 2026-02-28_02.47.41 - Model override flags and run-config persistence

- Source: merged historical notes.
- Added `--model` support to `one`, `run create`, `process`, and `go` for easier model selection without editing pipeline JSON.
- Updated run-config contract: `codex_model` is persisted only when explicitly overridden so queued/resumed runs remain deterministic.
- Extended JSON payload contracts for `run create --json` and `process --json` with resolved `codex_model`.
- Task-source evidence (merged historical notes): fail-before symptoms were missing CLI flags and worker-side override omission; targeted suites (`tests/test_cli_integration_contracts.py tests/test_worker.py`) and full `pytest -q` were recorded passing post-change.

## 2026-02-22_14.34.46 - CLI contract discovery pass

- Source: merged historical notes (merged).
- Confirmed `process --json` stdout must remain machine-clean with one final JSON object; creation/progress output belongs on stderr.
- Captured intentional default divergence: `run create` uses fixed `"**/*.json"` unless overridden, while `process` uses pipeline `input_glob_default` when `--glob` is omitted/empty.
- Locked run-config persistence behavior as part of CLI contract: `farm_root` always stored, `workspace_root` only stored when explicitly provided.
- Recorded `one` special-case behavior where `codex_cd_mode=input_dir` and `input_file_dir` both map to input-file parent.

## 2026-02-23_00.24.39 - Process hard-stop on rate-limit failures

- Source: merged historical notes.
- Updated `process` contract so worker threads share a stop signal and halt additional task claims after codex rate-limit (`429`) failures.
- Documented that early-stop can leave remaining tasks queued for later resumption.
- Task-source evidence (merged historical notes): lock tests were `test_worker_loop_stops_immediately_on_rate_limit` and `test_process_command_stops_after_first_rate_limit`; task note recorded `tests/test_worker.py tests/test_process_smoke.py` and CLI contract tests passing.

## 2026-02-22_13.07.23 - Pipeline-pack executor hardening (knowledge-correction plan)

- Source: merged historical notes (merged).
- Added pipeline-driven `codex_cd_mode` and codified CLI precedence: explicit `--workspace-root` override first, otherwise pipeline mode.
- Documented caller-facing error-inspection refinement: `run errors --json` now serves error-task fields tailored for machine diagnostics.
- Preserved deterministic external integration coverage via fake-Codex tests (`tests/test_fake_codex_pipeline_pack_demo.py`) and targeted contract suites.

## 2026-02-22_12.36.41 - External integrator contract baseline (recipe-correction plan)

- Source: merged historical notes (merged).
- Added first-class caller controls for external packs: `--root` precedence, explicit `--workspace-root`, and machine-safe `process --json`.
- Added machine-readable task introspection commands (`run tasks`, `run errors`) to avoid direct SQLite coupling in caller programs.
- Locked run-config persistence of root/workspace choices for resume determinism across shells.

## 2026-02-20_12.45.00 - Initial CLI surface baseline (initial build record)

- Source: merged historical notes (merged).
- Established V1 command surface and workflow split: `doctor`, `one`, `run create`, `process`, `go`, pipeline listing/scaffolding, and worker command.
- Captured product-scope contract that remains important for caller expectations: local-only tool, CLI-first automation, and recipes-first V1 pipeline focus.
