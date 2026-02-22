---
summary: "Pipeline pack loading, asset references, and root/workspace path resolution."
read_when:
  - "When editing pipeline JSON fields, prompt/schema paths, or --root behavior"
---

# Scope

Owns how pipeline packs are discovered and validated before execution starts.

## Primary files

- `src/codex_farm/pipeline_spec.py`
- `src/codex_farm/paths.py`
- `pipelines/*.json`
- `prompts/*.txt`
- `schemas/*.schema.json`

## Why separate

This is the configuration boundary; failures here should happen early and clearly.
