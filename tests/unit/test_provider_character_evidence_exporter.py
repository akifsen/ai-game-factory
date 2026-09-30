from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest

import gamefactory.workflows.provider_character_evidence as provider_evidence
import scripts.verify_asset_bundle as cold_verifier
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.workflows.provider_character_evidence import (
    ProviderCharacterEvidenceFile,
    _read,
    export_current_provider_character_evidence,
    export_provider_character_evidence,
)
from scripts.verify_asset_bundle import (
    _v07_bound_mesh_vertex_work,
    _v07_glb,
    _v07_positions,
    _v07_tris,
)


class _NoSelector:
    pass


class _Selector:
    def __init__(self, value: Any) -> None:
        self.value = value

    def current_provider_character_evidence_inputs(
        self, workflow: Any, task: Any, execution: Any
    ) -> Any:
        return self.value


def _binding() -> dict[str, Any]:
    digest = "a" * 64
    return {
        "workflow_id": "fixture-workflow",
        "revision": 1,
        "source_version": 1,
        "asset_id": "fixture_character",
        "spec_sha256": digest,
        "profile_id": "character_test",
        "profile_version": 1,
        "profile_document_sha256": digest,
        "review_views": ["front", "rear", "left", "right", "three_quarter"],
        "concept_sha256": digest,
        "paid_request_snapshot_sha256": digest,
        "production_readiness_report_sha256": digest,
        "provider_operation_sha256": digest,
        "cost_record_sha256": digest,
        "cost_ledger_row_count": 1,
        "cost_ledger_slice_sha256": digest,
        "raw_glb_sha256": digest,
        "processed_glb_sha256": digest,
        "processing_execution_id": "process-1",
        "processing_attempt_number": 1,
        "processing_report_sha256": digest,
        "processing_script_sha256": digest,
        "validation_execution_id": "validate-1",
        "validation_attempt_number": 1,
        "validation_sha256": digest,
        "runtime_execution_id": "godot-1",
        "runtime_attempt_number": 1,
        "runtime_request_digest": digest,
        "runtime_observation_sha256": digest,
        "runtime_harness_sha256": digest,
        "capture_sha256": dict.fromkeys(
            ("front", "rear", "left", "right", "three_quarter"), digest
        ),
    }


def _receipt(kind: str) -> dict[str, Any]:
    task_id = "fixture-task-" + kind
    inputs: dict[str, Any] = {}
    digest = compute_operation_hash(task_id, kind, inputs)
    return {
        "workflow_id": "fixture-workflow",
        "revision": 1,
        "task_id": task_id,
        "approval_type": kind,
        "status": "APPROVED",
        "inputs": inputs,
        "operation_hash": digest,
        "fingerprint": digest,
    }


