"""Closed rig-animation-clip-0.8.0 domain model and pure document parser (V0.8-6)."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract

ANIMATION_CLIP_SCHEMA_VERSION = "rig-animation-clip-0.8.0"

_CLIP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_TOP_LEVEL_KEYS = frozenset({"schema_version", "clip_id", "duration_seconds", "loop", "tracks"})
_TRACK_KEYS = frozenset({"bone", "keyframes"})
_KEYFRAME_KEYS = frozenset({"time", "rotation_xyzw"})

_MIN_DURATION_SECONDS = 0.0
_MAX_DURATION_SECONDS = 10.0
_MIN_TRACKS = 1
_MAX_TRACKS = 8
_MIN_KEYFRAMES_PER_TRACK = 1
_MAX_KEYFRAMES_PER_TRACK = 64
_QUATERNION_UNIT_TOLERANCE = 1e-5


@dataclass(frozen=True)
class RotationKeyframe:
    time: float
    rotation_xyzw: tuple[float, float, float, float]


@dataclass(frozen=True)
class BoneAnimationTrack:
    bone: str
    keyframes: tuple[RotationKeyframe, ...]


@dataclass(frozen=True)
class AuthoredAnimationClip:
    schema_version: str
    clip_id: str
    duration_seconds: float
    loop: bool
    tracks: tuple[BoneAnimationTrack, ...]


def _reject_unknown_keys(data: dict[str, Any], allowed: frozenset[str], *, label: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ValidationError(f"{label} has unknown fields: {sorted(unknown)}")


def _require_actual_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValidationError(f"{field} must be a boolean")
    return value


def _require_finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a finite number")
    try:
        out = float(value)
    except OverflowError as exc:
        raise ValidationError(f"{field} must be a finite number") from exc
    if not math.isfinite(out):
        raise ValidationError(f"{field} must be a finite number")
    return out


def _validate_clip_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("clip_id must be a string")
    if not _CLIP_ID_RE.fullmatch(value):
        raise ValidationError("clip_id must match [A-Za-z][A-Za-z0-9_]{0,63}")
    return value


def _validate_rotation_xyzw(raw: Any, field: str) -> tuple[float, float, float, float]:
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValidationError(f"{field} must be a length-4 array")
    components: list[float] = []
    for index, entry in enumerate(raw):
        components.append(_require_finite_number(entry, f"{field}[{index}]"))
    norm_sq = sum(c * c for c in components)
    if norm_sq <= 0.0:
        raise ValidationError(f"{field} must be a non-zero quaternion")
    norm = math.sqrt(norm_sq)
    if abs(norm - 1.0) > _QUATERNION_UNIT_TOLERANCE:
        raise ValidationError(f"{field} must be unit length within {_QUATERNION_UNIT_TOLERANCE}")
    return (components[0], components[1], components[2], components[3])


def _parse_track(
    raw: Any,
    *,
    duration_seconds: float,
    allowed_bones: frozenset[str],
    forbidden_bones: frozenset[str],
    label: str,
) -> BoneAnimationTrack:
    if not isinstance(raw, dict):
        raise ValidationError(f"{label} must be an object")
    _reject_unknown_keys(raw, _TRACK_KEYS, label=label)
    bone = raw.get("bone")
    if not isinstance(bone, str) or not bone:
        raise ValidationError(f"{label}.bone must be a non-empty string")
    if bone in forbidden_bones:
        raise ValidationError(f"{label}.bone must not target forbidden root bone {bone}")
    if bone not in allowed_bones:
        raise ValidationError(f"{label}.bone {bone} is not in the internal skin contract")
    keyframes_raw = raw.get("keyframes")
    if not isinstance(keyframes_raw, list):
        raise ValidationError(f"{label}.keyframes must be an array")
    if not (_MIN_KEYFRAMES_PER_TRACK <= len(keyframes_raw) <= _MAX_KEYFRAMES_PER_TRACK):
        raise ValidationError(
            f"{label}.keyframes must contain between "
            f"{_MIN_KEYFRAMES_PER_TRACK} and {_MAX_KEYFRAMES_PER_TRACK} entries"
        )
    keyframes: list[RotationKeyframe] = []
    previous_time: float | None = None
    for index, entry in enumerate(keyframes_raw):
        key_label = f"{label}.keyframes[{index}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{key_label} must be an object")
        _reject_unknown_keys(entry, _KEYFRAME_KEYS, label=key_label)
        time = _require_finite_number(entry.get("time"), f"{key_label}.time")
        if time < 0.0 or time > duration_seconds:
            raise ValidationError(f"{key_label}.time must be within [0, duration_seconds]")
        if previous_time is not None and time <= previous_time:
            raise ValidationError(f"{key_label}.time must be strictly increasing")
        rotation = _validate_rotation_xyzw(entry.get("rotation_xyzw"), f"{key_label}.rotation_xyzw")
        keyframes.append(RotationKeyframe(time=time, rotation_xyzw=rotation))
        previous_time = time
    return BoneAnimationTrack(bone=bone, keyframes=tuple(keyframes))


def parse_animation_clip_document(document: dict[str, Any]) -> AuthoredAnimationClip:
    """Parse and validate a closed rig-animation-clip-0.8.0 document."""
    if not isinstance(document, dict):
        raise ValidationError("animation clip document must be a JSON object")
    _reject_unknown_keys(document, _TOP_LEVEL_KEYS, label="animation clip")
    schema_version = document.get("schema_version")
    if schema_version != ANIMATION_CLIP_SCHEMA_VERSION:
        raise ValidationError(
            f"schema_version must be {ANIMATION_CLIP_SCHEMA_VERSION!r}, got {schema_version!r}"
        )
    clip_id = _validate_clip_id(document.get("clip_id"))
    duration_seconds = _require_finite_number(document.get("duration_seconds"), "duration_seconds")
    if duration_seconds <= _MIN_DURATION_SECONDS or duration_seconds > _MAX_DURATION_SECONDS:
        raise ValidationError("duration_seconds must be > 0 and <= 10")
    loop = _require_actual_bool(document.get("loop"), "loop")
    tracks_raw = document.get("tracks")
    if not isinstance(tracks_raw, list):
        raise ValidationError("tracks must be an array")
    if not (_MIN_TRACKS <= len(tracks_raw) <= _MAX_TRACKS):
        raise ValidationError(
            f"tracks must contain between {_MIN_TRACKS} and {_MAX_TRACKS} entries"
        )

    contract = load_internal_skin_contract()
    allowed_bones = frozenset(contract.bone_names)
    forbidden_bones = frozenset({contract.asset_root_name, "HumanoidRoot"})

    tracks: list[BoneAnimationTrack] = []
    seen_bones: set[str] = set()
    for index, entry in enumerate(tracks_raw):
        track = _parse_track(
            entry,
            duration_seconds=duration_seconds,
            allowed_bones=allowed_bones,
            forbidden_bones=forbidden_bones,
            label=f"tracks[{index}]",
        )
        if track.bone in seen_bones:
            raise ValidationError(f"duplicate bone track: {track.bone}")
        seen_bones.add(track.bone)
        tracks.append(track)

    return AuthoredAnimationClip(
        schema_version=ANIMATION_CLIP_SCHEMA_VERSION,
        clip_id=clip_id,
        duration_seconds=duration_seconds,
        loop=loop,
        tracks=tuple(tracks),
    )


def animation_clip_to_canonical_document(clip: AuthoredAnimationClip) -> dict[str, Any]:
    """Deterministic JSON-serializable document for the parsed clip."""
    return {
        "schema_version": clip.schema_version,
        "clip_id": clip.clip_id,
        "duration_seconds": clip.duration_seconds,
        "loop": clip.loop,
        "tracks": [
            {
                "bone": track.bone,
                "keyframes": [
                    {
                        "time": keyframe.time,
                        "rotation_xyzw": list(keyframe.rotation_xyzw),
                    }
                    for keyframe in track.keyframes
                ],
            }
            for track in clip.tracks
        ],
    }


def canonical_animation_clip_bytes(clip: AuthoredAnimationClip) -> bytes:
    """Canonical UTF-8 bytes for deterministic digests (distinct from source bytes)."""
    payload = animation_clip_to_canonical_document(clip)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )
