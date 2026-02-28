---
summary: "Integration contract tests and deterministic fixtures that guard cross-chunk behavior."
read_when:
  - "When changing CLI JSON payloads, --root/--workspace-root behavior, or run task/error exports"
  - "When adding or debugging integration tests that span CLI, worker, and schema boundaries"
---

# Scope

This chunk owns integration confidence across chunks 01-05.  
It does not implement runtime logic directly. It verifies that end-to-end seams still satisfy the public contracts.

If a module-level unit test passes but CLI behavior changed, this is the first place that should fail.

## Primary files

- `tests/test_cli_integration_contracts.py`
- `tests/test_fake_codex_pipeline_pack_demo.py`
- `tests/test_process_smoke.py`
- `tests/test_incremental.py`
- `examples/pipeline_pack_demo/` (`README.md`, `pipelines/`, `prompts/`, `schemas/`)

## Why separate

Most cross-chunk regressions first appear here, so this is the fastest place to confirm impact.

## Mental model

The integration layer tests a single call path from user command to terminal state:

1. CLI command accepts flags and resolves roots.
2. Run/task rows are created or queried in SQLite.
3. Worker loop claims tasks and computes `codex --cd`.
4. Codex execution writes output JSON.
5. Schema validation accepts/rejects output.
6. CLI emits stable JSON/text contracts.

Chunk 06 is where those boundaries are tested together, not in isolation.

## Contract map (what these tests lock down)

| Contract | Main assertion location |
| --- | --- |
| `--root` takes precedence over `CODEX_FARM_ROOT` for pipeline discovery. | `tests/test_cli_integration_contracts.py` (`test_pipelines_list_root_override_wins_over_env`) |
| `models list --json` returns stable model-picker rows for callers (`slug`, `display_name`, `description`, optional `supported_reasoning_efforts`). | `tests/test_cli_integration_contracts.py` (`test_models_list_json_contract`) |
| `process --json` stdout payload keeps stable keys (`run_id`, `pipeline_id`, `status`, `counts`, `input_dir`, `output_dir`, `farm_root`, `workspace_root`, `codex_model`, `codex_reasoning_effort`, `output_schema_path`, `worker_exit_codes`, `exit_code`). | `tests/test_cli_integration_contracts.py` (`test_process_json_stdout_contract_and_workspace_root`) |
| `run create --json` and `process --json` include additive `incremental` summary objects with stable fallback counters. | `tests/test_cli_integration_contracts.py` (`test_run_create_json_contract`, `test_process_json_stdout_contract_and_workspace_root`) |
| `run create --incremental-from <id>` fails clearly when source run is missing/incompatible. | `tests/test_cli_integration_contracts.py` (`test_run_create_incremental_from_missing_run_is_cli_error`) |
| Planning-time incremental reuse skips Codex calls for unchanged reruns and only executes changed inputs. | `tests/test_process_smoke.py` (`test_process_incremental_reuses_unchanged_inputs`) |
| `--workspace-root` overrides pipeline `codex_cd_mode` for all processed tasks. | `tests/test_cli_integration_contracts.py` (`test_process_json_stdout_contract_and_workspace_root`) |
| Run-based planning commands persist `runs.config_json.frozen_assets` and create the referenced manifest file under `<data_dir>/run_assets/<run_id>/`. | `tests/test_cli_integration_contracts.py` (`test_run_create_persists_model_override_in_run_config`, `test_process_json_stdout_contract_and_workspace_root`, `test_go_heads_up_runs_post_run_learning`) |
| Worker execution uses frozen prompt/schema/pipeline settings for snapshot-bearing runs, rejects corrupt snapshots without live fallback, and keeps older non-snapshot runs on live-pack behavior. | `tests/test_worker.py` (frozen-assets tests), `tests/test_fake_codex_pipeline_pack_demo.py` (`test_run_create_freezes_prompt_before_worker_execution`) |
| `run create --json` and `run status --json` include consistent identifiers and counts. | `tests/test_cli_integration_contracts.py` (`test_run_create_json_contract`) |
| `--model` override persists as run config `codex_model` for worker execution. | `tests/test_cli_integration_contracts.py` (`test_run_create_persists_model_override_in_run_config`), `tests/test_worker.py` (`test_worker_loop_processes_task_with_mocked_codex`) |
| effort override aliases persist as run config `codex_reasoning_effort` for worker execution. | `tests/test_cli_integration_contracts.py` (`test_run_create_persists_model_override_in_run_config`), `tests/test_worker.py` (`test_worker_loop_processes_task_with_mocked_codex`) |
| `--output-schema` override persists as run config `output_schema_path_override` and drives worker validation. | `tests/test_cli_integration_contracts.py` (`test_run_create_persists_model_override_in_run_config`), `tests/test_worker.py` (`test_worker_loop_uses_output_schema_override_from_run_config`) |
| `run tasks --json --status done` filters correctly and returns deterministic per-task rows. | `tests/test_cli_integration_contracts.py` (`test_run_errors_and_run_tasks_json`) |
| `run tasks --json` includes reuse metadata (`reused`, `reused_from_run_id`, `reused_from_task_id`) without breaking existing keys. | `tests/test_db.py` (`test_insert_planned_tasks_for_run_supports_reuse_metadata`), `tests/test_cli_integration_contracts.py` (`test_run_errors_and_run_tasks_json`) |
| `run errors --json` returns terminal error rows with required metadata fields. | `tests/test_cli_integration_contracts.py`, `tests/test_fake_codex_pipeline_pack_demo.py` |
| `run forensics --json` returns stable bundle-index rows for failed attempts without mutating `run errors --json` shape. | `tests/test_cli_integration_contracts.py` (`test_run_forensics_json_contract`) |
| `lint --json` keeps stdout parseable and returns stable findings/count fields for pack and schema targets. | `tests/test_cli_integration_contracts.py` (`test_lint_json_contract_clean_pack`, `test_lint_json_contract_broken_pack_reports_multiple_findings`, `test_lint_schema_json_contract_and_strict_exit`) |
| Explicit near-miss `lint --root` directories return diagnostics instead of argument parsing failure. | `tests/test_cli_integration_contracts.py` (`test_lint_reports_missing_sentinels_for_explicit_near_miss_root`) |
| External pack (`examples/pipeline_pack_demo`) + `codex_cd_mode: input_dir` behaves correctly in both `one` and `process`. | `tests/test_fake_codex_pipeline_pack_demo.py` |
| Schema failure propagates to `process` non-zero exit and detailed `run errors --json` rows. | `tests/test_fake_codex_pipeline_pack_demo.py` (`test_run_errors_json_on_schema_failure`) |
| Schema failure keeps normal output dir clean while preserving rejected payload evidence in a forensics bundle. | `tests/test_fake_codex_pipeline_pack_demo.py` (`test_process_schema_failure_preserves_forensics_bundle`) |
| Caller-provided `--output-schema` can force validation failure and retry/error behavior without editing pipeline assets. | `tests/test_fake_codex_pipeline_pack_demo.py` (`test_process_uses_output_schema_override_for_validation`) |
| `process` multi-worker orchestration remains functional with deterministic fake Codex output. | `tests/test_process_smoke.py` |
| `one` prints a `Forensics bundle: ...` line on failure when capture succeeds. | `tests/test_cli_integration_contracts.py` (`test_one_reports_forensics_bundle_on_failure`) |

