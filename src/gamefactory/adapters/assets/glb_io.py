"""Minimal deterministic GLB container I/O (no validation; see glb_validator)."""

from __future__ import annotations

import json
import struct
from typing import Any


def write_glb(document: dict[str, Any], binary: bytes) -> bytes:
    """Serialize a glTF document and BIN chunk deterministically."""
    body = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body += b" " * ((4 - len(body) % 4) % 4)
    blob = bytes(binary) + b"\x00" * ((4 - len(binary) % 4) % 4)
    total = 12 + 8 + len(body) + 8 + len(blob)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(body), 0x4E4F534A)
        + body
        + struct.pack("<II", len(blob), 0x004E4942)
        + blob
    )


def read_glb(data: bytes) -> tuple[dict[str, Any], bytes]:
    """Split a GLB into its JSON document and BIN chunk (no validation)."""
    json_len = struct.unpack_from("<I", data, 12)[0]
    document = json.loads(data[20 : 20 + json_len].decode("utf-8"))
    bin_len = struct.unpack_from("<I", data, 20 + json_len)[0]
    start = 28 + json_len
    return document, data[start : start + bin_len]
