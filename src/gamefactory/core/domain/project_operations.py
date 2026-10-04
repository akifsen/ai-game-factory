"""Strict, versioned input contracts for native Godot project actions."""

from __future__ import annotations

from dataclasses import dataclass

PROJECT_OPERATION_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Game Factory native project operation",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "operation", "parameters"],
    "properties": {
        "schema_version": {"const": 1},
        "operation": {"enum": ["discover", "create", "editor", "run", "export", "release"]},
        "parameters": {"type": "object"},
    },
    "oneOf": [
        {
            "properties": {
                "operation": {"const": "discover"},
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"include_git": {"type": "boolean"}},
                },
            }
        },
        {
            "properties": {
                "operation": {"const": "create"},
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["destination", "name"],
                    "properties": {
                        "destination": {"type": "string", "minLength": 1, "maxLength": 4096},
                        "name": {"type": "string", "minLength": 1, "maxLength": 80},
                        "dimension": {"enum": ["2d", "3d"]},
                    },
                },
            }
        },
        *[
            {
                "properties": {
                    "operation": {"const": name},
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["executable", *(["preset"] if name == "export" else [])],
                        "properties": {
                            "executable": {"type": "string", "minLength": 1, "maxLength": 4096},
                            "timeout_seconds": {
                                "type": "number",
                                "exclusiveMinimum": 0,
                                "maximum": 900,
                            },
                            **extra,
                        },
                    },
                }
            }
            for name, extra in (
                ("editor", {}),
                ("run", {"scene": {"type": ["string", "null"], "maxLength": 1024}}),
                (
                    "export",
                    {
                        "preset": {"type": "string", "minLength": 1, "maxLength": 128},
                        "output_name": {"type": "string", "minLength": 1, "maxLength": 255},
                    },
                ),
            )
        ],
        {
            "properties": {
                "operation": {"const": "release"},
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["build_attempt_id"],
                    "properties": {
                        "build_attempt_id": {"type": "string", "minLength": 1, "maxLength": 80}
                    },
                },
            }
        },
    ],
}

PROJECT_OPERATION_TYPES = (
    "project_discover",
    "project_create",
    "godot_editor",
    "godot_run",
    "godot_export",
    "release_manifest",
    "record_evidence",
)


@dataclass(frozen=True)
class ProjectOperationRequest:
    operation: str
    parameters: dict[str, object]
    schema_version: int = 1

    @classmethod
    def from_dict(cls, value: object) -> ProjectOperationRequest:
        from gamefactory.core.domain.errors import ValidationError

        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "operation", "parameters"}
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
        ):
            raise ValidationError("Project operation request must match schema version 1")
        params = validate_project_operation_request(value["operation"], value["parameters"])
        return cls(value["operation"], params, 1)


def validate_project_operation_request(operation: str, parameters: object) -> dict[str, object]:
    """Validate an operation's actual runtime inputs, rejecting unknown or mistyped values."""
    import math

    from gamefactory.core.domain.errors import ValidationError

    fields: dict[str, dict[str, type | tuple[type, ...]]] = {
        "discover": {"include_git": bool},
        "create": {"destination": str, "name": str, "dimension": str},
        "editor": {"executable": str, "timeout_seconds": (int, float)},
        "run": {"executable": str, "scene": (str, type(None)), "timeout_seconds": (int, float)},
        "export": {
            "executable": str,
            "preset": str,
            "output_name": str,
            "timeout_seconds": (int, float),
        },
        "release": {"build_attempt_id": str},
    }
    required = {
        "create": {"destination", "name"},
        "editor": {"executable"},
        "run": {"executable"},
        "export": {"executable", "preset"},
        "release": {"build_attempt_id"},
    }
    if (
        not isinstance(operation, str)
        or operation not in fields
        or not isinstance(parameters, dict)
    ):
        raise ValidationError(
            "Operation request must use a supported discriminator and an object of parameters"
        )
    if set(parameters) - set(fields[operation]):
        raise ValidationError("Unknown project operation parameter")
    if not required.get(operation, set()).issubset(parameters):
        raise ValidationError("Project operation is missing a required parameter")
    for key, value in parameters.items():
        expected = fields[operation][key]
        if isinstance(value, bool) and expected is not bool:
            raise ValidationError(f"{key} has an invalid type")
        if not isinstance(value, expected):
            raise ValidationError(f"{key} has an invalid type")
        if isinstance(value, str) and (
            not value or len(value) > 4096 or any(ord(char) < 32 for char in value)
        ):
            raise ValidationError(f"{key} must be bounded text without control characters")
    limits = {"name": 80, "preset": 128, "output_name": 255, "scene": 1024, "build_attempt_id": 80}
    for key, limit in limits.items():
        value = parameters.get(key)
        if isinstance(value, str) and len(value) > limit:
            raise ValidationError(f"{key} exceeds its schema length limit")
    if operation == "create" and parameters.get("dimension", "2d") not in {"2d", "3d"}:
        raise ValidationError("dimension must be 2d or 3d")
    if "timeout_seconds" in parameters and (
        not math.isfinite(parameters["timeout_seconds"])
        or not 0 < parameters["timeout_seconds"] <= 900
    ):
        raise ValidationError("timeout_seconds must be finite and between 0 and 900")
    return dict(parameters)
