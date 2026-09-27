"""Canonical V0.5 source identity ignores generated metadata and newlines."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.source_identity import fingerprint, identify, is_binary  # noqa: E402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=source-identity@example.com",
            "-c",
            "user.name=source-identity",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    source = tmp_path / "src" / "gamefactory" / "module.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"value = 1\n")
    (tmp_path / "pyproject.toml").write_bytes(b"[project]\nname='demo'\n")
    _git(tmp_path, "add", "src/gamefactory/module.py", "pyproject.toml")
    _git(tmp_path, "commit", "-m", "seed")
    return tmp_path


def test_generated_egg_info_does_not_change_tracked_fingerprint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = identify(repo)
    egg = repo / "src" / "gamefactory.egg-info" / "PKG-INFO"
    egg.parent.mkdir()
    egg.write_text("Name: demo\n", encoding="utf-8")
    after = identify(repo)
    assert after.tracked_source_fingerprint == before.tracked_source_fingerprint
    assert after.unexpected_untracked_source_files == ()
    assert "src/gamefactory.egg-info/PKG-INFO" not in after.tracked_files


def test_crlf_and_lf_have_the_same_canonical_fingerprint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = identify(repo).tracked_source_fingerprint
    module = repo / "src" / "gamefactory" / "module.py"
    module.write_bytes(module.read_bytes().replace(b"\n", b"\r\n"))
    assert identify(repo).tracked_source_fingerprint == before


def test_real_tracked_content_change_changes_fingerprint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = identify(repo).tracked_source_fingerprint
    module = repo / "src" / "gamefactory" / "module.py"
    module.write_bytes(b"value = 2\n")
    assert identify(repo).tracked_source_fingerprint != before


def test_unexpected_untracked_python_source_fails_verification(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    extra = repo / "src" / "gamefactory" / "new_real_module.py"
    extra.write_bytes(b"value = 3\n")
    identity = identify(repo)
    assert identity.unexpected_untracked_source_files == ("src/gamefactory/new_real_module.py",)


def test_report_outside_scope_does_not_change_fingerprint(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = identify(repo).tracked_source_fingerprint
    report = repo / "docs" / "reports" / "note.md"
    report.parent.mkdir(parents=True)
    report.write_text("outside the candidate scope\n", encoding="utf-8")
    after = identify(repo)
    assert after.tracked_source_fingerprint == before
    assert after.unexpected_untracked_source_files == ()


def test_binary_bytes_are_not_newline_normalized() -> None:
    payload = b"a\r\nb\0c"
    assert is_binary(payload)
    same = fingerprint([("blob.bin", payload)])
    changed = fingerprint([("blob.bin", b"a\nb\0c")])
    assert same != changed
