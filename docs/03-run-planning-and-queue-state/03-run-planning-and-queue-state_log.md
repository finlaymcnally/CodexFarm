---
summary: "High-level history for run planning contracts and queue-state semantics."
read_when:
  - "When changing run creation, task enqueue behavior, or run status reporting"
---

# 03 Run Planning And Queue State Log

## 2026-02-22_14.34.04 - Planning and inferred-status contract pass

- Source: `docs/understandings/2026-02-22_14.34.04_run-planning-and-queue-state-contract.md` (merged).
- Documented shared planning path (`_create_run_for_paths`) used by `run create` and `process`.
- Captured hidden contract that `runs.status` is inferred from grouped task counts in `db.run_status`, then synchronized back to `runs`.
- Preserved the `runs.config_json` seam: `farm_root` always persisted, `workspace_root` optional, with worker consumption later in execution flow.

