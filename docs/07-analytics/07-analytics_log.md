---
summary: "High-level evolution log for telemetry capture and analytics dashboard behavior."
read_when:
  - "When changing telemetry hook points, CSV row content, or dashboard loading behavior"
---

# 07 Analytics Log

## 2026-02-22_14.47.53 - Dashboard flow and file fallback contract

- Source: `docs/understandings/2026-02-22_14.47.53_analytics-dashboard-flow-and-fallback.md` (merged).
- Logged that telemetry CSV already carries enough context to build dashboard views without querying DB state.
- Preserved static-dashboard design rule: generator is read-only on telemetry input files.
- Captured renderer requirement to support direct `file://` use via inline data plus `assets/dashboard_data.json` fetch fallback.

## 2026-02-22_19.40.00 - Token telemetry hook-point decision

- Source: `docs/understandings/2026-02-22_19.40.00_token-telemetry-hook-point.md` (merged).
- Recorded central hook point at `src/codex_farm/codex_exec.py::run_codex_exec` so all Codex execution paths are covered once.
- Preserved caller-context requirement (`source`, `pipeline_id`, `run_id`, `task_id`, `worker_id`, `input_path`) for each row.
- Captured usage-field source as Codex JSONL `turn.completed.usage`.

