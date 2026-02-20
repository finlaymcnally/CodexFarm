from pathlib import Path

from codex_farm.paths import find_repo_root
from codex_farm.pipeline_spec import load_pipelines, render_prompt_template


def test_load_pipelines_reads_known_specs() -> None:
    repo_root = find_repo_root()
    pipelines = load_pipelines(repo_root / "pipelines")

    assert "recipe.schemaorg.normalize.v1" in pipelines
    assert "recipe.schemaorg.to_proprietary.v1" in pipelines


def test_render_prompt_template_replaces_input_path(tmp_path: Path) -> None:
    template = tmp_path / "template.txt"
    input_file = tmp_path / "recipe.json"

    template.write_text("Path={{INPUT_PATH}}", encoding="utf-8")
    input_file.write_text("{}", encoding="utf-8")

    rendered = render_prompt_template(template, input_file)

    assert "{{INPUT_PATH}}" not in rendered
    assert str(input_file.resolve()) in rendered
