import json
from pathlib import Path

from typer.testing import CliRunner

from codex_farm.cli import app
from codex_farm.codex_exec import CodexExecResult, CodexSessionTurnResult
from codex_farm.db import init_db, list_tasks_for_run, open_db


runner = CliRunner()


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
        "recipeInstructions": [{"@type": "HowToStep", "text": "Boil water."}],
    }


def test_process_command_smoke_with_mocked_codex(monkeypatch, tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)

    for idx in range(3):
        sample = {
            "@context": "https://schema.org",
            "@type": "Recipe",
            "name": f"Recipe {idx}",
            "recipeIngredient": ["1 cup water"],
            "recipeInstructions": ["Boil water."],
        }
        (input_dir / f"r{idx}.json").write_text(json.dumps(sample), encoding="utf-8")

    def fake_run_codex_exec(**kwargs):
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        name = Path(kwargs["prompt"].split("Input file path: ")[-1].strip().splitlines()[0]).stem
        output_path.write_text(json.dumps(_fake_recipe(name)), encoding="utf-8")
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "process",
            "--pipeline",
            "recipe.schemaorg.normalize.v1",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "2",
            "--data-dir",
            str(data_dir),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert (output_dir / "r0.json").exists()
    assert (output_dir / "r1.json").exists()
    assert (output_dir / "r2.json").exists()


