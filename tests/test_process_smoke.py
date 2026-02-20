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
