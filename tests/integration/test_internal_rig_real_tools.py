"""Real Blender/Godot internal rig evidence + cold verify outside checkout (V0.8-2)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from gamefactory.adapters.assets.internal_rig_evidence import export_rig_evidence_bundle
from gamefactory.adapters.engines.skin_oracle_verify import OracleObservationError
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb

REPO = Path(__file__).resolve().parents[2]
TRUSTED_VERIFIER = Path(
    str(resources.files("gamefactory.resources.scripts").joinpath("verify_rig_bundle.py"))
)
BLENDER = os.environ.get(
    "GAMEFACTORY_TEST_BLENDER",
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
)
GODOT = os.environ.get(
    "GAMEFACTORY_TEST_GODOT",
    r"C:\Users\lenovo\devel\godot\Godot_v4.7.2-stable_win64.exe\Godot_v4.7.2-stable_win64_console.exe",
)


def _export_script_path() -> Path:
    return Path(
        str(
            resources.files("gamefactory.resources.blender").joinpath(
                "export_humanoid_12bone_fixture.py"
            )
        )
    )


def _run_blender_export(out: Path) -> None:
    cmd = [BLENDER, "--background", "--python", str(_export_script_path()), "--", str(out)]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert out.is_file()


@pytest.mark.skipif(not Path(BLENDER).is_file(), reason="Blender executable not available")
@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
@pytest.mark.real_godot
def test_real_tools_bundle_cold_pass_outside_checkout(tmp_path: Path) -> None:
    glb = tmp_path / "humanoid.glb"
    _run_blender_export(glb)
    bundle_dir = tmp_path / "bundle"
    export_rig_evidence_bundle(
        glb,
        bundle_dir,
        godot_executable=Path(GODOT),
        appdata_dir=tmp_path / "appdata",
    )
    outside = tmp_path / "cold-cwd"
    outside.mkdir()
    proc = subprocess.run(
        [sys.executable, "-I", str(TRUSTED_VERIFIER), str(bundle_dir.resolve())],
        capture_output=True,
        text=True,
        cwd=str(outside),
        env={
            **os.environ,
            "PYTHONNOUSERSITE": "1",
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines[0] == "CONSISTENT_BUT_UNAUTHENTICATED"
    payload = json.loads(lines[1])
    assert payload["integrity_outcome"] == "VERIFIED"
    assert payload["execution_provenance"] == "CONSISTENT_BUT_UNAUTHENTICATED"
    assert payload["validation_status"] == "PASS"
    assert payload["runtime_status"] == "PASS"


@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
@pytest.mark.real_godot
def test_real_tools_export_rejects_false_deformation_oracle_no_output(tmp_path: Path) -> None:
    glb = tmp_path / "false_deform.glb"
    glb.write_bytes(build_humanoid_skinned_glb("false_deformation_oracle"))
    out = tmp_path / "bundle"
    with pytest.raises(OracleObservationError, match="expected PASS"):
        export_rig_evidence_bundle(
            glb,
            out,
            godot_executable=Path(GODOT),
            appdata_dir=tmp_path / "appdata",
        )
    assert not out.exists()
