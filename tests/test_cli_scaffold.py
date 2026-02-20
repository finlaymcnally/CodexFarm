from pathlib import Path

from typer.testing import CliRunner

from codex_farm.cli import app


runner = CliRunner()


def test_pipelines_new_generates_files(tmp_path: Path) -> None:
    for folder in ("pipelines", "prompts", "schemas"):
        (tmp_path / folder).mkdir(parents=True)

    result = runner.invoke(
        app,
        ["pipelines", "new", "--pipeline-id", "demo.pipeline.v1"],
        env={"CODEX_FARM_ROOT": str(tmp_path)},
    )

    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "pipelines" / "demo.pipeline.v1.json").exists()
    assert (tmp_path / "prompts" / "demo_pipeline_v1.txt").exists()
    assert (tmp_path / "schemas" / "demo_pipeline_v1.schema.json").exists()
