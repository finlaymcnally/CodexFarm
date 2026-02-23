---
summary: "Major integration-testing strategy decisions and seam-boundary rationale."
read_when:
  - "When modifying integration coverage strategy or deciding fixture style"
---

# 06 Integration Contracts And Fixtures Log

## 2026-02-22_13.22.52 - Flow-boundary mapping

- Source: `docs/understandings/2026-02-22_13.22.52_flow-chunk-boundary-mapping.md` (merged).
- Captured intentional six-boundary split: CLI contract, root/assets, queue state, worker execution/retries, codex/schema gate, integration contracts.
- Recorded purpose of split: follow real call path and keep most edits local to one chunk plus an adjacent seam.

## 2026-02-22_14.34.17 - Dual fixture strategy decision

- Source: `docs/understandings/2026-02-22_14.34.17_integration-contract-fixture-strategy.md` (merged).
- Preserved monkeypatch-based integration tests (`codex_farm.worker.run_codex_exec`) for fast CLI payload and queue-contract assertions.
- Preserved fake-`codex` binary integration tests for subprocess argument seam assertions.
- Logged coverage tradeoff history: single-strategy suites left blind spots either in subprocess wiring or in fast targeted contract checks.
