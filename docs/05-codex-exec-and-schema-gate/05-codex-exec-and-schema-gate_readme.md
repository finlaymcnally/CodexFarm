---
summary: "Codex subprocess contract, output acceptance rules, and the final local schema gate."
read_when:
  - "When changing codex exec flags, timeout behavior, or output acceptance/validation rules"
---

# Codex Exec And Schema Gate (Chunk 05)

This chunk is the "truth gate" between model output and durable task success.

It answers one question: "Do we accept this output as valid?"

It owns:

- How `codex exec` is invoked.
- How output files are written atomically.
- What counts as a usable Codex response.
- The final local JSON Schema validation.
- The health check used by `doctor`.

If you change behavior here, you are changing success/failure semantics across both:

- `one` command (single-shot, no retries).
- worker/process flow (retry/error policy handled in Chunk 04).

## Primary code

- `src/codex_farm/codex_exec.py`
- `src/codex_farm/schema_utils.py`
- `src/codex_farm/doctor.py`

## Main call sites

- `src/codex_farm/cli.py` (`doctor`, `one`)
- `src/codex_farm/worker.py` (batch task execution path)

## Boundary contract at a glance

Input to this chunk:

- A rendered prompt string.
- Runtime settings (`model`, `sandbox`, `ask_for_approval`, `web_search`, `reasoning_effort`, timeout) resolved by caller.
- Optional subprocess env overrides resolved by caller (currently used for `CODEX_HOME` isolation).
- Model is pipeline default unless caller applies a run/command override.
- Reasoning effort is pipeline default unless caller applies a run/command override.
- Resolved paths (`cd_dir`, `output_schema`, `output_path`).

Output from this chunk:

- `CodexExecResult` (`ok`, `exit_code`, `stderr_tail`, `stdout_tail`) or timeout exception.
- `is_rate_limit_message(...)` helper for classifying stderr/stdout tails that indicate API rate limiting (`429`/rate-limit text).
- `is_auth_failure_message(...)` helper for classifying stderr/stdout tails that indicate auth/session failures (`401/403`, login-required text, websocket auth denial).
- `extract_retry_after_seconds(...)` helper for parsing explicit provider retry hints used by adaptive worker cooldown policy.
- Parsed JSON payload on schema success.
- `SchemaValidationError` on JSON/schema failure.

Downstream behavior:

- `cli.one` exits non-zero on timeout, codex failure, or schema failure.
- `worker_loop` retries or marks terminal error based on attempt count.

## 1) Codex subprocess contract (`codex_exec.py`)

`run_codex_exec(...)` constructs this command shape:

```text
codex --ask-for-approval <mode> exec \
  --cd <absolute dir> \
  --skip-git-repo-check \
  --model <resolved model> \
  --sandbox <pipeline sandbox> \
  --config web_search=<pipeline web_search> \
  [--config model_reasoning_effort="<resolved effort>"] \
  --output-schema <absolute schema path> \
  --output-last-message <temp output path> \
  --json \
  <prompt>
```

Important details:

- `--ask-for-approval` is passed as a global Codex flag before `exec`.
- `--skip-git-repo-check` is always enabled to support non-git working dirs.
- callers may inject `env_overrides`; recipe isolation uses this for `CODEX_HOME`.
- Output is directed to a temp file in the final output directory.
- On accepted output, `os.replace(temp, final)` gives atomic replace semantics.
- Both stderr and stdout passthrough tails (up to 20 lines each) are returned to callers for failure diagnostics.
- A usage CSV row is appended per Codex call (`codex_exec_activity.csv`) with timing, token usage (from `turn.completed.usage`), prompt text, exit data, optional run/task context, additive `execution_context` / `codex_home_path` fields, and rollout reasoning metadata (`rollout_reasoning_*`).
- Callers can pass `trace_output_path` so each invocation writes a JSON trace artifact with raw Codex JSON events, passthrough lines, action/reasoning event slices, normalized `captured_reasoning`, and execution-context metadata.
- Trace classification is intentionally strict: CodexFarm only counts explicit top-level `event.type` values and explicit nested `item.type` values on wrapped `item.completed` events. Payload text inside ordinary `agent_message` outputs must not count as action or reasoning evidence.
- After stdout parsing, `run_codex_exec(...)` also correlates `thread.started.thread_id` against local rollout files under `CODEX_HOME/sessions/.../rollout-*.jsonl`. That best-effort harvest is observability only; it must never change task success/failure semantics.
- Telemetry rows also include parsed event types/counts, output payload fingerprint/preview, normalized failure categories, rollout reasoning classification, and structured pass-forward context (retry error carry-forward and applied Heads Up tips) for caller-side prompt tuning.

