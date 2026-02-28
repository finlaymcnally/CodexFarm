"""JSON Schema loading and validation utilities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


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


def validate_schema_definition(schema: object) -> str | None:
    """Return a schema-definition error message, or None when valid."""
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return str(exc)
    return None


def iter_schema_refs(document: object) -> Iterator[tuple[str, str]]:
    """Yield JSON-pointer locations and $ref values from a schema document."""
    yield from _iter_schema_refs(document, pointer="#")


def _iter_schema_refs(document: object, *, pointer: str) -> Iterator[tuple[str, str]]:
    if isinstance(document, dict):
        for key, value in document.items():
            child_pointer = f"{pointer}/{_escape_json_pointer_token(key)}"
            if key == "$ref" and isinstance(value, str):
                yield child_pointer, value
            yield from _iter_schema_refs(value, pointer=child_pointer)
        return

    if isinstance(document, list):
        for index, value in enumerate(document):
            child_pointer = f"{pointer}/{index}"
            yield from _iter_schema_refs(value, pointer=child_pointer)


def _escape_json_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape_json_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def json_pointer_exists(document: object, pointer: str) -> bool:
    """Return True if an RFC 6901 JSON pointer resolves in document."""
    if pointer in {"", "#"}:
        return True

    normalized = pointer[1:] if pointer.startswith("#") else pointer
    if not normalized.startswith("/"):
        return False

    current = document
    for token in normalized[1:].split("/"):
        key = _unescape_json_pointer_token(token)
        if isinstance(current, dict):
            if key not in current:
                return False
            current = current[key]
            continue
        if isinstance(current, list):
            if not key.isdigit():
                return False
            index = int(key)
            if index < 0 or index >= len(current):
                return False
            current = current[index]
            continue
        return False
    return True


def iter_properties_not_in_required(document: object) -> Iterator[tuple[str, list[str]]]:
    """Yield object-schema pointers and properties not listed in required."""
    yield from _iter_properties_not_in_required(document, pointer="#")


def _iter_properties_not_in_required(
    document: object,
    *,
    pointer: str,
) -> Iterator[tuple[str, list[str]]]:
    if isinstance(document, dict):
        properties = document.get("properties")
        if isinstance(properties, dict) and properties:
            required = document.get("required")
            required_keys: set[str] = set()
            if isinstance(required, list):
                required_keys = {item for item in required if isinstance(item, str)}
            missing = sorted(
                key for key in properties.keys() if isinstance(key, str) and key not in required_keys
            )
            if missing:
                yield pointer, missing

        for key, value in document.items():
            child_pointer = f"{pointer}/{_escape_json_pointer_token(str(key))}"
            yield from _iter_properties_not_in_required(value, pointer=child_pointer)
        return

    if isinstance(document, list):
        for index, value in enumerate(document):
            child_pointer = f"{pointer}/{index}"
            yield from _iter_properties_not_in_required(value, pointer=child_pointer)
