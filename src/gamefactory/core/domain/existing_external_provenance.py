"""Minimal provenance for externally authored GLBs reused in Factory (static_prop pilot)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gamefactory.core.domain.errors import ValidationError

PROVENANCE_SCHEMA_VERSION = "existing-external-source-provenance-1.0"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_EXISTING_EXTERNAL_PROVENANCE_BYTES = 65_536


def _reject_nonfinite_json_constant(token: str) -> Any:
    raise ValidationError(f"invalid JSON constant: {token}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValidationError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def parse_bounded_provenance_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_EXISTING_EXTERNAL_PROVENANCE_BYTES:
        raise ValidationError("Provenance JSON exceeds bounded byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"Provenance is not valid UTF-8: {exc}") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Provenance is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("Provenance must be a JSON object")
    return parsed


def _provenance_lexical_unsafe(path: Path) -> bool:
    lexical = path.absolute()
    ancestors: list[Path] = []
    for current in [lexical, *lexical.parents]:
        ancestors.append(current)
        if current.anchor == current:
            break
    for ancestor in ancestors:
        try:
            st = os.lstat(ancestor)
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if stat.S_ISLNK(st.st_mode):
            return True
        reparse_tag = int(getattr(st, "st_reparse_tag", 0) or 0)
        if reparse_tag != 0:
            return True
        if os.name == "nt":
            attrs = getattr(st, "st_file_attributes", None)
            if attrs is None:
                return True
            if int(attrs) & 0x400 and reparse_tag == 0:
                return True
    return False


def read_bounded_provenance_bytes(path: Path) -> bytes:
    if _provenance_lexical_unsafe(path):
        raise ValidationError("Provenance path is not a bounded safe location")
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise ValidationError("Provenance must be a regular file") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise ValidationError("Provenance must be a regular file")
    size = st.st_size
    if size <= 0 or size > MAX_EXISTING_EXTERNAL_PROVENANCE_BYTES:
        raise ValidationError("Provenance file size is outside bounded limits")
    hasher = hashlib.sha256()
    read_total = 0
    chunks: list[bytes] = []
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(8192)
            if not chunk:
                break
            read_total += len(chunk)
            if read_total > MAX_EXISTING_EXTERNAL_PROVENANCE_BYTES:
                raise ValidationError("Provenance exceeded bounded stream read limit")
            hasher.update(chunk)
            chunks.append(chunk)
    raw = b"".join(chunks)
    if read_total != size:
        raise ValidationError("Provenance file size changed during bounded read")
    return raw


class ExistingExternalProvenance(BaseModel):
    """Sidecar describing a manual or third-party GLB; never a Factory generation receipt."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["existing-external-source-provenance-1.0"] = (
        "existing-external-source-provenance-1.0"
    )
    source_mode: Literal["existing_external"]
    source_sha256: str
    original_provider: str = Field(..., min_length=1, max_length=120)
    generated_by_this_workflow: Literal[False] = False
    paid_by_this_workflow: Literal[False] = False
    historical_task_id: str | None = Field(default=None, max_length=120)
    historical_task_ids: list[str] | None = Field(default=None, max_length=32)
    historical_credits: float | None = Field(default=None, ge=0)

    @field_validator("source_sha256")
    @classmethod
    def hex_digest(cls, value: str) -> str:
        if not _HEX64.fullmatch(value):
            raise ValueError("source_sha256 must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("generated_by_this_workflow", "paid_by_this_workflow", mode="before")
    @classmethod
    def require_json_false(cls, value: object) -> Literal[False]:
        if value is not False:
            raise ValueError("reuse provenance must not claim Factory generation or spend")
        return False

    @field_validator("historical_credits", mode="before")
    @classmethod
    def finite_historical_credits(cls, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("historical_credits must be a finite number")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("historical_credits must be a finite non-negative number")
        return number

    @field_validator("historical_task_ids")
    @classmethod
    def bounded_task_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) > 32:
            raise ValueError("historical_task_ids exceeds bounded length")
        for item in value:
            if not isinstance(item, str) or not item or len(item) > 120:
                raise ValueError(
                    "historical_task_ids entries must be non-empty strings up to 120 chars"
                )
        return value

    def check_artifact(self, data: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        if digest != self.source_sha256:
            raise ValidationError(
                "Source GLB SHA-256 does not match provenance source_sha256",
                details={"expected": self.source_sha256, "actual": digest},
            )


def provenance_from_mapping(payload: dict[str, Any]) -> ExistingExternalProvenance:
    try:
        return ExistingExternalProvenance.model_validate(payload)
    except Exception as exc:
        raise ValidationError(f"Provenance failed validation: {exc}") from exc


def provenance_from_bounded_bytes(raw: bytes) -> ExistingExternalProvenance:
    return provenance_from_mapping(parse_bounded_provenance_json(raw))


def load_existing_external_provenance(
    path: Path | str,
) -> tuple[ExistingExternalProvenance, bytes, str]:
    """Load provenance with a single bounded read; digest matches validated bytes."""
    source = Path(path)
    raw = read_bounded_provenance_bytes(source)
    record = provenance_from_bounded_bytes(raw)
    return record, raw, hashlib.sha256(raw).hexdigest()


def parse_existing_external_provenance(path: Path | str) -> ExistingExternalProvenance:
    """Load and validate a provenance JSON file."""
    record, _raw, _digest = load_existing_external_provenance(path)
    return record
