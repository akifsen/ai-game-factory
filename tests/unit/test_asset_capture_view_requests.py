"""Capture request view validation fails before staging or launching Godot."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gamefactory.core.domain.errors import ValidationError
from gamefactory.workflows.asset_production import run_asset_in_godot


class _Artifacts:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows

    def list_by_workflow(self, _workflow_id: str) -> list[SimpleNamespace]:
        return self.rows


class _ArtifactManager:
    def verify_artifact_integrity(self, _artifact: SimpleNamespace) -> None:
        return None


@pytest.mark.parametrize(
    "angles",
    [(), ("front_typo",), ("front", "front"), ({"nested": "front"},)],
)
def test_capture_request_rejects_unknown_or_unhashable_angles_before_staging(
    tmp_path: Path, angles: tuple[object, ...]
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    executable = root / "Godot.exe"
    executable.write_bytes(b"test placeholder")
    (root / "validation.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    artifacts = _Artifacts(
        [
            SimpleNamespace(artifact_type="asset-processed-glb", relative_path="processed.glb"),
            SimpleNamespace(
                artifact_type="asset-validation-report", relative_path="validation.json"
            ),
        ]
    )
    task = SimpleNamespace(
        id="TASK-CAMERA",
        parameters={
            "specification": Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
        },
    )
    with pytest.raises(ValidationError, match="not an implemented review view set"):
        run_asset_in_godot(
            root,
            artifacts,  # type: ignore[arg-type]
            _ArtifactManager(),  # type: ignore[arg-type]
            SimpleNamespace(id="WF-CAMERA"),  # type: ignore[arg-type]
            task,  # type: ignore[arg-type]
            SimpleNamespace(id="EXEC-CAMERA"),  # type: ignore[arg-type]
            str(executable),
            runner=None,
            angles=angles,  # type: ignore[arg-type]
        )
    assert not (root / ".gamefactory" / "scratch").exists()
