"""Small matrix helpers for internal skin validation (ADR 0019)."""

from __future__ import annotations


def invert_mat4(m: list[list[float]]) -> list[list[float]]:
    """Invert a 4x4 matrix with partial pivoting (row operations)."""
    a = [[float(m[r][c]) for c in range(4)] for r in range(4)]
    inv = [[0.0] * 4 for _ in range(4)]
    for i in range(4):
        inv[i][i] = 1.0
    for col in range(4):
        pivot_row = col
        pivot_val = abs(a[pivot_row][col])
        for row in range(col + 1, 4):
            if abs(a[row][col]) > pivot_val:
                pivot_row = row
                pivot_val = abs(a[row][col])
        if pivot_val < 1e-12:
            raise ValueError("singular matrix")
        if pivot_row != col:
            a[col], a[pivot_row] = a[pivot_row], a[col]
            inv[col], inv[pivot_row] = inv[pivot_row], inv[col]
        pivot = a[col][col]
        inv_p = 1.0 / pivot
        for j in range(4):
            a[col][j] *= inv_p
            inv[col][j] *= inv_p
        for row in range(4):
            if row == col:
                continue
            factor = a[row][col]
            if abs(factor) < 1e-15:
                continue
            for j in range(4):
                a[row][j] -= factor * a[col][j]
                inv[row][j] -= factor * inv[col][j]
    return inv


def mat3_basis_columns(
    world: list[list[float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return world +Y and -Z basis vectors from a joint world matrix (glTF column basis)."""
    y_axis = (world[0][1], world[1][1], world[2][1])
    neg_z = (-world[0][2], -world[1][2], -world[2][2])
    return y_axis, neg_z
