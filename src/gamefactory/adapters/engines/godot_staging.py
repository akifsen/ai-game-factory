"""Bounded, attempt-scoped staging for trusted local Godot projects."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.execution.path_guard import PathGuard, assert_managed_directory

_SKIP_DIRS = {
    ".git",
    ".godot",
    ".gamefactory",
    ".hg",
    ".svn",
    ".venv",
    ".verify-venv",
    ".verification",
    ".ssh",
    ".aws",
    ".azure",
    ".gnupg",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "cache",
    ".cache",
    "__pycache__",
    "node_modules",
    "venv",
}
_SECRET_NAMES = re.compile(
    r"(^|[._-])(secrets?|credentials?|tokens?|passwords?|private(?:[_-]?keys?)?)([._-]|$)",
    re.I,
)
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
_SECRET_FILES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "authorized_keys",
    "id_rsa",
    "id_ed25519",
}
_MAX_FILES = 10_000
_MAX_BYTES = 512 * 1024 * 1024


def _is_reparse(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    if os.name == "nt":
        try:
            return bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
        except FileNotFoundError:
            return False
    return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_source(path: Path, expected_size: int) -> str:
    digest = hashlib.sha256()
    consumed = 0
    with path.open("rb") as stream:
        while chunk := stream.read(min(1024 * 1024, expected_size + 1 - consumed)):
            consumed += len(chunk)
            if consumed > expected_size or consumed > _MAX_BYTES:
                raise ValidationError(f"Godot source grew beyond its staging limit: {path}")
            digest.update(chunk)
    if consumed != expected_size:
        raise ValidationError(f"Godot source size changed while hashing: {path}")
    return digest.hexdigest()


@dataclass(frozen=True)
class SourceFile:
    relative_path: str
    sha256: str
    size: int


class GodotStager:
    """Copy bounded source into Factory-owned scratch and bind a manifest hash."""

    def __init__(self, project_root: Path | str, scratch_root: Path | str) -> None:
        original_root = Path(project_root)
        if _is_reparse(original_root):
            raise ValidationError("Godot project root cannot be a symlink or junction")
        self.project_root = original_root.resolve(strict=True)
        self.scratch_root = Path(scratch_root)

    def source_manifest(
        self, excluded_file: Path | str | None = None
    ) -> tuple[list[SourceFile], str]:
        excluded = Path(excluded_file).resolve(strict=True) if excluded_file else None
        files: list[SourceFile] = []
        total = 0

        def walk_error(error: OSError) -> None:
            raise ValidationError(f"Cannot enumerate Godot project source: {error}") from error

        for directory, child_dirs, filenames in os.walk(
            self.project_root, topdown=True, followlinks=False, onerror=walk_error
        ):
            current = Path(directory)
            kept_dirs: list[str] = []
            for name in sorted(child_dirs):
                candidate = current / name
                if name.lower() in _SKIP_DIRS:
                    continue
                if _is_reparse(candidate):
                    raise ValidationError(f"Godot project contains a link or junction: {candidate}")
                kept_dirs.append(name)
            child_dirs[:] = kept_dirs
            for name in sorted(filenames):
                path = current / name
                if _is_reparse(path):
                    raise ValidationError(f"Godot project contains a link or junction: {path}")
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(self.project_root)
                except (OSError, ValueError) as exc:
                    raise ValidationError(f"Godot project source escapes its root: {path}") from exc
                if excluded is not None and resolved == excluded:
                    continue
                # Godot always reads root override.cfg. Keep it outside the runtime
                # copy so it cannot undo attempt-specific user-data settings.
                if path.parent == self.project_root and name.lower() == "override.cfg":
                    continue
                if (
                    name.lower() == ".env"
                    or name.lower().startswith(".env.")
                    or name.lower() in _SECRET_FILES
                    or name.lower().startswith(("id_rsa.", "id_ed25519."))
                ):
                    continue
                if _SECRET_NAMES.search(name) or path.suffix.lower() in _SECRET_SUFFIXES:
                    continue
                relative = path.relative_to(self.project_root).as_posix()
                file_stat = path.stat()
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ValidationError(f"Godot project source must be a regular file: {path}")
                size = file_stat.st_size
                total += size
                if len(files) >= _MAX_FILES or total > _MAX_BYTES:
                    raise ValidationError("Godot project exceeds the staging file or byte limit")
                files.append(SourceFile(relative, _sha256_source(path, size), size))
        files.sort(key=lambda item: item.relative_path)
        digest = hashlib.sha256()
        for item in files:
            digest.update(f"{item.relative_path}\0{item.size}\0{item.sha256}\n".encode())
        return files, digest.hexdigest()

    def create_stage(
        self,
        workflow_id: str,
        execution_id: str,
        scenario_path: Path | str,
        expected_manifest_hash: str,
        harness_bytes: bytes,
    ) -> tuple[Path, list[SourceFile], str]:
        """Stage only an unchanged source snapshot; never merge with prior attempt data."""
        safe_component = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
        if not safe_component.fullmatch(workflow_id) or not safe_component.fullmatch(execution_id):
            raise ValidationError("Workflow and execution IDs must be safe single path components")
        scratch_relative = self.scratch_root.relative_to(self.project_root)
        owned_relative = scratch_relative / workflow_id / execution_id
        guard = PathGuard(self.project_root)
        current = self.project_root
        for index, component in enumerate(owned_relative.parts):
            current = current / component
            if _is_reparse(current):
                raise ValidationError(
                    f"Factory scratch path contains a link or junction: {current}"
                )
            guard.resolve_safe_path(current)
            exists = current.exists()
            if index == len(owned_relative.parts) - 1 and exists:
                raise ValidationError(
                    "Attempt scratch already exists; refusing to reuse staged state"
                )
            if not exists:
                current.mkdir()
            elif not current.is_dir():
                raise ValidationError(f"Factory scratch path is not a directory: {current}")
        attempt = current
        if _is_reparse(attempt):
            raise ValidationError("Current attempt scratch cannot be a link or junction")
        if any(_is_reparse(parent) for parent in (self.project_root / scratch_relative,)):
            raise ValidationError("Factory scratch root cannot be a link or junction")
        # The path has been created one component at a time only after checking each parent.
        assert_managed_directory(self.project_root, owned_relative)
        stage = attempt / "project"
        if stage.exists() or _is_reparse(stage):
            raise ValidationError("Current attempt already contains a staged project")
        stage.mkdir()
        files, manifest_hash = self.source_manifest(scenario_path)
        if manifest_hash != expected_manifest_hash:
            raise ValidationError("Godot project sources changed after approval")
        for item in files:
            source = self.project_root / Path(item.relative_path)
            destination = stage / Path(item.relative_path)
            PathGuard(stage).resolve_safe_path(item.relative_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive create avoids overwriting any unexpected stage content.
            copied = 0
            with source.open("rb") as src, destination.open("xb") as dst:
                while chunk := src.read(min(1024 * 1024, item.size + 1 - copied)):
                    copied += len(chunk)
                    if copied > item.size or copied > _MAX_BYTES:
                        raise ValidationError(
                            f"Godot source grew beyond its staging limit: {item.relative_path}"
                        )
                    dst.write(chunk)
            if copied != item.size:
                raise ValidationError(
                    f"Godot source size changed while staging: {item.relative_path}"
                )
            if (
                _sha256_source(source, item.size) != item.sha256
                or _sha256_source(destination, item.size) != item.sha256
            ):
                raise ValidationError(
                    f"Godot project source changed while staging: {item.relative_path}"
                )
        project_file = stage / "project.godot"
        if not project_file.is_file():
            raise ValidationError("Selected source is not a Godot project (project.godot missing)")
        harness_path = stage / ".factory-harness.gd"
        with harness_path.open("xb") as handle:
            handle.write(harness_bytes)
        self._isolate_user_data(project_file, f"factory-{execution_id.lower()}")
        return stage, files, manifest_hash

    @staticmethod
    def _isolate_user_data(project_file: Path, name: str) -> None:
        text = project_file.read_text(encoding="utf-8")
        section_re = re.compile(r"(?ms)^\[application\]\s*\n(.*?)(?=^\[|\Z)")
        match = section_re.search(text)
        settings = {
            "config/use_custom_user_dir": "true",
            "config/custom_user_dir_name": f'"{name}"',
            # Setting this to true also disables the verified --script entrypoint.
            "config/disable_project_settings_override": "false",
            "config/project_settings_override": '""',
        }
        if match:
            block = match.group(1)
            for key, value in settings.items():
                # Remove feature-qualified overrides of these controlled values too.
                line_re = re.compile(rf"(?m)^{re.escape(key)}(?:\.[^=\s]+)?\s*=.*$")
                block = line_re.sub("", block)
                block += f"{key}={value}\n"
            text = text[: match.start(1)] + block + text[match.end(1) :]
        else:
            text += "\n[application]\n" + "".join(
                f"{key}={value}\n" for key, value in settings.items()
            )
        project_file.write_text(text, encoding="utf-8", newline="\n")
