"""Character-processor unit tests for typed boundaries, preflight, and report trust."""

from __future__ import annotations

import hashlib
import json
import runpy
import struct
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.assets import glb_validator
from gamefactory.adapters.dcc.character_processor import (
    _MAX_JSON_BYTES,
    CharacterProcessor,
    _bounded_json_bytes,
    _raw_facts,
    _read_json_object,
)
from gamefactory.adapters.fakes.glb_generator import create_box_glb
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
from gamefactory.core.execution.process_runner import CommandResult


def _profile(profile_id: str = "test_character_profile") -> AssetProfileV07:
    base = builtin_registry().get("static_prop").document.model_dump(mode="json")
    data = {
        "schema_version": "asset-profile-0.7.0",
        "profile_id": profile_id,
        "version": 1,
        "categories": ["character"],
        "review_views": ["front"],
        "framing": base["framing"],
        "dimension_rules": {"min_m": 0.1, "max_m": 10.0},
        "processing": {
            "lod0_required": True,
            "lod1_required": True,
            "allowed_lod_policies": ["lod0_lod1"],
            "allowed_collider_policies": ["capsule"],
            "allowed_origin_policies": ["bottom_center", "center"],
            "default_origin_policy": "bottom_center",
            "dimension_tolerance_m": 0.02,
            "snap_grid_m": None,
            "rig_forbidden": True,
            "animation_forbidden": True,
            "max_materials": 8,
            "max_texture_dimension": 4096,
            "max_triangles_lod0": 20000,
        },
        "godot": base["godot"],
        "runtime": base["runtime"],
        "geometry_mode": "single_mesh",
        "accepted_source_kinds": ["provider_generated"],
        "assembly": None,
    }
    return AssetProfileV07(parse_profile_document_v07(data))


def _spec(
    profile: AssetProfileV07,
    *,
    width: float = 0.6,
    height: float = 1.8,
    depth: float = 0.4,
    radius: float = 0.3,
    capsule_height: float = 1.7,
) -> AssetSpecificationV07:
    data: dict[str, Any] = {
        "schema_version": "0.7.0",
        "asset_id": "test_character",
        "category": "character",
        "profile": profile.profile_id,
        "profile_version": profile.version,
        "intent": "static character geometry processor fixture",
        "source_kind": "provider_generated",
        "dimensions": {"width_m": width, "height_m": height, "depth_m": depth},
        "orientation": {"up": "+Y", "front": "-Z"},
        "origin_policy": "bottom_center",
        "lod_policy": "lod0_lod1",
        "geometry_budget": {
            "max_triangles_lod0": 20000,
            "max_triangles_lod1": 10000,
            "lod_ratio": 0.5,
        },
        "material_budget": {"max_materials": 8},
        "texture_budget": {"max_dimension": 4096},
        "collider": {
            "policy": "capsule",
            "capsule": {"radius_m": radius, "height_m": capsule_height},
        },
    }
    registry = ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))
    return parse_asset_specification_v07(data, registry=registry)


