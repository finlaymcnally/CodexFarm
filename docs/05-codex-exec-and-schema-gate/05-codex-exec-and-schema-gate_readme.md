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
- Pipeline runtime settings (`model`, `sandbox`, `ask_for_approval`, `web_search`, timeout).
- Resolved paths (`cd_dir`, `output_schema`, `output_path`).

Output from this chunk:

- `CodexExecResult` (`ok`, `exit_code`, `stderr_tail`) or timeout exception.
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
  --model <pipeline model> \
  --sandbox <pipeline sandbox> \
  --config web_search=<pipeline web_search> \
  --output-schema <absolute schema path> \
  --output-last-message <temp output path> \
  --json \
  <prompt>
```

Important details:

- `--ask-for-approval` is passed as a global Codex flag before `exec`.
- `--skip-git-repo-check` is always enabled to support non-git working dirs.
- Output is directed to a temp file in the final output directory.
- On accepted output, `os.replace(temp, final)` gives atomic replace semantics.
- Only stderr tail (up to 20 lines) is retained for user/task error reporting.
- A usage CSV row is appended per Codex call (`codex_exec_activity.csv`) with timing, token usage (from `turn.completed.usage`), prompt text, exit data, and optional run/task context.

## 2) Output acceptance rules (`codex_exec.py`)

`run_codex_exec(...)` intentionally does not treat `returncode != 0` as always fatal.

Current decision logic:

1. Timeout: raise `CodexExecTimeoutError("codex exec timed out after <N>s")` and remove temp file if present.
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
3. Non-interactive smoke call: `codex --ask-for-approval never exec --skip-git-repo-check --sandbox read-only --model gpt-5.3-codex-spark "Reply with exactly: OK"`.

Smoke success rule is intentionally tolerant:

- Success if return code is 0, OR stdout contains an exact line `OK`.

This avoids false failures when Codex prints expected output but exits non-zero because of local warnings.

## 5) Integration with CLI and worker

`cli.one` path:

- Calls `run_codex_exec`.
- If timeout or `result.ok == False`: exits with error.
- If schema validation fails: deletes output file, exits with error.

`worker_loop` path:

- Calls `run_codex_exec`.
- Converts `result.ok == False` into `RuntimeError`.
- Runs local schema validation.
- On failure (`timeout`, `SchemaValidationError`, `RuntimeError`):
  - deletes output path
  - requeues or marks terminal error (Chunk 04 owns retry policy)

## 6) Known non-obvious rules

- Do not remove local schema validation because Codex `--output-schema` is not enough.
- Keep `--ask-for-approval` at Codex global scope (`codex ... exec`), not `codex exec ...`.
- Keep `--skip-git-repo-check` in worker and doctor calls.
- A non-zero Codex exit can still produce an accepted payload.
- Task success in worker mode requires both: accepted payload file and local schema pass.

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

## See also

- `docs/04-worker-execution-and-retries/04-worker-execution-and-retries_readme.md`
- `docs/06-integration-contracts-and-fixtures/06-integration-contracts-and-fixtures_readme.md`
- `docs/IMPORTANT CONVENTIONS.md`
