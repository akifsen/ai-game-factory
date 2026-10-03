"""V0.8-9 disposable Godot overlay launcher for animation review sessions (V0.8-9c)."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.v08_candidate_evidence import (
    candidate_evidence_lexical_unsafe,
    parse_bounded_publication_json,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.cli.animation_review_session_bridge import (
    CONTEXT_SCHEMA_VERSION,
    BridgeContext,
    _assert_exchange_paths_safe,
    _assert_path_outside_roots,
    _assert_regular_single_link_file,
    _canonical_review_roots,
    _load_json_object,
    _mutation_forbidden_roots,
    _parse_context_document,
    _read_bounded_regular_file,
    _reject_unsafe_lexical_path,
)
from gamefactory.cli.exit_codes import EXIT_CONFIG_ERROR, EXIT_SUCCESS
from gamefactory.core.domain.errors import ValidationError
from gamefactory.workflows.v08_candidate_animation_clip_preview import (
    _decode_verified_glb_snapshot,
    _verified_weighted_bone_names,
)
from gamefactory.workflows.v08_candidate_animation_review import _review_controller_bytes
from gamefactory.workflows.v08_candidate_animation_review_set import (
    _MAX_REVIEW_SET_MANIFEST_BYTES,
    ANIMATION_REVIEW_SET_MANIFEST_SCHEMA,
    MAX_REVIEW_SET_SOURCES,
    MIN_REVIEW_SET_SOURCES,
    _build_animation_review_set_preview_tscn,
    _build_animation_review_set_project_godot,
    _inspect_review_set_directory,
    _player_script_bytes,
    _ReviewSetDirectorySnapshot,
    trusted_animation_review_set_ui_digests,
)
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from gamefactory.workflows.v08_candidate_preview import (
    PREVIEW_COLLIDER_SCHEMA,
    _build_character_tscn,
)

_SCENE_RESOURCE = "res://animation_review_session.tscn"
_PACKAGED_SESSION_FILES = (
    "animation_review_session.tscn",
    "animation_review_session_controller.gd",
)
_V08_RESOURCE_PACKAGE = "gamefactory.resources.v08_candidate"
_EXCHANGE_DIR_NAME = "exchange"
_CONTEXT_LEAF = "context.json"
_MAX_CONTEXT_BYTES = 64 * 1024
_COLLIDER_JSON_KEYS = frozenset(
    {
        "schema_version",
        "policy",
        "shape_class",
        "radius_m",
        "height_m",
        "center_m",
        "production_eligible",
        "promotion_eligible",
    }
)
_COLLIDER_MIN_RADIUS_M = 0.10
_MAX_PUBLICATION_CONTROL_JSON_CONTAINER_DEPTH = 64


def _reject_excessive_publication_control_json_container_depth(
    raw: bytes,
    *,
    malformed_message: str,
) -> None:
    """Linear preflight: reject deeply nested containers before json.loads."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return
    limit = _MAX_PUBLICATION_CONTROL_JSON_CONTAINER_DEPTH
    depth = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == '"':
            index += 1
            while index < length:
                inner = text[index]
                if inner == '"':
                    index += 1
                    break
                if inner == "\\":
                    index += 1
                    if index < length and text[index] == "u":
                        index += 5
                    elif index < length:
                        index += 1
                    continue
                index += 1
            continue
        if char in "{[":
            depth += 1
            if depth > limit:
                raise ValidationError(malformed_message)
        elif char in "}]":
            depth -= 1
        index += 1


@dataclass(frozen=True)
class PreparedAnimationReviewSessionViewer:
    overlay_dir: Path
    context_file: Path
    exchange_dir: Path
    python_executable: Path

    def godot_argv(self, *, godot_executable: Path) -> tuple[str, ...]:
        godot = _assert_trusted_executable(godot_executable, label="godot executable")
        python = _assert_trusted_executable(self.python_executable, label="python executable")
        context = _reject_unsafe_lexical_path(self.context_file, label="session context file")
        exchange = _reject_unsafe_lexical_path(self.exchange_dir, label="session exchange dir")
        overlay = _reject_unsafe_lexical_path(self.overlay_dir, label="overlay directory")
        return (
            str(godot),
            "--path",
            str(overlay),
            "--scene",
            _SCENE_RESOURCE,
            "--",
            f"--session-python-executable={python}",
            f"--session-context-file={context}",
            f"--session-exchange-dir={exchange}",
        )


