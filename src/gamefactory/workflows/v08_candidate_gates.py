"""Independent gate checks for V0.8-3C candidate readiness and review."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_validator import _MAX_FILE_BYTES
from gamefactory.adapters.assets.v08_candidate_runtime_digest import (
    candidate_runtime_bound_payload_canonical,
)
from gamefactory.adapters.assets.v08_candidate_runtime_json import (
    CandidateRuntimeJsonError,
    parse_strict_runtime_json_object,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.adapters.assets.v08_candidate_runtime_verify import (
    CandidateRuntimeObservationError,
    verify_candidate_runtime_observation,
)
from gamefactory.adapters.assets.v08_candidate_validate import validate_v08_candidate_glb
from gamefactory.adapters.engines.godot_execution import _ENGINE_ERROR_PATTERNS
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    AssetRevisionRepository,
)
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.approvals.operation_scope import build_operation_inputs
from gamefactory.core.domain.asset_contracts import AssetRevision
from gamefactory.core.domain.asset_contracts import ValidationFindingSeverity as Severity
from gamefactory.core.domain.asset_profiles import ProfileContractError
from gamefactory.core.domain.models import (
    ApprovalRequest,
    ApprovalStatus,
    Artifact,
    Execution,
    Task,
    Workflow,
)
from gamefactory.core.domain.v08_candidate_contracts import (
    AssetProfileV08Candidate,
    AssetSpecificationV08Candidate,
    candidate_spec_fingerprint,
    load_packaged_candidate_profile,
    parse_asset_specification_v08_candidate,
    parse_profile_document_v08_candidate,
    profile_document_hash,
    revalidate_candidate_binding,
)
from gamefactory.core.domain.v08_candidate_runtime_integers import (
    CandidateRuntimeIntegerError,
    strict_process_exit_code,
)
from gamefactory.workflows.v08_candidate_currentness import (
    CandidateCurrentnessError,
    artifact_bound_to_execution,
    select_artifact_for_execution,
)

_CANDIDATE_GRAPH_VERSION = "0.8.0-candidate"
_CANDIDATE_TEST_ONLY_APPROVAL = "candidate_test_only_review"
_CANDIDATE_RECEIPT_SCOPE = "candidate_test_only"

MAX_CANDIDATE_JSON_ARTIFACT_BYTES = 1_048_576
MAX_CANDIDATE_PNG_BYTES = 32 * 1024 * 1024
NESTED_RIG_COLD_VERIFY_TIMEOUT_SECONDS = 120.0


def load_strict_json_artifact(root: Path, artifact: Artifact) -> dict[str, Any]:
    path = root / artifact.relative_path
    if path_crosses_link(path):
        raise CandidateCurrentnessError(f"artifact {artifact.id} path crosses a link")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise CandidateCurrentnessError(f"artifact {artifact.id} is not readable: {exc}") from exc
    if size <= 0 or size > MAX_CANDIDATE_JSON_ARTIFACT_BYTES:
        raise CandidateCurrentnessError(f"artifact {artifact.id} JSON size {size} is out of bounds")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CandidateCurrentnessError(f"artifact {artifact.id} is not readable: {exc}") from exc
    try:
        parsed = parse_strict_runtime_json_object(raw, max_bytes=MAX_CANDIDATE_JSON_ARTIFACT_BYTES)
    except CandidateRuntimeJsonError as exc:
        raise CandidateCurrentnessError(
            f"artifact {artifact.id} JSON policy violation: {exc}"
        ) from exc
    return parsed


def parse_bound_spec_profile(
    prepare_params: dict[str, Any],
) -> tuple[AssetSpecificationV08Candidate, AssetProfileV08Candidate]:
    spec = parse_asset_specification_v08_candidate(prepare_params["specification"])
    if candidate_spec_fingerprint(spec) != prepare_params["specification_hash"]:
        raise CandidateCurrentnessError("prepare specification_hash does not match specification")
    profile = load_packaged_candidate_profile()
    doc_hash = profile_document_hash(profile.document)
    if doc_hash != prepare_params["profile_document_hash"]:
        raise CandidateCurrentnessError(
            "prepare profile_document_hash does not match packaged profile"
        )
    return revalidate_candidate_binding(spec, profile)


def assert_static_report_pass(
    root: Path,
    static_report: Artifact,
    *,
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
    processed_art: Artifact,
) -> None:
    payload = load_strict_json_artifact(root, static_report)
    status = payload.get("status")
    if status != Severity.PASS.value and status != "PASS":
        raise CandidateCurrentnessError("static validation report is not PASS")
    glb_path = root / processed_art.relative_path
    if path_crosses_link(glb_path):
        raise CandidateCurrentnessError("processed GLB path crosses a link for static gate")
    try:
        glb_size = glb_path.stat().st_size
    except OSError as exc:
        raise CandidateCurrentnessError(f"processed GLB unreadable for static gate: {exc}") from exc
    if glb_size <= 0 or glb_size > _MAX_FILE_BYTES:
        raise CandidateCurrentnessError("processed GLB size out of bounds for static gate")
    glb_bytes = glb_path.read_bytes()
    if hashlib.sha256(glb_bytes).hexdigest() != processed_art.content_hash:
        raise CandidateCurrentnessError("static gate processed GLB hash drifted")
    live = validate_v08_candidate_glb(glb_path, spec, profile=profile)
    if live.status != Severity.PASS:
        raise CandidateCurrentnessError("independent static validation no longer PASS")


def verify_authoritative_asset_revision(
    *,
    revisions: AssetRevisionRepository,
    workflow: Workflow,
    prepare_params: dict[str, Any],
    processed_art: Artifact,
) -> AssetRevision:
    asset_id = str(prepare_params["asset_id"])
    revision_number = int(prepare_params["revision_number"])
    row = revisions.get(asset_id, revision_number)
    if row is None:
        raise CandidateCurrentnessError("authoritative asset revision row is missing")
    if row.workflow_id != workflow.id:
        raise CandidateCurrentnessError("asset revision workflow_id does not match workflow")
    if row.spec_hash != prepare_params["specification_hash"]:
        raise CandidateCurrentnessError(
            "asset revision spec_hash does not match prepare parameters"
        )
    if row.raw_glb_hash != prepare_params["source_glb_hash"]:
        raise CandidateCurrentnessError(
            "asset revision raw_glb_hash does not match prepare parameters"
        )
    expected_profile_id = prepare_params.get("profile_id")
    expected_profile_version = prepare_params.get("profile_version")
    if row.profile_id != expected_profile_id:
        raise CandidateCurrentnessError(
            "asset revision profile_id does not match prepare parameters"
        )
    if row.profile_version != expected_profile_version:
        raise CandidateCurrentnessError(
            "asset revision profile_version does not match prepare parameters"
        )
    if row.processed_glb_hash is not None and row.processed_glb_hash != processed_art.content_hash:
        raise CandidateCurrentnessError(
            "asset revision processed_glb_hash does not match identity processed GLB"
        )
    return row


def verify_retained_prepare_artifacts(
    root: Path,
    *,
    prepare_params: dict[str, Any],
    prepare_task: Task,
    prepare_exec: Execution,
    artifact_rows: list[Artifact],
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
) -> dict[str, str]:
    retained_spec = select_artifact_for_execution(
        artifact_rows,
        task_id=prepare_task.id,
        artifact_type="candidate-specification",
        execution=prepare_exec,
    )
    retained_profile = select_artifact_for_execution(
        artifact_rows,
        task_id=prepare_task.id,
        artifact_type="candidate-profile-document",
        execution=prepare_exec,
    )
    for art in (retained_spec, retained_profile):
        spec_path = root / art.relative_path
        if path_crosses_link(spec_path):
            raise CandidateCurrentnessError(f"retained artifact {art.id} path crosses a link")
        on_disk = sha256_file(spec_path)
        if on_disk != art.content_hash:
            raise CandidateCurrentnessError(
                f"retained artifact {art.artifact_type} registration hash drifted"
            )
    spec_doc = load_strict_json_artifact(root, retained_spec)
    profile_doc = load_strict_json_artifact(root, retained_profile)
    try:
        rebound_spec = parse_asset_specification_v08_candidate(spec_doc)
    except Exception as exc:
        raise CandidateCurrentnessError(f"retained specification artifact invalid: {exc}") from exc
    try:
        retained_profile_doc = parse_profile_document_v08_candidate(profile_doc)
    except ProfileContractError as exc:
        raise CandidateCurrentnessError(
            f"retained profile document artifact invalid: {exc}"
        ) from exc
    rebound_profile = AssetProfileV08Candidate(document=retained_profile_doc)
    rebound_spec, rebound_profile = revalidate_candidate_binding(rebound_spec, rebound_profile)
    if candidate_spec_fingerprint(rebound_spec) != prepare_params["specification_hash"]:
        raise CandidateCurrentnessError(
            "retained specification artifact does not match prepare specification_hash"
        )
    if profile_document_hash(rebound_profile.document) != prepare_params["profile_document_hash"]:
        raise CandidateCurrentnessError(
            "retained profile document artifact does not match prepare profile_document_hash"
        )
    if candidate_spec_fingerprint(rebound_spec) != candidate_spec_fingerprint(spec):
        raise CandidateCurrentnessError(
            "retained specification semantics drift from prepare params"
        )
    if profile_document_hash(rebound_profile.document) != profile_document_hash(profile.document):
        raise CandidateCurrentnessError("retained profile semantics drift from prepare params")
    return {
        "candidate_specification_sha256": retained_spec.content_hash,
        "candidate_profile_document_sha256": retained_profile.content_hash,
    }


def _scan_engine_diag(text: str) -> list[str]:
    return [pattern.pattern for pattern in _ENGINE_ERROR_PATTERNS if pattern.search(text)]


def artifacts_in_test_only_approval_scope(
    approval: ApprovalRequest, artifact_rows: list[Artifact]
) -> list[Artifact]:
    """Resolve the exact artifact IDs persisted when the TEST_ONLY approval was created."""
    scope_ids = approval.artifact_ids
    if not scope_ids:
        raise CandidateCurrentnessError("persisted approval artifact scope is empty")
    by_id = {item.id: item for item in artifact_rows}
    scoped: list[Artifact] = []
    seen: set[str] = set()
    for artifact_id in scope_ids:
        if not isinstance(artifact_id, str) or not artifact_id:
            raise CandidateCurrentnessError("persisted approval artifact scope contains invalid id")
        if artifact_id in seen:
            raise CandidateCurrentnessError(
                "persisted approval artifact scope contains duplicate id"
            )
        seen.add(artifact_id)
        row = by_id.get(artifact_id)
        if row is None:
            raise CandidateCurrentnessError(
                f"persisted approval artifact scope references missing artifact {artifact_id}"
            )
        scoped.append(row)
    return scoped


def build_test_only_operation_inputs(
    workflow: Workflow,
    task: Task,
    artifact_rows: list[Artifact],
    approval: ApprovalRequest,
    handler_context: dict[str, Any],
) -> dict[str, object]:
    scoped = artifacts_in_test_only_approval_scope(approval, artifact_rows)
    return build_operation_inputs(
        workflow,
        task,
        scoped,
        task.cost_class,
        None,
        handler_context,
    )


def _bound_request_canonical_equal(lhs: dict[str, Any], rhs: dict[str, Any]) -> bool:
    try:
        left = candidate_runtime_bound_payload_canonical(lhs)
        right = candidate_runtime_bound_payload_canonical(rhs)
    except Exception:
        return False
    return left == right


def _resolve_lexical_path(root: Path, ref: str) -> Path:
    path = Path(ref)
    if path_crosses_link(path):
        raise CandidateCurrentnessError(f"runtime path crosses a link: {ref}")
    if not path.is_absolute():
        lexical = root / path
        if path_crosses_link(lexical):
            raise CandidateCurrentnessError(f"runtime path crosses a link: {ref}")
        return lexical
    return path


_RUNTIME_REQUEST_NAME = "request.json"
_RUNTIME_OBSERVATION_NAME = "observation.json"
_RUNTIME_PROVENANCE_NAME = "provenance.json"
_RUNTIME_IMPORT_LOG_NAME = "godot-import.log"
_RUNTIME_RENDER_LOG_NAME = "godot-render.log"
_RUNTIME_CAPTURES_DIR = "captures"


def _select_unique_runtime_artifact(
    artifact_rows: list[Artifact],
    *,
    task_id: str,
    execution: Execution,
    artifact_type: str,
) -> Artifact:
    matches = [
        item
        for item in artifact_rows
        if item.task_id == task_id
        and item.artifact_type == artifact_type
        and artifact_bound_to_execution(item, execution)
    ]
    if len(matches) != 1:
        raise CandidateCurrentnessError(
            f"expected exactly one registered {artifact_type} for capture execution, "
            f"found {len(matches)}"
        )
    return matches[0]


def _assert_registered_runtime_path(
    root: Path,
    artifact: Artifact,
    stage_resolved: Path,
    *,
    role: str,
    relative_within_stage: str,
) -> Path:
    rel_norm = artifact.relative_path.replace("\\", "/")
    lexical = root / rel_norm
    if path_crosses_link(lexical):
        raise CandidateCurrentnessError(f"registered {role} lexical path crosses a link")
    registered = lexical.resolve()
    expected = (stage_resolved / relative_within_stage).resolve()
    if registered != expected:
        raise CandidateCurrentnessError(
            f"registered {role} must be {relative_within_stage} directly under runtime stage"
        )
    if Path(rel_norm).name != Path(relative_within_stage).name:
        raise CandidateCurrentnessError(f"registered {role} basename mismatch")
    return registered


def _authoritative_runtime_stage(
    root: Path,
    runtime_request: Artifact,
    runtime_obs: Artifact,
    provenance_art: Artifact,
) -> Path:
    rel_norm = runtime_request.relative_path.replace("\\", "/")
    request_lexical = root / rel_norm
    if path_crosses_link(request_lexical):
        raise CandidateCurrentnessError("runtime request lexical path crosses a link")
    if Path(rel_norm).name != _RUNTIME_REQUEST_NAME:
        raise CandidateCurrentnessError("registered runtime request must be request.json")
    stage_resolved = request_lexical.resolve().parent
    _assert_registered_runtime_path(
        root,
        runtime_obs,
        stage_resolved,
        role="runtime observation",
        relative_within_stage=_RUNTIME_OBSERVATION_NAME,
    )
    _assert_registered_runtime_path(
        root,
        provenance_art,
        stage_resolved,
        role="runtime provenance",
        relative_within_stage=_RUNTIME_PROVENANCE_NAME,
    )
    return stage_resolved


def _select_unique_capture_log_artifact(
    artifact_rows: list[Artifact],
    *,
    task_id: str,
    execution: Execution,
    artifact_type: str,
    filename: str,
) -> Artifact:
    matches = [
        item
        for item in artifact_rows
        if item.task_id == task_id
        and item.artifact_type == artifact_type
        and artifact_bound_to_execution(item, execution)
    ]
    if len(matches) != 1:
        raise CandidateCurrentnessError(
            f"expected exactly one registered {artifact_type} for capture execution, "
            f"found {len(matches)}"
        )
    art = matches[0]
    if Path(art.relative_path).name != filename:
        raise CandidateCurrentnessError(f"registered {artifact_type} must use filename {filename}")
    return art


def verify_runtime_b_gate(
    *,
    root: Path,
    workflow_id: str,
    prepare_params: dict[str, Any],
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
    capture_exec: Execution,
    runtime_request: Artifact,
    runtime_obs: Artifact,
    processed_art: Artifact,
    artifact_rows: list[Artifact],
) -> tuple[list[str], dict[str, str]]:
    request_doc = load_strict_json_artifact(root, runtime_request)
    observation = load_strict_json_artifact(root, runtime_obs)
    if request_doc.get("workflow_id") != workflow_id:
        raise CandidateCurrentnessError("runtime request workflow_id mismatch")
    if observation.get("workflow_id") != workflow_id:
        raise CandidateCurrentnessError("runtime observation workflow_id mismatch")
    if request_doc.get("revision") != prepare_params["revision_number"]:
        raise CandidateCurrentnessError("runtime request revision mismatch")
    if observation.get("revision") != prepare_params["revision_number"]:
        raise CandidateCurrentnessError("runtime observation revision mismatch")
    if observation.get("execution_id") != capture_exec.id:
        raise CandidateCurrentnessError("runtime observation execution_id mismatch")
    if observation.get("strict_attempt_number") != capture_exec.attempt_number:
        raise CandidateCurrentnessError("runtime observation attempt mismatch")
    glb_path = root / processed_art.relative_path
    if path_crosses_link(glb_path):
        raise CandidateCurrentnessError("processed GLB path crosses a link for runtime B gate")
    try:
        glb_size = glb_path.stat().st_size
    except OSError as exc:
        raise CandidateCurrentnessError(f"processed GLB unreadable: {exc}") from exc
    if glb_size <= 0 or glb_size > _MAX_FILE_BYTES:
        raise CandidateCurrentnessError("processed GLB size out of bounds for runtime B gate")
    glb_bytes = glb_path.read_bytes()
    if sha256_file(glb_path) != processed_art.content_hash:
        raise CandidateCurrentnessError("processed GLB on-disk hash drifted")
    digest = hashlib.sha256(glb_bytes).hexdigest()
    if digest != processed_art.content_hash:
        raise CandidateCurrentnessError("processed GLB bytes disagree with artifact registration")
    if digest != spec.processed_glb_sha256:
        raise CandidateCurrentnessError("processed GLB does not match specification binding")
    if digest != prepare_params["source_glb_hash"]:
        raise CandidateCurrentnessError("processed GLB does not match retained source hash")
    harness = Path(
        str(resources.files("gamefactory.resources.godot").joinpath("candidate_capsule_harness.gd"))
    )
    provenance_art = _select_unique_runtime_artifact(
        artifact_rows,
        task_id=capture_exec.task_id,
        execution=capture_exec,
        artifact_type="candidate-runtime-provenance",
    )
    stage_resolved = _authoritative_runtime_stage(
        root, runtime_request, runtime_obs, provenance_art
    )
    capture_dir = stage_resolved / _RUNTIME_CAPTURES_DIR
    provenance_path = root / provenance_art.relative_path
    if path_crosses_link(provenance_path):
        raise CandidateCurrentnessError("runtime provenance path crosses a link")
    if sha256_file(provenance_path) != provenance_art.content_hash:
        raise CandidateCurrentnessError("runtime provenance registration hash drifted")
    provenance_doc = load_strict_json_artifact(root, provenance_art)
    registered_request_path = root / runtime_request.relative_path
    if path_crosses_link(registered_request_path):
        raise CandidateCurrentnessError("runtime request path crosses a link for provenance gate")
    expected_stage = registered_request_path.parent
    stage_dir_ref = provenance_doc.get("stage_dir")
    request_path_ref = provenance_doc.get("request_path")
    if not isinstance(stage_dir_ref, str) or not stage_dir_ref:
        raise CandidateCurrentnessError("runtime provenance stage_dir is missing")
    if not isinstance(request_path_ref, str) or not request_path_ref:
        raise CandidateCurrentnessError("runtime provenance request_path is missing")
    stage_lexical = _resolve_lexical_path(root, stage_dir_ref)
    request_lexical = _resolve_lexical_path(root, request_path_ref)
    stage_resolved = stage_lexical.resolve()
    request_resolved = request_lexical.resolve()
    if stage_resolved != expected_stage.resolve():
        raise CandidateCurrentnessError("runtime provenance stage_dir does not match capture stage")
    if request_resolved != registered_request_path.resolve():
        raise CandidateCurrentnessError(
            "runtime provenance request_path does not match registered runtime request"
        )
    if not stage_resolved.is_relative_to(root.resolve()):
        raise CandidateCurrentnessError("runtime provenance stage_dir escapes managed workspace")
    try:
        import_exit_code = strict_process_exit_code(
            provenance_doc.get("import_exit_code"), "import_exit_code"
        )
        process_exit_code = strict_process_exit_code(
            provenance_doc.get("process_exit_code"), "process_exit_code"
        )
    except CandidateRuntimeIntegerError as exc:
        raise CandidateCurrentnessError(f"runtime provenance exit codes invalid: {exc}") from exc
    for timeout_key in ("import_timed_out", "process_timed_out"):
        timed_out = provenance_doc.get(timeout_key)
        if timed_out is not False:
            raise CandidateCurrentnessError(
                f"runtime provenance {timeout_key} must be exactly false"
            )
    if import_exit_code != 0 or process_exit_code != 0:
        raise CandidateCurrentnessError(
            f"runtime provenance records non-zero exits "
            f"(import={import_exit_code}, process={process_exit_code})"
        )
    bound_request = provenance_doc.get("bound_request")
    if not isinstance(bound_request, dict):
        raise CandidateCurrentnessError("runtime provenance bound_request must be an object")
    if not _bound_request_canonical_equal(bound_request, request_doc):
        raise CandidateCurrentnessError(
            "runtime provenance bound_request does not match registered runtime request"
        )
    import_log_art = _select_unique_capture_log_artifact(
        artifact_rows,
        task_id=capture_exec.task_id,
        execution=capture_exec,
        artifact_type="candidate-runtime-import-log",
        filename="godot-import.log",
    )
    render_log_art = _select_unique_capture_log_artifact(
        artifact_rows,
        task_id=capture_exec.task_id,
        execution=capture_exec,
        artifact_type="candidate-runtime-render-log",
        filename="godot-render.log",
    )
    log_hashes: dict[str, str] = {}
    for log_key, hash_key, log_art in (
        ("import_log", "runtime_import_log_sha256", import_log_art),
        ("render_log", "runtime_render_log_sha256", render_log_art),
    ):
        log_ref = provenance_doc.get(log_key)
        if not isinstance(log_ref, str) or not log_ref:
            raise CandidateCurrentnessError(f"runtime provenance missing {log_key}")
        if path_crosses_link(Path(log_ref)):
            raise CandidateCurrentnessError(f"runtime {log_key} lexical path crosses a link")
        log_within_stage = (
            _RUNTIME_IMPORT_LOG_NAME if log_key == "import_log" else _RUNTIME_RENDER_LOG_NAME
        )
        registered_log_path = _assert_registered_runtime_path(
            root,
            log_art,
            stage_resolved,
            role=log_key,
            relative_within_stage=log_within_stage,
        )
        provenance_log_lexical = _resolve_lexical_path(root, log_ref)
        if provenance_log_lexical.resolve() != registered_log_path.resolve():
            raise CandidateCurrentnessError(
                f"runtime provenance {log_key} does not match registered capture log artifact"
            )
        log_path = registered_log_path.resolve()
        if not log_path.is_file():
            raise CandidateCurrentnessError(f"runtime {log_key} file is missing")
        try:
            log_size = log_path.stat().st_size
        except OSError as exc:
            raise CandidateCurrentnessError(f"runtime {log_key} unreadable: {exc}") from exc
        if log_size <= 0 or log_size > MAX_CANDIDATE_JSON_ARTIFACT_BYTES:
            raise CandidateCurrentnessError(f"runtime {log_key} size out of bounds")
        on_disk_hash = sha256_file(log_path)
        if on_disk_hash != log_art.content_hash:
            raise CandidateCurrentnessError(f"runtime {log_key} registration hash drifted")
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CandidateCurrentnessError(f"runtime {log_key} unreadable: {exc}") from exc
        if _scan_engine_diag(log_text):
            raise CandidateCurrentnessError(f"runtime {log_key} contains engine error diagnostics")
        log_hashes[hash_key] = on_disk_hash
    try:
        verify_candidate_runtime_observation(
            observation,
            request_doc,
            spec=spec,
            profile=profile,
            capture_dir=capture_dir,
            glb_path=glb_path,
            glb_bytes=glb_bytes,
            harness_path=harness,
            import_exit_code=import_exit_code,
            process_exit_code=process_exit_code,
        )
    except CandidateRuntimeObservationError as exc:
        raise CandidateCurrentnessError(f"runtime B gate failed: {exc}") from exc
    capture_hashes = sorted(entry["png_sha256"] for entry in observation.get("captures", []))
    if len(capture_hashes) != 9:
        raise CandidateCurrentnessError("runtime capture gate requires exactly nine PNG digests")
    registered_captures = [
        item
        for item in artifact_rows
        if item.task_id == capture_exec.task_id
        and item.artifact_type == "candidate-runtime-capture"
        and artifact_bound_to_execution(item, capture_exec)
    ]
    if len(registered_captures) != 9:
        raise CandidateCurrentnessError(
            f"expected nine registered runtime capture artifacts, found {len(registered_captures)}"
        )
    obs_by_view = {entry["view"]: entry["png_sha256"] for entry in observation.get("captures", [])}
    for art in registered_captures:
        view = Path(art.relative_path).stem
        if view not in obs_by_view:
            raise CandidateCurrentnessError(f"registered capture {view} missing from observation")
        capture_within_stage = f"{_RUNTIME_CAPTURES_DIR}/{view}.png"
        cap_path = _assert_registered_runtime_path(
            root,
            art,
            stage_resolved,
            role=f"runtime capture {view}",
            relative_within_stage=capture_within_stage,
        )
        if path_crosses_link(cap_path):
            raise CandidateCurrentnessError(f"capture path crosses link for view {view}")
        try:
            png_size = cap_path.stat().st_size
        except OSError as exc:
            raise CandidateCurrentnessError(f"capture {view} unreadable: {exc}") from exc
        if png_size <= 0 or png_size > MAX_CANDIDATE_PNG_BYTES:
            raise CandidateCurrentnessError(f"capture {view} size out of bounds")
        on_disk = sha256_file(cap_path)
        if on_disk != art.content_hash:
            raise CandidateCurrentnessError(f"capture {view} registration hash drifted")
        if on_disk != obs_by_view[view]:
            raise CandidateCurrentnessError(f"capture {view} hash does not match observation role")
    log_hashes["runtime_provenance_sha256"] = provenance_art.content_hash
    return capture_hashes, log_hashes


def verify_nested_rig_bundle_cold(bundle_dir: Path) -> None:
    verifier = Path(
        str(resources.files("gamefactory.resources.scripts").joinpath("verify_rig_bundle.py"))
    )
    if path_crosses_link(bundle_dir):
        raise CandidateCurrentnessError("nested rig bundle path crosses a link")
    proc = subprocess.run(
        [sys.executable, "-I", str(verifier), str(bundle_dir.resolve())],
        capture_output=True,
        text=True,
        cwd=str(bundle_dir.parent),
        timeout=NESTED_RIG_COLD_VERIFY_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0:
        first = proc.stdout.splitlines()[0] if proc.stdout else "FAILED"
        raise CandidateCurrentnessError(f"nested rig bundle cold verification failed: {first}")


def _safe_path_under_bundle(bundle_dir: Path, rel: str) -> Path:
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        raise CandidateCurrentnessError(f"unsafe nested rig bundle relative path: {rel!r}")
    root = bundle_dir.resolve()
    path = (bundle_dir / rel).resolve()
    if not path.is_relative_to(root):
        raise CandidateCurrentnessError(f"nested rig bundle path escapes bundle root: {rel}")
    if path_crosses_link(path):
        raise CandidateCurrentnessError(f"nested rig bundle path crosses link: {rel}")
    return path


def _manifest_file_hashes(bundle_dir: Path, manifest: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    files = manifest.get("files")
    if not isinstance(files, list):
        raise CandidateCurrentnessError("rig manifest files must be a list")
    for entry in files:
        if not isinstance(entry, dict):
            raise CandidateCurrentnessError("rig manifest file entry must be an object")
        rel = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(rel, str) or not isinstance(expected, str):
            raise CandidateCurrentnessError("rig manifest entry missing path or sha256")
        path = _safe_path_under_bundle(bundle_dir, rel)
        if not path.is_file():
            raise CandidateCurrentnessError(f"nested rig bundle missing file {rel}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise CandidateCurrentnessError(
                f"nested rig bundle file unreadable {rel}: {exc}"
            ) from exc
        if size <= 0 or size > _MAX_FILE_BYTES:
            raise CandidateCurrentnessError(f"nested rig bundle file size out of bounds: {rel}")
        actual = sha256_file(path)
        if actual != expected:
            raise CandidateCurrentnessError(f"nested rig bundle payload tampered at {rel}")
        out[rel] = actual
    return out


def verify_rig_oracle_gate(
    *,
    root: Path,
    workflow_id: str,
    prepare_params: dict[str, Any],
    oracle_exec: Execution,
    processed_art: Artifact,
    rig_wrapper: Artifact,
    nested_manifest: Artifact,
) -> None:
    wrapper_doc = load_strict_json_artifact(root, rig_wrapper)
    if wrapper_doc.get("workflow_id") != workflow_id:
        raise CandidateCurrentnessError("rig wrapper workflow_id mismatch")
    if wrapper_doc.get("execution_id") != oracle_exec.id:
        raise CandidateCurrentnessError("rig wrapper execution_id mismatch")
    if wrapper_doc.get("attempt_number") != oracle_exec.attempt_number:
        raise CandidateCurrentnessError("rig wrapper attempt_number mismatch")
    if wrapper_doc.get("revision_number") != prepare_params["revision_number"]:
        raise CandidateCurrentnessError("rig wrapper revision_number mismatch")
    if wrapper_doc.get("specification_hash") != prepare_params["specification_hash"]:
        raise CandidateCurrentnessError("rig wrapper specification_hash mismatch")
    if wrapper_doc.get("profile_document_hash") != prepare_params["profile_document_hash"]:
        raise CandidateCurrentnessError("rig wrapper profile_document_hash mismatch")
    if wrapper_doc.get("processed_glb_sha256") != processed_art.content_hash:
        raise CandidateCurrentnessError("rig wrapper processed_glb_sha256 mismatch")
    if wrapper_doc.get("candidate_graph_version") != _CANDIDATE_GRAPH_VERSION:
        raise CandidateCurrentnessError("rig wrapper graph_version mismatch")
    nested_dir_value = wrapper_doc.get("nested_bundle_dir")
    if not isinstance(nested_dir_value, str) or not nested_dir_value:
        raise CandidateCurrentnessError("rig wrapper nested_bundle_dir missing")
    nested_lexical = root / nested_dir_value
    if path_crosses_link(nested_lexical):
        raise CandidateCurrentnessError("nested rig bundle dir crosses a link")
    nested_dir = nested_lexical.resolve()
    manifest_path = root / nested_manifest.relative_path
    if path_crosses_link(manifest_path):
        raise CandidateCurrentnessError("nested rig manifest path crosses a link")
    if manifest_path.resolve().parent != nested_dir:
        raise CandidateCurrentnessError(
            "nested rig manifest path does not match wrapper bundle dir"
        )
    manifest = load_strict_json_artifact(root, nested_manifest)
    glb_sha = manifest.get("glb_sha256")
    if glb_sha != processed_art.content_hash:
        raise CandidateCurrentnessError(
            "nested rig manifest glb_sha256 does not match processed GLB"
        )
    _manifest_file_hashes(nested_dir, manifest)
    verify_nested_rig_bundle_cold(nested_dir)
    on_disk_manifest_hash = sha256_file(manifest_path)
    if on_disk_manifest_hash != nested_manifest.content_hash:
        raise CandidateCurrentnessError("nested rig manifest registration hash drifted")
    wrapper_manifest_hash = wrapper_doc.get("nested_manifest_sha256")
    if wrapper_manifest_hash != nested_manifest.content_hash:
        raise CandidateCurrentnessError("rig wrapper nested_manifest_sha256 mismatch")
    request_rel = "evidence/rig_runtime_request.json"
    obs_rel = "evidence/rig_runtime_observation.json"
    nested_request_path = _safe_path_under_bundle(nested_dir, request_rel)
    nested_obs_path = _safe_path_under_bundle(nested_dir, obs_rel)
    if nested_request_path.is_file() and nested_obs_path.is_file():
        req_payload = parse_strict_runtime_json_object(
            nested_request_path.read_bytes(), max_bytes=MAX_CANDIDATE_JSON_ARTIFACT_BYTES
        )
        obs_payload = parse_strict_runtime_json_object(
            nested_obs_path.read_bytes(), max_bytes=MAX_CANDIDATE_JSON_ARTIFACT_BYTES
        )
        if req_payload.get("glb_sha256") != processed_art.content_hash:
            raise CandidateCurrentnessError("nested rig request glb_sha256 mismatch")
        if obs_payload.get("glb_sha256") != processed_art.content_hash:
            raise CandidateCurrentnessError("nested rig observation glb_sha256 mismatch")
        if obs_payload.get("request_digest") != req_payload.get("request_digest"):
            raise CandidateCurrentnessError("nested rig observation request_digest mismatch")


def validate_test_only_receipt(
    *,
    root: Path,
    receipt_art: Artifact,
    snapshot: Any,
    approvals: ApprovalRepository,
    workflow: Workflow,
    review_task: Task,
    artifacts: list[Artifact],
) -> None:
    receipt = load_strict_json_artifact(root, receipt_art)
    if receipt.get("approval_type") != _CANDIDATE_TEST_ONLY_APPROVAL:
        raise CandidateCurrentnessError("receipt approval_type is not candidate TEST_ONLY")
    if receipt.get("receipt_scope") != _CANDIDATE_RECEIPT_SCOPE:
        raise CandidateCurrentnessError("receipt_scope mismatch")
    if receipt.get("production_eligible") is not False:
        raise CandidateCurrentnessError("receipt production_eligible must be false")
    if receipt.get("promotion_eligible") is not False:
        raise CandidateCurrentnessError("receipt promotion_eligible must be false")
    if receipt.get("workflow_id") != workflow.id:
        raise CandidateCurrentnessError("receipt workflow_id mismatch")
    if receipt.get("task_id") != review_task.id:
        raise CandidateCurrentnessError("receipt task_id mismatch")
    approval_id = receipt.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id:
        raise CandidateCurrentnessError("receipt approval_id is missing")
    approval = approvals.get(approval_id)
    if approval is None:
        raise CandidateCurrentnessError("receipt approval_id is not registered in approvals store")
    if approval.status != ApprovalStatus.APPROVED:
        raise CandidateCurrentnessError("persisted approval is not APPROVED")
    if approval.workflow_id != workflow.id:
        raise CandidateCurrentnessError("persisted approval workflow_id mismatch")
    if approval.task_id != review_task.id:
        raise CandidateCurrentnessError("persisted approval task_id mismatch")
    if approval.approval_type != _CANDIDATE_TEST_ONLY_APPROVAL:
        raise CandidateCurrentnessError("persisted approval approval_type mismatch")
    if receipt.get("approval_id") != approval.id:
        raise CandidateCurrentnessError("receipt approval_id does not match persisted approval")
    handler_context = {
        "workflow_id": workflow.id,
        "revision": review_task.parameters["revision_number"],
        "specification_hash": review_task.parameters["specification_hash"],
        "profile_document_hash": review_task.parameters["profile_document_hash"],
        "snapshot_fingerprint": snapshot.fingerprint(),
        "receipt_scope": _CANDIDATE_RECEIPT_SCOPE,
        "production_eligible": False,
        "promotion_eligible": False,
        "candidate_test_only": True,
    }
    operation_inputs = build_test_only_operation_inputs(
        workflow,
        review_task,
        artifacts,
        approval,
        handler_context,
    )
    expected_hash = compute_operation_hash(
        review_task.id, _CANDIDATE_TEST_ONLY_APPROVAL, operation_inputs
    )
    if approval.operation_hash != expected_hash:
        raise CandidateCurrentnessError(
            "persisted approval operation_hash is stale for approved artifact scope bindings"
        )
    if receipt.get("snapshot_fingerprint") != snapshot.fingerprint():
        raise CandidateCurrentnessError("TEST_ONLY receipt snapshot is stale for current bindings")
    receipt_op = receipt.get("approval_operation_hash") or receipt.get("fingerprint")
    if receipt_op != approval.operation_hash:
        raise CandidateCurrentnessError("TEST_ONLY receipt approval_operation_hash mismatch")
    if receipt.get("approval_status") != ApprovalStatus.APPROVED.value:
        raise CandidateCurrentnessError("TEST_ONLY receipt approval_status invalid")


def assert_all_latest_attempts_completed(
    executions: Any,
    tasks: list[Task],
    *,
    purpose: str,
) -> None:
    from gamefactory.workflows.v08_candidate_currentness import (
        BLOCKING_EXECUTION_STATUSES,
        assert_latest_attempt_completed,
    )

    for task in tasks:
        latest = executions.get_latest_attempt(task.id)
        if latest is None:
            continue
        if latest.status in BLOCKING_EXECUTION_STATUSES:
            raise CandidateCurrentnessError(
                f"{purpose}: task {task.task_type} latest attempt {latest.id} "
                f"has blocking status {latest.status.value}"
            )
        if task.task_type in {
            "v08_candidate_capsule_capture",
            "v08_candidate_rig_oracle",
            "v08_candidate_static_validate",
            "v08_candidate_identity_process",
        }:
            assert_latest_attempt_completed(executions, task, purpose=purpose)


__all__ = [
    "artifacts_in_test_only_approval_scope",
    "build_test_only_operation_inputs",
    "load_strict_json_artifact",
    "parse_bound_spec_profile",
    "assert_static_report_pass",
    "verify_authoritative_asset_revision",
    "verify_retained_prepare_artifacts",
    "verify_nested_rig_bundle_cold",
    "verify_rig_oracle_gate",
    "verify_runtime_b_gate",
    "validate_test_only_receipt",
    "assert_all_latest_attempts_completed",
]