## 2) Output acceptance rules (`codex_exec.py`)

`run_codex_exec(...)` intentionally does not treat `returncode != 0` as always fatal.

Current decision logic:

1. Timeout: raise `CodexExecTimeoutError("codex exec timed out after <N>s")`, attach stdout/stderr tails, and remove temp file if present.
2. Non-zero exit and no non-empty output payload: return `CodexExecResult(ok=False, exit_code=<code>, stderr_tail=<tail>)`.
3. Any exit code, but temp output exists and is non-empty: accept payload, atomically move temp file to final path, return `CodexExecResult(ok=True, exit_code=<code>, stderr_tail=<tail>)`.
4. Exit 0 with no non-empty output payload: return `ok=False` with message `codex exec exited 0 but produced no output file`.

Why this exists:

- Some Codex runs can emit usable JSON while still ending non-zero due warnings/noise.
- Final correctness is delegated to local schema validation, not subprocess exit code alone.

## 3) Final schema gate (`schema_utils.py`)

`validate_json_file_against_schema(json_path, schema_path)` is the final authority.

Flow:

1. Parse output JSON (`load_json_file`).
2. Parse schema JSON (`load_json_file`).
3. Validate with `jsonschema.Draft202012Validator`.
4. If errors exist, raise `SchemaValidationError` for the first sorted error.

Error messages are intentionally concise:

- Invalid JSON:
  `Invalid JSON at <path>: <json decode detail>`
- Schema mismatch:
  `Schema validation failed at <json.path.or.<root>>: <message>`

The function returns parsed payload on success.

## 4) Doctor behavior (`doctor.py`)

`run_doctor_checks()` returns `(checks, all_ok)` where each check is:

- `CheckResult(name, ok, detail)`

Checks:

1. Python version >= 3.11.
2. `codex` executable exists and `codex --version` succeeds.
3. Login status check: `codex login status` must indicate logged-in session.
4. Non-interactive smoke call: `codex --ask-for-approval never exec --skip-git-repo-check --sandbox read-only --model <resolved precheck model> [--config model_reasoning_effort="<resolved effort>"] "Reply with exactly: OK"`.
   CLI execution prechecks now rely on this same smoke path, not login status alone, because websocket auth failures can still happen after a superficially "logged in" status.

Precheck model/effort source:

- `doctor` and unscoped `worker` use the generic default smoke model because they are not tied to one resolved run config.
- `one`, `process`, and `go` pass their already-resolved execution model, reasoning effort, and `CODEX_HOME` override into the smoke call, so preflight failures reflect the same Codex settings that real execution will use.
- `worker --run-id <id>` reads that run first and prechecks the persisted `codex_home_path` before leasing.

Smoke success rule is intentionally tolerant:

- Success if return code is 0, OR stdout contains an exact line `OK`.

This avoids false failures when Codex prints expected output but exits non-zero because of local warnings.
When login-status check fails, smoke is skipped to keep failure diagnostics focused on auth/session setup first.

## 5) Integration with CLI and worker

`cli.one` path:

- Calls `run_codex_exec`.
- If timeout or `result.ok == False`: exits with error.
- If timeout/codex/schema failure occurs: captures best-effort forensics bundle, then exits with error.
- If schema validation fails: deletes output file after capture, then exits with error.

`worker_loop` path:

- Calls `run_codex_exec`.
- Resolves model from run config `codex_model` when present, else pipeline `codex_model`.
- Resolves effort from run config `codex_reasoning_effort` when present, else pipeline `codex_reasoning_effort`.
- Resolves output schema from run config `output_schema_path_override` when present, else pipeline `output_schema_path`.
- For snapshot-bearing runs, worker instead uses frozen execution files from run-assets snapshot (`prompt.template.txt`, `output.schema.json`, and frozen pipeline settings).
- Converts `result.ok == False` into `RuntimeError`.
- Runs local schema validation.
- On failure (`timeout`, `SchemaValidationError`, `RuntimeError`):
  - captures best-effort forensics first (when caller supplies context)
  - deletes staged output path
  - requeues or marks terminal error (Chunk 04 owns retry policy)