_MAX_TRUSTED_EXECUTABLE_SYMLINK_HOPS = 40


def _trusted_executable_directory_ancestors_cross_link(path: Path) -> bool:
    for current in path.parents:
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if stat.S_ISLNK(st.st_mode):
            return True
        reparse_tag = int(getattr(st, "st_reparse_tag", 0) or 0)
        if reparse_tag != 0:
            return True
        if os.name == "nt":
            attrs = getattr(st, "st_file_attributes", None)
            if attrs is None:
                return True
            if int(attrs) & 0x400 and reparse_tag == 0:
                return True
    return False


def _trusted_executable_composed_symlink_target(current: Path, link_text: str) -> Path:
    target = Path(link_text)
    if target.is_absolute():
        return target
    return current.parent / target


def _trusted_executable_symlink_hop_target(current: Path, link_text: str) -> Path:
    raw = _trusted_executable_composed_symlink_target(current, link_text)
    return Path(os.path.abspath(str(raw)))


def _resolve_trusted_executable_leaf_target(lexical: Path, *, label: str) -> Path:
    current = lexical
    seen: set[Path] = set()
    for _ in range(_MAX_TRUSTED_EXECUTABLE_SYMLINK_HOPS):
        if current in seen:
            raise ValidationError(f"{label} symlink loop detected")
        seen.add(current)
        if _trusted_executable_directory_ancestors_cross_link(current):
            raise ValidationError(f"{label} crosses a symlink or junction")
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            raise ValidationError(f"{label} does not exist") from None
        except OSError as exc:
            raise ValidationError(f"{label} cannot be inspected: {exc}") from exc
        if stat.S_ISLNK(st.st_mode):
            try:
                link_text = os.readlink(current)
            except OSError as exc:
                raise ValidationError(f"{label} cannot be inspected: {exc}") from exc
            if not link_text:
                raise ValidationError(f"{label} does not exist")
            raw_next = _trusted_executable_composed_symlink_target(current, link_text)
            if _trusted_executable_directory_ancestors_cross_link(raw_next):
                raise ValidationError(f"{label} crosses a symlink or junction")
            current = _trusted_executable_symlink_hop_target(current, link_text)
            continue
        return current
    raise ValidationError(f"{label} symlink chain is too long")


def _assert_trusted_executable(
    path: Path,
    *,
    label: str,
    allow_leaf_symlink_alias: bool = True,
) -> Path:
    if sys.platform == "win32" or not allow_leaf_symlink_alias:
        resolved = _reject_unsafe_lexical_path(path, label=label)
        _assert_regular_single_link_file(resolved, label=label)
        return resolved
    if not path.is_absolute():
        raise ValidationError(f"{label} must be an absolute path")
    if _trusted_executable_directory_ancestors_cross_link(path):
        raise ValidationError(f"{label} crosses a symlink or junction")
    lexical = Path(os.path.abspath(str(path)))
    target = _resolve_trusted_executable_leaf_target(lexical, label=label)
    _assert_regular_single_link_file(target, label=label)
    return lexical


def _assert_fresh_overlay_path(overlay_dir: Path) -> Path:
    if not overlay_dir.is_absolute():
        raise ValidationError("overlay_dir must be an absolute path")
    if os.path.lexists(str(overlay_dir)):
        raise ValidationError("overlay_dir must not already exist")
    return _reject_unsafe_lexical_path(overlay_dir, label="overlay_dir")


def _assert_overlay_parent_ready(overlay_dir: Path) -> None:
    parent = overlay_dir.parent
    parent_resolved = _reject_unsafe_lexical_path(parent, label="overlay_dir parent")
    if not parent_resolved.is_dir():
        raise ValidationError("overlay_dir parent is not a directory")


