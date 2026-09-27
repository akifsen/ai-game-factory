"""Capture contract, PNG decode, and static review page checks."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from gamefactory.adapters.engines.godot_capture_contracts import load_capture_scenario
from gamefactory.adapters.engines.godot_contracts import load_scenario
from gamefactory.adapters.engines.godot_image import (
    ImageValidationError,
    check_fixture_regions,
    decode_png,
)
from gamefactory.adapters.engines.godot_review_html import render_review_page

LANDSCAPE = "examples/godot-verification/visual-scenario.json"
PORTRAIT = "examples/godot-verification/visual-scenario-portrait.json"
HEADLESS = "examples/godot-verification/scenario.json"


def _png(width: int, height: int, color: tuple[int, int, int, int] = (31, 36, 46, 255)) -> bytes:
    image = Image.new("RGBA", (width, height), color)
    raw = io.BytesIO()
    image.save(raw, format="PNG")
    return raw.getvalue()


def test_v02_scenario_stays_headless_and_capture_schema_rejects_other_sizes() -> None:
    headless = load_scenario(HEADLESS)
    assert headless.schema_version == "0.2.0"
    landscape = load_capture_scenario(LANDSCAPE)
    portrait = load_capture_scenario(PORTRAIT)
    assert (landscape.viewport.width, landscape.viewport.height) == (1280, 720)
    assert (portrait.viewport.width, portrait.viewport.height) == (720, 1280)
    payload = landscape.model_dump(mode="json")
    payload["viewport"] = {"width": 1920, "height": 1080}
    with pytest.raises(ValueError):
        load_capture_scenario_from(payload)


def load_capture_scenario_from(payload: dict[str, object]) -> None:
    import json
    from pathlib import Path
    from tempfile import NamedTemporaryFile

    with NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump(payload, handle)
        name = handle.name
    try:
        load_capture_scenario(Path(name))
    finally:
        Path(name).unlink(missing_ok=True)


def test_binary_read_preserves_png_signature(tmp_path: Path) -> None:
    from gamefactory.adapters.engines.godot_execution import _read_bounded_regular_file

    payload = _png(32, 32)
    path = tmp_path / "sample.png"
    path.write_bytes(payload)
    read = _read_bounded_regular_file(path, tmp_path, 1024 * 1024)
    assert read.startswith(b"\x89PNG\r\n\x1a\n")
    assert read == payload


def test_png_decode_rejects_header_only_and_wrong_size() -> None:
    good = decode_png(_png(1280, 720), 1280, 720)
    assert good.mode == "RGBA"
    with pytest.raises(ImageValidationError):
        decode_png(b"\x89PNG\r\n\x1a\n" + b"not-a-body", 1280, 720)
    with pytest.raises(ImageValidationError):
        decode_png(_png(100, 100), 1280, 720)


def test_fixture_roi_fails_when_health_bar_ignores_hp() -> None:
    image = Image.new("RGBA", (1280, 720), (31, 36, 46, 255))
    for x in range(48, 528):
        for y in range(36, 68):
            image.putpixel((x, y), (51, 191, 71, 255))
    findings = check_fixture_regions(image, {"player_hp": 40, "enemies_remaining": 2, "score": 100})
    assert any(item["id"] == "health-tail" and item["status"] == "FAIL" for item in findings)


def test_review_html_escapes_markup_and_rejects_remote_images() -> None:
    page = render_review_page(
        {
            "title": "<script>alert(1)</script>",
            "project_id": "p",
            "scenario_id": "visual-fixture",
            "technical_status": "PASS",
            "human_status": "PENDING",
            "environment": "local",
            "commands": "gamefactory approve APPROVAL_ID",
            "images": [
                {
                    "id": "cp-000",
                    "tick": 0,
                    "src": "images/cp-000.png",
                    "width": 1280,
                    "height": 720,
                    "sha256": "abc",
                    "state": {"player_hp": 100, "enemies_remaining": 3, "score": 0},
                }
            ],
        }
    )
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    assert "https://" not in page
    assert 'src="images/cp-000.png"' in page
    with pytest.raises(ValueError):
        render_review_page(
            {
                "images": [
                    {
                        "id": "x",
                        "tick": 0,
                        "src": "https://example.invalid/a.png",
                        "width": 1,
                        "height": 1,
                    }
                ]
            }
        )
