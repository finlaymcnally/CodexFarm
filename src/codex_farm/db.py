"""SQLite queue and run metadata helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid


RUN_STATUSES = {"queued", "running", "done", "error"}
TASK_STATUSES = {"queued", "running", "done", "error"}


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


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            pipeline_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT NOT NULL,
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
            leased_by TEXT,
            lease_until REAL,
            error TEXT,
            output_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_run_status ON tasks(run_id, status);
        CREATE INDEX IF NOT EXISTS idx_tasks_lease_until ON tasks(lease_until);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_run_input ON tasks(run_id, input_path);
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
) -> str:
    run_id = uuid.uuid4().hex
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO runs (
            run_id, pipeline_id, created_at, updated_at, status,
            input_dir, glob_pattern, output_dir, config_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            pipeline_id,
            now,
            now,
            "queued",
            input_dir,
            glob,
            output_dir,
            json.dumps(config, sort_keys=True),
        ),
    )
    conn.commit()
    return run_id


def enqueue_tasks_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    input_files: list[Path],
    input_root: Path,
    output_root: Path,
    output_ext: str,
) -> int:
    now = _now_iso()
    rows: list[tuple[str, str, str, str, str, str, int, str, str]] = []

    input_root_resolved = input_root.resolve()
    output_root_resolved = output_root.resolve()

    for input_file in sorted(input_files):
        rel_out = _rel_output_path(
            input_file=input_file.resolve(),
            input_root=input_root_resolved,
            output_ext=output_ext,
        )
        task_id = uuid.uuid4().hex
        rows.append(
            (
                task_id,
                run_id,
                str(input_file.resolve()),
                _hash_file(input_file),
                rel_out.as_posix(),
                "queued",
                0,
                now,
                now,
            )
        )

    conn.executemany(
        """
        INSERT INTO tasks (
            task_id, run_id, input_path, input_hash, rel_output_path,
            status, attempts, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.execute(
        "UPDATE runs SET updated_at = ?, status = ? WHERE run_id = ?",
        (now, "queued", run_id),
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
    lease_until = now_epoch + lease_seconds
    now_iso = _now_iso()

    try:
        _begin_immediate(conn)
        task_row = conn.execute(
            """
            SELECT t.*
            FROM tasks AS t
            WHERE (
                t.status = 'queued'
                OR (t.status = 'running' AND t.lease_until IS NOT NULL AND t.lease_until < ?)
            )
            AND (? IS NULL OR t.run_id = ?)
            ORDER BY
                CASE WHEN t.status = 'queued' THEN 0 ELSE 1 END,
                t.updated_at ASC
            LIMIT 1
            """,
            (now_epoch, run_id, run_id),
        ).fetchone()

        if task_row is None:
            conn.commit()
            return None

        conn.execute(
            """
            UPDATE tasks
            SET status = 'running',
                attempts = attempts + 1,
                leased_by = ?,
                lease_until = ?,
                updated_at = ?,
                error = NULL
            WHERE task_id = ?
            """,
            (worker_id, lease_until, now_iso, task_row["task_id"]),
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

    return dict(claimed) if claimed else None


def mark_task_done(conn: sqlite3.Connection, *, task_id: str, output_path: str) -> None:
    now = _now_iso()
    conn.execute(
        """
        UPDATE tasks
        SET status = 'done',
            leased_by = NULL,
            lease_until = NULL,
            error = NULL,
            output_path = ?,
            updated_at = ?
        WHERE task_id = ?
        """,
        (output_path, now, task_id),
    )
    conn.commit()


def mark_task_error(conn: sqlite3.Connection, *, task_id: str, error: str) -> None:
    now = _now_iso()
    conn.execute(
        """
        UPDATE tasks
        SET status = 'error',
            leased_by = NULL,
            lease_until = NULL,
            error = ?,
            updated_at = ?
        WHERE task_id = ?
        """,
        (error[:2000], now, task_id),
    )
    conn.commit()


def requeue_task(conn: sqlite3.Connection, *, task_id: str, error: str) -> None:
    now = _now_iso()
    conn.execute(
        """
        UPDATE tasks
        SET status = 'queued',
            leased_by = NULL,
            lease_until = NULL,
            error = ?,
            updated_at = ?
        WHERE task_id = ?
        """,
        (error[:2000], now, task_id),
    )
    conn.commit()


def run_status(conn: sqlite3.Connection, *, run_id: str) -> dict:
    run = get_run(conn, run_id)
    counts = {"queued": 0, "running": 0, "done": 0, "error": 0}

    for row in conn.execute(
        "SELECT status, COUNT(*) AS count FROM tasks WHERE run_id = ? GROUP BY status",
        (run_id,),
    ).fetchall():
        counts[row["status"]] = int(row["count"])

    total = sum(counts.values())
    if total == 0:
        inferred_status = "queued"
    elif counts["queued"] == total:
        inferred_status = "queued"
    elif counts["queued"] == 0 and counts["running"] == 0:
        inferred_status = "error" if counts["error"] > 0 else "done"
    else:
        inferred_status = "running"

    if run["status"] != inferred_status:
        now = _now_iso()
        conn.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
            (inferred_status, now, run_id),
        )
        conn.commit()

    return {
        "run_id": run_id,
        "pipeline_id": run["pipeline_id"],
        "status": inferred_status,
        "total": total,
        **counts,
    }
