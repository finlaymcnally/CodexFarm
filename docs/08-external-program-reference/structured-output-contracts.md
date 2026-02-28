---
summary: "Caller-facing structured-output contract: schema override flags, persistence, and template shape guidance."
read_when:
  - "When external callers need deterministic JSON shape validation"
  - "When using --output-schema on one/run create/process/go"
---

# Structured Output Contracts

## Default behavior

Each pipeline defines an `output_schema_path` in its pipeline JSON.

At runtime, codex-farm uses that schema in two places:

1. Codex structured output request (`codex exec --output-schema ...`).
2. Local Draft 2020-12 validation after output is written.

## Caller override

External callers can override pipeline schema with:

```bash
--output-schema /abs/path/to/caller.schema.json
```

Supported on:

- `codex-farm one`
- `codex-farm run create`
- `codex-farm process`
- `codex-farm go`

Behavior notes:

- Path must exist and be a file.
- `one` uses the override for that invocation only.
- Run-based flows (`run create`, `process`, `go`) persist the override in run config as `output_schema_path_override`, and also freeze a copy of the effective schema under `<data_dir>/run_assets/<run_id>/`.
- Snapshot-bearing runs execute and locally validate against the frozen schema copy, so later edits to the live source schema do not drift already-created runs.

## Pass/Fail and Retry Semantics

Schema acceptance is the final gate.

- If output matches the resolved schema, task succeeds.
- If output fails schema validation:
  - `one` exits non-zero and deletes invalid output.
  - worker/process flows mark the attempt as failed, delete invalid output, then either retry or mark terminal `error` based on `--max-attempts`.

Caller inspection flow:

```bash
codex-farm process ... --output-schema /abs/path/to/caller.schema.json --json
codex-farm run errors --run-id <run_id> --data-dir ./var --json
```

Use `run errors --json` to read per-file terminal failures after retries are exhausted.

## JSON fields to verify

Caller-facing JSON payloads expose the resolved schema path as:

- `output_schema_path`

This is included in:

- `run create --json`
- `process --json`

Telemetry note:

- `codex_exec_activity.csv` keeps logical schema identity in `output_schema_path` when a run executes against frozen schema files, so external analytics can still group by original schema source path.

## Minimal schema template pattern

Use Draft 2020-12 and keep the shape explicit:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["id", "result"],
  "properties": {
    "id": { "type": "string" },
    "result": { "type": "object" }
  }
}
```

Current Codex structured-output behavior in this project expects every key under `properties` to also appear in `required`.

## Built-in recipe schema conventions

These are caller-visible because they affect validation outcomes even when prompts change:

- `recipe.schemaorg.normalize.v1` expects `recipeInstructions` to be an array of `{"@type":"HowToStep","text":...}` objects.
- `schemas/recipeimport_intermediate_fullshape_v1.schema.json` and `schemas/recipeimport_final_fullshape_v1.schema.json` are maintained as inbound contracts and must validate both sparse real samples and platonic full-shape samples in `examples/recipeimport_*`.
