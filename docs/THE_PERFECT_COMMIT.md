---
summary: "Rules for coding and git in this project"
read_when:
  - When editing any code
---

## Perfect Commits (required protocol)

We follow the **Perfect Commit** protocol for any non-trivial change — even if this is a solo hobby project and you’re just using git locally (or simply treating “a commit” as “the change you’re about to apply”). :contentReference[oaicite:0]{index=0}

A perfect commit is an **atomic state transition**: once it lands, the project is in a coherent, verified state; if reverted, it returns to the prior coherent state.

**Reference**

* The canonical rules live at: `docs/THE_PERFECT_COMMIT.md` (aka “The Perfect Commit”).
* When starting work, read it and follow it. When unsure, default to it.

**When to use it**
Use the Perfect Commit protocol when *any* of the following are true:

* The change alters externally observable behavior (UI/API/CLI/config/semantics/perf)
* The change fixes a bug
* The change touches more than one file
* The change is more than a trivial typo/comment/formatting tweak
* You are unsure whether the change is safe to revert

For **trivial edits** (typos, comment-only, formatting-only): one small commit is enough; tests/docs are optional.

**How to apply it (agent instructions)**
For each non-trivial task, produce exactly one “perfect commit” (or a small series of perfect commits if the work is truly separable). Each commit must include:

1. **Scope**: one coherent behavior change with explicit boundaries (no scope creep)
2. **Implement**: minimal diff; avoid drive-by refactors and noisy churn
3. **Prove**: verification that **fails before / passes after**

   * Prefer automated tests
   * If tests aren’t practical, provide deterministic manual steps + expected outputs
4. **Update docs conditionally**: update docs/comments **only** where the change creates a gap between docs and reality
5. **Package**: short commit message + task file link; keep your “main” state green (run your standard checks)

**Task spec (“task file”) is the contract**
If a task doesn’t exist, create a lightweight task spec **file** in `docs/tasks/`:

**Naming convention**
* `YYYY-MM-DD_HH.MM.SS - short-title.md`
  * Example: `2026-01-21_14.30.22 - add save slot ui.md`

Task file must include:

* Problem statement (1–3 lines)
* Acceptance criteria
* Verification steps (commands/steps)
* Evidence (test output or before/after)
* Constraints/gotchas + brief decision (approach + why)
* Rollback / compensation plan (only if the change is risky or spans multiple systems)

**Guardrails**

* Do not widen scope mid-flight: open a follow-up task file instead
* Do not merge exploratory history (no WIP/breadcrumb commits)
* Do not write long commit message essays; code + tests are the explanation
* Prefer **one workspace/branch per task/agent**; use the **task file + diff + proof** as the shared coordination surface

---

# The Perfect Commit (Agent Protocol)

A **perfect commit** is the smallest **reviewable, reversible, run-safe** unit of progress.

It is also an **atomic state transition**: **all required changes land together, or none do**—no “half-finished” states, no missing proofs, no follow-up-required-to-be-green commits.

It is defined by **outputs**, not effort:

* **Implementation**: minimal diff for one coherent change
* **Proof**: tests or deterministic verification that **fails before** and **passes after**
* **Docs (conditional)**: only where the change creates a gap between docs and reality
* **Tracking link**: the `docs/13-tasks/...` file that acts as the single context anchor

---

## Non-negotiables

### Must

* **One thing per commit.** The change must be explainable, testable, and revertable in isolation.
* **Be atomic.** Apply the commit and you get a coherent, verified state; revert it and you’re back to a coherent, verified state.
* **Keep your baseline green.** If it’s on your main line of work, it must pass your standard checks.
* **Prove causality.** New/updated tests must **fail on the baseline** and **pass with the change**.
* **Anchor to a task file.** Link the `docs/13-tasks/...` file that states what “done” means and how to verify it.
* **No scope creep.** If you find adjacent work, open a follow-up task.

### Never

