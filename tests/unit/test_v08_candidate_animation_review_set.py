"""V0.8-8 unit tests: multi-clip animation review set export and currentness."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sys
from importlib import resources
from pathlib import Path

import pytest

from gamefactory.adapters.assets.animation_clip_input import MAX_LOCAL_ANIMATION_CLIP_BYTES
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    ProviderInvocationRepository,
)
from gamefactory.core.domain.animation_clip import ANIMATION_CLIP_SCHEMA_VERSION
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import ExecutionStatus
from gamefactory.workflows.v08_candidate_animation_clip_preview import (
    _CLIP_PACKAGE_FILES,
    _read_bounded_package_file,
    acceptance_arm_wave_clip_raw_bytes,
    animation_clip_preview_current,
    build_acceptance_arm_wave_clip_document,
    export_rigged_character_animation_clip_preview,
)
from gamefactory.workflows.v08_candidate_animation_review_set import (
    CandidateAnimationReviewSetSource,
    animation_review_set_current,
    capture_animation_review_set_sources,
    export_rigged_character_animation_review_set,
    trusted_animation_review_set_ui_digests,
    validate_review_set_sources,
)
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from gamefactory.workflows.v08_candidate_preview import (
    candidate_preview_current,
    export_rigged_character_candidate_preview,
)
from gamefactory.workflows.v08_candidate_workflow import candidate_workflow_readiness
from tests.unit.test_v08_candidate_workflow import _run_to_test_only_gate
from tests.unit.v08_candidate_c2b_publication_fixtures import (
    inject_destination_directory_junction,
    inject_nonempty_destination_directory,
)
from tests.unit.v08_candidate_c2b_readiness_fixtures import (
    CompletedEvidenceContext,
    inject_newer_evidence_attempt,
    run_completed_managed_evidence,
)


def _y_rotation_quaternion_xyzw(degrees: float) -> tuple[float, float, float, float]:
    half = math.radians(degrees) * 0.5
    return (0.0, math.sin(half), 0.0, math.cos(half))


def build_authored_arm_reverse_02_clip_document() -> dict[str, object]:
    identity = [0.0, 0.0, 0.0, 1.0]
    y_neg30 = list(_y_rotation_quaternion_xyzw(-30.0))
    return {
        "schema_version": ANIMATION_CLIP_SCHEMA_VERSION,
        "clip_id": "arm_reverse_02",
        "duration_seconds": 2.0,
        "loop": True,
        "tracks": [
            {
                "bone": "Spine",
                "keyframes": [
                    {"time": 0.0, "rotation_xyzw": identity},
                    {"time": 0.75, "rotation_xyzw": y_neg30},
                    {"time": 1.5, "rotation_xyzw": identity},
                ],
            },
        ],
    }


def authored_arm_reverse_02_clip_raw_bytes() -> bytes:
    return json.dumps(
        build_authored_arm_reverse_02_clip_document(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


_EXPECTED_REVIEW_SET_ROOT_FILES = frozenset(
    {
        "character.glb",
        "character.tscn",
        "candidate.json",
        "collider.json",
        "preview-manifest.json",
        "animation_clip_preview_player.gd",
        "animation_review_controller.gd",
        "animation_review_set_player.gd",
        "animation_review_set_controller.gd",
        "animation_review_set.tscn",
        "animation_review_set_preview.tscn",
        "animation_review_set_manifest.json",
        "project.godot",
    }
)


@pytest.fixture(scope="module")
def managed_review_set_evidence(
    tmp_path_factory: pytest.TempPathFactory,
) -> CompletedEvidenceContext:
    return run_completed_managed_evidence(tmp_path_factory.mktemp("review-set-managed-c2b"))


@pytest.fixture
def isolated_review_set_evidence(tmp_path: Path) -> CompletedEvidenceContext:
    return run_completed_managed_evidence(tmp_path / "review-set-isolated-c2b")


@pytest.fixture(scope="module")
def shared_preview(
    managed_review_set_evidence: CompletedEvidenceContext,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    out = tmp_path_factory.mktemp("review-set-shared-preview") / "preview"
    export_rigged_character_candidate_preview(
        managed_review_set_evidence.handlers,
        managed_review_set_evidence.workflow_id,
        out,
    )
    return out


@pytest.fixture(scope="module")
def acceptance_clip_a_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("review-set-clip-a") / "arm_wave_01.json"
    path.write_bytes(acceptance_arm_wave_clip_raw_bytes())
    return path


@pytest.fixture(scope="module")
def acceptance_clip_b_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("review-set-clip-b") / "arm_reverse_02.json"
    path.write_bytes(authored_arm_reverse_02_clip_raw_bytes())
    return path


@pytest.fixture(scope="module")
def shared_clip_preview_a(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    out = tmp_path_factory.mktemp("review-set-v086-a") / "clip-preview-a"
    export_rigged_character_animation_clip_preview(
        managed_review_set_evidence.handlers,
        managed_review_set_evidence.workflow_id,
        shared_preview,
        acceptance_clip_a_path,
        out,
    )
    return out


@pytest.fixture(scope="module")
def shared_clip_preview_b(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_b_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    out = tmp_path_factory.mktemp("review-set-v086-b") / "clip-preview-b"
    export_rigged_character_animation_clip_preview(
        managed_review_set_evidence.handlers,
        managed_review_set_evidence.workflow_id,
        shared_preview,
        acceptance_clip_b_path,
        out,
    )
    return out


@pytest.fixture(scope="module")
def shared_review_set(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Path:
    out = tmp_path_factory.mktemp("review-set-shared-package") / "review-set"
    sources = (
        CandidateAnimationReviewSetSource(shared_clip_preview_a, acceptance_clip_a_path),
        CandidateAnimationReviewSetSource(shared_clip_preview_b, acceptance_clip_b_path),
    )
    export_rigged_character_animation_review_set(
        managed_review_set_evidence.handlers,
        managed_review_set_evidence.workflow_id,
        shared_preview,
        sources,
        out,
    )
    return out


def _sources_ab(
    clip_a: Path,
    clip_a_path: Path,
    clip_b: Path,
    clip_b_path: Path,
) -> tuple[CandidateAnimationReviewSetSource, CandidateAnimationReviewSetSource]:
    return (
        CandidateAnimationReviewSetSource(clip_a, clip_a_path),
        CandidateAnimationReviewSetSource(clip_b, clip_b_path),
    )


def _sources_ba(
    clip_a: Path,
    clip_a_path: Path,
    clip_b: Path,
    clip_b_path: Path,
) -> tuple[CandidateAnimationReviewSetSource, CandidateAnimationReviewSetSource]:
    return (
        CandidateAnimationReviewSetSource(clip_b, clip_b_path),
        CandidateAnimationReviewSetSource(clip_a, clip_a_path),
    )


def _export_pair(
    ctx: CompletedEvidenceContext,
    preview: Path,
    sources: tuple[CandidateAnimationReviewSetSource, ...],
    out: Path,
):
    return export_rigged_character_animation_review_set(
        ctx.handlers, ctx.workflow_id, preview, sources, out
    )


def _copy_review_set_fixture(
    shared_preview: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    tmp_path: Path,
    label: str,
) -> tuple[Path, Path, Path, Path, Path, Path]:
    preview = tmp_path / f"{label}-preview"
    shutil.copytree(shared_preview, preview)
    clip_a = tmp_path / f"{label}-v086-a"
    clip_b = tmp_path / f"{label}-v086-b"
    shutil.copytree(shared_clip_preview_a, clip_a)
    shutil.copytree(shared_clip_preview_b, clip_b)
    clip_a_path = tmp_path / f"{label}-clip-a.json"
    clip_b_path = tmp_path / f"{label}-clip-b.json"
    clip_a_path.write_bytes(acceptance_clip_a_path.read_bytes())
    clip_b_path.write_bytes(acceptance_clip_b_path.read_bytes())
    review_set = tmp_path / f"{label}-review-set"
    shutil.copytree(shared_review_set, review_set)
    return preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set


def _isolated_review_set_sources(
    shared_preview: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    tmp_path: Path,
    label: str,
) -> tuple[Path, Path, Path, Path, Path, tuple[CandidateAnimationReviewSetSource, ...]]:
    preview = tmp_path / f"{label}-preview"
    shutil.copytree(shared_preview, preview)
    clip_a = tmp_path / f"{label}-v086-a"
    clip_b = tmp_path / f"{label}-v086-b"
    shutil.copytree(shared_clip_preview_a, clip_a)
    shutil.copytree(shared_clip_preview_b, clip_b)
    clip_a_path = tmp_path / f"{label}-clip-a.json"
    clip_b_path = tmp_path / f"{label}-clip-b.json"
    clip_a_path.write_bytes(acceptance_clip_a_path.read_bytes())
    clip_b_path.write_bytes(acceptance_clip_b_path.read_bytes())
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    return preview, clip_a, clip_b, clip_a_path, clip_b_path, sources


_EIGHT_CLIP_IDS_ORDERED: tuple[str, ...] = (
    "arm_wave_01",
    "arm_reverse_02",
    "arm_review_03",
    "arm_review_04",
    "arm_review_05",
    "arm_review_06",
    "arm_review_07",
    "arm_review_08",
)


def _authored_acceptance_clip_raw_bytes(*, clip_id: str, duration_seconds: float) -> bytes:
    doc = build_acceptance_arm_wave_clip_document()
    doc["clip_id"] = clip_id
    doc["duration_seconds"] = duration_seconds
    identity = [0.0, 0.0, 0.0, 1.0]
    y30 = list(_y_rotation_quaternion_xyzw(30.0))
    doc["tracks"] = [
        {
            "bone": "LeftUpperArm",
            "keyframes": [
                {"time": 0.0, "rotation_xyzw": identity},
                {"time": min(0.75, duration_seconds), "rotation_xyzw": y30},
                {"time": duration_seconds, "rotation_xyzw": identity},
            ],
        },
        {
            "bone": "Spine",
            "keyframes": [{"time": 0.0, "rotation_xyzw": identity}],
        },
    ]
    return json.dumps(
        doc,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _eight_clip_raw_bytes_by_id() -> dict[str, bytes]:
    payloads: dict[str, bytes] = {
        "arm_wave_01": acceptance_arm_wave_clip_raw_bytes(),
        "arm_reverse_02": authored_arm_reverse_02_clip_raw_bytes(),
    }
    for index, clip_id in enumerate(_EIGHT_CLIP_IDS_ORDERED[2:], start=3):
        payloads[clip_id] = _authored_acceptance_clip_raw_bytes(
            clip_id=clip_id,
            duration_seconds=1.5 + (index * 0.05),
        )
    return payloads


def _arm_wave_duration_variant_raw_bytes(duration_seconds: float) -> bytes:
    doc = build_acceptance_arm_wave_clip_document()
    doc["duration_seconds"] = duration_seconds
    identity = [0.0, 0.0, 0.0, 1.0]
    doc["tracks"] = [
        {
            "bone": "LeftUpperArm",
            "keyframes": [{"time": 0.0, "rotation_xyzw": identity}],
        },
        {
            "bone": "Spine",
            "keyframes": [{"time": 0.0, "rotation_xyzw": identity}],
        },
    ]
    if duration_seconds > 0.0:
        doc["tracks"][0]["keyframes"].append({"time": duration_seconds, "rotation_xyzw": identity})
    return json.dumps(
        doc,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _coherent_v086_preview_bone_names(clip_dir: Path, bone_names: tuple[str, ...]) -> None:
    tscn_path = clip_dir / "animation_clip_preview.tscn"
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
    manifest_path = clip_dir / "animation_clip_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["file_digests"]["animation_clip_preview.tscn"] = sha256_file(tscn_path)
    manifest_path.write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )


def test_export_review_set_upper_bound_eight_capture_shares_character_glb_identity(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    clip_payloads = _eight_clip_raw_bytes_by_id()
    clip_paths: list[Path] = []
    v086_dirs: list[Path] = []
    for clip_id in _EIGHT_CLIP_IDS_ORDERED:
        clip_path = tmp_path / f"{clip_id}.json"
        clip_path.write_bytes(clip_payloads[clip_id])
        clip_paths.append(clip_path)
        v086_out = tmp_path / f"v086-{clip_id}"
        export_rigged_character_animation_clip_preview(
            ctx.handlers,
            ctx.workflow_id,
            shared_preview,
            clip_path,
            v086_out,
        )
        v086_dirs.append(v086_out)
    v086_before = [
        {name: (v086_dir / name).read_bytes() for name in _CLIP_PACKAGE_FILES}
        for v086_dir in v086_dirs
    ]
    sources = tuple(
        CandidateAnimationReviewSetSource(v086_dirs[index], clip_paths[index])
        for index in range(len(_EIGHT_CLIP_IDS_ORDERED))
    )
    shared_glb_sha256 = sha256_file(shared_preview / "character.glb")
    captured = capture_animation_review_set_sources(
        ctx.handlers,
        ctx.workflow_id,
        shared_preview,
        sources,
    )
    assert len(captured) == 8
    shared_glb = captured[0].v086_bytes["character.glb"]
    for item in captured:
        assert item.v086_bytes["character.glb"] is shared_glb
        assert hashlib.sha256(item.v086_bytes["character.glb"]).hexdigest() == shared_glb_sha256
    out = tmp_path / "review-set-upper-bound-eight"
    result = _export_pair(ctx, shared_preview, sources, out)
    leaf_files = [path for path in out.rglob("*") if path.is_file()]
    assert len(leaf_files) == 29
    assert {path.name for path in out.iterdir()} == _EXPECTED_REVIEW_SET_ROOT_FILES | {"clips"}
    for slot_index in range(8):
        slot_dir = out / "clips" / f"{slot_index:03d}"
        assert {path.name for path in slot_dir.iterdir()} == {
            "animation_clip.json",
            "animation_clip_manifest.json",
        }
    manifest = json.loads((out / "animation_review_set_manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("production_eligible") is False
    assert manifest.get("promotion_eligible") is False
    assert result.ordered_clip_ids == _EIGHT_CLIP_IDS_ORDERED
    assert [entry["clip_id"] for entry in manifest["ordered_clips"]] == list(
        _EIGHT_CLIP_IDS_ORDERED
    )
    for index, before in enumerate(v086_before):
        for name, payload in before.items():
            assert (v086_dirs[index] / name).read_bytes() == payload
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, shared_preview, sources, out)
        is True
    )


def test_export_review_set_thirteen_root_files_two_slots_v086_unchanged(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    v086_a_before = {
        name: (shared_clip_preview_a / name).read_bytes() for name in _CLIP_PACKAGE_FILES
    }
    v086_b_before = {
        name: (shared_clip_preview_b / name).read_bytes() for name in _CLIP_PACKAGE_FILES
    }
    arts_before = list(ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id))
    before_invocations = ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id)
    out = tmp_path / "review-set-bytes"
    sources = _sources_ab(
        shared_clip_preview_a,
        acceptance_clip_a_path,
        shared_clip_preview_b,
        acceptance_clip_b_path,
    )
    result = _export_pair(ctx, shared_preview, sources, out)
    assert {p.name for p in out.iterdir()} == _EXPECTED_REVIEW_SET_ROOT_FILES | {"clips"}
    for slot in ("000", "001"):
        slot_dir = out / "clips" / slot
        assert {p.name for p in slot_dir.iterdir()} == {
            "animation_clip.json",
            "animation_clip_manifest.json",
        }
    manifest = json.loads((out / "animation_review_set_manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("production_eligible") is False
    assert manifest.get("promotion_eligible") is False
    assert result.ordered_clip_ids == ("arm_wave_01", "arm_reverse_02")
    for name, payload in v086_a_before.items():
        assert (shared_clip_preview_a / name).read_bytes() == payload
    for name, payload in v086_b_before.items():
        assert (shared_clip_preview_b / name).read_bytes() == payload
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, shared_preview, sources, out)
        is True
    )
    assert candidate_preview_current(ctx.handlers, ctx.workflow_id, shared_preview) is True
    assert (
        ProviderInvocationRepository(ctx.workspace.db).count(ctx.workflow_id) == before_invocations
    )
    arts_after = list(ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id))
    assert len(arts_before) == len(arts_after)


def test_export_review_set_deterministic_and_order_reversed_renders_reversed(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    out_ab = tmp_path / "order-ab"
    out_ba = tmp_path / "order-ba"
    out_ab_2 = tmp_path / "order-ab-2"
    sources_ab = _sources_ab(
        shared_clip_preview_a,
        acceptance_clip_a_path,
        shared_clip_preview_b,
        acceptance_clip_b_path,
    )
    sources_ba = _sources_ba(
        shared_clip_preview_a,
        acceptance_clip_a_path,
        shared_clip_preview_b,
        acceptance_clip_b_path,
    )
    _export_pair(ctx, shared_preview, sources_ab, out_ab)
    _export_pair(ctx, shared_preview, sources_ab, out_ab_2)
    _export_pair(ctx, shared_preview, sources_ba, out_ba)
    for name in sorted(_EXPECTED_REVIEW_SET_ROOT_FILES):
        assert (out_ab / name).read_bytes() == (out_ab_2 / name).read_bytes()
    manifest_ab = json.loads((out_ab / "animation_review_set_manifest.json").read_bytes())
    manifest_ba = json.loads((out_ba / "animation_review_set_manifest.json").read_bytes())
    assert [e["clip_id"] for e in manifest_ab["ordered_clips"]] == [
        "arm_wave_01",
        "arm_reverse_02",
    ]
    assert [e["clip_id"] for e in manifest_ba["ordered_clips"]] == [
        "arm_reverse_02",
        "arm_wave_01",
    ]
    assert manifest_ab["ordered_clips"][0]["clip_id"] == "arm_wave_01"
    assert manifest_ba["ordered_clips"][0]["clip_id"] == "arm_reverse_02"
    preview_ab = (out_ab / "animation_review_set_preview.tscn").read_text(encoding="utf-8")
    preview_ba = (out_ba / "animation_review_set_preview.tscn").read_text(encoding="utf-8")
    assert preview_ab.index("arm_wave_01") < preview_ab.index("arm_reverse_02")
    assert preview_ba.index("arm_reverse_02") < preview_ba.index("arm_wave_01")


@pytest.mark.parametrize("count", [0, 1, 9])
def test_validate_rejects_invalid_source_count(
    shared_clip_preview_a: Path,
    acceptance_clip_a_path: Path,
    count: int,
) -> None:
    if count == 0:
        sources: list[CandidateAnimationReviewSetSource] = []
    elif count == 1:
        sources = [CandidateAnimationReviewSetSource(shared_clip_preview_a, acceptance_clip_a_path)]
    else:
        sources = [
            CandidateAnimationReviewSetSource(shared_clip_preview_a, acceptance_clip_a_path)
        ] * 9
    with pytest.raises(ValidationError, match="between"):
        validate_review_set_sources(sources)


def test_validate_rejects_duplicate_clip_ids(
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    acceptance_clip_a_path: Path,
) -> None:
    sources = (
        CandidateAnimationReviewSetSource(shared_clip_preview_a, acceptance_clip_a_path),
        CandidateAnimationReviewSetSource(shared_clip_preview_b, acceptance_clip_a_path),
    )
    with pytest.raises(ValidationError, match=r"review set clip_id .+ is duplicated"):
        validate_review_set_sources(sources)


def test_review_set_current_false_on_stale_evidence(
    isolated_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    tmp_path: Path,
) -> None:
    ctx = isolated_review_set_evidence
    preview = tmp_path / "stale-preview"
    export_rigged_character_candidate_preview(ctx.handlers, ctx.workflow_id, preview)
    clip_a = tmp_path / "stale-v086-a"
    clip_b = tmp_path / "stale-v086-b"
    export_rigged_character_animation_clip_preview(
        ctx.handlers, ctx.workflow_id, preview, acceptance_clip_a_path, clip_a
    )
    export_rigged_character_animation_clip_preview(
        ctx.handlers, ctx.workflow_id, preview, acceptance_clip_b_path, clip_b
    )
    sources = _sources_ab(clip_a, acceptance_clip_a_path, clip_b, acceptance_clip_b_path)
    out = tmp_path / "stale-review-set"
    _export_pair(ctx, preview, sources, out)
    inject_newer_evidence_attempt(ctx, status=ExecutionStatus.RUNNING, attempt_number=50)
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, preview, sources, out) is False
    )


def test_export_rejects_foreign_workflow_clip_package(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    foreign = run_completed_managed_evidence(tmp_path / "foreign-workflow")
    foreign_preview = tmp_path / "foreign-preview"
    export_rigged_character_candidate_preview(
        foreign.handlers, foreign.workflow_id, foreign_preview
    )
    foreign_clip = tmp_path / "foreign-v086"
    export_rigged_character_animation_clip_preview(
        foreign.handlers,
        foreign.workflow_id,
        foreign_preview,
        acceptance_clip_b_path,
        foreign_clip,
    )
    sources = _sources_ab(
        shared_clip_preview_a,
        acceptance_clip_a_path,
        foreign_clip,
        acceptance_clip_b_path,
    )
    out = tmp_path / "foreign-mix"
    with pytest.raises(CandidateCurrentnessError, match="not current"):
        _export_pair(ctx, shared_preview, sources, out)
    assert not out.exists()


def test_export_rejects_shared_identity_rig_mismatch(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    preview, _clip_a, clip_b, _clip_a_path, clip_b_path, sources = _isolated_review_set_sources(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "rig-mismatch",
    )
    _coherent_v086_preview_bone_names(clip_b, ("Spine", "RightUpperArm"))
    assert (
        animation_clip_preview_current(ctx.handlers, ctx.workflow_id, preview, clip_b_path, clip_b)
        is False
    )
    out = tmp_path / "rig-mismatch"
    with pytest.raises(CandidateCurrentnessError, match="not current"):
        _export_pair(ctx, preview, sources, out)
    assert not out.exists()


def test_review_set_current_false_on_coherent_new_ui_rehash(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    _preview, _a, _b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "ui-rehash",
    )
    sources = _sources_ab(_a, clip_a_path, _b, clip_b_path)
    tscn = review_set / "animation_review_set.tscn"
    text = tscn.read_text(encoding="utf-8")
    tscn.write_text(text.replace("AnimationReviewSet", "AnimationReviewSetX", 1), encoding="utf-8")
    manifest_path = review_set / "animation_review_set_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["file_digests"]["animation_review_set.tscn"] = sha256_file(tscn)
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
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, _preview, sources, review_set)
        is False
    )


def test_review_set_current_false_on_coherent_nested_clip_rehash_upstream_unchanged(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    _preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "nested-rehash",
    )
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    v086_a_before = {name: (clip_a / name).read_bytes() for name in _CLIP_PACKAGE_FILES}
    nested = review_set / "clips/000/animation_clip.json"
    doc = json.loads(nested.read_bytes())
    doc["duration_seconds"] = 2.0
    nested.write_text(json.dumps(doc), encoding="utf-8")
    nested_manifest = review_set / "clips/000/animation_clip_manifest.json"
    nested_manifest_doc = json.loads(nested_manifest.read_bytes())
    nested_manifest_doc["file_digests"]["animation_clip.json"] = sha256_file(nested)
    nested_manifest_doc["clip_sha256"] = sha256_file(nested)
    nested_manifest.write_text(json.dumps(nested_manifest_doc), encoding="utf-8")
    root_manifest_path = review_set / "animation_review_set_manifest.json"
    root_manifest = json.loads(root_manifest_path.read_bytes())
    ordered = list(root_manifest["ordered_clips"])
    ordered[0] = dict(ordered[0])
    ordered[0]["raw_clip_sha256"] = sha256_file(nested)
    ordered[0]["raw_original_manifest_sha256"] = sha256_file(nested_manifest)
    root_manifest["ordered_clips"] = ordered
    clip_digests = {
        path: sha256_file(review_set / path)
        for path in (
            "clips/000/animation_clip.json",
            "clips/000/animation_clip_manifest.json",
            "clips/001/animation_clip.json",
            "clips/001/animation_clip_manifest.json",
        )
    }
    root_manifest["nested_clips_payload_digest"] = hashlib.sha256(
        json.dumps(clip_digests, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()
    root_manifest_path.write_bytes(
        json.dumps(root_manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )
    for name, payload in v086_a_before.items():
        assert (clip_a / name).read_bytes() == payload
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, _preview, sources, review_set)
        is False
    )


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        ("missing_key", "fields do not match"),
        ("extra_key", "fields do not match"),
        ("duplicate_ordered", r"review set clip_id .+ is duplicated"),
    ],
)
def test_review_set_rejects_malformed_manifest_shape(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    tmp_path: Path,
    mutator: str,
    match: str,
) -> None:
    ctx = managed_review_set_evidence
    _preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        f"manifest-{mutator}",
    )
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    path = review_set / "animation_review_set_manifest.json"
    manifest = json.loads(path.read_bytes())
    if mutator == "missing_key":
        del manifest["workflow_id"]
    elif mutator == "extra_key":
        manifest["unexpected"] = True
    elif mutator == "duplicate_ordered":
        manifest["ordered_clips"] = [manifest["ordered_clips"][0], manifest["ordered_clips"][0]]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValidationError, match=match):
        animation_review_set_current(ctx.handlers, ctx.workflow_id, _preview, sources, review_set)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("production_eligible", 0),
        ("promotion_eligible", 0),
        ("evidence_attempt_number", True),
        ("evidence_attempt_number", 1.0),
    ],
)
def test_review_set_rejects_manifest_json_type_aliases(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    ctx = managed_review_set_evidence
    _preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "types",
    )
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    path = review_set / "animation_review_set_manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = value
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if field in ("production_eligible", "promotion_eligible"):
        with pytest.raises(ValidationError):
            animation_review_set_current(
                ctx.handlers, ctx.workflow_id, _preview, sources, review_set
            )
    else:
        assert (
            animation_review_set_current(
                ctx.handlers, ctx.workflow_id, _preview, sources, review_set
            )
            is False
        )


def test_export_refuses_existing_empty_review_set_destination(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    out = tmp_path / "review-set-busy"
    out.mkdir()
    sources = _sources_ab(
        shared_clip_preview_a,
        acceptance_clip_a_path,
        shared_clip_preview_b,
        acceptance_clip_b_path,
    )
    with pytest.raises(ArtifactError, match="already exists"):
        _export_pair(ctx, shared_preview, sources, out)


def test_export_refuses_nonempty_review_set_destination(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    out = tmp_path / "review-set-nonempty"
    inject_nonempty_destination_directory("", str(out))
    sources = _sources_ab(
        shared_clip_preview_a,
        acceptance_clip_a_path,
        shared_clip_preview_b,
        acceptance_clip_b_path,
    )
    with pytest.raises(ArtifactError, match="already exists"):
        _export_pair(ctx, shared_preview, sources, out)


def test_review_set_upstream_read_rejects_growth_after_size_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gamefactory.workflows.v08_candidate_animation_review_set as module

    path = tmp_path / "candidate.json"
    path.write_bytes(b"x")
    real_fstat = module.os.fstat

    def grow_after_stat(fd: int):
        info = real_fstat(fd)
        with path.open("ab") as stream:
            stream.write(b"x" * (1024 * 1024 + 1))
        return info

    monkeypatch.setattr(module.os, "fstat", grow_after_stat)
    with pytest.raises(ValidationError, match="byte limit"):
        module._read_bounded_review_set_bytes(path, label="candidate.json", max_bytes=1024)


@pytest.mark.skipif(
    sys.platform not in ("win32", "linux"), reason="junction/symlink review set clip slot guard"
)
def test_review_set_inspect_rejects_clip_slot_junction_alias(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    _preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "slot-alias",
    )
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    slot_backup = tmp_path / "slot-000-backup"
    shutil.copytree(review_set / "clips" / "000", slot_backup)
    shutil.rmtree(review_set / "clips" / "000")
    slot_alias = review_set / "clips" / "000"
    inject_destination_directory_junction("", str(slot_alias))
    slot_target = slot_alias.with_name(slot_alias.name + "_junction_target")
    shutil.copytree(slot_backup, slot_target, dirs_exist_ok=True)
    with pytest.raises(ValidationError, match="crosses a link"):
        animation_review_set_current(ctx.handlers, ctx.workflow_id, _preview, sources, review_set)


def test_prepublish_source_a_mutation_fails_before_output(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_review_set as module

    ctx = managed_review_set_evidence
    preview, clip_a, _clip_b, _clip_a_path, _clip_b_path, sources = _isolated_review_set_sources(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "prepublish",
    )
    out = tmp_path / "prepublish-review-set"
    original_write = module._write_review_set_tree

    def write_then_mutate_source_a(*args, **kwargs):
        result = original_write(*args, **kwargs)
        glb = clip_a / "character.glb"
        glb.write_bytes(glb.read_bytes() + b" ")
        return result

    monkeypatch.setattr(module, "_write_review_set_tree", write_then_mutate_source_a)
    with pytest.raises(CandidateCurrentnessError):
        _export_pair(ctx, preview, sources, out)
    assert not out.exists()


def test_postpublish_review_set_drift_leaves_stale_orphan(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_review_set as module

    ctx = managed_review_set_evidence
    preview, _clip_a, _clip_b, clip_a_path, _clip_b_path, sources = _isolated_review_set_sources(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "postpublish",
    )
    out = tmp_path / "postpublish-review-set"
    original_publish = module.atomic_publish_staged_container

    def publish_then_mutate_clip(stage, publish_parent, name):
        destination = original_publish(stage, publish_parent, name)
        mutated = json.loads(clip_a_path.read_bytes())
        mutated["clip_id"] = "mutated_after_publish"
        clip_a_path.write_bytes(json.dumps(mutated).encode("utf-8"))
        return destination

    monkeypatch.setattr(module, "atomic_publish_staged_container", publish_then_mutate_clip)
    with pytest.raises(CandidateCurrentnessError):
        _export_pair(ctx, preview, sources, out)
    assert out.is_dir()
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, preview, sources, out) is False
    )


def test_export_stage_coherent_corruption_leaves_no_output(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_review_set as module

    ctx = managed_review_set_evidence
    preview, _clip_a, _clip_b, _clip_a_path, _clip_b_path, sources = _isolated_review_set_sources(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "stage-corrupt",
    )
    out = tmp_path / "stage-corrupt-review-set"
    original_assert = module._assert_staged_review_set_matches_expected

    def corrupt_stage(stage, captured, *, workflow_id: str):
        gd_path = stage / "animation_review_set_player.gd"
        original_bytes = gd_path.read_bytes()
        corrupted_bytes = original_bytes + b"\n# stage-coherent-corruption-oracle\n"
        assert corrupted_bytes != original_bytes
        gd_path.write_bytes(corrupted_bytes)
        manifest_path = stage / "animation_review_set_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["file_digests"]["animation_review_set_player.gd"] = sha256_file(gd_path)
        for name in sorted(manifest["file_digests"]):
            assert manifest["file_digests"][name] == sha256_file(stage / name)
        independent_root_payload_digest = hashlib.sha256(
            json.dumps(
                {k: manifest["file_digests"][k] for k in sorted(manifest["file_digests"])},
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        manifest["root_payload_digest"] = independent_root_payload_digest
        manifest_path.write_bytes(
            json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        )
        reloaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert sorted(reloaded["file_digests"]) == sorted(manifest["file_digests"])
        assert reloaded["root_payload_digest"] == independent_root_payload_digest
        assert (
            hashlib.sha256(
                json.dumps(
                    {k: reloaded["file_digests"][k] for k in sorted(reloaded["file_digests"])},
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            == independent_root_payload_digest
        )
        original_assert(stage, captured, workflow_id=workflow_id)

    monkeypatch.setattr(module, "_assert_staged_review_set_matches_expected", corrupt_stage)
    with pytest.raises(
        CandidateCurrentnessError,
        match=(
            "staged animation review set bytes do not match authoritative captured sources"
            "|staged animation review set UI resource .+ digest mismatch"
        ),
    ):
        _export_pair(ctx, preview, sources, out)
    assert not out.exists()


def test_export_review_set_calls_animation_clip_preview_current_six_times(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_review_set as review_set_module

    ctx = managed_review_set_evidence
    sources = _sources_ab(
        shared_clip_preview_a,
        acceptance_clip_a_path,
        shared_clip_preview_b,
        acceptance_clip_b_path,
    )
    calls: list[str] = []
    original = review_set_module.animation_clip_preview_current

    def counting_current(h, w, p, c, a):
        calls.append("call")
        return original(h, w, p, c, a)

    monkeypatch.setattr(review_set_module, "animation_clip_preview_current", counting_current)
    _export_pair(ctx, shared_preview, sources, tmp_path / "call-count-export")
    assert len(calls) == 6


def test_animation_review_set_current_calls_clip_preview_twice(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import gamefactory.workflows.v08_candidate_animation_review_set as review_set_module

    ctx = managed_review_set_evidence
    sources = _sources_ab(
        shared_clip_preview_a,
        acceptance_clip_a_path,
        shared_clip_preview_b,
        acceptance_clip_b_path,
    )
    calls: list[str] = []
    original = review_set_module.animation_clip_preview_current

    def counting_current(h, w, p, c, a):
        calls.append("call")
        return original(h, w, p, c, a)

    monkeypatch.setattr(review_set_module, "animation_clip_preview_current", counting_current)
    assert (
        animation_review_set_current(
            ctx.handlers, ctx.workflow_id, shared_preview, sources, shared_review_set
        )
        is True
    )
    assert len(calls) == 2


def test_review_set_current_false_when_source0_coherent_b_during_source1_gate(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whole-collection pin must fail when source 0 drifts during source 1 public gate."""
    import gamefactory.workflows.v08_candidate_animation_review_set as review_set_module

    ctx = managed_review_set_evidence
    _preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "race-pin",
    )
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, _preview, sources, review_set)
        is True
    )
    variant_clip_path = tmp_path / "race-pin-clip-a-variant.json"
    variant_clip_path.write_bytes(_arm_wave_duration_variant_raw_bytes(2.0))
    clip_a_variant = tmp_path / "race-pin-v086-a-variant"
    export_rigged_character_animation_clip_preview(
        ctx.handlers,
        ctx.workflow_id,
        _preview,
        variant_clip_path,
        clip_a_variant,
    )
    original = review_set_module.animation_clip_preview_current
    gate_calls: list[str] = []
    swapped = False

    def gate_then_swap_source0_to_variant(h, w, p, c, a):
        nonlocal swapped
        gate_calls.append("call")
        result = original(h, w, p, c, a)
        if not swapped and a.resolve() == clip_b.resolve():
            assert result is True
            swapped = True
            for name in _CLIP_PACKAGE_FILES:
                (clip_a / name).write_bytes((clip_a_variant / name).read_bytes())
            clip_a_path.write_bytes(variant_clip_path.read_bytes())
        return result

    monkeypatch.setattr(
        review_set_module, "animation_clip_preview_current", gate_then_swap_source0_to_variant
    )
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, _preview, sources, review_set)
        is False
    )
    assert len(gate_calls) == 2
    assert (
        animation_clip_preview_current(ctx.handlers, ctx.workflow_id, _preview, clip_a_path, clip_a)
        is True
    )


