"""Pipeline specification model and file loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


CodexCdMode = Literal["asset_root", "input_dir", "input_file_dir"]
CodexReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh"]
PromptInputMode = Literal["path", "inline"]


@dataclass(frozen=True)
class PipelineSpec:
    source_path: Path
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
    codex_reasoning_effort: CodexReasoningEffort | None
    codex_timeout_seconds: int
    codex_cd_mode: CodexCdMode
    prompt_input_mode: PromptInputMode


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
    codex_reasoning_effort: CodexReasoningEffort | None = None
    codex_timeout_seconds: int = Field(default=180, ge=1)
    codex_cd_mode: CodexCdMode = "asset_root"
    prompt_input_mode: PromptInputMode = "path"

    @field_validator("output_ext")
    @classmethod
    def ensure_dot_ext(cls, value: str) -> str:
        if not value.startswith("."):
            raise ValueError("output_ext must start with '.'")
        return value


def _resolve_repo_relative(repo_root: Path, rel_path: str) -> Path:
    return resolve_repo_relative_path(repo_root, rel_path, require_exists=True)


def resolve_repo_relative_path(
    repo_root: Path,
    rel_path: str,
    *,
    require_exists: bool = True,
) -> Path:
    """Resolve a repo-relative path, optionally requiring that it already exists."""
    path = (repo_root / rel_path).resolve()
    if require_exists and not path.exists():
        raise FileNotFoundError(f"Referenced file does not exist: {rel_path}")
    return path


def parse_pipeline_model_file(path: Path) -> PipelineSpecModel:
    """Parse and validate a single pipeline JSON file."""
    raw_text = path.read_text(encoding="utf-8")
    try:
        return PipelineSpecModel.model_validate_json(raw_text)
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


def _to_spec(
    *,
    model: PipelineSpecModel,
    repo_root: Path,
    source_path: Path,
) -> PipelineSpec:
    return PipelineSpec(
        source_path=source_path.resolve(),
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
        codex_reasoning_effort=model.codex_reasoning_effort,
        codex_timeout_seconds=model.codex_timeout_seconds,
        codex_cd_mode=model.codex_cd_mode,
        prompt_input_mode=model.prompt_input_mode,
    )


def load_pipelines(pipelines_dir: Path) -> dict[str, PipelineSpec]:
    """Load every JSON pipeline spec from a directory keyed by pipeline_id."""
    repo_root = pipelines_dir.resolve().parent
    loaded: dict[str, PipelineSpec] = {}

    for path in sorted(pipelines_dir.glob("*.json")):
        try:
            model = parse_pipeline_model_file(path)
            spec = _to_spec(model=model, repo_root=repo_root, source_path=path)
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(f"Invalid pipeline file {path}: {exc}") from exc

        if spec.pipeline_id in loaded:
            raise ValueError(f"Duplicate pipeline_id found: {spec.pipeline_id}")
        loaded[spec.pipeline_id] = spec

    return loaded


def render_prompt_template(template_path: Path, input_path: Path) -> str:
    """Render a prompt template by replacing supported input placeholders."""
    text = template_path.read_text(encoding="utf-8")
    rendered = text.replace("{{INPUT_PATH}}", str(input_path.resolve()))
    if "{{INPUT_TEXT}}" in rendered:
        input_text = input_path.read_text(encoding="utf-8")
        rendered = rendered.replace("{{INPUT_TEXT}}", input_text)
    return rendered
