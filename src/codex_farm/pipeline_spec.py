"""Pipeline specification model and file loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


@dataclass(frozen=True)
class PipelineSpec:
    pipeline_id: str
    description: str
    prompt_template_path: Path
    output_schema_path: Path
    input_glob_default: str
    output_ext: str
    codex_model: str
    codex_sandbox: str
    codex_ask_for_approval: str
    codex_web_search: str
    codex_timeout_seconds: int


class PipelineSpecModel(BaseModel):
    """Validation model for on-disk pipeline JSON files."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    pipeline_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    prompt_template_path: str = Field(min_length=1)
    output_schema_path: str = Field(min_length=1)
    input_glob_default: str = "**/*.json"
    output_ext: str = ".json"
    codex_model: str = "gpt-5.3-codex-spark"
    codex_sandbox: str = "read-only"
    codex_ask_for_approval: str = "never"
    codex_web_search: str = "disabled"
    codex_timeout_seconds: int = Field(default=180, ge=1)

    @field_validator("output_ext")
    @classmethod
    def ensure_dot_ext(cls, value: str) -> str:
        if not value.startswith("."):
            raise ValueError("output_ext must start with '.'")
        return value


def _resolve_repo_relative(repo_root: Path, rel_path: str) -> Path:
    path = (repo_root / rel_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"Referenced file does not exist: {rel_path}")
    return path


def _to_spec(*, model: PipelineSpecModel, repo_root: Path) -> PipelineSpec:
    return PipelineSpec(
        pipeline_id=model.pipeline_id,
        description=model.description,
        prompt_template_path=_resolve_repo_relative(repo_root, model.prompt_template_path),
        output_schema_path=_resolve_repo_relative(repo_root, model.output_schema_path),
        input_glob_default=model.input_glob_default,
        output_ext=model.output_ext,
        codex_model=model.codex_model,
        codex_sandbox=model.codex_sandbox,
        codex_ask_for_approval=model.codex_ask_for_approval,
        codex_web_search=model.codex_web_search,
        codex_timeout_seconds=model.codex_timeout_seconds,
    )


def load_pipelines(pipelines_dir: Path) -> dict[str, PipelineSpec]:
    """Load every JSON pipeline spec from a directory keyed by pipeline_id."""
    repo_root = pipelines_dir.resolve().parent
    loaded: dict[str, PipelineSpec] = {}

    for path in sorted(pipelines_dir.glob("*.json")):
        try:
            raw_text = path.read_text(encoding="utf-8")
            model = PipelineSpecModel.model_validate_json(raw_text)
            spec = _to_spec(model=model, repo_root=repo_root)
        except (ValidationError, FileNotFoundError, ValueError) as exc:
            raise ValueError(f"Invalid pipeline file {path}: {exc}") from exc

        if spec.pipeline_id in loaded:
            raise ValueError(f"Duplicate pipeline_id found: {spec.pipeline_id}")
        loaded[spec.pipeline_id] = spec

    return loaded


def render_prompt_template(template_path: Path, input_path: Path) -> str:
    """Render a prompt template by replacing {{INPUT_PATH}}."""
    text = template_path.read_text(encoding="utf-8")
    return text.replace("{{INPUT_PATH}}", str(input_path.resolve()))
