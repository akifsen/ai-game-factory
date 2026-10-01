"""Authoritative bound snapshot + fingerprint for V0.8-3C candidate workflow."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ExecutionRepository,
    TaskRepository,
)
from gamefactory.core.domain.models import Artifact, ExecutionStatus, Task, Workflow
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.workflows.v08_candidate_currentness import (
    CandidateCurrentnessError,
    artifact_bound_to_execution,
    assert_latest_attempt_completed,
    require_observation_execution_match,
    select_artifact_for_execution,
    verify_artifact_bytes_and_hash,
)
from gamefactory.workflows.v08_candidate_gates import (
    assert_static_report_pass,
    parse_bound_spec_profile,
    validate_test_only_receipt,
    verify_authoritative_asset_revision,
    verify_retained_prepare_artifacts,
    verify_rig_oracle_gate,
    verify_runtime_b_gate,
)

CANDIDATE_GRAPH_VERSION = "0.8.0-candidate"
CANDIDATE_TEST_ONLY_APPROVAL = "candidate_test_only_review"
CANDIDATE_RECEIPT_SCOPE = "candidate_test_only"


def candidate_test_only_handler_context(
    workflow: Workflow, task: Task, snapshot: CandidateBoundSnapshot
) -> dict[str, Any]:
    return {
        "workflow_id": workflow.id,
        "revision": task.parameters["revision_number"],
        "specification_hash": task.parameters["specification_hash"],
        "profile_document_hash": task.parameters["profile_document_hash"],
        "snapshot_fingerprint": snapshot.fingerprint(),
        "receipt_scope": CANDIDATE_RECEIPT_SCOPE,
        "production_eligible": False,
        "promotion_eligible": False,
        "candidate_test_only": True,
    }


@dataclass(frozen=True)
class CandidateBoundSnapshot:
    """Immutable view of candidate identity bindings at a gate boundary."""

    payload: dict[str, Any]

    def fingerprint(self) -> str:
        raw = json.dumps(self.payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_v08_candidate_graph(task: Task | Any) -> bool:
    params = getattr(task, "parameters", None)
    if params is None and isinstance(task, dict):
        params = task.get("parameters")
    if not isinstance(params, dict):
        return False
    return (
        params.get("graph_version") == CANDIDATE_GRAPH_VERSION
        and params.get("source_kind") == "local_verified_rig"
    )


def _pre_review_artifact_binding(
    artifact: Artifact, *, producing_execution_id: str
) -> dict[str, Any]:
    return {
        "artifact_id": artifact.id,
        "task_id": artifact.task_id,
        "role": artifact.artifact_type,
        "relative_path": artifact.relative_path.replace("\\", "/"),
        "content_sha256": artifact.content_hash,
        "size_bytes": artifact.file_size,
        "producing_execution_id": producing_execution_id,
    }


def _task_by_suffix(tasks: list[Task], suffix: str) -> Task:
    for task in tasks:
        if task.id.endswith(f"-{suffix}"):
            return task
    raise CandidateCurrentnessError(f"candidate task suffix {suffix} is missing from workflow")


def build_candidate_bound_snapshot(
    *,
    root: Any,
    artifact_manager: Any,
    artifacts: ArtifactRepository,
    revisions: AssetRevisionRepository,
    approvals: ApprovalRepository,
    executions: ExecutionRepository,
    tasks: TaskRepository,
    workflow: Workflow,
    prepare_task: Task,
) -> CandidateBoundSnapshot:
    task_rows = tasks.list_by_workflow(workflow.id)
    if not task_rows:
        raise CandidateCurrentnessError("candidate workflow has no tasks")
    artifact_rows = artifacts.list_by_workflow(workflow.id)
    for artifact in artifact_rows:
        verify_artifact_bytes_and_hash(artifact, root_path=root, artifact_manager=artifact_manager)

    prepare_exec = assert_latest_attempt_completed(
        executions, prepare_task, purpose="prepare snapshot"
    )
    params = prepare_task.parameters
    spec, profile = parse_bound_spec_profile(params)
    source_rel = params.get("source_glb")
    if not isinstance(source_rel, str) or not source_rel:
        raise CandidateCurrentnessError("prepare parameters missing source_glb path")
    source_rel_norm = source_rel.replace("\\", "/")
    if path_crosses_link(Path(source_rel_norm)):
        raise CandidateCurrentnessError("authoritative source_glb lexical path crosses a link")
    source_lexical = root / source_rel_norm
    if path_crosses_link(source_lexical):
        raise CandidateCurrentnessError("authoritative source_glb lexical path crosses a link")
    source_path = PathGuard(root).resolve_safe_path(source_rel)
    if path_crosses_link(source_path):
        raise CandidateCurrentnessError("authoritative source_glb path crosses a link")
    from gamefactory.adapters.assets.glb_validator import _MAX_FILE_BYTES

    try:
        source_size = source_path.stat().st_size
    except OSError as exc:
        raise CandidateCurrentnessError(f"authoritative source_glb unreadable: {exc}") from exc
    if source_size <= 0 or source_size > _MAX_FILE_BYTES:
        raise CandidateCurrentnessError("authoritative source_glb size out of bounds")
    source_on_disk = sha256_file(source_path)
    if source_on_disk != params["source_glb_hash"]:
        raise CandidateCurrentnessError("authoritative source_glb bytes do not match prepare hash")

    identity = _task_by_suffix(task_rows, "IDENTITY")
    static_task = _task_by_suffix(task_rows, "STATIC")
    capture_task = _task_by_suffix(task_rows, "CAPTURE")
    oracle_task = _task_by_suffix(task_rows, "RIG-ORACLE")

    identity_exec = assert_latest_attempt_completed(
        executions, identity, purpose="identity snapshot"
    )
    static_exec = assert_latest_attempt_completed(
        executions, static_task, purpose="static snapshot"
    )
    capture_exec = assert_latest_attempt_completed(
        executions, capture_task, purpose="capture snapshot"
    )
    oracle_exec = assert_latest_attempt_completed(
        executions, oracle_task, purpose="rig oracle snapshot"
    )

    retained_source = select_artifact_for_execution(
        artifact_rows,
        task_id=prepare_task.id,
        artifact_type="candidate-source-retained",
        execution=prepare_exec,
    )
    if retained_source.content_hash != params["source_glb_hash"]:
        raise CandidateCurrentnessError("retained source hash does not match prepare parameters")

    raw_art = select_artifact_for_execution(
        artifact_rows,
        task_id=identity.id,
        artifact_type="candidate-raw-glb",
        execution=identity_exec,
    )
    processed_art = select_artifact_for_execution(
        artifact_rows,
        task_id=identity.id,
        artifact_type="candidate-processed-glb",
        execution=identity_exec,
    )
    if raw_art.content_hash != processed_art.content_hash:
        raise CandidateCurrentnessError("raw and processed GLB hashes must be identical")
    if processed_art.content_hash != params["source_glb_hash"]:
        raise CandidateCurrentnessError("processed GLB must match retained source hash")

    revision_row = verify_authoritative_asset_revision(
        revisions=revisions,
        workflow=workflow,
        prepare_params=params,
        processed_art=processed_art,
    )
    retained_hashes = verify_retained_prepare_artifacts(
        root,
        prepare_params=params,
        prepare_task=prepare_task,
        prepare_exec=prepare_exec,
        artifact_rows=artifact_rows,
        spec=spec,
        profile=profile,
    )

    static_report = select_artifact_for_execution(
        artifact_rows,
        task_id=static_task.id,
        artifact_type="candidate-static-validation-report",
        execution=static_exec,
    )
    assert_static_report_pass(
        root,
        static_report,
        spec=spec,
        profile=profile,
        processed_art=processed_art,
    )

    runtime_request = select_artifact_for_execution(
        artifact_rows,
        task_id=capture_task.id,
        artifact_type="candidate-runtime-request",
        execution=capture_exec,
    )
    runtime_obs = select_artifact_for_execution(
        artifact_rows,
        task_id=capture_task.id,
        artifact_type="candidate-runtime-observation",
        execution=capture_exec,
    )
    from gamefactory.workflows.v08_candidate_currentness import load_json_artifact

    observation = load_json_artifact(root, runtime_obs)
    require_observation_execution_match(observation, capture_exec, purpose="capture snapshot")

    capture_hashes, provenance_hashes = verify_runtime_b_gate(
        root=root,
        workflow_id=workflow.id,
        prepare_params=params,
        spec=spec,
        profile=profile,
        capture_exec=capture_exec,
        runtime_request=runtime_request,
        runtime_obs=runtime_obs,
        processed_art=processed_art,
        artifact_rows=artifact_rows,
    )
    retained_spec_art = select_artifact_for_execution(
        artifact_rows,
        task_id=prepare_task.id,
        artifact_type="candidate-specification",
        execution=prepare_exec,
    )
    retained_profile_art = select_artifact_for_execution(
        artifact_rows,
        task_id=prepare_task.id,
        artifact_type="candidate-profile-document",
        execution=prepare_exec,
    )
    runtime_provenance = select_artifact_for_execution(
        artifact_rows,
        task_id=capture_task.id,
        artifact_type="candidate-runtime-provenance",
        execution=capture_exec,
    )
    runtime_import_log = select_artifact_for_execution(
        artifact_rows,
        task_id=capture_task.id,
        artifact_type="candidate-runtime-import-log",
        execution=capture_exec,
    )
    runtime_render_log = select_artifact_for_execution(
        artifact_rows,
        task_id=capture_task.id,
        artifact_type="candidate-runtime-render-log",
        execution=capture_exec,
    )
    runtime_capture_arts = sorted(
        [
            item
            for item in artifact_rows
            if item.task_id == capture_task.id
            and item.artifact_type == "candidate-runtime-capture"
            and artifact_bound_to_execution(item, capture_exec)
        ],
        key=lambda item: item.relative_path,
    )

    rig_wrapper = select_artifact_for_execution(
        artifact_rows,
        task_id=oracle_task.id,
        artifact_type="candidate-rig-attempt-wrapper",
        execution=oracle_exec,
    )
    nested_manifest = select_artifact_for_execution(
        artifact_rows,
        task_id=oracle_task.id,
        artifact_type="candidate-nested-rig-manifest",
        execution=oracle_exec,
    )
    verify_rig_oracle_gate(
        root=root,
        workflow_id=workflow.id,
        prepare_params=params,
        oracle_exec=oracle_exec,
        processed_art=processed_art,
        rig_wrapper=rig_wrapper,
        nested_manifest=nested_manifest,
    )
    wrapper_doc = load_json_artifact(root, rig_wrapper)

    pre_review_bindings = sorted(
        [
            _pre_review_artifact_binding(retained_spec_art, producing_execution_id=prepare_exec.id),
            _pre_review_artifact_binding(
                retained_profile_art, producing_execution_id=prepare_exec.id
            ),
            _pre_review_artifact_binding(retained_source, producing_execution_id=prepare_exec.id),
            _pre_review_artifact_binding(raw_art, producing_execution_id=identity_exec.id),
            _pre_review_artifact_binding(processed_art, producing_execution_id=identity_exec.id),
            _pre_review_artifact_binding(static_report, producing_execution_id=static_exec.id),
            _pre_review_artifact_binding(runtime_request, producing_execution_id=capture_exec.id),
            _pre_review_artifact_binding(runtime_obs, producing_execution_id=capture_exec.id),
            _pre_review_artifact_binding(
                runtime_provenance, producing_execution_id=capture_exec.id
            ),
            _pre_review_artifact_binding(
                runtime_import_log, producing_execution_id=capture_exec.id
            ),
            _pre_review_artifact_binding(
                runtime_render_log, producing_execution_id=capture_exec.id
            ),
            *[
                _pre_review_artifact_binding(item, producing_execution_id=capture_exec.id)
                for item in runtime_capture_arts
            ],
            _pre_review_artifact_binding(rig_wrapper, producing_execution_id=oracle_exec.id),
            _pre_review_artifact_binding(nested_manifest, producing_execution_id=oracle_exec.id),
        ],
        key=lambda row: row["artifact_id"],
    )

    payload: dict[str, Any] = {
        "graph_version": CANDIDATE_GRAPH_VERSION,
        "workflow_id": workflow.id,
        "asset_id": params["asset_id"],
        "revision_number": params["revision_number"],
        "specification_hash": params["specification_hash"],
        "profile_document_hash": params["profile_document_hash"],
        "source_glb_hash": params["source_glb_hash"],
        "authoritative_source_glb_relative_path": source_rel_norm,
        "pre_review_artifact_bindings": pre_review_bindings,
        "profile_id": params.get("profile_id"),
        "profile_version": params.get("profile_version"),
        "asset_revision_spec_hash": revision_row.spec_hash,
        "asset_revision_raw_glb_hash": revision_row.raw_glb_hash,
        "asset_revision_processed_glb_hash": revision_row.processed_glb_hash,
        "retained_specification_sha256": retained_hashes["candidate_specification_sha256"],
        "retained_profile_document_sha256": retained_hashes["candidate_profile_document_sha256"],
        "retained_source_sha256": retained_source.content_hash,
        "raw_glb_sha256": raw_art.content_hash,
        "processed_glb_sha256": processed_art.content_hash,
        "static_validation_report_sha256": static_report.content_hash,
        "runtime_request_sha256": runtime_request.content_hash,
        "runtime_observation_sha256": runtime_obs.content_hash,
        "runtime_capture_hashes": capture_hashes,
        **provenance_hashes,
        "rig_attempt_wrapper_sha256": rig_wrapper.content_hash,
        "nested_rig_manifest_sha256": nested_manifest.content_hash,
        "prepare_execution": {
            "id": prepare_exec.id,
            "attempt_number": prepare_exec.attempt_number,
            "status": prepare_exec.status.value,
        },
        "identity_execution": {
            "id": identity_exec.id,
            "attempt_number": identity_exec.attempt_number,
            "status": identity_exec.status.value,
        },
        "static_execution": {
            "id": static_exec.id,
            "attempt_number": static_exec.attempt_number,
            "status": static_exec.status.value,
        },
        "capture_execution": {
            "id": capture_exec.id,
            "attempt_number": capture_exec.attempt_number,
            "status": capture_exec.status.value,
        },
        "oracle_execution": {
            "id": oracle_exec.id,
            "attempt_number": oracle_exec.attempt_number,
            "status": oracle_exec.status.value,
        },
        "runtime_request_digest": observation.get("request_digest"),
        "nested_bundle_id": wrapper_doc.get("nested_bundle_id"),
    }
    manifest_path = root / nested_manifest.relative_path
    payload["nested_rig_manifest_on_disk_sha256"] = sha256_file(manifest_path)
    if payload["nested_rig_manifest_on_disk_sha256"] != nested_manifest.content_hash:
        raise CandidateCurrentnessError("nested rig manifest hash drifted after registration")

    snapshot = CandidateBoundSnapshot(payload=payload)
    review_task = next(
        (t for t in task_rows if t.task_type == "v08_candidate_test_only_review"),
        None,
    )
    if review_task is not None:
        review_latest = executions.get_latest_attempt(review_task.id)
        if review_latest is not None and review_latest.status == ExecutionStatus.COMPLETED:
            receipt_art = select_artifact_for_execution(
                artifact_rows,
                task_id=review_task.id,
                artifact_type="candidate-test-only-receipt",
                execution=review_latest,
            )
            validate_test_only_receipt(
                root=root,
                receipt_art=receipt_art,
                snapshot=snapshot,
                approvals=approvals,
                workflow=workflow,
                review_task=review_task,
                artifacts=artifact_rows,
            )
    return snapshot