def _bounded_clip_count_from_manifest(review_dir: Path) -> int:
    manifest_path = review_dir / "animation_review_set_manifest.json"
    raw = _read_bounded_regular_file(
        manifest_path,
        label="animation_review_set_manifest.json",
        max_bytes=_MAX_REVIEW_SET_MANIFEST_BYTES,
    )
    _reject_excessive_publication_control_json_container_depth(
        raw,
        malformed_message=("animation review set manifest publication control JSON is malformed"),
    )
    document = parse_bounded_publication_json(raw)
    if document.get("schema_version") != ANIMATION_REVIEW_SET_MANIFEST_SCHEMA:
        raise ValidationError("animation review set manifest schema_version mismatch")
    ordered_clips = document.get("ordered_clips")
    if not isinstance(ordered_clips, list):
        raise ValidationError("animation review set ordered_clips must be a list")
    count = len(ordered_clips)
    if count < MIN_REVIEW_SET_SOURCES or count > MAX_REVIEW_SET_SOURCES:
        raise ValidationError("animation review set ordered_clips length is out of range")
    return count


def _capture_review_set_structure(review_dir: Path) -> _ReviewSetDirectorySnapshot:
    resolved = _reject_unsafe_lexical_path(review_dir, label="review_dir")
    if not resolved.is_dir():
        raise ValidationError("review_dir is not a directory")
    try:
        clip_count = _bounded_clip_count_from_manifest(resolved)
        return _inspect_review_set_directory(resolved, expected_clip_count=clip_count)
    except CandidateCurrentnessError as exc:
        raise ValidationError(str(exc)) from exc
    except (RecursionError, ValueError) as exc:
        raise ValidationError(
            "animation review set manifest publication control JSON is malformed"
        ) from exc


def _snapshots_match(
    left: _ReviewSetDirectorySnapshot,
    right: _ReviewSetDirectorySnapshot,
) -> bool:
    return left.root_files == right.root_files and left.clip_files == right.clip_files


def _lf_normalize_bytes(payload: bytes) -> bytes:
    return payload.replace(b"\r\n", b"\n")


def _digest_lf_normalized(payload: bytes) -> str:
    return hashlib.sha256(_lf_normalize_bytes(payload)).hexdigest()


def _validate_bridge_context_paths(ctx: BridgeContext) -> None:
    _assert_exchange_paths_safe(ctx)
    _reject_unsafe_lexical_path(ctx.project_root, label="project_root")
    for root in _canonical_review_roots(ctx):
        _reject_unsafe_lexical_path(root, label="canonical review root")


