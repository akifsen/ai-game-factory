"""In-revision concept versioning and replacement service."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.images.concept_ingest import (
    MAX_PROVENANCE_SIDECAR_BYTES,
    _read_bounded,
    ingest_concept_image,
    verify_png_image,
)
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    AuditLogRepository,
    ConceptVersionRecord,
    ConceptVersionRepository,
    ExecutionRepository,
    PaidRequestSnapshotRepository,
    ProductionReadinessRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import (
    ApprovalStatus,
    AuditEvent,
    TaskStatus,
    WorkflowStatus,
    generate_id,
    utc_now_iso,
)
from gamefactory.core.execution.locks import ExecutionLock
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.workflows.asset_production import is_immutable_paid_graph, is_v07_character_graph
from gamefactory.workflows.engine import WorkflowEngine


def replace_concept(
    root: Path | str,
    db: Database,
    engine: WorkflowEngine,
    workflow_id: str,
    concept_png: Path | str,
    provenance_json: Path | str,
    *,
    actor: str,
    reason: str,
    concept_source_type: str = "imported",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Replace an asset concept version inside the same asset revision before paid production.

    Holds the workflow execution lock so the paid-boundary checks and the mutations
    cannot interleave with a concurrent run of the same workflow.
    """
    with ExecutionLock(engine.locks_dir, workflow_id).hold():
        return _replace_concept_locked(
            root,
            db,
            engine,
            workflow_id,
            concept_png,
            provenance_json,
            actor=actor,
            reason=reason,
            concept_source_type=concept_source_type,
            dry_run=dry_run,
        )


