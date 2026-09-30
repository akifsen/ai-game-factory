"""Unit tests for AssemblySourceProvenance and domain contracts per ADR 0016."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from gamefactory.core.domain.assembly_source import (
    MAX_ASSEMBLY_SOURCE_BYTES,
    AssemblyIngestResult,
    AssemblyPublicationMarker,
    AssemblySourceProvenance,
    DerivedFromEntry,
    assert_provenance_matches_spec,
    create_assembly_provenance,
)
from gamefactory.core.domain.asset_contracts import (
    parse_asset_specification_v07,
    spec_fingerprint,
)
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import ValidationError


def _dummy_profile() -> AssetProfileV07:
    from gamefactory.core.domain.asset_profiles import builtin_registry

    base = builtin_registry().get("static_prop").document.model_dump(mode="json")
    assembly = {
        "roles": ["hull", "turret"],
        "required_roles": ["hull", "turret"],
        "role_motion_constraints": {},
        "required_sockets": [],
        "pivot_tolerance_m": 0.01,
        "basis_tolerance_deg": 1.0,
        "socket_position_tolerance_m": 0.01,
        "socket_angle_tolerance_deg": 1.0,
    }
    data = {
        "schema_version": "asset-profile-0.7.0",
        "profile_id": "test_assembly_profile",
        "version": 1,
        "categories": ["vehicle"],
        "review_views": ["front"],
        "framing": base["framing"],
        "dimension_rules": {"min_m": 0.1, "max_m": 10.0},
        "processing": {
            "lod0_required": True,
            "lod1_required": False,
            "allowed_lod_policies": ["lod0_only", "lod0_lod1"],
            "allowed_collider_policies": ["box"],
            "allowed_origin_policies": ["bottom_center", "center"],
            "default_origin_policy": "center",
            "dimension_tolerance_m": 0.02,
            "snap_grid_m": None,
            "rig_forbidden": True,
            "animation_forbidden": True,
            "max_materials": 4,
            "max_texture_dimension": 2048,
            "max_triangles_lod0": 10000,
        },
        "godot": base["godot"],
        "runtime": base["runtime"],
        "geometry_mode": "assembly",
        "accepted_source_kinds": ["local_operator_assembly"],
        "assembly": assembly,
    }
    return AssetProfileV07(parse_profile_document_v07(data))


def _sample_spec() -> Any:
    profile = _dummy_profile()
    registry = ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))
    spec_data = {
        "schema_version": "0.7.0",
        "asset_id": "rover_v1",
        "category": "vehicle",
        "profile": "test_assembly_profile",
        "profile_version": 1,
        "intent": "Authoring test rover",
        "source_kind": "local_operator_assembly",
        "dimensions": {"width_m": 2.0, "height_m": 1.5, "depth_m": 3.0},
        "origin_policy": "center",
        "lod_policy": "lod0_only",
        "geometry_budget": {
            "max_triangles_lod0": 8000,
            "max_triangles_lod1": 4000,
            "lod_ratio": 0.5,
        },
        "material_budget": {"max_materials": 2},
        "texture_budget": {"max_dimension": 2048},
        "collider": {"policy": "box"},
        "parts": [
            {
                "part_id": "hull",
                "role": "hull",
                "parent": "root",
                "pivot": {
                    "position_m": [0, 0, 0],
                    "basis": "identity",
                    "motion": {"kind": "fixed"},
                },
            },
            {
                "part_id": "turret",
                "role": "turret",
                "parent": "hull",
                "pivot": {
                    "position_m": [0, 0.5, 0],
                    "basis": "identity",
                    "motion": {"kind": "fixed"},
                },
            },
        ],
        "sockets": [
            {
                "socket_id": "antenna",
                "parent_part": "turret",
                "translation_m": [0, 0.2, 0],
                "rotation": "identity",
                "placement": "forward_end",
            }
        ],
    }
    return parse_asset_specification_v07(spec_data, registry=registry)


def test_assembly_provenance_creation_and_canonical_bytes() -> None:
    spec = _sample_spec()
    prov = create_assembly_provenance(
        source_artifact_sha256="a" * 64,
        source_artifact_byte_size=1024,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.0",
        source_front="-Z",
        spec=spec,
        actor="engineer_alice",
        reason="Initial authored rover baseline",
    )

    assert prov.schema_version == "0.7.0"
    assert prov.source_provenance_type == "local_operator_assembly"
    assert prov.paid is False
    assert prov.source_front == "-Z"
    assert prov.spec_fingerprint == spec_fingerprint(spec)
    assert set(prov.part_map.keys()) == {"hull", "turret"}
    assert set(prov.socket_map.keys()) == {"antenna"}

    canonical_bytes = prov.to_canonical_bytes()
    canonical_json = prov.to_canonical_json()
    assert isinstance(canonical_bytes, bytes)
    assert json.loads(
        canonical_json.decode("utf-8") if isinstance(canonical_json, bytes) else canonical_json
    )
    assert prov.provenance_hash()


@pytest.mark.parametrize("invalid_paid", [True, 0, 1, None, "false", "False"])
def test_reject_invalid_paid_types(invalid_paid: Any) -> None:
    spec = _sample_spec()
    prov_dict = create_assembly_provenance(
        source_artifact_sha256="a" * 64,
        source_artifact_byte_size=1024,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.0",
        source_front="-Z",
        spec=spec,
        actor="engineer_alice",
        reason="test",
    ).to_canonical_dict()

    prov_dict["paid"] = invalid_paid
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblySourceProvenance.model_validate(prov_dict)


@pytest.mark.parametrize("invalid_front", ["+X", "-X", "+Y", "-Y", "Z", "", None])
def test_reject_invalid_source_front(invalid_front: Any) -> None:
    spec = _sample_spec()
    prov_dict = create_assembly_provenance(
        source_artifact_sha256="a" * 64,
        source_artifact_byte_size=1024,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.0",
        source_front="-Z",
        spec=spec,
        actor="engineer_alice",
        reason="test",
    ).to_canonical_dict()

    prov_dict["source_front"] = invalid_front
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblySourceProvenance.model_validate(prov_dict)


def test_reject_extra_closed_fields() -> None:
    spec = _sample_spec()
    prov_dict = create_assembly_provenance(
        source_artifact_sha256="a" * 64,
        source_artifact_byte_size=1024,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.0",
        source_front="+Z",
        spec=spec,
        actor="engineer_alice",
        reason="test",
    ).to_canonical_dict()

    prov_dict["unauthorized_field"] = "injection"
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblySourceProvenance.model_validate(prov_dict)


def test_reject_invalid_byte_size_and_sha256() -> None:
    spec = _sample_spec()
    prov_dict = create_assembly_provenance(
        source_artifact_sha256="a" * 64,
        source_artifact_byte_size=1024,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.0",
        source_front="+Z",
        spec=spec,
        actor="engineer_alice",
        reason="test",
    ).to_canonical_dict()

    # Byte size < 20
    prov_dict["source_artifact_byte_size"] = 10
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblySourceProvenance.model_validate(prov_dict)

    # Byte size > MAX_ASSEMBLY_SOURCE_BYTES
    prov_dict["source_artifact_byte_size"] = MAX_ASSEMBLY_SOURCE_BYTES + 1
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblySourceProvenance.model_validate(prov_dict)

    # Byte size is boolean
    prov_dict["source_artifact_byte_size"] = True
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblySourceProvenance.model_validate(prov_dict)

    # Invalid sha256
    prov_dict["source_artifact_byte_size"] = 1024
    prov_dict["source_artifact_sha256"] = "invalid_hash"
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblySourceProvenance.model_validate(prov_dict)


def test_derived_from_validation_valid_and_invalid() -> None:
    spec = _sample_spec()
    valid_derived = [
        DerivedFromEntry(
            part_id="hull",
            source_artifact_sha256="b" * 64,
            source_asset_id="chassis_base",
            source_revision=2,
        )
    ]

    prov = create_assembly_provenance(
        source_artifact_sha256="a" * 64,
        source_artifact_byte_size=1024,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.0",
        source_front="-Z",
        spec=spec,
        actor="engineer_alice",
        reason="Reusing chassis_base hull",
        derived_from=valid_derived,
    )
    assert prov.derived_from is not None
    assert len(prov.derived_from) == 1
    assert prov.derived_from[0].part_id == "hull"

    # Unknown part_id in derived_from
    unknown_part_derived = [
        DerivedFromEntry(
            part_id="unknown_wing",
            source_artifact_sha256="b" * 64,
            source_asset_id="wing_part",
            source_revision=1,
        )
    ]
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        create_assembly_provenance(
            source_artifact_sha256="a" * 64,
            source_artifact_byte_size=1024,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.0",
            source_front="-Z",
            spec=spec,
            actor="engineer_alice",
            reason="test",
            derived_from=unknown_part_derived,
        )

    # Duplicate part_id in derived_from
    duplicate_derived = [
        DerivedFromEntry(
            part_id="hull",
            source_artifact_sha256="b" * 64,
            source_asset_id="chassis_base",
            source_revision=1,
        ),
        DerivedFromEntry(
            part_id="hull",
            source_artifact_sha256="c" * 64,
            source_asset_id="chassis_alt",
            source_revision=2,
        ),
    ]
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        create_assembly_provenance(
            source_artifact_sha256="a" * 64,
            source_artifact_byte_size=1024,
            authoring_tool_name="Blender",
            authoring_tool_version="4.2.0",
            source_front="-Z",
            spec=spec,
            actor="engineer_alice",
            reason="test",
            derived_from=duplicate_derived,
        )


def test_assert_provenance_matches_spec_catches_tamper() -> None:
    spec = _sample_spec()
    prov = create_assembly_provenance(
        source_artifact_sha256="a" * 64,
        source_artifact_byte_size=1024,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.0",
        source_front="-Z",
        spec=spec,
        actor="engineer_alice",
        reason="test",
    )
    assert_provenance_matches_spec(prov, spec)

    # Tamper with spec fingerprint
    tampered_fp = prov.model_copy(update={"spec_fingerprint": "f" * 64})
    with pytest.raises(ValidationError, match="does not match expected"):
        assert_provenance_matches_spec(tampered_fp, spec)

    # Tamper with part_map
    missing_part_map = dict(prov.part_map)
    del missing_part_map["turret"]
    tampered_parts = prov.model_copy(update={"part_map": missing_part_map})
    with pytest.raises(ValidationError, match="part_map keys"):
        assert_provenance_matches_spec(tampered_parts, spec)


@pytest.mark.parametrize("missing_field", ["paid", "schema_version", "source_provenance_type"])
def test_reject_omission_of_required_provenance_markers(missing_field: str) -> None:
    spec = _sample_spec()
    prov_dict = create_assembly_provenance(
        source_artifact_sha256="a" * 64,
        source_artifact_byte_size=1024,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.0",
        source_front="-Z",
        spec=spec,
        actor="engineer_alice",
        reason="test",
    ).to_canonical_dict()

    del prov_dict[missing_field]
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblySourceProvenance.model_validate(prov_dict)


def test_deep_immutability_and_detached_provenance_views() -> None:
    from pathlib import Path

    from gamefactory.core.domain.assembly_source import AssemblyIngestResult

    spec = _sample_spec()
    prov = create_assembly_provenance(
        source_artifact_sha256="a" * 64,
        source_artifact_byte_size=1024,
        authoring_tool_name="Blender",
        authoring_tool_version="4.2.0",
        source_front="-Z",
        spec=spec,
        actor="engineer_alice",
        reason="test",
        derived_from=[
            DerivedFromEntry(
                part_id="hull",
                source_artifact_sha256="b" * 64,
                source_asset_id="chassis_base",
                source_revision=1,
            )
        ],
    )
    canonical_bytes = prov.to_canonical_bytes()
    canonical_hash = prov.provenance_hash()

    result = AssemblyIngestResult(
        retained_glb_path=Path("retained/source.glb"),
        retained_provenance_path=Path("retained/source_provenance.json"),
        retained_glb_sha256="a" * 64,
        retained_glb_byte_size=1024,
        retained_provenance_sha256=canonical_hash,
        retained_provenance_bytes=canonical_bytes,
        spec_fingerprint=prov.spec_fingerprint,
        package_dir=Path("retained"),
    )

    # 1. Mutate detached part_map
    view1 = result.provenance
    assert len(view1.part_map) == 2
    view1.part_map.clear()
    assert len(view1.part_map) == 0

    # Retained authoritative package remains intact
    assert result.retained_provenance_sha256 == canonical_hash
    assert result.retained_provenance_bytes == canonical_bytes
    view2 = result.provenance
    assert len(view2.part_map) == 2
    assert "hull" in view2.part_map

    # 2. Mutate map by replacing an entry on a detached view
    view2.part_map["hull"] = view2.part_map["turret"]
    view3 = result.provenance
    assert view3.part_map["hull"].part_id == "hull"

    # 3. Mutate derived_from on detached view
    assert view3.derived_from is not None and len(view3.derived_from) == 1
    view3.derived_from.clear()
    view4 = result.provenance
    assert view4.derived_from is not None and len(view4.derived_from) == 1

    # 4. Mutate input spec after provenance creation
    assert spec.parts is not None
    spec.parts.clear()
    view5 = result.provenance
    assert len(view5.part_map) == 2
    assert "hull" in view5.part_map

    # 5. Direct immutability of AssemblyIngestResult attributes
    with pytest.raises(AttributeError, match="immutable"):
        result.retained_glb_sha256 = "b" * 64

    with pytest.raises(AttributeError, match="immutable"):
        del result.package_dir


def test_assembly_publication_marker_validation() -> None:
    valid_marker_dict = {
        "schema_version": "0.7.0",
        "marker_type": "assembly_publication_completion",
        "source_artifact_sha256": "a" * 64,
        "source_artifact_byte_size": 2048,
        "provenance_sha256": "b" * 64,
        "provenance_byte_size": 1024,
        "spec_fingerprint": "c" * 64,
        "created_at": "2026-09-30T00:00:00Z",
    }
    marker = AssemblyPublicationMarker.model_validate(valid_marker_dict)
    assert marker.schema_version == "0.7.0"
    assert marker.marker_type == "assembly_publication_completion"
    assert marker.source_artifact_sha256 == "a" * 64
    assert len(marker.to_canonical_bytes()) > 0
    assert json.loads(marker.to_canonical_json())

    # Reject invalid schema version
    bad_ver = dict(valid_marker_dict, schema_version="0.6.0")
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblyPublicationMarker.model_validate(bad_ver)

    # Reject invalid marker type
    bad_type = dict(valid_marker_dict, marker_type="single_mesh_completion")
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblyPublicationMarker.model_validate(bad_type)

    # Reject invalid sha256
    bad_sha = dict(valid_marker_dict, source_artifact_sha256="not_a_sha256")
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblyPublicationMarker.model_validate(bad_sha)

    # Reject invalid byte size
    bad_size = dict(valid_marker_dict, source_artifact_byte_size=0)
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblyPublicationMarker.model_validate(bad_size)

    # Reject boolean byte size
    bool_size = dict(valid_marker_dict, source_artifact_byte_size=True)
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblyPublicationMarker.model_validate(bool_size)

    # Reject extra fields
    extra_field = dict(valid_marker_dict, extra_injected="injection")
    with pytest.raises((ValidationError, PydanticValidationError, ValueError)):
        AssemblyPublicationMarker.model_validate(extra_field)


def test_assembly_ingest_result_explicit_constructor() -> None:
    from pathlib import Path

    result = AssemblyIngestResult(
        retained_glb_path=Path("sources/source.glb"),
        retained_provenance_path=Path("sources/source_provenance.json"),
        retained_glb_sha256="1" * 64,
        retained_glb_byte_size=4096,
        retained_provenance_sha256="2" * 64,
        retained_provenance_bytes=b'{"paid":false}',
        spec_fingerprint="3" * 64,
        package_dir=Path("sources"),
    )
    assert result.retained_glb_sha256 == "1" * 64
    assert result.retained_glb_byte_size == 4096
    assert result.spec_fingerprint == "3" * 64
    assert result.package_dir == Path("sources")
