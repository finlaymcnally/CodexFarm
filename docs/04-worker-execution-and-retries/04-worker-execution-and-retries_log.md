---
summary: "Major decisions and retries/lease semantics history for worker execution."
read_when:
  - "When editing lease logic, retry policy, or process worker orchestration"
---

# 04 Worker Execution And Retries Log

## 2026-02-20_12.50.00 - Worker leasing and in-process process loop discovery

- Source: `docs/understandings/2026-02-20_12.50.00_worker-leasing-and-process-loop.md` (merged).
- Recorded that `process` runs worker threads in-process rather than spawning nested `codex-farm worker` subprocesses.
- Confirmed lease safety model: `BEGIN IMMEDIATE` plus atomic claim update prevents duplicate task claims and increments `attempts` at claim time.
- Captured retry terminal behavior: recoverable errors requeue until budget is exhausted, then task is marked `error`.

## 2026-02-20_13.24.12 - End-to-end process and worker control-flow map

- Source: `docs/understandings/2026-02-20_13.24.12_end-to-end-process-and-worker-flow.md` (merged).
- Mapped call path from CLI process/go to worker leasing, codex execution, schema validation, and final run-status inference.
- Clarified that codex non-zero exits can still proceed when payload exists, with final pass/fail decided by local schema validation.

## 2026-02-22_14.34.10 - Attempt budget and terminal-branch clarification

- Source: `docs/understandings/2026-02-22_14.34.10_worker-attempt-budget-and-terminal-branches.md` (merged).
- Preserved the key invariant: attempts are consumed on lease claim, including expired-lease reclaims after crashes.
- Logged explicit pre-execution guard (`attempts > max_attempts`) that terminates over-budget reclaimed tasks immediately.
- Captured output-cleanup policy on execution failure before requeue/terminal transitions.

## 2026-02-23_00.24.39 - Rate-limit hard stop policy

- Source: `docs/understandings/2026-02-23_00.24.39_rate-limit-429-stop-policy.md`.
- Added explicit terminal branch for codex rate-limit failures (`429`/rate-limit text) so workers do not retry those errors.
- Documented shared stop-event behavior used by `process` to halt additional task claims after a rate-limit hit.
