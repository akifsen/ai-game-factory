"""Build small, explicitly selected, secret-screened project context bundles."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from gamefactory.core.domain.agent_contracts import SourceReference
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.execution.path_guard import PathGuard

_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
_SECRET_DIRS = {".aws", ".ssh", ".gnupg", ".codex", ".agents", ".git"}
_SECRET_NAMES = {"secret", "secrets", "credentials", "credential", "id_rsa", "id_ed25519"}
_SECRET_CONTENT = re.compile(
    r"(?im)^\s*(?:[A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*\s*=\s*\S+|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


def _has_reparse_component(root: Path, candidate: Path) -> bool:
    """Check unresolved path components through the selected leaf for links/junctions."""
    root = Path(os.path.abspath(root))
    candidate = Path(os.path.abspath(candidate))
    if candidate != root and root not in candidate.parents:
        return True
    current = candidate
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            info = None
        except OSError:
            return True
        if info is not None and (
            stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse_flag
        ):
            return True
        if current == root:
            return False
        current = current.parent


def _secret_path(relative: str) -> bool:
    parts = Path(relative).parts
    lowered = {part.lower() for part in parts}
    filename = parts[-1].lower() if parts else ""
    suffix = Path(filename).suffix.lower()
    return (
        bool(lowered & _SECRET_DIRS)
        or filename == ".env"
        or filename.startswith(".env.")
        or filename in _SECRET_NAMES
        or Path(filename).stem in _SECRET_NAMES
        or suffix in _SECRET_SUFFIXES
    )


@dataclass(frozen=True)
class ContextSelection:
    path: str
    purpose: str


@dataclass(frozen=True)
class ContextItem:
    source: SourceReference
    content: bytes


@dataclass(frozen=True)
class BoundedContext:
    items: tuple[ContextItem, ...]
    total_bytes: int

    @property
    def sources(self) -> tuple[SourceReference, ...]:
        return tuple(item.source for item in self.items)


class ContextBuilder:
    """Reads only caller-selected regular files contained under project_root."""

    def __init__(self, project_root: Path | str, max_total_bytes: int = 256_000) -> None:
        if (
            isinstance(max_total_bytes, bool)
            or not isinstance(max_total_bytes, int)
            or not 0 <= max_total_bytes <= 16 * 1024 * 1024
        ):
            raise ValueError("max_total_bytes must be an integer between zero and 16 MiB")
        supplied_root = Path(project_root).expanduser()
        raw_root = Path(os.path.abspath(supplied_root))
        # Check every lexical ancestor before resolution. A junction above the
        # supplied project root must not be silently normalized away.
        cursor = raw_root
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        while True:
            try:
                info = cursor.lstat()
            except FileNotFoundError:
                info = None
            except OSError as err:
                raise ValidationError("Project root path cannot be inspected safely") from err
            if info is not None and (
                stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse_flag
            ):
                raise ValidationError(
                    "Project root ancestry cannot cross symlinks or reparse points"
                )
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        if _has_reparse_component(raw_root, raw_root):
            raise ValidationError("Project root cannot be a symlink or reparse point")
        self.root = raw_root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValidationError("Project root must be an existing directory")
        self.guard = PathGuard(self.root)
        self.max_total_bytes = max_total_bytes

    def build(
        self, selections: list[ContextSelection] | tuple[ContextSelection, ...]
    ) -> BoundedContext:
        items: list[ContextItem] = []
        total = 0
        seen: set[str] = set()
        for selection in selections:
            if not selection.purpose.strip():
                raise ValidationError("Context selection purpose cannot be empty")
            if selection.path in seen:
                raise ValidationError(
                    "Context paths must be unique", details={"path": selection.path}
                )
            seen.add(selection.path)
            raw = Path(selection.path)
            lexical_path = raw if raw.is_absolute() else self.root / raw
            if _has_reparse_component(self.root, lexical_path):
                raise ValidationError(
                    "Reparse-point context sources are forbidden", details={"path": selection.path}
                )
            path = self.guard.resolve_safe_path(selection.path)
            relative = path.relative_to(self.root).as_posix()
            if _secret_path(relative):
                raise ValidationError(
                    "Secret-bearing files cannot be included in agent context",
                    details={"path": relative},
                )
            remaining = self.max_total_bytes - total
            try:
                descriptor = os.open(
                    path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                )
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise ValidationError(
                            "Context source must be a regular file", details={"path": relative}
                        )
                    after_open = path.lstat()
                    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                    if (
                        stat.S_ISLNK(after_open.st_mode)
                        or getattr(after_open, "st_file_attributes", 0) & reparse_flag
                        or (info.st_dev, info.st_ino) != (after_open.st_dev, after_open.st_ino)
                    ):
                        raise ValidationError(
                            "Context source changed while being opened", details={"path": relative}
                        )
                    content = stream.read(remaining + 1)
                    after_read = os.fstat(stream.fileno())
                    after_read_path = path.lstat()
                    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                        after_read.st_dev,
                        after_read.st_ino,
                        after_read.st_size,
                        after_read.st_mtime_ns,
                    ) or (after_read.st_dev, after_read.st_ino) != (
                        after_read_path.st_dev,
                        after_read_path.st_ino,
                    ):
                        raise ValidationError(
                            "Context source changed while being read", details={"path": relative}
                        )
            except OSError as err:
                raise ValidationError(
                    "Context source could not be read safely", details={"path": relative}
                ) from err
            if len(content) > remaining:
                raise ValidationError(
                    "Selected context exceeds total byte limit",
                    details={"limit": self.max_total_bytes},
                )
            if _SECRET_CONTENT.search(content.decode("utf-8", errors="ignore")):
                raise ValidationError(
                    "Secret-like content cannot be included in agent context",
                    details={"path": relative},
                )
            source = SourceReference(
                path=relative,
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                purpose=selection.purpose.strip(),
            )
            items.append(ContextItem(source, content))
            total += len(content)
        return BoundedContext(tuple(items), total)
