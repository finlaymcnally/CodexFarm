---
summary: "Major Codex subprocess and schema-gate decisions, including compatibility and schema-shape lessons."
read_when:
  - "When changing codex exec invocation, doctor checks, or schema acceptance rules"
---

# 05 Codex Exec And Schema Gate Log

## 2026-03-02_00.52.58 - Doctor login-status check before smoke

- Source: prevention work after repeated unauthenticated external runs.
- Added `codex login status` check as an explicit doctor step before non-interactive smoke execution.
- Locked sequencing: when login-status fails, non-interactive smoke is skipped so the primary auth/setup failure is surfaced directly.

## 2026-03-02_00.45.23 - Auth/session failure classification helper

- Source: RecipeImport incident where Codex workers hit websocket `403 Forbidden` due missing login/session.
- Added `is_auth_failure_message(...)` in `codex_exec.py` for shared classification of login/session failures (`401/403`, backend websocket auth denial, login-required text).
- Locked reuse contract: caller layers (`worker`, `one`, `doctor`) consume this helper for policy/messaging instead of duplicating string matching.

## 2026-02-28_18.46.00 - Failed-attempt forensics bundles made first-class

- Source: merged task doc `docs/tasks/idea1-6.md`.
- Added self-contained forensic bundle contract for failed attempts (prompt/input/schema snapshots, metadata, runtime tails, optional rejected payload copy) with SQLite index rows in `task_forensics`.
- Locked additive interface strategy from task history:
  - new machine surface is `run forensics --json`,
  - existing `run errors --json` contract remains task-state only.
- Preserved ordering guarantee: worker/CLI failure paths capture forensics before output cleanup so schema-invalid payload evidence survives while normal output paths remain clean.
- Captured explicit scope limit: timeout failures remain metadata/tail-oriented for raw output because timeout cleanup in `run_codex_exec(...)` currently removes temp output before bundle capture can copy bytes.

## 2026-02-28_14.50.39 - Caller-tuning telemetry fields added at exec boundary

- Source: merged historical notes.
- Added normalized failure classification and rate-limit suspicion flags to per-call CSV rows emitted by `run_codex_exec`.
- Added output payload digest/preview plus parsed event-type summaries so each invocation row carries both execution and output-side debugging context.
- Preserved single-hook telemetry rule: field expansion stayed inside `run_codex_exec` so all call paths remain covered once.

## 2026-02-28_09.31.02 - Runtime schema source clarified for queued runs

- Source: merged historical notes.
- Clarified that `run_codex_exec` receives a fully resolved schema path from callers.
- Documented worker precedence: run-config `output_schema_path_override` first, pipeline `output_schema_path` fallback.

## 2026-02-28_09.21.54 - Output verification stack clarified

- Source: merged historical notes.
- Captured layered acceptance rule: payload promotion from `run_codex_exec(...)` is necessary but not sufficient; local Draft202012 validation is the final gate.
- Preserved compatibility rule: non-zero Codex exits can still be accepted only when non-empty payload exists and schema validation passes.
- Documented verification visibility surfaces (`run tasks --json`, `run errors --json`, `codex_exec_activity.csv`) as the debugging contract for acceptance outcomes.
- Logged regression suites that should move together when acceptance behavior changes (`test_codex_exec.py`, `test_worker.py`, `test_fake_codex_pipeline_pack_demo.py`, `test_cli_integration_contracts.py`, `test_recipeimport_schemas.py`).

## 2026-02-28_02.55.22 - Runtime effort source clarified for queued runs

- Source: merged historical notes.
- Clarified that `run_codex_exec` receives resolved `reasoning_effort` from callers and maps it to Codex `model_reasoning_effort`.
- Documented worker precedence: run-config `codex_reasoning_effort` override first, pipeline `codex_reasoning_effort` fallback.

## 2026-02-28_02.47.41 - Runtime model source clarified for queued runs

- Source: merged historical notes.
- Clarified that `run_codex_exec` receives a fully resolved model string from callers.
- Documented worker precedence: run-config `codex_model` override first, pipeline `codex_model` fallback.

## 2026-02-20_13.05.00 - Codex CLI compatibility quirks and acceptance behavior

- Source: merged historical notes (merged).
- Locked invocation shape for compatibility: use global approval flag (`codex --ask-for-approval never exec ...`) and always pass `--skip-git-repo-check`.
- Preserved tolerant acceptance rule for non-zero exits when `--output-last-message` produced non-empty payload.
- Kept doctor smoke-check tolerance for exact `OK` output even with non-zero exit to avoid false negatives.
- Documented strict current `--output-schema` subset behavior requiring all property keys in `required` and the nullable-required workaround for optional fields.

## 2026-02-20_13.09.19 - Recipeimport schema coverage correction

- Source: merged historical notes (merged).
- Captured previous failure mode where over-required fullshape schemas rejected sparse real payloads.
- Recorded contract update to support both sparse real samples and platonic full-shape samples.
- Aligned `schemas/recipeimport_final_fullshape_v1.schema.json` with canonical schema at `examples/recipeimport_final/recipeDraftV1.canonical.recipeimport.schema.json`.

## 2026-02-22_14.33.40 - Chunk 05 acceptance-boundary framing

- Source: merged historical notes (merged).
- Reframed chunk 05 as acceptance boundary, not only subprocess wrapper.
- Preserved atomic temp-file-to-final promotion requirement.
- Reconfirmed separation of concerns: one-shot CLI failure behavior in `cli.one` versus retry/terminal branching in worker flow.
