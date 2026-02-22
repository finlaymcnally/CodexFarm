Core runtime modules: `cli.py` (commands), `db.py` (queue state), `worker.py` (task execution), and `codex_exec.py` (safe subprocess wrapper).
`codex_exec.py` now also writes per-call telemetry CSV rows (prompt, token usage, timing, and run/task metadata) to `codex_exec_activity.csv` in the active data dir.
`analytics_dashboard.py` reads that CSV and writes a static dashboard bundle (`index.html` + `assets/`) used by `codex-farm stats-dashboard`.
