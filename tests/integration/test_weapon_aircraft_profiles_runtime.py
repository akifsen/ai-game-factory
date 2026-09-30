"""Real Blender/Godot local assembly and cold-export tests for two private candidates."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image

from gamefactory.adapters.assets.assembly_ingest import ingest_assembly_source
from gamefactory.adapters.assets.v07_geometry_validation import (
    VerifiedSourceNormalization,
    validate_glb_v07,
)
from gamefactory.adapters.dcc.assembly_processor import AssemblyProcessor
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.engines.godot import GodotAdapter
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import AssetRevisionRepository, ProjectRepository
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.asset_profiles import (
    UNSUPPORTED_PROFILE_IDS,
    AssetProfileV07,
    ProfileRegistry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.models import CostClass, Project, WorkflowStatus
from gamefactory.workflows.assembly_evidence import verify_local_assembly_evidence_bundle
from gamefactory.workflows.assembly_production import (
    AssemblyAdapters,
    create_local_assembly_workflow,
)
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.handlers import TaskHandlerRegistry

_VIEWS = (
    "front",
    "rear",
    "left",
    "right",
    "side",
    "three_quarter",
    "three_quarter_front",
    "three_quarter_rear",
    "top",
)
_BOX_TRIS = (
    0,
    1,
    2,
    0,
    2,
    3,
    4,
    6,
    5,
    4,
    7,
    6,
    0,
    4,
    5,
    0,
    5,
    1,
    2,
    6,
    7,
    2,
    7,
    3,
    0,
    3,
    7,
    0,
    7,
    4,
    1,
    5,
    6,
    1,
    6,
    2,
)


def _load_candidate(name: str) -> tuple[AssetProfileV07, Any]:
    profile_doc = (
        files("gamefactory").joinpath(f"resources/profiles/{name}.yml").read_text(encoding="utf-8")
    )
    profile = AssetProfileV07(parse_profile_document_v07(profile_doc))
    unsupported = tuple(item for item in UNSUPPORTED_PROFILE_IDS if item != name)
    registry = ProfileRegistry(available=(), unsupported=unsupported, available_v07=(profile,))
    spec_data = yaml.safe_load(
        files("gamefactory")
        .joinpath(f"resources/specs/{name}_test.yml")
        .read_text(encoding="utf-8")
    )
    return profile, parse_asset_specification_v07(spec_data, registry=registry)


def _box_vertices(
    dimensions: tuple[float, float, float], center: tuple[float, float, float]
) -> list[tuple[float, float, float]]:
    dx, dy, dz = (dimension / 2 for dimension in dimensions)
    cx, cy, cz = center
    return [
        (cx - dx, cy - dy, cz - dz),
        (cx + dx, cy - dy, cz - dz),
        (cx + dx, cy - dy, cz + dz),
        (cx - dx, cy - dy, cz + dz),
        (cx - dx, cy + dy, cz - dz),
        (cx + dx, cy + dy, cz - dz),
        (cx + dx, cy + dy, cz + dz),
        (cx - dx, cy + dy, cz + dz),
    ]


def _append(data: bytes, target: int, binary: bytearray, views: list[dict[str, int]]) -> int:
    binary.extend(b"\0" * ((-len(binary)) % 4))
    offset = len(binary)
    binary.extend(data)
    views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(data), "target": target})
    return len(views) - 1


def _mesh_boxes(
    boxes: list[tuple[tuple[float, float, float], tuple[float, float, float]]],
    binary: bytearray,
    views: list[dict[str, int]],
    accessors: list[dict[str, Any]],
    asset_id: str,
    part_id: str,
) -> dict[str, Any]:
    vertices: list[tuple[float, float, float]] = []
    indices: list[int] = []
    for dims, center in boxes:
        base = len(vertices)
        vertices.extend(_box_vertices(dims, center))
        indices.extend(base + index for index in _BOX_TRIS)
    position_view = _append(
        b"".join(struct.pack("<fff", *row) for row in vertices), 34962, binary, views
    )
    position_index = len(accessors)
    accessors.append(
        {
            "bufferView": position_view,
            "componentType": 5126,
            "count": len(vertices),
            "type": "VEC3",
            "min": [min(row[axis] for row in vertices) for axis in range(3)],
            "max": [max(row[axis] for row in vertices) for axis in range(3)],
        }
    )
    index_view = _append(struct.pack(f"<{len(indices)}H", *indices), 34963, binary, views)
    index_index = len(accessors)
    accessors.append(
        {
            "bufferView": index_view,
            "componentType": 5123,
            "count": len(indices),
            "type": "SCALAR",
            "min": [min(indices)],
            "max": [max(indices)],
        }
    )
    return {
        "name": f"{asset_id}_{part_id}_LOD0_mesh",
        "primitives": [
            {"attributes": {"POSITION": position_index}, "indices": index_index, "material": 0}
        ],
    }


def _geometry(
    name: str,
) -> dict[str, list[tuple[tuple[float, float, float], tuple[float, float, float]]]]:
    if name == "weapon":
        # Distinct receiver block, narrow long bore, canted grip, and compact bolt.
        return {
            "receiver": [((0.60, 0.30, 0.88), (0.0, 0.0, 0.0))],
            "barrel": [((0.12, 0.12, 0.90), (0.0, 0.0, -0.43))],
            "grip": [((0.36, 0.32, 0.32), (0.0, 0.0, 0.0))],
            "bolt": [((0.30, 0.08, 0.20), (0.0, 0.0, 0.0))],
        }
    return {
        "fuselage": [((0.90, 2.40, 7.60), (0.0, 0.0, 0.0))],
        "wing": [((11.0, 0.18, 2.40), (0.0, 0.0, 0.0))],
        "tailplane": [((3.20, 0.12, 1.0), (0.0, 0.0, 0.0))],
        # One mesh with two crossing blades, rotating about aircraft-longitudinal Z.
        "propeller": [
            ((0.12, 1.50, 0.12), (0.0, 0.0, 0.0)),
            ((1.50, 0.12, 0.12), (0.0, 0.0, 0.0)),
        ],
    }


def _write_authored_source(path: Path, name: str, spec: Any) -> None:
    binary = bytearray()
    views: list[dict[str, int]] = []
    accessors: list[dict[str, Any]] = []
    meshes = [
        _mesh_boxes(
            _geometry(name)[part.part_id], binary, views, accessors, spec.asset_id, part.part_id
        )
        for part in spec.parts
    ]
    part_nodes: dict[str, int] = {}
    nodes: list[dict[str, Any]] = [{"name": "ROOT", "children": []}]
    for part in spec.parts:
        motion = part.pivot.motion
        extras: dict[str, Any] = {"gf_motion": motion.kind}
        if motion.axis is not None:
            extras["gf_axis"] = list(motion.axis)
        part_nodes[part.part_id] = len(nodes)
        nodes.append(
            {
                "name": f"PART_{part.part_id}",
                "translation": list(part.pivot.position_m),
                "rotation": [0.0, 0.0, 0.0, 1.0],
                "scale": [1.0, 1.0, 1.0],
                "extras": extras,
                "children": [],
            }
        )
    for index, part in enumerate(spec.parts):
        pnode = nodes[part_nodes[part.part_id]]
        if part.parent == "root":
            nodes[0]["children"].append(part_nodes[part.part_id])
        else:
            nodes[part_nodes[part.parent]]["children"].append(part_nodes[part.part_id])
        mesh_node = len(nodes)
        pnode["children"].append(mesh_node)
        nodes.append(
            {
                "name": f"SM_{spec.asset_id}_{part.part_id}_LOD0",
                "mesh": index,
                "translation": [0.0, 0.0, 0.0],
                "rotation": [0.0, 0.0, 0.0, 1.0],
                "scale": [1.0, 1.0, 1.0],
            }
        )
    for socket in spec.sockets or []:
        pnode = nodes[part_nodes[socket.parent_part]]
        socket_node = len(nodes)
        pnode["children"].append(socket_node)
        rotation = list(socket.rotation) if socket.rotation != "identity" else [0.0, 0.0, 0.0, 1.0]
        nodes.append(
            {
                "name": f"SOCKET_{socket.socket_id}",
                "translation": list(socket.translation_m),
                "rotation": rotation,
                "scale": [1.0, 1.0, 1.0],
            }
        )
    document = {
        "asset": {"version": "2.0", "generator": "local-authored-weapon-aircraft-fixture"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": [
            {
                "name": f"M_{name}",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.24, 0.33, 0.38, 1.0],
                    "metallicFactor": 0.18,
                    "roughnessFactor": 0.72,
                },
            }
        ],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(binary)}],
    }
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    encoded += b" " * ((-len(encoded)) % 4)
    binary.extend(b"\0" * ((-len(binary)) % 4))
    total = 12 + 8 + len(encoded) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(encoded), 0x4E4F534A)
        + encoded
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def _read_glb(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    json_length = struct.unpack_from("<I", raw, 12)[0]
    document = json.loads(raw[20 : 20 + json_length].decode("utf-8").rstrip())
    binary_offset = 20 + json_length
    binary_length = struct.unpack_from("<I", raw, binary_offset)[0]
    return document, raw[binary_offset + 8 : binary_offset + 8 + binary_length]


def _rewrite_glb(path: Path, document: dict[str, Any], binary: bytes) -> None:
    encoded = json.dumps(document, separators=(",", ":")).encode("utf-8")
    encoded += b" " * ((-len(encoded)) % 4)
    binary += b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(encoded) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(encoded), 0x4E4F534A)
        + encoded
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def _check_semantic_negatives(source: Path, temp_dir: Path, name: str, spec: Any) -> None:
    raw = source.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    observation = VerifiedSourceNormalization(
        source_sha256=digest,
        processed_sha256=digest,
        source_front="-Z",
        normalization_applied=False,
        root_rotation_xyzw=(0.0, 0.0, 0.0, 1.0),
        source_glb_bytes=raw,
    )
    moved_part = "bolt" if name == "weapon" else "propeller"
    socket_ids = [socket.socket_id for socket in spec.sockets]
    mutations: dict[str, tuple[str, Any]] = {
        f"part.parent.{moved_part}": (
            f"parent-{moved_part}",
            lambda doc: _move_part_to_root(doc, moved_part),
        ),
        f"pivot.position.{moved_part}": (
            f"pivot-{moved_part}",
            lambda doc: _shift_part_pivot(doc, moved_part),
        ),
        f"pivot.axis.{moved_part}": (
            f"axis-{moved_part}",
            lambda doc: _change_part_axis(doc, moved_part),
        ),
    }
    for socket_id in socket_ids:
        mutations[f"socket.orientation.{socket_id}"] = (
            f"orientation-{socket_id}",
            lambda doc, sid=socket_id: _change_socket_orientation(doc, sid),
        )
        mutations[f"socket.position.{socket_id}"] = (
            f"placement-{socket_id}",
            lambda doc, sid=socket_id: _move_socket_from_forward_end(doc, sid),
        )

    for expected_rule, (suffix, mutate) in mutations.items():
        document, binary = _read_glb(source)
        mutate(document)
        bad = temp_dir / f"{name}-negative-{suffix}.glb"
        _rewrite_glb(bad, document, binary)
        result = validate_glb_v07(bad, spec, source_observation=observation)
        failed_rules = {
            finding.rule_id for finding in result.findings if finding.severity.value == "FAIL"
        }
        assert expected_rule in failed_rules, (expected_rule, failed_rules)


def _named(document: dict[str, Any], name: str) -> tuple[int, dict[str, Any]]:
    index = next(i for i, node in enumerate(document["nodes"]) if node.get("name") == name)
    return index, document["nodes"][index]


def _move_part_to_root(document: dict[str, Any], part_id: str) -> None:
    parent = "receiver" if part_id in {"barrel", "grip", "bolt"} else "fuselage"
    index, _ = _named(document, f"PART_{part_id}")
    _, parent_node = _named(document, f"PART_{parent}")
    parent_node["children"].remove(index)
    document["nodes"][0]["children"].append(index)


def _shift_part_pivot(document: dict[str, Any], part_id: str) -> None:
    _, node = _named(document, f"PART_{part_id}")
    node["translation"][0] += 0.05


def _change_part_axis(document: dict[str, Any], part_id: str) -> None:
    _, node = _named(document, f"PART_{part_id}")
    node["extras"]["gf_axis"] = [0.0, 1.0, 0.0]


def _change_socket_orientation(document: dict[str, Any], socket_id: str) -> None:
    _, node = _named(document, f"SOCKET_{socket_id}")
    node["rotation"] = [1.0, 0.0, 0.0, 0.0]


def _move_socket_from_forward_end(document: dict[str, Any], socket_id: str) -> None:
    _, node = _named(document, f"SOCKET_{socket_id}")
    node["translation"][2] = abs(node["translation"][2]) + 0.25


def _approve_pending(engine: WorkflowEngine, approval_id: str) -> None:
    approval = engine.app_repo.get(approval_id)
    assert approval is not None
    task = engine.task_repo.get(approval.task_id)
    workflow = engine.wf_repo.get(approval.workflow_id)
    assert task is not None and workflow is not None
    approval = ApprovalService.approve(
        approval,
        actor="isolated-weapon-aircraft-fixture-operator",
        comment="Explicit fixture approval; not a production approval.",
        current_inputs=engine.approval_inputs(workflow, task, CostClass.LOCAL),
    )
    engine.app_repo.save(approval)


def _paid_counts(db: Database) -> dict[str, int]:
    tables = (
        "provider_invocations",
        "provider_operation_intents",
        "cost_ledger",
        "paid_request_snapshots",
        "production_readiness_reports",
    )
    with db.connect() as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


@pytest.mark.real_godot
@pytest.mark.parametrize("name", ["weapon", "aircraft"])
def test_candidate_runs_local_human_gated_blender_godot_and_cold_export(
    name: str, tmp_path: Path
) -> None:
    blender_path = BlenderAdapter().detect_tool()
    godot = os.environ.get("GAMEFACTORY_TEST_GODOT") or GodotAdapter().find_candidate_executable()
    if not blender_path.available:
        pytest.skip("Blender is not installed on this host")
    if not godot:
        pytest.skip("Godot is not installed on this host")
    godot_path = Path(godot)
    godot_stub_without_pair = (
        godot_path.name.lower().endswith("_console.exe")
        and not godot_path.with_name(godot_path.name[: -len("_console.exe")] + ".exe").is_file()
    )
    blender = AssemblyProcessor()

    profile, spec = _load_candidate(name)
    assert profile.assembly is not None
    assert spec.parts is not None and spec.sockets is not None
    project_root = tmp_path / "p"
    project_root.mkdir()
    (project_root / ".gamefactory" / "locks").mkdir(parents=True)
    db = Database(project_root / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    project_id = f"isolated-{name}-p"
    ProjectRepository(db).save(Project(project_id, f"{name} fixture", "godot", str(project_root)))
    engine = WorkflowEngine(project_root, db, handler_registry=TaskHandlerRegistry())

    source = tmp_path / "source.glb"
    _write_authored_source(source, name, spec)
    source_bytes_before = source.read_bytes()
    _check_semantic_negatives(source, tmp_path, name, spec)
    package = ingest_assembly_source(
        source_glb_path=source,
        managed_root=project_root,
        relative_package_dir=f"sources/{name}/r001",
        spec=spec,
        authoring_tool_name="Isolated GLB fixture writer",
        authoring_tool_version="1",
        source_front="-Z",
        actor="isolated-fixture-author",
        reason=f"Authored semantic {name} fixture for local integration only",
    )
    concept = project_root / "concept" / "concept.png"
    concept.parent.mkdir(parents=True)
    Image.new("RGB", (64, 64), (62, 81, 92)).save(concept, format="PNG")
    concept_provenance = project_root / "concept" / "provenance.json"
    concept_provenance.write_text(
        json.dumps({"schema_version": 1, "origin": "isolated-test-fixture", "profile": name}),
        encoding="utf-8",
    )

    created = create_local_assembly_workflow(
        engine,
        project_id,
        spec,
        profile,
        package.package_dir,
        expected_provenance_sha256=package.retained_provenance_sha256,
        concept_image=concept,
        concept_provenance=concept_provenance,
        workflow_id=f"fixture-{name}-assembly-workflow",
        adapters=AssemblyAdapters(blender=blender, godot_executable=godot),
    )
    for _ in range(8):
        result = engine.run_workflow(created.workflow.id)
        if result.status == WorkflowStatus.COMPLETED:
            break
        if result.status == WorkflowStatus.FAILED and godot_stub_without_pair:
            failed_task = next(task for task in created.tasks if task.task_type.endswith("_godot"))
            validate_task = next(
                task for task in created.tasks if task.task_type.endswith("_validate")
            )
            failed_task_state = engine.task_repo.get(failed_task.id)
            validate_task_state = engine.task_repo.get(validate_task.id)
            assert failed_task_state is not None and failed_task_state.status.value == "FAILED"
            assert (
                validate_task_state is not None and validate_task_state.status.value == "COMPLETED"
            )
            validation_row = next(
                row
                for row in engine.art_repo.list_by_task(validate_task.id)
                if row.artifact_type == "assembly-validation-report"
            )
            validation_report = json.loads(
                (project_root / validation_row.relative_path).read_text(encoding="utf-8")
            )
            assert validation_report["status"] == "PASS"
            pytest.skip(
                "Blender processing and V0.7 validation passed; installed Godot console "
                "launcher has no required paired executable"
            )
        assert result.status == WorkflowStatus.BLOCKED, result.error_message
        assert result.pending_approval_id, result.error_message
        _approve_pending(engine, result.pending_approval_id)
    completed_workflow = engine.wf_repo.get(created.workflow.id)
    assert completed_workflow is not None and completed_workflow.status == WorkflowStatus.COMPLETED

    revision = AssetRevisionRepository(db).get(spec.asset_id, created.revision.revision_number)
    assert revision is not None
    assert revision.processed_glb_hash and revision.validation_report_hash
    assert package.retained_glb_path.read_bytes() == source_bytes_before
    process_task = next(task for task in created.tasks if task.task_type.endswith("_process"))
    process_report_row = next(
        row
        for row in engine.art_repo.list_by_task(process_task.id)
        if row.artifact_type == "assembly-blender-report"
    )
    process_report = json.loads(
        (project_root / process_report_row.relative_path).read_text(encoding="utf-8")
    )
    assert process_report["normalization"]["applied"] is False
    assert process_report["assembly_bounds"]["dimensions"] == pytest.approx(
        [spec.dimensions.width_m, spec.dimensions.height_m, spec.dimensions.depth_m],
        abs=0.02,
    )
    runtime_task = next(task for task in created.tasks if task.task_type.endswith("_godot"))
    runtime = next(
        row
        for row in engine.art_repo.list_by_task(runtime_task.id)
        if row.artifact_type == "assembly-runtime-observation"
    )
    assert runtime is not None
    runtime_json = json.loads((project_root / runtime.relative_path).read_text(encoding="utf-8"))
    assert runtime_json["restoration_verified"] is True
    assert runtime_json["collider"]["physics_ray_hit"] is True
    assert set(runtime_json["captures"]) == set(_VIEWS)
    moving = {part.part_id for part in spec.parts if part.pivot.motion.kind != "fixed"}
    records = {row["part_id"]: row for row in runtime_json["articulation_results"]}
    assert set(records) == moving
    assert all(row["descendants_rigid_ok"] and row["restored_ok"] for row in records.values())
    if name == "aircraft":
        assert "propeller" in records
        assert records["propeller"]["pivot_world_ok"] is True

    evidence_task = next(task for task in created.tasks if task.task_type.endswith("_evidence"))
    evidence_rows = [
        row
        for row in engine.art_repo.list_by_task(evidence_task.id)
        if row.artifact_type == "assembly-evidence-manifest"
    ]
    assert len(evidence_rows) == 1
    bundle = (project_root / evidence_rows[0].relative_path).parent
    cold = verify_local_assembly_evidence_bundle(bundle)
    assert cold["status"] == "PASS" and cold["product_ready"] is True
    assert _paid_counts(db) == {
        "provider_invocations": 0,
        "provider_operation_intents": 0,
        "cost_ledger": 0,
        "paid_request_snapshots": 0,
        "production_readiness_reports": 0,
    }
