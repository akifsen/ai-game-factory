"""The published V0.7 JSON Schemas are generated from the strict models and never drift."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamefactory.core.domain.assembly_source import AssemblySourceRegistration
from gamefactory.core.domain.asset_contracts import AssetSpecificationV07
from gamefactory.core.domain.asset_profiles import ProfileDocumentV07

SCHEMAS = Path("src/gamefactory/schemas")


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("asset-spec-0.7.0", AssetSpecificationV07),
        ("asset-profile-0.7.0", ProfileDocumentV07),
        ("asset-source-registration-0.7.0", AssemblySourceRegistration),
    ],
)
def test_published_schema_matches_the_model(name: str, model: type) -> None:
    published = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    assert published == model.model_json_schema()  # type: ignore[attr-defined]
    assert published.get("additionalProperties") is False


def test_frozen_historical_schema_is_unchanged() -> None:
    """asset-spec-0.4.0 is frozen; compare canonical content, not checkout line endings."""
    import hashlib

    document = json.loads((SCHEMAS / "asset-spec-0.4.0.schema.json").read_text(encoding="utf-8"))
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    assert (
        hashlib.sha256(canonical).hexdigest()
        == "e743e7898f7b1a1aa1580a549cafad3cec33aee294ac0789fb72ba90618a1e2c"
    )
