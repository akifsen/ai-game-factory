from __future__ import annotations

from pathlib import Path

import pytest

from gamefactory.adapters.dcc.blender_processor import BlenderAssetProcessor
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import AssetSpecification, parse_asset_specification


class _NeverRunRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, request) -> None:
        self.calls += 1
        raise AssertionError("Blender runner must not run for an unsafe destination")


def _inputs(
    tmp_path: Path,
) -> tuple[Path, AssetSpecification, _NeverRunRunner, BlenderAssetProcessor]:
    raw = tmp_path / "raw.glb"
    create_box_glb(output_path=raw)
    spec = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    runner = _NeverRunRunner()
    processor = BlenderAssetProcessor("blender-does-not-need-to-run.exe", runner)  # type: ignore[arg-type]
    return raw, spec, runner, processor


def test_processor_refuses_existing_output_or_report_without_modification(tmp_path: Path) -> None:
    raw, spec, runner, processor = _inputs(tmp_path)
    output = tmp_path / "processed.glb"
    output.write_bytes(b"keep output")
    with pytest.raises(ValueError, match="refusing to overwrite existing processed GLB"):
        processor.process_asset(raw, output, spec)
    assert output.read_bytes() == b"keep output"

    output.unlink()
    report = tmp_path / "report.json"
    report.write_text("keep report", encoding="utf-8")
    with pytest.raises(ValueError, match="refusing to overwrite existing processing report"):
        processor.process_asset(raw, output, spec, report_path=report)
    assert report.read_text(encoding="utf-8") == "keep report"
    assert runner.calls == 0


def test_processor_refuses_report_path_equal_to_raw(tmp_path: Path) -> None:
    raw, spec, runner, processor = _inputs(tmp_path)
    before = raw.read_bytes()
    with pytest.raises(ValueError, match="paths must be distinct"):
        processor.process_asset(raw, tmp_path / "processed.glb", spec, report_path=raw)
    assert raw.read_bytes() == before
    assert runner.calls == 0


def test_processor_refuses_symlink_target_without_running_blender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw, spec, runner, processor = _inputs(tmp_path)
    protected = tmp_path / "protected.glb"
    protected.write_bytes(b"preserve")
    link = tmp_path / "linked-output.glb"
    try:
        link.symlink_to(protected)
    except OSError:
        original_is_symlink = Path.is_symlink

        def pretend_symlink(path: Path) -> bool:
            return True if path == link else original_is_symlink(path)

        monkeypatch.setattr(Path, "is_symlink", pretend_symlink)
    with pytest.raises(ValueError, match="symlink or junction"):
        processor.process_asset(raw, link, spec)
    assert protected.read_bytes() == b"preserve"
    assert runner.calls == 0
