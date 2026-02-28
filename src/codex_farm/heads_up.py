"""Heads Up adaptive prompt helpers and run-level learning."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import sqlite3
from typing import cast

from .codex_exec import CodexExecTimeoutError, run_codex_exec
from .db import (
    list_task_learning_rows,
    record_heads_up_tip_usage as db_record_heads_up_tip_usage,
    run_status as db_run_status,
    select_heads_up_tips as db_select_heads_up_tips,
    upsert_heads_up_tips,
)
from .paths import resolve_farm_root
from .pipeline_spec import load_pipelines
from .schema_utils import SchemaValidationError, validate_json_file_against_schema


CODEX_REASONING_EFFORT_VALUES = {"none", "minimal", "low", "medium", "high", "xhigh"}
DEFAULT_HEADS_UP_MAX_TIPS = 3
HEADS_UP_MAX_TIPS_MIN = 1
HEADS_UP_MAX_TIPS_MAX = 8
HEADS_UP_WILDCARD_SIGNATURE = "*"


def parse_heads_up_enabled(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def parse_heads_up_max_tips(value: object, *, default: int = DEFAULT_HEADS_UP_MAX_TIPS) -> int:
    parsed = default
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text:
            try:
                parsed = int(text)
            except ValueError:
                parsed = default
    return max(HEADS_UP_MAX_TIPS_MIN, min(HEADS_UP_MAX_TIPS_MAX, parsed))


def compute_input_signature(input_path: Path) -> str:
    try:
        raw = input_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "unknown"

    if isinstance(parsed, dict):
        keys = sorted(str(key) for key in parsed.keys())
        return "json_obj_keys:" + ",".join(keys)
    if isinstance(parsed, list):
        return "json_array"
    return "unknown"


def select_heads_up_tips(
    conn: sqlite3.Connection,
    *,
    pipeline_id: str,
    input_signature: str,
    limit: int,
) -> list[dict]:
    return db_select_heads_up_tips(
        conn,
        pipeline_id=pipeline_id,
        input_signature=input_signature,
        limit=limit,
    )


def append_heads_up_block(base_prompt: str, tips: list[dict]) -> str:
    tip_lines = [
        str(row.get("tip_text", "")).strip()
        for row in tips
        if str(row.get("tip_text", "")).strip()
    ]
    if not tip_lines:
        return base_prompt

    lines = ["Heads up for this task:"]
    for idx, tip in enumerate(tip_lines, start=1):
        lines.append(f"{idx}) {tip}")
    lines.append(
        "These are guardrails from prior runs. Follow them while still satisfying the output schema exactly."
    )

    suffix = "\n".join(lines)
    prefix = base_prompt.rstrip()
    if prefix:
        return f"{prefix}\n\n{suffix}\n"
    return f"{suffix}\n"


def record_tip_usage(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    task_id: str,
    tip_ids: list[str],
    outcome: str,
) -> int:
    return db_record_heads_up_tip_usage(
        conn,
        run_id=run_id,
        task_id=task_id,
        tip_ids=tip_ids,
        outcome=outcome,
    )


def _parse_run_config(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _path_from_config(value: object) -> Path | None:
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser().resolve()
    return None


def _resolve_learning_paths(*, farm_root: Path) -> tuple[Path, Path]:
    prompt_path = farm_root / "prompts" / "heads_up_distiller_v1.txt"
    schema_path = farm_root / "schemas" / "heads_up_tipset_v1.schema.json"
    return prompt_path, schema_path


def _build_observation_payload(*, run_id: str, task_rows: list[dict]) -> dict[str, object]:
    grouped: dict[str, dict[str, object]] = {}
    for row in task_rows:
        signature = compute_input_signature(Path(str(row["input_path"])))
        if signature not in grouped:
            grouped[signature] = {
                "input_signature": signature,
                "done": 0,
                "error": 0,
                "error_examples": [],
                "input_examples": [],
            }

        group = grouped[signature]
        status = str(row["status"])
        if status == "done":
            group["done"] = int(group["done"]) + 1
        elif status == "error":
            group["error"] = int(group["error"]) + 1
            error_examples = cast(list[str], group["error_examples"])
            if row.get("error") and len(error_examples) < 3:
                error_examples.append(str(row["error"]))

        input_examples = cast(list[str], group["input_examples"])
        if len(input_examples) < 2:
            input_examples.append(str(row["input_path"]))

    signatures = sorted(grouped.keys())
    done_total = sum(int(grouped[key]["done"]) for key in signatures)
    error_total = sum(int(grouped[key]["error"]) for key in signatures)
    return {
        "run_id": run_id,
        "totals": {"done": done_total, "error": error_total, "signatures": len(signatures)},
        "signatures": [grouped[key] for key in signatures],
    }


def _normalize_distilled_tips(
    *,
    payload: dict[str, object],
    known_signatures: set[str],
) -> list[dict[str, str]]:
    raw_tips = payload.get("tips")
    if not isinstance(raw_tips, list):
        return []

    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    per_signature_counts: dict[str, int] = defaultdict(int)

    for row in raw_tips:
        if not isinstance(row, dict):
            continue
        raw_signature = str(row.get("input_signature", "")).strip()
        tip_text = " ".join(str(row.get("tip_text", "")).split()).strip()
        if not tip_text:
            continue

        signature = raw_signature or HEADS_UP_WILDCARD_SIGNATURE
        if signature not in known_signatures and signature != HEADS_UP_WILDCARD_SIGNATURE:
            signature = HEADS_UP_WILDCARD_SIGNATURE
        if per_signature_counts[signature] >= 4:
            continue

        key = (signature, tip_text)
        if key in seen:
            continue
        seen.add(key)
        per_signature_counts[signature] += 1
        cleaned.append({"input_signature": signature, "tip_text": tip_text})

        if len(cleaned) >= 24:
            break

    return cleaned


def learn_heads_up_from_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    data_dir: Path,
    fallback_farm_root: Path | None = None,
    model_override: str | None = None,
    reasoning_effort_override: str | None = None,
) -> dict[str, object]:
    from .db import get_run

    try:
        run = get_run(conn, run_id)
    except KeyError as exc:
        return {"tips_added": 0, "warning": str(exc)}

    status = db_run_status(conn, run_id=run_id)
    run_status_value = str(status.get("status", ""))
    if run_status_value not in {"done", "error"}:
        return {
            "tips_added": 0,
            "warning": (
                "Heads Up learning requires a terminal run status "
                f"(done|error); current status is '{run_status_value or 'unknown'}'."
            ),
        }

    run_config = _parse_run_config(str(run.get("config_json", "{}")))
    configured_farm_root = _path_from_config(run_config.get("farm_root"))
    try:
        run_farm_root = resolve_farm_root(configured_farm_root or fallback_farm_root)
    except FileNotFoundError as exc:
        return {"tips_added": 0, "warning": str(exc)}

    pipelines = load_pipelines(run_farm_root / "pipelines")
    pipeline_id = str(run["pipeline_id"])
    spec = pipelines.get(pipeline_id)
    if spec is None:
        return {"tips_added": 0, "warning": f"Unknown pipeline_id for learning: {pipeline_id}"}

    task_rows = list_task_learning_rows(conn, run_id=run_id)
    if not task_rows:
        return {"tips_added": 0, "warning": None}

    observations = _build_observation_payload(run_id=run_id, task_rows=task_rows)
    prompt_template_path, output_schema_path = _resolve_learning_paths(farm_root=run_farm_root)
    if not prompt_template_path.exists():
        return {
            "tips_added": 0,
            "warning": f"Heads Up distiller prompt is missing: {prompt_template_path}",
        }
    if not output_schema_path.exists():
        return {
            "tips_added": 0,
            "warning": f"Heads Up distiller schema is missing: {output_schema_path}",
        }

    template = prompt_template_path.read_text(encoding="utf-8")
    prompt = (
        template.replace("{{PIPELINE_ID}}", spec.pipeline_id)
        .replace(
            "{{PIPELINE_PROMPT}}",
            spec.prompt_template_path.read_text(encoding="utf-8"),
        )
        .replace(
            "{{OBSERVATIONS_JSON}}",
            json.dumps(observations, indent=2, sort_keys=True),
        )
    )

    selected_model = model_override or str(run_config.get("codex_model") or spec.codex_model)
    configured_effort = run_config.get("codex_reasoning_effort")
    selected_effort: str | None = None
    if reasoning_effort_override is not None:
        selected_effort = reasoning_effort_override
    elif isinstance(configured_effort, str) and configured_effort in CODEX_REASONING_EFFORT_VALUES:
        selected_effort = configured_effort
    else:
        selected_effort = spec.codex_reasoning_effort

    learn_output_path = data_dir.resolve() / "heads_up" / f"learn-{run_id}.json"
    usage_log_csv = data_dir.resolve() / "codex_exec_activity.csv"
    usage_context = {
        "source": "heads_up.learn",
        "pipeline_id": spec.pipeline_id,
        "run_id": run_id,
        "heads_up_applied": False,
        "heads_up_tip_count": 0,
        "heads_up_tip_ids_json": "[]",
        "heads_up_tip_texts_json": "[]",
        "heads_up_tip_scores_json": "[]",
        "attempt_index": 1,
        "retry_context_applied": False,
        "retry_previous_error": None,
    }

    try:
        result = run_codex_exec(
            cd_dir=run_farm_root,
            prompt=prompt,
            model=selected_model,
            sandbox=spec.codex_sandbox,
            ask_for_approval=spec.codex_ask_for_approval,
            web_search=spec.codex_web_search,
            reasoning_effort=selected_effort,
            output_schema=output_schema_path,
            output_path=learn_output_path,
            timeout_seconds=spec.codex_timeout_seconds,
            usage_log_csv=usage_log_csv,
            usage_context=usage_context,
        )
    except CodexExecTimeoutError as exc:
        return {"tips_added": 0, "warning": str(exc)}

    if not result.ok:
        return {
            "tips_added": 0,
            "warning": f"Heads Up learning codex exec failed (exit={result.exit_code}): {result.stderr_tail}",
        }

    try:
        payload = validate_json_file_against_schema(
            json_path=learn_output_path,
            schema_path=output_schema_path,
        )
    except SchemaValidationError as exc:
        learn_output_path.unlink(missing_ok=True)
        return {"tips_added": 0, "warning": str(exc)}

    signature_rows = cast(list[dict[str, object]], observations["signatures"])
    known_signatures = {
        str(item["input_signature"])
        for item in signature_rows
        if "input_signature" in item
    }
    known_signatures.add(HEADS_UP_WILDCARD_SIGNATURE)
    normalized_tips = _normalize_distilled_tips(
        payload=payload if isinstance(payload, dict) else {},
        known_signatures=known_signatures,
    )
    tips_added = upsert_heads_up_tips(
        conn,
        pipeline_id=spec.pipeline_id,
        source_run_id=run_id,
        tips=normalized_tips,
    )
    return {"tips_added": tips_added, "warning": None}
