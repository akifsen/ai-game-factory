"""V0.7 assembly workflow and provider isolation (SQLite-backed, no provider).

The Godot stage is exercised for real only when GAMEFACTORY_TEST_GODOT points
at an executable and a display is available; otherwise the tests stop at the
Godot stage, which must fail closed without an executable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.external.meshy_cli import resolve_paid_request as meshy_resolve
from gamefactory.adapters.fakes import assembly_generator as ag
from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ProjectRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
)
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.errors import (
    PaidRequestInvalidError,
    SpecInvalidError,
    ValidationError,
)
from gamefactory.core.domain.models import AuditEvent, Project, WorkflowStatus, generate_id
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.assembly_production import (
    LocalAssemblyNoProvider,
    create_assembly_workflow,
)
from gamefactory.workflows.asset_production import (
    AssetProductionHandlers,
    create_asset_production_workflow,
    register_asset_production_handlers,
)
from gamefactory.workflows.engine import WorkflowEngine

PROJECT = Path(__file__).resolve().parents[2]
GODOT = os.environ.get("GAMEFACTORY_TEST_GODOT")
HAS_DISPLAY = sys.platform != "linux" or bool(os.environ.get("DISPLAY"))


def _project(tmp_path: Path) -> tuple[Path, Database]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="T"\n', encoding="utf-8"
    )
    db = Database(root / ".gamefactory/state/factory.db")
    MigrationRunner(db).apply_all()
    ProjectRepository(db).save(
        Project(id="asm", name="Assembly test", engine_type="godot", root_path=str(root))
    )
    return root, db


def _inputs(
    root: Path, design: ag.AssemblyDesign, *, source_front: str = "+Z", collapsed: bool = False
) -> tuple[Any, Path, Path]:
    spec = parse_asset_specification_v07(ag.assembly_spec(design))
    source = root / "inputs" / f"{design.asset_id}.glb"
    source.parent.mkdir(parents=True, exist_ok=True)
    glb = ag.assembly_glb(design, source_front=source_front, collapsed=collapsed)
    source.write_bytes(glb)
    registration = root / "inputs" / f"{design.asset_id}.registration.json"
    registration.write_text(
        json.dumps(ag.source_registration(design, glb, source_front=source_front)),
        encoding="utf-8",
    )
    return spec, source, registration


def _engine(root: Path, db: Database, godot: str | None) -> WorkflowEngine:
    engine = WorkflowEngine(
        root,
        db,
        policy_engine=PolicyEngine(
            PolicyRule(require_approval_for_paid=True, require_approval_for_process_execution=False)
        ),
        asset_provider=LocalAssemblyNoProvider(),  # type: ignore[arg-type]
    )
    handlers = AssetProductionHandlers(
        root,
        engine.art_repo,
        AssetRevisionRepository(db),
        engine.app_repo,
        ProviderOperationIntentRepository(db),
        engine.evi_repo,
        engine.gate_repo,
        engine.exec_repo,
        engine.artifact_mgr,
        LocalAssemblyNoProvider(),  # type: ignore[arg-type]
        godot_path=godot,
    )
    register_asset_production_handlers(engine.handler_registry, handlers)
    return engine


def _create(
    root: Path, db: Database, spec: Any, source: Path, registration: Path
) -> tuple[WorkflowEngine, str]:
    workflow, tasks = create_assembly_workflow(
        "asm", root, spec, source, registration, revision_repository=AssetRevisionRepository(db)
    )
    engine = _engine(root, db, GODOT if HAS_DISPLAY else None)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    return engine, workflow.id


def _types(db: Database, workflow_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for artifact in ArtifactRepository(db).list_by_workflow(workflow_id):
        counts[artifact.artifact_type] = counts.get(artifact.artifact_type, 0) + 1
    return counts


def test_assembly_graph_has_no_concept_paid_or_provider_stage(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, registration = _inputs(root, ag.VEHICLE_TANK)
    workflow, tasks = create_assembly_workflow(
        "asm", root, spec, source, registration, revision_repository=AssetRevisionRepository(db)
    )
    assert [t.task_type for t in tasks] == [
        "asset_assembly_prepare",
        "asset_assembly_process",
        "asset_validate",
        "asset_godot",
        "asset_final_review",
        "record_evidence",
    ]
    assert all("cost" not in t.parameters for t in tasks)
    revision = AssetRevisionRepository(db).get(spec.asset_id, 1)
    assert revision is not None
    assert revision.concept_hash is None
    assert revision.raw_glb_hash == json.loads(registration.read_text())["artifact_sha256"]
    assert revision.profile_id == "vehicle" and revision.profile_version == 1


def test_prepare_process_validate_run_without_provider(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, registration = _inputs(root, ag.VEHICLE_TANK)
    workflow, tasks = create_assembly_workflow(
        "asm", root, spec, source, registration, revision_repository=AssetRevisionRepository(db)
    )
    engine = _engine(root, db, None)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    result = engine.run_workflow(workflow.id)
    # Without a Godot executable the runtime stage fails closed after validation.
    assert result.status in {WorkflowStatus.FAILED, WorkflowStatus.BLOCKED}
    counts = _types(db, workflow.id)
    for kind in (
        "asset-specification",
        "asset-source-glb",
        "asset-source-registration",
        "asset-processed-glb",
        "asset-processing-report",
        "asset-validation-report",
    ):
        assert counts.get(kind) == 1, counts
    assert "asset-concept" not in counts and "asset-raw-glb" not in counts
    report_artifact = next(
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow.id)
        if a.artifact_type == "asset-validation-report"
    )
    report = json.loads((root / report_artifact.relative_path).read_text(encoding="utf-8"))
    assert report["status"] == "PASS"
    assert report["schema_version"] == "asset-validation-report-0.7.0"
    assert report["rule_groups"] == [
        "core",
        "parts",
        "orientation",
        "pivot",
        "sockets",
        "collider_box",
    ]
    assert report["normalization"]["source_front"] == "+Z"
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow.id) == []


def test_source_changed_after_creation_stops_prepare(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, registration = _inputs(root, ag.WEAPON_RIFLE, source_front="-Z")
    workflow, tasks = create_assembly_workflow(
        "asm", root, spec, source, registration, revision_repository=AssetRevisionRepository(db)
    )
    source.write_bytes(source.read_bytes() + b"\0\0\0\0")
    engine = _engine(root, db, None)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    result = engine.run_workflow(workflow.id)
    assert result.status == WorkflowStatus.FAILED
    assert "changed after workflow creation" in (result.error_message or "")


def test_collapsed_pivots_fail_validation_before_godot(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, registration = _inputs(root, ag.VEHICLE_TANK, source_front="-Z", collapsed=True)
    engine, workflow_id = _create(root, db, spec, source, registration)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.FAILED
    godot = next(
        t for t in TaskRepository(db).list_by_workflow(workflow_id) if t.task_type == "asset_godot"
    )
    assert godot.status.value == "PENDING"
    report_artifact = next(
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "asset-validation-report"
    )
    report = json.loads((root / report_artifact.relative_path).read_text(encoding="utf-8"))
    assert "pivot.collapsed" in {
        f["rule_id"] for f in report["findings"] if f["severity"] == "FAIL"
    }


def test_workflow_creation_rejects_mismatched_registration(tmp_path: Path) -> None:
    root, db = _project(tmp_path)
    spec, source, registration = _inputs(root, ag.VEHICLE_TANK)
    data = json.loads(registration.read_text())
    data["socket_map"] = []
    registration.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SpecInvalidError, match="socket_map"):
        create_assembly_workflow(
            "asm", root, spec, source, registration, revision_repository=AssetRevisionRepository(db)
        )
    character = parse_asset_specification_v07(ag.character_spec(ag.HUMANOID_CHARACTER))
    with pytest.raises(ValidationError, match="not an assembly profile"):
        create_assembly_workflow(
            "asm",
            root,
            character,
            source,
            registration,
            revision_repository=AssetRevisionRepository(db),
        )


# --- ADR 0016: assemblies never reach the provider request ---------------------------


def test_assembly_spec_is_rejected_by_the_provider_workflow_with_zero_intents(
    tmp_path: Path,
) -> None:
    from PIL import Image

    root, db = _project(tmp_path)
    spec, _, _ = _inputs(root, ag.VEHICLE_TANK)
    concept = root / "concept.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(concept)
    provenance = root / "concept.json"
    provenance.write_text("{}", encoding="utf-8")
    with pytest.raises(PaidRequestInvalidError, match="cannot bind to a provider request"):
        create_asset_production_workflow(
            "asm",
            root,
            spec,
            concept,
            provenance,
            provider_name="fake",
            revision_repository=AssetRevisionRepository(db),
        )
    assert AssetRevisionRepository(db).get(spec.asset_id, 1) is None
    assert ProviderOperationIntentRepository(db).list_by_workflow("any") == []


@pytest.mark.parametrize("adapter", ["meshy", "fake"])
@pytest.mark.parametrize("variant", ["parts", "sockets_only", "assembly_source"])
def test_provider_adapters_refuse_assembly_specifications(adapter: str, variant: str) -> None:
    data = ag.assembly_spec(ag.WEAPON_RIFLE)
    if variant == "sockets_only":
        data = {"sockets": data["sockets"]}
    elif variant == "assembly_source":
        data = {"schema_version": "0.7.0", **{k: v for k, v in data.items() if k != "parts"}}
        data.pop("sockets", None)
    binding = {"asset_id": "x", "revision_number": 1}
    cost = {"estimate": None, "reservation": 0.0, "unit": "credits"}
    with pytest.raises(PaidRequestInvalidError):
        if adapter == "meshy":
            meshy_resolve(binding, data, cost)
        else:
            FakeAssetGenerationProvider().resolve_paid_request(binding, data, cost)


def test_single_mesh_character_spec_still_binds_a_provider_request() -> None:
    data = ag.character_spec(ag.HUMANOID_CHARACTER)
    content = FakeAssetGenerationProvider().resolve_paid_request(
        {
            "asset_id": data["asset_id"],
            "revision_number": 1,
            "concept_version": 1,
            "concept_sha256": "0" * 64,
            "specification_sha256": "1" * 64,
            "profile_id": "character",
            "profile_version": 1,
        },
        data,
        {"estimate": 5.0, "reservation": 5.0, "unit": "credits"},
    )
    assert content["request"]["target_polycount"] == 5000


# --- CLI --------------------------------------------------------------------------


def _cli(root: Path, *arguments: str) -> tuple[subprocess.CompletedProcess[str], Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "gamefactory", "--json", "--project", str(root), *arguments],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    return result, json.loads(result.stdout.strip() or result.stderr.strip())


def test_cli_register_source_and_assemble_dry_run(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="T"\n', encoding="utf-8"
    )
    assert _cli(root, "init")[0].returncode == 0
    (root / "spec.json").write_text(json.dumps(ag.assembly_spec(ag.AIRCRAFT_TRAINER)))
    (root / "source.glb").write_bytes(ag.assembly_glb(ag.AIRCRAFT_TRAINER, source_front="+Z"))
    proc, payload = _cli(
        root,
        "asset",
        "register-source",
        "--spec",
        "spec.json",
        "--source",
        "source.glb",
        "--source-front=+Z",
        "--authoring-tool",
        "blender",
        "--authoring-tool-version",
        "4.0.2",
        "--actor",
        "tester",
        "--reason",
        "cli test",
        "--output",
        "registration.json",
    )
    assert proc.returncode == 0, payload
    assert payload["paid"] is False and payload["source_front"] == "+Z"
    proc, payload = _cli(
        root,
        "asset",
        "assemble",
        "--spec",
        "spec.json",
        "--source",
        "source.glb",
        "--registration",
        "registration.json",
        "--dry-run",
    )
    assert proc.returncode == 0, payload
    assert payload["workflow_state_mutated"] is False
    assert payload["normalization"] == "180 deg about +Y at the root"
    assert payload["required_approvals"] == ["final_visual_review"]
    proc, payload = _cli(root, "asset", "profiles")
    rows = {row["qualified"]: row for row in payload["asset_profiles"]}
    assert rows["aircraft@1"]["geometry_mode"] == "assembly"
    assert rows["character@1"]["geometry_mode"] == "single_mesh"
    assert rows["rigged_character"]["status"] == "UNSUPPORTED"


@pytest.mark.skipif(
    not GODOT or not HAS_DISPLAY, reason="requires GAMEFACTORY_TEST_GODOT and a display"
)
def test_real_godot_assembly_to_cold_verified_bundle(tmp_path: Path) -> None:
    from gamefactory.workflows.asset_evidence import export_asset_evidence_bundle

    root, db = _project(tmp_path)
    spec, source, registration = _inputs(root, ag.VEHICLE_TANK, source_front="+Z")
    engine, workflow_id = _create(root, db, spec, source, registration)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.BLOCKED, result.error_message
    assert result.pending_approval_id
    repo = ApprovalRepository(db)
    approval = repo.get(result.pending_approval_id)
    assert approval is not None
    task = TaskRepository(db).get(approval.task_id)
    workflow = engine.wf_repo.get(workflow_id)
    assert task is not None and workflow is not None
    decided = ApprovalService.approve(
        approval, "test-only", current_inputs=engine.approval_inputs(workflow, task)
    )
    assert repo.decide_if_pending(
        decided,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=decided.id,
            action="APPROVED",
            actor="test-only",
            previous_state="PENDING",
            new_state="APPROVED",
        ),
    )
    assert engine.run_workflow(workflow_id).status == WorkflowStatus.COMPLETED
    bundle = export_asset_evidence_bundle(root, db, workflow_id, root / "bundle")
    proc = subprocess.run(
        [sys.executable, "-I", str(bundle / "verify_asset_bundle.py"), str(bundle)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    verdict = json.loads(proc.stdout)
    assert verdict["status"] == "PASS" and verdict["source_front"] == "+Z"
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert manifest["paid"] is False
    assert len(manifest["review_views"]) == 7


@pytest.mark.skipif(
    not GODOT or not HAS_DISPLAY, reason="requires GAMEFACTORY_TEST_GODOT and a display"
)
def test_every_placed_view_renders_in_real_godot(tmp_path: Path) -> None:
    """ADR 0014: every placed view, including the legacy side alias, has a real capture."""
    import shutil

    from gamefactory.core.domain.camera_framing import PLACED_VIEWS

    assert GODOT is not None
    spec = parse_asset_specification_v07(ag.assembly_spec(ag.VEHICLE_TANK))
    profile = spec.bound_profile()
    stage = tmp_path / "stage"
    (stage / "assets").mkdir(parents=True)
    (stage / "captures").mkdir()
    (stage / "assets" / "asset.glb").write_bytes(ag.assembly_glb(ag.VEHICLE_TANK))
    shutil.copyfile(
        PROJECT / "src/gamefactory/resources/godot/asset_runtime_harness.gd",
        stage / "asset_runtime_harness.gd",
    )
    (stage / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="Views"\n[display]\n'
        "window/size/viewport_width=1280\nwindow/size/viewport_height=720\n[rendering]\n"
        'renderer/rendering_method="gl_compatibility"\n'
        'renderer/rendering_method.mobile="gl_compatibility"\n',
        encoding="utf-8",
    )
    from gamefactory.workflows.asset_production import runtime_contract_v07

    views = sorted(PLACED_VIEWS)
    request = {
        "workflow_id": "WF-VIEWS",
        "revision": 1,
        "asset_id": spec.asset_id,
        "execution_id": "EXEC-VIEWS",
        "attempt_number": 1,
        "glb": "res://assets/asset.glb",
        "processed_glb_sha256": "0" * 64,
        "output_dir": str(stage / "captures"),
        "observation_path": str(stage / "observation.json"),
        "angles": views,
        "profile": profile.capture_request_profile(spec),
        "contract_v07": runtime_contract_v07(spec),
    }
    (stage / "request.json").write_text(json.dumps(request), encoding="utf-8")
    for command in (
        [GODOT, "--headless", "--path", str(stage), "--editor", "--import", "--quit"],
        [
            GODOT,
            "--path",
            str(stage),
            "--script",
            "res://asset_runtime_harness.gd",
            "--",
            "--request",
            str(stage / "request.json"),
        ],
    ):
        subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    observation = json.loads((stage / "observation.json").read_text(encoding="utf-8"))
    assert observation["status"] == "PASS", observation["errors"]
    assert sorted(p.stem for p in (stage / "captures").glob("*.png")) == views
    from gamefactory.core.domain.camera_framing import view_axis_label

    for view in views:
        assert observation["view_framing"][view]["view_axis"] == view_axis_label(view)
