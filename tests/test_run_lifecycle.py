from codex_farm.run_lifecycle import RunControlState, RunCounts, derive_effective_status


def _counts(
    *,
    queued: int = 0,
    running: int = 0,
    done: int = 0,
    error: int = 0,
    canceled: int = 0,
) -> RunCounts:
    total = queued + running + done + error + canceled
    return RunCounts(
        queued=queued,
        running=running,
        done=done,
        error=error,
        canceled=canceled,
        total=total,
    )


def test_paused_effective_status_requires_queued_and_no_running() -> None:
    assert (
        derive_effective_status(RunControlState.PAUSED, _counts(queued=2))
        == "paused"
    )
    assert (
        derive_effective_status(RunControlState.PAUSED, _counts(queued=1, running=1))
        == "running"
    )
    assert (
        derive_effective_status(RunControlState.PAUSED, _counts(done=1))
        == "done"
    )


def test_cancel_requested_effective_status_becomes_canceled_when_drained() -> None:
    assert (
        derive_effective_status(
            RunControlState.CANCEL_REQUESTED,
            _counts(done=1, error=1, canceled=2),
        )
        == "canceled"
    )
    assert (
        derive_effective_status(
            RunControlState.CANCEL_REQUESTED,
            _counts(queued=1, done=1),
        )
        == "running"
    )