## Fixture strategy

Two fake-Codex approaches are used on purpose:

1. Monkeypatch worker execution (`monkeypatch.setattr("codex_farm.worker.run_codex_exec", ...)`):
   - Fast and deterministic.
   - Best for asserting CLI payload shape and queue outcomes.
2. Fake `codex` binary on `PATH`:
   - Closer to real subprocess wiring.
   - Validates that `codex_exec` passes the expected `--cd`, prompt content, and output path arguments.

Use monkeypatch for orchestration assertions, fake binary for subprocess-argument assertions.

## Fixture inventory

- `tests/test_cli_integration_contracts.py::_write_pipeline_pack`
  - Creates temporary `pipelines/`, `prompts/`, and `schemas/` assets for contract tests.
- `tests/test_cli_integration_contracts.py::_write_heads_up_assets`
  - Creates optional Heads Up assets so lint-clean test packs avoid warning-only noise.
- `tests/test_fake_codex_pipeline_pack_demo.py::_write_fake_codex`
  - Writes an executable `codex` script that emits deterministic JSON with `ok`, `cd`, `input_path`.
- `tests/test_fake_codex_pipeline_pack_demo.py::_write_schema_failure_pack`
  - Creates a pack whose schema requires `must_be_present`, forcing predictable schema-validation failure.
- `examples/pipeline_pack_demo/`
  - Persistent external-pack fixture used to validate `--root` behavior and `codex_cd_mode: input_dir`.

## Running this chunk