def _collider_json_finite_scalar(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"collider.json {field} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValidationError(f"collider.json {field} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValidationError(f"collider.json {field} must be a finite number")
    return result


def _parse_collider_publication_json(collider_bytes: bytes) -> dict[str, Any]:
    _reject_excessive_publication_control_json_container_depth(
        collider_bytes,
        malformed_message="collider.json publication control JSON is malformed",
    )
    try:
        return parse_bounded_publication_json(collider_bytes)
    except ValidationError:
        raise
    except (RecursionError, ValueError) as exc:
        raise ValidationError("collider.json publication control JSON is malformed") from exc


def _validated_collider_fields(
    collider_bytes: bytes,
) -> tuple[float, float, tuple[float, float, float]]:
    document = _parse_collider_publication_json(collider_bytes)
    if set(document.keys()) != _COLLIDER_JSON_KEYS:
        raise ValidationError("collider.json fields do not match the preview collider contract")
    if document.get("schema_version") != PREVIEW_COLLIDER_SCHEMA:
        raise ValidationError("collider.json schema_version mismatch")
    if document.get("policy") != "capsule":
        raise ValidationError("collider.json policy mismatch")
    if document.get("shape_class") != "CapsuleShape3D":
        raise ValidationError("collider.json shape_class mismatch")
    if document.get("production_eligible") is not False:
        raise ValidationError("collider.json production_eligible must be false")
    if document.get("promotion_eligible") is not False:
        raise ValidationError("collider.json promotion_eligible must be false")
    radius_m = _collider_json_finite_scalar(document.get("radius_m"), "radius_m")
    height_m = _collider_json_finite_scalar(document.get("height_m"), "height_m")
    if radius_m < _COLLIDER_MIN_RADIUS_M:
        raise ValidationError("collider.json radius_m is below the allowed minimum")
    if height_m <= 2 * radius_m:
        raise ValidationError("collider.json height_m must be greater than twice radius_m")
    center = document.get("center_m")
    if not isinstance(center, list) or len(center) != 3:
        raise ValidationError("collider.json center_m must be a three-element list")
    center_m = (
        _collider_json_finite_scalar(center[0], "center_m[0]"),
        _collider_json_finite_scalar(center[1], "center_m[1]"),
        _collider_json_finite_scalar(center[2], "center_m[2]"),
    )
    return radius_m, height_m, center_m


def _expected_character_tscn_from_collider(collider_bytes: bytes) -> bytes:
    radius_m, height_m, center_m = _validated_collider_fields(collider_bytes)
    return _build_character_tscn(
        radius_m=radius_m,
        height_m=height_m,
        center_m=center_m,
    ).encode("utf-8")


def _verified_weighted_bone_names_from_snapshot_glb(
    snapshot: _ReviewSetDirectorySnapshot,
) -> tuple[str, ...]:
    glb_bytes = snapshot.root_files.get("character.glb")
    if glb_bytes is None:
        raise ValidationError("review set character.glb is missing")
    glb_sha256 = hashlib.sha256(glb_bytes).hexdigest()
    decoded = _decode_verified_glb_snapshot(glb_bytes, glb_sha256)
    return _verified_weighted_bone_names(decoded)


def _expected_review_set_preview_tscn(snapshot: _ReviewSetDirectorySnapshot) -> bytes:
    if snapshot.root_files.get("animation_review_set_preview.tscn") is None:
        raise ValidationError("animation review set preview scene is missing")
    bone_names = _verified_weighted_bone_names_from_snapshot_glb(snapshot)
    ordered_clips = snapshot.manifest_doc.get("ordered_clips")
    if not isinstance(ordered_clips, list):
        raise ValidationError("animation review set ordered_clips must be a list")
    clip_paths: list[str] = []
    clip_sha256s: list[str] = []
    clip_ids: list[str] = []
    for index, entry in enumerate(ordered_clips):
        if not isinstance(entry, dict):
            raise ValidationError("animation review set ordered clip entry is invalid")
        clip_id = entry.get("clip_id")
        clip_sha = entry.get("raw_clip_sha256")
        if not isinstance(clip_id, str) or not clip_id:
            raise ValidationError("animation review set ordered clip entry is invalid")
        if not isinstance(clip_sha, str):
            raise ValidationError("animation review set ordered clip entry is invalid")
        rel_json = f"clips/{index:03d}/animation_clip.json"
        clip_paths.append(f"res://{rel_json}")
        clip_sha256s.append(clip_sha)
        clip_ids.append(clip_id)
    return _build_animation_review_set_preview_tscn(
        clip_paths,
        clip_sha256s,
        clip_ids,
        bone_names,
    )


def _assert_snapshot_trusted_godot_executables(snapshot: _ReviewSetDirectorySnapshot) -> None:
    trusted_playback = {
        "animation_clip_preview_player.gd": _player_script_bytes(),
        "animation_review_controller.gd": _review_controller_bytes(),
    }
    for name, trusted_bytes in trusted_playback.items():
        payload = snapshot.root_files.get(name)
        if payload is None:
            raise ValidationError(f"review set missing trusted Godot executable {name}")
        if _digest_lf_normalized(payload) != _digest_lf_normalized(trusted_bytes):
            raise ValidationError(f"review set Godot executable {name} is not a trusted template")

    for name, trusted_digest in trusted_animation_review_set_ui_digests().items():
        payload = snapshot.root_files.get(name)
        if payload is None:
            raise ValidationError(f"review set missing trusted Godot executable {name}")
        if _digest_lf_normalized(payload) != trusted_digest:
            raise ValidationError(f"review set Godot executable {name} is not a trusted template")

    project = snapshot.root_files.get("project.godot")
    if project is None or project != _build_animation_review_set_project_godot():
        raise ValidationError("review set project.godot is not a trusted template")

    preview = snapshot.root_files.get("animation_review_set_preview.tscn")
    if preview is None:
        raise ValidationError("animation review set preview scene is missing")
    if preview != _expected_review_set_preview_tscn(snapshot):
        raise ValidationError("animation review set preview scene is not a trusted template")

    collider = snapshot.root_files.get("collider.json")
    character = snapshot.root_files.get("character.tscn")
    if collider is None or character is None:
        raise ValidationError("review set collider or character scene is missing")
    if character != _expected_character_tscn_from_collider(collider):
        raise ValidationError("review set character.tscn is not a trusted template")


def _write_exclusive_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValidationError(f"could not write {path.name}: {exc}") from exc


def _materialize_snapshot(snapshot: _ReviewSetDirectorySnapshot, overlay_dir: Path) -> None:
    for name, payload in sorted(snapshot.root_files.items()):
        _write_exclusive_bytes(overlay_dir / name, payload)
    for rel_path, payload in sorted(snapshot.clip_files.items()):
        _write_exclusive_bytes(overlay_dir / rel_path, payload)


def _install_packaged_session_scenes(overlay_dir: Path) -> None:
    package = resources.files(_V08_RESOURCE_PACKAGE)
    for leaf in _PACKAGED_SESSION_FILES:
        target = overlay_dir / leaf
        if target.exists():
            raise ValidationError(f"overlay session resource {leaf} already exists")
        payload = package.joinpath(leaf).read_bytes()
        _write_exclusive_bytes(target, payload)


def _context_document(ctx: BridgeContext, *, exchange_dir: Path) -> dict[str, Any]:
    clip_packages = [
        {
            "animation_dir": str(Path(entry.animation_dir).absolute()),
            "clip_path": str(Path(entry.clip_path).absolute()),
        }
        for entry in ctx.clip_packages
    ]
    session_path: str | None
    if ctx.session_path is None:
        session_path = None
    else:
        session_path = str(ctx.session_path.absolute())
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "project_root": str(ctx.project_root.absolute()),
        "workflow_id": ctx.workflow_id,
        "preview_dir": str(ctx.preview_dir.absolute()),
        "review_dir": str(ctx.review_dir.absolute()),
        "clip_packages": clip_packages,
        "session_path": session_path,
        "exchange_dir": str(exchange_dir.absolute()),
    }


