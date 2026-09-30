from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamefactory.cli.main import _assembly_profile_registry, _dispatch, build_parser
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.asset_profiles import (
    UNSUPPORTED_PROFILE_IDS,
    UNSUPPORTED_PROFILE_IDS_V07,
    ProfileContractError,
    builtin_registry,
    builtin_v07_registry,
)


def test_v07_catalog_is_separate_and_explicit() -> None:
    legacy = builtin_registry()
    v07 = builtin_v07_registry()

    assert legacy.unsupported == UNSUPPORTED_PROFILE_IDS
    assert legacy.available_v07 == ()
    assert v07.available == ()
    assert tuple(profile.qualified for profile in v07.available_v07) == (
        "vehicle@1",
        "weapon@1",
        "aircraft@1",
        "character@1",
    )
    assert v07.unsupported == UNSUPPORTED_PROFILE_IDS_V07
    assert [row["qualified"] for row in v07.availability()[:4]] == [
        "vehicle@1",
        "weapon@1",
        "aircraft@1",
        "character@1",
    ]
    assert all(row["status"] == "UNSUPPORTED" for row in v07.availability()[4:])
    assert "character" not in {row["profile_id"] for row in v07.availability()[4:]}
    assert "rigged_character" in {row["profile_id"] for row in v07.availability()[4:]}
    assert builtin_registry().availability() == legacy.availability()


def test_catalog_lookups_do_not_fall_back_across_contract_versions() -> None:
    legacy = builtin_registry()
    v07 = builtin_v07_registry()

    assert legacy.get("static_prop", 1).qualified == "static_prop@1"
    with pytest.raises(ProfileContractError):
        v07.get("static_prop", 1)
    with pytest.raises(ProfileContractError):
        legacy.get_v07("static_prop", 1)
    assert v07.get_v07("vehicle", 1).qualified == "vehicle@1"
    assert v07.get_v07("character", 1).qualified == "character@1"
    with pytest.raises(ProfileContractError, match="UNSUPPORTED"):
        v07.get_v07("rigged_character", 1)
    with pytest.raises(ProfileContractError, match="not registered"):
        v07.get_v07("character", 2)


def test_v07_spec_parser_uses_public_catalog_by_default() -> None:
    project = Path(__file__).resolve().parents[1]
    spec_path = project.parent / "src/gamefactory/resources/specs/armored_vehicle_test.yml"
    assert spec_path.is_file()

    spec = parse_asset_specification_v07(spec_path)
    assert spec.bound_profile().qualified == "vehicle@1"

    char_spec_path = project.parent / "src/gamefactory/resources/specs/character_test.yml"
    assert char_spec_path.is_file()
    char_spec = parse_asset_specification_v07(char_spec_path)
    assert char_spec.bound_profile().qualified == "character@1"


def test_assembly_cli_uses_independent_v07_catalog() -> None:
    assert _assembly_profile_registry().availability() == builtin_v07_registry().availability()


def test_asset_profiles_cli_preserves_legacy_default_and_exposes_v07_catalog() -> None:
    parser = build_parser()
    default_args = parser.parse_args(["asset", "profiles", "--json"])
    legacy_payload, _, _ = _dispatch(default_args)
    assert legacy_payload["asset_profiles"] == builtin_registry().availability()
    assert {row["status"] for row in legacy_payload["asset_profiles"]} == {
        "AVAILABLE",
        "UNSUPPORTED",
    }

    v07_args = parser.parse_args(["asset", "profiles", "--contract-version", "0.7.0", "--json"])
    v07_payload, _, _ = _dispatch(v07_args)
    assert v07_payload["asset_profiles"] == builtin_v07_registry().availability()
    v07_rows = v07_payload["asset_profiles"]
    assert [row["qualified"] for row in v07_rows if row["status"] == "AVAILABLE"] == [
        "vehicle@1",
        "weapon@1",
        "aircraft@1",
        "character@1",
    ]
    assert any(
        row["profile_id"] == "rigged_character" and row["status"] == "UNSUPPORTED"
        for row in v07_rows
    )
    assert not any(
        row["profile_id"] == "character" and row["status"] == "UNSUPPORTED" for row in v07_rows
    )

    before = json.dumps(legacy_payload, sort_keys=True)
    _dispatch(v07_args)
    assert json.dumps(_dispatch(default_args)[0], sort_keys=True) == before


def test_asset_profiles_cli_rejects_unknown_contract_version() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["asset", "profiles", "--contract-version", "9.9.9"])
