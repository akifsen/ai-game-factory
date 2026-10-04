from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gamefactory.core.domain.asset_contracts import parse_asset_specification
from gamefactory.core.domain.asset_installation import AssetInstallationSnapshot
from gamefactory.core.domain.errors import ValidationError
from gamefactory.workflows.asset_installation import _installation_lock_identity, _wrapper_scene


def _snapshot(**changes: object) -> AssetInstallationSnapshot:
    values: dict[str, object] = {
        "project_id": "project-1",
        "target_root": str(Path("C:/games/project").resolve()),
        "source_workflow_id": "WF-ASSET-1",
        "asset_id": "energy_crate",
        "revision_number": 2,
        "specification_sha256": "a" * 64,
        "target_import_path": "assets/generated/props/energy_crate/",
        "accepted_artifact_id": "ARTIFACT-1",
        "accepted_artifact_sha256": "b" * 64,
        "final_approval_id": "APPROVAL-1",
        "final_approval_sha256": "c" * 64,
        "target_baseline_sha256": None,
        "schema_version": 1,
    }
    values.update(changes)
    return AssetInstallationSnapshot(**values)  # type: ignore[arg-type]


def test_installation_snapshot_round_trip_binds_every_approved_hash() -> None:
    snapshot = _snapshot()
    restored = AssetInstallationSnapshot.from_dict(snapshot.to_dict())
    assert restored == snapshot
    assert restored.fingerprint() == snapshot.fingerprint()
    assert _snapshot(accepted_artifact_sha256="d" * 64).fingerprint() != snapshot.fingerprint()
    assert _snapshot(final_approval_sha256="e" * 64).fingerprint() != snapshot.fingerprint()
    assert _snapshot(target_import_path="assets/other").fingerprint() != snapshot.fingerprint()


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 1.0},
        {"revision_number": True},
        {"accepted_artifact_sha256": None},
        {"target_import_path": "../outside"},
        {"target_import_path": "assets/CON"},
        {"target_baseline_sha256": {"unrelated.glb": "d" * 64}},
    ],
)
def test_installation_snapshot_rejects_malformed_or_unsafe_provenance(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _snapshot(**changes).validate()


def test_wrapper_scene_points_to_accepted_import_path_and_keeps_collision_contract() -> None:
    spec_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "gamefactory"
        / "resources"
        / "specs"
        / "prop_energy_crate_01.yml"
    )
    spec = parse_asset_specification(spec_path)
    scene = _wrapper_scene(spec).decode("utf-8")
    expected = f"res://{spec.target_import_path.strip('/')}/{spec.asset_id}.glb"
    assert expected in scene
    assert "CollisionShape3D" in scene
    assert "ACCEPTED_LOD0_NAMES" in scene
    for lod0_name in sorted(
        name for name in spec.bound_profile().expected_mesh_names(spec) if name.endswith("_LOD0")
    ):
        assert lod0_name in scene
    assert "mesh_instance.visible = str(mesh_instance.name) in ACCEPTED_LOD0_NAMES" in scene
    assert (
        hashlib.sha256(scene.encode("utf-8")).hexdigest()
        == hashlib.sha256(_wrapper_scene(spec)).hexdigest()
    )


def test_installation_lock_identity_serializes_case_aliases_portably() -> None:
    upper = {"asset.glb": "Assets/Generated/Crate.glb", "asset.tscn": "Assets/Generated/Crate.tscn"}
    lower = {"asset.glb": "assets/generated/crate.glb", "asset.tscn": "assets/generated/crate.tscn"}
    assert _installation_lock_identity(upper) == _installation_lock_identity(lower)
