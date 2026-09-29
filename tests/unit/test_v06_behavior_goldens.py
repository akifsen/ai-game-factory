"""Immutable V0.6 asset profile behavior goldens.

These tests freeze externally observable V0.6 behavior before the V0.7 profile
foundation and the later validator modularization. Expected values were
captured at commit af0f379 and are stored as literals (hashes below) and in
tests/golden/v06_asset_profile_behavior.json. They must never be regenerated to
make a failing test pass: a failure here means historical behavior changed.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.adapters.external.meshy_cli import resolve_paid_request as meshy_resolve
from gamefactory.adapters.fakes.fake_provider import resolve_paid_request as fake_resolve
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    parse_asset_specification,
    spec_fingerprint,
)
from gamefactory.core.domain.asset_profiles import (
    ProfileContractError,
    builtin_registry,
    parse_profile_document,
    render_scene_contract,
)
from gamefactory.core.domain.errors import SpecInvalidError
from gamefactory.core.domain.paid_request import canonical_json, paid_request_sha256

GOLDEN = json.loads(
    (Path(__file__).parents[1] / "golden" / "v06_asset_profile_behavior.json").read_text(
        encoding="utf-8"
    )
)

SPEC_PATHS = {
    "crate_040": Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml"),
    "cell_050": Path("src/gamefactory/resources/specs/pickup_energy_cell_01.yml"),
    "pickup_fixture_050": Path("src/gamefactory/resources/fixtures/energy_pickup_test.yml"),
    "wall_fixture_050": Path("src/gamefactory/resources/fixtures/wall_panel_test.yml"),
}
PROFILE_PATHS = {
    "static_prop@1": Path("src/gamefactory/resources/profiles/static_prop.yml"),
    "pickup@1": Path("src/gamefactory/resources/profiles/pickup.yml"),
    "modular_piece@1": Path("src/gamefactory/resources/profiles/modular_piece.yml"),
}

MINIMAL_040: dict[str, Any] = {
    "schema_version": "0.4.0",
    "asset_id": "golden_minimal_040",
    "intent": "Minimal historical static prop",
    "dimensions": {"width_m": 1.0, "depth_m": 1.0, "height_m": 1.0},
}
MINIMAL_050: dict[str, Any] = {
    "schema_version": "0.5.0",
    "asset_id": "golden_static_050",
    "category": "prop",
    "profile": "static_prop",
    "profile_version": 1,
    "intent": "Minimal 0.5.0 static prop",
    "dimensions": {"width_m": 0.8, "depth_m": 0.6, "height_m": 1.4},
    "lod_policy": "lod0_lod1",
    "origin_policy": "center",
}

# Literal V0.6 specification fingerprints (spec_fingerprint at af0f379).
GOLDEN_FINGERPRINTS = {
    "crate_040": "42f1a38e28c7b95e36505e47318fb4ed116ca723a67d46c3e895e750ea99e432",
    "cell_050": "d9bb56bbe5da3c6d375201a2be8d39ff0f51d87263c59ebe156f750f8fd29089",
    "pickup_fixture_050": "ba8848f948faec578d1e15de420f7c0c398bef5423cbaba60ea8ff2063ff34cf",
    "wall_fixture_050": "b57c4831060d87fd72af6cd64c74c2d2ba5b14953b9280089463a6c3b31751a2",
    "min_040": "fbf43ae23355352a72116b495ff206760d63e6ac1ec9572e2d143a7e4a19c0d9",
    "min_050": "7363a4eed0f1a09d51b113435b1ef58be569b3c0dd71d5fb2564917dcea620cd",
}
# Literal V0.6 paid request snapshot digests bound to the crate_040 fingerprint.
GOLDEN_PAID_REQUEST_SHA256 = {
    "meshy": "3352e092e8c304261d59caf1f553823f441e48b600a04bef4d42ba8b4a66dfc7",
    "fake": "3bcf500dc46a4c1797fe1158bea7d3b4a18261baa56f42fd245160fdb3c6ccfc",
}

# Fields a V0.7 document may introduce. Historical schemas must reject them.
V07_ONLY_SPEC_FIELDS = {
    "geometry_mode": "multi_part",
    "parts": [{"part_id": "body", "parent": None}],
    "sockets": [{"socket_id": "muzzle", "parent_part": "body"}],
    "source_front": "+X",
    "motion": {"kind": "revolute", "axis": "+Y"},
}
V07_ONLY_PROFILE_FIELDS = {
    "geometry_mode": "multi_part",
    "parts": [{"part_id": "body"}],
    "sockets": [],
    "capabilities": {"articulation": True},
    "orientation": {"canonical_front": "-Z"},
}


def _spec(key: str) -> AssetSpecification:
    if key == "min_040":
        return parse_asset_specification(dict(MINIMAL_040))
    if key == "min_050":
        return parse_asset_specification(dict(MINIMAL_050))
    return parse_asset_specification(SPEC_PATHS[key])


def _canonical(spec: AssetSpecification) -> str:
    return json.dumps(
        spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
    )


# --------------------------------------------------------------------------
# 1. Specification fingerprints, dump shape, canonicalization, approval binding
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(GOLDEN_FINGERPRINTS))
def test_spec_fingerprint_matches_v06_literal(key: str) -> None:
    assert spec_fingerprint(_spec(key)) == GOLDEN_FINGERPRINTS[key]


@pytest.mark.parametrize("key", sorted(GOLDEN_FINGERPRINTS))
def test_spec_canonical_dump_matches_v06(key: str) -> None:
    canonical = _canonical(_spec(key))
    assert canonical == GOLDEN["spec_canonical"][key]
    # The stored canonical text is itself the fingerprint preimage.
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == GOLDEN_FINGERPRINTS[key]


@pytest.mark.parametrize("key", sorted(GOLDEN_FINGERPRINTS))
def test_spec_dump_key_set_is_frozen(key: str) -> None:
    spec = _spec(key)
    dump = spec.model_dump(mode="json")
    expected = set(json.loads(GOLDEN["spec_canonical"][key]))
    assert set(dump) == expected
    assert set(spec.model_dump()) == expected
    if spec.schema_version == "0.4.0":
        assert "profile_version" not in dump
    else:
        assert dump["profile_version"] == 1


@pytest.mark.parametrize("key", sorted(GOLDEN_FINGERPRINTS))
def test_approval_binding_round_trip_is_stable(key: str) -> None:
    """Workflows persist model_dump(mode='json') and re-verify its fingerprint."""
    stored = _spec(key).model_dump(mode="json")
    reparsed = parse_asset_specification(json.loads(json.dumps(stored)))
    assert spec_fingerprint(reparsed) == GOLDEN_FINGERPRINTS[key]
    assert reparsed.model_dump(mode="json") == stored


@pytest.mark.parametrize(
    ("provider", "resolve"), [("meshy", meshy_resolve), ("fake", fake_resolve)]
)
def test_paid_request_binding_digest_matches_v06(provider: str, resolve: Any) -> None:
    spec = _spec("crate_040")
    binding = {
        "asset_id": spec.asset_id,
        "revision_number": 1,
        "concept_version": 1,
        "concept_sha256": "a" * 64,
        "specification_sha256": spec_fingerprint(spec),
        "profile_id": "static_prop",
        "profile_version": 1,
    }
    cost = {"estimate": 20.0, "reservation": 30.0, "unit": "credits"}
    content = resolve(binding, spec.model_dump(mode="json"), cost)
    assert canonical_json(content) == GOLDEN["paid_request_canonical"][provider]
    assert paid_request_sha256(content) == GOLDEN_PAID_REQUEST_SHA256[provider]


# --------------------------------------------------------------------------
# 2. Built-in profile outputs
# --------------------------------------------------------------------------


def test_builtin_registry_availability_is_frozen() -> None:
    assert builtin_registry().availability() == GOLDEN["availability"]
    available = [row["qualified"] for row in GOLDEN["availability"] if row["status"] == "AVAILABLE"]
    assert available == ["static_prop@1", "pickup@1", "modular_piece@1"]


@pytest.mark.parametrize("qualified", sorted(PROFILE_PATHS))
def test_builtin_profile_document_dump_is_frozen(qualified: str) -> None:
    profile_id, version = qualified.split("@")
    profile = builtin_registry().get(profile_id, int(version))
    assert profile.schema_version == "asset-profile-0.5.0"
    assert profile.document.model_dump(mode="json") == GOLDEN["profile_documents"][qualified]
    reparsed = parse_profile_document(PROFILE_PATHS[qualified].read_text(encoding="utf-8"))
    assert reparsed.model_dump(mode="json") == GOLDEN["profile_documents"][qualified]


@pytest.mark.parametrize("key", sorted(GOLDEN["profile_outputs"]))
def test_builtin_profile_contract_outputs_are_frozen(key: str) -> None:
    expected = GOLDEN["profile_outputs"][key]
    spec = _spec(key)
    profile = spec.bound_profile()
    assert profile.qualified == expected["qualified"]
    assert profile.processing_contract(spec) == expected["processing_contract"]
    assert profile.scene_contract() == expected["scene_contract"]
    assert profile.runtime_requirements() == expected["runtime_requirements"]
    assert profile.capture_request_profile() == expected["capture_request_profile"]
    assert sorted(profile.expected_mesh_names(spec)) == expected["expected_mesh_names"]
    assert profile.lod1_mesh_required(spec) is expected["lod1_mesh_required"]
    assert render_scene_contract(profile.scene_contract()) == expected["scene_tscn"]


def test_every_builtin_profile_has_a_contract_golden() -> None:
    covered = {value["qualified"] for value in GOLDEN["profile_outputs"].values()}
    assert covered == {"static_prop@1", "pickup@1", "modular_piece@1"}


@pytest.mark.parametrize(
    ("profile_key", "lod_policy", "expect_lod1"),
    [
        ("static_prop@1", "lod0_lod1", True),
        ("pickup@1", "lod0_only", False),
        ("pickup@1", "lod0_lod1", True),
        ("modular_piece@1", "lod0_only", False),
        ("modular_piece@1", "lod0_lod1", True),
    ],
)
def test_lod_behavior_is_frozen(profile_key: str, lod_policy: str, expect_lod1: bool) -> None:
    source = {
        "static_prop@1": "min_050",
        "pickup@1": "pickup_fixture_050",
        "modular_piece@1": "wall_fixture_050",
    }[profile_key]
    data = _spec(source).model_dump(mode="json")
    data["lod_policy"] = lod_policy
    spec = parse_asset_specification(data)
    profile = spec.bound_profile()
    assert profile.qualified == profile_key
    assert profile.lod1_mesh_required(spec) is expect_lod1
    assert profile.processing_contract(spec)["lod1_required"] is expect_lod1
    names = {f"SM_{spec.asset_id}_LOD0", f"COL_{spec.asset_id}"}
    if expect_lod1:
        names.add(f"SM_{spec.asset_id}_LOD1")
    assert profile.expected_mesh_names(spec) == names


def test_static_prop_rejects_lod0_only() -> None:
    data = dict(MINIMAL_050, lod_policy="lod0_only")
    with pytest.raises(SpecInvalidError, match="lod_policy lod0_only is not allowed"):
        parse_asset_specification(data)


# --------------------------------------------------------------------------
# 3. Validator ordered findings
# --------------------------------------------------------------------------


def _read_document(raw: bytes) -> tuple[dict[str, Any], bytes]:
    json_len = struct.unpack_from("<I", raw, 12)[0]
    return json.loads(raw[20 : 20 + json_len].decode().rstrip()), raw[20 + json_len + 8 :]


def _write_document(path: Path, doc: dict[str, Any], binary: bytes) -> None:
    json_data = json.dumps(doc, separators=(",", ":")).encode()
    json_data += b" " * ((-len(json_data)) % 4)
    binary += b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(json_data) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_data), 0x4E4F534A)
        + json_data
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def _box(path: Path, spec: AssetSpecification, **overrides: Any) -> None:
    arguments: dict[str, Any] = {
        "width_m": spec.dimensions.width_m,
        "depth_m": spec.dimensions.depth_m,
        "height_m": spec.dimensions.height_m,
        "mesh_name": f"SM_{spec.asset_id}",
        "collider_name": f"COL_{spec.asset_id}",
        "origin": spec.origin_policy,
        "output_path": path,
    }
    arguments.update(overrides)
    create_box_glb(**arguments)


def _build_case(name: str, tmp_path: Path) -> tuple[Path, AssetSpecification]:
    crate = _spec("crate_040")
    pickup = _spec("pickup_fixture_050")
    wall = _spec("wall_fixture_050")
    path = tmp_path / f"{name}.glb"
    boxes: dict[str, tuple[AssetSpecification, dict[str, Any]]] = {
        "static_valid": (
            crate,
            {"include_lod1": True, "include_collider": True, "include_texture": True},
        ),
        "static_missing_collider": (crate, {"include_lod1": True, "include_collider": False}),
        "static_missing_lod1": (crate, {"include_lod1": False, "include_collider": True}),
        "static_over_budget": (
            crate,
            {
                "include_lod1": True,
                "include_collider": True,
                "num_materials": 3,
                "include_texture": True,
                "texture_size": (4096, 4096),
            },
        ),
        "static_wrong_mesh_names": (
            crate,
            {"include_lod1": True, "include_collider": True, "mesh_name": "SM_wrong"},
        ),
        "pickup_valid": (pickup, {"include_lod1": False, "include_collider": True}),
        "pickup_missing_collider": (pickup, {"include_lod1": False, "include_collider": False}),
        "pickup_wrong_scale": (
            pickup,
            {"include_lod1": False, "include_collider": True, "height_m": 0.8},
        ),
        "modular_valid": (wall, {"include_lod1": False, "include_collider": True}),
        "modular_wrong_origin": (
            wall,
            {"include_lod1": False, "include_collider": True, "origin": "center"},
        ),
    }
    if name in boxes:
        spec, overrides = boxes[name]
        _box(path, spec, **overrides)
        return path, spec
    if name == "static_rotated_lod0":
        _box(path, crate, include_lod1=True, include_collider=True)
        doc, binary = _read_document(path.read_bytes())
        for node in doc["nodes"]:
            if node.get("name") == f"SM_{crate.asset_id}_LOD0":
                node["rotation"] = [0.0, 1.0, 0.0, 0.0]
        _write_document(path, doc, binary)
        return path, crate
    if name == "static_truncated":
        _box(path, crate, include_lod1=True, include_collider=True)
        path.write_bytes(path.read_bytes()[:-8])
        return path, crate
    if name == "missing_file":
        return tmp_path / "missing.glb", crate
    raise AssertionError(f"no builder for validator golden {name}")


@pytest.mark.parametrize("name", sorted(GOLDEN["validator"]))
def test_validator_ordered_findings_match_v06(name: str, tmp_path: Path) -> None:
    expected = GOLDEN["validator"][name]
    path, spec = _build_case(name, tmp_path)
    result = validate_glb(path, spec)
    actual = []
    for finding in result.findings:
        assert finding.artifact == str(path)
        value = finding.actual
        if finding.rule_id == "glb.hash":
            assert re.fullmatch(r"[0-9a-f]{64}", value)
            assert value == hashlib.sha256(path.read_bytes()).hexdigest()
            value = "<sha256-of-artifact>"
        actual.append(
            {
                "rule_id": finding.rule_id,
                "severity": finding.severity.value,
                "expected": finding.expected,
                "actual": value,
            }
        )
    assert actual == expected["findings"]
    assert result.status.value == expected["status"]
    assert result.summary == expected["summary"]


def test_validator_golden_preserves_interleaved_parse_failure_order() -> None:
    """The V0.6 validator appends a second glb.parse FAIL after partial checks."""
    rules = [
        (row["rule_id"], row["severity"])
        for row in GOLDEN["validator"]["static_wrong_mesh_names"]["findings"]
    ]
    assert rules[0] == ("glb.parse", "PASS")
    assert rules[-1] == ("glb.parse", "FAIL")
    assert rules[-2] == ("textures.dimension", "PASS")
    assert "scale.bounds" not in {rule for rule, _ in rules}


def test_validator_golden_covers_every_builtin_profile() -> None:
    names = set(GOLDEN["validator"])
    assert {"static_valid", "pickup_valid", "modular_valid"} <= names
    for valid in ("static_valid", "pickup_valid", "modular_valid"):
        assert GOLDEN["validator"][valid]["status"] == "PASS"
    snap_rules = [row["rule_id"] for row in GOLDEN["validator"]["modular_valid"]["findings"]]
    assert "dimensions.snap" in snap_rules
    assert snap_rules.index("dimensions.snap") == snap_rules.index("origin.policy") + 1


# --------------------------------------------------------------------------
# 4. Historical schema compatibility
# --------------------------------------------------------------------------


def test_spec_040_contract_is_frozen() -> None:
    spec = _spec("min_040")
    assert (spec.category, spec.profile, spec.profile_version) == ("prop", "static_prop", None)
    assert spec.bound_profile().qualified == "static_prop@1"
    assert spec.target_import_path == "assets/generated/props/golden_minimal_040/"
    for override, message in (
        ({"profile_version": 1}, "does not carry profile_version"),
        ({"category": "pickup", "profile": "pickup"}, "only accepts category prop"),
        ({"lod_policy": "lod0_only"}, "only accepts a box collider and lod0_lod1"),
    ):
        with pytest.raises(SpecInvalidError, match=message):
            parse_asset_specification(dict(MINIMAL_040, **override))


def test_spec_050_contract_is_frozen() -> None:
    spec = _spec("min_050")
    assert spec.bound_profile().qualified == "static_prop@1"
    missing = {key: value for key, value in MINIMAL_050.items() if key != "profile_version"}
    with pytest.raises(SpecInvalidError, match="requires profile_version"):
        parse_asset_specification(missing)
    with pytest.raises(SpecInvalidError, match="static_prop@2 is not registered"):
        parse_asset_specification(dict(MINIMAL_050, profile_version=2))
    with pytest.raises(SpecInvalidError, match="category pickup is not supported"):
        parse_asset_specification(dict(MINIMAL_050, category="pickup"))


@pytest.mark.parametrize("schema_version", ["0.4.0", "0.5.0"])
@pytest.mark.parametrize(
    ("category", "profile"),
    [
        ("vehicle", "vehicle"),
        ("weapon", "weapon"),
        ("aircraft", "aircraft"),
        ("character", "character"),
        ("prop", "vehicle"),
        ("prop", "weapon"),
        ("prop", "aircraft"),
        ("prop", "character"),
    ],
)
def test_historical_specs_reject_advanced_identifiers(
    schema_version: str, category: str, profile: str
) -> None:
    base = MINIMAL_040 if schema_version == "0.4.0" else MINIMAL_050
    with pytest.raises(SpecInvalidError):
        parse_asset_specification(dict(base, category=category, profile=profile))


@pytest.mark.parametrize("schema_version", ["0.4.0", "0.5.0"])
@pytest.mark.parametrize("field", sorted(V07_ONLY_SPEC_FIELDS))
def test_historical_specs_reject_v07_only_fields(schema_version: str, field: str) -> None:
    base = MINIMAL_040 if schema_version == "0.4.0" else MINIMAL_050
    with pytest.raises(SpecInvalidError, match="Extra inputs are not permitted"):
        parse_asset_specification(dict(base, **{field: V07_ONLY_SPEC_FIELDS[field]}))


@pytest.mark.parametrize("qualified", sorted(PROFILE_PATHS))
@pytest.mark.parametrize("field", sorted(V07_ONLY_PROFILE_FIELDS))
def test_profile_schema_050_rejects_v07_only_fields(qualified: str, field: str) -> None:
    document = dict(GOLDEN["profile_documents"][qualified])
    document[field] = V07_ONLY_PROFILE_FIELDS[field]
    with pytest.raises(ProfileContractError, match="Extra inputs are not permitted"):
        parse_profile_document(document)


@pytest.mark.parametrize("qualified", sorted(PROFILE_PATHS))
def test_profile_schema_050_rejects_advanced_categories_and_policies(qualified: str) -> None:
    base = GOLDEN["profile_documents"][qualified]
    for category in ("vehicle", "weapon", "aircraft", "character"):
        document = dict(base, categories=[category])
        with pytest.raises(ProfileContractError, match="unsupported asset category"):
            parse_profile_document(document)
    for collider in ("convex_hull", "compound_box", "none"):
        processing = dict(base["processing"], allowed_collider_policies=[collider])
        with pytest.raises(ProfileContractError, match="allowed_collider_policies"):
            parse_profile_document(dict(base, processing=processing))
    for body_kind in ("rigid_body", "character_body", "vehicle_body"):
        godot = dict(base["godot"], body_kind=body_kind)
        with pytest.raises(ProfileContractError, match="body_kind"):
            parse_profile_document(dict(base, godot=godot))


@pytest.mark.parametrize("qualified", sorted(PROFILE_PATHS))
def test_profile_schema_050_documents_round_trip(qualified: str) -> None:
    document = GOLDEN["profile_documents"][qualified]
    assert document["schema_version"] == "asset-profile-0.5.0"
    assert parse_profile_document(dict(document)).model_dump(mode="json") == document