* Merge “exploration history” into your mainline (no breadcrumb commits, no “wip”, no diary).
* Use the commit message to explain implementation details or narrate your journey.
* Make noisy churn (mass renames/reformatting/drive-by refactors) unless that churn is the goal.
* Pretend multi-system work is magically all-or-nothing: if the change spans systems, plan it (see “multi-system” guidance below).

---

## The protocol (5 steps)

### 1) Scope — define the single behavior change and its boundaries

**Must**

* Define the *one coherent behavior change*.
* Define explicit boundaries (what you will not change).
* Identify the verification surface (tests, CLI output, API behavior, UI state, perf metric).
* Identify the **state boundaries** you touch (code/config/schema/external systems) and decide whether this can be **one atomic project change** or must be staged as a **saga**.

**Should**

* Prefer the smallest externally-observable change that moves the task forward.
* Keep the diff shaped for one-sitting review (even if the “reviewer” is future-you).

**May**

* Split into multiple commits if the work is truly multiple independent changes (each must meet this doc).
* If staging is required, model it explicitly as sequential, safe steps (each step must be reviewable and reversible).

**Never**

* Widen the scope mid-flight. Create a follow-up task file instead.

---

### 2) Implement — minimal diff to achieve the behavior change

**Must**

* Make the smallest change that satisfies the task spec.
* Keep edits localized (touch the fewest files/lines that make sense).
* Avoid collateral rewrites that harm blame/bisect.
* Keep the commit **self-contained**: include whatever is required so the post-commit state is coherent (code + config + tests + any necessary compatibility glue).

**Should**

* Prefer obvious code over clever code.
* Keep public interfaces stable unless the task requires changing them.

**May**

* Do mechanical refactors *only* if they are required for correctness or to enable the change.

**Never**

* Mix refactors + behavior changes unless separating them would be harder to review or less safe.

---

### 3) Prove — tests or deterministic verification (fail-before / pass-after)

**Must**

* Add/modify proof that demonstrates the new behavior:

  * **Tests** are preferred.
  * If tests aren’t practical, provide **deterministic repro steps** + expected outputs.
* **Confirm fail-before**:

  * Run the proof against the baseline (before your implementation) and verify it fails.
  * Then run again with your implementation and verify it passes.
* Record the exact command(s) used to verify (in the task file).

**Should**

* Make the proof as direct as possible (avoid tests that can pass even if the code is wrong).
* Keep proof scoped to the “one thing.”
* If you touch multiple state boundaries (e.g., schema + app), include a deterministic check for each boundary you changed.

**May**

* Include a minimal benchmark if the change is performance-critical.
* If rollback is non-trivial (migrations/flags), include a *minimal* rollback/compensation verification when feasible.

**Never**

* Write tests that would pass regardless of the implementation (false positives).
* Rely on “it seems to work” when a deterministic check is possible.

---

### 4) Update — docs/comments only when behavior/API changed

**Decision rule**

* If **externally observable behavior changes** (API/CLI/config/UX/semantics/errors/performance characteristics): **update docs**.
* If the change is **internal** (refactor, tests, tooling, build, cleanup): docs are usually **not required**.

**Must**

* When docs are required, update the **smallest relevant spot** (often 1–3 lines).
* Keep docs in the **same project** and, when applicable, in the **same commit** as the code that changed reality.

**Should**

* Prefer updating an existing doc near the interface over creating new docs.
* Update comments only when they would otherwise become incorrect or misleading.

**May**

* Add “documentation tests” only for critical invariants where drift is expensive.

**Never**

* Add speculative docs (“might work like this someday”) or long narrative explanations.

---

### 5) Package — commit message, task link, and green baseline

**Must**

* Ensure your standard checks pass (format/lint/unit tests + relevant integration checks).
* Produce a single commit that includes the required outputs (Implementation, Proof, optional Docs, Task file link).

**Commit message: requirements**

* **Short and factual.**
* One-line summary + link/path to the task file (e.g., `Refs: docs/13-tasks/...`).
* No implementation essay.

