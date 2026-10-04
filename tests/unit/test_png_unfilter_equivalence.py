from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
VERIFIERS = (
    REPO / "scripts" / "verify_candidate_bundle.py",
    REPO / "src/gamefactory/resources/scripts/verify_candidate_bundle.py",
)


def _load_verifier(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=enumerate(VERIFIERS), ids=("checkout", "packaged"))
def unfilter(request: pytest.FixtureRequest):
    index, path = request.param
    return _load_verifier(path, f"png_unfilter_verifier_{index}")._png_unfilter_row


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    distances = (abs(p - a), abs(p - b), abs(p - c))
    return (a, b, c)[distances.index(min(distances))]


def _encode_row(filter_type: int, pixels: bytes, previous: bytes, bpp: int) -> bytes:
    """Independent forward PNG filter, used as an oracle for the decoder."""
    encoded = bytearray(len(pixels))
    for i, pixel in enumerate(pixels):
        left = pixels[i - bpp] if i >= bpp else 0
        up = previous[i] if previous else 0
        up_left = previous[i - bpp] if previous and i >= bpp else 0
        if filter_type == 0:
            predictor = 0
        elif filter_type == 1:
            predictor = left
        elif filter_type == 2:
            predictor = up
        elif filter_type == 3:
            predictor = (left + up) // 2
        else:
            predictor = _paeth(left, up, up_left)
        encoded[i] = (pixel - predictor) & 0xFF
    return bytes(encoded)


@pytest.mark.parametrize("bpp", (3, 4))
@pytest.mark.parametrize("filter_type", range(5))
@pytest.mark.parametrize("pattern", ("zero", "uniform", "wrap", "ties", "random"))
def test_unfilter_matches_independent_encoder_oracle(unfilter, bpp, filter_type, pattern):
    rng = random.Random(0x504E47 + bpp * 17 + filter_type)
    size = bpp * 13
    previous = bytes(rng.randrange(256) for _ in range(size))
    if pattern == "zero":
        pixels = bytes(size)
    elif pattern == "uniform":
        pixels = bytes((91,)) * size
        previous = bytes((91,)) * size
    elif pattern == "wrap":
        pixels = bytes((250 + i) & 0xFF for i in range(size))
        previous = bytes((255 - i) & 0xFF for i in range(size))
    elif pattern == "ties":
        pixels = bytes((previous[i - bpp] if i >= bpp else 0) for i in range(size))
        previous = bytes((pixels[i - bpp] if i >= bpp else 0) for i in range(size))
    else:
        pixels = bytes(rng.randrange(256) for _ in range(size))

    filtered = _encode_row(filter_type, pixels, previous, bpp)
    assert unfilter(filter_type, filtered, previous, bpp) == pixels


@pytest.mark.parametrize("bpp", (3, 4))
@pytest.mark.parametrize("filter_type", range(5))
def test_first_row_and_deterministic_random_rows(unfilter, bpp, filter_type):
    rng = random.Random(0xC0FFEE + bpp * 13 + filter_type)
    previous = b""
    size = bpp * 24
    for _ in range(25):
        pixels = bytes(rng.randrange(256) for _ in range(size))
        filtered = _encode_row(filter_type, pixels, previous, bpp)
        assert unfilter(filter_type, filtered, previous, bpp) == pixels
        previous = pixels


def test_short_previous_preserves_index_error_and_filter_precedence(unfilter):
    with pytest.raises(IndexError):
        unfilter(1, b"\0\0", b"\x01", 3)
    with pytest.raises(IndexError):
        unfilter(4, b"\0\0", b"\x01", 3)
    with pytest.raises(ValueError, match="invalid scanline filter"):
        unfilter(5, b"", b"\x01", 3)
    assert unfilter(0, b"unchanged", b"\x01", 3) == b"unchanged"


@pytest.mark.parametrize("filter_type", (-1, -4))
def test_negative_internal_filter_keeps_paeth_fallback(unfilter, filter_type):
    row = b"\x07\x11\xf3\x82"
    previous = b"\x44\x7f\x12\x91"
    assert unfilter(filter_type, row, previous, 3) == unfilter(4, row, previous, 3)
