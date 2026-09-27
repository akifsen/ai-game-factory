from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from gamefactory.adapters.engines import godot_staging
from gamefactory.adapters.engines.godot_staging import GodotStager
from gamefactory.core.domain.errors import ValidationError


def _make_project(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True)
    (root / "project.godot").write_text(
        "config_version=5\n\n[application]\n"
        'config/name="Staging test"\n'
        'config/project_settings_override="override.cfg"\n'
        "config/use_custom_user_dir.windows=false\n"
        'config/custom_user_dir_name.windows="host-specific"\n'
        "config/disable_project_settings_override.windows=true\n"
        'config/project_settings_override.windows="override.cfg"\n',
        encoding="utf-8",
    )
    (root / "main.gd").write_text("extends Node\n", encoding="utf-8")
    scenario = root / "scenario.json"
    scenario.write_text('{"schema_version":"0.2.0"}\n', encoding="utf-8")
    (root / "override.cfg").write_text(
        '[application]\nrun/main_scene="res://untrusted-override.tscn"\n',
        encoding="utf-8",
    )
    return root, scenario


def _stager(project: Path) -> GodotStager:
    return GodotStager(project, project / ".gamefactory" / "scratch")


def _make_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":

        def quote(path: Path) -> str:
            return "'" + str(path).replace("'", "''") + "'"

        command = (
            f"New-Item -ItemType Junction -Path {quote(link)} -Target {quote(target)} | Out-Null"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"Windows junction creation unavailable: {result.stderr or result.stdout}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Directory symlink creation unavailable on this host: {exc}")


def _remove_directory_link(link: Path) -> None:
    # POSIX symlinks are unlinked; Windows junctions are removed as directory entries.
    if link.is_symlink():
        link.unlink()
    else:
        os.rmdir(link)


def test_manifest_excludes_scenario_state_caches_and_secrets(tmp_path: Path) -> None:
    project, scenario = _make_project(tmp_path / "game")
    (project / ".env").write_text("A=1", encoding="utf-8")
    (project / ".env.local").write_text("B=2", encoding="utf-8")
    (project / "api_token.txt").write_text("secret", encoding="utf-8")
    (project / "signing.p12").write_bytes(b"keystore")
    (project / ".npmrc").write_text("//registry:token=secret", encoding="utf-8")
    (project / "id_rsa").write_text("private", encoding="utf-8")
    (project / "id_ed25519.pub").write_text("public key", encoding="utf-8")
    (project / ".aws").mkdir()
    (project / ".aws" / "credentials").write_text("secret", encoding="utf-8")
    (project / ".ssh").mkdir()
    (project / ".ssh" / "known_hosts").write_text("host key", encoding="utf-8")
    (project / ".godot").mkdir()
    (project / ".godot" / "cache.bin").write_bytes(b"cache")
    (project / ".gamefactory").mkdir()
    (project / ".gamefactory" / "database.sqlite3").write_bytes(b"state")
    (project / ".venv").mkdir()
    (project / ".venv" / "python.exe").write_bytes(b"runtime")

    files, _ = _stager(project).source_manifest(scenario)
    included = {item.relative_path for item in files}

    assert included == {"main.gd", "project.godot"}


def test_manifest_enforces_file_and_total_byte_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _make_project(tmp_path / "game")
    monkeypatch.setattr(godot_staging, "_MAX_FILES", 1)

    with pytest.raises(ValidationError, match="staging file or byte limit"):
        _stager(project).source_manifest()

    monkeypatch.setattr(godot_staging, "_MAX_FILES", 10)
    monkeypatch.setattr(godot_staging, "_MAX_BYTES", 4)
    with pytest.raises(ValidationError, match="staging file or byte limit"):
        _stager(project).source_manifest()


