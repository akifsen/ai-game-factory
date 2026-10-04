"""Unit tests for existing-external provenance parsing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.existing_external_provenance import (
    MAX_EXISTING_EXTERNAL_PROVENANCE_BYTES,
    load_existing_external_provenance,
    parse_bounded_provenance_json,
    parse_existing_external_provenance,
)


def _valid_payload(source_sha256: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "existing-external-source-provenance-1.0",
        "source_mode": "existing_external",
        "source_sha256": source_sha256,
        "original_provider": "meshy",
        "generated_by_this_workflow": False,
        "paid_by_this_workflow": False,
    }
    payload.update(extra)
    return payload


def test_rejects_claimed_factory_generation(tmp_path: Path) -> None:
    path = tmp_path / "prov.json"
    path.write_text(
        json.dumps(_valid_payload("a" * 64, generated_by_this_workflow=True)),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        parse_existing_external_provenance(path)


def test_rejects_zero_as_false_literal(tmp_path: Path) -> None:
    path = tmp_path / "prov.json"
    path.write_text(
        json.dumps(_valid_payload("a" * 64, paid_by_this_workflow=0)),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        parse_existing_external_provenance(path)


def test_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    raw = b'{"schema_version":"existing-external-source-provenance-1.0","schema_version":"x"}'
    with pytest.raises(ValidationError, match="duplicate JSON key"):
        parse_bounded_provenance_json(raw)


def test_rejects_oversized_provenance_file(tmp_path: Path) -> None:
    path = tmp_path / "prov.json"
    path.write_bytes(b"x" * (MAX_EXISTING_EXTERNAL_PROVENANCE_BYTES + 1))
    with pytest.raises(ValidationError, match="bounded"):
        parse_existing_external_provenance(path)


def test_rejects_nonfinite_historical_credits(tmp_path: Path) -> None:
    path = tmp_path / "prov.json"
    path.write_text(
        json.dumps(_valid_payload("a" * 64, historical_credits=float("inf"))),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        parse_existing_external_provenance(path)


def test_accepts_historical_task_ids_list(tmp_path: Path) -> None:
    path = tmp_path / "prov.json"
    path.write_text(
        json.dumps(
            _valid_payload(
                "a" * 64,
                historical_task_ids=["geom-task", "tex-task"],
            )
        ),
        encoding="utf-8",
    )
    record = parse_existing_external_provenance(path)
    assert record.historical_task_ids == ["geom-task", "tex-task"]


def test_check_artifact_enforces_sha(tmp_path: Path) -> None:
    data = b"glb-bytes"
    digest = hashlib.sha256(data).hexdigest()
    path = tmp_path / "prov.json"
    path.write_text(json.dumps(_valid_payload(digest)), encoding="utf-8")
    record = parse_existing_external_provenance(path)
    record.check_artifact(data)
    with pytest.raises(ValidationError, match="does not match"):
        record.check_artifact(data + b"!")


def test_load_digest_matches_validated_bytes(tmp_path: Path) -> None:
    raw_text = json.dumps(_valid_payload("b" * 64), sort_keys=True)
    path = tmp_path / "prov.json"
    path.write_text(raw_text, encoding="utf-8")
    _record, raw, digest = load_existing_external_provenance(path)
    assert digest == hashlib.sha256(raw).hexdigest()
    assert raw.decode("utf-8") == raw_text
