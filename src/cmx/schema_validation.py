"""Bounded, offline JSON Schema validation for CMX capability contracts.

Only local fragment references are allowed. Validation never fetches remote schema
resources, and both schema documents and payloads have size/depth limits.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from itertools import islice
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing.exceptions import Unresolvable

MAX_SCHEMA_BYTES = 32_768
MAX_PAYLOAD_BYTES = 262_144
MAX_SCHEMA_DEPTH = 32
MAX_PAYLOAD_DEPTH = 64
MAX_VALIDATION_ERRORS = 10


class CapabilitySchemaError(ValueError):
    """Raised when a declared capability schema is unsafe or invalid."""


class CapabilityPayloadValidationError(ValueError):
    """Raised when capability input or output does not satisfy its schema."""


def _container_depth(value: Any) -> int:
    """Return maximum nesting of JSON containers without recursive traversal."""
    maximum = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            depth += 1
            maximum = max(maximum, depth)
            stack.extend((child, depth) for child in current.values())
        elif isinstance(current, list):
            depth += 1
            maximum = max(maximum, depth)
            stack.extend((child, depth) for child in current)
    return maximum


def _encode_json(value: Any, *, label: str, max_bytes: int, max_depth: int) -> str:
    if _container_depth(value) > max_depth:
        raise ValueError(f"{label} exceeds the maximum nesting depth of {max_depth}.")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} must be a finite, JSON-serializable value.") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{label} exceeds the maximum encoded size of {max_bytes} bytes.")
    return encoded


def _reject_remote_references(schema: Mapping[str, Any]) -> None:
    stack: list[Any] = [schema]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    if not isinstance(value, str) or not value.startswith("#"):
                        raise CapabilitySchemaError(
                            "Remote JSON Schema references are not permitted; use local fragment references."
                        )
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)


def validate_json_schema(schema: Mapping[str, Any], *, label: str = "Capability schema") -> None:
    """Validate a JSON Schema Draft 2020-12 document without network resolution."""
    if not isinstance(schema, Mapping):
        raise CapabilitySchemaError(f"{label} must be a JSON object.")
    try:
        _encode_json(
            schema,
            label=label,
            max_bytes=MAX_SCHEMA_BYTES,
            max_depth=MAX_SCHEMA_DEPTH,
        )
    except ValueError as exc:
        raise CapabilitySchemaError(str(exc)) from exc

    _reject_remote_references(schema)
    try:
        Draft202012Validator.check_schema(dict(schema))
    except SchemaError as exc:
        raise CapabilitySchemaError(
            f"{label} is not a valid JSON Schema Draft 2020-12 document."
        ) from exc


def _error_path(path: Any) -> str:
    result = "$"
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        elif isinstance(part, str) and part.isidentifier():
            result += f".{part}"
        else:
            result += f"[{json.dumps(part, ensure_ascii=False)}]"
    return result


def validate_json_payload(
    schema: Mapping[str, Any],
    payload: Any,
    *,
    label: str = "Capability payload",
) -> None:
    """Raise a safe, bounded error when a JSON value fails a declared schema."""
    try:
        validate_json_schema(schema)
        _encode_json(
            payload,
            label=label,
            max_bytes=MAX_PAYLOAD_BYTES,
            max_depth=MAX_PAYLOAD_DEPTH,
        )
    except CapabilitySchemaError:
        raise
    except ValueError as exc:
        raise CapabilityPayloadValidationError(str(exc)) from exc

    validator = Draft202012Validator(dict(schema), format_checker=FormatChecker())
    try:
        errors = list(islice(validator.iter_errors(payload), MAX_VALIDATION_ERRORS + 1))
    except (Unresolvable, RecursionError) as exc:
        raise CapabilitySchemaError(
            "Capability schema contains a reference that cannot be resolved safely."
        ) from exc

    if not errors:
        return

    rendered = [
        f"{_error_path(error.absolute_path)}: constraint '{str(error.validator)[:40]}' failed"
        for error in errors[:MAX_VALIDATION_ERRORS]
    ]
    if len(errors) > MAX_VALIDATION_ERRORS:
        rendered.append("additional validation errors omitted")
    raise CapabilityPayloadValidationError(f"{label} does not match its JSON Schema: " + "; ".join(rendered))
