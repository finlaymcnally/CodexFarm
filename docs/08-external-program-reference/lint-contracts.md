---
summary: "Caller-facing lint command contract for pack/schema preflight and machine-readable findings."
read_when:
  - "When external callers need read-only preflight checks before process/run create/one"
  - "When integrating codex-farm lint --json in automation"
---

# Lint Contracts

## Purpose

`codex-farm lint` is a local read-only preflight command.

It validates pack structure and schema quality without creating runs, touching SQLite, or calling the `codex` binary.

## Command forms

Pack mode:

```bash
codex-farm lint --root /abs/path/to/pack --json
codex-farm lint --root /abs/path/to/pack --pipeline demo.echo.v1 --json
```

Schema mode:

```bash
codex-farm lint --schema /abs/path/to/schema.json --json
```

Strict exit behavior:

```bash
codex-farm lint --root /abs/path/to/pack --strict --json
```

## Exit-code contract

- default mode (`--strict` off):
  - exit `0` when no `error` findings exist (warnings allowed)
  - exit `1` when one or more `error` findings exist
- strict mode (`--strict` on):
  - exit `1` when any warning or error finding exists

## JSON output contract

`--json` emits one final object on stdout with this shape:

```json
{
  "target": {
    "kind": "pack",
    "root": "/abs/path/to/pack",
    "pipeline_id": null
  },
  "ok": true,
  "error_count": 0,
  "warning_count": 1,
  "scanned": {
    "pipeline_files": 1,
    "schema_files": 2
  },
  "findings": [
    {
      "code": "pack.missing_heads_up_assets",
      "severity": "warning",
      "path": "/abs/path/to/pack",
      "pipeline_id": null,
      "message": "Heads Up learning assets are missing: prompts/heads_up_distiller_v1.txt, schemas/heads_up_tipset_v1.schema.json",
      "hint": "Add these assets to enable full `heads-up learn` behavior; core pipeline execution can still work without them."
    }
  ]
}
```

Schema mode uses `target.kind: "schema"` and `target.path`.

## Finding codes

Stable finding codes exposed by the current lint engine:

- `pack.missing_sentinel_dirs`
- `pack.no_pipeline_files`
- `pack.missing_heads_up_assets`
- `pipeline.invalid_file`
- `pipeline.duplicate_id`
- `pipeline.missing_prompt_template`
- `pipeline.missing_output_schema`
- `pipeline.asset_outside_pack`
- `schema.invalid_json`
- `schema.invalid_definition`
- `schema.missing_local_ref`
- `schema.external_ref_not_supported`
- `schema.properties_not_in_required`

## Caller notes

- Explicit near-miss roots are supported for diagnostics:
  - if `--root` points to an existing directory missing `pipelines/`, `prompts/`, or `schemas/`, lint still runs and reports `pack.missing_sentinel_dirs`.
- `--schema` and `--pipeline` are mutually exclusive.
- Findings are deterministic in order: errors before warnings, then code/path/pipeline ordering.
