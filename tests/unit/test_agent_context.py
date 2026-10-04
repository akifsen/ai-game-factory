from __future__ import annotations

import hashlib

import pytest

from gamefactory.agents.context import ContextBuilder, ContextSelection
from gamefactory.core.domain.errors import ValidationError


def test_context_reads_only_explicit_contained_files_and_records_provenance(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "design.md"
    source.write_text("small design", encoding="utf-8")
    context = ContextBuilder(root, max_total_bytes=100).build(
        [ContextSelection("design.md", "Design brief")]
    )
    assert context.total_bytes == len(b"small design")
    assert context.sources[0].sha256 == hashlib.sha256(b"small design").hexdigest()
    assert context.sources[0].purpose == "Design brief"


def test_context_rejects_traversal_secret_files_secret_content_and_oversize(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("API_KEY=secret", encoding="utf-8")
    (root / ".env.production").write_text("API_KEY=secret", encoding="utf-8")
    (root / "notes.md").write_text("TOKEN=secret", encoding="utf-8")
    (root / "large.md").write_text("x" * 11, encoding="utf-8")
    (root / "certificate.pem").write_text("private", encoding="utf-8")
    builder = ContextBuilder(root, max_total_bytes=10)
    for path in (
        "../outside",
        ".env",
        ".env.production",
        "notes.md",
        "large.md",
        "certificate.pem",
    ):
        with pytest.raises(ValidationError):
            builder.build([ContextSelection(path, "test")])


def test_context_rejects_symlink_source_and_parent(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    try:
        (root / "alias.md").symlink_to(outside)
        (root / "alias-dir").symlink_to(outside.parent, target_is_directory=True)
    except OSError as err:
        pytest.skip(f"symlink creation is unavailable on this platform: {err}")
    builder = ContextBuilder(root)
    for path in ("alias.md", "alias-dir/outside.md"):
        with pytest.raises(ValidationError):
            builder.build([ContextSelection(path, "symlink regression")])
