import json
from pathlib import Path

from codex_farm.codex_exec import CodexExecResult
from codex_farm.db import create_run, enqueue_tasks_for_run, init_db, open_db, run_status
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
