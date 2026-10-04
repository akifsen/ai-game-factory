"""Opt-in real Godot import/render check for the standalone accepted-asset wrapper."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.adapters.projects.onboarding import create_godot_project
from gamefactory.core.domain.asset_contracts import parse_asset_specification
from gamefactory.workflows.asset_installation import _wrapper_scene

SPEC_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "gamefactory"
    / "resources"
    / "specs"
    / "prop_energy_crate_01.yml"
)
GODOT_BIN = os.environ.get("GAMEFACTORY_GODOT_BIN")


@pytest.mark.real_godot
@pytest.mark.skipif(
    not GODOT_BIN, reason="Set GAMEFACTORY_GODOT_BIN to opt into local Godot integration"
)
def test_imported_wrapper_shows_only_accepted_lod0_and_keeps_physics(tmp_path: Path) -> None:
    spec = parse_asset_specification(SPEC_PATH)
    project = tmp_path / "godot-project"
    create_godot_project(project, "Accepted asset fixture", "3d")
    import_dir = project / spec.target_import_path
    import_dir.mkdir(parents=True)
    glb = import_dir / f"{spec.asset_id}.glb"
    create_box_glb(
        width_m=spec.dimensions.width_m,
        depth_m=spec.dimensions.depth_m,
        height_m=spec.dimensions.height_m,
        mesh_name=f"SM_{spec.asset_id}",
        collider_name=f"COL_{spec.asset_id}",
        include_lod1=True,
        include_collider=True,
        output_path=glb,
    )
    wrapper = import_dir / f"{spec.asset_id}.tscn"
    wrapper.write_bytes(_wrapper_scene(spec))

    probe = project / "lod_probe.gd"
    probe.write_text(
        "extends SceneTree\n"
        "func _initialize() -> void:\n"
        '    call_deferred("_run")\n'
        "func _dump_meshes(node: Node) -> void:\n"
        "    if node is MeshInstance3D:\n"
        '        print("MESH ", node.name, " visible=", node.visible)\n'
        "    for child in node.get_children():\n"
        "        _dump_meshes(child)\n"
        "func _run() -> void:\n"
        f'    var packed = load("res://{spec.target_import_path.strip("/")}/{spec.asset_id}.tscn")\n'
        "    if packed == null:\n"
        "        quit(2)\n"
        "        return\n"
        "    var instance = packed.instantiate()\n"
        '    print("SCRIPT ", instance.get_script(), " method=", instance.has_method("_apply_accepted_lod0"))\n'
        "    root.add_child(instance)\n"
        "    await process_frame\n"
        "    _dump_meshes(instance)\n"
        '    var lod0 = instance.find_child("SM_prop_energy_crate_01_LOD0", true, false)\n'
        '    var lod1 = instance.find_child("SM_prop_energy_crate_01_LOD1", true, false)\n'
        '    var collision = instance.find_child("CollisionShape3D", true, false)\n'
        "    if lod0 == null or not lod0 is MeshInstance3D or not lod0.visible:\n"
        "        quit(3)\n"
        "        return\n"
        "    if lod1 == null or not lod1 is MeshInstance3D or lod1.visible:\n"
        "        quit(4)\n"
        "        return\n"
        "    if collision == null or collision.shape == null or not collision.shape is BoxShape3D:\n"
        "        quit(5)\n"
        "        return\n"
        '    var factory_runtime = instance.find_child("FactoryRuntime", true, false)\n'
        "    if factory_runtime != null:\n"
        "        quit(6)\n"
        "        return\n"
        "    quit(0)\n",
        encoding="utf-8",
    )
    godot_env = os.environ.copy()
    godot_env["APPDATA"] = str(tmp_path / "appdata")
    godot_env["LOCALAPPDATA"] = str(tmp_path / "localappdata")
    Path(godot_env["APPDATA"]).mkdir()
    Path(godot_env["LOCALAPPDATA"]).mkdir()

    subprocess.run(
        [GODOT_BIN, "--headless", "--editor", "--path", str(project), "--quit", "--import"],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
        env=godot_env,
    )
    probe_result = subprocess.run(
        [GODOT_BIN, "--headless", "--path", str(project), "--script", "res://lod_probe.gd"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env=godot_env,
    )
    assert probe_result.returncode == 0, probe_result.stdout + probe_result.stderr
