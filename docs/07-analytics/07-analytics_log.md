---
summary: "High-level evolution log for telemetry capture and analytics dashboard behavior."
read_when:
  - "When changing telemetry hook points, CSV row content, or dashboard loading behavior"
---

# 07 Analytics Log

## 2026-02-28_15.20.27 - Telemetry report v2 automation surfaces

- Source: merged historical notes.
- Bumped report schema to `2` and added `insights` (model/effort performance, prompt fingerprints, input hotspots, pass-forward deltas, and Codex event-stream diagnostics).
- Added `tuning_playbook` section with machine-consumable prompt/input/schema/runtime/model patch candidates so caller programs can apply adjustments directly.
- Preserved explicit limitation from task history: recommendation generation remains deterministic/rule-based in v2 (not yet multi-run weighted ranking).

## 2026-02-28_15.02.31 - Caller-facing telemetry report API

- Source: merged historical notes.
- Added `run telemetry` report contract so callers can request aggregated failure/retry/tip-effectiveness signals and recommendation categories without implementing CSV parsing logic.
- Added `process --json` embedded telemetry report output (default on) so one process call can immediately feed caller prompt-adjustment loops.
- Added legacy-row guardrail from task history: when newer telemetry columns are absent in old CSV rows, report logic infers cautiously (rather than treating missing fields as explicit `false`) to prevent false recommendation spikes.

## 2026-02-28_14.50.39 - Prompt-tuning telemetry enrichment

- Source: merged historical notes.
- Expanded telemetry row contract with structured retry carry-forward context, applied Heads Up tip payloads, failure classification (`failure_category`, `rate_limit_suspected`), Codex event summaries, and output payload digest/preview fields.
- Rationale: external caller programs can now detect recurring failure patterns and prompt guardrail effectiveness without scraping free-form prompt text.

## 2026-02-28_10.29.20 - Autotune translation layer over telemetry playbooks

- Source: merged historical notes.
- Added `run autotune` command as a machine-facing transformation from telemetry `tuning_playbook` to immediate caller artifacts (flag overrides, command preview, prompt/pipeline diffs).
- Preserved read-only contract: generated diffs are suggestions and no file mutation occurs in command execution.
- Captured boundary from task history: runtime timeout remediation is emitted as pipeline-config diff guidance because `process` currently has no direct `--timeout` override flag.

## 2026-02-28_09.21.54 - Verification visibility contract for telemetry rows

- Source: merged historical notes.
- Logged `codex_exec_activity.csv` as a first-class visibility surface for output acceptance outcomes, paired with `run tasks --json` and `run errors --json`.
- Preserved expectation that per-call row fields remain sufficient to debug accept/retry/error outcomes without replaying execution.

## 2026-02-28_02.55.22 - Model/effort telemetry expansion

- Source: merged historical notes.
- Added optional `reasoning_effort` CSV column in telemetry rows emitted by `run_codex_exec`.
- Rationale: external callers now set both model and effort via codex-farm flags, so telemetry needs both values for audit/debug reports.

## 2026-02-22_19.40.00 - Token telemetry hook-point decision

- Source: merged historical notes (merged).
- Recorded central hook point at `src/codex_farm/codex_exec.py::run_codex_exec` so all Codex execution paths are covered once.
- Preserved caller-context requirement (`source`, `pipeline_id`, `run_id`, `task_id`, `worker_id`, `input_path`) for each row.
- Captured usage-field source as Codex JSONL `turn.completed.usage`.

## 2026-02-22_14.47.53 - Dashboard flow and file fallback contract

- Source: merged historical notes (merged).
- Logged that telemetry CSV already carries enough context to build dashboard views without querying DB state.
- Preserved static-dashboard design rule: generator is read-only on telemetry input files.
- Captured renderer requirement to support direct `file://` use via inline data plus `assets/dashboard_data.json` fetch fallback.
