"""Portable, hash-bound static review export for V0.4 asset workflows."""

from __future__ import annotations

import hashlib
import html
import json
import shutil
from pathlib import Path
from typing import Any

from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ConceptVersionRepository,
    ExecutionRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.execution.path_guard import PathGuard


def _write_json(path: Path, value: dict[str, Any]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path.read_bytes()


def export_asset_evidence_bundle(
    root: Path | str, db: Any, workflow_id: str, output_dir: Path | str
) -> Path:
    """Export verified evidence; final review may still be pending or rejected."""
    project = Path(root).resolve(strict=True)
    workflow = WorkflowRepository(db).get(workflow_id)
    if workflow is None or not (
        workflow.name.startswith("Asset production:") or workflow.name.startswith("Asset reuse:")
    ):
        raise ValidationError("Asset evidence export requires an asset-production workflow")
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    if any(t.task_type == "asset_reuse_prepare" for t in tasks):
        raise ValidationError(
            "Asset evidence export does not support existing_external reuse workflows; "
            "the cold bundle verifier has no honest schema for reuse without provider receipts."
        )
    if any(t.task_type == "asset_assembly_prepare" for t in tasks):
        from gamefactory.workflows.assembly_evidence import export_assembly_evidence_bundle

        return export_assembly_evidence_bundle(project, db, workflow_id, output_dir)
    prepare = next((t for t in tasks if t.task_type == "asset_prepare"), None)
    if prepare is None:
        raise ArtifactError("Asset specification task is missing")
    asset_id = str(prepare.parameters["asset_id"])
    revision_number = int(prepare.parameters["revision_number"])
    revision = AssetRevisionRepository(db).get(asset_id, revision_number)
    if revision is None or revision.workflow_id != workflow_id:
        raise ArtifactError("Asset revision is not linked to this workflow")
    artifacts = ArtifactRepository(db).list_by_workflow(workflow_id)
    manager = ArtifactManager(project)
    for artifact in artifacts:
        manager.verify_artifact_integrity(artifact)
    types: dict[str, list[Any]] = {}
    for item in artifacts:
        types.setdefault(item.artifact_type, []).append(item)
    role_types = {
        "specification": "asset-specification",
        "concept": "asset-concept",
        "concept_provenance": "asset-concept-provenance",
        "raw_glb": "asset-raw-glb",
        "processed_glb": "asset-processed-glb",
        "processing_report": "asset-processing-report",
        "validation": "asset-validation-report",
        "runtime_observation": "asset-runtime-observation",
    }
    selected: dict[str, Any] = {}
    for role, kind in role_types.items():
        candidates = types.get(kind, [])
        if not candidates:
            raise ArtifactError(f"Missing required evidence artifact: {kind}")
        selected[role] = candidates[-1]
    active_concept_ver = ConceptVersionRepository(db).active_for_workflow(workflow_id)
    if active_concept_ver is not None:
        concept_art = next((a for a in artifacts if a.id == active_concept_ver.artifact_id), None)
        if concept_art is None:
            raise ArtifactError(
                f"Active concept artifact {active_concept_ver.artifact_id} is missing"
            )
        selected["concept"] = concept_art
        if active_concept_ver.provenance_artifact_id:
            prov_art = next(
                (a for a in artifacts if a.id == active_concept_ver.provenance_artifact_id), None
            )
            if prov_art is None:
                raise ArtifactError(
                    f"Active concept provenance artifact {active_concept_ver.provenance_artifact_id} is missing"
                )
            selected["concept_provenance"] = prov_art
    runtime_task = next(t for t in tasks if t.task_type == "asset_godot")
    runtime_runs = ExecutionRepository(db).list_by_task(runtime_task.id)
    if not runtime_runs:
        raise ArtifactError("Runtime execution record is missing")
    observation = json.loads(
        (project / selected["runtime_observation"].relative_path).read_text(encoding="utf-8")
    )
    runtime_execution = next(
        (item for item in runtime_runs if item.id == observation.get("execution_id")),
        None,
    )
    if runtime_execution is None:
        raise ArtifactError("Runtime observation execution record is missing")
    binding = {
        "workflow_id": workflow_id,
        "revision": revision_number,
        "asset_id": asset_id,
        "execution_id": runtime_execution.id,
        "attempt_number": runtime_execution.attempt_number,
        "processed_glb_sha256": selected["processed_glb"].content_hash,
    }
    from gamefactory.core.domain.asset_contracts import parse_any_asset_specification
    from gamefactory.workflows.asset_production import (
        capture_belongs_to_execution,
        select_review_captures,
    )

    specification = parse_any_asset_specification(prepare.parameters["specification"])
    profile = specification.bound_profile()
    review_views = profile.review_views
    captures = select_review_captures(
        types.get("asset-runtime-capture", []), runtime_execution.id, review_views
    )
    if any(observation.get(key) != value for key, value in binding.items()):
        raise ValidationError("Runtime observation does not bind this revision and processed GLB")
    side_records = {
        artifact.relative_path: json.loads(
            (project / artifact.relative_path).read_text(encoding="utf-8")
        )
        for artifact in types.get("asset-side-correction", [])
    }

    approvals = ApprovalRepository(db).list_by_workflow(workflow_id)
    from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
    from gamefactory.workflows.asset_production import (
        AssetProductionHandlers,
        register_asset_production_handlers,
    )
    from gamefactory.workflows.engine import WorkflowEngine

    approval_engine = WorkflowEngine(project, db, asset_provider=None)
    approval_handlers = AssetProductionHandlers(
        project,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ProviderOperationIntentRepository(db),
        approval_engine.evi_repo,
        approval_engine.gate_repo,
        ExecutionRepository(db),
        ArtifactManager(project),
        FakeAssetGenerationProvider(),
    )
    register_asset_production_handlers(approval_engine.handler_registry, approval_handlers)
    kinds = {
        "concept_approval": ("asset_concept_review", "concept_review"),
        "paid_approval": ("asset_paid_generation", "paid_generation"),
        "final_approval": ("asset_final_review", "final_visual_review"),
    }
    receipt_data: dict[str, tuple[Any, dict[str, Any]]] = {}
    for role, (task_kind, approval_kind) in kinds.items():
        approval_task = next(t for t in tasks if t.task_type == task_kind)
        candidates = [
            a
            for a in approvals
            if a.task_id == approval_task.id and a.approval_type == approval_kind
        ]
        if role == "final_approval":
            candidates = [
                a
                for a in candidates
                if a.status.value in {"PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"}
            ]
        else:
            candidates = [a for a in candidates if a.status.value == "APPROVED"]
        if not candidates:
            raise ArtifactError(f"Missing required {approval_kind} approval record")
        approval = max(candidates, key=lambda a: a.requested_at)
        inputs = approval_engine.approval_inputs(
            workflow,
            approval_task,
            approval.cost_class,
            artifact_ids=approval.artifact_ids,
        )
        fingerprint = compute_operation_hash(approval.task_id, approval_kind, inputs)
        if fingerprint != approval.operation_hash:
            raise ValidationError(
                f"{approval_kind} fingerprint does not match immutable workflow inputs"
            )
        receipt_data[role] = (approval, inputs)

    paid_task_id = next(t.id for t in tasks if t.task_type == "asset_paid_generation")
    intent = ProviderOperationIntentRepository(db).get_by_task(paid_task_id)
    if intent is None or not intent.external_task_id or intent.status != "SUCCEEDED":
        raise ArtifactError("Provider operation is not confirmed successful")
    paid_receipt, paid_inputs = receipt_data["paid_approval"]
    # V0.6 intents bind the approved canonical paid request snapshot; the paid
    # approval binds the same snapshot hash through its immutable parameters.
    v06_evidence: dict[str, Any] | None = None
    if intent.paid_request_snapshot_hash:
        snapshot_sha = intent.paid_request_snapshot_hash
        parameters = paid_inputs.get("parameters", {})
        readiness_sha = parameters.get("production_readiness_report_sha256")
        if (
            intent.request_fingerprint != snapshot_sha
            or paid_receipt.paid_request_snapshot_hash != snapshot_sha
            or parameters.get("paid_request_snapshot_sha256") != snapshot_sha
            or not readiness_sha
        ):
            raise ValidationError(
                "Provider operation is not bound to the approved paid request snapshot"
            )
        snapshot_artifact = next(
            (
                a
                for a in types.get("asset-paid-request-snapshot", [])
                if a.content_hash == snapshot_sha
            ),
            None,
        )
        readiness_artifact = next(
            (
                a
                for a in types.get("asset-production-readiness-report", [])
                if a.content_hash == readiness_sha
            ),
            None,
        )
        if snapshot_artifact is None or readiness_artifact is None:
            raise ArtifactError("Approved paid request snapshot or readiness report is missing")
        v06_evidence = {
            "paid_request_snapshot": snapshot_artifact,
            "production_readiness_report": readiness_artifact,
            "snapshot_sha256": snapshot_sha,
            "readiness_sha256": readiness_sha,
        }
    elif intent.request_fingerprint != paid_receipt.operation_hash:
        raise ValidationError("Provider operation fingerprint differs from paid approval")
    requested_output = Path(output_dir)
    if not requested_output.is_absolute():
        requested_output = project / requested_output
    try:
        output_relative = requested_output.relative_to(project)
    except ValueError as exc:
        raise ValidationError("Evidence bundle output must remain inside the project root") from exc
    guard = PathGuard(project)
    output = guard.resolve_safe_path(output_relative)
    cursor = project
    for part in output_relative.parts:
        cursor = cursor / part
        is_junction = getattr(cursor, "is_junction", lambda: False)()
        if cursor.is_symlink() or is_junction:
            raise ValidationError("Evidence bundle output cannot traverse a symlink or junction")
    output.mkdir(parents=True, exist_ok=False)
    entries: list[dict[str, Any]] = []

    def add(role: str, source: Path, relative: str, extra: dict[str, Any] | None = None) -> None:
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        data = destination.read_bytes()
        entries.append(
            {
                "role": role,
                "path": relative,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                **(extra or {}),
            }
        )

    for role, artifact in selected.items():
        add(
            role,
            project / artifact.relative_path,
            f"evidence/{role}{Path(artifact.relative_path).suffix}",
        )
    if v06_evidence is not None:
        for role in ("paid_request_snapshot", "production_readiness_report"):
            artifact = v06_evidence[role]
            add(role, project / artifact.relative_path, f"evidence/{role}.json")
    for artifact in captures:
        angle = Path(artifact.relative_path).stem
        execution_prefix = f"{runtime_execution.id}-"
        if angle.startswith(execution_prefix):
            angle = angle[len(execution_prefix) :]
        if angle not in review_views:
            raise ValidationError("Unexpected runtime capture angle")
        capture_binding = binding
        if not capture_belongs_to_execution(artifact.relative_path, runtime_execution.id):
            record = next(
                (
                    payload
                    for path, payload in side_records.items()
                    if capture_belongs_to_execution(path, str(payload.get("execution_id", "")))
                    and capture_belongs_to_execution(
                        artifact.relative_path, str(payload.get("execution_id", ""))
                    )
                ),
                None,
            )
            if not isinstance(record, dict):
                raise ValidationError("Corrected side capture has no side-correction record")
            side_execution = next(
                (item for item in runtime_runs if item.id == record.get("execution_id")),
                None,
            )
            if (
                side_execution is None
                or side_execution.status.value != "COMPLETED"
                or side_execution.attempt_number != record.get("attempt_number")
            ):
                raise ValidationError("Corrected side capture execution does not match its record")
            capture_binding = {
                "workflow_id": workflow_id,
                "revision": revision_number,
                "asset_id": asset_id,
                "execution_id": record.get("execution_id"),
                "attempt_number": record.get("attempt_number"),
                "processed_glb_sha256": record.get("processed_glb_sha256"),
            }
        add(
            "runtime_capture",
            project / artifact.relative_path,
            f"captures/{angle}.png",
            {**capture_binding, "angle": angle},
        )
    if any(
        not capture_belongs_to_execution(artifact.relative_path, runtime_execution.id)
        for artifact in captures
    ):
        record_path = next(
            (
                path
                for path, payload in side_records.items()
                if capture_belongs_to_execution(
                    captures[-1].relative_path, str(payload.get("execution_id", ""))
                )
                and path.endswith("observation.json")
            ),
            None,
        )
        if record_path is None:
            raise ValidationError("Corrected side capture has no side-correction record")
        add("side_correction", project / record_path, "evidence/side-correction.json")
    for role, (approval, inputs) in receipt_data.items():
        receipt = {
            "approval_id": approval.id,
            "workflow_id": workflow_id,
            "revision": revision_number,
            "task_id": approval.task_id,
            "approval_type": approval.approval_type,
            "status": approval.status.value,
            "actor": approval.actor,
            "decided_at": approval.decided_at,
            "inputs": inputs,
            "fingerprint": approval.operation_hash,
        }
        relative = f"evidence/{role}.json"
        data = _write_json(output / relative, receipt)
        entries.append(
            {
                "role": role,
                "path": relative,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    provider = {
        "provider": intent.provider,
        "operation": intent.operation,
        "status": intent.status,
        "external_task_id": intent.external_task_id,
        "request_fingerprint": intent.request_fingerprint,
        **(
            {"paid_request_snapshot_sha256": v06_evidence["snapshot_sha256"]}
            if v06_evidence is not None
            else {}
        ),
        "actual_cost": intent.actual_cost if intent.actual_cost is not None else "UNKNOWN",
        "workflow_id": workflow_id,
        "task_id": intent.task_id,
        "revision": revision_number,
        "concept_sha256": intent.concept_hash,
    }
    paid_task = next(t for t in tasks if t.task_type == "asset_paid_generation")
    cost_record = {
        "estimate": paid_task.parameters["provider_estimate"]
        if paid_task.parameters["provider_estimate"] is not None
        else "UNKNOWN",
        "budget_reservation": paid_task.parameters["budget_reservation"],
        "actual": provider["actual_cost"],
        "unit": intent.cost_unit,
    }
    for role, relative, value in (
        ("provider_operation", "evidence/provider-operation.json", provider),
        ("cost_record", "evidence/cost-record.json", cost_record),
    ):
        data = _write_json(output / relative, value)
        entries.append(
            {
                "role": role,
                "path": relative,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    final = receipt_data["final_approval"][0]
    concept_entry = next(entry for entry in entries if entry["role"] == "concept")
    concept_href = html.escape(concept_entry["path"], quote=True)
    lines = [
        '<!doctype html><html><head><meta charset="utf-8"><title>Asset evidence review</title></head><body>',
        f"<h1>{html.escape(asset_id)} revision {revision_number}</h1>",
        f"<p><strong>{'TEST / FAKE PROVIDER EVIDENCE' if intent.provider.lower().startswith('fake') else 'PROVIDER EVIDENCE'}</strong></p>",
        f"<p>Technical evidence integrity checked. Human decision: {html.escape(final.status.value)} by {html.escape(final.actor or 'unknown actor')}.</p>",
    ]

    def add_table(title: str, rows: list[tuple[str, Any]]) -> None:
        lines.append(f"<h2>{html.escape(title)}</h2><table><tbody>")
        for label, value in rows:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True, ensure_ascii=False)
            lines.append(
                f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
            )
        lines.append("</tbody></table>")

    spec_payload = json.loads(
        (project / selected["specification"].relative_path).read_text(encoding="utf-8")
    )
    processing_payload = json.loads(
        (project / selected["processing_report"].relative_path).read_text(encoding="utf-8")
    )
    validation_payload = json.loads(
        (project / selected["validation"].relative_path).read_text(encoding="utf-8")
    )
    add_table("Specification", [("Canonical specification", spec_payload)])
    add_table(
        "Asset processing",
        [
            ("Raw GLB SHA-256", selected["raw_glb"].content_hash),
            ("Raw GLB bytes", selected["raw_glb"].file_size),
            ("Processed GLB SHA-256", selected["processed_glb"].content_hash),
            ("Processed GLB bytes", selected["processed_glb"].file_size),
            ("Processing report", processing_payload),
        ],
    )
    add_table(
        "Independent validation",
        [
            ("Status", validation_payload.get("status", "UNKNOWN")),
            ("Summary", validation_payload.get("summary", "")),
            ("Findings", validation_payload.get("findings", [])),
        ],
    )
    add_table("Provider and cost", [("Provider operation", provider), ("Cost record", cost_record)])
    add_table(
        "Human approval receipts",
        [
            (
                role,
                {
                    "type": approval.approval_type,
                    "status": approval.status.value,
                    "actor": approval.actor,
                    "fingerprint": approval.operation_hash,
                },
            )
            for role, (approval, _) in receipt_data.items()
        ],
    )
    from gamefactory.core.domain.asset_contracts import AssetSpecificationV07

    v07_spec = specification if isinstance(specification, AssetSpecificationV07) else None
    is_v07 = v07_spec is not None
    production_receipt: dict[str, Any] = {
        "schema_version": (
            "production-receipt-0.7.0"
            if is_v07
            else "production-receipt-0.6.0"
            if v06_evidence is not None
            else "production-receipt-0.5.0"
        ),
        "asset_id": asset_id,
        "revision": revision_number,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_qualified": profile.qualified,
        "profile_schema": profile.schema_version,
        "spec_hash": prepare.parameters["specification_hash"],
        "concept_hash": concept_entry["sha256"],
        "provider_request_fingerprint": intent.request_fingerprint,
        "provider_task_id": intent.external_task_id,
        "cost": provider["actual_cost"],
        "raw_artifact_hash": selected["raw_glb"].content_hash,
        "processed_artifact_hash": selected["processed_glb"].content_hash,
        "validation_hash": selected["validation"].content_hash,
        "runtime_hash": selected["runtime_observation"].content_hash,
        "render_hashes": {
            entry["angle"]: entry["sha256"]
            for entry in entries
            if entry["role"] == "runtime_capture"
        },
        "approval_ids": {role: approval.id for role, (approval, _inputs) in receipt_data.items()},
        "completed_at": workflow.updated_at,
    }
    if v06_evidence is not None:
        production_receipt["paid_request_snapshot_sha256"] = v06_evidence["snapshot_sha256"]
        production_receipt["production_readiness_report_sha256"] = v06_evidence["readiness_sha256"]
    if is_v07:
        production_receipt["source_kind"] = "provider_generated"
        production_receipt["geometry_mode"] = profile.geometry_mode
        production_receipt["collider_policy"] = v07_spec.collider.policy if v07_spec else None
    receipt_bytes = _write_json(output / "evidence/production-receipt.json", production_receipt)
    entries.append(
        {
            "role": "production_receipt",
            "path": "evidence/production-receipt.json",
            "size": len(receipt_bytes),
            "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        }
    )
    add_table("Artifact digests", [(entry["role"], entry["sha256"]) for entry in entries])
    lines.append("<ul>")
    for entry in entries:
        label = entry["role"] + (
            f" ({entry['angle']})" if entry["role"] == "runtime_capture" else ""
        )
        href = html.escape(entry["path"], quote=True)
        lines.append(f'<li><a href="{href}">{html.escape(label)}</a></li>')
    header = "".join(f"<th>{html.escape(angle)}</th>" for angle in ("concept", *review_views))
    lines.append(f"<table><thead><tr>{header}</tr></thead><tbody><tr>")
    lines.append(f'<td><img src="{concept_href}" alt="Approved concept" width="320"></td>')
    capture_by_angle = {
        entry["angle"]: entry for entry in entries if entry["role"] == "runtime_capture"
    }
    for angle in review_views:
        entry = capture_by_angle[angle]
        lines.append(
            f'<td><img src="{html.escape(entry["path"], quote=True)}" alt="{angle} view" width="320" height="180"></td>'
        )
    lines.append("</tr></tbody></table>")
    lines.append("</body></html>")
    (output / "index.html").write_text("\n".join(lines) + "\n", encoding="utf-8")
    review = (output / "index.html").read_bytes()
    entries.append(
        {
            "role": "review_html",
            "path": "index.html",
            "size": len(review),
            "sha256": hashlib.sha256(review).hexdigest(),
        }
    )
    manifest = {
        "schema_version": (
            "asset-evidence-0.7.0"
            if is_v07
            else "asset-evidence-0.6.0"
            if v06_evidence is not None
            else "asset-evidence-0.5.0"
        ),
        **binding,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_qualified": profile.qualified,
        "review_views": list(review_views),
        "runtime_requirements": (
            profile.runtime_requirements(v07_spec) if v07_spec else profile.runtime_requirements()
        ),
        "specification_fingerprint": prepare.parameters["specification_hash"],
        "final_review": {"decision": final.status.value, "fingerprint": final.operation_hash},
        "files": entries,
    }
    if is_v07:
        manifest.update(
            {
                "profile_schema": profile.schema_version,
                "source_kind": "provider_generated",
                "geometry_mode": profile.geometry_mode,
                "validator": {
                    "composition": validation_payload.get("composition"),
                    "rule_groups": validation_payload.get("rule_groups"),
                },
            }
        )
    _write_json(output / "manifest.json", manifest)
    verifier = Path(__file__).parents[1] / "resources" / "scripts" / "verify_asset_bundle.py"
    shutil.copyfile(verifier, output / "verify_asset_bundle.py")
    return output
