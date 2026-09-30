#!/usr/bin/env python3
"""Cold, standard-library-only verification of a portable V0.4 asset bundle.

Run ``python -I scripts/verify_asset_bundle.py BUNDLE`` in any directory. The
manifest and all bundled content are untrusted. This checks internal evidence
consistency; it cannot authenticate the named human without an external trust
anchor.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
import sys
import zlib
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any, cast

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SINGLE_ROLES = {
    "specification",
    "concept",
    "concept_provenance",
    "concept_approval",
    "paid_approval",
    "provider_operation",
    "cost_record",
    "raw_glb",
    "processed_glb",
    "processing_report",
    "validation",
    "runtime_observation",
    "review_html",
    "final_approval",
}
_ANGLES = {"front", "three_quarter", "side"}
_OPTIONAL_ROLES = {"side_correction"}
_EXCLUDED = {"manifest.json", "verify_asset_bundle.py"}
_LINKED_ROLES = _SINGLE_ROLES - {"review_html"}
_MAX_FILE_BYTES = 100_000_000
_MAX_BUNDLE_BYTES = 512_000_000
_MAX_JSON_BYTES = 4_000_000
_MAX_FILES = 96
_V07_VIEWS = {
    "front",
    "rear",
    "left",
    "right",
    "side",
    "three_quarter",
    "three_quarter_front",
    "three_quarter_rear",
    "top",
}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _approval_hash(task_id: str, approval_type: str, inputs: Any) -> str:
    raw = json.dumps(
        {"task_id": task_id, "approval_type": approval_type, "inputs": inputs},
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(raw)


def _spec_fingerprint(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _sha256(raw)


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"unsafe bundle path: {value!r}")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} or ":" in part for part in value.split("/"))
    ):
        raise ValueError(f"unsafe bundle path: {value!r}")
    return value


def _linked(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _object(path: Path) -> dict[str, Any]:
    if path.stat().st_size > _MAX_JSON_BYTES:
        raise ValueError(f"JSON file exceeds size limit: {path.name}")
    with path.open("rb") as stream:
        raw = stream.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError(f"JSON file exceeds size limit: {path.name}")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def nonfinite(token: str) -> None:
        raise ValueError(f"non-finite JSON value: {token}")

    value = json.loads(
        raw.decode("utf-8"), object_pairs_hook=unique_pairs, parse_constant=nonfinite
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path.name}")
    return value


class _References(HTMLParser):
    """Allow inert static HTML and gather every local link target."""

    _TAGS = {
        "html",
        "head",
        "title",
        "body",
        "main",
        "header",
        "footer",
        "section",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "div",
        "span",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "caption",
        "ul",
        "ol",
        "li",
        "a",
        "img",
        "pre",
        "code",
        "strong",
        "em",
        "b",
        "i",
        "small",
        "br",
        "hr",
        "meta",
    }
    _GLOBAL_ATTRS = {"id", "class", "title"}
    _ATTRS = {
        "html": {"lang"},
        "meta": {"charset"},
        "a": {"href"},
        "img": {"src", "alt", "width", "height", "loading"},
        "th": {"colspan", "rowspan"},
        "td": {"colspan", "rowspan"},
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag not in self._TAGS:
            raise ValueError(f"active or external HTML element is forbidden: {tag}")
        seen: set[str] = set()
        for name, value in attrs:
            name = name.lower()
            if name in seen or name not in self._GLOBAL_ATTRS | self._ATTRS.get(tag, set()):
                raise ValueError(f"active or unsupported HTML attribute is forbidden: {name}")
            seen.add(name)
            if tag == "meta" and (name != "charset" or value is None or value.lower() != "utf-8"):
                raise ValueError("only static UTF-8 charset metadata is allowed")
            if name in {"src", "href"}:
                if not value:
                    raise ValueError(f"empty HTML reference: {name}")
                reference = value.split("#", 1)[0].split("?", 1)[0]
                self.paths.add(_path(reference))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _png_dimensions(raw: bytes) -> tuple[int, int]:
    """Validate non-interlaced PNG chunks and fully decompress image scanlines."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("runtime capture is not a PNG")
    offset = 8
    width: int | None = None
    height: int | None = None
    depth: int | None = None
    color: int | None = None
    interlace: int | None = None
    palette_entries: int | None = None
    seen_palette = False
    seen_transparency = False
    seen_data = False
    data_closed = False
    data_bytes = 0
    compressed = bytearray()
    ended = False
    while offset + 12 <= len(raw):
        length = struct.unpack_from(">I", raw, offset)[0]
        kind = raw[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(raw):
            raise ValueError("runtime PNG chunk is truncated")
        chunk = raw[offset + 8 : offset + 8 + length]
        crc = struct.unpack_from(">I", raw, offset + 8 + length)[0]
        if zlib.crc32(kind + chunk) & 0xFFFFFFFF != crc:
            raise ValueError("runtime PNG chunk CRC mismatch")
        if len(kind) != 4 or any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind):
            raise ValueError("runtime PNG chunk type is invalid")
        if not (65 <= kind[2] <= 90):
            raise ValueError("runtime PNG chunk reserved bit is invalid")
        if kind == b"IHDR":
            if offset != 8 or width is not None or length != 13:
                raise ValueError("runtime PNG has invalid IHDR")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            legal_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                not width
                or not height
                or compression
                or filtering
                or interlace
                or color not in legal_depths
                or depth not in legal_depths[color]
            ):
                raise ValueError("runtime PNG dimensions or encoding are unsupported")
        elif width is None:
            raise ValueError("runtime PNG IHDR must be first")
        elif kind == b"PLTE":
            if seen_palette or seen_data or length == 0 or length > 768 or length % 3:
                raise ValueError("runtime PNG has invalid PLTE")
            if color in {0, 4}:
                raise ValueError("runtime PNG PLTE is invalid for grayscale color type")
            palette_entries = length // 3
            if color == 3 and depth is not None and palette_entries > 2**depth:
                raise ValueError("runtime PNG palette exceeds indexed bit depth")
            seen_palette = True
        elif kind == b"tRNS":
            if seen_data or seen_transparency:
                raise ValueError("runtime PNG has invalid tRNS ordering")
            if color == 3:
                if not seen_palette or length > (palette_entries or 0):
                    raise ValueError("runtime PNG has invalid palette transparency")
            elif (
                (color == 0 and length != 2)
                or (color == 2 and length != 6)
                or color not in {0, 2, 3}
            ):
                raise ValueError("runtime PNG has invalid transparency for color type")
            seen_transparency = True
        elif kind == b"IDAT":
            if data_closed:
                raise ValueError("runtime PNG IDAT chunks are not consecutive")
            if color == 3 and not seen_palette:
                raise ValueError("runtime PNG indexed image is missing PLTE")
            seen_data = True
            data_bytes += length
            compressed.extend(chunk)
        elif kind == b"IEND":
            if length or end != len(raw) or not seen_data or data_bytes == 0:
                raise ValueError("runtime PNG has invalid trailing data")
            ended = True
            break
        elif kind[0] & 0x20 == 0 and kind not in {b"IHDR", b"PLTE", b"IDAT", b"IEND"}:
            raise ValueError("runtime PNG contains an unknown critical chunk")
        if seen_data and kind != b"IDAT":
            data_closed = True
        offset = end
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color) if color is not None else None
    if (
        not ended
        or width is None
        or height is None
        or (color == 3 and not seen_palette)
        or channels is None
        or depth is None
        or depth not in {1, 2, 4, 8, 16}
    ):
        raise ValueError("runtime PNG is incomplete or unsupported")
    if width * height > 16_000_000:
        raise ValueError("runtime PNG exceeds pixel limit")
    row_bytes = (width * channels * depth + 7) // 8
    expected = height * (row_bytes + 1)
    if expected > 128_000_000:
        raise ValueError("runtime PNG exceeds decoded size limit")
    try:
        inflater = zlib.decompressobj()
        pixels = inflater.decompress(bytes(compressed), expected + 1)
        if len(pixels) > expected:
            raise ValueError("runtime PNG decoded image exceeds expected size")
    except zlib.error as exc:
        raise ValueError("runtime PNG image data cannot be decoded") from exc
    if (
        len(pixels) != expected
        or not inflater.eof
        or inflater.unused_data
        or inflater.unconsumed_tail
    ):
        raise ValueError("runtime PNG decoded image size is invalid")
    if any(pixels[row * (row_bytes + 1)] > 4 for row in range(height)):
        raise ValueError("runtime PNG uses an invalid scanline filter")
    return width, height


def _jpeg_dimensions(raw: bytes) -> tuple[int, int]:
    """Read bounded JPEG frame dimensions and require a structurally closed scan."""
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        raise ValueError("embedded JPEG texture has invalid signature")
    offset = 2
    width = height = None
    saw_scan = False
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset < len(raw):
        if saw_scan:
            marker = raw.find(b"\xff\xd9", offset)
            if marker < 0:
                break
            if marker + 2 != len(raw):
                raise ValueError("embedded JPEG texture has trailing bytes")
            offset = marker + 2
            break
        if raw[offset] != 0xFF:
            raise ValueError("embedded JPEG marker is malformed")
        while offset < len(raw) and raw[offset] == 0xFF:
            offset += 1
        if offset >= len(raw):
            break
        marker = raw[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or marker == 0x00 or 0xD0 <= marker <= 0xD7:
            raise ValueError("embedded JPEG marker ordering is invalid")
        if offset + 2 > len(raw):
            raise ValueError("embedded JPEG segment is truncated")
        length = struct.unpack_from(">H", raw, offset)[0]
        if length < 2 or offset + length > len(raw):
            raise ValueError("embedded JPEG segment bounds are invalid")
        if marker in sof:
            if width is not None or length < 8:
                raise ValueError("embedded JPEG frame header is invalid")
            height, width = struct.unpack_from(">HH", raw, offset + 3)
            components = raw[offset + 7]
            if length != 8 + components * 3 or components not in {1, 3, 4}:
                raise ValueError("embedded JPEG frame components are unsupported")
        if marker == 0xDA:
            if width is None or length < 6:
                raise ValueError("embedded JPEG scan has no frame")
            saw_scan = True
        offset += length
    if offset != len(raw) or not saw_scan or width is None or height is None:
        raise ValueError("embedded JPEG texture is incomplete or unsupported")
    if not width or not height or max(width, height) > 16_384 or width * height > 100_000_000:
        raise ValueError("embedded JPEG texture exceeds decoded-size safety limit")
    return width, height


def _receipt(
    root: Path,
    item: dict[str, Any],
    kind: str,
    workflow_id: str,
    revision: int,
    allowed_statuses: set[str] | None = None,
) -> dict[str, Any]:
    receipt = _object(root / item["path"])
    allowed = allowed_statuses or {"APPROVED"}
    if (
        receipt.get("workflow_id") != workflow_id
        or receipt.get("revision") != revision
        or receipt.get("approval_type") != kind
        or receipt.get("status") not in allowed
        or not isinstance(receipt.get("task_id"), str)
        or not receipt["task_id"]
    ):
        raise ValueError(f"{kind} approval receipt does not bind this revision")
    try:
        expected = _approval_hash(receipt["task_id"], kind, receipt["inputs"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{kind} approval inputs are invalid") from exc
    if receipt.get("fingerprint") != expected:
        raise ValueError(f"{kind} approval fingerprint mismatch")
    return receipt


def _v07_matrix(node: dict[str, Any]) -> list[list[float]]:
    if "matrix" in node:
        values = node["matrix"]
        if (
            not isinstance(values, list)
            or len(values) != 16
            or any(type(x) not in {int, float} or not math.isfinite(x) for x in values)
        ):
            raise ValueError("V0.7 GLB node matrix is invalid")
        return [[float(values[c * 4 + r]) for c in range(4)] for r in range(4)]
    t, q, s = (
        node.get("translation", [0, 0, 0]),
        node.get("rotation", [0, 0, 0, 1]),
        node.get("scale", [1, 1, 1]),
    )
    if not all(isinstance(x, list) and len(x) == n for x, n in ((t, 3), (q, 4), (s, 3))):
        raise ValueError("V0.7 GLB node TRS is invalid")
    values = [*t, *q, *s]
    if any(type(x) not in {int, float} or not math.isfinite(x) for x in values):
        raise ValueError("V0.7 GLB node TRS is non-finite or malformed")
    x, y, z, w = map(float, q)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm < 1e-12:
        raise ValueError("V0.7 GLB node quaternion is zero")
    x, y, z, w = (v / norm for v in (x, y, z, w))
    r = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    return [[r[i][j] * float(s[j]) for j in range(3)] + [float(t[i])] for i in range(3)] + [
        [0.0, 0.0, 0.0, 1.0]
    ]


def _v07_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _v07_parts(node: dict[str, Any]) -> tuple[list[float], list[list[float]], list[float]]:
    matrix = _v07_matrix(node)
    if any(
        abs(matrix[3][column] - expected) > 1e-6 for column, expected in enumerate((0, 0, 0, 1))
    ):
        raise ValueError("V0.7 transform has a projective bottom row")
    columns = [[matrix[row][column] for row in range(3)] for column in range(3)]
    scale = [math.sqrt(sum(value * value for value in column)) for column in columns]
    if any(not math.isfinite(value) or value <= 1e-10 for value in scale):
        raise ValueError("V0.7 transform has zero/invalid scale")
    basis = [[columns[column][row] / scale[column] for column in range(3)] for row in range(3)]
    if any(
        abs(sum(basis[row][left] * basis[row][right] for row in range(3))) > 1e-5
        for left in range(3)
        for right in range(left + 1, 3)
    ):
        raise ValueError("V0.7 transform contains shear")
    det = (
        basis[0][0] * (basis[1][1] * basis[2][2] - basis[1][2] * basis[2][1])
        - basis[0][1] * (basis[1][0] * basis[2][2] - basis[1][2] * basis[2][0])
        + basis[0][2] * (basis[1][0] * basis[2][1] - basis[1][1] * basis[2][0])
    )
    if det <= 0 or abs(det - 1.0) > 1e-5:
        raise ValueError("V0.7 transform contains reflection or invalid rotation basis")
    return [matrix[row][3] for row in range(3)], basis, scale


def _v07_quaternion_basis(value: Any) -> list[list[float]]:
    if value == "identity":
        return [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(x) not in {int, float} or not math.isfinite(x) for x in value)
    ):
        raise ValueError("V0.7 declared quaternion basis is malformed")
    x, y, z, w = (float(item) for item in value)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if abs(norm - 1.0) > 1e-5:
        raise ValueError("V0.7 declared quaternion is not normalized")
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def _v07_basis_angle(left: list[list[float]], right: list[list[float]]) -> float:
    relative_trace = sum(left[r][c] * right[r][c] for r in range(3) for c in range(3))
    cosine = max(-1.0, min(1.0, (relative_trace - 1.0) / 2.0))
    return math.degrees(math.acos(cosine))


def _v07_close_vector(value: Any, expected: Any, tolerance: float) -> bool:
    return (
        isinstance(value, list)
        and isinstance(expected, list | tuple)
        and len(value) == len(expected) == 3
        and all(
            type(actual) in {int, float}
            and type(target) in {int, float}
            and math.isfinite(actual)
            and math.isfinite(target)
            and abs(actual - target) <= tolerance
            for actual, target in zip(value, expected, strict=True)
        )
    )


def _v07_same_int(value: Any, expected: Any) -> bool:
    """Compare JSON integers without accepting booleans as Python integers."""
    return type(value) is int and type(expected) is int and value == expected


def _v07_paid_execution_history(
    intent: dict[str, Any], workflow_id: str, revision: Any
) -> set[str]:
    """Validate the complete paid-task execution lineage pinned by V0.7 evidence."""
    history = intent.get("paid_execution_history")
    row_keys = {
        "id",
        "attempt_number",
        "status",
        "task_id",
        "workflow_id",
        "revision_number",
        "provider",
        "external_op_id",
        "cost",
        "cost_unit",
    }
    statuses = {"UNCERTAIN", "FAILED", "COMPLETED"}
    task_id = intent.get("task_id")
    if not isinstance(history, list) or not history or not isinstance(task_id, str) or not task_id:
        raise ValueError("Provider-character paid execution history is malformed")
    execution_ids: set[str] = set()
    completed_seen = False
    for index, row in enumerate(history, start=1):
        if not isinstance(row, dict) or set(row) != row_keys:
            raise ValueError("Provider-character paid execution history row is malformed")
        execution_id = row.get("id")
        status = row.get("status")
        provider = row.get("provider")
        external_id = row.get("external_op_id")
        cost = row.get("cost")
        if (
            not isinstance(execution_id, str)
            or not execution_id
            or execution_id in execution_ids
            or type(row.get("attempt_number")) is not int
            or row["attempt_number"] != index
            or row.get("task_id") != task_id
            or row.get("workflow_id") != workflow_id
            or not _v07_same_int(row.get("revision_number"), revision)
            or not isinstance(status, str)
            or status not in statuses
            or (provider is not None and (not isinstance(provider, str) or not provider))
            or (external_id is not None and (not isinstance(external_id, str) or not external_id))
            or (provider is not None and provider != intent.get("provider"))
            or (external_id is not None and external_id != intent.get("external_task_id"))
            or type(cost) not in {int, float}
            or not math.isfinite(float(cast(int | float, cost)))
            or float(cast(int | float, cost)) < 0
            or not isinstance(row.get("cost_unit"), str)
            or not row["cost_unit"]
            or completed_seen
        ):
            raise ValueError(
                "Provider-character paid execution history is incomplete or conflicting"
            )
        execution_ids.add(execution_id)
        completed_seen = status == "COMPLETED"
    latest = history[-1]
    if (
        latest.get("status") != "COMPLETED"
        or latest.get("id") != intent.get("execution_id")
        or latest.get("attempt_number") != intent.get("attempt_number")
        or latest.get("provider") != intent.get("paid_execution_provider")
        or latest.get("external_op_id") != intent.get("paid_execution_external_id")
        or latest.get("status") != intent.get("paid_execution_status")
        or latest.get("cost") != intent.get("paid_execution_cost")
        or latest.get("cost_unit") != intent.get("cost_unit")
    ):
        raise ValueError("Provider-character paid execution history has a stale terminal row")
    return execution_ids


def _v07_reserve_metadata_allowed(
    metadata: tuple[Any, Any, Any], paid_execution_ids: set[str]
) -> bool:
    """Accept only unbound reserves or exact pre-intent rows for known paid attempts."""
    return metadata == (None, None, None) or (
        metadata[0] is None
        and metadata[2] is None
        and isinstance(metadata[1], str)
        and metadata[1] in paid_execution_ids
    )


def _v07_close_matrix3(value: Any, expected: Any, tolerance: float) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and isinstance(expected, list | tuple)
        and len(expected) == 3
        and all(
            _v07_close_vector(row, list(expected[index]), tolerance)
            for index, row in enumerate(value)
        )
    )


def _v07_expected_view(
    view: str,
) -> tuple[tuple[float, float, float], tuple[float, float, float], str]:
    placements: dict[str, tuple[tuple[float, float, float], tuple[float, float, float], str]] = {
        "front": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0), "-Z"),
        "rear": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), "+Z"),
        "left": ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), "-X"),
        "right": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), "+X"),
        "side": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), "+X"),
        "three_quarter": ((1.0, 0.65, -1.0), (0.0, 1.0, 0.0), "+X-Z"),
        "three_quarter_front": ((1.0, 0.65, -1.0), (0.0, 1.0, 0.0), "+X-Z"),
        "three_quarter_rear": ((1.0, 0.65, 1.0), (0.0, 1.0, 0.0), "+X+Z"),
        "top": ((0.0, 1.0, 0.0), (0.0, 0.0, -1.0), "+Y"),
    }
    if view not in placements:
        raise ValueError(f"V0.7 runtime view has no canonical camera placement: {view}")
    placement, requested_up, axis = placements[view]

    def normalize(vector: tuple[float, float, float]) -> tuple[float, float, float]:
        length = math.sqrt(sum(component * component for component in vector))
        return (vector[0] / length, vector[1] / length, vector[2] / length)

    normalized_placement = normalize(placement)
    direction = normalize(
        (-normalized_placement[0], -normalized_placement[1], -normalized_placement[2])
    )
    up_projection = sum(requested_up[i] * direction[i] for i in range(3))
    up = normalize(
        (
            requested_up[0] - up_projection * direction[0],
            requested_up[1] - up_projection * direction[1],
            requested_up[2] - up_projection * direction[2],
        )
    )
    return direction, up, axis


def _v07_matrix_point(
    matrix: list[list[float]], point: tuple[float, float, float]
) -> tuple[float, float, float]:
    value = [*point, 1.0]
    return tuple(
        sum(matrix[row][column] * value[column] for column in range(4)) for row in range(3)
    )  # type: ignore[return-value]


