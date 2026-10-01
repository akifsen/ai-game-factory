"""Staging safety checks for V0.8-3B candidate runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.adapters.engines.v08_candidate_runtime_runner import (
    CandidateRuntimeStageError,
    _assert_fresh_stage_dir,
)


def test_path_crosses_symlink_parent(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link_parent = tmp_path / "link_parent"
    if os.name == "nt":
        try:
            link_parent.symlink_to(real, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation unavailable")
    else:
        link_parent.symlink_to(real)
    stage = link_parent / "stage"
    with pytest.raises(CandidateRuntimeStageError, match="link|junction|symlink"):
        _assert_fresh_stage_dir(stage)


def test_path_crosses_junction_when_is_junction_api_missing(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows junction semantics")
    import subprocess
    from unittest.mock import patch

    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    try:
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("junction creation unavailable")
    with patch.object(Path, "is_junction", None, create=True):
        assert path_crosses_link(junction / "nested")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction check")
def test_path_crosses_junction_when_available(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    try:
        import subprocess

        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("junction creation unavailable without privilege")
    assert path_crosses_link(junction / "nested")
    assert path_crosses_link(junction / "deep" / "nested" / "stage")


def test_reviewed_harness_pin_stable_under_crlf_raw_change(tmp_path: Path) -> None:
    from gamefactory.adapters.assets.internal_rig_canonical import reviewed_text_sha256
    from gamefactory.adapters.assets.v08_candidate_runtime_pins import (
        PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256,
        packaged_candidate_harness_path,
    )

    source = packaged_candidate_harness_path()
    reviewed, raw_lf = reviewed_text_sha256(source)
    assert reviewed == PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256
    crlf_copy = tmp_path / "harness_crlf.gd"
    crlf_copy.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    reviewed_crlf, raw_crlf = reviewed_text_sha256(crlf_copy)
    assert reviewed_crlf == reviewed
    assert raw_crlf != raw_lf