Run only the integration-contract suites:

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/test_cli_integration_contracts.py tests/test_fake_codex_pipeline_pack_demo.py tests/test_process_smoke.py
```

## Update checklist for future AI coders

- If you change JSON payload fields for `process`, `run create`, `run status`, `run tasks`, or `run errors`, update these tests first.
- If you change `run forensics --json` payload fields, update contract tests and external reference docs together.
- If you change model override semantics, update both CLI integration tests and worker tests that assert `codex_model` persistence.
- If you change effort override aliases/normalization, update both CLI integration tests and worker tests that assert `codex_reasoning_effort` persistence.
- If you change schema override semantics, update both CLI integration tests and worker tests that assert `output_schema_path_override` persistence/use.
- If you change lint finding payload shape, update lint contract tests in `tests/test_cli_integration_contracts.py` and unit behavior in `tests/test_pack_lint.py`.
- If you change model-picker discovery shape for callers, update `test_models_list_json_contract` and unit coverage in `tests/test_model_catalog.py`.
- If you change `codex_cd_mode` logic or `--workspace-root` precedence, update both fake-binary and monkeypatch integration tests.
- If you change schema failure messaging, preserve the `"Schema validation failed"` signal in terminal error rows unless intentionally changing the contract.
- Keep `--json` outputs parseable from stdout. Progress/status logs belong on stderr when JSON mode is enabled.
- Add new integration tests with deterministic fixtures only; avoid network or live-model dependencies.

## Fast triage hints

- `JSONDecodeError` while parsing CLI output usually means non-JSON text leaked to stdout in `--json` mode.
- `cd` mismatch failures usually mean `codex_cd_mode` or `workspace_root` resolution changed.
- Missing keys in `run errors --json` usually mean the DB select/query contract was changed.
- Empty `run forensics --json` output during known failures usually means capture ordering regressed or `task_forensics` insertion failed.
- Lint integration parse failures usually mean extra text leaked to stdout in `lint --json` mode.
- `process` exits 0 with expected error cases usually means retry/terminal-error boundary shifted in worker logic.

## Related docs

- `docs/01-cli-and-command-contracts/01-cli-and-command-contracts_readme.md`
- `docs/03-run-planning-and-queue-state/03-run-planning-and-queue-state_readme.md`
- `docs/04-worker-execution-and-retries/04-worker-execution-and-retries_readme.md`
- `docs/05-codex-exec-and-schema-gate/05-codex-exec-and-schema-gate_readme.md`
- `docs/08-external-program-reference/failure-forensics-contracts.md`
- `docs/how-codex-farm-works.md` (integration test strategy overview)

## Task doc merges from `docs/tasks`

Historical task docs merged into this chunk to preserve integration-evidence context:

- `Initial-Build.md` (`2026-02-20_12.45.00` revision note):
  - recorded first end-to-end acceptance baseline (`pytest` green and live `one/process/go` validations) that still anchors integration expectations.
- `Plan-for-recipe-correction.md` (`2026-02-22_12.36.41`):
  - introduced deterministic fake-Codex external-pack integration tests for `one`, `process --json`, and schema-failure inspection via `run errors --json`.
  - established that `process --json` contract and root/workspace behavior must be asserted at CLI integration level, not only unit level.
- `Plan-for-knowledge-correction.md` (`2026-02-22_13.07.23`):
  - expanded fake-Codex coverage to lock `codex_cd_mode` behavior and refined error-row contract expectations.
  - recorded targeted acceptance bundles spanning worker, pipeline spec, DB, and CLI integration tests to prevent seam drift.
- `2026-02-28_02.47.41`, `2026-02-28_02.55.22`, `2026-02-28_04.16.54` task specs:
  - provided explicit fail-before evidence patterns for model/effort/menu contracts; retained in integration suites as regression anchors.

## Merged discoveries from `docs/understandings`

- `2026-02-22_13.22.52`: The docs and code boundaries were intentionally split into six seams (CLI, root/assets, queue state, worker retries, codex/schema gate, integration) to match the real call path and localize edits.
- `2026-02-22_13.22.52`: Most changes should touch one chunk plus one neighbor seam; broad cross-chunk changes are a smell that contracts are being mixed.
- `2026-02-22_14.34.17`: Integration strategy intentionally keeps monkeypatched `run_codex_exec` tests for fast contract/orchestration checks.
- `2026-02-22_14.34.17`: Integration strategy intentionally also keeps fake-`codex` binary tests for subprocess seam checks (`--cd`, prompt substitution, output handoff).
- `2026-02-22_14.34.17`: Using only one fixture style leaves blind spots (monkeypatch misses subprocess wiring regressions; fake-binary-only suites are slower and less surgical for JSON contract checks).
- `2026-02-28_02.47.41`: Model override coverage spans two seams by design: CLI verifies `--model` contract/persistence, worker verifies persisted `codex_model` is actually used at execution time.
- `2026-02-28_02.55.22`: Effort override coverage follows the same two-seam rule: CLI verifies alias contract/persistence, worker verifies persisted `codex_reasoning_effort` is actually used at execution time.
- `2026-02-28_04.16.54`: Caller-facing model-picker integration is anchored on `models list --json`; command-shape assertions stay in CLI integration tests while cache parsing/normalization stays in `tests/test_model_catalog.py`.
- `2026-02-28_09.21.54`: Output-verification regressions should be treated as a cross-suite contract and validated together in `test_codex_exec.py`, `test_worker.py`, `test_fake_codex_pipeline_pack_demo.py`, `test_cli_integration_contracts.py`, and `test_recipeimport_schemas.py`.
- `2026-02-28_09.31.02`: Schema override coverage follows the same two-seam rule: CLI verifies `--output-schema` contract/persistence, worker verifies persisted `output_schema_path_override` is actually used at execution time.
- `2026-02-28_12.34.52`: lifecycle integration coverage now includes pause/resume/cancel/retry-errors command contracts, `control_state`/`counts.canceled` JSON fields, and worker stale-lease safety behavior.
- `2026-02-28_18.46.00`: failure-forensics coverage spans CLI (`run forensics --json`, `one` failure stderr line), DB index rows, worker capture ordering, and end-to-end schema-failure evidence retention.

Known anti-pattern:

- Replacing dual fixture strategy with a single "simpler" approach has repeatedly reduced coverage on important seams.
