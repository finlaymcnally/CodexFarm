# Make codex-farm a clean “pipeline executor” for external knowledge-extraction passes

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This repository includes `PLANS.md` at the repository root. This document must be maintained in accordance with `PLANS.md`. :contentReference[oaicite:0]{index=0}

## Purpose / Big Picture

After this change, codex-farm can be used as a reusable, “domain-agnostic” batch executor from other projects (like the recipe/knowledge-extraction pipeline described in the discussion) without copying codex-farm internals or hard-coding recipe logic into codex-farm. :contentReference[oaicite:1]{index=1}

Concretely, an external project can:

1. Ship its own pipeline pack (a folder containing `pipelines/`, `prompts/`, `schemas/`).
2. Invoke codex-farm with an explicit `--root` flag pointing at that pipeline pack (instead of relying on `CODEX_FARM_ROOT` environment variables).
3. Run `process` / `one` / `worker` against job-bundle files produced by the external project, and reliably receive validated JSON outputs (or clear, machine-readable error listings).
4. Configure where Codex runs (`codex exec --cd …`) in a pipeline-driven way so that Codex always has access to the input job files, even when pipeline assets live elsewhere.

You can see it working by running a deterministic end-to-end demo test included in this plan: it uses a small “fake codex” executable placed on `PATH` that writes valid JSON to `--output-last-message`. This proves codex-farm’s orchestration, root selection, prompt templating, and schema validation without requiring any real LLM calls.

## Progress

- [ ] (2026-02-22 00:00Z) Add `--root` CLI option to all pipeline-loading commands and update root resolution to prioritize explicit flag over `CODEX_FARM_ROOT` and auto-discovery.
- [ ] (2026-02-22 00:00Z) Add pipeline-driven Codex working-directory selection (`codex_cd_mode`) and thread it through pipeline spec loading → worker/one execution → Codex subprocess call.
- [ ] (2026-02-22 00:00Z) Make `process --json` output stable, machine-readable run summaries including `run_id` and counts, and add `run errors --json` to list failing tasks without manual SQLite inspection.
- [ ] (2026-02-22 00:00Z) Add deterministic integration test(s) using a fake `codex` executable + a tiny demo pipeline pack to prove `--root` and `codex_cd_mode` behavior end-to-end.
- [ ] (2026-02-22 00:00Z) Update docs to describe pipeline packs, `--root`, `codex_cd_mode`, and error inspection workflow.

## Surprises & Discoveries

- (empty; update during implementation with concrete evidence snippets, especially if any Codex CLI constraints or sandbox behaviors differ from expectations)

## Decision Log

- Decision: Keep codex-farm domain-agnostic; add only generic integration features (`--root`, `codex_cd_mode`, JSON run summaries, and error listing). Do not add recipe/knowledge-specific transforms into codex-farm code.
  Rationale: The discussion explicitly frames codex-farm as a “batch executor with strong I/O contracts” and suggests keeping domain ETL and meaning in the importing project. This preserves reuse across unrelated personal projects. :contentReference[oaicite:2]{index=2}
  Date/Author: 2026-02-22 / GPT-5.2 Pro

- Decision: Add a deterministic “fake codex” integration test to prove the CLI orchestration without requiring real LLM access.
  Rationale: ExecPlans must result in demonstrably working behavior. The codex-farm design shells out to an installed `codex` binary; a fake replacement on `PATH` is the cleanest way to validate end-to-end behavior in CI and for novices. :contentReference[oaicite:3]{index=3}
  Date/Author: 2026-02-22 / GPT-5.2 Pro

- Decision: Introduce pipeline-driven `codex_cd_mode` with a conservative default (current behavior) and an “input dir” option for external pipeline packs.
  Rationale: codex-farm currently uses a single repo-root concept for both pipeline assets and `codex exec --cd`. When pipeline assets are external (pipeline pack) and job bundles are elsewhere, sandboxed Codex may not be able to read inputs unless `--cd` is chosen appropriately. Making this data-driven keeps codex-farm generic and avoids forcing external projects into awkward directory layouts. 
  Date/Author: 2026-02-22 / GPT-5.2 Pro

