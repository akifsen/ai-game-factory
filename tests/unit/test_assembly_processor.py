"""Unit tests for standalone V0.7 authored-assembly Blender processing adapter."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import runpy
import struct
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

import pytest

from gamefactory.adapters.assets.assembly_ingest import ingest_assembly_source
from gamefactory.adapters.assets.glb_validator import _accessor
from gamefactory.adapters.assets.v07_geometry_validation import (
    VerifiedSourceNormalization,
    _triangle_soup,
    verify_source_to_processed_preservation,
)
from gamefactory.adapters.dcc.assembly_processor import (
    _MAX_CONTRACT_BYTES,
    AssemblyProcessor,
    AssemblyProcessResult,
    _prove_processed_assembly_glb,
    _read_bounded_json_object,
    _serialize_contract,
)
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.assembly_source import AssemblyIngestResult
from gamefactory.core.domain.asset_contracts import (
    AssetSpecificationV07,
    parse_asset_specification_v07,
)
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    builtin_registry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import DccFailedError, ValidationError
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner


def _create_assembly_profile(*, required_sockets: bool = True) -> AssetProfileV07:
    base = builtin_registry().get("static_prop").document.model_dump(mode="json")
    assembly_conf = {
        "roles": ["hull", "turret", "barrel"],
        "required_roles": ["hull", "turret", "barrel"],
        "role_motion_constraints": {},
        "required_sockets": (
            [
                {
                    "socket_id": "muzzle",
                    "parent_role": "barrel",
                    "placement": "forward_end",
                    "forward_end_fraction": 0.2,
                    "rest_forward": [0, 0, -1],
                }
            ]
            if required_sockets
            else []
        ),
        "pivot_tolerance_m": 0.01,
        "basis_tolerance_deg": 1.0,
        "socket_position_tolerance_m": 0.01,
        "socket_angle_tolerance_deg": 1.0,
    }
    doc = {
        "schema_version": "asset-profile-0.7.0",
        "profile_id": "test_assembly_processor_profile",
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
        "assembly": assembly_conf,
    }
    return AssetProfileV07(parse_profile_document_v07(doc))


def _create_assembly_spec(
    profile: AssetProfileV07,
    *,
    source_front: str = "-Z",
    root_basis: Any = "identity",
) -> AssetSpecificationV07:
    parts = [
        {
            "part_id": "hull",
            "role": "hull",
            "parent": "root",
            "pivot": {
                "position_m": [0, -0.2, 0],
                "basis": root_basis,
                "motion": {"kind": "fixed"},
            },
        },
        {
            "part_id": "turret",
            "role": "turret",
            "parent": "hull",
            "pivot": {
                "position_m": [0, 0.2, 0.1],
                "basis": "identity",
                "motion": {"kind": "revolute", "axis": [0, 1, 0]},
            },
        },
        {
            "part_id": "barrel",
            "role": "barrel",
            "parent": "turret",
            "pivot": {
                "position_m": [0, 0.2, -0.2],
                "basis": "identity",
                "motion": {"kind": "revolute", "axis": [1, 0, 0]},
            },
        },
    ]
    sockets = [
        {
            "socket_id": "muzzle",
            "parent_part": "barrel",
            "translation_m": [0, 0, -0.49],
            "rotation": "identity",
            "placement": "forward_end",
        }
    ]
    data: dict[str, Any] = {
        "schema_version": "0.7.0",
        "asset_id": "test_assembly_tank",
        "category": "vehicle",
        "profile": profile.profile_id,
        "profile_version": profile.version,
        "intent": "assembly unit test",
        "source_kind": "local_operator_assembly",
        "dimensions": {"width_m": 1.0, "height_m": 1.4, "depth_m": 1.2},
        "origin_policy": "center",
        "lod_policy": "lod0_only",
        "geometry_budget": {
            "max_triangles_lod0": 10000,
            "max_triangles_lod1": 5000,
            "lod_ratio": 0.5,
        },
        "material_budget": {"max_materials": 4},
        "texture_budget": {"max_dimension": 2048},
        "collider": {"policy": "box"},
        "parts": parts,
        "sockets": sockets,
    }
    registry = ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))
    return parse_asset_specification_v07(data, registry=registry)


def _read_glb(raw: bytes) -> tuple[dict[str, Any], bytes]:
    json_len = struct.unpack_from("<I", raw, 12)[0]
    doc = json.loads(raw[20 : 20 + json_len].decode("utf-8").rstrip())
    return doc, raw[20 + json_len + 8 :]


def _write_glb(path: Path, doc: dict[str, Any], binary: bytes) -> None:
    enc = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    enc += b" " * ((-len(enc)) % 4)
    bin_pad = binary + b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(enc) + 8 + len(bin_pad)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(enc), 0x4E4F534A)
        + enc
        + struct.pack("<II", len(bin_pad), 0x004E4942)
        + bin_pad
    )


def _create_source_assembly_glb(
    path: Path,
    spec: AssetSpecificationV07,
    *,
    source_front: Literal["-Z", "+Z"] = "-Z",
) -> None:
    """Create self-contained source assembly GLB with exact ROOT->hull->turret->barrel->muzzle tree."""
    create_box_glb(
        width_m=1.0,
        depth_m=1.0,
        height_m=1.0,
        mesh_name="shared_geometry",
        origin="center",
        include_collider=False,
        output_path=path,
    )
    doc, binary = _read_glb(path.read_bytes())

    # Raw root PART rotation: if source_front == "+Z", raw root PART rotation is Ry(180Y) * canonical.
    assert spec.parts
    root_pivot = spec.parts[0].pivot
    root_basis = list(root_pivot.basis) if root_pivot.basis != "identity" else [0.0, 0.0, 0.0, 1.0]
    root_position = list(root_pivot.position_m)
    hull_rot: list[float] | None = root_basis
    if source_front == "+Z":
        hull_rot = [root_basis[2], root_basis[3], -root_basis[0], -root_basis[1]]
        root_position = [-root_position[0], root_position[1], -root_position[2]]
    elif root_basis == [0.0, 0.0, 0.0, 1.0]:
        hull_rot = None

    nodes: list[dict[str, Any]] = [
        {"name": "ROOT", "children": [1]},
        {
            "name": "PART_hull",
            "translation": root_position,
            **({"rotation": hull_rot} if hull_rot else {}),
            "extras": {"gf_motion": "fixed"},
            "children": [2, 3],
        },
        {"name": f"SM_{spec.asset_id}_hull_LOD0", "mesh": 0},
        {
            "name": "PART_turret",
            "translation": [0, 0.2, 0.1],
            "extras": {"gf_motion": "revolute", "gf_axis": [0, 1, 0]},
            "children": [4, 5],
        },
        {"name": f"SM_{spec.asset_id}_turret_LOD0", "mesh": 0},
        {
            "name": "PART_barrel",
            "translation": [0, 0.2, -0.2],
            "extras": {"gf_motion": "revolute", "gf_axis": [1, 0, 0]},
            "children": [6, 7],
        },
        {"name": f"SM_{spec.asset_id}_barrel_LOD0", "mesh": 0},
        {"name": "SOCKET_muzzle", "translation": [0, 0, -0.49]},
    ]
    doc["nodes"] = nodes
    doc["scenes"] = [{"nodes": [0]}]
    _write_glb(path, doc, binary)


def _setup_ingested_package(
    tmp_path: Path,
    spec: AssetSpecificationV07,
    *,
    source_front: Literal["-Z", "+Z"] = "-Z",
) -> AssemblyIngestResult:
    source_raw_path = tmp_path / "raw_source.glb"
    _create_source_assembly_glb(source_raw_path, spec, source_front=source_front)

    return ingest_assembly_source(
        source_glb_path=source_raw_path,
        managed_root=tmp_path / "managed_repo",
        relative_package_dir="test_package_01",
        spec=spec,
        authoring_tool_name="Blender",
        authoring_tool_version="5.2.1",
        source_front=source_front,
        actor="unit_test_author",
        reason="Assembly processor unit tests",
    )


def test_reject_provider_generated_and_single_mesh_before_dcc(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)

    # Ingest valid assembly package
    ingest_result = _setup_ingested_package(tmp_path, spec)
    processor = AssemblyProcessor(blender_executable="blender.exe", dependency_preflight=False)

    # Negative 1: Provider-generated source kind
    object.__setattr__(spec, "source_kind", "provider_generated")
    with pytest.raises(ValidationError, match="rejects non-assembly source_kind"):
        processor.process_assembly(
            package=ingest_result,
            spec=spec,
            expected_provenance_sha256=ingest_result.retained_provenance_sha256,
            processed_glb_path=tmp_path / "out.glb",
        )

    # Negative 2: Single-mesh geometry mode
    object.__setattr__(spec, "source_kind", "local_operator_assembly")
    object.__setattr__(profile.document, "geometry_mode", "single_mesh")
    with pytest.raises(ValidationError, match="rejects non-assembly profile geometry_mode"):
        processor.process_assembly(
            package=ingest_result,
            spec=spec,
            expected_provenance_sha256=ingest_result.retained_provenance_sha256,
            processed_glb_path=tmp_path / "out.glb",
        )


def test_mandatory_pinned_provenance_authentication(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    ingest_result = _setup_ingested_package(tmp_path, spec)
    processor = AssemblyProcessor(blender_executable="blender.exe", dependency_preflight=False)

    # Tampered or wrong pinned hash must fail before running Blender
    wrong_pin = "0" * 64
    with pytest.raises(ValidationError, match="Authenticated provenance verification failed"):
        processor.process_assembly(
            package=ingest_result,
            spec=spec,
            expected_provenance_sha256=wrong_pin,
            processed_glb_path=tmp_path / "out.glb",
        )

    # Invalid hex length must fail
    with pytest.raises(
        ValidationError, match="expected_provenance_sha256 must be a valid 64-character"
    ):
        processor.process_assembly(
            package=ingest_result,
            spec=spec,
            expected_provenance_sha256="not_a_sha256",
            processed_glb_path=tmp_path / "out.glb",
        )


def test_refuse_clobber_existing_output_targets(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    ingest_result = _setup_ingested_package(tmp_path, spec)
    processor = AssemblyProcessor(blender_executable="blender.exe", dependency_preflight=False)

    existing_glb = tmp_path / "preexisting.glb"
    existing_glb.write_bytes(b"existing content")

    # Refuse to overwrite existing output GLB
    with pytest.raises(ValueError, match="refusing to overwrite existing processed GLB"):
        processor.process_assembly(
            package=ingest_result,
            spec=spec,
            expected_provenance_sha256=ingest_result.retained_provenance_sha256,
            processed_glb_path=existing_glb,
        )

    existing_report = tmp_path / "preexisting.json"
    existing_report.write_text("{}", encoding="utf-8")

    # Refuse to overwrite existing report
    with pytest.raises(ValueError, match="refusing to overwrite existing processing report"):
        processor.process_assembly(
            package=ingest_result,
            spec=spec,
            expected_provenance_sha256=ingest_result.retained_provenance_sha256,
            processed_glb_path=tmp_path / "new.glb",
            report_path=existing_report,
        )


def test_blender_script_publication_preserves_concurrent_target(tmp_path: Path) -> None:
    publish = runpy.run_path(str(AssemblyProcessor.get_script_path()))["_publish_no_clobber"]
    staged_output = tmp_path / "staged.glb"
    staged_report = tmp_path / "staged.json"
    output_target = tmp_path / "final.glb"
    report_target = tmp_path / "final.json"
    staged_output.write_bytes(b"complete generated artifact")
    staged_report.write_bytes(b"complete report")
    output_target.write_bytes(b"created concurrently")

    with pytest.raises(FileExistsError):
        publish(staged_output, staged_report, output_target, report_target)

    assert output_target.read_bytes() == b"created concurrently"
    assert not report_target.exists()


def test_contract_serialization_is_bounded_before_publication(tmp_path: Path) -> None:
    contract_path = tmp_path / "huge.contract.json"
    with pytest.raises(ValidationError, match="exceeds"):
        _serialize_contract({"parts": ["x" * _MAX_CONTRACT_BYTES]})
    assert not contract_path.exists()


@pytest.mark.parametrize(
    ("contents", "limit", "match"),
    [
        (b"[]", 10, "JSON object"),
        (b"{broken", 16, "valid UTF-8 JSON"),
        (b"{" + b" " * 64, 16, "exceeds"),
    ],
)
def test_adapter_bounded_report_reader_rejects_nonobjects_and_oversize(
    tmp_path: Path, contents: bytes, limit: int, match: str
) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(contents)
    with pytest.raises(DccFailedError, match=match):
        _read_bounded_json_object(report, "test report", limit)


@pytest.mark.parametrize(
    ("content_kind", "match"),
    [("nonobject", "JSON object"), ("oversized", "exceeds")],
)
def test_blender_consumer_contract_reader_is_bounded_and_requires_object(
    tmp_path: Path, content_kind: str, match: str
) -> None:
    read_contract = runpy.run_path(str(AssemblyProcessor.get_script_path()))["_read_contract"]
    contract = tmp_path / "contract.json"
    contents = b"[]" if content_kind == "nonobject" else b" " * (_MAX_CONTRACT_BYTES + 1)
    contract.write_bytes(contents)
    with pytest.raises(ValueError, match=match):
        read_contract(contract)


def test_blender_report_producer_is_bounded_before_file_creation(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(AssemblyProcessor.get_script_path()))
    serialize_report = namespace["_serialize_report"]
    report_path = tmp_path / "oversized.stage.json"
    with pytest.raises(ValueError, match="processing report exceeds"):
        serialized = serialize_report({"metrics": "x" * _MAX_CONTRACT_BYTES})
        report_path.write_bytes(serialized)
    assert not report_path.exists()


@pytest.mark.parametrize("replace_by", [b"foreign replacement", None])
def test_blender_staging_cleanup_preserves_replaced_or_mutated_files(
    tmp_path: Path, replace_by: bytes | None
) -> None:
    namespace = runpy.run_path(str(AssemblyProcessor.get_script_path()))
    capture = namespace["_capture_staged_file"]
    cleanup = namespace["_remove_staged_if_unchanged"]
    stage = tmp_path / "private.stage.glb"
    stage.write_bytes(b"owned generated file")
    identity = capture(stage, 1024)
    assert identity.device is not None and identity.inode is not None
    if replace_by is None:
        stage.write_bytes(b"mutated same inode")
    else:
        stage.unlink()
        stage.write_bytes(replace_by)
    cleanup(stage, identity, 1024)
    assert stage.exists()
    assert stage.read_bytes() == (replace_by if replace_by is not None else b"mutated same inode")


class _FailingAssemblyRunner(ProcessRunner):
    def __init__(self, mutation: str | None = None) -> None:
        super().__init__(sanitize_output=False)
        self.mutation = mutation
        self.calls = 0

    def build_env(self, request: CommandRequest) -> dict[str, str]:
        return {}

    def run(self, request: CommandRequest) -> CommandResult:
        self.calls += 1
        contract = Path(request.args[request.args.index("--contract") + 1])
        if self.mutation == "replace":
            contract.unlink()
            contract.write_bytes(b"foreign replacement contract")
        elif self.mutation == "mutate":
            contract.write_bytes(b"mutated in-place contract")
        return CommandResult(exit_code=17, stdout="", stderr="simulated Blender failure")


def test_failed_dcc_cleans_unchanged_owned_contract_and_allows_retry(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    package = _setup_ingested_package(tmp_path, spec)
    runner = _FailingAssemblyRunner()
    processor = AssemblyProcessor(
        blender_executable="blender.exe", runner=runner, dependency_preflight=False
    )
    output = tmp_path / "retry.glb"
    report = tmp_path / "retry.json"
    contract = report.with_suffix(".contract.json")

    for _ in range(2):
        with pytest.raises(DccFailedError, match="simulated Blender failure"):
            processor.process_assembly(
                package,
                spec,
                expected_provenance_sha256=package.retained_provenance_sha256,
                processed_glb_path=output,
                report_path=report,
            )
        assert not contract.exists()
        assert not output.exists()
        assert not report.exists()
    assert runner.calls == 2


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [("replace", b"foreign replacement contract"), ("mutate", b"mutated in-place contract")],
)
def test_contract_cleanup_preserves_foreign_replacement_or_mutation(
    tmp_path: Path, mutation: str, expected: bytes
) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    package = _setup_ingested_package(tmp_path, spec)
    processor = AssemblyProcessor(
        blender_executable="blender.exe",
        runner=_FailingAssemblyRunner(mutation),
        dependency_preflight=False,
    )
    report = tmp_path / "foreign.json"
    contract = report.with_suffix(".contract.json")
    with pytest.raises(DccFailedError, match="simulated Blender failure"):
        processor.process_assembly(
            package,
            spec,
            expected_provenance_sha256=package.retained_provenance_sha256,
            processed_glb_path=tmp_path / "foreign.glb",
            report_path=report,
        )
    assert contract.read_bytes() == expected


def test_outside_blender_proof_oracle_catches_shape_and_equal_aabb_tamper(tmp_path: Path) -> None:
    """Equal AABB is NOT sufficient: modifying internal vertex coordinates must be rejected."""
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    ingest_result = _setup_ingested_package(tmp_path, spec)

    source_path = ingest_result.retained_glb_path
    source_bytes = source_path.read_bytes()

    # Create a processed GLB that preserves AABB bounds exactly but shifts internal vertices
    tampered_glb = tmp_path / "tampered_mesh.glb"
    doc, binary = _read_glb(source_bytes)

    # Add collider node
    doc["nodes"].append(
        {
            "name": f"COL_{spec.asset_id}",
            "mesh": 0,
        }
    )
    doc["nodes"][0]["children"].append(len(doc["nodes"]) - 1)

    # Tamper the mesh binary: change internal triangle indices or vertex positions slightly
    # but keep outer min/max accessor values
    doc["meshes"][0]["primitives"][0]["indices"] = 2  # re-assign indices
    # Mutate one byte in binary where indices are stored
    bin_list = bytearray(binary)
    # Swap two index elements: modifies triangle soup while vertex pool and AABB remain unchanged
    bin_list[160] ^= 1
    mutated_bin = bytes(bin_list)

    _write_glb(tampered_glb, doc, mutated_bin)

    obs = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(tampered_glb.read_bytes()).hexdigest(),
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0, 0, 0, 1),
        source_glb_bytes=source_bytes,
    )

    with pytest.raises(DccFailedError, match="LOD0 triangle multiset mismatch"):
        _prove_processed_assembly_glb(
            processed_glb_path=tampered_glb,
            spec=spec,
            profile=profile,
            verified_norm=obs,
            source_bytes=source_bytes,
        )


@pytest.mark.parametrize(
    ("delta", "expected_error"), [(1e-6, None), (0.1, "LOD0 triangle multiset mismatch")]
)
def test_triangle_preservation_uses_float32_tolerance_and_rejects_equal_aabb_shape_change(
    tmp_path: Path, delta: float, expected_error: str | None
) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    ingest_result = _setup_ingested_package(tmp_path, spec)
    source_bytes = ingest_result.retained_glb_path.read_bytes()
    doc, binary = _read_glb(source_bytes)
    doc, binary = _add_collider_box(doc, binary, spec)

    position_accessor_index = doc["meshes"][0]["primitives"][0]["attributes"]["POSITION"]
    accessor = doc["accessors"][position_accessor_index]
    view = doc["bufferViews"][accessor["bufferView"]]
    byte_offset = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    changed_binary = bytearray(binary)
    x, y, z = struct.unpack_from("<fff", changed_binary, byte_offset)
    struct.pack_into("<fff", changed_binary, byte_offset, x, y + delta, z + delta)

    processed_path = tmp_path / f"jitter_{str(delta).replace('.', '_')}.glb"
    _write_glb(processed_path, doc, bytes(changed_binary))
    observation = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(processed_path.read_bytes()).hexdigest(),
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0, 0, 0, 1),
        source_glb_bytes=source_bytes,
    )
    if expected_error:
        with pytest.raises(DccFailedError, match=expected_error):
            _prove_processed_assembly_glb(
                processed_path,
                spec,
                profile,
                observation,
                source_bytes,
            )
    else:
        _prove_processed_assembly_glb(
            processed_path,
            spec,
            profile,
            observation,
            source_bytes,
        )


def test_outside_blender_proof_oracle_catches_missing_collider(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    ingest_result = _setup_ingested_package(tmp_path, spec)
    source_bytes = ingest_result.retained_glb_path.read_bytes()

    # GLB without collider
    missing_col_path = tmp_path / "no_collider.glb"
    missing_col_path.write_bytes(source_bytes)

    obs = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0, 0, 0, 1),
        source_glb_bytes=source_bytes,
    )

    with pytest.raises(DccFailedError, match="Required root box collider.*missing"):
        _prove_processed_assembly_glb(
            processed_glb_path=missing_col_path,
            spec=spec,
            profile=profile,
            verified_norm=obs,
            source_bytes=source_bytes,
        )


def test_outside_blender_proof_accepts_normal_split_box_collider(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    ingest_result = _setup_ingested_package(tmp_path, spec)
    source_bytes = ingest_result.retained_glb_path.read_bytes()
    doc, binary = _read_glb(source_bytes)
    doc, binary = _add_collider_box(doc, binary, spec)

    # Model a normal-split exporter: every triangle has its own three position rows.
    soup = _triangle_soup(doc, binary, f"COL_{spec.asset_id}")
    split_points = [point for triangle in soup for point in triangle]
    split_blob = b"".join(struct.pack("<fff", *point) for point in split_points)
    offset = len(binary)
    view_index = len(doc["bufferViews"])
    doc["bufferViews"].append(
        {"buffer": 0, "byteOffset": offset, "byteLength": len(split_blob), "target": 34962}
    )
    accessor_index = len(doc["accessors"])
    doc["accessors"].append(
        {
            "bufferView": view_index,
            "byteOffset": 0,
            "componentType": 5126,
            "count": len(split_points),
            "type": "VEC3",
        }
    )
    collider_mesh_index = doc["nodes"][-1]["mesh"]
    primitive = doc["meshes"][collider_mesh_index]["primitives"][0]
    primitive["attributes"]["POSITION"] = accessor_index
    primitive.pop("indices", None)
    doc["buffers"][0]["byteLength"] = len(binary) + len(split_blob)

    processed_path = tmp_path / "normal_split_box.glb"
    _write_glb(processed_path, doc, binary + split_blob)
    observation = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(processed_path.read_bytes()).hexdigest(),
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0, 0, 0, 1),
        source_glb_bytes=source_bytes,
    )
    _prove_processed_assembly_glb(
        processed_glb_path=processed_path,
        spec=spec,
        profile=profile,
        verified_norm=observation,
        source_bytes=source_bytes,
    )


def test_outside_blender_proof_bounds_processed_file_read(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    oversized_path = tmp_path / "oversized.glb"
    oversized_path.write_bytes(b"x" * 32)
    with pytest.raises(DccFailedError, match="maximum allowed size"):
        _prove_processed_assembly_glb(
            processed_glb_path=oversized_path,
            spec=spec,
            profile=profile,
            verified_norm=VerifiedSourceNormalization(
                source_sha256="0" * 64,
                processed_sha256="0" * 64,
                source_front="-Z",
                normalization_applied=False,
                root_rotation_xyzw=(0, 0, 0, 1),
                source_glb_bytes=b"",
            ),
            source_bytes=b"",
            max_file_size_bytes=16,
        )


def test_process_result_is_immutable_and_json_safe(tmp_path: Path) -> None:
    observation = VerifiedSourceNormalization(
        source_sha256="0" * 64,
        processed_sha256="1" * 64,
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0, 0, 0, 1),
        source_glb_bytes=b"private-retained-bytes",
    )
    report: dict[str, Any] = {"status": "SUCCESS", "nested": {"values": [1, 2]}}
    result = AssemblyProcessResult(
        status="SUCCESS",
        exit_code=0,
        duration_seconds=0.1,
        blender_version="test",
        script_sha256="2" * 64,
        source_glb_sha256="0" * 64,
        provenance_sha256="3" * 64,
        processed_glb_sha256="1" * 64,
        spec_fingerprint="4" * 64,
        source_front="-Z",
        verified_normalization=observation,
        processed_glb_path=tmp_path / "processed.glb",
        report_path=tmp_path / "report.json",
        report_data=report,
        part_metrics={"hull": {"lod0_triangles": 12}},
    )
    report["nested"]["values"].append(3)
    assert result.report_data["nested"]["values"] == (1, 2)
    with pytest.raises(TypeError):
        result.report_data["status"] = "FAILED"
    serialized = json.dumps(result.to_dict())
    assert "private-retained-bytes" not in serialized
    assert "source_glb_bytes" not in serialized


def test_outside_blender_proof_oracle_catches_wrong_root_node(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    ingest_result = _setup_ingested_package(tmp_path, spec)
    source_bytes = ingest_result.retained_glb_path.read_bytes()

    # Mutate root node name
    wrong_root_path = tmp_path / "wrong_root.glb"
    doc, binary = _read_glb(source_bytes)
    doc["nodes"][0]["name"] = "Armature"
    _write_glb(wrong_root_path, doc, binary)

    obs = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(wrong_root_path.read_bytes()).hexdigest(),
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0, 0, 0, 1),
        source_glb_bytes=source_bytes,
    )

    with pytest.raises(DccFailedError, match="must be an identity 'ROOT' node"):
        _prove_processed_assembly_glb(
            processed_glb_path=wrong_root_path,
            spec=spec,
            profile=profile,
            verified_norm=obs,
            source_bytes=source_bytes,
        )


def test_outside_blender_proof_oracle_catches_missing_socket(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    ingest_result = _setup_ingested_package(tmp_path, spec)
    source_bytes = ingest_result.retained_glb_path.read_bytes()

    # Remove socket node from scene
    no_socket_path = tmp_path / "no_socket.glb"
    doc, binary = _read_glb(source_bytes)
    # Add box collider to pass collider check
    doc["nodes"].append(
        {
            "name": f"COL_{spec.asset_id}",
            "mesh": 0,
        }
    )
    doc["nodes"][0]["children"].append(len(doc["nodes"]) - 1)
    # Remove socket 7 from PART_barrel children and rename
    doc["nodes"][5]["children"] = [6]
    doc["nodes"][7]["name"] = "OTHER_NODE"
    doc["nodes"][5]["children"].append(7)
    _write_glb(no_socket_path, doc, binary)

    obs = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(no_socket_path.read_bytes()).hexdigest(),
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0, 0, 0, 1),
        source_glb_bytes=source_bytes,
    )

    with pytest.raises(DccFailedError, match="Required socket.*missing"):
        _prove_processed_assembly_glb(
            processed_glb_path=no_socket_path,
            spec=spec,
            profile=profile,
            verified_norm=obs,
            source_bytes=source_bytes,
        )


def _add_collider_box(
    doc: dict[str, Any], binary: bytes, spec: AssetSpecificationV07
) -> tuple[dict[str, Any], bytes]:
    points = [
        (-0.5, -0.7, -0.6),
        (0.5, -0.7, -0.6),
        (0.5, -0.7, 0.6),
        (-0.5, -0.7, 0.6),
        (-0.5, 0.7, -0.6),
        (0.5, 0.7, -0.6),
        (0.5, 0.7, 0.6),
        (-0.5, 0.7, 0.6),
    ]
    pos_data = b"".join(struct.pack("<fff", *p) for p in points)
    offset = len(binary)
    view_idx = len(doc["bufferViews"])
    doc["bufferViews"].append(
        {"buffer": 0, "byteOffset": offset, "byteLength": len(pos_data), "target": 34962}
    )
    acc_idx = len(doc["accessors"])
    doc["accessors"].append(
        {
            "bufferView": view_idx,
            "byteOffset": 0,
            "componentType": 5126,
            "count": 8,
            "type": "VEC3",
            "min": [-0.5, -0.7, -0.6],
            "max": [0.5, 0.7, 0.6],
        }
    )
    bin_out = binary + pos_data
    bin_out += b"\0" * ((-len(bin_out)) % 4)
    doc["buffers"][0]["byteLength"] = len(bin_out)

    col_mesh_idx = len(doc["meshes"])
    doc["meshes"].append(
        {
            "name": f"COL_{spec.asset_id}",
            "primitives": [
                {
                    "attributes": {"POSITION": acc_idx},
                    "indices": 2,
                }
            ],
        }
    )
    col_node_idx = len(doc["nodes"])
    doc["nodes"].append(
        {
            "name": f"COL_{spec.asset_id}",
            "mesh": col_mesh_idx,
        }
    )
    doc["nodes"][0]["children"].append(col_node_idx)
    return doc, bin_out


def _add_lod1_meshes(
    doc: dict[str, Any],
    binary: bytes,
    spec: AssetSpecificationV07,
    *,
    mutate_hull: Literal["shift", "collapse"] | None = None,
) -> tuple[dict[str, Any], bytes]:
    output = bytearray(binary)
    node_by_name = {node.get("name"): (index, node) for index, node in enumerate(doc["nodes"])}
    for part in spec.parts or []:
        lod0_name = f"SM_{spec.asset_id}_{part.part_id}_LOD0"
        lod0_index, lod0_node = node_by_name[lod0_name]
        mesh = copy.deepcopy(doc["meshes"][lod0_node["mesh"]])
        mesh["name"] = f"SM_{spec.asset_id}_{part.part_id}_LOD1"
        primitive = mesh["primitives"][0]
        position_index = primitive["attributes"]["POSITION"]
        rows = [
            tuple(float(v) for v in row) for row in _accessor(doc, binary, position_index, "VEC3")
        ]
        if part.part_id == "hull" and mutate_hull:
            if mutate_hull == "shift":
                rows = [(row[0] + 0.2, row[1], row[2]) for row in rows]
            else:
                rows = tuple((row[0] * 0.5, row[1] * 0.5, row[2] * 0.5) for row in rows)
        position_bytes = b"".join(struct.pack("<fff", *row) for row in rows)
        offset = len(output)
        output.extend(b"\0" * ((-offset) % 4))
        offset = len(output)
        output.extend(position_bytes)
        view_index = len(doc["bufferViews"])
        doc["bufferViews"].append(
            {
                "buffer": 0,
                "byteOffset": offset,
                "byteLength": len(position_bytes),
                "target": 34962,
            }
        )
        accessor_index = len(doc["accessors"])
        doc["accessors"].append(
            {
                "bufferView": view_index,
                "byteOffset": 0,
                "componentType": 5126,
                "count": len(rows),
                "type": "VEC3",
                "min": [min(row[axis] for row in rows) for axis in range(3)],
                "max": [max(row[axis] for row in rows) for axis in range(3)],
            }
        )
        primitive["attributes"]["POSITION"] = accessor_index
        mesh_index = len(doc["meshes"])
        doc["meshes"].append(mesh)
        parent_index = next(
            parent
            for parent, node in enumerate(doc["nodes"])
            if lod0_index in node.get("children", [])
        )
        lod1_index = len(doc["nodes"])
        doc["nodes"].append({"name": mesh["name"], "mesh": mesh_index})
        doc["nodes"][parent_index]["children"].append(lod1_index)
    doc["buffers"][0]["byteLength"] = len(output)
    return doc, bytes(output)


@pytest.mark.parametrize("mutation", ["shift", "collapse"])
def test_outside_blender_proof_rejects_per_part_lod1_bounds_drift(
    tmp_path: Path, mutation: Literal["shift", "collapse"]
) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    object.__setattr__(spec, "lod_policy", "lod0_lod1")
    ingest_result = _setup_ingested_package(tmp_path, spec)
    source_bytes = ingest_result.retained_glb_path.read_bytes()
    document, binary = _read_glb(source_bytes)
    document, binary = _add_lod1_meshes(document, binary, spec, mutate_hull=mutation)
    document, binary = _add_collider_box(document, binary, spec)
    processed_path = tmp_path / f"lod1-{mutation}.glb"
    _write_glb(processed_path, document, binary)
    observation = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(processed_path.read_bytes()).hexdigest(),
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0, 0, 0, 1),
        source_glb_bytes=source_bytes,
    )

    with pytest.raises(DccFailedError, match="Per-part LOD1 bounds differ from LOD0 for 'hull'"):
        _prove_processed_assembly_glb(
            processed_path,
            spec,
            profile,
            observation,
            source_bytes,
        )


def test_outside_blender_proof_oracle_catches_budget_overflow(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    # Set tight budget
    object.__setattr__(spec.geometry_budget, "max_triangles_lod0", 5)

    ingest_result = _setup_ingested_package(tmp_path, spec)
    source_bytes = ingest_result.retained_glb_path.read_bytes()

    out_path = tmp_path / "budget_overflow.glb"
    doc, binary = _read_glb(source_bytes)
    doc, binary = _add_collider_box(doc, binary, spec)
    _write_glb(out_path, doc, binary)

    obs = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(out_path.read_bytes()).hexdigest(),
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0, 0, 0, 1),
        source_glb_bytes=source_bytes,
    )

    with pytest.raises(DccFailedError, match="exceeds budget"):
        _prove_processed_assembly_glb(
            processed_glb_path=out_path,
            spec=spec,
            profile=profile,
            verified_norm=obs,
            source_bytes=source_bytes,
        )


def test_outside_blender_proof_oracle_catches_double_norm_and_deeper_drift(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile, source_front="+Z", root_basis=(0, 1, 0, 0))
    ingest_result = _setup_ingested_package(tmp_path, spec, source_front="+Z")
    source_bytes = ingest_result.retained_glb_path.read_bytes()

    # 1. Double normalization: root part rotated by Ry(180) twice becomes identity
    double_norm_path = tmp_path / "double_norm.glb"
    doc, binary = _read_glb(source_bytes)
    doc, binary = _add_collider_box(doc, binary, spec)
    # Apply Ry(180) again to root part
    doc["nodes"][1]["rotation"] = [0, 0, 0, 1]
    _write_glb(double_norm_path, doc, binary)

    obs = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(double_norm_path.read_bytes()).hexdigest(),
        source_front="+Z",
        normalization_applied=True,
        root_rotation_xyzw=(0, 1, 0, 0),
        source_glb_bytes=source_bytes,
    )

    pres = verify_source_to_processed_preservation(obs, double_norm_path, spec)
    assert not pres.passed

    # 2. Deeper local drift: child turret transform drifted
    drift_path = tmp_path / "deeper_drift.glb"
    doc2, binary2 = _read_glb(source_bytes)
    doc2, binary2 = _add_collider_box(doc2, binary2, spec)
    # Root part correctly normalized to (0, 1, 0, 0)
    doc2["nodes"][1]["rotation"] = [0, 1, 0, 0]
    # But child turret drifted!
    doc2["nodes"][3]["translation"][1] += 0.1
    _write_glb(drift_path, doc2, binary2)

    obs2 = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(drift_path.read_bytes()).hexdigest(),
        source_front="+Z",
        normalization_applied=True,
        root_rotation_xyzw=(0, 1, 0, 0),
        source_glb_bytes=source_bytes,
    )
    pres2 = verify_source_to_processed_preservation(obs2, drift_path, spec)
    assert not pres2.passed


def test_outside_blender_proof_rejects_wrong_noncommuting_root_multiplication(
    tmp_path: Path,
) -> None:
    profile = _create_assembly_profile()

    def axis_quaternion(axis: int, degrees: float) -> tuple[float, float, float, float]:
        half = math.radians(degrees) / 2
        values = [0.0, 0.0, 0.0, math.cos(half)]
        values[axis] = math.sin(half)
        return tuple(values)  # type: ignore[return-value]

    def multiply(left: Sequence[float], right: Sequence[float]) -> tuple[float, ...]:
        x1, y1, z1, w1 = left
        x2, y2, z2, w2 = right
        return (
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        )

    canonical = multiply(
        multiply(axis_quaternion(0, 30), axis_quaternion(1, 20)),
        axis_quaternion(2, 10),
    )
    spec = _create_assembly_spec(profile, source_front="+Z", root_basis=canonical)
    ingest_result = _setup_ingested_package(tmp_path, spec, source_front="+Z")
    source_bytes = ingest_result.retained_glb_path.read_bytes()
    output_path = tmp_path / "wrong_multiplication_order.glb"
    document, binary = _read_glb(source_bytes)
    raw_root_rotation = tuple(document["nodes"][1]["rotation"])
    ry_180 = (0.0, 1.0, 0.0, 0.0)
    wrong_rotation = multiply(raw_root_rotation, ry_180)
    document["nodes"][1]["rotation"] = list(wrong_rotation)
    _write_glb(output_path, document, binary)

    observation = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(output_path.read_bytes()).hexdigest(),
        source_front="+Z",
        normalization_applied=True,
        root_rotation_xyzw=ry_180,
        source_glb_bytes=source_bytes,
    )
    result = verify_source_to_processed_preservation(observation, output_path, spec)
    assert not result.passed
    assert any("triangle" in finding.message.lower() for finding in result.findings)


def test_outside_blender_proof_oracle_catches_extras_loss(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)
    ingest_result = _setup_ingested_package(tmp_path, spec)
    source_bytes = ingest_result.retained_glb_path.read_bytes()

    out_path = tmp_path / "lost_extras.glb"
    doc, binary = _read_glb(source_bytes)
    doc, binary = _add_collider_box(doc, binary, spec)
    # Strip gf_motion extras from turret
    doc["nodes"][3].pop("extras", None)
    _write_glb(out_path, doc, binary)

    obs = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(out_path.read_bytes()).hexdigest(),
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0, 0, 0, 1),
        source_glb_bytes=source_bytes,
    )

    pres = verify_source_to_processed_preservation(obs, out_path, spec)
    assert not pres.passed


def test_outside_blender_proof_oracle_catches_dimension_and_origin_mismatch(tmp_path: Path) -> None:
    profile = _create_assembly_profile()
    spec = _create_assembly_spec(profile)

    ingest_result = _setup_ingested_package(tmp_path, spec)
    source_bytes = ingest_result.retained_glb_path.read_bytes()

    out_path = tmp_path / "dim_mismatch.glb"
    doc, binary = _read_glb(source_bytes)
    doc, binary = _add_collider_box(doc, binary, spec)
    _write_glb(out_path, doc, binary)

    obs = VerifiedSourceNormalization(
        source_sha256=ingest_result.retained_glb_sha256,
        processed_sha256=hashlib.sha256(out_path.read_bytes()).hexdigest(),
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0, 0, 0, 1),
        source_glb_bytes=source_bytes,
    )

    # 1. Dimension mismatch
    object.__setattr__(spec.dimensions, "width_m", 5.0)
    with pytest.raises(DccFailedError, match="dimensions.*do not match specification"):
        _prove_processed_assembly_glb(
            processed_glb_path=out_path,
            spec=spec,
            profile=profile,
            verified_norm=obs,
            source_bytes=source_bytes,
        )

    # 2. Origin mismatch
    object.__setattr__(spec.dimensions, "width_m", 1.0)
    object.__setattr__(spec, "origin_policy", "bottom_center")
    with pytest.raises(DccFailedError, match="origin does not match origin policy"):
        _prove_processed_assembly_glb(
            processed_glb_path=out_path,
            spec=spec,
            profile=profile,
            verified_norm=obs,
            source_bytes=source_bytes,
        )
