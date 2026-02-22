---
summary: "Implemented codex-farm changes for external pipeline packs, workspace-root control, and machine-safe run introspection."
read_when:
  - "When integrating codex-farm from another project with --root/--workspace-root"
  - "When reviewing JSON CLI contracts and run task/error exports"
---

# Make codex-farm integrator-friendly for 3-stage external pipeline packs

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` are kept current as implementation progressed.

This plan is maintained according to `docs/PLANS.md`.

## Purpose / Big Picture

External orchestrators can now call codex-farm in repeated stages while keeping pipeline assets outside this repository. The CLI supports explicit asset-root and workspace-root control, machine-safe JSON output, and per-task error export so callers do not need to scrape logs or inspect SQLite manually.

## Progress

- [x] (2026-02-22_12.05.00) Baseline complete: `.venv` install + existing test suite pass before edits.
- [x] (2026-02-22_12.18.00) Added first-class `--root` handling with precedence `--root` > `CODEX_FARM_ROOT` > auto-discovery.
- [x] (2026-02-22_12.22.00) Added `--workspace-root` for `one`, `run create`, `process`, and `go`; persisted `farm_root` and `workspace_root` in run metadata.
- [x] (2026-02-22_12.25.00) Updated worker execution to read persisted roots from `runs.config_json` and pass `workspace_root` into Codex `--cd`.
- [x] (2026-02-22_12.29.00) Made `process --json` machine-safe (stdout JSON only, progress on stderr) and stabilized JSON shapes for run commands.
- [x] (2026-02-22_12.30.00) Added `run tasks` and `run errors` with JSON output contracts.
- [x] (2026-02-22_12.33.00) Added tests for `--root`, workspace-root usage, JSON contracts, and error/task filtering.
- [x] (2026-02-22_12.34.00) Added external demo pipeline pack under `examples/pipeline_pack_demo/`.
- [x] (2026-02-22_12.34.53) Updated docs and conventions, plus discovery notes.
- [x] (2026-02-22_12.36.00) Full automated suite passes after implementation (`16 passed`).
- [ ] (pending manual) Run manual Codex-installed smoke workflow from outside repo and capture transcript.

## Surprises & Discoveries

- Observation: Worker resume behavior was previously tied to runtime discovery (`find_repo_root`) and not persisted run settings.
  Evidence: Existing `worker_loop` loaded pipelines once from discovered repo root before processing tasks.

- Observation: `process --json` previously still wrote progress lines to stdout.
  Evidence: `_run_workers` used `typer.echo(...)` unconditionally during polling.

## Decision Log

- Decision: Persist `farm_root` and `workspace_root` in `runs.config_json` instead of adding DB columns.
  Rationale: No schema migration needed; backward compatible with existing runs by falling back to worker/root discovery.
  Date/Author: 2026-02-22 / GPT-5 Codex

- Decision: Keep JSON contracts in CLI layer while leaving `db.run_status()` return shape unchanged.
  Rationale: Preserves internal call sites and text-mode summaries while introducing stable machine output externally.
  Date/Author: 2026-02-22 / GPT-5 Codex

- Decision: Add a generic `run tasks` command and implement `run errors` as the filtered variant.
  Rationale: Gives callers full or filtered task introspection with one DB query path.
  Date/Author: 2026-02-22 / GPT-5 Codex

## Outcomes & Retrospective

Implemented outcomes:

- External pipeline packs are first-class with `--root`.
- Codex execution directory is explicit with `--workspace-root` and persisted per run.
- `process --json` now emits parseable JSON on stdout only.
- `run tasks --json` and `run errors --json` provide machine-readable task diagnostics.
- Test coverage added for the new contracts and precedence rules.

Remaining gap:

- Manual smoke with a real installed `codex` binary was not run in this session.

## Context and Orientation

Primary implementation files:

- `src/codex_farm/paths.py`: new root resolver with explicit override precedence and validation errors.
- `src/codex_farm/cli.py`: new `--root` and `--workspace-root` options, JSON-safe process output, and `run tasks/errors` commands.
- `src/codex_farm/worker.py`: root/workspace loading from `runs.config_json`; Codex call now uses persisted workspace root.
- `src/codex_farm/db.py`: task listing query with optional status filter.
- `src/codex_farm/codex_exec.py`: clarified `cd_dir` argument naming for `codex exec --cd`.

Validation/test files:

- `tests/test_paths.py`
- `tests/test_db.py`
- `tests/test_worker.py`
- `tests/test_cli_integration_contracts.py`

Docs touched:

- `README.md`
- `docs/how-codex-farm-works.md`
- `docs/how-codex-farm-works-for-AI.md`
- `docs/IMPORTANT CONVENTIONS.md`
- `docs/understandings/2026-02-22_12.33.22_root-workspace-run-contract.md`

## Implemented Work

`--root` is now accepted by pipeline-loading/scaffolding commands (`pipelines list`, `pipelines new`, `one`, `run create`, `process`, `worker`, `go`). Resolution precedence is explicit override first, then env var, then discovery.

`--workspace-root` is now accepted by single and batch execution commands (`one`, `run create`, `process`, `go`). If omitted, it defaults to resolved `farm_root`. Both roots are stored in run config for consistent retries and resumed workers.

Workers now parse `runs.config_json` and prefer persisted roots for pipeline loading and Codex `--cd`. This makes historical runs deterministic even when invoked from a different shell or directory.

`process --json` now keeps stdout clean for one JSON object payload and pushes progress to stderr. `run create --json` and `run status --json` return stable object contracts with expected keys.

`run tasks` and `run errors` now expose per-task states and errors in both text and JSON modes.

## Concrete Steps

Commands executed during implementation:

- `source .venv/bin/activate && pip install -e '.[dev]' && pytest`
- Code edits across `src/codex_farm/*` and tests.
- Added example pack files under `examples/pipeline_pack_demo/`.
- Updated docs listed above.

## Validation and Acceptance

Automated acceptance completed:

- Root override precedence covered by `tests/test_paths.py` and CLI-level `pipelines list --root ...` test.
- Workspace-root flow validated by mocked Codex call assertions in `tests/test_worker.py` and `tests/test_cli_integration_contracts.py`.
- JSON-only stdout for `process --json` validated by direct JSON parse of CLI stdout in tests.
- `run errors --json` and `run tasks --status ... --json` correctness validated by CLI tests over seeded DB rows.
- Full suite result: `16 passed in 2.64s`.

## Idempotence and Recovery

Changes are additive and safe to rerun:

- Existing runs without persisted roots continue to work via fallback discovery.
- New CLI flags are optional and backward compatible.
- Re-running tests and scaffolding commands remains deterministic (scaffolding still refuses to overwrite existing files).

## Artifacts and Notes

Representative `process --json` shape:

    {
      "run_id": "<id>",
      "pipeline_id": "demo.contract.v1",
      "status": "done",
      "counts": {"queued": 0, "running": 0, "done": 2, "error": 0, "total": 2},
      "input_dir": "...",
      "output_dir": "...",
      "farm_root": "...",
      "workspace_root": "...",
      "worker_exit_codes": [0, 0],
      "exit_code": 0
    }

## Interfaces and Dependencies

New/updated interfaces:

- `paths.resolve_farm_root(root_override: Path | str | None, start: Path | None = None) -> Path`
- `worker.worker_loop(..., farm_root: Path | None = None) -> int`
- `db.list_tasks_for_run(conn, run_id, status=None) -> list[dict]`
- CLI commands:
  - `run tasks --run-id ... [--status ...] [--json]`
  - `run errors --run-id ... [--json]`

Codex wrapper contract remains the same except argument naming clarity (`cd_dir`), still mapped to `codex exec --cd ...`.

Plan update note: Replaced planning-only content with implementation-complete state, including exact shipped behavior, tests, and docs updates, so this file now serves as restart-safe operational context.