## Outcomes & Retrospective

- (empty; at completion, summarize what was achieved, what remains, and what you would do differently)

## Context and Orientation

codex-farm is a local CLI orchestrator around the `codex exec` command. It does not run a model itself; it shells out to the `codex` CLI, queues file-level tasks in SQLite, retries on failure, and enforces that each task’s output is valid JSON matching a JSON Schema. 

Key concepts (define these now so the plan is self-contained):

- Pipeline: A named operation identified by `pipeline_id` (for example, `recipe.chunking.v1`). A pipeline is configured by a JSON file under `pipelines/` that points at a prompt template (`prompts/*.txt`) and an output schema (`schemas/*.schema.json`) plus Codex runtime flags (model, sandbox, timeout, etc.). 
- Prompt template: Plain text with literal placeholder `{{INPUT_PATH}}` substituted by codex-farm to the absolute path of the task’s input file. This is the only supported placeholder today. :contentReference[oaicite:7]{index=7}
- Pipeline pack: A directory that contains `pipelines/`, `prompts/`, and `schemas/`. codex-farm can treat such a directory as its “asset root” and load pipelines from there. Today this is controlled by `CODEX_FARM_ROOT` or auto-discovery. This plan adds an explicit `--root` flag to make external-project integration simple and robust. 
- Run: A batch invocation created by `process` (or `run create`). It creates a row in `runs` and one row per input file in `tasks`. Workers lease tasks, run Codex, validate output, and mark tasks done/error. 
- Job bundle: The input file per task (typically JSON) produced by an external project. For the recipe/knowledge extraction pipeline, job bundles contain the text blocks/context the model needs, plus provenance anchors. codex-farm should not interpret job-bundle meaning; it only feeds the file path to the prompt and validates the output. :contentReference[oaicite:10]{index=10}

Relevant code modules and responsibilities (paths are repo-relative):

- `src/codex_farm/cli.py`: CLI entrypoint and command wiring (`doctor`, `one`, `process`, `run create`, etc.). :contentReference[oaicite:11]{index=11}
- `src/codex_farm/paths.py`: Resolves the “repo root” / asset root by sentinel directories and `CODEX_FARM_ROOT`. This plan extends it to support an explicit CLI override cleanly. 
- `src/codex_farm/pipeline_spec.py`: Loads/validates pipeline JSON and renders prompts (substituting `{{INPUT_PATH}}`). This plan adds a new pipeline field controlling Codex working directory. 
- `src/codex_farm/codex_exec.py`: Builds and runs the `codex exec` subprocess command. It currently passes `--cd <repo_root>`. This plan makes that “cd directory” configurable. 
- `src/codex_farm/db.py`: SQLite schema and task/run queries. This plan adds a “list failing tasks” query used by a new CLI command. 
- `src/codex_farm/worker.py`: Worker loop that leases tasks and executes them. This plan threads `codex_cd_mode` through here. 

## Plan of Work

### Milestone 1: Make pipeline packs first-class via `--root`

At the end of this milestone, every codex-farm command that needs pipeline assets can be pointed at an arbitrary pipeline pack directory by passing `--root /path/to/pack`. This should work without setting environment variables. The behavior must be deterministic and test-covered.

Implementation details:

1. In `src/codex_farm/paths.py`, introduce a single “source of truth” resolver for the asset root.

   - Add a function with a stable signature (choose names that match the existing style in the file):

         def resolve_asset_root(explicit_root: Path | None) -> Path:
             """
             Returns the directory considered the pipeline asset root.
             Precedence:
               1) explicit_root (from CLI flag)
               2) CODEX_FARM_ROOT environment variable
               3) auto-discovery (search upward from cwd, then module path)
             Validation:
               - The directory must exist.
               - It must contain the sentinel subdirectories: pipelines/, prompts/, schemas/
             Raises FileNotFoundError with a helpful message on failure.
             """

   - If the repository already has an equivalent function, refactor to accept `explicit_root` and keep all existing behavior as the default when `explicit_root` is not provided.

