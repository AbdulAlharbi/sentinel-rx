"""Tiny JSON-schema subset validator (type, required, properties, items, enum, maxItems)."""

from __future__ import annotations

from typing import Any

_TYPES = {"object": dict, "array": list, "string": str, "number": (int, float), "boolean": bool}


def validate(obj: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """Return a list of violations (empty = valid)."""
    errors: list[str] = []
    t = schema.get("type")
    if t and not isinstance(obj, _TYPES[t]):
        return [f"{path}: expected {t}, got {type(obj).__name__}"]
    if "enum" in schema and obj not in schema["enum"]:
        errors.append(f"{path}: {obj!r} not in {schema['enum']}")
    if t == "object":
        for key in schema.get("required", []):
            if key not in obj:
                errors.append(f"{path}.{key}: missing")
        for key, sub in schema.get("properties", {}).items():
            if key in obj:
                errors += validate(obj[key], sub, f"{path}.{key}")
    if t == "array":
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        for i, item in enumerate(obj):
            errors += validate(item, schema.get("items", {}), f"{path}[{i}]")
    return errors
