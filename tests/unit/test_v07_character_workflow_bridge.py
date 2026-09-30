"""Atomic paid-character workflow registration and binding checks."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
from gamefactory.adapters.persistence.character_graph import (
    _COMMON_FIELDS,
    CharacterGraphRepository,
)
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    EvidenceRepository,
    ExecutionRepository,
    ProjectRepository,
    ProviderOperationIntentRepository,
    QualityGateRepository,
    TaskRepository,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    builtin_v07_registry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import Execution, Project, Task, Workflow, WorkflowStatus
from gamefactory.workflows.asset_production import (
    AssetProductionHandlers,
    _embedded_profile_registry,
    _parse_task_specification,
    create_asset_production_workflow,
    is_immutable_paid_graph,
    is_v06_graph,
    is_v07_character_graph,
    register_asset_production_handlers,
)
from gamefactory.workflows.handlers import TaskHandlerRegistry

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _profile() -> AssetProfileV07:
    path = _FIXTURES / "profiles" / "character_profile_070.yml"
    profile_document = parse_profile_document_v07(path.read_text(encoding="utf-8"))
    document = profile_document.model_dump(mode="json")
    document["review_views"] = ["front", "rear", "left", "right", "three_quarter"]
    return AssetProfileV07(parse_profile_document_v07(document))


def _profile_with_review_view_count(count: int) -> AssetProfileV07:
    document = _profile().document.model_dump(mode="json")
    additional = ["side", "top", "three_quarter_front", "three_quarter_rear"]
    document["review_views"] = [
        "front",
        "rear",
        "left",
        "right",
        "three_quarter",
        *additional[: count - 5],
    ]
    return AssetProfileV07(parse_profile_document_v07(document))


def _registry(profile: AssetProfileV07) -> ProfileRegistry:
    return ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))


def _spec(profile: AssetProfileV07, *, asset_id: str = "character_hero_01") -> Any:
    return parse_asset_specification_v07(
        {
            "schema_version": "0.7.0",
            "asset_id": asset_id,
            "category": "character",
            "profile": profile.profile_id,
            "profile_version": 1,
            "intent": "Fixture unrigged static character for workflow bridge tests",
            "source_kind": "provider_generated",
            "dimensions": {"width_m": 0.6, "depth_m": 0.5, "height_m": 1.8},
            "origin_policy": "bottom_center",
            "geometry_budget": {"max_triangles_lod0": 20000, "max_triangles_lod1": 10000},
            "collider": {
                "policy": "capsule",
                "capsule": {"radius_m": 0.25, "height_m": 1.8},
            },
            "lod_policy": "lod0_lod1",
        },
        registry=_registry(profile),
    )


def _setup(root: Path) -> tuple[Database, str, AssetProfileV07]:
    root.mkdir(parents=True, exist_ok=True)
    db = Database(root / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    project_id = "character-bridge-project"
    ProjectRepository(db).save(Project(project_id, "Character bridge fixture", "godot", str(root)))
    profile = _profile()
    return db, project_id, profile


def _concept(root: Path, *, color: tuple[int, int, int] = (20, 40, 60)) -> tuple[Path, Path]:
    path = root / "concepts" / "character.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    output = BytesIO()
    Image.new("RGB", (24, 24), color).save(output, format="PNG")
    path.write_bytes(output.getvalue())
    provenance = root / "concepts" / "character-provenance.json"
    provenance.write_text(json.dumps({"fixture": "isolated-workflow-test"}), encoding="utf-8")
    return path, provenance


def _create(
    root: Path,
    db: Database,
    project_id: str,
    profile: AssetProfileV07,
    *,
    workflow_id: str,
    color: tuple[int, int, int] = (20, 40, 60),
) -> tuple[Any, list[Any]]:
    concept, provenance = _concept(root, color=color)
    return create_asset_production_workflow(
        project_id,
        root,
        _spec(profile),
        concept,
        provenance,
        provider_name="fake",
        provider_estimate=0.0,
        workflow_id=workflow_id,
        revision_repository=AssetRevisionRepository(db),
        profile_registry=_registry(profile),
    )


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@pytest.mark.parametrize("view_count", [6, 7, 8])
def test_paid_creation_rejects_unpublished_capture_counts_before_state_changes(
    tmp_path: Path, view_count: int
) -> None:
    root = tmp_path / "project"
    db, project_id, _ = _setup(root)
    profile = _profile_with_review_view_count(view_count)
    concept, provenance = _concept(root)
    with pytest.raises(ValidationError, match="Paid V0.7 processing supports only"):
        create_asset_production_workflow(
            project_id,
            root,
            _spec(profile),
            concept,
            provenance,
            provider_name="fake",
            provider_estimate=0,
            workflow_id=f"character-invalid-views-{view_count}",
            revision_repository=AssetRevisionRepository(db),
            profile_registry=_registry(profile),
        )
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM asset_revisions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_character_workflow_graph_is_atomic_and_exact_retry_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "project"
    db, project_id, profile = _setup(root)

    workflow, tasks = _create(root, db, project_id, profile, workflow_id="character-atomic")
    assert len(tasks) == 10
    assert [task.task_type for task in tasks] == [
        "asset_prepare",
        "asset_concept_review",
        "asset_paid_request_snapshot",
        "asset_production_readiness",
        "asset_paid_generation",
        "asset_process",
        "asset_validate",
        "asset_godot",
        "asset_final_review",
        "asset_v07_provider_evidence",
    ]
    assert is_v07_character_graph(tasks[0])
    assert is_immutable_paid_graph(tasks[0])
    assert not is_v06_graph(tasks[0])
    assert workflow.status.value == "PENDING"
    assert tasks[0].parameters["revision_number"] == 1
    assert tasks[0].parameters["profile_document_hash"]
    assert sum(task.cost_class.value == "PAID" for task in tasks) == 1

    second_workflow, second_tasks = _create(
        root, db, project_id, profile, workflow_id="character-atomic"
    )
    assert second_workflow.id == workflow.id
    assert [task.id for task in second_tasks] == [task.id for task in tasks]
    conn = db.connect()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM workflows WHERE id=?", (workflow.id,)).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM asset_revisions WHERE workflow_id=?", (workflow.id,)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE workflow_id=?", (workflow.id,)
            ).fetchone()[0]
            == 10
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_type='Workflow' AND entity_id=? AND action='REGISTERED'",
                (workflow.id,),
            ).fetchone()[0]
            == 1
        )
        for table in (
            "provider_operation_intents",
            "provider_invocations",
            "cost_ledger",
            "paid_request_snapshots",
            "production_readiness_reports",
        ):
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE workflow_id=?", (workflow.id,)
                ).fetchone()[0]
                == 0
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    "trigger_sql",
    [
        "CREATE TRIGGER fail_revision BEFORE INSERT ON asset_revisions BEGIN SELECT RAISE(ABORT, 'fixture revision failure'); END",
        "CREATE TRIGGER fail_task BEFORE INSERT ON tasks WHEN NEW.task_type='asset_validate' BEGIN SELECT RAISE(ABORT, 'fixture task failure'); END",
        "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events WHEN NEW.action='REGISTERED' BEGIN SELECT RAISE(ABORT, 'fixture audit failure'); END",
    ],
)
def test_graph_write_failures_rollback_workflow_revision_and_tasks(
    tmp_path: Path, trigger_sql: str
) -> None:
    root = tmp_path / "project"
    db, project_id, profile = _setup(root)
    conn = db.connect()
    try:
        conn.execute(trigger_sql)
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(sqlite3.DatabaseError):
        _create(root, db, project_id, profile, workflow_id="character-rollback")
    conn = db.connect()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM workflows WHERE id='character-rollback'").fetchone()[
                0
            ]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM asset_revisions WHERE asset_id='character_hero_01'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE workflow_id='character-rollback'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id='character-rollback'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()

    conn = db.connect()
    try:
        conn.execute("DROP TRIGGER IF EXISTS fail_revision")
        conn.execute("DROP TRIGGER IF EXISTS fail_task")
        conn.execute("DROP TRIGGER IF EXISTS fail_audit")
        conn.commit()
    finally:
        conn.close()
    workflow, tasks = _create(root, db, project_id, profile, workflow_id="character-rollback")
    assert tasks[0].parameters["revision_number"] == 1
    assert workflow.id == "character-rollback"


def test_character_revision_allocations_serialize_concurrent_workflows(tmp_path: Path) -> None:
    root = tmp_path / "project"
    db, project_id, profile = _setup(root)

    def create(workflow_id: str) -> int:
        _workflow, tasks = _create(root, db, project_id, profile, workflow_id=workflow_id)
        return tasks[0].parameters["revision_number"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        revisions = sorted(pool.map(create, ("character-race-a", "character-race-b")))
    assert revisions == [1, 2]


def test_character_workflow_id_conflict_fails_without_consuming_a_revision(tmp_path: Path) -> None:
    root = tmp_path / "project"
    db, project_id, profile = _setup(root)
    _create(root, db, project_id, profile, workflow_id="character-conflict")

    with pytest.raises(ValidationError, match="conflicting pinned inputs"):
        _create(
            root,
            db,
            project_id,
            profile,
            workflow_id="character-conflict",
            color=(200, 10, 90),
        )
    _workflow, next_tasks = _create(root, db, project_id, profile, workflow_id="character-next")
    assert next_tasks[0].parameters["revision_number"] == 2


@pytest.mark.parametrize(
    "contract_mutation",
    [
        "area_body",
        "ray_disabled",
        "area_enabled",
        "rig_allowed",
        "animation_allowed",
        "review_view_missing",
        "review_view_unhashable",
        "review_view_count_6",
        "review_view_count_7",
        "review_view_count_8",
    ],
)
def test_atomic_graph_repository_rejects_noncharacter_runtime_contracts_before_commit(
    tmp_path: Path, contract_mutation: str
) -> None:
    root = tmp_path / "project"
    db, project_id, profile = _setup(root)
    original_workflow, original_tasks = _create(
        root, db, project_id, profile, workflow_id="character-contract-base"
    )
    common = original_tasks[0].parameters
    invalid_document = copy.deepcopy(common["profile_document"])
    if contract_mutation == "area_body":
        invalid_document["godot"].update(
            {"body_kind": "area", "require_ray_hit": False, "require_area": True}
        )
    elif contract_mutation == "ray_disabled":
        invalid_document["godot"]["require_ray_hit"] = False
    elif contract_mutation == "area_enabled":
        invalid_document["godot"].update(
            {"body_kind": "area", "require_ray_hit": False, "require_area": True}
        )
    elif contract_mutation == "rig_allowed":
        invalid_document["processing"]["rig_forbidden"] = False
    elif contract_mutation == "animation_allowed":
        invalid_document["processing"]["animation_forbidden"] = False
    elif contract_mutation == "review_view_missing":
        invalid_document["review_views"].remove("right")
    elif contract_mutation.startswith("review_view_count_"):
        count = int(contract_mutation.rsplit("_", 1)[1])
        invalid_document["review_views"] = [
            "front",
            "rear",
            "left",
            "right",
            "three_quarter",
            *["side", "top", "three_quarter_front"][: count - 5],
        ]
    else:
        invalid_document["review_views"][0] = {"unhashable": "view"}

    bad_hash = _canonical_hash(invalid_document)
    workflow_id = "character-graph-bypass"
    workflow = Workflow(
        workflow_id,
        project_id,
        "Invalid character graph",
        status=WorkflowStatus.PENDING,
    )

    def task_factory(revision: Any) -> list[Task]:
        tasks: list[Task] = []
        for old in original_tasks:
            suffix = old.id.removeprefix(f"{original_workflow.id}-")
            params = copy.deepcopy(old.parameters)
            params.update(
                {
                    "workflow_id": workflow_id,
                    "revision_number": revision.revision_number,
                    "asset_dir": (
                        f".gamefactory/assets/{revision.asset_id}/r{revision.revision_number:03d}"
                    ),
                    "profile_document": copy.deepcopy(invalid_document),
                    "profile_document_hash": bad_hash,
                }
            )
            tasks.append(
                Task(
                    id=f"{workflow_id}-{suffix}",
                    workflow_id=workflow_id,
                    name=old.name,
                    task_type=old.task_type,
                    cost_class=old.cost_class,
                    depends_on=[
                        f"{workflow_id}-{dependency.removeprefix(f'{original_workflow.id}-')}"
                        for dependency in old.depends_on
                    ],
                    parameters=params,
                    max_retries=old.max_retries,
                    timeout_seconds=old.timeout_seconds,
                )
            )
        return tasks

    with pytest.raises(ValidationError, match="canonical asset revision"):
        CharacterGraphRepository(db).create_graph(
            workflow=workflow,
            asset_id=common["asset_id"],
            spec_hash=common["specification_hash"],
            profile_id=common["profile_id"],
            profile_version=common["profile_version"],
            concept_hash=common["concept_source_hash"],
            task_factory=task_factory,
        )
    conn = db.connect()
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM workflows WHERE id=?", (workflow_id,)).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE workflow_id=?", (workflow_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM asset_revisions WHERE asset_id=?", (common["asset_id"],)
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity_id=?", (workflow_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_embedded_profile_document_is_never_a_paid_trust_registration(tmp_path: Path) -> None:
    root = tmp_path / "project"
    db, project_id, profile = _setup(root)
    workflow, tasks = _create(root, db, project_id, profile, workflow_id="character-profile-trust")
    task = tasks[0]
    embedded = _embedded_profile_registry(task.parameters)
    assert _parse_task_specification(task.parameters, embedded).bound_profile() == profile
    with pytest.raises(ValidationError, match="not registered|UNSUPPORTED"):
        _parse_task_specification(task.parameters, builtin_v07_registry())
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM provider_operation_intents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM paid_request_snapshots").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("rehash_document", [False, True])
def test_profile_document_bool_int_tampering_is_rejected_before_paid_state(
    tmp_path: Path, rehash_document: bool
) -> None:
    root = tmp_path / "project"
    db, project_id, profile = _setup(root)
    _workflow, tasks = _create(root, db, project_id, profile, workflow_id="character-doc-tamper")
    params = copy.deepcopy(tasks[0].parameters)
    params["profile_document"]["processing"]["rig_forbidden"] = 1
    if rehash_document:
        params["profile_document_hash"] = _canonical_hash(params["profile_document"])
    with pytest.raises(ValidationError):
        display_registry = _embedded_profile_registry(params)
        _parse_task_specification(params, display_registry)
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM provider_operation_intents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM paid_request_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize(
    "mutation",
    ["graph_version", "revision_bool", "dependencies", "cost_class", *_COMMON_FIELDS],
)
def test_future_graph_tampering_blocks_paid_gate_without_provider_or_snapshot_side_effects(
    tmp_path: Path, mutation: str
) -> None:
    root = tmp_path / "project"
    db, project_id, profile = _setup(root)
    workflow, tasks = _create(root, db, project_id, profile, workflow_id="character-future-tamper")
    provider = FakeAssetGenerationProvider()
    handlers = AssetProductionHandlers(
        root,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ProviderOperationIntentRepository(db),
        EvidenceRepository(db),
        QualityGateRepository(db),
        ExecutionRepository(db),
        ArtifactManager(root),
        provider,
        profile_registry=_registry(profile),
    )
    target = tasks[7]
    conn = db.connect()
    try:
        if mutation in {"graph_version", "revision_bool", *_COMMON_FIELDS}:
            row = conn.execute(
                "SELECT parameters_json FROM tasks WHERE id=?", (target.id,)
            ).fetchone()
            params = json.loads(row["parameters_json"])
            if mutation == "graph_version":
                params["graph_version"] = "0.6.0"
            elif mutation == "revision_bool":
                params["revision_number"] = True
            else:
                value = params[mutation]
                if isinstance(value, dict):
                    value["tamper"] = True
                elif isinstance(value, list):
                    value.append("tamper")
                elif isinstance(value, str):
                    params[mutation] = value + "-tamper"
                elif value is None:
                    params[mutation] = "tamper"
                else:
                    params[mutation] = value + 1
            conn.execute(
                "UPDATE tasks SET parameters_json=? WHERE id=?",
                (json.dumps(params, sort_keys=True), target.id),
            )
        elif mutation == "dependencies":
            conn.execute(
                "UPDATE tasks SET depends_on_json=? WHERE id=?",
                (json.dumps([tasks[5].id]), target.id),
            )
        else:
            conn.execute("UPDATE tasks SET cost_class='PAID' WHERE id=?", (target.id,))
        conn.commit()
    finally:
        conn.close()

    current_paid_task = TaskRepository(db).get(tasks[4].id)
    assert current_paid_task is not None
    with pytest.raises(ValidationError):
        handlers._guard_paid_task("tampered graph test", workflow, current_paid_task)
    assert provider.invocation_count == 0
    conn = db.connect()
    try:
        for table in (
            "provider_operation_intents",
            "provider_invocations",
            "cost_ledger",
            "paid_request_snapshots",
            "production_readiness_reports",
        ):
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE workflow_id=?", (workflow.id,)
                ).fetchone()[0]
                == 0
            )
    finally:
        conn.close()


def test_provider_evidence_stage_stays_blocked_and_paid_gates_remain_mandatory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    db, project_id, profile = _setup(root)
    workflow, tasks = _create(root, db, project_id, profile, workflow_id="character-evidence-gate")
    provider = FakeAssetGenerationProvider()
    handlers = AssetProductionHandlers(
        root,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ProviderOperationIntentRepository(db),
        EvidenceRepository(db),
        QualityGateRepository(db),
        ExecutionRepository(db),
        ArtifactManager(root),
        provider,
        profile_registry=_registry(profile),
    )
    registry = TaskHandlerRegistry()
    register_asset_production_handlers(registry, handlers)
    assert registry.metadata(tasks[1].task_type).mandatory_approval_type == "concept_review"
    assert registry.metadata(tasks[4].task_type).mandatory_approval_type == "paid_generation"
    assert registry.metadata(tasks[8].task_type).mandatory_approval_type == "final_visual_review"

    with pytest.raises(
        ArtifactError,
        match="execution identity|RUNNING or COMPLETED|exactly one active concept",
    ):
        handlers.publish_provider_v07_evidence_bundle(
            workflow,
            tasks[-1],
            Execution("fixture-evidence-attempt", tasks[-1].id, 1),
        )
    assert provider.invocation_count == 0
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM provider_operation_intents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM provider_invocations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM paid_request_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM production_readiness_reports").fetchone()[0] == 0
    finally:
        conn.close()
