"""V0.8-3C local candidate WorkflowEngine DAG (through TEST_ONLY review + evidence hook)."""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from gamefactory.adapters.assets.internal_rig_evidence import export_rig_evidence_bundle
from gamefactory.adapters.assets.v08_candidate_evidence import (
    assert_cold_verification_live_binding,
    assert_fresh_managed_staging_container,
    fingerprint_cold_bundle_payload,
    parse_staging_container,
    prepare_evidence_publication_container,
    trusted_cold_verify_candidate_bundle,
)
from gamefactory.adapters.assets.v08_candidate_validate import validate_v08_candidate_glb
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.engines.v08_candidate_runtime_runner import (
    CandidateGuardedProcessRunner,
    CandidateRuntimeExecutionError,
    guard_candidate_process_runner,
    run_v08_candidate_capsule_runtime,
)
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ExecutionRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.approvals.operation_scope import build_operation_inputs
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.asset_contracts import ValidationFindingSeverity as Severity
from gamefactory.core.domain.errors import (
    ArtifactError,
    ToolExecutionError,
    ValidationError,
)
from gamefactory.core.domain.models import (
    Execution,
    ExecutionStatus,
    Task,
    Workflow,
    WorkflowStatus,
    generate_id,
)
from gamefactory.core.domain.v08_candidate_contracts import (
    AssetProfileV08Candidate,
    AssetSpecificationV08Candidate,
    candidate_spec_fingerprint,
    load_packaged_candidate_profile,
    parse_asset_specification_v08_candidate,
    profile_document_hash,
    revalidate_candidate_binding,
)
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandlerMetadata,
    TaskHandlerRegistry,
    TaskHandlerResult,
)
from gamefactory.workflows.v08_candidate_currentness import (
    BLOCKING_EXECUTION_STATUSES,
    CandidateCurrentnessError,
    assert_latest_attempt_completed,
    assert_review_still_current,
    assert_zero_provider_activity,
    load_json_artifact,
    require_observation_execution_match,
    select_artifact_for_execution,
    verify_artifact_bytes_and_hash,
)
from gamefactory.workflows.v08_candidate_gates import (
    build_test_only_operation_inputs,
    validate_test_only_receipt,
)
from gamefactory.workflows.v08_candidate_snapshot import (
    CANDIDATE_GRAPH_VERSION,
    CANDIDATE_RECEIPT_SCOPE,
    CANDIDATE_TEST_ONLY_APPROVAL,
    CandidateBoundSnapshot,
    build_candidate_bound_snapshot,
    candidate_test_only_handler_context,
    is_v08_candidate_graph,
)
from gamefactory.workflows.v08_candidate_workspace import (
    V08CandidateWorkspace,
    create_fresh_v08_candidate_workspace,
    reject_unmanaged_candidate_database,
)


class CandidateC2ExportRequest(Protocol):
    """Minimal context passed to the injectable C2 exporter (implemented in C2 slice)."""

    workflow_id: str
    project_root: Path
    snapshot: CandidateBoundSnapshot
    nested_rig_bundle_dir: Path
    test_only_receipt_path: Path


C2ExportCallback = Callable[[Any], Path]


def strict_godot_version_line(
    godot_executable: Path, runner: ProcessRunner | CandidateGuardedProcessRunner
) -> str:
    result = runner.run(
        CommandRequest(
            args=[str(godot_executable), "--version"],
            cwd=Path.cwd(),
            timeout_seconds=30.0,
            minimal_env=True,
        )
    )
    if result.timed_out or result.exit_code != 0:
        raise ToolExecutionError(
            "Godot --version probe failed",
            exit_code=result.exit_code,
            details={"timed_out": result.timed_out, "stderr": result.stderr[-500:]},
        )
    lines = (result.stdout or result.stderr or "").strip().splitlines()
    if not lines or not lines[0].strip():
        raise ToolExecutionError("Godot --version produced no output")
    return lines[0].strip()