Telemetry schema identity rule:

- `run_codex_exec(...)` now accepts both execution schema path and optional logical schema path.
- Worker passes frozen schema file for execution/validation while telemetry `output_schema_path` preserves logical source schema identity when available.
- `run_codex_exec(...)` also accepts optional `trace_output_path`; trace writes are best-effort and never fail task execution.
- Trace heuristics must keep recognizing nested wrapper events such as `item.completed`; otherwise downstream callers like recipeimport see trace files with `reasoning_event_count=0` even though reasoning text exists in the raw event stream.
- When a rollout file exists, traces now also persist a normalized `captured_reasoning` block that distinguishes stdout reasoning, rollout summaries, empty-summary encrypted rollout metadata, missing rollouts, and missing thread ids.

## 5.1) Verification visibility surfaces

When acceptance behavior changes, keep these caller-facing inspection surfaces aligned:

- `run tasks --json`
- `run errors --json`
- `run forensics --json`
- `codex_exec_activity.csv`
- `.codex-farm-traces/.../*.trace.json` (worker + one) and `heads_up/traces/*.trace.json` (Heads Up distiller)

Together they are the practical debugging contract for why outputs were accepted, retried, or marked terminal.
If no explicit reasoning event exists in those artifacts, inspect the trace artifact's `captured_reasoning` block before concluding that no thinking was available; it now tells you whether rollout reasoning existed but lacked human-readable summary text.

## 6) Known non-obvious rules

- Do not remove local schema validation because Codex `--output-schema` is not enough.
- Keep `--ask-for-approval` at Codex global scope (`codex ... exec`), not `codex exec ...`.
- Keep `--skip-git-repo-check` in worker and doctor calls.
- A non-zero Codex exit can still produce an accepted payload.
- Task success in worker mode requires both: accepted payload file and local schema pass.
- Timeout cleanup removes temp output before caller retry logic; timeout raw payload retention is therefore metadata/tail-based unless codex-exec timeout behavior changes.
- Auth/login failure detection is text-based (`is_auth_failure_message(...)`) and is consumed by callers (`worker`, `one`) for remediation messaging and terminal/non-retry policy.
- Run-level model overrides are resolved in CLI/worker before this chunk; `run_codex_exec` should keep treating `model` as the final resolved value.
- Run-level effort overrides are resolved in CLI/worker before this chunk; `run_codex_exec` should keep treating `reasoning_effort` as the final resolved value.
- Run-level schema overrides are resolved in CLI/worker before this chunk; `run_codex_exec` should keep treating `output_schema` as the final resolved path.
- Current local probe result on `gpt-5.3-codex-spark`: forcing `--config model_reasoning_summary=\"concise\"` fails with `unsupported_parameter`, so CodexFarm should not add that flag blindly for this model.
- `recipe.schemaorg.normalize.v1` output contract requires `recipeInstructions` as an array of `{"@type":"HowToStep","text":...}` objects.
- `schemas/recipeimport_intermediate_fullshape_v1.schema.json` and `schemas/recipeimport_final_fullshape_v1.schema.json` are inbound acceptance contracts and must validate both sparse real samples and platonic full-shape fixtures under `examples/recipeimport_*`.

## 7) If you edit this chunk

Minimum checks:

1. Run tests touching worker/CLI contracts:
   - `tests/test_worker.py`
   - `tests/test_process_smoke.py`
   - `tests/test_fake_codex_pipeline_pack_demo.py`
   - `tests/test_cli_integration_contracts.py`
2. Re-verify schema examples:
   - `tests/test_recipeimport_schemas.py`
3. Re-run:
   - `codex-farm doctor`

Common regressions to watch for:

- Breaking the Codex flag order/placement.
- Returning success before atomic replace is complete.
- Marking model output "good" without local schema pass.
- Losing useful stderr context needed in task error rows.

## Task doc merges from `docs/tasks`

Historical task docs merged into this chunk to preserve codex-exec/schema-gate evidence decisions:

