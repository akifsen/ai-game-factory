"""Published V0.8-3A candidate JSON Schemas must match strict Pydantic models."""

from __future__ import annotations

import json
from importlib import resources

import pytest

from gamefactory.core.domain.v08_candidate_contracts import (
    AssetSpecificationV08Candidate,
    ProfileDocumentV08Candidate,
)

SCHEMAS = resources.files("gamefactory").joinpath("schemas")


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("asset-profile-0.8.0-candidate", ProfileDocumentV08Candidate),
        ("asset-spec-0.8.0-candidate", AssetSpecificationV08Candidate),
    ],
)
def test_published_schema_matches_the_model(name: str, model: type) -> None:
    published = json.loads(SCHEMAS.joinpath(f"{name}.schema.json").read_text(encoding="utf-8"))
    assert published == model.model_json_schema()  # type: ignore[attr-defined]
    assert published.get("additionalProperties") is False
