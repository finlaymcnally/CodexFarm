---
summary: "Recipeimport benchmark mode flags and artifact layout contract."
read_when:
  - "When using line-label benchmark mode from run create/process/go"
  - "When consuming benchmark artifacts from CodexFarm output directories"
---

# Benchmark Runtime Contracts

CodexFarm now supports explicit benchmark-native mode for recipeimport line labeling.

Enable it on run-based commands:

- `--recipeimport-benchmark-mode line_label_v1`
- optional `--recipeimport-benchmark-debug` for raw prompt/response captures

Supported commands:

- `codex-farm run create`
- `codex-farm process`
- `codex-farm go`

Run config persistence:

- `recipeimport_benchmark_mode` is stored only when explicitly set.
- `recipeimport_benchmark_debug` is stored only when explicitly set.

Worker behavior:

- In run-based CLI flows, `line_label_v1` dispatches to pipeline `recipeimport.benchmark.line_label.v1` regardless of the user-provided `--pipeline` value.
- Benchmark mode parses canonical lines from input JSON (`canonical_lines` or `lines`).
- Model output must contain `line_predictions`.
- Worker applies deterministic alignment/calibration and writes artifacts per completed task.
- The task output JSON (`<run output>/<rel_output_path>`) is the calibrated canonical payload, not the raw model payload.
- Benchmark schema validation failures are categorized as `benchmark_contract_error` and treated as terminal contract failures (no retry loop for unrecoverable contract mismatches).

Per-task artifact root:

- `<run output dir>/.recipeimport-benchmark/<task_id>/`

Required artifacts:

- `canonical.lines.json`
- `predictions.raw.json`
- `predictions.aligned.json`
- `predictions.calibrated.json`
- `aligned.line_mappings.json`
- `calibration.actions.json`
- `pass.metrics.json`
- `debug.manifest.json`

Debug-only artifacts (`--recipeimport-benchmark-debug`):

- `debug.request.prompt.txt`
- `debug.response.output.raw.json`
- `debug.response.stdout_tail.txt`
- `debug.response.stderr_tail.txt`

`debug.response.output.raw.json` stores the raw model response payload captured before calibration/canonicalization.

Manifest requirements (`debug.manifest.json`):

- `mode` (`line_label_v1`)
- `model`
- `prompt_sha256`
- `output_schema_sha256`
- `pass_metrics` with `pass_stage_scores`
- `files` map with relative paths + hashes

Pass metrics semantics:

- `alignment_coverage` measures observed model coverage before fill/calibration (`observed_lines / total_lines`), so it can be below `1.0` even when calibrated output includes one prediction per canonical line.

Failure behavior:

- If benchmark payload parsing fails, task fails with `benchmark_contract_error`.
- If benchmark artifact writing fails, task fails/retries like other execution failures (it is not warning-only).
