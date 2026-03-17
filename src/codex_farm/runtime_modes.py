"""Shared runtime-mode contracts for classic and session-aware execution."""

from __future__ import annotations


CLASSIC_TASK_FARM_V1 = "classic_task_farm_v1"
STRUCTURED_LOOP_AGENTIC_V1 = "structured_loop_agentic_v1"
RUNTIME_MODE_VALUES = (
    CLASSIC_TASK_FARM_V1,
    STRUCTURED_LOOP_AGENTIC_V1,
)
DEFAULT_RUNTIME_MODE = CLASSIC_TASK_FARM_V1
DEFAULT_CLASSIC_WORKERS = 8
DEFAULT_AGENTIC_WORKERS = 1
DEFAULT_SESSION_TASK_BUDGET = 25
DEFAULT_MAX_TURNS_PER_TASK = 1
DEFAULT_SESSION_RESET_ON_ERROR = True
SESSION_BOOTSTRAP_PROMPT = (
    "You are operating inside a persistent CodexFarm worker session.\n"
    "You will receive a sequence of independent tasks from CodexFarm.\n"
    "Treat each task turn as isolated work even though the conversation persists.\n"
    "For every task turn, respond with only one JSON object that satisfies the task schema.\n"
    "Do not include markdown fences, prose, or commentary outside the JSON object."
)
SESSION_TASK_TURN_TEMPLATE = (
    "CodexFarm task turn.\n"
    "Task ID: {{TASK_ID}}\n"
    "Session task index: {{SESSION_TASK_INDEX}}\n"
    "Return only the final JSON object for this task.\n"
    "\n"
    "<<CODEX_FARM_TASK_PROMPT_START>>\n"
    "{{TASK_PROMPT}}\n"
    "<<CODEX_FARM_TASK_PROMPT_END>>"
)


def normalize_runtime_mode(value: str | None) -> str:
    if value is None:
        return DEFAULT_RUNTIME_MODE
    normalized = value.strip()
    if normalized in RUNTIME_MODE_VALUES:
        return normalized
    allowed = ", ".join(RUNTIME_MODE_VALUES)
    raise ValueError(f"runtime_mode must be one of: {allowed}")


def is_session_runtime(runtime_mode: str) -> bool:
    return normalize_runtime_mode(runtime_mode) == STRUCTURED_LOOP_AGENTIC_V1


def resolve_effective_workers(
    *,
    runtime_mode: str,
    requested_workers: int | None,
) -> int:
    normalized_mode = normalize_runtime_mode(runtime_mode)
    if requested_workers is not None and requested_workers < 1:
        raise ValueError("workers must be >= 1")
    if normalized_mode == STRUCTURED_LOOP_AGENTIC_V1:
        if requested_workers is None:
            return DEFAULT_AGENTIC_WORKERS
        if requested_workers > 1:
            raise ValueError(
                "structured_loop_agentic_v1 currently requires --workers=1 because one worker maps to one persisted Codex session."
            )
        return requested_workers
    if requested_workers is None:
        return DEFAULT_CLASSIC_WORKERS
    return requested_workers


def default_session_runtime_config() -> dict[str, object]:
    return {
        "session_task_budget": DEFAULT_SESSION_TASK_BUDGET,
        "max_turns_per_task": DEFAULT_MAX_TURNS_PER_TASK,
        "session_reset_on_error": DEFAULT_SESSION_RESET_ON_ERROR,
    }


def session_bootstrap_prompt() -> str:
    return SESSION_BOOTSTRAP_PROMPT


def session_task_turn_template() -> str:
    return SESSION_TASK_TURN_TEMPLATE