def create_v08_candidate_workflow(
    workspace: V08CandidateWorkspace,
    source_glb: Path | str,
    specification: AssetSpecificationV08Candidate,
    *,
    profile: AssetProfileV08Candidate | None = None,
    workflow_id: str | None = None,
) -> tuple[Workflow, list[Task]]:
    """Create the CLOSED candidate DAG; no provider or production DB side effects."""
    workspace.assert_managed_database()
    reject_unmanaged_candidate_database(
        workspace.db, workspace=workspace, project_root=workspace.root
    )
    project_id = workspace.project_id
    revision_repository = workspace.revision_repository
    if specification.source_kind != "local_verified_rig":
        raise ValidationError("Candidate workflow accepts only local_verified_rig sources")
    bound_profile = profile or load_packaged_candidate_profile()
    spec, bound_profile = revalidate_candidate_binding(specification, bound_profile)
    project = workspace.root.resolve(strict=True)
    source = Path(source_glb).resolve(strict=True)
    if source.suffix.casefold() != ".glb" or not source.is_file():
        raise ValidationError("Candidate source must be a regular .glb file")
    try:
        source_relative = source.relative_to(project).as_posix()
    except ValueError as exc:
        raise ValidationError("Candidate source GLB must live inside the project root") from exc
    source_hash = sha256_file(source)
    spec_hash = candidate_spec_fingerprint(spec)
    wf_id = workflow_id or generate_id("WF-CAND")
    WorkflowRepository(revision_repository.db).save(
        Workflow(
            id=wf_id,
            project_id=project_id,
            name=f"Candidate production: {spec.asset_id}",
            status=WorkflowStatus.PENDING,
        )
    )
    revision = revision_repository.allocate_revision(
        spec.asset_id,
        wf_id,
        spec_hash,
        raw_glb_hash=source_hash,
        profile_id=bound_profile.document.profile_id,
        profile_version=bound_profile.document.version,
    )
    number = revision.revision_number
    asset_dir = f".gamefactory/assets/{spec.asset_id}/r{number:03d}"
    workflow = Workflow(
        id=wf_id,
        project_id=project_id,
        name=f"Candidate production: {spec.asset_id} r{number:03d}",
        status=WorkflowStatus.PENDING,
    )
    doc_hash = profile_document_hash(bound_profile.document)
    common: dict[str, Any] = {
        "graph_version": CANDIDATE_GRAPH_VERSION,
        "source_kind": "local_verified_rig",
        "asset_id": spec.asset_id,
        "revision_number": number,
        "specification": spec.model_dump(mode="json"),
        "specification_hash": spec_hash,
        "profile_document_hash": doc_hash,
        "source_glb": source_relative,
        "source_glb_hash": source_hash,
        "asset_dir": asset_dir,
        "profile_id": bound_profile.document.profile_id,
        "profile_version": bound_profile.document.version,
        "paid_provider_invocations": 0,
    }
    suffixes = (
        "PREPARE",
        "IDENTITY",
        "STATIC",
        "CAPTURE",
        "RIG-ORACLE",
        "TEST-ONLY-REVIEW",
        "EVIDENCE",
    )
    ids = {suffix: f"{wf_id}-{suffix}" for suffix in suffixes}
    tasks = [
        Task(
            id=ids["PREPARE"],
            workflow_id=wf_id,
            name="Retain candidate specification and verified rig source",
            task_type="v08_candidate_prepare",
            parameters=common,
        ),
        Task(
            id=ids["IDENTITY"],
            workflow_id=wf_id,
            name="Identity process (raw bytes equal processed bytes)",
            task_type="v08_candidate_identity_process",
            depends_on=[ids["PREPARE"]],
            parameters=common,
        ),
        Task(
            id=ids["STATIC"],
            workflow_id=wf_id,
            name="Candidate static validation (typed A)",
            task_type="v08_candidate_static_validate",
            depends_on=[ids["IDENTITY"]],
            parameters=common,
        ),
        Task(
            id=ids["CAPTURE"],
            workflow_id=wf_id,
            name="Candidate capsule runtime and nine-view capture (B)",
            task_type="v08_candidate_capsule_capture",
            depends_on=[ids["STATIC"]],
            parameters=common,
            timeout_seconds=240.0,
        ),
        Task(
            id=ids["RIG-ORACLE"],
            workflow_id=wf_id,
            name="Frozen rig oracle evidence bundle (single export)",
            task_type="v08_candidate_rig_oracle",
            depends_on=[ids["CAPTURE"]],
            parameters=common,
            timeout_seconds=240.0,
        ),
        Task(
            id=ids["TEST-ONLY-REVIEW"],
            workflow_id=wf_id,
            name="Candidate TEST_ONLY final review",
            task_type="v08_candidate_test_only_review",
            depends_on=[ids["RIG-ORACLE"]],
            parameters=common,
        ),
        Task(
            id=ids["EVIDENCE"],
            workflow_id=wf_id,
            name="Candidate evidence envelope (C2 hook)",
            task_type="v08_candidate_evidence",
            depends_on=[ids["TEST-ONLY-REVIEW"]],
            parameters=common,
        ),
    ]
    return workflow, tasks


