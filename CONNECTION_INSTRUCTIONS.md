# Connection Instructions

Use this when another program wants to call `codex-farm` and let users pick a model.

## 1. Prerequisites

```bash
source .venv/bin/activate
pip install -e ".[dev]"
codex-farm doctor
```

## 2. Discover choices for your UI

Get pipelines:

```bash
codex-farm pipelines list --root /abs/path/to/pack --json
```

Get model picker options:

```bash
codex-farm models list --json
```

`models list --json` returns rows like:

- `slug` (pass this to `--model`)
- `display_name`
- `description`
- optional `supported_reasoning_efforts`

## 3. Run with selected model/effort

```bash
codex-farm process \
  --root /abs/path/to/pack \
  --pipeline demo.echo.v1 \
  --in /abs/path/to/inputs \
  --out /abs/path/to/outputs \
  --model gpt-5.3-codex \
  --reasoning-effort high \
  --json
```

Caller-provided output validation contract (optional):

```bash
codex-farm process \
  --root /abs/path/to/pack \
  --pipeline demo.echo.v1 \
  --in /abs/path/to/inputs \
  --out /abs/path/to/outputs \
  --output-schema /abs/path/to/caller.schema.json \
  --json
```

`--output-schema` also works on `one`, `run create`, and `go`. For run-based flows, it is persisted so resumed workers use the same schema.

## 4. JSON contract rules

- Use `--json` for machine parsing.
- For `process --json`, stdout is JSON only; progress is on stderr.
- Important fields in `process --json` output:
  - `run_id`
  - `status`
  - `counts`
  - `codex_model`
  - `codex_reasoning_effort`
  - `output_schema_path`
  - `exit_code`

## 5. Error inspection

```bash
codex-farm run errors --run-id <run_id> --data-dir ./var --json
```

## 6. Optional queued flow

If you want create-now/process-later behavior:

1. `codex-farm run create ... --json`
2. `codex-farm worker --run-id <run_id> ...` or `codex-farm process ...`
3. `codex-farm run status --run-id <run_id> --json`

## 7. Python caller example

```python
import json
import subprocess

def run_json(cmd: list[str]) -> dict | list:
    completed = subprocess.run(cmd, text=True, capture_output=True, check=True)
    return json.loads(completed.stdout)

models = run_json(["codex-farm", "models", "list", "--json"])
model_slug = models[0]["slug"]

result = run_json(
    [
        "codex-farm", "process",
        "--root", "/abs/path/to/pack",
        "--pipeline", "demo.echo.v1",
        "--in", "/abs/path/to/inputs",
        "--out", "/abs/path/to/outputs",
        "--model", model_slug,
        "--reasoning-effort", "medium",
        "--json",
    ]
)

if result["exit_code"] != 0:
    errors = run_json(
        ["codex-farm", "run", "errors", "--run-id", result["run_id"], "--json"]
    )
    print(errors)
```
