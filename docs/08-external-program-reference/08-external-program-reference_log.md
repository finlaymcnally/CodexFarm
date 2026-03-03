---
summary: "Historical caller-facing contract changes for external programs that drive codex-farm."
read_when:
  - "When changing model/effort/schema override behavior for external callers"
  - "When modifying JSON contracts consumed by non-human codex-farm integrations"
---

# 08 External Program Reference Log

## 2026-03-02_21.10.10 - External reasoning-token telemetry contract

- Source: caller integrations can now observe reasoning token usage in Codex status output and need the same signal in machine-readable telemetry.
- Updated external telemetry contract to include additive CSV field `tokens_reasoning` plus report aggregates (`summary.tokens_reasoning_total`, `summary.tokens_reasoning_avg_per_call`) and per-model breakdown `tokens_reasoning_avg_per_call`.
- Compatibility rule preserved: existing token fields and report keys remain unchanged; reasoning fields are additive.

## 2026-03-02_00.52.58 - Caller-facing login precheck contract

- Source: repeated integration runs failed only after queueing work because local Codex login was missing.
- Added execution precheck contract: `one`, `worker`, `process`, and `go` now run `codex login status` before execution by default.
- Locked explicit bypasses for orchestrators: `--no-login-precheck` and env `CODEX_FARM_SKIP_LOGIN_PRECHECK=1`.

## 2026-03-01_20.40.00 - Spinner/progress contracts for external callers

- Source: RecipeImport integration request for richer spinner/progress state.
- Added `progress-contracts.md` documenting `run progress --json` snapshots/watch mode and `process --progress-events` stderr event stream.
- Locked stream-safety rule: `process --json` stdout remains single-payload JSON; machine progress events are opt-in and stderr-prefixed.

## 2026-02-28_17.30.33 - External lint preflight contract

- Source: merged understanding note (`2026-02-28_12.30.33`).
- Added `lint-contracts.md` documenting `codex-farm lint --json` for pack/schema preflight.
- Locked caller contract for deterministic finding codes, strict-vs-default exit behavior, and near-miss `--root` diagnostic handling.

## 2026-02-28_10.29.20 - External autotune diff-emitter contract

- Source: merged historical notes.
- Added caller contract for `run autotune --json` so integrations can map telemetry playbook output directly to command overrides and file diffs.
- Locked non-mutating expectation for external orchestrators: diffs are emitted for review/application by caller policy.

## 2026-02-28_15.20.27 - External caller telemetry v2 contract

- Source: merged historical notes.
- Updated caller contract for `run telemetry --json` to include schema-versioned `insights` and `tuning_playbook` sections.
- Clarified caller intent: external automation can now map telemetry directly to prompt edits, input prechecks, schema adjustments, runtime knobs, and model/effort overrides.

## 2026-02-28_15.02.31 - Aggregated telemetry report command contract

- Source: merged historical notes.
- Added `run telemetry --json` as the machine-facing recommendation report API over telemetry rows with caller filters.
- Added process integration contract: `process --json` now includes `telemetry_report` by default so callers can adjust prompts/data/schema without a second command.

## 2026-02-28_14.50.39 - Caller telemetry contract for prompt-tuning loops

- Source: merged historical notes.
- Added `telemetry-contracts.md` documenting machine-readable CSV fields for retry carry-forward context, Heads Up pass-forward hints, normalized failure categories, and output payload previews/fingerprints.
- Clarified caller strategy: join telemetry rows with `run tasks --json` / `run errors --json` via `run_id` + `task_id` to close prompt-improvement loops.

## 2026-02-28_04.16.54 - Caller model-menu contract

- Source: merged historical notes (merged).
- Added `models list --json` as the stable machine-facing model discovery command.
- Locked source contract: visible rows from local Codex cache metadata with filtering/deduping and a deterministic fallback row when cache metadata is absent.
- Acceptance evidence recorded in task doc: `tests/test_model_catalog.py tests/test_cli_integration_contracts.py -q` and full `pytest -q`.

## 2026-02-28_02.55.22 - Caller model-effort override contract