def test_export_requires_authoritative_live_selector(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not expose current evidence selection"):
        export_current_provider_character_evidence(
            _NoSelector(), object(), object(), object(), tmp_path / "evidence"
        )


def test_export_rejects_unstructured_handler_selection(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="snapshot is malformed"):
        export_current_provider_character_evidence(
            _Selector(["not", "a", "snapshot"]), object(), object(), object(), tmp_path / "evidence"
        )


def test_export_refuses_incomplete_snapshot_before_creating_destination(tmp_path: Path) -> None:
    target = tmp_path / "new-bundle"
    with pytest.raises(ValidationError, match="fields are incomplete"):
        export_current_provider_character_evidence(
            _Selector({"files": []}), object(), object(), object(), target
        )
    assert not target.exists()


def test_export_rejects_stale_input_pin_before_creating_destination(tmp_path: Path) -> None:
    binding = _binding()
    binding["concept_sha256"] = "b" * 64
    target = tmp_path / "new-bundle"
    with pytest.raises(ArtifactError, match="hash"):
        export_provider_character_evidence(
            target,
            files=[
                ProviderCharacterEvidenceFile("concept", data=b"concept", expected_sha256="a" * 64)
            ],
            binding=binding,
            concept_review_receipt=_receipt("concept_review"),
            paid_review_receipt=_receipt("paid_generation"),
            final_review_receipt=_receipt("final_visual_review"),
            attempt_history=[],
        )
    assert not target.exists()


def test_export_rejects_duplicate_role_view_pairs(tmp_path: Path) -> None:
    binding = _binding()
    with pytest.raises(ArtifactError, match="duplicated"):
        export_provider_character_evidence(
            tmp_path / "new-bundle",
            files=[
                ProviderCharacterEvidenceFile("concept", data=b"one"),
                ProviderCharacterEvidenceFile("concept", data=b"two"),
            ],
            binding=binding,
            concept_review_receipt=_receipt("concept_review"),
            paid_review_receipt=_receipt("paid_generation"),
            final_review_receipt=_receipt("final_visual_review"),
            attempt_history=[],
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda receipt: receipt.update(workflow_id="other-workflow"), "stale"),
        (lambda receipt: receipt.update(revision=2), "stale"),
        (lambda receipt: receipt.update(fingerprint="0" * 64), "operation hash"),
    ],
)
def test_export_rejects_stale_or_tampered_approval_receipt(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    paid_receipt = _receipt("paid_generation")
    mutation(paid_receipt)
    target = tmp_path / "new-bundle"
    with pytest.raises(ArtifactError, match=message):
        export_provider_character_evidence(
            target,
            files=[],
            binding=_binding(),
            concept_review_receipt=_receipt("concept_review"),
            paid_review_receipt=paid_receipt,
            final_review_receipt=_receipt("final_visual_review"),
            attempt_history={},
        )
    assert not target.exists()


def test_export_rejects_malformed_capture_digest_map(
    tmp_path: Path,
) -> None:
    binding = _binding()
    binding["capture_sha256"].pop("front")
    target = tmp_path / "new-bundle"
    with pytest.raises(ArtifactError, match="capture digest map is malformed"):
        export_provider_character_evidence(
            target,
            files=[
                ProviderCharacterEvidenceFile("concept", data=b"concept"),
                ProviderCharacterEvidenceFile("runtime_capture", view="front", data=b"front"),
            ],
            binding=binding,
            concept_review_receipt=_receipt("concept_review"),
            paid_review_receipt=_receipt("paid_generation"),
            final_review_receipt=_receipt("final_visual_review"),
            attempt_history={},
        )
    assert not target.exists()


def test_role_specific_json_limit_is_checked_before_reading_file(tmp_path: Path) -> None:
    source = tmp_path / "oversized.json"
    source.write_bytes(b"x" * 1_000_001)
    with pytest.raises(ArtifactError, match="bounded regular file"):
        _read(ProviderCharacterEvidenceFile("specification", path=source))


def test_file_input_must_match_pinned_size_and_digest(tmp_path: Path) -> None:
    source = tmp_path / "pinned.json"
    source.write_bytes(b'{"ok":true}')
    item = ProviderCharacterEvidenceFile(
        "specification", path=source, expected_size=11, expected_sha256="0" * 64
    )
    with pytest.raises(ArtifactError, match="hash differs"):
        _read(item)


def test_export_removes_exclusively_created_partial_stage_file_on_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding = _binding()
    views = binding["review_views"]
    default_bytes = b"{}\n"
    payload_by_role = dict.fromkeys(
        provider_evidence._ROLES - provider_evidence._GENERATED - {"runtime_capture"}, default_bytes
    )
    payload_by_role["concept"] = b"concept-bytes"
    payload_by_role["provider_generated_glb"] = b"raw-glb-bytes"
    payload_by_role["processed_glb"] = b"processed-glb-bytes"
    request_digest = hashlib.sha256(b"{}").hexdigest()
    payload_by_role["runtime_request"] = json.dumps(
        {"request_digest": request_digest}, separators=(",", ":")
    ).encode()
    binding["runtime_request_digest"] = request_digest
    binding["raw_glb_sha256"] = hashlib.sha256(
        payload_by_role["provider_generated_glb"]
    ).hexdigest()
    binding["processed_glb_sha256"] = hashlib.sha256(payload_by_role["processed_glb"]).hexdigest()
    binding["concept_sha256"] = hashlib.sha256(payload_by_role["concept"]).hexdigest()
    binding["processing_report_sha256"] = hashlib.sha256(
        payload_by_role["processing_report"]
    ).hexdigest()
    binding["processing_script_sha256"] = hashlib.sha256(
        payload_by_role["processing_script"]
    ).hexdigest()
    binding["validation_sha256"] = hashlib.sha256(payload_by_role["validation"]).hexdigest()
    binding["runtime_observation_sha256"] = hashlib.sha256(
        payload_by_role["runtime_observation"]
    ).hexdigest()
    binding["runtime_harness_sha256"] = hashlib.sha256(
        payload_by_role["runtime_harness"]
    ).hexdigest()
    captures = {view: hashlib.sha256(f"capture-{view}".encode()).hexdigest() for view in views}
    binding["capture_sha256"] = captures
    files = [
        ProviderCharacterEvidenceFile(role, data=payload)
        for role, payload in payload_by_role.items()
    ]
    files.extend(
        ProviderCharacterEvidenceFile("runtime_capture", view=view, data=f"capture-{view}".encode())
        for view in views
    )
    history = {
        "process": [{"id": "process-1", "attempt_number": 1, "status": "COMPLETED"}],
        "validate": [{"id": "validate-1", "attempt_number": 1, "status": "COMPLETED"}],
        "godot": [{"id": "godot-1", "attempt_number": 1, "status": "COMPLETED"}],
    }

    def fail_fsync(_fd: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(provider_evidence.os, "fsync", fail_fsync)
    destination = tmp_path / "bundle"
    with pytest.raises(OSError, match="injected fsync failure"):
        export_provider_character_evidence(
            destination,
            files=files,
            binding=binding,
            concept_review_receipt=_receipt("concept_review"),
            paid_review_receipt=_receipt("paid_generation"),
            final_review_receipt=_receipt("final_visual_review"),
            attempt_history=history,
        )
    assert not destination.exists()
    assert list(tmp_path.glob(".bundle.staging-*")) == []


def test_provider_raw_node_indexing_cannot_collide_with_authored_names(tmp_path: Path) -> None:
    source = tmp_path / "raw.glb"
    create_box_glb(
        width_m=0.6,
        depth_m=0.5,
        height_m=1.8,
        mesh_name="provider-mesh",
        include_lod1=False,
        include_collider=False,
        output_path=source,
    )
    raw = source.read_bytes()
    json_length = struct.unpack_from("<I", raw, 12)[0]
    document = json.loads(raw[20 : 20 + json_length].decode("utf-8").rstrip())
    binary = raw[20 + json_length + 8 :]
    mesh_index = document["nodes"][0]["mesh"]
    document["nodes"] = [
        {"mesh": mesh_index},
        {"name": "__provider_raw_node_0", "mesh": mesh_index},
    ]
    document["scenes"] = [{"nodes": [0, 1]}]
    document["scene"] = 0
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary)
    encoded = (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )
    _parsed, _blob, nodes, _parents, _world = _v07_glb(encoded, allow_unnamed_duplicate_nodes=True)
    assert nodes == {"__provider_raw_node_0": 0, "__provider_raw_node_1": 1}


def test_provider_glb_aggregate_vertex_work_is_bounded_before_decode() -> None:
    document = {
        "nodes": [{"mesh": 0}, {"mesh": 1}],
        "meshes": [
            {"primitives": [{"attributes": {"POSITION": 0}}]},
            {"primitives": [{"attributes": {"POSITION": 0}}]},
        ],
        "accessors": [{"count": 600_000}],
    }
    with pytest.raises(ValueError, match="aggregate decoded vertex work"):
        _v07_bound_mesh_vertex_work(document, [0, 1])


def test_provider_glb_attribute_components_are_bounded_before_decode() -> None:
    accessors = [{"count": 1_000_000, "type": "VEC3"}]
    attributes = {"POSITION": 0}
    for index in range(1, 7):
        accessors.append({"count": 1_000_000, "type": "VEC3"})
        attributes[f"TEXCOORD_{index}"] = index
    document = {
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": attributes}]}],
        "accessors": accessors,
    }
    with pytest.raises(ValueError, match="aggregate decoded attribute work"):
        _v07_bound_mesh_vertex_work(document, [0])


def test_provider_glb_negative_attribute_count_cannot_cancel_attribute_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attributes: dict[str, int] = {"POSITION": 0}
    accessors = [{"count": 1_000_000, "type": "VEC3"}]
    for index in range(1, 6):
        attributes[f"COLOR_{index - 1}"] = index
        accessors.append({"count": 1_000_000, "type": "VEC4"})
    attributes["COLOR_5"] = len(accessors)
    accessors.append({"count": -1_000_000_000, "type": "VEC4"})
    document = {
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": attributes}]}],
        "accessors": accessors,
    }

    def no_decode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("attribute decoding must not precede the metadata budget check")

    monkeypatch.setattr(cold_verifier.struct, "unpack_from", no_decode)
    with pytest.raises(ValueError, match="accessor count must match POSITION"):
        _v07_bound_mesh_vertex_work(document, [0])


def test_provider_glb_repeated_mesh_instances_check_attributes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, binary, _mesh_index, _uv1 = _raw_glb_with_uv1()
    document["meshes"][0]["primitives"][0]["attributes"].update(
        {f"TEXCOORD_{index}": 1 for index in range(2, 1002)}
    )
    document["nodes"] = [{"name": f"raw-{index}", "mesh": 0} for index in range(50_000)]
    nodes = {f"raw-{index}": index for index in range(len(document["nodes"]))}
    identity = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    world = dict.fromkeys(range(len(nodes)), identity)
    assert _v07_bound_mesh_vertex_work(document, list(range(len(nodes)))) == 50_000 * 8
    decoded_meshes: dict[int, Any] = {}
    original_positions = cold_verifier._v07_positions
    decode_calls = 0

    def count_positions(*args: Any, **kwargs: Any) -> Any:
        nonlocal decode_calls
        decode_calls += 1
        return original_positions(*args, **kwargs)

    monkeypatch.setattr(cold_verifier, "_v07_positions", count_positions)
    original_unpack = cold_verifier.struct.unpack_from
    position_reads = 0

    def count_position_reads(fmt: str, data: bytes, offset: int = 0):
        nonlocal position_reads
        if fmt == "<3f":
            position_reads += 1
        return original_unpack(fmt, data, offset)

    monkeypatch.setattr(cold_verifier.struct, "unpack_from", count_position_reads)
    result = _v07_tris(
        document,
        binary,
        nodes,
        world,
        "",
        selected_names={"raw-0", "raw-49"},
        decoded_meshes=decoded_meshes,
    )
    assert set(result) == {"raw-0", "raw-49"}
    assert len(decoded_meshes) == 1
    assert decode_calls == 2
    assert position_reads == 8


def _raw_glb_with_uv1() -> tuple[dict[str, Any], bytes, int, int]:
    encoded = create_box_glb(
        width_m=0.6,
        depth_m=0.5,
        height_m=1.8,
        include_lod1=False,
        include_collider=False,
    )
    document, binary, _nodes, _parents, _world = _v07_glb(encoded)
    primitive = document["meshes"][0]["primitives"][0]
    uv0 = primitive["attributes"]["TEXCOORD_0"]
    uv1 = len(document["accessors"])
    document["accessors"].append(dict(document["accessors"][uv0]))
    primitive["attributes"]["TEXCOORD_1"] = uv1
    return document, binary, 0, uv1


def test_provider_glb_accepts_float_uv1_and_normalized_integer_color_uv() -> None:
    document, binary, mesh_index, uv1 = _raw_glb_with_uv1()
    _v07_positions(document, binary, mesh_index)

    uv = document["accessors"][uv1]
    original_view = document["bufferViews"][uv["bufferView"]]
    integer_view = len(document["bufferViews"])
    document["bufferViews"].append(
        {
            "buffer": 0,
            "byteOffset": original_view.get("byteOffset", 0),
            "byteLength": 16,
            "target": 34962,
        }
    )
    uv.update(
        bufferView=integer_view,
        byteOffset=0,
        componentType=5121,
        count=8,
        type="VEC2",
        normalized=True,
    )
    _v07_positions(document, binary, mesh_index)

    color_view = len(document["bufferViews"])
    document["bufferViews"].append(
        {
            "buffer": 0,
            "byteOffset": original_view.get("byteOffset", 0),
            "byteLength": 48,
            "target": 34962,
        }
    )
    color_index = len(document["accessors"])
    document["accessors"].append(
        {
            "bufferView": color_view,
            "byteOffset": 0,
            "componentType": 5123,
            "count": 8,
            "type": "VEC3",
            "normalized": True,
        }
    )
    document["meshes"][mesh_index]["primitives"][0]["attributes"]["COLOR_1"] = color_index
    _v07_positions(document, binary, mesh_index)


def test_provider_glb_finite_attribute_alias_is_scanned_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document, binary, mesh_index, uv1 = _raw_glb_with_uv1()
    attributes = document["meshes"][mesh_index]["primitives"][0]["attributes"]
    attributes["TEXCOORD_2"] = uv1
    attributes["TEXCOORD_3"] = uv1
    original = cold_verifier.struct.unpack_from
    uv_scans = 0

    def track_uv(fmt: str, data: bytes, offset: int = 0):
        nonlocal uv_scans
        if fmt == "<ff":
            uv_scans += 1
        return original(fmt, data, offset)

    monkeypatch.setattr(cold_verifier.struct, "unpack_from", track_uv)
    _v07_positions(document, binary, mesh_index, validated_attribute_accessors=set())
    assert uv_scans == 16


@pytest.mark.parametrize("malformation", ["count", "bounds", "normalization", "semantic"])
def test_provider_glb_rejects_malformed_uv1_attributes(malformation: str) -> None:
    document, binary, mesh_index, uv1 = _raw_glb_with_uv1()
    uv = document["accessors"][uv1]
    if malformation == "count":
        uv["count"] = 7
    elif malformation == "bounds":
        document["bufferViews"][uv["bufferView"]]["byteLength"] = 4
    elif malformation == "normalization":
        uv["componentType"] = 5121
        uv["normalized"] = False
    else:
        document["meshes"][mesh_index]["primitives"][0]["attributes"]["TEXCOORD_01"] = uv1
    with pytest.raises(ValueError, match="unsupported|out of bounds"):
        _v07_positions(document, binary, mesh_index)