**Suggested format**

* `Imperative summary`
  `Refs: docs/13-tasks/2026-01-21_14.30.22 - short-title.md`

**Should**

* Make it easy to revert (avoid bundling unrelated edits).
* Keep the task file as the place where validation steps and evidence live.

**Never**

* Put design discussions, research dumps, or motivation into the commit message.

---

## Multi-system changes: transaction vs saga

Some tasks touch more than the project (e.g., code + database + config + operational state). Treat this explicitly:

* If you can make it **one atomic change** (compat code + migration + safe defaults + verification): do that.
* If the work can’t be safely all-or-nothing across systems, model it as a **saga**:

  * Break it into staged commits/tasks where each step is safe, reviewable, and reversible.
  * Define **compensating actions** (how to undo/mitigate partial completion).
  * Put the staged plan + verification into the task file (not the commit message).

---

## The task file is the contract

Agents don’t need a diary; you need a contract and proof you can trust later.

### Must include

* **Problem statement** (1–3 lines)
* **Acceptance criteria** (what must be true after the change)
* **Verification steps** (commands or steps to observe the behavior)
* **Evidence** (test output, before/after screenshots *only if needed*, benchmark numbers if relevant)
* **Non-obvious constraints** (gotchas, tradeoffs, compatibility requirements)
* **Decision** (chosen approach + why, 1–5 lines)
* **Rollback / compensation plan** (only when needed: migrations, multi-system changes, risky changes)

### Should

* Link directly to relevant code/docs if it speeds review.
* Keep it concise; prefer bullet points.

### May

* Include a small number of external links if they are directly load-bearing.

### Never

* Timestamped narrative logging, “temporal documentation”, or breadcrumb trails of exploration.

---

## Agent guardrails (operational constraints)

* **Treat history as the audit log.** Prefer clean, reviewable commits; avoid rewriting shared history.
* **One workspace/branch per task/agent.** Don’t stack unrelated work on the same working state.
* **Use the task file + diff + proof as the coordination surface.** The changes + verification are the shared state; applying the commit is the state transition.
* **Don’t widen scope mid-flight.** Open a follow-up task for adjacent improvements.
* **Prefer the smallest reviewable diff.** Avoid drive-by refactors.
* **Run the standard checks.** Don’t assume you’ll “notice later.”
* **Demonstrate causality.** Always show fail-before / pass-after (or the closest equivalent).
* **Respect history tools.** Avoid noisy churn that breaks blame/bisect unless explicitly required.

---

## Exceptions: a compact decision tree

1. **Trivial change?** (typo, comment-only, formatting-only)
   → One commit. Tests/docs usually not required. Keep baseline green.

2. **Bug fix?**
   → Must include proof (test or deterministic repro).
   → Docs only if the bug affected user-facing behavior or documented semantics.
   → Link/create a task file if needed.

3. **Refactor (behavior-preserving)?**
   → Proof is required (existing tests + targeted additions if needed).
   → Docs usually not required unless interfaces change.

4. **Multi-system change?** (code + schema + infra + operational state)
   → Prefer a single atomic change if it can be made run-safe.
   → Otherwise stage it as a saga: small, reversible steps with explicit compensating actions + verification.

5. **Exploration/spike?**
   → Prefer asking for clarification instead of exploring blindly.
   → If a spike is necessary, keep it off your mainline and **do not merge the exploratory history**.
   → Land only a clean commit that meets the “perfect commit” outputs.

---

## Checklist (copy/paste)

* [ ] Scope is one coherent change; boundaries are explicit
* [ ] Atomic outcome: post-commit state is coherent and verified; revert is safe
* [ ] Minimal diff; no unrelated churn
* [ ] Proof exists and is scoped to the change
* [ ] Proof **fails before** and **passes after**
* [ ] Docs updated **only if** external behavior changed (and only where needed)
* [ ] Standard checks pass; baseline stays green
* [ ] Commit message is short + links the task file
```

