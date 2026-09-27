"""Filesystem safety guard for paths managed beneath a project root."""

import re
from pathlib import Path

from gamefactory.core.domain.errors import ValidationError

_DEVICE_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)


class PathGuard:
    """Enforces lexical and resolved containment, including existing symlink parents."""

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir).resolve()
        if not self.base_dir.is_absolute():
            raise ValidationError("Managed root must resolve to an absolute path")

    def _validate_raw(self, raw: str) -> None:
        if not raw:
            raise ValidationError("Target path cannot be empty")
        if "\x00" in raw:
            raise ValidationError("Target path contains a null byte")
        if raw.startswith(("\\\\", "//", "\\?\\", "\\.\\")):
            reason = (
                "UNC paths are forbidden"
                if raw.startswith(("\\\\", "//"))
                else "Device paths are forbidden"
            )
            raise ValidationError(reason, details={"target_path": raw})
        # Colon in a relative component enables Windows drive changes and NTFS ADS.
        parts = re.split(r"[\\/]", raw)
        for index, part in enumerate(parts):
            if not part or part in (".", ".."):
                continue
            if ":" in part and not (index == 0 and re.fullmatch(r"[A-Za-z]:", part)):
                raise ValidationError(
                    "Drive and alternate data stream paths are forbidden",
                    details={"target_path": raw},
                )
            if part.endswith((".", " ")):
                raise ValidationError(
                    "Windows paths ending in a dot or space are forbidden",
                    details={"target_path": raw},
                )
            if _DEVICE_NAME.match(part):
                raise ValidationError(
                    "Windows device paths are forbidden", details={"target_path": raw}
                )

    def resolve_safe_path(
        self, target_path: Path | str, allow_symlinks_outside: bool = False
    ) -> Path:
        """Resolve a path under the root; ``allow_symlinks_outside`` is retained for API compatibility."""
        raw_str = str(target_path).strip()
        self._validate_raw(raw_str)
        target = Path(raw_str)
        candidate = target if target.is_absolute() else self.base_dir / target
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.base_dir)
        except (ValueError, OSError, RuntimeError) as err:
            raise ValidationError(
                f"Path traversal detected: path '{raw_str}' resolves outside base directory '{self.base_dir}'",
                details={"target_path": raw_str, "base_dir": str(self.base_dir)},
            ) from err
        # resolve(strict=False) follows every existing symlink/junction in the path,
        # including parents of a not-yet-created leaf. Never permit an escape.
        if not allow_symlinks_outside:
            try:
                resolved.relative_to(self.base_dir)
            except ValueError as err:
                raise ValidationError(
                    "Symlink escape detected", details={"target_path": raw_str}
                ) from err
        return resolved

    def ensure_safe_parent(self, target_path: Path | str) -> Path:
        """Create a contained parent then resolve again in case the filesystem changed."""
        safe_path = self.resolve_safe_path(target_path)
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        return self.resolve_safe_path(safe_path)


def assert_managed_directory(project_root: Path | str, relative_path: Path | str) -> Path:
    """Resolve a managed subdirectory against the project root trust boundary.

    Existing junctions and symlinks are resolved before containment is checked;
    callers should invoke this before creating or using managed state directories.
    """
    guard = PathGuard(project_root)
    path = guard.resolve_safe_path(relative_path)
    if path.exists() and not path.is_dir():
        raise ValidationError(
            "Managed path exists but is not a directory", details={"path": str(path)}
        )
    return path
