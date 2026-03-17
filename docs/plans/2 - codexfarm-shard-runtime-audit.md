---
summary: "ExecPlan for turning the current per-task Codex farm into an honest session-aware shard runtime for external callers such as RecipeImport."
read_when:
  - "When planning or implementing persistent multi-task Codex session reuse in CodexFarm."
  - "When deciding whether `process` already provides honest shard-worker session semantics."
  - "When adding runtime-mode, session, or external caller contract changes to CodexFarm."
---

# Build Honest Session-Aware Shard Runtime

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept up to date as work proceeds.

This document follows `docs/PLANS.md` and must be maintained in accordance with that file.

## Purpose / Big Picture

After this change, CodexFarm will be able to tell the truth about two different execution shapes instead of blurring them together. The existing mode will remain a reliable one-task-per-`codex exec` farm. The new mode will let one worker boot a Codex session once, handle multiple queued shard tasks inside that same session, and expose machine-readable session counts, turn counts, and task-to-session attribution to external callers such as RecipeImport.

The user-visible outcome is not just “compatibility.” A caller will be able to run `codex-farm process` with a runtime mode that explicitly means session reuse, then verify that one fresh Codex session handled multiple tasks by inspecting `process --json`, progress events, session artifacts, and telemetry summaries. Just as importantly, callers that stay on the classic mode will continue seeing the current one-shot behavior and current JSON contracts.

## Progress

- [x] (2026-03-17 16:56Z) Read `docs/PLANS.md`, `docs/AGENTS.md`, docs-list output, and the runtime docs that own CLI, queue, worker, telemetry, and external-caller contracts.
- [x] (2026-03-17 16:59Z) Verified the core audit claim in code: `process` still creates ordinary worker loops, each claimed task still ends in one `run_codex_exec(...)` call, and current reuse is worker-thread reuse rather than Codex session reuse.
- [x] (2026-03-17 17:01Z) Verified that local Codex exposes `codex exec resume`, so persistent-session work should target a non-interactive resume seam rather than driving the interactive TUI.
- [x] (2026-03-17 17:04Z) Added `docs/understandings/2026-03-17_13.04.10-codex-exec-resume-is-the-session-transport-but-drops-per-turn-controls.md` to capture the hidden constraint that resumed turns do not expose per-turn `--cd` or `--output-schema`.
- [x] (2026-03-17 17:09Z) Rewrote this file from an audit memo into a self-contained ExecPlan with milestones, validation, recovery guidance, and explicit design choices.
- [x] (2026-03-17 19:06Z) Implemented runtime-mode plumbing and guardrails in `process`, `run create`, `go`, frozen run assets, worker dispatch, and machine output (`runtime_mode`, `effective_workers`, session counters).
- [x] (2026-03-17 19:19Z) Added low-level session transport helpers in `src/codex_farm/codex_exec.py`, including focused tests for boot/resume telemetry fields and a real local `codex exec` + `codex exec resume` smoke.
- [x] (2026-03-17 19:27Z) Added session persistence in SQLite (`worker_sessions` + task linkage fields) and on-disk `.codex-farm-sessions/<session_row_id>/` artifacts.
- [x] (2026-03-17 19:33Z) Extended progress snapshots/events, process JSON, telemetry rows, telemetry summaries, and autotune runtime advice with session-aware fields.
- [x] (2026-03-17 19:41Z) Added focused agentic tests, full pytest verification, and refreshed the relevant docs chunks plus `src/codex_farm/README.md`.

## Surprises & Discoveries

- Observation: the current audit direction was correct about the architectural gap, but it was too vague about the transport seam.
  Evidence: local `codex exec --help` exposes `resume`, so persistent reuse can stay non-interactive.

- Observation: `codex exec resume --help` still supports `--json` and `--output-last-message`, but it does not expose per-turn `--cd`, `--sandbox`, `--ask-for-approval`, or `--output-schema`.
  Evidence: local CLI help captured during planning and summarized in `docs/understandings/2026-03-17_13.04.10-codex-exec-resume-is-the-session-transport-but-drops-per-turn-controls.md`.

- Observation: current CodexFarm output acceptance relies on the one-shot subprocess wrapper invoking `codex exec --output-schema ... --output-last-message ...`.
  Evidence: `src/codex_farm/codex_exec.py::run_codex_exec(...)` is the only execution hook and always builds one fresh `codex exec` command per task.

