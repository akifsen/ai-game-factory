"""V0.8-12a disposable animation review compare viewer (fast unit coverage)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import gamefactory.cli.animation_review_compare_viewer as compare_viewer
from gamefactory.cli.animation_review_session_bridge import (
    CONTEXT_SCHEMA_VERSION,
    BridgeContext,
    build_readonly_candidate_handlers,
)
from gamefactory.cli.animation_review_session_viewer import _context_document
from gamefactory.core.domain.errors import ValidationError
from gamefactory.workflows.v08_candidate_animation_review_set import (
    ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
    CandidateAnimationReviewSetSource,
    _ReviewSetDirectorySnapshot,
)
from tests.unit.test_animation_review_session_bridge import (
    _clip_packages,
    _layout,
)
from tests.unit.test_animation_review_session_bridge import (
    _context_document as _bridge_context_document,
)

_GODOT = os.environ.get(
    "GAMEFACTORY_TEST_GODOT",
    r"C:\Users\lenovo\devel\godot\Godot_v4.7.2-stable_win64.exe\Godot_v4.7.2-stable_win64_console.exe",
)
_COMPARE_SCRIPTS = (
    "animation_review_compare_controller.gd",
    "animation_review_compare_side.gd",
    "animation_review_compare_inspect.gd",
    "animation_review_compare_probe_player.gd",
    "animation_review_compare_runtime_probe.gd",
)
_SESSION_COMPARE_SCRIPTS = (
    "animation_review_compare_session_controller.gd",
    "animation_review_compare_session_inspect.gd",
)
_PROBE_SCRIPTS = (
    "animation_review_compare_controller.gd",
    "animation_review_compare_side.gd",
    "animation_review_compare_probe_player.gd",
    "animation_review_compare_runtime_probe.gd",
)
_V08_PACKAGE = resources.files("gamefactory.resources.v08_candidate")


def _sources(layout: dict[str, Any]) -> tuple[CandidateAnimationReviewSetSource, ...]:
    packages = _clip_packages(layout["workspace"].parent.parent)
    return tuple(
        CandidateAnimationReviewSetSource(
            Path(entry["animation_dir"]),
            Path(entry["clip_path"]),
        )
        for entry in packages
    )


def _handlers(layout: dict[str, Any]):
    return build_readonly_candidate_handlers(layout["workspace"])


def _fresh_overlay_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "owned-root"
    parent.mkdir()
    return parent / "overlay"


def _noop_trusted_executables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        compare_viewer,
        "_assert_snapshot_trusted_godot_executables",
        lambda _snapshot: None,
    )


def _always_current(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compare_viewer, "animation_review_set_current", lambda *a, **k: True)


def _noop_validate_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compare_viewer, "validate_review_set_sources", lambda _sources: None)


def _minimal_snapshot(*, root_payload: bytes = b"project") -> _ReviewSetDirectorySnapshot:
    root_files = {
        "project.godot": root_payload,
        "animation_review_set_manifest.json": b"{}",
    }
    clip_files = {
        "clips/000/animation_clip.json": b"{}",
        "clips/001/animation_clip.json": b"{}",
    }
    return _ReviewSetDirectorySnapshot(
        manifest_doc={
            "schema_version": ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
            "ordered_clips": [
                {"clip_id": "IdleA", "raw_clip_sha256": "ab" * 32},
                {"clip_id": "WalkB", "raw_clip_sha256": "cd" * 32},
            ],
        },
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


def _prepare_args(layout: dict[str, Any], overlay: Path) -> dict[str, Any]:
    return {
        "handlers": _handlers(layout),
        "workflow_id": layout["workflow_id"],
        "preview_dir": layout["preview"],
        "clip_packages": _sources(layout),
        "review_dir": layout["review"],
        "overlay_dir": overlay,
        "left_clip_id": "IdleA",
        "right_clip_id": "WalkB",
    }


def _bridge_context(layout: dict[str, Any]) -> BridgeContext:
    document = _bridge_context_document(layout)
    exchange = layout["workspace"].parent / "viewer-exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    document["exchange_dir"] = str(exchange.resolve())
    from gamefactory.cli.animation_review_session_bridge import _parse_context_document

    return _parse_context_document(document)


def _session_prepare_args(
    layout: dict[str, Any], overlay: Path, *, python_executable: Path | None = None
) -> dict[str, Any]:
    args = _prepare_args(layout, overlay)
    args["session_context"] = _bridge_context(layout)
    if python_executable is not None:
        args["python_executable"] = python_executable
    return args


def test_prepare_copies_snapshot_and_installs_compare_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    review = layout["review"]
    before = _tree_digest(review)
    snapshot = _minimal_snapshot(root_payload=b"canonical-bytes")
    _noop_trusted_executables(monkeypatch)
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    monkeypatch.setattr(compare_viewer, "_capture_review_set_structure", lambda _p: snapshot)

    overlay = _fresh_overlay_parent(tmp_path)
    prepared = compare_viewer.prepare_animation_review_compare_viewer(
        **_prepare_args(layout, overlay)
    )

    assert _tree_digest(review) == before
    assert prepared.left_clip_id == "IdleA"
    assert prepared.right_clip_id == "WalkB"
    for name in compare_viewer._PACKAGED_COMPARE_FILES:
        assert (prepared.overlay_dir / name).is_file()
        assert (prepared.overlay_dir / name).read_bytes() == _V08_PACKAGE.joinpath(
            name
        ).read_bytes()
    for name in compare_viewer._PACKAGED_COMPARE_SESSION_FILES:
        assert not (prepared.overlay_dir / name).exists()
    assert prepared.session_context_file is None
    assert prepared.session_exchange_dir is None
    context = json.loads(prepared.context_file.read_text(encoding="utf-8"))
    assert context["schema_version"] == compare_viewer._COMPARE_SCHEMA_VERSION
    assert context["left_clip_id"] == "IdleA"
    assert context["right_clip_id"] == "WalkB"


def test_prepare_rejects_same_clip_ids_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    overlay = _fresh_overlay_parent(tmp_path)
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    args = _prepare_args(layout, overlay)
    args["right_clip_id"] = "IdleA"
    with pytest.raises(ValidationError, match="differ"):
        compare_viewer.prepare_animation_review_compare_viewer(**args)
    assert not overlay.exists()


def test_prepare_rejects_unknown_clip_id_before_writes(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    overlay = _fresh_overlay_parent(tmp_path)
    args = _prepare_args(layout, overlay)
    args["left_clip_id"] = "MissingClip"
    with pytest.raises(ValidationError, match="unknown"):
        compare_viewer.prepare_animation_review_compare_viewer(**args)
    assert not overlay.exists()


def test_prepare_rejects_nonstring_clip_ids(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    overlay = _fresh_overlay_parent(tmp_path)
    args = _prepare_args(layout, overlay)
    args["left_clip_id"] = True  # type: ignore[assignment]
    with pytest.raises(ValidationError, match="strings"):
        compare_viewer.prepare_animation_review_compare_viewer(**args)
    assert not overlay.exists()


def test_prepare_rejects_stale_review_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    overlay = _fresh_overlay_parent(tmp_path)
    monkeypatch.setattr(compare_viewer, "animation_review_set_current", lambda *a, **k: False)
    _noop_validate_sources(monkeypatch)
    with pytest.raises(ValidationError, match="not current"):
        compare_viewer.prepare_animation_review_compare_viewer(**_prepare_args(layout, overlay))
    assert not overlay.exists()


def test_prepare_rejects_existing_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    overlay = _fresh_overlay_parent(tmp_path)
    overlay.mkdir()
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    with pytest.raises(ValidationError, match="must not already exist"):
        compare_viewer.prepare_animation_review_compare_viewer(**_prepare_args(layout, overlay))


def test_prepare_rejects_overlay_inside_review_dir(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    overlay = layout["review"] / "nested-overlay"
    with pytest.raises(ValidationError, match="outside"):
        compare_viewer.prepare_animation_review_compare_viewer(**_prepare_args(layout, overlay))


def test_prepare_rejects_stale_review_set_after_post_install_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    review = layout["review"]
    before = _tree_digest(review)
    calls = {"n": 0}

    def _current(*_args: object, **_kwargs: object) -> bool:
        calls["n"] += 1
        return calls["n"] == 1

    _noop_trusted_executables(monkeypatch)
    monkeypatch.setattr(compare_viewer, "animation_review_set_current", _current)
    _noop_validate_sources(monkeypatch)
    monkeypatch.setattr(
        compare_viewer,
        "_capture_review_set_structure",
        lambda _p: _minimal_snapshot(),
    )

    overlay = _fresh_overlay_parent(tmp_path)
    with pytest.raises(ValidationError, match="not current"):
        compare_viewer.prepare_animation_review_compare_viewer(**_prepare_args(layout, overlay))
    assert not overlay.exists()
    assert _tree_digest(review) == before


def test_prepare_cleans_owned_overlay_on_materialization_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    stable = _minimal_snapshot(root_payload=b"stable")
    drifted = _minimal_snapshot(root_payload=b"drifted")
    calls = {"n": 0}

    def _capture(_path: Path) -> _ReviewSetDirectorySnapshot:
        calls["n"] += 1
        if calls["n"] <= 2:
            return stable
        return drifted

    _noop_trusted_executables(monkeypatch)
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    monkeypatch.setattr(compare_viewer, "_capture_review_set_structure", _capture)

    overlay = _fresh_overlay_parent(tmp_path)
    with pytest.raises(ValidationError, match="drifted"):
        compare_viewer.prepare_animation_review_compare_viewer(**_prepare_args(layout, overlay))
    assert not overlay.exists()


def test_godot_argv_uses_compare_context_equals_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    _noop_trusted_executables(monkeypatch)
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    monkeypatch.setattr(
        compare_viewer,
        "_capture_review_set_structure",
        lambda _p: _minimal_snapshot(),
    )
    overlay = _fresh_overlay_parent(tmp_path)
    prepared = compare_viewer.prepare_animation_review_compare_viewer(
        **_prepare_args(layout, overlay)
    )
    fake_godot = tmp_path / "godot.exe"
    fake_godot.write_bytes(b"")
    argv = prepared.godot_argv(godot_executable=fake_godot)
    assert argv[1:5] == (
        "--path",
        str(prepared.overlay_dir.resolve()),
        "--scene",
        compare_viewer._SCENE_RESOURCE,
    )
    assert argv[5] == "--"
    assert argv[6] == f"{compare_viewer._ARG_COMPARE_CONTEXT}{prepared.context_file.resolve()}"


def test_run_compare_viewer_cleans_owned_temp_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    _noop_trusted_executables(monkeypatch)
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    monkeypatch.setattr(
        compare_viewer,
        "_capture_review_set_structure",
        lambda _p: _minimal_snapshot(),
    )
    seen: list[Path] = []

    def _fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(Path(argv[2]))
        return subprocess.CompletedProcess(argv, 0)

    godot = tmp_path / "godot.exe"
    godot.write_bytes(b"")

    with patch.object(compare_viewer.subprocess, "run", side_effect=_fake_run):
        code = compare_viewer.run_animation_review_compare_viewer(
            handlers=_handlers(layout),
            workflow_id=layout["workflow_id"],
            preview_dir=layout["preview"],
            clip_packages=_sources(layout),
            review_dir=layout["review"],
            left_clip_id="IdleA",
            right_clip_id="WalkB",
            godot_executable=godot,
        )

    assert code == 0
    assert len(seen) == 1
    assert not seen[0].exists()
    assert not seen[0].parent.exists()


def _stage_compare_runtime_probe(tmp_path: Path) -> tuple[Path, Path]:
    staged = tmp_path / "compare-runtime-probe"
    staged.mkdir()
    (staged / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="gf-compare-runtime-probe"\n',
        encoding="utf-8",
        newline="\n",
    )
    for name in _PROBE_SCRIPTS:
        (staged / name).write_bytes(_V08_PACKAGE.joinpath(name).read_bytes())
    context_path = staged / "compare_context.json"
    context_path.write_text(
        json.dumps(
            {
                "schema_version": compare_viewer._COMPARE_SCHEMA_VERSION,
                "left_clip_id": "probe_clip_a",
                "right_clip_id": "probe_clip_b",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return staged, context_path


def _run_compare_runtime_probe(
    staged: Path,
    context_path: Path,
    *,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(_GODOT),
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
            "--path",
            str(staged),
            "--script",
            "res://animation_review_compare_runtime_probe.gd",
            "--",
            f"{compare_viewer._ARG_COMPARE_CONTEXT}{context_path}",
        ],
        cwd=staged,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.skipif(not Path(_GODOT).is_file(), reason="Godot executable not available")
def test_compare_runtime_probe_sparse_clips(tmp_path: Path) -> None:
    staged, context_path = _stage_compare_runtime_probe(tmp_path)
    completed = _run_compare_runtime_probe(staged, context_path)
    out = completed.stdout + completed.stderr
    assert completed.returncode == 0, out[-3000:]
    assert "PASS: animation_review_compare_runtime_probe" in out
    assert "SCRIPT ERROR" not in out


@pytest.mark.skipif(not Path(_GODOT).is_file(), reason="Godot executable not available")
def test_compare_runtime_probe_rejects_oversize_context(tmp_path: Path) -> None:
    staged, _ = _stage_compare_runtime_probe(tmp_path)
    oversize = staged / "oversize_compare_context.json"
    oversize.write_bytes(b" " * 8193)
    completed = _run_compare_runtime_probe(staged, oversize)
    out = completed.stdout + completed.stderr
    assert completed.returncode != 0, out[-3000:]
    assert "compare context exceeds allowed size" in out


@pytest.mark.skipif(not Path(_GODOT).is_file(), reason="Godot executable not available")
def test_compare_runtime_probe_rejects_malformed_context(tmp_path: Path) -> None:
    staged, _ = _stage_compare_runtime_probe(tmp_path)
    malformed = staged / "malformed_compare_context.json"
    malformed.write_text("[1,2,3]", encoding="utf-8")
    completed = _run_compare_runtime_probe(staged, malformed)
    out = completed.stdout + completed.stderr
    assert completed.returncode != 0, out[-3000:]
    assert "compare context is not an object" in out


@pytest.mark.skipif(not Path(_GODOT).is_file(), reason="Godot executable not available")
def test_compare_packaged_scripts_check_only(tmp_path: Path) -> None:
    staged = tmp_path / "compare-check"
    staged.mkdir()
    (staged / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="gf-compare-check"\n',
        encoding="utf-8",
        newline="\n",
    )
    for name in _COMPARE_SCRIPTS:
        (staged / name).write_bytes(_V08_PACKAGE.joinpath(name).read_bytes())

    for script in _COMPARE_SCRIPTS:
        completed = subprocess.run(
            [
                str(_GODOT),
                "--headless",
                "--path",
                str(staged),
                "--script",
                f"res://{script}",
                "--check-only",
            ],
            cwd=staged,
            capture_output=True,
            text=True,
            timeout=180,
        )
        out = completed.stdout + completed.stderr
        assert completed.returncode == 0, out[-2000:]
        assert "SCRIPT ERROR" not in out
        assert "ParseError" not in out


def test_prepare_sessionless_rejects_python_without_session_context(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    overlay = _fresh_overlay_parent(tmp_path)
    args = _prepare_args(layout, overlay)
    args["python_executable"] = Path(sys.executable)
    with pytest.raises(ValidationError, match="requires session_context"):
        compare_viewer.prepare_animation_review_compare_viewer(**args)
    assert not overlay.exists()


def test_prepare_with_session_installs_session_overlay_and_owned_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    original_exchange = ctx.exchange_dir.resolve()
    _noop_trusted_executables(monkeypatch)
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    monkeypatch.setattr(
        compare_viewer,
        "_capture_review_set_structure",
        lambda _p: _minimal_snapshot(),
    )
    overlay = _fresh_overlay_parent(tmp_path)
    trusted_python = tmp_path / "python.exe"
    trusted_python.write_bytes(b"")
    monkeypatch.setattr(
        compare_viewer,
        "_assert_trusted_executable",
        lambda path, label="": path.resolve(),
    )
    prepared = compare_viewer.prepare_animation_review_compare_viewer(
        **_session_prepare_args(layout, overlay, python_executable=trusted_python)
    )
    for name in compare_viewer._PACKAGED_COMPARE_SESSION_FILES:
        assert (prepared.overlay_dir / name).is_file()
        assert (prepared.overlay_dir / name).read_bytes() == _V08_PACKAGE.joinpath(
            name
        ).read_bytes()
    assert prepared.session_exchange_dir is not None
    assert prepared.session_exchange_dir == prepared.overlay_dir / "exchange"
    assert prepared.session_context_file is not None
    session_doc = json.loads(prepared.session_context_file.read_text(encoding="utf-8"))
    assert session_doc["exchange_dir"] == str(prepared.session_exchange_dir.resolve())
    assert session_doc["exchange_dir"] != str(original_exchange)
    assert session_doc["schema_version"] == CONTEXT_SCHEMA_VERSION
    assert session_doc == _context_document(ctx, exchange_dir=prepared.session_exchange_dir)
    compare_doc = json.loads(prepared.context_file.read_text(encoding="utf-8"))
    assert compare_doc["schema_version"] == compare_viewer._COMPARE_SCHEMA_VERSION


def test_godot_argv_session_uses_session_scene_and_bridge_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    _noop_trusted_executables(monkeypatch)
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    monkeypatch.setattr(
        compare_viewer,
        "_capture_review_set_structure",
        lambda _p: _minimal_snapshot(),
    )
    monkeypatch.setattr(
        compare_viewer,
        "_assert_trusted_executable",
        lambda path, label="": path.resolve(),
    )
    overlay = _fresh_overlay_parent(tmp_path)
    trusted_python = tmp_path / "python.exe"
    trusted_python.write_bytes(b"")
    prepared = compare_viewer.prepare_animation_review_compare_viewer(
        **_session_prepare_args(layout, overlay, python_executable=trusted_python)
    )
    fake_godot = tmp_path / "godot.exe"
    fake_godot.write_bytes(b"")
    argv = prepared.godot_argv(godot_executable=fake_godot)
    assert argv[1:5] == (
        "--path",
        str(prepared.overlay_dir.resolve()),
        "--scene",
        compare_viewer._SCENE_SESSION_RESOURCE,
    )
    assert argv[5] == "--"
    assert argv[6] == f"{compare_viewer._ARG_COMPARE_CONTEXT}{prepared.context_file.resolve()}"
    assert argv[7] == f"{compare_viewer._ARG_SESSION_PYTHON}{trusted_python.resolve()}"
    assert (
        argv[8] == f"{compare_viewer._ARG_SESSION_CONTEXT}{prepared.session_context_file.resolve()}"
    )
    assert (
        argv[9]
        == f"{compare_viewer._ARG_SESSION_EXCHANGE}{prepared.session_exchange_dir.resolve()}"
    )


@pytest.mark.parametrize(
    ("field", "mutator", "match"),
    [
        ("workflow_id", lambda layout, ctx: "foreign-workflow", "workflow_id mismatch"),
        (
            "project_root",
            lambda layout, ctx: layout["workspace"].parent / "other-root",
            "project_root mismatch",
        ),
        (
            "preview_dir",
            lambda layout, ctx: layout["preview"].parent / "other-preview",
            "preview_dir mismatch",
        ),
        (
            "review_dir",
            lambda layout, ctx: layout["review"].parent / "other-review",
            "review_dir mismatch",
        ),
    ],
)
def test_prepare_session_rejects_mismatched_bridge_context_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    mutator: Any,
    match: str,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    overlay = _fresh_overlay_parent(tmp_path)
    mutated = {
        "project_root": ctx.project_root,
        "workflow_id": ctx.workflow_id,
        "preview_dir": ctx.preview_dir,
        "review_dir": ctx.review_dir,
        "clip_packages": ctx.clip_packages,
        "session_path": ctx.session_path,
        "exchange_dir": ctx.exchange_dir,
    }
    mutated[field] = mutator(layout, ctx)
    bad_ctx = BridgeContext(**mutated)
    args = _prepare_args(layout, overlay)
    args["session_context"] = bad_ctx
    with pytest.raises(ValidationError, match=match):
        compare_viewer.prepare_animation_review_compare_viewer(**args)
    assert not overlay.exists()


def test_prepare_session_rejects_mismatched_clip_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    packages = _clip_packages(layout["workspace"].parent.parent)
    pkg_a = Path(packages[0]["animation_dir"])
    clip_a = Path(packages[0]["clip_path"])
    swapped = BridgeContext(
        project_root=ctx.project_root,
        workflow_id=ctx.workflow_id,
        preview_dir=ctx.preview_dir,
        review_dir=ctx.review_dir,
        clip_packages=(CandidateAnimationReviewSetSource(pkg_a, clip_a),),
        session_path=ctx.session_path,
        exchange_dir=ctx.exchange_dir,
    )
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    overlay = _fresh_overlay_parent(tmp_path)
    args = _prepare_args(layout, overlay)
    args["session_context"] = swapped
    with pytest.raises(ValidationError, match="clip_packages mismatch"):
        compare_viewer.prepare_animation_review_compare_viewer(**args)
    assert not overlay.exists()


def test_prepare_session_rejects_unsafe_overlay_inside_custom_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    overlay = layout["session_sidecar"].parent / "overlay"
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    args = _prepare_args(layout, overlay)
    args["session_context"] = ctx
    with pytest.raises(ValidationError, match="outside"):
        compare_viewer.prepare_animation_review_compare_viewer(**args)
    assert not overlay.exists()


def test_prepare_session_rejects_unsafe_session_context_paths(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    ctx = _bridge_context(layout)
    unsafe_ctx = BridgeContext(
        project_root=Path("../unsafe-project"),
        workflow_id=ctx.workflow_id,
        preview_dir=ctx.preview_dir,
        review_dir=ctx.review_dir,
        clip_packages=ctx.clip_packages,
        session_path=ctx.session_path,
        exchange_dir=ctx.exchange_dir,
    )
    overlay = _fresh_overlay_parent(tmp_path)
    args = _prepare_args(layout, overlay)
    args["session_context"] = unsafe_ctx
    with pytest.raises(ValidationError):
        compare_viewer.prepare_animation_review_compare_viewer(**args)
    assert not overlay.exists()


def test_prepare_session_cleans_owned_overlay_on_install_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    _noop_trusted_executables(monkeypatch)
    _always_current(monkeypatch)
    _noop_validate_sources(monkeypatch)
    monkeypatch.setattr(
        compare_viewer,
        "_capture_review_set_structure",
        lambda _p: _minimal_snapshot(),
    )
    monkeypatch.setattr(
        compare_viewer,
        "_install_packaged_compare_session_scenes",
        lambda _overlay: (_ for _ in ()).throw(ValidationError("session install failed")),
    )
    overlay = _fresh_overlay_parent(tmp_path)
    with pytest.raises(ValidationError, match="session install failed"):
        compare_viewer.prepare_animation_review_compare_viewer(
            **_session_prepare_args(layout, overlay)
        )
    assert not overlay.exists()


@pytest.mark.skipif(not Path(_GODOT).is_file(), reason="Godot executable not available")
def test_compare_session_bridge_validation_probe(tmp_path: Path) -> None:
    staged = tmp_path / "compare-session-probe"
    staged.mkdir()
    (staged / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="gf-compare-session-probe"\n',
        encoding="utf-8",
        newline="\n",
    )
    for name in _SESSION_COMPARE_SCRIPTS:
        (staged / name).write_bytes(_V08_PACKAGE.joinpath(name).read_bytes())
    completed = subprocess.run(
        [
            str(_GODOT),
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
            "--path",
            str(staged),
            "--script",
            "res://animation_review_compare_session_inspect.gd",
        ],
        cwd=staged,
        capture_output=True,
        text=True,
        timeout=180,
    )
    out = completed.stdout + completed.stderr
    assert completed.returncode == 0, out[-3000:]
    assert "PASS: animation_review_compare_session_bridge_validation" in out
    assert "SCRIPT ERROR" not in out


def _stage_compare_session_panel_probe(tmp_path: Path) -> tuple[Path, Path]:
    staged = tmp_path / "compare-session-panel-probe"
    staged.mkdir()
    (staged / "project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="gf-compare-session-panel-probe"\n',
        encoding="utf-8",
        newline="\n",
    )
    probe_deps = (
        "animation_review_compare_session_panel_probe.gd",
        "animation_review_compare_session_controller.gd",
        "animation_review_compare_controller.gd",
        "animation_review_compare_side.gd",
        "animation_review_compare_probe_player.gd",
    )
    for name in probe_deps:
        (staged / name).write_bytes(_V08_PACKAGE.joinpath(name).read_bytes())
    context_path = staged / "compare_context.json"
    context_path.write_text(
        json.dumps(
            {
                "schema_version": compare_viewer._COMPARE_SCHEMA_VERSION,
                "left_clip_id": "probe_clip_a",
                "right_clip_id": "probe_clip_b",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return staged, context_path


@pytest.mark.skipif(not Path(_GODOT).is_file(), reason="Godot executable not available")
def test_compare_session_panel_regression_probe(tmp_path: Path) -> None:
    staged, context_path = _stage_compare_session_panel_probe(tmp_path)
    completed = subprocess.run(
        [
            str(_GODOT),
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
            "--path",
            str(staged),
            "--script",
            "res://animation_review_compare_session_panel_probe.gd",
            "--",
            f"{compare_viewer._ARG_COMPARE_CONTEXT}{context_path}",
        ],
        cwd=staged,
        capture_output=True,
        text=True,
        timeout=45,
    )
    out = completed.stdout + completed.stderr
    assert completed.returncode == 0, out[-3000:]
    assert "PASS: animation_review_compare_session_panel_regression" in out
    assert "SCRIPT ERROR" not in out
