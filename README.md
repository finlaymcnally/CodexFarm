# codex-farm

Local CLI worker farm for running many `codex exec` tasks against files.

## Quickstart

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
codex-farm doctor
```

## Workflows

`process` is script-friendly:

```bash
codex-farm process \
  --pipeline recipe.schemaorg.normalize.v1 \
  --in examples/schemaorg_recipes_in \
  --out var/demo_out \
  --workers 4 \
  --data-dir ./var
```

External pipeline-pack workflow (pack can live outside this repo):

```bash
codex-farm pipelines list --root /abs/path/to/pack --json
codex-farm process \
  --root /abs/path/to/pack \
  --pipeline demo.echo.v1 \
  --in /abs/path/to/inputs \
  --out /abs/path/to/outputs \
  --json
codex-farm run errors --run-id <run_id> --data-dir ./var --json
```

`--workspace-root` is optional. If omitted, Codex `--cd` comes from pipeline
`codex_cd_mode` (`asset_root`, `input_dir`, or `input_file_dir`).

`go` is interactive inbox/outbox mode:

```bash
codex-farm init --data-dir ./var
cp examples/schemaorg_recipes_in/*.json ./var/inbox/
codex-farm go --data-dir ./var
```

Telemetry dashboard snapshot:

```bash
codex-farm stats-dashboard --data-dir ./var
```

## Folder notes

- `pipelines/`: pipeline config JSON files.
- `prompts/`: prompt templates used by workers.
- `schemas/`: JSON Schemas enforced by Codex and validated locally.
- `examples/`: tiny sample inputs and expected structural examples.
- `examples/pipeline_pack_demo/`: minimal external pipeline-pack for `--root` smoke tests.
