"""Read-only pack and schema linting utilities."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from .paths import REPO_SENTINELS
from .pipeline_spec import parse_pipeline_model_file, resolve_repo_relative_path
from .schema_utils import (
    iter_properties_not_in_required,
    iter_schema_refs,
    json_pointer_exists,
    validate_schema_definition,
)


LintSeverity = Literal["error", "warning"]
LintTargetKind = Literal["pack", "schema"]

_SEVERITY_ORDER: dict[LintSeverity, int] = {"error": 0, "warning": 1}
_HEADS_UP_OPTIONAL_ASSETS = (
    Path("prompts/heads_up_distiller_v1.txt"),
    Path("schemas/heads_up_tipset_v1.schema.json"),
)
_PATH_MODE_PROMPT_TOKEN = "{{INPUT_PATH}}"
_INLINE_MODE_PROMPT_TOKEN = "{{INPUT_TEXT}}"


@dataclass(frozen=True)
class LintFinding:
    code: str
    severity: LintSeverity
    path: str
    message: str
    hint: str | None = None
    pipeline_id: str | None = None


@dataclass(frozen=True)
class LintReport:
    target_kind: LintTargetKind
    target_path: str
    pipeline_id: str | None
    findings: list[LintFinding]
    error_count: int
    warning_count: int
    scanned_pipeline_files: int
    scanned_schema_files: int


@dataclass(frozen=True)
class _LocalRefTarget:
    path: Path
    fragment: str


def lint_pack(*, root: Path, pipeline_id: str | None = None) -> LintReport:
    resolved_root = root.expanduser().resolve()
    findings: list[LintFinding] = []
    schema_pipeline_map: dict[Path, set[str]] = {}

    missing_sentinels = [
        sentinel
        for sentinel in REPO_SENTINELS
        if not (resolved_root / sentinel).exists()
    ]
    if missing_sentinels:
        findings.append(
            LintFinding(
                code="pack.missing_sentinel_dirs",
                severity="error",
                path=str(resolved_root),
                message=(
                    "Pack root is missing required folders: "
                    f"{', '.join(missing_sentinels)}"
                ),
                hint="Create pipelines/, prompts/, and schemas/ under the pack root.",
            )
        )

    pipeline_files, pipeline_findings = _discover_pipeline_files(
        root=resolved_root,
        pipeline_id=pipeline_id,
        missing_sentinels=missing_sentinels,
    )
    findings.extend(pipeline_findings)

    scanned_pipeline_files = 0
    seen_pipeline_ids: dict[str, Path] = {}
    for pipeline_file in pipeline_files:
        scanned_pipeline_files += 1
        file_findings, discovered_schema = _lint_pipeline_file(
            pipeline_file=pipeline_file,
            root=resolved_root,
            seen_pipeline_ids=seen_pipeline_ids,
        )
        findings.extend(file_findings)
        if discovered_schema is not None:
            schema_path, discovered_pipeline_id = discovered_schema
            schema_pipeline_map.setdefault(schema_path, set()).add(discovered_pipeline_id)

    findings.extend(_lint_heads_up_assets(root=resolved_root, missing_sentinels=missing_sentinels))

    scanned_schema_files = 0
    schema_dir = resolved_root / "schemas"
    if schema_dir.exists() and schema_dir.is_dir():
        schema_files = sorted(path for path in schema_dir.rglob("*.json") if path.is_file())
        scanned_schema_files = len(schema_files)
        schema_cache: dict[Path, object | Exception] = {}
        for schema_file in schema_files:
            resolved_schema = schema_file.resolve()
            pipelines_for_schema = sorted(schema_pipeline_map.get(resolved_schema, set()))
            associated_pipeline = pipelines_for_schema[0] if pipelines_for_schema else None
            findings.extend(
                _lint_schema_document(
                    schema_path=resolved_schema,
                    pipeline_id=associated_pipeline,
                    schema_cache=schema_cache,
                )
            )

    return _build_report(
        target_kind="pack",
        target_path=resolved_root,
        pipeline_id=pipeline_id,
        findings=findings,
        scanned_pipeline_files=scanned_pipeline_files,
        scanned_schema_files=scanned_schema_files,
    )


def lint_schema_file(*, schema_path: Path) -> LintReport:
    resolved_schema_path = schema_path.expanduser().resolve()
    findings: list[LintFinding] = []
    if not resolved_schema_path.exists() or not resolved_schema_path.is_file():
        findings.append(
            LintFinding(
                code="schema.invalid_json",
                severity="error",
                path=str(resolved_schema_path),
                message=f"Schema file does not exist or is not a file: {resolved_schema_path}",
                hint="Pass an existing JSON schema file to --schema.",
            )
        )
    else:
        findings.extend(
            _lint_schema_document(
                schema_path=resolved_schema_path,
                pipeline_id=None,
                schema_cache={},
            )
        )

    return _build_report(
        target_kind="schema",
        target_path=resolved_schema_path,
        pipeline_id=None,
        findings=findings,
        scanned_pipeline_files=0,
        scanned_schema_files=1,
    )


def lint_exit_code(report: LintReport, *, strict: bool) -> int:
    if report.error_count > 0:
        return 1
    if strict and report.warning_count > 0:
        return 1
    return 0


def _discover_pipeline_files(
    *,
    root: Path,
    pipeline_id: str | None,
    missing_sentinels: list[str],
) -> tuple[list[Path], list[LintFinding]]:
    findings: list[LintFinding] = []
    pipelines_dir = root / "pipelines"
    if "pipelines" in missing_sentinels or not pipelines_dir.exists() or not pipelines_dir.is_dir():
        return [], findings

    if pipeline_id is not None:
        target = pipelines_dir / f"{pipeline_id}.json"
        if not target.exists() or not target.is_file():
            findings.append(
                LintFinding(
                    code="pipeline.invalid_file",
                    severity="error",
                    path=str(target.resolve()),
                    pipeline_id=pipeline_id,
                    message=f"Pipeline file was not found for pipeline_id '{pipeline_id}'.",
                    hint="Verify the pipeline_id or remove --pipeline to lint all pipeline files.",
                )
            )
            return [], findings
        return [target.resolve()], findings

    pipeline_files = sorted(path.resolve() for path in pipelines_dir.glob("*.json") if path.is_file())
    if not pipeline_files:
        findings.append(
            LintFinding(
                code="pack.no_pipeline_files",
                severity="error",
                path=str(pipelines_dir.resolve()),
                message="Pack has no pipeline JSON files under pipelines/.",
                hint="Add at least one pipelines/*.json definition.",
            )
        )
    return pipeline_files, findings


def _lint_pipeline_file(
    *,
    pipeline_file: Path,
    root: Path,
    seen_pipeline_ids: dict[str, Path],
) -> tuple[list[LintFinding], tuple[Path, str] | None]:
    findings: list[LintFinding] = []

    try:
        model = parse_pipeline_model_file(pipeline_file)
    except ValueError as exc:
        findings.append(
            LintFinding(
                code="pipeline.invalid_file",
                severity="error",
                path=str(pipeline_file),
                message=f"Pipeline file is invalid: {exc}",
                hint="Fix JSON syntax and allowed pipeline fields.",
            )
        )
        return findings, None

    pipeline_id = model.pipeline_id
    first_path = seen_pipeline_ids.get(pipeline_id)
    if first_path is not None and first_path != pipeline_file:
        findings.append(
            LintFinding(
                code="pipeline.duplicate_id",
                severity="error",
                path=str(pipeline_file),
                pipeline_id=pipeline_id,
                message=(
                    f"Duplicate pipeline_id '{pipeline_id}' also exists in {first_path}."
                ),
                hint="Use unique pipeline_id values across pipelines/*.json.",
            )
        )
    else:
        seen_pipeline_ids[pipeline_id] = pipeline_file

    prompt_path = resolve_repo_relative_path(
        root,
        model.prompt_template_path,
        require_exists=False,
    )
    output_schema_path = resolve_repo_relative_path(
        root,
        model.output_schema_path,
        require_exists=False,
    )

    _lint_pipeline_asset_path(
        findings=findings,
        pipeline_file=pipeline_file,
        pipeline_id=pipeline_id,
        root=root,
        asset_label="prompt template",
        asset_code_missing="pipeline.missing_prompt_template",
        asset_rel_path=model.prompt_template_path,
        asset_abs_path=prompt_path,
    )
    _lint_pipeline_asset_path(
        findings=findings,
        pipeline_file=pipeline_file,
        pipeline_id=pipeline_id,
        root=root,
        asset_label="output schema",
        asset_code_missing="pipeline.missing_output_schema",
        asset_rel_path=model.output_schema_path,
        asset_abs_path=output_schema_path,
    )
    _lint_prompt_template_contract(
        findings=findings,
        pipeline_file=pipeline_file,
        pipeline_id=pipeline_id,
        prompt_path=prompt_path,
        prompt_input_mode=model.prompt_input_mode,
    )

    schema_discovery: tuple[Path, str] | None = None
    if output_schema_path.exists() and output_schema_path.is_file():
        schema_discovery = (output_schema_path.resolve(), pipeline_id)

    return findings, schema_discovery


def _lint_pipeline_asset_path(
    *,
    findings: list[LintFinding],
    pipeline_file: Path,
    pipeline_id: str,
    root: Path,
    asset_label: str,
    asset_code_missing: str,
    asset_rel_path: str,
    asset_abs_path: Path,
) -> None:
    if not _is_within_root(asset_abs_path, root):
        findings.append(
            LintFinding(
                code="pipeline.asset_outside_pack",
                severity="error",
                path=str(pipeline_file),
                pipeline_id=pipeline_id,
                message=(
                    f"Referenced {asset_label} resolves outside the pack root: {asset_rel_path}"
                ),
                hint="Use paths under the current pack root only.",
            )
        )

    if not asset_abs_path.exists() or not asset_abs_path.is_file():
        findings.append(
            LintFinding(
                code=asset_code_missing,
                severity="error",
                path=str(pipeline_file),
                pipeline_id=pipeline_id,
                message=(
                    f"Referenced {asset_label} does not exist: {asset_rel_path}"
                ),
                hint=f"Create the file or fix {asset_label.replace(' ', '_')} path.",
            )
        )


def _lint_prompt_template_contract(
    *,
    findings: list[LintFinding],
    pipeline_file: Path,
    pipeline_id: str,
    prompt_path: Path,
    prompt_input_mode: str,
) -> None:
    if not prompt_path.exists() or not prompt_path.is_file():
        return

    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
    except OSError as exc:
        findings.append(
            LintFinding(
                code="pipeline.unreadable_prompt_template",
                severity="error",
                path=str(pipeline_file),
                pipeline_id=pipeline_id,
                message=f"Prompt template could not be read: {exc}",
                hint="Ensure the prompt template file is readable UTF-8 text.",
            )
        )
        return

    required_token = (
        _INLINE_MODE_PROMPT_TOKEN
        if prompt_input_mode == "inline"
        else _PATH_MODE_PROMPT_TOKEN
    )
    if required_token in prompt_text:
        return

    findings.append(
        LintFinding(
            code="pipeline.prompt_missing_required_token",
            severity="error",
            path=str(pipeline_file),
            pipeline_id=pipeline_id,
            message=(
                "Prompt template does not include the required placeholder token "
                f"for prompt_input_mode '{prompt_input_mode}': {required_token}"
            ),
            hint=(
                "Add the required placeholder token to the prompt template "
                "or change prompt_input_mode."
            ),
        )
    )


def _lint_heads_up_assets(*, root: Path, missing_sentinels: list[str]) -> list[LintFinding]:
    if "prompts" in missing_sentinels or "schemas" in missing_sentinels:
        return []

    missing_assets = [
        rel_path.as_posix()
        for rel_path in _HEADS_UP_OPTIONAL_ASSETS
        if not (root / rel_path).exists()
    ]
    if not missing_assets:
        return []

    return [
        LintFinding(
            code="pack.missing_heads_up_assets",
            severity="warning",
            path=str(root),
            message=(
                "Heads Up learning assets are missing: "
                f"{', '.join(missing_assets)}"
            ),
            hint=(
                "Add these assets to enable full `heads-up learn` behavior; "
                "core pipeline execution can still work without them."
            ),
        )
    ]


def _lint_schema_document(
    *,
    schema_path: Path,
    pipeline_id: str | None,
    schema_cache: dict[Path, object | Exception],
) -> list[LintFinding]:
    findings: list[LintFinding] = []

    schema_document, json_error = _load_json_document(schema_path=schema_path, schema_cache=schema_cache)
    if json_error is not None:
        findings.append(
            LintFinding(
                code="schema.invalid_json",
                severity="error",
                path=str(schema_path),
                pipeline_id=pipeline_id,
                message=f"Schema file is not valid JSON: {json_error}",
                hint="Fix JSON syntax before rerunning lint.",
            )
        )
        return findings

    definition_error = validate_schema_definition(schema_document)
    if definition_error is not None:
        findings.append(
            LintFinding(
                code="schema.invalid_definition",
                severity="error",
                path=str(schema_path),
                pipeline_id=pipeline_id,
                message=(
                    "Schema definition is not valid Draft 2020-12: "
                    f"{definition_error}"
                ),
                hint="Adjust schema keywords/types to satisfy the Draft 2020-12 metaschema.",
            )
        )

    for ref_pointer, ref_value in iter_schema_refs(schema_document):
        findings.extend(
            _lint_schema_ref(
                source_schema_path=schema_path,
                source_pipeline_id=pipeline_id,
                ref_pointer=ref_pointer,
                ref_value=ref_value,
                schema_cache=schema_cache,
            )
        )

    for pointer, missing_properties in iter_properties_not_in_required(schema_document):
        joined = ", ".join(missing_properties)
        findings.append(
            LintFinding(
                code="schema.properties_not_in_required",
                severity="warning",
                path=str(schema_path),
                pipeline_id=pipeline_id,
                message=(
                    f"Object schema at {pointer} defines properties not listed in required: {joined}"
                ),
                hint=(
                    "Move these keys into required and model truly optional values "
                    "as nullable required fields."
                ),
            )
        )

    return findings


def _lint_schema_ref(
    *,
    source_schema_path: Path,
    source_pipeline_id: str | None,
    ref_pointer: str,
    ref_value: str,
    schema_cache: dict[Path, object | Exception],
) -> list[LintFinding]:
    local_target = _resolve_local_ref_target(
        source_schema_path=source_schema_path,
        ref_value=ref_value,
    )

    if local_target is None:
        return [
            LintFinding(
                code="schema.external_ref_not_supported",
                severity="warning",
                path=str(source_schema_path),
                pipeline_id=source_pipeline_id,
                message=(
                    f"External $ref is not supported: {ref_value} "
                    f"(at {ref_pointer})"
                ),
                hint="Use same-document fragments or local file references.",
            )
        ]

    target_path = local_target.path
    if not target_path.exists() or not target_path.is_file():
        return [
            LintFinding(
                code="schema.missing_local_ref",
                severity="error",
                path=str(source_schema_path),
                pipeline_id=source_pipeline_id,
                message=(
                    f"Local $ref target does not exist: {ref_value} "
                    f"(at {ref_pointer})"
                ),
                hint="Create the referenced file/path or correct the $ref value.",
            )
        ]

    target_document, target_error = _load_json_document(
        schema_path=target_path,
        schema_cache=schema_cache,
    )
    if target_error is not None:
        return [
            LintFinding(
                code="schema.missing_local_ref",
                severity="error",
                path=str(source_schema_path),
                pipeline_id=source_pipeline_id,
                message=(
                    f"Local $ref target is not valid JSON: {ref_value} "
                    f"(at {ref_pointer})"
                ),
                hint="Fix the referenced schema file so it is valid JSON.",
            )
        ]

    fragment = local_target.fragment
    if fragment:
        pointer = f"#{fragment}"
        if not pointer.startswith("#/"):
            return [
                LintFinding(
                    code="schema.missing_local_ref",
                    severity="error",
                    path=str(source_schema_path),
                    pipeline_id=source_pipeline_id,
                    message=(
                        f"Local $ref fragment is not a JSON pointer: {ref_value} "
                        f"(at {ref_pointer})"
                    ),
                    hint="Use JSON-pointer fragments such as #/$defs/MyDefinition.",
                )
            ]
        if not json_pointer_exists(target_document, pointer):
            return [
                LintFinding(
                    code="schema.missing_local_ref",
                    severity="error",
                    path=str(source_schema_path),
                    pipeline_id=source_pipeline_id,
                    message=(
                        f"Local $ref target pointer was not found: {ref_value} "
                        f"(at {ref_pointer})"
                    ),
                    hint="Fix the fragment path or add the missing definition.",
                )
            ]

    return []


def _resolve_local_ref_target(
    *,
    source_schema_path: Path,
    ref_value: str,
) -> _LocalRefTarget | None:
    parsed = urlsplit(ref_value)
    if parsed.scheme and parsed.scheme != "file":
        return None
    if parsed.netloc and parsed.scheme != "file":
        return None

    fragment = parsed.fragment
    if parsed.scheme == "file":
        target = Path(unquote(parsed.path)).expanduser().resolve()
        return _LocalRefTarget(path=target, fragment=fragment)

    path_text = unquote(parsed.path)
    if path_text:
        raw_target = Path(path_text)
        if raw_target.is_absolute():
            target = raw_target.resolve()
        else:
            target = (source_schema_path.parent / raw_target).resolve()
        return _LocalRefTarget(path=target, fragment=fragment)

    return _LocalRefTarget(path=source_schema_path.resolve(), fragment=fragment)


def _load_json_document(
    *,
    schema_path: Path,
    schema_cache: dict[Path, object | Exception],
) -> tuple[object, str | None]:
    cached = schema_cache.get(schema_path)
    if cached is not None:
        if isinstance(cached, Exception):
            return {}, str(cached)
        return cached, None

    try:
        loaded = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        schema_cache[schema_path] = exc
        return {}, str(exc)

    schema_cache[schema_path] = loaded
    return loaded, None


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _build_report(
    *,
    target_kind: LintTargetKind,
    target_path: Path,
    pipeline_id: str | None,
    findings: list[LintFinding],
    scanned_pipeline_files: int,
    scanned_schema_files: int,
) -> LintReport:
    sorted_findings = sorted(
        findings,
        key=lambda finding: (
            _SEVERITY_ORDER[finding.severity],
            finding.code,
            finding.path,
            finding.pipeline_id or "",
        ),
    )
    error_count = sum(1 for finding in sorted_findings if finding.severity == "error")
    warning_count = sum(1 for finding in sorted_findings if finding.severity == "warning")
    return LintReport(
        target_kind=target_kind,
        target_path=str(target_path),
        pipeline_id=pipeline_id,
        findings=sorted_findings,
        error_count=error_count,
        warning_count=warning_count,
        scanned_pipeline_files=scanned_pipeline_files,
        scanned_schema_files=scanned_schema_files,
    )
