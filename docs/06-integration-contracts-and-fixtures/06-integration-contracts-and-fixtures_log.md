---
summary: "Major integration-testing strategy decisions and seam-boundary rationale."
read_when:
  - "When modifying integration coverage strategy or deciding fixture style"
---

# 06 Integration Contracts And Fixtures Log

## 2026-02-28_09.31.02 - Caller output-schema override integration coverage

- Source: merged historical notes.
- Added integration assertions for `--output-schema` behavior across seams: CLI contract accepts/persists override, and worker consumes persisted `output_schema_path_override`.
- Updated `process --json` contract tracking to include resolved `output_schema_path`.

## 2026-02-28_09.21.54 - Output verification cross-suite coverage contract

- Source: merged historical notes.
- Logged that output acceptance regressions are cross-seam and must be validated across codex-exec, worker, process/CLI-contract, and schema-fixture suites.
- Recorded practical visibility contract for debugging acceptance outcomes: `run tasks --json`, `run errors --json`, and `codex_exec_activity.csv`.

## 2026-02-28_04.16.54 - Caller model-picker contract coverage

- Source: merged historical notes.
- Added CLI integration assertion for `models list --json` shape so caller-facing model-picker payload stays stable.
- Split coverage intentionally: CLI contract shape in `tests/test_cli_integration_contracts.py`, cache parsing/normalization in `tests/test_model_catalog.py`.
- Task-source evidence (merged historical notes): acceptance recorded with targeted `tests/test_model_catalog.py tests/test_cli_integration_contracts.py -q` plus full `pytest -q`.

## 2026-02-28_02.55.22 - Effort override integration coverage

- Source: merged historical notes.
- Added integration assertions for effort aliases and persistence: CLI contract accepts/persists `codex_reasoning_effort`, and worker consumes persisted effort override.
- Updated `process --json` contract tracking to include `codex_reasoning_effort` field.
- Task-source evidence (merged historical notes): fail-before contract checks were unknown effort flags, missing worker effort precedence, and missing Codex pass-through; post-change targeted + full suites were recorded passing.

## 2026-02-28_02.47.41 - Model override integration coverage

- Source: merged historical notes.
- Added integration assertions for `--model` behavior across seams: CLI contract accepts/persists override, and worker consumes persisted `codex_model`.
- Updated `process --json` contract tracking to include `codex_model` field.
- Task-source evidence (merged historical notes): fail-before checks captured missing CLI options and worker override usage; task note recorded targeted CLI/worker and full suites passing.

## 2026-02-22_13.22.52 - Flow-boundary mapping

- Source: merged historical notes (merged).
- Captured intentional six-boundary split: CLI contract, root/assets, queue state, worker execution/retries, codex/schema gate, integration contracts.
- Recorded purpose of split: follow real call path and keep most edits local to one chunk plus an adjacent seam.

## 2026-02-22_14.34.17 - Dual fixture strategy decision

- Source: merged historical notes (merged).
- Preserved monkeypatch-based integration tests (`codex_farm.worker.run_codex_exec`) for fast CLI payload and queue-contract assertions.
- Preserved fake-`codex` binary integration tests for subprocess argument seam assertions.
- Logged coverage tradeoff history: single-strategy suites left blind spots either in subprocess wiring or in fast targeted contract checks.

## 2026-02-22_13.07.23 - Fake-Codex external-pack coverage expansion

- Source: merged historical notes (merged).
- Added deterministic integration coverage for pipeline `codex_cd_mode` behavior and schema-failure inspection contracts.
- Recorded broad acceptance run (`24 passed`) after targeted suites covering worker, CLI contracts, DB error rows, and fake-codex flows.

## 2026-02-22_12.36.41 - Integrator-friendly contract baseline coverage

- Source: merged historical notes (merged).
- Added deterministic fake-codex tests for `one`, `process --json`, and `run errors --json` around external pipeline packs.
- Captured initial machine-safe CLI contract coverage for `--root`, `--workspace-root`, and JSON-only stdout behavior.

## 2026-02-20_12.45.00 - Initial end-to-end acceptance baseline

- Source: merged historical notes (merged).
- Recorded first repository-level acceptance baseline (`pytest` passing plus live `one`, `process`, and `go` checks) used as historical integration context.
