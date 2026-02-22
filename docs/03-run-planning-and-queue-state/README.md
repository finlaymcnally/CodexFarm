---
summary: "Run creation, task queue records, and inferred run status state."
read_when:
  - "When changing input enumeration, task rows, run metadata, or status reporting"
---

# Scope

Owns durable run/task state and the mapping from input tree to output tree.

## Primary files

- `src/codex_farm/db.py` (run/task schema, create/enqueue/status/listing paths)
- `src/codex_farm/cli.py` (`run create`, `run status`, `run tasks`, `run errors`)

## Why separate

Queue shape and status inference are a persistent data contract independent of Codex execution details.
