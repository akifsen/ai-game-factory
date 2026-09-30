"""Bounded standalone authored assembly source provenance and domain models.

Implements ADR 0016 strict immutable typed local_operator_assembly provenance
with versioned closed fields, spec fingerprint binding, and derived_from validation.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gamefactory.core.domain.asset_contracts import (
    AssetSpecificationV07,
    PartSpec,
    SocketSpec,
    spec_fingerprint,
)
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import utc_now_iso

SOURCE_PROVENANCE_SCHEMA_VERSION_V07: Literal["0.7.0"] = "0.7.0"
SOURCE_PROVENANCE_TYPE_LOCAL_ASSEMBLY: Literal["local_operator_assembly"] = (
    "local_operator_assembly"
)
MAX_ASSEMBLY_SOURCE_BYTES = 50 * 1024 * 1024  # 50 MiB
MAX_PROVENANCE_SIDECAR_BYTES = 1024 * 1024  # 1 MiB

_HEX_64_REGEX = re.compile(r"^[0-9a-fA-F]{64}$")
_IDENTIFIER_REGEX = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ASSET_ID_REGEX = re.compile(r"^[a-z][a-z0-9_]{1,79}$")


class DerivedFromEntry(BaseModel):
    """Provenance-only record of upstream Factory mesh derivation per ADR 0016.

    Derived entries are never resolved, fetched, or required at processing,
    validation, or cold verification time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    part_id: str = Field(..., min_length=1, max_length=64)
    source_artifact_sha256: str
    source_asset_id: str = Field(..., min_length=2, max_length=80)
    source_revision: int = Field(..., ge=1)

    @field_validator("part_id")
    @classmethod
    def validate_part_id(cls, value: str) -> str:
        if not _IDENTIFIER_REGEX.fullmatch(value) or value == "root":
            raise ValueError(f"Invalid part_id in derived_from: '{value}'")
        return value

    @field_validator("source_artifact_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not isinstance(value, str) or not _HEX_64_REGEX.fullmatch(value):
            raise ValueError(f"source_artifact_sha256 must be a 64-character hex string: '{value}'")
        return value.lower()

    @field_validator("source_asset_id")
    @classmethod
    def validate_source_asset_id(cls, value: str) -> str:
        if not _ASSET_ID_REGEX.fullmatch(value):
            raise ValueError(f"Invalid source_asset_id in derived_from: '{value}'")
        return value

    @field_validator("source_revision", mode="before")
    @classmethod
    def validate_revision_integer(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"source_revision must be an integer, got {type(value).__name__} ({value!r})"
            )
        if value < 1:
            raise ValueError(f"source_revision must be >= 1, got {value}")
        return value


class AssemblySourceProvenance(BaseModel):
    """Immutable, versioned, closed provenance record for local_operator_assembly sources.

    Complies with ADR 0016 item 4 and ADR 0014 normative orientation requirements:
    - Binds source artifact SHA-256 and byte size
    - Requires paid == False (literal boolean False, never 0, null, or string)
    - Declares authoring tool name and version
    - Requires source_front ('-Z' or '+Z')
    - Full part and socket maps matching the bound AssetSpecificationV07
    - Registering actor and reason
    - Binds specification SHA-256 fingerprint
    - Optional validated derived_from entries (never fetched or resolved)
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["0.7.0"] = Field(...)
    source_provenance_type: Literal["local_operator_assembly"] = Field(...)
    source_artifact_sha256: str
    source_artifact_byte_size: int
    paid: Literal[False] = Field(...)
    authoring_tool_name: str = Field(..., min_length=1, max_length=128)
    authoring_tool_version: str = Field(..., min_length=1, max_length=128)
    source_front: Literal["-Z", "+Z"]
    spec_fingerprint: str
    actor: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)
    created_at: str
    part_map: dict[str, PartSpec] = Field(default_factory=dict)
    socket_map: dict[str, SocketSpec] = Field(default_factory=dict)
    derived_from: list[DerivedFromEntry] | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: Any) -> Literal["0.7.0"]:
        if value != SOURCE_PROVENANCE_SCHEMA_VERSION_V07:
            raise ValueError(
                f"schema_version must be {SOURCE_PROVENANCE_SCHEMA_VERSION_V07!r}, got {value!r}"
            )
        return SOURCE_PROVENANCE_SCHEMA_VERSION_V07

    @field_validator("source_provenance_type", mode="before")
    @classmethod
    def validate_provenance_type(cls, value: Any) -> Literal["local_operator_assembly"]:
        if value != SOURCE_PROVENANCE_TYPE_LOCAL_ASSEMBLY:
            raise ValueError(
                f"source_provenance_type must be {SOURCE_PROVENANCE_TYPE_LOCAL_ASSEMBLY!r}, got {value!r}"
            )
        return SOURCE_PROVENANCE_TYPE_LOCAL_ASSEMBLY

    @field_validator("paid", mode="before")
    @classmethod
    def validate_paid_literal_false(cls, value: Any) -> Literal[False]:
        if type(value) is not bool or value is not False:
            raise ValueError(
                f"paid must be literal boolean False (no 0/null/true), got {value!r} ({type(value).__name__})"
            )
        return False

    @field_validator("source_front", mode="before")
    @classmethod
    def validate_source_front(cls, value: Any) -> str:
        if value not in ("-Z", "+Z"):
            raise ValueError(f"source_front must be '-Z' or '+Z', got {value!r}")
        return str(value)

    @field_validator("source_artifact_sha256")
    @classmethod
    def validate_artifact_sha256(cls, value: str) -> str:
        if not isinstance(value, str) or not _HEX_64_REGEX.fullmatch(value):
            raise ValueError(f"source_artifact_sha256 must be a 64-character hex string: {value!r}")
        return value.lower()

    @field_validator("source_artifact_byte_size", mode="before")
    @classmethod
    def validate_byte_size(cls, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"source_artifact_byte_size must be an integer, got {type(value).__name__} ({value!r})"
            )
        if value < 20 or value > MAX_ASSEMBLY_SOURCE_BYTES:
            raise ValueError(
                f"source_artifact_byte_size {value} is outside allowable range (20..{MAX_ASSEMBLY_SOURCE_BYTES} bytes)"
            )
        return int(value)

    @field_validator("spec_fingerprint")
    @classmethod
    def validate_spec_fingerprint(cls, value: str) -> str:
        if not isinstance(value, str) or not _HEX_64_REGEX.fullmatch(value):
            raise ValueError(f"spec_fingerprint must be a 64-character hex string: {value!r}")
        return value.lower()

    @field_validator(
        "authoring_tool_name", "authoring_tool_version", "actor", "reason", mode="before"
    )
    @classmethod
    def validate_bounded_non_empty_string(cls, value: Any, info: Any) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{info.field_name} must be a string, got {type(value).__name__}")
        trimmed = value.strip()
        if not trimmed:
            raise ValueError(f"{info.field_name} cannot be empty or whitespace only")
        return str(trimmed)

    @model_validator(mode="after")
    def validate_maps_and_derived_from(self) -> AssemblySourceProvenance:
        # 1. Part map key alignment
        for key, part in self.part_map.items():
            if key != part.part_id:
                raise ValueError(
                    f"part_map key '{key}' does not match PartSpec part_id '{part.part_id}'"
                )

        # 2. Socket map key alignment
        for key, socket in self.socket_map.items():
            if key != socket.socket_id:
                raise ValueError(
                    f"socket_map key '{key}' does not match SocketSpec socket_id '{socket.socket_id}'"
                )

        # 3. Socket parent_part must exist in declared part_map
        for key, socket in self.socket_map.items():
            if socket.parent_part not in self.part_map:
                raise ValueError(
                    f"socket '{key}' parent_part '{socket.parent_part}' is not in declared part_map"
                )

        # 4. Bounded metadata & derived_from checks per ADR 0016
        if self.derived_from is not None:
            seen_derived_parts: set[str] = set()
            for entry in self.derived_from:
                if entry.part_id not in self.part_map:
                    raise ValueError(
                        f"derived_from entry references unknown part_id '{entry.part_id}' not in part_map"
                    )
                if entry.part_id in seen_derived_parts:
                    raise ValueError(f"duplicate derived_from entry for part_id '{entry.part_id}'")
                seen_derived_parts.add(entry.part_id)

        return self

    def to_canonical_dict(self) -> dict[str, Any]:
        """Convert provenance to a clean JSON-serializable dictionary."""
        return self.model_dump(mode="json")

    def to_canonical_json(self) -> str:
        """Produce canonical, deterministic JSON formatting with sorted keys and no whitespace."""
        return json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def to_canonical_bytes(self) -> bytes:
        """Produce canonical UTF-8 bytes for hashing and sidecar storage."""
        return self.to_canonical_json().encode("utf-8")

    def provenance_hash(self) -> str:
        """Compute SHA-256 digest of canonical provenance bytes."""
        return hashlib.sha256(self.to_canonical_bytes()).hexdigest()


PUBLICATION_MARKER_SCHEMA_VERSION_V07: Literal["0.7.0"] = "0.7.0"
PUBLICATION_MARKER_TYPE_ASSEMBLY: Literal["assembly_publication_completion"] = (
    "assembly_publication_completion"
)
PUBLICATION_MARKER_FILENAME = "publication_marker.json"


class AssemblyPublicationMarker(BaseModel):
    """Immutable, versioned, closed completion marker written last atomically upon publication.

    Binds source artifact SHA-256, retained provenance SHA-256, and spec fingerprint
    in closed, versioned, bounded fields. Used as an atomic publication completion gate,
    not as a replacement for authenticated pinned digest verification.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["0.7.0"] = Field(...)
    marker_type: Literal["assembly_publication_completion"] = Field(...)
    source_artifact_sha256: str
    source_artifact_byte_size: int
    provenance_sha256: str
    provenance_byte_size: int
    spec_fingerprint: str
    created_at: str

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: Any) -> Literal["0.7.0"]:
        if value != PUBLICATION_MARKER_SCHEMA_VERSION_V07:
            raise ValueError(
                f"schema_version must be {PUBLICATION_MARKER_SCHEMA_VERSION_V07!r}, got {value!r}"
            )
        return PUBLICATION_MARKER_SCHEMA_VERSION_V07

    @field_validator("marker_type", mode="before")
    @classmethod
    def validate_marker_type(cls, value: Any) -> Literal["assembly_publication_completion"]:
        if value != PUBLICATION_MARKER_TYPE_ASSEMBLY:
            raise ValueError(
                f"marker_type must be {PUBLICATION_MARKER_TYPE_ASSEMBLY!r}, got {value!r}"
            )
        return PUBLICATION_MARKER_TYPE_ASSEMBLY

    @field_validator("source_artifact_sha256", "provenance_sha256", "spec_fingerprint")
    @classmethod
    def validate_sha256_hex(cls, value: str, info: Any) -> str:
        if not isinstance(value, str) or not _HEX_64_REGEX.fullmatch(value):
            raise ValueError(f"{info.field_name} must be a 64-character hex string: {value!r}")
        return value.lower()

    @field_validator("source_artifact_byte_size", "provenance_byte_size", mode="before")
    @classmethod
    def validate_byte_sizes(cls, value: Any, info: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{info.field_name} must be an integer, got {type(value).__name__}")
        if value < 1:
            raise ValueError(f"{info.field_name} must be positive, got {value}")
        return int(value)

    def to_canonical_dict(self) -> dict[str, Any]:
        """Convert marker to a clean JSON-serializable dictionary."""
        return self.model_dump(mode="json")

    def to_canonical_json(self) -> str:
        """Produce canonical, deterministic JSON formatting with sorted keys and no whitespace."""
        return json.dumps(
            self.to_canonical_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    def to_canonical_bytes(self) -> bytes:
        """Produce canonical UTF-8 bytes."""
        return self.to_canonical_json().encode("utf-8")


class AssemblyIngestResult:
    """Immutable value object representing an accepted, bounded assembly source package.

    Guarantees deep immutability: the authoritative package state is anchored in
    its canonical retained provenance bytes and hashes. Although an in-memory
    AssemblySourceProvenance instance contains mutable dictionary and list containers,
    the `provenance` property returns a fresh detached view decoded from canonical
    bytes on every access, preventing caller mutations from affecting the authoritative
    package or future retrieved views.
    """

    retained_glb_path: Path
    retained_provenance_path: Path
    retained_glb_sha256: str
    retained_glb_byte_size: int
    retained_provenance_sha256: str
    retained_provenance_bytes: bytes
    spec_fingerprint: str
    package_dir: Path

    def __init__(
        self,
        retained_glb_path: Path | str,
        retained_provenance_path: Path | str,
        retained_glb_sha256: str,
        retained_glb_byte_size: int,
        retained_provenance_sha256: str,
        retained_provenance_bytes: bytes,
        spec_fingerprint: str,
        package_dir: Path | str,
    ) -> None:
        object.__setattr__(self, "retained_glb_path", Path(retained_glb_path))
        object.__setattr__(self, "retained_provenance_path", Path(retained_provenance_path))
        object.__setattr__(self, "retained_glb_sha256", str(retained_glb_sha256))
        object.__setattr__(self, "retained_glb_byte_size", int(retained_glb_byte_size))
        object.__setattr__(self, "retained_provenance_sha256", str(retained_provenance_sha256))
        object.__setattr__(self, "retained_provenance_bytes", bytes(retained_provenance_bytes))
        object.__setattr__(self, "spec_fingerprint", str(spec_fingerprint))
        object.__setattr__(self, "package_dir", Path(package_dir))

    @property
    def provenance(self) -> AssemblySourceProvenance:
        """Return a fresh, detached provenance view decoded from canonical immutable bytes.

        Mutating this detached view cannot affect the authoritative result's canonical
        bytes, hash, or future retrieved views.
        """
        raw_dict = json.loads(self.retained_provenance_bytes.decode("utf-8"))
        return AssemblySourceProvenance.model_validate(raw_dict)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"AssemblyIngestResult is immutable, cannot modify '{name}'")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"AssemblyIngestResult is immutable, cannot delete '{name}'")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AssemblyIngestResult):
            return False
        return (
            self.retained_glb_path == other.retained_glb_path
            and self.retained_provenance_path == other.retained_provenance_path
            and self.retained_glb_sha256 == other.retained_glb_sha256
            and self.retained_glb_byte_size == other.retained_glb_byte_size
            and self.retained_provenance_sha256 == other.retained_provenance_sha256
            and self.retained_provenance_bytes == other.retained_provenance_bytes
            and self.spec_fingerprint == other.spec_fingerprint
            and self.package_dir == other.package_dir
        )

    def __repr__(self) -> str:
        return (
            f"AssemblyIngestResult("
            f"retained_glb_path={self.retained_glb_path!r}, "
            f"retained_provenance_path={self.retained_provenance_path!r}, "
            f"retained_glb_sha256={self.retained_glb_sha256!r}, "
            f"retained_glb_byte_size={self.retained_glb_byte_size!r}, "
            f"retained_provenance_sha256={self.retained_provenance_sha256!r}, "
            f"retained_provenance_bytes={self.retained_provenance_bytes!r}, "
            f"spec_fingerprint={self.spec_fingerprint!r}, "
            f"package_dir={self.package_dir!r})"
        )


