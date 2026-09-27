#!/usr/bin/env python3
"""Canonical fingerprint for the V0.5 candidate source scope.

The fingerprint covers Git-tracked files in the scope below. Generated install
metadata is excluded even when it sits on disk under ``src/``. Text files are
hashed after CRLF-to-LF normalization. Binary files are hashed unchanged.

Scope:

- ``src/``
- ``tests/``
- ``scripts/``
- ``pyproject.toml``
- ``.github/workflows/ci.yml``

Algorithm:

1. Take ``git ls-files`` for that scope.
2. Drop explicit generated paths (``*.egg-info``, ``__pycache__``, ``.pyc``).
3. Normalize paths to repository-relative POSIX form and sort them.
4. SHA-256 the canonical bytes of each file.
5. SHA-256 the lines ``{path}\\0{file_sha256}\\n``.

A file is binary when its first 8000 bytes contain a NUL. That is the same
heuristic Git uses. Text newline normalization is ``\\r\\n`` to ``\\n`` only.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SCOPE_PREFIXES = ("src/", "tests/", "scripts/")
SCOPE_FILES = ("pyproject.toml", ".github/workflows/ci.yml")
GENERATED_DIR_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
GENERATED_SUFFIXES = frozenset({".pyc", ".pyo"})
_NUL_PROBE = 8000


@dataclass(frozen=True)
class SourceIdentity:
    """Tracked fingerprint plus source files that Git does not yet track."""

    tracked_files: tuple[str, ...]
    tracked_source_fingerprint: str
    unexpected_untracked_source_files: tuple[str, ...]

    @property
    def tracked_file_count(self) -> int:
        return len(self.tracked_files)


def is_generated(relative: str) -> bool:
    """Return whether a POSIX path is explicit build or cache metadata."""
    path = PurePosixPath(relative)
    if path.suffix in GENERATED_SUFFIXES:
        return True
    return any(part in GENERATED_DIR_NAMES or part.endswith(".egg-info") for part in path.parts)


def in_scope(relative: str) -> bool:
    """Return whether a POSIX path belongs to the V0.5 candidate scope."""
    if relative in SCOPE_FILES:
        return True
    return relative.startswith(SCOPE_PREFIXES)


def is_binary(payload: bytes) -> bool:
    """Return whether payload should skip newline normalization."""
    return b"\0" in payload[:_NUL_PROBE]


def canonical_bytes(payload: bytes) -> bytes:
    """Return LF-normalized text, or the original bytes for a binary file."""
    if is_binary(payload):
        return payload
    return payload.replace(b"\r\n", b"\n")


def fingerprint(entries: list[tuple[str, bytes]]) -> str:
    """Hash sorted POSIX path and canonical-content pairs."""
    digest = hashlib.sha256()
    for relative, payload in sorted(entries, key=lambda item: item[0]):
        file_hash = hashlib.sha256(canonical_bytes(payload)).hexdigest()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(file_hash.encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=repo)


def tracked_scope_files(repo: Path) -> list[str]:
    """Return sorted Git-tracked scope paths, without generated metadata."""
    output = _git(
        repo,
        "ls-files",
        "-z",
        "--",
        "src",
        "tests",
        "scripts",
        "pyproject.toml",
        ".github/workflows/ci.yml",
    )
    paths = [item.decode().replace("\\", "/") for item in output.split(b"\0") if item]
    selected = [path for path in paths if in_scope(path) and not is_generated(path)]
    return sorted(set(selected))


def _walk_scope(repo: Path) -> list[str]:
    found: list[str] = []
    for name in ("src", "tests", "scripts"):
        root = repo / name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(repo).as_posix()
            if is_generated(relative):
                continue
            found.append(relative)
    for name in SCOPE_FILES:
        if (repo / name).is_file():
            found.append(name)
    return sorted(set(found))


def unexpected_untracked(repo: Path, tracked: set[str]) -> list[str]:
    """Return on-disk scope files that are real source and not Git-tracked."""
    return [path for path in _walk_scope(repo) if path not in tracked]


def identify(repo: Path) -> SourceIdentity:
    """Fingerprint the worktree bytes of tracked scope files."""
    tracked = tracked_scope_files(repo)
    entries = [(relative, (repo / relative).read_bytes()) for relative in tracked]
    return SourceIdentity(
        tracked_files=tuple(tracked),
        tracked_source_fingerprint=fingerprint(entries),
        unexpected_untracked_source_files=tuple(unexpected_untracked(repo, set(tracked))),
    )


def main() -> None:
    identity = identify(Path("."))
    print(f"tracked_file_count {identity.tracked_file_count}")
    print(f"tracked_source_fingerprint {identity.tracked_source_fingerprint}")
    print(f"unexpected_untracked_source_files {len(identity.unexpected_untracked_source_files)}")
    for relative in identity.unexpected_untracked_source_files:
        print(f"UNTRACKED {relative}")


if __name__ == "__main__":
    main()
