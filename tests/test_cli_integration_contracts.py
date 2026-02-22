import json
from pathlib import Path

from typer.testing import CliRunner

from codex_farm.cli import app
from codex_farm.codex_exec import CodexExecResult
from codex_farm.db import (
    create_run,
    enqueue_tasks_for_run,
    init_db,
    lease_one_task,
    mark_task_done,
    mark_task_error,
    open_db,
)


runner = CliRunner()


def _write_pipeline_pack(root: Path, pipeline_id: str) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    slug = pipeline_id.replace(".", "_")
    pipeline_path = root / "pipelines" / f"{pipeline_id}.json"
    prompt_rel = Path("prompts") / f"{slug}.txt"
    schema_rel = Path("schemas") / f"{slug}.schema.json"

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
    }
    (root / "pipelines" / f"{pipeline_id}.json").write_text(
        json.dumps(pipeline_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    (root / prompt_rel).write_text("Input file path: {{INPUT_PATH}}\n", encoding="utf-8")

    schema_payload = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": ["ok", "source_path"],
        "properties": {
            "ok": {"type": "string"},
            "source_path": {"type": "string"},
        },
    }
    (root / schema_rel).write_text(json.dumps(schema_payload, indent=2) + "\n", encoding="utf-8")


def test_pipelines_list_root_override_wins_over_env(tmp_path: Path) -> None:
    env_pack = tmp_path / "env_pack"
    root_pack = tmp_path / "root_pack"
    _write_pipeline_pack(env_pack, "env.pipeline.v1")
    _write_pipeline_pack(root_pack, "root.pipeline.v1")

    result = runner.invoke(
        app,
        [
            "pipelines",
            "list",
            "--root",
            str(root_pack),
            "--json",
        ],
        env={"CODEX_FARM_ROOT": str(env_pack)},
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    pipeline_ids = {row["pipeline_id"] for row in payload}
    assert pipeline_ids == {"root.pipeline.v1"}


def test_process_json_stdout_contract_and_workspace_root(monkeypatch, tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    workspace_root = tmp_path / "workspace"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.contract.v1"

    _write_pipeline_pack(pack, pipeline_id)
    workspace_root.mkdir(parents=True)
    input_dir.mkdir(parents=True)
    (input_dir / "a.json").write_text("{}", encoding="utf-8")
    (input_dir / "b.json").write_text("{}", encoding="utf-8")

    captured_cd_dirs: list[str] = []

    def fake_run_codex_exec(**kwargs):
        captured_cd_dirs.append(str(kwargs["cd_dir"]))
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)

        prompt_line = kwargs["prompt"].strip().splitlines()[-1]
        input_path = prompt_line.replace("Input file path: ", "")

        output_path.write_text(
            json.dumps({"ok": "OK", "source_path": input_path}),
            encoding="utf-8",
        )
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "process",
            "--root",
            str(pack),
            "--workspace-root",
            str(workspace_root),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "2",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr

    payload = json.loads(result.stdout)
    assert payload["pipeline_id"] == pipeline_id
    assert payload["status"] == "done"
    assert payload["counts"]["done"] == 2
    assert payload["counts"]["error"] == 0
    assert payload["counts"]["total"] == 2
    assert payload["input_dir"] == str(input_dir.resolve())
    assert payload["output_dir"] == str(output_dir.resolve())
    assert payload["farm_root"] == str(pack.resolve())
    assert payload["workspace_root"] == str(workspace_root.resolve())
    assert payload["exit_code"] == 0
    assert all(path == str(workspace_root.resolve()) for path in captured_cd_dirs)


def test_run_create_json_contract(tmp_path: Path) -> None:
    pack = tmp_path / "pack"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    pipeline_id = "demo.contract.v1"

    _write_pipeline_pack(pack, pipeline_id)
    input_dir.mkdir(parents=True)
    (input_dir / "one.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "run",
            "create",
            "--root",
            str(pack),
            "--pipeline",
            pipeline_id,
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["pipeline_id"] == pipeline_id
    assert payload["total"] == 1
    assert payload["input_dir"] == str(input_dir.resolve())
    assert payload["output_dir"] == str(output_dir.resolve())

    status_result = runner.invoke(
        app,
        [
            "run",
            "status",
            "--run-id",
            payload["run_id"],
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert status_result.exit_code == 0, status_result.stderr
    status_payload = json.loads(status_result.stdout)
    assert status_payload["run_id"] == payload["run_id"]
    assert status_payload["pipeline_id"] == pipeline_id
    assert status_payload["counts"]["total"] == 1


def test_run_errors_and_run_tasks_json(tmp_path: Path) -> None:
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
        pipeline_id="demo.contract.v1",
        input_dir=str(input_dir),
        glob="**/*.json",
        output_dir=str(output_dir),
        config={},
    )
    enqueue_tasks_for_run(
        conn,
        run_id=run_id,
        input_files=[file_a, file_b],
        input_root=input_dir,
        output_root=output_dir,
        output_ext=".json",
    )

    task_one = lease_one_task(conn, worker_id="w1", lease_seconds=30, run_id=run_id)
    task_two = lease_one_task(conn, worker_id="w2", lease_seconds=30, run_id=run_id)
    assert task_one is not None
    assert task_two is not None

    mark_task_done(conn, task_id=task_one["task_id"], output_path=str(output_dir / task_one["rel_output_path"]))
    mark_task_error(conn, task_id=task_two["task_id"], error="expected failure")

    errors_result = runner.invoke(
        app,
        [
            "run",
            "errors",
            "--run-id",
            run_id,
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert errors_result.exit_code == 0, errors_result.stderr
    errors_payload = json.loads(errors_result.stdout)
    assert len(errors_payload) == 1
    assert errors_payload[0]["status"] == "error"
    assert errors_payload[0]["error"] == "expected failure"

    done_result = runner.invoke(
        app,
        [
            "run",
            "tasks",
            "--run-id",
            run_id,
            "--status",
            "done",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert done_result.exit_code == 0, done_result.stderr
    done_payload = json.loads(done_result.stdout)
    assert len(done_payload) == 1
    assert done_payload[0]["status"] == "done"