2. In `src/codex_farm/cli.py`, add a `--root` option (a filesystem path) for commands that load pipeline specs or scaffold pipeline assets.

   Commands that should accept `--root`:

   - `pipelines list`
   - `pipelines new`
   - `one`
   - `run create`
   - `process`
   - `worker`
   - `go`

   `run status` does not strictly need pipeline assets, but it is fine to accept `--root` for consistency; do not make it required.

   Wiring rules:

   - The `--root` option must be optional.
   - When present, it must override `CODEX_FARM_ROOT`.
   - Avoid side effects that leak across tests; prefer passing the root down into the internal “load pipelines” functions rather than mutating global environment variables unless the existing code already relies heavily on env vars. If you choose to set `os.environ["CODEX_FARM_ROOT"]` as a minimal-change approach, do it only within the CLI process, and ensure tests isolate environment state via fixtures.

3. In `src/codex_farm/pipeline_spec.py`, update any helpers that locate prompt/schema files to accept the resolved asset root path rather than assuming it is the same as the code repository root.

   - The key invariant: prompt and schema paths in pipeline JSON remain “root-relative,” but “root” now means “asset root,” which may be a pipeline pack directory.

4. Tests:

   - Add tests in the existing test suite (likely `tests/test_paths.py` or similar) that verify:
     - `--root` with a valid pack is accepted.
     - An invalid root (missing sentinel dirs) errors with a clear message.
     - When both `--root` and `CODEX_FARM_ROOT` are present, `--root` wins.

   - Add one CLI-level test if the repo already has CLI tests, otherwise keep this as unit tests around `resolve_asset_root()` and pipeline loading.

Acceptance for Milestone 1:

- Running `codex-farm pipelines list --root <path-to-pack>` lists pipelines from the pack (not from the codex-farm repo).
- Running `codex-farm pipelines new --root <path-to-pack> --pipeline-id demo.echo.v1` creates the three scaffold files under that pack.
- Tests covering root resolution pass.

### Milestone 2: Make Codex working directory pipeline-configurable (`codex_cd_mode`)

At the end of this milestone, pipelines can specify where Codex runs (the `--cd` argument passed to `codex exec`) using a new pipeline JSON field. This makes external pipeline packs safe even if job bundles are outside the pack directory.

Implementation details:

1. In `src/codex_farm/pipeline_spec.py`:

   - Extend the Pydantic model (`PipelineSpecModel`) and the runtime dataclass (`PipelineSpec`) with a new field:

         codex_cd_mode: Literal["asset_root", "input_dir", "input_file_dir"] = "asset_root"

     Definitions:

     - `asset_root`: current behavior (Codex runs with `--cd` set to the resolved asset root).
     - `input_dir`: Codex runs with `--cd` set to the run’s input directory (the directory passed to `process --in ...` / stored on the run row).
     - `input_file_dir`: Codex runs with `--cd` set to the specific task input file’s parent directory (useful if tasks are scattered and you want the most local sandbox root).

   - Keep the default `"asset_root"` so existing pipelines behave identically unless they opt in.

   - Update any pipeline scaffolding logic (`pipelines new`) so the generated pipeline JSON either:
     - omits this field (relying on default), or
     - includes it explicitly as `"asset_root"` for discoverability.
     Pick one and document it. Prefer including it explicitly so users discover the feature by reading the scaffold.

2. In `src/codex_farm/worker.py` and the single-file execution path used by `one`:

   - Determine the correct `cd_dir` for each task using `pipeline.codex_cd_mode` and the run/task info:

         if pipeline.codex_cd_mode == "asset_root":
             cd_dir = asset_root
         elif pipeline.codex_cd_mode == "input_dir":
             cd_dir = Path(run.input_dir)
         else:  # "input_file_dir"
             cd_dir = Path(task.input_path).parent

   - Ensure the chosen `cd_dir` exists; if not, fail the task with a clear error (this is a configuration error, not a transient LLM error).

