"""Unit tests for PathGuard directory containment and escape prevention."""

from pathlib import Path

import pytest

from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.execution.path_guard import PathGuard


class TestPathGuard:
    def test_safe_relative_path(self, tmp_path: Path) -> None:
        guard = PathGuard(tmp_path)
        safe = guard.resolve_safe_path("assets/models/character.glb")
        assert safe == (tmp_path / "assets" / "models" / "character.glb").resolve()

    def test_traversal_attack_rejected(self, tmp_path: Path) -> None:
        guard = PathGuard(tmp_path)
        with pytest.raises(ValidationError, match="Path traversal detected"):
            guard.resolve_safe_path("../../outside.txt")

    def test_traversal_with_nested_dots(self, tmp_path: Path) -> None:
        guard = PathGuard(tmp_path)
        with pytest.raises(ValidationError, match="Path traversal detected"):
            guard.resolve_safe_path("sub/dir/../../../../sensitive.dat")

    def test_unc_path_rejected(self, tmp_path: Path) -> None:
        guard = PathGuard(tmp_path)
        with pytest.raises(ValidationError, match="UNC paths are forbidden"):
            guard.resolve_safe_path(r"\\remote-server\share\file.txt")

    def test_empty_path_rejected(self, tmp_path: Path) -> None:
        guard = PathGuard(tmp_path)
        with pytest.raises(ValidationError, match="cannot be empty"):
            guard.resolve_safe_path("   ")

    def test_ensure_safe_parent_creates_directories(self, tmp_path: Path) -> None:
        guard = PathGuard(tmp_path)
        safe = guard.ensure_safe_parent("nested/folder/target.json")
        assert safe.parent.exists()
        assert safe.parent.is_dir()

    def test_symlink_escape_rejected_if_pointing_outside(self, tmp_path: Path) -> None:
        outside_dir = tmp_path.parent / "outside_jail"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("classified", encoding="utf-8")

        jail_dir = tmp_path / "jail"
        jail_dir.mkdir()
        symlink_path = jail_dir / "link_to_outside"

        try:
            symlink_path.symlink_to(outside_file)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not supported or permitted in this test environment")

        guard = PathGuard(jail_dir)
        with pytest.raises(
            ValidationError, match="Symlink escape detected|Path traversal detected"
        ):
            guard.resolve_safe_path("link_to_outside")

    @pytest.mark.parametrize("target", ["safe.txt:secret", "CON", "NUL.txt", "folder/name. "])
    def test_windows_special_paths_rejected_on_all_platforms(
        self, tmp_path: Path, target: str
    ) -> None:
        guard = PathGuard(tmp_path)
        with pytest.raises(ValidationError):
            guard.resolve_safe_path(target)

    def test_symlink_parent_escape_rejected_for_missing_leaf(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        jail = tmp_path / "jail"
        jail.mkdir()
        try:
            (jail / "linked").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not supported or permitted in this test environment")
        with pytest.raises(ValidationError):
            PathGuard(jail).resolve_safe_path("linked/new-file")
