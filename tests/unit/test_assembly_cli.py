"""Local V0.7 assembly CLI safety and fresh-engine lifecycle tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image
from test_assembly_ingest import _generate_valid_assembly_glb, _profile, _spec

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    TaskRepository,
)
from gamefactory.cli import main as cli
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.domain.asset_contracts import AssetSpecificationV07
from gamefactory.core.domain.asset_profiles import AssetProfileV07, ProfileRegistry


def _invoke(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict]:
    code = cli.main([*args, "--json"])
    captured = capsys.readouterr()
    stream = captured.out if code == 0 or captured.out else captured.err
    assert stream.strip(), captured.err
    return code, json.loads(stream)


def _table_count(db: Database, table: str) -> int:
    # Table names are fixed by callers in this focused regression test.
    with db.connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


def _make_inputs(
    root: Path,
) -> tuple[AssetProfileV07, AssetSpecificationV07, Path, str]:
    profile = _profile()
    spec = _spec(profile)
    spec_path = root / "assembly-spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec.model_dump(mode="json")), encoding="utf-8")
    source_path = _generate_valid_assembly_glb(root / "source.glb")
    concept_path = root / "concept.png"
    Image.new("RGB", (24, 24), (40, 80, 120)).save(concept_path, format="PNG")
    provenance_path = root / "concept-provenance.json"
    provenance_path.write_text('{"schema_version":1,"origin":"cli-fixture"}', encoding="utf-8")
    ConfigLoader.init_project(root, project_name="CLI assembly test")
    return profile, spec, source_path, hashlib.sha256(concept_path.read_bytes()).hexdigest()


def test_paid_provider_flags_are_rejected_before_database_or_provider_construction(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    ConfigLoader.init_project(root)
    db = Database(root / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()

    def forbidden_provider(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("paid provider constructor must not be reached")

    monkeypatch.setattr(cli, "MeshyAssetGenerationProvider", forbidden_provider)
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                "--json",
                "--project",
                str(root),
                "assembly",
                "create",
                "--spec",
                "spec.yaml",
                "--package",
                "package",
                "--expected-provenance-sha256",
                "0" * 64,
                "--concept",
                "concept.png",
                "--concept-provenance",
                "concept.json",
                "--provider",
                "meshy",
            ]
        )
    assert error.value.code == 1
    assert "USAGE_ERROR" in capsys.readouterr().err
    assert _table_count(db, "provider_invocations") == 0
    assert _table_count(db, "provider_operation_intents") == 0
    assert _table_count(db, "cost_ledger") == 0

def test_public_cli_rejects_unavailable_v07_profile_before_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    _, _, source, _ = _make_inputs(root)
    db = Database(root / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()

    code, error = _invoke(
        capsys,
        "--project",
        str(root),
        "assembly",
        "ingest",
        "--spec",
        "assembly-spec.yaml",
        "--source",
        source.name,
        "--package",
        "sources/package",
        "--actor",
        "operator",
        "--reason",
        "fixture import",
        "--authoring-tool",
        "Blender",
        "--authoring-tool-version",
        "5.2",
        "--source-front=-Z",
    )
    assert code == 1
    assert "profile" in error["message"].lower()
    assert not (root / "sources" / "package").exists()
    assert _table_count(db, "provider_invocations") == 0
    assert _table_count(db, "provider_operation_intents") == 0
    assert _table_count(db, "cost_ledger") == 0


def test_ingest_verify_create_and_human_gates_rehydrate_in_fresh_cli_engines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    profile, spec, source, concept_hash = _make_inputs(root)
    registry = ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))
    monkeypatch.setattr(cli, "_assembly_profile_registry", lambda: registry)

    code, ingested = _invoke(
        capsys,
        "--project",
        str(root),
        "assembly",
        "ingest",
        "--spec",
        "assembly-spec.yaml",
        "--source",
        source.name,
        "--package",
        "sources/package",
        "--actor",
        "test-operator",
        "--reason",
        "retain authored fixture",
        "--authoring-tool",
        "Blender",
        "--authoring-tool-version",
        "5.2.1",
        "--source-front=-Z",
    )
    assert code == 0 and ingested["status"] == "INGESTED"
    pin = ingested["provenance_sha256"]

    code, verified = _invoke(
        capsys,
        "--project",
        str(root),
        "assembly",
        "verify",
        "--spec",
        "assembly-spec.yaml",
        "--package",
        "sources/package",
        "--expected-provenance-sha256",
        pin,
    )
    assert code == 0 and verified["status"] == "VERIFIED"

    code, failed_pin = _invoke(
        capsys,
        "--project",
        str(root),
        "assembly",
        "verify",
        "--spec",
        "assembly-spec.yaml",
        "--package",
        "sources/package",
        "--expected-provenance-sha256",
        "0" * 64,
    )
    assert code == 1 and "provenance" in failed_pin["message"].lower()

    common_create = (
        "--project",
        str(root),
        "assembly",
        "create",
        "--spec",
        "assembly-spec.yaml",
        "--package",
        "sources/package",
        "--expected-provenance-sha256",
        pin,
        "--concept",
        "concept.png",
        "--concept-provenance",
        "concept-provenance.json",
        "--workflow-id",
        "cli-assembly-fixed-id",
    )
    code, created = _invoke(capsys, *common_create)
    assert code == 3 and created["status"] == "BLOCKED"
    first_approval = created["pending_approval_id"]
    assert first_approval

    db = Database(root / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    assert len(AssetRevisionRepository(db).list_by_workflow("cli-assembly-fixed-id")) == 1
    approvals = ApprovalRepository(db).list_by_workflow("cli-assembly-fixed-id")
    assert len(approvals) == 1 and approvals[0].approval_type == "concept_review"

    # Exact retries reuse the same immutable revision and pending human checkpoint.
    code, retried = _invoke(capsys, *common_create)
    assert code == 3 and retried["pending_approval_id"] == first_approval
    assert len(AssetRevisionRepository(db).list_by_workflow("cli-assembly-fixed-id")) == 1

    # A new CLI invocation rebuilds the engine/handlers from persisted pins.
    code, decision = _invoke(
        capsys,
        "--project",
        str(root),
        "approve",
        first_approval,
        "--actor",
        "human-reviewer",
        "--comment",
        "concept inspected",
    )
    assert code == 0 and decision["status"] == "APPROVED"
    code, resumed = _invoke(capsys, "--project", str(root), "resume", "cli-assembly-fixed-id")
    assert code == 3 and resumed["status"] == "BLOCKED"
    approvals = ApprovalRepository(db).list_by_workflow("cli-assembly-fixed-id")
    assert len(approvals) == 2
    assert {item.approval_type for item in approvals} == {"concept_review", "source_review"}
    assert next(item for item in approvals if item.approval_type == "source_review").status.value == "PENDING"
    code, export_gate = _invoke(
        capsys,
        "--project",
        str(root),
        "assembly",
        "export",
        "cli-assembly-fixed-id",
    )
    assert code == 3 and export_gate["status"] == "BLOCKED"
    assert not any(
        row.artifact_type == "assembly-evidence-manifest"
        for row in ArtifactRepository(db).list_by_workflow("cli-assembly-fixed-id")
    )

    # Mutating only concept input conflicts with the fixed workflow ID and cannot allocate a revision.
    changed_concept = root / "concept-changed.png"
    Image.new("RGB", (24, 24), (200, 50, 20)).save(changed_concept, format="PNG")
    assert hashlib.sha256(changed_concept.read_bytes()).hexdigest() != concept_hash
    conflicting = list(common_create)
    concept_index = conflicting.index("concept.png")
    conflicting[concept_index] = changed_concept.name
    code, conflict = _invoke(capsys, *conflicting)
    assert code == 1 and "workflow" in conflict["message"].lower()
    assert len(AssetRevisionRepository(db).list_by_workflow("cli-assembly-fixed-id")) == 1

    assert _table_count(db, "provider_invocations") == 0
    assert _table_count(db, "provider_operation_intents") == 0
    assert _table_count(db, "cost_ledger") == 0


def test_completed_export_rejects_missing_manifest_without_rerunning_workflow(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    profile, _specification, source, _concept_hash = _make_inputs(root)
    registry = ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))
    monkeypatch.setattr(cli, "_assembly_profile_registry", lambda: registry)
    from gamefactory.adapters.assets.assembly_ingest import ingest_assembly_source

    retained = ingest_assembly_source(
        source_glb_path=source,
        managed_root=root,
        relative_package_dir="sources/package",
        spec=_spec(profile),
        authoring_tool_name="Fixture Blender",
        authoring_tool_version="5.2.1",
        source_front="-Z",
        actor="test-operator",
        reason="isolated negative test",
    )
    code, created = _invoke(
        capsys,
        "--project",
        str(root),
        "assembly",
        "create",
        "--spec",
        "assembly-spec.yaml",
        "--package",
        "sources/package",
        "--expected-provenance-sha256",
        retained.retained_provenance_sha256,
        "--concept",
        "concept.png",
        "--concept-provenance",
        "concept-provenance.json",
        "--workflow-id",
        "incomplete-fake-completed",
    )
    assert code == 3 and created["status"] == "BLOCKED"
    db = Database(root / ".gamefactory" / "state" / "factory.db")
    with db.connect() as conn:
        conn.execute(
            "UPDATE workflows SET status = 'COMPLETED' WHERE id = ?",
            ("incomplete-fake-completed",),
        )
    db_files_before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in db.db_path.parent.glob("factory.db*")
        if path.is_file() and not path.name.endswith("-shm")
    }
    execution_count_before = _table_count(db, "executions")
    code, invalid_completed = _invoke(
        capsys,
        "--project",
        str(root),
        "assembly",
        "export",
        "incomplete-fake-completed",
    )
    assert code == 1 and "manifest" in invalid_completed["message"].lower()
    assert execution_count_before == _table_count(db, "executions")
    assert db_files_before == {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in db.db_path.parent.glob("factory.db*")
        if path.is_file() and not path.name.endswith("-shm")
    }


def test_rehydration_rejects_tampered_task_binding_before_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    profile, _specification, source, _concept_hash = _make_inputs(root)
    monkeypatch.setattr(
        cli,
        "_assembly_profile_registry",
        lambda: ProfileRegistry(available=(), unsupported=(), available_v07=(profile,)),
    )
    from gamefactory.adapters.assets.assembly_ingest import ingest_assembly_source

    retained = ingest_assembly_source(
        source_glb_path=source,
        managed_root=root,
        relative_package_dir="sources/package",
        spec=_spec(profile),
        authoring_tool_name="Blender",
        authoring_tool_version="5.2.1",
        source_front="-Z",
        actor="operator",
        reason="fixture source",
    )
    (root / "concept.png").touch()
    Image.new("RGB", (24, 24), (1, 2, 3)).save(root / "concept.png", format="PNG")
    (root / "concept-provenance.json").write_text('{"schema_version":1}', encoding="utf-8")
    # Stop at the concept gate and ensure prepare has persisted immutable parameters.
    code, _created = _invoke(
        capsys,
        "--project",
        str(root),
        "assembly",
        "create",
        "--spec",
        "assembly-spec.yaml",
        "--package",
        "sources/package",
        "--expected-provenance-sha256",
        retained.retained_provenance_sha256,
        "--concept",
        "concept.png",
        "--concept-provenance",
        "concept-provenance.json",
        "--workflow-id",
        "cli-tamper-check",
    )
    assert code == 3
    db = Database(root / ".gamefactory" / "state" / "factory.db")
    altered_profile = _profile(pivot_tolerance_m=0.005)
    monkeypatch.setattr(
        cli,
        "_assembly_profile_registry",
        lambda: ProfileRegistry(available=(), unsupported=(), available_v07=(altered_profile,)),
    )
    code, profile_error = _invoke(capsys, "--project", str(root), "resume", "cli-tamper-check")
    assert code == 1 and "tampered" in profile_error["message"]
    monkeypatch.setattr(
        cli,
        "_assembly_profile_registry",
        lambda: ProfileRegistry(available=(), unsupported=(), available_v07=(profile,)),
    )
    task = TaskRepository(db).list_by_workflow("cli-tamper-check")[0]
    parameters = dict(task.parameters)
    parameters["source_glb_sha256"] = "f" * 64
    with db.connect() as conn:
        conn.execute(
            "UPDATE tasks SET parameters_json = ? WHERE id = ?",
            (json.dumps(parameters, sort_keys=True), task.id),
        )

    code, error = _invoke(capsys, "--project", str(root), "resume", "cli-tamper-check")
    assert code == 1 and "parameters conflict" in error["message"]


def test_atomic_v07_asset_graph_skips_duplicate_engine_registration() -> None:
    class EngineSpy:
        def __init__(self) -> None:
            self.registered: list[tuple[object, object, dict[str, object]]] = []

        def register_workflow(self, workflow: object, tasks: object, **kwargs: object) -> None:
            self.registered.append((workflow, tasks, kwargs))

    workflow = object()
    task_v07 = SimpleNamespace(parameters={"graph_version": "0.7.0"})
    task_v06 = SimpleNamespace(parameters={"graph_version": "0.6.0"})

    v07_engine = EngineSpy()
    cli._register_asset_graph_if_needed(v07_engine, workflow, [task_v07])
    assert v07_engine.registered == []

    legacy_engine = EngineSpy()
    cli._register_asset_graph_if_needed(legacy_engine, workflow, [task_v06])
    assert legacy_engine.registered == [
        (workflow, [task_v06], {"allow_existing_empty_placeholder": True})
    ]
