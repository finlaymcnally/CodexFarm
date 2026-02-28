import json
from pathlib import Path

from typer.testing import CliRunner

from codex_farm.cli import app
from codex_farm.codex_exec import CodexExecResult


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