def _v07_glb(
    raw: bytes,
    *,
    allow_unnamed_duplicate_nodes: bool = False,
) -> tuple[dict[str, Any], bytes, dict[str, int], dict[int, int], dict[int, list[list[float]]]]:
    if len(raw) < 20 or len(raw) > _MAX_FILE_BYTES:
        raise ValueError("V0.7 GLB size is invalid")
    magic, version, total = struct.unpack_from("<4sII", raw)
    if magic != b"glTF" or version != 2 or total != len(raw):
        raise ValueError("V0.7 GLB header is invalid")
    offset, json_chunk, binary = 12, None, None
    while offset < total:
        if offset + 8 > total:
            raise ValueError("V0.7 GLB chunk header is truncated")
        length, kind = struct.unpack_from("<II", raw, offset)
        offset += 8
        end = offset + length
        if length % 4 or end > total:
            raise ValueError("V0.7 GLB chunk bounds are invalid")
        chunk = raw[offset:end]
        if kind == 0x4E4F534A and json_chunk is None and offset == 20:
            json_chunk = chunk
        elif kind == 0x004E4942 and binary is None:
            binary = chunk
        else:
            raise ValueError("V0.7 GLB has duplicate or unsupported chunks")
        offset = end
    if json_chunk is None or binary is None:
        raise ValueError("V0.7 GLB requires JSON and embedded BIN chunks")

    def reject_constant(token: str) -> None:
        raise ValueError(f"V0.7 GLB JSON contains non-finite number: {token}")

    document = json.loads(
        json_chunk.decode("utf-8").rstrip(" \t\r\n\x00"),
        object_pairs_hook=_unique_json,
        parse_constant=reject_constant,
    )
    asset = document.get("asset") if isinstance(document, dict) else None
    if not isinstance(asset, dict) or asset.get("version") != "2.0":
        raise ValueError("V0.7 GLB glTF document is unsupported")
    if (
        document.get("animations")
        or document.get("skins")
        or document.get("extensionsUsed")
        or document.get("extensionsRequired")
    ):
        raise ValueError("V0.7 GLB skins, animations, and extensions are forbidden")

    def reject_extensions(obj: Any) -> None:
        if isinstance(obj, dict):
            if obj.get("extensions"):
                raise ValueError("V0.7 GLB extensions are forbidden")
            if "uri" in obj:
                raise ValueError("V0.7 GLB external or data URI resources are forbidden")
            for child in obj.values():
                reject_extensions(child)
        elif isinstance(obj, list):
            for child in obj:
                reject_extensions(child)
        elif isinstance(obj, float) and not math.isfinite(obj):
            raise ValueError("V0.7 GLB JSON contains non-finite number")

    reject_extensions(document)
    buffers = document.get("buffers")
    if (
        not isinstance(buffers, list)
        or len(buffers) != 1
        or not isinstance(buffers[0], dict)
        or "uri" in buffers[0]
        or type(buffers[0].get("byteLength")) is not int
        or buffers[0]["byteLength"] < 0
        or buffers[0].get("byteLength", -1) > len(binary)
    ):
        raise ValueError("V0.7 GLB requires one in-bounds embedded buffer")
    nodes = document.get("nodes")
    scenes = document.get("scenes")
    if (
        not isinstance(nodes, list)
        or not isinstance(scenes, list)
        or len(scenes) != 1
        or len(nodes) > 100_000
    ):
        raise ValueError("V0.7 GLB node or scene table is invalid")
    node_by_name: dict[str, int] = {}
    parents: dict[int, int] = {}
    local: list[list[list[float]]] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise ValueError("V0.7 GLB node is malformed")
        node_name = node.get("name")
        if allow_unnamed_duplicate_nodes:
            # Raw character identity is geometric and node-index based; names
            # may be missing, duplicated, or collide with any fallback label.
            node_name = f"__provider_raw_node_{index}"
        elif not isinstance(node_name, str) or node_name in node_by_name:
            raise ValueError("V0.7 GLB nodes need unique names")
        if node_name in node_by_name:
            raise ValueError("V0.7 GLB nodes need unique names")
        node_by_name[node_name] = index
        local.append(_v07_matrix(node))
        _v07_parts(node)
        children = node.get("children", [])
        if not isinstance(children, list):
            raise ValueError("V0.7 GLB children must be an array")
        for child in children:
            if type(child) is not int or not 0 <= child < len(nodes) or child in parents:
                raise ValueError("V0.7 GLB node parent graph is invalid")
            parents[child] = index
    scene_i = document.get("scene", 0)
    if (
        type(scene_i) is not int
        or not 0 <= scene_i < len(scenes)
        or not isinstance(scenes[scene_i], dict)
    ):
        raise ValueError("V0.7 GLB active scene is invalid")
    reachable: set[int] = set()

    def visit(index: int, trail: frozenset[int]) -> None:
        if index in trail or len(trail) > 128:
            raise ValueError("V0.7 GLB node cycle or depth overflow")
        if index in reachable:
            return
        reachable.add(index)
        for child in nodes[index].get("children", []):
            visit(child, trail | {index})

    scene_roots = scenes[scene_i].get("nodes")
    if not isinstance(scene_roots, list):
        raise ValueError("V0.7 GLB scene roots are invalid")
    for index in scene_roots:
        if type(index) is not int or not 0 <= index < len(nodes):
            raise ValueError("V0.7 GLB scene root is invalid")
        visit(index, frozenset())
    if reachable != set(range(len(nodes))):
        raise ValueError("V0.7 GLB has unreachable semantic nodes")
    world: dict[int, list[list[float]]] = {}

    def get_world(index: int, trail: frozenset[int] = frozenset()) -> list[list[float]]:
        if index in world:
            return world[index]
        if index in trail or len(trail) > 128:
            raise ValueError("V0.7 GLB hierarchy cycle or depth overflow")
        parent = parents.get(index)
        value = (
            local[index]
            if parent is None
            else _v07_mul(get_world(parent, trail | {index}), local[index])
        )
        world[index] = value
        return value

    for index in range(len(nodes)):
        get_world(index)
    meshes = document.get("meshes", [])
    if not isinstance(meshes, list):
        raise ValueError("V0.7 GLB mesh table must be an array")
    referenced_meshes = {
        node["mesh"] for node in nodes if isinstance(node, dict) and "mesh" in node
    }
    if referenced_meshes != set(range(len(meshes))):
        raise ValueError("V0.7 GLB has unreferenced or missing mesh resources")
    accessors = document.get("accessors")
    views = document.get("bufferViews")
    if not isinstance(accessors, list) or not isinstance(views, list):
        raise ValueError("V0.7 GLB accessor or bufferView table is missing")
    referenced_accessors: set[int] = set()
    referenced_views: set[int] = set()
    for mesh in meshes:
        for primitive in mesh.get("primitives", []):
            attrs = primitive.get("attributes", {})
            referenced_accessors.update(attrs.values())
            if "indices" in primitive:
                referenced_accessors.add(primitive["indices"])
    for accessor_index in referenced_accessors:
        if type(accessor_index) is not int or not 0 <= accessor_index < len(accessors):
            raise ValueError("V0.7 GLB accessor reference is invalid")
        accessor = accessors[accessor_index]
        if not isinstance(accessor, dict) or type(accessor.get("bufferView")) is not int:
            raise ValueError("V0.7 GLB dense accessors require a bufferView")
        referenced_views.add(accessor["bufferView"])
    images = document.get("images", [])
    if not isinstance(images, list):
        raise ValueError("V0.7 GLB image table must be an array")
    for image in images:
        if not isinstance(image, dict) or "uri" in image:
            raise ValueError("V0.7 GLB external or malformed image is forbidden")
        view_index = image.get("bufferView")
        if type(view_index) is not int or not 0 <= view_index < len(views):
            raise ValueError("V0.7 GLB embedded image bufferView is invalid")
        view = views[view_index]
        if (
            not isinstance(view, dict)
            or view.get("buffer") != 0
            or "byteStride" in view
            or type(view.get("byteOffset", 0)) is not int
            or type(view.get("byteLength")) is not int
            or view.get("byteOffset", 0) < 0
            or view["byteLength"] <= 0
            or view.get("byteOffset", 0) + view["byteLength"] > len(binary)
        ):
            raise ValueError("V0.7 GLB embedded image bytes are out of bounds")
        image_bytes = binary[
            view.get("byteOffset", 0) : view.get("byteOffset", 0) + view["byteLength"]
        ]
        mime = image.get("mimeType")
        if mime == "image/png" and image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            image_width, image_height = _png_dimensions(image_bytes)
        elif mime == "image/jpeg" and image_bytes.startswith(b"\xff\xd8"):
            image_width, image_height = _jpeg_dimensions(image_bytes)
        else:
            raise ValueError("V0.7 GLB embedded image MIME or payload is unsupported")
        if max(image_width, image_height) > 16_384 or image_width * image_height > 100_000_000:
            raise ValueError("V0.7 GLB embedded image exceeds decoded-size safety limit")
        referenced_views.add(view_index)
    textures = document.get("textures", [])
    if not isinstance(textures, list):
        raise ValueError("V0.7 GLB texture table must be an array")
    for texture in textures:
        if not isinstance(texture, dict):
            raise ValueError("V0.7 GLB texture entry is malformed")
        source = texture.get("source")
        sampler = texture.get("sampler")
        if type(source) is not int or not 0 <= source < len(images):
            raise ValueError("V0.7 GLB texture image source is invalid")
        samplers = document.get("samplers", [])
        if sampler is not None and (
            type(sampler) is not int
            or not isinstance(samplers, list)
            or not 0 <= sampler < len(samplers)
        ):
            raise ValueError("V0.7 GLB texture sampler is invalid")
    materials = document.get("materials", [])
    if not isinstance(materials, list) or any(
        not isinstance(material, dict) for material in materials
    ):
        raise ValueError("V0.7 GLB material table is malformed")
    for mesh in meshes:
        for primitive in mesh.get("primitives", []):
            if not isinstance(primitive, dict):
                raise ValueError("V0.7 GLB primitive is malformed")
            material_id = primitive.get("material")
            if material_id is not None and (
                type(material_id) is not int or not 0 <= material_id < len(materials)
            ):
                raise ValueError("V0.7 GLB primitive references a missing material")
            if material_id is None:
                continue
            material = materials[material_id]
            pbr = material.get("pbrMetallicRoughness", {})
            if not isinstance(pbr, dict):
                raise ValueError("V0.7 GLB PBR material is malformed")
            slots = (
                pbr.get("baseColorTexture"),
                pbr.get("metallicRoughnessTexture"),
                material.get("normalTexture"),
                material.get("occlusionTexture"),
                material.get("emissiveTexture"),
            )
            for info in slots:
                if info is None:
                    continue
                texcoord = info.get("texCoord", 0) if isinstance(info, dict) else None
                attrs = primitive.get("attributes", {})
                if (
                    not isinstance(info, dict)
                    or type(info.get("index")) is not int
                    or not 0 <= info["index"] < len(textures)
                    or type(texcoord) is not int
                    or texcoord < 0
                    or not isinstance(attrs, dict)
                    or f"TEXCOORD_{texcoord}" not in attrs
                ):
                    raise ValueError("V0.7 GLB material texture reference is invalid")
    if not allow_unnamed_duplicate_nodes and (
        referenced_accessors != set(range(len(accessors)))
        or referenced_views != set(range(len(views)))
    ):
        raise ValueError("V0.7 GLB contains unreferenced accessors or bufferViews")
    return document, binary, node_by_name, parents, world


