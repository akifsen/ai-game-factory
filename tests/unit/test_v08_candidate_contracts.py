"""V0.8-3A candidate profile/spec parsing, discovery isolation, and loader binding."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.fakes import assembly_generator as ag
from gamefactory.core.domain.asset_contracts import parse_any_asset_specification
from gamefactory.core.domain.asset_profiles import (
    UNSUPPORTED_PROFILE_IDS,
    ProfileContractError,
    builtin_registry,
)
from gamefactory.core.domain.errors import SpecInvalidError
from gamefactory.core.domain.v08_candidate_contracts import (
    SPEC_SCHEMA_VERSION_V08_CANDIDATE,
    AssetProfileV08Candidate,
    CandidateContractError,
    ProfileDocumentV08Candidate,
    load_packaged_candidate_profile,
    load_packaged_candidate_specification,
    parse_asset_specification_v08_candidate,
    parse_profile_document_v08_candidate,
    profile_document_hash,
    revalidate_candidate_binding,
)


def test_packaged_candidate_profile_is_closed_and_unsupported() -> None:
    profile = load_packaged_candidate_profile()
    doc = profile.document
    assert doc.candidate_state == "CLOSED"
    assert doc.public_status == "UNSUPPORTED"
    assert doc.profile_id == "rigged_character"
    assert doc.accepted_source_kinds == ["local_verified_rig"]


def test_packaged_spec_binds_profile_hash_and_loads() -> None:
    spec = load_packaged_candidate_specification()
    assert spec.schema_version == SPEC_SCHEMA_VERSION_V08_CANDIDATE
    assert spec.source_kind == "local_verified_rig"
    assert spec.profile_document_hash == profile_document_hash(
        load_packaged_candidate_profile().document
    )


def test_legacy_parse_any_rejects_candidate_schema() -> None:
    data = copy.deepcopy(load_packaged_candidate_specification().model_dump(mode="json"))
    with pytest.raises(SpecInvalidError):
        parse_any_asset_specification(data)


def test_public_registry_still_refuses_rigged_character() -> None:
    assert "rigged_character" in UNSUPPORTED_PROFILE_IDS
    with pytest.raises(SpecInvalidError, match="UNSUPPORTED"):
        builtin_registry().get("rigged_character")
    with pytest.raises(SpecInvalidError, match="UNSUPPORTED"):
        builtin_registry().get_v07("rigged_character", 1)


def test_character_v07_still_rejects_skin_path() -> None:
    data = ag.character_spec(ag.HUMANOID_CHARACTER)
    with pytest.raises(SpecInvalidError):
        parse_asset_specification_v08_candidate(data)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda d: d.update({"schema_version": "0.7.0"}),
        lambda d: d.update({"source_kind": "provider_generated"}),
        lambda d: d.update({"profile_version": True}),
        lambda d: d.update({"dimensions": {"width_m": "1.0", "depth_m": 0.2, "height_m": 0.2}}),
        lambda d: d.update({"unknown_field": 1}),
    ],
)
def test_candidate_spec_rejects_invalid_documents(mutator: Any) -> None:
    base = load_packaged_candidate_specification().model_dump(mode="json")
    mutator(base)
    with pytest.raises((CandidateContractError, ValueError)):
        parse_asset_specification_v08_candidate(base)


def test_wrong_profile_document_hash_rejected() -> None:
    data = load_packaged_candidate_specification().model_dump(mode="json")
    data["profile_document_hash"] = "0" * 64
    with pytest.raises(CandidateContractError, match="profile_document_hash"):
        parse_asset_specification_v08_candidate(data)


def test_json_schema_and_model_both_reject_bool_profile_version() -> None:
    import json

    from gamefactory.core.domain.v08_candidate_contracts import AssetSpecificationV08Candidate

    data = load_packaged_candidate_specification().model_dump(mode="json")
    data["profile_version"] = True
    schema = json.loads(
        (Path("src/gamefactory/schemas/asset-spec-0.8.0-candidate.schema.json")).read_text(
            encoding="utf-8"
        )
    )
    profile_version_schema = schema["properties"]["profile_version"]
    assert profile_version_schema.get("type") == "integer"
    with pytest.raises((CandidateContractError, ValueError)):
        AssetSpecificationV08Candidate.model_validate(data)
    with pytest.raises(CandidateContractError):
        parse_asset_specification_v08_candidate(data)


def test_profile_model_rejects_integer_bool_literals_in_processing() -> None:
    dump = load_packaged_candidate_profile().document.model_dump(mode="json")
    dump["processing"]["animation_forbidden"] = 1
    dump["processing"]["rig_forbidden"] = 0
    with pytest.raises((CandidateContractError, ProfileContractError, ValueError)):
        parse_profile_document_v08_candidate(dump)


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        ("processing.lod0_required", 1),
        ("processing.lod1_required", 0),
        ("processing.rig_forbidden", 0),
        ("processing.animation_forbidden", 1),
        ("animation_forbidden", 1),
    ],
)
def test_profile_strict_bool_schema_parity(field_path: str, value: object) -> None:
    import json

    dump = load_packaged_candidate_profile().document.model_dump(mode="json")
    if field_path.startswith("processing."):
        dump["processing"][field_path.split(".", 1)[1]] = value
    else:
        dump[field_path] = value
    schema = json.loads(
        (Path("src/gamefactory/schemas/asset-profile-0.8.0-candidate.schema.json")).read_text(
            encoding="utf-8"
        )
    )
    if field_path.startswith("processing."):
        leaf = schema["$defs"]["CandidateProcessingPolicy"]["properties"][
            field_path.split(".", 1)[1]
        ]
    else:
        leaf = schema["properties"][field_path]
    assert leaf.get("type") == "boolean"
    with pytest.raises((CandidateContractError, ProfileContractError, ValueError)):
        ProfileDocumentV08Candidate.model_validate(dump)
    with pytest.raises((CandidateContractError, ProfileContractError, ValueError)):
        parse_profile_document_v08_candidate(dump)


def test_revalidate_binding_rejects_mutated_rehashed_profile_contents() -> None:
    profile = load_packaged_candidate_profile()
    spec = load_packaged_candidate_specification()
    dump = profile.document.model_dump(mode="json")
    dump["runtime"]["bounds_tolerance_floor_m"] = True
    bad_document = ProfileDocumentV08Candidate.model_construct(**dump)
    bad_profile = AssetProfileV08Candidate(document=bad_document)
    forged = spec.model_copy(update={"profile_document_hash": profile_document_hash(bad_document)})
    with pytest.raises(CandidateContractError, match="bounds_tolerance_floor_m"):
        revalidate_candidate_binding(forged, bad_profile)


def test_revalidate_binding_rejects_wrong_schema_version_in_profile_dump() -> None:
    profile = load_packaged_candidate_profile()
    spec = load_packaged_candidate_specification()
    dump = profile.document.model_dump(mode="json")
    dump["schema_version"] = "asset-profile-0.7.0"
    bad_document = ProfileDocumentV08Candidate.model_construct(**dump)
    bad_profile = AssetProfileV08Candidate(document=bad_document)
    with pytest.raises(CandidateContractError):
        revalidate_candidate_binding(spec, bad_profile)
