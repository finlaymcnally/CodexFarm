"""Run lifecycle policy helpers shared by DB/CLI/worker code."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RunControlState(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"


RUN_EFFECTIVE_STATUSES = {"queued", "running", "paused", "done", "error", "canceled"}


@dataclass(frozen=True)
class RunCounts:
    queued: int
    running: int
    done: int
    error: int
    canceled: int
    total: int


def coerce_control_state(value: object) -> RunControlState:
    if isinstance(value, RunControlState):
        return value
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned:
            try:
                return RunControlState(cleaned)
            except ValueError:
                pass
    return RunControlState.ACTIVE


def derive_effective_status(control_state: RunControlState, counts: RunCounts) -> str:
    """Derive public run status from operator control state and task counts."""
    if counts.total == 0:
        return "queued"

    if control_state is RunControlState.PAUSED and counts.queued > 0 and counts.running == 0:
        return "paused"

    if (
        control_state in {RunControlState.CANCEL_REQUESTED, RunControlState.CANCELED}
        and counts.queued == 0
        and counts.running == 0
    ):
        return "canceled"

    if counts.queued == counts.total:
        return "queued"

    if counts.queued == 0 and counts.running == 0:
        return "error" if counts.error > 0 else "done"

    return "running"