def test_trusted_review_set_ui_digests_match_packaged_resources() -> None:
    expected_names = (
        "animation_review_set_controller.gd",
        "animation_review_set_player.gd",
        "animation_review_set.tscn",
    )
    digests = trusted_animation_review_set_ui_digests()
    assert len(digests) == 3
    assert set(digests) == set(expected_names)
    package = resources.files("gamefactory.resources.v08_candidate")
    for name in expected_names:
        raw = package.joinpath(name).read_bytes().replace(b"\r\n", b"\n")
        assert digests[name] == hashlib.sha256(raw).hexdigest()


def test_export_review_set_fails_when_evidence_not_ready(
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    tmp_path: Path,
) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path / "ws-not-ready")
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    candidate_workflow_readiness(handlers, workflow_id)
    preview = tmp_path / "preview-placeholder"
    preview.mkdir()
    sources = (
        CandidateAnimationReviewSetSource(tmp_path / "v086-a", acceptance_clip_a_path),
        CandidateAnimationReviewSetSource(tmp_path / "v086-b", acceptance_clip_b_path),
    )
    with pytest.raises(CandidateCurrentnessError):
        export_rigged_character_animation_review_set(
            handlers,
            workflow_id,
            preview,
            sources,
            tmp_path / "review-set-blocked",
        )


