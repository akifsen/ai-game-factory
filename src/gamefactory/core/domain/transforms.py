"""Small, dependency-free 4x4 / quaternion helpers for glTF node transforms.

Matrices are row-major ``list[list[float]]`` with the translation in the last
column, matching :func:`gamefactory.adapters.assets.glb_validator._node_matrix`.
Quaternions are ``(x, y, z, w)`` as in glTF.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Matrix = list[list[float]]
Quat = tuple[float, float, float, float]
Vec3 = tuple[float, float, float]

IDENTITY_QUAT: Quat = (0.0, 0.0, 0.0, 1.0)
# ADR 0014: the only permitted source_front normalization, 180 degrees about +Y.
SOURCE_FRONT_PLUS_Z_QUAT: Quat = (0.0, 1.0, 0.0, 0.0)


def identity() -> Matrix:
    return [[1.0 if r == c else 0.0 for c in range(4)] for r in range(4)]


def mat_mul(a: Matrix, b: Matrix) -> Matrix:
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def normalize_quat(q: Sequence[float]) -> Quat:
    x, y, z, w = (float(v) for v in q)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm < 1e-12:
        raise ValueError("quaternion is zero")
    return (x / norm, y / norm, z / norm, w / norm)


def quat_to_rotation(q: Sequence[float]) -> list[list[float]]:
    x, y, z, w = normalize_quat(q)
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def trs_matrix(
    translation: Sequence[float] = (0.0, 0.0, 0.0),
    rotation: Sequence[float] = IDENTITY_QUAT,
    scale: Sequence[float] = (1.0, 1.0, 1.0),
) -> Matrix:
    r = quat_to_rotation(rotation)
    rows = [
        [r[i][j] * float(scale[j]) for j in range(3)] + [float(translation[i])] for i in range(3)
    ]
    return [*rows, [0.0, 0.0, 0.0, 1.0]]


def translation_of(m: Matrix) -> Vec3:
    return (m[0][3], m[1][3], m[2][3])


def scale_of(m: Matrix) -> Vec3:
    """Column norms of the upper 3x3, i.e. the absolute per-axis scale."""
    return (
        math.sqrt(sum(m[r][0] ** 2 for r in range(3))),
        math.sqrt(sum(m[r][1] ** 2 for r in range(3))),
        math.sqrt(sum(m[r][2] ** 2 for r in range(3))),
    )


def determinant3(m: Matrix) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def rotation_of(m: Matrix) -> list[list[float]]:
    """Upper 3x3 with the per-axis scale divided out (assumes non-zero scale)."""
    s = scale_of(m)
    if min(s) < 1e-12:
        raise ValueError("matrix has zero scale")
    return [[m[r][c] / s[c] for c in range(3)] for r in range(3)]


def rotation_to_quat(r: list[list[float]]) -> Quat:
    """Convert an orthonormal rotation matrix to a unit quaternion (x, y, z, w)."""
    trace = r[0][0] + r[1][1] + r[2][2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        w = 0.25 * s
        x = (r[2][1] - r[1][2]) / s
        y = (r[0][2] - r[2][0]) / s
        z = (r[1][0] - r[0][1]) / s
    elif r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = math.sqrt(1.0 + r[0][0] - r[1][1] - r[2][2]) * 2
        w = (r[2][1] - r[1][2]) / s
        x = 0.25 * s
        y = (r[0][1] + r[1][0]) / s
        z = (r[0][2] + r[2][0]) / s
    elif r[1][1] > r[2][2]:
        s = math.sqrt(1.0 + r[1][1] - r[0][0] - r[2][2]) * 2
        w = (r[0][2] - r[2][0]) / s
        x = (r[0][1] + r[1][0]) / s
        y = 0.25 * s
        z = (r[1][2] + r[2][1]) / s
    else:
        s = math.sqrt(1.0 + r[2][2] - r[0][0] - r[1][1]) * 2
        w = (r[1][0] - r[0][1]) / s
        x = (r[0][2] + r[2][0]) / s
        y = (r[1][2] + r[2][1]) / s
        z = 0.25 * s
    return normalize_quat((x, y, z, w))


def quat_angle_deg(a: Sequence[float], b: Sequence[float]) -> float:
    """Smallest rotation angle between two orientations, in degrees."""
    qa, qb = normalize_quat(a), normalize_quat(b)
    dot = abs(sum(x * y for x, y in zip(qa, qb, strict=True)))
    return math.degrees(2 * math.acos(min(1.0, dot)))


def vector_angle_deg(a: Sequence[float], b: Sequence[float]) -> float:
    na = math.sqrt(sum(float(v) ** 2 for v in a))
    nb = math.sqrt(sum(float(v) ** 2 for v in b))
    if na < 1e-12 or nb < 1e-12:
        return 180.0
    cos = sum(float(x) * float(y) for x, y in zip(a, b, strict=True)) / (na * nb)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


def apply_rotation(r: list[list[float]], v: Sequence[float]) -> Vec3:
    return (
        r[0][0] * v[0] + r[0][1] * v[1] + r[0][2] * v[2],
        r[1][0] * v[0] + r[1][1] * v[1] + r[1][2] * v[2],
        r[2][0] * v[0] + r[2][1] * v[1] + r[2][2] * v[2],
    )


def matrices_close(a: Matrix, b: Matrix, tol: float = 1e-5) -> bool:
    return all(abs(a[r][c] - b[r][c]) <= tol for r in range(4) for c in range(4))


def spec_quat(basis: object) -> Quat:
    """Declared basis/rotation (``"identity"`` or ``[x, y, z, w]``) as a quaternion."""
    if basis == "identity" or basis is None:
        return IDENTITY_QUAT
    if isinstance(basis, (list, tuple)) and len(basis) == 4:
        return normalize_quat([float(v) for v in basis])
    raise ValueError(f"unsupported basis {basis!r}")
