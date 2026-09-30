from __future__ import annotations

from pathlib import Path

import pytest

from gamefactory.adapters.dcc.windows_paths import (
    MAX_WINDOWS_PATH_UTF16_UNITS,
    ensure_windows_output_paths,
)
from gamefactory.core.domain.errors import ValidationError


def _path_with_utf16_length(length: int) -> str:
    prefix = "C:\\"
    return prefix + ("x" * (length - len(prefix)))


def test_windows_path_preflight_accepts_maximum_visible_length() -> None:
    path = _path_with_utf16_length(MAX_WINDOWS_PATH_UTF16_UNITS)
    ensure_windows_output_paths((("report", path),), windows=True)


def test_windows_path_preflight_rejects_first_over_limit_path_actionably() -> None:
    path = _path_with_utf16_length(MAX_WINDOWS_PATH_UTF16_UNITS + 1)
    with pytest.raises(ValidationError, match="Shorten the project, workflow, or asset path"):
        ensure_windows_output_paths((("staged report", path),), windows=True)


def test_windows_path_preflight_counts_unicode_as_utf16_code_units() -> None:
    accepted = Path("C:\\" + "😀" * 128)  # 3 + 256 = 259 UTF-16 units.
    rejected = Path("C:\\" + "😀" * 129)
    ensure_windows_output_paths((("Unicode path", accepted),), windows=True)
    with pytest.raises(ValidationError, match="UTF-16 code units"):
        ensure_windows_output_paths((("Unicode path", rejected),), windows=True)


def test_windows_path_preflight_is_a_noop_on_posix() -> None:
    ensure_windows_output_paths((("long POSIX path", "/" + "x" * 1000),), windows=False)


def test_blender_processor_rejects_over_limit_windows_paths_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The V0.6 MAX_PATH limitation now fails before Blender starts."""
    from gamefactory.adapters.dcc import blender_processor, windows_paths
    from gamefactory.adapters.fakes.glb_generator import create_box_glb
    from gamefactory.core.domain.asset_contracts import parse_asset_specification

    real = windows_paths.ensure_windows_output_paths
    monkeypatch.setattr(
        blender_processor,
        "ensure_windows_output_paths",
        lambda paths: real(paths, windows=True),
    )
    spec = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    raw = tmp_path / "raw.glb"
    create_box_glb(output_path=raw)
    deep = tmp_path / ("d" * 240) / "processed.glb"
    launched: list[object] = []

    class Runner:
        def run(self, request: object) -> object:
            launched.append(request)
            raise AssertionError("Blender must not launch")

    processor = blender_processor.BlenderAssetProcessor("/nonexistent/blender", Runner())  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="Shorten the project"):
        processor.process_asset(raw, deep, spec)
    assert launched == []
    assert not deep.parent.exists()