def test_create_stage_is_attempt_scoped_and_preserves_original_project(
    tmp_path: Path,
) -> None:
    project, scenario = _make_project(tmp_path / "game")
    original_config = (project / "project.godot").read_bytes()
    original_override = (project / "override.cfg").read_bytes()
    stager = _stager(project)
    _, manifest_hash = stager.source_manifest(scenario)

    stage, files, staged_hash = stager.create_stage(
        "WF-GODOT-1", "EXEC-1", scenario, manifest_hash, b"extends SceneTree\n"
    )

    assert stage == project / ".gamefactory" / "scratch" / "WF-GODOT-1" / "EXEC-1" / "project"
    assert staged_hash == manifest_hash
    assert {item.relative_path for item in files} == {"main.gd", "project.godot"}
    assert not (stage / ".gamefactory").exists()
    assert (stage / ".factory-harness.gd").read_bytes() == b"extends SceneTree\n"
    staged_config = (stage / "project.godot").read_text(encoding="utf-8")
    assert "config/use_custom_user_dir=true" in staged_config
    assert 'config/custom_user_dir_name="factory-exec-1"' in staged_config
    assert 'config/project_settings_override=""' in staged_config
    assert "config/disable_project_settings_override=false" in staged_config
    assert "config/use_custom_user_dir.windows" not in staged_config
    assert "config/custom_user_dir_name.windows" not in staged_config
    assert "config/disable_project_settings_override.windows" not in staged_config
    assert "config/project_settings_override.windows" not in staged_config
    assert not (stage / "override.cfg").exists()
    assert (project / "project.godot").read_bytes() == original_config
    assert (project / "override.cfg").read_bytes() == original_override
    assert (project / "main.gd").read_text(encoding="utf-8") == "extends Node\n"

    second_stage, _, _ = stager.create_stage(
        "WF-GODOT-1", "EXEC-2", scenario, manifest_hash, b"harness-v2"
    )
    assert second_stage != stage
    assert (
        hashlib.sha256((stage / "main.gd").read_bytes()).hexdigest()
        == hashlib.sha256((second_stage / "main.gd").read_bytes()).hexdigest()
    )
    with pytest.raises(ValidationError, match="already exists"):
        stager.create_stage("WF-GODOT-1", "EXEC-1", scenario, manifest_hash, b"again")


def test_source_change_after_preflight_fingerprint_is_rejected(tmp_path: Path) -> None:
    project, scenario = _make_project(tmp_path / "game")
    stager = _stager(project)
    _, fingerprint = stager.source_manifest(scenario)
    (project / "main.gd").write_text("extends RefCounted\n", encoding="utf-8")

    with pytest.raises(ValidationError, match="sources changed after approval"):
        stager.create_stage("WF-GODOT-1", "EXEC-1", scenario, fingerprint, b"harness")


def test_source_change_between_manifest_and_copy_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, scenario = _make_project(tmp_path / "game")
    stager = _stager(project)
    _, fingerprint = stager.source_manifest(scenario)
    original_hash = godot_staging._sha256_source
    changed = False

    def hash_and_change(path: Path, expected_size: int) -> str:
        nonlocal changed
        digest = original_hash(path, expected_size)
        if path == project / "main.gd" and not changed:
            changed = True
            path.write_text("extends Nade\n", encoding="utf-8")
        return digest

    monkeypatch.setattr(godot_staging, "_sha256_source", hash_and_change)
    with pytest.raises(ValidationError, match="changed while staging"):
        stager.create_stage("WF-GODOT-1", "EXEC-1", scenario, fingerprint, b"harness")


def test_manifest_rejects_symlinked_file_when_supported(tmp_path: Path) -> None:
    project, _ = _make_project(tmp_path / "game")
    external = tmp_path / "outside.txt"
    external.write_text("outside", encoding="utf-8")
    linked_file = project / "linked.txt"
    try:
        linked_file.symlink_to(external)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink creation unavailable on this host: {exc}")

    with pytest.raises(ValidationError, match="link or junction"):
        _stager(project).source_manifest()


def test_manifest_rejects_linked_source_directory(tmp_path: Path) -> None:
    project, _ = _make_project(tmp_path / "game")
    external = tmp_path / "outside"
    external.mkdir()
    link = project / "linked-assets"
    _make_directory_link(link, external)
    try:
        with pytest.raises(ValidationError, match="link or junction"):
            _stager(project).source_manifest()
    finally:
        _remove_directory_link(link)


def test_stager_rejects_root_directory_link(tmp_path: Path) -> None:
    project, _ = _make_project(tmp_path / "game")
    alias = tmp_path / "game-alias"
    _make_directory_link(alias, project)
    try:
        with pytest.raises(ValidationError, match="root cannot be a symlink or junction"):
            GodotStager(alias, alias / ".gamefactory" / "scratch")
    finally:
        _remove_directory_link(alias)


def test_attempt_parent_link_is_rejected_before_writing_outside_scratch(tmp_path: Path) -> None:
    project, scenario = _make_project(tmp_path / "game")
    stager = _stager(project)
    _, fingerprint = stager.source_manifest(scenario)
    scratch = project / ".gamefactory" / "scratch"
    scratch.mkdir(parents=True)
    outside = tmp_path / "outside-attempts"
    outside.mkdir()
    workflow_link = scratch / "WF-GODOT-1"
    _make_directory_link(workflow_link, outside)
    try:
        with pytest.raises(ValidationError):
            stager.create_stage("WF-GODOT-1", "EXEC-1", scenario, fingerprint, b"harness")
        assert not (outside / "EXEC-1").exists()
    finally:
        _remove_directory_link(workflow_link)