3. In `src/codex_farm/codex_exec.py`:

   - Rename the first parameter in `run_codex_exec(...)` to reflect meaning (it is the `--cd` directory), or keep the name but treat it consistently as “cd_dir”.

   - Ensure the subprocess command uses the chosen directory:

         codex exec --cd <cd_dir> ...

   - Maintain the existing “do not break” invariants:
     - `--ask-for-approval` remains a global flag before `exec`.
     - `--skip-git-repo-check` remains included.
     - Output is written to a temp file and atomically replaced.
     :contentReference[oaicite:17]{index=17}

4. Tests:

   - Add unit tests that verify `cd_dir` selection for each `codex_cd_mode` option, preferably by asserting the constructed subprocess argv in `run_codex_exec` (many repos do this by monkeypatching `subprocess.run`).

Acceptance for Milestone 2:

- A pipeline that sets `"codex_cd_mode": "input_dir"` results in Codex being invoked with `--cd` equal to the run input directory (verified by tests).
- All existing tests still pass with default behavior unchanged.

### Milestone 3: Make integration observable and debuggable (JSON summaries + `run errors`) and add deterministic end-to-end demo

At the end of this milestone:

- `process --json` prints a stable JSON object that external code can parse (for example, recipeimport can parse it).
- There is a new `run errors --run-id ... --json` command that prints error tasks in machine-readable form.
- There is at least one deterministic integration test that runs the actual CLI against a fake `codex` binary and a tiny demo pipeline pack, proving `--root`, prompt substitution, `codex_cd_mode`, and schema validation end-to-end.

Implementation details:

1. Stable JSON output for `process --json`:

   In `src/codex_farm/cli.py`, ensure `process --json` outputs a single JSON object to stdout on completion with fields:

   - `run_id` (string)
   - `pipeline_id` (string)
   - `input_dir` (string, absolute path)
   - `output_dir` (string, absolute path)
   - `counts` object:
     - `queued`, `running`, `done`, `error`, `total` (integers)
   - `worker_exit_codes` (array of ints)
   - `exit_code` (int; 0 on success, non-zero if any errors)

   Requirements:

   - When `--json` is specified, do not print human progress lines to stdout. If progress is still desired, print it to stderr or guard it behind a separate `--quiet/--progress` flag. Pick one behavior and test it.
   - The final JSON must be parseable even when tasks fail.

2. Add `run errors` command:

   - In `src/codex_farm/db.py`, add a helper:

         def list_error_tasks(conn: sqlite3.Connection, run_id: str) -> list[dict]:
             """
             Returns a list of task rows where status='error' for the given run_id.
             Each dict should include: task_id, input_path, rel_output_path, attempts, error, leased_by, lease_until, updated_at.
             """

     Keep the error string truncated behavior consistent with existing writes.

   - In `src/codex_farm/cli.py`, add:

     - `run errors --run-id <id> [--data-dir ...] [--json]`

     Human output: a short, readable list of failing input paths and error messages.
     JSON output: a JSON array of objects with the fields returned by `list_error_tasks()`.