def _write_context_file(ctx: BridgeContext, *, path: Path, exchange_dir: Path) -> None:
    document = _context_document(ctx, exchange_dir=exchange_dir)
    try:
        payload = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValidationError("viewer context is not JSON-encodable") from exc
    if len(payload) > _MAX_CONTEXT_BYTES:
        raise ValidationError("viewer context exceeds allowed size")
    _write_exclusive_bytes(path, payload)
    _parse_context_document(document)


def _same_owned_cleanup_root(candidate: Path, intended: Path) -> bool:
    if not candidate.is_absolute() or not intended.is_absolute():
        return False
    if sys.platform == "win32":
        if os.path.normcase(str(candidate)) != os.path.normcase(str(intended)):
            return False
    elif candidate != intended:
        return False
    if candidate_evidence_lexical_unsafe(candidate) or path_crosses_link(candidate):
        return False
    try:
        resolved = candidate.resolve(strict=False)
        intended_resolved = intended.resolve(strict=False)
    except OSError:
        return False
    if candidate_evidence_lexical_unsafe(resolved) or path_crosses_link(resolved):
        return False
    if sys.platform == "win32":
        return os.path.normcase(str(resolved)) == os.path.normcase(str(intended_resolved))
    return resolved == intended_resolved


def _cleanup_owned_tree(root: Path, *, intended_root: Path) -> None:
    if not _same_owned_cleanup_root(root, intended_root):
        return
    try:
        resolved = intended_root.resolve(strict=False)
    except OSError:
        return
    if not resolved.exists():
        return
    if not resolved.is_dir():
        try:
            resolved.unlink(missing_ok=True)
        except OSError:
            pass
        return
    shutil.rmtree(resolved)