- Observation: current queue and telemetry models are task-centric, not session-centric.
  Evidence: `docs/03-run-planning-and-queue-state/03-run-planning-and-queue-state_readme.md` defines `runs` plus `tasks` only, and `docs/07-analytics/07-analytics_readme.md` states that one telemetry row is appended per `run_codex_exec(...)` call.

- Observation: RecipeImport-side audit labels are not enough on their own.
  Evidence: external callers can currently stamp their own runtime metadata, but `process --json`, progress events, frozen assets, and telemetry do not carry a first-class CodexFarm runtime mode or fresh-session count.

- Observation: a live Codex smoke showed that `thread.started.thread_id` is a valid `codex exec resume` key in this environment.
  Evidence: `codex exec resume 019cfcdc-f160-7b22-863e-bc2cb4c058d8 ...` returned a valid follow-up payload in the same conversation.

- Observation: resumed turns can emit a non-fatal model-mismatch warning because `codex exec resume` does not take `--model`.
  Evidence: the live smoke emitted an `item.completed` warning that a session recorded with `gpt-5.3-codex-spark` was resuming with `gpt-5.4`; the resumed payload still succeeded.

## Decision Log

- Decision: keep the current one-shot behavior as an explicit runtime mode named `classic_task_farm_v1`.
  Rationale: the existing behavior is useful, stable, and already integrated by callers. The problem is honesty and missing capability, not that the current path should be removed.
  Date/Author: 2026-03-17 / Codex

- Decision: use `structured_loop_agentic_v1` as the first real persistent-session mode.
  Rationale: RecipeImport already uses that label for audit intent, so promoting it into a real CodexFarm contract removes the gap between caller-side claims and runtime truth.
  Date/Author: 2026-03-17 / Codex

- Decision: design the new mode around `codex exec` for session boot and `codex exec resume` for follow-up turns.
  Rationale: local Codex already exposes those non-interactive commands. Driving the interactive TUI through a PTY would be less deterministic and less aligned with CodexFarm’s current subprocess architecture.
  Date/Author: 2026-03-17 / Codex

- Decision: use a neutral persisted field such as `resume_key` for the thing passed back into `codex exec resume`, while storing `thread_id` separately.
  Rationale: current runtime already captures `thread_id`, but the CLI help for `exec resume` talks about a “conversation/session id” or thread name. Until the exact resume identifier is proven, the storage model should not falsely assume `thread_id == session_id`.
  Date/Author: 2026-03-17 / Codex

- Decision: in this implementation, persist the captured `thread.started.thread_id` as the `resume_key` while still storing `thread_id` separately.
  Rationale: the live smoke proved that the current local CLI accepts the thread id as the resume identifier, and keeping the neutral field name preserves flexibility if upstream semantics change later.
  Date/Author: 2026-03-17 / Codex

- Decision: make the first release of `structured_loop_agentic_v1` default to `workers=1` and reject `--workers >1`.
  Rationale: the immediate caller need is “one phase worker equals one reused Codex session.” Multi-session parallel agentic mode can come later, but the first cut should make the semantics impossible to misread.
  Date/Author: 2026-03-17 / Codex

- Decision: keep legacy one-shot schema enforcement for `classic_task_farm_v1`, but move follow-up-turn output extraction and schema validation for `structured_loop_agentic_v1` into CodexFarm.
  Rationale: resumed turns do not expose per-turn `--output-schema`, so the new mode needs its own deterministic local acceptance gate.
  Date/Author: 2026-03-17 / Codex

## Outcomes & Retrospective

As of 2026-03-17, the main implementation milestones in this plan are complete. CodexFarm now exposes explicit runtime modes, persists session-aware run assets, executes `structured_loop_agentic_v1` through a dedicated `session_runtime.py` state machine, records truthful `worker_sessions` rows plus task/session linkage, writes session artifacts under `.codex-farm-sessions/`, and emits additive session counters in progress snapshots, `process --json`, telemetry rows, telemetry summaries, and autotune output.

The remaining caveat is upstream resume behavior: resumed turns still do not expose per-turn `--model`, `--cd`, or `--output-schema`, and the live smoke showed a non-fatal model-mismatch warning on resume. The current implementation handles that truthfully by pinning session-level execution context, validating every resumed payload locally, persisting `resume_key` separately from `thread_id`, and exposing session-aware diagnostics so callers can see when resets or low reuse reduce the benefit of agentic mode.

