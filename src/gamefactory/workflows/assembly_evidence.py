"""asset-evidence-0.7.0 export for operator-authored assemblies (ADR 0016).

The bundle carries the source provenance role set (retained source GLB, its
registration and the normalization record) instead of the concept and paid
roles. ``paid`` is always false and no provider role may appear.
"""

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
    ExecutionRepository,
    ProviderInvocationRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.execution.path_guard import PathGuard

EVIDENCE_SCHEMA_V07 = "asset-evidence-0.7.0"
RECEIPT_SCHEMA_V07 = "production-receipt-0.7.0"

ROLE_TYPES = {
    "specification": "asset-specification",
    "source_glb": "asset-source-glb",
    "source_provenance": "asset-source-registration",
    "processed_glb": "asset-processed-glb",
    "processing_report": "asset-processing-report",
    "validation": "asset-validation-report",
    "runtime_observation": "asset-runtime-observation",
}


def _write_json(path: Path, value: dict[str, Any]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path.read_bytes()


def _output_directory(project: Path, output_dir: Path | str) -> Path:
    requested = Path(output_dir)
    if not requested.is_absolute():
        requested = project / requested
    try:
        relative = requested.relative_to(project)
    except ValueError as exc:
        raise ValidationError("Evidence bundle output must remain inside the project root") from exc
    output = PathGuard(project).resolve_safe_path(relative)
    cursor = project
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink() or getattr(cursor, "is_junction", lambda: False)():
            raise ValidationError("Evidence bundle output cannot traverse a symlink or junction")
    output.mkdir(parents=True, exist_ok=False)
    return output


def export_assembly_evidence_bundle(
    root: Path | str, db: Any, workflow_id: str, output_dir: Path | str
) -> Path:
    """Export verified assembly evidence; the final review may still be pending."""
    from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
    from gamefactory.workflows.assembly_production import LocalAssemblyNoProvider
    from gamefactory.workflows.asset_production import (
        AssetProductionHandlers,
        register_asset_production_handlers,
        select_review_captures,
    )
    from gamefactory.workflows.engine import WorkflowEngine

    project = Path(root).resolve(strict=True)
    workflow = WorkflowRepository(db).get(workflow_id)
    if workflow is None:
        raise ValidationError("Asset evidence export requires an asset-production workflow")
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    prepare = next((t for t in tasks if t.task_type == "asset_assembly_prepare"), None)
    if prepare is None:
        raise ArtifactError("Assembly preparation task is missing")
    if any(
        t.task_type in {"asset_paid_generation", "asset_concept_review", "paid_generation"}
        for t in tasks
    ):
        raise ValidationError("Assembly workflow unexpectedly contains a paid or concept task")
    if ProviderOperationIntentRepository(db).list_by_workflow(workflow_id) or (
        ProviderInvocationRepository(db).count(workflow_id)
    ):
        raise ValidationError("Assembly workflow has provider activity; refusing to export")
    params = prepare.parameters
    asset_id = str(params["asset_id"])
    revision_number = int(params["revision_number"])
    revision = AssetRevisionRepository(db).get(asset_id, revision_number)
    if revision is None or revision.workflow_id != workflow_id:
        raise ArtifactError("Asset revision is not linked to this workflow")
    artifacts = ArtifactRepository(db).list_by_workflow(workflow_id)
    manager = ArtifactManager(project)
    for artifact in artifacts:
        manager.verify_artifact_integrity(artifact)
    by_type: dict[str, list[Any]] = {}
    for item in artifacts:
        by_type.setdefault(item.artifact_type, []).append(item)
    selected: dict[str, Any] = {}
    for role, kind in ROLE_TYPES.items():
        if not by_type.get(kind):
            raise ArtifactError(f"Missing required evidence artifact: {kind}")
        selected[role] = by_type[kind][-1]
    specification = parse_asset_specification_v07(params["specification"])
    profile = specification.bound_profile()
    review_views = profile.review_views
    runtime_task = next(t for t in tasks if t.task_type == "asset_godot")
    runtime_runs = ExecutionRepository(db).list_by_task(runtime_task.id)
    observation = json.loads(
        (project / selected["runtime_observation"].relative_path).read_text(encoding="utf-8")
    )
    runtime_execution = next(
        (item for item in runtime_runs if item.id == observation.get("execution_id")), None
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
    if any(observation.get(key) != value for key, value in binding.items()):
        raise ValidationError("Runtime observation does not bind this revision and processed GLB")
    captures = select_review_captures(
        by_type.get("asset-runtime-capture", []), runtime_execution.id, review_views
    )
    processing = json.loads(
        (project / selected["processing_report"].relative_path).read_text(encoding="utf-8")
    )
    validation = json.loads(
        (project / selected["validation"].relative_path).read_text(encoding="utf-8")
    )
    registration = json.loads(
        (project / selected["source_provenance"].relative_path).read_text(encoding="utf-8")
    )
    normalization = processing.get("normalization")
    if (
        not isinstance(normalization, dict)
        or validation.get("normalization") != normalization
        or processing.get("source_sha256") != selected["source_glb"].content_hash
        or processing.get("processed_sha256") != selected["processed_glb"].content_hash
        or registration.get("paid") is not False
        or registration.get("source_front") != normalization.get("source_front")
    ):
        raise ValidationError("Processing, validation and registration disagree on the source")

    # Approval fingerprints are recomputed from immutable workflow inputs.
    approval_engine = WorkflowEngine(project, db, asset_provider=None)
    handlers = AssetProductionHandlers(
        project,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ProviderOperationIntentRepository(db),
        approval_engine.evi_repo,
        approval_engine.gate_repo,
        ExecutionRepository(db),
        ArtifactManager(project),
        LocalAssemblyNoProvider(),
    )
    register_asset_production_handlers(approval_engine.handler_registry, handlers)
    review_task = next(t for t in tasks if t.task_type == "asset_final_review")
    candidates = [
        a
        for a in ApprovalRepository(db).list_by_workflow(workflow_id)
        if a.task_id == review_task.id
        and a.approval_type == "final_visual_review"
        and a.status.value in {"PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"}
    ]
    if not candidates:
        raise ArtifactError("Missing required final_visual_review approval record")
    final = max(candidates, key=lambda a: a.requested_at)
    inputs = approval_engine.approval_inputs(
        workflow, review_task, final.cost_class, artifact_ids=final.artifact_ids
    )
    if compute_operation_hash(final.task_id, "final_visual_review", inputs) != final.operation_hash:
        raise ValidationError("final_visual_review fingerprint does not match workflow inputs")

    output = _output_directory(project, output_dir)
    entries: list[dict[str, Any]] = []

    def entry(role: str, relative: str, data: bytes, extra: dict[str, Any] | None = None) -> None:
        entries.append(
            {
                "role": role,
                "path": relative,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                **(extra or {}),
            }
        )

    def copy(role: str, artifact: Any, relative: str, extra: dict[str, Any] | None = None) -> None:
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project / artifact.relative_path, destination)
        entry(role, relative, destination.read_bytes(), extra)

    for role, artifact in selected.items():
        copy(role, artifact, f"evidence/{role}{Path(artifact.relative_path).suffix}")
    entry(
        "normalization",
        "evidence/normalization.json",
        _write_json(output / "evidence/normalization.json", normalization),
    )
    for artifact in captures:
        angle = Path(artifact.relative_path).stem
        if angle not in review_views:
            raise ValidationError("Unexpected runtime capture angle")
        copy("runtime_capture", artifact, f"captures/{angle}.png", {**binding, "angle": angle})
    receipt = {
        "approval_id": final.id,
        "workflow_id": workflow_id,
        "revision": revision_number,
        "task_id": final.task_id,
        "approval_type": final.approval_type,
        "status": final.status.value,
        "actor": final.actor,
        "decided_at": final.decided_at,
        "inputs": inputs,
        "fingerprint": final.operation_hash,
    }
    entry(
        "final_approval",
        "evidence/final_approval.json",
        _write_json(output / "evidence/final_approval.json", receipt),
    )
    hashes = {e["role"]: e["sha256"] for e in entries if e["role"] != "runtime_capture"}
    production_receipt = {
        "schema_version": RECEIPT_SCHEMA_V07,
        "asset_id": asset_id,
        "revision": revision_number,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_qualified": profile.qualified,
        "profile_schema": profile.schema_version,
        "spec_hash": params["specification_hash"],
        "source_kind": "local_operator_assembly",
        "geometry_mode": profile.geometry_mode,
        "paid": False,
        "paid_provider_invocations": 0,
        "source_sha256": hashes["source_glb"],
        "source_registration_sha256": hashes["source_provenance"],
        "source_front": normalization.get("source_front"),
        "normalization_applied": normalization.get("normalization_applied"),
        "normalization_sha256": hashes["normalization"],
        "processed_artifact_hash": hashes["processed_glb"],
        "validation_hash": hashes["validation"],
        "runtime_hash": hashes["runtime_observation"],
        "render_hashes": {
            e["angle"]: e["sha256"] for e in entries if e["role"] == "runtime_capture"
        },
        "approval_ids": {"final_approval": final.id},
        "completed_at": workflow.updated_at,
    }
    entry(
        "production_receipt",
        "evidence/production-receipt.json",
        _write_json(output / "evidence/production-receipt.json", production_receipt),
    )

    lines = [
        '<!doctype html><html><head><meta charset="utf-8"><title>Assembly evidence review</title></head><body>',
        f"<h1>{html.escape(asset_id)} revision {revision_number}</h1>",
        "<p><strong>OPERATOR-AUTHORED ASSEMBLY (no provider, paid: false)</strong></p>",
        f"<p>Technical evidence integrity checked. Human decision: {html.escape(final.status.value)}"
        f" by {html.escape(final.actor or 'unknown actor')}.</p>",
    ]

    def table(title: str, rows: list[tuple[str, Any]]) -> None:
        lines.append(f"<h2>{html.escape(title)}</h2><table><tbody>")
        for label, value in rows:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True, ensure_ascii=False)
            lines.append(
                f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
            )
        lines.append("</tbody></table>")

    table("Specification", [("Canonical specification", params["specification"])])
    table(
        "Source provenance",
        [
            ("Source kind", "local_operator_assembly"),
            ("Authoring tool", registration.get("authoring_tool")),
            ("Registered by", registration.get("actor")),
            ("Reason", registration.get("reason")),
            ("Source SHA-256", hashes["source_glb"]),
            ("Normalization", normalization),
        ],
    )
    table(
        "Independent validation",
        [
            ("Status", validation.get("status")),
            ("Rule groups", validation.get("rule_groups")),
            ("Findings", validation.get("findings", [])),
        ],
    )
    table(
        "Godot runtime",
        [
            ("Hierarchy", observation.get("hierarchy")),
            ("Articulation", observation.get("articulation")),
            ("Mesh bounds", observation.get("mesh_bounds")),
        ],
    )
    table("Artifact digests", [(e["role"], e["sha256"]) for e in entries])
    lines.append("<ul>")
    for row in entries:
        label = row["role"] + (f" ({row['angle']})" if row["role"] == "runtime_capture" else "")
        lines.append(
            f'<li><a href="{html.escape(row["path"], quote=True)}">{html.escape(label)}</a></li>'
        )
    lines.append("</ul>")
    header = "".join(f"<th>{html.escape(view)}</th>" for view in review_views)
    lines.append(f"<table><thead><tr>{header}</tr></thead><tbody><tr>")
    by_angle = {e["angle"]: e for e in entries if e["role"] == "runtime_capture"}
    for view in review_views:
        lines.append(
            f'<td><img src="{html.escape(by_angle[view]["path"], quote=True)}" '
            f'alt="{html.escape(view)} view" width="320" height="180"></td>'
        )
    lines.append("</tr></tbody></table></body></html>")
    (output / "index.html").write_text("\n".join(lines) + "\n", encoding="utf-8")
    entry("review_html", "index.html", (output / "index.html").read_bytes())
    manifest = {
        "schema_version": EVIDENCE_SCHEMA_V07,
        **binding,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_qualified": profile.qualified,
        "profile_schema": profile.schema_version,
        "source_kind": "local_operator_assembly",
        "geometry_mode": profile.geometry_mode,
        "paid": False,
        "paid_provider_invocations": 0,
        "review_views": list(review_views),
        "runtime_requirements": profile.runtime_requirements(specification),
        "specification_fingerprint": params["specification_hash"],
        "validator": {
            "composition": validation.get("composition"),
            "rule_groups": validation.get("rule_groups"),
        },
        "normalization": normalization,
        "final_review": {"decision": final.status.value, "fingerprint": final.operation_hash},
        "files": entries,
    }
    _write_json(output / "manifest.json", manifest)
    verifier = Path(__file__).parents[1] / "resources" / "scripts" / "verify_asset_bundle.py"
    shutil.copyfile(verifier, output / "verify_asset_bundle.py")
    return output
