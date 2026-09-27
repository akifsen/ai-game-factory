"""Unit tests for ArtifactManager hashing, registration, and independent tamper/missing verification."""

from pathlib import Path

import pytest

from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ArtifactError


class TestArtifactManager:
    def test_create_and_verify_artifact(self, tmp_path: Path) -> None:
        mgr = ArtifactManager(tmp_path)
        art = mgr.create_text_artifact(
            workflow_id="WF-001",
            task_id="TASK-001",
            artifact_type="concept_spec",
            producer="ConceptArtist",
            relative_path="docs/concept.md",
            content="# Heavy Enemy Concept\nHigh armor, slow movement.",
        )
        assert art.id.startswith("ART-")
        assert len(art.content_hash) == 64
        assert art.file_size > 0

        # Verify integrity
        res = mgr.verify_artifact_integrity(art)
        assert res["status"] == "VERIFIED"
        assert res["content_hash"] == art.content_hash

    def test_missing_artifact_fails_verification(self, tmp_path: Path) -> None:
        mgr = ArtifactManager(tmp_path)
        art = mgr.create_text_artifact(
            workflow_id="WF-001",
            task_id="TASK-002",
            artifact_type="model_3d",
            producer="AssetAgent",
            relative_path="models/enemy.glb",
            content="binary_mesh_data",
        )
        # Delete file from disk
        (tmp_path / "models" / "enemy.glb").unlink()

        with pytest.raises(ArtifactError, match="file missing on disk"):
            mgr.verify_artifact_integrity(art)
        assert art.validation_state == "FAILED_MISSING"

    def test_tampered_artifact_fails_verification(self, tmp_path: Path) -> None:
        mgr = ArtifactManager(tmp_path)
        art = mgr.create_text_artifact(
            workflow_id="WF-001",
            task_id="TASK-003",
            artifact_type="config",
            producer="Engineer",
            relative_path="config/settings.json",
            content='{"resolution": "1080p"}',
        )
        # Tamper with file on disk
        (tmp_path / "config" / "settings.json").write_text(
            '{"resolution": "4K_TAMPERED"}', encoding="utf-8"
        )

        with pytest.raises(ArtifactError, match="content hash mismatch"):
            mgr.verify_artifact_integrity(art)
        assert art.validation_state == "FAILED_TAMPERED"
