"""Build caller-facing autotune payloads from telemetry playbooks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import difflib
import json
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class AutotuneContext:
    """Execution context used to generate concrete autotune suggestions."""

    run_id: str | None
    pipeline_id: str | None
    input_dir: str | None
    output_dir: str | None
    workers: int | None
    codex_model: str | None
    codex_reasoning_effort: str | None
    codex_timeout_seconds: int | None
    prompt_template_path: Path | None
    pipeline_json_path: Path | None
    output_schema_path: Path | None


def build_autotune_payload(
    *,
    telemetry_report: Mapping[str, object],
    context: AutotuneContext,
) -> dict[str, object]:
    """Convert telemetry report signals into caller-ready flags and diffs."""
    playbook = telemetry_report.get("tuning_playbook")
    playbook_sections = playbook if isinstance(playbook, Mapping) else {}

    flag_overrides = _build_flag_overrides(
        playbook_sections=playbook_sections,
        context=context,
    )
    prompt_template_diff = _build_prompt_template_diff(
        playbook_sections=playbook_sections,
        prompt_template_path=context.prompt_template_path,
    )
    pipeline_config_diff = _build_pipeline_config_diff(
        pipeline_json_path=context.pipeline_json_path,
        flag_overrides=flag_overrides,
        playbook_sections=playbook_sections,
    )

    command_preview = _build_command_preview(
        context=context,
        flag_overrides=flag_overrides,
    )

    return {
        "schema_version": 1,
        "generated_at_utc": _utc_ts(datetime.now(UTC)),
        "run_id": context.run_id,
        "pipeline_id": context.pipeline_id,
        "telemetry_schema_version": telemetry_report.get("schema_version"),
        "telemetry_filters": telemetry_report.get("filters"),
        "warnings": list(telemetry_report.get("warnings", []))
        if isinstance(telemetry_report.get("warnings"), list)
        else [],
        "flag_overrides": flag_overrides,
        "command_preview": command_preview,
        "prompt_template_diff": prompt_template_diff,
        "pipeline_config_diff": pipeline_config_diff,
        "schema_edits": _list_rows(playbook_sections.get("schema_edits")),
        "input_prechecks": _list_rows(playbook_sections.get("input_prechecks")),
        "runtime_tuning": _list_rows(playbook_sections.get("runtime_tuning")),
        "model_tuning": _list_rows(playbook_sections.get("model_tuning")),
    }


def _build_flag_overrides(
    *,
    playbook_sections: Mapping[str, object],
    context: AutotuneContext,
) -> list[dict[str, object]]:
    overrides: list[dict[str, object]] = []
    existing_flags: set[str] = set()

    def add(flag: str, suggested: str, item: Mapping[str, object], current: str | None) -> None:
        if not suggested:
            return
        if flag in existing_flags:
            return
        existing_flags.add(flag)
        overrides.append(
            {
                "flag": flag,
                "current": current,
                "suggested": suggested,
                "source_item_id": _clean_text(item.get("id")),
                "priority": _clean_text(item.get("priority")),
                "trigger": _clean_text(item.get("trigger")),
                "evidence": item.get("evidence") if isinstance(item.get("evidence"), Mapping) else {},
            }
        )

    runtime_rows = _list_rows(playbook_sections.get("runtime_tuning"))
    for item in runtime_rows:
        item_id = _clean_text(item.get("id"))
        if item_id == "runtime.rate_limit_backoff":
            current_workers = context.workers
            if current_workers is None:
                continue
            suggested_workers = max(1, current_workers // 2)
            if suggested_workers == current_workers:
                continue
            add(
                "--workers",
                str(suggested_workers),
                item=item,
                current=str(current_workers),
            )

    model_rows = _list_rows(playbook_sections.get("model_tuning"))
    for item in model_rows:
        item_id = _clean_text(item.get("id"))
        if item_id != "model.prefer_high_success_config":
            continue
        evidence = item.get("evidence")
        evidence_map = evidence if isinstance(evidence, Mapping) else {}
        best_model = _clean_text(evidence_map.get("best_model"))
        best_effort = _clean_text(evidence_map.get("best_reasoning_effort"))
        if best_model and best_model != _clean_text(context.codex_model):
            add(
                "--model",
                best_model,
                item=item,
                current=context.codex_model,
            )
        if best_effort and best_effort != _clean_text(context.codex_reasoning_effort):
            add(
                "--reasoning-effort",
                best_effort,
                item=item,
                current=context.codex_reasoning_effort,
            )

    return overrides


def _build_prompt_template_diff(
    *,
    playbook_sections: Mapping[str, object],
    prompt_template_path: Path | None,
) -> dict[str, object] | None:
    if prompt_template_path is None:
        return None
    resolved = prompt_template_path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_file():
        return None

    original = resolved.read_text(encoding="utf-8")
    lines_to_add: list[str] = []
    applied_items: list[str] = []

    for item in _list_rows(playbook_sections.get("prompt_edits")):
        item_id = _clean_text(item.get("id"))
        candidate_lines = _prompt_lines_for_item(item)
        if not candidate_lines:
            continue
        for line in candidate_lines:
            if not line:
                continue
            if line in lines_to_add:
                continue
            if line in original:
                continue
            lines_to_add.append(line)
        if candidate_lines:
            applied_items.append(item_id)

    if not lines_to_add:
        return None

    new_text = original.rstrip("\n")
    if new_text:
        new_text += "\n\n"
    new_text += "\n".join(lines_to_add).rstrip("\n") + "\n"

    diff_text = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(resolved),
            tofile=f"{resolved} (autotune)",
        )
    )
    if not diff_text:
        return None

    return {
        "path": str(resolved),
        "applied_item_ids": [item for item in applied_items if item],
        "appended_lines": lines_to_add,
        "diff": diff_text,
    }


def _build_pipeline_config_diff(
    *,
    pipeline_json_path: Path | None,
    flag_overrides: list[dict[str, object]],
    playbook_sections: Mapping[str, object],
) -> dict[str, object] | None:
    if pipeline_json_path is None:
        return None
    resolved = pipeline_json_path.expanduser().resolve()
    if not resolved.exists() or not resolved.is_file():
        return None

    try:
        original_text = resolved.read_text(encoding="utf-8")
        parsed = json.loads(original_text)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None

    updated = dict(parsed)
    applied_changes: list[dict[str, object]] = []

    for override in flag_overrides:
        flag = _clean_text(override.get("flag"))
        suggested = _clean_text(override.get("suggested"))
        if not suggested:
            continue
        if flag == "--model":
            if updated.get("codex_model") == suggested:
                continue
            updated["codex_model"] = suggested
            applied_changes.append({"key": "codex_model", "suggested": suggested, "source_flag": flag})
        elif flag == "--reasoning-effort":
            if updated.get("codex_reasoning_effort") == suggested:
                continue
            updated["codex_reasoning_effort"] = suggested
            applied_changes.append(
                {"key": "codex_reasoning_effort", "suggested": suggested, "source_flag": flag}
            )

    for item in _list_rows(playbook_sections.get("runtime_tuning")):
        if _clean_text(item.get("id")) != "runtime.timeout_increase":
            continue
        current_timeout = _as_int(updated.get("codex_timeout_seconds"))
        if current_timeout is None or current_timeout <= 0:
            continue
        suggested_timeout = max(current_timeout + 1, int(round(current_timeout * 1.5)))
        if suggested_timeout == current_timeout:
            continue
        updated["codex_timeout_seconds"] = suggested_timeout
        applied_changes.append(
            {
                "key": "codex_timeout_seconds",
                "suggested": suggested_timeout,
                "source_item_id": _clean_text(item.get("id")),
            }
        )

    if not applied_changes:
        return None

    new_text = json.dumps(updated, indent=2, sort_keys=False) + "\n"
    diff_text = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(resolved),
            tofile=f"{resolved} (autotune)",
        )
    )
    if not diff_text:
        return None

    return {
        "path": str(resolved),
        "applied_changes": applied_changes,
        "diff": diff_text,
    }


def _build_command_preview(
    *,
    context: AutotuneContext,
    flag_overrides: list[dict[str, object]],
) -> str:
    cmd: list[str] = ["codex-farm", "process"]
    if context.pipeline_id:
        cmd.extend(["--pipeline", context.pipeline_id])
    if context.input_dir:
        cmd.extend(["--in", context.input_dir])
    if context.output_dir:
        cmd.extend(["--out", context.output_dir])

    for row in flag_overrides:
        flag = _clean_text(row.get("flag"))
        suggested = _clean_text(row.get("suggested"))
        if not flag or not suggested:
            continue
        cmd.extend([flag, suggested])

    return " ".join(cmd)


def _prompt_lines_for_item(item: Mapping[str, object]) -> list[str]:
    item_id = _clean_text(item.get("id"))
    if item_id == "prompt.raw_json_only_footer":
        return [
            "Return only JSON matching the configured output schema.",
            "Do not include markdown, prose, or code fences.",
        ]
    if item_id == "prompt.no_extra_keys":
        return ["Do not emit keys that are not defined in the output schema."]
    if item_id == "prompt.schema_path_contract":
        evidence = item.get("evidence")
        evidence_map = evidence if isinstance(evidence, Mapping) else {}
        top_paths = evidence_map.get("top_schema_paths")
        if isinstance(top_paths, list) and top_paths:
            first = top_paths[0]
            if isinstance(first, Mapping):
                path = _clean_text(first.get("path"))
                if path:
                    return [f"Strictly satisfy schema constraints for `{path}`."]
        return []
    if item_id == "prompt.retry_context_compact":
        return [
            "When retry context is present, focus on fixing that exact failure first.",
            "Keep output strict to the schema; do not add extra explanation.",
        ]
    return []


def _list_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            rows.append(dict(item))
    return rows


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _as_int(value: object) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _utc_ts(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