def prepare_animation_review_session_viewer(
    context: BridgeContext,
    overlay_dir: Path,
    python_executable: Path | None = None,
) -> PreparedAnimationReviewSessionViewer:
    """Stage a disposable Godot overlay with a copied review-set package and bridge context."""
    python = _assert_trusted_executable(
        Path(python_executable or sys.executable),
        label="python executable",
    )
    _validate_bridge_context_paths(context)
    overlay = _assert_fresh_overlay_path(overlay_dir)
    forbidden = _mutation_forbidden_roots(context)
    _assert_path_outside_roots(overlay, forbidden, label="overlay_dir")
    _assert_overlay_parent_ready(overlay)

    original_snapshot = _capture_review_set_structure(context.review_dir)
    _assert_snapshot_trusted_godot_executables(original_snapshot)
    overlay_owned = False
    try:
        overlay.mkdir(parents=False)
        overlay_owned = True
        _materialize_snapshot(original_snapshot, overlay)
        original_after_copy = _capture_review_set_structure(context.review_dir)
        if not _snapshots_match(original_snapshot, original_after_copy):
            raise ValidationError("review set bytes changed during viewer preparation")
        overlay_snapshot = _capture_review_set_structure(overlay)
        if not _snapshots_match(original_snapshot, overlay_snapshot):
            raise ValidationError("overlay review set bytes drifted during materialization")
        _install_packaged_session_scenes(overlay)
        exchange_dir = overlay / _EXCHANGE_DIR_NAME
        exchange_dir.mkdir(parents=False)
        context_path = overlay / _CONTEXT_LEAF
        _write_context_file(context, path=context_path, exchange_dir=exchange_dir)
    except Exception:
        if overlay_owned:
            _cleanup_owned_tree(overlay, intended_root=overlay)
        raise

    _assert_path_outside_roots(exchange_dir, forbidden, label="exchange_dir")
    _assert_path_outside_roots(context_path, forbidden, label="context file")

    return PreparedAnimationReviewSessionViewer(
        overlay_dir=overlay,
        context_file=context_path,
        exchange_dir=exchange_dir,
        python_executable=python,
    )


def _load_bridge_context_from_file(context_file: Path) -> BridgeContext:
    path = _assert_trusted_executable(
        context_file,
        label="context file",
        allow_leaf_symlink_alias=False,
    )
    document = _load_json_object(path, label="context file")
    bridge = _parse_context_document(document)
    forbidden = _mutation_forbidden_roots(bridge)
    _assert_path_outside_roots(path, forbidden, label="context file")
    return bridge


def _subprocess_run_kwargs() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def run_animation_review_session_viewer(
    *,
    context_file: Path,
    godot_executable: Path,
    python_executable: Path | None = None,
) -> int:
    """Create a disposable overlay, launch Godot, and remove the overlay when Godot exits."""
    try:
        bridge = _load_bridge_context_from_file(context_file)
        godot = _assert_trusted_executable(godot_executable, label="godot executable")
    except (ValidationError, OSError):
        return EXIT_CONFIG_ERROR

    try:
        owned_parent = Path(tempfile.mkdtemp(prefix="gf-anim-review-viewer-"))
    except OSError:
        return EXIT_CONFIG_ERROR

    overlay_dir = owned_parent / "overlay"
    exit_code = EXIT_CONFIG_ERROR
    try:
        prepared = prepare_animation_review_session_viewer(
            bridge,
            overlay_dir,
            python_executable=python_executable,
        )
        argv = list(prepared.godot_argv(godot_executable=godot))
        completed = subprocess.run(
            argv,
            check=False,
            shell=False,
            **_subprocess_run_kwargs(),
        )
        exit_code = EXIT_SUCCESS if completed.returncode == 0 else completed.returncode
    except (ValidationError, OSError):
        exit_code = EXIT_CONFIG_ERROR
    finally:
        _cleanup_owned_tree(owned_parent, intended_root=owned_parent)

    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="V0.8-9 disposable animation review session Godot viewer",
    )
    parser.add_argument("--context-file", required=True)
    parser.add_argument("--godot-executable", required=True)
    ns = parser.parse_args(argv)
    context_path = Path(ns.context_file)
    godot_path = Path(ns.godot_executable)
    if not context_path.is_absolute() or not godot_path.is_absolute():
        return EXIT_CONFIG_ERROR
    return run_animation_review_session_viewer(
        context_file=context_path,
        godot_executable=godot_path,
    )


if __name__ == "__main__":
    raise SystemExit(main())
