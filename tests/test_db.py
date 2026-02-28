from pathlib import Path
import time

from codex_farm.db import (
    PlannedTaskRow,
    begin_task_execution,
    cancel_run_tasks,
    clear_heads_up_tips,
    count_heads_up_tip_usage_for_run,
    create_run,
    heartbeat_task_lease,
    effective_attempts,
    enqueue_tasks_for_run,
    get_run_throttle_state,
    init_db,
    infer_run_desired_concurrency,
    insert_planned_tasks_for_run,
    insert_failure_forensics,
    lease_one_task,
    list_failure_forensics,
    list_heads_up_tips,
    list_error_tasks,
    list_tasks_for_run,
    mark_task_canceled,
    mark_task_done,
    mark_task_error,
    open_db,
    requeue_task,
    requeue_task_after_rate_limit,
    requeue_error_tasks_for_run,
    record_heads_up_tip_usage,
    run_has_waitable_work,
    run_status,
    select_heads_up_tips,
    set_run_control_state,
    upsert_run_throttle_state,
    upsert_heads_up_tips,
)
from codex_farm.rate_limit_policy import RunThrottleState


def test_db_run_and_task_lifecycle(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    (input_dir / "nested").mkdir(parents=True)
    output_dir.mkdir(parents=True)

    file_a = input_dir / "nested" / "a.json"
    file_b = input_dir / "b.json"
    file_a.write_text('{"name": "A"}', encoding="utf-8")
    file_b.write_text('{"name": "B"}', encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)

    run_id = create_run(
        conn,
        pipeline_id="recipe.schemaorg.normalize.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={"source": "test"},
    )
    count = enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[file_a, file_b],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    assert count == 2

    task = lease_one_task(
        conn,
        worker_id="worker-1",
        lease_seconds=60,
        run_id=run_id,
    )
    assert task is not None
    assert task["status"] == "running"
    assert task["attempts"] == 1
    assert task["execution_attempts"] == 0

    mark_task_done(conn, task_id=task["task_id"], output_path=str(output_dir / task["rel_output_path"]))

    status = run_status(conn, run_id=run_id)
    assert status["total"] == 2
    assert status["done"] == 1
    assert status["queued"] == 1


def test_list_tasks_for_run_filters_status(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    file_a = input_dir / "a.json"
    file_b = input_dir / "b.json"
    file_a.write_text("{}", encoding="utf-8")
    file_b.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)

    run_id = create_run(
        conn,
        pipeline_id="recipe.schemaorg.normalize.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={"source": "test"},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[file_a, file_b],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    first = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    second = lease_one_task(conn, worker_id="w2", lease_seconds=30, run_id=run_id)
    assert first is not None
    assert second is not None

    mark_task_done(conn, task_id=first["task_id"], output_path=str(output_dir / first["rel_output_path"]))
    mark_task_error(conn, task_id=second["task_id"], error="boom")

    all_tasks = list_tasks_for_run(conn, run_id=run_id)
    done_tasks = list_tasks_for_run(conn, run_id=run_id, status="done")
    error_tasks = list_tasks_for_run(conn, run_id=run_id, status="error")

    assert len(all_tasks) == 2
    assert len(done_tasks) == 1
    assert len(error_tasks) == 1
    assert done_tasks[0]["status"] == "done"
    assert done_tasks[0]["lease_claims"] == done_tasks[0]["attempts"]
    assert done_tasks[0]["execution_attempts"] == 0
    assert done_tasks[0]["last_heartbeat_at"] is None
    assert error_tasks[0]["status"] == "error"
    assert error_tasks[0]["error"] == "boom"
    assert error_tasks[0]["lease_claims"] == error_tasks[0]["attempts"]
    assert error_tasks[0]["execution_attempts"] == 0
    assert error_tasks[0]["last_heartbeat_at"] is None

    error_rows = list_error_tasks(conn, run_id=run_id)
    assert len(error_rows) == 1
    assert error_rows[0]["input_path"] == str(file_b.resolve())
    assert error_rows[0]["rel_output_path"] == second["rel_output_path"]
    assert error_rows[0]["attempts"] == 1
    assert error_rows[0]["lease_claims"] == 1
    assert error_rows[0]["execution_attempts"] == 0
    assert error_rows[0]["last_heartbeat_at"] is None
    assert error_rows[0]["error"] == "boom"


def test_init_db_backfills_execution_attempts_for_migrated_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    conn = open_db(db_path)
    conn.executescript(
        """
        CREATE TABLE runs (
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

        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            input_path TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            rel_output_path TEXT NOT NULL,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            rate_limit_count INTEGER NOT NULL DEFAULT 0,
            leased_by TEXT,
            lease_until REAL,
            error TEXT,
            output_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO runs (
            run_id, pipeline_id, created_at, updated_at, status,
            input_dir, glob_pattern, output_dir, config_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "run-1",
            "demo.v1",
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
            "running",
            "/tmp/in",
            "**/*.json",
            "/tmp/out",
            "{}",
        ),
    )
    conn.execute(
        """
        INSERT INTO tasks (
            task_id, run_id, input_path, input_hash, rel_output_path, status,
            attempts, rate_limit_count, leased_by, lease_until, error, output_path,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "task-1",
            "run-1",
            "/tmp/in/a.json",
            "hash",
            "a.json",
            "running",
            5,
            2,
            "worker",
            time.time() + 30.0,
            None,
            None,
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ),
    )
    conn.commit()

    init_db(conn)
    row = conn.execute(
        "SELECT execution_attempts, last_heartbeat_at FROM tasks WHERE task_id = 'task-1'"
    ).fetchone()
    assert row is not None
    assert row["execution_attempts"] == 3
    assert row["last_heartbeat_at"] is None

    conn.execute("UPDATE tasks SET execution_attempts = 9 WHERE task_id = 'task-1'")
    conn.commit()
    init_db(conn)
    row_after = conn.execute(
        "SELECT execution_attempts FROM tasks WHERE task_id = 'task-1'"
    ).fetchone()
    assert row_after is not None
    assert row_after["execution_attempts"] == 9


def test_begin_task_execution_and_heartbeat_require_current_lease_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    input_path = input_dir / "one.json"
    input_path.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.execution.v1",
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    first = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert first is not None
    first_token = str(first["lease_token"])
    first_lease_until = float(first["lease_until"])

    assert begin_task_execution(
        conn,
        task_id=str(first["task_id"]),
        lease_token=first_token,
    ) == 1
    assert begin_task_execution(
        conn,
        task_id=str(first["task_id"]),
        lease_token=first_token,
    ) == 2
    assert heartbeat_task_lease(
        conn,
        task_id=str(first["task_id"]),
        lease_token=first_token,
        lease_seconds=60,
    )
    extended = conn.execute(
        "SELECT lease_until, last_heartbeat_at FROM tasks WHERE task_id = ?",
        (first["task_id"],),
    ).fetchone()
    assert extended is not None
    assert float(extended["lease_until"]) > first_lease_until
    assert isinstance(extended["last_heartbeat_at"], str)
    assert extended["last_heartbeat_at"]

    conn.execute(
        "UPDATE tasks SET lease_until = ? WHERE task_id = ?",
        (time.time() - 1.0, first["task_id"]),
    )
    conn.commit()
    reclaimed = lease_one_task(conn, worker_id="w2", lease_seconds=30, run_id=run_id)
    assert reclaimed is not None
    assert reclaimed["lease_token"] != first_token

    assert begin_task_execution(
        conn,
        task_id=str(first["task_id"]),
        lease_token=first_token,
    ) is None
    assert heartbeat_task_lease(
        conn,
        task_id=str(first["task_id"]),
        lease_token=first_token,
        lease_seconds=30,
    ) is False
    assert begin_task_execution(
        conn,
        task_id=str(reclaimed["task_id"]),
        lease_token=str(reclaimed["lease_token"]),
    ) == 3


def test_lease_one_task_returns_previous_error_for_retry_claim(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    input_path = input_dir / "a.json"
    input_path.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="recipe.schemaorg.normalize.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={"source": "test"},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    first_claim = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert first_claim is not None
    assert first_claim["attempts"] == 1
    assert first_claim["previous_error"] is None

    retry_message = "Schema validation failed at <root>: must be object"
    requeue_task(conn, task_id=first_claim["task_id"], error=retry_message)

    second_claim = lease_one_task(conn, worker_id="w2", lease_seconds=30, run_id=run_id)
    assert second_claim is not None
    assert second_claim["attempts"] == 2
    assert second_claim["previous_error"] == retry_message


def test_insert_planned_tasks_for_run_supports_reuse_metadata(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    input_path = input_dir / "a.json"
    input_path.write_text("{}", encoding="utf-8")
    output_path = output_dir / "a.json"
    output_path.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.incremental.v1",
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={},
        execution_fingerprint="fp-1",
    )

    inserted = insert_planned_tasks_for_run(
        conn,
        run_id=run_id,
        planned_tasks=[
            PlannedTaskRow(
                input_path=str(input_path.resolve()),
                input_hash="hash-a",
                rel_output_path="a.json",
                status="done",
                output_path=str(output_path.resolve()),
                reused_from_run_id="prior-run",
                reused_from_task_id="prior-task",
            )
        ],
    )
    assert inserted == 1

    tasks = list_tasks_for_run(conn, run_id=run_id)
    assert len(tasks) == 1
    assert tasks[0]["status"] == "done"
    assert tasks[0]["reused"] is True
    assert tasks[0]["reused_from_run_id"] == "prior-run"
    assert tasks[0]["reused_from_task_id"] == "prior-task"

    run_row = conn.execute(
        "SELECT execution_fingerprint FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    assert run_row is not None
    assert run_row["execution_fingerprint"] == "fp-1"


def test_pause_control_state_blocks_leasing_until_resume(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.lifecycle.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_dir / "one.json"],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    set_run_control_state(conn, run_id=run_id, control_state="paused")
    status = run_status(conn, run_id=run_id)
    assert status["control_state"] == "paused"
    assert status["status"] == "paused"
    assert lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id) is None

    set_run_control_state(conn, run_id=run_id, control_state="active")
    claimed = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert claimed is not None
    assert isinstance(claimed.get("lease_token"), str)
    assert claimed["lease_token"]


def test_lease_token_blocks_stale_finalization(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.lifecycle.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_dir / "one.json"],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    first = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert first is not None
    conn.execute(
        "UPDATE tasks SET lease_until = ? WHERE task_id = ?",
        (time.time() - 1, first["task_id"]),
    )
    conn.commit()
    second = lease_one_task(conn, worker_id="w2", lease_seconds=30, run_id=run_id)
    assert second is not None
    assert second["task_id"] == first["task_id"]
    assert second["lease_token"] != first["lease_token"]

    stale_ok = mark_task_done(
        conn,
        task_id=first["task_id"],
        lease_token=first["lease_token"],
        output_path=str(output_dir / "one.json"),
    )
    assert stale_ok is False

    live_ok = mark_task_done(
        conn,
        task_id=second["task_id"],
        lease_token=second["lease_token"],
        output_path=str(output_dir / "one.json"),
    )
    assert live_ok is True


def test_cancel_marks_queued_and_expired_running_then_finalizes_control_state(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    files = [input_dir / "a.json", input_dir / "b.json", input_dir / "c.json"]
    for path in files:
        path.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.lifecycle.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=files,
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    live_running = lease_one_task(conn, worker_id="live", lease_seconds=300, run_id=run_id)
    expired_running = lease_one_task(conn, worker_id="expired", lease_seconds=300, run_id=run_id)
    assert live_running is not None
    assert expired_running is not None
    conn.execute(
        "UPDATE tasks SET lease_until = ? WHERE task_id = ?",
        (time.time() - 1, expired_running["task_id"]),
    )
    conn.commit()

    changed = cancel_run_tasks(conn, run_id=run_id, now_epoch=time.time())
    assert changed == 2

    mid = run_status(conn, run_id=run_id)
    assert mid["control_state"] == "cancel_requested"
    assert mid["status"] == "running"
    assert mid["canceled"] == 2
    assert mid["running"] == 1

    assert mark_task_canceled(
        conn,
        task_id=live_running["task_id"],
        lease_token=live_running["lease_token"],
        error="canceled during drain",
    )
    final = run_status(conn, run_id=run_id)
    assert final["control_state"] == "canceled"
    assert final["status"] == "canceled"
    assert final["canceled"] == 3


def test_retry_errors_resets_attempts_and_keeps_last_error(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.lifecycle.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_dir / "one.json"],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    leased = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert leased is not None
    assert mark_task_error(
        conn,
        task_id=leased["task_id"],
        lease_token=leased["lease_token"],
        error="schema mismatch",
    )

    changed = requeue_error_tasks_for_run(conn, run_id=run_id)
    assert changed == 1

    rows = list_tasks_for_run(conn, run_id=run_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "queued"
    assert row["attempts"] == 0
    assert row["error"] == "schema mismatch"


def test_heads_up_tip_tables_and_usage_lifecycle(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    input_path = input_dir / "one.json"
    input_path.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.heads-up.v1",
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    inserted = upsert_heads_up_tips(
        conn,
        pipeline_id="demo.heads-up.v1",
        source_run_id=run_id,
        tips=[
            {"input_signature": "json_obj_keys:", "tip_text": "Return JSON only."},
            {"input_signature": "*", "tip_text": "Do not emit markdown."},
        ],
    )
    assert inserted == 2

    rows = select_heads_up_tips(
        conn,
        pipeline_id="demo.heads-up.v1",
        input_signature="json_obj_keys:",
        limit=3,
    )
    assert len(rows) == 2
    task_row = conn.execute("SELECT task_id FROM tasks WHERE run_id = ?", (run_id,)).fetchone()
    assert task_row is not None

    recorded = record_heads_up_tip_usage(
        conn,
        run_id=run_id,
        task_id=str(task_row["task_id"]),
        tip_ids=[str(rows[0]["tip_id"])],
        outcome="done",
    )
    assert recorded == 1
    assert count_heads_up_tip_usage_for_run(conn, run_id=run_id) == 1

    listed = list_heads_up_tips(conn, pipeline_id="demo.heads-up.v1")
    assert len(listed) == 2
    scored = next(row for row in listed if row["tip_id"] == rows[0]["tip_id"])
    assert scored["uses"] == 1
    assert scored["wins"] == 1

    deleted = clear_heads_up_tips(conn, pipeline_id="demo.heads-up.v1")
    assert deleted == 2
    assert list_heads_up_tips(conn, pipeline_id="demo.heads-up.v1") == []


def test_rate_limit_requeue_uses_internal_counter_for_effective_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    input_path = input_dir / "one.json"
    input_path.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.rate-limit.v1",
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    first = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert first is not None
    assert first["attempts"] == 1
    assert first["rate_limit_count"] == 0
    assert requeue_task_after_rate_limit(
        conn,
        task_id=first["task_id"],
        lease_token=first["lease_token"],
    )

    second = lease_one_task(conn, worker_id="w2", lease_seconds=30, run_id=run_id)
    assert second is not None
    assert second["attempts"] == 2
    assert second["rate_limit_count"] == 1
    assert effective_attempts(second) == 1
    assert second["previous_error"] is None


def test_lease_skips_run_with_active_cooldown(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    file_a = input_dir / "a.json"
    file_b = input_dir / "b.json"
    file_a.write_text("{}", encoding="utf-8")
    file_b.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_a = create_run(
        conn,
        pipeline_id="demo.rate-limit.v1",
        input_dir=str(input_dir.resolve()),
        glob="a.json",
        output_dir=str(output_dir.resolve()),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_a,
        input_files=[file_a],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )
    run_b = create_run(
        conn,
        pipeline_id="demo.rate-limit.v1",
        input_dir=str(input_dir.resolve()),
        glob="b.json",
        output_dir=str(output_dir.resolve()),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_b,
        input_files=[file_b],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    upsert_run_throttle_state(
        conn,
        state=RunThrottleState(
            run_id=run_a,
            desired_concurrency=1,
            concurrency_limit=1,
            cooldown_until=time.time() + 30.0,
            last_cooldown_seconds=30,
            consecutive_rate_limits=1,
            success_streak=0,
            last_rate_limit_error="429",
        ),
    )

    claimed = lease_one_task(conn, worker_id="w", lease_seconds=30, run_id=None)
    assert claimed is not None
    assert claimed["run_id"] == run_b


def test_lease_respects_concurrency_limit_but_reclaims_after_expiry(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    files = [input_dir / "a.json", input_dir / "b.json"]
    for path in files:
        path.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.rate-limit.v1",
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={"workers": 2},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=files,
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )
    upsert_run_throttle_state(
        conn,
        state=RunThrottleState(
            run_id=run_id,
            desired_concurrency=2,
            concurrency_limit=1,
            cooldown_until=None,
            last_cooldown_seconds=0,
            consecutive_rate_limits=0,
            success_streak=0,
            last_rate_limit_error=None,
        ),
    )

    first = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    assert first is not None
    blocked = lease_one_task(conn, worker_id="w2", lease_seconds=30, run_id=run_id)
    assert blocked is None

    conn.execute(
        "UPDATE tasks SET lease_until = ? WHERE task_id = ?",
        (time.time() - 1.0, first["task_id"]),
    )
    conn.commit()
    reclaimed = lease_one_task(conn, worker_id="w2", lease_seconds=30, run_id=run_id)
    assert reclaimed is not None


def test_run_has_waitable_work_reports_cooldown_and_concurrency(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    path = input_dir / "one.json"
    path.write_text("{}", encoding="utf-8")

    conn = open_db(db_path)
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id="demo.rate-limit.v1",
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={"workers": 4},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    upsert_run_throttle_state(
        conn,
        state=RunThrottleState(
            run_id=run_id,
            desired_concurrency=4,
            concurrency_limit=2,
            cooldown_until=time.time() + 15.0,
            last_cooldown_seconds=15,
            consecutive_rate_limits=1,
            success_streak=0,
            last_rate_limit_error="429",
        ),
    )
    waitable, remaining, reason = run_has_waitable_work(conn, run_id=run_id, now=time.time())
    assert waitable is True
    assert reason == "cooldown"
    assert remaining is not None and remaining > 0

    desired = infer_run_desired_concurrency(conn, run_id=run_id)
    assert desired == 4
    stored = get_run_throttle_state(conn, run_id)
    assert stored is not None
    assert stored.concurrency_limit == 2


def test_task_forensics_insert_and_list(tmp_path: Path) -> None:
    db_path = tmp_path / "var" / "codex_farm.sqlite3"
    conn = open_db(db_path)
    init_db(conn)

    insert_failure_forensics(
        conn,
        forensics_id="forensics-1",
        source="worker",
        run_id="run-1",
        task_id="task-1",
        pipeline_id="demo.forensics.v1",
        attempt_index=2,
        terminal=True,
        input_path="/tmp/in/a.json",
        rel_output_path="a.json",
        error_summary="Schema validation failed at <root>: missing field",
        failure_stage="schema_validation",
        failure_category="schema_validation",
        bundle_dir="/tmp/var/forensics/runs/run-1/task-1/attempt-2",
        metadata_path="/tmp/var/forensics/runs/run-1/task-1/attempt-2/metadata.json",
        raw_output_path="/tmp/var/forensics/runs/run-1/task-1/attempt-2/output.raw.json",
        created_at="2026-02-28T12:30:00Z",
    )

    rows = list_failure_forensics(conn, run_id="run-1")
    assert len(rows) == 1
    assert rows[0]["forensics_id"] == "forensics-1"
    assert rows[0]["task_id"] == "task-1"
    assert rows[0]["attempt_index"] == 2
    assert rows[0]["failure_stage"] == "schema_validation"
    assert rows[0]["failure_category"] == "schema_validation"
    assert rows[0]["terminal"] is True
