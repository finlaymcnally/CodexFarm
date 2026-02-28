"""Lease heartbeat helper for long-running worker executions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading

from .db import heartbeat_task_lease, open_db


@dataclass(frozen=True)
class LeaseContext:
    db_path: Path
    task_id: str
    lease_token: str
    lease_seconds: int
    interval_seconds: float


class LeaseHeartbeatSession:
    def __init__(self, *, context: LeaseContext) -> None:
        self._context = context
        self._stop_event = threading.Event()
        self._lost_ownership_event = threading.Event()
        self._last_error_lock = threading.Lock()
        self._last_error: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"lease-heartbeat-{self._context.task_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self._context.interval_seconds * 2.0))

    @property
    def lost_ownership(self) -> bool:
        return self._lost_ownership_event.is_set()

    @property
    def last_error(self) -> str | None:
        with self._last_error_lock:
            return self._last_error

    def _set_last_error(self, error: str | None) -> None:
        with self._last_error_lock:
            self._last_error = error

    def _run(self) -> None:
        try:
            conn = open_db(self._context.db_path)
        except Exception as exc:  # pragma: no cover - exercised in integration paths.
            self._set_last_error(str(exc))
            return

        try:
            while not self._stop_event.wait(self._context.interval_seconds):
                try:
                    still_owner = heartbeat_task_lease(
                        conn,
                        task_id=self._context.task_id,
                        lease_token=self._context.lease_token,
                        lease_seconds=self._context.lease_seconds,
                    )
                except Exception as exc:  # pragma: no cover - exercised in integration paths.
                    self._set_last_error(str(exc))
                    continue
                if not still_owner:
                    self._lost_ownership_event.set()
                    break
                self._set_last_error(None)
        finally:
            conn.close()
