"""Independent PNG decode and reference-fixture region checks.

Engine-reported dimensions, paths, and hashes are not authoritative.
Fixture region rules stay in this adapter and are not core domain policy.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import TypedDict

from PIL import Image, UnidentifiedImageError

MAX_PNG_BYTES = 4 * 1024 * 1024
MAX_PIXELS = 1280 * 1280
CHANNEL_TOLERANCE = 12
FILL = (51, 191, 71)
EMPTY = (51, 51, 56)
ENEMY = (217, 56, 46)
ARENA = (31, 36, 46)
FIXTURE_SCENARIOS = frozenset({"visual-fixture", "visual-fixture-portrait"})


class _Layout(TypedDict):
    bar: tuple[int, int, int, int]
    enemies: tuple[tuple[int, int, int, int], ...]


LAYOUTS: dict[tuple[int, int], _Layout] = {
    (1280, 720): {
        "bar": (48, 36, 480, 32),
        "enemies": ((220, 280, 100, 100), (520, 280, 100, 100), (820, 280, 100, 100)),
    },
    (720, 1280): {
        "bar": (36, 48, 400, 36),
        "enemies": ((70, 520, 90, 90), (280, 520, 90, 90), (490, 520, 90, 90)),
    },
}


@dataclass(frozen=True)
class DecodedImage:
    width: int
    height: int
    mode: str
    sha256: str
    size: int
    image: Image.Image


class ImageValidationError(ValueError):
    pass


def decode_png(raw: bytes, expected_width: int, expected_height: int) -> DecodedImage:
    """Fully decode one SDR PNG and reject truncated, oversized, or wrong-size files."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ImageValidationError("file is not a PNG")
    if len(raw) > MAX_PNG_BYTES:
        raise ImageValidationError("PNG exceeds the byte limit")
    if expected_width * expected_height > MAX_PIXELS:
        raise ImageValidationError("requested viewport exceeds the pixel limit")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if image.format != "PNG":
                raise ImageValidationError("decoded image is not a PNG")
            if image.mode not in ("RGB", "RGBA"):
                raise ImageValidationError(f"unsupported image mode {image.mode}")
            if image.width != expected_width or image.height != expected_height:
                raise ImageValidationError(
                    f"image size {image.width}x{image.height} does not match "
                    f"{expected_width}x{expected_height}"
                )
            copied = image.copy()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageValidationError(f"PNG could not be fully decoded: {exc}") from exc
    return DecodedImage(
        width=copied.width,
        height=copied.height,
        mode=copied.mode,
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
        image=copied,
    )


def _pixel(image: Image.Image, point: tuple[int, int]) -> tuple[int, ...]:
    value = image.getpixel(point)
    if not isinstance(value, tuple):
        raise ImageValidationError("pixel sample was not a color tuple")
    return tuple(int(channel) for channel in value)


def _near(pixel: tuple[int, ...], expected: tuple[int, int, int]) -> bool:
    if len(pixel) < 3:
        return False
    if len(pixel) > 3 and pixel[3] < 250:
        return False
    return all(abs(int(pixel[index]) - expected[index]) <= CHANNEL_TOLERANCE for index in range(3))


def check_fixture_regions(image: Image.Image, state: dict[str, int]) -> list[dict[str, object]]:
    """Compare reference-scene regions with the observed gameplay state."""
    layout = LAYOUTS.get((image.width, image.height))
    if layout is None:
        raise ImageValidationError("fixture region checks have no layout for this viewport")
    findings: list[dict[str, object]] = []
    bar_x, bar_y, bar_w, bar_h = layout["bar"]
    hp = int(state["player_hp"])
    fill_at = (bar_x + 8, bar_y + bar_h // 2)
    tail_at = (bar_x + bar_w - 8, bar_y + bar_h // 2)
    fill_pixel = _pixel(image, fill_at)
    tail_pixel = _pixel(image, tail_at)
    tail_expected = FILL if hp >= 100 else EMPTY
    findings.append(
        {
            "id": "health-fill",
            "status": "PASS" if _near(fill_pixel, FILL) else "FAIL",
            "detail": f"hp={hp} pixel={fill_pixel}",
        }
    )
    findings.append(
        {
            "id": "health-tail",
            "status": "PASS" if _near(tail_pixel, tail_expected) else "FAIL",
            "detail": f"hp={hp} pixel={tail_pixel} expected={tail_expected}",
        }
    )
    enemies = int(state["enemies_remaining"])
    for index, (ex, ey, ew, eh) in enumerate(layout["enemies"]):
        pixel = _pixel(image, (ex + ew // 2, ey + eh // 2))
        expected = ENEMY if index < enemies else ARENA
        findings.append(
            {
                "id": f"enemy-{index + 1}",
                "status": "PASS" if _near(pixel, expected) else "FAIL",
                "detail": f"enemies={enemies} pixel={pixel} expected={expected}",
            }
        )
    return findings
