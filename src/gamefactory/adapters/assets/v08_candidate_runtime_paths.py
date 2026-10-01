"""Filesystem safety checks for V0.8-3B candidate runtime staging and verification."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def path_crosses_link(path: Path) -> bool:
    """Lexical ancestor scan for symlinks/junctions/reparse points (incl. Python 3.11 Windows)."""
    lexical = Path(path).absolute()
    for current in [lexical, *lexical.parents]:
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
        if current.anchor == current:
            break
    return False


def assert_no_link_in_path(path: Path, *, label: str) -> None:
    if path_crosses_link(path):
        raise ValueError(f"{label} crosses a symlink or junction: {path}")
