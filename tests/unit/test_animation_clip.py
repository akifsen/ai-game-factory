"""V0.8-6 authored local animation clip ingest (domain + adapter)."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from gamefactory.adapters.assets.animation_clip_input import (
    MAX_LOCAL_ANIMATION_CLIP_BYTES,
    MAX_LOCAL_ANIMATION_CLIP_JSON_CONTAINER_DEPTH,
    assert_animation_clip_skin_influence,
    load_local_animation_clip,
)
from gamefactory.adapters.assets.internal_skin_decode import (
    DecodedInternalSkinnedGLB,
    SkinVertexWeights,
    decode_internal_skinned_glb,
)
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.core.domain.animation_clip import (
    ANIMATION_CLIP_SCHEMA_VERSION,
    AuthoredAnimationClip,
    BoneAnimationTrack,
    RotationKeyframe,
    animation_clip_to_canonical_document,
    canonical_animation_clip_bytes,
    parse_animation_clip_document,
)
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract


def _identity_rotation() -> list[float]:
    return [0.0, 0.0, 0.0, 1.0]


def _valid_clip_document(
    *,
    clip_id: str = "IdleA",
    duration_seconds: float = 1.0,
    loop: bool = False,
    bones: list[str] | None = None,
    extra_root: dict[str, object] | None = None,
) -> dict[str, object]:
    target_bones = bones or ["Spine"]
    tracks = [
        {
            "bone": bone,
            "keyframes": [{"time": 0.0, "rotation_xyzw": _identity_rotation()}],
        }
        for bone in target_bones
    ]
    doc: dict[str, object] = {
        "schema_version": ANIMATION_CLIP_SCHEMA_VERSION,
        "clip_id": clip_id,
        "duration_seconds": duration_seconds,
        "loop": loop,
        "tracks": tracks,
    }
    if extra_root:
        doc.update(extra_root)
    return doc


def _write_clip(tmp_path: Path, document: dict[str, object], *, name: str = "clip.json") -> Path:
    path = tmp_path / name
    path.write_bytes(json.dumps(document).encode("utf-8"))
    return path


def _keyframes_spread(count: int, duration_seconds: float) -> list[dict[str, object]]:
    if count < 1:
        return []
    if count == 1:
        return [{"time": 0.0, "rotation_xyzw": _identity_rotation()}]
    step = duration_seconds / (count - 1)
    return [{"time": step * index, "rotation_xyzw": _identity_rotation()} for index in range(count)]


def _decode_positive_fixture(tmp_path: Path):
    glb_path = tmp_path / "positive.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    return decode_internal_skinned_glb(glb_path)


def test_parse_valid_single_and_multiple_tracks() -> None:
    single = parse_animation_clip_document(_valid_clip_document(bones=["Spine"]))
    assert single.clip_id == "IdleA"
    assert len(single.tracks) == 1
    assert single.tracks[0].bone == "Spine"

    multi = parse_animation_clip_document(
        _valid_clip_document(bones=["Spine", "LeftUpperArm", "Head"])
    )
    assert [track.bone for track in multi.tracks] == ["Spine", "LeftUpperArm", "Head"]


def test_parse_preserves_supplied_quaternion_components() -> None:
    raw = _valid_clip_document()
    raw["tracks"][0]["keyframes"][0]["rotation_xyzw"] = [
        0.0,
        0.0,
        0.0,
        1.000001,
    ]
    clip = parse_animation_clip_document(raw)
    assert clip.tracks[0].keyframes[0].rotation_xyzw == (0.0, 0.0, 0.0, 1.000001)


def test_parse_rejects_unknown_and_policy_fields() -> None:
    with pytest.raises(ValidationError, match="unknown fields"):
        parse_animation_clip_document(
            _valid_clip_document(extra_root={"rootmotion": True, "eligibility": "ready"})
        )
    doc = _valid_clip_document()
    doc["tracks"][0]["position_xyz"] = [0, 0, 0]
    with pytest.raises(ValidationError, match="unknown fields"):
        parse_animation_clip_document(doc)


def test_parse_rejects_bool_numeric_and_nonfinite() -> None:
    doc = _valid_clip_document()
    doc["duration_seconds"] = True
    with pytest.raises(ValidationError, match="duration_seconds"):
        parse_animation_clip_document(doc)

    doc = _valid_clip_document()
    doc["tracks"][0]["keyframes"][0]["time"] = "0"
    with pytest.raises(ValidationError, match="time"):
        parse_animation_clip_document(doc)

    doc = _valid_clip_document()
    doc["tracks"][0]["keyframes"][0]["rotation_xyzw"] = [0, 0, 0, float("nan")]
    with pytest.raises(ValidationError, match="rotation_xyzw"):
        parse_animation_clip_document(doc)


def test_parse_duration_track_and_key_limits() -> None:
    with pytest.raises(ValidationError, match="duration_seconds"):
        parse_animation_clip_document(_valid_clip_document(duration_seconds=0.0))
    with pytest.raises(ValidationError, match="duration_seconds"):
        parse_animation_clip_document(_valid_clip_document(duration_seconds=10.1))

    with pytest.raises(ValidationError, match="tracks must contain"):
        parse_animation_clip_document({**_valid_clip_document(), "tracks": []})

    too_many = _valid_clip_document(
        bones=[
            "Spine",
            "Chest",
            "Neck",
            "Head",
            "LeftUpperArm",
            "LeftLowerArm",
            "LeftHand",
            "RightUpperArm",
            "RightLowerArm",
        ]
    )
    with pytest.raises(ValidationError, match="tracks must contain"):
        parse_animation_clip_document(too_many)

    doc = _valid_clip_document()
    doc["tracks"][0]["keyframes"] = []
    with pytest.raises(ValidationError, match="keyframes must contain"):
        parse_animation_clip_document(doc)


def test_parse_rejects_duplicate_bones_and_non_increasing_times() -> None:
    with pytest.raises(ValidationError, match="duplicate bone"):
        parse_animation_clip_document(_valid_clip_document(bones=["Spine", "Spine"]))

    doc = _valid_clip_document()
    doc["tracks"][0]["keyframes"] = [
        {"time": 0.5, "rotation_xyzw": _identity_rotation()},
        {"time": 0.5, "rotation_xyzw": _identity_rotation()},
    ]
    with pytest.raises(ValidationError, match="strictly increasing"):
        parse_animation_clip_document(doc)


def test_parse_clip_id_and_forbidden_root_bones() -> None:
    with pytest.raises(ValidationError, match="clip_id"):
        parse_animation_clip_document(_valid_clip_document(clip_id="1bad"))
    with pytest.raises(ValidationError, match="clip_id"):
        parse_animation_clip_document(_valid_clip_document(clip_id="clip/path"))

    con_clip = parse_animation_clip_document(_valid_clip_document(clip_id="CON"))
    assert con_clip.clip_id == "CON"

    with pytest.raises(ValidationError, match="forbidden root"):
        parse_animation_clip_document(_valid_clip_document(bones=["HumanoidRoot"]))


def test_parse_loop_must_be_actual_bool() -> None:
    doc = _valid_clip_document(loop=True)
    assert parse_animation_clip_document(doc).loop is True
    assert parse_animation_clip_document(_valid_clip_document(loop=False)).loop is False
    doc = _valid_clip_document()
    doc["loop"] = 0
    with pytest.raises(ValidationError, match="loop must be a boolean"):
        parse_animation_clip_document(doc)


def test_parse_duration_ten_and_keyframe_time_bounds() -> None:
    at_max = _valid_clip_document(duration_seconds=10.0)
    at_max["tracks"][0]["keyframes"] = _keyframes_spread(2, 10.0)
    clip = parse_animation_clip_document(at_max)
    assert clip.duration_seconds == 10.0
    assert clip.tracks[0].keyframes[-1].time == 10.0

    doc = _valid_clip_document(duration_seconds=1.0)
    doc["tracks"][0]["keyframes"] = [
        {"time": 1.5, "rotation_xyzw": _identity_rotation()},
    ]
    with pytest.raises(ValidationError, match="within"):
        parse_animation_clip_document(doc)

    doc = _valid_clip_document()
    doc["tracks"][0]["keyframes"] = [
        {"time": 0.5, "rotation_xyzw": _identity_rotation()},
        {"time": 0.25, "rotation_xyzw": _identity_rotation()},
    ]
    with pytest.raises(ValidationError, match="strictly increasing"):
        parse_animation_clip_document(doc)


def test_parse_keyframe_and_track_count_boundaries() -> None:
    doc = _valid_clip_document()
    doc["tracks"][0]["keyframes"] = _keyframes_spread(64, 1.0)
    parse_animation_clip_document(doc)

    doc = _valid_clip_document()
    doc["tracks"][0]["keyframes"] = _keyframes_spread(65, 10.0)
    with pytest.raises(ValidationError, match="keyframes must contain"):
        parse_animation_clip_document(doc)

    eight_bones = list(load_internal_skin_contract().bone_names[:8])
    eight_track_doc = _valid_clip_document(bones=eight_bones, duration_seconds=10.0)
    for index, _bone in enumerate(eight_bones):
        eight_track_doc["tracks"][index]["keyframes"] = _keyframes_spread(64, 10.0)
    parse_animation_clip_document(eight_track_doc)


def test_parse_rejects_missing_nested_and_unknown_keyframe_fields() -> None:
    doc = _valid_clip_document()
    del doc["tracks"][0]["bone"]
    with pytest.raises(ValidationError, match="bone"):
        parse_animation_clip_document(doc)

    doc = _valid_clip_document()
    doc["tracks"][0]["keyframes"][0]["ease"] = "linear"
    with pytest.raises(ValidationError, match="unknown fields"):
        parse_animation_clip_document(doc)


def test_parse_multiple_tracks_with_interior_keyframe_times() -> None:
    doc = _valid_clip_document(bones=["Spine", "LeftUpperArm"], duration_seconds=2.0)
    doc["tracks"][0]["keyframes"] = _keyframes_spread(3, 2.0)
    doc["tracks"][1]["keyframes"] = [
        {"time": 0.0, "rotation_xyzw": _identity_rotation()},
        {"time": 0.75, "rotation_xyzw": [0.0, 0.0, 0.0, 1.000001]},
        {"time": 2.0, "rotation_xyzw": _identity_rotation()},
    ]
    clip = parse_animation_clip_document(doc)
    assert clip.tracks[1].keyframes[1].time == 0.75


def test_parse_quaternion_normalization_tolerance() -> None:
    almost_unit = _valid_clip_document()
    almost_unit["tracks"][0]["keyframes"][0]["rotation_xyzw"] = [0.0, 0.0, 0.0, 1.000009]
    parse_animation_clip_document(almost_unit)

    bad = _valid_clip_document()
    bad["tracks"][0]["keyframes"][0]["rotation_xyzw"] = [0.0, 0.0, 0.0, 1.001]
    with pytest.raises(ValidationError, match="unit length"):
        parse_animation_clip_document(bad)

    zero = _valid_clip_document()
    zero["tracks"][0]["keyframes"][0]["rotation_xyzw"] = [0.0, 0.0, 0.0, 0.0]
    with pytest.raises(ValidationError, match="non-zero"):
        parse_animation_clip_document(zero)


def test_canonical_document_bytes_are_deterministic() -> None:
    clip = parse_animation_clip_document(_valid_clip_document(bones=["Spine", "LeftUpperArm"]))
    doc_a = animation_clip_to_canonical_document(clip)
    doc_b = animation_clip_to_canonical_document(clip)
    assert doc_a == doc_b
    assert canonical_animation_clip_bytes(clip) == canonical_animation_clip_bytes(clip)


def test_load_raw_sha256_and_whitespace_canonical_stability(tmp_path: Path) -> None:
    compact = _write_clip(tmp_path, _valid_clip_document(bones=["Spine"]), name="compact.json")
    spaced = tmp_path / "spaced.json"
    spaced.write_text(
        json.dumps(_valid_clip_document(bones=["Spine"]), indent=2),
        encoding="utf-8",
    )
    loaded_compact = load_local_animation_clip(compact)
    loaded_spaced = load_local_animation_clip(spaced)
    assert loaded_compact.sha256 == hashlib.sha256(compact.read_bytes()).hexdigest()
    assert loaded_spaced.sha256 != loaded_compact.sha256
    assert canonical_animation_clip_bytes(loaded_compact.clip) == canonical_animation_clip_bytes(
        loaded_spaced.clip
    )


def test_load_local_animation_clip_round_trip(tmp_path: Path) -> None:
    raw_doc = _valid_clip_document(bones=["LeftUpperArm"])
    path = _write_clip(tmp_path, raw_doc)
    loaded = load_local_animation_clip(path)
    assert loaded.raw_bytes == path.read_bytes()
    assert loaded.clip.tracks[0].bone == "LeftUpperArm"
    assert len(loaded.sha256) == 64


def test_load_rejects_duplicate_json_keys_and_nonfinite_constants(tmp_path: Path) -> None:
    dup_path = tmp_path / "dup.json"
    dup_path.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
    with pytest.raises(ValidationError, match="JSON policy"):
        load_local_animation_clip(dup_path)

    nested_dup = tmp_path / "nested_dup.json"
    nested_dup.write_text('{"outer": {"k": 1, "k": 2}}', encoding="utf-8")
    with pytest.raises(ValidationError, match="JSON policy"):
        load_local_animation_clip(nested_dup)

    nan_path = tmp_path / "nan.json"
    nan_path.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(ValidationError, match="JSON policy"):
        load_local_animation_clip(nan_path)

    root_array = tmp_path / "array.json"
    root_array.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValidationError, match="JSON policy"):
        load_local_animation_clip(root_array)

    utf8_path = tmp_path / "bad_utf8.json"
    utf8_path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValidationError, match="JSON policy"):
        load_local_animation_clip(utf8_path)

    huge_int = tmp_path / "huge_int.json"
    huge_int.write_text('{"n": ' + "9" * 5000 + "}", encoding="utf-8")
    with pytest.raises(ValidationError, match="JSON policy"):
        load_local_animation_clip(huge_int)

    overflow_duration = tmp_path / "overflow.json"
    overflow_duration.write_text(
        json.dumps(_valid_clip_document()).replace(
            '"duration_seconds": 1.0', '"duration_seconds": 1e999'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="finite number"):
        load_local_animation_clip(overflow_duration)


def test_load_rejects_deep_json(tmp_path: Path) -> None:
    depth = 3000
    payload = '{"x":' * depth + "0" + "}" * depth
    assert len(payload.encode("utf-8")) < MAX_LOCAL_ANIMATION_CLIP_BYTES
    deep_path = tmp_path / "deep.json"
    deep_path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValidationError, match="JSON policy"):
        load_local_animation_clip(deep_path)


def _nested_object_json(depth: int) -> str:
    payload = "0"
    for _ in range(depth):
        payload = '{"x":' + payload + "}"
    return payload


def _nested_mixed_container_json(depth: int) -> str:
    payload = "0"
    for level in range(depth):
        if level % 2 == 0:
            payload = "[" + payload + "]"
        else:
            payload = '{"x":' + payload + "}"
    return payload


def test_load_rejects_deep_json_with_raised_recursion_limit(tmp_path: Path) -> None:
    depth = 3000
    payload = '{"x":' * depth + "0" + "}" * depth
    deep_path = tmp_path / "deep_recursion.json"
    deep_path.write_text(payload, encoding="utf-8")
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, depth * 2))
    try:
        with pytest.raises(ValidationError, match="JSON policy"):
            load_local_animation_clip(deep_path)
    finally:
        sys.setrecursionlimit(old_limit)


def test_load_json_container_depth_boundary_and_one_over(tmp_path: Path) -> None:
    limit = MAX_LOCAL_ANIMATION_CLIP_JSON_CONTAINER_DEPTH
    at_limit = tmp_path / "depth_at_limit.json"
    at_limit.write_text(_nested_object_json(limit), encoding="utf-8")
    with pytest.raises(ValidationError) as at_limit_exc:
        load_local_animation_clip(at_limit)
    assert "maximum depth" not in at_limit_exc.value.message

    one_over = tmp_path / "depth_one_over.json"
    one_over.write_text(_nested_object_json(limit + 1), encoding="utf-8")
    with pytest.raises(ValidationError, match="JSON policy"):
        load_local_animation_clip(one_over)

    mixed_at_limit_payload = _nested_mixed_container_json(limit)
    json.loads(mixed_at_limit_payload)
    mixed_at_limit = tmp_path / "mixed_depth_at_limit.json"
    mixed_at_limit.write_text(mixed_at_limit_payload, encoding="utf-8")
    with pytest.raises(ValidationError) as mixed_at_limit_exc:
        load_local_animation_clip(mixed_at_limit)
    assert "maximum depth" not in mixed_at_limit_exc.value.message

    mixed_over_payload = _nested_mixed_container_json(limit + 1)
    json.loads(mixed_over_payload)
    mixed_over = tmp_path / "mixed_depth_one_over.json"
    mixed_over.write_text(mixed_over_payload, encoding="utf-8")
    with pytest.raises(ValidationError, match="maximum depth"):
        load_local_animation_clip(mixed_over)


def test_load_json_depth_ignores_quotes_and_escapes(tmp_path: Path) -> None:
    decorative = "{" * 80 + "[" * 40
    escaped = r"line \" quote \\ tail"
    payload = '{"note": "' + decorative + escaped + '", "inner": {"v": 1}, "tail": "}]}"}'
    json.loads(payload)
    path = tmp_path / "quoted_braces.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValidationError, match="unknown fields") as exc_info:
        load_local_animation_clip(path)
    assert "maximum depth" not in exc_info.value.message


def test_load_valid_clip_bytes_and_sha_unchanged_by_depth_preflight(tmp_path: Path) -> None:
    path = _write_clip(tmp_path, _valid_clip_document(bones=["Spine"]), name="preflight.json")
    raw_bytes = path.read_bytes()
    loaded = load_local_animation_clip(path)
    assert loaded.raw_bytes == raw_bytes
    assert loaded.sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert loaded.clip.tracks[0].bone == "Spine"


def test_load_rejects_oversize_file(tmp_path: Path) -> None:
    big = tmp_path / "big.json"
    big.write_bytes(b"x" * (MAX_LOCAL_ANIMATION_CLIP_BYTES + 1))
    with pytest.raises(ValidationError, match="64KiB"):
        load_local_animation_clip(big)


def test_load_exact_64kib_boundary_and_rejects_one_byte_over(tmp_path: Path) -> None:
    doc = _valid_clip_document(bones=["Spine"])
    base = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    pad_len = MAX_LOCAL_ANIMATION_CLIP_BYTES - len(base)
    assert pad_len >= 0
    exact = tmp_path / "exact.json"
    exact.write_bytes(base + (b" " * pad_len))
    assert exact.stat().st_size == MAX_LOCAL_ANIMATION_CLIP_BYTES
    loaded = load_local_animation_clip(exact)
    assert loaded.clip.tracks[0].bone == "Spine"

    over = tmp_path / "over.json"
    over.write_bytes(exact.read_bytes() + b"x")
    with pytest.raises(ValidationError, match="64KiB"):
        load_local_animation_clip(over)


def test_load_rejects_missing_directory_and_unreadable_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ValidationError, match="not a regular file|cannot be read"):
        load_local_animation_clip(missing)

    with pytest.raises(ValidationError, match="not a regular file"):
        load_local_animation_clip(tmp_path)


def test_load_rejects_path_crossing_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write_clip(tmp_path, _valid_clip_document())
    import gamefactory.adapters.assets.animation_clip_input as clip_input

    monkeypatch.setattr(clip_input, "path_crosses_link", lambda _path: True)
    with pytest.raises(ValidationError, match="link, junction, or reparse"):
        load_local_animation_clip(path)


def test_load_rejects_growth_after_fstat_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    path = tmp_path / "growth.json"
    path.write_bytes(b"{" + b"x" * 128 + b"}")
    real_fstat = os.fstat

    def fake_fstat(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        return os.stat_result(
            (
                info.st_mode,
                info.st_ino,
                info.st_dev,
                info.st_nlink,
                info.st_uid,
                info.st_gid,
                2,
                info.st_atime,
                info.st_mtime,
                info.st_ctime,
            )
        )

    monkeypatch.setattr(os, "fstat", fake_fstat)
    with pytest.raises(ValidationError, match="size changed"):
        load_local_animation_clip(path)


def test_load_rejects_growth_via_actual_write_after_fstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "grow_on_read.json"
    path.write_bytes(json.dumps(_valid_clip_document()).encode("utf-8"))
    original_open = Path.open

    def opening(self: Path, *args: object, **kwargs: object):
        handle = original_open(self, *args, **kwargs)
        if self == path and args == ("rb",):
            real_read = handle.read

            def read(size: int = -1) -> bytes:
                with original_open(path, "ab") as grow:
                    grow.write(b"x" * (MAX_LOCAL_ANIMATION_CLIP_BYTES + 1))
                return real_read(size)

            handle.read = read  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr(Path, "open", opening)
    with pytest.raises(ValidationError, match="size changed|64KiB"):
        load_local_animation_clip(path)


def test_load_rejects_unc_path_string(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unsafe|UNC"):
        load_local_animation_clip(Path(r"\\server\share\clip.json"))


def test_skin_influence_positive_fixture(tmp_path: Path) -> None:
    decoded = _decode_positive_fixture(tmp_path)
    clip = parse_animation_clip_document(_valid_clip_document(bones=["LeftUpperArm"]))
    assert_animation_clip_skin_influence(clip, decoded)

    spine_clip = parse_animation_clip_document(_valid_clip_document(bones=["Spine"]))
    assert_animation_clip_skin_influence(spine_clip, decoded)


def test_skin_influence_rejects_unweighted_missing_and_root(tmp_path: Path) -> None:
    decoded = _decode_positive_fixture(tmp_path)
    unweighted = parse_animation_clip_document(_valid_clip_document(bones=["RightUpperArm"]))
    with pytest.raises(ValidationError, match="no positive finite vertex weight"):
        assert_animation_clip_skin_influence(unweighted, decoded)

    root_clip = AuthoredAnimationClip(
        schema_version=ANIMATION_CLIP_SCHEMA_VERSION,
        clip_id="Rooted",
        duration_seconds=1.0,
        loop=False,
        tracks=(
            BoneAnimationTrack(
                bone="HumanoidRoot",
                keyframes=(RotationKeyframe(0.0, (0.0, 0.0, 0.0, 1.0)),),
            ),
        ),
    )
    with pytest.raises(ValidationError, match="forbidden"):
        assert_animation_clip_skin_influence(root_clip, decoded)

    doc = _valid_clip_document(bones=["NotABone"])
    with pytest.raises(ValidationError, match="not in the internal skin contract"):
        parse_animation_clip_document(doc)

    bad_track_clip = AuthoredAnimationClip(
        schema_version=ANIMATION_CLIP_SCHEMA_VERSION,
        clip_id="Evil",
        duration_seconds=1.0,
        loop=False,
        tracks=(
            BoneAnimationTrack(
                bone="RightUpperArm",
                keyframes=(RotationKeyframe(0.0, (0.0, 0.0, 0.0, 1.0)),),
            ),
        ),
    )
    with pytest.raises(ValidationError, match="no positive finite vertex weight"):
        assert_animation_clip_skin_influence(bad_track_clip, decoded)


def _joint_names_for_decoded(decoded: DecodedInternalSkinnedGLB) -> tuple[str, ...]:
    nodes = decoded.document["nodes"]
    names: list[str] = []
    for node_index in decoded.joint_node_indices:
        node = nodes[node_index]
        names.append(node["name"])
    return tuple(names)


def _swap_joint_slots_with_vertex_remap(
    decoded: DecodedInternalSkinnedGLB, slot_a: int, slot_b: int
) -> DecodedInternalSkinnedGLB:
    joint_indices = list(decoded.joint_node_indices)
    joint_indices[slot_a], joint_indices[slot_b] = joint_indices[slot_b], joint_indices[slot_a]

    def remap_slot(slot: int) -> int:
        if slot == slot_a:
            return slot_b
        if slot == slot_b:
            return slot_a
        return slot

    remapped_vertices = tuple(
        SkinVertexWeights(
            joints=tuple(remap_slot(j) for j in vertex.joints),
            weights=vertex.weights,
        )
        for vertex in decoded.primitive.vertex_weights
    )
    new_primitive = replace(decoded.primitive, vertex_weights=remapped_vertices)
    return replace(decoded, joint_node_indices=tuple(joint_indices), primitive=new_primitive)


def test_skin_influence_maps_reordered_joint_slots_to_weight_indices(tmp_path: Path) -> None:
    decoded = _decode_positive_fixture(tmp_path)
    joint_names = _joint_names_for_decoded(decoded)
    lua_slot = joint_names.index("LeftUpperArm")
    rua_slot = joint_names.index("RightUpperArm")
    remapped_decoded = _swap_joint_slots_with_vertex_remap(decoded, lua_slot, rua_slot)

    lua_clip = parse_animation_clip_document(_valid_clip_document(bones=["LeftUpperArm"]))
    assert_animation_clip_skin_influence(lua_clip, remapped_decoded)

    rua_clip = parse_animation_clip_document(_valid_clip_document(bones=["RightUpperArm"]))
    with pytest.raises(ValidationError, match="no positive finite vertex weight"):
        assert_animation_clip_skin_influence(rua_clip, remapped_decoded)


def test_skin_influence_rejects_duplicate_and_renamed_joint_names(tmp_path: Path) -> None:
    decoded = _decode_positive_fixture(tmp_path)
    doc = copy.deepcopy(decoded.document)
    nodes = doc["nodes"]
    spine_index = decoded.joint_node_indices[1]
    nodes[spine_index]["name"] = nodes[decoded.joint_node_indices[0]]["name"]
    duplicate_decoded = replace(decoded, document=doc)
    clip = parse_animation_clip_document(_valid_clip_document(bones=["Spine"]))
    with pytest.raises(ValidationError, match="joint names must be unique"):
        assert_animation_clip_skin_influence(clip, duplicate_decoded)

    doc2 = copy.deepcopy(decoded.document)
    for node in doc2["nodes"]:
        if node.get("name") == "Head":
            node["name"] = "NotInContract"
            break
    renamed_decoded = replace(decoded, document=doc2)
    with pytest.raises(ValidationError, match="must match the internal skin contract"):
        assert_animation_clip_skin_influence(clip, renamed_decoded)
