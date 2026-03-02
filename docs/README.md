---
summary: "Onboarding map for the docs/ chunk: what lives here, how to navigate it, and how to keep it accurate."
read_when:
  - "When you are new to this repository or touching documentation/contracts"
---

# docs/ chunk guide for AI coders

## What this folder is

`docs/` is the project's knowledge layer. It stores:

- behavior contracts for code boundaries
- project rules that are easy to break silently
- implementation plans and discovered gotchas
- tooling that indexes and snapshots documentation

Code lives under `src/`, but this folder explains how and why that code is expected to behave.

## 60-second orientation (read in this order)

1. `docs/AGENTS.md` for required docs workflow and front-matter rules.
2. `docs/how-codex-farm-works.md` for the full end-to-end runtime story.
3. `docs/01-cli-and-command-contracts/01-cli-and-command-contracts_readme.md` through `docs/07-analytics/07-analytics_readme.md` for boundary ownership and non-obvious runtime rules.
4. `docs/08-external-program-reference/README.md` for caller-facing machine contracts.
5. `docs/AI_Context.md` for cross-cutting runtime invariants and debugging flow.
6. `docs/how-codex-farm-works-for-AI.md` when you need deep debugging-level context.

If your task matches a `read_when` hint in any file, read that file before coding.

## Folder map and ownership

- `docs/01-cli-and-command-contracts/01-cli-and-command-contracts_readme.md`
  Owns CLI command/output contracts in `src/codex_farm/cli.py`.
- `docs/02-pipeline-assets-and-root-resolution/02-pipeline-assets-and-root-resolution_readme.md`
  Owns pipeline spec loading and root/workspace resolution.
- `docs/03-run-planning-and-queue-state/03-run-planning-and-queue-state_readme.md`
  Owns SQLite run/task model and run-state reporting.
- `docs/04-worker-execution-and-retries/04-worker-execution-and-retries_readme.md`
  Owns worker leasing/retry behavior and task execution lifecycle.
- `docs/05-codex-exec-and-schema-gate/05-codex-exec-and-schema-gate_readme.md`
  Owns Codex subprocess contract and output acceptance rules.
- `docs/06-integration-contracts-and-fixtures/06-integration-contracts-and-fixtures_readme.md`
  Owns cross-boundary integration checks and fixture expectations.
- `docs/07-analytics/07-analytics_readme.md`
  Owns telemetry CSV + static dashboard contracts.
- `docs/08-external-program-reference/`
  Caller-facing reference docs for external programs integrating via CLI/JSON.
- `docs/understandings/`
  Timestamped short discoveries from code exploration.
- `docs/plans/`
  ExecPlans (living design/implementation documents; see `docs/PLANS.md`).
- `docs/tasks/`
  Task specs used by the workflow in `docs/THE_PERFECT_COMMIT.md`.

## Docs tooling in this chunk

- `docs/docs-list.ts`
  Walks `docs/` and prints each markdown file's `summary` and `read_when`.
  Skips hidden entries and directories named `archive` or `research`.
  Flags metadata problems (`missing front matter`, `summary key missing`, etc.).
- `docs/docs-list.md`
  Human-readable contract for the docs-list script.
- `docs/build-docs-summary.sh`
  Generates timestamped combined snapshots:
  `docs/YYYY-MM-DD_HH.MM.SS_<repo>-docs-summary.md`
  Includes `.md`/`.txt`, skips `_log.md` and prior `-docs-summary.md` files.

## Required document format

Every `docs/**/*.md` file must start with front matter:

```md
---
summary: "One-line summary"
read_when:
  - "When this doc should be read"
---
```

`summary` is required and must be non-empty.
`read_when` is optional but strongly recommended.

## When to update which doc

- Changed behavior at a specific code boundary:
  Update the matching chunk README (`01` through `07`).
- Learned or changed a cross-cutting invariant:
  Update the relevant chunk README non-obvious/discoveries section and caller references in `docs/08-external-program-reference/` when machine-facing behavior changes.
- Spent time untangling a non-trivial code path:
  Add a timestamped note in `docs/understandings/`.
- Designing or implementing a significant feature/refactor:
  Create or update an ExecPlan in `docs/plans/` and keep it current while implementing.

## Practical workflow for future AI coders

1. Run the docs index (`npm run docs:list` or `npx tsx docs/docs-list.ts`) when available.
2. Read files whose `read_when` matches your task.
3. Make code changes.
4. Update docs in the same boundary so docs stay source-aligned.
5. Add/update `docs/understandings/` plus the relevant chunk README sections when new hidden rules are discovered.

## Timestamp rule used in this repo

When creating timestamped docs files, use:
`YYYY-MM-DD_HH.MM.SS`

## 2026-02-28_14.36.38 task-doc triage map (moved from `docs/understandings`)

This note was used to collapse `docs/tasks` and `docs/understandings` entries into chunk ownership boundaries:

- `docs/tasks/idea1-2.md`, `docs/tasks/idea1-7.md`, and related task triage entries belong to `docs/02-pipeline-assets-and-root-resolution`.
- `docs/tasks/idea1-1.md` and `docs/tasks/idea1-4.md` map to `docs/03-run-planning-and-queue-state`.
- `docs/tasks/2026-02-28_09.33.49-heads-up-adaptive-prompts.md`, `docs/tasks/idea1-3.md`, and `docs/tasks/idea1-5.md` map to `docs/04-worker-execution-and-retries`.
- `docs/tasks/idea1-6.md` maps to `docs/05-codex-exec-and-schema-gate`.
- `docs/tasks/2026-02-28_10.29.20-autotune-cli-diff-emitter.md` and `docs/tasks/2026-02-28-15.02.31-telemetry-reporting-api.md` map to `docs/07-analytics`.
- `docs/understandings/2026-02-28_14.50.12-oracle-manual-login-timeout.md` and `docs/understandings/2026-02-28_15.03.51-github-push-protection-browser-profile-secret.md` are cross-cutting and were merged into `docs/08-external-program-reference`.
- `docs/understandings/2026-03-01_20.40.25-recipeimport-codexfarm-progress-surface.md` also merged into `docs/08-external-program-reference`.

Primary rule used: attribute runtime-facing notes to the chunk owning the failing seam, and keep cross-cutting exceptions in `08` so external integrations remain discoverable.
