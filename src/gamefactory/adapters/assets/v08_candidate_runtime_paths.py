"""Filesystem safety checks for V0.8-3B candidate runtime staging and verification."""

from __future__ import annotations

from pathlib import Path


def path_crosses_link(path: Path) -> bool:
    for current in [path, *path.parents]:
        junction = getattr(current, "is_junction", None)
        if current.is_symlink() or (junction is not None and junction()):
            return True
        if current.anchor == current:
            break
    return False


def assert_no_link_in_path(path: Path, *, label: str) -> None:
    if path_crosses_link(path):
        raise ValueError(f"{label} crosses a symlink or junction: {path}")
