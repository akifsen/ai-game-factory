"""Install only a completed, human-accepted asset revision into its declared game path."""

from __future__ import annotations

import hashlib
import importlib
import json
import json as _json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Protocol, cast

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    EvidenceRepository,
    ExecutionRepository,
    ProjectRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    AssetSpecificationV07,
    spec_fingerprint,
)
from gamefactory.core.domain.asset_installation import AssetInstallationSnapshot
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import (
    ApprovalStatus,
    CostClass,
    Execution,
    ExecutionStatus,
    GateStatus,
    Task,
    Workflow,
    WorkflowStatus,
    generate_id,
)
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandlerMetadata,
    TaskHandlerRegistry,
    TaskHandlerResult,
)

_MAX_GLB_BYTES = 512 * 1024 * 1024


class _FcntlApi(Protocol):
    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, descriptor: int, operation: int) -> None: ...


class _InstallationLock:
    """Stable OS lock file; ownership metadata is never used as the lock primitive."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> None:
        _reject_project_links(self.path)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            before = self.path.lstat()
            opened = os.fstat(self.fd)
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                raise ValidationError("Installation OS lock changed while opening")
            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b"\0")
                os.fsync(self.fd)
            if os.name == "nt":
                import msvcrt

                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl = cast(_FcntlApi, importlib.import_module("fcntl"))
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ValidationError:
            os.close(self.fd)
            self.fd = None
            raise
        except (OSError, ImportError) as exc:
            os.close(self.fd)
            self.fd = None
            raise ValidationError(
                "Another asset installation currently owns this target lock"
            ) from exc

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl = cast(_FcntlApi, importlib.import_module("fcntl"))
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None


def _wrapper_scene(spec: AssetSpecification | AssetSpecificationV07) -> bytes:
    if isinstance(spec, AssetSpecificationV07):
        profile = spec.bound_profile()
        contract = profile.scene_contract(spec)
        expected_mesh_names = profile.expected_mesh_names(spec)
    else:
        profile = spec.bound_profile()
        contract = profile.scene_contract()
        expected_mesh_names = profile.expected_mesh_names(spec)
    physics = contract.get("physics")
    if physics not in {"StaticBody3D", "Area3D"}:
        raise ValidationError("Accepted profile has an unsupported Godot physics node")
    if isinstance(spec, AssetSpecificationV07):
        shape = "CapsuleShape3D" if spec.collider.policy == "capsule" else "BoxShape3D"
        capsule = spec.collider.capsule
    else:
        shape, capsule = "BoxShape3D", None
    dims = spec.dimensions
    if shape == "CapsuleShape3D":
        if capsule is None:
            raise ValidationError("Capsule collider is missing its accepted dimensions")
        shape_data = f"radius = {capsule.radius_m}\nheight = {capsule.height_m}\n"
    else:
        shape_data = f"size = Vector3({dims.width_m}, {dims.height_m}, {dims.depth_m})\n"
    y = dims.height_m / 2 if spec.origin_policy == "bottom_center" else 0.0
    asset = spec.asset_id
    import_dir = spec.target_import_path.replace("\\", "/").strip("/")
    resource_path = f"res://{import_dir}/{asset}.glb"
    accepted_lod0 = sorted(name for name in expected_mesh_names if name.endswith("_LOD0"))
    if not accepted_lod0:
        raise ValidationError("Accepted profile does not declare any visible LOD0 mesh names")
    runtime_script = (
        "extends Node3D\n"
        f"const ACCEPTED_LOD0_NAMES := {_json.dumps(accepted_lod0, ensure_ascii=False)}\n\n"
        "func _ready() -> void:\n"
        '    _apply_accepted_lod0(get_node("Visual"))\n\n'
        "func _apply_accepted_lod0(node: Node) -> void:\n"
        "    if node is MeshInstance3D:\n"
        "        var mesh_instance := node as MeshInstance3D\n"
        "        mesh_instance.visible = str(mesh_instance.name) in ACCEPTED_LOD0_NAMES\n"
        "    for child in node.get_children():\n"
        "        _apply_accepted_lod0(child)\n"
    )
    text = (
        "[gd_scene load_steps=4 format=3]\n\n"
        f'[ext_resource type="PackedScene" path={_json.dumps(resource_path)} id="1"]\n'
        f'[sub_resource type="{shape}" id="AcceptedCollider"]\n{shape_data}\n'
        '[sub_resource type="GDScript" id="AcceptedVisualPolicy"]\n'
        f"script/source = {_json.dumps(runtime_script, ensure_ascii=False)}\n\n"
        f'[node name="{asset}" type="Node3D"]\n\n'
        'script = SubResource("AcceptedVisualPolicy")\n\n'
        f'[node name="Visual" parent="." instance=ExtResource("1")]\n\n'
        f'[node name="{physics}" type="{physics}" parent="."]\n\n'
        f'[node name="CollisionShape3D" type="CollisionShape3D" parent="{physics}"]\n'
        'shape = SubResource("AcceptedCollider")\n'
        f"position = Vector3(0, {y}, 0)\n"
    )
    return text.encode("utf-8")


def _safe_root(root: Path | str) -> Path:
    original = Path(root)
    current = original.absolute()
    while current != current.parent:
        if _is_reparse(current):
            raise ValidationError("Game project path cannot contain a symbolic link or junction")
        current = current.parent
    resolved = original.resolve(strict=True)
    if not resolved.is_dir():
        raise ValidationError("Game project root must be an existing directory")
    return resolved


def _installation_lock_identity(relative_targets: dict[str, str]) -> str:
    # Windows project paths alias by case. Case-fold on every host so checkout
    # behavior and concurrent writer serialization remain portable.
    normalized = sorted(value.replace("\\", "/").casefold() for value in relative_targets.values())
    return hashlib.sha256("\0".join(normalized).encode("utf-8")).hexdigest()


def _is_reparse(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    if os.name == "nt":
        try:
            return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
        except FileNotFoundError:
            return False
    return False


def _sha256(path: Path, limit: int = _MAX_GLB_BYTES) -> tuple[str, int]:
    if _is_reparse(path):
        raise ValidationError("Asset source cannot be a link or junction")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise ValidationError("Asset must be a bounded regular GLB file")
        digest, total = hashlib.sha256(), 0
        while total <= limit:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValidationError("Asset grew beyond the installation size limit")
            digest.update(chunk)
        if total != st.st_size:
            raise ValidationError("Asset size changed while hashing")
        return digest.hexdigest(), total
    finally:
        os.close(fd)


def _read_json_bounded(path: Path, limit: int) -> dict[str, Any]:
    _reject_project_links(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValidationError("Installation recovery metadata is not a bounded regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ValidationError("Installation recovery metadata changed while opening")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise ValidationError("Installation recovery metadata exceeds its size limit")
    finally:
        os.close(fd)
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValidationError("Installation recovery metadata must be a JSON object")
    return value


def _create_baseline_backup(source: Path, backup: Path, expected_hash: str) -> None:
    """Copy a target into an exclusive journal file; never hard-link mutable user data."""
    if backup.exists():
        if _sha256(backup)[0] != expected_hash:
            raise ValidationError("Existing baseline journal does not match the approved hash")
        return
    _reject_project_links(source)
    _reject_project_links(backup.parent)
    temporary = backup.with_name(f".{backup.name}.{uuid.uuid4().hex}.tmp")
    before = source.lstat()
    source_fd = os.open(
        source, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    digest, total = hashlib.sha256(), 0
    try:
        source_stat = os.fstat(source_fd)
        if (
            not stat.S_ISREG(source_stat.st_mode)
            or (before.st_dev, before.st_ino) != (source_stat.st_dev, source_stat.st_ino)
            or source_stat.st_size > _MAX_GLB_BYTES
        ):
            raise ValidationError("Revision baseline must be a bounded regular file")
        with temporary.open("xb") as destination:
            while True:
                chunk = os.read(source_fd, min(1024 * 1024, _MAX_GLB_BYTES + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_GLB_BYTES:
                    raise ValidationError("Revision baseline grew beyond the backup limit")
                digest.update(chunk)
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if digest.hexdigest() != expected_hash:
            raise ValidationError(
                "Target drifted from its approved baseline; original remains untouched"
            )
        try:
            os.link(temporary, backup)
        except FileExistsError:
            if _sha256(backup)[0] != expected_hash:
                raise ValidationError(
                    "Concurrent baseline journal has an unexpected hash"
                ) from None
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        os.close(source_fd)
    temporary.unlink(missing_ok=True)


def _revision_number(value: int | str) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"r[0-9]{3,8}", value):
        return int(value[1:])
    raise ValidationError("revision_id must be a positive number or rNNN identifier")


def _accepted_context(
    project_id: str,
    root: Path,
    source_workflow_id: str,
    revision_number: int,
    db: Database,
    replace_baseline_sha256: dict[str, str] | None = None,
    *,
    check_targets: bool = True,
) -> tuple[AssetInstallationSnapshot, Any, Any]:
    workflows, tasks = WorkflowRepository(db), TaskRepository(db)
    source_workflow = workflows.get(source_workflow_id)
    if (
        source_workflow is None
        or source_workflow.project_id != project_id
        or source_workflow.status != WorkflowStatus.COMPLETED
    ):
        raise ValidationError("Source asset workflow must belong to this project and be completed")
    project = ProjectRepository(db).get(project_id)
    if project is None or Path(project.root_path).resolve(strict=True) != root:
        raise ValidationError("Target root must match the registered project root")
    source_tasks = tasks.list_by_workflow(source_workflow_id)
    if not source_tasks or any(task.status.value != "COMPLETED" for task in source_tasks):
        raise ValidationError("Every source asset task must be completed")
    gates = QualityGateRepository(db)
    for task in source_tasks:
        task_gates = gates.list_by_task(task.id)
        latest: dict[str, Any] = {}
        for gate in sorted(task_gates, key=lambda item: (item.evaluated_at or "", item.id)):
            latest[gate.gate_type] = gate
        if not latest or any(gate.status != GateStatus.PASSED for gate in latest.values()):
            raise ValidationError("Source asset workflow is missing a passed task quality gate")
    review_tasks = [task for task in source_tasks if task.task_type == "asset_final_review"]
    if len(review_tasks) != 1:
        raise ValidationError("Source workflow must have one final visual review task")
    review = review_tasks[0]
    if int(review.parameters.get("revision_number", -1)) != revision_number:
        raise ValidationError("Final review task is bound to a different asset revision")
    reviews = [
        approval
        for approval in ApprovalRepository(db).list_by_workflow(source_workflow_id)
        if approval.task_id == review.id and approval.approval_type == "final_visual_review"
    ]
    if not reviews:
        raise ValidationError("Source asset requires a final visual review record")
    approval = max(reviews, key=lambda item: (item.requested_at, item.id))
    if approval.status != ApprovalStatus.APPROVED:
        raise ValidationError("Latest final visual review is not approved")
    from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
    from gamefactory.adapters.persistence.repositories import ProviderOperationIntentRepository
    from gamefactory.workflows.asset_production import (
        AssetProductionHandlers,
        register_asset_production_handlers,
    )

    approval_engine = WorkflowEngine(root, db, asset_provider=None)
    source_handler = AssetProductionHandlers(
        root,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ProviderOperationIntentRepository(db),
        approval_engine.evi_repo,
        approval_engine.gate_repo,
        ExecutionRepository(db),
        ArtifactManager(root),
        FakeAssetGenerationProvider(),
    )
    register_asset_production_handlers(approval_engine.handler_registry, source_handler)
    inputs = approval_engine.approval_inputs(
        source_workflow, review, approval.cost_class, artifact_ids=approval.artifact_ids
    )
    if compute_operation_hash(review.id, approval.approval_type, inputs) != approval.operation_hash:
        raise ValidationError(
            "Final visual acceptance fingerprint no longer matches the source workflow"
        )
    spec_data = review.parameters.get("specification")
    if not isinstance(spec_data, dict):
        raise ValidationError("Final review task lacks its accepted specification snapshot")
    spec = (
        AssetSpecificationV07 if spec_data.get("schema_version") == "0.7.0" else AssetSpecification
    ).model_validate(spec_data)
    spec_hash = spec_fingerprint(spec)
    revision = next(
        (
            row
            for row in AssetRevisionRepository(db).list_by_workflow(source_workflow_id)
            if row.revision_number == revision_number
        ),
        None,
    )
    if (
        revision is None
        or revision.workflow_id != source_workflow_id
        or revision.asset_id != spec.asset_id
        or revision.spec_hash != spec_hash
    ):
        raise ValidationError("Selected asset revision does not match the accepted specification")
    candidates = [
        artifact
        for artifact in ArtifactRepository(db).list_by_workflow(source_workflow_id)
        if artifact.id in approval.artifact_ids
        and artifact.task_id != review.id
        and artifact.artifact_type == "asset-processed-glb"
        and artifact.content_hash == revision.processed_glb_hash
    ]
    if len(candidates) != 1:
        raise ValidationError("Selected revision has no unique processed GLB artifact")
    artifact = candidates[0]
    _reject_project_links(root / Path(artifact.relative_path))
    ArtifactManager(root).verify_artifact_integrity(artifact)
    source = ArtifactManager(root).path_guard.resolve_safe_path(artifact.relative_path)
    current_hash, _ = _sha256(source)
    if current_hash != artifact.content_hash:
        raise ArtifactError("Accepted GLB artifact hash changed")
    import_dir = spec.target_import_path.rstrip("/\\")
    bundle = {
        f"{spec.asset_id}.glb": (current_hash, source.stat().st_size),
        f"{spec.asset_id}.tscn": (
            hashlib.sha256(_wrapper_scene(spec)).hexdigest(),
            len(_wrapper_scene(spec)),
        ),
    }
    baseline: dict[str, str] = {}
    for name in bundle:
        relative_target = (Path(import_dir) / name).as_posix()
        current_path = root / Path(relative_target)
        _reject_project_links(current_path)
        target = PathGuard(root).resolve_safe_path(relative_target)
        if target.exists():
            if not target.is_file():
                raise ValidationError("Import component exists and is not a regular file")
            digest, _ = _sha256(target)
            baseline[name] = digest
    if check_targets:
        if baseline and replace_baseline_sha256 is None:
            raise ValidationError(
                "Import components already exist; explicit revision approval with exact baseline hashes is required"
            )
        if baseline != (replace_baseline_sha256 or {}):
            raise ValidationError(
                "Target components do not match the requested absence or exact revision baseline"
            )
    else:
        baseline = dict(replace_baseline_sha256 or {})
    snapshot = AssetInstallationSnapshot(
        project_id,
        str(root),
        source_workflow_id,
        spec.asset_id,
        revision_number,
        spec_hash,
        spec.target_import_path,
        artifact.id,
        artifact.content_hash,
        approval.id,
        approval.operation_hash,
        baseline or None,
    )
    snapshot.validate()
    return (
        snapshot,
        spec,
        {
            "revision": revision,
            "artifact": artifact,
            "source": source,
            "bundle": bundle,
            "wrapper": _wrapper_scene(spec),
        },
    )


def build_asset_installation_workflow(
    project_id: str,
    root: Path | str,
    source_workflow_id: str,
    revision_id: int | str,
    *,
    db: Database,
    replace_baseline_sha256: dict[str, str] | None = None,
) -> tuple[Workflow, list[Task]]:
    """Prepare one exact accepted revision install; existing files require an explicitly bound baseline hash."""
    target_root = _safe_root(root)
    snapshot, _, _ = _accepted_context(
        project_id,
        target_root,
        source_workflow_id,
        _revision_number(revision_id),
        db,
        replace_baseline_sha256,
    )
    workflow_id = generate_id("WF-ASSET-INSTALL")
    install_id, evidence_id = f"{workflow_id}-INSTALL", f"{workflow_id}-EVIDENCE"
    workflow = Workflow(
        id=workflow_id,
        project_id=project_id,
        name=f"Install accepted asset {snapshot.asset_id} r{snapshot.revision_number:03d}",
    )
    task_type = (
        "asset_install_revision" if snapshot.target_baseline_sha256 is not None else "asset_install"
    )
    task = Task(
        id=install_id,
        workflow_id=workflow_id,
        name="Install accepted GLB into game project",
        task_type=task_type,
        cost_class=CostClass.LOCAL,
        parameters={"snapshot": snapshot.to_dict(), "snapshot_sha256": snapshot.fingerprint()},
        max_retries=1,
        timeout_seconds=120,
    )
    final = Task(
        id=evidence_id,
        workflow_id=workflow_id,
        name="Record installed asset evidence",
        task_type="record_evidence",
        depends_on=[install_id],
        cost_class=CostClass.LOCAL,
    )
    return workflow, [task, final]


class AssetInstallationHandlers:
    def __init__(
        self,
        root: Path | str,
        db: Database,
        artifacts: ArtifactRepository,
        evidence: EvidenceRepository,
        gates: QualityGateRepository,
        executions: ExecutionRepository,
        artifact_manager: ArtifactManager,
    ) -> None:
        self.root = _safe_root(root)
        self.db, self.artifacts, self.evidence, self.gates, self.executions = (
            db,
            artifacts,
            evidence,
            gates,
            executions,
        )
        self.artifact_manager = artifact_manager

    def _refresh(self, task: Task) -> dict[str, Any]:
        payload = task.parameters
        snapshot_data = payload.get("snapshot")
        approved = AssetInstallationSnapshot.from_dict(snapshot_data)
        if approved.fingerprint() != payload.get("snapshot_sha256"):
            raise ValidationError("Installation snapshot fingerprint changed")
        current, spec, components = _accepted_context(
            approved.project_id,
            self.root,
            approved.source_workflow_id,
            approved.revision_number,
            self.db,
            approved.target_baseline_sha256,
            check_targets=False,
        )
        if current != approved:
            raise ValidationError(
                "Accepted asset inputs or target baseline changed; create a new install workflow"
            )
        intent_path = (
            self.root
            / ".gamefactory"
            / "asset-installations"
            / approved.fingerprint()
            / "intent.json"
        )
        _reject_project_links(intent_path)
        intent_exists = intent_path.is_file()
        if intent_exists:
            try:
                intent = _read_json_bounded(intent_path, 16 * 1024)
            except (OSError, ValueError) as exc:
                raise ValidationError("Installation recovery intent is invalid") from exc
            if intent.get("snapshot_sha256") != approved.fingerprint():
                raise ValidationError("Recovery intent does not match approved install inputs")
            expected_targets = {
                name: (Path(approved.target_import_path) / name).as_posix()
                for name in components["bundle"]
            }
            expected_intent = {
                "snapshot_sha256": approved.fingerprint(),
                "source_sha256": approved.accepted_artifact_sha256,
                "targets": expected_targets,
                "baseline_sha256": approved.target_baseline_sha256,
            }
            if intent != expected_intent:
                raise ValidationError(
                    "Recovery intent does not match the exact approved source, targets, and baseline"
                )
        import_dir = Path(approved.target_import_path.rstrip("/\\"))
        backup_dir = intent_path.parent / "baseline-backups"
        for name, (expected_hash, _) in components["bundle"].items():
            target = PathGuard(self.root).resolve_safe_path((import_dir / name).as_posix())
            baseline = (approved.target_baseline_sha256 or {}).get(name)
            recovery_files = []
            for recovery_file in target.parent.glob(f".{target.name}.gamefactory-recovery-*"):
                recovery_files.append(recovery_file)
                if len(recovery_files) > 32:
                    raise ValidationError(
                        "Target has too many recovery files; operator reconciliation is required"
                    )
            for recovery_file in recovery_files:
                _reject_project_links(recovery_file)
                if (
                    baseline is None
                    or not recovery_file.is_file()
                    or _sha256(recovery_file)[0] != baseline
                ):
                    raise ValidationError(
                        "A concurrent edit was preserved in a recovery file; operator reconciliation is required"
                    )
            if target.exists():
                actual, _ = _sha256(target)
                if actual != expected_hash and (baseline is None or actual != baseline):
                    raise ValidationError(
                        "Install target changed from both its approved baseline and accepted output"
                    )
                if actual == expected_hash and not intent_exists:
                    raise ValidationError(
                        "Unexpected existing output has no matching durable installation intent"
                    )
            elif baseline is not None:
                backup = backup_dir / f"{name}.sha256-{baseline}"
                _reject_project_links(backup)
                if not backup.is_file() or _sha256(backup)[0] != baseline:
                    raise ValidationError("Approved revision baseline disappeared before recovery")
        return dict(payload)

    def refresh_parameters(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        del workflow
        return self._refresh(task)

    def approval_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        del workflow
        snapshot = task.parameters["snapshot"]
        return {
            **snapshot,
            "snapshot_sha256": task.parameters["snapshot_sha256"],
            "install_filename": f"{snapshot['asset_id']}.glb",
        }

    def install(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        params = self._refresh(task)
        snapshot = AssetInstallationSnapshot.from_dict(params["snapshot"])
        _, spec, components = _accepted_context(
            snapshot.project_id,
            self.root,
            snapshot.source_workflow_id,
            snapshot.revision_number,
            self.db,
            snapshot.target_baseline_sha256,
            check_targets=False,
        )
        asset_row = ArtifactRepository(self.db).get(snapshot.accepted_artifact_id)
        if asset_row is None or asset_row.workflow_id != snapshot.source_workflow_id:
            raise ValidationError("Accepted artifact is outside its source workflow")
        self.artifact_manager.verify_artifact_integrity(asset_row)
        source = self.artifact_manager.path_guard.resolve_safe_path(asset_row.relative_path)
        source_hash, source_size = _sha256(source)
        if source_hash != snapshot.accepted_artifact_sha256:
            raise ValidationError("Accepted asset hash changed after approval")
        guard = PathGuard(self.root)
        import_dir = Path(snapshot.target_import_path.rstrip("/\\"))
        relative_targets = {name: (import_dir / name).as_posix() for name in components["bundle"]}
        for relative in relative_targets.values():
            _reject_project_links(self.root / Path(relative))
        lock_key = _installation_lock_identity(relative_targets)
        lock_relative = Path(".gamefactory") / "locks" / lock_key / "owner.json"
        _reject_project_links(self.root / lock_relative)
        locks = guard.ensure_safe_parent(lock_relative)
        lock_dir = locks.parent
        lock_dir.mkdir(exist_ok=True)
        lock_file = locks
        token = uuid.uuid4().hex
        os_lock = _InstallationLock(lock_dir / "operation.lock")
        os_lock.acquire()
        try:
            if lock_file.exists():
                self._recover_stale_lock(lock_file, workflow.id, task.id, execution.id)
            self._write_lock_owner(
                lock_file,
                {
                    "workflow_id": workflow.id,
                    "task_id": task.id,
                    "execution_id": execution.id,
                    "lock_token": token,
                },
            )
        except Exception:
            os_lock.release()
            raise
        try:
            for relative in relative_targets.values():
                target = guard.resolve_safe_path(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                _reject_project_links(target)
            intent_relative = (
                Path(".gamefactory")
                / "asset-installations"
                / params["snapshot_sha256"]
                / "intent.json"
            )
            _reject_project_links(self.root / intent_relative)
            intent_dir = guard.ensure_safe_parent(intent_relative).parent
            intent_dir.mkdir(parents=True, exist_ok=True)
            intent_path = intent_dir / "intent.json"
            intent = {
                "snapshot_sha256": params["snapshot_sha256"],
                "source_sha256": source_hash,
                "targets": relative_targets,
                "baseline_sha256": snapshot.target_baseline_sha256,
            }
            if intent_path.exists():
                if _read_json_bounded(intent_path, 16 * 1024) != intent:
                    raise ValidationError(
                        "Existing installation recovery intent does not match approved inputs"
                    )
            else:
                with intent_path.open("x", encoding="utf-8") as stream:
                    json.dump(intent, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
            backup_dir = intent_dir / "baseline-backups"
            _reject_project_links(backup_dir)
            backup_dir.mkdir(exist_ok=True)
            wrapper = components["wrapper"]
            payloads = {f"{snapshot.asset_id}.glb": source, f"{snapshot.asset_id}.tscn": wrapper}
            component_receipts = []
            for name, payload in payloads.items():
                target = guard.resolve_safe_path(relative_targets[name])
                expected_hash, expected_size = components["bundle"][name]
                if target.exists() and _sha256(target)[0] == expected_hash:
                    component_receipts.append(
                        {
                            "path": relative_targets[name],
                            "sha256": expected_hash,
                            "size": expected_size,
                            "recovered": True,
                        }
                    )
                    continue
                baseline = (snapshot.target_baseline_sha256 or {}).get(name)
                backup_path = (
                    backup_dir / f"{name}.sha256-{baseline}" if baseline is not None else None
                )
                component_receipts.append(
                    {
                        "path": relative_targets[name],
                        **self._copy_component(
                            payload,
                            target,
                            snapshot,
                            expected_hash,
                            expected_size,
                            baseline,
                            backup_path,
                        ),
                        "recovered": False,
                    }
                )
            result = {
                "schema_version": 1,
                "snapshot_sha256": params["snapshot_sha256"],
                "asset_id": snapshot.asset_id,
                "revision_number": snapshot.revision_number,
                "source_workflow_id": snapshot.source_workflow_id,
                "source_artifact_id": asset_row.id,
                "source_sha256": source_hash,
                "target_import_path": snapshot.target_import_path,
                "components": component_receipts,
                "lod_policy": spec.lod_policy,
            }
            receipt_path = intent_dir / f"receipt-{execution.id}.json"
            with receipt_path.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, sort_keys=True, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            ids = [
                self._register(
                    workflow,
                    task,
                    execution,
                    "installed-asset-glb",
                    relative_targets[f"{snapshot.asset_id}.glb"],
                )
            ]
            ids.append(
                self._register(
                    workflow,
                    task,
                    execution,
                    "installed-asset-scene",
                    relative_targets[f"{snapshot.asset_id}.tscn"],
                )
            )
            ids.append(
                self._register(
                    workflow,
                    task,
                    execution,
                    "asset-installation-manifest",
                    receipt_path.relative_to(self.root).as_posix(),
                )
            )
            return TaskHandlerResult(1, "Installed the exact human-accepted GLB revision", ids)
        finally:
            # Keep the terminal owner record for audited recovery. The stable OS lock
            # is released by descriptor; no pathname is unlinked by the old owner.
            os_lock.release()

    def _recover_stale_lock(
        self, lock_file: Path, workflow_id: str, task_id: str, new_execution_id: str
    ) -> None:
        if _is_reparse(lock_file) or not lock_file.is_file() or lock_file.stat().st_size > 4096:
            raise ValidationError(
                "Installation target lock is invalid and requires operator recovery"
            )
        try:
            owner = _read_json_bounded(lock_file, 4096)
        except (OSError, ValueError) as exc:
            raise ValidationError(
                "Installation target lock is unreadable and requires operator recovery"
            ) from exc
        if (
            not isinstance(owner, dict)
            or set(owner) != {"workflow_id", "task_id", "execution_id", "lock_token"}
            or owner.get("workflow_id") != workflow_id
            or owner.get("task_id") != task_id
            or not isinstance(owner.get("lock_token"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", owner["lock_token"])
        ):
            raise ValidationError("Installation target lock belongs to another workflow")
        execution_id = owner.get("execution_id")
        previous = self.executions.get(execution_id) if isinstance(execution_id, str) else None
        previous_task = (
            TaskRepository(self.db).get(previous.task_id) if previous is not None else None
        )
        if (
            previous is None
            or previous.id == new_execution_id
            or previous.status
            not in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.UNCERTAIN}
            or previous.task_id != task_id
            or previous_task is None
            or previous_task.workflow_id != workflow_id
        ):
            raise ValidationError("Installation target lock may still be active; refusing takeover")
        audit = lock_file.parent / f"recovery-{uuid.uuid4().hex}.json"
        with audit.open("x", encoding="utf-8") as stream:
            json.dump(
                {
                    "recovered_execution_id": execution_id,
                    "recovery_execution_id": new_execution_id,
                    "workflow_id": workflow_id,
                    "task_id": task_id,
                },
                stream,
                sort_keys=True,
            )
            stream.flush()
            os.fsync(stream.fileno())
        # Caller holds the separate stable OS lock. Preserve the prior owner record
        # until the new owner record is written under that same lock.

    def _write_lock_owner(self, path: Path, owner: dict[str, str]) -> None:
        _reject_project_links(path)
        if path.exists():
            before = path.lstat()
            fd = os.open(
                path, os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise ValidationError("Installation lock owner changed while opening")
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                payload = json.dumps(owner, sort_keys=True).encode("utf-8")
                offset = 0
                while offset < len(payload):
                    offset += os.write(fd, payload[offset:])
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(owner, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())

    def _copy_component(
        self,
        source: Path | bytes,
        target: Path,
        snapshot: AssetInstallationSnapshot,
        expected_hash: str,
        expected_size: int,
        baseline_hash: str | None,
        backup_path: Path | None = None,
    ) -> dict[str, Any]:
        temp = target.with_name(
            f".{target.name}.{snapshot.fingerprint()[:12]}.{uuid.uuid4().hex}.tmp"
        )
        _reject_project_links(target.parent)
        if temp.exists() or _is_reparse(temp):
            raise ValidationError("Installation temporary path already exists")
        digest, count = hashlib.sha256(), 0
        source_fd = (
            os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            if isinstance(source, Path)
            else None
        )
        try:
            with temp.open("xb") as out:
                if isinstance(source, Path):
                    if source_fd is None:
                        raise ValidationError("Accepted component source could not be opened")
                    while chunk := os.read(source_fd, min(1024 * 1024, _MAX_GLB_BYTES + 1 - count)):
                        count += len(chunk)
                        if count > _MAX_GLB_BYTES:
                            raise ValidationError(
                                "Accepted component exceeds the install size limit"
                            )
                        digest.update(chunk)
                        out.write(chunk)
                else:
                    data: bytes = source
                    if len(data) > 1024 * 1024:
                        raise ValidationError("Generated wrapper exceeds its size limit")
                    count = len(data)
                    digest.update(data)
                    out.write(data)
                out.flush()
                os.fsync(out.fileno())
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        finally:
            if source_fd is not None:
                os.close(source_fd)
        actual = digest.hexdigest()
        if actual != expected_hash or count != expected_size:
            temp.unlink(missing_ok=True)
            raise ValidationError("Installation component differs from its approved hash and size")
        try:
            if baseline_hash is None:
                os.link(temp, target)
                temp.unlink()
            else:
                if backup_path is None:
                    raise ValidationError(
                        "Approved revision replacement lacks its durable baseline journal"
                    )
                _reject_project_links(backup_path.parent)
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                if not backup_path.exists() and target.is_file():
                    _create_baseline_backup(target, backup_path, baseline_hash)
                if not backup_path.is_file() or _sha256(backup_path)[0] != baseline_hash:
                    raise ValidationError(
                        "Target drifted from its approved baseline; captured data is preserved for recovery"
                    )
                displaced: Path | None = None
                if target.exists():
                    displaced = target.with_name(
                        f".{target.name}.gamefactory-recovery-{uuid.uuid4().hex}"
                    )
                    os.rename(target, displaced)
                    if _sha256(displaced)[0] != baseline_hash:
                        raise ValidationError(
                            f"Target changed during replacement; preserved its bytes at {displaced}"
                        )
                else:
                    recovery_copies = []
                    for path in target.parent.glob(f".{target.name}.gamefactory-recovery-*"):
                        recovery_copies.append(path)
                        if len(recovery_copies) > 32:
                            raise ValidationError(
                                "Target has too many recovery files; operator reconciliation is required"
                            )
                    if not recovery_copies or not any(
                        path.is_file() and _sha256(path)[0] == baseline_hash
                        for path in recovery_copies
                    ):
                        raise ValidationError(
                            "Approved target disappeared outside a recoverable replacement; baseline backup retained"
                        )
                try:
                    os.link(temp, target)
                except FileExistsError as exc:
                    raise ValidationError(
                        "A concurrent target appeared; preserved it and the approved baseline backup"
                    ) from exc
                temp.unlink()
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        final_hash, final_size = _sha256(target)
        if final_hash != actual or final_size != count:
            raise ArtifactError("Installed component failed post-write hash verification")
        return {
            "sha256": final_hash,
            "size": final_size,
            "replaced_baseline_sha256": baseline_hash,
            "baseline_backup": backup_path.relative_to(self.root).as_posix()
            if baseline_hash and backup_path
            else None,
            "preserved_recovery_copy": displaced.relative_to(self.root).as_posix()
            if baseline_hash and displaced
            else None,
        }

    def _register(
        self,
        workflow: Workflow,
        task: Task,
        execution: Execution,
        artifact_type: str,
        relative_path: str,
    ) -> str:
        _reject_project_links(self.root / Path(relative_path))
        artifact = self.artifact_manager.register_file_artifact(
            workflow.id, task.id, artifact_type, "AssetInstallationHandlers", relative_path
        )
        self.artifacts.save(artifact)
        return artifact.id


def register_asset_installation_handlers(
    registry: TaskHandlerRegistry,
    root: Path | str,
    db: Database,
    artifacts: ArtifactRepository,
    evidence: EvidenceRepository,
    gates: QualityGateRepository,
    executions: ExecutionRepository,
    artifact_manager: ArtifactManager,
) -> AssetInstallationHandlers:
    handlers = AssetInstallationHandlers(
        root, db, artifacts, evidence, gates, executions, artifact_manager
    )
    registry.register(
        "asset_install",
        handlers.install,
        TaskHandlerMetadata(
            operation=HandlerOperation.REPOSITORY_WRITE,
            managed_write=True,
            recovery=HandlerRecovery.SAFE_TO_RETRY,
            mandatory_approval_type="HUMAN_GAME_WRITE",
            approval_context=handlers.approval_context,
            refresh_parameters=handlers.refresh_parameters,
        ),
    )
    registry.register(
        "asset_install_revision",
        handlers.install,
        TaskHandlerMetadata(
            operation=HandlerOperation.REPOSITORY_WRITE,
            managed_write=True,
            recovery=HandlerRecovery.SAFE_TO_RETRY,
            mandatory_approval_type="HUMAN_GAME_WRITE_REVISION",
            approval_context=handlers.approval_context,
            refresh_parameters=handlers.refresh_parameters,
        ),
    )
    return handlers


def _reject_project_links(path: Path) -> None:
    current = path.absolute()
    while current != current.parent:
        if _is_reparse(current):
            raise ValidationError(
                "Managed installation path cannot contain a symbolic link or junction"
            )
        current = current.parent
