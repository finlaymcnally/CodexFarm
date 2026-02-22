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
| `process --json` stdout payload keeps stable keys (`run_id`, `pipeline_id`, `status`, `counts`, `input_dir`, `output_dir`, `farm_root`, `workspace_root`, `worker_exit_codes`, `exit_code`). | `tests/test_cli_integration_contracts.py` (`test_process_json_stdout_contract_and_workspace_root`) |
| `--workspace-root` overrides pipeline `codex_cd_mode` for all processed tasks. | `tests/test_cli_integration_contracts.py` (`test_process_json_stdout_contract_and_workspace_root`) |
| `run create --json` and `run status --json` include consistent identifiers and counts. | `tests/test_cli_integration_contracts.py` (`test_run_create_json_contract`) |
| `run tasks --json --status done` filters correctly and returns deterministic per-task rows. | `tests/test_cli_integration_contracts.py` (`test_run_errors_and_run_tasks_json`) |
| `run errors --json` returns terminal error rows with required metadata fields. | `tests/test_cli_integration_contracts.py`, `tests/test_fake_codex_pipeline_pack_demo.py` |
| External pack (`examples/pipeline_pack_demo`) + `codex_cd_mode: input_dir` behaves correctly in both `one` and `process`. | `tests/test_fake_codex_pipeline_pack_demo.py` |
| Schema failure propagates to `process` non-zero exit and detailed `run errors --json` rows. | `tests/test_fake_codex_pipeline_pack_demo.py` (`test_run_errors_json_on_schema_failure`) |
| `process` multi-worker orchestration remains functional with deterministic fake Codex output. | `tests/test_process_smoke.py` |

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
- If you change `codex_cd_mode` logic or `--workspace-root` precedence, update both fake-binary and monkeypatch integration tests.
- If you change schema failure messaging, preserve the `"Schema validation failed"` signal in terminal error rows unless intentionally changing the contract.
- Keep `--json` outputs parseable from stdout. Progress/status logs belong on stderr when JSON mode is enabled.
- Add new integration tests with deterministic fixtures only; avoid network or live-model dependencies.

## Fast triage hints

- `JSONDecodeError` while parsing CLI output usually means non-JSON text leaked to stdout in `--json` mode.
- `cd` mismatch failures usually mean `codex_cd_mode` or `workspace_root` resolution changed.
- Missing keys in `run errors --json` usually mean the DB select/query contract was changed.
- `process` exits 0 with expected error cases usually means retry/terminal-error boundary shifted in worker logic.

## Related docs

- `docs/01-cli-and-command-contracts/01-cli-and-command-contracts_readme.md`
- `docs/03-run-planning-and-queue-state/03-run-planning-and-queue-state_readme.md`
- `docs/04-worker-execution-and-retries/04-worker-execution-and-retries_readme.md`
- `docs/05-codex-exec-and-schema-gate/05-codex-exec-and-schema-gate_readme.md`
- `docs/how-codex-farm-works.md` (integration test strategy overview)
