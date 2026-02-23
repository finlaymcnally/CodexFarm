---
summary: "High-level change history and major contract decisions for chunk 01 CLI behavior."
read_when:
  - "When CLI behavior feels inconsistent across commands or JSON output consumers"
  - "When deciding whether to change command defaults or persisted run config fields"
---

# 01 CLI And Command Contracts Log

## 2026-02-22_14.34.46 - CLI contract discovery pass

- Source: `docs/understandings/2026-02-22_14.34.46_cli-command-contract-discoveries.md` (merged).
- Confirmed `process --json` stdout must remain machine-clean with one final JSON object; creation/progress output belongs on stderr.
- Captured intentional default divergence: `run create` uses fixed `"**/*.json"` unless overridden, while `process` uses pipeline `input_glob_default` when `--glob` is omitted/empty.
- Locked run-config persistence behavior as part of CLI contract: `farm_root` always stored, `workspace_root` only stored when explicitly provided.
- Recorded `one` special-case behavior where `codex_cd_mode=input_dir` and `input_file_dir` both map to input-file parent.

## 2026-02-23_00.24.39 - Process hard-stop on rate-limit failures

- Source: `docs/understandings/2026-02-23_00.24.39_rate-limit-429-stop-policy.md`.
- Updated `process` contract so worker threads share a stop signal and halt additional task claims after codex rate-limit (`429`) failures.
- Documented that early-stop can leave remaining tasks queued for later resumption.
