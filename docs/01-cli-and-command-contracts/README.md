---
summary: "User-facing CLI entrypoints and command output contracts."
read_when:
  - "When changing commands, options, JSON payloads, or go mode prompts"
---

# Scope

Owns command definitions and top-level orchestration glue.

## Primary files

- `src/codex_farm/cli.py`

## Why separate

CLI contract changes should be isolated from worker internals and schema logic.
