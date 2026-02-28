# IMPORTANT CONVENTIONS

- Treat `run telemetry --json` and embedded `process --json.telemetry_report` as the same caller contract. If report shape changes, keep both in lockstep.
- When telemetry report JSON shape changes, increment `TELEMETRY_REPORT_SCHEMA_VERSION`, update contract docs in `docs/01-*`, `docs/07-*`, and `docs/08-*`, and add/adjust tests.
- Keep `run autotune --json` aligned to telemetry playbook IDs; if playbook item IDs change, update autotune mapping logic and tests together.
- Keep telemetry row writes centralized in `src/codex_farm/codex_exec.py::run_codex_exec`; duplicating writes elsewhere causes double-counting.
- `process --json` stdout must remain parseable single JSON output, even when telemetry warnings exist.
- `run autotune` is non-mutating by contract: emit suggestions/diffs only, never write patches automatically.
