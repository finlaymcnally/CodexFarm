"""SQLite queue and run metadata helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .rate_limit_policy import RunThrottleState
from .run_lifecycle import RunControlState, RunCounts, coerce_control_state, derive_effective_status


RUN_STATUSES = {"queued", "running", "paused", "done", "error", "canceled"}
RUN_CONTROL_STATES = {state.value for state in RunControlState}
TASK_STATUSES = {"queued", "running", "done", "error", "canceled"}
HEADS_UP_OUTCOMES = {"done", "error"}
LEASE_EXPIRY_GRACE_SECONDS = 2.0


@dataclass(frozen=True)
class PlannedTaskRow:
    input_path: str
    input_hash: str
    rel_output_path: str
    status: str
    output_path: str | None = None
    reused_from_run_id: str | None = None
    reused_from_task_id: str | None = None


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _now_epoch() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel_output_path(*, input_file: Path, input_root: Path, output_ext: str) -> Path:
    rel = input_file.relative_to(input_root)
    return rel.with_suffix(output_ext)


def open_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def _ensure_column(
    conn: sqlite3.Connection,
    *,
    table_name: str,
    column_name: str,
    column_ddl: str,
) -> bool:
    if column_name in _table_columns(conn, table_name):
        return False
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_ddl}")
    return True


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            pipeline_id TEXT NOT NULL,
            execution_fingerprint TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
            control_state TEXT NOT NULL DEFAULT 'active',
            input_dir TEXT NOT NULL,
            glob_pattern TEXT NOT NULL,
            output_dir TEXT NOT NULL,
            config_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            input_path TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            rel_output_path TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            execution_attempts INTEGER NOT NULL DEFAULT 0,
            rate_limit_count INTEGER NOT NULL DEFAULT 0,
            leased_by TEXT,
            lease_until REAL,
            lease_token TEXT,
            last_heartbeat_at TEXT,
            error TEXT,
            output_path TEXT,
            reused_from_run_id TEXT,
            reused_from_task_id TEXT,
            session_row_id INTEGER REFERENCES worker_sessions(session_row_id) ON DELETE SET NULL,
            session_task_index INTEGER,
            session_turn_index INTEGER,
            fresh_session_started INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_run_status ON tasks(run_id, status);
        CREATE INDEX IF NOT EXISTS idx_tasks_lease_until ON tasks(lease_until);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_run_input ON tasks(run_id, input_path);

        CREATE TABLE IF NOT EXISTS run_throttle_state (
            run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
            desired_concurrency INTEGER NOT NULL,
            concurrency_limit INTEGER NOT NULL,
            cooldown_until REAL,
            last_cooldown_seconds INTEGER NOT NULL DEFAULT 0,
            consecutive_rate_limits INTEGER NOT NULL DEFAULT 0,
            success_streak INTEGER NOT NULL DEFAULT 0,
            last_rate_limit_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS worker_sessions (
            session_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            worker_id TEXT NOT NULL,
            runtime_mode TEXT NOT NULL,
            resume_key TEXT,
            thread_id TEXT,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            turn_count INTEGER NOT NULL DEFAULT 0,
            task_count INTEGER NOT NULL DEFAULT 0,
            last_task_id TEXT,
            end_reason TEXT,
            codex_home_path TEXT,
            cd_dir TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_worker_sessions_run_started
            ON worker_sessions(run_id, started_at, session_row_id);

        CREATE TABLE IF NOT EXISTS task_forensics (
            forensics_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            run_id TEXT,
            task_id TEXT,
            pipeline_id TEXT NOT NULL,
            attempt_index INTEGER,
            terminal INTEGER NOT NULL,
            failure_stage TEXT NOT NULL,
            failure_category TEXT NOT NULL,
            input_path TEXT,
            rel_output_path TEXT,
            error_summary TEXT NOT NULL,
            bundle_dir TEXT NOT NULL,
            metadata_path TEXT NOT NULL,
            raw_output_path TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_task_forensics_run_created
            ON task_forensics(run_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_task_forensics_run_task_created
            ON task_forensics(run_id, task_id, created_at);

        CREATE TABLE IF NOT EXISTS heads_up_tips (
            tip_id TEXT PRIMARY KEY,
            pipeline_id TEXT NOT NULL,
            input_signature TEXT NOT NULL,
            tip_text TEXT NOT NULL,
            score REAL NOT NULL DEFAULT 0.5,
            uses INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            source_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_heads_up_tips_unique
            ON heads_up_tips(pipeline_id, input_signature, tip_text);
        CREATE INDEX IF NOT EXISTS idx_heads_up_tips_lookup
            ON heads_up_tips(pipeline_id, input_signature, score, updated_at);

        CREATE TABLE IF NOT EXISTS heads_up_tip_usage (
            usage_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
            tip_id TEXT NOT NULL REFERENCES heads_up_tips(tip_id) ON DELETE CASCADE,
            outcome TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_heads_up_tip_usage_unique
            ON heads_up_tip_usage(task_id, tip_id);
        CREATE INDEX IF NOT EXISTS idx_heads_up_tip_usage_run
            ON heads_up_tip_usage(run_id);
        """
    )
    execution_attempts_added = _ensure_column(
        conn,
        table_name="tasks",
        column_name="execution_attempts",
        column_ddl="execution_attempts INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        table_name="tasks",
        column_name="last_heartbeat_at",
        column_ddl="last_heartbeat_at TEXT",
    )
    _ensure_column(
        conn,
        table_name="runs",
        column_name="execution_fingerprint",
        column_ddl="execution_fingerprint TEXT",
    )
    _ensure_column(
        conn,
        table_name="tasks",
        column_name="reused_from_run_id",
        column_ddl="reused_from_run_id TEXT",
    )
    _ensure_column(
        conn,
        table_name="tasks",
        column_name="reused_from_task_id",
        column_ddl="reused_from_task_id TEXT",
    )
    _ensure_column(
        conn,
        table_name="runs",
        column_name="control_state",
        column_ddl="control_state TEXT NOT NULL DEFAULT 'active'",
    )
    _ensure_column(
        conn,
        table_name="tasks",
        column_name="lease_token",
        column_ddl="lease_token TEXT",
    )
    _ensure_column(
        conn,
        table_name="tasks",
        column_name="rate_limit_count",
        column_ddl="rate_limit_count INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn,
        table_name="tasks",
        column_name="session_row_id",
        column_ddl="session_row_id INTEGER REFERENCES worker_sessions(session_row_id) ON DELETE SET NULL",
    )
    _ensure_column(
        conn,
        table_name="tasks",
        column_name="session_task_index",
        column_ddl="session_task_index INTEGER",
    )
    _ensure_column(
        conn,
        table_name="tasks",
        column_name="session_turn_index",
        column_ddl="session_turn_index INTEGER",
    )
    _ensure_column(
        conn,
        table_name="tasks",
        column_name="fresh_session_started",
        column_ddl="fresh_session_started INTEGER NOT NULL DEFAULT 0",
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_runs_pipeline_fingerprint_created
            ON runs(pipeline_id, execution_fingerprint, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_run_reused_source
            ON tasks(run_id, reused_from_task_id)
        """
    )
    if execution_attempts_added:
        conn.execute(
            """
            UPDATE tasks
            SET execution_attempts = max(0, attempts - COALESCE(rate_limit_count, 0))
            """
        )
    conn.commit()


def create_run(
    conn: sqlite3.Connection,
    *,
    pipeline_id: str,
    input_dir: str,
    glob: str,
    output_dir: str,
    config: dict,
    execution_fingerprint: str | None = None,
    run_id: str | None = None,
) -> str:
    resolved_run_id = run_id or uuid.uuid4().hex
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO runs (
            run_id, pipeline_id, created_at, updated_at, status,
            control_state, input_dir, glob_pattern, output_dir, config_json,
            execution_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            resolved_run_id,
            pipeline_id,
            now,
            now,
            "queued",
            RunControlState.ACTIVE.value,
            input_dir,
            glob,
            output_dir,
            json.dumps(config, sort_keys=True),
            execution_fingerprint,
        ),
    )
    conn.commit()
    return resolved_run_id


def enqueue_tasks_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    input_files: list[Path],
    input_root: Path,
    output_root: Path,
    output_ext: str,
) -> int:
    del output_root  # output path materialization happens at execution/reuse time.
    input_root_resolved = input_root.resolve()

    planned_tasks: list[PlannedTaskRow] = []
    for input_file in sorted(input_files):
        resolved_input = input_file.resolve()
        rel_out = _rel_output_path(
            input_file=resolved_input,
            input_root=input_root_resolved,
            output_ext=output_ext,
        )
        planned_tasks.append(
            PlannedTaskRow(
                input_path=str(resolved_input),
                input_hash=_hash_file(input_file),
                rel_output_path=rel_out.as_posix(),
                status="queued",
            )
        )

    return insert_planned_tasks_for_run(
        conn,
        run_id=run_id,
        planned_tasks=planned_tasks,
    )


def insert_planned_tasks_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    planned_tasks: list[PlannedTaskRow],
) -> int:
    now = _now_iso()
    rows: list[
        tuple[
            str,
            str,
            str,
            str,
            str,
            str,
            int,
            int,
            str | None,
            float | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            int | None,
            int | None,
            int | None,
            int,
            str,
            str,
        ]
    ] = []

    for planned in planned_tasks:
        if planned.status not in TASK_STATUSES:
            raise ValueError(f"Invalid task status: {planned.status}")
        output_path = planned.output_path
        if planned.status != "done":
            output_path = None

        rows.append(
            (
                uuid.uuid4().hex,
                run_id,
                planned.input_path,
                planned.input_hash,
                planned.rel_output_path,
                planned.status,
                0,
                0,
                None,
                None,
                None,
                None,
                output_path,
                planned.reused_from_run_id,
                planned.reused_from_task_id,
                None,
                None,
                None,
                0,
                now,
                now,
            )
        )

    if rows:
        conn.executemany(
            """
            INSERT INTO tasks (
                task_id,
                run_id,
                input_path,
                input_hash,
                rel_output_path,
                status,
                attempts,
                rate_limit_count,
                leased_by,
                lease_until,
                lease_token,
                error,
                output_path,
                reused_from_run_id,
                reused_from_task_id,
                session_row_id,
                session_task_index,
                session_turn_index,
                fresh_session_started,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    run_status_value = "done" if rows and all(row[5] == "done" for row in rows) else "queued"
    conn.execute(
        "UPDATE runs SET updated_at = ?, status = ? WHERE run_id = ?",
        (now, run_status_value, run_id),
    )
    conn.commit()
    return len(rows)


def get_run(conn: sqlite3.Connection, run_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Run not found: {run_id}")
    return dict(row)


def create_worker_session(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    worker_id: str,
    runtime_mode: str,
    status: str,
    codex_home_path: str | None,
    cd_dir: str | None,
    commit: bool = True,
) -> int:
    now = _now_iso()
    cursor = conn.execute(
        """
        INSERT INTO worker_sessions (
            run_id,
            worker_id,
            runtime_mode,
            resume_key,
            thread_id,
            status,
            started_at,
            finished_at,
            turn_count,
            task_count,
            last_task_id,
            end_reason,
            codex_home_path,
            cd_dir
        ) VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, 0, 0, NULL, NULL, ?, ?)
        """,
        (
            run_id,
            worker_id,
            runtime_mode,
            status,
            now,
            codex_home_path,
            cd_dir,
        ),
    )
    if commit:
        conn.commit()
    return int(cursor.lastrowid)


def update_worker_session(
    conn: sqlite3.Connection,
    *,
    session_row_id: int,
    resume_key: str | None = None,
    thread_id: str | None = None,
    status: str | None = None,
    turn_count: int | None = None,
    task_count: int | None = None,
    last_task_id: str | None = None,
    end_reason: str | None = None,
    finished_at: str | None = None,
    commit: bool = True,
) -> bool:
    updates: list[str] = []
    params: list[object] = []
    if resume_key is not None:
        updates.append("resume_key = ?")
        params.append(resume_key)
    if thread_id is not None:
        updates.append("thread_id = ?")
        params.append(thread_id)
    if status is not None:
        updates.append("status = ?")
        params.append(status)
    if turn_count is not None:
        updates.append("turn_count = ?")
        params.append(turn_count)
    if task_count is not None:
        updates.append("task_count = ?")
        params.append(task_count)
    if last_task_id is not None:
        updates.append("last_task_id = ?")
        params.append(last_task_id)
    if end_reason is not None:
        updates.append("end_reason = ?")
        params.append(end_reason)
    if finished_at is not None:
        updates.append("finished_at = ?")
        params.append(finished_at)
    if not updates:
        return True
    params.append(session_row_id)
    cursor = conn.execute(
        f"""
        UPDATE worker_sessions
        SET {", ".join(updates)}
        WHERE session_row_id = ?
        """,
        tuple(params),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def finish_worker_session(
    conn: sqlite3.Connection,
    *,
    session_row_id: int,
    status: str,
    end_reason: str,
    resume_key: str | None = None,
    thread_id: str | None = None,
    turn_count: int | None = None,
    task_count: int | None = None,
    last_task_id: str | None = None,
    commit: bool = True,
) -> bool:
    return update_worker_session(
        conn,
        session_row_id=session_row_id,
        resume_key=resume_key,
        thread_id=thread_id,
        status=status,
        turn_count=turn_count,
        task_count=task_count,
        last_task_id=last_task_id,
        end_reason=end_reason,
        finished_at=_now_iso(),
        commit=commit,
    )


def link_task_to_worker_session(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    session_row_id: int,
    session_task_index: int,
    session_turn_index: int,
    fresh_session_started: bool,
    lease_token: str | None = None,
    commit: bool = True,
) -> bool:
    now = _now_iso()
    where_sql, where_params = _lease_guard_predicate(task_id=task_id, lease_token=lease_token)
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET session_row_id = ?,
            session_task_index = ?,
            session_turn_index = ?,
            fresh_session_started = ?,
            updated_at = ?
        WHERE {where_sql}
        """,
        (
            session_row_id,
            session_task_index,
            session_turn_index,
            1 if fresh_session_started else 0,
            now,
            *where_params,
        ),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def list_worker_sessions_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM worker_sessions
        WHERE run_id = ?
        ORDER BY session_row_id ASC
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def summarize_worker_sessions_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> dict[str, object]:
    sessions = list_worker_sessions_for_run(conn, run_id=run_id)
    session_count = len(sessions)
    fresh_session_count = session_count
    session_turn_count_total = sum(int(row.get("turn_count") or 0) for row in sessions)
    session_failures = sum(
        1
        for row in sessions
        if str(row.get("status") or "").strip().lower() not in {"finished", "completed", "running"}
    )
    active_sessions = sum(
        1 for row in sessions if str(row.get("status") or "").strip().lower() == "running"
    )
    sessions_started = session_count
    sessions_finished = sum(1 for row in sessions if row.get("finished_at"))
    task_counts = [int(row.get("task_count") or 0) for row in sessions]
    tasks_per_session_summary = {
        "min": min(task_counts) if task_counts else 0,
        "max": max(task_counts) if task_counts else 0,
        "avg": round(sum(task_counts) / len(task_counts), 2) if task_counts else 0.0,
        "values": task_counts,
    }
    current_session_task_count = max(
        (int(row.get("task_count") or 0) for row in sessions if row.get("status") == "running"),
        default=0,
    )
    return {
        "session_count": session_count,
        "fresh_session_count": fresh_session_count,
        "session_turn_count_total": session_turn_count_total,
        "session_failures": session_failures,
        "active_sessions": active_sessions,
        "sessions_started": sessions_started,
        "sessions_finished": sessions_finished,
        "current_session_task_count": current_session_task_count,
        "tasks_per_session_summary": tasks_per_session_summary,
        "sessions": sessions,
    }


def find_latest_compatible_terminal_run(
    conn: sqlite3.Connection,
    *,
    pipeline_id: str,
    execution_fingerprint: str,
) -> dict | None:
    row = conn.execute(
        """
        SELECT *
        FROM runs
        WHERE pipeline_id = ?
          AND execution_fingerprint = ?
          AND status IN ('done', 'error')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (pipeline_id, execution_fingerprint),
    ).fetchone()
    return dict(row) if row is not None else None


def list_successful_tasks_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            task_id,
            run_id,
            input_path,
            input_hash,
            rel_output_path,
            output_path
        FROM tasks
        WHERE run_id = ?
          AND status = 'done'
        ORDER BY input_path ASC
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _as_positive_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value > 0 else default
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return default
        try:
            parsed = int(cleaned)
        except ValueError:
            return default
        return parsed if parsed > 0 else default
    return default


def _run_throttle_row_to_state(row: sqlite3.Row) -> RunThrottleState:
    cooldown_until: float | None = None
    raw_cooldown_until = row["cooldown_until"]
    if raw_cooldown_until is not None:
        cooldown_until = float(raw_cooldown_until)
    return RunThrottleState(
        run_id=str(row["run_id"]),
        desired_concurrency=max(1, int(row["desired_concurrency"])),
        concurrency_limit=max(1, int(row["concurrency_limit"])),
        cooldown_until=cooldown_until,
        last_cooldown_seconds=max(0, int(row["last_cooldown_seconds"])),
        consecutive_rate_limits=max(0, int(row["consecutive_rate_limits"])),
        success_streak=max(0, int(row["success_streak"])),
        last_rate_limit_error=(
            str(row["last_rate_limit_error"])
            if row["last_rate_limit_error"] is not None
            else None
        ),
    )


def _count_live_running_tasks(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    now_epoch: float,
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM tasks
        WHERE run_id = ?
          AND status = 'running'
          AND lease_until IS NOT NULL
          AND lease_until >= ?
        """,
        (run_id, now_epoch),
    ).fetchone()
    return int(row["count"]) if row is not None else 0


def infer_run_desired_concurrency(conn: sqlite3.Connection, *, run_id: str) -> int:
    run = get_run(conn, run_id)
    try:
        config = json.loads(str(run.get("config_json") or "{}"))
    except json.JSONDecodeError:
        config = {}
    workers = _as_positive_int(config.get("workers")) if isinstance(config, dict) else 0
    if workers > 0:
        return workers
    live_running = _count_live_running_tasks(conn, run_id=run_id, now_epoch=_now_epoch())
    return max(1, live_running)


def get_run_throttle_state(conn: sqlite3.Connection, run_id: str) -> RunThrottleState | None:
    row = conn.execute(
        """
        SELECT
            run_id,
            desired_concurrency,
            concurrency_limit,
            cooldown_until,
            last_cooldown_seconds,
            consecutive_rate_limits,
            success_streak,
            last_rate_limit_error
        FROM run_throttle_state
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return _run_throttle_row_to_state(row)


def upsert_run_throttle_state(
    conn: sqlite3.Connection,
    *,
    state: RunThrottleState,
    commit: bool = True,
) -> None:
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO run_throttle_state (
            run_id,
            desired_concurrency,
            concurrency_limit,
            cooldown_until,
            last_cooldown_seconds,
            consecutive_rate_limits,
            success_streak,
            last_rate_limit_error,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            desired_concurrency = excluded.desired_concurrency,
            concurrency_limit = excluded.concurrency_limit,
            cooldown_until = excluded.cooldown_until,
            last_cooldown_seconds = excluded.last_cooldown_seconds,
            consecutive_rate_limits = excluded.consecutive_rate_limits,
            success_streak = excluded.success_streak,
            last_rate_limit_error = excluded.last_rate_limit_error,
            updated_at = excluded.updated_at
        """,
        (
            state.run_id,
            max(1, int(state.desired_concurrency)),
            max(1, int(state.concurrency_limit)),
            state.cooldown_until,
            max(0, int(state.last_cooldown_seconds)),
            max(0, int(state.consecutive_rate_limits)),
            max(0, int(state.success_streak)),
            state.last_rate_limit_error,
            now,
            now,
        ),
    )
    if commit:
        conn.commit()


def effective_attempts(task_row: dict[str, object] | sqlite3.Row) -> int:
    attempts = int(task_row["attempts"]) if task_row["attempts"] is not None else 0
    rate_limit_count = (
        int(task_row["rate_limit_count"]) if task_row["rate_limit_count"] is not None else 0
    )
    return max(0, attempts - rate_limit_count)


def requeue_task_after_rate_limit(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_token: str | None = None,
    commit: bool = True,
) -> bool:
    now = _now_iso()
    where_sql, where_params = _lease_guard_predicate(task_id=task_id, lease_token=lease_token)
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'queued',
            leased_by = NULL,
            lease_until = NULL,
            lease_token = NULL,
            error = NULL,
            output_path = NULL,
            rate_limit_count = COALESCE(rate_limit_count, 0) + 1,
            updated_at = ?
        WHERE {where_sql}
        """,
        (now, *where_params),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def run_has_waitable_work(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    now: float,
) -> tuple[bool, float | None, str | None]:
    status = run_status(conn, run_id=run_id)
    if status["status"] in {"done", "error", "canceled"}:
        return False, None, None

    run = get_run(conn, run_id)
    control_state = str(run.get("control_state", RunControlState.ACTIVE.value))
    if control_state in {
        RunControlState.PAUSED.value,
        RunControlState.CANCEL_REQUESTED.value,
    }:
        return True, None, "control_state"

    throttle_state = get_run_throttle_state(conn, run_id)
    if throttle_state is not None and throttle_state.cooldown_until is not None:
        remaining = max(0.0, float(throttle_state.cooldown_until) - now)
        if remaining > 0:
            return True, remaining, "cooldown"

    if throttle_state is not None:
        live_running = _count_live_running_tasks(conn, run_id=run_id, now_epoch=now)
        if live_running >= max(1, int(throttle_state.concurrency_limit)):
            return True, None, "concurrency"

    has_pending = status["queued"] > 0 or status["running"] > 0
    return bool(has_pending), None, ("pending" if has_pending else None)


def _begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def lease_one_task(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    lease_seconds: int,
    run_id: str | None,
) -> dict | None:
    now_epoch = _now_epoch()
    lease_window = max(0.0, float(lease_seconds)) + LEASE_EXPIRY_GRACE_SECONDS
    lease_until = now_epoch + lease_window
    now_iso = _now_iso()
    lease_token = uuid.uuid4().hex

    has_candidate = conn.execute(
        """
        SELECT 1
        FROM tasks AS t
        JOIN runs AS r ON r.run_id = t.run_id
        WHERE (
            t.status = 'queued'
            OR (t.status = 'running' AND t.lease_until IS NOT NULL AND t.lease_until < ?)
        )
        AND r.control_state = ?
        AND (? IS NULL OR t.run_id = ?)
        LIMIT 1
        """,
        (now_epoch, RunControlState.ACTIVE.value, run_id, run_id),
    ).fetchone()
    if has_candidate is None:
        return None

    try:
        _begin_immediate(conn)
        candidate_rows = conn.execute(
            """
            SELECT t.*
            FROM tasks AS t
            JOIN runs AS r ON r.run_id = t.run_id
            WHERE (
                t.status = 'queued'
                OR (t.status = 'running' AND t.lease_until IS NOT NULL AND t.lease_until < ?)
            )
            AND r.control_state = ?
            AND (? IS NULL OR t.run_id = ?)
            ORDER BY
                CASE WHEN t.status = 'queued' THEN 0 ELSE 1 END,
                t.updated_at ASC
            """,
            (now_epoch, RunControlState.ACTIVE.value, run_id, run_id),
        ).fetchall()

        task_row: sqlite3.Row | None = None
        throttle_cache: dict[str, sqlite3.Row | None] = {}
        live_running_cache: dict[str, int] = {}
        for candidate in candidate_rows:
            candidate_run_id = str(candidate["run_id"])
            if candidate_run_id not in throttle_cache:
                throttle_cache[candidate_run_id] = conn.execute(
                    """
                    SELECT concurrency_limit, cooldown_until
                    FROM run_throttle_state
                    WHERE run_id = ?
                    """,
                    (candidate_run_id,),
                ).fetchone()
            throttle_row = throttle_cache[candidate_run_id]
            if throttle_row is not None:
                cooldown_until = throttle_row["cooldown_until"]
                if cooldown_until is not None and float(cooldown_until) > now_epoch:
                    continue
                if candidate_run_id not in live_running_cache:
                    live_running_cache[candidate_run_id] = _count_live_running_tasks(
                        conn,
                        run_id=candidate_run_id,
                        now_epoch=now_epoch,
                    )
                concurrency_limit = max(1, int(throttle_row["concurrency_limit"]))
                if live_running_cache[candidate_run_id] >= concurrency_limit:
                    continue
            task_row = candidate
            break

        if task_row is None:
            conn.commit()
            return None
        previous_error = task_row["error"]

        conn.execute(
            """
            UPDATE tasks
            SET status = 'running',
                attempts = attempts + 1,
                leased_by = ?,
                lease_until = ?,
                lease_token = ?,
                updated_at = ?,
                error = NULL
            WHERE task_id = ?
            """,
            (worker_id, lease_until, lease_token, now_iso, task_row["task_id"]),
        )
        conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
            ("running", now_iso, task_row["run_id"]),
        )

        claimed = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?",
            (task_row["task_id"],),
        ).fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if claimed is None:
        return None

    leased_task = dict(claimed)
    leased_task["previous_error"] = previous_error
    return leased_task


def _lease_guard_predicate(
    *,
    task_id: str,
    lease_token: str | None,
) -> tuple[str, tuple[object, ...]]:
    if lease_token is None:
        return "task_id = ?", (task_id,)
    return (
        "task_id = ? AND status = 'running' AND lease_token = ?",
        (task_id, lease_token),
    )


def _normalize_control_state(control_state: str) -> str:
    normalized = control_state.strip().lower()
    if normalized not in RUN_CONTROL_STATES:
        allowed = ", ".join(sorted(RUN_CONTROL_STATES))
        raise ValueError(f"Invalid control_state: {control_state}. Expected one of: {allowed}")
    return normalized


def mark_task_done(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    output_path: str,
    lease_token: str | None = None,
    commit: bool = True,
) -> bool:
    now = _now_iso()
    where_sql, where_params = _lease_guard_predicate(task_id=task_id, lease_token=lease_token)
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'done',
            leased_by = NULL,
            lease_until = NULL,
            lease_token = NULL,
            error = NULL,
            output_path = ?,
            updated_at = ?
        WHERE {where_sql}
        """,
        (output_path, now, *where_params),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def mark_task_error(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    error: str,
    lease_token: str | None = None,
    commit: bool = True,
) -> bool:
    now = _now_iso()
    where_sql, where_params = _lease_guard_predicate(task_id=task_id, lease_token=lease_token)
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'error',
            leased_by = NULL,
            lease_until = NULL,
            lease_token = NULL,
            error = ?,
            output_path = NULL,
            updated_at = ?
        WHERE {where_sql}
        """,
        (error[:2000], now, *where_params),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def requeue_task(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    error: str,
    lease_token: str | None = None,
    commit: bool = True,
) -> bool:
    now = _now_iso()
    where_sql, where_params = _lease_guard_predicate(task_id=task_id, lease_token=lease_token)
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET status = 'queued',
            leased_by = NULL,
            lease_until = NULL,
            lease_token = NULL,
            error = ?,
            output_path = NULL,
            updated_at = ?
        WHERE {where_sql}
        """,
        (error[:2000], now, *where_params),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def mark_task_canceled(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_token: str | None = None,
    error: str | None = None,
    commit: bool = True,
) -> bool:
    now = _now_iso()
    where_sql, where_params = _lease_guard_predicate(task_id=task_id, lease_token=lease_token)
    if error is None:
        cursor = conn.execute(
            f"""
            UPDATE tasks
            SET status = 'canceled',
                leased_by = NULL,
                lease_until = NULL,
                lease_token = NULL,
                output_path = NULL,
                updated_at = ?
            WHERE {where_sql}
            """,
            (now, *where_params),
        )
    else:
        cursor = conn.execute(
            f"""
            UPDATE tasks
            SET status = 'canceled',
                leased_by = NULL,
                lease_until = NULL,
                lease_token = NULL,
                error = ?,
                output_path = NULL,
                updated_at = ?
            WHERE {where_sql}
            """,
            (error[:2000], now, *where_params),
        )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def heartbeat_task_lease(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_token: str,
    lease_seconds: int,
    commit: bool = True,
) -> bool:
    now_epoch = _now_epoch()
    now_iso = _now_iso()
    lease_window = max(0.0, float(lease_seconds)) + LEASE_EXPIRY_GRACE_SECONDS
    lease_until = now_epoch + lease_window
    where_sql, where_params = _lease_guard_predicate(task_id=task_id, lease_token=lease_token)
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET lease_until = ?,
            last_heartbeat_at = ?,
            updated_at = ?
        WHERE {where_sql}
        """,
        (lease_until, now_iso, now_iso, *where_params),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def begin_task_execution(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    lease_token: str,
    commit: bool = True,
) -> int | None:
    now_iso = _now_iso()
    where_sql, where_params = _lease_guard_predicate(task_id=task_id, lease_token=lease_token)
    cursor = conn.execute(
        f"""
        UPDATE tasks
        SET execution_attempts = COALESCE(execution_attempts, 0) + 1,
            last_heartbeat_at = COALESCE(last_heartbeat_at, ?),
            updated_at = ?
        WHERE {where_sql}
        """,
        (now_iso, now_iso, *where_params),
    )
    if cursor.rowcount != 1:
        if commit:
            conn.commit()
        return None
    row = conn.execute(
        "SELECT execution_attempts FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if commit:
        conn.commit()
    if row is None:
        return None
    return int(row["execution_attempts"])


def set_run_control_state(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    control_state: str,
) -> dict[str, object]:
    normalized_state = _normalize_control_state(control_state)
    now = _now_iso()
    cursor = conn.execute(
        """
        UPDATE runs
        SET control_state = ?, updated_at = ?
        WHERE run_id = ?
        """,
        (normalized_state, now, run_id),
    )
    conn.commit()
    if cursor.rowcount != 1:
        raise KeyError(f"Run not found: {run_id}")
    return run_status(conn, run_id=run_id)


def cancel_run_tasks(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    now_epoch: float | None = None,
) -> int:
    now = _now_iso()
    cutoff = _now_epoch() if now_epoch is None else float(now_epoch)
    changed = 0
    try:
        _begin_immediate(conn)
        run_row = conn.execute(
            "SELECT run_id FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if run_row is None:
            raise KeyError(f"Run not found: {run_id}")

        conn.execute(
            """
            UPDATE runs
            SET control_state = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (RunControlState.CANCEL_REQUESTED.value, now, run_id),
        )

        queued_cursor = conn.execute(
            """
            UPDATE tasks
            SET status = 'canceled',
                leased_by = NULL,
                lease_until = NULL,
                lease_token = NULL,
                error = COALESCE(error, ?),
                output_path = NULL,
                updated_at = ?
            WHERE run_id = ?
              AND status = 'queued'
            """,
            ("Run canceled by operator.", now, run_id),
        )
        changed += int(queued_cursor.rowcount or 0)

        expired_cursor = conn.execute(
            """
            UPDATE tasks
            SET status = 'canceled',
                leased_by = NULL,
                lease_until = NULL,
                lease_token = NULL,
                error = COALESCE(error, ?),
                output_path = NULL,
                updated_at = ?
            WHERE run_id = ?
              AND status = 'running'
              AND (lease_until IS NULL OR lease_until < ?)
            """,
            ("Run canceled by operator.", now, run_id, cutoff),
        )
        changed += int(expired_cursor.rowcount or 0)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return changed


def requeue_error_tasks_for_run(conn: sqlite3.Connection, *, run_id: str) -> int:
    now = _now_iso()
    cursor = conn.execute(
        """
        UPDATE tasks
        SET status = 'queued',
            attempts = 0,
            rate_limit_count = 0,
            leased_by = NULL,
            lease_until = NULL,
            lease_token = NULL,
            output_path = NULL,
            updated_at = ?
        WHERE run_id = ?
          AND status = 'error'
        """,
        (now, run_id),
    )
    conn.execute(
        "UPDATE runs SET updated_at = ? WHERE run_id = ?",
        (now, run_id),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def run_status(conn: sqlite3.Connection, *, run_id: str) -> dict:
    run = get_run(conn, run_id)
    counts = {"queued": 0, "running": 0, "done": 0, "error": 0, "canceled": 0}

    for row in conn.execute(
        "SELECT status, COUNT(*) AS count FROM tasks WHERE run_id = ? GROUP BY status",
        (run_id,),
    ).fetchall():
        status_key = str(row["status"])
        if status_key in counts:
            counts[status_key] = int(row["count"])

    total = sum(counts.values())
    control_state = coerce_control_state(run.get("control_state")).value
    effective_status = derive_effective_status(
        coerce_control_state(control_state),
        RunCounts(
            queued=counts["queued"],
            running=counts["running"],
            done=counts["done"],
            error=counts["error"],
            canceled=counts["canceled"],
            total=total,
        ),
    )

    persisted_control_state = control_state
    if (
        control_state == RunControlState.CANCEL_REQUESTED.value
        and counts["queued"] == 0
        and counts["running"] == 0
    ):
        persisted_control_state = RunControlState.CANCELED.value

    if (
        run["status"] != effective_status
        or run.get("control_state", RunControlState.ACTIVE.value) != persisted_control_state
    ):
        now = _now_iso()
        conn.execute(
            """
            UPDATE runs
            SET status = ?, control_state = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (effective_status, persisted_control_state, now, run_id),
        )
        conn.commit()

    return {
        "run_id": run_id,
        "pipeline_id": run["pipeline_id"],
        "status": effective_status,
        "control_state": persisted_control_state,
        "total": total,
        **counts,
    }


def list_tasks_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str | None = None,
) -> list[dict]:
    if status is not None and status not in TASK_STATUSES:
        raise ValueError(f"Invalid task status filter: {status}")

    query = """
        SELECT
            input_path,
            rel_output_path,
            status,
            attempts,
            attempts AS lease_claims,
            execution_attempts,
            last_heartbeat_at,
            error,
            output_path,
            reused_from_run_id,
            reused_from_task_id,
            session_row_id,
            session_task_index,
            session_turn_index,
            fresh_session_started
        FROM tasks
        WHERE run_id = ?
    """
    params: list[object] = [run_id]
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY input_path ASC"

    rows = conn.execute(query, tuple(params)).fetchall()
    tasks: list[dict] = []
    for row in rows:
        task = dict(row)
        task["reused"] = task["reused_from_task_id"] is not None
        tasks.append(task)
    return tasks


def list_running_tasks_snapshot(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    limit: int = 8,
) -> list[dict]:
    safe_limit = max(0, int(limit))
    if safe_limit == 0:
        return []

    rows = conn.execute(
        """
        SELECT
            task_id,
            input_path,
            rel_output_path,
            attempts,
            attempts AS lease_claims,
            execution_attempts,
            leased_by,
            lease_until,
            last_heartbeat_at,
            updated_at,
            session_row_id,
            session_task_index,
            session_turn_index,
            fresh_session_started
        FROM tasks
        WHERE run_id = ? AND status = 'running'
        ORDER BY COALESCE(last_heartbeat_at, updated_at) DESC, input_path ASC
        LIMIT ?
        """,
        (run_id, safe_limit),
    ).fetchall()
    return [dict(row) for row in rows]


def list_recent_error_tasks_snapshot(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    limit: int = 5,
) -> list[dict]:
    safe_limit = max(0, int(limit))
    if safe_limit == 0:
        return []

    rows = conn.execute(
        """
        SELECT
            task_id,
            input_path,
            rel_output_path,
            attempts,
            attempts AS lease_claims,
            execution_attempts,
            error,
            updated_at,
            session_row_id,
            session_task_index,
            session_turn_index,
            fresh_session_started
        FROM tasks
        WHERE run_id = ? AND status = 'error'
        ORDER BY updated_at DESC, input_path ASC
        LIMIT ?
        """,
        (run_id, safe_limit),
    ).fetchall()
    return [dict(row) for row in rows]


def list_error_tasks(conn: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            task_id,
            input_path,
            rel_output_path,
            attempts,
            attempts AS lease_claims,
            execution_attempts,
            last_heartbeat_at,
            error,
            leased_by,
            lease_until,
            updated_at,
            session_row_id,
            session_task_index,
            session_turn_index,
            fresh_session_started
        FROM tasks
        WHERE run_id = ? AND status = 'error'
        ORDER BY input_path ASC
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def insert_failure_forensics(
    conn: sqlite3.Connection,
    *,
    forensics_id: str,
    source: str,
    run_id: str | None,
    task_id: str | None,
    pipeline_id: str,
    attempt_index: int | None,
    terminal: bool,
    input_path: str | None,
    rel_output_path: str | None,
    error_summary: str,
    failure_stage: str,
    failure_category: str,
    bundle_dir: str,
    metadata_path: str,
    raw_output_path: str | None,
    created_at: str | None = None,
) -> None:
    created_value = created_at or _now_iso()
    conn.execute(
        """
        INSERT INTO task_forensics (
            forensics_id,
            source,
            run_id,
            task_id,
            pipeline_id,
            attempt_index,
            terminal,
            failure_stage,
            failure_category,
            input_path,
            rel_output_path,
            error_summary,
            bundle_dir,
            metadata_path,
            raw_output_path,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            forensics_id,
            source,
            run_id,
            task_id,
            pipeline_id,
            attempt_index,
            1 if terminal else 0,
            failure_stage,
            failure_category,
            input_path,
            rel_output_path,
            error_summary[:2000],
            bundle_dir,
            metadata_path,
            raw_output_path,
            created_value,
        ),
    )
    conn.commit()


def list_failure_forensics(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    task_id: str | None = None,
) -> list[dict]:
    query = """
        SELECT
            forensics_id,
            source,
            run_id,
            task_id,
            pipeline_id,
            attempt_index,
            terminal,
            input_path,
            rel_output_path,
            failure_stage,
            failure_category,
            error_summary,
            bundle_dir,
            metadata_path,
            raw_output_path,
            created_at
        FROM task_forensics
        WHERE run_id = ?
    """
    params: list[object] = [run_id]
    if task_id is not None:
        query += " AND task_id = ?"
        params.append(task_id)
    query += " ORDER BY created_at DESC, forensics_id DESC"
    rows = conn.execute(query, tuple(params)).fetchall()
    payload: list[dict] = []
    for row in rows:
        entry = dict(row)
        entry["terminal"] = bool(entry["terminal"])
        payload.append(entry)
    return payload


def upsert_heads_up_tips(
    conn: sqlite3.Connection,
    *,
    pipeline_id: str,
    source_run_id: str | None,
    tips: list[dict[str, str]],
) -> int:
    inserted = 0
    now = _now_iso()
    for tip in tips:
        input_signature = str(tip.get("input_signature", "")).strip()
        tip_text = str(tip.get("tip_text", "")).strip()
        if not input_signature or not tip_text:
            continue

        existing = conn.execute(
            """
            SELECT tip_id
            FROM heads_up_tips
            WHERE pipeline_id = ? AND input_signature = ? AND tip_text = ?
            """,
            (pipeline_id, input_signature, tip_text),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO heads_up_tips (
                    tip_id,
                    pipeline_id,
                    input_signature,
                    tip_text,
                    score,
                    uses,
                    wins,
                    source_run_id,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    pipeline_id,
                    input_signature,
                    tip_text,
                    0.5,
                    0,
                    0,
                    source_run_id,
                    now,
                    now,
                ),
            )
            inserted += 1
            continue

        conn.execute(
            """
            UPDATE heads_up_tips
            SET source_run_id = ?,
                updated_at = ?
            WHERE tip_id = ?
            """,
            (source_run_id, now, existing["tip_id"]),
        )

    conn.commit()
    return inserted


def select_heads_up_tips(
    conn: sqlite3.Connection,
    *,
    pipeline_id: str,
    input_signature: str,
    limit: int,
) -> list[dict]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    rows = conn.execute(
        """
        SELECT
            tip_id,
            pipeline_id,
            input_signature,
            tip_text,
            score,
            uses,
            wins,
            source_run_id,
            created_at,
            updated_at
        FROM heads_up_tips
        WHERE pipeline_id = ?
          AND (input_signature = ? OR input_signature = '*')
          AND NOT (uses >= 8 AND score < 0.25)
        ORDER BY
            CASE WHEN input_signature = ? THEN 0 ELSE 1 END,
            score DESC,
            updated_at DESC
        LIMIT ?
        """,
        (pipeline_id, input_signature, input_signature, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def list_heads_up_tips(
    conn: sqlite3.Connection,
    *,
    pipeline_id: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            tip_id,
            pipeline_id,
            input_signature,
            tip_text,
            score,
            uses,
            wins,
            source_run_id,
            created_at,
            updated_at
        FROM heads_up_tips
        WHERE pipeline_id = ?
        ORDER BY score DESC, updated_at DESC, tip_text ASC
        """,
        (pipeline_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def clear_heads_up_tips(
    conn: sqlite3.Connection,
    *,
    pipeline_id: str,
) -> int:
    cursor = conn.execute(
        "DELETE FROM heads_up_tips WHERE pipeline_id = ?",
        (pipeline_id,),
    )
    conn.commit()
    return int(cursor.rowcount or 0)


def record_heads_up_tip_usage(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    task_id: str,
    tip_ids: list[str],
    outcome: str,
) -> int:
    if outcome not in HEADS_UP_OUTCOMES:
        allowed = ", ".join(sorted(HEADS_UP_OUTCOMES))
        raise ValueError(f"Invalid outcome: {outcome}. Expected one of: {allowed}")

    now = _now_iso()
    wins_delta = 1 if outcome == "done" else 0
    recorded = 0
    for tip_id in sorted({tip_id.strip() for tip_id in tip_ids if tip_id and tip_id.strip()}):
        usage_cursor = conn.execute(
            """
            INSERT OR IGNORE INTO heads_up_tip_usage (
                usage_id,
                run_id,
                task_id,
                tip_id,
                outcome,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex, run_id, task_id, tip_id, outcome, now),
        )
        if usage_cursor.rowcount != 1:
            continue

        conn.execute(
            """
            UPDATE heads_up_tips
            SET uses = uses + 1,
                wins = wins + ?,
                score = (CAST((wins + ? + 1) AS REAL) / CAST((uses + 3) AS REAL)),
                updated_at = ?
            WHERE tip_id = ?
            """,
            (wins_delta, wins_delta, now, tip_id),
        )
        recorded += 1

    conn.commit()
    return recorded


def count_heads_up_tip_usage_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM heads_up_tip_usage
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    return int(row["count"]) if row is not None else 0


def list_task_learning_rows(
    conn: sqlite3.Connection,
    *,
    run_id: str,
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
            task_id,
            input_path,
            status,
            attempts,
            error
        FROM tasks
        WHERE run_id = ?
          AND status IN ('done', 'error')
        ORDER BY input_path ASC
        """,
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]
