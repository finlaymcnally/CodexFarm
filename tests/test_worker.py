import json
from pathlib import Path

import pytest

from codex_farm.codex_exec import CodexExecResult
from codex_farm.db import (
    create_run,
    enqueue_tasks_for_run,
    init_db,
    list_tasks_for_run,
    open_db,
    run_status,
)
from codex_farm.paths import find_repo_root
from codex_farm.pipeline_spec import load_pipelines
from codex_farm.worker import worker_loop


def _fake_recipe(name: str) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": name,
        "description": None,
        "recipeYield": None,
        "prepTime": None,
        "cookTime": None,
        "totalTime": None,
        "recipeIngredient": ["1 cup water"],
        "recipeInstructions": [
            {
                "@type": "HowToStep",
                "text": "Boil water."
            }
        ]
    }


def _write_demo_pack(root: Path, *, pipeline_id: str, codex_cd_mode: str) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    slug = pipeline_id.replace(".", "_")
    prompt_rel = Path("prompts") / f"{slug}.txt"
    schema_rel = Path("schemas") / f"{slug}.schema.json"
    pipeline_path = root / "pipelines" / f"{pipeline_id}.json"

    pipeline_payload = {
        "pipeline_id": pipeline_id,
        "description": f"Pipeline {pipeline_id}",
        "prompt_template_path": prompt_rel.as_posix(),
        "output_schema_path": schema_rel.as_posix(),
        "input_glob_default": "**/*.json",
        "output_ext": ".json",
        "codex_model": "gpt-5.3-codex-spark",
        "codex_sandbox": "read-only",
        "codex_ask_for_approval": "never",
        "codex_web_search": "disabled",
        "codex_timeout_seconds": 180,
        "codex_cd_mode": codex_cd_mode,
    }
    pipeline_path.write_text(json.dumps(pipeline_payload, indent=2) + "\n", encoding="utf-8")

    (root / prompt_rel).write_text("Input file path: {{INPUT_PATH}}\n", encoding="utf-8")
    (root / schema_rel).write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "additionalProperties": False,
                "required": ["ok", "source_path"],
                "properties": {
                    "ok": {"type": "string"},
                    "source_path": {"type": "string"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_worker_loop_processes_task_with_mocked_codex(monkeypatch, tmp_path: Path) -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")
    spec = pipelines["recipe.schemaorg.normalize.v1"]

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    workspace_root = tmp_path / "workspace"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)
    workspace_root.mkdir(parents=True)

    input_path = input_dir / "r1.json"
    input_path.write_text(json.dumps({"name": "Mock Chili"}), encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={
            "farm_root": str(repo_root),
            "workspace_root": str(workspace_root),
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    captured_cd_dirs: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_cd_dirs.append(str(kwargs["cd_dir"]))
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(_fake_recipe("Mock Chili")), encoding="utf-8")
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="test-worker",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 0
    produced = output_dir / "r1.json"
    assert produced.exists()

    status = run_status(conn, run_id=run_id)
    assert status["done"] == 1
    assert status["error"] == 0
    assert captured_cd_dirs == [str(workspace_root.resolve())]


@pytest.mark.parametrize(
    ("codex_cd_mode", "expected_kind"),
    [
        ("asset_root", "asset_root"),
        ("input_dir", "input_dir"),
        ("input_file_dir", "input_file_dir"),
    ],
)
def test_worker_loop_selects_cd_dir_from_pipeline_mode(
    monkeypatch,
    tmp_path: Path,
    codex_cd_mode: str,
    expected_kind: str,
) -> None:
    pack_root = tmp_path / "pack"
    pipeline_id = f"demo.{codex_cd_mode}.v1"
    _write_demo_pack(pack_root, pipeline_id=pipeline_id, codex_cd_mode=codex_cd_mode)
    pipelines = load_pipelines(pack_root / "pipelines")
    spec = pipelines[pipeline_id]

    input_dir = tmp_path / "input_root"
    nested_dir = input_dir / "nested"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    nested_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    input_path = nested_dir / "a.json"
    input_path.write_text("{}", encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir.resolve()),
        glob="**/*.json",
        output_dir=str(output_dir.resolve()),
        config={
            "farm_root": str(pack_root.resolve()),
        },
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[input_path],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    captured_cd_dirs: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_cd_dirs.append(str(kwargs["cd_dir"]))
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"ok": "OK", "source_path": str(input_path.resolve())}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="test-worker",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
    )

    assert code == 0
    assert len(captured_cd_dirs) == 1

    if expected_kind == "asset_root":
        expected_dir = pack_root.resolve()
    elif expected_kind == "input_dir":
        expected_dir = input_dir.resolve()
    else:
        expected_dir = input_path.resolve().parent

    assert captured_cd_dirs[0] == str(expected_dir)


def test_worker_loop_stops_immediately_on_rate_limit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")
    spec = pipelines["recipe.schemaorg.normalize.v1"]

    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)
    data_dir.mkdir(parents=True)

    first_input = input_dir / "r1.json"
    second_input = input_dir / "r2.json"
    first_input.write_text(json.dumps({"name": "One"}), encoding="utf-8")
    second_input.write_text(json.dumps({"name": "Two"}), encoding="utf-8")

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    run_id = create_run(
        conn,
        pipeline_id=spec.pipeline_id,
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={"farm_root": str(repo_root)},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[first_input, second_input],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=spec.output_ext,
    )

    warnings: list[str] = []

    def fake_run_codex_exec(**kwargs):
        return CodexExecResult(
            ok=False,
            exit_code=1,
            stderr_tail="HTTP 429 Too Many Requests: rate limit exceeded",
        )

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    code = worker_loop(
        data_dir=data_dir,
        worker_id="test-worker",
        run_id=run_id,
        lease_seconds=120,
        max_attempts=3,
        poll_seconds=0.01,
        once=True,
        warning_callback=warnings.append,
    )

    assert code == 1
    status = run_status(conn, run_id=run_id)
    assert status["error"] == 1
    assert status["queued"] == 1

    tasks = list_tasks_for_run(conn, run_id=run_id)
    error_task = next(task for task in tasks if task["status"] == "error")
    assert error_task["attempts"] == 1
    assert "429" in (error_task["error"] or "")
    assert warnings
    assert "429" in warnings[0]