def _read_glb(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    json_size = struct.unpack_from("<I", raw, 12)[0]
    document = json.loads(raw[20 : 20 + json_size].decode("utf-8").rstrip())
    return document, raw[20 + json_size + 8 :]


def _write_glb(path: Path, document: dict[str, Any], binary: bytes) -> None:
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    binary += b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def _append_attribute(
    document: dict[str, Any],
    binary: bytes,
    *,
    semantic: str,
    values: bytes,
    count: int,
    accessor_type: str,
    component_type: int,
    normalized: bool,
) -> bytes:
    binary += b"\0" * ((-len(binary)) % 4)
    view_index = len(document["bufferViews"])
    document["bufferViews"].append(
        {"buffer": 0, "byteOffset": len(binary), "byteLength": len(values)}
    )
    accessor_index = len(document["accessors"])
    document["accessors"].append(
        {
            "bufferView": view_index,
            "componentType": component_type,
            "count": count,
            "type": accessor_type,
            "normalized": normalized,
        }
    )
    document["meshes"][0]["primitives"][0]["attributes"][semantic] = accessor_index
    document["buffers"][0]["byteLength"] = len(binary) + len(values)
    return binary + values


def _source(path: Path) -> None:
    create_box_glb(
        width_m=0.6,
        depth_m=0.4,
        height_m=1.8,
        mesh_name="source_humanoid_block",
        origin="center",
        include_collider=False,
        output_path=path,
    )
    document, binary = _read_glb(path)
    mesh_id = document["nodes"][0]["mesh"]
    document["nodes"] = [
        {
            "name": "source_body",
            "mesh": mesh_id,
            "translation": [0.0, 0.45, 0.0],
            "scale": [1.0, 0.5, 1.0],
        },
        {
            "name": "source_head",
            "mesh": mesh_id,
            "translation": [0.0, 1.35, 0.0],
            "scale": [0.5, 0.5, 0.75],
        },
    ]
    document["scenes"] = [{"nodes": [0, 1]}]
    document["scene"] = 0
    _write_glb(path, document, binary)


class _NoCallRunner:
    calls = 0

    def run(self, request: Any) -> CommandResult:
        self.calls += 1
        raise AssertionError(f"Blender must not run for rejected source: {request.args}")


class _ForgedReportRunner:
    def run(self, request: Any) -> CommandResult:
        args = request.args
        output = Path(args[args.index("--output-glb") + 1])
        report = Path(args[args.index("--report-path") + 1])
        contract = json.loads(Path(args[args.index("--contract") + 1]).read_text(encoding="utf-8"))
        output.write_bytes(b"not a glb")
        script_path = Path(args[args.index("--python") + 1])
        report.write_text(
            json.dumps(
                {
                    "status": "SUCCESS",
                    "exit_code": 0,
                    "attempt_id": contract["attempt_id"],
                    "asset_id": contract["asset_id"],
                    "source_sha256": contract["source_sha256"],
                    "output_sha256": "0" * 64,
                    "spec_sha256": contract["spec_sha256"],
                    "profile_id": contract["profile_id"],
                    "profile_version": contract["profile_version"],
                    "profile_sha256": contract["profile_sha256"],
                    "script_sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
                    "blender_version": "Blender 5.2.1",
                }
            ),
            encoding="utf-8",
        )
        return CommandResult(exit_code=0, stdout="", stderr="", duration_seconds=0.01)


class _ReplacingContractRunner:
    def run(self, request: Any) -> CommandResult:
        contract = Path(request.args[request.args.index("--contract") + 1])
        contract.unlink()
        contract.write_text('{"owner":"foreign"}', encoding="utf-8")
        return CommandResult(exit_code=9, stdout="", stderr="controlled failure")


def _processor(runner: Any) -> CharacterProcessor:
    return CharacterProcessor(
        blender_executable="blender-test-double",
        runner=runner,
        dependency_preflight=False,
    )


@pytest.mark.parametrize(
    "forbidden", ["skins", "animations", "extensionsUsed", "joints", "weights"]
)
def test_rigged_or_extended_source_is_rejected_before_blender(
    tmp_path: Path, forbidden: str
) -> None:
    profile = _profile()
    spec = _spec(profile)
    source = tmp_path / "forbidden.glb"
    _source(source)
    document, binary = _read_glb(source)
    if forbidden in {"skins", "animations", "extensionsUsed"}:
        document[forbidden] = [{}] if forbidden != "extensionsUsed" else ["VENDOR_unsupported"]
    elif forbidden == "joints":
        document["meshes"][0]["primitives"][0]["attributes"]["JOINTS_0"] = 0
    else:
        document["meshes"][0]["primitives"][0]["attributes"]["WEIGHTS_0"] = 0
    _write_glb(source, document, binary)
    runner = _NoCallRunner()
    with pytest.raises(ValidationError, match="preflight"):
        _processor(runner).process_character(
            source,
            spec,
            profile,
            expected_raw_glb_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            processed_glb_path=tmp_path / "output.glb",
        )
    assert runner.calls == 0


def test_repeated_shared_mesh_positions_are_bounded_before_decode_or_blender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "expanded.glb"
    _source(source)
    document, binary = _read_glb(source)
    position_index = document["meshes"][0]["primitives"][0]["attributes"]["POSITION"]
    # Counts alone exceed the aggregate budget. The embedded bytes intentionally
    # remain small: the guard must run before accessor bounds checks or decoding.
    document["accessors"][position_index]["count"] = 600_000
    document["meshes"][0]["primitives"][0]["attributes"] = {"POSITION": position_index}
    _write_glb(source, document, binary)

    decoded: list[int] = []
    original = glb_validator._accessor

    def counted(document, binary, index, expected_type=None):
        decoded.append(index)
        return original(document, binary, index, expected_type)

    monkeypatch.setattr(glb_validator, "_accessor", counted)
    runner = _NoCallRunner()
    profile = _profile()
    with pytest.raises(ValidationError, match="expanded POSITION elements"):
        _processor(runner).process_character(
            source,
            _spec(profile),
            profile,
            expected_raw_glb_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            processed_glb_path=tmp_path / "output.glb",
        )
    assert decoded == []
    assert runner.calls == 0


def test_shared_mesh_instance_budget_allows_valid_source_and_bounds_triangle_soup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gamefactory.adapters.dcc import character_processor

    source = tmp_path / "shared.glb"
    _source(source)
    document, binary = _read_glb(source)
    position_index = document["meshes"][0]["primitives"][0]["attributes"]["POSITION"]
    count = document["accessors"][position_index]["count"]
    # The two node instances reference the same accessor, so the expanded work
    # is twice its accessor count; valid source remains below the same cap.
    inspected = glb_validator._inspect(document, binary, max_expanded_position_elements=2 * count)
    assert inspected[4] > 0

    decoded: list[int] = []
    original = glb_validator._accessor

    def counted(document, binary, index, expected_type=None):
        decoded.append(index)
        return original(document, binary, index, expected_type)

    monkeypatch.setattr(character_processor, "_accessor", counted)
    matrices = {
        index: [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
        for index in range(len(document["nodes"]))
    }
    with pytest.raises(ValueError, match="expanded POSITION elements"):
        character_processor._triangle_soup(
            document,
            binary,
            matrices,
            max_expanded_position_elements=2 * count - 1,
        )
    assert decoded == []

    soup = character_processor._triangle_soup(
        document,
        binary,
        matrices,
        max_expanded_position_elements=2 * count,
    )
    assert soup
    assert decoded.count(position_index) == 2


def test_character_preflight_accepts_normalized_uv1_and_color_streams(tmp_path: Path) -> None:
    from gamefactory.adapters.dcc import character_processor

    source = tmp_path / "attributes.glb"
    _source(source)
    document, binary = _read_glb(source)
    position = document["accessors"][
        document["meshes"][0]["primitives"][0]["attributes"]["POSITION"]
    ]
    count = position["count"]
    binary = _append_attribute(
        document,
        binary,
        semantic="TEXCOORD_1",
        values=bytes([0, 255] * count),
        count=count,
        accessor_type="VEC2",
        component_type=5121,
        normalized=True,
    )
    binary = _append_attribute(
        document,
        binary,
        semantic="COLOR_0",
        values=struct.pack("<" + "H" * (4 * count), *([65535] * (4 * count))),
        count=count,
        accessor_type="VEC4",
        component_type=5123,
        normalized=True,
    )
    _write_glb(source, document, binary)
    accessors = character_processor._validate_character_vertex_attributes(document)
    assert len(accessors) == 3
    profile = _profile()
    facts = _raw_facts(source.read_bytes(), _spec(profile), profile)
    assert facts[1]["triangles"] > 0


def test_character_attribute_aliases_decode_once_and_overlapping_descriptors_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gamefactory.adapters.dcc import character_processor

    source = tmp_path / "attribute-budget.glb"
    _source(source)
    document, binary = _read_glb(source)
    count = document["accessors"][document["meshes"][0]["primitives"][0]["attributes"]["POSITION"]][
        "count"
    ]
    binary = _append_attribute(
        document,
        binary,
        semantic="TEXCOORD_1",
        values=bytes([0, 255] * count),
        count=count,
        accessor_type="VEC2",
        component_type=5121,
        normalized=True,
    )
    uv_index = document["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_1"]
    document["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_2"] = uv_index

    decoded: list[int] = []
    original = character_processor._accessor

    def counted(document, binary, index, expected_type=None):
        decoded.append(index)
        return original(document, binary, index, expected_type)

    monkeypatch.setattr(character_processor, "_accessor", counted)
    validated = character_processor._validate_character_vertex_attributes(document)
    assert validated.count(uv_index) == 1
    character_processor._decode_character_vertex_attributes(document, binary, validated)
    assert decoded.count(uv_index) == 1
    assert len(decoded) == len(validated)

    # Distinct descriptor indices over the same bytes each consume the budget.
    document["accessors"].append(dict(document["accessors"][uv_index]))
    second_index = len(document["accessors"]) - 1
    document["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_3"] = second_index
    decoded.clear()
    with pytest.raises(ValueError, match="attribute scalar values"):
        character_processor._validate_character_vertex_attributes(
            document, max_scalar_values=4 * count - 1
        )
    assert decoded == []


@pytest.mark.parametrize("mutation", ["count", "bounds", "normalization", "custom"])
def test_character_preflight_rejects_malformed_unused_attribute_before_blender(
    tmp_path: Path, mutation: str
) -> None:
    source = tmp_path / f"bad-attribute-{mutation}.glb"
    _source(source)
    document, binary = _read_glb(source)
    position = document["accessors"][
        document["meshes"][0]["primitives"][0]["attributes"]["POSITION"]
    ]
    count = position["count"]
    binary = _append_attribute(
        document,
        binary,
        semantic="TEXCOORD_1",
        values=bytes([0, 255] * count),
        count=count,
        accessor_type="VEC2",
        component_type=5121,
        normalized=True,
    )
    accessor_index = document["meshes"][0]["primitives"][0]["attributes"]["TEXCOORD_1"]
    accessor = document["accessors"][accessor_index]
    if mutation == "count":
        accessor["count"] -= 1
    elif mutation == "bounds":
        document["bufferViews"][accessor["bufferView"]]["byteLength"] = 1
    elif mutation == "normalization":
        accessor["normalized"] = False
    else:
        document["meshes"][0]["primitives"][0]["attributes"]["CUSTOM_0"] = accessor_index
    _write_glb(source, document, binary)

    profile = _profile()
    runner = _NoCallRunner()
    with pytest.raises(ValidationError, match="preflight"):
        _processor(runner).process_character(
            source,
            _spec(profile),
            profile,
            expected_raw_glb_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            processed_glb_path=tmp_path / "output.glb",
        )
    assert runner.calls == 0


def test_bad_hash_unsafe_or_oversized_source_fails_before_blender(tmp_path: Path) -> None:
    profile = _profile()
    spec = _spec(profile)
    runner = _NoCallRunner()
    source = tmp_path / "source.glb"
    source.write_bytes(b"broken")
    with pytest.raises(DccFailedError, match="pinned SHA"):
        _processor(runner).process_character(
            source,
            spec,
            profile,
            expected_raw_glb_sha256="a" * 64,
            processed_glb_path=tmp_path / "output.glb",
        )
    with source.open("wb") as stream:
        stream.truncate(50 * 1024 * 1024 + 1)
    with pytest.raises(DccFailedError, match="52428800-byte limit"):
        _processor(runner).process_character(
            source,
            spec,
            profile,
            expected_raw_glb_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            processed_glb_path=tmp_path / "output.glb",
        )
    assert runner.calls == 0


@pytest.mark.parametrize(
    "mutation, expected",
    [
        ("source_kind", "provider_generated"),
        ("parts", "parts"),
        ("sockets", "sockets"),
    ],
)
def test_character_processor_rejects_wrong_typed_scope(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    profile = _profile()
    spec = _spec(profile)
    if mutation == "source_kind":
        object.__setattr__(spec, "source_kind", "local_operator_assembly")
    elif mutation == "parts":
        object.__setattr__(spec, "parts", [])
    else:
        object.__setattr__(spec, "sockets", [])
    source = tmp_path / "scope.glb"
    _source(source)
    runner = _NoCallRunner()
    with pytest.raises(ValidationError):
        _processor(runner).process_character(
            source,
            spec,
            profile,
            expected_raw_glb_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            processed_glb_path=tmp_path / "output.glb",
        )
    assert runner.calls == 0


def test_character_processor_rejects_a_different_typed_profile(tmp_path: Path) -> None:
    profile = _profile()
    other_profile = _profile("different_character_profile")
    spec = _spec(profile)
    source = tmp_path / "profile.glb"
    _source(source)
    runner = _NoCallRunner()
    with pytest.raises(ValidationError, match="bound"):
        _processor(runner).process_character(
            source,
            spec,
            other_profile,
            expected_raw_glb_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            processed_glb_path=tmp_path / "profile-output.glb",
        )
    assert runner.calls == 0


def test_nonuniform_fit_capsule_and_existing_output_rejected_before_blender(tmp_path: Path) -> None:
    profile = _profile()
    source = tmp_path / "fit.glb"
    _source(source)
    raw_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    runner = _NoCallRunner()
    with pytest.raises(ValidationError, match="uniform height scaling"):
        _processor(runner).process_character(
            source,
            _spec(profile, width=0.9),
            profile,
            expected_raw_glb_sha256=raw_hash,
            processed_glb_path=tmp_path / "fit.glb.out.glb",
        )
    with pytest.raises(ValidationError):
        _processor(runner).process_character(
            source,
            _spec(profile, radius=0.5),
            profile,
            expected_raw_glb_sha256=raw_hash,
            processed_glb_path=tmp_path / "capsule.glb",
        )
    existing = tmp_path / "existing.glb"
    existing.write_bytes(b"foreign")
    with pytest.raises(ValidationError, match="overwrite"):
        _processor(runner).process_character(
            source,
            _spec(profile),
            profile,
            expected_raw_glb_sha256=raw_hash,
            processed_glb_path=existing,
        )
    assert existing.read_bytes() == b"foreign"
    assert runner.calls == 0


def test_forged_blender_report_is_not_accepted(tmp_path: Path) -> None:
    profile = _profile()
    spec = _spec(profile)
    source = tmp_path / "forged.glb"
    _source(source)
    runner = _ForgedReportRunner()
    with pytest.raises(DccFailedError, match="stale or forged"):
        _processor(runner).process_character(
            source,
            spec,
            profile,
            expected_raw_glb_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            processed_glb_path=tmp_path / "forged_output.glb",
        )


def test_foreign_contract_replacement_survives_failed_blender_attempt(tmp_path: Path) -> None:
    profile = _profile()
    spec = _spec(profile)
    source = tmp_path / "contract-owner.glb"
    _source(source)
    with pytest.raises(DccFailedError, match="failed"):
        _processor(_ReplacingContractRunner()).process_character(
            source,
            spec,
            profile,
            expected_raw_glb_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            processed_glb_path=tmp_path / "retry.glb",
        )
    contracts = list(tmp_path.glob(".*.contract.json"))
    assert len(contracts) == 1
    assert json.loads(contracts[0].read_text(encoding="utf-8")) == {"owner": "foreign"}


def test_standalone_contract_reader_and_report_writer_are_bounded(tmp_path: Path) -> None:
    script = runpy.run_path(str(CharacterProcessor.get_script_path()))
    read_contract = script["_read_contract"]
    write_report = script["_write_report"]
    contract_path = tmp_path / "contract.json"
    contract_path.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError, match="JSON object"):
        read_contract(contract_path)
    contract_path.write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid JSON"):
        read_contract(contract_path)
    contract_path.write_bytes(b" " * (_MAX_JSON_BYTES + 1))
    with pytest.raises(RuntimeError, match="exceeds"):
        read_contract(contract_path)

    report_path = tmp_path / "oversized-report.json"
    with pytest.raises(RuntimeError, match="exceeds"):
        write_report(report_path, {"claim": "x" * (_MAX_JSON_BYTES + 1)})
    assert not report_path.exists()


def test_contract_serializer_bounds_before_file_creation(tmp_path: Path) -> None:
    contract_path = tmp_path / "oversized.contract.json"
    with pytest.raises(ValidationError, match="exceeds"):
        payload = _bounded_json_bytes({"claim": "x" * (_MAX_JSON_BYTES + 1)}, "character contract")
        contract_path.write_bytes(payload)
    assert not contract_path.exists()


def test_adapter_report_reader_is_bounded_and_requires_json_object(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text("[]", encoding="utf-8")
    with pytest.raises(DccFailedError, match="JSON object"):
        _read_json_object(report, "report")
    report.write_bytes(b" " * (_MAX_JSON_BYTES + 1))
    with pytest.raises(DccFailedError, match="exceeds"):
        _read_json_object(report, "report")


@pytest.mark.parametrize("payload", [b'{"x":1,"x":2}', b'{"x":1e999}'])
def test_adapter_report_reader_rejects_duplicate_keys_and_overflow_numbers(
    tmp_path: Path, payload: bytes
) -> None:
    report = tmp_path / "ambiguous-report.json"
    report.write_bytes(payload)
    with pytest.raises(DccFailedError, match="valid UTF-8 JSON|non-finite"):
        _read_json_object(report, "report")
