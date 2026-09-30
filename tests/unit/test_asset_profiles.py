"""Profile schema, registry, and revision binding."""

from pathlib import Path

import pytest

from gamefactory.core.domain.asset_contracts import (
    parse_asset_specification,
    spec_fingerprint,
)
from gamefactory.core.domain.asset_profiles import (
    ProfileContractError,
    builtin_registry,
    parse_profile_document,
)
from gamefactory.core.domain.errors import SpecInvalidError

CRATE = Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
CRATE_FINGERPRINT = "42f1a38e28c7b95e36505e47318fb4ed116ca723a67d46c3e895e750ea99e432"


def test_builtin_registry_lists_available_profiles_and_unsupported_ids() -> None:
    rows = {row["profile_id"]: row["status"] for row in builtin_registry().availability()}
    assert rows["static_prop"] == "AVAILABLE"
    assert rows["pickup"] == "AVAILABLE"
    assert rows["modular_piece"] == "AVAILABLE"
    for implemented in ("character", "vehicle", "weapon", "aircraft"):
        assert rows[implemented] == "AVAILABLE"
    assert rows["rigged_character"] == "UNSUPPORTED"
    assert "future" not in " ".join(rows.values()).lower()
    assert builtin_registry().get("static_prop").qualified == "static_prop@1"
    assert builtin_registry().get("pickup", 1).qualified == "pickup@1"
    assert builtin_registry().get("modular_piece").qualified == "modular_piece@1"


def test_unsupported_and_unknown_versions_are_rejected() -> None:
    with pytest.raises(ProfileContractError, match="UNSUPPORTED"):
        builtin_registry().get("rigged_character")
    with pytest.raises(ProfileContractError, match="UNSUPPORTED"):
        builtin_registry().get_v07("rigged_character", 1)
    # V0.7 profiles never bind a historical specification.
    with pytest.raises(ProfileContractError, match="not registered"):
        builtin_registry().get("character")
    with pytest.raises(ProfileContractError, match="not registered"):
        builtin_registry().get("static_prop", 9)


def test_profile_document_rejects_unknown_fields_and_bad_ranges() -> None:
    document = builtin_registry().get("pickup").document.model_dump(mode="json")
    document["exec"] = "print(1)"
    with pytest.raises(ProfileContractError):
        parse_profile_document(document)
    document = builtin_registry().get("pickup").document.model_dump(mode="json")
    document["dimension_rules"]["min_m"] = -1
    with pytest.raises(ProfileContractError):
        parse_profile_document(document)
    document = builtin_registry().get("static_prop").document.model_dump(mode="json")
    document["review_views"] = ["front", "rear"]
    with pytest.raises(ProfileContractError):
        parse_profile_document(document)


def test_v04_crate_fingerprint_is_unchanged_and_binds_static_prop_v1() -> None:
    spec = parse_asset_specification(CRATE)
    assert spec_fingerprint(spec) == CRATE_FINGERPRINT
    assert "profile_version" not in spec.model_dump(mode="json")
    assert spec.bound_profile().qualified == "static_prop@1"


def test_profile_change_is_a_different_specification() -> None:
    pickup = parse_asset_specification(
        Path("src/gamefactory/resources/fixtures/energy_pickup_test.yml")
    )
    modular = parse_asset_specification(
        Path("src/gamefactory/resources/fixtures/wall_panel_test.yml")
    )
    assert spec_fingerprint(pickup) != spec_fingerprint(modular)
    assert pickup.bound_profile().qualified == "pickup@1"
    assert modular.bound_profile().qualified == "modular_piece@1"


def test_v04_document_cannot_select_another_profile() -> None:
    spec = parse_asset_specification(CRATE).model_dump(mode="json")
    spec["profile"] = "pickup"
    spec["category"] = "pickup"
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(spec)


def test_pickup_rejects_oversized_dimensions() -> None:
    spec = parse_asset_specification(
        Path("src/gamefactory/resources/fixtures/energy_pickup_test.yml")
    ).model_dump(mode="json")
    spec["dimensions"]["height_m"] = 3.0
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(spec)
