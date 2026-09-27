"""Artifact management, SHA-256 hashing, and independent verification.

Ensures evidence over claims: agents cannot merely claim an artifact is produced.
Artifacts are hashed via SHA-256, recorded, and verified independently before
quality gates allow task completion.
"""

import hashlib
from pathlib import Path
from typing import Any

from gamefactory.core.domain.errors import ArtifactError
from gamefactory.core.domain.models import Artifact, generate_id, utc_now_iso
from gamefactory.core.execution.path_guard import PathGuard


def compute_sha256(file_path: Path | str, chunk_size: int = 65536) -> str:
    """Stream and compute SHA-256 hash of a file."""
    path = Path(file_path)
    if not path.is_file():
        raise ArtifactError(
            f"Cannot compute hash: file does not exist or is not a regular file: {path}"
        )

    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


class ArtifactManager:
    """Manages artifact creation, metadata, and independent verification."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.path_guard = PathGuard(self.base_dir)

    def register_file_artifact(
        self,
        workflow_id: str,
        task_id: str,
        artifact_type: str,
        producer: str,
        relative_path: str,
    ) -> Artifact:
        """Register an existing file inside the base directory as an artifact."""
        safe_path = self.path_guard.resolve_safe_path(relative_path)
        if not safe_path.exists() or not safe_path.is_file():
            raise ArtifactError(
                f"Cannot register artifact: file does not exist at '{relative_path}'",
                details={"relative_path": relative_path, "resolved": str(safe_path)},
            )

        content_hash = compute_sha256(safe_path)
        file_size = safe_path.stat().st_size

        return Artifact(
            id=generate_id("ART"),
            workflow_id=workflow_id,
            task_id=task_id,
            artifact_type=artifact_type,
            producer=producer,
            relative_path=relative_path,
            content_hash=content_hash,
            file_size=file_size,
            validation_state="VALID",
            created_at=utc_now_iso(),
        )

    def create_text_artifact(
        self,
        workflow_id: str,
        task_id: str,
        artifact_type: str,
        producer: str,
        relative_path: str,
        content: str,
    ) -> Artifact:
        """Create a text file within the managed directory and register it as an artifact."""
        safe_path = self.path_guard.ensure_safe_parent(relative_path)
        try:
            with safe_path.open("x", encoding="utf-8") as output:
                output.write(content)
        except FileExistsError as exc:
            raise ArtifactError(
                f"Refusing to overwrite existing artifact: '{relative_path}'"
            ) from exc
        return self.register_file_artifact(
            workflow_id=workflow_id,
            task_id=task_id,
            artifact_type=artifact_type,
            producer=producer,
            relative_path=relative_path,
        )

    def verify_artifact_integrity(self, artifact: Artifact) -> dict[str, Any]:
        """Independently verify that the physical artifact file exists and has not been tampered with.

        Raises ArtifactError if missing or tampered.
        """
        try:
            safe_path = self.path_guard.resolve_safe_path(artifact.relative_path)
        except Exception as exc:
            artifact.validation_state = "FAILED_INVALID_PATH"
            raise ArtifactError(
                f"Artifact path invalid for '{artifact.id}': {exc}",
                details={"artifact_id": artifact.id, "error": str(exc)},
            ) from exc

        if not safe_path.exists() or not safe_path.is_file():
            artifact.validation_state = "FAILED_MISSING"
            raise ArtifactError(
                f"Artifact integrity failure: file missing on disk for '{artifact.id}' at '{artifact.relative_path}'",
                details={"artifact_id": artifact.id, "path": artifact.relative_path},
            )

        current_hash = compute_sha256(safe_path)
        if current_hash != artifact.content_hash:
            artifact.validation_state = "FAILED_TAMPERED"
            raise ArtifactError(
                f"Artifact integrity failure: content hash mismatch for '{artifact.id}'. Expected {artifact.content_hash[:8]}..., found {current_hash[:8]}...",
                details={
                    "artifact_id": artifact.id,
                    "expected_hash": artifact.content_hash,
                    "actual_hash": current_hash,
                },
            )

        artifact.validation_state = "VERIFIED"
        return {
            "artifact_id": artifact.id,
            "status": "VERIFIED",
            "content_hash": current_hash,
            "file_size": safe_path.stat().st_size,
        }
