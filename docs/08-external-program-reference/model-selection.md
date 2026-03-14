---
summary: "Caller-facing model and reasoning-effort selection contract for codex-farm CLI integrations."
read_when:
  - "When wiring a model picker UI for codex-farm"
  - "When passing model or effort overrides from an external caller"
---

# Model Selection

## Discover model options

Use:

```bash
codex-farm models list --json
```

Each row includes:

- `slug` (pass to `--model`)
- `display_name`
- `description`
- optional `supported_reasoning_efforts`

## Pass model and effort

The following commands accept model override:

- `codex-farm one`
- `codex-farm run create`
- `codex-farm process`
- `codex-farm go`

Model flags:

- `--model`
- `--codex-model` (alias)

Reasoning-effort flags:

- `--effort`
- `--reasoning-effort`
- `--thinking-effort`
- `--codex-reasoning-effort`
- `--codex-thinking-effort`

Allowed effort values:

- `none`
- `minimal`
- `low`
- `medium`
- `high`
- `xhigh`

Example:

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

## JSON output fields to read

For `process --json`, key caller fields include:

- `codex_model`
- `codex_reasoning_effort`
- `run_id`
- `status`
- `counts`
- `exit_code`

For `run create --json`, key caller fields include:

- `codex_model`
- `codex_reasoning_effort`
- `run_id`
- `total`

## Precheck behavior

`one`, `process`, and `go` use the resolved `--model` / `--reasoning-effort` values for the startup non-interactive Codex smoke check. If that precheck fails, the failure now reflects the same model/effort pair the command would have used for real execution.