3. Deterministic end-to-end demo test

   Goal: prove codex-farm’s folder-in → folder-out pipeline mechanism without real Codex.

   Add under `examples/pipeline_pack_demo/` a minimal pipeline pack containing:

   - `pipelines/demo.echo.v1.json`
   - `prompts/demo_echo_v1.txt`
   - `schemas/demo_echo_v1.schema.json`

   Define the pipeline JSON with:

   - `pipeline_id`: `"demo.echo.v1"`
   - `prompt_template_path`: `"prompts/demo_echo_v1.txt"`
   - `output_schema_path`: `"schemas/demo_echo_v1.schema.json"`
   - `codex_cd_mode`: set to `"input_dir"` (so we can test that behavior)
   - Keep model/sandbox defaults as-is, since fake codex ignores them.

   Prompt template should include a parseable marker:

       INPUT={{INPUT_PATH}}

   Output schema should require:

   - `ok`: string
   - `cd`: string
   - `input_path`: string

   Then implement a fake `codex` executable for tests:

   - In tests, create a temporary directory and write an executable file named `codex` (Python script with a shebang).
   - The fake codex should:
     - Parse argv to find:
       - `--output-last-message <path>`
       - `--cd <cd_dir>`
       - the final positional argument (prompt text)
     - Extract `input_path` from the prompt by finding the substring that starts with `INPUT=`.
     - Write JSON to the `--output-last-message` path:

           {"ok": "OK", "cd": "<cd_dir>", "input_path": "<extracted_input_path>"}

     - Exit with code 0.

   Test cases to include (at least):

   - `test_one_with_root_and_cd_mode`:
     - Creates a temp input JSON file (content irrelevant).
     - Runs `codex-farm one --root examples/pipeline_pack_demo --pipeline demo.echo.v1 --in <input> --out <output>`.
     - Asserts output JSON validates and contains:
       - `ok == "OK"`
       - `cd == <input_file.parent>` OR `cd == <input_dir>` depending on how `one` defines “input dir”.
         This is important: define “input_dir” for `one` now:
           - Decision: For `one`, treat “input_dir” as `Path(input_path).parent` (since there is no run input root).
           - Implement `one` so `codex_cd_mode="input_dir"` uses the input file’s parent directory.
     - Asserts `input_path` equals the absolute input file path.

   - `test_process_with_root_and_cd_mode`:
     - Creates a temp directory with 2 JSON input files (possibly nested).
     - Runs `codex-farm process --root examples/pipeline_pack_demo --pipeline demo.echo.v1 --in <dir> --out <outdir> --workers 2 --json`.
     - Asserts exit code 0.
     - Parses JSON summary and asserts `counts.done == 2`, `counts.error == 0`, and output dir contains corresponding output JSON files with expected keys.
     - Validates that each output JSON’s `cd` equals the run input directory (because pipeline sets `codex_cd_mode="input_dir"`).

   Notes:
   - Ensure the tests manipulate `PATH` so the fake `codex` is used, not a real one.
   - Keep tests fast and hermetic (no network calls).

4. Docs update

   Update or add documentation under `docs/` to describe:

   - What a “pipeline pack” is (sentinel dirs).
   - How to use `--root` to point codex-farm at an external project’s pipelines.
   - What `codex_cd_mode` does, and when to use `"input_dir"` (recommended for pipeline packs in other projects that write job bundles elsewhere).
   - How to diagnose failures:
     - `codex-farm process --json` for run summaries
     - `codex-farm run errors --run-id ... --json` for failing tasks

   Update `docs/how-codex-farm-works.md` and (if present in-repo) the deeper “for AI” doc as needed so they remain accurate. 

Acceptance for Milestone 3:

- `pytest` (or the repository test command) passes, including the new CLI integration tests.
- `codex-farm process --json` returns parseable JSON with run metadata and counts.
- `codex-farm run errors --json` returns an empty array for successful runs and a populated array for failing runs (create a test that forces a failure by using a deliberately-invalid schema once, then list errors).

## Concrete Steps

All commands below are run from the repository root (the directory containing `pyproject.toml` and `src/`).

1. Run the existing test suite to establish a baseline:

       python -m pytest

   If the repo uses a different test runner, prefer the one already documented in the README, but keep `pytest` as the default assumption since codex-farm is described with pytest tests in the docs. :contentReference[oaicite:19]{index=19}

2. Implement Milestone 1 (`--root`), then run:

       python -m pytest -k "paths or pipeline" -q
       codex-farm pipelines list --help

   Expected: `--root` appears in help for relevant commands, and tests pass.

3. Implement Milestone 2 (`codex_cd_mode`), then run:

       python -m pytest -k "codex_cd_mode or codex_exec or worker" -q

   Expected: tests covering argv construction pass.

4. Implement Milestone 3 (JSON summaries, `run errors`, fake codex demo), then run:

       python -m pytest -k "fake_codex or pipeline_pack_demo or run_errors or process_json" -q
       python -m pytest

   Optional manual demo (does not require real Codex, uses your shell PATH; do this only if you intentionally place the fake codex on PATH yourself):

       codex-farm process --root examples/pipeline_pack_demo --pipeline demo.echo.v1 --in examples/pipeline_pack_demo/sample_inputs --out /tmp/codex_farm_demo_out --json

   Expected: JSON printed to stdout and output files created in `/tmp/codex_farm_demo_out` mirroring the input tree.

