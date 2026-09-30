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
import re
import struct
import sys
import zlib
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

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
        path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs, parse_constant=nonfinite
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
        if kind == b"IHDR":
            if width is not None or length != 13:
                raise ValueError("runtime PNG has invalid IHDR")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", chunk
            )
            if not width or not height or compression or filtering or interlace:
                raise ValueError("runtime PNG dimensions or encoding are unsupported")
        elif kind == b"IDAT":
            compressed.extend(chunk)
        elif kind == b"IEND":
            if length or end != len(raw):
                raise ValueError("runtime PNG has invalid trailing data")
            ended = True
            break
        offset = end
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color) if color is not None else None
    if (
        not ended
        or width is None
        or height is None
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
    except (OSError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
