---
summary: "Major decisions and retries/lease semantics history for worker execution."
read_when:
  - "When editing lease logic, retry policy, or process worker orchestration"
---

# 04 Worker Execution And Retries Log

## 2026-03-02_00.45.23 - Immediate terminal handling for Codex auth failures

- Source: RecipeImport incident where all tasks retried despite websocket `403 Forbidden` auth failure.
- Worker runtime classification now treats Codex auth/session failures as `failure_category="auth_failure"` instead of generic retryable runtime failures.
- Locked policy: auth failures become terminal on first effective execution attempt with remediation text (`run codex and sign in`), preventing wasted retry loops.

## 2026-02-28_14.03.46 - Heartbeat reclaim jitter hardening

- Source: flaky test triage (`tests/test_worker.py::test_worker_heartbeat_prevents_lease_reclaim_for_long_running_task`).
- Added lease jitter guard in DB lease writes: claims and heartbeats now set `lease_until` with `+2s` grace to tolerate thread scheduling spikes on very short leases.
- Reduced contention path: `lease_one_task(...)` now read-checks for queued/expired candidates before opening `BEGIN IMMEDIATE`, avoiding unnecessary write-lock churn when no claim is possible.
- Stabilized heartbeat contention test by synchronizing on observed heartbeat before introducing competing worker pressure.

## 2026-02-28_13.50.27 - Heartbeat + staged-promotion stale-owner safety

- Source: merged understanding note.
- Clarified coupling requirement: heartbeat prevents healthy long-running work from false reclaim, but does not by itself protect canonical outputs from stale-owner cleanup races.
- Locked owner-checked staged promotion (`task_id + lease_token`) as the canonical-output safety seam; stale workers only clean up staged artifacts.
- Preserved retry-budget clarity: debugging/limits should key off `execution_attempts - rate_limit_count` rather than raw lease claims.
- Added explicit caller-surface continuity from task history: legacy `attempts` remains lease-claim index while additive `execution_attempts`, `lease_claims`, and `last_heartbeat_at` expose true execution/lease behavior without breaking existing JSON consumers.

## 2026-02-28_13.49.48 - Failure-forensics capture order guardrails

- Source: merged understanding note.
- Locked ordering contract: capture forensics before staged/final output cleanup on schema/runtime failure paths so rejected payload evidence is not lost.
- Documented timeout limitation: timeout bundles are metadata/tail-based because `run_codex_exec(...)` removes temp output in timeout branches.

## 2026-02-28_13.27.21 - Baseline refresh for lifecycle/throttle/forensics seams

- Source: merged understanding note.
- Captured that `lease_one_task(...)` claim gating already combines `runs.control_state` and `run_throttle_state`; heartbeat work must preserve pause/cancel/cooldown semantics together.
- Recorded refactor constraint: staged-output/promotion changes must keep forensics coverage across preflight, codex-exec, schema, and unexpected-exception failure branches.
- Noted attempt-counter split remained explicit only by convention at this point (`attempts` vs `execution_attempts`).

## 2026-02-28_13.24.13 - Adaptive 429 transactional claim gating and budget semantics

- Source: merged understanding note.
- Locked transactional safety rule: cooldown/concurrency checks must run inside the same claim transaction as lease updates to prevent concurrent over-claim.
- Preserved retry-budget semantics based on `effective_attempts = execution_attempts - rate_limit_count` so provider throttling does not consume execution budget.
- Documented `once=True` waitability requirement (`run_has_waitable_work`) so `process` can remain alive through cooldown windows and finish automatically.
- Captured failed attempt from task history: stderr warning de-duplication hid cooldown/give-up transitions during persistent throttling tests; transition messages are now emitted per state change so operators and tests can see recovery path evolution.

## 2026-02-28_13.20.16 - Lease-token baseline and direct-output race discovery

- Source: merged understanding note.
- Documented baseline ownership guards on DB transitions (`mark_task_done`, `mark_task_error`, `requeue_task`) before heartbeat rollout.
- Captured stale-worker filesystem race: direct canonical writes plus stale-owner unlink could delete valid output from a newer lease owner.
- Logged design direction that became staged promotion: write to lease-scoped staging first, then promote only when owner check passes.

## 2026-02-28_10.14.00 - Heads Up prompt adaptation integrated into worker flow

- Source: merged task doc `docs/tasks/2026-02-28_09.33.49-heads-up-adaptive-prompts.md`.
- Added worker-time prompt augmentation seam: compute input signature, select pipeline tips (exact + wildcard), append deterministic `Heads up` block before Codex execution.
- Locked deterministic constraints from task history:
  - adaptation is cross-run only (not same-run mutable),
  - run-config flags (`heads_up_enabled`, `heads_up_max_tips`) are persisted and consumed at execution time,
  - learning remains terminal-only and warning-safe.
