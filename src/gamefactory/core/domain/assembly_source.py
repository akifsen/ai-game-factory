"""Source registration for operator-authored assemblies (ADR 0016).

A ``local_operator_assembly`` source is a self-contained GLB supplied by an
operator. Its registration records where the geometry came from; it is never a
paid provider result. ``source_front`` is required and describes the source
file, not the desired asset (ADR 0014). ``derived_from`` is provenance only and
is never resolved.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gamefactory.core.domain.errors import SpecInvalidError

REGISTRATION_SCHEMA_VERSION = "asset-source-registration-0.7.0"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class AuthoringTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=80)
    version: str = Field(..., min_length=1, max_length=80)


class PartMapEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_id: str
    role: str
    parent: str

    @field_validator("part_id", "role")
    @classmethod
    def identifier(cls, value: str) -> str:
        if not _IDENT.fullmatch(value):
            raise ValueError(f"'{value}' is not a lowercase identifier")
        return value


class SocketMapEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    socket_id: str
    parent_part: str


class DerivedFrom(BaseModel):
    """Provenance only. The factory never resolves or loads these references."""

    model_config = ConfigDict(extra="forbid")

    part_id: str
    source_artifact_sha256: str
    source_asset_id: str | None = None
    source_revision: int | None = Field(default=None, ge=1)

    @field_validator("source_artifact_sha256")
    @classmethod
    def hex_digest(cls, value: str) -> str:
        if not _HEX64.fullmatch(value):
            raise ValueError("source_artifact_sha256 must be a lowercase SHA-256 hex digest")
        return value


class AssemblySourceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["asset-source-registration-0.7.0"] = "asset-source-registration-0.7.0"
    source_provenance_type: Literal["local_operator_assembly"]
    paid: bool
    artifact_sha256: str
    artifact_bytes: int = Field(..., gt=0)
    authoring_tool: AuthoringTool
    source_front: Literal["-Z", "+Z"]
    part_map: list[PartMapEntry] = Field(..., min_length=1)
    socket_map: list[SocketMapEntry] = Field(default_factory=list)
    derived_from: list[DerivedFrom] = Field(default_factory=list)
    actor: str = Field(..., min_length=1, max_length=120)
    reason: str = Field(..., min_length=1, max_length=1000)

    @field_validator("artifact_sha256")
    @classmethod
    def hex_digest(cls, value: str) -> str:
        if not _HEX64.fullmatch(value):
            raise ValueError("artifact_sha256 must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("paid")
    @classmethod
    def never_paid(cls, value: bool) -> bool:
        if value is not False:
            raise ValueError("a local_operator_assembly source is never paid (paid must be false)")
        return value

    @field_validator("actor", "reason")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("actor and reason must not be blank")
        return value

    @model_validator(mode="after")
    def maps_are_unique(self) -> AssemblySourceRegistration:
        ids = [p.part_id for p in self.part_map]
        if len(ids) != len(set(ids)):
            raise ValueError("part_map contains a duplicate part_id")
        sockets = [s.socket_id for s in self.socket_map]
        if len(sockets) != len(set(sockets)):
            raise ValueError("socket_map contains a duplicate socket_id")
        for entry in self.derived_from:
            if entry.part_id not in ids:
                raise ValueError(f"derived_from names unknown part '{entry.part_id}'")
        return self

    def check_against_spec(self, spec: Any) -> None:
        """The declared maps must equal the specification's parts and sockets."""
        spec_parts = sorted(
            (p.part_id, p.role, p.parent) for p in (getattr(spec, "parts", None) or [])
        )
        declared = sorted((p.part_id, p.role, p.parent) for p in self.part_map)
        if spec_parts != declared:
            raise SpecInvalidError(
                "source registration part_map does not equal the specification parts",
                details={"part_map": declared, "specification": spec_parts},
            )
        spec_sockets = sorted(
            (s.socket_id, s.parent_part) for s in (getattr(spec, "sockets", None) or [])
        )
        declared_sockets = sorted((s.socket_id, s.parent_part) for s in self.socket_map)
        if spec_sockets != declared_sockets:
            raise SpecInvalidError(
                "source registration socket_map does not equal the specification sockets",
                details={"socket_map": declared_sockets, "specification": spec_sockets},
            )
        if getattr(spec, "source_kind", None) != "local_operator_assembly":
            raise SpecInvalidError("specification source_kind is not local_operator_assembly")

    def check_artifact(self, data: bytes) -> None:
        if len(data) != self.artifact_bytes:
            raise SpecInvalidError(
                f"source artifact is {len(data)} bytes, registration declares {self.artifact_bytes}"
            )
        if hashlib.sha256(data).hexdigest() != self.artifact_sha256:
            raise SpecInvalidError("source artifact SHA-256 does not match the registration")


def parse_source_registration(content: str | dict[str, Any] | Path) -> AssemblySourceRegistration:
    """Parse a registration strictly. A missing or unknown ``source_front`` is rejected."""
    from gamefactory.core.domain.asset_contracts import _load_spec_data

    data = _load_spec_data(content)
    if "source_front" not in data:
        raise SpecInvalidError(
            "source registration requires source_front ('-Z' or '+Z'); there is no default"
        )
    if data.get("source_front") not in ("-Z", "+Z"):
        raise SpecInvalidError(
            f"source_front must be '-Z' or '+Z', got {data.get('source_front')!r}"
        )
    try:
        return AssemblySourceRegistration.model_validate(data)
    except Exception as exc:
        raise SpecInvalidError(
            f"source registration rejected: {exc}", details={"error": str(exc)}
        ) from exc
