"""Closed animation-review-session-0.8.0 domain model (V0.8-9a, domain only)."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from gamefactory.core.domain.errors import ValidationError

ANIMATION_REVIEW_SESSION_SCHEMA_VERSION = "animation-review-session-0.8.0"

_CLIP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_MIN_CLIPS = 2
_MAX_CLIPS = 8
_MIN_DURATION_SECONDS = 0.0
_MAX_DURATION_SECONDS = 10.0
_MAX_NOTE_UTF8_BYTES = 2048
_MAX_BOOKMARKS_PER_CLIP = 32
_MAX_SESSION_DOCUMENT_BYTES = 64 * 1024

_REVIEW_STATUSES = frozenset({"unreviewed", "keep", "revise"})

_BINDING_KEYS = frozenset(
    {
        "review_root",
        "raw_manifest_sha256",
        "root_payload_sha256",
        "clip_payload_sha256",
        "clips",
    }
)
_CLIP_BINDING_KEYS = frozenset(
    {
        "clip_id",
        "raw_clip_sha256",
        "duration_seconds",
    }
)
_SESSION_KEYS = frozenset(
    {
        "schema_version",
        "binding",
        "revision",
        "clip_records",
        "production_eligible",
        "promotion_eligible",
    }
)
_RECORD_KEYS = frozenset({"clip_id", "status", "note", "bookmarks"})

_OPERATION_KEY_SETS: dict[str, frozenset[str]] = {
    "SetStatus": frozenset({"op", "clip_id", "status"}),
    "SetNote": frozenset({"op", "clip_id", "note"}),
    "AddBookmark": frozenset({"op", "clip_id", "timestamp"}),
}


@dataclass(frozen=True)
class ReviewSetClipBinding:
    clip_id: str
    raw_clip_sha256: str
    duration_seconds: float


@dataclass(frozen=True)
class ReviewSetBinding:
    review_root: str
    raw_manifest_sha256: str
    root_payload_sha256: str
    clip_payload_sha256: str
    clips: tuple[ReviewSetClipBinding, ...]


@dataclass(frozen=True)
class AnimationReviewClipRecord:
    clip_id: str
    status: str
    note: str
    bookmarks: tuple[float, ...]


@dataclass(frozen=True)
class AnimationReviewSession:
    schema_version: str
    binding: ReviewSetBinding
    revision: int
    clip_records: tuple[AnimationReviewClipRecord, ...]
    production_eligible: Literal[False]
    promotion_eligible: Literal[False]


def _require_str_dict_keys(data: dict[Any, Any], *, label: str) -> None:
    for key in data.keys():
        if not isinstance(key, str):
            raise ValidationError(f"{label} keys must be strings")


def _require_exact_keys(data: dict[str, Any], allowed: frozenset[str], *, label: str) -> None:
    _require_str_dict_keys(data, label=label)
    if set(data.keys()) != allowed:
        missing = allowed - set(data.keys())
        extra = set(data.keys()) - allowed
        if missing:
            raise ValidationError(f"{label} is missing {sorted(missing)[0]}")
        if extra:
            raise ValidationError(f"{label} has unknown fields: {sorted(extra)}")


def _require_actual_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ValidationError(f"{field} must be a boolean")
    return value


def _require_false_flag(value: Any, field: str) -> Literal[False]:
    if type(value) is not bool:
        raise ValidationError(f"{field} must be a boolean")
    if value is not False:
        raise ValidationError(f"{field} must be false")
    return False


def _require_nonnegative_strict_int(value: Any, field: str) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ValidationError(f"{field} must be a non-negative integer")
    if value < 0:
        raise ValidationError(f"{field} must be a non-negative integer")
    return value


def _require_finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} must be a finite number")
    try:
        out = float(value)
    except OverflowError as exc:
        raise ValidationError(f"{field} must be a finite number") from exc
    if not math.isfinite(out):
        raise ValidationError(f"{field} must be a finite number")
    return out


def _validate_non_empty_utf8_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{field} must be valid UTF-8") from exc
    if not value:
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _validate_clip_id(value: Any, *, field: str = "clip_id") -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if not _CLIP_ID_RE.fullmatch(value):
        raise ValidationError(f"{field} must match [A-Za-z][A-Za-z0-9_]{{0,63}}")
    return value


def _validate_sha256_hex(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    if not _SHA256_HEX_RE.fullmatch(value):
        raise ValidationError(f"{field} must be a 64-character lowercase hex digest")
    return value


def _validate_duration_seconds(value: Any, field: str) -> float:
    duration = _require_finite_number(value, field)
    if duration <= _MIN_DURATION_SECONDS or duration > _MAX_DURATION_SECONDS:
        raise ValidationError(f"{field} must be > 0 and <= {_MAX_DURATION_SECONDS}")
    return duration


def _validate_note(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("note must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError("note must be valid UTF-8") from exc
    if len(encoded) > _MAX_NOTE_UTF8_BYTES:
        raise ValidationError(f"note must be at most {_MAX_NOTE_UTF8_BYTES} UTF-8 bytes")
    return value


def _validate_review_status(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError("status must be a string")
    if value not in _REVIEW_STATUSES:
        raise ValidationError("status must be one of unreviewed, keep, revise")
    return value


def _validate_bookmark_timestamp(value: Any, *, duration_seconds: float, field: str) -> float:
    timestamp = _require_finite_number(value, field)
    if timestamp < 0.0 or timestamp > duration_seconds:
        raise ValidationError(f"{field} must be within [0, duration_seconds]")
    return timestamp


def _validate_bookmarks_sequence(
    bookmarks: Any,
    *,
    duration_seconds: float,
    label: str,
) -> None:
    if not isinstance(bookmarks, tuple):
        raise ValidationError(f"{label} must be a tuple")
    if len(bookmarks) > _MAX_BOOKMARKS_PER_CLIP:
        raise ValidationError(f"{label} must contain at most {_MAX_BOOKMARKS_PER_CLIP} entries")
    seen: set[float] = set()
    for index, entry in enumerate(bookmarks):
        ts = _validate_bookmark_timestamp(
            entry,
            duration_seconds=duration_seconds,
            field=f"{label}[{index}]",
        )
        if ts in seen:
            raise ValidationError(f"{label} must not contain duplicate timestamps")
        seen.add(ts)


def _parse_bookmarks(
    raw: Any,
    *,
    duration_seconds: float,
    label: str,
) -> tuple[float, ...]:
    if not isinstance(raw, list):
        raise ValidationError(f"{label} must be an array")
    if len(raw) > _MAX_BOOKMARKS_PER_CLIP:
        raise ValidationError(f"{label} must contain at most {_MAX_BOOKMARKS_PER_CLIP} entries")
    bookmarks: list[float] = []
    seen: set[float] = set()
    for index, entry in enumerate(raw):
        ts = _validate_bookmark_timestamp(
            entry,
            duration_seconds=duration_seconds,
            field=f"{label}[{index}]",
        )
        if ts in seen:
            raise ValidationError(f"{label} must not contain duplicate timestamps")
        seen.add(ts)
        bookmarks.append(ts)
    return tuple(bookmarks)


def _parse_clip_binding(raw: Any, *, label: str) -> ReviewSetClipBinding:
    if not isinstance(raw, dict):
        raise ValidationError(f"{label} must be an object")
    _require_exact_keys(raw, _CLIP_BINDING_KEYS, label=label)
    clip_id = _validate_clip_id(raw.get("clip_id"), field=f"{label}.clip_id")
    raw_clip_sha256 = _validate_sha256_hex(
        raw.get("raw_clip_sha256"),
        f"{label}.raw_clip_sha256",
    )
    duration_seconds = _validate_duration_seconds(
        raw.get("duration_seconds"),
        f"{label}.duration_seconds",
    )
    return ReviewSetClipBinding(
        clip_id=clip_id,
        raw_clip_sha256=raw_clip_sha256,
        duration_seconds=duration_seconds,
    )


def _validate_review_set_clip_binding_model(clip: Any, *, label: str) -> None:
    if not isinstance(clip, ReviewSetClipBinding):
        raise ValidationError(f"{label} must be a ReviewSetClipBinding")
    _validate_clip_id(clip.clip_id, field=f"{label}.clip_id")
    _validate_sha256_hex(clip.raw_clip_sha256, f"{label}.raw_clip_sha256")
    _validate_duration_seconds(clip.duration_seconds, f"{label}.duration_seconds")


def _parse_review_set_binding(raw: Any) -> ReviewSetBinding:
    if not isinstance(raw, dict):
        raise ValidationError("binding must be an object")
    _require_exact_keys(raw, _BINDING_KEYS, label="binding")
    review_root = _validate_non_empty_utf8_string(raw.get("review_root"), "binding.review_root")
    raw_manifest_sha256 = _validate_sha256_hex(
        raw.get("raw_manifest_sha256"),
        "binding.raw_manifest_sha256",
    )
    root_payload_sha256 = _validate_sha256_hex(
        raw.get("root_payload_sha256"),
        "binding.root_payload_sha256",
    )
    clip_payload_sha256 = _validate_sha256_hex(
        raw.get("clip_payload_sha256"),
        "binding.clip_payload_sha256",
    )
    clips_raw = raw.get("clips")
    if not isinstance(clips_raw, list):
        raise ValidationError("binding.clips must be an array")
    if not (_MIN_CLIPS <= len(clips_raw) <= _MAX_CLIPS):
        raise ValidationError(
            f"binding.clips must contain between {_MIN_CLIPS} and {_MAX_CLIPS} entries"
        )
    clips: list[ReviewSetClipBinding] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(clips_raw):
        clip = _parse_clip_binding(entry, label=f"binding.clips[{index}]")
        if clip.clip_id in seen_ids:
            raise ValidationError(f"duplicate clip_id in binding: {clip.clip_id}")
        seen_ids.add(clip.clip_id)
        clips.append(clip)
    return ReviewSetBinding(
        review_root=review_root,
        raw_manifest_sha256=raw_manifest_sha256,
        root_payload_sha256=root_payload_sha256,
        clip_payload_sha256=clip_payload_sha256,
        clips=tuple(clips),
    )


def _validate_review_set_binding(binding: Any) -> None:
    if not isinstance(binding, ReviewSetBinding):
        raise ValidationError("binding must be a ReviewSetBinding")
    _validate_non_empty_utf8_string(binding.review_root, "binding.review_root")
    _validate_sha256_hex(binding.raw_manifest_sha256, "binding.raw_manifest_sha256")
    _validate_sha256_hex(binding.root_payload_sha256, "binding.root_payload_sha256")
    _validate_sha256_hex(binding.clip_payload_sha256, "binding.clip_payload_sha256")
    if not isinstance(binding.clips, tuple):
        raise ValidationError("binding.clips must be a tuple")
    if not (_MIN_CLIPS <= len(binding.clips) <= _MAX_CLIPS):
        raise ValidationError(
            f"binding.clips must contain between {_MIN_CLIPS} and {_MAX_CLIPS} entries"
        )
    seen_ids: set[str] = set()
    for index, clip in enumerate(binding.clips):
        _validate_review_set_clip_binding_model(clip, label=f"binding.clips[{index}]")
        if clip.clip_id in seen_ids:
            raise ValidationError(f"duplicate clip_id in binding: {clip.clip_id}")
        seen_ids.add(clip.clip_id)


def _duration_for_clip(binding: ReviewSetBinding, clip_id: str) -> float:
    for clip in binding.clips:
        if clip.clip_id == clip_id:
            return clip.duration_seconds
    raise ValidationError(f"unknown clip_id: {clip_id}")


def _validate_clip_record_model(
    record: Any,
    *,
    binding: ReviewSetBinding,
    expected_clip_id: str,
    label: str,
) -> None:
    if not isinstance(record, AnimationReviewClipRecord):
        raise ValidationError(f"{label} must be an AnimationReviewClipRecord")
    clip_id = _validate_clip_id(record.clip_id, field=f"{label}.clip_id")
    if clip_id != expected_clip_id:
        raise ValidationError(f"{label}.clip_id must match binding clip order")
    _validate_review_status(record.status)
    _validate_note(record.note)
    duration_seconds = _duration_for_clip(binding, clip_id)
    _validate_bookmarks_sequence(
        record.bookmarks,
        duration_seconds=duration_seconds,
        label=f"{label}.bookmarks",
    )


def _parse_clip_record(
    raw: Any,
    *,
    binding: ReviewSetBinding,
    expected_clip_id: str,
    label: str,
) -> AnimationReviewClipRecord:
    if not isinstance(raw, dict):
        raise ValidationError(f"{label} must be an object")
    _require_exact_keys(raw, _RECORD_KEYS, label=label)
    clip_id = _validate_clip_id(raw.get("clip_id"), field=f"{label}.clip_id")
    if clip_id != expected_clip_id:
        raise ValidationError(f"{label}.clip_id must match binding clip order")
    status = _validate_review_status(raw.get("status"))
    note = _validate_note(raw.get("note"))
    duration_seconds = _duration_for_clip(binding, clip_id)
    bookmarks = _parse_bookmarks(
        raw.get("bookmarks"),
        duration_seconds=duration_seconds,
        label=f"{label}.bookmarks",
    )
    return AnimationReviewClipRecord(
        clip_id=clip_id,
        status=status,
        note=note,
        bookmarks=bookmarks,
    )


def _parse_clip_records(
    raw: Any,
    *,
    binding: ReviewSetBinding,
) -> tuple[AnimationReviewClipRecord, ...]:
    if not isinstance(raw, list):
        raise ValidationError("clip_records must be an array")
    expected_ids = [clip.clip_id for clip in binding.clips]
    if len(raw) != len(expected_ids):
        raise ValidationError("clip_records length must match binding.clips")
    records: list[AnimationReviewClipRecord] = []
    for index, entry in enumerate(raw):
        record = _parse_clip_record(
            entry,
            binding=binding,
            expected_clip_id=expected_ids[index],
            label=f"clip_records[{index}]",
        )
        records.append(record)
    return tuple(records)


def _encode_document_duration_seconds(value: Any, field: str) -> float:
    """Canonical JSON number for a validated clip duration (ints normalized to float)."""
    return _validate_duration_seconds(value, field)


def _encode_document_bookmark_timestamp(
    value: Any,
    *,
    duration_seconds: float,
    field: str,
) -> float:
    """Canonical JSON number for a validated bookmark (ints normalized to float)."""
    return _validate_bookmark_timestamp(
        value,
        duration_seconds=duration_seconds,
        field=field,
    )


def _build_session_document_dict(session: AnimationReviewSession) -> dict[str, Any]:
    binding = session.binding
    clips_document: list[dict[str, Any]] = []
    for clip_index, clip in enumerate(binding.clips):
        clips_document.append(
            {
                "clip_id": clip.clip_id,
                "raw_clip_sha256": clip.raw_clip_sha256,
                "duration_seconds": _encode_document_duration_seconds(
                    clip.duration_seconds,
                    f"binding.clips[{clip_index}].duration_seconds",
                ),
            }
        )
    clip_records_document: list[dict[str, Any]] = []
    for record_index, record in enumerate(session.clip_records):
        duration_seconds = _duration_for_clip(binding, record.clip_id)
        clip_records_document.append(
            {
                "clip_id": record.clip_id,
                "status": record.status,
                "note": record.note,
                "bookmarks": [
                    _encode_document_bookmark_timestamp(
                        timestamp,
                        duration_seconds=duration_seconds,
                        field=f"clip_records[{record_index}].bookmarks[{bookmark_index}]",
                    )
                    for bookmark_index, timestamp in enumerate(record.bookmarks)
                ],
            }
        )
    return {
        "schema_version": session.schema_version,
        "binding": {
            "review_root": binding.review_root,
            "raw_manifest_sha256": binding.raw_manifest_sha256,
            "root_payload_sha256": binding.root_payload_sha256,
            "clip_payload_sha256": binding.clip_payload_sha256,
            "clips": clips_document,
        },
        "revision": session.revision,
        "clip_records": clip_records_document,
        "production_eligible": session.production_eligible,
        "promotion_eligible": session.promotion_eligible,
    }


def _encode_document_dict(document: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValidationError("animation review session document is not JSON-encodable") from exc


def _ensure_session_document_within_limit(session: AnimationReviewSession) -> None:
    encoded = _encode_document_dict(_build_session_document_dict(session))
    if len(encoded) > _MAX_SESSION_DOCUMENT_BYTES:
        raise ValidationError(
            f"animation review session document exceeds {_MAX_SESSION_DOCUMENT_BYTES} bytes"
        )


def _validate_animation_review_session_model(session: Any) -> None:
    if not isinstance(session, AnimationReviewSession):
        raise ValidationError("animation review session must be an AnimationReviewSession")
    if session.schema_version != ANIMATION_REVIEW_SESSION_SCHEMA_VERSION:
        raise ValidationError(
            "schema_version must be "
            f"{ANIMATION_REVIEW_SESSION_SCHEMA_VERSION!r}, got {session.schema_version!r}"
        )
    _validate_review_set_binding(session.binding)
    _require_nonnegative_strict_int(session.revision, "revision")
    if not isinstance(session.clip_records, tuple):
        raise ValidationError("clip_records must be a tuple")
    expected_ids = [clip.clip_id for clip in session.binding.clips]
    if len(session.clip_records) != len(expected_ids):
        raise ValidationError("clip_records length must match binding.clips")
    for index, record in enumerate(session.clip_records):
        _validate_clip_record_model(
            record,
            binding=session.binding,
            expected_clip_id=expected_ids[index],
            label=f"clip_records[{index}]",
        )
    _require_false_flag(session.production_eligible, "production_eligible")
    _require_false_flag(session.promotion_eligible, "promotion_eligible")


def create_animation_review_session(binding: ReviewSetBinding) -> AnimationReviewSession:
    """Create a new session with revision 0 and unreviewed empty per-clip records."""
    _validate_review_set_binding(binding)
    records = tuple(
        AnimationReviewClipRecord(
            clip_id=clip.clip_id,
            status="unreviewed",
            note="",
            bookmarks=(),
        )
        for clip in binding.clips
    )
    session = AnimationReviewSession(
        schema_version=ANIMATION_REVIEW_SESSION_SCHEMA_VERSION,
        binding=binding,
        revision=0,
        clip_records=records,
        production_eligible=False,
        promotion_eligible=False,
    )
    _validate_animation_review_session_model(session)
    _ensure_session_document_within_limit(session)
    return session


def parse_animation_review_session_document(document: dict[str, Any]) -> AnimationReviewSession:
    """Parse and validate a closed animation-review-session-0.8.0 document."""
    if not isinstance(document, dict):
        raise ValidationError("animation review session document must be a JSON object")
    _require_str_dict_keys(document, label="animation review session")
    _require_exact_keys(document, _SESSION_KEYS, label="animation review session")
    schema_version = document.get("schema_version")
    if schema_version != ANIMATION_REVIEW_SESSION_SCHEMA_VERSION:
        raise ValidationError(
            "schema_version must be "
            f"{ANIMATION_REVIEW_SESSION_SCHEMA_VERSION!r}, got {schema_version!r}"
        )
    binding = _parse_review_set_binding(document.get("binding"))
    revision = _require_nonnegative_strict_int(document.get("revision"), "revision")
    clip_records = _parse_clip_records(document.get("clip_records"), binding=binding)
    production_eligible = _require_false_flag(
        document.get("production_eligible"),
        "production_eligible",
    )
    promotion_eligible = _require_false_flag(
        document.get("promotion_eligible"),
        "promotion_eligible",
    )
    session = AnimationReviewSession(
        schema_version=ANIMATION_REVIEW_SESSION_SCHEMA_VERSION,
        binding=binding,
        revision=revision,
        clip_records=clip_records,
        production_eligible=production_eligible,
        promotion_eligible=promotion_eligible,
    )
    _validate_animation_review_session_model(session)
    _ensure_session_document_within_limit(session)
    return session


def animation_review_session_to_document(session: AnimationReviewSession) -> dict[str, Any]:
    """Deterministic JSON-serializable document for the parsed session."""
    _validate_animation_review_session_model(session)
    _ensure_session_document_within_limit(session)
    return _build_session_document_dict(session)


def serialize_animation_review_session(session: AnimationReviewSession) -> bytes:
    """Canonical UTF-8 bytes for deterministic storage (total size bounded)."""
    _validate_animation_review_session_model(session)
    encoded = _encode_document_dict(_build_session_document_dict(session))
    if len(encoded) > _MAX_SESSION_DOCUMENT_BYTES:
        raise ValidationError(
            f"animation review session document exceeds {_MAX_SESSION_DOCUMENT_BYTES} bytes"
        )
    return encoded


def apply_animation_review_session_operation(
    session: AnimationReviewSession,
    operation: dict[str, Any],
) -> AnimationReviewSession:
    """Apply one typed mutation and return a new session with revision incremented by one."""
    _validate_animation_review_session_model(session)
    _ensure_session_document_within_limit(session)
    if not isinstance(operation, dict):
        raise ValidationError("operation must be a JSON object")
    _require_str_dict_keys(operation, label="operation")
    op_name = operation.get("op")
    if not isinstance(op_name, str):
        raise ValidationError("operation.op must be a string")
    allowed_keys = _OPERATION_KEY_SETS.get(op_name)
    if allowed_keys is None:
        raise ValidationError(f"unsupported operation: {op_name!r}")
    if set(operation.keys()) != allowed_keys:
        extra = set(operation.keys()) - allowed_keys
        missing = allowed_keys - set(operation.keys())
        if missing:
            raise ValidationError(f"operation is missing {sorted(missing)[0]}")
        if extra:
            raise ValidationError(f"operation has unknown fields: {sorted(extra)}")

    clip_id = _validate_clip_id(operation.get("clip_id"), field="operation.clip_id")
    clip_index = next(
        (index for index, clip in enumerate(session.binding.clips) if clip.clip_id == clip_id),
        None,
    )
    if clip_index is None:
        raise ValidationError(f"unknown clip_id: {clip_id}")

    current = session.clip_records[clip_index]
    duration_seconds = session.binding.clips[clip_index].duration_seconds

    if op_name == "SetStatus":
        status = _validate_review_status(operation.get("status"))
        updated_record = AnimationReviewClipRecord(
            clip_id=current.clip_id,
            status=status,
            note=current.note,
            bookmarks=current.bookmarks,
        )
    elif op_name == "SetNote":
        note = _validate_note(operation.get("note"))
        updated_record = AnimationReviewClipRecord(
            clip_id=current.clip_id,
            status=current.status,
            note=note,
            bookmarks=current.bookmarks,
        )
    elif op_name == "AddBookmark":
        timestamp = _validate_bookmark_timestamp(
            operation.get("timestamp"),
            duration_seconds=duration_seconds,
            field="operation.timestamp",
        )
        if timestamp in current.bookmarks:
            raise ValidationError("bookmark timestamp must be unique within the clip record")
        if len(current.bookmarks) >= _MAX_BOOKMARKS_PER_CLIP:
            raise ValidationError(
                f"clip record must contain at most {_MAX_BOOKMARKS_PER_CLIP} bookmarks"
            )
        updated_bookmarks = tuple(list(current.bookmarks) + [timestamp])
        updated_record = AnimationReviewClipRecord(
            clip_id=current.clip_id,
            status=current.status,
            note=current.note,
            bookmarks=updated_bookmarks,
        )
    else:
        raise ValidationError(f"unsupported operation: {op_name!r}")

    new_records = list(session.clip_records)
    new_records[clip_index] = updated_record
    updated = AnimationReviewSession(
        schema_version=session.schema_version,
        binding=session.binding,
        revision=session.revision + 1,
        clip_records=tuple(new_records),
        production_eligible=session.production_eligible,
        promotion_eligible=session.promotion_eligible,
    )
    _validate_animation_review_session_model(updated)
    _ensure_session_document_within_limit(updated)
    return updated
