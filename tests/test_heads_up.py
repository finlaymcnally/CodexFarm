import json
from pathlib import Path

import pytest

from codex_farm.db import (
    create_run,
    enqueue_tasks_for_run,
    init_db,
    list_heads_up_tips,
    open_db,
    select_heads_up_tips,
    upsert_heads_up_tips,
)
from codex_farm.heads_up import append_heads_up_block, compute_input_signature, record_tip_usage


def test_compute_input_signature_variants(tmp_path: Path) -> None:
    obj_path = tmp_path / "obj.json"
    arr_path = tmp_path / "arr.json"
    txt_path = tmp_path / "bad.txt"

    obj_path.write_text('{"b": 1, "a": 2}', encoding="utf-8")
    arr_path.write_text("[1, 2, 3]", encoding="utf-8")
    txt_path.write_text("not-json", encoding="utf-8")

    assert compute_input_signature(obj_path) == "json_obj_keys:a,b"
    assert compute_input_signature(arr_path) == "json_array"
    assert compute_input_signature(txt_path) == "unknown"


def test_append_heads_up_block_only_when_tips_present() -> None:
    base_prompt = "Process this file."

    unchanged = append_heads_up_block(base_prompt, [])
    assert unchanged == base_prompt

    prompt_with_tips = append_heads_up_block(
        base_prompt,
        [
            {"tip_text": "Keep nullable fields as null."},
            {"tip_text": "Return raw JSON only."},
        ],
    )
    assert "Heads up for this task:" in prompt_with_tips
    assert "1) Keep nullable fields as null." in prompt_with_tips
    assert "2) Return raw JSON only." in prompt_with_tips


def test_heads_up_tip_scoring_and_retrieval_filter(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    db_path = data_dir / "codex_farm.sqlite3"
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    input_path = input_dir / "sample.json"
    input_path.write_text(json.dumps({"a": 1, "b": 2}), encoding="utf-8")

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
            {
                "input_signature": "json_obj_keys:a,b",
                "tip_text": "Keep nullable fields as null.",
            },
            {
                "input_signature": "*",
                "tip_text": "Do not emit markdown.",
            },
        ],
    )
    assert inserted == 2

    selected = select_heads_up_tips(
        conn,
        pipeline_id="demo.heads-up.v1",
        input_signature="json_obj_keys:a,b",
        limit=5,
    )
    assert len(selected) == 2

    exact_tip = next(row for row in selected if row["input_signature"] == "json_obj_keys:a,b")
    task_row = conn.execute("SELECT task_id FROM tasks WHERE run_id = ?", (run_id,)).fetchone()
    assert task_row is not None

    recorded = record_tip_usage(
        conn,
        run_id=run_id,
        task_id=str(task_row["task_id"]),
        tip_ids=[str(exact_tip["tip_id"])],
        outcome="done",
    )
    assert recorded == 1

    rows = list_heads_up_tips(conn, pipeline_id="demo.heads-up.v1")
    scored_exact = next(row for row in rows if row["tip_id"] == exact_tip["tip_id"])
    assert scored_exact["uses"] == 1
    assert scored_exact["wins"] == 1
    assert scored_exact["score"] == pytest.approx(2.0 / 3.0)

    conn.execute(
        """
        UPDATE heads_up_tips
        SET uses = 8,
            wins = 0,
            score = 0.1
        WHERE pipeline_id = ? AND input_signature = '*'
        """,
        ("demo.heads-up.v1",),
    )
    conn.commit()

    selected_after_filter = select_heads_up_tips(
        conn,
        pipeline_id="demo.heads-up.v1",
        input_signature="json_obj_keys:a,b",
        limit=5,
    )
    assert len(selected_after_filter) == 1
    assert selected_after_filter[0]["input_signature"] == "json_obj_keys:a,b"
