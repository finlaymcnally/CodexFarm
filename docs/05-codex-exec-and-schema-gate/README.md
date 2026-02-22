---
summary: "Codex subprocess contract, atomic output handling, and local schema enforcement."
read_when:
  - "When changing codex exec flags, timeout behavior, or output validation rules"
---

# Scope

Owns the boundary with the external Codex CLI and final output acceptance rules.

## Primary files

- `src/codex_farm/codex_exec.py`
- `src/codex_farm/schema_utils.py`
- `src/codex_farm/doctor.py`

## Why separate

This chunk defines what counts as a successful generation regardless of queue/worker logic.
