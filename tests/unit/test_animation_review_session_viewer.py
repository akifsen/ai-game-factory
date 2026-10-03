"""V0.8-9c disposable animation review session viewer (fast unit coverage)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import gamefactory.cli.animation_review_session_viewer as viewer
from gamefactory.cli.animation_review_session_bridge import (
    CONTEXT_SCHEMA_VERSION,
    BridgeContext,
)
from gamefactory.cli.exit_codes import EXIT_CONFIG_ERROR, EXIT_SUCCESS
from gamefactory.core.domain.errors import ValidationError
from gamefactory.workflows.v08_candidate_animation_review_set import (
    ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
    CandidateAnimationReviewSetSource,
    _inspect_review_set_directory,
    _ReviewSetDirectorySnapshot,
)
from gamefactory.workflows.v08_candidate_workspace import (
    CANDIDATE_DB_FILENAME,
    CANDIDATE_STATE_DIR,
)
from tests.unit.test_animation_review_session_bridge import (
    _clip_packages,
    _context_document,
    _layout,
)

_VALID_COLLIDER_DOC = {
    "schema_version": "candidate-preview-collider-0.8.0",
    "policy": "capsule",
    "shape_class": "CapsuleShape3D",
    "radius_m": 0.25,
    "height_m": 1.0,
    "center_m": [0.0, 0.5, 0.0],
    "production_eligible": False,
    "promotion_eligible": False,
}


def _collider_bytes(**overrides: Any) -> bytes:
    document = dict(_VALID_COLLIDER_DOC)
    document.update(overrides)
    return json.dumps(document, sort_keys=True).encode("utf-8")


def _bridge_context(layout: dict[str, Any]) -> BridgeContext:
    document = _context_document(layout)
    exchange = layout["workspace"].parent / "viewer-exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    document["exchange_dir"] = str(exchange.resolve())
    from gamefactory.cli.animation_review_session_bridge import _parse_context_document

    return _parse_context_document(document)


def _fresh_overlay_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "owned-root"
    parent.mkdir()
    return parent / "overlay"


def _noop_trusted_executables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        viewer,
        "_assert_snapshot_trusted_godot_executables",
        lambda _snapshot: None,
    )


def _minimal_snapshot(*, root_payload: bytes = b"project") -> _ReviewSetDirectorySnapshot:
    root_files = {
        "project.godot": root_payload,
        "animation_review_set_manifest.json": b"{}",
    }
    clip_files = {
        "clips/000/animation_clip.json": b"{}",
        "clips/000/animation_clip_manifest.json": b"{}",
        "clips/001/animation_clip.json": b"{}",
        "clips/001/animation_clip_manifest.json": b"{}",
    }
    return _ReviewSetDirectorySnapshot(
        manifest_doc={"ordered_clips": [{}, {}]},
        root_files=root_files,
        clip_files=clip_files,
    )


def _tree_digest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root)).replace("\\", "/")] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


def test_prepare_copies_snapshot_bytes_and_leaves_original_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    review = layout["review"]
    before = _tree_digest(review)
    snapshot = _minimal_snapshot(root_payload=b"canonical-project-bytes")

    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(viewer, "_capture_review_set_structure", lambda _path: snapshot)

    overlay = _fresh_overlay_parent(tmp_path)
    prepared = viewer.prepare_animation_review_session_viewer(ctx, overlay)

    assert _tree_digest(review) == before
    assert prepared.overlay_dir.is_dir()
    assert prepared.context_file.is_file()
    assert prepared.exchange_dir.is_dir()
    for name, payload in snapshot.root_files.items():
        assert (prepared.overlay_dir / name).read_bytes() == payload
    for rel_path, payload in snapshot.clip_files.items():
        assert (prepared.overlay_dir / rel_path).read_bytes() == payload
    assert (prepared.overlay_dir / "animation_review_session.tscn").is_file()
    assert (prepared.overlay_dir / "animation_review_session_controller.gd").is_file()


def test_prepare_rejects_post_copy_structure_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    first = _minimal_snapshot(root_payload=b"stable")
    second = _minimal_snapshot(root_payload=b"drifted")
    calls = {"n": 0}

    def _capture(_path: Path) -> _ReviewSetDirectorySnapshot:
        calls["n"] += 1
        if calls["n"] <= 2:
            return first
        return second

    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(viewer, "_capture_review_set_structure", _capture)

    overlay = _fresh_overlay_parent(tmp_path)
    with pytest.raises(ValidationError, match="drifted"):
        viewer.prepare_animation_review_session_viewer(ctx, overlay)
    assert not overlay.exists()


def test_prepare_rejects_original_review_dir_drift_after_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    stable = _minimal_snapshot(root_payload=b"stable")
    drifted = _minimal_snapshot(root_payload=b"mutated")
    calls = {"n": 0}

    def _capture(_path: Path) -> _ReviewSetDirectorySnapshot:
        calls["n"] += 1
        if calls["n"] == 1:
            return stable
        if calls["n"] == 2:
            return drifted
        return stable

    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(viewer, "_capture_review_set_structure", _capture)

    overlay = _fresh_overlay_parent(tmp_path)
    with pytest.raises(ValidationError, match="changed during viewer preparation"):
        viewer.prepare_animation_review_session_viewer(ctx, overlay)
    assert not overlay.exists()


def test_prepare_rejects_relative_overlay_dir(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    with pytest.raises(ValidationError, match="absolute path"):
        viewer.prepare_animation_review_session_viewer(ctx, Path("relative-overlay"))


def test_prepare_rejects_overlay_inside_review_dir(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    overlay = layout["review"] / "nested-overlay"
    with pytest.raises(ValidationError, match="outside"):
        viewer.prepare_animation_review_session_viewer(ctx, overlay)


def test_prepare_rejects_existing_overlay_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    overlay = _fresh_overlay_parent(tmp_path)
    overlay.mkdir()
    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(
        viewer,
        "_capture_review_set_structure",
        lambda _path: _minimal_snapshot(),
    )
    with pytest.raises(ValidationError, match="must not already exist"):
        viewer.prepare_animation_review_session_viewer(ctx, overlay)


def test_context_serializes_original_roots_and_argv_uses_equals_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(
        viewer,
        "_capture_review_set_structure",
        lambda _path: _minimal_snapshot(),
    )
    overlay = _fresh_overlay_parent(tmp_path)
    prepared = viewer.prepare_animation_review_session_viewer(ctx, overlay)
    document = json.loads(prepared.context_file.read_text(encoding="utf-8"))

    assert document["schema_version"] == CONTEXT_SCHEMA_VERSION
    assert document["review_dir"] == str(layout["review"].resolve())
    assert document["project_root"] == str(layout["workspace"].resolve())
    assert document["preview_dir"] == str(layout["preview"].resolve())
    assert document["exchange_dir"] == str(prepared.exchange_dir.resolve())
    assert document["session_path"] == str(layout["session_sidecar"].resolve())

    fake_godot = tmp_path / "godot.exe"
    fake_godot.write_bytes(b"")
    argv = prepared.godot_argv(godot_executable=fake_godot)
    assert argv[0] == str(fake_godot.resolve())
    assert argv[1:5] == (
        "--path",
        str(prepared.overlay_dir.resolve()),
        "--scene",
        viewer._SCENE_RESOURCE,
    )
    assert argv[5] == "--"
    assert argv[6] == f"--session-python-executable={prepared.python_executable}"
    assert argv[7] == f"--session-context-file={prepared.context_file.resolve()}"
    assert argv[8] == f"--session-exchange-dir={prepared.exchange_dir.resolve()}"


def test_run_viewer_rejects_context_file_inside_review_tree(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    context_path = layout["review"] / "context.json"
    context_path.write_text("{}", encoding="utf-8")
    godot = tmp_path / "godot.exe"
    godot.write_bytes(b"")
    code = viewer.run_animation_review_session_viewer(
        context_file=context_path,
        godot_executable=godot,
    )
    assert code == EXIT_CONFIG_ERROR


def test_run_viewer_cleans_owned_overlay_after_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    context_path = tmp_path / "external-context.json"
    exchange = tmp_path / "external-exchange"
    exchange.mkdir()
    document = viewer._context_document(ctx, exchange_dir=exchange)
    context_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")

    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(
        viewer,
        "_capture_review_set_structure",
        lambda _path: _minimal_snapshot(),
    )

    seen_overlay: list[Path] = []

    def _fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        overlay = Path(argv[2])
        seen_overlay.append(overlay)
        assert overlay.is_dir()
        assert overlay.parent.name.startswith("gf-anim-review-viewer-")
        return subprocess.CompletedProcess(argv, 0)

    godot = tmp_path / "godot.exe"
    godot.write_bytes(b"")

    with patch.object(viewer.subprocess, "run", side_effect=_fake_run):
        code = viewer.run_animation_review_session_viewer(
            context_file=context_path,
            godot_executable=godot,
        )

    assert code == EXIT_SUCCESS
    assert len(seen_overlay) == 1
    assert not seen_overlay[0].exists()
    assert not seen_overlay[0].parent.exists()


def test_run_viewer_cleans_overlay_when_subprocess_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    context_path = tmp_path / "external-context.json"
    exchange = tmp_path / "external-exchange"
    exchange.mkdir()
    context_path.write_text(
        json.dumps(viewer._context_document(ctx, exchange_dir=exchange), sort_keys=True),
        encoding="utf-8",
    )
    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(
        viewer,
        "_capture_review_set_structure",
        lambda _path: _minimal_snapshot(),
    )
    seen: list[Path] = []

    def _fail(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(Path(argv[2]))
        return subprocess.CompletedProcess(argv, 17)

    godot = tmp_path / "godot.exe"
    godot.write_bytes(b"")

    with patch.object(viewer.subprocess, "run", side_effect=_fail):
        code = viewer.run_animation_review_session_viewer(
            context_file=context_path,
            godot_executable=godot,
        )

    assert code == 17
    assert len(seen) == 1
    assert not seen[0].exists()


def test_prepare_does_not_touch_candidate_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    db_path = layout["workspace"] / CANDIDATE_STATE_DIR / CANDIDATE_DB_FILENAME
    assert db_path.is_file()
    before = db_path.read_bytes()
    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(
        viewer,
        "_capture_review_set_structure",
        lambda _path: _minimal_snapshot(),
    )
    overlay = _fresh_overlay_parent(tmp_path)
    viewer.prepare_animation_review_session_viewer(ctx, overlay)
    assert db_path.read_bytes() == before


def test_expected_review_set_preview_derives_bones_from_character_glb_not_scene_claim() -> None:
    from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
    from gamefactory.workflows.v08_candidate_animation_clip_preview import (
        _decode_verified_glb_snapshot,
        _verified_weighted_bone_names,
    )
    from gamefactory.workflows.v08_candidate_animation_review_set import (
        _build_animation_review_set_preview_tscn,
    )

    glb_bytes = build_humanoid_skinned_glb("positive")
    glb_sha256 = hashlib.sha256(glb_bytes).hexdigest()
    glb_bones = _verified_weighted_bone_names(_decode_verified_glb_snapshot(glb_bytes, glb_sha256))
    clip_sha = "ab" * 32
    tampered_preview = _build_animation_review_set_preview_tscn(
        ["res://clips/000/animation_clip.json"],
        [clip_sha],
        ["acceptance-arm-wave"],
        ("Spine", "RightUpperArm"),
    )
    snapshot = _ReviewSetDirectorySnapshot(
        manifest_doc={
            "ordered_clips": [{"clip_id": "acceptance-arm-wave", "raw_clip_sha256": clip_sha}],
        },
        root_files={
            "character.glb": glb_bytes,
            "animation_review_set_preview.tscn": tampered_preview,
        },
        clip_files={},
    )
    expected = viewer._expected_review_set_preview_tscn(snapshot)
    assert expected != tampered_preview
    assert viewer._verified_weighted_bone_names_from_snapshot_glb(snapshot) == glb_bones
    assert b"RightUpperArm" not in expected
    assert b"LeftUpperArm" in expected


def test_trusted_godot_guard_rejects_coherent_playback_script_comment() -> None:
    from gamefactory.workflows.v08_candidate_animation_review_set import _player_script_bytes

    player = _player_script_bytes() + b"\n# benign coherent rehash comment\n"
    snapshot = _ReviewSetDirectorySnapshot(
        manifest_doc={"ordered_clips": []},
        root_files={"animation_clip_preview_player.gd": player},
        clip_files={},
    )
    with pytest.raises(ValidationError, match="trusted template"):
        viewer._assert_snapshot_trusted_godot_executables(snapshot)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"radius_m": True}, "radius_m"),
        ({"height_m": False}, "height_m"),
        ({"center_m": ["0", 0.0, 0.0]}, r"center_m\[0\]"),
        ({"center_m": [0.0, None, 0.0]}, r"center_m\[1\]"),
        ({"center_m": [0.0, 0.0, float("nan")]}, "NaN"),
        ({"production_eligible": True}, "production_eligible"),
        ({"extra_field": 1}, "fields do not match"),
        ({"radius_m": 0.05}, "minimum"),
        ({"height_m": 0.4}, "greater than twice"),
    ],
)
def test_validated_collider_rejects_invalid_contract(
    overrides: dict[str, Any],
    match: str,
) -> None:
    with pytest.raises(ValidationError, match=match):
        viewer._validated_collider_fields(_collider_bytes(**overrides))


def test_validated_collider_accepts_canonical_preview_contract() -> None:
    radius, height, center = viewer._validated_collider_fields(_collider_bytes())
    assert radius == 0.25
    assert height == 1.0
    assert center == (0.0, 0.5, 0.0)


def _collider_bytes_with_radius_decimal_digits(digit_count: int) -> bytes:
    assert digit_count >= 1
    radius_literal = "1" + "0" * (digit_count - 1)
    text = _collider_bytes().decode("utf-8")
    updated = text.replace('"radius_m": 0.25', f'"radius_m": {radius_literal}', 1)
    assert updated != text
    return updated.encode("utf-8")


def _collider_deeply_nested_malformed_bytes(depth: int) -> bytes:
    return b'{"a":' * depth + b"0" + b"}" * depth


def test_collider_json_finite_scalar_accepts_finite_normals() -> None:
    assert viewer._collider_json_finite_scalar(0.25, "radius_m") == 0.25
    assert viewer._collider_json_finite_scalar(1, "height_m") == 1.0


def test_validated_collider_rejects_radius_with_four_thousand_decimal_digits() -> None:
    payload = _collider_bytes_with_radius_decimal_digits(4000)
    assert len(payload) < 64 * 1024
    with pytest.raises(ValidationError, match="radius_m must be a finite number"):
        viewer._validated_collider_fields(payload)


def test_validated_collider_rejects_radius_with_five_thousand_decimal_digits() -> None:
    payload = _collider_bytes_with_radius_decimal_digits(5000)
    with pytest.raises(ValidationError, match="malformed"):
        viewer._validated_collider_fields(payload)


def test_validated_collider_rejects_deeply_nested_malformed_bytes() -> None:
    payload = _collider_deeply_nested_malformed_bytes(4000)
    assert len(payload) < 64 * 1024
    with pytest.raises(ValidationError, match="malformed"):
        viewer._validated_collider_fields(payload)


def test_run_viewer_returns_config_error_when_tempdir_allocation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    context_path = tmp_path / "external-context.json"
    exchange = tmp_path / "external-exchange"
    exchange.mkdir()
    ctx = _bridge_context(layout)
    context_path.write_text(
        json.dumps(viewer._context_document(ctx, exchange_dir=exchange), sort_keys=True),
        encoding="utf-8",
    )
    godot = tmp_path / "godot.exe"
    godot.write_bytes(b"")

    def _fail_mkdtemp(*_args: object, **_kwargs: object) -> str:
        raise OSError("tempdir unavailable")

    monkeypatch.setattr(viewer.tempfile, "mkdtemp", _fail_mkdtemp)
    code = viewer.run_animation_review_session_viewer(
        context_file=context_path,
        godot_executable=godot,
    )
    assert code == EXIT_CONFIG_ERROR


def test_run_viewer_subprocess_oserror_returns_config_error_and_owned_tree_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    context_path = tmp_path / "external-context.json"
    exchange = tmp_path / "external-exchange"
    exchange.mkdir()
    context_path.write_text(
        json.dumps(viewer._context_document(ctx, exchange_dir=exchange), sort_keys=True),
        encoding="utf-8",
    )
    owned_parent = tmp_path / "gf-anim-review-viewer-owned"
    owned_parent.mkdir()

    def _mkdtemp_in_tmp(*_args: object, **_kwargs: object) -> str:
        return str(owned_parent)

    monkeypatch.setattr(viewer.tempfile, "mkdtemp", _mkdtemp_in_tmp)
    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(
        viewer,
        "_capture_review_set_structure",
        lambda _path: _minimal_snapshot(),
    )
    godot = tmp_path / "godot.exe"
    godot.write_bytes(b"")

    def _exec_fail(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise OSError("godot launch failed")

    with patch.object(viewer.subprocess, "run", side_effect=_exec_fail):
        code = viewer.run_animation_review_session_viewer(
            context_file=context_path,
            godot_executable=godot,
        )
    assert code == EXIT_CONFIG_ERROR
    assert not owned_parent.exists()


def test_run_viewer_context_read_oserror_returns_config_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context_path = tmp_path / "external-context.json"
    context_path.write_text("{}", encoding="utf-8")
    godot = tmp_path / "godot.exe"
    godot.write_bytes(b"")

    def _read_fail(_path: Path) -> BridgeContext:
        raise OSError("context read failed")

    monkeypatch.setattr(viewer, "_load_bridge_context_from_file", _read_fail)
    code = viewer.run_animation_review_session_viewer(
        context_file=context_path,
        godot_executable=godot,
    )
    assert code == EXIT_CONFIG_ERROR


def test_run_viewer_does_not_swallow_unexpected_programmer_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    context_path = tmp_path / "external-context.json"
    exchange = tmp_path / "external-exchange"
    exchange.mkdir()
    context_path.write_text(
        json.dumps(viewer._context_document(ctx, exchange_dir=exchange), sort_keys=True),
        encoding="utf-8",
    )
    godot = tmp_path / "godot.exe"
    godot.write_bytes(b"")

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("programmer fault")

    monkeypatch.setattr(viewer, "prepare_animation_review_session_viewer", _boom)
    with pytest.raises(RuntimeError, match="programmer fault"):
        viewer.run_animation_review_session_viewer(
            context_file=context_path,
            godot_executable=godot,
        )


def test_main_returns_config_error_for_relative_godot_path(tmp_path: Path) -> None:
    context_path = tmp_path / "context.json"
    context_path.write_text("{}", encoding="utf-8")
    code = viewer.main(
        [
            "--context-file",
            str(context_path.resolve()),
            "--godot-executable",
            "relative/godot.exe",
        ],
    )
    assert code == EXIT_CONFIG_ERROR


def test_run_viewer_hides_console_on_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "win32":
        pytest.skip("CREATE_NO_WINDOW is Windows-specific")
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    context_path = tmp_path / "external-context.json"
    exchange = tmp_path / "external-exchange"
    exchange.mkdir()
    context_path.write_text(
        json.dumps(viewer._context_document(ctx, exchange_dir=exchange), sort_keys=True),
        encoding="utf-8",
    )
    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(
        viewer,
        "_capture_review_set_structure",
        lambda _path: _minimal_snapshot(),
    )
    godot = tmp_path / "godot.exe"
    godot.write_bytes(b"")
    captured: dict[str, Any] = {}

    def _record_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    with patch.object(viewer.subprocess, "run", side_effect=_record_run):
        viewer.run_animation_review_session_viewer(
            context_file=context_path,
            godot_executable=godot,
        )
    assert captured.get("creationflags") == subprocess.CREATE_NO_WINDOW


def test_prepare_overlay_mkdir_failure_does_not_cleanup_unowned_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(
        viewer,
        "_capture_review_set_structure",
        lambda _path: _minimal_snapshot(),
    )
    overlay = _fresh_overlay_parent(tmp_path)
    cleanup_calls: list[Path] = []
    original_cleanup = viewer._cleanup_owned_tree

    def _record_cleanup(root: Path, *, intended_root: Path) -> None:
        cleanup_calls.append(root)
        original_cleanup(root, intended_root=intended_root)

    monkeypatch.setattr(viewer, "_cleanup_owned_tree", _record_cleanup)

    real_mkdir = Path.mkdir

    def _mkdir_maybe_fail(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == overlay:
            raise OSError("overlay mkdir race")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _mkdir_maybe_fail)

    with pytest.raises(OSError, match="overlay mkdir race"):
        viewer.prepare_animation_review_session_viewer(ctx, overlay)
    assert cleanup_calls == []


def test_cleanup_owned_tree_skips_symlink_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-owned"
    real_root.mkdir()
    marker = real_root / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    link_root = tmp_path / "linked-owned"
    try:
        link_root.symlink_to(real_root, target_is_directory=True)
    except OSError:
        pytest.skip("symlink not supported on this platform")
    viewer._cleanup_owned_tree(link_root, intended_root=link_root)
    assert marker.read_text(encoding="utf-8") == "keep"


def _malformed_review_manifest_huge_integer_bytes(digit_count: int) -> bytes:
    assert digit_count >= 1
    literal = "1" + "0" * (digit_count - 1)
    return f'{{"ordered_clips": [{literal}, {{}}]}}'.encode()


def _malformed_review_manifest_deeply_nested_bytes(depth: int) -> bytes:
    return b'{"a":' * depth + b"0" + b"}" * depth


def test_capture_review_set_structure_rejects_manifest_with_five_thousand_digit_integer(
    tmp_path: Path,
) -> None:
    review = tmp_path / "review"
    review.mkdir()
    payload = _malformed_review_manifest_huge_integer_bytes(5000)
    assert len(payload) < 64 * 1024
    (review / "animation_review_set_manifest.json").write_bytes(payload)
    with pytest.raises(ValidationError, match="malformed"):
        viewer._capture_review_set_structure(review)


def test_capture_review_set_structure_rejects_deeply_nested_manifest_bytes(
    tmp_path: Path,
) -> None:
    review = tmp_path / "review"
    review.mkdir()
    payload = _malformed_review_manifest_deeply_nested_bytes(4000)
    assert len(payload) < 64 * 1024
    (review / "animation_review_set_manifest.json").write_bytes(payload)
    with pytest.raises(ValidationError, match="malformed"):
        viewer._capture_review_set_structure(review)


def test_capture_review_set_structure_maps_inspector_value_error_to_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = tmp_path / "review"
    review.mkdir()
    manifest = {
        "schema_version": ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
        "ordered_clips": [
            {"clip_id": "clip-a", "raw_clip_sha256": "ab" * 32},
            {"clip_id": "clip-b", "raw_clip_sha256": "cd" * 32},
        ],
    }
    (review / "animation_review_set_manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True).encode("utf-8"),
    )

    def _inspector_fault(*_args: object, **_kwargs: object) -> _ReviewSetDirectorySnapshot:
        raise ValueError("synthetic inspector manifest parse fault")

    monkeypatch.setattr(viewer, "_inspect_review_set_directory", _inspector_fault)
    with pytest.raises(ValidationError, match="malformed") as exc_info:
        viewer._capture_review_set_structure(review)
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_prepare_leaves_no_overlay_when_second_capture_inspector_raises_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    review = layout["review"]
    manifest = {
        "schema_version": ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
        "ordered_clips": [
            {"clip_id": "clip-a", "raw_clip_sha256": "ab" * 32},
            {"clip_id": "clip-b", "raw_clip_sha256": "cd" * 32},
        ],
    }
    (review / "animation_review_set_manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True).encode("utf-8"),
    )
    _noop_trusted_executables(monkeypatch)
    capture_calls = {"n": 0}
    stable_snapshot = _minimal_snapshot(root_payload=b"stable-for-first-capture")
    real_capture = viewer._capture_review_set_structure

    def _capture_with_second_read_inspector_fault(path: Path) -> _ReviewSetDirectorySnapshot:
        capture_calls["n"] += 1
        if capture_calls["n"] == 1:
            return stable_snapshot
        return real_capture(path)

    def _inspector_fault_on_second_capture(
        _review_dir: Path,
        *,
        expected_clip_count: int,
    ) -> _ReviewSetDirectorySnapshot:
        if capture_calls["n"] >= 2:
            raise ValueError("injected second-read inspector fault")
        return _inspect_review_set_directory(
            _review_dir,
            expected_clip_count=expected_clip_count,
        )

    monkeypatch.setattr(
        viewer, "_capture_review_set_structure", _capture_with_second_read_inspector_fault
    )
    monkeypatch.setattr(viewer, "_inspect_review_set_directory", _inspector_fault_on_second_capture)
    overlay = _fresh_overlay_parent(tmp_path)
    with pytest.raises(ValidationError, match="malformed"):
        viewer.prepare_animation_review_session_viewer(ctx, overlay)
    assert not overlay.exists()


def test_session_paths_stay_outside_canonical_review_roots(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    review = layout["review"]
    preview = layout["preview"]
    packages = _clip_packages(layout["workspace"].parent.parent)
    pkg_a = Path(packages[0]["animation_dir"])
    clip_a = Path(packages[0]["clip_path"])
    exchange = layout["workspace"].parent / "viewer-exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    ctx = BridgeContext(
        project_root=layout["workspace"],
        workflow_id=layout["workflow_id"],
        preview_dir=preview,
        review_dir=review,
        clip_packages=(
            CandidateAnimationReviewSetSource(pkg_a, clip_a),
            CandidateAnimationReviewSetSource(pkg_a, clip_a),
        ),
        session_path=layout["session_sidecar"],
        exchange_dir=exchange,
    )
    overlay = review / "overlay-blocked"
    with pytest.raises(ValidationError, match="outside"):
        viewer.prepare_animation_review_session_viewer(ctx, overlay)


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip(f"Windows symlink privilege unavailable: {exc}")
        raise


def test_assert_trusted_executable_accepts_current_interpreter_alias() -> None:
    alias = Path(sys.executable)
    if not alias.is_absolute():
        pytest.skip("sys.executable is not absolute on this platform")
    trusted = viewer._assert_trusted_executable(alias, label="python executable")
    assert trusted == Path(os.path.abspath(str(alias)))
    if os.name != "nt" and alias.is_symlink():
        assert trusted != alias.resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX leaf symlink alias contract")
def test_assert_trusted_executable_venv_alias_preserves_prefix(tmp_path: Path) -> None:
    venv_dir = tmp_path / "probe-venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(venv_dir)
    venv_python = venv_dir / "bin" / "python"
    assert venv_python.is_symlink()
    trusted = viewer._assert_trusted_executable(venv_python, label="python executable")
    assert trusted == Path(os.path.abspath(str(venv_python)))
    proc = subprocess.run(
        [str(trusted), "-c", "import sys; print(sys.prefix)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == str(venv_dir)


@pytest.mark.skipif(os.name == "nt", reason="POSIX leaf symlink alias contract")
def test_assert_trusted_executable_rejects_dangling_leaf_symlink(tmp_path: Path) -> None:
    dangling = tmp_path / "dangling-python"
    _symlink_or_skip(dangling, tmp_path / "missing-target")
    with pytest.raises(ValidationError, match="does not exist"):
        viewer._assert_trusted_executable(dangling, label="python executable")


@pytest.mark.skipif(os.name == "nt", reason="POSIX leaf symlink alias contract")
def test_assert_trusted_executable_rejects_symlink_loop(tmp_path: Path) -> None:
    link_a = tmp_path / "loop-a"
    link_b = tmp_path / "loop-b"
    _symlink_or_skip(link_a, link_b)
    _symlink_or_skip(link_b, link_a)
    with pytest.raises(ValidationError, match="loop"):
        viewer._assert_trusted_executable(link_a, label="python executable")


@pytest.mark.skipif(os.name == "nt", reason="POSIX leaf symlink alias contract")
def test_assert_trusted_executable_relative_leaf_chain_preserves_alias(tmp_path: Path) -> None:
    real_bin = tmp_path / "real-bin"
    real_bin.mkdir()
    interpreter = real_bin / "python3"
    interpreter.write_bytes(b"")
    alias_bin = tmp_path / "alias-bin"
    alias_bin.mkdir()
    _symlink_or_skip(alias_bin / "python3", interpreter)
    alias = alias_bin / "python"
    _symlink_or_skip(alias, Path("python3"))
    trusted = viewer._assert_trusted_executable(alias, label="python executable")
    assert trusted == Path(os.path.abspath(str(alias)))


@pytest.mark.skipif(os.name == "nt", reason="POSIX leaf symlink alias contract")
def test_assert_trusted_executable_rejects_dangling_relative_symlink_target(tmp_path: Path) -> None:
    alias = tmp_path / "python-alias"
    _symlink_or_skip(alias, Path("relative-target"))
    with pytest.raises(ValidationError, match="does not exist"):
        viewer._assert_trusted_executable(alias, label="python executable")


@pytest.mark.skipif(os.name == "nt", reason="POSIX leaf symlink alias contract")
def test_assert_trusted_executable_rejects_absolute_target_under_linked_directory(
    tmp_path: Path,
) -> None:
    real_bin = tmp_path / "real-bin"
    real_bin.mkdir()
    interpreter = real_bin / "python"
    interpreter.write_bytes(b"")
    linked_bin = tmp_path / "linked-bin"
    _symlink_or_skip(linked_bin, real_bin, target_is_directory=True)
    alias = tmp_path / "python-alias"
    _symlink_or_skip(alias, linked_bin / "python")
    with pytest.raises(ValidationError, match="crosses a symlink"):
        viewer._assert_trusted_executable(alias, label="python executable")


@pytest.mark.skipif(os.name == "nt", reason="POSIX leaf symlink alias contract")
def test_assert_trusted_executable_rejects_linked_intermediate_symlink_target(
    tmp_path: Path,
) -> None:
    real_bin = tmp_path / "real-bin"
    real_bin.mkdir()
    interpreter = real_bin / "python"
    interpreter.write_bytes(b"")
    linked_bin = tmp_path / "linked-bin"
    _symlink_or_skip(linked_bin, real_bin, target_is_directory=True)
    hop = tmp_path / "hop"
    _symlink_or_skip(hop, linked_bin / "python")
    alias = tmp_path / "python-alias"
    _symlink_or_skip(alias, hop)
    with pytest.raises(ValidationError, match="crosses a symlink"):
        viewer._assert_trusted_executable(alias, label="python executable")


@pytest.mark.skipif(os.name == "nt", reason="POSIX leaf symlink alias contract")
def test_assert_trusted_executable_rejects_readlink_target_with_linked_dir_parent_dotdot(
    tmp_path: Path,
) -> None:
    real_bin = tmp_path / "real-bin"
    real_bin.mkdir()
    interpreter = real_bin / "python"
    interpreter.write_bytes(b"")
    linked_bin = tmp_path / "linked-bin"
    _symlink_or_skip(linked_bin, real_bin, target_is_directory=True)
    alias = tmp_path / "python-alias"
    _symlink_or_skip(alias, Path("linked-bin/../real-bin/python"))
    with pytest.raises(ValidationError, match="crosses a symlink"):
        viewer._assert_trusted_executable(alias, label="python executable")


@pytest.mark.skipif(os.name == "nt", reason="POSIX leaf symlink alias contract")
def test_assert_trusted_executable_rejects_lexical_parent_hiding_linked_directory(
    tmp_path: Path,
) -> None:
    real_bin = tmp_path / "real-bin"
    real_bin.mkdir()
    interpreter = real_bin / "python"
    interpreter.write_bytes(b"")
    linked_bin = tmp_path / "linked-bin"
    _symlink_or_skip(linked_bin, real_bin, target_is_directory=True)
    lexical_alias = linked_bin / ".." / "linked-bin" / "python"
    with pytest.raises(ValidationError, match="crosses a symlink"):
        viewer._assert_trusted_executable(lexical_alias, label="python executable")


@pytest.mark.skipif(os.name == "nt", reason="POSIX leaf symlink alias contract")
def test_assert_trusted_executable_rejects_directory_symlink_leaf(tmp_path: Path) -> None:
    real_dir = tmp_path / "real-dir"
    real_dir.mkdir()
    alias = tmp_path / "python-dirlink"
    _symlink_or_skip(alias, real_dir, target_is_directory=True)
    with pytest.raises(ValidationError, match="regular file"):
        viewer._assert_trusted_executable(alias, label="python executable")


@pytest.mark.skipif(os.name == "nt", reason="POSIX leaf symlink alias contract")
def test_assert_trusted_executable_rejects_directory_symlink_ancestor(tmp_path: Path) -> None:
    real_bin = tmp_path / "real-bin"
    real_bin.mkdir()
    interpreter = real_bin / "python"
    interpreter.write_bytes(b"")
    linked_bin = tmp_path / "linked-bin"
    _symlink_or_skip(linked_bin, real_bin, target_is_directory=True)
    alias = linked_bin / "python"
    with pytest.raises(ValidationError, match="crosses a symlink"):
        viewer._assert_trusted_executable(alias, label="python executable")


def test_assert_trusted_executable_posix_alias_does_not_use_whole_leaf_lexical_unsafe_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """notOSproof: POSIX alias must use ancestor-only guard, not whole-leaf lexical unsafe."""
    monkeypatch.setattr(viewer.sys, "platform", "linux")
    real_bin = tmp_path / "real-bin"
    real_bin.mkdir()
    interpreter = real_bin / "python3"
    interpreter.write_bytes(b"")
    alias = tmp_path / "python-alias"
    _symlink_or_skip(alias, interpreter)
    with patch.object(
        viewer,
        "candidate_evidence_lexical_unsafe",
        side_effect=AssertionError("whole-leaf lexical unsafe must not run for POSIX alias"),
    ) as lexical_unsafe:
        trusted = viewer._assert_trusted_executable(alias, label="python executable")
    lexical_unsafe.assert_not_called()
    assert trusted == Path(os.path.abspath(str(alias)))


def test_assert_trusted_executable_posix_leaf_alias_synthetic_when_platform_patched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows-only synthetic POSIX platform patch")
    monkeypatch.setattr(viewer.sys, "platform", "linux")
    real_bin = tmp_path / "real-bin"
    real_bin.mkdir()
    interpreter = real_bin / "python3"
    interpreter.write_bytes(b"")
    alias = tmp_path / "python-alias"
    _symlink_or_skip(alias, interpreter)
    trusted = viewer._assert_trusted_executable(alias, label="python executable")
    assert trusted == Path(os.path.abspath(str(alias)))


def test_assert_trusted_executable_rejects_relative_path(tmp_path: Path) -> None:
    relative = Path("relative-python")
    with pytest.raises(ValidationError, match="absolute"):
        viewer._assert_trusted_executable(relative, label="python executable")


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link rejection uses single-link stat")
def test_assert_trusted_executable_rejects_hard_linked_target(tmp_path: Path) -> None:
    primary = tmp_path / "python-real"
    primary.write_bytes(b"")
    duplicate = tmp_path / "python-hardlink"
    os.link(primary, duplicate)
    with pytest.raises(ValidationError, match="hard link"):
        viewer._assert_trusted_executable(duplicate, label="python executable")


def test_godot_argv_preserves_python_executable_alias_in_equals_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(
        viewer,
        "_capture_review_set_structure",
        lambda _path: _minimal_snapshot(),
    )
    overlay = _fresh_overlay_parent(tmp_path)
    python_alias = Path(sys.executable)
    prepared = viewer.prepare_animation_review_session_viewer(
        ctx,
        overlay,
        python_executable=python_alias,
    )
    godot = tmp_path / "godot.bin"
    godot.write_bytes(b"")
    argv = prepared.godot_argv(godot_executable=godot)
    assert argv[6] == f"--session-python-executable={prepared.python_executable}"
    assert prepared.python_executable == viewer._assert_trusted_executable(
        python_alias,
        label="python executable",
    )
