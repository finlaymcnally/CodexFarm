---
summary: "Major Codex subprocess and schema-gate decisions, including compatibility and schema-shape lessons."
read_when:
  - "When changing codex exec invocation, doctor checks, or schema acceptance rules"
---

# 05 Codex Exec And Schema Gate Log

## 2026-02-20_13.05.00 - Codex CLI compatibility quirks and acceptance behavior

- Source: `docs/understandings/2026-02-20_13.05.00_codex-cli-exec-compat.md` (merged).
- Locked invocation shape for compatibility: use global approval flag (`codex --ask-for-approval never exec ...`) and always pass `--skip-git-repo-check`.
- Preserved tolerant acceptance rule for non-zero exits when `--output-last-message` produced non-empty payload.
- Kept doctor smoke-check tolerance for exact `OK` output even with non-zero exit to avoid false negatives.
- Documented strict current `--output-schema` subset behavior requiring all property keys in `required` and the nullable-required workaround for optional fields.

## 2026-02-20_13.09.19 - Recipeimport schema coverage correction

- Source: `docs/understandings/2026-02-20_13.09.19_recipeimport-schema-coverage.md` (merged).
- Captured previous failure mode where over-required fullshape schemas rejected sparse real payloads.
- Recorded contract update to support both sparse real samples and platonic full-shape samples.
- Aligned `schemas/recipeimport_final_fullshape_v1.schema.json` with canonical schema at `examples/recipeimport_final/recipeDraftV1.canonical.recipeimport.schema.json`.

## 2026-02-22_14.33.40 - Chunk 05 acceptance-boundary framing

- Source: `docs/understandings/2026-02-22_14.33.40_chunk-05-codex-exec-schema-gate-behavior.md` (merged).
- Reframed chunk 05 as acceptance boundary, not only subprocess wrapper.
- Preserved atomic temp-file-to-final promotion requirement.
- Reconfirmed separation of concerns: one-shot CLI failure behavior in `cli.one` versus retry/terminal branching in worker flow.

