"""Immutable provenance binding for installing an accepted GLB into a game."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from gamefactory.core.domain.errors import ValidationError


@dataclass(frozen=True)
class AssetInstallationSnapshot:
    project_id: str
    target_root: str
    source_workflow_id: str
    asset_id: str
    revision_number: int
    specification_sha256: str
    target_import_path: str
    accepted_artifact_id: str
    accepted_artifact_sha256: str
    final_approval_id: str
    final_approval_sha256: str
    target_baseline_sha256: dict[str, str] | None = None
    schema_version: int = 1

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValidationError("Unsupported asset installation snapshot schema version")
        if not all(
            isinstance(value, str)
            and value
            and len(value) <= 4096
            and not any(ord(char) < 32 for char in value)
            for value in (
                self.project_id,
                self.target_root,
                self.source_workflow_id,
                self.asset_id,
                self.target_import_path,
                self.accepted_artifact_id,
                self.final_approval_id,
            )
        ):
            raise ValidationError("Asset installation provenance fields are required")
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.project_id)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.source_workflow_id)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.accepted_artifact_id)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.final_approval_id)
        ):
            raise ValidationError("Asset installation identifiers contain unsafe characters")
        if len(self.target_import_path) > 1024 or not Path(self.target_root).is_absolute():
            # Windows absolute paths are accepted; all other roots must be absolute POSIX paths.
            raise ValidationError(
                "Asset installation root or import path is not a bounded absolute path"
            )
        if (
            isinstance(self.revision_number, bool)
            or not isinstance(self.revision_number, int)
            or self.revision_number < 1
        ):
            raise ValidationError("Asset revision number must be positive")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", self.asset_id):
            raise ValidationError("Asset id is unsafe")
        for digest in (
            self.specification_sha256,
            self.accepted_artifact_sha256,
            self.final_approval_sha256,
        ):
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValidationError(
                    "Asset installation SHA-256 values must be lowercase hex digests"
                )
        if self.target_baseline_sha256 is not None:
            if not isinstance(self.target_baseline_sha256, dict) or not self.target_baseline_sha256:
                raise ValidationError("Target baseline hashes must be a non-empty object")
            for name, digest in self.target_baseline_sha256.items():
                if (
                    name not in {f"{self.asset_id}.glb", f"{self.asset_id}.tscn"}
                    or not isinstance(digest, str)
                    or not re.fullmatch(r"[a-f0-9]{64}", digest)
                ):
                    raise ValidationError("Target baseline hash entry is invalid")
        normalized = self.target_import_path.replace("\\", "/")
        parts = normalized.split("/")
        if parts and parts[-1] == "":
            parts.pop()
        reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        if (
            not normalized
            or len(normalized) > 1024
            or normalized.startswith("/")
            or any(
                part in {"", ".", ".."}
                or ":" in part
                or part.endswith((".", " "))
                or part.split(".", 1)[0].upper() in reserved
                or part.casefold() == ".gamefactory"
                for part in parts
            )
        ):
            raise ValidationError("Target import path must be a safe project-relative directory")

    def fingerprint(self) -> str:
        self.validate()
        raw = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def to_dict(self) -> dict[str, object]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> AssetInstallationSnapshot:
        fields = set(cls.__dataclass_fields__)
        if not isinstance(value, dict) or set(value) != fields:
            raise ValidationError(
                "Asset installation snapshot fields do not match schema version 1"
            )
        try:
            snapshot = cls(**value)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Asset installation snapshot types are invalid") from exc
        snapshot.validate()
        return snapshot