## Context and Orientation

CodexFarm is a local Python CLI that turns a directory of input files into queued tasks, stores run state in SQLite, freezes prompt/schema assets for reproducibility, and executes Codex through a single subprocess wrapper. Right now the durable planning model is “one input file equals one queued task,” and the runtime model is “one claimed task equals one fresh `codex exec` subprocess.”

The key files are:

`src/codex_farm/cli.py` owns the main commands. `process_command(...)` creates a run, persists run config, starts worker threads, and emits the final JSON payload plus optional progress events.

`src/codex_farm/worker.py` owns task claiming, retries, and per-task execution. `worker_loop(...)` leases one task at a time, renders the prompt, and calls `run_codex_exec(...)` once for that task.

`src/codex_farm/codex_exec.py` owns the actual Codex subprocess contract. Today it builds one `codex exec` command, passes `--output-schema` and `--output-last-message`, parses JSONL events, and writes one telemetry row for each subprocess invocation.

`src/codex_farm/db.py` owns the SQLite schema and queue primitives. Today it cleanly models `runs`, `tasks`, and failed-attempt evidence, but it does not yet have a first-class concept of a worker session that spans multiple tasks.

`src/codex_farm/run_assets.py` freezes effective pipeline configuration under `var/run_assets/<run_id>/`. Any runtime-mode knob that must survive resume or later pack edits belongs here.

`src/codex_farm/telemetry_report.py`, `src/codex_farm/autotune.py`, and `docs/07-analytics/07-analytics_readme.md` define how execution telemetry is aggregated today. The current aggregation unit is still one Codex subprocess call.

`docs/08-external-program-reference/` documents the caller-facing contracts that RecipeImport and other external programs depend on. Any additive JSON fields or progress-event fields introduced by this work must be documented there.

In this plan, a “task” means one input file mapped to one queue row. A “worker session” means one Codex conversation that can serve multiple queued tasks over multiple turns. A “runtime mode” means the contract selected by the caller that determines whether workers behave as classic one-shot task processors or as persistent multi-turn session processors. A “resume key” means the exact identifier string that CodexFarm stores and later passes to `codex exec resume`; it is intentionally named neutrally until the CLI’s identifier semantics are proven.

## Milestone 1: Make runtime mode a first-class contract and prove the transport seam

At the end of this milestone, CodexFarm will still execute the old way, but the caller contract will stop pretending that all workers are the same shape. `process`, run config, and frozen assets will have an explicit runtime mode. The implementation will also have a small, isolated low-level transport proof that confirms how CodexFarm starts and resumes a Codex session non-interactively.

Start in `src/codex_farm/cli.py`. Add a `--runtime-mode` option to `process`, `run create`, and any helper path that persists run config. Use `classic_task_farm_v1` as the default. Accept `structured_loop_agentic_v1` as the new mode. Persist the selected mode in `runs.config_json`, surface it additively in `run create --json` and `process --json`, and freeze it in `run_assets.py` so resumed workers use the same mode even if pipeline files change later.

Still in `cli.py`, implement the first guardrail now instead of later: if `runtime_mode == "structured_loop_agentic_v1"` and the caller does not pass `--workers`, default to `1`; if the caller explicitly passes `--workers > 1`, reject the command with `typer.BadParameter`. Also surface `effective_workers` in machine JSON so callers never need to infer this from stderr.

Then isolate the transport seam in `src/codex_farm/codex_exec.py`. Do not yet rewrite `worker_loop(...)`. Instead, add a low-level pair of helpers that can be exercised independently from the queue:

    start_codex_session(...)
    resume_codex_session(...)

Both helpers should stay close to the current `run_codex_exec(...)` implementation so they reuse JSONL parsing, tail capture, rollout harvest, trace persistence, and best-effort telemetry writing. `start_codex_session(...)` should run `codex exec`. `resume_codex_session(...)` should run `codex exec resume <resume_key>`. Both should support `--json` and `--output-last-message` because those are available in the local CLI. Both must also receive the same persisted execution isolation that current recipe-safe runs already use: the resolved `codex_home_path` from run config and the prepared scratch execution context when the pipeline is configured for scratch isolation. The helpers must return a richer result type than the current `CodexExecResult`, including the parsed event list, last-message path information, observed `thread_id`, and any candidate `resume_key` if it can be determined.

