from codex_farm.rate_limit_policy import (
    RunThrottleState,
    apply_rate_limit,
    apply_success,
    should_give_up,
)


def test_apply_rate_limit_honors_retry_hint() -> None:
    state = apply_rate_limit(
        None,
        run_id="run-1",
        desired_concurrency=8,
        now=100.0,
        retry_after_seconds=42,
    )
    assert state.cooldown_until == 142.0
    assert state.last_cooldown_seconds == 42
    assert state.concurrency_limit == 4
    assert state.consecutive_rate_limits == 1


def test_apply_rate_limit_uses_exponential_backoff_without_hint() -> None:
    first = apply_rate_limit(
        None,
        run_id="run-1",
        desired_concurrency=8,
        now=100.0,
        retry_after_seconds=None,
        base_cooldown_seconds=15,
        max_cooldown_seconds=300,
    )
    second = apply_rate_limit(
        first,
        run_id="run-1",
        desired_concurrency=8,
        now=120.0,
        retry_after_seconds=None,
        base_cooldown_seconds=15,
        max_cooldown_seconds=300,
    )
    assert first.last_cooldown_seconds == 15
    assert second.last_cooldown_seconds == 30
    assert second.cooldown_until == 150.0


def test_apply_rate_limit_never_reduces_concurrency_below_one() -> None:
    existing = RunThrottleState(
        run_id="run-1",
        desired_concurrency=1,
        concurrency_limit=1,
        cooldown_until=None,
        last_cooldown_seconds=15,
        consecutive_rate_limits=2,
        success_streak=0,
        last_rate_limit_error=None,
    )
    next_state = apply_rate_limit(
        existing,
        run_id="run-1",
        desired_concurrency=1,
        now=200.0,
        retry_after_seconds=None,
    )
    assert next_state.concurrency_limit == 1


def test_apply_success_does_not_recover_during_active_cooldown() -> None:
    state = RunThrottleState(
        run_id="run-1",
        desired_concurrency=6,
        concurrency_limit=2,
        cooldown_until=500.0,
        last_cooldown_seconds=30,
        consecutive_rate_limits=3,
        success_streak=0,
        last_rate_limit_error="429",
    )
    recovered = apply_success(state, now=450.0, recovery_success_threshold=3)
    assert recovered == state


def test_apply_success_recovers_one_step_after_threshold() -> None:
    state = RunThrottleState(
        run_id="run-1",
        desired_concurrency=5,
        concurrency_limit=2,
        cooldown_until=100.0,
        last_cooldown_seconds=30,
        consecutive_rate_limits=2,
        success_streak=2,
        last_rate_limit_error="429",
    )
    recovered = apply_success(state, now=120.0, recovery_success_threshold=3)
    assert recovered is not None
    assert recovered.cooldown_until is None
    assert recovered.concurrency_limit == 3
    assert recovered.success_streak == 0
    assert recovered.consecutive_rate_limits == 0


def test_should_give_up_after_consecutive_threshold() -> None:
    state = RunThrottleState(
        run_id="run-1",
        desired_concurrency=5,
        concurrency_limit=1,
        cooldown_until=100.0,
        last_cooldown_seconds=60,
        consecutive_rate_limits=6,
        success_streak=0,
        last_rate_limit_error="429",
    )
    assert should_give_up(state, max_consecutive_rate_limits=6) is True
