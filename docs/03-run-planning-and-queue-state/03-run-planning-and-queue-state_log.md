---
summary: "High-level history for run planning contracts and queue-state semantics."
read_when:
  - "When changing run creation, task enqueue behavior, or run status reporting"
---

# 03 Run Planning And Queue State Log

## 2026-02-28_20.36.00 - Lifecycle control-plane rollout (`pause|resume|cancel|retry-errors`)

- Source: merged task doc `docs/tasks/idea1-1.md`.
- Added explicit operator-control axis (`runs.control_state`) separate from task-derived run status so pause/cancel intent is represented without corrupting progress semantics.
- Added queue-level lifecycle data and transitions:
  - `tasks.status='canceled'`,
  - `tasks.lease_token` ownership checks,
  - `run status --json` / `process --json` exposure of `control_state` and `counts.canceled`.
- Preserved critical stale-write boundary from implementation history: lease-token-guarded task transitions are mandatory because worker IDs can repeat across invocations.
- Failed path worth avoiding: treating pause/cancel as hard interrupt semantics was intentionally rejected; this codebase relies on graceful lease gating and drain behavior.

## 2026-02-28_18.23.00 - Incremental planning contract (`--incremental`, `--incremental-from`)

- Source: merged task doc `docs/tasks/idea1-4.md`.
- Added additive planning metadata:
  - `runs.execution_fingerprint`,
  - task reuse provenance fields (`reused_from_run_id`, `reused_from_task_id`),
  - additive `incremental` block in `run create --json` and `process --json`.
- Locked safety gate from task history: hash-only reuse is unsafe; planner must require both matching `input_hash` and fingerprint compatibility before reusing outputs.
- Preserved resilience behavior: missing/invalid source output should downgrade that task to queued work, not fail the entire run.
- Noted unresolved historical pitfall from task thread: brittle assertions against one stderr phrase (`budget exhausted`) gave false negatives even when queue/error behavior was correct.

## 2026-02-28_20.38.15 - Lifecycle status/control split and stale-write boundary

- Source: merged understanding note.
- Clarified run-state contract: task-derived `status` and operator-intent `control_state` are separate dimensions and must remain separately visible in JSON surfaces.
- Reaffirmed stale-safety seam: lease-token ownership checks on DB task transitions prevent lost-lease workers from writing `done` over reclaimed or canceled tasks.

## 2026-02-28_13.22.04 - Incremental reuse fingerprint contract

- Source: merged understanding note.
- Tightened reuse safety framing: `input_hash` is necessary but insufficient; reuse requires matching `runs.execution_fingerprint` plus valid prior output/schema compatibility.
- Documented fallback behavior: when prior outputs are missing or fail current schema validation, planning downgrades those tasks to queued execution instead of failing the whole run.

## 2026-02-28_13.18.06 - Frozen run-assets determinism seam

- Source: merged understanding note.
- Recorded deterministic-run requirement: freeze effective prompt/schema/pipeline assets at run creation and persist snapshot metadata in `runs.config_json.frozen_assets`.
- Locked snapshot-first worker expectation for snapshot-bearing runs so live pack edits do not silently alter queued/retried task behavior.

## 2026-02-28_10.12.00 - Heads Up cross-run learning contract hardening

- Source: merged historical notes.
- Locked deterministic adaptation contract: workers consume persisted `heads_up_enabled` and `heads_up_max_tips` from `runs.config_json`, while learning remains cross-run rather than same-run mutable state.
- Captured local tip-ranking contract for future tuning work: smoothed score `(wins + 1) / (uses + 2)` with bad-tip suppression at `uses >= 8` and `score < 0.25`.
- Preserved terminal-only learning gate so non-terminal runs do not feed distillation and learner failures stay warning-only.

## 2026-02-28_09.32.28 - Prompt-adjustment seam without queue-schema changes

- Source: merged historical notes.
- Recorded current prompt seam: `render_prompt_template(...)` remains the template entrypoint and currently only substitutes `{{INPUT_PATH}}`.
- Captured extension rule: new adaptive prompt toggles should persist in `runs.config_json` and be consumed at worker execution time.
- Preserved queue contract: prompt adaptation should not require `tasks` schema changes.
- Logged determinism warning: same-run adaptation is lease-order dependent under concurrent workers; prior-run adaptation is deterministic.

## 2026-02-28_09.31.02 - Optional output-schema run-config persistence

- Source: merged historical notes.
- Extended run planning contract so schema overrides persist as optional `runs.config_json.output_schema_path_override`.
- Documented additive `output_schema_path` in `run create --json` payload for machine callers.

## 2026-02-28_02.55.22 - Optional codex_reasoning_effort run-config persistence

- Source: merged historical notes.
- Extended run planning contract so effort overrides persist as optional `runs.config_json.codex_reasoning_effort`.
- Documented additive `codex_reasoning_effort` in `run create --json` payload for machine callers.

## 2026-02-28_02.47.41 - Optional codex_model run-config persistence

- Source: merged historical notes.
- Extended planning contract so run-based CLI flows persist `codex_model` in `runs.config_json` only when `--model` override is provided.
- Documented additive `codex_model` field in `run create --json` payload for machine-readable confirmation of resolved model.

## 2026-02-22_14.34.04 - Planning and inferred-status contract pass

- Source: merged historical notes (merged).
- Documented shared planning path (`_create_run_for_paths`) used by `run create` and `process`.
- Captured hidden contract that `runs.status` is inferred from grouped task counts in `db.run_status`, then synchronized back to `runs`.
- Preserved the `runs.config_json` seam: `farm_root` always persisted, `workspace_root` optional, with worker consumption later in execution flow.

## 2026-02-22_13.07.23 - Error-task JSON contract refinement

- Source: merged historical notes (merged).
- Switched `run errors` behavior to dedicated error-task payload shape suitable for machine diagnostics (instead of generic task rows).
- Preserved compatibility goal for external orchestrators that should not inspect SQLite tables directly.

## 2026-02-22_12.36.41 - External caller run/task introspection baseline

- Source: merged historical notes (merged).
- Introduced explicit caller-facing contracts for `run tasks --json` and `run errors --json`.
- Captured planning requirement that run config persists root/workspace decisions for deterministic resume behavior.

## 2026-02-20_12.45.00 - Initial queue schema baseline

- Source: merged historical notes (merged).
- Established V1 queue model with immutable run records, one task per input file, lease metadata on task rows, and attempt-budget semantics.
- Recorded initial idempotence principle: reruns create new run IDs while preserving deterministic per-task output mapping.