Do not assume that `thread_id` is the resume key. Instead, implement a tiny transport proof path that captures whatever identifier is truly needed to resume. If the CLI output already contains a stable resume identifier, store it directly. If it does not, inspect the non-ephemeral session artifacts under `CODEX_HOME/sessions/...` using the observed `thread_id` and the current Codex home path to derive the exact resume key. Keep that logic local to the transport layer so higher-level code can depend only on a neutral `resume_key`.

Acceptance for this milestone is twofold. First, `process --json` must show `runtime_mode` and `effective_workers` for both classic and agentic requests, with agentic mode rejecting `--workers > 1`. Second, a focused low-level test must prove that CodexFarm can issue one boot call and one resumed call through the new helpers without involving the worker queue. A manual smoke command using real Codex is also required at least once during implementation, because fake-Codex tests alone cannot prove upstream `exec resume` behavior.

## Milestone 2: Build a session runner that can process multiple queued tasks inside one Codex conversation

At the end of this milestone, CodexFarm will have a new execution path alongside the current worker loop. One persistent worker session will claim tasks one at a time, send each task as a follow-up turn when possible, and keep per-task ownership boundaries intact even though the Codex conversation persists.

Create a new runtime module, for example `src/codex_farm/session_runtime.py`. This module should own a small state machine rather than stretching the existing `worker_loop(...)` into something ambiguous. The essential object can be a dataclass such as `WorkerSessionState` with fields like:

    run_id: str
    worker_id: str
    runtime_mode: str
    status: str
    resume_key: str | None
    thread_id: str | None
    turn_count: int
    task_count: int
    started_at: str
    current_task_id: str | None

The session runner should keep the outer worker behavior recognizable. It still claims one queued task at a time through SQLite. It still updates heartbeats and preserves task lease ownership. What changes is the execution step. The first claimed task boots the session with `start_codex_session(...)`. Later tasks use `resume_codex_session(...)` against the stored `resume_key`, unless a reset condition forces a fresh session.

Session reuse must not silently drop the recipe isolation work that already exists in CodexFarm. When a run was created with `codex_execution_context="scratch"` and a resolved `codex_home_path` such as the recipe profile home, the session runner must use that same `CODEX_HOME` and scratch-root strategy for the entire session lifecycle. In plain terms: boot and resume must stay inside the dedicated recipe Codex home rather than falling back to the coding-agent home between turns.

This runtime must define two prompt layers and freeze both in `run_assets.py`:

1. A boot prompt contract that establishes the session behavior and tells Codex how task turns will be framed.
2. A task-turn contract that wraps each task input in a deterministic marker format so CodexFarm can recover the final JSON payload from the last message without guessing where one task ends and the next begins.

Because resumed turns do not expose per-turn `--output-schema`, the agentic runtime must treat output extraction as a CodexFarm responsibility. The simplest acceptable contract is: each task turn instructs Codex to end its final response with only the JSON object for that task, and CodexFarm then validates that last message locally against the selected schema. Keep the legacy one-shot path unchanged; only the agentic runtime uses this local follow-up-turn extraction path.

The session runner must also define reset rules. Keep them explicit and frozen per run. Minimum required controls are:

- `session_task_budget`: maximum number of tasks in one session before a reset.
- `max_turns_per_task`: hard cap for a task’s turns before the task is failed or the session is reset.
- `session_reset_on_error`: whether a failed resumed turn forces a fresh session before the next task.

For the first release, retries should stay conservative. If a resumed turn fails in a way that leaves session state ambiguous, mark the current task retryable, end the worker session, and let the next attempt boot fresh. It is better to lose reuse than to corrupt task attribution.

Acceptance for this milestone is a deterministic test that enqueues at least three tasks, runs the agentic mode with `workers=1`, and proves that one boot call plus at least two resumed calls were used while task completion, output paths, and retry boundaries stayed correct.

## Milestone 3: Persist session truth in SQLite and session artifacts

At the end of this milestone, “session reuse” will no longer be an inference from stdout or rollout files. SQLite and the run output directory will contain first-class records that explain which worker session handled which tasks and how that session ended.