def test_oversized_root_manifest_rejected_on_inspect(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    _preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "oversized-manifest",
    )
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    manifest_path = review_set / "animation_review_set_manifest.json"
    raw = manifest_path.read_bytes()
    overflow = 65 * 1024 - len(raw) + 1
    assert overflow > 0
    manifest_path.write_bytes(raw + (b" " * overflow))
    with pytest.raises(ValidationError, match="byte limit"):
        animation_review_set_current(ctx.handlers, ctx.workflow_id, _preview, sources, review_set)


@pytest.mark.parametrize(
    "mutator",
    [
        "duplicate_json_key",
        "nonfinite_number",
    ],
)
def test_review_set_rejects_strict_manifest_json_on_inspect(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    tmp_path: Path,
    mutator: str,
) -> None:
    ctx = managed_review_set_evidence
    _preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        f"strict-manifest-{mutator}",
    )
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    path = review_set / "animation_review_set_manifest.json"
    if mutator == "duplicate_json_key":
        raw = path.read_bytes()
        path.write_bytes(b'{"production_eligible":false,' + raw[1:])
        match = "duplicate JSON key"
    else:
        manifest = json.loads(path.read_bytes())
        attempt = manifest["evidence_attempt_number"]
        raw = path.read_bytes()
        path.write_bytes(
            raw.replace(
                f'"evidence_attempt_number":{attempt}'.encode(),
                b'"evidence_attempt_number":NaN',
            )
        )
        match = r"invalid JSON constant"
    with pytest.raises(ValidationError, match=match):
        animation_review_set_current(ctx.handlers, ctx.workflow_id, _preview, sources, review_set)


