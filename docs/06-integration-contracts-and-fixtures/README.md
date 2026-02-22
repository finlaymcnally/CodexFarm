---
summary: "End-to-end contract coverage, sample packs, and regression fixtures."
read_when:
  - "When validating behavior across chunk boundaries or updating test fixtures"
---

# Scope

Owns integration confidence: expected contracts across CLI, queue, worker, and Codex boundary.

## Primary files

- `tests/test_cli_integration_contracts.py`
- `tests/test_fake_codex_pipeline_pack_demo.py`
- `tests/test_process_smoke.py`
- `examples/`

## Why separate

Most cross-chunk regressions first appear here, so this is the fastest place to confirm impact.
