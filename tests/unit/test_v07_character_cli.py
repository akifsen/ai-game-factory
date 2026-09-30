"""Closed-catalog and typed parsing guarantees for the V0.7 character CLI."""

from __future__ import annotations

import hashlib
import json
import shutil
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    CostLedgerRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.cli import main as cli_main
from gamefactory.cli.main import (
    _create_asset_workflow,
    _db,
    _dispatch,
    _parse_cli_asset_specification,
    build_parser,
)
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.domain.asset_contracts import AssetSpecificationV07
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    builtin_registry,
    builtin_v07_registry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import TaskStatus, WorkflowStatus
from gamefactory.workflows import asset_evidence, provider_character_evidence
from gamefactory.workflows.engine import WorkflowEngine

_RESOURCE_ROOT = files("gamefactory").joinpath("resources")


def _character_registry() -> ProfileRegistry:
    document = parse_profile_document_v07(
        _RESOURCE_ROOT.joinpath("profiles/character.yml").read_text(encoding="utf-8")
    )
    profile = AssetProfileV07(document)
    return ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))


def test_packaged_character_pair_is_typed_and_matches_the_public_candidate_contract() -> None:
    profile_path = _RESOURCE_ROOT.joinpath("profiles/character.yml")
    spec_path = _RESOURCE_ROOT.joinpath("specs/character_test.yml")
    assert profile_path.is_file() and spec_path.is_file()
    registry = builtin_v07_registry()
    profile = registry.get_v07("character", 1)
    spec = _parse_cli_asset_specification(Path(str(spec_path)), registry=registry)

    assert type(spec) is AssetSpecificationV07
    assert spec.bound_profile() == profile
    assert spec.profile == "character" and spec.profile_version == 1
    assert spec.source_kind == "provider_generated"
    assert spec.dimensions.model_dump(mode="python") == {
        "width_m": 0.6,
        "depth_m": 0.5,
        "height_m": 1.8,
    }
    assert spec.orientation.up == "+Y" and spec.orientation.front == "-Z"
    assert spec.collider.policy == "capsule"
    assert spec.collider.capsule is not None
    assert spec.collider.capsule.radius_m == 0.3
    assert spec.collider.capsule.height_m == 1.8
    assert spec.parts is None and spec.sockets is None
    assert profile.geometry_mode == "single_mesh"
    assert profile.review_views == (
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
    assert profile.document.processing.rig_forbidden is True
    assert profile.document.processing.animation_forbidden is True
    assert profile.document.godot.body_kind == "static_body"
    assert profile.document.godot.require_ray_hit is True
    assert profile.document.godot.require_area is False


def test_public_character_cli_rejects_unsupported_profile_before_database_creation(
    tmp_path: Path,
) -> None:
    spec = tmp_path / "rigged-character.yml"
    spec.write_text(
        _RESOURCE_ROOT.joinpath("specs/character_test.yml")
        .read_text(encoding="utf-8")
        .replace("profile: character\n", "profile: rigged_character\n"),
        encoding="utf-8",
    )
    root = tmp_path / "uninitialized-project"
    root.mkdir()
    args = build_parser().parse_args(
        [
            "asset",
            "create",
            "--project",
            str(root),
            "--spec",
            str(spec),
            "--concept",
            "concept.png",
            "--provenance",
            "concept.json",
            "--dry-run",
            "--json",
        ]
    )

    with pytest.raises(ValidationError, match="UNSUPPORTED"):
        _dispatch(args)

    registry = builtin_v07_registry()
    assert tuple(profile.qualified for profile in registry.available_v07) == (
        "vehicle@1",
        "weapon@1",
        "aircraft@1",
        "character@1",
    )
    assert "rigged_character" in registry.unsupported
    assert builtin_registry().availability()  # legacy catalog stays independently available
    assert not (root / ".gamefactory").exists()


def test_asset_create_routes_assembly_specs_to_the_assembly_command_before_state_mutation(
    tmp_path: Path,
) -> None:
    spec = Path(str(_RESOURCE_ROOT.joinpath("specs/armored_vehicle_test.yml")))
    root = tmp_path / "uninitialized-project"
    root.mkdir()
    args = build_parser().parse_args(
        [
            "asset-create",
            "--project",
            str(root),
            "--spec",
            str(spec),
            "--concept",
            "concept.png",
            "--provenance",
            "concept.json",
            "--provider",
            "meshy",
            "--json",
        ]
    )

    with pytest.raises(ValidationError, match="assembly create"):
        _dispatch(args)

    assert not (root / ".gamefactory").exists()


def test_cli_asset_schema_selection_keeps_legacy_parser_and_catalog_separate() -> None:
    legacy = _parse_cli_asset_specification(
        {
            "schema_version": "0.4.0",
            "asset_id": "legacy_prop",
            "intent": "legacy parser fixture",
            "dimensions": {"width_m": 1.0, "depth_m": 1.0, "height_m": 1.0},
        }
    )
    assert not isinstance(legacy, AssetSpecificationV07)
    assert tuple(profile.qualified for profile in builtin_v07_registry().available_v07) == (
        "vehicle@1",
        "weapon@1",
        "aircraft@1",
        "character@1",
    )


def _persist_private_character_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Database, str]:
    root = tmp_path / "project"
    root.mkdir()
    ConfigLoader.init_project(root, project_name="private character CLI fixture")
    db = _db(root)
    spec_path = root / "character.yml"
    shutil.copyfile(_RESOURCE_ROOT.joinpath("specs/character_test.yml"), spec_path)
    spec = _parse_cli_asset_specification(spec_path, registry=_character_registry())
    concept = root / "concept.png"
    Image.new("RGB", (24, 24), (30, 70, 120)).save(concept, format="PNG")
    provenance = root / "concept.json"
    provenance.write_text(
        json.dumps(
            {
                "sha256": hashlib.sha256(concept.read_bytes()).hexdigest(),
                "source": "named-local-cli-fixture",
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        spec=str(spec_path),
        concept=str(concept),
        provenance=str(provenance),
        provider="fake",
        concept_source_type="imported",
        dry_run=False,
        budget_reservation=None,
        godot_path=None,
        blender_path=None,
    )

    def stop_before_handlers(_engine, workflow_id: str):
        return SimpleNamespace(
            workflow_id=workflow_id,
            status=WorkflowStatus.BLOCKED,
            pending_approval_id=None,
            completed_tasks=[],
            failed_tasks=[],
            blocked_tasks=[],
            error_message="private registry fixture; task handlers not run",
        )

    monkeypatch.setattr(WorkflowEngine, "run_workflow", stop_before_handlers)

    payload, code, _message = _create_asset_workflow(
        root,
        db,
        args,
        specification=spec,
        profile_registry=_character_registry(),
    )

    assert payload["status"] == "BLOCKED"
    assert code != 0
    return root, db, payload["workflow_id"]


def test_private_character_create_persists_atomic_graph_without_legacy_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db, workflow_id = _persist_private_character_graph(tmp_path, monkeypatch)
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    assert len(tasks) == 10
    assert sum(task.task_type == "asset_prepare" for task in tasks) == 1
    assert WorkflowRepository(db).get(workflow_id) is not None
    assert all(task.parameters["graph_version"] == "0.7.0" for task in tasks)
    paid_task = next(task for task in tasks if task.task_type == "asset_paid_generation")
    assert paid_task.parameters["cost_unit"] == "fake_credits"
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow_id) == []
    assert CostLedgerRepository(db).list_by_workflow(workflow_id) == []


def _cli_export_args(command: str, root: Path, workflow_id: str):
    if command == "report":
        return build_parser().parse_args(
            ["report", "--workflow", workflow_id, "--project", str(root), "--json"]
        )
    return build_parser().parse_args(
        ["asset", "export", workflow_id, "--project", str(root), "--json"]
    )


@pytest.mark.parametrize("command", ("report", "asset-export"))
def test_incomplete_v07_character_cli_export_reports_gate_without_advancing_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    root, db, workflow_id = _persist_private_character_graph(tmp_path, monkeypatch)
    workflow = WorkflowRepository(db).get(workflow_id)
    assert workflow is not None
    workflow.status = WorkflowStatus.BLOCKED
    WorkflowRepository(db).save(workflow)
    concept_review = next(
        task
        for task in TaskRepository(db).list_by_workflow(workflow_id)
        if task.task_type == "asset_concept_review"
    )
    TaskRepository(db).update_status(concept_review.id, TaskStatus.BLOCKED)
    database_file = root / ".gamefactory" / "state" / "factory.db"
    before = hashlib.sha256(database_file.read_bytes()).hexdigest()

    def legacy_export_must_not_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("V0.7 character export must not call the legacy exporter")

    monkeypatch.setattr(asset_evidence, "export_asset_evidence_bundle", legacy_export_must_not_run)
    monkeypatch.setattr(
        MigrationRunner,
        "apply_all",
        lambda *_args, **_kwargs: pytest.fail("character export must not run migrations"),
    )
    monkeypatch.setattr(
        cli_main,
        "_engine",
        lambda *_args, **_kwargs: pytest.fail("incomplete export must not create handlers"),
    )
    monkeypatch.setattr(
        provider_character_evidence,
        "revalidate_current_provider_character_evidence_bundle",
        lambda *_args, **_kwargs: pytest.fail("incomplete export must not revalidate a bundle"),
    )

    args = _cli_export_args(command, root, workflow_id)
    payload, code, message = _dispatch(args)

    assert not (root / ".gamefactory" / "reports" / workflow_id).exists()
    assert payload["status"] == "BLOCKED"
    assert payload["evidence_verification"] == "NOT_RUN_INCOMPLETE_WORKFLOW"
    assert payload["current_gate"]["task_id"] == concept_review.id
    assert "export did not" in message
    assert code != 0
    after = hashlib.sha256(database_file.read_bytes()).hexdigest()
    assert after == before


@pytest.mark.parametrize("command", ("report", "asset-export"))
def test_completed_v07_character_cli_export_uses_readonly_current_gate_and_preserves_cold_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    root, db, workflow_id = _persist_private_character_graph(tmp_path, monkeypatch)
    workflow = WorkflowRepository(db).get(workflow_id)
    assert workflow is not None
    workflow.status = WorkflowStatus.COMPLETED
    WorkflowRepository(db).save(workflow)
    database_file = root / ".gamefactory" / "state" / "factory.db"
    before = hashlib.sha256(database_file.read_bytes()).hexdigest()

    bundle_relative = ".gamefactory/assets/test_character/r001/provider-evidence/EXEC-fixture-a1"
    bundle = root / Path(bundle_relative)
    bundle.mkdir(parents=True)
    manifest_bytes = b"fixture manifest\n"
    (bundle / "manifest.json").write_bytes(manifest_bytes)
    (bundle / "index.html").write_text("<html>fixture review</html>", encoding="utf-8")
    artifact_id = "ARTIFACT-current-manifest"
    false_authentication_flags = {
        "human_identity_authenticated": False,
        "provider_execution_authenticated": False,
        "runtime_origin_authenticated": False,
        "capture_origin_authenticated": False,
        "currentness": "as_of_export_snapshot",
    }
    cold_result = {
        "status": "PASS",
        "product_ready": True,
        **false_authentication_flags,
    }
    engine_calls: list[str | None] = []
    api_calls: list[tuple[object, object]] = []

    class _EvidenceHandlerOwner:
        def publish(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("read-only export must never invoke the evidence handler")

        def current_provider_character_evidence_inputs(self, *_args: object) -> dict[str, object]:
            return {}

    owner = _EvidenceHandlerOwner()

    class _Registry:
        def get(self, task_type: str):
            assert task_type == "asset_v07_provider_evidence"
            return owner.publish

    monkeypatch.setattr(
        cli_main,
        "_engine",
        lambda _root, _db, **kwargs: (
            engine_calls.append(kwargs.get("asset_provider_name"))
            or SimpleNamespace(handler_registry=_Registry())
        ),
    )

    def revalidate(handlers: object, persisted_workflow: object) -> dict[str, object]:
        api_calls.append((handlers, persisted_workflow))
        return {
            **cold_result,
            "evidence_manifest": f"{bundle_relative}/manifest.json",
            "evidence_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "evidence_manifest_artifact_id": artifact_id,
        }

    monkeypatch.setattr(
        provider_character_evidence,
        "revalidate_current_provider_character_evidence_bundle",
        revalidate,
    )
    monkeypatch.setattr(
        asset_evidence,
        "export_asset_evidence_bundle",
        lambda *_args, **_kwargs: pytest.fail("V0.7 export must not fall back to legacy"),
    )
    monkeypatch.setattr(
        WorkflowEngine,
        "run_workflow",
        lambda *_args, **_kwargs: pytest.fail("completed export must not advance workflow"),
    )
    monkeypatch.setattr(
        MigrationRunner,
        "apply_all",
        lambda *_args, **_kwargs: pytest.fail("character export must not run migrations"),
    )

    args = _cli_export_args(command, root, workflow_id)
    payload, code, message = _dispatch(args)

    assert code == 0
    assert payload["status"] == "COMPLETED"
    assert payload["evidence_manifest_relative_path"] == f"{bundle_relative}/manifest.json"
    assert payload["evidence_manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert payload["evidence_manifest_artifact_id"] == artifact_id
    assert payload["review_html"] == str((bundle / "index.html").resolve())
    for key, expected in false_authentication_flags.items():
        assert payload["evidence_verification"][key] == expected
    assert payload["evidence_verification"]["evidence_manifest_artifact_id"] == artifact_id
    assert payload["workflow_mutated"] is False
    assert "revalidated read-only" in message
    assert engine_calls == ["fake"]
    assert len(api_calls) == 1
    assert api_calls[0][0] is owner
    assert api_calls[0][1].id == workflow_id
    assert hashlib.sha256(database_file.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("command", ("report", "asset-export"))
def test_completed_v07_character_cli_surfaces_current_gate_revalidation_failure_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    from gamefactory.core.domain.errors import ArtifactError

    root, db, workflow_id = _persist_private_character_graph(tmp_path, monkeypatch)
    workflow = WorkflowRepository(db).get(workflow_id)
    assert workflow is not None
    workflow.status = WorkflowStatus.COMPLETED
    WorkflowRepository(db).save(workflow)
    database_file = root / ".gamefactory" / "state" / "factory.db"
    before = hashlib.sha256(database_file.read_bytes()).hexdigest()

    class _EvidenceHandlerOwner:
        def publish(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("read-only export must not invoke publication")

        def current_provider_character_evidence_inputs(self, *_args: object) -> dict[str, object]:
            return {}

    owner = _EvidenceHandlerOwner()
    registry = SimpleNamespace(get=lambda _task_type: owner.publish)
    monkeypatch.setattr(
        cli_main,
        "_engine",
        lambda *_args, **_kwargs: SimpleNamespace(handler_registry=registry),
    )

    def reject_stale_bundle(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise ArtifactError("Latest provider-evidence attempt is no longer current")

    monkeypatch.setattr(
        provider_character_evidence,
        "revalidate_current_provider_character_evidence_bundle",
        reject_stale_bundle,
    )
    monkeypatch.setattr(
        asset_evidence,
        "export_asset_evidence_bundle",
        lambda *_args, **_kwargs: pytest.fail("failed V0.7 revalidation must not use legacy"),
    )
    monkeypatch.setattr(
        MigrationRunner,
        "apply_all",
        lambda *_args, **_kwargs: pytest.fail("character export must not run migrations"),
    )

    with pytest.raises(ArtifactError, match="no longer current"):
        _dispatch(_cli_export_args(command, root, workflow_id))

    assert hashlib.sha256(database_file.read_bytes()).hexdigest() == before
    assert not (root / ".gamefactory" / "reports" / workflow_id).exists()