@pytest.mark.parametrize(
    ("case", "setup"),
    [
        ("invalid_clip_json", lambda p: p.write_bytes(b"{not-json")),
        (
            "oversized_clip",
            lambda p: p.write_bytes(b"x" * (MAX_LOCAL_ANIMATION_CLIP_BYTES + 1)),
        ),
    ],
)
def test_review_set_current_false_on_malformed_source_clip(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    tmp_path: Path,
    case: str,
    setup,
) -> None:
    ctx = managed_review_set_evidence
    _preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        f"bad-source-{case}",
    )
    setup(clip_a_path)
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, _preview, sources, review_set)
        is False
    )


@pytest.mark.skipif(
    sys.platform not in ("win32", "linux"), reason="junction/symlink review set source clip guard"
)
def test_review_set_current_false_on_linked_source_clip_path(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    tmp_path: Path,
) -> None:
    ctx = managed_review_set_evidence
    _preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "linked-source-clip",
    )
    linked_clip_dir = tmp_path / "linked-source-clip-dir"
    inject_destination_directory_junction("", str(linked_clip_dir))
    clip_target = linked_clip_dir.with_name(linked_clip_dir.name + "_junction_target")
    linked_clip_path = linked_clip_dir / clip_a_path.name
    (clip_target / clip_a_path.name).write_bytes(clip_a_path.read_bytes())
    sources = _sources_ab(clip_a, linked_clip_path, clip_b, clip_b_path)
    assert (
        animation_review_set_current(ctx.handlers, ctx.workflow_id, _preview, sources, review_set)
        is False
    )


def test_bounded_package_read_growth_via_clip_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gamefactory.workflows.v08_candidate_animation_clip_preview as clip_module

    path = tmp_path / "package.json"
    path.write_bytes(b"x")
    real_fstat = clip_module.os.fstat

    def grow_after_stat(fd: int):
        info = real_fstat(fd)
        with path.open("ab") as stream:
            stream.write(b"x" * (1024 * 1024 + 1))
        return info

    monkeypatch.setattr(clip_module.os, "fstat", grow_after_stat)
    with pytest.raises(ValidationError, match="byte limit"):
        _read_bounded_package_file(path)
