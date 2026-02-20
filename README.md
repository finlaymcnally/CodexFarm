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

`go` is interactive inbox/outbox mode:

```bash
codex-farm init --data-dir ./var
cp examples/schemaorg_recipes_in/*.json ./var/inbox/
codex-farm go --data-dir ./var
```

## Folder notes

- `pipelines/`: pipeline config JSON files.
- `prompts/`: prompt templates used by workers.
- `schemas/`: JSON Schemas enforced by Codex and validated locally.
- `examples/`: tiny sample inputs and expected structural examples.