def _unique_json(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _v07_positions(
    document: dict[str, Any],
    binary: bytes,
    mesh_index: int,
    *,
    decoded_meshes: dict[int, list[list[tuple[tuple[float, float, float], ...]]]] | None = None,
    validated_attribute_accessors: set[int] | None = None,
) -> list[list[tuple[tuple[float, float, float], ...]]]:
    meshes, accessors, views = (
        document.get("meshes"),
        document.get("accessors"),
        document.get("bufferViews"),
    )
    if (
        not isinstance(meshes, list)
        or not isinstance(accessors, list)
        or not isinstance(views, list)
        or not 0 <= mesh_index < len(meshes)
    ):
        raise ValueError("V0.7 GLB mesh or accessor table is invalid")
    if decoded_meshes is not None and mesh_index in decoded_meshes:
        return decoded_meshes[mesh_index]
    mesh = meshes[mesh_index]
    if not isinstance(mesh, dict) or not isinstance(mesh.get("primitives"), list):
        raise ValueError("V0.7 GLB mesh is malformed")
    result: list[list[tuple[tuple[float, float, float], ...]]] = []
    work = 0
    for primitive in mesh["primitives"]:
        if (
            not isinstance(primitive, dict)
            or primitive.get("mode", 4) != 4
            or "targets" in primitive
        ):
            raise ValueError("V0.7 GLB primitive must be static triangle geometry")
        attrs = primitive.get("attributes")
        if not isinstance(attrs, dict) or not isinstance(attrs.get("POSITION"), int):
            raise ValueError("V0.7 GLB primitive lacks POSITION")
        for semantic in attrs:
            if semantic in {"POSITION", "NORMAL", "TANGENT"}:
                continue
            if not isinstance(semantic, str) or not re.fullmatch(
                r"(?:TEXCOORD|COLOR)_(?:0|[1-9][0-9]*)", semantic
            ):
                raise ValueError("V0.7 GLB primitive has unsupported vertex attributes")
        ai = attrs["POSITION"]
        if type(ai) is not int or not 0 <= ai < len(accessors):
            raise ValueError("V0.7 GLB position accessor is invalid")
        acc = accessors[ai]
        if (
            not isinstance(acc, dict)
            or acc.get("type") != "VEC3"
            or acc.get("componentType") != 5126
            or "sparse" in acc
        ):
            raise ValueError("V0.7 GLB position accessor must be dense float VEC3")
        count, vi = acc.get("count"), acc.get("bufferView")
        if (
            type(count) is not int
            or not 1 <= count <= 1_000_000
            or type(vi) is not int
            or not 0 <= vi < len(views)
        ):
            raise ValueError("V0.7 GLB position accessor count or view invalid")
        view = views[vi]
        if not isinstance(view, dict) or view.get("buffer") != 0:
            raise ValueError("V0.7 GLB accessors require embedded buffer 0")
        stride = view.get("byteStride", 12)
        start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
        end = view.get("byteOffset", 0) + view.get("byteLength", -1)
        if (
            any(type(x) is not int for x in (stride, start, end))
            or stride < 12
            or start < view.get("byteOffset", 0)
            or start + (count - 1) * stride + 12 > end
            or end > len(binary)
        ):
            raise ValueError("V0.7 GLB position accessor is out of bounds")
        points = [struct.unpack_from("<3f", binary, start + i * stride) for i in range(count)]
        if any(not math.isfinite(v) for p in points for v in p):
            raise ValueError("V0.7 GLB position is non-finite")
        attribute_shape = {"NORMAL": "VEC3", "TANGENT": "VEC4"}
        for semantic, attribute_index in attrs.items():
            if semantic == "POSITION":
                continue
            if type(attribute_index) is not int or not 0 <= attribute_index < len(accessors):
                raise ValueError("V0.7 GLB vertex attribute accessor index is invalid")
            attribute = accessors[attribute_index]
            accessor_type = attribute.get("type") if isinstance(attribute, dict) else None
            expected_type = attribute_shape.get(semantic)
            if semantic.startswith("TEXCOORD_"):
                expected_type = "VEC2"
            elif semantic.startswith("COLOR_"):
                expected_type = accessor_type if accessor_type in {"VEC3", "VEC4"} else None
            components = (
                {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}.get(accessor_type)
                if isinstance(accessor_type, str)
                else None
            )
            av = attribute.get("bufferView") if isinstance(attribute, dict) else None
            component_type = attribute.get("componentType") if isinstance(attribute, dict) else None
            normalized = attribute.get("normalized", False) if isinstance(attribute, dict) else None
            integer_color_or_uv = semantic.startswith(
                ("TEXCOORD_", "COLOR_")
            ) and component_type in {5121, 5123}
            valid_component = component_type == 5126 or integer_color_or_uv and normalized is True
            if (
                not isinstance(attribute, dict)
                or attribute.get("type") != expected_type
                or not valid_component
                or type(normalized) is not bool
                or (component_type == 5126 and normalized)
                or type(attribute.get("count")) is not int
                or attribute.get("count") != count
                or "sparse" in attribute
                or type(av) is not int
                or not 0 <= av < len(views)
                or components is None
            ):
                raise ValueError("V0.7 GLB vertex attribute accessor is unsupported")
            if type(component_type) is not int or component_type not in {5121, 5123, 5126}:
                raise ValueError("V0.7 GLB vertex attribute component type is unsupported")
            attribute_view = views[av]
            if not isinstance(attribute_view, dict) or attribute_view.get("buffer") != 0:
                raise ValueError("V0.7 GLB attribute accessor requires embedded buffer 0")
            component_bytes, code = {5121: (1, "B"), 5123: (2, "H"), 5126: (4, "f")}[component_type]
            element_bytes = components * component_bytes
            attribute_stride = attribute_view.get("byteStride", element_bytes)
            view_start = attribute_view.get("byteOffset", 0)
            view_length = attribute_view.get("byteLength", -1)
            accessor_offset = attribute.get("byteOffset", 0)
            attribute_start = (
                view_start + accessor_offset
                if type(view_start) is int and type(accessor_offset) is int
                else -1
            )
            attribute_end = (
                view_start + view_length
                if type(view_start) is int and type(view_length) is int
                else -1
            )
            if (
                any(
                    type(value) is not int
                    for value in (
                        attribute_stride,
                        view_start,
                        view_length,
                        accessor_offset,
                        attribute_start,
                        attribute_end,
                    )
                )
                or view_start < 0
                or view_length <= 0
                or accessor_offset < 0
                or attribute_stride < element_bytes
                or (
                    "byteStride" in attribute_view
                    and (
                        attribute_stride < 4 or attribute_stride > 252 or attribute_stride % 4 != 0
                    )
                )
                or attribute_start % component_bytes
                or attribute_start < view_start
                or attribute_start + (count - 1) * attribute_stride + element_bytes > attribute_end
                or attribute_end > len(binary)
            ):
                raise ValueError("V0.7 GLB vertex attribute accessor is out of bounds")
            if component_type == 5126 and (
                validated_attribute_accessors is None
                or attribute_index not in validated_attribute_accessors
            ):
                fmt = "<" + code * components
                if any(
                    not math.isfinite(value)
                    for index in range(count)
                    for value in struct.unpack_from(
                        fmt, binary, attribute_start + index * attribute_stride
                    )
                ):
                    raise ValueError("V0.7 GLB vertex attribute contains non-finite values")
                if validated_attribute_accessors is not None:
                    validated_attribute_accessors.add(attribute_index)
        if "indices" in primitive:
            ii = primitive["indices"]
            if type(ii) is not int or not 0 <= ii < len(accessors):
                raise ValueError("V0.7 GLB index accessor is invalid")
            ia = accessors[ii]
            if (
                not isinstance(ia, dict)
                or ia.get("type") != "SCALAR"
                or ia.get("componentType") not in {5121, 5123, 5125}
                or "sparse" in ia
            ):
                raise ValueError("V0.7 GLB index accessor is invalid")
            ic, iv = ia.get("count"), ia.get("bufferView")
            if (
                type(ic) is not int
                or ic < 3
                or ic % 3
                or ic > 3_000_000
                or type(iv) is not int
                or not 0 <= iv < len(views)
            ):
                raise ValueError("V0.7 GLB index count is invalid")
            v = views[iv]
            if not isinstance(v, dict):
                raise ValueError("V0.7 GLB index buffer view is invalid")
            size, code = {5121: (1, "B"), 5123: (2, "H"), 5125: (4, "I")}[ia["componentType"]]
            off = v.get("byteOffset", 0) + ia.get("byteOffset", 0)
            vend = v.get("byteOffset", 0) + v.get("byteLength", -1)
            if (
                v.get("buffer") != 0
                or "byteStride" in v
                or type(off) is not int
                or type(vend) is not int
                or off + ic * size > vend
                or vend > len(binary)
            ):
                raise ValueError("V0.7 GLB index bytes are out of bounds")
            indices = [struct.unpack_from("<" + code, binary, off + i * size)[0] for i in range(ic)]
        else:
            indices = list(range(count))
        if len(indices) % 3 or any(i >= count for i in indices):
            raise ValueError("V0.7 GLB indices exceed vertex accessor")
        work += len(indices) // 3
        if work > 100_000:
            raise ValueError("V0.7 GLB triangle safety limit exceeded")
        result.append(
            [
                (
                    (points[indices[j]][0], points[indices[j]][1], points[indices[j]][2]),
                    (
                        points[indices[j + 1]][0],
                        points[indices[j + 1]][1],
                        points[indices[j + 1]][2],
                    ),
                    (
                        points[indices[j + 2]][0],
                        points[indices[j + 2]][1],
                        points[indices[j + 2]][2],
                    ),
                )
                for j in range(0, len(indices), 3)
            ]
        )
    if decoded_meshes is not None:
        decoded_meshes[mesh_index] = result
    return result


_V07_MAX_DECODED_VERTEX_REFERENCES = 1_000_000
_V07_MAX_DECODED_ATTRIBUTE_COMPONENTS = 16_000_000


def _v07_bound_mesh_vertex_work(document: dict[str, Any], node_indices: list[int]) -> int:
    """Bound total POSITION decoding before any per-node vertex allocations."""
    nodes = document.get("nodes")
    meshes = document.get("meshes")
    accessors = document.get("accessors")
    if (
        not isinstance(nodes, list)
        or not isinstance(meshes, list)
        or not isinstance(accessors, list)
    ):
        raise ValueError("V0.7 GLB mesh work tables are malformed")
    total = 0
    unique_attribute_accessors: set[int] = set()
    attribute_checked_meshes: set[int] = set()
    for node_index in node_indices:
        if type(node_index) is not int or not 0 <= node_index < len(nodes):
            raise ValueError("V0.7 GLB mesh node index is invalid")
        node = nodes[node_index]
        mesh_index = node.get("mesh") if isinstance(node, dict) else None
        if type(mesh_index) is not int or not 0 <= mesh_index < len(meshes):
            raise ValueError("V0.7 GLB mesh reference is invalid")
        mesh = meshes[mesh_index]
        primitives = mesh.get("primitives") if isinstance(mesh, dict) else None
        if not isinstance(primitives, list) or not primitives:
            raise ValueError("V0.7 GLB mesh primitive table is invalid")
        if mesh_index not in attribute_checked_meshes:
            for mesh_primitive in primitives:
                mesh_attributes = (
                    mesh_primitive.get("attributes") if isinstance(mesh_primitive, dict) else None
                )
                if not isinstance(mesh_attributes, dict):
                    raise ValueError("V0.7 GLB vertex attribute table is malformed")
                position_index = mesh_attributes.get("POSITION")
                if type(position_index) is not int or not 0 <= position_index < len(accessors):
                    raise ValueError("V0.7 GLB POSITION accessor reference is invalid")
                position_accessor = accessors[position_index]
                position_count = (
                    position_accessor.get("count") if isinstance(position_accessor, dict) else None
                )
                if (
                    type(position_count) is not int
                    or not 1 <= position_count <= _V07_MAX_DECODED_VERTEX_REFERENCES
                ):
                    raise ValueError("V0.7 GLB POSITION accessor count is invalid")
                for semantic, attribute_index in mesh_attributes.items():
                    if semantic == "POSITION":
                        continue
                    if type(attribute_index) is not int or not 0 <= attribute_index < len(
                        accessors
                    ):
                        raise ValueError("V0.7 GLB vertex attribute accessor index is invalid")
                    attribute_accessor = accessors[attribute_index]
                    attribute_count = (
                        attribute_accessor.get("count")
                        if isinstance(attribute_accessor, dict)
                        else None
                    )
                    if (
                        type(attribute_count) is not int
                        or not 1 <= attribute_count <= _V07_MAX_DECODED_VERTEX_REFERENCES
                        or attribute_count != position_count
                    ):
                        raise ValueError(
                            "V0.7 GLB vertex attribute accessor count must match POSITION"
                        )
                    unique_attribute_accessors.add(attribute_index)
            attribute_checked_meshes.add(mesh_index)
        for primitive in primitives:
            attributes = primitive.get("attributes") if isinstance(primitive, dict) else None
            accessor_index = attributes.get("POSITION") if isinstance(attributes, dict) else None
            if type(accessor_index) is not int or not 0 <= accessor_index < len(accessors):
                raise ValueError("V0.7 GLB POSITION accessor reference is invalid")
            accessor = accessors[accessor_index]
            count = accessor.get("count") if isinstance(accessor, dict) else None
            if type(count) is not int or not 1 <= count <= _V07_MAX_DECODED_VERTEX_REFERENCES:
                raise ValueError("V0.7 GLB POSITION accessor count is invalid")
            total += count
            if total > _V07_MAX_DECODED_VERTEX_REFERENCES:
                raise ValueError("V0.7 GLB aggregate decoded vertex work exceeds its safety bound")
    attribute_components = 0
    for accessor_index in unique_attribute_accessors:
        accessor = accessors[accessor_index]
        accessor_type = accessor.get("type") if isinstance(accessor, dict) else None
        count = accessor.get("count") if isinstance(accessor, dict) else None
        width = (
            {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}.get(accessor_type)
            if isinstance(accessor_type, str)
            else None
        )
        if type(count) is not int or count <= 0 or width is None:
            raise ValueError("V0.7 GLB vertex attribute accessor count or type is invalid")
        attribute_components += count * width
        if attribute_components > _V07_MAX_DECODED_ATTRIBUTE_COMPONENTS:
            raise ValueError("V0.7 GLB aggregate decoded attribute work exceeds its safety bound")
    return total


def _v07_tris(
    document: dict[str, Any],
    binary: bytes,
    nodes: dict[str, int],
    world: dict[int, list[list[float]]],
    suffix: str,
    *,
    selected_names: set[str] | None = None,
    decoded_meshes: dict[int, list[list[tuple[tuple[float, float, float], ...]]]] | None = None,
    validated_attribute_accessors: set[int] | None = None,
) -> dict[str, list[tuple[tuple[float, float, float], ...]]]:
    result: dict[str, list[tuple[tuple[float, float, float], ...]]] = {}
    if decoded_meshes is None:
        decoded_meshes = {}
    if validated_attribute_accessors is None:
        validated_attribute_accessors = set()
    gltf_nodes = document["nodes"]
    for name, ni in nodes.items():
        if name not in selected_names if selected_names is not None else not name.endswith(suffix):
            continue
        node = gltf_nodes[ni]
        if "mesh" not in node:
            raise ValueError(f"V0.7 GLB {name} is not a mesh node")
        primitives = _v07_positions(
            document,
            binary,
            node["mesh"],
            decoded_meshes=decoded_meshes,
            validated_attribute_accessors=validated_attribute_accessors,
        )
        triangles: list[tuple[tuple[float, float, float], ...]] = []
        matrix = world[ni]
        for primitive in primitives:
            for tri in primitive:
                points: list[tuple[float, float, float]] = []
                for p in tri:
                    homogeneous = (p[0], p[1], p[2], 1.0)
                    v = [sum(matrix[r][c] * homogeneous[c] for c in range(4)) for r in range(4)]
                    points.append((v[0], v[1], v[2]))
                triangles.append(tuple(points))
        result[name] = triangles
    return result


def _v07_enforce_core_budgets(
    document: dict[str, Any],
    binary: bytes,
    nodes: dict[str, int],
    world: dict[int, list[list[float]]],
    specification: dict[str, Any],
    profile: dict[str, Any],
    *,
    decoded_meshes: dict[int, list[list[tuple[tuple[float, float, float], ...]]]] | None = None,
    validated_attribute_accessors: set[int] | None = None,
) -> None:
    geometry_value = specification.get("geometry_budget")
    material_value = specification.get("material_budget")
    texture_value = specification.get("texture_budget")
    processing_value = profile.get("processing")
    if not all(
        isinstance(value, dict)
        for value in (geometry_value, material_value, texture_value, processing_value)
    ):
        raise ValueError("V0.7 specification/profile budgets are malformed")
    geometry = cast(dict[str, Any], geometry_value)
    material_budget = cast(dict[str, Any], material_value)
    texture_budget = cast(dict[str, Any], texture_value)
    profile_processing = cast(dict[str, Any], processing_value)

    def positive_int(value: Any) -> bool:
        return type(value) is int and value > 0

    caps = (
        (geometry.get("max_triangles_lod0"), profile_processing.get("max_triangles_lod0")),
        (material_budget.get("max_materials"), profile_processing.get("max_materials")),
        (texture_budget.get("max_dimension"), profile_processing.get("max_texture_dimension")),
    )
    lod0_budget = geometry.get("max_triangles_lod0")
    lod1_budget = geometry.get("max_triangles_lod1")
    if any(not positive_int(value) or not positive_int(cap) for value, cap in caps):
        raise ValueError("V0.7 specification budgets exceed or malformed against profile caps")
    typed_caps = cast(tuple[tuple[int, int], ...], caps)
    if (
        any(value > cap for value, cap in typed_caps)
        or not positive_int(lod0_budget)
        or not positive_int(lod1_budget)
        or type(lod0_budget) is not int
        or type(lod1_budget) is not int
        or lod1_budget >= lod0_budget
    ):
        raise ValueError("V0.7 specification budgets exceed or malformed against profile caps")
    assert type(lod0_budget) is int and type(lod1_budget) is int

    lod0 = _v07_tris(
        document,
        binary,
        nodes,
        world,
        "_LOD0",
        decoded_meshes=decoded_meshes,
        validated_attribute_accessors=validated_attribute_accessors,
    )
    lod1 = _v07_tris(
        document,
        binary,
        nodes,
        world,
        "_LOD1",
        decoded_meshes=decoded_meshes,
        validated_attribute_accessors=validated_attribute_accessors,
    )
    lod0_count = sum(len(triangles) for triangles in lod0.values())
    lod1_count = sum(len(triangles) for triangles in lod1.values())
    if lod0_count > lod0_budget:
        raise ValueError("V0.7 processed LOD0 exceeds specification triangle budget")
    if lod1_count > lod1_budget:
        raise ValueError("V0.7 processed LOD1 exceeds specification triangle budget")

    materials = document.get("materials", [])
    max_materials = material_budget.get("max_materials")
    if not isinstance(materials, list) or type(max_materials) is not int or max_materials < 1:
        raise ValueError("V0.7 processed material budget is malformed")
    if len(materials) > max_materials:
        raise ValueError("V0.7 processed material count exceeds specification budget")
    images = document.get("images", [])
    views = document.get("bufferViews", [])
    maximum = 0
    max_texture_dimension = texture_budget.get("max_dimension")
    if not positive_int(max_texture_dimension):
        raise ValueError("V0.7 texture dimension budget is malformed")
    max_texture_dimension = cast(int, max_texture_dimension)
    for image in images:
        view = views[image["bufferView"]]
        start = view.get("byteOffset", 0)
        data = binary[start : start + view["byteLength"]]
        dimensions = (
            _png_dimensions(data) if image["mimeType"] == "image/png" else _jpeg_dimensions(data)
        )
        maximum = max(maximum, *dimensions)
    if maximum > max_texture_dimension:
        raise ValueError("V0.7 processed embedded texture exceeds specification dimension budget")


def _v07_compare_triangles(
    source: list[tuple[tuple[float, float, float], ...]],
    processed: list[tuple[tuple[float, float, float], ...]],
    tolerance: float = 1e-5,
) -> bool:
    if len(source) != len(processed):
        return False

    def exact_key(triangle: tuple[tuple[float, float, float], ...]) -> tuple[Any, ...]:
        rotations = (triangle, triangle[1:] + triangle[:1], triangle[2:] + triangle[:2])
        return min(rotations)

    if Counter(map(exact_key, source)) == Counter(map(exact_key, processed)):
        return True

    def cell(tri: tuple[tuple[float, float, float], ...]) -> tuple[int, int, int]:
        return tuple(math.floor(sum(p[i] for p in tri) / (3 * tolerance)) for i in range(3))  # type: ignore[return-value]

    buckets: dict[tuple[int, int, int], list[int]] = {}
    for i, tri in enumerate(processed):
        buckets.setdefault(cell(tri), []).append(i)
    comparisons = 0
    candidates: list[list[int]] = []
    # Build a bounded bipartite graph, then use augmenting paths so spatially
    # ambiguous coincident triangles cannot make a valid multiset fail greedily.
    for tri in source:
        c = cell(tri)
        matches: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in buckets.get((c[0] + dx, c[1] + dy, c[2] + dz), []):
                        comparisons += 1
                        if comparisons > 2_000_000:
                            raise ValueError("V0.7 triangle matching comparison limit exceeded")
                        other = processed[j]
                        if any(
                            all(
                                abs(tri[k][axis] - other[(k + shift) % 3][axis]) <= tolerance
                                for k in range(3)
                                for axis in range(3)
                            )
                            for shift in range(3)
                        ):
                            matches.append(j)
        if not matches:
            return False
        candidates.append(matches)
    matched_source: dict[int, int] = {}
    matched_processed: dict[int, int] = {}
    # Most-constrained-first reduces bounded recursion and matching work.
    order = sorted(range(len(candidates)), key=lambda index: len(candidates[index]))
    for start in order:
        parent_processed: dict[int, int] = {}
        visited_source = {start}
        stack = [start]
        free_processed: int | None = None
        while stack and free_processed is None:
            source_index = stack.pop()
            for processed_index in candidates[source_index]:
                if processed_index in parent_processed:
                    continue
                parent_processed[processed_index] = source_index
                owner = matched_processed.get(processed_index)
                if owner is None:
                    free_processed = processed_index
                    break
                if owner not in visited_source:
                    visited_source.add(owner)
                    stack.append(owner)
        if free_processed is None:
            return False
        cursor = free_processed
        while True:
            source_index = parent_processed[cursor]
            previous = matched_source.get(source_index)
            matched_source[source_index] = cursor
            matched_processed[cursor] = source_index
            if previous is None:
                break
            cursor = previous
    return len(matched_processed) == len(processed)


def _v07_bounds(
    triangles: list[tuple[tuple[float, float, float], ...]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    points = [point for triangle in triangles for point in triangle]
    if not points:
        raise ValueError("V0.7 visual geometry contains no triangles")
    return (
        (
            min(point[0] for point in points),
            min(point[1] for point in points),
            min(point[2] for point in points),
        ),
        (
            max(point[0] for point in points),
            max(point[1] for point in points),
            max(point[2] for point in points),
        ),
    )


def _v07_num_close(value: Any, expected: Any, tolerance: float = 1e-4) -> bool:
    if type(value) not in {int, float} or type(expected) not in {int, float}:
        return False
    try:
        actual_float, expected_float = float(value), float(expected)
    except (OverflowError, TypeError, ValueError):
        return False
    return (
        math.isfinite(actual_float)
        and math.isfinite(expected_float)
        and abs(actual_float - expected_float) <= tolerance
    )


def _v07_vec_close(value: Any, expected: Any, tolerance: float = 1e-4) -> bool:
    if (
        not isinstance(value, (list, tuple))
        or not isinstance(expected, (list, tuple))
        or len(value) != 3
        or len(expected) != 3
    ):
        return False
    return all(
        _v07_num_close(actual, target, tolerance)
        for actual, target in zip(value, expected, strict=True)
    )


def _v07_matrix3_close(value: Any, expected: Any, tolerance: float = 1e-4) -> bool:
    if (
        not isinstance(value, list)
        or not isinstance(expected, list)
        or len(value) != 3
        or len(expected) != 3
    ):
        return False
    return all(
        _v07_vec_close(row, target, tolerance) for row, target in zip(value, expected, strict=True)
    )


def _v07_check_box(
    triangles: list[tuple[tuple[float, float, float], ...]],
    expected_min: tuple[float, float, float],
    expected_max: tuple[float, float, float],
    tolerance: float,
) -> bool:
    if tolerance <= 0 or len(triangles) != 12:
        return False
    actual_min, actual_max = _v07_bounds(triangles)
    geom_tol = min(tolerance, 1e-5)
    if any(
        abs(actual_min[i] - expected_min[i]) > tolerance
        or abs(actual_max[i] - expected_max[i]) > tolerance
        for i in range(3)
    ):
        return False
    corners = [
        (x, y, z)
        for x in (actual_min[0], actual_max[0])
        for y in (actual_min[1], actual_max[1])
        for z in (actual_min[2], actual_max[2])
    ]
    face_triangles: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    face_areas: dict[tuple[int, int], float] = {}
    for tri in triangles:
        corner_ids = []
        for point in tri:
            match = next(
                (
                    index
                    for index, corner in enumerate(corners)
                    if all(abs(point[axis] - corner[axis]) <= geom_tol for axis in range(3))
                ),
                None,
            )
            if match is None:
                return False
            corner_ids.append(match)
        if len(set(corner_ids)) != 3:
            return False
        face = next(
            (
                (axis, side)
                for axis in range(3)
                for side, boundary in ((-1, actual_min[axis]), (1, actual_max[axis]))
                if all(abs(point[axis] - boundary) <= geom_tol for point in tri)
            ),
            None,
        )
        if face is None:
            return False
        a, b, c = tri
        ab = tuple(b[i] - a[i] for i in range(3))
        ac = tuple(c[i] - a[i] for i in range(3))
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        area = math.sqrt(sum(v * v for v in cross)) / 2
        if area <= geom_tol * geom_tol:
            return False
        face_triangles.setdefault(face, []).append((corner_ids[0], corner_ids[1], corner_ids[2]))
        face_areas[face] = face_areas.get(face, 0.0) + area
    for axis in range(3):
        other_axes = [index for index in range(3) if index != axis]
        expected_area = (actual_max[other_axes[0]] - actual_min[other_axes[0]]) * (
            actual_max[other_axes[1]] - actual_min[other_axes[1]]
        )
        for side in (-1, 1):
            face = (axis, side)
            rows = face_triangles.get(face, [])
            expected_corners = {
                index
                for index, corner in enumerate(corners)
                if abs(corner[axis] - (actual_min[axis] if side < 0 else actual_max[axis]))
                <= geom_tol
            }
            if (
                len(rows) != 2
                or set(rows[0]) == set(rows[1])
                or set(rows[0]) | set(rows[1]) != expected_corners
                or abs(face_areas[face] - expected_area)
                > max(
                    geom_tol * max(actual_max[i] - actual_min[i] for i in range(3)) * 4,
                    geom_tol * geom_tol,
                )
            ):
                return False
            shared = set(rows[0]) & set(rows[1])
            if len(shared) != 2:
                return False
            first, second = (corners[index] for index in shared)
            if any(abs(first[i] - second[i]) <= geom_tol for i in other_axes):
                return False
    return True


def _verify_v07_bundle(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("generation_mode") == "provider_generated_character":
        return _verify_v07_provider_character_bundle(root, manifest)
    allowed_manifest_keys = {
        "schema_version",
        "workflow_id",
        "revision",
        "source_version",
        "asset_id",
        "generation_mode",
        "paid",
        "product_ready",
        "spec_sha256",
        "profile_id",
        "profile_version",
        "profile_document_sha256",
        "review_views",
        "source_glb_sha256",
        "source_provenance_sha256",
        "source_front",
        "normalization_applied",
        "selected_rule_groups",
        "processed_glb_sha256",
        "processing_execution_id",
        "processing_attempt_number",
        "processing_report_sha256",
        "process_report_sha256",
        "processing_script_sha256",
        "validation_execution_id",
        "validation_attempt_number",
        "validation_sha256",
        "runtime_execution_id",
        "runtime_attempt_number",
        "runtime_index_sha256",
        "runtime_request_digest",
        "runtime_profile_sha256",
        "runtime_specification_sha256",
        "runtime_observation_sha256",
        "runtime_harness_sha256",
        "capture_sha256",
        "current_attempt_history",
        "files",
        "final_review",
        "human_reviews",
    }
    if set(manifest) - allowed_manifest_keys:
        raise ValueError("V0.7 manifest contains unknown fields")
    if (
        manifest.get("generation_mode") != "local_operator_assembly"
        or manifest.get("paid") is not False
    ):
        raise ValueError("V0.7 evidence must be explicitly unpaid local-operator assembly")
    if manifest.get("product_ready") is not True:
        raise ValueError("V0.7 evidence is not cold-verified for product release")
    final_review = manifest.get("final_review")
    if not isinstance(final_review, dict) or final_review.get("decision") != "APPROVED":
        raise ValueError("V0.7 final human review is not approved")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > _MAX_FILES:
        raise ValueError("V0.7 evidence file list is invalid")
    rolemap: dict[str, list[dict[str, Any]]] = {}
    paths: set[str] = set()
    artifact_ids: set[str] = set()
    total = 0
    forbidden = {
        "paid_approval",
        "paid_request_snapshot",
        "production_readiness_report",
        "provider_operation",
        "cost_record",
        "provider_generated_glb",
    }
    for row in files:
        if not isinstance(row, dict):
            raise ValueError("V0.7 file entry is malformed")
        if set(row) - {
            "role",
            "path",
            "sha256",
            "size",
            "view",
            "artifact_id",
            "source_relative_path",
        }:
            raise ValueError("V0.7 file entry contains unknown fields")
        role = row.get("role")
        path = _path(row.get("path"))
        if role in forbidden or not isinstance(role, str) or path in paths:
            raise ValueError("V0.7 bundle contains forbidden role or duplicate path")
        if role not in {
            "specification",
            "concept",
            "concept_provenance",
            "concept_approval",
            "raw_glb",
            "source_provenance",
            "source_publication_marker",
            "source_approval",
            "bound_profile",
            "processed_glb",
            "processing_report",
            "validation",
            "runtime_observation",
            "runtime_request",
            "runtime_index",
            "processing_script",
            "runtime_harness",
            "runtime_capture",
            "review_html",
            "final_approval",
            "production_receipt",
        }:
            raise ValueError(f"unknown V0.7 evidence role: {role}")
        digest, size = row.get("sha256"), row.get("size")
        if (
            not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or type(size) is not int
            or size < 0
            or size > _MAX_FILE_BYTES
        ):
            raise ValueError("V0.7 role digest or size is invalid")
        total += size
        if total > _MAX_BUNDLE_BYTES:
            raise ValueError("V0.7 bundle exceeds total size limit")
        target = root.joinpath(*PurePosixPath(path).parts)
        cursor = target
        while cursor != root:
            if _linked(cursor):
                raise ValueError("V0.7 evidence path contains a link or junction")
            cursor = cursor.parent
        if not target.is_file() or target.stat().st_size != size:
            raise ValueError(f"V0.7 evidence file missing or size differs: {path}")
        raw = target.read_bytes()
        if len(raw) != size or _sha256(raw) != digest:
            raise ValueError(f"V0.7 evidence hash mismatch: {path}")
        paths.add(path)
        artifact_id = row.get("artifact_id")
        if artifact_id is not None:
            if not isinstance(artifact_id, str) or not artifact_id or artifact_id in artifact_ids:
                raise ValueError("V0.7 artifact identity is invalid or duplicated")
            artifact_ids.add(artifact_id)
        source_path = row.get("source_relative_path")
        if source_path is not None:
            _path(source_path)
        rolemap.setdefault(role, []).append(row)
    required = {
        "specification",
        "concept",
        "concept_provenance",
        "concept_approval",
        "raw_glb",
        "source_provenance",
        "source_publication_marker",
        "source_approval",
        "bound_profile",
        "processed_glb",
        "processing_report",
        "validation",
        "runtime_observation",
        "runtime_request",
        "runtime_index",
        "processing_script",
        "runtime_harness",
        "review_html",
        "final_approval",
        "production_receipt",
    }
    if required - set(rolemap):
        raise ValueError("V0.7 evidence is missing required roles")
    for role in required:
        if len(rolemap[role]) != 1:
            raise ValueError(f"V0.7 role must appear exactly once: {role}")
    capture_rows = rolemap.get("runtime_capture", [])
    views = {row.get("view") for row in capture_rows}
    review_views = manifest.get("review_views")
    if (
        not isinstance(review_views, list)
        or len(review_views) != 9
        or len(set(review_views)) != 9
        or set(review_views) != _V07_VIEWS
        or views != set(review_views)
        or len(capture_rows) != 9
    ):
        raise ValueError("V0.7 captures must contain the nine distinct profile views")
    on_disk: set[str] = set()
    for p in root.rglob("*"):
        if _linked(p):
            raise ValueError("V0.7 bundle contains a link or junction")
        if p.is_file():
            on_disk.add(p.relative_to(root).as_posix())
    if on_disk - {"manifest.json", "verify_asset_bundle.py"} != paths:
        raise ValueError("V0.7 bundle has unlisted or missing files")

    def one(role: str) -> dict[str, Any]:
        return rolemap[role][0]

    def payload(row: dict[str, Any]) -> bytes:
        target = root / row["path"]
        if _linked(target) or not target.is_file():
            raise ValueError("V0.7 evidence role disappeared or became linked during verification")
        with target.open("rb") as stream:
            raw = stream.read(row["size"] + 1)
        if len(raw) != row["size"] or _sha256(raw) != row["sha256"]:
            raise ValueError("V0.7 evidence role changed during semantic verification")
        return cast(bytes, raw)

    def role_object(row: dict[str, Any]) -> dict[str, Any]:
        role = row["role"]
        if row["size"] > _MAX_JSON_BYTES:
            raise ValueError(f"V0.7 JSON role exceeds size limit: {role}")

        def nonfinite(token: str) -> None:
            raise ValueError(f"V0.7 JSON contains non-finite number: {token}")

        def finite_float(token: str) -> float:
            value = float(token)
            if not math.isfinite(value):
                raise ValueError("V0.7 JSON number is outside finite float range")
            return value

        value = json.loads(
            payload(row).decode("utf-8"),
            object_pairs_hook=_unique_json,
            parse_constant=nonfinite,
            parse_float=finite_float,
        )
        if not isinstance(value, dict):
            raise ValueError(f"V0.7 JSON role must contain an object: {role}")
        return value

    def obj(role: str) -> dict[str, Any]:
        return role_object(one(role))

    workflow_id = manifest.get("workflow_id")
    revision = manifest.get("revision")
    asset_id = manifest.get("asset_id")
    if (
        not isinstance(workflow_id, str)
        or not workflow_id
        or type(revision) is not int
        or revision < 1
        or not _v07_same_int(manifest.get("source_version"), revision)
        or not isinstance(asset_id, str)
        or not asset_id
    ):
        raise ValueError("V0.7 evidence identity is invalid")
    spec = obj("specification")
    profile = obj("bound_profile")
    source_prov = obj("source_provenance")
    marker = obj("source_publication_marker")
    obj("concept_provenance")
    _png_dimensions(payload(one("concept")))
    if (
        spec.get("asset_id") != asset_id
        or spec.get("schema_version") != "0.7.0"
        or spec.get("source_kind") != "local_operator_assembly"
        or spec.get("profile") != manifest.get("profile_id")
        or not _v07_same_int(spec.get("profile_version"), manifest.get("profile_version"))
    ):
        raise ValueError("V0.7 specification does not bind local asset/profile")
    spec_hash = _spec_fingerprint(spec)
    if manifest.get("spec_sha256") != spec_hash:
        raise ValueError("V0.7 specification fingerprint mismatch")
    profile_hash = _spec_fingerprint(profile)
    runtime_spec_hash = _sha256(
        json.dumps(
            spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    )
    runtime_profile_hash = _sha256(
        json.dumps(
            profile,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )
    if (
        manifest.get("profile_document_sha256") != profile_hash
        or profile.get("schema_version") != "asset-profile-0.7.0"
        or profile.get("profile_id") != manifest.get("profile_id")
        or not _v07_same_int(profile.get("version"), manifest.get("profile_version"))
        or type(manifest.get("profile_version")) is not int
        or profile.get("geometry_mode") != "assembly"
        or "local_operator_assembly" not in profile.get("accepted_source_kinds", [])
        or set(profile.get("review_views", [])) != set(manifest.get("review_views", []))
    ):
        raise ValueError("V0.7 profile document binding mismatch")
    assembly_tolerance = profile.get("assembly")
    if not isinstance(assembly_tolerance, dict):
        raise ValueError("V0.7 assembly profile tolerances are missing")

    def tolerance_value(key: str) -> float:
        raw_value = assembly_tolerance.get(key)
        if type(raw_value) not in {int, float}:
            raise ValueError("V0.7 assembly profile tolerance is invalid")
        value = cast(int | float, raw_value)
        if not math.isfinite(float(value)) or value <= 0:
            raise ValueError("V0.7 assembly profile tolerance is invalid")
        return float(value)

    pivot_tolerance = tolerance_value("pivot_tolerance_m")
    basis_tolerance = tolerance_value("basis_tolerance_deg")
    socket_position_tolerance = tolerance_value("socket_position_tolerance_m")
    socket_angle_tolerance = tolerance_value("socket_angle_tolerance_deg")
    if source_prov.get("source_front") not in {"+Z", "-Z"} or source_prov.get(
        "source_front"
    ) != manifest.get("source_front"):
        raise ValueError("V0.7 source front declaration mismatch")
    if (
        manifest.get("normalization_applied") is not (source_prov.get("source_front") == "+Z")
        or spec.get("source_kind") != "local_operator_assembly"
    ):
        raise ValueError("V0.7 source normalization declaration is inconsistent")
    selected = manifest.get("selected_rule_groups")
    if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected):
        raise ValueError("V0.7 selected validation group list is malformed")
    expected_groups = ["core", "parts", "orientation_source_front", "pivot"]
    if spec.get("sockets") or profile.get("assembly", {}).get("required_sockets"):
        expected_groups.append("sockets")
    if (spec.get("collider") or {}).get("policy") == "capsule":
        expected_groups.append("collider_capsule")
    elif (spec.get("collider") or {}).get("policy") == "box":
        expected_groups.append("collider_box")
    if selected != expected_groups:
        raise ValueError("V0.7 validation groups differ from closed asset/profile capabilities")
    src_hash = one("raw_glb")["sha256"]
    processed_hash = one("processed_glb")["sha256"]
    raw_size = one("raw_glb")["size"]
    if (
        manifest.get("source_glb_sha256") != src_hash
        or manifest.get("source_provenance_sha256") != one("source_provenance")["sha256"]
        or manifest.get("processed_glb_sha256") != processed_hash
        or manifest.get("processing_report_sha256") != one("processing_report")["sha256"]
        or manifest.get("process_report_sha256") != one("processing_report")["sha256"]
        or manifest.get("processing_script_sha256") != one("processing_script")["sha256"]
        or manifest.get("validation_sha256") != one("validation")["sha256"]
        or manifest.get("runtime_observation_sha256") != one("runtime_observation")["sha256"]
        or manifest.get("runtime_harness_sha256") != one("runtime_harness")["sha256"]
    ):
        raise ValueError("V0.7 manifest does not bind all required current artifact hashes")
    expected_provenance_fields = {
        "schema_version",
        "source_provenance_type",
        "source_artifact_sha256",
        "source_artifact_byte_size",
        "paid",
        "authoring_tool_name",
        "authoring_tool_version",
        "source_front",
        "spec_fingerprint",
        "actor",
        "reason",
        "created_at",
        "part_map",
        "socket_map",
    }
    if (
        set(source_prov)
        not in (expected_provenance_fields, expected_provenance_fields | {"derived_from"})
        or set(marker)
        != {
            "schema_version",
            "marker_type",
            "source_artifact_sha256",
            "source_artifact_byte_size",
            "provenance_sha256",
            "provenance_byte_size",
            "spec_fingerprint",
            "created_at",
        }
        or source_prov.get("schema_version") != "0.7.0"
        or source_prov.get("source_provenance_type") != "local_operator_assembly"
        or source_prov.get("paid") is not False
        or source_prov.get("spec_fingerprint") != spec_hash
        or source_prov.get("source_artifact_sha256") != src_hash
        or source_prov.get("source_artifact_byte_size") != raw_size
        or not isinstance(source_prov.get("authoring_tool_name"), str)
        or not source_prov.get("authoring_tool_name")
        or not isinstance(source_prov.get("authoring_tool_version"), str)
        or not source_prov.get("authoring_tool_version")
        or not isinstance(source_prov.get("actor"), str)
        or not source_prov.get("actor")
        or not isinstance(source_prov.get("reason"), str)
        or not source_prov.get("reason")
        or source_prov.get("part_map") != {part["part_id"]: part for part in spec.get("parts", [])}
        or source_prov.get("socket_map")
        != {socket["socket_id"]: socket for socket in spec.get("sockets", [])}
    ):
        raise ValueError(
            "V0.7 source provenance does not bind raw bytes, specification, and declared semantics"
        )
    derived = source_prov.get("derived_from")
    if derived is not None and (
        not isinstance(derived, list)
        or len(derived) > 256
        or any(
            not isinstance(item, dict)
            or set(item)
            != {"part_id", "source_artifact_sha256", "source_asset_id", "source_revision"}
            or not isinstance(item.get("part_id"), str)
            or not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", item["part_id"])
            or not isinstance(item.get("source_asset_id"), str)
            or not re.fullmatch(r"^[a-z][a-z0-9_]{1,79}$", item["source_asset_id"])
            or not isinstance(item.get("source_artifact_sha256"), str)
            or not _SHA256.fullmatch(item["source_artifact_sha256"])
            or type(item.get("source_revision")) is not int
            or item["source_revision"] < 1
            for item in derived
        )
    ):
        raise ValueError("V0.7 source derivation pins are malformed")
    if (
        marker.get("schema_version") != "0.7.0"
        or marker.get("marker_type") != "assembly_publication_completion"
        or marker.get("source_artifact_sha256") != src_hash
        or marker.get("source_artifact_byte_size") != raw_size
        or marker.get("provenance_sha256") != one("source_provenance")["sha256"]
        or marker.get("provenance_byte_size") != one("source_provenance")["size"]
        or marker.get("spec_fingerprint") != spec_hash
    ):
        raise ValueError("V0.7 retained source marker does not bind pinned files")
    raw = payload(one("raw_glb"))
    processed = payload(one("processed_glb"))
    sdoc, sbin, snodes, sparents, sworld = _v07_glb(raw)
    pdoc, pbin, pnodes, pparents, pworld = _v07_glb(processed)
    for doc, nodes, parents, world, label in (
        (sdoc, snodes, sparents, sworld, "source"),
        (pdoc, pnodes, pparents, pworld, "processed"),
    ):
        scene_roots = doc["scenes"][doc.get("scene", 0)].get("nodes", [])
        if scene_roots != [nodes.get("ROOT")]:
            raise ValueError(f"V0.7 {label} active scene must contain only semantic ROOT")
        root_i = nodes.get("ROOT")
        if (
            root_i is None
            or root_i in parents
            or any(
                abs(world[root_i][r][c] - (1.0 if r == c else 0.0)) > 1e-5
                for r in range(4)
                for c in range(4)
            )
        ):
            raise ValueError(f"V0.7 {label} GLB requires one identity scene ROOT")
    parts = spec.get("parts")
    sockets = spec.get("sockets", [])
    if not isinstance(parts, list) or not parts or not isinstance(sockets, list):
        raise ValueError("V0.7 assembly part/socket specification is malformed")
    expected_parts = {f"PART_{p.get('part_id')}" for p in parts if isinstance(p, dict)}
    expected_sockets = {f"SOCKET_{s.get('socket_id')}" for s in sockets if isinstance(s, dict)}
    if len(expected_parts) != len(parts) or len(expected_sockets) != len(sockets):
        raise ValueError("V0.7 part or socket IDs are not unique")
    expected_lod = {f"SM_{asset_id}_{p.get('part_id')}_LOD0" for p in parts}
    processing_policy = profile.get("processing", {})
    lod1_required = (
        spec.get("lod_policy") == "lod0_lod1" or processing_policy.get("lod1_required") is True
    )
    collider_box = (spec.get("collider") or {}).get("policy") == "box"
    for doc, nodes, parents, label in (
        (sdoc, snodes, sparents, "source"),
        (pdoc, pnodes, pparents, "processed"),
    ):
        allowed = {"ROOT"} | expected_parts | expected_sockets | expected_lod
        if label == "processed":
            if lod1_required:
                allowed |= {f"SM_{asset_id}_{p.get('part_id')}_LOD1" for p in parts}
            if collider_box:
                allowed.add(f"COL_{asset_id}")
        if set(nodes) != allowed:
            raise ValueError(f"V0.7 {label} GLB semantic node inventory is invalid")
        rootpart = next(p for p in parts if p.get("parent") == "root")
        if parents.get(nodes[f"PART_{rootpart['part_id']}"]) != nodes["ROOT"]:
            raise ValueError(f"V0.7 {label} root PART is not direct ROOT child")
        expected_root_children = {nodes[f"PART_{rootpart['part_id']}"]}
        if label == "processed" and collider_box:
            expected_root_children.add(nodes[f"COL_{asset_id}"])
        if set(doc["nodes"][nodes["ROOT"]].get("children", [])) != expected_root_children:
            raise ValueError(f"V0.7 {label} ROOT has extra or missing direct children")
        for part in parts:
            name = f"PART_{part['part_id']}"
            parent = "ROOT" if part.get("parent") == "root" else f"PART_{part.get('parent')}"
            if parent not in nodes or parents.get(nodes[name]) != nodes[parent]:
                raise ValueError(f"V0.7 {label} PART hierarchy mismatch")
            lod = f"SM_{asset_id}_{part['part_id']}_LOD0"
            if parents.get(nodes[lod]) != nodes[name]:
                raise ValueError(f"V0.7 {label} LOD0 must be a direct PART child")
            if any(
                abs(_v07_matrix(doc["nodes"][nodes[lod]])[r][c] - (1.0 if r == c else 0.0)) > 1e-5
                for r in range(4)
                for c in range(4)
            ):
                raise ValueError(f"V0.7 {label} LOD0 local transform must remain identity")
            lod1 = f"SM_{asset_id}_{part['part_id']}_LOD1"
            if label == "processed" and lod1_required:
                if parents.get(nodes[lod1]) != nodes[name]:
                    raise ValueError(f"V0.7 processed LOD1 must be direct child of {name}")
                if any(
                    abs(_v07_matrix(doc["nodes"][nodes[lod1]])[r][c] - (1.0 if r == c else 0.0))
                    > 1e-5
                    for r in range(4)
                    for c in range(4)
                ):
                    raise ValueError("V0.7 processed LOD1 local transform must be identity")
        for socket in sockets:
            name = f"SOCKET_{socket['socket_id']}"
            parent = f"PART_{socket['parent_part']}"
            if (
                parent not in nodes
                or parents.get(nodes[name]) != nodes[parent]
                or doc["nodes"][nodes[name]].get("children", [])
            ):
                raise ValueError(f"V0.7 {label} socket parent/leaf is invalid")
    # Only the declared root PART may receive the one +Z -> -Z parent-frame turn.
    rp = next(p for p in parts if p.get("parent") == "root")
    rp_name = f"PART_{rp['part_id']}"
    si = snodes[rp_name]
    pi = pnodes[rp_name]
    declared_front = source_prov["source_front"]
    expected_rotation = (
        [[-1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, -1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
        if declared_front == "+Z"
        else [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]
    )
    for r in range(4):
        for c in range(4):
            expected = sum(
                expected_rotation[r][k] * _v07_matrix(sdoc["nodes"][si])[k][c] for k in range(4)
            )
            if abs(_v07_matrix(pdoc["nodes"][pi])[r][c] - expected) > 1e-5:
                raise ValueError(
                    "V0.7 root PART transform is not exactly the declared parent-frame normalization"
                )
    for name in expected_parts | expected_sockets:
        if name == rp_name:
            continue
        a, b = _v07_matrix(sdoc["nodes"][snodes[name]]), _v07_matrix(pdoc["nodes"][pnodes[name]])
        if any(abs(a[r][c] - b[r][c]) > 1e-5 for r in range(4) for c in range(4)):
            raise ValueError(f"V0.7 local transform changed: {name}")
        if name in expected_parts and sdoc["nodes"][snodes[name]].get("extras") != pdoc["nodes"][
            pnodes[name]
        ].get("extras"):
            raise ValueError(f"V0.7 articulation metadata changed: {name}")
    for part in parts:
        name = f"PART_{part['part_id']}"
        source_pos, source_basis, source_scale = _v07_parts(sdoc["nodes"][snodes[name]])
        processed_pos, processed_basis, processed_scale = _v07_parts(pdoc["nodes"][pnodes[name]])
        if (
            max(source_scale) - min(source_scale) > 1e-5
            or max(processed_scale) - min(processed_scale) > 1e-5
            or any(abs(a - b) > 1e-5 for a, b in zip(source_scale, processed_scale, strict=True))
        ):
            raise ValueError(f"V0.7 PART {name} scale is not positive uniform and preserved")
        pivot = part.get("pivot")
        if not isinstance(pivot, dict):
            raise ValueError(f"V0.7 PART {name} has no declared pivot")
        declared_position = pivot.get("position_m")
        declared_basis = _v07_quaternion_basis(pivot.get("basis", "identity"))
        extras = pdoc["nodes"][pnodes[name]].get("extras", {})
        motion = pivot.get("motion", {})
        if (
            not isinstance(declared_position, list)
            or len(declared_position) != 3
            or any(
                type(component) not in {int, float} or not math.isfinite(component)
                for component in declared_position
            )
            or any(
                abs(actual - declared) > pivot_tolerance
                for actual, declared in zip(processed_pos, declared_position, strict=True)
            )
            or _v07_basis_angle(processed_basis, declared_basis) > basis_tolerance
            or not isinstance(extras, dict)
            or extras.get("gf_motion") != motion.get("kind")
            or (
                motion.get("kind") != "fixed"
                and not _v07_close_vector(extras.get("gf_axis"), motion.get("axis"), 1e-4)
            )
        ):
            raise ValueError(
                f"V0.7 PART {name} differs from declared pivot beyond profile tolerance"
            )
    for socket in sockets:
        name = f"SOCKET_{socket['socket_id']}"
        source_pos, source_basis, source_scale = _v07_parts(sdoc["nodes"][snodes[name]])
        processed_pos, processed_basis, processed_scale = _v07_parts(pdoc["nodes"][pnodes[name]])
        declared_position = socket.get("translation_m")
        declared_basis = _v07_quaternion_basis(socket.get("rotation", "identity"))
        if (
            any(abs(value - 1.0) > 1e-5 for value in source_scale + processed_scale)
            or not isinstance(declared_position, list)
            or len(declared_position) != 3
            or any(
                type(component) not in {int, float} or not math.isfinite(component)
                for component in declared_position
            )
            or any(
                abs(actual - declared) > socket_position_tolerance
                for actual, declared in zip(processed_pos, declared_position, strict=True)
            )
            or _v07_basis_angle(processed_basis, declared_basis) > socket_angle_tolerance
        ):
            raise ValueError(f"V0.7 socket {name} differs from its declared transform")
    source_tris = _v07_tris(sdoc, sbin, snodes, sworld, "_LOD0")
    processed_tris = _v07_tris(pdoc, pbin, pnodes, pworld, "_LOD0")
    if declared_front == "+Z":
        source_tris = {
            name: [
                tuple((-point[0], point[1], -point[2]) for point in triangle)
                for triangle in triangles
            ]
            for name, triangles in source_tris.items()
        }
    if set(source_tris) != set(processed_tris) or any(
        not _v07_compare_triangles(source_tris[n], processed_tris[n]) for n in source_tris
    ):
        raise ValueError("V0.7 oriented LOD0 triangle topology/positions changed")
    lod0_min, lod0_max = _v07_bounds(
        [triangle for triangles in processed_tris.values() for triangle in triangles]
    )
    dimensions = spec.get("dimensions")
    if not isinstance(dimensions, dict):
        raise ValueError("V0.7 typed dimensions are malformed")
    expected_size = [
        dimensions.get("width_m"),
        dimensions.get("height_m"),
        dimensions.get("depth_m"),
    ]
    actual_size = [lod0_max[i] - lod0_min[i] for i in range(3)]
    dim_tolerance_raw = profile.get("processing", {}).get("dimension_tolerance_m")
    if type(dim_tolerance_raw) not in {int, float}:
        raise ValueError("V0.7 dimension tolerance is malformed")
    dim_tolerance_value = float(cast(int | float, dim_tolerance_raw))
    expected_dimensions: list[float] = []
    for expected_component in expected_size:
        if type(expected_component) not in {int, float} or isinstance(expected_component, bool):
            raise ValueError("V0.7 typed dimensions are malformed")
        expected_dimensions.append(float(cast(int | float, expected_component)))
    dimensions_are_valid = (
        math.isfinite(dim_tolerance_value)
        and dim_tolerance_value > 0
        and all(
            math.isfinite(expected) and abs(actual - expected) <= dim_tolerance_value
            for actual, expected in zip(actual_size, expected_dimensions, strict=True)
        )
    )
    if not dimensions_are_valid:
        raise ValueError("V0.7 actual processed LOD0 bounds differ from declared dimensions")
    dim_tolerance = dim_tolerance_value
    if lod1_required:
        processed_lod1 = _v07_tris(pdoc, pbin, pnodes, pworld, "_LOD1")
        if set(processed_lod1) != {f"SM_{asset_id}_{part['part_id']}_LOD1" for part in parts}:
            raise ValueError("V0.7 processed LOD1 inventory is incomplete")
        for name, triangles in processed_lod1.items():
            lo1, hi1 = _v07_bounds(triangles)
            lo0, hi0 = _v07_bounds(processed_tris[name.replace("_LOD1", "_LOD0")])
            if any(
                abs(lo1[i] - lo0[i]) > dim_tolerance or abs(hi1[i] - hi0[i]) > dim_tolerance
                for i in range(3)
            ):
                raise ValueError(f"V0.7 LOD1 bounds differ from corresponding LOD0: {name}")
    _v07_enforce_core_budgets(pdoc, pbin, pnodes, pworld, spec, profile)
    if collider_box:
        col_name = f"COL_{asset_id}"
        col_i = pnodes[col_name]
        if pparents.get(col_i) != pnodes["ROOT"] or any(
            abs(pworld[col_i][r][c] - (1.0 if r == c else 0.0)) > 1e-5
            for r in range(4)
            for c in range(4)
        ):
            raise ValueError("V0.7 generated root box collider must be identity child of ROOT")
        collider_tris = _v07_tris(pdoc, pbin, pnodes, pworld, col_name).get(col_name, [])
        if not _v07_check_box(collider_tris, lod0_min, lod0_max, dim_tolerance):
            raise ValueError(
                "V0.7 root collider is not twelve full non-overlapping box-face triangles"
            )
    # Exact generated script and harness bytes must agree with their reports/requests.
    processing = obj("processing_report")
    request = obj("runtime_request")
    runtime_index = obj("runtime_index")
    observation = obj("runtime_observation")
    script_hash = one("processing_script")["sha256"]
    harness_hash = one("runtime_harness")["sha256"]
    if (
        processing.get("status") != "SUCCESS"
        or not _v07_same_int(processing.get("exit_code"), 0)
        or processing.get("asset_id") != asset_id
        or processing.get("spec_fingerprint") != spec_hash
        or processing.get("profile_id") != manifest.get("profile_id")
        or not _v07_same_int(processing.get("profile_version"), manifest.get("profile_version"))
        or processing.get("output_glb_sha256", processing.get("processed_glb_sha256"))
        != processed_hash
        or processing.get("source_glb_sha256", processing.get("input_raw_glb_sha256")) != src_hash
        or processing.get("processing_script_sha256", processing.get("script_sha256"))
        != script_hash
        or processing.get("provenance_sha256") != one("source_provenance")["sha256"]
        or processing.get("processing_execution_id")
        not in (None, manifest.get("processing_execution_id"))
        or (
            processing.get("attempt_number") is not None
            and not _v07_same_int(
                processing.get("attempt_number"), manifest.get("processing_attempt_number")
            )
        )
    ):
        raise ValueError("V0.7 processing report does not bind current script/input/output")
    validation = obj("validation")
    if (
        validation.get("status") != "PASS"
        or validation.get("passed") is not True
        or validation.get("source_glb_sha256") != src_hash
        or validation.get("processed_glb_sha256") != processed_hash
        or validation.get("profile_id") != manifest.get("profile_id")
        or not _v07_same_int(validation.get("profile_version"), manifest.get("profile_version"))
        or validation.get("profile_document_sha256") != profile_hash
        or validation.get("preservation_passed") is not True
    ):
        raise ValueError("V0.7 independent geometry validation report is stale or incomplete")
    request_profile = request.get("profile")
    if (
        request.get("schema_version") != "assembly-runtime-request-0.7.0"
        or request.get("workflow_id") != workflow_id
        or not _v07_same_int(request.get("revision"), revision)
        or request.get("asset_id") != asset_id
        or request.get("execution_id") != manifest.get("runtime_execution_id")
        or not _v07_same_int(request.get("attempt_number"), manifest.get("runtime_attempt_number"))
        or request.get("processed_glb_sha256") != processed_hash
        or request.get("harness_sha256") != harness_hash
        or request.get("profile_sha256") != runtime_profile_hash
        or request.get("specification_sha256") != runtime_spec_hash
        or not isinstance(request_profile, dict)
        or request_profile.get("profile_id") != manifest.get("profile_id")
        or not _v07_same_int(request_profile.get("version"), manifest.get("profile_version"))
        or request_profile.get("geometry_mode") != "assembly"
        or manifest.get("runtime_profile_sha256") != runtime_profile_hash
        or manifest.get("runtime_specification_sha256") != runtime_spec_hash
    ):
        raise ValueError("V0.7 Godot request does not bind exact bundled inputs")
    req_digest = request.get("request_digest")
    bare = {k: v for k, v in request.items() if k != "request_digest"}
    if (
        not isinstance(req_digest, str)
        or _sha256(
            json.dumps(
                bare, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ).encode()
        )
        != req_digest
    ):
        raise ValueError("V0.7 runtime request canonical digest mismatch")
    if manifest.get("runtime_request_digest") != req_digest:
        raise ValueError("V0.7 manifest does not bind the runtime request digest")
    execution = manifest.get("runtime_execution_id")
    attempt = manifest.get("runtime_attempt_number")
    if (
        type(manifest.get("processing_attempt_number")) is not int
        or type(manifest.get("validation_attempt_number")) is not int
        or type(attempt) is not int
        or attempt < 1
        or one("runtime_index")["sha256"] != manifest.get("runtime_index_sha256")
        or not _v07_same_int(runtime_index.get("schema_version"), 1)
        or runtime_index.get("status") != "PASS"
        or runtime_index.get("workflow_id") != workflow_id
        or runtime_index.get("execution_id") != execution
        or not _v07_same_int(runtime_index.get("attempt_number"), attempt)
        or runtime_index.get("processed_glb_sha256") != processed_hash
        or runtime_index.get("request_digest") != req_digest
        or runtime_index.get("runtime_request_sha256") != one("runtime_request")["sha256"]
        or runtime_index.get("runtime_observation_sha256") != one("runtime_observation")["sha256"]
        or runtime_index.get("runtime_harness_sha256") != harness_hash
        or runtime_index.get("observation") != observation
    ):
        raise ValueError("V0.7 runtime index bytes do not bind current request/observation/harness")
    expected_attempts = {
        "process": (
            manifest.get("processing_execution_id"),
            manifest.get("processing_attempt_number"),
        ),
        "validate": (
            manifest.get("validation_execution_id"),
            manifest.get("validation_attempt_number"),
        ),
        "godot": (execution, attempt),
    }
    histories = manifest.get("current_attempt_history")
    if not isinstance(histories, dict) or set(histories) != set(expected_attempts):
        raise ValueError("V0.7 current attempt history is missing or has unknown stages")
    for stage, (expected_id, expected_number) in expected_attempts.items():
        history = histories[stage]
        if not isinstance(history, list) or not history:
            raise ValueError(f"V0.7 {stage} execution history is empty")
        if any(not isinstance(item, dict) for item in history):
            raise ValueError(f"V0.7 {stage} execution history contains a malformed attempt")
        raw_numbers = [cast(dict[str, Any], item).get("attempt_number") for item in history]
        if any(type(number) is not int or number < 1 for number in raw_numbers):
            raise ValueError(f"V0.7 {stage} execution history is malformed")
        numbers = [cast(int, number) for number in raw_numbers]
        if numbers != sorted(set(numbers)):
            raise ValueError(f"V0.7 {stage} execution history is malformed")
        latest = history[-1]
        if not isinstance(expected_id, str) or type(expected_number) is not int:
            raise ValueError(f"V0.7 {stage} expected attempt binding is malformed")
        if (
            latest.get("id") != expected_id
            or not _v07_same_int(latest.get("attempt_number"), expected_number)
            or latest.get("status") != "COMPLETED"
        ):
            raise ValueError(f"V0.7 {stage} evidence does not use its latest successful attempt")
    for role, attempt_id, attempt_number in (
        ("processing_report", *expected_attempts["process"]),
        ("validation", *expected_attempts["validate"]),
        ("runtime_observation", *expected_attempts["godot"]),
        ("runtime_request", *expected_attempts["godot"]),
        ("runtime_index", *expected_attempts["godot"]),
        ("runtime_harness", *expected_attempts["godot"]),
    ):
        source_path = one(role).get("source_relative_path")
        if (
            not isinstance(source_path, str)
            or not isinstance(attempt_id, str)
            or type(attempt_number) is not int
            or attempt_id not in source_path
            or f"-a{attempt_number}" not in source_path
        ):
            raise ValueError(f"V0.7 {role} artifact is not bound to the selected execution")
    for capture in capture_rows:
        source_path = capture.get("source_relative_path")
        if (
            not isinstance(source_path, str)
            or not isinstance(execution, str)
            or type(attempt) is not int
            or execution not in source_path
            or f"-a{attempt}" not in source_path
        ):
            raise ValueError("V0.7 capture artifact is stale for the current Godot attempt")
    if request.get("review_views") != manifest.get("review_views"):
        raise ValueError("V0.7 runtime request review views differ from the bound profile")
    if (
        observation.get("status") != "PASS"
        or observation.get("errors") != []
        or observation.get("workflow_id") != workflow_id
        or not _v07_same_int(observation.get("revision"), revision)
        or observation.get("asset_id") != asset_id
        or observation.get("execution_id") != execution
        or not _v07_same_int(observation.get("attempt_number"), attempt)
        or observation.get("processed_glb_sha256") != processed_hash
        or observation.get("request_digest") != req_digest
        or observation.get("harness_sha256") != harness_hash
        or observation.get("profile_sha256") != runtime_profile_hash
        or observation.get("profile_id") != manifest.get("profile_id")
        or not _v07_same_int(observation.get("profile_version"), manifest.get("profile_version"))
        or observation.get("specification_sha256") != runtime_spec_hash
    ):
        raise ValueError("V0.7 Godot observation is stale or does not bind exact request")
    capture_hashes = {row.get("view"): row["sha256"] for row in capture_rows}
    observed_capture_hashes = {
        view: capture.get("sha256")
        for view, capture in observation.get("captures", {}).items()
        if isinstance(capture, dict)
    }
    if (
        manifest.get("capture_sha256") != capture_hashes
        or observed_capture_hashes != capture_hashes
        or runtime_index.get("capture_sha256") != capture_hashes
    ):
        raise ValueError("V0.7 runtime capture set/hashes do not match observation")
    if observation.get("restoration_verified") is not True:
        raise ValueError("V0.7 runtime observation did not verify restoration")
    semantic_root = observation.get("semantic_root")
    if not isinstance(semantic_root, dict) or semantic_root.get("name") != "ROOT":
        raise ValueError("V0.7 runtime observation does not identify semantic ROOT")
    root_transform = semantic_root.get("transform")
    if (
        not isinstance(root_transform, dict)
        or not _v07_close_vector(root_transform.get("origin"), [0, 0, 0], 1e-4)
        or not _v07_close_matrix3(
            root_transform.get("basis"), [[1, 0, 0], [0, 1, 0], [0, 0, 1]], 1e-4
        )
    ):
        raise ValueError("V0.7 runtime ROOT transform is not identity")
    runtime_parts = observation.get("parts_verified")
    if not isinstance(runtime_parts, list) or len(runtime_parts) != len(parts):
        raise ValueError("V0.7 runtime PART inventory is malformed")
    part_observations = {
        item.get("part_id"): item for item in runtime_parts if isinstance(item, dict)
    }
    if part_observations.keys() != {part["part_id"] for part in parts}:
        raise ValueError("V0.7 runtime PART inventory differs from specification")
    for part in parts:
        part_id = part["part_id"]
        name = f"PART_{part_id}"
        item = part_observations[part_id]
        position, _basis, scale = _v07_parts(pdoc["nodes"][pnodes[name]])
        local_basis = [row[:3] for row in _v07_matrix(pdoc["nodes"][pnodes[name]])[:3]]
        if (
            item.get("parent") != part.get("parent")
            or item.get("motion_kind") != part.get("pivot", {}).get("motion", {}).get("kind")
            or not _v07_close_vector(item.get("local_position"), position, 1e-4)
            or not _v07_close_matrix3(item.get("local_basis"), local_basis, 1e-4)
            or not _v07_close_vector(item.get("local_scale"), [scale[0]] * 3, 1e-4)
            or item.get("lod0_present") is not True
            or item.get("lod1_present") is not lod1_required
        ):
            raise ValueError(f"V0.7 runtime PART_{part_id} differs from processed GLB")
        motion = part.get("pivot", {}).get("motion", {})
        if motion.get("kind") != "fixed":
            if not _v07_close_vector(item.get("gf_axis"), motion.get("axis"), 1e-4):
                raise ValueError(f"V0.7 runtime PART_{part_id} articulation axis mismatch")
    runtime_sockets = observation.get("sockets_verified")
    if not isinstance(runtime_sockets, list) or len(runtime_sockets) != len(sockets):
        raise ValueError("V0.7 runtime socket inventory is malformed")
    socket_observations = {
        item.get("socket_id"): item for item in runtime_sockets if isinstance(item, dict)
    }
    if socket_observations.keys() != {socket["socket_id"] for socket in sockets}:
        raise ValueError("V0.7 runtime socket inventory differs from specification")
    for socket in sockets:
        socket_id = socket["socket_id"]
        name = f"SOCKET_{socket_id}"
        item = socket_observations[socket_id]
        local_position, _basis, _scale = _v07_parts(pdoc["nodes"][pnodes[name]])
        local_basis = [row[:3] for row in _v07_matrix(pdoc["nodes"][pnodes[name]])[:3]]
        world_position = [pworld[pnodes[name]][i][3] for i in range(3)]
        world_basis = [row[:3] for row in pworld[pnodes[name]][:3]]
        if (
            item.get("parent_part") != socket.get("parent_part")
            or item.get("marker_created") is not True
            or not _v07_close_vector(item.get("local_position"), local_position, 1e-4)
            or not _v07_close_matrix3(item.get("local_basis"), local_basis, 1e-4)
            or not _v07_close_vector(item.get("world_position"), world_position, 1e-4)
            or not _v07_close_matrix3(item.get("world_basis"), world_basis, 1e-4)
        ):
            raise ValueError(
                f"V0.7 runtime socket {socket_id} differs from full processed ancestry"
            )
    moving = {
        part["part_id"]
        for part in parts
        if part.get("pivot", {}).get("motion", {}).get("kind") != "fixed"
    }
    articulations = observation.get("articulation_results")
    if (
        not isinstance(articulations, list)
        or {item.get("part_id") for item in articulations if isinstance(item, dict)} != moving
        or len(articulations) != len(moving)
    ):
        raise ValueError("V0.7 runtime articulation inventory differs from moving PARTs")
    for item in articulations:
        if not isinstance(item, dict) or any(
            item.get(key) is not True
            for key in (
                "motion_applied",
                "pivot_world_ok",
                "axis_world_ok",
                "descendants_rigid_ok",
                "descendant_moved",
                "ancestors_siblings_unchanged",
                "restored_ok",
            )
        ):
            raise ValueError("V0.7 runtime articulation observation is incomplete")
    framing = observation.get("view_framing")
    profile_framing = profile.get("framing", {})
    if not isinstance(framing, dict) or set(framing) != set(review_views):
        raise ValueError("V0.7 runtime framing inventory differs from requested views")
    for view in review_views:
        expected_direction, expected_up, axis = _v07_expected_view(view)
        frame = framing[view]
        if not isinstance(frame, dict):
            raise ValueError(f"V0.7 runtime framing row is malformed: {view}")
        ratio = frame.get("height_ratio") if isinstance(frame, dict) else None
        if (
            frame.get("ok") is not True
            or frame.get("inside_viewport") is not True
            or frame.get("margin_ok") is not True
            or frame.get("horizontally_centered") is not True
            or frame.get("reference_between_camera_and_asset") is not False
            or frame.get("view_axis") != axis
            or not _v07_close_vector(frame.get("camera_direction"), expected_direction, 1e-4)
            or not _v07_close_vector(frame.get("camera_up"), expected_up, 1e-4)
            or type(ratio) not in {int, float}
            or type(frame.get("margin_fraction")) not in {int, float}
            or abs(frame["margin_fraction"] - profile_framing.get("margin_fraction", -1)) > 1e-5
            or type(frame.get("center_offset_ratio")) not in {int, float}
            or not math.isfinite(frame["center_offset_ratio"])
            or frame["center_offset_ratio"] < 0
            or frame["center_offset_ratio"] > 0.08
            or not profile_framing.get("min_screen_fraction", 0)
            <= ratio
            <= profile_framing.get("max_screen_fraction", 1)
        ):
            raise ValueError(f"V0.7 runtime {view} framing is outside profile policy")
    for row in capture_rows:
        _png_dimensions(payload(row))
    htmlp = _References()
    html_bytes = payload(one("review_html"))
    if len(html_bytes) > _MAX_JSON_BYTES:
        raise ValueError("V0.7 inert review page exceeds size limit")
    htmlp.feed(html_bytes.decode("utf-8"))
    expected_html_paths = {path for path in paths if path != one("review_html")["path"]}
    if htmlp.paths != expected_html_paths:
        raise ValueError("V0.7 inert review page must link every local evidence role exactly")
    receipts = {
        "concept_review": one("concept_approval"),
        "source_review": one("source_approval"),
        "final_visual_review": one("final_approval"),
    }
    for kind, row in receipts.items():
        receipt = role_object(row)
        if set(receipt) != {
            "schema_version",
            "workflow_id",
            "revision",
            "source_version",
            "approval_type",
            "status",
            "task_id",
            "inputs",
            "operation_hash",
            "fingerprint",
            "actor",
            "comment",
        }:
            raise ValueError(f"V0.7 human {kind} receipt fields are not closed")
        if (
            receipt.get("schema_version") != "production-receipt-0.7.0"
            or receipt.get("workflow_id") != workflow_id
            or not _v07_same_int(receipt.get("revision"), revision)
            or not _v07_same_int(receipt.get("source_version"), revision)
            or receipt.get("approval_type") != kind
            or receipt.get("status") != "APPROVED"
        ):
            raise ValueError(f"V0.7 human {kind} receipt is incomplete/stale")
        receipt_task_id = receipt.get("task_id")
        if not isinstance(receipt_task_id, str) or not receipt_task_id:
            raise ValueError(f"V0.7 human {kind} receipt has no task ID")
        fingerprint = _approval_hash(receipt_task_id, kind, receipt.get("inputs"))
        if (
            receipt.get("operation_hash") != fingerprint
            or receipt.get("fingerprint") != fingerprint
        ):
            raise ValueError(f"V0.7 human {kind} receipt fingerprint mismatch")
        if (
            kind == "final_visual_review"
            and manifest.get("final_review", {}).get("fingerprint") != fingerprint
        ):
            raise ValueError("V0.7 final human receipt fingerprint mismatch")
        inputs = receipt.get("inputs")
        scope = inputs.get("scope") if isinstance(inputs, dict) else None
        context = scope.get("handler_context") if isinstance(scope, dict) else None
        if not isinstance(context, dict):
            raise ValueError(f"V0.7 human {kind} receipt has no approved handler context")
        if kind == "concept_review" and (
            context.get("workflow_id") != workflow_id
            or context.get("asset_id") != asset_id
            or context.get("concept_image_sha256") != one("concept")["sha256"]
            or context.get("concept_provenance_sha256") != one("concept_provenance")["sha256"]
            or context.get("spec_sha256") != spec_hash
            or context.get("profile_document_sha256") != profile_hash
            or context.get("profile_id") != manifest.get("profile_id")
            or not _v07_same_int(context.get("profile_version"), manifest.get("profile_version"))
        ):
            raise ValueError("V0.7 concept approval does not bind the bundled concept and profile")
        if kind == "source_review" and (
            context.get("workflow_id") != workflow_id
            or context.get("asset_id") != asset_id
            or not _v07_same_int(context.get("source_version"), revision)
            or context.get("spec_sha256") != spec_hash
            or context.get("profile_document_sha256") != profile_hash
            or context.get("profile_id") != manifest.get("profile_id")
            or not _v07_same_int(context.get("profile_version"), manifest.get("profile_version"))
            or context.get("source_glb_sha256") != src_hash
            or context.get("source_provenance_sha256") != one("source_provenance")["sha256"]
            or context.get("source_front") != source_prov.get("source_front")
        ):
            raise ValueError("V0.7 source approval does not bind the retained source package")
        if kind == "final_visual_review" and (
            context.get("workflow_id") != workflow_id
            or not _v07_same_int(context.get("source_version"), revision)
            or context.get("asset_id") != asset_id
            or context.get("spec_sha256") != spec_hash
            or context.get("profile_document_sha256") != profile_hash
            or context.get("profile_id") != manifest.get("profile_id")
            or not _v07_same_int(context.get("profile_version"), manifest.get("profile_version"))
            or context.get("source_glb_sha256") != src_hash
            or context.get("processed_glb_sha256") != processed_hash
            or context.get("process_report_sha256", context.get("processing_report_sha256"))
            != one("processing_report")["sha256"]
            or context.get("validation_report_sha256") != one("validation")["sha256"]
            or context.get("runtime_execution_id") != execution
            or not _v07_same_int(context.get("runtime_attempt_number"), attempt)
            or context.get("runtime_index_sha256") != manifest.get("runtime_index_sha256")
            or context.get("capture_sha256") != capture_hashes
        ):
            raise ValueError(
                "V0.7 final approval does not bind the current output/runtime/captures"
            )
    production = obj("production_receipt")
    production_reviews = {
        kind: {
            "operation_hash": role_object(row).get("operation_hash"),
            "status": role_object(row).get("status"),
        }
        for kind, row in receipts.items()
    }
    production_binding = {
        **{
            key: value
            for key, value in manifest.items()
            if key not in {"product_ready", "files", "final_review", "human_reviews"}
        },
        "product_ready": False,
    }
    production_checks = {
        "closed_fields": set(production)
        == {
            "schema_version",
            "status",
            "workflow_id",
            "revision",
            "source_version",
            "asset_id",
            "paid",
            "generation_mode",
            "binding",
            "human_reviews",
            "role_sha256",
        },
        "schema": production.get("schema_version") == "production-receipt-0.7.0",
        "status": production.get("status") == "PASS",
        "unpaid": production.get("paid") is False,
        "generation_mode": production.get("generation_mode") == "local_operator_assembly",
        "workflow": production.get("workflow_id") == workflow_id,
        "asset": production.get("asset_id") == asset_id,
        "revision": _v07_same_int(production.get("revision"), revision),
        "source_version": _v07_same_int(production.get("source_version"), revision),
        "role_hashes": production.get("role_sha256")
        == {
            f"{x['role']}:{x.get('view', '')}": x["sha256"]
            for x in files
            if x["role"] not in {"production_receipt", "review_html"}
        },
        "manifest_reviews": manifest.get("human_reviews")
        == {kind: role_object(row)["operation_hash"] for kind, row in receipts.items()},
        "receipt_reviews": production.get("human_reviews") == production_reviews,
        "not_ready": production.get("binding", {}).get("product_ready") is False,
        "binding": production.get("binding") == production_binding,
    }
    if not all(production_checks.values()):
        failed = ", ".join(key for key, valid in production_checks.items() if not valid)
        raise ValueError(f"V0.7 production receipt is inconsistent: {failed}")
    return {
        "status": "PASS",
        "schema_version": "asset-evidence-0.7.0",
        "workflow_id": workflow_id,
        "revision": revision,
        "verified_files": len(files),
        "product_ready": True,
        "human_identity_authenticated": False,
        "source_semantics_authenticated": False,
        "execution_origin_authenticated": False,
        "capture_origin_authenticated": False,
        "currentness": "as_of_export_snapshot",
        "limitation": "A self-contained bundle verifies internal consistency only; source semantics remain a human review boundary, while human identity, execution origin, capture origin, and changes after export require an external trust anchor.",
    }


def _verify_v07_provider_character_bundle(root: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Verify a provider-generated single-mesh character evidence snapshot.

    This is a consistency verifier. Receipts and named actors are not identity
    credentials, and the package cannot authenticate remote provider/runtime
    execution or capture origin.
    """
    allowed_manifest = {
        "schema_version",
        "workflow_id",
        "revision",
        "source_version",
        "asset_id",
        "generation_mode",
        "paid",
        "product_ready",
        "spec_sha256",
        "profile_id",
        "profile_version",
        "profile_document_sha256",
        "review_views",
        "concept_sha256",
        "concept_provenance_sha256",
        "concept_approval_sha256",
        "paid_approval_sha256",
        "paid_request_snapshot_sha256",
        "production_readiness_report_sha256",
        "provider_operation_sha256",
        "cost_record_sha256",
        "raw_glb_sha256",
        "cost_ledger_row_count",
        "cost_ledger_slice_sha256",
        "processed_glb_sha256",
        "processing_execution_id",
        "processing_attempt_number",
        "processing_report_sha256",
        "processing_script_sha256",
        "validation_execution_id",
        "validation_attempt_number",
        "validation_sha256",
        "runtime_execution_id",
        "runtime_attempt_number",
        "runtime_request_digest",
        "runtime_observation_sha256",
        "runtime_harness_sha256",
        "capture_sha256",
        "current_attempt_history",
        "files",
        "final_review",
        "human_reviews",
    }
    if set(manifest) - allowed_manifest:
        raise ValueError("Provider-character manifest contains unknown fields")
    if (
        manifest.get("generation_mode") != "provider_generated_character"
        or manifest.get("paid") is not True
        or manifest.get("product_ready") is not True
    ):
        raise ValueError(
            "Provider-character evidence must represent a paid, cold-verified snapshot"
        )
    workflow_id, revision, asset_id = (
        manifest.get("workflow_id"),
        manifest.get("revision"),
        manifest.get("asset_id"),
    )
    if (
        not isinstance(workflow_id, str)
        or not workflow_id
        or type(revision) is not int
        or revision < 1
        or not _v07_same_int(manifest.get("source_version"), revision)
        or not isinstance(asset_id, str)
        or not asset_id
    ):
        raise ValueError("Provider-character workflow identity is invalid")

    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > _MAX_FILES:
        raise ValueError("Provider-character file list is invalid")
    allowed_roles = {
        "specification",
        "concept",
        "concept_provenance",
        "concept_approval",
        "paid_approval",
        "paid_request_snapshot",
        "production_readiness_report",
        "provider_operation",
        "cost_record",
        "provider_generated_glb",
        "processed_glb",
        "processing_report",
        "processing_script",
        "validation",
        "runtime_request",
        "runtime_observation",
        "runtime_harness",
        "runtime_capture",
        "final_approval",
        "production_receipt",
        "review_html",
        "bound_profile",
    }
    single_roles = allowed_roles - {"runtime_capture"}
    roles: dict[str, list[dict[str, Any]]] = {}
    seen_paths: set[str] = set()
    seen_artifacts: set[str] = set()
    total = 0

    def read_evidence_file(
        path: Path, *, limit: int, expected_size: int, expected_digest: str
    ) -> bytes:
        if limit < 0 or expected_size < 0 or expected_size > limit:
            raise ValueError("Provider-character role exceeds its bounded file size")
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or _linked(path)
            or getattr(before, "st_nlink", 1) != 1
            or before.st_size != expected_size
        ):
            raise ValueError("Provider-character file is not an unchanged bounded regular file")
        cursor = path.parent
        while cursor != root:
            if _linked(cursor):
                raise ValueError("Provider-character file path contains a link or junction")
            cursor = cursor.parent
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
            ) != (before.st_dev, before.st_ino, before.st_size):
                raise ValueError("Provider-character file changed while it was opened")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(limit + 1)
            after = os.fstat(fd)
            after_path = path.lstat()
            cursor = path.parent
            while cursor != root:
                if _linked(cursor):
                    raise ValueError("Provider-character file path changed to a link or junction")
                cursor = cursor.parent
            if (
                (after.st_dev, after.st_ino, after.st_size)
                != (opened.st_dev, opened.st_ino, opened.st_size)
                or (after_path.st_dev, after_path.st_ino, after_path.st_size)
                != (before.st_dev, before.st_ino, before.st_size)
                or len(raw) != expected_size
                or _sha256(raw) != expected_digest
            ):
                raise ValueError("Provider-character file changed during bounded verification")
            return raw
        finally:
            os.close(fd)

    for row in files:
        if not isinstance(row, dict) or set(row) - {
            "role",
            "path",
            "sha256",
            "size",
            "view",
            "artifact_id",
            "source_relative_path",
        }:
            raise ValueError("Provider-character file row is malformed")
        role, path = row.get("role"), _path(row.get("path"))
        if role not in allowed_roles or path in seen_paths:
            raise ValueError("Provider-character bundle has an unknown role or duplicate path")
        digest, size = row.get("sha256"), row.get("size")
        role_limit = (
            50 * 1024 * 1024
            if role in {"provider_generated_glb", "processed_glb"}
            else 25_000_000
            if role in {"concept", "runtime_capture"}
            else 4_000_000
            if role in {"processing_script", "runtime_harness"}
            else 1_000_000
        )
        if (
            not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or type(size) is not int
            or size < 0
            or size > role_limit
        ):
            raise ValueError("Provider-character role digest or size is invalid")
        total += size
        if total > _MAX_BUNDLE_BYTES:
            raise ValueError("Provider-character bundle exceeds the total size limit")
        target = root.joinpath(*PurePosixPath(path).parts)
        cursor = target
        while cursor != root:
            if _linked(cursor):
                raise ValueError("Provider-character path contains a link or junction")
            cursor = cursor.parent
        if not target.is_file():
            raise ValueError(f"Provider-character file is missing: {path}")
        read_evidence_file(target, limit=role_limit, expected_size=size, expected_digest=digest)
        aid = row.get("artifact_id")
        if aid is not None:
            if (
                not isinstance(aid, str)
                or not aid
                or aid in seen_artifacts
                or not isinstance(row.get("source_relative_path"), str)
            ):
                raise ValueError("Provider-character artifact ID is invalid or repeated")
            seen_artifacts.add(aid)
        source_relative_path = row.get("source_relative_path")
        if source_relative_path is not None:
            _path(source_relative_path)
        elif aid is not None:
            raise ValueError("Provider-character artifact source path is missing")
        if role == "runtime_capture" and row.get("view") not in _V07_VIEWS:
            raise ValueError("Provider-character capture view is unknown")
        if role != "runtime_capture" and row.get("view") is not None:
            raise ValueError("Only runtime captures may name a view")
        roles.setdefault(role, []).append(row)
        seen_paths.add(path)
    required = allowed_roles - {"runtime_capture"}
    if required - set(roles) or any(len(roles.get(role, [])) != 1 for role in single_roles):
        raise ValueError("Provider-character evidence is missing or duplicates a required role")
    views = manifest.get("review_views")
    captures = roles.get("runtime_capture", [])
    if (
        not isinstance(views, list)
        or len(views) not in (5, 9)
        or len(set(views)) != len(views)
        or any(view not in _V07_VIEWS for view in views)
        or {row.get("view") for row in captures} != set(views)
        or len(captures) != len(views)
    ):
        raise ValueError("Provider-character captures do not match the profile review views")
    history = manifest.get("current_attempt_history")
    expected_history = {
        "process": (
            manifest.get("processing_execution_id"),
            manifest.get("processing_attempt_number"),
        ),
        "validate": (
            manifest.get("validation_execution_id"),
            manifest.get("validation_attempt_number"),
        ),
        "godot": (manifest.get("runtime_execution_id"), manifest.get("runtime_attempt_number")),
    }
    if not isinstance(history, dict) or set(history) != set(expected_history):
        raise ValueError("Provider-character stage attempt history is incomplete")
    for stage, (execution_id, attempt_number) in expected_history.items():
        attempts = history[stage]
        if not isinstance(attempts, list) or not attempts or len(attempts) > 256:
            raise ValueError(f"Provider-character {stage} attempt history is invalid")
        numbers = []
        ids = set()
        for attempt in attempts:
            if (
                not isinstance(attempt, dict)
                or set(attempt) != {"id", "attempt_number", "status"}
                or not isinstance(attempt.get("id"), str)
                or not attempt["id"]
                or attempt["id"] in ids
                or type(attempt.get("attempt_number")) is not int
                or attempt["attempt_number"] < 1
                or attempt.get("status") not in {"COMPLETED", "FAILED", "UNCERTAIN", "RUNNING"}
            ):
                raise ValueError(f"Provider-character {stage} attempt row is malformed")
            numbers.append(attempt["attempt_number"])
            ids.add(attempt["id"])
        latest = attempts[-1]
        if (
            numbers != sorted(set(numbers))
            or latest["id"] != execution_id
            or latest["attempt_number"] != attempt_number
            or latest["status"] != "COMPLETED"
            or any(row["status"] == "RUNNING" for row in attempts)
        ):
            raise ValueError(f"Provider-character {stage} latest attempt is stale or incomplete")
    disk_paths: set[str] = set()
    for disk_path in root.rglob("*"):
        if _linked(disk_path):
            raise ValueError("Provider-character bundle contains a link or junction")
        if disk_path.is_file():
            disk_paths.add(disk_path.relative_to(root).as_posix())
    if disk_paths - {"manifest.json", "verify_asset_bundle.py"} != seen_paths:
        raise ValueError("Provider-character bundle contains unlisted or missing files")

    def payload(role: str) -> bytes:
        row = roles[role][0]
        path = root.joinpath(*PurePosixPath(row["path"]).parts)
        role_limit = (
            50 * 1024 * 1024
            if role in {"provider_generated_glb", "processed_glb"}
            else 25_000_000
            if role in {"concept", "runtime_capture"}
            else 4_000_000
            if role in {"processing_script", "runtime_harness"}
            else 1_000_000
        )
        return read_evidence_file(
            path, limit=role_limit, expected_size=row["size"], expected_digest=row["sha256"]
        )

    if roles["review_html"][0]["path"] != "index.html":
        raise ValueError("Provider-character review page must be root index.html")
    try:
        html_text = payload("review_html").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Provider-character review HTML must be UTF-8") from exc
    html_parser = _References()
    html_parser.feed(html_text)
    expected_html_paths = {
        row["path"] for row in files if row["role"] not in {"review_html", "production_receipt"}
    }
    if html_parser.paths != expected_html_paths:
        raise ValueError("Provider-character review HTML links do not match bundled evidence")

    def obj(role: str) -> dict[str, Any]:
        raw = payload(role)
        if len(raw) > _MAX_JSON_BYTES:
            raise ValueError(f"Provider-character JSON role exceeds limit: {role}")

        def reject_constant(token: str) -> None:
            raise ValueError(f"non-finite JSON value: {token}")

        def finite_float(token: str) -> float:
            value = float(token)
            if not math.isfinite(value):
                raise ValueError("JSON number is outside finite range")
            return value

        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json,
            parse_constant=reject_constant,
            parse_float=finite_float,
        )
        if not isinstance(value, dict):
            raise ValueError(f"Provider-character role must be a JSON object: {role}")
        return value

    def reject_secrets_paths_urls(value: Any, breadcrumb: str = "$", role: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = key.lower()
                if any(
                    marker in normalized
                    for marker in (
                        "api_key",
                        "secret",
                        "token",
                        "password",
                        "credential",
                        "authorization",
                    )
                ):
                    raise ValueError(
                        f"Provider-character {role} contains a credential-like field at {breadcrumb}.{key}"
                    )
                reject_secrets_paths_urls(child, f"{breadcrumb}.{key}", role)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                reject_secrets_paths_urls(child, f"{breadcrumb}[{index}]", role)
        elif isinstance(value, str):
            if (
                re.search(r"(?i)https?://|wss?://", value)
                or re.match(r"(?i)^[a-z]:[\\/]", value)
                or value.startswith(("\\\\", "//", "/"))
            ):
                raise ValueError(
                    f"Provider-character {role} contains an absolute path or URL at {breadcrumb}"
                )

    for role in allowed_roles - {
        "review_html",
        "concept",
        "runtime_capture",
        "provider_generated_glb",
        "processed_glb",
        "processing_script",
        "runtime_harness",
    }:
        reject_secrets_paths_urls(obj(role), role=role)

    spec, profile = obj("specification"), obj("bound_profile")
    if (
        spec.get("schema_version") != "0.7.0"
        or spec.get("asset_id") != asset_id
        or spec.get("source_kind") != "provider_generated"
        or spec.get("category") != "character"
        or spec.get("profile") != manifest.get("profile_id")
        or not _v07_same_int(spec.get("profile_version"), manifest.get("profile_version"))
        or spec.get("parts") is not None
        or spec.get("sockets") is not None
        or not isinstance(spec.get("collider"), dict)
        or spec.get("collider", {}).get("policy") != "capsule"
    ):
        raise ValueError(
            "Provider-character specification exceeds the trusted single-mesh capsule contract"
        )
    spec_hash = _spec_fingerprint(spec)
    profile_hash = _spec_fingerprint(profile)
    if (
        manifest.get("spec_sha256") != spec_hash
        or manifest.get("profile_document_sha256") != profile_hash
        or profile.get("schema_version") != "asset-profile-0.7.0"
        or profile.get("profile_id") != manifest.get("profile_id")
        or not _v07_same_int(profile.get("version"), manifest.get("profile_version"))
        or profile.get("geometry_mode") != "single_mesh"
        or profile.get("accepted_source_kinds") != ["provider_generated"]
        or set(profile.get("review_views", [])) != set(views)
        or not {"front", "rear", "left", "right", "three_quarter"} <= set(views)
    ):
        raise ValueError("Provider-character profile binding is invalid")
    if _sha256(payload("concept")) != manifest.get("concept_sha256"):
        raise ValueError("Provider-character concept hash differs")
    _png_dimensions(payload("concept"))
    concept_prov = obj("concept_provenance")
    if (
        concept_prov.get("artifact_hash") != manifest.get("concept_sha256")
        or concept_prov.get("asset_spec_hash") != spec_hash
    ):
        raise ValueError("Provider-character concept provenance does not bind the concept")

    snapshot = obj("paid_request_snapshot")
    snap_bytes = json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    snapshot_hash = _sha256(snap_bytes)
    snapshot_binding = snapshot.get("binding")
    if (
        snapshot_hash != manifest.get("paid_request_snapshot_sha256")
        or set(snapshot)
        != {"schema", "provider", "operation", "adapter", "binding", "request", "cost"}
        or snapshot.get("schema") != "paid-request-0.6.0"
        or not isinstance(snapshot_binding, dict)
        or set(snapshot_binding)
        != {
            "asset_id",
            "revision_number",
            "concept_version",
            "concept_sha256",
            "specification_sha256",
            "profile_id",
            "profile_version",
        }
        or snapshot_binding.get("asset_id") != asset_id
        or not _v07_same_int(snapshot_binding.get("revision_number"), revision)
        or snapshot_binding.get("concept_sha256") != manifest.get("concept_sha256")
        or snapshot_binding.get("specification_sha256") != spec_hash
        or snapshot_binding.get("profile_id") != manifest.get("profile_id")
        or not _v07_same_int(
            snapshot_binding.get("profile_version"), manifest.get("profile_version")
        )
        or not isinstance(snapshot.get("provider"), str)
        or not snapshot.get("provider")
        or not isinstance(snapshot.get("operation"), str)
        or not snapshot.get("operation")
        or not isinstance(snapshot.get("adapter"), dict)
        or not isinstance(snapshot.get("request"), dict)
        or not isinstance(snapshot.get("cost"), dict)
    ):
        raise ValueError("Provider-character paid-request snapshot is invalid or stale")
    readiness = obj("production_readiness_report")
    readiness_checks = readiness.get("checks")
    check_names = {
        "provider_available",
        "blender_available",
        "godot_available",
        "workspace_writable",
        "profile_supported",
    }
    required_checksets = {
        frozenset(check_names),
        frozenset(
            check_names
            | {
                "profile_review_views_implemented",
                "profile_runtime_validations_implemented",
            }
        ),
        frozenset(
            {
                "provider_adapter_available",
                "provider_credential_configured",
                "blender_configured",
                "blender_executable",
                "blender_launch_version",
                "blender_version_supported",
                "blender_python_dependencies",
                "godot_configured",
                "godot_executable",
                "godot_launch_version",
                "godot_version_supported",
                "workspace_writable",
                "scratch_creatable",
                "free_space",
                "profile_supported",
                "profile_review_views_implemented",
                "profile_runtime_validations_implemented",
            }
        ),
    }
    observed_names = (
        [check.get("name") for check in readiness_checks]
        if isinstance(readiness_checks, list)
        and all(isinstance(check, dict) for check in readiness_checks)
        else []
    )
    if (
        readiness.get("schema") != "production-readiness-0.6.0"
        or readiness.get("result") != "PASS"
        or readiness.get("asset_id") != asset_id
        or not _v07_same_int(readiness.get("revision_number"), revision)
        or readiness.get("profile_id") != manifest.get("profile_id")
        or not _v07_same_int(readiness.get("profile_version"), manifest.get("profile_version"))
        or readiness.get("paid_request_snapshot_sha256") != snapshot_hash
        or not isinstance(readiness_checks, list)
        or frozenset(observed_names) not in required_checksets
        or len(observed_names) != len(set(observed_names))
        or any(
            not isinstance(check, dict)
            or set(check) != {"name", "category", "status", "critical", "detail", "observed"}
            or check.get("status") != "PASS"
            or check.get("critical") is not True
            or not isinstance(check.get("category"), str)
            or not check["category"]
            or not isinstance(check.get("detail"), str)
            or not check["detail"]
            or not isinstance(check.get("observed"), dict)
            for check in readiness_checks
        )
    ):
        raise ValueError("Provider-character readiness report is not current PASS")
    approval = obj("paid_approval")
    intent = obj("provider_operation")
    intent_keys = {
        "id",
        "workflow_id",
        "task_id",
        "asset_id",
        "revision_number",
        "provider",
        "operation",
        "concept_hash",
        "request_fingerprint",
        "approval_id",
        "estimated_cost",
        "actual_cost",
        "cost_unit",
        "external_task_id",
        "status",
        "created_at",
        "updated_at",
        "paid_request_snapshot_hash",
        "raw_glb_sha256",
        "execution_id",
        "attempt_number",
        "paid_execution_external_id",
        "paid_execution_provider",
        "paid_execution_status",
        "paid_execution_cost",
        "paid_execution_history",
    }
    raw_row = roles["provider_generated_glb"][0]
    raw_hash = raw_row["sha256"]
    if (
        set(intent) != intent_keys
        or intent.get("status") != "SUCCEEDED"
        or intent.get("workflow_id") != workflow_id
        or not isinstance(intent.get("external_task_id"), str)
        or not intent.get("external_task_id")
        or intent.get("asset_id") != asset_id
        or not _v07_same_int(intent.get("revision_number"), revision)
        or intent.get("paid_execution_provider") != intent.get("provider")
        or intent.get("operation") != snapshot.get("operation")
        or intent.get("concept_hash") != manifest.get("concept_sha256")
        or intent.get("request_fingerprint") != snapshot_hash
        or intent.get("paid_request_snapshot_hash") != snapshot_hash
        or intent.get("raw_glb_sha256") != raw_hash
        or approval.get("status") != "APPROVED"
        or approval.get(
            "paid_request_snapshot_hash",
            approval.get("inputs", {}).get("paid_request_snapshot_sha256"),
        )
        != snapshot_hash
        or intent.get("approval_id") != approval.get("id")
        or intent.get("task_id") != approval.get("task_id")
        or not isinstance(intent.get("execution_id"), str)
        or not intent.get("execution_id")
        or not isinstance(intent.get("paid_execution_external_id"), str)
        or intent.get("paid_execution_external_id") != intent.get("external_task_id")
        or intent.get("paid_execution_status") != "COMPLETED"
        or type(intent.get("attempt_number")) is not int
        or intent["attempt_number"] < 1
    ):
        raise ValueError(
            "Provider operation, paid approval, provider ID, or raw-byte pin is inconsistent"
        )
    paid_execution_ids = _v07_paid_execution_history(intent, workflow_id, revision)
    cost = obj("cost_record")
    ledger_entries = cost.get("entries")
    ledger_account = cost.get("operation_account")
    ledger_schema = {
        "id",
        "project_id",
        "workflow_id",
        "task_id",
        "execution_id",
        "intent_id",
        "request_fingerprint",
        "entry_type",
        "amount",
        "cost_unit",
        "reason",
        "source",
        "actor",
        "created_at",
    }
    if (
        set(cost) != {"schema_version", "entries", "operation_account", "row_count", "slice_sha256"}
        or cost.get("schema_version") != "provider-character-cost-record-0.7.0"
        or not isinstance(ledger_entries, list)
        or not ledger_entries
        or type(manifest.get("cost_ledger_row_count")) is not int
        or len(ledger_entries) != manifest["cost_ledger_row_count"]
        or cost.get("row_count") != manifest.get("cost_ledger_row_count")
        or cost.get("slice_sha256") != manifest.get("cost_ledger_slice_sha256")
        or _sha256(
            json.dumps(
                ledger_entries, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        )
        != manifest.get("cost_ledger_slice_sha256")
        or not isinstance(ledger_account, dict)
    ):
        raise ValueError("Provider-character cost ledger slice is malformed or stale")
    operation_task = intent.get("task_id")
    project_ids: set[str] = set()
    row_ids: set[str] = set()
    reserves = releases = settled = 0.0
    settle_rows = []
    for entry in ledger_entries:
        amount = entry.get("amount") if isinstance(entry, dict) else None
        if (
            not isinstance(entry, dict)
            or set(entry) != ledger_schema
            or not isinstance(entry.get("id"), str)
            or not entry["id"]
            or entry["id"] in row_ids
            or not isinstance(entry.get("project_id"), str)
            or not entry["project_id"]
            or entry.get("workflow_id") != workflow_id
            or entry.get("task_id") != operation_task
            or entry.get("cost_unit") != intent.get("cost_unit")
            or not isinstance(entry.get("reason"), str)
            or not entry["reason"]
            or not isinstance(entry.get("source"), str)
            or not entry["source"]
            or not isinstance(entry.get("actor"), str)
            or not entry["actor"]
            or not isinstance(entry.get("created_at"), str)
            or not entry["created_at"]
            or type(amount) not in {int, float}
            or not isinstance(entry.get("entry_type"), str)
            or entry["entry_type"] not in {"RESERVE", "SETTLE", "RELEASE"}
        ):
            raise ValueError(
                "Provider-character ledger contains malformed, conflicting, or adjusted rows"
            )
        if type(amount) not in {int, float}:
            raise ValueError("Provider-character ledger amount is not numeric")
        amount_value = float(cast(int | float, amount))
        if not math.isfinite(amount_value):
            raise ValueError("Provider-character ledger amount is non-finite")
        row_ids.add(entry["id"])
        project_ids.add(entry["project_id"])
        metadata = (
            entry.get("intent_id"),
            entry.get("execution_id"),
            entry.get("request_fingerprint"),
        )
        if entry["entry_type"] == "RESERVE":
            if not _v07_reserve_metadata_allowed(metadata, paid_execution_ids):
                raise ValueError(
                    "Provider-character reservation metadata conflicts with its paid intent"
                )
            if amount_value < 0:
                raise ValueError("Provider-character reservation amount cannot be negative")
            reserves += amount_value
        else:
            if metadata != (
                intent.get("id"),
                intent.get("execution_id"),
                intent.get("request_fingerprint"),
            ):
                raise ValueError(
                    "Provider-character terminal ledger row is not pinned to the paid execution"
                )
            if amount_value < 0:
                raise ValueError("Provider-character terminal ledger amount cannot be negative")
            if entry["entry_type"] == "RELEASE":
                releases += amount_value
            else:
                settled += amount_value
                settle_rows.append(entry)
    if len(project_ids) != 1 or len(settle_rows) != 1:
        raise ValueError(
            "Provider-character paid operation must have exactly one settlement and project"
        )
    actual = intent.get("actual_cost")
    paid_execution_cost = intent.get("paid_execution_cost")
    if type(actual) not in {int, float} or type(paid_execution_cost) not in {int, float}:
        raise ValueError("Provider-character settled amount is not numeric")
    actual_value = float(cast(int | float, actual))
    paid_execution_cost_value = float(cast(int | float, paid_execution_cost))
    if (
        not math.isfinite(actual_value)
        or actual_value < 0
        or not math.isfinite(paid_execution_cost_value)
        or paid_execution_cost_value != actual_value
        or settle_rows[0]["amount"] != actual_value
    ):
        raise ValueError(
            "Provider-character settled amount differs from the terminal provider actual"
        )
    actual = actual_value
    account_keys = {
        "task_id",
        "project_id",
        "workflow_id",
        "reserved_total",
        "released_total",
        "settled_total",
        "adjustments",
        "held",
        "settled",
        "net",
        "cost_unit",
    }
    account_values = (
        "reserved_total",
        "released_total",
        "settled_total",
        "adjustments",
        "held",
        "net",
    )
    if (
        set(ledger_account) != account_keys
        or ledger_account.get("task_id") != operation_task
        or ledger_account.get("project_id") not in project_ids
        or ledger_account.get("workflow_id") != workflow_id
        or ledger_account.get("cost_unit") != intent.get("cost_unit")
        or ledger_account.get("settled") is not True
        or any(
            type(ledger_account.get(key)) not in {int, float}
            or not math.isfinite(float(ledger_account[key]))
            for key in account_values
        )
    ):
        raise ValueError("Provider-character paid operation account is malformed or unsettled")

    def account_close(key: str, value: float) -> bool:
        return math.isclose(float(ledger_account[key]), value, rel_tol=1e-12, abs_tol=1e-9)

    held = reserves - releases
    net = held + settled
    if (
        held < -1e-9
        or not account_close("reserved_total", reserves)
        or not account_close("released_total", releases)
        or not account_close("settled_total", settled)
        or not account_close("adjustments", 0.0)
        or not account_close("held", held)
        or not account_close("net", net)
        or not math.isclose(held, 0.0, abs_tol=1e-9)
        or not math.isclose(net, float(actual), rel_tol=1e-12, abs_tol=1e-9)
    ):
        raise ValueError("Provider-character paid ledger arithmetic is inconsistent or still held")
    receipts: dict[str, dict[str, Any]] = {}
    receipt_kinds = (
        ("concept_approval", "concept_review"),
        ("paid_approval", "paid_generation"),
        ("final_approval", "final_visual_review"),
    )
    for role, key in receipt_kinds:
        receipt = obj(role)
        if (
            set(receipt)
            != {
                "id",
                "workflow_id",
                "revision",
                "task_id",
                "approval_type",
                "status",
                "inputs",
                "operation_hash",
                "fingerprint",
                "paid_request_snapshot_hash",
                "concept_sha256",
                "actor",
                "reason",
                "comment",
                "decided_at",
            }
            or receipt.get("status") != "APPROVED"
            or receipt.get("workflow_id") != workflow_id
            or receipt.get("approval_type") != key
            or not _v07_same_int(receipt.get("revision"), revision)
            or not isinstance(receipt.get("id"), str)
            or not receipt["id"]
            or not isinstance(receipt.get("task_id"), str)
            or not receipt["task_id"]
            or not isinstance(receipt.get("inputs"), dict)
            or not isinstance(receipt.get("actor"), str)
            or not receipt["actor"].strip()
            or not isinstance(receipt.get("reason"), str)
            or not receipt["reason"].strip()
            or not isinstance(receipt.get("decided_at"), str)
            or not receipt["decided_at"]
        ):
            raise ValueError(f"Provider-character {role} is not approved for this workflow")
        task_id_value = receipt.get("task_id")
        if not isinstance(task_id_value, str) or not task_id_value:
            raise ValueError(f"Provider-character {key} receipt has no task ID")
        op_hash = _approval_hash(task_id_value, key, receipt["inputs"])
        if op_hash != receipt.get("operation_hash") or op_hash != receipt.get("fingerprint"):
            raise ValueError(f"Provider-character {role} operation hash is invalid")
        receipts[role] = receipt
    concept_context = (
        receipts["concept_approval"]["inputs"].get("scope", {}).get("handler_context", {})
    )
    paid_inputs = receipts["paid_approval"]["inputs"]
    paid_scope = paid_inputs.get("scope", {}) if isinstance(paid_inputs, dict) else {}
    paid_parameters = paid_inputs.get("parameters", {}) if isinstance(paid_inputs, dict) else {}
    paid_scope_artifacts = paid_scope.get("artifacts") if isinstance(paid_scope, dict) else None
    concept_artifacts = (
        concept_context.get("artifacts") if isinstance(concept_context, dict) else None
    )
    if not isinstance(concept_context, dict):
        raise ValueError("Provider-character concept approval has no handler context")
    if not isinstance(concept_artifacts, dict):
        raise ValueError("Provider-character concept approval has no artifact hash map")
    if not isinstance(paid_parameters, dict) or not isinstance(paid_scope, dict):
        raise ValueError("Provider-character paid approval scope or parameters are malformed")
    if not isinstance(paid_scope_artifacts, list) or any(
        not isinstance(pair, list)
        or len(pair) != 2
        or not all(isinstance(value, str) for value in pair)
        for pair in paid_scope_artifacts
    ):
        raise ValueError("Provider-character paid approval artifact pins are malformed")
    concept_expected = {
        "asset_id": asset_id,
        "revision": revision,
        "concept_sha256": manifest.get("concept_sha256"),
        "concept_provenance_sha256": roles["concept_provenance"][0]["sha256"],
        "specification_hash": spec_hash,
        "profile_id": manifest.get("profile_id"),
        "profile_qualified": f"{manifest.get('profile_id')}@{manifest.get('profile_version')}",
        "profile_version": manifest.get("profile_version"),
    }
    concept_mismatches = [
        key
        for key, value in concept_expected.items()
        if (
            not _v07_same_int(concept_context.get(key), value)
            if key in {"revision", "profile_version"}
            else concept_context.get(key) != value
        )
    ]
    concept_mismatches.extend(
        key
        for key, value in {
            "asset-specification": roles["specification"][0]["sha256"],
            "asset-concept": manifest.get("concept_sha256"),
            "asset-concept-provenance": roles["concept_provenance"][0]["sha256"],
            "asset-profile-v07": roles["bound_profile"][0]["sha256"],
        }.items()
        if concept_artifacts.get(key) != value
    )
    paid_expected = {
        "asset_id": asset_id,
        "provider": snapshot.get("provider"),
        "specification_hash": spec_hash,
        "profile_document_hash": profile_hash,
        "profile_id": manifest.get("profile_id"),
        "graph_version": "0.7.0",
        "source_kind": "provider_generated",
        "category": "character",
        "collider_policy": "capsule",
    }
    paid_mismatches = [
        key for key, value in paid_expected.items() if paid_parameters.get(key) != value
    ]
    if not _v07_same_int(paid_parameters.get("revision_number"), revision):
        paid_mismatches.append("revision_number")
    if not _v07_same_int(paid_parameters.get("profile_version"), manifest.get("profile_version")):
        paid_mismatches.append("profile_version")
    paid_mismatches.extend(
        key
        for key, value in {
            "workflow_id": workflow_id,
            "task_type": "asset_paid_generation",
        }.items()
        if paid_scope.get(key) != value
    )
    paid_scope_provider = paid_scope.get("provider")
    if paid_scope_provider is not None:
        paid_mismatches.append("scope.provider")
    if (
        not isinstance(snapshot.get("provider"), str)
        or not snapshot.get("provider")
        or snapshot.get("operation") != intent.get("operation")
    ):
        paid_mismatches.append("snapshot operation/provider alias")
    if concept_mismatches or paid_mismatches:
        raise ValueError(
            "Provider-character concept or paid approval is stale or weakly bound: "
            f"concept={sorted(concept_mismatches)}, paid={sorted(paid_mismatches)}"
        )
    paid_approved_hashes = {pair[1] for pair in paid_scope_artifacts}
    required_paid_hashes = {
        roles["specification"][0]["sha256"],
        manifest.get("concept_sha256"),
        roles["concept_provenance"][0]["sha256"],
        roles["bound_profile"][0]["sha256"],
        roles["paid_request_snapshot"][0]["sha256"],
        roles["production_readiness_report"][0]["sha256"],
    }
    missing_paid_hashes = required_paid_hashes - paid_approved_hashes
    if missing_paid_hashes:
        raise ValueError(
            "Provider-character paid approval did not include current prerequisite artifacts: "
            + ",".join(sorted(str(value) for value in missing_paid_hashes))
        )
    processed_hash = roles["processed_glb"][0]["sha256"]
    final_inputs = obj("final_approval").get("inputs", {})
    final_context = final_inputs.get("scope", {}).get("handler_context", {})
    final_artifacts = final_context.get("artifacts") if isinstance(final_context, dict) else None
    expected_final_artifacts = {
        "asset-concept": manifest.get("concept_sha256"),
        "asset-processed-glb": processed_hash,
        "asset-validation-report": roles["validation"][0]["sha256"],
        "asset-runtime-observation": roles["runtime_observation"][0]["sha256"],
        "asset-runtime-capture": sorted(manifest.get("capture_sha256", {}).values())[0],
        "asset-profile-v07": roles["bound_profile"][0]["sha256"],
        "asset-runtime-request": roles["runtime_request"][0]["sha256"],
        "asset-runtime-harness": roles["runtime_harness"][0]["sha256"],
    }
    if (
        not isinstance(final_context, dict)
        or final_context.get("workflow_id") != workflow_id
        or not _v07_same_int(final_context.get("revision"), revision)
        or final_context.get("specification_hash") != spec_hash
        or final_context.get("profile_id") != manifest.get("profile_id")
        or not _v07_same_int(final_context.get("profile_version"), manifest.get("profile_version"))
        or final_context.get("profile_qualified")
        != f"{manifest.get('profile_id')}@{manifest.get('profile_version')}"
        or final_context.get("runtime_request_digest") != manifest.get("runtime_request_digest")
        or final_context.get("graph_version") != "0.7.0"
        or final_context.get("all_artifacts_verified") is not True
        or final_context.get("runtime_capture_hashes")
        != sorted(manifest.get("capture_sha256", {}).values())
        or not isinstance(final_artifacts, dict)
        or any(final_artifacts.get(key) != value for key, value in expected_final_artifacts.items())
    ):
        raise ValueError(
            "Provider-character final approval is bound to stale runtime/output evidence"
        )
    if (
        manifest.get("final_review", {}).get("decision") != "APPROVED"
        or manifest.get("final_review", {}).get("fingerprint")
        != receipts["final_approval"]["fingerprint"]
        or manifest.get("human_reviews")
        != {
            "concept_review": receipts["concept_approval"]["operation_hash"],
            "paid_generation": receipts["paid_approval"]["operation_hash"],
            "final_visual_review": receipts["final_approval"]["operation_hash"],
        }
    ):
        raise ValueError("Provider-character manifest does not bind all human operation hashes")
    pinned_roles = {
        "concept_provenance_sha256": "concept_provenance",
        "concept_approval_sha256": "concept_approval",
        "paid_approval_sha256": "paid_approval",
        "paid_request_snapshot_sha256": "paid_request_snapshot",
        "production_readiness_report_sha256": "production_readiness_report",
        "provider_operation_sha256": "provider_operation",
        "cost_record_sha256": "cost_record",
    }
    if (
        manifest.get("raw_glb_sha256") != raw_hash
        or manifest.get("processed_glb_sha256") != processed_hash
        or any(manifest.get(key) != roles[role][0]["sha256"] for key, role in pinned_roles.items())
    ):
        raise ValueError("Provider-character manifest raw or processed pin mismatch")
    for role, key in (
        ("processing_report", "processing_report_sha256"),
        ("validation", "validation_sha256"),
        ("runtime_observation", "runtime_observation_sha256"),
        ("runtime_harness", "runtime_harness_sha256"),
        ("processing_script", "processing_script_sha256"),
    ):
        if manifest.get(key) != roles[role][0]["sha256"]:
            raise ValueError(f"Provider-character current artifact hash mismatch: {role}")
    runtime_request = obj("runtime_request")
    observation = obj("runtime_observation")
    unsigned_runtime_request = dict(runtime_request)
    runtime_request_digest = unsigned_runtime_request.pop("request_digest", None)
    calculated_runtime_request_digest = _sha256(
        json.dumps(
            unsigned_runtime_request, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    )
    if (
        runtime_request.get("raw_glb_sha256") != raw_hash
        or runtime_request.get("processed_glb_sha256") != processed_hash
        or runtime_request.get("harness_sha256") != roles["runtime_harness"][0]["sha256"]
        or runtime_request.get("specification_sha256") != spec_hash
        or runtime_request.get("profile_sha256") != profile_hash
        or runtime_request.get("workflow_id") != workflow_id
        or not _v07_same_int(runtime_request.get("revision"), revision)
        or runtime_request.get("execution_id") != manifest.get("runtime_execution_id")
        or not _v07_same_int(
            runtime_request.get("attempt_number"), manifest.get("runtime_attempt_number")
        )
        or runtime_request_digest != manifest.get("runtime_request_digest")
        or calculated_runtime_request_digest != manifest.get("runtime_request_digest")
        or observation.get("execution_id") != manifest.get("runtime_execution_id")
        or not _v07_same_int(
            observation.get("attempt_number"), manifest.get("runtime_attempt_number")
        )
        or observation.get("specification_sha256") != spec_hash
        or observation.get("profile_sha256") != profile_hash
        or observation.get("harness_sha256") != roles["runtime_harness"][0]["sha256"]
        or observation.get("request_digest") != manifest.get("runtime_request_digest")
        or observation.get("status") != "PASS"
    ):
        raise ValueError(
            "Provider-character Godot request and observation are not current and bound"
        )

    # Validate source and processed payloads, single-mesh policy, aggregate budget,
    # material references, embedded texture dimensions, and capsule metadata.
    raw_doc, raw_bin, raw_nodes, raw_parents, raw_world = _v07_glb(
        payload("provider_generated_glb"), allow_unnamed_duplicate_nodes=True
    )
    proc_doc, proc_bin, proc_nodes, proc_parents, proc_world = _v07_glb(payload("processed_glb"))
    raw_mesh_node_names: set[str] = set()
    for label, doc, _nodes, _parents in (
        ("raw", raw_doc, raw_nodes, raw_parents),
        ("processed", proc_doc, proc_nodes, proc_parents),
    ):
        meshes = doc.get("meshes", [])
        if (
            not isinstance(meshes, list)
            or not meshes
            or len(meshes) > (10_000 if label == "raw" else 2)
            or any(not isinstance(mesh, dict) for mesh in meshes)
        ):
            raise ValueError(f"Provider-character {label} mesh table is empty or exceeds its bound")
        mesh_nodes = [node for node in doc["nodes"] if isinstance(node, dict) and "mesh" in node]
        if (
            not mesh_nodes
            or any(type(node.get("mesh")) is not int for node in mesh_nodes)
            or {node["mesh"] for node in mesh_nodes} != set(range(len(meshes)))
            or (label == "processed" and len(mesh_nodes) != len(meshes))
        ):
            raise ValueError(f"Provider-character {label} mesh-node inventory is incomplete")
        mesh_node_indices = [
            index
            for index, node in enumerate(doc["nodes"])
            if isinstance(node, dict) and "mesh" in node
        ]
        _v07_bound_mesh_vertex_work(doc, mesh_node_indices)
        forbidden_names = [
            node.get("name", "")
            for node in doc["nodes"]
            if isinstance(node.get("name", ""), str)
            and re.search(r"(^|_)(COL|PART|SOCKET)(_|$)", node.get("name", ""))
        ]
        if forbidden_names or doc.get("skins") or doc.get("animations"):
            raise ValueError(f"Provider-character {label} contains assembly or rigging nodes")
        if any(
            node.get("name", "").startswith(("PART_", "SOCKET_", "COL_"))
            for node in doc["nodes"]
            if isinstance(node, dict)
        ):
            raise ValueError(f"Provider-character {label} includes assembly semantics")
        if label == "raw":
            raw_mesh_node_names = {
                name for name, node_index in raw_nodes.items() if "mesh" in doc["nodes"][node_index]
            }
        elif (
            {node.get("name") for node in mesh_nodes}
            != {
                node.get("name")
                for node in mesh_nodes
                if node.get("name", "").endswith(("LOD0", "LOD1"))
            }
            or len(mesh_nodes) != 2
            or {"LOD0" if node.get("name", "").endswith("LOD0") else "LOD1" for node in mesh_nodes}
            != {"LOD0", "LOD1"}
        ):
            raise ValueError(
                "Processed provider character must contain exactly one LOD0 and one LOD1 node"
            )
    budgets = spec.get("geometry_budget", {})
    max_tri0 = budgets.get("max_triangles_lod0")
    max_tri1 = budgets.get("max_triangles_lod1")
    max_materials = spec.get("material_budget", {}).get("max_materials")
    max_texture = spec.get("texture_budget", {}).get("max_dimension")
    caps = profile.get("processing", {})
    if (
        type(max_tri0) is not int
        or max_tri0 <= 0
        or type(max_tri1) is not int
        or max_tri1 <= 0
        or max_tri1 >= max_tri0
        or type(max_materials) is not int
        or max_materials <= 0
        or type(max_texture) is not int
        or max_texture <= 0
        or type(caps.get("max_triangles_lod0")) is not int
        or type(caps.get("max_materials")) is not int
        or type(caps.get("max_texture_dimension")) is not int
        or max_tri0 > caps["max_triangles_lod0"]
        or max_materials > caps["max_materials"]
        or max_texture > caps["max_texture_dimension"]
    ):
        raise ValueError("Provider-character specification budgets are invalid")
    profile_godot = profile.get("godot", {})
    if (
        not isinstance(profile_godot, dict)
        or profile_godot.get("body_kind") != "static_body"
        or profile_godot.get("require_ray_hit") is not True
        or profile_godot.get("require_area") is not False
        or caps.get("rig_forbidden") is not True
        or caps.get("animation_forbidden") is not True
        or spec.get("origin_policy") not in caps.get("allowed_origin_policies", [])
    ):
        raise ValueError(
            "Provider-character profile runtime or processing contract is not static/unrigged"
        )
    dims, collider = spec.get("dimensions", {}), spec.get("collider", {}).get("capsule", {})
    radius, capsule_height = collider.get("radius_m"), collider.get("height_m")
    tolerance = caps.get("dimension_tolerance_m")
    if (
        not isinstance(dims, dict)
        or any(
            type(dims.get(key)) not in {int, float}
            or not math.isfinite(dims[key])
            or dims[key] <= 0
            for key in ("width_m", "depth_m", "height_m")
        )
        or type(radius) not in {int, float}
        or not math.isfinite(radius)
        or radius <= 0
        or type(capsule_height) not in {int, float}
        or not math.isfinite(capsule_height)
        or capsule_height <= 2 * radius
        or type(tolerance) not in {int, float}
        or not math.isfinite(tolerance)
        or tolerance <= 0
        or capsule_height > dims["height_m"] + tolerance
        or 2 * radius > max(dims["width_m"], dims["depth_m"]) + tolerance
    ):
        raise ValueError("Provider-character spec dimensions or capsule collider are inconsistent")
    processed_texture_max = 0
    raw_decoded_meshes: dict[int, list[list[tuple[tuple[float, float, float], ...]]]] = {}
    processed_decoded_meshes: dict[int, list[list[tuple[tuple[float, float, float], ...]]]] = {}
    raw_validated_attribute_accessors: set[int] = set()
    processed_validated_attribute_accessors: set[int] = set()
    for doc, binary, nodes, world, label in (
        (raw_doc, raw_bin, raw_nodes, raw_world, "raw"),
        (proc_doc, proc_bin, proc_nodes, proc_world, "processed"),
    ):
        lod_counts = {"LOD0": 0, "LOD1": 0}
        mesh_nodes = [node for node in doc["nodes"] if isinstance(node, dict) and "mesh" in node]
        seen_mesh_lods: set[str] = set()
        for node in mesh_nodes:
            name = node.get("name", "")
            if label == "raw":
                lod = "LOD0"
            else:
                if not (name.endswith("LOD0") or name.endswith("LOD1")):
                    raise ValueError("Processed character mesh nodes must use canonical LOD names")
                lod = "LOD1" if name.endswith("LOD1") else "LOD0"
            if label == "processed" and lod in seen_mesh_lods:
                raise ValueError(f"Provider-character {label} has duplicate {lod} geometry")
            seen_mesh_lods.add(lod)
            decoded_meshes = raw_decoded_meshes if label == "raw" else processed_decoded_meshes
            validated_attributes = (
                raw_validated_attribute_accessors
                if label == "raw"
                else processed_validated_attribute_accessors
            )
            primitives = _v07_positions(
                doc,
                binary,
                node["mesh"],
                decoded_meshes=decoded_meshes,
                validated_attribute_accessors=validated_attributes,
            )
            lod_counts[lod] += sum(len(triangles) for triangles in primitives)
        if (
            lod_counts["LOD0"] <= 0
            or lod_counts["LOD0"] > max_tri0
            or lod_counts["LOD1"] > max_tri1
        ):
            raise ValueError(f"Provider-character {label} aggregate LOD triangle budget exceeded")
        if label == "processed" and (
            "LOD1" not in seen_mesh_lods
            or caps.get("lod1_required") is True
            and "LOD1" not in seen_mesh_lods
        ):
            raise ValueError(
                "Processed character must contain its canonical LOD0 and LOD1 surfaces"
            )
        materials = doc.get("materials", [])
        if not isinstance(materials, list) or len(materials) > max_materials:
            raise ValueError(f"Provider-character {label} material budget exceeded")
        referenced = {
            primitive.get("material", 0)
            for mesh in doc.get("meshes", [])
            for primitive in mesh.get("primitives", [])
        }
        if any(
            type(index) is not int or index < 0 or index >= max(1, len(materials))
            for index in referenced
        ):
            raise ValueError(f"Provider-character {label} primitive material reference is invalid")
        images = doc.get("images", [])
        textures = doc.get("textures", [])
        buffer_views = doc.get("bufferViews", [])
        if (
            not isinstance(images, list)
            or not isinstance(textures, list)
            or not isinstance(buffer_views, list)
        ):
            raise ValueError("Provider-character image, texture, or buffer-view tables are invalid")
        max_seen_texture = 0
        for texture in textures:
            source_index = texture.get("source") if isinstance(texture, dict) else None
            if type(source_index) is not int or not 0 <= source_index < len(images):
                raise ValueError("Provider-character texture references an invalid embedded image")
        for material in materials:
            if not isinstance(material, dict):
                raise ValueError("Provider-character material row is malformed")
            slots = [
                material.get("normalTexture"),
                material.get("occlusionTexture"),
                material.get("emissiveTexture"),
            ]
            pbr = material.get("pbrMetallicRoughness", {})
            if isinstance(pbr, dict):
                slots.extend((pbr.get("baseColorTexture"), pbr.get("metallicRoughnessTexture")))
            for slot in slots:
                if slot is not None and (
                    not isinstance(slot, dict)
                    or type(slot.get("index")) is not int
                    or not 0 <= slot["index"] < len(textures)
                ):
                    raise ValueError("Provider-character material texture slot is invalid")
        for image in images:
            if (
                not isinstance(image, dict)
                or "bufferView" not in image
                or image.get("mimeType") not in {"image/png", "image/jpeg"}
            ):
                raise ValueError(
                    "Provider-character images must be embedded PNG or JPEG bufferViews"
                )
            view_index = image["bufferView"]
            if (
                type(view_index) is not int
                or not 0 <= view_index < len(buffer_views)
                or not isinstance(buffer_views[view_index], dict)
            ):
                raise ValueError("Provider-character embedded image view is invalid")
            view = buffer_views[view_index]
            off, length = view.get("byteOffset", 0), view.get("byteLength")
            if (
                type(off) is not int
                or type(length) is not int
                or off < 0
                or length <= 0
                or off + length > len(binary)
            ):
                raise ValueError("Provider-character embedded image bounds are invalid")
            blob = binary[off : off + length]
            dimensions = (
                _png_dimensions(blob)
                if image["mimeType"] == "image/png"
                else _jpeg_dimensions(blob)
            )
            max_seen_texture = max(max_seen_texture, *dimensions)
        if label == "processed":
            processed_texture_max = max_seen_texture
        if max_seen_texture > max_texture:
            raise ValueError(
                f"Provider-character {label} embedded texture exceeds its dimension budget"
            )
        if label == "processed":
            _v07_enforce_core_budgets(
                doc,
                binary,
                nodes,
                world,
                spec,
                profile,
                decoded_meshes=processed_decoded_meshes,
                validated_attribute_accessors=processed_validated_attribute_accessors,
            )
    raw_lod0 = _v07_tris(
        raw_doc,
        raw_bin,
        raw_nodes,
        raw_world,
        "",
        selected_names=raw_mesh_node_names,
        decoded_meshes=raw_decoded_meshes,
        validated_attribute_accessors=raw_validated_attribute_accessors,
    )
    processed_lod0 = _v07_tris(
        proc_doc,
        proc_bin,
        proc_nodes,
        proc_world,
        "LOD0",
        decoded_meshes=processed_decoded_meshes,
        validated_attribute_accessors=processed_validated_attribute_accessors,
    )
    if not raw_lod0 or len(processed_lod0) != 1:
        raise ValueError(
            "Provider-character raw and processed inventories lack complete visual LOD0 geometry"
        )
    source_triangles = [triangle for values in raw_lod0.values() for triangle in values]
    processed_triangles = [triangle for values in processed_lod0.values() for triangle in values]
    source_min, source_max = _v07_bounds(source_triangles)
    processed_min, processed_max = _v07_bounds(processed_triangles)
    source_size = [source_max[i] - source_min[i] for i in range(3)]
    processed_size = [processed_max[i] - processed_min[i] for i in range(3)]
    target_height = dims["height_m"]
    if source_size[1] <= 0:
        raise ValueError("Provider-character source mesh has zero height")
    scale = target_height / source_size[1]
    tolerance = max(1e-5, target_height * 1e-5)
    if (
        not math.isfinite(scale)
        or scale <= 0
        or any(abs(processed_size[i] - source_size[i] * scale) > tolerance for i in range(3))
        or any(
            abs(processed_size[i] - dims[dim_key]) > max(caps["dimension_tolerance_m"], tolerance)
            for i, dim_key in enumerate(("width_m", "height_m", "depth_m"))
        )
    ):
        raise ValueError(
            "Provider-character raw-to-processed transform is not a bounded uniform height fit"
        )
    if (
        abs((processed_min[0] + processed_max[0]) / 2) > caps["dimension_tolerance_m"]
        or abs((processed_min[2] + processed_max[2]) / 2) > caps["dimension_tolerance_m"]
        or (
            spec.get("origin_policy") == "bottom_center"
            and abs(processed_min[1]) > caps["dimension_tolerance_m"]
        )
        or (
            spec.get("origin_policy") == "center"
            and abs((processed_min[1] + processed_max[1]) / 2) > caps["dimension_tolerance_m"]
        )
    ):
        raise ValueError("Provider-character processed LOD0 origin differs from specification")
    if caps.get("lod1_required") is True or spec.get("lod_policy") == "lod0_lod1":
        processed_lod1 = _v07_tris(
            proc_doc,
            proc_bin,
            proc_nodes,
            proc_world,
            "LOD1",
            decoded_meshes=processed_decoded_meshes,
            validated_attribute_accessors=processed_validated_attribute_accessors,
        )
        if (
            set(processed_lod1) != {name for name in proc_nodes if name.endswith("LOD1")}
            or len(processed_lod1) != 1
        ):
            raise ValueError("Provider-character processed LOD1 inventory is incomplete")
        lod1_min, lod1_max = _v07_bounds(next(iter(processed_lod1.values())))
        if any(
            abs(lod1_min[i] - processed_min[i]) > caps["dimension_tolerance_m"]
            or abs(lod1_max[i] - processed_max[i]) > caps["dimension_tolerance_m"]
            for i in range(3)
        ):
            raise ValueError("Provider-character decoded LOD1 bounds differ from LOD0")
    if spec.get("origin_policy") == "bottom_center":
        translation = tuple(
            (processed_min[i] + processed_max[i]) / 2 - (source_min[i] + source_max[i]) / 2 * scale
            if i != 1
            else processed_min[i] - source_min[i] * scale
            for i in range(3)
        )
    else:
        translation = tuple(
            (processed_min[i] + processed_max[i]) / 2 - (source_min[i] + source_max[i]) / 2 * scale
            for i in range(3)
        )
    fitted_source = [
        tuple(
            tuple(point[axis] * scale + translation[axis] for axis in range(3))
            for point in triangle
        )
        for triangle in source_triangles
    ]
    if not _v07_compare_triangles(fitted_source, processed_triangles, tolerance):
        raise ValueError(
            "Provider-character raw-to-processed oriented triangle geometry was not uniformly preserved"
        )
    processing_report = obj("processing_report")
    raw_metrics = processing_report.get("raw_metrics")
    processed_metrics = processing_report.get("processed_metrics")
    transform = processing_report.get("transform")
    if (
        processing_report.get("contract_version") != "character-processing-0.7.0"
        or processing_report.get("status") != "SUCCESS"
        or processing_report.get("exit_code") != 0
        or processing_report.get("asset_id") != asset_id
        or processing_report.get("source_sha256") != raw_hash
        or processing_report.get("output_sha256") != processed_hash
        or processing_report.get("spec_sha256") != spec_hash
        or processing_report.get("profile_sha256") != profile_hash
        or processing_report.get("profile_id") != manifest.get("profile_id")
        or not _v07_same_int(
            processing_report.get("profile_version"), manifest.get("profile_version")
        )
        or processing_report.get("script_sha256") != roles["processing_script"][0]["sha256"]
        or not isinstance(raw_metrics, dict)
        or not isinstance(processed_metrics, dict)
        or not isinstance(transform, dict)
    ):
        raise ValueError(
            "Provider-character Blender processing report is stale or contradicts current pins"
        )
    raw_dimensions_blender = (source_size[0], source_size[2], source_size[1])
    raw_min_blender = (source_min[0], source_min[2], source_min[1])
    raw_max_blender = (source_max[0], source_max[2], source_max[1])
    if (
        not _v07_vec_close(
            raw_metrics.get("dimensions"), raw_dimensions_blender, caps["dimension_tolerance_m"]
        )
        or not _v07_vec_close(
            raw_metrics.get("bounds_min"), raw_min_blender, caps["dimension_tolerance_m"]
        )
        or not _v07_vec_close(
            raw_metrics.get("bounds_max"), raw_max_blender, caps["dimension_tolerance_m"]
        )
        or not _v07_num_close(raw_metrics.get("lod0_triangles"), len(source_triangles), 0)
        or not _v07_num_close(raw_metrics.get("mesh_objects"), len(raw_mesh_node_names), 0)
        or not _v07_num_close(raw_metrics.get("materials"), len(raw_doc.get("materials", [])), 0)
    ):
        raise ValueError(
            "Provider-character Blender raw metrics differ from decoded provider geometry"
        )
    lod0_triangle_count = sum(len(values) for values in processed_lod0.values())
    processed_lod1_for_report = _v07_tris(
        proc_doc,
        proc_bin,
        proc_nodes,
        proc_world,
        "LOD1",
        decoded_meshes=processed_decoded_meshes,
        validated_attribute_accessors=processed_validated_attribute_accessors,
    )
    lod1_triangle_count = sum(len(values) for values in processed_lod1_for_report.values())
    if (
        not _v07_vec_close(
            processed_metrics.get("dimensions"), processed_size, caps["dimension_tolerance_m"]
        )
        or not _v07_vec_close(
            processed_metrics.get("bounds_min"), processed_min, caps["dimension_tolerance_m"]
        )
        or not _v07_vec_close(
            processed_metrics.get("bounds_max"), processed_max, caps["dimension_tolerance_m"]
        )
        or not _v07_num_close(processed_metrics.get("lod0_triangles"), lod0_triangle_count, 0)
        or not _v07_num_close(processed_metrics.get("lod1_triangles"), lod1_triangle_count, 0)
        or not _v07_num_close(
            processed_metrics.get("materials"), len(proc_doc.get("materials", [])), 0
        )
        or not _v07_num_close(
            processed_metrics.get("texture_max_dimension"), processed_texture_max, 0
        )
        or processed_metrics.get("mesh_nodes")
        != sorted(name for name in proc_nodes if name.endswith(("LOD0", "LOD1")))
        or not _v07_num_close(
            processed_metrics.get("lod1_actual_triangle_ratio"),
            lod1_triangle_count / max(1, lod0_triangle_count),
            1e-4,
        )
    ):
        raise ValueError("Provider-character Blender processed metrics differ from decoded GLB")
    report_capsule = processing_report.get("runtime_collider")
    if (
        not isinstance(report_capsule, dict)
        or report_capsule.get("policy") != "capsule"
        or not _v07_num_close(report_capsule.get("radius_m"), radius, 1e-5)
        or not _v07_num_close(report_capsule.get("height_m"), capsule_height, 1e-5)
        or transform.get("origin_policy") != spec.get("origin_policy")
        or transform.get("scale_mode") != "uniform_height_fit"
        or not _v07_num_close(transform.get("uniform_scale"), scale, 1e-5)
        or not _v07_vec_close(transform.get("translation_gltf_m"), translation, 1e-5)
    ):
        raise ValueError(
            "Provider-character Blender transform or capsule report differs from decoded geometry"
        )
    collider = spec.get("collider", {}).get("capsule", {})
    if not collider or not isinstance(collider, dict):
        raise ValueError("Provider-character capsule dimensions are missing")
    validation = obj("validation")
    validation_findings = validation.get("findings")
    required_validation_rules = {
        "glb.parse",
        "geometry.nonempty",
        "nodes.unique",
        "materials.budget",
        "textures.dimension",
        "geometry.lod0_budget",
        "geometry.lod1_budget",
        "collider.capsule.shape",
        "collider.capsule.fit",
        "mesh.single",
        "dimensions.bounds",
        "origin.policy",
        "lod1.present",
        "lod1.bounds",
        "mesh.set",
        "orientation.identity",
    }
    observed_validation_rules = (
        {finding.get("rule_id") for finding in validation_findings if isinstance(finding, dict)}
        if isinstance(validation_findings, list)
        else set()
    )
    if (
        validation.get("schema_version") != "asset-validation-report-0.7.0"
        or validation.get("passed") is not True
        or not isinstance(validation_findings, list)
        or any(
            not isinstance(finding, dict) or finding.get("passed") is not True
            for finding in validation_findings
        )
        or not required_validation_rules <= observed_validation_rules
    ):
        raise ValueError("Provider-character semantic validation did not pass")
    # Match the standalone character adapter's dictionary-shaped observation.
    lod_rows = observation.get("lods")
    collider_row = observation.get("collider")
    if (
        observation.get("schema_version") != "character-runtime-observation-0.7.0"
        or observation.get("workflow_id") != workflow_id
        or observation.get("asset_id") != asset_id
        or not _v07_same_int(observation.get("revision"), revision)
        or observation.get("execution_id") != manifest.get("runtime_execution_id")
        or not _v07_same_int(
            observation.get("attempt_number"), manifest.get("runtime_attempt_number")
        )
        or observation.get("raw_glb_sha256") != raw_hash
        or observation.get("processed_glb_sha256") != processed_hash
        or observation.get("harness_sha256") != roles["runtime_harness"][0]["sha256"]
        or observation.get("specification_sha256") != spec_hash
        or observation.get("profile_sha256") != profile_hash
        or observation.get("request_digest") != manifest.get("runtime_request_digest")
        or observation.get("status") != "PASS"
        or observation.get("errors") != []
        or not isinstance(lod_rows, dict)
        or not isinstance(collider_row, dict)
        or collider_row.get("body_kind") != "StaticBody3D"
        or collider_row.get("shape_class") != "CapsuleShape3D"
        or collider_row.get("physics_ray_hit") is not True
        or not isinstance(observation.get("view_framing"), dict)
        or not isinstance(observation.get("captures"), dict)
    ):
        raise ValueError(
            "Provider-character Godot capsule, static-body, LOD, or request result is incomplete"
        )
    processed_nodes = [
        node for node in proc_doc["nodes"] if isinstance(node, dict) and "mesh" in node
    ]
    expected_lod_names = {node["name"] for node in processed_nodes}
    if set(lod_rows) != expected_lod_names:
        raise ValueError("Provider-character Godot LOD inventory differs from processed GLB nodes")
    for node in processed_nodes:
        name = node["name"]
        lod = lod_rows[name]
        mesh = proc_doc["meshes"][node["mesh"]]
        position, basis, node_scale = _v07_parts(node)
        expected_visible = name.endswith("LOD0")
        if (
            not isinstance(lod, dict)
            or set(lod)
            != {
                "mesh_present",
                "surface_count",
                "visible",
                "local_position",
                "local_basis",
                "local_scale",
            }
            or lod.get("mesh_present") is not True
            or type(lod.get("surface_count")) is not int
            or lod.get("surface_count") != len(mesh.get("primitives", []))
            or lod.get("visible") is not expected_visible
            or not _v07_vec_close(lod.get("local_position"), position, 1e-4)
            or not _v07_vec_close(lod.get("local_scale"), node_scale, 1e-4)
            or not _v07_matrix3_close(lod.get("local_basis"), basis, 1e-4)
        ):
            raise ValueError(
                "Provider-character Godot LOD transform, surface count, or visibility is invalid"
            )
    expected_capsule = spec["collider"]["capsule"]
    expected_center_y = (
        expected_capsule["height_m"] / 2 if spec.get("origin_policy") == "bottom_center" else 0.0
    )
    if (
        set(collider_row)
        != {"body_kind", "shape_class", "radius_m", "height_m", "center_y_m", "physics_ray_hit"}
        or not _v07_num_close(collider_row.get("radius_m"), expected_capsule.get("radius_m"), 1e-4)
        or not _v07_num_close(collider_row.get("height_m"), expected_capsule.get("height_m"), 1e-4)
        or not _v07_num_close(collider_row.get("center_y_m"), expected_center_y, 1e-4)
    ):
        raise ValueError(
            "Provider-character Godot capsule dimensions or ray probe differ from specification"
        )
    framing = observation.get("view_framing")
    captures_observed = observation.get("captures")
    if (
        not isinstance(framing, dict)
        or set(framing) != set(views)
        or not isinstance(captures_observed, dict)
        or set(captures_observed) != set(views)
    ):
        raise ValueError("Provider-character Godot review-view framing evidence is incomplete")
    capture_hashes = {row["view"]: row["sha256"] for row in captures}
    framing_policy = profile.get("framing", {})
    camera_views = {
        "front": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0), "-Z"),
        "rear": ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), "+Z"),
        "left": ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), "-X"),
        "right": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), "+X"),
        "side": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), "+X"),
        "three_quarter": ((1.0, 0.65, -1.0), (0.0, 1.0, 0.0), "+X-Z"),
        "three_quarter_front": ((1.0, 0.65, -1.0), (0.0, 1.0, 0.0), "+X-Z"),
        "three_quarter_rear": ((1.0, 0.65, 1.0), (0.0, 1.0, 0.0), "+X+Z"),
        "top": ((0.0, 1.0, 0.0), (0.0, 0.0, -1.0), "+Y"),
    }

    def capture_payload(row: dict[str, Any]) -> bytes:
        path = root.joinpath(*PurePosixPath(row["path"]).parts)
        return read_evidence_file(
            path, limit=25_000_000, expected_size=row["size"], expected_digest=row["sha256"]
        )

    for view in views:
        result = framing[view]
        direction, requested_up, axis = camera_views[view]
        direction_length = math.sqrt(sum(value * value for value in direction))
        camera_forward = tuple(-value / direction_length for value in direction)
        projection = sum(requested_up[index] * camera_forward[index] for index in range(3))
        projected_up = tuple(
            requested_up[index] - projection * camera_forward[index] for index in range(3)
        )
        up_length = math.sqrt(sum(value * value for value in projected_up))
        expected_up = tuple(value / up_length for value in projected_up)
        min_fraction = framing_policy.get("min_screen_fraction")
        max_fraction = framing_policy.get("max_screen_fraction")
        ratio = result.get("height_ratio") if isinstance(result, dict) else None
        if (
            type(ratio) not in {int, float}
            or isinstance(ratio, bool)
            or type(min_fraction) not in {int, float}
            or isinstance(min_fraction, bool)
            or type(max_fraction) not in {int, float}
            or isinstance(max_fraction, bool)
        ):
            raise ValueError(f"Provider-character Godot framing ratio is malformed for {view}")
        ratio_value = float(cast(int | float, ratio))
        min_fraction_value = float(cast(int | float, min_fraction))
        max_fraction_value = float(cast(int | float, max_fraction))
        capture = captures_observed[view]
        capture_row = next(row for row in captures if row["view"] == view)
        capture_bytes = capture_payload(capture_row)
        dimensions = _png_dimensions(capture_bytes)
        if (
            not isinstance(result, dict)
            or result.get("ok") is not True
            or result.get("inside_viewport") is not True
            or result.get("margin_ok") is not True
            or result.get("view_axis") != axis
            or not _v07_vec_close(result.get("camera_direction"), camera_forward, 1e-4)
            or not _v07_vec_close(result.get("camera_up"), expected_up, 1e-4)
            or not math.isfinite(ratio_value)
            or not math.isfinite(min_fraction_value)
            or not math.isfinite(max_fraction_value)
            or not min_fraction_value <= ratio_value <= max_fraction_value
            or not isinstance(capture, dict)
            or set(capture) != {"sha256"}
            or capture.get("sha256") != capture_hashes[view]
            or dimensions != (1280, 720)
        ):
            raise ValueError(f"Provider-character Godot framing failed for {view}")
    receipt = obj("production_receipt")
    if (
        receipt.get("status") != "PASS"
        or receipt.get("generation_mode") != "provider_generated_character"
        or receipt.get("paid") is not True
        or receipt.get("workflow_id") != workflow_id
        or not _v07_same_int(receipt.get("revision"), revision)
    ):
        raise ValueError("Provider-character production receipt is not a current PASS")
    receipt_rows = {
        f"{item['role']}:{item.get('view', '')}": item["sha256"]
        for item in files
        if item["role"] not in {"review_html", "production_receipt"}
    }
    expected_receipt_binding = {
        key: value
        for key, value in manifest.items()
        if key not in {"product_ready", "files", "final_review", "human_reviews"}
    }
    if (
        receipt.get("role_sha256") != receipt_rows
        or receipt.get("binding") != expected_receipt_binding
        or receipt.get("human_reviews")
        != {
            "concept_review": {
                "operation_hash": receipts["concept_approval"]["operation_hash"],
                "status": "APPROVED",
            },
            "paid_generation": {
                "operation_hash": receipts["paid_approval"]["operation_hash"],
                "status": "APPROVED",
            },
            "final_visual_review": {
                "operation_hash": receipts["final_approval"]["operation_hash"],
                "status": "APPROVED",
            },
        }
        or manifest.get("human_reviews")
        != {
            "concept_review": receipts["concept_approval"]["operation_hash"],
            "paid_generation": receipts["paid_approval"]["operation_hash"],
            "final_visual_review": receipts["final_approval"]["operation_hash"],
        }
    ):
        raise ValueError(
            "Provider-character production receipt does not bind current role hashes and reviews"
        )
    return {
        "status": "PASS",
        "schema_version": "asset-evidence-0.7.0",
        "workflow_id": workflow_id,
        "revision": revision,
        "verified_files": len(files),
        "product_ready": True,
        "paid": True,
        "human_identity_authenticated": False,
        "provider_execution_authenticated": False,
        "runtime_origin_authenticated": False,
        "capture_origin_authenticated": False,
        "currentness": "as_of_export_snapshot",
        "limitation": "The bundle establishes internal consistency only; human identity, provider execution, runtime execution, capture origin, and post-export changes require external trust anchors.",
    }


def verify_bundle(bundle: Path) -> dict[str, Any]:
    if _linked(bundle):
        raise ValueError("bundle root is a symlink or junction")
    root = bundle.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("bundle root is not a directory")
    if _linked(root / "manifest.json"):
        raise ValueError("bundle manifest is a symlink or junction")
    manifest = _object(root / "manifest.json")
    schema = manifest.get("schema_version")
    if schema == "asset-evidence-0.7.0":
        return _verify_v07_bundle(root, manifest)
    if schema == "asset-evidence-0.4.0":
        required_angles = set(_ANGLES)
        extra_single_roles: set[str] = set()
    elif schema in ("asset-evidence-0.5.0", "asset-evidence-0.6.0"):
        views = manifest.get("review_views")
        allowed_views = {
            "front",
            "rear",
            "left",
            "right",
            "side",
            "three_quarter",
            "three_quarter_front",
            "three_quarter_rear",
            "top",
        }
        if (
            not isinstance(views, list)
            or not views
            or any(not isinstance(view, str) for view in views)
            or len(views) != len(set(views))
            or any(view not in allowed_views for view in views)
        ):
            raise ValueError("0.5.0 bundle review_views are missing or unsupported")
        required_angles = set(views)
        extra_single_roles = {"production_receipt"}
        if schema == "asset-evidence-0.6.0":
            extra_single_roles |= {"paid_request_snapshot", "production_readiness_report"}
    else:
        raise ValueError("unsupported evidence manifest schema")
    workflow_id, revision = manifest.get("workflow_id"), manifest.get("revision")
    if (
        not isinstance(workflow_id, str)
        or not workflow_id
        or type(revision) is not int
        or revision < 1
    ):
        raise ValueError("manifest must bind workflow_id and positive revision")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("manifest files must be an array")
    roles: dict[str, list[dict[str, Any]]] = {}
    listed: set[str] = set()
    total_size = 0
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("file entry must be an object")
        rel = _path(item.get("path"))
        role = item.get("role")
        allowed_roles = _SINGLE_ROLES | extra_single_roles | {"runtime_capture"} | _OPTIONAL_ROLES
        if rel in _EXCLUDED or rel in listed or role not in allowed_roles:
            raise ValueError(f"duplicate, excluded, or unknown bundle entry: {rel}")
        digest, size = item.get("sha256"), item.get("size")
        if (
            not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
            or type(size) is not int
            or size < 0
        ):
            raise ValueError(f"invalid hash or size: {rel}")
        total_size += size
        if size > _MAX_FILE_BYTES or total_size > _MAX_BUNDLE_BYTES:
            raise ValueError("bundle file or total payload exceeds size limit")
        target = root.joinpath(*PurePosixPath(rel).parts)
        cursor = target
        while cursor != root:
            if _linked(cursor):
                raise ValueError(f"linked bundle path: {rel}")
            cursor = cursor.parent
        if not target.is_file() or not target.resolve(strict=True).is_relative_to(root):
            raise ValueError(f"bundle entry missing or escapes: {rel}")
        if target.stat().st_size > _MAX_FILE_BYTES:
            raise ValueError(f"bundle file exceeds size limit: {rel}")
        with target.open("rb") as stream:
            raw = stream.read(_MAX_FILE_BYTES + 1)
        if len(raw) != size or _sha256(raw) != digest:
            raise ValueError(f"bundle size or SHA-256 mismatch: {rel}")
        listed.add(rel)
        roles.setdefault(role, []).append(item)
    for role in _SINGLE_ROLES | extra_single_roles:
        if len(roles.get(role, [])) != 1:
            raise ValueError(f"bundle requires exactly one {role} entry")
    captures = roles.get("runtime_capture", [])
    if (
        len(captures) != len(required_angles)
        or {item.get("angle") for item in captures} != required_angles
    ):
        raise ValueError("bundle captures do not match the required review views")

    # Every physical file belongs to the manifest, except the manifest and this optional verifier.
    on_disk: set[str] = set()
    for path in root.rglob("*"):
        if _linked(path):
            raise ValueError(f"bundle contains a linked path: {path}")
        if path.is_file():
            on_disk.add(path.relative_to(root).as_posix())
    if on_disk - _EXCLUDED != listed:
        raise ValueError("bundle has unlisted files or missing manifest entries")

    def one(role: str) -> dict[str, Any]:
        return roles[role][0]

    if one("review_html")["path"] != "index.html":
        raise ValueError("review page must be root index.html")
    parser = _References()
    parser.feed((root / "index.html").read_text(encoding="utf-8"))
    if not parser.paths <= listed:
        raise ValueError("review HTML references an unmanifested file")
    for role in _LINKED_ROLES | {"runtime_capture"} | (set(roles) & _OPTIONAL_ROLES):
        if not any(item["path"] in parser.paths for item in roles[role]):
            raise ValueError(f"review HTML does not link to {role}")

    concept = _receipt(root, one("concept_approval"), "concept_review", workflow_id, revision)
    paid = _receipt(root, one("paid_approval"), "paid_generation", workflow_id, revision)
    final = _receipt(
        root,
        one("final_approval"),
        "final_visual_review",
        workflow_id,
        revision,
        {"PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"},
    )
    specification = _object(root / one("specification")["path"])
    spec_hash = _spec_fingerprint(specification)
    concept_hash = one("concept")["sha256"]
    _png_dimensions((root / one("concept")["path"]).read_bytes())
    provenance = _object(root / one("concept_provenance")["path"])
    if (
        provenance.get("asset_spec_hash") != spec_hash
        or provenance.get("artifact_hash") != concept_hash
    ):
        raise ValueError("concept provenance does not bind specification and image bytes")
    for kind, receipt in (("concept", concept), ("paid", paid)):
        inputs = receipt["inputs"]
        if (
            not isinstance(inputs, dict)
            or not isinstance(inputs.get("parameters"), dict)
            or not isinstance(inputs.get("scope"), dict)
        ):
            raise ValueError(f"{kind} approval inputs are missing canonical scope")
        parameters, scope = inputs["parameters"], inputs["scope"]
        artifact_hashes = scope.get("artifacts")
        if (
            parameters.get("asset_id") != specification.get("asset_id")
            or parameters.get("revision_number") != revision
            or parameters.get("specification_hash") != spec_hash
            or (
                schema != "asset-evidence-0.6.0"
                and parameters.get("concept_source_hash") != concept_hash
            )
            or scope.get("workflow_id") != workflow_id
            or not isinstance(artifact_hashes, list)
            or not all(isinstance(row, list) and len(row) == 2 for row in artifact_hashes)
            or not {one("specification")["sha256"], concept_hash}
            <= {row[1] for row in artifact_hashes}
        ):
            raise ValueError(f"{kind} approval does not bind specification and concept")
    if schema == "asset-evidence-0.6.0":
        # V0.6 concepts are versioned inside a revision; the creation-time
        # concept_source_hash parameter may name an earlier version. The approved
        # concept is the one named by the hashed concept-review context.
        context = concept["inputs"]["scope"].get("handler_context")
        if (
            not isinstance(context, dict)
            or context.get("concept_sha256") != concept_hash
            or context.get("concept_provenance_sha256") != one("concept_provenance")["sha256"]
        ):
            raise ValueError("concept approval does not bind the bundled concept version")

    provider = _object(root / one("provider_operation")["path"])
    if schema == "asset-evidence-0.6.0":
        # The provider request is bound to the approved canonical snapshot, and the
        # paid approval's hashed inputs bind that snapshot and the readiness report.
        snapshot_entry = one("paid_request_snapshot")
        readiness_entry = one("production_readiness_report")
        paid_parameters = (
            paid["inputs"].get("parameters", {}) if isinstance(paid["inputs"], dict) else {}
        )
        expected_fingerprint = snapshot_entry["sha256"]
        if (
            provider.get("paid_request_snapshot_sha256") != expected_fingerprint
            or paid_parameters.get("paid_request_snapshot_sha256") != expected_fingerprint
            or paid_parameters.get("production_readiness_report_sha256")
            != readiness_entry["sha256"]
        ):
            raise ValueError(
                "paid approval does not bind the bundled request snapshot and readiness"
            )
        snapshot = _object(root / snapshot_entry["path"])
        snapshot_binding = snapshot.get("binding")
        if (
            snapshot.get("schema") != "paid-request-0.6.0"
            or not isinstance(snapshot_binding, dict)
            or snapshot_binding.get("asset_id") != specification.get("asset_id")
            or snapshot_binding.get("revision_number") != revision
            or snapshot_binding.get("specification_sha256") != spec_hash
            or snapshot_binding.get("concept_sha256") != concept_hash
        ):
            raise ValueError("paid request snapshot does not bind this revision and concept")
        readiness = _object(root / readiness_entry["path"])
        if (
            readiness.get("schema") != "production-readiness-0.6.0"
            or readiness.get("result") != "PASS"
            or readiness.get("paid_request_snapshot_sha256") != expected_fingerprint
        ):
            raise ValueError("production readiness report is not a PASS for the approved snapshot")
    else:
        expected_fingerprint = paid["fingerprint"]
    if (
        not isinstance(provider.get("provider"), str)
        or not provider["provider"]
        or provider.get("operation") != "image-to-3d"
        or provider.get("status") != "SUCCEEDED"
        or not provider.get("external_task_id")
        or provider.get("request_fingerprint") != expected_fingerprint
    ):
        raise ValueError("provider operation does not bind the paid approval")
    if (
        provider.get("workflow_id") != workflow_id
        or provider.get("revision") != revision
        or provider.get("task_id") != paid["task_id"]
        or provider.get("concept_sha256") != concept_hash
    ):
        raise ValueError("provider operation belongs to another revision")
    cost = _object(root / one("cost_record")["path"])
    if not isinstance(cost.get("unit"), str) or not cost["unit"]:
        raise ValueError("cost record unit is missing")
    for field in ("estimate", "actual", "budget_reservation"):
        value = cost.get(field)
        if field == "budget_reservation" and value is None:
            continue
        if field != "budget_reservation" and value == "UNKNOWN":
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"cost record {field} must be nonnegative or UNKNOWN")
    if cost.get("actual") != provider.get("actual_cost"):
        raise ValueError("cost record actual does not match provider operation")

    processing = _object(root / one("processing_report")["path"])
    if (
        processing.get("status") != "SUCCESS"
        or processing.get("exit_code") != 0
        or processing.get("input_raw_glb_sha256") != one("raw_glb")["sha256"]
        or processing.get("output_sha256") != one("processed_glb")["sha256"]
        or not _SHA256.fullmatch(str(processing.get("processing_script_sha256", "")))
    ):
        raise ValueError("processing report does not bind successful raw-to-processed conversion")
    validation = _object(root / one("validation")["path"])
    if validation.get("status") != "PASS" or validation.get("passed") is not True:
        raise ValueError("independent GLB validation did not pass")

    observation = _object(root / one("runtime_observation")["path"])
    bound = {
        key: observation.get(key)
        for key in (
            "workflow_id",
            "revision",
            "asset_id",
            "execution_id",
            "attempt_number",
            "processed_glb_sha256",
        )
    }
    if (
        bound["workflow_id"] != workflow_id
        or bound["revision"] != revision
        or not isinstance(bound["asset_id"], str)
        or not bound["asset_id"]
        or bound["asset_id"] != specification.get("asset_id")
        or not isinstance(bound["execution_id"], str)
        or not bound["execution_id"]
        or type(bound["attempt_number"]) is not int
        or bound["attempt_number"] < 1
        or bound["processed_glb_sha256"] != one("processed_glb")["sha256"]
    ):
        raise ValueError("runtime observation identity or processed GLB hash mismatch")
    if schema == "asset-evidence-0.4.0":
        runtime_ok = (
            observation.get("physics_body_present") is True
            and observation.get("physics_ray_hit") is True
        )
    else:
        requirements = manifest.get("runtime_requirements")
        if not isinstance(requirements, dict):
            raise ValueError("0.5.0 bundle is missing runtime_requirements")
        if manifest.get("profile_id") != specification.get("profile"):
            raise ValueError("bundle profile does not match the specification")
        runtime_ok = True
        if requirements.get("require_physics_body") is True:
            runtime_ok = runtime_ok and observation.get("physics_body_present") is True
        if requirements.get("require_ray_hit") is True:
            runtime_ok = runtime_ok and observation.get("physics_ray_hit") is True
        if requirements.get("require_area") is True:
            runtime_ok = runtime_ok and observation.get("area_present") is True
        if requirements.get("require_collision", True) is True:
            runtime_ok = runtime_ok and observation.get("collision_shape_present") is True
    if (
        observation.get("status") != "PASS"
        or observation.get("mesh_visible") is not True
        or observation.get("collision_shape_present") is not True
        or observation.get("errors") != []
        or not runtime_ok
    ):
        raise ValueError("runtime observation does not confirm mesh and live collider")
    bounds = observation.get("mesh_bounds")
    if (
        not isinstance(bounds, dict)
        or not isinstance(bounds.get("size"), list)
        or len(bounds["size"]) != 3
    ):
        raise ValueError("runtime observation has no mesh bounds")
    if any(
        type(value) not in {int, float} or value <= 0 or value != value for value in bounds["size"]
    ):
        raise ValueError("runtime mesh bounds are invalid")
    dimensions = specification.get("dimensions")
    if not isinstance(dimensions, dict):
        raise ValueError("specification dimensions are missing")
    expected_size = [
        dimensions.get("width_m"),
        dimensions.get("height_m"),
        dimensions.get("depth_m"),
    ]
    for measured, expected in zip(bounds["size"], expected_size, strict=True):
        if (
            isinstance(expected, bool)
            or not isinstance(expected, (int, float))
            or not math.isfinite(expected)
            or expected <= 0
            or abs(measured - expected) > max(0.15, expected * 0.2)
        ):
            raise ValueError("runtime mesh bounds do not match specification")
    identity_keys = ("workflow_id", "revision", "asset_id", "processed_glb_sha256")
    attempt_keys = ("execution_id", "attempt_number")
    corrections = roles.get("side_correction", [])
    if len(corrections) > 1:
        raise ValueError("bundle has more than one side correction record")
    correction = _object(root / corrections[0]["path"]) if corrections else None
    for capture in captures:
        if any(capture.get(key) != bound[key] for key in identity_keys):
            raise ValueError(
                f"runtime capture belongs to wrong revision or processed GLB: {capture['path']}"
            )
        same_attempt = all(capture.get(key) == bound[key] for key in attempt_keys)
        if capture.get("angle") != "side" or same_attempt:
            if not same_attempt:
                raise ValueError(
                    f"runtime capture belongs to wrong revision or attempt: {capture['path']}"
                )
        else:
            framing = correction.get("side_framing") if isinstance(correction, dict) else None
            height = framing.get("height_ratio") if isinstance(framing, dict) else None
            if (
                not isinstance(correction, dict)
                or correction.get("workflow_id") != bound["workflow_id"]
                or correction.get("revision") != bound["revision"]
                or correction.get("asset_id") != bound["asset_id"]
                or correction.get("processed_glb_sha256") != bound["processed_glb_sha256"]
                or correction.get("execution_id") != capture.get("execution_id")
                or correction.get("attempt_number") != capture.get("attempt_number")
                or correction.get("status") != "PASS"
                or correction.get("errors") != []
                or correction.get("mesh_visible") is not True
                or correction.get("physics_ray_hit") is not True
                or not isinstance(framing, dict)
                or framing.get("inside_viewport") is not True
                or framing.get("margin_ok") is not True
                or framing.get("horizontally_centered") is not True
                or framing.get("reference_between_camera_and_asset") is not False
                or framing.get("view_axis") != "+X"
                or isinstance(height, bool)
                or not isinstance(height, (int, float))
                or not math.isfinite(height)
                or height < 0.55
                or height > 0.75
            ):
                raise ValueError(
                    f"corrected side capture binding or framing is invalid: {capture['path']}"
                )
        _png_dimensions((root / capture["path"]).read_bytes())

    # The final receipt must explicitly bind the current artifact digests.
    final_inputs = final["inputs"]
    if not isinstance(final_inputs, dict) or not isinstance(final_inputs.get("scope"), dict):
        raise ValueError("final approval inputs have no canonical scope")
    context = final_inputs["scope"].get("handler_context")
    if not isinstance(context, dict) or not isinstance(context.get("artifacts"), dict):
        raise ValueError("final approval does not bind artifact context")
    expected_artifacts = {
        "asset-concept": concept_hash,
        "asset-processed-glb": one("processed_glb")["sha256"],
        "asset-validation-report": one("validation")["sha256"],
        "asset-runtime-observation": one("runtime_observation")["sha256"],
    }
    if (
        context.get("workflow_id") != workflow_id
        or context.get("revision") != revision
        or context.get("specification_hash") != spec_hash
        or any(context["artifacts"].get(key) != value for key, value in expected_artifacts.items())
        or sorted(context.get("runtime_capture_hashes", []))
        != sorted(item["sha256"] for item in captures)
    ):
        raise ValueError("final approval does not bind current evidence hashes")
    if final["status"] not in {"PENDING", "APPROVED", "REJECTED", "CHANGES_REQUESTED"}:
        raise ValueError("final review decision is invalid")
    if manifest.get("final_review") != {
        "decision": final["status"],
        "fingerprint": final["fingerprint"],
    }:
        raise ValueError("manifest final review does not match final approval receipt")
    return {
        "status": "PASS",
        "workflow_id": workflow_id,
        "revision": revision,
        "verified_files": len(files),
        "review_fingerprint": final["fingerprint"],
        "final_review_decision": final["status"],
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -I verify_asset_bundle.py BUNDLE", file=sys.stderr)
        return 2
    try:
        result = verify_bundle(Path(args[0]))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        struct.error,
        json.JSONDecodeError,
        UnicodeError,
        AttributeError,
        RecursionError,
        OverflowError,
    ) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