Extend `src/codex_farm/db.py` with a new table for session-level truth. Use a name such as `worker_sessions`. Keep the existing `tasks` table. Do not try to replace it. The new table should minimally persist:

    session_row_id
    run_id
    worker_id
    runtime_mode
    resume_key
    thread_id
    status
    started_at
    finished_at
    turn_count
    task_count
    last_task_id
    end_reason
    codex_home_path
    cd_dir

Add additive task linkage fields so each task can point back to the session that handled it. The simplest shape is:

    tasks.session_row_id
    tasks.session_task_index
    tasks.session_turn_index
    tasks.fresh_session_started

The worker session runtime should update these fields only after lease ownership is secure and only when the task outcome is known. A retry that discards session state should still preserve truthful attribution for the failed attempt.

Also add an on-disk session artifact under the run output tree, for example:

    <run output>/.codex-farm-sessions/<session_row_id>/session.json
    <run output>/.codex-farm-sessions/<session_row_id>/turns/<n>.trace.json

`session.json` should be a compact summary for humans and tools: session status, runtime mode, resume key, thread id, tasks handled, turn count, and end reason. Keep it additive and reproducible.

Acceptance for this milestone is a run-level inspection test that verifies both SQLite and on-disk artifacts tell the same story for a session that processed multiple tasks and then finished normally, plus a second test where a resumed-turn failure triggers a reset and preserves honest task/session linkage.

## Milestone 4: Extend JSON output, progress events, telemetry, and autotune for session-aware runs

At the end of this milestone, external callers will not have to scrape trace files or invent their own audit labels to understand what happened. All machine-facing surfaces that currently talk about workers and tasks will gain additive session-aware fields.

Start with `src/codex_farm/cli.py` and the progress helpers. `process --json` should keep the existing fields and add:

    runtime_mode
    effective_workers
    session_count
    fresh_session_count
    tasks_per_session_summary
    session_turn_count_total
    session_failures

`process --progress-events` should keep the current event names and add session-aware counters, for example:

    active_sessions
    sessions_started
    sessions_finished
    current_session_task_count

Keep stdout cleanliness unchanged. The final JSON object must still be the only stdout payload.

Then update `src/codex_farm/codex_exec.py`, `src/codex_farm/telemetry_report.py`, and `src/codex_farm/autotune.py`. The current telemetry row model is one row per Codex subprocess call, and that should remain true because it is still useful. What must be added is a session-summary layer that aggregates those rows honestly for agentic runs. The easiest path is:

1. Keep per-turn telemetry rows in `codex_exec_activity.csv`.
2. Add additive per-turn fields such as `runtime_mode`, `session_row_id`, `resume_key`, `session_task_index`, and `session_turn_index`.
3. Write a session summary JSON or CSV artifact from the session runtime.
4. Teach `run telemetry --json` and `run autotune --json` to read both per-turn rows and session summaries when `runtime_mode == "structured_loop_agentic_v1"`.

Autotune should not guess session advice from worker counts alone anymore. Add at least these session-aware recommendations:

- boot cost dominates total cost
- sessions reset too often
- sessions run too long before drift/failure
- task budget per session is too low or too high

Acceptance for this milestone is an integration test that runs both runtime modes and proves that classic runs still produce valid telemetry reports while agentic runs add session-aware counters and recommendations without breaking existing JSON consumers.

## Milestone 5: Documentation, caller contracts, and end-to-end validation

At the end of this milestone, the code change will be explained well enough that another contributor or external caller can use it without reverse-engineering the implementation.

Update the docs that own the changed seams:

- `docs/01-cli-and-command-contracts/01-cli-and-command-contracts_readme.md` for `--runtime-mode`, `effective_workers`, and `process --json` additions.
- `docs/03-run-planning-and-queue-state/03-run-planning-and-queue-state_readme.md` for run config and new session-related SQLite fields.
- `docs/04-worker-execution-and-retries/04-worker-execution-and-retries_readme.md` for the new session runtime path and reset/retry rules.
- `docs/05-codex-exec-and-schema-gate/05-codex-exec-and-schema-gate_readme.md` for the split between one-shot upstream schema enforcement and agentic local follow-up-turn validation.
- `docs/07-analytics/07-analytics_readme.md` for session-aware telemetry/report semantics.
- `docs/08-external-program-reference/README.md`, `progress-contracts.md`, and `telemetry-contracts.md` for the caller-facing runtime-mode and session metrics contract.

