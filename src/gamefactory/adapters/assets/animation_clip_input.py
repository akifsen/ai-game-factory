"""Local authored animation clip ingest and skin influence checks (V0.8-6)."""

from __future__ import annotations

import hashlib
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from gamefactory.adapters.assets.internal_skin_decode import DecodedInternalSkinnedGLB
from gamefactory.adapters.assets.v08_candidate_runtime_json import (
    CandidateRuntimeJsonError,
    parse_strict_runtime_json_object,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.core.domain.animation_clip import (
    AuthoredAnimationClip,
    parse_animation_clip_document,
)
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract
from gamefactory.core.execution.path_guard import PathGuard

MAX_LOCAL_ANIMATION_CLIP_BYTES = 64 * 1024
MAX_LOCAL_ANIMATION_CLIP_JSON_CONTAINER_DEPTH = 32


@dataclass(frozen=True)
class LoadedLocalAnimationClip:
    raw_bytes: bytes
    sha256: str
    clip: AuthoredAnimationClip


def _reject_unsafe_path_lexical(path: Path) -> None:
    raw = str(path)
    guard = PathGuard(Path.cwd().resolve())
    try:
        guard._validate_raw(raw)
    except ValidationError as exc:
        raise ValidationError(f"animation clip path is unsafe: {exc.message}") from exc
    if path_crosses_link(path.absolute()):
        raise ValidationError("animation clip path crosses a link, junction, or reparse point")


def _read_bounded_regular_file(path: Path) -> bytes:
    _reject_unsafe_path_lexical(path)
    limit = MAX_LOCAL_ANIMATION_CLIP_BYTES
    try:
        if not path.is_file():
            raise ValidationError(f"animation clip is not a regular file: {path}")
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValidationError(f"animation clip is not a regular file: {path}")
            if info.st_size > limit:
                raise ValidationError("animation clip exceeds the 64KiB byte limit")
            payload = stream.read(limit + 1)
    except OSError as exc:
        raise ValidationError(f"animation clip cannot be read: {path}") from exc
    if len(payload) > limit:
        raise ValidationError("animation clip exceeds the 64KiB byte limit")
    if len(payload) != info.st_size:
        raise ValidationError("animation clip size changed during bounded read")
    _reject_unsafe_path_lexical(path)
    return payload


def _reject_excessive_json_container_depth(raw: bytes) -> None:
    """Linear preflight: reject deeply nested containers before json.loads."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return
    limit = MAX_LOCAL_ANIMATION_CLIP_JSON_CONTAINER_DEPTH
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
                raise ValidationError(
                    "animation clip JSON policy violation: "
                    f"JSON container nesting exceeds maximum depth {limit}"
                )
        elif char in "}]":
            depth -= 1
        index += 1


def load_local_animation_clip(path: Path) -> LoadedLocalAnimationClip:
    """Load a local JSON animation clip with bounded IO and closed-schema validation."""
    raw = _read_bounded_regular_file(path)
    _reject_excessive_json_container_depth(raw)
    try:
        document = parse_strict_runtime_json_object(raw, max_bytes=MAX_LOCAL_ANIMATION_CLIP_BYTES)
    except CandidateRuntimeJsonError as exc:
        raise ValidationError(f"animation clip JSON policy violation: {exc}") from exc
    except (RecursionError, ValueError) as exc:
        raise ValidationError(f"animation clip JSON policy violation: {exc}") from exc
    clip = parse_animation_clip_document(document)
    return LoadedLocalAnimationClip(
        raw_bytes=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        clip=clip,
    )


def _decoded_contract_joint_names(decoded: DecodedInternalSkinnedGLB) -> tuple[str, ...]:
    nodes = decoded.document.get("nodes")
    if not isinstance(nodes, list):
        raise ValidationError("decoded GLB nodes must be an array")
    names: list[str] = []
    for node_index in decoded.joint_node_indices:
        if node_index < 0 or node_index >= len(nodes):
            raise ValidationError("decoded GLB joint node index out of range")
        node = nodes[node_index]
        if not isinstance(node, dict):
            raise ValidationError("decoded GLB joint node must be an object")
        name = node.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError("decoded GLB joint node name must be a non-empty string")
        names.append(name)
    return tuple(names)


def _bone_has_positive_finite_weight(decoded: DecodedInternalSkinnedGLB, joint_slot: int) -> bool:
    for vertex in decoded.primitive.vertex_weights:
        for slot_index, weight in zip(vertex.joints, vertex.weights, strict=True):
            if slot_index == joint_slot and weight > 0.0 and math.isfinite(weight):
                return True
    return False


def assert_animation_clip_skin_influence(
    clip: AuthoredAnimationClip, decoded: DecodedInternalSkinnedGLB
) -> None:
    """Ensure each animated bone is positively weighted in the decoded skin mesh."""
    contract = load_internal_skin_contract()
    forbidden = frozenset({contract.asset_root_name, "HumanoidRoot"})
    expected_names = frozenset(contract.bone_names)

    joint_names = _decoded_contract_joint_names(decoded)
    if len(joint_names) != len(expected_names):
        raise ValidationError(
            "decoded GLB must expose exactly 12 internal-skin contract joint names"
        )
    if len(set(joint_names)) != len(joint_names):
        raise ValidationError("decoded GLB joint names must be unique")
    if frozenset(joint_names) != expected_names:
        raise ValidationError("decoded GLB joint names must match the internal skin contract")

    slot_by_name: dict[str, int] = {name: slot for slot, name in enumerate(joint_names)}

    for track in clip.tracks:
        if track.bone in forbidden:
            raise ValidationError(f"animation clip bone {track.bone} is forbidden")
        if track.bone not in expected_names:
            raise ValidationError(f"animation clip bone {track.bone} is not in skin contract")
        joint_slot = slot_by_name[track.bone]
        if not _bone_has_positive_finite_weight(decoded, joint_slot):
            raise ValidationError(
                f"animation clip bone {track.bone} has no positive finite vertex weight"
            )
