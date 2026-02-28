"""Pure adaptive policy helpers for provider rate-limit recovery."""

from __future__ import annotations

from dataclasses import dataclass
import math


DEFAULT_BASE_COOLDOWN_SECONDS = 15
DEFAULT_MAX_COOLDOWN_SECONDS = 300
DEFAULT_RECOVERY_SUCCESS_THRESHOLD = 3
DEFAULT_MAX_CONSECUTIVE_RATE_LIMITS = 6


@dataclass(frozen=True)
class RunThrottleState:
    """Mutable runtime throttle state materialized in SQLite per run."""

    run_id: str
    desired_concurrency: int
    concurrency_limit: int
    cooldown_until: float | None
    last_cooldown_seconds: int
    consecutive_rate_limits: int
    success_streak: int
    last_rate_limit_error: str | None


def _clamp_cooldown(value: int, *, min_seconds: int, max_seconds: int) -> int:
    return max(min_seconds, min(int(value), max_seconds))


def is_cooldown_active(state: RunThrottleState | None, *, now: float) -> bool:
    if state is None or state.cooldown_until is None:
        return False
    return now < state.cooldown_until


def remaining_cooldown_seconds(state: RunThrottleState | None, *, now: float) -> float:
    if state is None or state.cooldown_until is None:
        return 0.0
    return max(0.0, state.cooldown_until - now)


def apply_rate_limit(
    state: RunThrottleState | None,
    *,
    run_id: str,
    desired_concurrency: int,
    now: float,
    retry_after_seconds: int | None,
    base_cooldown_seconds: int = DEFAULT_BASE_COOLDOWN_SECONDS,
    max_cooldown_seconds: int = DEFAULT_MAX_COOLDOWN_SECONDS,
) -> RunThrottleState:
    """Return the next throttle state after a detected provider rate limit."""
    desired = max(1, int(desired_concurrency))
    current_limit = desired if state is None else max(1, int(state.concurrency_limit))

    if retry_after_seconds is not None and retry_after_seconds > 0:
        cooldown_seconds = _clamp_cooldown(
            retry_after_seconds,
            min_seconds=1,
            max_seconds=max_cooldown_seconds,
        )
    else:
        previous = base_cooldown_seconds
        if state is not None and state.last_cooldown_seconds > 0:
            previous = state.last_cooldown_seconds
        cooldown_seconds = _clamp_cooldown(
            max(base_cooldown_seconds, previous * 2 if state is not None else previous),
            min_seconds=base_cooldown_seconds,
            max_seconds=max_cooldown_seconds,
        )

    return RunThrottleState(
        run_id=run_id,
        desired_concurrency=desired,
        concurrency_limit=max(1, int(math.ceil(current_limit / 2))),
        cooldown_until=now + float(cooldown_seconds),
        last_cooldown_seconds=cooldown_seconds,
        consecutive_rate_limits=1 if state is None else int(state.consecutive_rate_limits) + 1,
        success_streak=0,
        last_rate_limit_error=state.last_rate_limit_error if state is not None else None,
    )


def apply_success(
    state: RunThrottleState | None,
    *,
    now: float,
    recovery_success_threshold: int = DEFAULT_RECOVERY_SUCCESS_THRESHOLD,
) -> RunThrottleState | None:
    """Return updated state after a healthy success, if throttling is active."""
    if state is None:
        return None
    threshold = max(1, int(recovery_success_threshold))

    if is_cooldown_active(state, now=now):
        return state

    cooldown_until = state.cooldown_until
    if cooldown_until is not None and now >= cooldown_until:
        cooldown_until = None

    next_success_streak = max(0, int(state.success_streak)) + 1
    next_limit = max(1, int(state.concurrency_limit))
    if next_success_streak >= threshold:
        next_limit = min(max(1, int(state.desired_concurrency)), next_limit + 1)
        next_success_streak = 0

    return RunThrottleState(
        run_id=state.run_id,
        desired_concurrency=max(1, int(state.desired_concurrency)),
        concurrency_limit=next_limit,
        cooldown_until=cooldown_until,
        last_cooldown_seconds=max(0, int(state.last_cooldown_seconds)),
        consecutive_rate_limits=0,
        success_streak=next_success_streak,
        last_rate_limit_error=state.last_rate_limit_error,
    )


def should_give_up(
    state: RunThrottleState,
    *,
    max_consecutive_rate_limits: int = DEFAULT_MAX_CONSECUTIVE_RATE_LIMITS,
) -> bool:
    threshold = max(1, int(max_consecutive_rate_limits))
    return int(state.consecutive_rate_limits) >= threshold
