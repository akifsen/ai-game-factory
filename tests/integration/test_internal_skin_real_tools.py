"""Opt-in Blender/Godot paths for internal skin oracle (V0.8-1)."""

from __future__ import annotations

import json
import os
import subprocess
from importlib import resources
from pathlib import Path

import pytest

from gamefactory.adapters.assets.internal_rig_canonical import runtime_request_digest
from gamefactory.adapters.assets.internal_skin import validate_internal_skinned_glb
from gamefactory.adapters.assets.internal_skin_decode import decode_internal_skinned_glb
from gamefactory.adapters.engines.skin_deformation_oracle import run_skin_deformation_oracle
from gamefactory.adapters.engines.skin_oracle_verify import verify_oracle_payload
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract

REPO = Path(__file__).resolve().parents[2]
BLENDER = os.environ.get(
    "GAMEFACTORY_TEST_BLENDER",
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
)
GODOT = os.environ.get(
    "GAMEFACTORY_TEST_GODOT",
    r"C:\Users\lenovo\devel\godot\Godot_v4.7.2-stable_win64.exe\Godot_v4.7.2-stable_win64_console.exe",
)
CONTRACT = load_internal_skin_contract()


def _export_script_path() -> Path:
    return Path(
        str(
            resources.files("gamefactory.resources.blender").joinpath(
                "export_humanoid_12bone_fixture.py"
            )
        )
    )


def _run_blender_export(out: Path) -> None:
    cmd = [
        BLENDER,
        "--background",
        "--python",
        str(_export_script_path()),
        "--",
        str(out),
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert out.is_file()


@pytest.mark.skipif(not Path(BLENDER).is_file(), reason="Blender executable not available")
def test_blender_export_semantics(tmp_path: Path) -> None:
    out = tmp_path / "blender_humanoid.glb"
    _run_blender_export(out)
    decoded = decode_internal_skinned_glb(out)
    assert len(decoded.joint_node_indices) == 12
    assert len(decoded.primitive.positions) >= 8
    result = validate_internal_skinned_glb(out, CONTRACT)
    assert result.status.value == "PASS"
    assert not any(f.severity.value == "FAIL" for f in result.findings)


@pytest.mark.real_godot
@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
def test_godot_skin_deformation_oracle_positive(tmp_path: Path) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    payload = run_skin_deformation_oracle(
        Path(GODOT),
        glb,
        CONTRACT,
        tmp_path / "oracle_out",
        appdata_dir=appdata,
    )
    verify_oracle_payload(payload, CONTRACT, expect_pass=True)
    assert payload["process_exit_code"] == 0
    assert payload["import_exit_code"] == 0
    request_path = Path(payload["stage_dir"]) / "request.json"
    bound_request = json.loads(request_path.read_text(encoding="utf-8"))
    for transient in ("glb", "output_path", "request_digest"):
        bound_request.pop(transient, None)
    assert payload["request_digest"] == runtime_request_digest(bound_request)


@pytest.mark.real_godot
@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
def test_godot_false_deformation_negative(tmp_path: Path) -> None:
    glb = tmp_path / "false_deform.glb"
    glb.write_bytes(build_humanoid_skinned_glb("false_deformation_oracle"))
    validation = validate_internal_skinned_glb(glb, CONTRACT)
    assert validation.status.value == "PASS"
    payload = run_skin_deformation_oracle(
        Path(GODOT),
        glb,
        CONTRACT,
        tmp_path / "oracle_neg",
    )
    verify_oracle_payload(payload, CONTRACT, expect_pass=False)
    assert payload["process_exit_code"] == 1


@pytest.mark.real_godot
@pytest.mark.skipif(
    not Path(GODOT).is_file() or not Path(BLENDER).is_file(),
    reason="Blender and Godot required",
)
def test_blender_export_validate_godot_e2e(tmp_path: Path) -> None:
    out = tmp_path / "blender_humanoid.glb"
    _run_blender_export(out)
    validation = validate_internal_skinned_glb(out, CONTRACT)
    assert validation.status.value == "PASS"
    payload = run_skin_deformation_oracle(Path(GODOT), out, CONTRACT, tmp_path / "e2e")
    verify_oracle_payload(payload, CONTRACT, expect_pass=True)
