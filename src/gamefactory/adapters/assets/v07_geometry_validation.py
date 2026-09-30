"""Closed V0.7 geometry validator for test-only typed profiles.

This module deliberately does not register profiles or enter the production workflow.
It consumes the same bounded GLB parser as the historical validator and never edits geometry.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_validator import (
    _accessor,
    _inspect,
    _InvalidGLB,
    _mat_mul,
    _node_matrix,
    _read_glb_bytes,
)
from gamefactory.core.domain.asset_contracts import (
    AssetSpecificationV07,
    AssetValidationResult,
    ValidationFinding,
    ValidationFindingSeverity,
)
from gamefactory.core.domain.asset_profiles import AssetProfileV07


class V07RuleGroup(StrEnum):
    CORE = "core"
    ASSEMBLY = "parts"
    SOURCE_ORIENTATION = "orientation_source_front"
    PIVOTS = "pivot"
    SOCKETS = "sockets"
    CAPSULE = "collider_capsule"
    SINGLE_MESH = "single_mesh"
    BOX_COLLIDER = "collider_box"


@dataclass(frozen=True)
class VerifiedSourceNormalization:
    """Immutable source/evidence observations supplied by a future verifier.

    Geometry validation alone cannot establish these facts, so callers must provide a
    hash-bound source observation. No default or geometry-based inference is permitted.
    """

    source_sha256: str
    processed_sha256: str
    source_front: str
    normalization_applied: bool
    root_rotation_xyzw: tuple[float, float, float, float]
    source_glb_bytes: bytes


@dataclass(frozen=True)
class GeometryFinding:
    rule_id: str
    passed: bool
    expected: str
    actual: str
    message: str


@dataclass(frozen=True)
class V07GeometryResult:
    passed: bool
    findings: tuple[GeometryFinding, ...]


def selected_v07_groups(
    spec: AssetSpecificationV07, profile: AssetProfileV07
) -> tuple[V07RuleGroup, ...]:
    """Select the closed composition solely from typed geometry/source/collider/socket fields."""
    selected: list[V07RuleGroup] = [V07RuleGroup.CORE]
    if profile.geometry_mode == "assembly":
        selected.append(V07RuleGroup.ASSEMBLY)
    if spec.source_kind == "local_operator_assembly":
        selected.append(V07RuleGroup.SOURCE_ORIENTATION)
    if profile.geometry_mode == "assembly":
        selected.append(V07RuleGroup.PIVOTS)
    if spec.sockets or (profile.assembly is not None and profile.assembly.required_sockets):
        selected.append(V07RuleGroup.SOCKETS)
    if spec.collider.policy == "capsule":
        selected.append(V07RuleGroup.CAPSULE)
    elif spec.collider.policy == "box":
        selected.append(V07RuleGroup.BOX_COLLIDER)
    if profile.geometry_mode == "single_mesh":
        selected.append(V07RuleGroup.SINGLE_MESH)
    return tuple(group for group in V07RuleGroup if group in selected)


def validate_v07_geometry(
    path: Path,
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    *,
    source_observation: VerifiedSourceNormalization | None = None,
    max_file_size_bytes: int = 50 * 1024 * 1024,
) -> V07GeometryResult:
    """Build one frozen decode context, then execute its typed ordered rule plan."""
    from gamefactory.adapters.assets.v07_validation_context import build_v07_validation_context
    from gamefactory.adapters.assets.v07_validation_rules import (
        V07RuleExecutionContext,
        evaluate_ordered_v07_rules,
    )

    if not isinstance(spec, AssetSpecificationV07) or not isinstance(profile, AssetProfileV07):
        raise TypeError("V0.7 geometry validation requires typed specification and profile")
    if spec.bound_profile() != profile:
        raise ValueError("specification and profile are not the same bound typed profile")
    groups = selected_v07_groups(spec, profile)
    context = build_v07_validation_context(
        path, spec, profile, source_observation, max_file_size_bytes
    )
    execution = V07RuleExecutionContext(context, max_file_size_bytes)
    findings = evaluate_ordered_v07_rules(execution, groups)
    return V07GeometryResult(not any(not finding.passed for finding in findings), findings)


def verify_source_to_processed_preservation(
    observation: VerifiedSourceNormalization,
    processed_path: Path,
    spec: AssetSpecificationV07,
    *,
    max_file_size_bytes: int = 50 * 1024 * 1024,
) -> V07GeometryResult:
    """Compare frozen source bytes with one processed GLB without trusting claims as proof.

    This checks the declared normalization transform, part/socket local structure and
    motion metadata, and exact LOD0 triangle soup. It does not authenticate who supplied
    the source bytes or establish that the source itself is semantically correct.
    """
    finding = GeometryFinding(
        "source.preservation",
        False,
        "hash-bound source bytes preserved by one declared normalization",
        "unavailable",
        "Source-to-processed comparison failed",
    )
    try:
        if max_file_size_bytes < 1:
            raise ValueError("maximum GLB size must be positive")
        if not isinstance(observation, VerifiedSourceNormalization):
            raise TypeError("source preservation requires a typed normalization observation")
        if (
            not isinstance(spec, AssetSpecificationV07)
            or spec.source_kind != "local_operator_assembly"
        ):
            raise ValueError("source preservation requires a typed local assembly specification")
        if not processed_path.is_file() or processed_path.suffix.casefold() != ".glb":
            raise ValueError("processed artifact is missing or is not a GLB")
        with processed_path.open("rb") as stream:
            processed_bytes = stream.read(max_file_size_bytes + 1)
        if len(processed_bytes) > max_file_size_bytes:
            raise _InvalidGLB("processed artifact exceeds maximum file size")
        if len(observation.source_glb_bytes) > max_file_size_bytes:
            raise _InvalidGLB("retained source exceeds maximum file size")
        processed_digest = hashlib.sha256(processed_bytes).hexdigest()
        document, binary = _read_glb_bytes(processed_bytes, max_file_size_bytes)
        meshes, _, _, _, _, _ = _inspect(document, binary)
        passed = _valid_source_observation(
            observation,
            processed_digest,
            document,
            binary,
            meshes,
            spec,
            max_file_size_bytes,
        )
    except (OSError, _InvalidGLB, KeyError, TypeError, ValueError, IndexError):
        passed = False
    finding = GeometryFinding(
        "source.preservation",
        passed,
        "hash-bound source bytes preserved by one declared normalization",
        "preserved" if passed else "changed, incomplete, or invalid source comparison",
        "Checks the allowed root transform plus source-part/socket local semantics and LOD0 triangle soup",
    )
    return V07GeometryResult(passed, (finding,))


def validate_glb_v07(
    path: Path,
    spec: AssetSpecificationV07,
    *,
    source_observation: VerifiedSourceNormalization | None = None,
    max_file_size_bytes: int = 50 * 1024 * 1024,
) -> AssetValidationResult:
    """Adapt V0.7 validation to the shared report model.

    Local operator assemblies must pass their immutable source-normalization
    observation. Omitting it causes the selected orientation group to fail.
    """
    profile = spec.bound_profile()
    result = validate_v07_geometry(
        path,
        spec,
        profile,
        source_observation=source_observation,
        max_file_size_bytes=max_file_size_bytes,
    )
    severity = ValidationFindingSeverity.PASS
    findings: list[ValidationFinding] = []
    for item in result.findings:
        item_severity = (
            ValidationFindingSeverity.PASS if item.passed else ValidationFindingSeverity.FAIL
        )
        if item_severity == ValidationFindingSeverity.FAIL:
            severity = ValidationFindingSeverity.FAIL
        findings.append(
            ValidationFinding(
                item.rule_id, item_severity, item.expected, item.actual, str(path), item.message
            )
        )
    return AssetValidationResult(
        severity,
        findings,
        f"{'Passed' if result.passed else 'Failed'} V0.7 geometry validation with {sum(not item.passed for item in result.findings)} failing rule(s)",
    )


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _quaternion_matrix(value: object) -> list[list[float]]:
    if value == "identity":
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return [[math.nan] * 3 for _ in range(3)]
    x, y, z, w = (float(item) for item in value)
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def _angular_difference_degrees(left: list[list[float]], right: list[list[float]]) -> float:
    if any(not math.isfinite(value) for row in left for value in row):
        return math.inf
    relative_trace = sum(
        left[row][column] * right[row][column] for row in range(3) for column in range(3)
    )
    cosine = max(-1.0, min(1.0, (relative_trace - 1.0) / 2.0))
    return math.degrees(math.acos(cosine))


def _vector_angle_degrees(
    left: tuple[float, float, float] | list[float],
    right: tuple[float, float, float] | list[float],
) -> float:
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm <= 1e-12 or right_norm <= 1e-12:
        return math.inf
    dot = sum(float(a) * float(b) for a, b in zip(left, right, strict=True))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot / (left_norm * right_norm)))))


def _valid_source_observation(
    observation: VerifiedSourceNormalization | None,
    processed_sha256: str,
    processed_document: Mapping[str, Any],
    processed_binary: bytes,
    processed_meshes: list[Any],
    spec: AssetSpecificationV07,
    max_file_size_bytes: int,
) -> bool:
    if observation is None:
        return False
    if observation.processed_sha256 != processed_sha256 or len(observation.source_sha256) != 64:
        return False
    if any(char not in "0123456789abcdef" for char in observation.source_sha256.lower()):
        return False
    if (
        hashlib.sha256(observation.source_glb_bytes).hexdigest()
        != observation.source_sha256.lower()
    ):
        return False
    if observation.source_front not in {"-Z", "+Z"}:
        return False
    expected_rotation = (
        (0.0, 0.0, 0.0, 1.0) if observation.source_front == "-Z" else (0.0, 1.0, 0.0, 0.0)
    )
    if observation.root_rotation_xyzw != expected_rotation or observation.normalization_applied != (
        observation.source_front == "+Z"
    ):
        return False
    try:
        source_document, source_binary = _read_glb_bytes(
            observation.source_glb_bytes, max_file_size_bytes
        )
        source_meshes, _, _, _, _, _ = _inspect(source_document, source_binary)
    except (OSError, _InvalidGLB, KeyError, TypeError, ValueError, IndexError):
        return False
    source_nodes = {
        str(node.get("name", "")): (index, node)
        for index, node in enumerate(source_document.get("nodes", []))
    }
    processed_nodes = {
        str(node.get("name", "")): (index, node)
        for index, node in enumerate(processed_document.get("nodes", []))
    }
    roots = [node for node in source_document.get("nodes", []) if node.get("name") == "ROOT"]
    if len(roots) != 1 or not _matrix_is_identity(_node_matrix(roots[0])):
        return False
    source_scene_index = source_document.get("scene", 0)
    processed_scene_index = processed_document.get("scene", 0)
    source_scenes = source_document.get("scenes", [])
    processed_scenes = processed_document.get("scenes", [])
    if (
        not isinstance(source_scene_index, int)
        or not 0 <= source_scene_index < len(source_scenes)
        or not isinstance(processed_scene_index, int)
        or not 0 <= processed_scene_index < len(processed_scenes)
    ):
        return False
    source_root_index = next(
        index for index, node in enumerate(source_document["nodes"]) if node.get("name") == "ROOT"
    )
    processed_root_index = next(
        (
            index
            for index, node in enumerate(processed_document.get("nodes", []))
            if node.get("name") == "ROOT"
        ),
        -1,
    )

    def active_reachable(document: Mapping[str, Any], scene_index: int) -> tuple[int, ...] | None:
        roots = tuple(document["scenes"][scene_index].get("nodes", ()))
        seen: set[int] = set()
        pending = list(roots)
        while pending:
            index = pending.pop()
            if index in seen or not 0 <= index < len(document.get("nodes", [])):
                continue
            seen.add(index)
            pending.extend(document["nodes"][index].get("children", ()))
        if seen != set(range(len(document.get("nodes", [])))):
            return None
        return roots

    if active_reachable(source_document, source_scene_index) != (source_root_index,):
        return False
    if processed_root_index < 0 or active_reachable(processed_document, processed_scene_index) != (
        processed_root_index,
    ):
        return False
    part_ids = {part.part_id for part in (spec.parts or [])}
    source_expected = {"ROOT"}
    source_expected |= {f"PART_{part_id}" for part_id in part_ids}
    source_expected |= {f"SM_{spec.asset_id}_{part_id}_LOD0" for part_id in part_ids}
    source_expected |= {f"SOCKET_{socket.socket_id}" for socket in (spec.sockets or [])}
    if set(source_nodes) != source_expected:
        return False
    processed_expected = set(source_expected)
    profile = spec.bound_profile()
    lod1_required = profile.document.processing.lod1_required or spec.lod_policy == "lod0_lod1"
    for part_id in part_ids:
        lod_name = f"SM_{spec.asset_id}_{part_id}_LOD1"
        if lod1_required or lod_name in processed_nodes:
            processed_expected.add(lod_name)
    if spec.collider.policy == "box":
        processed_expected.add(f"COL_{spec.asset_id}")
    if set(processed_nodes) != processed_expected:
        return False
    source_parent: dict[int, int] = {}
    for parent, node in enumerate(source_document.get("nodes", [])):
        for child in node.get("children", []):
            source_parent[child] = parent
    root_index = next(
        index for index, node in enumerate(source_document["nodes"]) if node.get("name") == "ROOT"
    )
    rotation = _quaternion_matrix(observation.root_rotation_xyzw)
    rotation4 = [[*rotation[row], 0.0] for row in range(3)] + [[0.0, 0.0, 0.0, 1.0]]
    source_mesh_info = {mesh.name: mesh for mesh in source_meshes}
    processed_mesh_info = {mesh.name: mesh for mesh in processed_meshes}
    source_node_indices = {name: index for name, (index, _) in source_nodes.items()}
    processed_parent: dict[int, int] = {}
    for parent, node in enumerate(processed_document.get("nodes", [])):
        for child in node.get("children", []):
            processed_parent[child] = parent
    processed_root = processed_root_index
    processed_roots = [
        node for node in processed_document.get("nodes", []) if node.get("name") == "ROOT"
    ]
    if (
        len(processed_roots) != 1
        or not _matrix_is_identity(_node_matrix(processed_roots[0]))
        or not _matrix_is_identity(_node_matrix(processed_document["nodes"][processed_root]))
    ):
        return False
    if len(source_nodes) != len(source_document.get("nodes", [])) or len(processed_nodes) != len(
        processed_document.get("nodes", [])
    ):
        return False
    for part in spec.parts or []:
        part_name = f"PART_{part.part_id}"
        source_entry = source_nodes.get(part_name)
        processed_entry = processed_nodes.get(part_name)
        if source_entry is None or processed_entry is None:
            return False
        source_index, source_node = source_entry
        processed_index, processed_node = processed_entry
        source_expected_parent = (
            root_index if part.parent == "root" else source_node_indices.get(f"PART_{part.parent}")
        )
        if source_parent.get(source_index) != source_expected_parent:
            return False
        source_local = _node_matrix(source_node)
        expected_local = (
            _mat_mul(rotation4, source_local) if part.parent == "root" else source_local
        )
        processed_local = _node_matrix(processed_node)
        if not _matrices_close(expected_local, processed_local, 1e-5):
            return False
        processed_expected_parent = (
            processed_root
            if part.parent == "root"
            else next(
                (
                    index
                    for index, node in enumerate(processed_document["nodes"])
                    if node.get("name") == f"PART_{part.parent}"
                ),
                -1,
            )
        )
        if processed_parent.get(processed_index) != processed_expected_parent:
            return False
        if source_node.get("extras", {}) != processed_node.get("extras", {}):
            return False
        mesh_name = f"SM_{spec.asset_id}_{part.part_id}_LOD0"
        if mesh_name not in source_mesh_info or mesh_name not in processed_mesh_info:
            return False
        source_mesh_index = next(
            (
                index
                for index, node in enumerate(source_document["nodes"])
                if node.get("name") == mesh_name
            ),
            -1,
        )
        processed_mesh_index = next(
            (
                index
                for index, node in enumerate(processed_document["nodes"])
                if node.get("name") == mesh_name
            ),
            -1,
        )
        if source_mesh_index < 0 or processed_mesh_index < 0:
            return False
        if (
            source_parent.get(source_mesh_index) != source_index
            or processed_parent.get(processed_mesh_index) != processed_index
        ):
            return False
        if not _matrix_is_identity(
            _node_matrix(source_document["nodes"][source_mesh_index])
        ) or not _matrix_is_identity(
            _node_matrix(processed_document["nodes"][processed_mesh_index])
        ):
            return False
        if not triangle_soups_equivalent(
            _triangle_soup(source_document, source_binary, mesh_name),
            _triangle_soup(processed_document, processed_binary, mesh_name),
        ):
            return False
    for socket in spec.sockets or []:
        socket_name = f"SOCKET_{socket.socket_id}"
        source_socket = source_nodes.get(socket_name)
        processed_socket = processed_nodes.get(socket_name)
        if source_socket is None or processed_socket is None:
            return False
        source_socket_index, source_socket_node = source_socket
        processed_socket_index, processed_socket_node = processed_socket
        parent_name = f"PART_{socket.parent_part}"
        expected_source_parent = source_node_indices.get(parent_name)
        expected_processed_parent = next(
            (
                index
                for index, node in enumerate(processed_document["nodes"])
                if node.get("name") == parent_name
            ),
            -1,
        )
        if (
            source_parent.get(source_socket_index) != expected_source_parent
            or processed_parent.get(processed_socket_index) != expected_processed_parent
            or source_socket_node.get("extras", {}) != processed_socket_node.get("extras", {})
            or not _matrices_close(
                _node_matrix(source_socket_node), _node_matrix(processed_socket_node), 1e-5
            )
        ):
            return False
    return processed_root >= 0


def _matrix_is_identity(matrix: list[list[float]], tolerance: float = 1e-5) -> bool:
    return all(
        abs(matrix[row][column] - (1.0 if row == column else 0.0)) <= tolerance
        for row in range(4)
        for column in range(4)
    )


def _matrices_close(left: list[list[float]], right: list[list[float]], tolerance: float) -> bool:
    return all(
        abs(left[row][column] - right[row][column]) <= tolerance
        for row in range(4)
        for column in range(4)
    )


def _triangle_soup(
    document: Mapping[str, Any], binary: bytes, node_name: str
) -> tuple[tuple[tuple[float, float, float], ...], ...]:
    node = next(
        (
            candidate
            for candidate in document.get("nodes", [])
            if candidate.get("name") == node_name
        ),
        None,
    )
    if node is None or "mesh" not in node:
        return ()
    mesh = document.get("meshes", [])[node["mesh"]]
    triangles: list[tuple[tuple[float, float, float], ...]] = []
    for primitive in mesh.get("primitives", []):
        attributes = primitive.get("attributes", {})
        rows = _accessor(dict(document), binary, attributes["POSITION"], "VEC3")
        if "indices" in primitive:
            index_rows = _accessor(dict(document), binary, primitive["indices"], "SCALAR")
            indices = [int(row[0]) for row in index_rows]
        else:
            indices = list(range(len(rows)))
        for offset in range(0, len(indices), 3):
            vertices: list[tuple[float, float, float]] = [
                (
                    float(rows[indices[offset + index]][0]),
                    float(rows[indices[offset + index]][1]),
                    float(rows[indices[offset + index]][2]),
                )
                for index in range(3)
            ]
            cyclic: list[tuple[tuple[float, float, float], ...]] = [
                tuple(vertices[index:] + vertices[:index]) for index in range(3)
            ]
            triangles.append(min(cyclic))
    return tuple(sorted(triangles))


def triangle_soups_equivalent(
    source: tuple[tuple[tuple[float, float, float], ...], ...],
    processed: tuple[tuple[tuple[float, float, float], ...], ...],
    tolerance: float = 1e-5,
) -> bool:
    """Compare oriented triangle multisets independent of vertex/index ordering."""
    if len(source) != len(processed):
        return False
    if not source:
        return True
    if Counter(source) == Counter(processed):
        return True
    if not math.isfinite(tolerance) or tolerance <= 0:
        return False

    def center(triangle: tuple[tuple[float, float, float], ...]) -> tuple[float, float, float]:
        return (
            sum(point[0] for point in triangle) / 3,
            sum(point[1] for point in triangle) / 3,
            sum(point[2] for point in triangle) / 3,
        )

    def cell(point: tuple[float, float, float]) -> tuple[int, int, int]:
        return (
            math.floor(point[0] / tolerance),
            math.floor(point[1] / tolerance),
            math.floor(point[2] / tolerance),
        )

    buckets: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for candidate_index, triangle in enumerate(processed):
        buckets[cell(center(triangle))].append(candidate_index)

    # Build a bounded bipartite candidate graph before assigning anything.
    # A greedy closest-match choice is unsafe near tolerance boundaries because
    # one source triangle can consume another triangle's only candidate.
    comparisons = 0
    comparison_limit = max(100_000, len(source) * 32)
    adjacency: list[list[int]] = []
    for triangle in source:
        origin = cell(center(triangle))
        candidates: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    key = (origin[0] + dx, origin[1] + dy, origin[2] + dz)
                    for candidate_index in buckets.get(key, ()):
                        comparisons += 1
                        if comparisons > comparison_limit:
                            return False
                        candidate = processed[candidate_index]
                        equivalent = False
                        for rotation in range(3):
                            shifted = candidate[rotation:] + candidate[:rotation]
                            errors = [
                                abs(triangle[i][axis] - shifted[i][axis])
                                for i in range(3)
                                for axis in range(3)
                            ]
                            if max(errors) <= tolerance:
                                equivalent = True
                                break
                        if equivalent:
                            candidates.append(candidate_index)
        if not candidates:
            return False
        adjacency.append(candidates)

    # Iterative augmenting-path matching avoids recursion depth hazards. Both
    # candidate comparisons and path work are capped for adversarial GLBs.
    match_source = [-1] * len(source)
    match_candidate = [-1] * len(processed)
    path_work = 0
    path_work_limit = max(1_000_000, len(source) * 64)
    for start in range(len(source)):
        queue = deque([start])
        seen_sources = {start}
        seen_candidates: set[int] = set()
        parent_candidate: dict[int, int] = {}
        parent_source: dict[int, int] = {}
        free_candidate = -1
        while queue and free_candidate < 0:
            source_index = queue.popleft()
            for candidate_index in adjacency[source_index]:
                path_work += 1
                if path_work > path_work_limit:
                    return False
                if candidate_index in seen_candidates:
                    continue
                seen_candidates.add(candidate_index)
                parent_candidate[candidate_index] = source_index
                matched_source = match_candidate[candidate_index]
                if matched_source < 0:
                    free_candidate = candidate_index
                    break
                if matched_source not in seen_sources:
                    seen_sources.add(matched_source)
                    parent_source[matched_source] = candidate_index
                    queue.append(matched_source)
        if free_candidate < 0:
            return False

        candidate_index = free_candidate
        while True:
            source_index = parent_candidate[candidate_index]
            previous_candidate = match_source[source_index]
            match_source[source_index] = candidate_index
            match_candidate[candidate_index] = source_index
            if previous_candidate < 0:
                break
            candidate_index = parent_source[source_index]
    return True


def _validate_sockets(
    document: Mapping[str, Any],
    node_by_name: Mapping[str, tuple[int, Mapping[str, Any]]],
    node_mesh: Mapping[str, tuple[int, Mapping[str, Any]]],
    parent_by_index: Mapping[int, int],
    world: Mapping[int, list[list[float]]],
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    meshes: list[Any],
    add: Callable[..., None],
) -> None:
    declared = {sock.socket_id: sock for sock in (spec.sockets or [])}
    actual = {
        str(node.get("name", ""))[7:]: (index, node)
        for index, node in enumerate(document["nodes"])
        if str(node.get("name", "")).startswith("SOCKET_")
    }
    for socket_id, socket in declared.items():
        entry = actual.get(socket_id)
        add(
            f"socket.missing.{socket_id}",
            entry is not None,
            f"SOCKET_{socket_id}",
            "present" if entry else "missing",
            "Declared socket node exists",
        )
        if entry is None:
            continue
        index, node = entry
        shape = "mesh" not in node and not node.get("children") and _identity_scale(node)
        add(
            f"socket.structure.{socket_id}",
            shape,
            "meshless leaf with identity scale",
            node,
            "Socket must be a meshless leaf",
        )
        parent_index = parent_by_index.get(index)
        expected_parent = node_by_name.get(f"PART_{socket.parent_part}", (-1, {}))[0]
        add(
            f"socket.parent.{socket_id}",
            parent_index == expected_parent,
            expected_parent,
            parent_index,
            "Socket parent matches declaration",
        )
        local = _node_matrix(node)
        translation = tuple(local[i][3] for i in range(3))
        tol = profile.assembly.socket_position_tolerance_m if profile.assembly else 0.01
        position_ok = all(
            abs(a - b) <= tol for a, b in zip(translation, socket.translation_m, strict=True)
        )
        parent_mesh = (
            next((m for m in profile.assembly.required_sockets if m.socket_id == socket_id), None)
            if profile.assembly
            else None
        )
        if parent_mesh and parent_mesh.placement == "forward_end":
            part = next((p for p in spec.parts or [] if p.part_id == socket.parent_part), None)
            lod_name = f"SM_{spec.asset_id}_{socket.parent_part}_LOD0"
            lod_index = node_mesh.get(lod_name, (-1, {}))[0]
            part_index = node_by_name.get(f"PART_{socket.parent_part}", (-1, {}))[0]
            mesh_data = next((mesh for mesh in meshes if mesh.name == lod_name), None)
            placement_ok = (
                part is not None and mesh_data is not None and lod_index >= 0 and part_index >= 0
            )
            if placement_ok and mesh_data is not None:
                part_matrix = world.get(part_index)
                placement_ok = part_matrix is not None
                if part_matrix is None:
                    part_matrix = [[math.nan] * 4 for _ in range(4)]
                local_points = [
                    tuple(_transform_point(point, _inverse_rigid(part_matrix))[i] for i in range(3))
                    for point in mesh_data.points
                ]
                if local_points:
                    z_min = min(point[2] for point in local_points)
                    z_max = max(point[2] for point in local_points)
                    socket_z = float(socket.translation_m[2])
                    fraction = parent_mesh.forward_end_fraction
                    lateral_ok = (
                        min(point[0] for point in local_points) - tol
                        <= socket.translation_m[0]
                        <= max(point[0] for point in local_points) + tol
                        and min(point[1] for point in local_points) - tol
                        <= socket.translation_m[1]
                        <= max(point[1] for point in local_points) + tol
                    )
                    end_max = z_min + (z_max - z_min) * fraction
                    placement_ok = (
                        placement_ok
                        and lateral_ok
                        and socket_z >= z_min - tol
                        and socket_z <= end_max + tol
                    )
                else:
                    placement_ok = False
            position_ok = position_ok and bool(placement_ok)
        add(
            f"socket.position.{socket_id}",
            position_ok,
            socket.translation_m,
            translation,
            "Socket local translation matches declaration and placement contract",
        )
        rotation = _quaternion_matrix(socket.rotation)
        scale_columns = [tuple(local[row][col] for row in range(3)) for col in range(3)]
        scale_lengths = tuple(
            math.sqrt(sum(value * value for value in column)) for column in scale_columns
        )
        actual_rotation = [
            [
                local[row][col] / scale_lengths[col] if scale_lengths[col] > 1e-12 else math.nan
                for col in range(3)
            ]
            for row in range(3)
        ]
        angle_tolerance = profile.assembly.socket_angle_tolerance_deg if profile.assembly else 0.01
        orientation_ok = (
            min(scale_lengths) > 1e-12
            and _angular_difference_degrees(actual_rotation, rotation) <= angle_tolerance
        )
        required = (
            next(
                (item for item in profile.assembly.required_sockets if item.socket_id == socket_id),
                None,
            )
            if profile.assembly
            else None
        )
        if required and required.rest_forward is not None:
            parent_world = world.get(parent_index) if parent_index is not None else None
            socket_world = _mat_mul(parent_world, local) if parent_world is not None else local
            forward = (-socket_world[0][2], -socket_world[1][2], -socket_world[2][2])
            norm = math.sqrt(sum(value * value for value in forward))
            angle = _vector_angle_degrees(forward, required.rest_forward)
            orientation_ok = orientation_ok and norm > 1e-12 and angle <= angle_tolerance
        add(
            f"socket.orientation.{socket_id}",
            orientation_ok,
            socket.rotation,
            "local basis",
            "Socket basis and asserted rest forward",
        )
    add(
        "socket.unexpected",
        set(actual) <= set(declared),
        sorted(declared),
        sorted(actual),
        "No undeclared socket nodes",
    )


def _identity_scale(node: Mapping[str, Any]) -> bool:
    matrix = _node_matrix(dict(node))
    lengths = tuple(math.sqrt(sum(matrix[row][col] ** 2 for row in range(3))) for col in range(3))
    columns = [tuple(matrix[row][col] for row in range(3)) for col in range(3)]
    orthogonal = all(
        abs(sum(columns[a][i] * columns[b][i] for i in range(3))) <= 1e-5
        for a, b in ((0, 1), (0, 2), (1, 2))
    )
    determinant = (
        matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
        - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
        + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0])
    )
    return all(abs(value - 1.0) <= 1e-5 for value in lengths) and orthogonal and determinant > 0


def _transform_point(
    point: tuple[float, float, float], matrix: list[list[float]]
) -> tuple[float, float, float]:
    result = tuple(
        sum(matrix[row][col] * (point[0], point[1], point[2], 1.0)[col] for col in range(4))
        for row in range(3)
    )
    return (float(result[0]), float(result[1]), float(result[2]))


def _inverse_rigid(matrix: list[list[float]]) -> list[list[float]]:
    inverse = [[0.0] * 4 for _ in range(4)]
    a, b, c = matrix[0][:3]
    d, e, f = matrix[1][:3]
    g, h, i = matrix[2][:3]
    determinant = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(determinant) <= 1e-12:
        return [[math.nan] * 4 for _ in range(4)]
    reciprocal = 1.0 / determinant
    inverse[:3] = [
        [
            (e * i - f * h) * reciprocal,
            (c * h - b * i) * reciprocal,
            (b * f - c * e) * reciprocal,
            0.0,
        ],
        [
            (f * g - d * i) * reciprocal,
            (a * i - c * g) * reciprocal,
            (c * d - a * f) * reciprocal,
            0.0,
        ],
        [
            (d * h - e * g) * reciprocal,
            (b * g - a * h) * reciprocal,
            (a * e - b * d) * reciprocal,
            0.0,
        ],
    ]
    for row in range(3):
        inverse[row][3] = -sum(inverse[row][col] * matrix[col][3] for col in range(3))
    inverse[3][3] = 1.0
    return inverse


def _validate_assembly_bounds(
    meshes: list[Any],
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    add: Callable[..., None],
) -> None:
    visual = [
        point
        for mesh in meshes
        if mesh.name.startswith(f"SM_{spec.asset_id}_")
        for point in mesh.points
    ]
    if not visual:
        add(
            "dimensions.bounds",
            False,
            spec.dimensions.model_dump(),
            "no part geometry",
            "Actual visual vertices required",
        )
        return
    mins = tuple(min(point[axis] for point in visual) for axis in range(3))
    maxs = tuple(max(point[axis] for point in visual) for axis in range(3))
    actual = tuple(maxs[i] - mins[i] for i in range(3))
    expected = (spec.dimensions.width_m, spec.dimensions.height_m, spec.dimensions.depth_m)
    tol = profile.document.processing.dimension_tolerance_m
    add(
        "dimensions.bounds",
        all(abs(a - b) <= tol for a, b in zip(actual, expected, strict=True)),
        expected,
        actual,
        "Assembly bounds measured from decoded vertices; no scaling is applied",
    )
    origin = all(abs((mins[i] + maxs[i]) / 2) <= tol for i in (0, 2))
    if spec.origin_policy == "bottom_center":
        origin = origin and abs(mins[1]) <= tol
    else:
        origin = origin and abs((mins[1] + maxs[1]) / 2) <= tol
    add(
        "origin.policy",
        origin,
        spec.origin_policy,
        (mins, maxs),
        "Assembly root rest bounds honor origin policy",
    )


def _validate_assembly_box(
    document: Mapping[str, Any],
    binary: bytes,
    meshes: list[Any],
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    add: Callable[..., None],
) -> None:
    if spec.collider.policy != "box":
        return
    nodes = document.get("nodes", [])
    root_index = next((index for index, node in enumerate(nodes) if node.get("name") == "ROOT"), -1)
    collider_node = next(
        (index for index, node in enumerate(nodes) if node.get("name") == f"COL_{spec.asset_id}"),
        -1,
    )
    parent_index = next(
        (parent for parent, node in enumerate(nodes) if collider_node in node.get("children", [])),
        None,
    )
    collider_node_data = nodes[collider_node] if collider_node >= 0 else {}
    collider_transform_ok = collider_node >= 0 and _matrix_is_identity(
        _node_matrix(collider_node_data)
    )
    add(
        "collider.parent",
        parent_index == root_index and collider_transform_ok,
        root_index,
        (parent_index, collider_transform_ok),
        "Root box is an identity child of ROOT",
    )
    collider = next((mesh for mesh in meshes if mesh.name == f"COL_{spec.asset_id}"), None)
    visual = [
        point
        for mesh in meshes
        if mesh.name.startswith(f"SM_{spec.asset_id}_") and mesh.name.endswith("LOD0")
        for point in mesh.points
    ]
    tolerance = profile.document.processing.dimension_tolerance_m
    okay = collider is not None and bool(visual)
    if okay and collider is not None:
        vmin = tuple(min(p[i] for p in visual) for i in range(3))
        vmax = tuple(max(p[i] for p in visual) for i in range(3))
        okay = box_mesh_geometry_matches(
            document,
            binary,
            f"COL_{spec.asset_id}",
            collider.points,
            vmin,
            vmax,
            tolerance,
        )
    add(
        "collider.box",
        bool(okay),
        "root rest box enclosing assembly bounds",
        "valid" if okay else "missing or mismatched",
        "Assembly collider must match actual rest bounds",
    )


def box_mesh_geometry_matches(
    document: Mapping[str, Any],
    binary: bytes,
    node_name: str,
    points: tuple[tuple[float, float, float], ...],
    expected_min: tuple[float, float, float],
    expected_max: tuple[float, float, float],
    tolerance: float,
) -> bool:
    if tolerance <= 0 or not points:
        return False
    geometry_tolerance = min(tolerance, 1e-5)
    minima = tuple(min(point[axis] for point in points) for axis in range(3))
    maxima = tuple(max(point[axis] for point in points) for axis in range(3))
    if any(
        abs(minima[axis] - expected_min[axis]) > tolerance
        or abs(maxima[axis] - expected_max[axis]) > tolerance
        for axis in range(3)
    ):
        return False
    corners = [
        (x, y, z)
        for x in (minima[0], maxima[0])
        for y in (minima[1], maxima[1])
        for z in (minima[2], maxima[2])
    ]
    unique_points: list[tuple[float, float, float]] = []
    for point in points:
        if not any(
            all(abs(point[axis] - known[axis]) <= geometry_tolerance for axis in range(3))
            for known in unique_points
        ):
            unique_points.append(point)
    if len(unique_points) != 8 or any(
        not any(
            all(abs(point[axis] - corner[axis]) <= geometry_tolerance for axis in range(3))
            for point in unique_points
        )
        for corner in corners
    ):
        return False
    triangles = _triangle_soup(document, binary, node_name)
    if len(triangles) != 12:
        return False
    face_triangles: dict[tuple[int, int], list[tuple[int, int, int]]] = defaultdict(list)
    face_areas: dict[tuple[int, int], float] = defaultdict(float)
    sizes = tuple(maxima[axis] - minima[axis] for axis in range(3))
    for triangle in triangles:
        face = next(
            (
                (axis, side)
                for axis in range(3)
                for side, boundary in ((-1, minima[axis]), (1, maxima[axis]))
                if all(abs(point[axis] - boundary) <= geometry_tolerance for point in triangle)
            ),
            None,
        )
        if face is None:
            return False
        corner_indices: list[int] = []
        for point in triangle:
            corner_index = next(
                (
                    index
                    for index, corner in enumerate(corners)
                    if all(
                        abs(point[axis] - corner[axis]) <= geometry_tolerance for axis in range(3)
                    )
                ),
                -1,
            )
            if corner_index < 0:
                return False
            corner_indices.append(corner_index)
        if len(set(corner_indices)) != 3:
            return False
        a, b, c = triangle
        ab = tuple(b[i] - a[i] for i in range(3))
        ac = tuple(c[i] - a[i] for i in range(3))
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        area = math.sqrt(sum(value * value for value in cross)) / 2
        if area <= geometry_tolerance * geometry_tolerance:
            return False
        face_triangles[face].append((corner_indices[0], corner_indices[1], corner_indices[2]))
        face_areas[face] += area
    for axis in range(3):
        other_axes = [index for index in range(3) if index != axis]
        expected_area = sizes[other_axes[0]] * sizes[other_axes[1]]
        area_tolerance = max(
            geometry_tolerance * max(sizes) * 4, geometry_tolerance * geometry_tolerance
        )
        for side in (-1, 1):
            face = (axis, side)
            triangles_on_face = face_triangles[face]
            if (
                len(triangles_on_face) != 2
                or abs(face_areas[face] - expected_area) > area_tolerance
            ):
                return False
            triangle_sets = [set(triangle) for triangle in triangles_on_face]
            face_corners = {
                index
                for index, corner in enumerate(corners)
                if abs(corner[axis] - (minima[axis] if side < 0 else maxima[axis]))
                <= geometry_tolerance
            }
            shared = triangle_sets[0] & triangle_sets[1]
            in_face_axes = [index for index in range(3) if index != axis]
            diagonal = len(shared) == 2 and all(
                abs(corners[left][other_axis] - corners[right][other_axis]) > geometry_tolerance
                for left, right in [tuple(shared)]
                for other_axis in in_face_axes
            )
            if (
                triangle_sets[0] == triangle_sets[1]
                or triangle_sets[0] | triangle_sets[1] != face_corners
                or not diagonal
            ):
                return False
    return True


def _validate_single_mesh(
    document: Mapping[str, Any],
    meshes: list[Any],
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    add: Callable[..., None],
) -> None:
    expected_lod0 = f"SM_{spec.asset_id}_LOD0"
    visual = next((mesh for mesh in meshes if mesh.name == expected_lod0), None)
    add(
        "mesh.single",
        visual is not None and visual.triangle_count > 0,
        expected_lod0,
        visual.name if visual else "missing",
        "Single-mesh LOD0 node is present",
    )
    if visual is not None and visual.points:
        mins = tuple(min(point[axis] for point in visual.points) for axis in range(3))
        maxs = tuple(max(point[axis] for point in visual.points) for axis in range(3))
        actual = tuple(maxs[axis] - mins[axis] for axis in range(3))
        expected = (spec.dimensions.width_m, spec.dimensions.height_m, spec.dimensions.depth_m)
        tolerance = profile.document.processing.dimension_tolerance_m
        add(
            "dimensions.bounds",
            all(abs(a - b) <= tolerance for a, b in zip(actual, expected, strict=True)),
            expected,
            actual,
            "Dimensions measured from actual decoded vertex positions",
        )
        origin_ok = (
            abs((mins[0] + maxs[0]) / 2) <= tolerance and abs((mins[2] + maxs[2]) / 2) <= tolerance
        )
        origin_ok = origin_ok and (
            abs(mins[1]) <= tolerance
            if spec.origin_policy == "bottom_center"
            else abs((mins[1] + maxs[1]) / 2) <= tolerance
        )
        add(
            "origin.policy",
            origin_ok,
            spec.origin_policy,
            (mins, maxs),
            "Visual root origin matches policy",
        )
    else:
        add(
            "dimensions.bounds",
            False,
            spec.dimensions.model_dump(),
            "missing geometry",
            "Actual visual vertices are required",
        )
        add(
            "origin.policy",
            False,
            spec.origin_policy,
            "missing geometry",
            "Cannot establish origin without vertices",
        )
    lod1 = next((mesh for mesh in meshes if mesh.name == f"SM_{spec.asset_id}_LOD1"), None)
    lod1_required = profile.document.processing.lod1_required or spec.lod_policy == "lod0_lod1"
    add(
        "lod1.present",
        (lod1 is not None and lod1.triangle_count > 0) or not lod1_required,
        "LOD1" if lod1_required else "optional",
        "present" if lod1 else "missing",
        "Single-mesh LOD policy",
    )
    if lod1_required:
        bounds_ok = (
            visual is not None and bool(visual.points) and lod1 is not None and bool(lod1.points)
        )
        if bounds_ok and visual is not None and lod1 is not None:
            lo0 = tuple(min(point[i] for point in visual.points) for i in range(3))
            hi0 = tuple(max(point[i] for point in visual.points) for i in range(3))
            lo1 = tuple(min(point[i] for point in lod1.points) for i in range(3))
            hi1 = tuple(max(point[i] for point in lod1.points) for i in range(3))
            bounds_ok = all(
                abs(lo0[i] - lo1[i]) <= profile.document.processing.dimension_tolerance_m
                and abs(hi0[i] - hi1[i]) <= profile.document.processing.dimension_tolerance_m
                for i in range(3)
            )
        add(
            "lod1.bounds",
            bool(bounds_ok),
            "LOD1 matches LOD0 bounds",
            lod1.name if lod1 else "missing",
            "LOD bounds derive from decoded vertices",
        )
    expected_meshes = {expected_lod0}
    if lod1_required or lod1 is not None:
        expected_meshes.add(f"SM_{spec.asset_id}_LOD1")
    if spec.collider.policy == "box":
        expected_meshes.add(f"COL_{spec.asset_id}")
    actual_meshes = {mesh.name for mesh in meshes}
    add(
        "mesh.set",
        actual_meshes == expected_meshes,
        sorted(expected_meshes),
        sorted(actual_meshes),
        "No undeclared single-mesh geometry",
    )
    mesh_nodes = [node for node in document.get("nodes", []) if "mesh" in node]
    identity_ok = all(
        all(
            abs(_node_matrix(node)[row][column] - (1.0 if row == column else 0.0)) <= 1e-5
            for row in range(4)
            for column in range(4)
        )
        for node in mesh_nodes
        if str(node.get("name", "")).startswith("SM_")
    )
    add(
        "orientation.identity",
        identity_ok,
        "identity visual node transforms",
        "identity" if identity_ok else "non-identity",
        "Single-mesh canonical frame has no residual visual transform",
    )


def _validate_capsule(
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    meshes: list[Any],
    add: Callable[..., None],
) -> None:
    capsule = spec.collider.capsule
    has_collider_mesh = any(mesh.name.startswith("COL_") for mesh in meshes)
    add(
        "collider.capsule.shape",
        capsule is not None and not has_collider_mesh,
        "declared capsule, no capsule mesh",
        "present" if capsule and not has_collider_mesh else "missing or mesh found",
        "Capsule shape is runtime data and has no GLB mesh",
    )
    tol = profile.document.processing.dimension_tolerance_m
    fit = (
        capsule is not None
        and capsule.height_m <= spec.dimensions.height_m + tol
        and 2 * capsule.radius_m <= max(spec.dimensions.width_m, spec.dimensions.depth_m) + tol
    )
    add(
        "collider.capsule.fit",
        bool(fit),
        spec.dimensions.model_dump(),
        capsule,
        "Capsule dimensions fit the specified asset bounds",
    )
