Core runtime modules: `cli.py` (commands), `db.py` (queue state), `worker.py` (task execution), and `codex_exec.py` (safe subprocess wrapper).
`codex_exec.py` now also writes per-call telemetry CSV rows (prompt + output fingerprints, token/event usage, timing, failure class, and run/task metadata) to `codex_exec_activity.csv` in the active data dir.
`worker.py` treats Codex rate-limit failures (`429`) as adaptive runtime throttling events: tasks are requeued with cooldown/concurrency gating and only become terminal after the configured consecutive-rate-limit budget is exhausted.
`worker.py` treats Codex auth/session failures (`401/403`, websocket auth denial, login-required text) as immediate terminal task errors with remediation guidance, so runs do not burn retry budget when the local Codex session is not signed in.
`worker.py` now keeps active leases alive with heartbeat writes and stages outputs under `.codex-farm-stage/` so stale owners cannot overwrite or delete canonical outputs; task rows expose both lease claims (`attempts`) and real execution starts (`execution_attempts`).
`forensics.py` captures self-contained failure bundles under `<data_dir>/forensics/` and backs `run forensics --json` via the `task_forensics` SQLite index table.
Run lifecycle controls now exist in DB/CLI/worker (`run pause|resume|cancel|retry-errors`): runs have operator `control_state`, tasks can be `canceled`, and worker task-finalization is lease-token guarded to prevent stale writes.
`cli.py` now runs a default execution precheck (`codex login status`) on `one`, `worker`, `process`, and `go` to fail before enqueue/execution when local Codex login is missing.
`cli.py` now also exposes spinner-facing progress APIs: `run progress --json` (snapshot/watch polling) and optional `process --progress-events` stderr JSON events prefixed with `__codex_farm_progress__ `.
`cli.py` supports model/effort/schema overrides on `one`, `run create`, `process`, and `go`; run-based flows persist `codex_model`, `codex_reasoning_effort`, and optional `output_schema_path_override` in run config so workers use the same execution settings and validation contract across resumes.
`run_assets.py` freezes effective pipeline + prompt + schema files per run under `<data_dir>/run_assets/<run_id>/`; worker execution is snapshot-first when `runs.config_json.frozen_assets` is present.
`pipeline_spec.py` supports prompt placeholders `{{INPUT_PATH}}` and `{{INPUT_TEXT}}`; pipeline packs can choose required prompt token via `prompt_input_mode` (`path` default, `inline` optional), and `pack_lint.py` enforces that contract.
`recipeimport_benchmark_line_label.py`, `recipeimport_benchmark_calibration.py`, and `recipeimport_benchmark_eval.py` implement `--recipeimport-benchmark-mode line_label_v1`, including per-task benchmark artifacts under `<run output>/.recipeimport-benchmark/<task_id>/`.
In benchmark mode, schema validation failures are categorized as terminal `benchmark_contract_error` after calibration (no retry loop for unrecoverable contract mismatches).
Benchmark `alignment_coverage` in `pass.metrics.json` reflects observed pre-fill model coverage (`observed_lines / total_lines`), while calibrated output remains one row per canonical line.
`pack_lint.py` powers `codex-farm lint` for read-only pack/schema preflight with deterministic error/warning findings and machine-parseable JSON output.
`cli.py` also exposes `heads-up` commands plus `--heads-up`/`--heads-up-max-tips` run settings so prompts can include learned guidance from prior runs.
`heads_up.py` handles input signatures, prompt block rendering, one-shot post-run distillation, and SQLite tip scoring metadata.
`model_catalog.py` reads local Codex `models_cache.json` metadata; `codex-farm models list --json` is the caller-facing model-picker feed.
`analytics_dashboard.py` reads that CSV and writes a static dashboard bundle (`index.html` + `assets/`) used by `codex-farm stats-dashboard`.
`telemetry_report.py` builds schema-versioned telemetry reports consumed by `codex-farm run telemetry --json` and embedded in `process --json`, including `insights` and `tuning_playbook` sections for caller-side prompt/data/schema/runtime adjustments.
`autotune.py` translates telemetry playbook output into caller-ready `process` overrides and prompt/pipeline diff suggestions for `codex-farm run autotune --json`.