def test_process_agentic_runtime_reuses_one_session(monkeypatch, tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)

    for idx in range(3):
        sample = {
            "@context": "https://schema.org",
            "@type": "Recipe",
            "name": f"Recipe {idx}",
            "recipeIngredient": ["1 cup water"],
            "recipeInstructions": ["Boil water."],
        }
        (input_dir / f"r{idx}.json").write_text(json.dumps(sample), encoding="utf-8")

    session_resume_key = "session-abc"
    started_prompts: list[str] = []
    resumed_prompts: list[str] = []

    def fake_start_codex_session(**kwargs):
        started_prompts.append(str(kwargs["prompt"]))
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        name = Path(kwargs["prompt"].split("Input file path: ")[-1].strip().splitlines()[0]).stem
        output_path.write_text(json.dumps(_fake_recipe(name)), encoding="utf-8")
        return CodexSessionTurnResult(
            ok=True,
            exit_code=0,
            stderr_tail="",
            resume_key=session_resume_key,
            thread_id=session_resume_key,
        )

    def fake_resume_codex_session(**kwargs):
        resumed_prompts.append(str(kwargs["prompt"]))
        assert kwargs["resume_key"] == session_resume_key
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        name = Path(kwargs["prompt"].split("Input file path: ")[-1].strip().splitlines()[0]).stem
        output_path.write_text(json.dumps(_fake_recipe(name)), encoding="utf-8")
        return CodexSessionTurnResult(
            ok=True,
            exit_code=0,
            stderr_tail="",
            resume_key=session_resume_key,
            thread_id=session_resume_key,
        )

    monkeypatch.setattr("codex_farm.session_runtime.start_codex_session", fake_start_codex_session)
    monkeypatch.setattr(
        "codex_farm.session_runtime.resume_codex_session",
        fake_resume_codex_session,
    )

    result = runner.invoke(
        app,
        [
            "process",
            "--pipeline",
            "recipe.schemaorg.normalize.v1",
            "--runtime-mode",
            "structured_loop_agentic_v1",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--no-login-precheck",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["runtime_mode"] == "structured_loop_agentic_v1"
    assert payload["effective_workers"] == 1
    assert payload["session_count"] == 1
    assert payload["fresh_session_count"] == 1
    assert payload["session_turn_count_total"] == 3
    assert payload["tasks_per_session_summary"]["values"] == [3]
    assert len(started_prompts) == 1
    assert len(resumed_prompts) == 2
    assert (output_dir / "r0.json").exists()
    assert (output_dir / "r1.json").exists()
    assert (output_dir / "r2.json").exists()

    session_json = output_dir / ".codex-farm-sessions" / "1" / "session.json"
    assert session_json.exists()
    session_payload = json.loads(session_json.read_text(encoding="utf-8"))
    assert session_payload["task_count"] == 3
    assert session_payload["turn_count"] == 3
    assert session_payload["resume_key"] == session_resume_key

    conn = open_db(data_dir / "codex_farm.sqlite3")
    init_db(conn)
    tasks = list_tasks_for_run(conn, run_id=payload["run_id"])
    assert {task["session_row_id"] for task in tasks} == {1}
    assert sum(int(task["fresh_session_started"] or 0) for task in tasks) == 1


def test_process_agentic_runtime_rejects_multiple_workers(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)
    (input_dir / "r0.json").write_text(json.dumps({"name": "Recipe 0"}), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "process",
            "--pipeline",
            "recipe.schemaorg.normalize.v1",
            "--runtime-mode",
            "structured_loop_agentic_v1",
            "--workers",
            "2",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--data-dir",
            str(data_dir),
            "--no-login-precheck",
            "--json",
        ],
    )

    assert result.exit_code != 0
    assert "--workers=1" in result.stderr


def test_process_command_recovers_after_transient_rate_limits(
    monkeypatch,
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)

    for idx in range(3):
        sample = {
            "@context": "https://schema.org",
            "@type": "Recipe",
            "name": f"Recipe {idx}",
            "recipeIngredient": ["1 cup water"],
            "recipeInstructions": ["Boil water."],
        }
        (input_dir / f"r{idx}.json").write_text(json.dumps(sample), encoding="utf-8")

    call_count = 0

    def fake_run_codex_exec(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return CodexExecResult(
                ok=False,
                exit_code=1,
                stderr_tail="HTTP 429 Too Many Requests: retry after 1 seconds",
            )
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        name = Path(kwargs["prompt"].split("Input file path: ")[-1].strip().splitlines()[0]).stem
        output_path.write_text(json.dumps(_fake_recipe(name)), encoding="utf-8")
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "process",
            "--pipeline",
            "recipe.schemaorg.normalize.v1",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "1",
            "--max-attempts",
            "3",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["counts"]["error"] == 0
    assert payload["counts"]["queued"] == 0
    assert payload["counts"]["done"] == 3
    assert payload["counts"]["canceled"] == 0
    assert payload["control_state"] == "active"
    assert call_count >= 5
    assert "cooling for" in result.stderr

    errors_result = runner.invoke(
        app,
        [
            "run",
            "errors",
            "--run-id",
            payload["run_id"],
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert errors_result.exit_code == 0
    assert json.loads(errors_result.stdout) == []


def test_process_command_gives_up_after_persistent_rate_limits(
    monkeypatch,
    tmp_path: Path,
) -> None:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)

    for idx in range(3):
        (input_dir / f"r{idx}.json").write_text(
            json.dumps({"name": f"Recipe {idx}"}),
            encoding="utf-8",
        )

    call_count = 0

    def fake_run_codex_exec(**kwargs):
        nonlocal call_count
        call_count += 1
        return CodexExecResult(
            ok=False,
            exit_code=1,
            stderr_tail="HTTP 429 Too Many Requests: retry after 1 seconds",
        )

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    result = runner.invoke(
        app,
        [
            "process",
            "--pipeline",
            "recipe.schemaorg.normalize.v1",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir),
            "--workers",
            "1",
            "--max-attempts",
            "3",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["counts"]["error"] == 0
    assert payload["counts"]["queued"] == 3
    assert payload["counts"]["done"] == 0
    assert payload["exit_code"] == 1
    assert call_count >= 6
    assert "budget exhausted" in result.stderr


def test_process_incremental_reuses_unchanged_inputs(monkeypatch, tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    output_dir1 = tmp_path / "output1"
    output_dir2 = tmp_path / "output2"
    output_dir3 = tmp_path / "output3"
    data_dir = tmp_path / "var"
    input_dir.mkdir(parents=True)

    for idx in range(3):
        sample = {
            "@context": "https://schema.org",
            "@type": "Recipe",
            "name": f"Recipe {idx}",
            "recipeIngredient": ["1 cup water"],
            "recipeInstructions": ["Boil water."],
        }
        (input_dir / f"r{idx}.json").write_text(json.dumps(sample), encoding="utf-8")

    call_count = 0

    def fake_run_codex_exec(**kwargs):
        nonlocal call_count
        call_count += 1
        output_path = kwargs["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        name = Path(kwargs["prompt"].split("Input file path: ")[-1].strip().splitlines()[0]).stem
        output_path.write_text(json.dumps(_fake_recipe(name)), encoding="utf-8")
        return CodexExecResult(ok=True, exit_code=0, stderr_tail="")

    monkeypatch.setattr("codex_farm.worker.run_codex_exec", fake_run_codex_exec)

    first = runner.invoke(
        app,
        [
            "process",
            "--pipeline",
            "recipe.schemaorg.normalize.v1",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir1),
            "--workers",
            "2",
            "--data-dir",
            str(data_dir),
            "--json",
        ],
    )
    assert first.exit_code == 0, first.stderr
    first_payload = json.loads(first.stdout)
    assert first_payload["incremental"]["enabled"] is False
    assert call_count == 3

    second = runner.invoke(
        app,
        [
            "process",
            "--pipeline",
            "recipe.schemaorg.normalize.v1",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir2),
            "--workers",
            "2",
            "--data-dir",
            str(data_dir),
            "--incremental",
            "--json",
        ],
    )
    assert second.exit_code == 0, second.stderr
    second_payload = json.loads(second.stdout)
    assert second_payload["incremental"]["enabled"] is True
    assert second_payload["incremental"]["reused"] == 3
    assert second_payload["incremental"]["queued"] == 0
    assert call_count == 3

    changed = json.loads((input_dir / "r1.json").read_text(encoding="utf-8"))
    changed["name"] = "Recipe 1 changed"
    (input_dir / "r1.json").write_text(json.dumps(changed), encoding="utf-8")

    third = runner.invoke(
        app,
        [
            "process",
            "--pipeline",
            "recipe.schemaorg.normalize.v1",
            "--in",
            str(input_dir),
            "--out",
            str(output_dir3),
            "--workers",
            "2",
            "--data-dir",
            str(data_dir),
            "--incremental",
            "--json",
        ],
    )
    assert third.exit_code == 0, third.stderr
    third_payload = json.loads(third.stdout)
    assert third_payload["incremental"]["enabled"] is True
    assert third_payload["incremental"]["reused"] == 2
    assert third_payload["incremental"]["queued"] == 1
    assert call_count == 4
