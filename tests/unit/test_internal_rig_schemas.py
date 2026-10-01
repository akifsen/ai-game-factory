"""JSON schema presence checks for internal rig evidence (V0.8-2)."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path


def _schema(name: str) -> dict:
    raw = (
        resources.files("gamefactory.resources.internal_rig.schemas")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )
    return json.loads(raw)


def test_rig_evidence_schema_versions() -> None:
    names = [
        "rig-verification-contract-0.8.0.schema.json",
        "rig-validation-report-0.8.0.schema.json",
        "rig-runtime-request-0.8.0.schema.json",
        "rig-runtime-observation-0.8.0.schema.json",
        "rig-evidence-0.8.0.schema.json",
    ]
    for name in names:
        doc = _schema(name)
        assert doc["$schema"].startswith("https://json-schema.org/")
        assert "0.8.0" in doc.get("title", name)


def test_schemas_ship_in_package_tree() -> None:
    root = Path(__file__).resolve().parents[2]
    schema_dir = root / "src/gamefactory/resources/internal_rig/schemas"
    assert schema_dir.is_dir()
    assert len(list(schema_dir.glob("*.schema.json"))) == 5


def test_rig_evidence_schema_requires_reviewed_pins_and_runtime() -> None:
    doc = _schema("rig-evidence-0.8.0.schema.json")
    required = set(doc["required"])
    assert "harness_reviewed_sha256" in required
    assert "blender_script_reviewed_sha256" in required
    assert "runtime_status" in required
    file_items = doc["properties"]["files"]["items"]
    assert file_items.get("additionalProperties") is False


def test_rig_validation_report_schema_requires_canonical_fields() -> None:
    doc = _schema("rig-validation-report-0.8.0.schema.json")
    required = set(doc["required"])
    assert "contract_canonical_sha256" in required
    assert "validator_contract_version" in required
    assert doc["properties"]["passed"]["type"] == "boolean"
