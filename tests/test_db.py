from pathlib import Path

from codex_farm.db import (
    create_run,
    enqueue_tasks_for_run,
    init_db,
    lease_one_task,
    mark_task_done,
    open_db,
    run_status,
)


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

    mark_task_done(conn, task_id=task["task_id"], output_path=str(output_dir / task["rel_output_path"]))

    status = run_status(conn, run_id=run_id)
    assert status["total"] == 2
    assert status["done"] == 1
    assert status["queued"] == 1