Add one short note under `src/codex_farm/README.md` or the most relevant docs chunk explaining that CodexFarm now supports both classic per-task execution and session-aware agentic execution, and that the mode is explicit in run config and JSON output.

Acceptance for this milestone is a clean focused test run plus one manual smoke run that uses a small input directory with `structured_loop_agentic_v1`, confirms more than one task completed in one session, and shows matching counts in `process --json`, progress events, session artifacts, and telemetry report output.

## Plan of Work

Begin with honesty, not concurrency. Add `runtime_mode`, `effective_workers`, and frozen asset persistence first so the external contract becomes explicit before any runtime internals change. At the same time, build the low-level session transport helpers in `codex_exec.py` and prove them with one manual real-Codex smoke. That proof should answer two questions before broader implementation starts: what exact string must be stored as the resume key, and what local parsing/validation rule is needed because resumed turns lack `--output-schema`.

Revision note (2026-03-17 / Codex): updated the plan after implementation so it now records the shipped runtime-mode/session behavior, the live `exec resume` proof that `thread_id` currently works as `resume_key`, and the remaining upstream caveat around resume-time model control.

Once the low-level transport is proven, build the session runner as a parallel path rather than mutating the current `worker_loop(...)` into a confusing hybrid. Keep classic mode intact and route only `structured_loop_agentic_v1` into the new module. After the new runner can process multiple tasks in one session, persist session truth in SQLite and on disk, then expand the machine-facing outputs and analytics.

Only after the runtime is producing honest session facts should you update the caller docs and autotune. That order matters because the docs and analytics should describe real fields and real reset behavior, not aspirational labels.

## Concrete Steps

Work from the repository root:

    cd /home/mcnal/projects/shared/CodexFarm

Before running Python tests, use the project-local virtual environment:

    test -d .venv || python3 -m venv .venv
    . .venv/bin/activate
    python -m pip install -e '.[dev]'

If `pip` is missing inside `.venv`, bootstrap it inside the virtual environment first, then install the dev extras.

Implement Milestone 1 by editing:

    src/codex_farm/cli.py
    src/codex_farm/run_assets.py
    src/codex_farm/codex_exec.py
    tests/test_cli_integration_contracts.py
    tests/test_run_assets.py
    tests/test_codex_exec.py

The manual smoke for Milestone 1 should use a disposable prompt and a small schema, run one boot call plus one resumed call, and record the observed resume behavior in this plan’s `Surprises & Discoveries` section.

Implement Milestone 2 by adding and editing:

    src/codex_farm/session_runtime.py
    src/codex_farm/worker.py
    src/codex_farm/run_assets.py
    tests/test_worker.py
    tests/test_process_smoke.py
    tests/test_fake_codex_pipeline_pack_demo.py

Implement Milestone 3 by editing:

    src/codex_farm/db.py
    src/codex_farm/cli.py
    src/codex_farm/session_runtime.py
    tests/test_db.py
    tests/test_worker.py

Implement Milestone 4 by editing:

    src/codex_farm/codex_exec.py
    src/codex_farm/telemetry_report.py
    src/codex_farm/autotune.py
    src/codex_farm/cli.py
    tests/test_telemetry_report.py
    tests/test_cli_integration_contracts.py

Implement Milestone 5 by updating:

    docs/01-cli-and-command-contracts/01-cli-and-command-contracts_readme.md
    docs/03-run-planning-and-queue-state/03-run-planning-and-queue-state_readme.md
    docs/04-worker-execution-and-retries/04-worker-execution-and-retries_readme.md
    docs/05-codex-exec-and-schema-gate/05-codex-exec-and-schema-gate_readme.md
    docs/07-analytics/07-analytics_readme.md
    docs/08-external-program-reference/README.md
    docs/08-external-program-reference/progress-contracts.md
    docs/08-external-program-reference/telemetry-contracts.md

## Validation and Acceptance

The change is not complete until all of the following are true.

Run the focused automated tests from the repo root with the virtual environment active:

    pytest \
      tests/test_codex_exec.py \
      tests/test_worker.py \
      tests/test_db.py \
      tests/test_process_smoke.py \
      tests/test_cli_integration_contracts.py \
      tests/test_telemetry_report.py