- `idea1-6.md` (`2026-02-28_18.46.00`):
  - added self-contained failed-attempt forensics bundles that snapshot prompt/input/schema plus runtime tails and optional rejected payload bytes.
  - locked capture ordering contract: for schema/runtime failures, capture evidence before staged/normal output cleanup so normal output directories remain clean while debugging artifacts survive.
  - preserved compatibility rule that `run forensics --json` is additive and `run errors --json` remains unchanged task-state introspection.
  - captured key limit from task history: timeout branches remain metadata/tail-based for raw payload because codex timeout cleanup currently removes temp output before capture.

## See also

- `docs/04-worker-execution-and-retries/04-worker-execution-and-retries_readme.md`
- `docs/06-integration-contracts-and-fixtures/06-integration-contracts-and-fixtures_readme.md`
- `docs/08-external-program-reference/structured-output-contracts.md`
- `docs/08-external-program-reference/failure-forensics-contracts.md`

## Merged discoveries from `docs/understandings`

Historical discoveries that now belong in this chunk:

- `2026-02-20_13.05.00`: Codex CLI in this environment requires approval policy as a global flag (`codex --ask-for-approval never exec ...`), not `codex exec --ask-for-approval ...`.
- `2026-02-20_13.05.00`: `--skip-git-repo-check` is mandatory for non-git execution contexts and must remain in runtime and doctor command shapes.
- `2026-02-20_13.05.00`: Codex can return non-zero while still producing valid `--output-last-message`; local schema validation is the final acceptance authority.
- `2026-02-20_13.05.00`: Current `--output-schema` behavior is stricter than generic JSON Schema expectations and effectively requires every `properties` key in `required`; optional fields should be modeled as nullable required keys when needed.
- `2026-02-20_13.09.19`: Recipeimport schema contracts were realigned to accept both sparse real payloads and platonic full-shape fixtures; the old over-required fullshape contract caused false failures.
- `2026-02-20_13.09.19`: `schemas/recipeimport_final_fullshape_v1.schema.json` now tracks canonical shape from `examples/recipeimport_final/recipeDraftV1.canonical.recipeimport.schema.json`.
- `2026-02-22_14.33.40`: Chunk 05 is an acceptance boundary: temp-file write + atomic promote + schema gate is intentional and should not be bypassed by subprocess exit code shortcuts.
- `2026-02-28_09.21.54`: Output verification is layered: `run_codex_exec(...)` must produce a non-empty payload, then local Draft 2020-12 validation must pass; task success requires both.
- `2026-02-28_09.21.54`: Non-zero Codex exits remain conditionally acceptable only when payload exists and local schema validation passes.
- `2026-02-28_09.21.54`: Regression coverage for this stack spans `test_codex_exec.py`, `test_worker.py`, `test_fake_codex_pipeline_pack_demo.py`, `test_cli_integration_contracts.py`, and `test_recipeimport_schemas.py`.
- `2026-02-28_09.31.02`: Validation schema source is run-config aware: optional `output_schema_path_override` in run config can override pipeline schema path, and both Codex `--output-schema` and local Draft202012 validation use the resolved path.
- `2026-02-28_14.50.39`: `run_codex_exec` telemetry now carries structured caller-tuning signals (retry carry-forward text, applied Heads Up hints, failure category/rate-limit flags, and output previews) so external programs can diagnose repeated failure modes without parsing prompt bodies.
- `2026-02-28_18.46.00`: `CodexExecResult`/timeout errors expose both stdout and stderr tails so worker/CLI failure forensics can capture the same operator-visible diagnostics as telemetry without scraping CSV.

Known fragile areas:

- Treating Codex exit code alone as truth breaks valid-output acceptance in real environments with noisy local shutdown/telemetry errors.
- Over-tightening recipeimport fullshape requirements has already rejected production-like sparse payloads.

## Merged understanding notes (`docs/understandings`)

### 2026-03-02_07.03.57 - `invalid_json_schema` treated as explicit schema failure
- Both direct `cli.py` execution path and worker path now parse stderr/stdout tails for `invalid_json_schema` and record a dedicated `failure_category`.
- This keeps forensics actionable instead of collapsing into generic warnings like `no last agent message`.
- In worker, `invalid_json_schema` follows terminal behavior so retries are not wasted on hard schema-violation failures.