def create_assembly_provenance(
    *,
    source_artifact_sha256: str,
    source_artifact_byte_size: int,
    authoring_tool_name: str,
    authoring_tool_version: str,
    source_front: Literal["-Z", "+Z"],
    spec: AssetSpecificationV07,
    actor: str,
    reason: str,
    derived_from: list[DerivedFromEntry] | None = None,
    created_at: str | None = None,
) -> AssemblySourceProvenance:
    """Construct a strictly validated AssemblySourceProvenance matching an AssetSpecificationV07."""
    if not isinstance(spec, AssetSpecificationV07):
        raise ValidationError(f"spec must be an AssetSpecificationV07, got {type(spec).__name__}")
    if spec.source_kind != "local_operator_assembly":
        raise ValidationError(
            f"spec source_kind must be 'local_operator_assembly', got '{spec.source_kind}'"
        )

    part_map: dict[str, PartSpec] = {}
    if spec.parts is not None:
        for part in spec.parts:
            part_map[part.part_id] = PartSpec.model_validate(part.model_dump())

    socket_map: dict[str, SocketSpec] = {}
    if spec.sockets is not None:
        for socket in spec.sockets:
            socket_map[socket.socket_id] = SocketSpec.model_validate(socket.model_dump())

    derived_entries: list[DerivedFromEntry] | None = None
    if derived_from is not None:
        derived_entries = [DerivedFromEntry.model_validate(d.model_dump()) for d in derived_from]

    spec_fp = spec_fingerprint(spec)
    ts = created_at or utc_now_iso()

    try:
        return AssemblySourceProvenance(
            schema_version=SOURCE_PROVENANCE_SCHEMA_VERSION_V07,
            source_provenance_type=SOURCE_PROVENANCE_TYPE_LOCAL_ASSEMBLY,
            source_artifact_sha256=source_artifact_sha256,
            source_artifact_byte_size=source_artifact_byte_size,
            paid=False,
            authoring_tool_name=authoring_tool_name,
            authoring_tool_version=authoring_tool_version,
            source_front=source_front,
            spec_fingerprint=spec_fp,
            actor=actor,
            reason=reason,
            created_at=ts,
            part_map=part_map,
            socket_map=socket_map,
            derived_from=derived_entries,
        )
    except Exception as exc:
        raise ValidationError(
            f"Assembly source provenance validation failed: {exc}",
            details={"error": str(exc)},
        ) from exc


