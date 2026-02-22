---
summary: "Implemented codex_cd_mode, deterministic fake-codex integration tests, and machine-safe error introspection for external knowledge-correction pipeline packs."
read_when:
  - "When integrating codex-farm as an external pipeline executor with --root and pipeline-driven Codex --cd"
  - "When validating process --json and run errors --json contracts"
---

# Make codex-farm a clean pipeline executor for external knowledge-correction passes

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` were updated during implementation.

This plan is maintained according to `docs/PLANS.md`.

## Purpose / Big Picture

codex-farm now supports pipeline packs in external projects with deterministic, machine-readable behavior across `one`, `process`, and worker execution. Pipeline authors can select Codex working-directory behavior in pipeline JSON (`codex_cd_mode`) and still override explicitly with `--workspace-root` when needed. External orchestrators can consume stable JSON summaries from `process --json` and inspect failures via `run errors --json` without opening SQLite directly.

## Progress

- [x] (2026-02-22_13.01.00) Re-ran baseline in `.venv` and confirmed pre-change suite was green.
- [x] (2026-02-22_13.08.00) Added pipeline `codex_cd_mode` to pipeline spec model/runtime with default `asset_root`.
- [x] (2026-02-22_13.13.00) Updated CLI/worker `--cd` resolution so explicit `--workspace-root` overrides pipeline mode, otherwise mode drives `--cd`.
- [x] (2026-02-22_13.16.00) Added `db.list_error_tasks(...)` and switched `run errors` to the dedicated error-task payload.
- [x] (2026-02-22_13.19.00) Updated scaffold/demo pipeline assets to include and demonstrate `codex_cd_mode`.
- [x] (2026-02-22_13.25.00) Added deterministic fake-codex integration tests (`one`, `process --json`, and schema-failure `run errors --json`) plus worker mode-selection tests.
- [x] (2026-02-22_13.27.00) Updated docs (`README`, architecture docs, conventions, understandings) to match shipped behavior.
- [x] (2026-02-22_13.28.00) Full test suite passes after implementation (`24 passed`).

## Surprises & Discoveries

- Observation: Previous `--workspace-root` behavior acted as a default and masked any pipeline-driven `--cd` behavior.
  Evidence: `run create` and `process` always persisted `workspace_root` even when user did not pass it.

- Observation: `run errors --json` previously returned generic task rows (`status`, `output_path`) rather than the operational error fields needed for machine diagnostics.
  Evidence: CLI path reused `list_tasks_for_run(..., status="error")`.

- Observation: Fake-codex subprocess tests are practical and stable for end-to-end contract checks.
  Evidence: `tests/test_fake_codex_pipeline_pack_demo.py` validates prompt substitution + `--cd` outcomes without real Codex access.

## Decision Log

- Decision: Keep `--workspace-root` as an explicit override, but only persist it when user provides it.
  Rationale: Preserves existing override semantics while allowing pipeline `codex_cd_mode` to work by default for external pack use cases.
  Date/Author: 2026-02-22 / GPT-5 Codex

- Decision: Define `one` semantics for `codex_cd_mode=input_dir` as `Path(--in).parent`.
  Rationale: `one` has no run-wide input root, so input-dir mode must map deterministically to file-parent.
  Date/Author: 2026-02-22 / GPT-5 Codex

- Decision: Treat missing computed `--cd` directories as terminal configuration errors.
  Rationale: These failures are non-transient and should not consume retry budget via requeue loops.
  Date/Author: 2026-02-22 / GPT-5 Codex

- Decision: Add dedicated `list_error_tasks` query for `run errors`.
  Rationale: Maintains a stable machine payload and decouples error introspection from generic task-list output shape.
  Date/Author: 2026-02-22 / GPT-5 Codex

## Outcomes & Retrospective

Implemented outcomes:

- Pipelines support `codex_cd_mode` with values `asset_root`, `input_dir`, and `input_file_dir`.
- Worker and `one` select Codex `--cd` from explicit override or pipeline mode.
- `run errors --json` now returns focused error diagnostics (`task_id`, lease/error metadata).
- Demo pack under `examples/pipeline_pack_demo/` now encodes `codex_cd_mode: input_dir`.
- Deterministic fake-codex integration tests prove end-to-end behavior without real LLM calls.

No remaining implementation gaps were found against this plan. Manual local CLI experimentation with a real `codex` binary was not run in this session.

## Context and Orientation

Primary files changed:

- `src/codex_farm/pipeline_spec.py` for `codex_cd_mode` model/runtime field.
- `src/codex_farm/cli.py` for explicit workspace override behavior, one-file `--cd` resolution, scaffold defaults, and `run errors` wiring.
- `src/codex_farm/worker.py` for run-config parsing and per-task `--cd` resolution using pipeline mode.
- `src/codex_farm/db.py` for `list_error_tasks`.
- `examples/pipeline_pack_demo/*` for concrete external-pack behavior.
- `tests/test_fake_codex_pipeline_pack_demo.py` and updated unit/CLI tests for contracts.

Key runtime rule after implementation:

- Codex working directory is selected in strict precedence:
  1. explicit `--workspace-root`
  2. pipeline `codex_cd_mode`

## Implemented Work

Milestone 1 (`--root`) was already present. This implementation focused on closing Milestone 2 and Milestone 3 gaps:

- Added pipeline-driven `codex_cd_mode` and threaded it through `one` and worker execution.
- Preserved backward compatibility for callers that explicitly pass `--workspace-root`.
- Added dedicated DB/CLI error listing path for `run errors --json`.
- Added fake-codex end-to-end tests and updated demo pipeline assets to assert `INPUT={{INPUT_PATH}}` plus `cd` output behavior.
- Updated architecture/docs/conventions to keep behavior and contracts synchronized.

## Concrete Steps

Commands run from repository root:

- `source .venv/bin/activate && pip install -e '.[dev]' && pytest`
- `source .venv/bin/activate && pytest tests/test_worker.py tests/test_fake_codex_pipeline_pack_demo.py tests/test_cli_integration_contracts.py tests/test_db.py tests/test_cli_scaffold.py tests/test_pipeline_spec.py -q`
- `source .venv/bin/activate && pytest -q`

## Validation and Acceptance

Automated acceptance completed:

- `codex_cd_mode` unit coverage:
  - `tests/test_worker.py::test_worker_loop_selects_cd_dir_from_pipeline_mode`
  - `tests/test_pipeline_spec.py` explicit/invalid mode checks
- Deterministic fake-codex integration:
  - `tests/test_fake_codex_pipeline_pack_demo.py::test_one_with_root_and_cd_mode`
  - `tests/test_fake_codex_pipeline_pack_demo.py::test_process_with_root_and_cd_mode`
  - `tests/test_fake_codex_pipeline_pack_demo.py::test_run_errors_json_on_schema_failure`
- Error introspection contract:
  - `tests/test_db.py` (`list_error_tasks`)
  - `tests/test_cli_integration_contracts.py` (`run errors --json`)
- Full suite result:
  - `24 passed`

## Idempotence and Recovery

Changes are additive and safe to rerun:

- Pipelines omitting `codex_cd_mode` still default to `asset_root`.
- Older runs with persisted `workspace_root` continue to honor that explicit override.
- New runs only persist `workspace_root` when supplied, so pipeline mode remains effective.
- Fake-codex tests use temporary directories and temporary PATH overrides, leaving no persistent global state.

## Artifacts and Notes

Representative `process --json` payload now includes nullable workspace override:

    {
      "run_id": "<id>",
      "pipeline_id": "demo.echo.v1",
      "status": "done",
      "counts": {"queued": 0, "running": 0, "done": 2, "error": 0, "total": 2},
      "workspace_root": null,
      "worker_exit_codes": [0, 0],
      "exit_code": 0
    }

Representative `run errors --json` row fields:

    {
      "task_id": "<task>",
      "input_path": "/tmp/.../bad.json",
      "rel_output_path": "bad.json",
      "attempts": 1,
      "error": "Schema validation failed ...",
      "leased_by": null,
      "lease_until": null,
      "updated_at": "..."
    }

## Interfaces and Dependencies

New/updated interfaces:

- `PipelineSpecModel.codex_cd_mode` and `PipelineSpec.codex_cd_mode`
- `db.list_error_tasks(conn, run_id) -> list[dict]`
- `cli run errors` now uses `list_error_tasks`
- Worker/one `--cd` selection logic now depends on `codex_cd_mode` when no explicit workspace override is present

No third-party dependencies were added.

Plan update note (required living-plan note): Replaced planning draft content with implementation-complete state, removed stale citation placeholders, added required front matter, and recorded shipped behavior/tests so this file can serve as a restart-safe reference.