Expected result: all selected tests pass, and at least one new test clearly fails before the implementation and passes after it. The most important new behaviors to assert are:

1. `process --json` returns `runtime_mode` and `effective_workers`.
2. `structured_loop_agentic_v1` rejects `--workers > 1`.
3. one session boot plus multiple resumed turns can process multiple queued tasks.
4. agentic follow-up turns are accepted only after local JSON extraction plus schema validation.
5. task retries and session resets preserve honest attribution.
6. session rows and session artifacts agree on counts and end reason.
7. classic mode telemetry still works unchanged enough for existing tests and reports.
8. agentic mode adds session-aware telemetry/report fields without breaking classic consumers.
9. a recipe-style run that uses the dedicated recipe `CODEX_HOME` keeps that home and scratch execution context across both boot and resumed turns.

Then run one manual end-to-end smoke using a tiny input directory and `process --json --progress-events`. The exact pipeline can be a fake-Codex test pack or a low-cost real Codex pack, but the observed result must show:

- more than one task completed,
- `fresh_session_count == 1`,
- `session_count == 1`,
- at least one resumed turn beyond the boot turn,
- matching session attribution in run JSON, progress output, and on-disk session summary.

## Idempotence and Recovery

All schema and docs changes in this plan are additive. If implementation stops halfway, the safe recovery path is to keep the new code behind `runtime_mode == "structured_loop_agentic_v1"` and leave `classic_task_farm_v1` untouched. Do not partially replace the legacy worker loop.

If the transport proof shows that the local Codex CLI cannot yet support a reliable headless resume identifier, stop after Milestone 1, leave the runtime-mode honesty fields in place, and record the blocker in `Decision Log` plus `Outcomes & Retrospective`. That fallback state is still valuable because it prevents external callers from overclaiming reuse.

If a migrated SQLite schema needs to be retried during development, write migrations so they are safe to rerun or safely no-op when the target table/columns already exist. Do not require destructive resets of existing run data.

## Artifacts and Notes

Important evidence to capture while implementing:

- a short snippet of local `codex exec --help` / `codex exec resume --help` behavior if the upstream CLI changes during implementation,
- one manual boot-plus-resume transcript showing the real resume key shape,
- one example `process --json` payload for classic mode and one for agentic mode,
- one session summary artifact example from `.codex-farm-sessions/`,
- one telemetry-report excerpt that shows the new session-aware counters.

Keep these snippets short and add only the parts that prove the behavior.

## Interfaces and Dependencies

Be explicit about the interfaces that must exist at the end of the work.

In `src/codex_farm/codex_exec.py`, define a richer turn result and two low-level helpers instead of forcing all session work through the old one-shot result:

    @dataclass(frozen=True)
    class CodexTurnResult:
        ok: bool
        exit_code: int
        stderr_tail: str
        stdout_tail: str
        output_text: str
        thread_id: str | None
        resume_key: str | None
        codex_event_count: int
        codex_event_types: tuple[str, ...]

    def start_codex_session(...) -> CodexTurnResult: ...
    def resume_codex_session(...) -> CodexTurnResult: ...

The existing `run_codex_exec(...)` can remain as the classic one-shot helper or become a thin wrapper over `start_codex_session(...)` plus the current output-schema acceptance path.

In `src/codex_farm/session_runtime.py`, define the session-oriented execution seam:

    def run_agentic_session_worker(...) -> int:
        ...

This function should mirror the outer worker exit-code contract from `worker_loop(...)`, but it owns session lifecycle, reset rules, and session artifact writes.

In `src/codex_farm/db.py`, define explicit session persistence helpers rather than sprinkling ad hoc SQL through runtime code:

    def create_worker_session(...)
    def update_worker_session(...)
    def attach_task_to_session(...)

In `src/codex_farm/run_assets.py`, freeze all knobs that change session semantics, including runtime mode, boot/task prompt templates or prompt wrapper contract, and reset budgets.

When this plan is revised later, add a short note at the end explaining what changed and why.

Revision note: 2026-03-17. Rewrote the original audit memo into a real ExecPlan, anchored the design on `codex exec` plus `codex exec resume`, and added an explicit first milestone for transport proof because resumed turns do not expose per-turn `--cd` or `--output-schema`.