def assert_provenance_matches_spec(
    provenance: AssemblySourceProvenance,
    spec: AssetSpecificationV07,
) -> None:
    """Assert that a provenance record strictly matches the bound AssetSpecificationV07."""
    expected_spec_fp = spec_fingerprint(spec)
    if provenance.spec_fingerprint != expected_spec_fp:
        raise ValidationError(
            f"Provenance spec_fingerprint '{provenance.spec_fingerprint}' does not match expected '{expected_spec_fp}'"
        )

    expected_parts = {p.part_id: p for p in (spec.parts or [])}
    if set(provenance.part_map.keys()) != set(expected_parts.keys()):
        raise ValidationError(
            f"Provenance part_map keys {sorted(provenance.part_map.keys())} do not match spec parts {sorted(expected_parts.keys())}"
        )
    for part_id, expected_part in expected_parts.items():
        prov_part = provenance.part_map[part_id]
        if prov_part.model_dump() != expected_part.model_dump():
            raise ValidationError(
                f"Provenance part '{part_id}' does not match spec PartSpec definition"
            )

    expected_sockets = {s.socket_id: s for s in (spec.sockets or [])}
    if set(provenance.socket_map.keys()) != set(expected_sockets.keys()):
        raise ValidationError(
            f"Provenance socket_map keys {sorted(provenance.socket_map.keys())} do not match spec sockets {sorted(expected_sockets.keys())}"
        )
    for socket_id, expected_socket in expected_sockets.items():
        prov_socket = provenance.socket_map[socket_id]
        if prov_socket.model_dump() != expected_socket.model_dump():
            raise ValidationError(
                f"Provenance socket '{socket_id}' does not match spec SocketSpec definition"
            )