@dataclass
class CandidateWorkflowHandlers:
    root: Path
    artifacts: ArtifactRepository
    revisions: AssetRevisionRepository
    approvals: ApprovalRepository
    executions: ExecutionRepository
    tasks: TaskRepository
    artifact_manager: ArtifactManager
    godot_path: str | None = None
    runner: ProcessRunner | CandidateGuardedProcessRunner | None = None
    c2_export_callback: C2ExportCallback | None = None
    godot_version_probe: (
        Callable[[Path, ProcessRunner | CandidateGuardedProcessRunner], str] | None
    ) = None
    rig_export: Callable[..., Path] = export_rig_evidence_bundle
    capsule_runtime: Callable[..., dict[str, Any]] = run_v08_candidate_capsule_runtime

    def __post_init__(self) -> None:
        self.root = self.root.resolve(strict=True)
        self.runner = guard_candidate_process_runner(self.runner)

    def _rig_staging_paths(self, execution: Execution) -> tuple[Path, Path]:
        tag = execution.id.replace("\\", "_").replace("/", "_")
        bundle_dir = self.root / ".gf" / "rig" / tag
        appdata = self.root / ".gf" / "ga" / tag
        return bundle_dir, appdata

    def _probe_godot_version(self, godot: Path) -> str:
        probe = self.godot_version_probe or strict_godot_version_line
        runner = self.runner or ProcessRunner(sanitize_output=True)
        return probe(godot, runner)

    def _path(self, task: Task, name: str) -> Path:
        relative = f"{task.parameters['asset_dir']}/{name}"
        return PathGuard(self.root).ensure_safe_parent(relative)

    def _register(
        self, workflow: Workflow, task: Task, execution: Execution, kind: str, path: Path
    ) -> str:
        relative = path.resolve(strict=True).relative_to(self.root).as_posix()
        artifact = self.artifact_manager.register_file_artifact(
            workflow.id, task.id, kind, task.task_type, relative
        )
        self.artifacts.save(artifact)
        return artifact.id

    def _spec_and_profile(
        self, task: Task
    ) -> tuple[AssetSpecificationV08Candidate, AssetProfileV08Candidate]:
        spec = parse_asset_specification_v08_candidate(task.parameters["specification"])
        profile = load_packaged_candidate_profile()
        if candidate_spec_fingerprint(spec) != task.parameters["specification_hash"]:
            raise ValidationError("Candidate specification fingerprint changed")
        return revalidate_candidate_binding(spec, profile)

    def _prepare_task(self, workflow_id: str) -> Task:
        rows = self.tasks.list_by_workflow(workflow_id)
        prepare = next((t for t in rows if t.task_type == "v08_candidate_prepare"), None)
        if prepare is None:
            raise ArtifactError("Candidate prepare task is missing")
        return prepare

    def _bound_snapshot(self, workflow: Workflow, prepare: Task) -> CandidateBoundSnapshot:
        return build_candidate_bound_snapshot(
            root=self.root,
            artifact_manager=self.artifact_manager,
            artifacts=self.artifacts,
            revisions=self.revisions,
            approvals=self.approvals,
            executions=self.executions,
            tasks=self.tasks,
            workflow=workflow,
            prepare_task=prepare,
        )

    def _assert_stale_test_only_approval_blocked(
        self, workflow: Workflow, task: Task, snapshot: CandidateBoundSnapshot
    ) -> None:
        approval = next(
            (
                a
                for a in self.approvals.list_by_workflow(workflow.id)
                if a.task_id == task.id and a.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
            ),
            None,
        )
        if approval is None or approval.status.value != "APPROVED":
            return
        handler_context = self._test_only_handler_context(workflow, task, snapshot)
        operation_inputs = build_test_only_operation_inputs(
            workflow,
            task,
            self.artifacts.list_by_workflow(workflow.id),
            approval,
            handler_context,
        )
        expected_hash = compute_operation_hash(
            task.id, CANDIDATE_TEST_ONLY_APPROVAL, operation_inputs
        )
        assert_review_still_current(
            approval_operation_hash=approval.operation_hash,
            current_fingerprint=expected_hash,
            purpose="candidate TEST_ONLY readiness",
        )

    def _guard_review_currentness(self, workflow: Workflow, task: Task) -> CandidateBoundSnapshot:
        try:
            prepare = self._prepare_task(workflow.id)
            snapshot = self._bound_snapshot(workflow, prepare)
            self._assert_stale_test_only_approval_blocked(workflow, task, snapshot)
            return snapshot
        except CandidateCurrentnessError as exc:
            raise ArtifactError(str(exc)) from exc

    def prepare(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        spec, profile = self._spec_and_profile(task)
        guard = PathGuard(self.root)
        source = guard.resolve_safe_path(task.parameters["source_glb"])
        if sha256_file(source) != task.parameters["source_glb_hash"]:
            raise ValidationError("Candidate source GLB changed after workflow creation")
        directory = self._path(task, "specification.json").parent
        directory.mkdir(parents=True, exist_ok=True)
        spec_path = self._path(task, f"specification-{execution.id}.json")
        spec_path.write_text(
            json.dumps(spec.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        profile_path = self._path(task, f"profile-document-{execution.id}.json")
        profile_path.write_text(
            json.dumps(profile.document.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        raw_copy = self._path(task, f"source-retained-{execution.id}.glb")
        shutil.copy2(source, raw_copy)
        if sha256_file(raw_copy) != task.parameters["source_glb_hash"]:
            raise ValidationError("Retained source copy hash mismatch")
        ids = [
            self._register(workflow, task, execution, "candidate-specification", spec_path),
            self._register(workflow, task, execution, "candidate-profile-document", profile_path),
            self._register(workflow, task, execution, "candidate-source-retained", raw_copy),
        ]
        return TaskHandlerResult(
            1, "Candidate specification, profile, and rig source retained with digests", ids
        )

    def identity_process(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        spec, _ = self._spec_and_profile(task)
        prepare = self._prepare_task(workflow.id)
        prepare_exec = assert_latest_attempt_completed(
            self.executions, prepare, purpose="identity process"
        )
        retained = select_artifact_for_execution(
            self.artifacts.list_by_workflow(workflow.id),
            task_id=prepare.id,
            artifact_type="candidate-source-retained",
            execution=prepare_exec,
        )
        verify_artifact_bytes_and_hash(
            retained, root_path=self.root, artifact_manager=self.artifact_manager
        )
        raw_path = self.root / retained.relative_path
        processed = self._path(task, f"processed-{execution.id}.glb")
        raw_out = self._path(task, f"raw-{execution.id}.glb")
        shutil.copy2(raw_path, raw_out)
        shutil.copy2(raw_path, processed)
        raw_hash = sha256_file(raw_out)
        processed_hash = sha256_file(processed)
        if raw_hash != processed_hash:
            raise ValidationError("Identity process violated raw/processed byte equality")
        if processed_hash != spec.processed_glb_sha256:
            raise ValidationError("Processed GLB hash does not match specification binding")
        report = self._path(task, f"identity-{execution.id}.json")
        report.write_text(
            json.dumps(
                {
                    "schema_version": "candidate-identity-report-0.8.0",
                    "raw_sha256": raw_hash,
                    "processed_sha256": processed_hash,
                    "byte_identity": True,
                    "execution_id": execution.id,
                    "attempt_number": execution.attempt_number,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        ids = [
            self._register(workflow, task, execution, "candidate-raw-glb", raw_out),
            self._register(workflow, task, execution, "candidate-processed-glb", processed),
            self._register(workflow, task, execution, "candidate-identity-report", report),
        ]
        revision = self.revisions.get(spec.asset_id, int(task.parameters["revision_number"]))
        if revision is None:
            raise ValidationError("Candidate revision record disappeared")
        return TaskHandlerResult(
            1, "Identity process retained raw and processed GLB with identical bytes", ids
        )

    def static_validate(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        spec, profile = self._spec_and_profile(task)
        identity = next(
            (
                t
                for t in self.tasks.list_by_workflow(workflow.id)
                if t.task_type == "v08_candidate_identity_process"
            ),
            None,
        )
        if identity is None:
            raise ArtifactError("Identity task missing")
        identity_exec = assert_latest_attempt_completed(
            self.executions, identity, purpose="static validate"
        )
        processed = select_artifact_for_execution(
            self.artifacts.list_by_workflow(workflow.id),
            task_id=identity.id,
            artifact_type="candidate-processed-glb",
            execution=identity_exec,
        )
        verify_artifact_bytes_and_hash(
            processed, root_path=self.root, artifact_manager=self.artifact_manager
        )
        path = self.root / processed.relative_path
        result = validate_v08_candidate_glb(path, spec, profile=profile)
        report_payload = result.to_dict()
        report_path = self._path(task, f"static-report-{execution.id}.json")
        report_path.write_text(
            json.dumps(report_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        if result.status != Severity.PASS:
            raise ValidationError("Candidate static validation did not PASS")
        artifact_id = self._register(
            workflow, task, execution, "candidate-static-validation-report", report_path
        )
        return TaskHandlerResult(1, "Candidate static validation PASS", [artifact_id])

    def capsule_capture(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        if not self.godot_path:
            raise ToolExecutionError("Godot executable is not configured for candidate capture")
        spec, profile = self._spec_and_profile(task)
        identity = next(
            (
                t
                for t in self.tasks.list_by_workflow(workflow.id)
                if t.task_type == "v08_candidate_identity_process"
            ),
            None,
        )
        if identity is None:
            raise ArtifactError("Identity task missing")
        identity_exec = assert_latest_attempt_completed(
            self.executions, identity, purpose="capsule capture"
        )
        processed = select_artifact_for_execution(
            self.artifacts.list_by_workflow(workflow.id),
            task_id=identity.id,
            artifact_type="candidate-processed-glb",
            execution=identity_exec,
        )
        glb_path = self.root / processed.relative_path
        godot = Path(self.godot_path)
        self._probe_godot_version(godot)
        out_dir = self._path(task, f"capsule-runtime-{execution.id}")
        try:
            observation = self.capsule_runtime(
                godot,
                glb_path,
                spec,
                profile,
                out_dir,
                workflow_id=workflow.id,
                revision=int(task.parameters["revision_number"]),
                execution_id=execution.id,
                strict_attempt_number=execution.attempt_number,
                runner=self.runner,
            )
        except CandidateRuntimeExecutionError as exc:
            raise ToolExecutionError(str(exc)) from exc
        stage_dirs = [
            p
            for p in out_dir.iterdir()
            if p.is_dir() and p.name.startswith("godot_candidate_stage_")
        ]
        if len(stage_dirs) != 1:
            raise ArtifactError("Candidate runtime stage directory is ambiguous")
        stage = stage_dirs[0]
        request_path = stage / "request.json"
        observation_path = stage / "observation.json"
        if not request_path.is_file() or not observation_path.is_file():
            raise ArtifactError("Candidate runtime did not retain request/observation beside stage")
        require_observation_execution_match(
            observation, execution, purpose="capsule capture handler"
        )
        ids = [
            self._register(workflow, task, execution, "candidate-runtime-request", request_path),
            self._register(
                workflow, task, execution, "candidate-runtime-observation", observation_path
            ),
        ]
        capture_dir = stage / "captures"
        if capture_dir.is_dir():
            for capture in sorted(capture_dir.glob("*.png")):
                ids.append(
                    self._register(
                        workflow,
                        task,
                        execution,
                        "candidate-runtime-capture",
                        capture,
                    )
                )
        import_log = stage / "godot-import.log"
        render_log = stage / "godot-render.log"
        if import_log.is_file():
            ids.append(
                self._register(
                    workflow,
                    task,
                    execution,
                    "candidate-runtime-import-log",
                    import_log,
                )
            )
        if render_log.is_file():
            ids.append(
                self._register(
                    workflow,
                    task,
                    execution,
                    "candidate-runtime-render-log",
                    render_log,
                )
            )
        provenance = stage / "provenance.json"
        if provenance.is_file():
            ids.append(
                self._register(
                    workflow, task, execution, "candidate-runtime-provenance", provenance
                )
            )
        return TaskHandlerResult(
            1, "Candidate capsule runtime completed with verified observation", ids
        )

    def rig_oracle(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        if not self.godot_path:
            raise ToolExecutionError("Godot executable is not configured for rig oracle export")
        _, _profile = self._spec_and_profile(task)
        identity = next(
            (
                t
                for t in self.tasks.list_by_workflow(workflow.id)
                if t.task_type == "v08_candidate_identity_process"
            ),
            None,
        )
        if identity is None:
            raise ArtifactError("Identity task missing")
        identity_exec = assert_latest_attempt_completed(
            self.executions, identity, purpose="rig oracle"
        )
        processed = select_artifact_for_execution(
            self.artifacts.list_by_workflow(workflow.id),
            task_id=identity.id,
            artifact_type="candidate-processed-glb",
            execution=identity_exec,
        )
        glb_path = self.root / processed.relative_path
        godot = Path(self.godot_path)
        self._probe_godot_version(godot)
        bundle_dir, appdata = self._rig_staging_paths(execution)
        bundle_dir.parent.mkdir(parents=True, exist_ok=True)
        appdata.mkdir(parents=True, exist_ok=True)
        bundle_path = self.rig_export(
            glb_path,
            bundle_dir,
            godot_executable=godot,
            appdata_dir=appdata,
            bundle_id=f"candidate-nested-{execution.id}",
            runner=self.runner,
        )
        manifest_path = bundle_path / "manifest.json"
        if not manifest_path.is_file():
            raise ArtifactError("Rig export did not write manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ArtifactError("Rig manifest must be a JSON object")
        wrapper_path = self._path(task, f"rig-attempt-{execution.id}.json")
        wrapper_payload = {
            "schema_version": "candidate-rig-attempt-0.8.0",
            "workflow_id": workflow.id,
            "revision_number": task.parameters["revision_number"],
            "execution_id": execution.id,
            "attempt_number": execution.attempt_number,
            "processed_glb_sha256": processed.content_hash,
            "specification_hash": task.parameters["specification_hash"],
            "profile_document_hash": task.parameters["profile_document_hash"],
            "nested_bundle_dir": bundle_path.relative_to(self.root).as_posix(),
            "nested_bundle_id": manifest.get("bundle_id"),
            "nested_manifest_sha256": sha256_file(manifest_path),
            "candidate_graph_version": CANDIDATE_GRAPH_VERSION,
        }
        wrapper_path.write_text(
            json.dumps(wrapper_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        ids = [
            self._register(
                workflow,
                task,
                execution,
                "candidate-nested-rig-manifest",
                manifest_path,
            ),
            self._register(
                workflow,
                task,
                execution,
                "candidate-rig-attempt-wrapper",
                wrapper_path,
            ),
        ]
        return TaskHandlerResult(
            1,
            "Frozen rig oracle export retained as nested bundle for later C2 reuse",
            ids,
        )

    def _test_only_handler_context(
        self, workflow: Workflow, task: Task, snapshot: CandidateBoundSnapshot
    ) -> dict[str, Any]:
        return candidate_test_only_handler_context(workflow, task, snapshot)

    def _approval_operation_inputs(
        self, workflow: Workflow, task: Task, *, snapshot: CandidateBoundSnapshot | None = None
    ) -> dict[str, object]:
        bound_snapshot = snapshot or self._guard_review_currentness(workflow, task)
        handler_context = self._test_only_handler_context(workflow, task, bound_snapshot)
        artifacts = self.artifacts.list_by_workflow(workflow.id)
        approval = next(
            (
                a
                for a in self.approvals.list_by_workflow(workflow.id)
                if a.task_id == task.id and a.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
            ),
            None,
        )
        if approval is not None and approval.status.value == "APPROVED":
            return build_test_only_operation_inputs(
                workflow, task, artifacts, approval, handler_context
            )
        return build_operation_inputs(
            workflow,
            task,
            artifacts,
            task.cost_class,
            None,
            handler_context,
        )

    def test_only_review_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        snapshot = self._guard_review_currentness(workflow, task)
        return self._test_only_handler_context(workflow, task, snapshot)

    def test_only_review(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult:
        snapshot = self._guard_review_currentness(workflow, task)
        approval = next(
            (
                a
                for a in self.approvals.list_by_workflow(workflow.id)
                if a.task_id == task.id
                and a.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
                and a.status.value == "APPROVED"
            ),
            None,
        )
        if approval is None:
            raise ArtifactError("Candidate TEST_ONLY approval is missing")
        operation_inputs = self._approval_operation_inputs(workflow, task, snapshot=snapshot)
        expected_hash = compute_operation_hash(
            task.id, CANDIDATE_TEST_ONLY_APPROVAL, operation_inputs
        )
        assert_review_still_current(
            approval_operation_hash=approval.operation_hash,
            current_fingerprint=expected_hash,
            purpose="candidate TEST_ONLY review",
        )
        receipt = self._path(task, f"test-only-receipt-{execution.id}.json")
        receipt.write_text(
            json.dumps(
                {
                    "approval_id": approval.id,
                    "approval_type": CANDIDATE_TEST_ONLY_APPROVAL,
                    "status": approval.status.value,
                    "receipt_scope": CANDIDATE_RECEIPT_SCOPE,
                    "production_eligible": False,
                    "promotion_eligible": False,
                    "fingerprint": approval.operation_hash,
                    "snapshot_fingerprint": snapshot.fingerprint(),
                    "approval_operation_hash": approval.operation_hash,
                    "workflow_id": workflow.id,
                    "task_id": task.id,
                    "review_execution_id": execution.id,
                    "approval_status": approval.status.value,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        artifact_id = self._register(
            workflow, task, execution, "candidate-test-only-receipt", receipt
        )
        return TaskHandlerResult(
            1, "Candidate TEST_ONLY review receipt recorded for current snapshot", [artifact_id]
        )

    def evidence(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        assert_zero_provider_activity(self.artifacts.db, workflow.id)
        prepare = self._prepare_task(workflow.id)
        review_task = next(
            (
                t
                for t in self.tasks.list_by_workflow(workflow.id)
                if t.task_type == "v08_candidate_test_only_review"
            ),
            None,
        )
        if review_task is None:
            raise ArtifactError("TEST_ONLY review task missing")
        review_exec = assert_latest_attempt_completed(
            self.executions, review_task, purpose="candidate evidence"
        )
        receipt_art = select_artifact_for_execution(
            self.artifacts.list_by_workflow(workflow.id),
            task_id=review_task.id,
            artifact_type="candidate-test-only-receipt",
            execution=review_exec,
        )
        receipt_doc = load_json_artifact(self.root, receipt_art)
        if receipt_doc.get("promotion_eligible") is not False:
            raise ValidationError("TEST_ONLY receipt must remain promotion-ineligible")
        snapshot_live = self._bound_snapshot(workflow, prepare)
        snapshot_before = CandidateBoundSnapshot(payload=copy.deepcopy(snapshot_live.payload))
        before_bytes = json.dumps(
            snapshot_before.payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        snapshot_payload_canonical_bytes = before_bytes.encode("utf-8")
        review_approval = next(
            (
                a
                for a in self.approvals.list_by_workflow(workflow.id)
                if a.task_id == review_task.id
                and a.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
                and a.status.value == "APPROVED"
            ),
            None,
        )
        if review_approval is None:
            raise ArtifactError("APPROVED TEST_ONLY approval missing for candidate evidence")
        validate_test_only_receipt(
            root=self.root,
            receipt_art=receipt_art,
            snapshot=snapshot_before,
            approvals=self.approvals,
            workflow=workflow,
            review_task=review_task,
            artifacts=self.artifacts.list_by_workflow(workflow.id),
        )
        if self.c2_export_callback is None:
            raise ValidationError(
                "Candidate evidence completion requires C2 trusted cold verification; "
                "C1 blocks evidence task completion"
            )
        oracle_task = next(
            (
                t
                for t in self.tasks.list_by_workflow(workflow.id)
                if t.task_type == "v08_candidate_rig_oracle"
            ),
            None,
        )
        if oracle_task is None:
            raise ArtifactError("Rig oracle task missing")
        oracle_exec = assert_latest_attempt_completed(
            self.executions, oracle_task, purpose="candidate evidence"
        )
        wrapper = select_artifact_for_execution(
            self.artifacts.list_by_workflow(workflow.id),
            task_id=oracle_task.id,
            artifact_type="candidate-rig-attempt-wrapper",
            execution=oracle_exec,
        )
        wrapper_doc = load_json_artifact(self.root, wrapper)
        nested_dir = self.root / str(wrapper_doc["nested_bundle_dir"])
        request = SimpleNamespace(
            workflow_id=workflow.id,
            project_root=self.root,
            snapshot=snapshot_before,
            nested_rig_bundle_dir=nested_dir,
            test_only_receipt_path=self.root / receipt_art.relative_path,
        )
        staging_container = Path(self.c2_export_callback(request))
        if not staging_container.exists():
            raise ArtifactError("C2 export hook returned a missing path")
        latest_evidence = self.executions.get_latest_attempt(task.id)
        if latest_evidence is None or latest_evidence.id != execution.id:
            raise ValidationError(
                "A newer candidate evidence attempt appeared during C2 export hook"
            )
        for row in self.tasks.list_by_workflow(workflow.id):
            latest = self.executions.get_latest_attempt(row.id)
            if latest is None:
                continue
            if row.id == task.id:
                if latest.id != execution.id:
                    raise ValidationError(
                        "A newer candidate evidence attempt appeared during C2 export hook"
                    )
                if latest.status == ExecutionStatus.RUNNING and latest.id == execution.id:
                    continue
            if latest.status not in BLOCKING_EXECUTION_STATUSES:
                continue
            raise ValidationError(
                f"Execution {latest.id} entered blocking status during C2 export hook"
            )
        try:
            snapshot_after = self._bound_snapshot(workflow, prepare)
        except (CandidateCurrentnessError, ArtifactError) as exc:
            raise ValidationError(
                "Authoritative candidate snapshot changed during C2 export hook"
            ) from exc
        after_bytes = json.dumps(
            snapshot_after.payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if before_bytes != after_bytes:
            raise ValidationError("Authoritative candidate snapshot changed during C2 export hook")
        assert_fresh_managed_staging_container(self.root, staging_container)
        layout = parse_staging_container(staging_container)
        digest_before_cold = fingerprint_cold_bundle_payload(layout.cold_bundle_dir)
        cold_result = trusted_cold_verify_candidate_bundle(layout.cold_bundle_dir)
        digest_after_cold = fingerprint_cold_bundle_payload(layout.cold_bundle_dir)
        if digest_before_cold != digest_after_cold:
            raise ValidationError("cold verification mutated staged candidate evidence payload")
        assert_cold_verification_live_binding(
            cold_result=cold_result,
            cold_bundle_dir=layout.cold_bundle_dir,
            snapshot=snapshot_before,
            workflow_id=workflow.id,
            snapshot_payload_canonical_bytes=snapshot_payload_canonical_bytes,
            review_approval_id=review_approval.id,
        )
        layout, d_ready = prepare_evidence_publication_container(
            layout,
            workflow_id=workflow.id,
            task_id=task.id,
            execution_id=execution.id,
            attempt_number=execution.attempt_number,
            snapshot_before=snapshot_before,
            cold_result=cold_result,
            cold_bundle_payload_digest=digest_after_cold,
        )
        from gamefactory.workflows.v08_candidate_evidence_publication import (
            publish_candidate_evidence_from_staging,
        )

        return publish_candidate_evidence_from_staging(
            self,
            workflow,
            task,
            execution,
            snapshot_before=snapshot_before,
            staging_container=layout.container_root,
            d_ready=d_ready,
        )


def register_v08_candidate_handlers(
    registry: TaskHandlerRegistry, handlers: CandidateWorkflowHandlers
) -> None:
    registry.register("v08_candidate_prepare", handlers.prepare)
    registry.register(
        "v08_candidate_identity_process",
        handlers.identity_process,
        TaskHandlerMetadata(operation=HandlerOperation.PROCESS_EXECUTION),
    )
    registry.register("v08_candidate_static_validate", handlers.static_validate)
    registry.register(
        "v08_candidate_capsule_capture",
        handlers.capsule_capture,
        TaskHandlerMetadata(
            operation=HandlerOperation.PROCESS_EXECUTION,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
        ),
    )
    registry.register(
        "v08_candidate_rig_oracle",
        handlers.rig_oracle,
        TaskHandlerMetadata(
            operation=HandlerOperation.PROCESS_EXECUTION,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
        ),
    )
    registry.register(
        "v08_candidate_test_only_review",
        handlers.test_only_review,
        TaskHandlerMetadata(
            operation=HandlerOperation.LOCAL_READ,
            mandatory_approval_type=CANDIDATE_TEST_ONLY_APPROVAL,
            approval_context=handlers.test_only_review_context,
        ),
    )
    registry.register("v08_candidate_evidence", handlers.evidence)


def candidate_workflow_readiness(
    handlers: CandidateWorkflowHandlers, workflow_id: str
) -> CandidateBoundSnapshot:
    """Upstream pre-evidence currentness query (C1 bindings through TEST_ONLY receipt).

    Does not attest candidate evidence envelope completion; use
    ``candidate_evidence_readiness`` after the evidence task has published.
    """
    tasks = handlers.tasks.list_by_workflow(workflow_id)
    prepare = next((t for t in tasks if t.task_type == "v08_candidate_prepare"), None)
    if prepare is None:
        raise ValidationError("Not a candidate workflow")
    workflow = WorkflowRepository(handlers.artifacts.db).get(workflow_id)
    if workflow is None:
        raise ValidationError("Workflow is missing")
    return build_candidate_bound_snapshot(
        root=handlers.root,
        artifact_manager=handlers.artifact_manager,
        artifacts=handlers.artifacts,
        revisions=handlers.revisions,
        approvals=handlers.approvals,
        executions=handlers.executions,
        tasks=handlers.tasks,
        workflow=workflow,
        prepare_task=prepare,
    )


__all__ = [
    "C2ExportCallback",
    "CandidateWorkflowHandlers",
    "V08CandidateWorkspace",
    "candidate_workflow_readiness",
    "create_v08_candidate_workflow",
    "create_fresh_v08_candidate_workspace",
    "is_v08_candidate_graph",
    "register_v08_candidate_handlers",
    "strict_godot_version_line",
]
