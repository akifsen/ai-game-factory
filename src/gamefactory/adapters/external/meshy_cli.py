"""Meshy CLI 0.4.0 tool runner and provider adapter.

Integrates with the official meshy-cli 0.4.0 on Node.js >= 22.12 via subprocess.
Enforces:
- Deterministic runner resolution with safe pinned Node invocation on Windows
- Doctor diagnostics distinguishing missing credentials from approval requirements
- Mandatory durable intent recording before external submission
- Known SUBMITTED/SUCCEEDED task IDs query existing work, never create
- Failed/uncertain submissions never re-create without a new revision
- Nonzero/malformed/timeout on create is classified as UNCERTAIN
- Immediate persistence of external task ID before query/download
- 2k texture cap and smart-topology target within budget
- Bounded download with SHA-256 verification and path containment
- Redaction of credentials, signed URLs, and raw outputs
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ProviderOperationIntent,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.approvals.operation_scope import build_operation_inputs
from gamefactory.core.domain.asset_contracts import parse_asset_specification, spec_fingerprint
from gamefactory.core.domain.errors import (
    ApprovalRequired,
    ProviderFailedError,
    ProviderUnavailable,
    ProviderUncertainError,
    RawArtifactInvalidError,
    TimeoutError,
)
from gamefactory.core.domain.models import (
    ApprovalStatus,
    CostClass,
    generate_id,
)
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner
from gamefactory.workflows.ports import (
    AssetGenerationProvider,
    GenerationRequest,
    GenerationResponse,
)

_VALID_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_TASK_ID_REGEX = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")
_RECOGNIZED_TASK_STATES = frozenset(
    {"PENDING", "IN_PROGRESS", "SUCCEEDED", "FAILED", "EXPIRED", "CANCELED", "CANCELLED"}
)


def _validate_finite_nonnegative_cost(val: Any, name: str) -> float | None:
    """Validate finite non-negative cost value, rejecting bool, NaN, Infinity, negative."""
    if val is None or val == "" or val == "UNKNOWN":
        return None
    if isinstance(val, bool):
        raise ProviderFailedError(f"{name} cannot be a boolean", provider="meshy")
    try:
        num = float(val)
    except (TypeError, ValueError) as exc:
        raise ProviderFailedError(f"{name} must be numeric", provider="meshy") from exc
    if math.isnan(num) or math.isinf(num):
        raise ProviderFailedError(f"{name} must be finite", provider="meshy")
    if num < 0:
        raise ProviderFailedError(f"{name} cannot be negative", provider="meshy")
    return num


@dataclass(frozen=True)
class MeshyDoctorResult:
    available: bool
    status: str  # AVAILABLE, CREDENTIAL_MISSING, MISCONFIGURED, UNAVAILABLE
    cli_version: str | None = None
    node_version: str | None = None
    has_credential: bool = False
    credential_source: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class MeshyCliRunner:
    """Subprocess runner for meshy-cli 0.4.0 commands."""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or ProcessRunner(sanitize_output=True)
        self._cached_runner_cmd: list[str] | None = None

    @staticmethod
    def _host_api_key_env() -> dict[str, str]:
        """Pass only the host Meshy key into the otherwise minimal child environment."""
        key = os.environ.get("MESHY_API_KEY")
        return {"MESHY_API_KEY": key} if key else {}

    def resolve_runner_cmd(self) -> list[str]:
        """Resolve command prefix to execute meshy-cli safely.

        Avoids Windows npm.cmd shell expansion vulnerabilities by preferring direct
        binary execution or invoking node directly with npm-cli.js.
        """
        if self._cached_runner_cmd is not None:
            return list(self._cached_runner_cmd)

        # 1. Check if direct 'meshy' executable exists and reports 0.4.0
        meshy_bin = shutil.which("meshy")
        if meshy_bin:
            try:
                res = self.runner.run(
                    CommandRequest(
                        args=[meshy_bin, "--version"],
                        cwd=Path.cwd(),
                        timeout_seconds=5.0,
                    )
                )
                if res.exit_code == 0 and "0.4.0" in res.stdout:
                    self._cached_runner_cmd = [meshy_bin]
                    return list(self._cached_runner_cmd)
            except Exception:
                pass

        # 2. Check Node.js readiness
        node_bin = shutil.which("node")
        if not node_bin:
            raise ProviderUnavailable("Node.js was not found on PATH; required for meshy-cli")

        # On Windows, invoke node directly with npm-cli.js if present to bypass npm.cmd shell hazards
        if sys.platform == "win32":
            node_dir = Path(node_bin).parent
            npm_cli_js = node_dir / "node_modules" / "npm" / "bin" / "npm-cli.js"
            if npm_cli_js.is_file():
                self._cached_runner_cmd = [
                    node_bin,
                    str(npm_cli_js),
                    "exec",
                    "--yes",
                    "--offline",
                    "--package=meshy-cli@0.4.0",
                    "--",
                    "meshy",
                ]
                return list(self._cached_runner_cmd)

        npm_name = "npm.cmd" if sys.platform == "win32" else "npm"
        npm_bin = shutil.which(npm_name)
        if not npm_bin:
            raise ProviderUnavailable(
                f"{npm_name} was not found on PATH; required for meshy-cli runner"
            )

        self._cached_runner_cmd = [
            npm_bin,
            "exec",
            "--yes",
            "--offline",
            "--package=meshy-cli@0.4.0",
            "--",
            "meshy",
        ]
        return list(self._cached_runner_cmd)

    def doctor(self) -> MeshyDoctorResult:
        """Run diagnostic check via meshy doctor. Distinguishes missing credentials from misconfiguration."""
        try:
            cmd = self.resolve_runner_cmd() + [
                "doctor",
                "--output-schema",
                "v1",
                "--format",
                "json",
                "--no-update-check",
            ]
            res = self.runner.run(
                CommandRequest(
                    args=cmd,
                    cwd=Path.cwd(),
                    timeout_seconds=15.0,
                    env_overrides=self._host_api_key_env(),
                    structured_json_output=True,
                )
            )
            if res.exit_code != 0:
                return MeshyDoctorResult(
                    available=False,
                    status="UNAVAILABLE",
                    details={
                        "error": "CLI doctor returned non-zero exit code",
                        "exit_code": res.exit_code,
                    },
                )

            data = json.loads(res.stdout)
            result = data.get("result", {})
            cli_info = result.get("cli", {})
            sources = result.get("credential_sources", {})

            cli_version = cli_info.get("version")
            node_version = cli_info.get("node")
            local_ready = result.get("local_ready", True)

            # Validate exact version
            if cli_version != "0.4.0":
                return MeshyDoctorResult(
                    available=False,
                    status="MISCONFIGURED",
                    cli_version=cli_version,
                    node_version=node_version,
                    details={"reason": f"Expected meshy-cli version 0.4.0, got {cli_version}"},
                )

            if not local_ready:
                return MeshyDoctorResult(
                    available=False,
                    status="MISCONFIGURED",
                    cli_version=cli_version,
                    node_version=node_version,
                    details={"reason": "Node environment reported not ready"},
                )

            has_cred = bool(
                sources.get("flag")
                or sources.get("env")
                or sources.get("api_key_file")
                or (sources.get("stored_profile", {}).get("exists"))
            )

            source_desc = None
            if sources.get("env"):
                source_desc = "env:MESHY_API_KEY"
            elif sources.get("api_key_file"):
                source_desc = "api_key_file"
            elif sources.get("stored_profile", {}).get("exists"):
                source_desc = "stored_profile"
            elif sources.get("flag"):
                source_desc = "cli_flag"

            if not has_cred:
                # Distinguish missing credentials from APPROVAL_REQUIRED
                return MeshyDoctorResult(
                    available=True,
                    status="CREDENTIAL_MISSING",
                    cli_version=cli_version,
                    node_version=node_version,
                    has_credential=False,
                    credential_source=None,
                    details={"platform": cli_info.get("platform"), "local_ready": True},
                )

            return MeshyDoctorResult(
                available=True,
                status="AVAILABLE",
                cli_version=cli_version,
                node_version=node_version,
                has_credential=True,
                credential_source=source_desc,
                details={"platform": cli_info.get("platform"), "local_ready": True},
            )
        except ProviderUnavailable as pu:
            return MeshyDoctorResult(
                available=False,
                status="UNAVAILABLE",
                details={"reason": str(pu)},
            )
        except Exception as exc:
            return MeshyDoctorResult(
                available=False,
                status="UNAVAILABLE",
                details={"error": f"Doctor probe failed: {type(exc).__name__}"},
            )

    def get_task(self, task_id: str, timeout_seconds: float = 15.0) -> dict[str, Any]:
        """Query task state via `meshy-cli image-to-3d get <task_id>` without leaking raw responses or credentials.

        Normalizes actual v1 response shape: `result.task.task_id`, `result.task.status`,
        `result.task.progress`, `result.task.consumed_credits`.
        """
        if not _SAFE_TASK_ID_REGEX.match(task_id):
            raise ProviderFailedError(f"Invalid task_id format: '{task_id}'", provider="meshy")

        cmd = self.resolve_runner_cmd() + [
            "image-to-3d",
            "get",
            task_id,
            "--output-schema",
            "v1",
            "--format",
            "json",
            "--no-update-check",
        ]
        try:
            res = self.runner.run(
                CommandRequest(
                    args=cmd,
                    cwd=Path.cwd(),
                    timeout_seconds=timeout_seconds,
                    env_overrides=self._host_api_key_env(),
                    structured_json_output=True,
                )
            )
        except Exception as exc:
            raise ProviderFailedError(
                f"Failed to execute Meshy task query: {type(exc).__name__}",
                provider="meshy",
                details={"task_id": task_id},
            ) from exc

        if res.exit_code != 0:
            raise ProviderFailedError(
                f"Failed to query Meshy task (exit code {res.exit_code})",
                provider="meshy",
                details={"task_id": task_id, "exit_code": res.exit_code},
            )

        try:
            data = json.loads(res.stdout)
            result = data.get("result", {})
            task_info = result.get("task", {})
        except Exception as exc:
            raise ProviderFailedError(
                f"Malformed JSON returned from Meshy task query: {type(exc).__name__}",
                provider="meshy",
                details={"task_id": task_id},
            ) from exc

        returned_id = task_info.get("task_id") or task_info.get("id")
        if not returned_id or returned_id != task_id:
            raise ProviderFailedError(
                f"Mismatched task_id returned from Meshy query (expected '{task_id}', got '{returned_id}')",
                provider="meshy",
                details={"task_id": task_id},
            )

        raw_status = str(task_info.get("status", "")).strip().upper()
        if raw_status not in _RECOGNIZED_TASK_STATES:
            raise ProviderFailedError(
                f"Unrecognized task status '{raw_status}' returned by Meshy",
                provider="meshy",
                details={"task_id": task_id},
            )

        raw_progress = task_info.get("progress", 0)
        try:
            progress = int(raw_progress)
        except (TypeError, ValueError):
            progress = 0

        # In v1 shape, credits consumed is in `consumed_credits` (legacy fallback `cost`)
        raw_credits = task_info.get("consumed_credits")
        if raw_credits is None:
            raw_credits = task_info.get("cost")
        actual_cost = _validate_finite_nonnegative_cost(raw_credits, "consumed_credits")

        return {
            "task_id": task_id,
            "status": raw_status,
            "progress": progress,
            "actual_cost": actual_cost,
        }

    def download_glb(
        self,
        task_id: str,
        destination_dir: Path,
        max_bytes: int = 50_000_000,
        timeout_seconds: float = 60.0,
    ) -> tuple[Path, str]:
        """Download GLB model into managed destination beneath PathGuard containment.

        Requires --resource image-to-3d with --task-id. Uses global --output and
        --workspace managed directory without --overwrite. Rejects preexisting files,
        symlinks, path escapes, malformed envelopes, and wrong hashes or formats.

        Note: meshy-cli 0.4.0 has a built-in transport transfer cap of 2 GiB and bounded
        timeout. The max_bytes (default 50 MB) parameter enforces the Factory's accepted
        artifact limit post-download, as meshy-cli provides no CLI flag to enforce a 50 MB
        transfer-time cap.
        """
        if not _SAFE_TASK_ID_REGEX.match(task_id):
            raise ProviderFailedError(f"Invalid task_id format: '{task_id}'", provider="meshy")

        workspace_dir = destination_dir.resolve()
        workspace_dir.mkdir(parents=True, exist_ok=True)
        guard = PathGuard(workspace_dir)

        target_file = (workspace_dir / "raw.glb").resolve()
        guard.ensure_safe_parent(target_file)

        # Reject preexisting stale file
        if target_file.exists() or target_file.is_symlink():
            raise RawArtifactInvalidError(
                f"Target GLB file already exists prior to download; overwrite forbidden: {target_file}",
                details={"task_id": task_id},
            )

        cmd = self.resolve_runner_cmd() + [
            "--output",
            str(target_file),
            "--workspace",
            str(workspace_dir),
            "download",
            "--resource",
            "image-to-3d",
            "--task-id",
            task_id,
            "--model-format",
            "glb",
            "--output-schema",
            "v1",
            "--format",
            "json",
            "--no-update-check",
        ]

        try:
            res = self.runner.run(
                CommandRequest(
                    args=cmd,
                    cwd=workspace_dir,
                    timeout_seconds=timeout_seconds,
                    env_overrides=self._host_api_key_env(),
                    structured_json_output=True,
                )
            )
        except Exception as exc:
            raise ProviderFailedError(
                f"Meshy download command failed: {type(exc).__name__}",
                provider="meshy",
                details={"task_id": task_id},
            ) from exc

        if res.exit_code != 0:
            raise ProviderFailedError(
                f"Meshy download failed for task {task_id} with exit code {res.exit_code}",
                provider="meshy",
                details={"task_id": task_id, "exit_code": res.exit_code},
            )

        try:
            data = json.loads(res.stdout)
            downloads = data.get("result", {}).get("downloads")
        except Exception as exc:
            raise RawArtifactInvalidError(
                f"Malformed JSON envelope returned from Meshy download: {type(exc).__name__}",
                details={"task_id": task_id},
            ) from exc

        if not isinstance(downloads, dict):
            raise RawArtifactInvalidError(
                "Malformed download response: missing result.downloads",
                details={"task_id": task_id},
            )

        envelope_state = str(downloads.get("state", "")).strip().lower()
        if envelope_state != "completed":
            raise RawArtifactInvalidError(
                f"Download envelope state is '{downloads.get('state')}'; expected 'completed'",
                details={"task_id": task_id, "state": downloads.get("state")},
            )

        files = downloads.get("files")
        if not isinstance(files, list) or not files:
            raise RawArtifactInvalidError(
                "Download response contains no files",
                details={"task_id": task_id},
            )

        # Locate GLB entries in manifest
        glb_files = [
            f
            for f in files
            if isinstance(f, dict)
            and (
                f.get("format") == "glb"
                or str(f.get("key", "")).endswith(".glb")
                or str(f.get("path", "")).endswith(".glb")
            )
        ]

        if not glb_files:
            raise RawArtifactInvalidError(
                "No GLB format file found in download manifest",
                details={"task_id": task_id},
            )

        # Reject any matching GLB file with partial/failed/skipped status
        for gf in glb_files:
            f_status = str(gf.get("status", "")).strip().lower()
            if f_status in {"partial", "failed", "skipped"}:
                raise RawArtifactInvalidError(
                    f"Download manifest contains GLB file with unsuccessful status '{gf.get('status')}'",
                    details={"task_id": task_id, "file_status": gf.get("status")},
                )

        # Exactly one GLB status written
        written_glb_files = [
            gf for gf in glb_files if str(gf.get("status", "")).strip().lower() == "written"
        ]
        if len(written_glb_files) != 1:
            raise RawArtifactInvalidError(
                f"Expected exactly one GLB file with status 'written', found {len(written_glb_files)}",
                details={"task_id": task_id, "written_count": len(written_glb_files)},
            )

        manifest_entry = written_glb_files[0]

        if manifest_entry.get("format") != "glb":
            raise RawArtifactInvalidError(
                f"Invalid manifest format '{manifest_entry.get('format')}'; expected glb",
                details={"task_id": task_id},
            )

        raw_path = manifest_entry.get("path")
        if not raw_path:
            raise RawArtifactInvalidError(
                "Manifest file entry missing path", details={"task_id": task_id}
            )

        returned_path = Path(raw_path)
        if not returned_path.is_absolute():
            resolved_file = (workspace_dir / returned_path).resolve()
        else:
            resolved_file = returned_path.resolve()

        # Containment check
        try:
            resolved_file.relative_to(workspace_dir)
        except ValueError as exc:
            raise RawArtifactInvalidError(
                f"Downloaded file path escapes workspace containment: {resolved_file}",
                details={"task_id": task_id},
            ) from exc

        # Reject symlink
        if resolved_file.is_symlink() or Path(raw_path).is_symlink():
            raise RawArtifactInvalidError(
                f"Downloaded file is a symlink: {resolved_file}",
                details={"task_id": task_id},
            )

        # Verify against expected explicit output path
        if resolved_file != target_file:
            raise RawArtifactInvalidError(
                f"Downloaded file path {resolved_file} does not match expected output path {target_file}",
                details={"task_id": task_id},
            )

        if not resolved_file.is_file():
            raise RawArtifactInvalidError(
                f"Expected downloaded GLB file does not exist on disk: {resolved_file}",
                details={"task_id": task_id},
            )

        file_bytes = resolved_file.read_bytes()
        size = len(file_bytes)
        if size == 0:
            raise RawArtifactInvalidError(
                "Downloaded GLB file is empty (0 bytes)", details={"task_id": task_id}
            )
        if size > max_bytes:
            raise RawArtifactInvalidError(
                f"Downloaded GLB size ({size} bytes) exceeds maximum accepted artifact limit of {max_bytes} bytes",
                details={"task_id": task_id, "size": size, "max_bytes": max_bytes},
            )

        # Strict non-negative integer bytes validation; no skipped or malformed bytes
        raw_manifest_bytes = manifest_entry.get("bytes")
        if raw_manifest_bytes is None or isinstance(raw_manifest_bytes, bool):
            raise RawArtifactInvalidError(
                "Manifest file entry missing valid non-negative integer bytes",
                details={"task_id": task_id},
            )
        if isinstance(raw_manifest_bytes, int):
            expected_bytes = raw_manifest_bytes
        elif isinstance(raw_manifest_bytes, str) and raw_manifest_bytes.isdigit():
            expected_bytes = int(raw_manifest_bytes)
        else:
            raise RawArtifactInvalidError(
                f"Manifest file entry contains invalid/malformed bytes value: {raw_manifest_bytes!r}",
                details={"task_id": task_id},
            )
        if expected_bytes < 0:
            raise RawArtifactInvalidError(
                f"Manifest bytes cannot be negative: {expected_bytes}",
                details={"task_id": task_id},
            )
        if size != expected_bytes:
            raise RawArtifactInvalidError(
                f"Downloaded file size ({size}) does not match manifest bytes ({expected_bytes})",
                details={"task_id": task_id},
            )

        actual_hash = hashlib.sha256(file_bytes).hexdigest()
        manifest_hash = str(manifest_entry.get("sha256", "")).strip()
        if not manifest_hash or not _VALID_HEX_64.match(manifest_hash):
            raise RawArtifactInvalidError(
                "Downloaded file manifest missing valid sha256", details={"task_id": task_id}
            )

        if actual_hash.lower() != manifest_hash.lower():
            raise RawArtifactInvalidError(
                f"Downloaded GLB SHA-256 ({actual_hash}) does not match manifest hash ({manifest_hash})",
                details={"task_id": task_id},
            )

        return resolved_file, actual_hash


class MeshyAssetGenerationProvider(AssetGenerationProvider):
    """Production Meshy adapter wrapping CLI 0.4.0 with durable intent, recovery, and paid safety."""

    def __init__(
        self,
        cli_runner: MeshyCliRunner | None = None,
        intent_repo: ProviderOperationIntentRepository | None = None,
        allow_paid_calls: bool = False,
    ) -> None:
        self.cli_runner = cli_runner or MeshyCliRunner()
        if intent_repo is None:
            raise ProviderFailedError(
                "ProviderOperationIntentRepository is mandatory for MeshyAssetGenerationProvider; cannot be None"
            )
        self.intent_repo = intent_repo
        self.allow_paid_calls = allow_paid_calls
        self.invocation_count = 0

    @property
    def name(self) -> str:
        return "meshy"

    @property
    def cost_class(self) -> CostClass:
        return CostClass.PAID

    def is_configured(self) -> bool:
        """Check if meshy credential is configured without exposing it."""
        doc = self.cli_runner.doctor()
        return doc.has_credential

    def _handle_existing_intent(
        self,
        intent: ProviderOperationIntent,
        asset_id: str,
        revision_number: int,
        concept_hash: str,
        approval_id: str,
        request_fingerprint: str,
        task_id: str,
    ) -> GenerationResponse:
        """Handle existing intent safely, verifying identity and state invariants."""
        # Invariant: existing intent identity must match request parameters
        if (
            intent.task_id != task_id
            or intent.asset_id != asset_id
            or intent.revision_number != revision_number
            or intent.concept_hash != concept_hash
            or intent.approval_id != approval_id
            or intent.request_fingerprint != request_fingerprint
        ):
            raise ProviderFailedError(
                f"Existing intent {intent.id} does not match request parameters "
                f"(task={intent.task_id} vs {task_id}, "
                f"revision={intent.revision_number} vs {revision_number}, "
                f"concept_hash={intent.concept_hash[:8]} vs {concept_hash[:8]}, "
                f"approval={intent.approval_id} vs {approval_id}, "
                f"fingerprint={intent.request_fingerprint[:8]} vs {request_fingerprint[:8]}). "
                "Refusing to attach to unrelated task.",
                provider="meshy",
            )

        if intent.external_task_id:
            ext_id = intent.external_task_id
            if intent.status == "SUCCEEDED":
                return GenerationResponse(
                    external_op_id=ext_id,
                    status="SUCCESS",
                    cost=0.0,
                    cost_unit="credits",
                    details={"resumed": True, "task_id": ext_id, "actual_cost": intent.actual_cost},
                )
            if intent.status == "FAILED":
                raise ProviderFailedError(
                    f"Prior Meshy task {ext_id} for '{asset_id}' revision {revision_number} failed remotely. "
                    "Re-submission on the same revision is forbidden; allocate a new revision.",
                    provider="meshy",
                )

            # Query provider for latest status of existing task; distinguish temporary failure
            try:
                task_data = self.cli_runner.get_task(ext_id)
            except Exception as exc:
                raise ProviderFailedError(
                    f"Temporary query failure for Meshy task {ext_id}: {type(exc).__name__}",
                    provider="meshy",
                    details={"task_id": ext_id},
                ) from exc

            remote_status = task_data.get("status")
            actual_cost = task_data.get("actual_cost")
            if actual_cost is not None:
                intent.actual_cost = actual_cost

            if remote_status == "SUCCEEDED":
                intent.status = "SUCCEEDED"
                self.intent_repo.save(intent)
                return GenerationResponse(
                    external_op_id=ext_id,
                    status="SUCCESS",
                    cost=0.0,
                    cost_unit="credits",
                    details={"resumed": True, "task_id": ext_id, "actual_cost": intent.actual_cost},
                )
            elif remote_status in {"FAILED", "EXPIRED", "CANCELED", "CANCELLED"}:
                intent.status = "FAILED"
                self.intent_repo.save(intent)
                raise ProviderFailedError(
                    f"Existing Meshy task {ext_id} terminated with remote status '{remote_status}'. "
                    "Re-submission on the same revision is forbidden; allocate a new revision.",
                    provider="meshy",
                    details={"task_id": ext_id, "remote_status": remote_status},
                )
            elif remote_status in {"PENDING", "IN_PROGRESS"}:
                return GenerationResponse(
                    external_op_id=ext_id,
                    status="SUBMITTED",
                    cost=0.0,
                    cost_unit="credits",
                    details={
                        "task_id": ext_id,
                        "remote_status": remote_status,
                        "actual_cost": None,
                    },
                )
            else:
                raise ProviderFailedError(
                    f"Unrecognized remote status '{remote_status}' for Meshy task {ext_id}",
                    provider="meshy",
                    details={"task_id": ext_id, "remote_status": remote_status},
                )

        if intent.status == "FAILED":
            raise ProviderFailedError(
                f"Prior Meshy submission for '{asset_id}' revision {revision_number} failed. "
                "Re-submission on the same revision is forbidden; allocate a new revision.",
                provider="meshy",
            )

        # Any nonterminal state without external_task_id (INTENDED, SUBMITTING, UNCERTAIN)
        raise ProviderUncertainError(
            f"Meshy generation for '{asset_id}' has nonterminal status '{intent.status}' "
            "without confirmed external ID. Automatic second paid generation is forbidden; reconciliation required.",
            task_id=task_id,
            execution_id=intent.id,
            provider="meshy",
            details={"intent_id": intent.id, "status": intent.status},
        )

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        """Execute Meshy generation with pre-submission durable intent, paid guards, and recovery.

        Workflow Integration Contract:
        - Required request.parameters:
          - asset_id (str): identifier of the asset
          - revision_number (int >= 1): monotonic revision number
          - workflow_id (str): owning workflow identifier
          - task_id (str): owning task identifier
          - approval_id (str): human approval request identifier
          - concept_hash (str, 64-hex SHA-256): verified concept digest
          - concept_image_path (str): path to concept PNG on disk
          - request_fingerprint (str, 64-hex SHA-256, or request.operation_hash): deterministic operation hash
          - cost (float | None | "UNKNOWN"): finite non-negative cost or None/UNKNOWN
          - target_polycount / max_triangles_lod0 (int >= 100): geometry budget (clamped to 15000)
          - mandatory_approval_type (str, default 'paid_generation')
        - API Behavior:
          - When a task is already completed (SUCCEEDED), generate() returns status="SUCCESS"
            with external_op_id and output_path=None. The caller (e.g. AssetProductionWorkflow)
            is expected to call cli_runner.download_glb(response.external_op_id, destination_dir)
            to retrieve and verify the raw GLB artifact.
          - Does NOT call BuiltinTaskActions.
          - Known-ID queries never create a new task.
          - Distinguishes known-ID temporary query failure from unsafe resubmit.
        """
        params = request.parameters
        asset_id = str(params.get("asset_id", "")).strip()
        revision_number = int(params.get("revision_number", 0))
        workflow_id = str(params.get("workflow_id", "")).strip()
        task_id = str(params.get("task_id", "")).strip()
        approval_id = str(params.get("approval_id", "")).strip()
        concept_hash = str(params.get("concept_hash", "")).strip()
        request_fingerprint = str(
            request.operation_hash or params.get("request_fingerprint", "")
        ).strip()

        # Reject placeholder or missing identity values
        for label, val in [
            ("asset_id", asset_id),
            ("workflow_id", workflow_id),
            ("task_id", task_id),
            ("approval_id", approval_id),
            ("concept_hash", concept_hash),
            ("request_fingerprint", request_fingerprint),
        ]:
            if not val or "unknown_" in val.lower():
                raise ProviderFailedError(
                    f"Required parameter '{label}' is missing or contains placeholder value: '{val}'",
                    provider="meshy",
                )

        if revision_number < 1:
            raise ProviderFailedError("revision_number must be >= 1", provider="meshy")

        # Require 64-character SHA-256 hexadecimal hashes; no invented FP fallback
        if not _VALID_HEX_64.match(concept_hash):
            raise ProviderFailedError(
                f"concept_hash must be a 64-character hex SHA-256 hash, got '{concept_hash}'",
                provider="meshy",
            )
        if not _VALID_HEX_64.match(request_fingerprint):
            raise ProviderFailedError(
                f"request_fingerprint must be a 64-character hex SHA-256 hash, got '{request_fingerprint}'",
                provider="meshy",
            )

        # Finite non-negative cost only; UNKNOWN stays None
        raw_cost = params.get("provider_estimate", params.get("cost"))
        estimated_cost: float | None = _validate_finite_nonnegative_cost(raw_cost, "cost")

        # Deny unsafe target polycount before invocation count / intent
        request_spec = params.get("specification")
        nested_spec_poly: int | None = None
        if request_spec is not None:
            if not isinstance(request_spec, dict):
                raise ProviderFailedError("Task specification must be an object", provider="meshy")
            try:
                parsed_spec = parse_asset_specification(request_spec)
            except Exception as exc:
                raise ProviderFailedError(
                    "Task specification is invalid", provider="meshy"
                ) from exc
            nested_spec_poly = parsed_spec.geometry_budget.max_triangles_lod0
            if (
                parsed_spec.asset_id != asset_id
                or spec_fingerprint(parsed_spec)
                != str(params.get("specification_hash", "")).lower()
            ):
                raise ProviderFailedError(
                    "Task specification fingerprint or asset binding is invalid", provider="meshy"
                )
            supplied_poly = params.get("target_polycount", params.get("max_triangles_lod0"))
            if supplied_poly is not None and (
                isinstance(supplied_poly, bool) or str(supplied_poly) != str(nested_spec_poly)
            ):
                raise ProviderFailedError(
                    "Request target_polycount does not match nested specification", provider="meshy"
                )
        raw_poly = (
            nested_spec_poly
            if nested_spec_poly is not None
            else params.get("target_polycount", params.get("max_triangles_lod0", 10000))
        )
        if isinstance(raw_poly, bool):
            raise ProviderFailedError("target_polycount cannot be a boolean", provider="meshy")
        try:
            target_triangles = int(raw_poly)
        except (TypeError, ValueError) as exc:
            raise ProviderFailedError(
                "target_polycount must be an integer", provider="meshy"
            ) from exc
        if target_triangles < 100 or target_triangles <= 0:
            raise ProviderFailedError(
                f"Unsafe target polycount {target_triangles}; minimum is 100 for smart-topology",
                provider="meshy",
            )
        smart_polycount = min(target_triangles, 15000)

        # Check for existing durable intent before performing any actions
        existing_intent: ProviderOperationIntent | None = self.intent_repo.get_by_task(task_id)
        if existing_intent is None and request_fingerprint:
            existing_intent = self.intent_repo.get_by_fingerprint(request_fingerprint)
        if existing_intent is None:
            existing_intent = self.intent_repo.get_by_asset_revision(
                asset_id, revision_number, provider="meshy", operation="image-to-3d"
            )

        if existing_intent is not None:
            return self._handle_existing_intent(
                existing_intent,
                asset_id,
                revision_number,
                concept_hash,
                approval_id,
                request_fingerprint,
                task_id,
            )

        # Paid call guard: allow_paid_calls boolean alone must not impersonate human approval
        if not self.allow_paid_calls:
            raise ApprovalRequired(
                f"Real paid Meshy call for '{asset_id}' is BLOCKED per Phase A requirements. "
                "Human explicit approval and separate resume required.",
                approval_id=approval_id,
                details={
                    "provider": "meshy",
                    "operation": "image-to-3d",
                    "asset_id": asset_id,
                    "estimated_cost": params.get("cost"),
                },
            )

        # Authoritative repository loads and bindings
        db = self.intent_repo.db
        task_repo = TaskRepository(db)
        workflow_repo = WorkflowRepository(db)
        revision_repo = AssetRevisionRepository(db)
        artifact_repo = ArtifactRepository(db)
        approval_repo = ApprovalRepository(db)

        # 1. Authoritative Task checks
        task = task_repo.get(task_id)
        if task is None:
            raise ProviderFailedError(
                f"Task '{task_id}' was not found in authoritative TaskRepository",
                provider="meshy",
            )
        if task.task_type != "asset_paid_generation":
            raise ProviderFailedError(
                f"Task '{task_id}' has task_type '{task.task_type}'; expected 'asset_paid_generation'",
                provider="meshy",
            )
        if task.cost_class != CostClass.PAID:
            raise ProviderFailedError(
                f"Task '{task_id}' has cost_class '{task.cost_class}'; expected CostClass.PAID",
                provider="meshy",
            )
        if task.workflow_id != workflow_id:
            raise ProviderFailedError(
                f"Task workflow_id '{task.workflow_id}' does not match request workflow_id '{workflow_id}'",
                provider="meshy",
            )
        if task.parameters.get("provider") != "meshy":
            raise ProviderFailedError(
                f"Task parameters provider '{task.parameters.get('provider')}' must be 'meshy'",
                provider="meshy",
            )
        if task.parameters.get("asset_id") != asset_id:
            raise ProviderFailedError(
                f"Task parameters asset_id '{task.parameters.get('asset_id')}' does not match request asset_id '{asset_id}'",
                provider="meshy",
            )
        if int(task.parameters.get("revision_number", 0)) != revision_number:
            raise ProviderFailedError(
                f"Task parameters revision_number '{task.parameters.get('revision_number')}' does not match request revision_number '{revision_number}'",
                provider="meshy",
            )

        # 2. Authoritative Workflow checks
        workflow = workflow_repo.get(workflow_id)
        if workflow is None:
            raise ProviderFailedError(
                f"Workflow '{workflow_id}' was not found in authoritative WorkflowRepository",
                provider="meshy",
            )

        # 3. Authoritative Revision checks
        revision = revision_repo.get(asset_id, revision_number)
        if revision is None:
            raise ProviderFailedError(
                f"Asset revision {revision_number} for '{asset_id}' does not exist in authoritative repository",
                provider="meshy",
            )
        if revision.workflow_id != workflow_id:
            raise ProviderFailedError(
                f"Asset revision workflow '{revision.workflow_id}' does not match request workflow '{workflow_id}'",
                provider="meshy",
            )
        if not revision.concept_hash or revision.concept_hash.lower() != concept_hash.lower():
            raise ProviderFailedError(
                f"Asset revision concept_hash '{revision.concept_hash}' does not match request concept_hash '{concept_hash}'",
                provider="meshy",
            )

        # 4. Bind request spec / specification_hash
        task_spec = task.parameters.get("specification")
        if task_spec is not None:
            if not isinstance(task_spec, dict):
                raise ProviderFailedError(
                    "Authoritative task specification must be an object", provider="meshy"
                )
            try:
                authoritative_spec = parse_asset_specification(task_spec)
            except Exception as exc:
                raise ProviderFailedError(
                    "Authoritative task specification is invalid", provider="meshy"
                ) from exc
            authoritative_spec_hash = spec_fingerprint(authoritative_spec)
            if (
                authoritative_spec.asset_id != asset_id
                or authoritative_spec_hash != revision.spec_hash.lower()
            ):
                raise ProviderFailedError(
                    "Authoritative task specification does not match revision", provider="meshy"
                )
            if (
                request_spec is not None
                and spec_fingerprint(parse_asset_specification(request_spec))
                != authoritative_spec_hash
            ):
                raise ProviderFailedError(
                    "Request specification does not match authoritative task specification",
                    provider="meshy",
                )
            nested_spec_poly = authoritative_spec.geometry_budget.max_triangles_lod0
            supplied_poly = params.get("target_polycount", params.get("max_triangles_lod0"))
            if supplied_poly is not None and (
                isinstance(supplied_poly, bool) or str(supplied_poly) != str(nested_spec_poly)
            ):
                raise ProviderFailedError(
                    "Request target_polycount does not match nested specification", provider="meshy"
                )
        elif request_spec is not None:
            raise ProviderFailedError(
                "Request specification is not present in authoritative task", provider="meshy"
            )
        task_spec_hash = task.parameters.get("specification_hash") or task.parameters.get(
            "spec_hash"
        )
        if task_spec is not None and not task_spec_hash:
            raise ProviderFailedError(
                "Nested task specification requires specification_hash", provider="meshy"
            )
        if task_spec_hash is not None and str(task_spec_hash).lower() != revision.spec_hash.lower():
            raise ProviderFailedError(
                f"Authoritative task spec_hash '{task_spec_hash}' does not match revision spec_hash '{revision.spec_hash}'",
                provider="meshy",
            )
        req_spec_hash = params.get("specification_hash") or params.get("spec_hash")
        if req_spec_hash is not None and str(req_spec_hash).lower() != revision.spec_hash.lower():
            raise ProviderFailedError(
                f"Request spec_hash '{req_spec_hash}' does not match authoritative revision spec_hash '{revision.spec_hash}'",
                provider="meshy",
            )

        # 5. Bind actual polycount to authoritative task values
        task_poly = (
            nested_spec_poly
            if task_spec is not None
            else task.parameters.get("target_polycount", task.parameters.get("max_triangles_lod0"))
        )
        if task_poly is not None:
            if isinstance(task_poly, bool):
                raise ProviderFailedError(
                    "Task target_polycount cannot be a boolean", provider="meshy"
                )
            try:
                auth_poly = int(task_poly)
            except (TypeError, ValueError) as exc:
                raise ProviderFailedError(
                    "Task target_polycount must be an integer", provider="meshy"
                ) from exc
            if auth_poly < 100 or auth_poly <= 0:
                raise ProviderFailedError(
                    f"Unsafe authoritative task polycount {auth_poly}", provider="meshy"
                )

            req_poly = params.get("target_polycount", params.get("max_triangles_lod0"))
            if req_poly is not None:
                if isinstance(req_poly, bool):
                    raise ProviderFailedError(
                        "Request target_polycount cannot be a boolean", provider="meshy"
                    )
                try:
                    r_poly = int(req_poly)
                except (TypeError, ValueError) as exc:
                    raise ProviderFailedError(
                        "Request target_polycount must be an integer", provider="meshy"
                    ) from exc
                if r_poly != auth_poly:
                    raise ProviderFailedError(
                        f"Request target_polycount ({r_poly}) does not match authoritative task polycount ({auth_poly})",
                        provider="meshy",
                    )
            smart_polycount = min(auth_poly, 15000)
        else:
            raw_poly = params.get("target_polycount", params.get("max_triangles_lod0", 10000))
            if isinstance(raw_poly, bool):
                raise ProviderFailedError("target_polycount cannot be a boolean", provider="meshy")
            try:
                target_triangles = int(raw_poly)
            except (TypeError, ValueError) as exc:
                raise ProviderFailedError(
                    "target_polycount must be an integer", provider="meshy"
                ) from exc
            if target_triangles < 100 or target_triangles <= 0:
                raise ProviderFailedError(
                    f"Unsafe target polycount {target_triangles}", provider="meshy"
                )
            smart_polycount = min(target_triangles, 15000)

        # 6. Bind cost estimate and budget reservation
        auth_raw_cost = task.parameters.get("cost")
        auth_cost = _validate_finite_nonnegative_cost(auth_raw_cost, "task cost")
        if task_spec is not None:
            auth_estimate = _validate_finite_nonnegative_cost(
                task.parameters.get("provider_estimate"), "task provider_estimate"
            )
            raw_cost = params.get("provider_estimate", params.get("cost"))
            request_estimate = _validate_finite_nonnegative_cost(raw_cost, "provider_estimate")
            if request_estimate != auth_estimate:
                raise ProviderFailedError(
                    f"Request provider_estimate ({request_estimate}) does not match authoritative task estimate ({auth_estimate})",
                    provider="meshy",
                )
            estimated_cost = auth_estimate
        else:
            raw_cost = params.get("cost")
            estimated_cost = _validate_finite_nonnegative_cost(raw_cost, "cost")
        if (
            task_spec is None
            and auth_cost is not None
            and estimated_cost is not None
            and auth_cost != estimated_cost
        ):
            raise ProviderFailedError(
                f"Request cost ({estimated_cost}) does not match authoritative task cost ({auth_cost})",
                provider="meshy",
            )
        if task_spec is None and auth_cost is not None:
            estimated_cost = auth_cost

        auth_budget = _validate_finite_nonnegative_cost(
            task.parameters.get("budget_reservation"), "task budget_reservation"
        )
        req_budget = _validate_finite_nonnegative_cost(
            params.get("budget_reservation"), "budget_reservation"
        )
        if task_spec is not None:
            if auth_budget is None or req_budget is None:
                raise ProviderFailedError(
                    "Workflow budget_reservation must be explicitly finite and non-negative",
                    provider="meshy",
                )
            if auth_budget != auth_cost:
                raise ProviderFailedError(
                    "Task cost does not match its budget_reservation", provider="meshy"
                )
            if req_budget != auth_budget:
                raise ProviderFailedError(
                    "Request budget_reservation does not match authoritative task reservation",
                    provider="meshy",
                )
        if (
            task_spec is None
            and auth_budget is not None
            and req_budget is not None
            and auth_budget != req_budget
        ):
            raise ProviderFailedError(
                f"Request budget_reservation '{req_budget}' does not match authoritative task budget_reservation '{auth_budget}'",
                provider="meshy",
            )

        # 7. Concept image file and workflow artifact verification
        concept_image_path = params.get("concept_image_path")
        if not concept_image_path:
            raise ProviderFailedError(
                "Missing concept_image_path in generation request parameters",
                provider="meshy",
            )
        concept_file = Path(concept_image_path).resolve()
        if not concept_file.is_file():
            raise ProviderFailedError(
                f"Concept image file does not exist: {concept_file}",
                provider="meshy",
            )
        real_concept_bytes = concept_file.read_bytes()
        real_concept_hash = hashlib.sha256(real_concept_bytes).hexdigest()
        if real_concept_hash.lower() != concept_hash.lower():
            raise ProviderFailedError(
                f"Concept image content hash {real_concept_hash} does not match expected concept_hash {concept_hash}",
                provider="meshy",
            )
        if not revision.concept_hash or real_concept_hash.lower() != revision.concept_hash.lower():
            raise ProviderFailedError(
                f"Concept image content hash {real_concept_hash} does not match revision concept_hash {revision.concept_hash}",
                provider="meshy",
            )

        artifacts = artifact_repo.list_by_workflow(workflow_id)
        for art in artifacts:
            if (
                art.artifact_type in ("asset-concept", "concept_spec")
                or art.relative_path.endswith(f"{asset_id}.png")
                or art.relative_path.endswith("concept.png")
            ):
                if art.content_hash.lower() != concept_hash.lower():
                    raise ProviderFailedError(
                        f"Workflow artifact '{art.id}' hash '{art.content_hash}' does not match concept_hash '{concept_hash}'",
                        provider="meshy",
                    )

        # 8. Approval verification
        approval = approval_repo.get(approval_id)
        if approval is None:
            raise ApprovalRequired(
                f"Approval request '{approval_id}' was not found in authoritative ApprovalRepository",
                approval_id=approval_id,
                details={"asset_id": asset_id, "task_id": task_id},
            )
        if approval.status != ApprovalStatus.APPROVED:
            raise ApprovalRequired(
                f"Approval request '{approval_id}' is not approved (status: {approval.status.value})",
                approval_id=approval_id,
                details={"status": approval.status.value},
            )
        if approval.task_id != task_id:
            raise ApprovalRequired(
                f"Approval '{approval_id}' task_id '{approval.task_id}' does not match request task_id '{task_id}'",
                approval_id=approval_id,
            )
        if approval.workflow_id != workflow_id:
            raise ApprovalRequired(
                f"Approval '{approval_id}' workflow_id '{approval.workflow_id}' does not match request workflow_id '{workflow_id}'",
                approval_id=approval_id,
            )
        if approval.cost_class != CostClass.PAID:
            raise ApprovalRequired(
                f"Approval '{approval_id}' cost_class '{approval.cost_class}' does not match CostClass.PAID",
                approval_id=approval_id,
            )
        # Hardcode paid_generation approval type; caller cannot change mandatory_approval_type
        if approval.approval_type != "paid_generation":
            raise ApprovalRequired(
                f"Approval '{approval_id}' approval_type '{approval.approval_type}' does not match required 'paid_generation'",
                approval_id=approval_id,
            )

        # Compute current hash via pure helper
        current_inputs = build_operation_inputs(
            workflow=workflow,
            task=task,
            artifacts=artifacts,
            cost_class=CostClass.PAID,
            provider_name=None,
            handler_context=None,
        )
        current_hash = compute_operation_hash(task.id, "paid_generation", current_inputs)

        # Require equals approvedhash + requesthash
        if approval.operation_hash != current_hash:
            raise ApprovalRequired(
                f"Approval '{approval_id}' operation_hash '{approval.operation_hash}' "
                f"does not match current authoritative operation hash '{current_hash}'. "
                "Authoritative task parameters or artifacts have changed since approval was granted.",
                approval_id=approval_id,
                details={
                    "approved_hash": approval.operation_hash,
                    "current_hash": current_hash,
                },
            )
        if request_fingerprint != current_hash:
            raise ApprovalRequired(
                f"Request fingerprint '{request_fingerprint}' does not match current authoritative operation hash '{current_hash}'",
                approval_id=approval_id,
                details={
                    "request_fingerprint": request_fingerprint,
                    "current_hash": current_hash,
                },
            )

        # Atomic claim (BEGIN IMMEDIATE) returning winner/existing intent
        intent_candidate = ProviderOperationIntent(
            id=generate_id("INTENT"),
            workflow_id=workflow_id,
            task_id=task_id,
            asset_id=asset_id,
            revision_number=revision_number,
            provider="meshy",
            operation="image-to-3d",
            concept_hash=concept_hash,
            request_fingerprint=request_fingerprint,
            approval_id=approval_id,
            estimated_cost=estimated_cost,
            actual_cost=None,
            cost_unit="credits",
            status="SUBMITTING",
        )

        intent, claimed = self.intent_repo.claim_intent(intent_candidate)
        if not claimed:
            # Loser of concurrent race: handle existing intent safely
            return self._handle_existing_intent(
                intent,
                asset_id,
                revision_number,
                concept_hash,
                approval_id,
                request_fingerprint,
                task_id,
            )

        # Winner: perform submission via CLI runner
        self.invocation_count += 1

        cmd = self.cli_runner.resolve_runner_cmd() + [
            "image-to-3d",
            "create",
            "--image-url",
            str(concept_file),
            "--model-type",
            "smart-topology",
            "--target-polycount",
            str(smart_polycount),
            "--should-texture",
            "true",
            "--enable-pbr",
            "true",
            "--texture-resolution",
            "2k",  # Strictly 2k textures per V0.4 budget, NEVER 4k
            "--target-formats",
            "glb",
            "--async",
            "--operation-id",
            intent.id,  # Local journaling only
            "--output-schema",
            "v1",
            "--format",
            "json",
            "--no-update-check",
        ]

        try:
            res = self.cli_runner.runner.run(
                CommandRequest(
                    args=cmd,
                    cwd=Path.cwd(),
                    timeout_seconds=60.0,
                    env_overrides=self.cli_runner._host_api_key_env(),
                    structured_json_output=True,
                )
            )
        except (TimeoutError, Exception) as exc:
            intent.status = "UNCERTAIN"
            self.intent_repo.save(intent)
            raise ProviderUncertainError(
                f"Meshy CLI invocation crashed or timed out: {type(exc).__name__}. Acceptance status is UNCERTAIN.",
                task_id=task_id,
                execution_id=intent.id,
                provider="meshy",
            ) from exc

        if res.exit_code != 0:
            intent.status = "UNCERTAIN"
            self.intent_repo.save(intent)
            raise ProviderUncertainError(
                f"Meshy CLI submission exited with code {res.exit_code}. Acceptance is UNCERTAIN.",
                task_id=task_id,
                execution_id=intent.id,
                provider="meshy",
            )

        try:
            data = json.loads(res.stdout)
            result_obj = data.get("result", {})
            task_id_external = result_obj.get("submission", {}).get("task_id") or result_obj.get(
                "task", {}
            ).get("task_id")
        except Exception as exc:
            intent.status = "UNCERTAIN"
            self.intent_repo.save(intent)
            raise ProviderUncertainError(
                f"Meshy returned malformed JSON output after submission: {type(exc).__name__}; acceptance is UNCERTAIN.",
                task_id=task_id,
                execution_id=intent.id,
                provider="meshy",
            ) from exc

        if not task_id_external or not _SAFE_TASK_ID_REGEX.match(str(task_id_external)):
            intent.status = "UNCERTAIN"
            self.intent_repo.save(intent)
            raise ProviderUncertainError(
                "Meshy output did not contain valid task_id; acceptance is UNCERTAIN.",
                task_id=task_id,
                execution_id=intent.id,
                provider="meshy",
            )

        # Persist accepted external task ID immediately BEFORE any subsequent query
        intent.external_task_id = str(task_id_external)
        intent.status = "SUBMITTED"
        self.intent_repo.save(intent)

        return GenerationResponse(
            external_op_id=intent.external_task_id,
            status="SUBMITTED",
            cost=0.0,
            cost_unit="credits",
            details={
                "task_id": intent.external_task_id,
                "intent_id": intent.id,
                "actual_cost": None,
            },
        )
