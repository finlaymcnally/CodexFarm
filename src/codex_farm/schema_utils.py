"""JSON Schema loading and validation utilities."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator


class SchemaValidationError(RuntimeError):
    pass


def load_json_file(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(f"Invalid JSON at {path}: {exc}") from exc


def validate_json_file_against_schema(*, json_path: Path, schema_path: Path) -> object:
    """Validate JSON file content against a schema and return parsed JSON on success."""
    payload = load_json_file(json_path)
    schema = load_json_file(schema_path)

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda err: err.path)
    if errors:
        first = errors[0]
        path = ".".join(str(p) for p in first.path)
        location = path if path else "<root>"
        raise SchemaValidationError(f"Schema validation failed at {location}: {first.message}")
    return payload
