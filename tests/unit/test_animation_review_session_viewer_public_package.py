"""Unmocked viewer prepare against exported V0.8-8 review-set bytes (managed fixtures)."""

# ruff: noqa: F811

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.cli.animation_review_session_bridge import (
    CONTEXT_SCHEMA_VERSION,
    BridgeContext,
    _parse_context_document,
)
from gamefactory.cli.animation_review_session_viewer import prepare_animation_review_session_viewer
from gamefactory.core.domain.errors import ValidationError
from gamefactory.workflows.v08_candidate_animation_review_set import _inspect_review_set_directory
from gamefactory.workflows.v08_candidate_workspace import (
    CANDIDATE_DB_FILENAME,
    CANDIDATE_STATE_DIR,
)
from tests.unit.test_v08_candidate_animation_review_set import (
    _copy_review_set_fixture,
    _sources_ab,
    acceptance_clip_a_path,  # noqa: F401
    acceptance_clip_b_path,  # noqa: F401
    managed_review_set_evidence,  # noqa: F401
    shared_clip_preview_a,  # noqa: F401
    shared_clip_preview_b,  # noqa: F401
    shared_preview,  # noqa: F401
    shared_review_set,  # noqa: F401
)
from tests.unit.v08_candidate_c2b_readiness_fixtures import CompletedEvidenceContext

_VIEWER_EXTRA_FILES = frozenset(
    {
        "animation_review_session.tscn",
        "animation_review_session_controller.gd",
        "context.json",
    }
)