- Preserved anti-regression detail: missing distiller assets in external packs must degrade to warning-only learning behavior, not task/run failure.

## 2026-02-28_09.47.44 - Attempt-aware retry prompts

- Source: merged historical notes.
- Updated lease contract: `lease_one_task(...)` now returns `previous_error` from the pre-claim task row so retry context survives requeue and cross-worker claims.
- Updated worker prompt contract: attempts `>1` append retry context with the previous failure message to guide corrective structured output on retries.
- Preserved queue semantics: retry state still flows through `requeue_task(...)`, with terminal handling unchanged at `attempts >= max_attempts`.

## 2026-02-28_09.39.25 - Task failure lifecycle consolidation

- Source: merged historical notes.
- Consolidated retry lifecycle: timeout/runtime/schema failures requeue while `attempts < max_attempts`, then become terminal `error` at budget.
- Preserved output-cleanup contract: worker removes produced output files before requeue/error to avoid stale invalid JSON artifacts.
- Recorded then-current rate-limit short-circuit behavior as a temporary mitigation; this was later superseded by adaptive cooldown/concurrency handling (`2026-02-28_13.24.13`).
- Captured operator inspection contract: `run tasks --json` and `run errors --json` are the durable surfaces for attempts/error/lease metadata.

## 2026-02-28_09.31.02 - Run-config output-schema override precedence

- Source: merged historical notes.
- Added worker contract: when `runs.config_json.output_schema_path_override` exists, it overrides pipeline `output_schema_path` for both Codex structured-output request and local validation.
- Preserved fallback behavior to pipeline schema when no override is stored.

## 2026-02-28_02.55.22 - Run-config effort override precedence

- Source: merged historical notes.
- Added worker contract: when `runs.config_json.codex_reasoning_effort` exists, it overrides pipeline `codex_reasoning_effort` for execution.
- Preserved fallback behavior to pipeline-config effort (or none) when no override is stored.

## 2026-02-28_02.47.41 - Run-config model override precedence

- Source: merged historical notes.
- Added worker contract: when `runs.config_json.codex_model` exists, it overrides pipeline `codex_model` for execution.
- Preserved fallback behavior to pipeline model when no override is stored.

## 2026-02-20_12.50.00 - Worker leasing and in-process process loop discovery

- Source: merged historical notes (merged).
- Recorded that `process` runs worker threads in-process rather than spawning nested `codex-farm worker` subprocesses.
- Confirmed lease safety model: `BEGIN IMMEDIATE` plus atomic claim update prevents duplicate task claims and increments `attempts` at claim time.
- Captured retry terminal behavior: recoverable errors requeue until budget is exhausted, then task is marked `error`.

## 2026-02-20_13.24.12 - End-to-end process and worker control-flow map

- Source: merged historical notes (merged).
- Mapped call path from CLI process/go to worker leasing, codex execution, schema validation, and final run-status inference.
- Clarified that codex non-zero exits can still proceed when payload exists, with final pass/fail decided by local schema validation.

## 2026-02-22_14.34.10 - Attempt budget and terminal-branch clarification

- Source: merged historical notes (merged).
- Preserved the key invariant: attempts are consumed on lease claim, including expired-lease reclaims after crashes.
- Logged explicit pre-execution guard (`attempts > max_attempts`) that terminates over-budget reclaimed tasks immediately.
- Captured output-cleanup policy on execution failure before requeue/terminal transitions.

## 2026-02-23_00.24.39 - Rate-limit hard stop policy

- Source: merged historical notes.
- Added explicit terminal branch for codex rate-limit failures (`429`/rate-limit text) so workers did not retry those errors in the initial mitigation.
- Documented shared stop-event behavior used by `process` to halt additional task claims after a rate-limit hit before adaptive throttling existed.
- Task-source evidence (merged historical notes): lock tests were `tests/test_worker.py::test_worker_loop_stops_immediately_on_rate_limit` and `tests/test_process_smoke.py::test_process_command_stops_after_first_rate_limit`; task note also recorded targeted worker/process suites and CLI integration contracts as passing.

## 2026-02-22_13.07.23 - Worker `cd` mode and terminal config-error pass

- Source: merged historical notes (merged).
- Added worker-time `codex_cd_mode` resolution for external pack runs with deterministic precedence (`workspace_root` override first, then mode).
- Explicitly marked missing computed `--cd` directories as terminal configuration errors to avoid retry loops.
- Recorded acceptance coverage including `tests/test_worker.py::test_worker_loop_selects_cd_dir_from_pipeline_mode` and fake-codex integration suite checks.

## 2026-02-20_12.45.00 - Initial worker/retry baseline

- Source: merged historical notes (merged).
- Established lease-based retry architecture, attempt-budget defaults, and crash-reclaim behavior as V1 worker foundations.
- Recorded early design decision to use in-process thread fan-out for `process` instead of nested worker subprocesses.
