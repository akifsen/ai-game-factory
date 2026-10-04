from __future__ import annotations

from pathlib import Path

import pytest

from gamefactory.adapters.engines.godot_operations import (
    build_export_request,
    build_run_request,
)
from gamefactory.adapters.projects.onboarding import create_godot_project
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.project_operations import ProjectOperationRequest


def test_onboarding_creates_standalone_2d_and_3d_projects(tmp_path: Path) -> None:
    two_d = create_godot_project(tmp_path / "two", "Two", "2d")
    three_d = create_godot_project(tmp_path / "three", "Three", "3d")
    assert set(two_d["files"]) == {"project.godot", "main.tscn", "main.gd"}
    assert "res://main.tscn" in (tmp_path / "two" / "project.godot").read_text()
    assert "draw_circle" in (tmp_path / "two" / "main.gd").read_text()
    scene = (tmp_path / "three" / "main.tscn").read_text()
    assert "Camera3D" in scene and "DirectionalLight3D" in scene
    assert "Factory" not in "".join((tmp_path / "three" / f).read_text() for f in three_d["files"])


def test_onboarding_refuses_existing_paths_and_config_injection(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(ValidationError):
        create_godot_project(existing, "Game")
    with pytest.raises(ValidationError):
        create_godot_project(tmp_path / "bad", 'bad\nrun/main_scene="evil.tscn"')


def test_onboarding_staging_failure_does_not_leave_partial_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "staged-failure"

    def fail_sync(_fd: int) -> None:
        raise OSError("simulated disk flush failure")

    monkeypatch.setattr("gamefactory.adapters.projects.onboarding.os.fsync", fail_sync)
    with pytest.raises(OSError):
        create_godot_project(destination, "Game")
    assert not destination.exists()
    assert not list(tmp_path.glob(".gamefactory-project-*"))


def test_onboarding_failure_preserves_concurrently_edited_published_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gamefactory.adapters.projects.onboarding as onboarding

    destination = tmp_path / "racing-project"
    real_link = onboarding.os.link
    calls = 0
    edited = b"concurrent operator edit"

    def fail_after_edit(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            published = destination / "project.godot"
            published.write_bytes(edited)
            raise OSError("simulated second-file publication failure")
        real_link(source, target)

    monkeypatch.setattr(onboarding.os, "link", fail_after_edit)
    with pytest.raises(ValidationError, match="preserved for recovery"):
        create_godot_project(destination, "Game")
    assert (destination / "project.godot").read_bytes() == edited


def test_run_and_export_requests_bind_scene_and_named_preset(tmp_path: Path) -> None:
    exe = tmp_path / "godot"
    exe.write_bytes(b"placeholder executable")
    project = tmp_path / "project"
    project.mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    (project / "main.tscn").write_text("[gd_scene format=3]\n")
    (project / "export_presets.cfg").write_text(
        '[preset.0]\nname="Linux Release"\nplatform="Linux/X11"\n'
    )
    attempt = project / ".gamefactory" / "attempts" / "one"
    attempt.mkdir(parents=True)
    run = build_run_request(exe, project, "res://main.tscn", timeout_seconds=12)
    assert run.args[-1] == "res://main.tscn" and run.timeout_seconds == 12
    request = build_export_request(exe, project, "Linux Release", attempt / "game.x86_64")
    assert "--export-release" in request.args
    with pytest.raises(ValidationError):
        build_export_request(exe, project, "made-up", attempt / "other")


def test_versioned_project_operation_request_rejects_unknown_and_mistyped_inputs() -> None:
    assert ProjectOperationRequest.from_dict(
        {"schema_version": 1, "operation": "discover", "parameters": {"include_git": False}}
    ).parameters == {"include_git": False}
    with pytest.raises(ValidationError):
        ProjectOperationRequest.from_dict(
            {"schema_version": 1, "operation": "discover", "parameters": {"include_git": "false"}}
        )
    with pytest.raises(ValidationError):
        ProjectOperationRequest.from_dict(
            {
                "schema_version": 1,
                "operation": "export",
                "parameters": {"executable": "godot", "preset": "x", "unexpected": True},
            }
        )
    with pytest.raises(ValidationError):
        ProjectOperationRequest.from_dict(
            {"schema_version": 1.0, "operation": "run", "parameters": {"executable": "godot"}}
        )