## Validation and Acceptance

This work is accepted when all of the following are true:

1. Pipeline pack root selection works:

   - `codex-farm pipelines list --root examples/pipeline_pack_demo` succeeds and includes `demo.echo.v1`.
   - Passing an invalid root (missing `pipelines/`, `prompts/`, or `schemas/`) fails with a clear error message that mentions the missing sentinel directories.

2. Codex working directory selection works and is pipeline-driven:

   - With `codex_cd_mode="input_dir"`, `process` invokes Codex with `--cd` equal to the run input directory (proven by fake-codex output).
   - With `codex_cd_mode="asset_root"`, `--cd` equals the asset root.
   - With `codex_cd_mode="input_file_dir"`, `--cd` equals the specific input file’s directory.

3. Machine-readable status and errors are available:

   - `codex-farm process --json` prints exactly one JSON object to stdout with the schema described above.
   - `codex-farm run errors --run-id ... --json` prints a JSON array of error tasks without requiring manual SQLite inspection.

4. Deterministic tests prove behavior:

   - The new fake-codex integration tests pass on a machine without real `codex` installed.
   - Tests are hermetic (no network, no reliance on global state outside the temp dirs).

## Idempotence and Recovery

- All changes should be safe to run repeatedly.
- Tests must use temporary directories and must not depend on (or mutate) the default `./var` data directory unless they create it under a temp location.
- If a test fails because a real `codex` binary is being used, fix the test to always prepend the fake-codex directory to `PATH` within the test process.
- If you need to debug run/task state, prefer the new `run errors` command rather than opening SQLite manually. If SQLite inspection is still needed, keep it read-only and document the query in `Surprises & Discoveries`. :contentReference[oaicite:20]{index=20}

## Artifacts and Notes

Include concise evidence snippets here as you implement. Examples to add during implementation:

- Example `codex-farm process --json` output (truncated):

      {"run_id":"...","pipeline_id":"demo.echo.v1","counts":{"queued":0,"running":0,"done":2,"error":0,"total":2},"exit_code":0,...}

- Example `run errors --json` output when forcing a schema failure:

      [{"task_id":"...","input_path":"/tmp/.../bad.json","attempts":3,"error":"Schema validation failed at ...",...}]

## Interfaces and Dependencies

### New/extended pipeline spec fields

In `src/codex_farm/pipeline_spec.py`, extend the pipeline spec model to include:

    codex_cd_mode: Literal["asset_root", "input_dir", "input_file_dir"] = "asset_root"

This must be accepted in pipeline JSON (`pipelines/*.json`) and exposed in the runtime `PipelineSpec` object used by workers.

### New root-resolution function

In `src/codex_farm/paths.py`, ensure there is a single callable that resolves the asset root based on:

1) explicit CLI flag, 2) `CODEX_FARM_ROOT`, 3) auto-discovery.

All pipeline-loading code paths must use this resolver.

### CLI additions

In `src/codex_farm/cli.py`:

- Add `--root` option to relevant commands.
- Ensure `process --json` emits a stable JSON summary and does not mix progress text into stdout.
- Add `run errors` subcommand with `--json`.

### DB helper

In `src/codex_farm/db.py`:

- Add `list_error_tasks(conn, run_id) -> list[dict]` used by the CLI.

### Test-only dependency

The fake `codex` executable used in tests must be implemented as a small script written into a temp directory at runtime (preferred), or as a checked-in test helper under `tests/helpers/` that tests copy into temp and mark executable.

No new third-party runtime dependencies should be added for this plan.

## Sources

This ExecPlan is based on:

- The integration strategy discussion (three-pass knowledge/recipe extraction + codex-farm as file-driven executor). :contentReference[oaicite:21]{index=21}
- codex-farm architecture and CLI contracts. 
- ExecPlan authoring requirements. :contentReference[oaicite:23]{index=23}

Change note (required for living plans): This is the initial version of the ExecPlan written on 2026-02-22. Future edits must update all living sections and include a new change note explaining what changed and why.