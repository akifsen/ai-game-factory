"""Published V0.8-3B candidate runtime JSON Schemas match strict Pydantic models."""

from __future__ import annotations

import json
from importlib import resources

import pytest

from gamefactory.core.domain.v08_candidate_runtime_contract import CandidateRuntimeContract
from gamefactory.core.domain.v08_candidate_runtime_models import (
    CandidateRuntimeObservation,
    CandidateRuntimeRequestBound,
)

SCHEMAS = resources.files("gamefactory").joinpath("schemas")


@pytest.mark.parametrize(
    ("name", "model"),
    [
        ("candidate-runtime-contract-0.8.0", CandidateRuntimeContract),
        ("candidate-runtime-request-0.8.0", CandidateRuntimeRequestBound),
        ("candidate-runtime-observation-0.8.0", CandidateRuntimeObservation),
    ],
)
def test_published_schema_matches_the_model(name: str, model: type) -> None:
    published = json.loads(SCHEMAS.joinpath(f"{name}.schema.json").read_text(encoding="utf-8"))
    assert published == model.model_json_schema()  # type: ignore[attr-defined]