def _coherent_review_set_preview_bone_names(
    review_set: Path,
    bone_names: tuple[str, ...],
) -> None:
    tscn_path = review_set / "animation_review_set_preview.tscn"
    text = tscn_path.read_text(encoding="utf-8")
    packed = ", ".join(json.dumps(name) for name in bone_names)
    updated, count = re.subn(
        r"verified_weighted_bone_names\s*=\s*PackedStringArray\([^)]*\)",
        f"verified_weighted_bone_names = PackedStringArray({packed})",
        text,
        count=1,
    )
    assert count == 1
    assert updated != text
    tscn_path.write_text(updated, encoding="utf-8", newline="\n")
    manifest_path = review_set / "animation_review_set_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_digests"]["animation_review_set_preview.tscn"] = sha256_file(tscn_path)
    manifest["root_payload_digest"] = hashlib.sha256(
        json.dumps(
            {k: manifest["file_digests"][k] for k in sorted(manifest["file_digests"])},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )


def _rehash_review_set_root_manifest_file(review_set: Path, relative_path: str) -> None:
    manifest_path = review_set / "animation_review_set_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_digests"][relative_path] = sha256_file(review_set / relative_path)
    manifest["root_payload_digest"] = hashlib.sha256(
        json.dumps(
            {k: manifest["file_digests"][k] for k in sorted(manifest["file_digests"])},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )


def _review_set_tree_digest(review_set: Path) -> dict[str, str]:
    return {
        str(path.relative_to(review_set)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(review_set.rglob("*"))
        if path.is_file()
    }


def _project_tree_digest(project_root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(project_root)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(project_root.rglob("*"))
        if path.is_file()
    }


def _bridge_context(
    *,
    project_root: Path,
    workflow_id: str,
    preview: Path,
    review_set: Path,
    sources: tuple[Any, ...],
    session_path: Path,
    exchange_dir: Path,
) -> BridgeContext:
    clip_packages = [
        {
            "animation_dir": str(source.animation_dir.resolve()),
            "clip_path": str(source.clip_path.resolve()),
        }
        for source in sources
    ]
    document = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "project_root": str(project_root.resolve()),
        "workflow_id": workflow_id,
        "preview_dir": str(preview.resolve()),
        "review_dir": str(review_set.resolve()),
        "clip_packages": clip_packages,
        "session_path": str(session_path.resolve()),
        "exchange_dir": str(exchange_dir.resolve()),
    }
    return _parse_context_document(document)


@pytest.mark.candidate_slow
def test_unmocked_viewer_prepare_real_review_set_package(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    tmp_path: Path,
) -> None:
    preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "viewer-public-package",
    )
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    ctx_evidence = managed_review_set_evidence
    project_root = ctx_evidence.workspace.root
    session_path = tmp_path / "viewer-public-package-sidecar" / "session.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    external_exchange = tmp_path / "viewer-public-package-exchange"
    external_exchange.mkdir(parents=True, exist_ok=True)
    bridge = _bridge_context(
        project_root=project_root,
        workflow_id=ctx_evidence.workflow_id,
        preview=preview,
        review_set=review_set,
        sources=sources,
        session_path=session_path,
        exchange_dir=external_exchange,
    )

    shared_review_set_before = _review_set_tree_digest(shared_review_set)
    review_before = _review_set_tree_digest(review_set)
    project_before = _project_tree_digest(project_root)
    db_path = project_root / CANDIDATE_STATE_DIR / CANDIDATE_DB_FILENAME
    db_before = db_path.read_bytes()

    overlay_parent = tmp_path / "viewer-overlay-parent"
    overlay_parent.mkdir()
    overlay_dir = overlay_parent / "overlay"
    prepared = prepare_animation_review_session_viewer(bridge, overlay_dir)

    assert _review_set_tree_digest(review_set) == review_before
    assert _project_tree_digest(project_root) == project_before
    assert db_path.read_bytes() == db_before

    overlay_digest = _review_set_tree_digest(prepared.overlay_dir)
    for rel_path, digest in review_before.items():
        assert overlay_digest[rel_path] == digest

    overlay_only = set(overlay_digest) - set(review_before)
    assert overlay_only == set(_VIEWER_EXTRA_FILES)

    assert prepared.exchange_dir.is_dir()
    assert prepared.context_file.is_file()
    assert prepared.overlay_dir == overlay_dir.resolve()

    bone_tamper_review = tmp_path / "viewer-bone-tamper-review-set"
    shutil.copytree(review_set, bone_tamper_review)
    _coherent_review_set_preview_bone_names(bone_tamper_review, ("Spine", "RightUpperArm"))
    bone_manifest = json.loads(
        (bone_tamper_review / "animation_review_set_manifest.json").read_text(encoding="utf-8")
    )
    bone_clip_count = len(bone_manifest["ordered_clips"])
    _inspect_review_set_directory(bone_tamper_review, expected_clip_count=bone_clip_count)
    bone_bridge = _bridge_context(
        project_root=project_root,
        workflow_id=ctx_evidence.workflow_id,
        preview=preview,
        review_set=bone_tamper_review,
        sources=sources,
        session_path=session_path,
        exchange_dir=external_exchange,
    )
    overlay_bone_parent = tmp_path / "viewer-bone-tamper-overlay-parent"
    overlay_bone_parent.mkdir()
    overlay_bone = overlay_bone_parent / "overlay"
    with pytest.raises(ValidationError, match="trusted template"):
        prepare_animation_review_session_viewer(bone_bridge, overlay_bone)
    assert not overlay_bone.exists()

    controller_path = review_set / "animation_review_controller.gd"
    controller_path.write_bytes(
        controller_path.read_bytes() + b"\n# benign coherent rehash comment\n"
    )
    _rehash_review_set_root_manifest_file(review_set, "animation_review_controller.gd")
    manifest_path = review_set / "animation_review_set_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    clip_count = len(manifest["ordered_clips"])
    _inspect_review_set_directory(review_set, expected_clip_count=clip_count)

    overlay_parent_reject = tmp_path / "viewer-overlay-reject-parent"
    overlay_parent_reject.mkdir()
    overlay_reject = overlay_parent_reject / "overlay"
    with pytest.raises(ValidationError, match="trusted template"):
        prepare_animation_review_session_viewer(bridge, overlay_reject)
    assert not overlay_reject.exists()

    assert _review_set_tree_digest(shared_review_set) == shared_review_set_before
