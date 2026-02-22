---
summary: "Worker loop behavior, lease-based claiming, retry policy, and task-level cd selection."
read_when:
  - "When changing processing order, retries, lease handling, or worker failure behavior"
---

# Scope

Owns task execution lifecycle after a task has been enqueued.

## Primary files

- `src/codex_farm/worker.py`
- `src/codex_farm/db.py` (`lease_one_task`, `mark_task_done`, `mark_task_error`, `requeue_task`)

## Why separate

This is the runtime engine boundary where correctness depends on idempotency and retry semantics.