- Source: merged historical notes (merged).
- Added caller effort aliases across execution commands and normalized values (`none|minimal|low|medium|high|xhigh`).
- Added deterministic run persistence (`codex_reasoning_effort`) so queued worker retries/resumes keep caller-selected effort.
- Captured fail-before context from task doc: unknown effort flags, worker ignoring persisted effort, and missing codex-exec effort pass-through.

## 2026-02-28_02.47.41 - Caller model override contract

- Source: merged historical notes (merged).
- Added `--model` / `--codex-model` override support on `one`, `run create`, `process`, and `go`.
- Added deterministic run persistence (`codex_model`) only when explicitly provided by caller.
- Captured fail-before context from task doc: missing CLI model options and worker ignoring run-level model override.

## 2026-02-22_13.07.23 - External pack execution refinement

- Source: merged historical notes (merged).
- Added pipeline-driven `codex_cd_mode` behavior so external packs can control Codex working-directory semantics without patching core code.
- Refined `run errors --json` payload to include practical machine-diagnostic fields for caller programs.
- Recorded deterministic fake-codex integration coverage used to lock these contracts.

## 2026-02-22_12.36.41 - External pack integration baseline

- Source: merged historical notes (merged).
- Established external integrator workflow around `--root`, explicit `--workspace-root`, and machine-safe `process --json`.
- Added caller-accessible task/error introspection (`run tasks --json`, `run errors --json`) to remove direct SQLite dependency.
- Preserved run-config persistence of root/workspace values for deterministic retries and resumed workers.

## 2026-03-01_20.40.25 - External caller progress surface for RecipeImport
- Source: RecipeImport used blocking subprocess output capture and could not reflect CodexFarm task progress live.
- Decision: prefer `run progress --json` and optional progress events for callers while preserving single-payload `process --json` contract.
- Outcome: aligned caller contract with spinner-friendly polling/event channels and reduced coupling to stdout scraping.

## 2026-03-02_14.50.12 - External browser integration timeout diagnostics
- Source: forced manual-login mode in `oracle-browser-headless` produced hanging pending runs.
- Decision: document timeboxed diagnostic sequence and timeout log markers for caller support.
- Outcome: caller guidance now includes repeatable troubleshooting path for interactive-session-dependent browser helpers.

## 2026-03-02_15.03.51 - Push-protection failure from browser profile secrets
- Source: one commit introduced `.oracle/browser-profile/...` artifacts with JS containing a token; later cleanup did not clear historical push check.
- Decision: add persistent `.gitignore` controls and maintain this as high-priority integration hygiene note.
- Outcome: avoids repeated secret-history blocks for external automation teams and keeps CI/remote pushes deterministic.

## 2026-03-01_20.36.00 - Spinner/progress surfaces for external callers

- Source: `docs/tasks/2026-03-01_20.36.00-codexfarm-progress-events-for-callers.md`.
- Decision: add `run progress` (snapshot + optional watch mode) and `process --progress-events` (stderr stream) instead of changing default `process` stdout behavior.
- Why: external callers need live run visibility for spinner UX while the existing `process --json` single-payload contract must remain parse-stable.
- Design constraint that remains locked: all progress events remain opt-in and additive; stdout machine contracts stay unchanged.
- Operational memory:
  - initial caller integrations that only consumed final JSON could not display intermediate progress.
  - event output on stderr with fixed `__codex_farm_progress__ ` prefix became the stable boundary.

## 2026-03-02_09.37.43 - Recipeimport benchmark runtime mode and artifact contracts

- Source: `docs/tasks/2026-03-02_09.37.43-codexfarm-benchmark-runtime.md`.
- Decision: keep benchmark mode additive behind explicit flags and dispatch `line_label_v1` to dedicated prompt/schema/pipeline assets.
- Outcome:
  - canonical per-line calibrated payload is now promoted as the task output contract.
  - raw model payload remains a debug artifact for traceability.
  - benchmark pass artifacts and manifest hashes are written per-task for downstream compare tooling.
- High-severity path correction recorded:
  - schema validation was moved after calibration; validating before calibration caused false negatives for normalizable fields.
  - benchmark artifact write failures were moved from warning-only behavior to hard failures to avoid silent diagnostics loss.