def _replace_concept_locked(
    root: Path | str,
    db: Database,
    engine: WorkflowEngine,
    workflow_id: str,
    concept_png: Path | str,
    provenance_json: Path | str,
    *,
    actor: str,
    reason: str,
    concept_source_type: str = "imported",
    dry_run: bool = False,
) -> dict[str, Any]:
    if not actor or not actor.strip():
        raise ValidationError("Actor is required for concept replacement")
    if not reason or not reason.strip():
        raise ValidationError("Reason is required for concept replacement")

    project_root = Path(root).resolve(strict=True)
    concept_src = Path(concept_png)
    if not concept_src.is_absolute():
        concept_src = (project_root / concept_src).resolve()
    provenance_src = Path(provenance_json)
    if not provenance_src.is_absolute():
        provenance_src = (project_root / provenance_src).resolve()

    if not concept_src.is_file():
        raise ValidationError(f"Concept image file does not exist: {concept_src}")
    if not provenance_src.is_file():
        raise ValidationError(f"Concept provenance file does not exist: {provenance_src}")

    wf_repo = WorkflowRepository(db)
    workflow = wf_repo.get(workflow_id)
    if workflow is None:
        raise ValidationError(f"Workflow '{workflow_id}' not found")
    if workflow.status == WorkflowStatus.COMPLETED:
        raise ValidationError(
            "paid production has begun; allocate a new asset revision (workflow is completed)"
        )

    task_repo = TaskRepository(db)
    tasks = task_repo.list_by_workflow(workflow_id)
    if not tasks:
        raise ValidationError(f"Workflow '{workflow_id}' has no tasks")

    concept_task = next((t for t in tasks if t.task_type == "asset_concept_review"), None)
    if concept_task is None:
        raise ValidationError(
            "paid production has begun; allocate a new asset revision (missing concept review task)"
        )
    if concept_task.status == TaskStatus.FAILED:
        raise ValidationError(
            "paid production has begun; allocate a new asset revision (concept review task is failed; rejected is terminal)"
        )

    # Safety checks:
    # 1. Workflow is not a V0.6 asset graph (legacy -> refuse: new revision required)
    if not is_immutable_paid_graph(concept_task) or not any(
        t.task_type == "asset_paid_request_snapshot" for t in tasks
    ):
        raise ValidationError(
            "paid production has begun; allocate a new asset revision (workflow is not a V0.6 asset graph)"
        )

    # 2. Any provider_operation_intents row for the workflow
    intent_repo = ProviderOperationIntentRepository(db)
    if intent_repo.list_by_workflow(workflow_id):
        raise ValidationError(
            "paid production has begun; allocate a new asset revision (provider operation intent exists)"
        )

    # 3. Any provider_invocations row for the workflow
    conn = db.connect()
    try:
        inv_row = conn.execute(
            "SELECT 1 FROM provider_invocations WHERE workflow_id = ? LIMIT 1;",
            (workflow_id,),
        ).fetchone()
        if inv_row is not None:
            raise ValidationError(
                "paid production has begun; allocate a new asset revision (provider invocation exists)"
            )
    finally:
        conn.close()

    # 4. Any execution row for the PAID-GENERATION task
    paid_task = next((t for t in tasks if t.task_type == "asset_paid_generation"), None)
    if paid_task is not None:
        exec_repo = ExecutionRepository(db)
        if exec_repo.list_by_task(paid_task.id):
            raise ValidationError(
                "paid production has begun; allocate a new asset revision (execution exists for paid generation)"
            )

    # 5. Any APPROVED paid_generation approval for the workflow
    app_repo = ApprovalRepository(db)
    approvals = app_repo.list_by_workflow(workflow_id)
    if any(
        a.approval_type == "paid_generation" and a.status == ApprovalStatus.APPROVED
        for a in approvals
    ):
        raise ValidationError(
            "paid production has begun; allocate a new asset revision (paid generation is approved)"
        )

    # 6. Revision: raw_glb_hash is not NULL
    asset_id = str(concept_task.parameters["asset_id"])
    revision_number = int(concept_task.parameters["revision_number"])
    rev_repo = AssetRevisionRepository(db)
    revision = rev_repo.get(asset_id, revision_number)
    if revision is None:
        raise ValidationError(f"Asset revision {revision_number} for '{asset_id}' not found")
    if revision.raw_glb_hash is not None:
        raise ValidationError(
            "paid production has begun; allocate a new asset revision (raw GLB is present)"
        )

    # Check pre-paid and post-paid tasks:
    pre_paid_task_types = [
        "asset_concept_review",
        "asset_paid_request_snapshot",
        "asset_production_readiness",
        "asset_paid_generation",
    ]
    pre_paid_tasks = [t for t in tasks if t.task_type in pre_paid_task_types]
    if len(pre_paid_tasks) != 4:
        raise ValidationError(
            "paid production has begun; allocate a new asset revision (missing expected pre-paid tasks)"
        )

    allowed_pre_paid_statuses = {
        TaskStatus.PENDING,
        TaskStatus.BLOCKED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    }
    for t in pre_paid_tasks:
        if t.status not in allowed_pre_paid_statuses:
            raise ValidationError(
                f"paid production has begun; allocate a new asset revision (pre-paid task {t.id} status is {t.status.value})"
            )

    post_paid_types = {
        "asset_process",
        "asset_validate",
        "asset_godot",
        "asset_final_review",
        "asset_v07_provider_evidence",
        "record_evidence",
    }
    for t in tasks:
        if t.task_type in post_paid_types and t.status != TaskStatus.PENDING:
            raise ValidationError(
                f"paid production has begun; allocate a new asset revision (downstream task {t.id} status is {t.status.value}, expected PENDING)"
            )

    cv_repo = ConceptVersionRepository(db)
    active_version_record = cv_repo.active_for_workflow(workflow_id)
    if active_version_record is not None:
        old_version = active_version_record.version
        old_concept_sha256 = active_version_record.content_hash
    else:
        old_version = 1
        old_concept_sha256 = revision.concept_hash or ""

    new_version = old_version + 1

    asset_dir_rel = concept_task.parameters["asset_dir"]
    guard = PathGuard(project_root)
    target_png = guard.ensure_safe_parent(f"{asset_dir_rel}/concept-v{new_version}.png")
    target_prov = guard.ensure_safe_parent(
        f"{asset_dir_rel}/concept-provenance-v{new_version}.json"
    )
    v07_character = is_v07_character_graph(concept_task)
    target_sidecar = guard.ensure_safe_parent(
        f"{asset_dir_rel}/concept-provenance-source-v{new_version}.json"
    )

    if target_png.exists():
        raise ValidationError(f"Destination concept file already exists: {target_png}")
    if target_prov.exists():
        raise ValidationError(f"Destination concept provenance file already exists: {target_prov}")
    if v07_character and target_sidecar.exists():
        raise ValidationError(f"Destination provenance sidecar already exists: {target_sidecar}")

    spec_hash = str(concept_task.parameters["specification_hash"])

    if dry_run:
        verify_png_image(concept_src)
        prov_bytes = provenance_src.read_text(encoding="utf-8")
        prov_data = json.loads(prov_bytes)
        concept_bytes = concept_src.read_bytes()
        new_concept_sha = hashlib.sha256(concept_bytes).hexdigest()
        sidecar_sha = prov_data.get("sha256") or prov_data.get("artifact_hash")
        if sidecar_sha and sidecar_sha.lower() != new_concept_sha.lower():
            raise ValidationError("Sidecar provenance sha256 does not match concept image bytes")

        snap_repo = PaidRequestSnapshotRepository(db)
        active_snaps = [r for r in snap_repo.list_by_workflow(workflow_id) if r.status == "ACTIVE"]
        readiness_repo = ProductionReadinessRepository(db)
        active_readiness = [
            r for r in readiness_repo.list_by_workflow(workflow_id) if r.status == "ACTIVE"
        ]
        pre_paid_task_ids = [t.id for t in pre_paid_tasks]

        return {
            "workflow_id": workflow_id,
            "asset_id": asset_id,
            "revision": revision_number,
            "old_version": old_version,
            "new_version": new_version,
            "old_concept_sha256": old_concept_sha256,
            "new_concept_sha256": new_concept_sha,
            "superseded_records": {
                "snapshots": [r.id for r in active_snaps],
                "readiness_reports": [r.id for r in active_readiness],
            },
            "reopened_tasks": pre_paid_task_ids,
            "new_approval_id": None,
            "dry_run": True,
        }

    # Take a bounded V0.7 sidecar snapshot before ingest creates the managed PNG.
    # Ingestion validates and hashes these same immutable bytes, which are
    # retained with the managed replacement instead of rereading the source.
    v07_sidecar_bytes = (
        _read_bounded(
            provenance_src,
            MAX_PROVENANCE_SIDECAR_BYTES,
            "Concept provenance sidecar",
        )
        if v07_character
        else None
    )

    # a) Validate and ingest the new PNG + provenance
    concept_path, provenance = ingest_concept_image(
        concept_src,
        target_png,
        spec_hash,
        sidecar_provenance_path=provenance_src,
        source_type=concept_source_type,
        sidecar_provenance_bytes=v07_sidecar_bytes,
    )
    provenance_document = provenance.to_dict()
    if v07_character and provenance.sidecar_path is not None:
        expected_sidecar_hash = provenance.sidecar_hash
        assert v07_sidecar_bytes is not None and expected_sidecar_hash is not None
        assert hashlib.sha256(v07_sidecar_bytes).hexdigest() == expected_sidecar_hash
        with target_sidecar.open("xb") as stream:
            stream.write(v07_sidecar_bytes)
        if sha256_file(target_sidecar) != expected_sidecar_hash:
            raise ValidationError("Managed concept provenance sidecar failed its hash check")
        provenance_document["sidecar_path"] = target_sidecar.relative_to(project_root).as_posix()
    target_prov.write_text(
        json.dumps(provenance_document, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    new_concept_sha256 = sha256_file(target_png)
    new_prov_sha256 = sha256_file(target_prov)

    # Register both artifacts under the CONCEPT-REVIEW task
    art_mgr = ArtifactManager(project_root)
    art_repo = ArtifactRepository(db)

    rel_png = target_png.resolve(strict=True).relative_to(project_root).as_posix()
    rel_prov = target_prov.resolve(strict=True).relative_to(project_root).as_posix()

    concept_art = art_mgr.register_file_artifact(
        workflow_id, concept_task.id, "asset-concept", concept_task.task_type, rel_png
    )
    art_repo.save(concept_art)

    prov_art = art_mgr.register_file_artifact(
        workflow_id,
        concept_task.id,
        "asset-concept-provenance",
        concept_task.task_type,
        rel_prov,
    )
    art_repo.save(prov_art)

    # b) ConceptVersionRepository.supersede_and_add
    new_record = ConceptVersionRecord(
        id=generate_id("VER"),
        workflow_id=workflow_id,
        asset_id=asset_id,
        revision_number=revision_number,
        version=new_version,
        artifact_id=concept_art.id,
        content_hash=new_concept_sha256,
        provenance_artifact_id=prov_art.id,
        provenance_hash=new_prov_sha256,
        provenance_type=provenance.provenance_type,
        source_type=provenance.source_type,
        status="ACTIVE",
        actor=actor,
        reason=reason,
        created_at=utc_now_iso(),
    )
    old_id = active_version_record.id if active_version_record else None
    cv_repo.supersede_and_add(old_id, new_record)

    # c) Update asset_revisions.concept_hash to the new hash
    superseded = rev_repo.supersede_concept_hash(
        asset_id, revision_number, old_concept_sha256, new_concept_sha256
    )
    if not superseded:
        raise ValidationError(
            f"Failed to supersede concept hash for revision {revision_number} of '{asset_id}'"
        )

    # d) Obsolete derived pre-paid state
    snap_repo = PaidRequestSnapshotRepository(db)
    active_snaps = [r for r in snap_repo.list_by_workflow(workflow_id) if r.status == "ACTIVE"]
    if active_snaps:
        snap_repo.mark_superseded([r.id for r in active_snaps])

    readiness_repo = ProductionReadinessRepository(db)
    active_readiness = [
        r for r in readiness_repo.list_by_workflow(workflow_id) if r.status == "ACTIVE"
    ]
    if active_readiness:
        readiness_repo.mark_superseded([r.id for r in active_readiness])

    # e) Reopen pre-paid segment: CONCEPT-REVIEW, PAID-REQUEST, READINESS, PAID-GENERATION to PENDING, workflow to BLOCKED
    pre_paid_task_ids = [t.id for t in pre_paid_tasks]
    task_reopen_audit = AuditEvent(
        id=generate_id("AUDIT"),
        entity_type="Task",
        entity_id=concept_task.id,
        action="TASK_REOPENED_FOR_CONCEPT_REPLACEMENT",
        actor=actor,
        details={"workflow_id": workflow_id, "reason": reason, "concept_version": new_version},
    )
    task_repo.reopen_for_concept_replacement(workflow_id, pre_paid_task_ids, task_reopen_audit)

    # Update in-memory models so engine.approval_inputs uses current status and bindings
    concept_task.status = TaskStatus.PENDING
    workflow.status = WorkflowStatus.BLOCKED

    # g) Create a fresh PENDING concept_review ApprovalRequest bound to engine.approval_inputs
    all_artifacts = art_repo.list_by_workflow(workflow_id)
    op_inputs = engine.approval_inputs(workflow, concept_task)
    new_approval = ApprovalService.create_request(
        workflow_id=workflow_id,
        task_id=concept_task.id,
        approval_type="concept_review",
        reason=f"Human concept approval required for revised concept v{new_version}",
        cost_class=concept_task.cost_class,
        operation_inputs=op_inputs,
        artifact_ids=[a.id for a in all_artifacts],
    )
    app_repo.save(new_approval)

    # h) Audit event CONCEPT_REPLACED (entity Workflow)
    now = utc_now_iso()
    audit_repo = AuditLogRepository(db)
    audit_repo.append(
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Workflow",
            entity_id=workflow_id,
            action="CONCEPT_REPLACED",
            actor=actor,
            timestamp=now,
            previous_state=str(old_version),
            new_state=str(new_version),
            details={
                "workflow_id": workflow_id,
                "asset_id": asset_id,
                "revision_number": revision_number,
                "old_version": old_version,
                "new_version": new_version,
                "old_concept_sha256": old_concept_sha256,
                "new_concept_sha256": new_concept_sha256,
                "actor": actor,
                "reason": reason,
                "superseded_snapshot_ids": [r.id for r in active_snaps],
                "superseded_readiness_ids": [r.id for r in active_readiness],
            },
        )
    )

    return {
        "workflow_id": workflow_id,
        "asset_id": asset_id,
        "revision": revision_number,
        "old_version": old_version,
        "new_version": new_version,
        "old_concept_sha256": old_concept_sha256,
        "new_concept_sha256": new_concept_sha256,
        "superseded_records": {
            "snapshots": [r.id for r in active_snaps],
            "readiness_reports": [r.id for r in active_readiness],
        },
        "reopened_tasks": pre_paid_task_ids,
        "new_approval_id": new_approval.id,
        "dry_run": False,
    }
