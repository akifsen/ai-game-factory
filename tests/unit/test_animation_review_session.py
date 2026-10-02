"""V0.8-9a animation review session domain (pure, no IO)."""

from __future__ import annotations

import dataclasses
import json
import math

import pytest

from gamefactory.core.domain.animation_review_session import (
    AnimationReviewClipRecord,
    AnimationReviewSession,
    ReviewSetBinding,
    ReviewSetClipBinding,
    animation_review_session_to_document,
    apply_animation_review_session_operation,
    create_animation_review_session,
    parse_animation_review_session_document,
    serialize_animation_review_session,
)
from gamefactory.core.domain.errors import ValidationError

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_DIGEST_D = "d" * 64


def _clip(
    clip_id: str,
    *,
    duration: float = 1.0,
    raw_clip_sha256: str = _DIGEST_A,
) -> ReviewSetClipBinding:
    return ReviewSetClipBinding(
        clip_id=clip_id,
        raw_clip_sha256=raw_clip_sha256,
        duration_seconds=duration,
    )


def _binding_two() -> ReviewSetBinding:
    return ReviewSetBinding(
        review_root="/tmp/review-set",
        raw_manifest_sha256=_DIGEST_A,
        root_payload_sha256=_DIGEST_B,
        clip_payload_sha256=_DIGEST_D,
        clips=(_clip("IdleA"), _clip("WalkB", raw_clip_sha256=_DIGEST_C)),
    )


def _binding_eight() -> ReviewSetBinding:
    ids = ["ClipA", "ClipB", "ClipC", "ClipD", "ClipE", "ClipF", "ClipG", "ClipH"]
    return ReviewSetBinding(
        review_root="review/root",
        raw_manifest_sha256=_DIGEST_A,
        root_payload_sha256=_DIGEST_B,
        clip_payload_sha256=_DIGEST_D,
        clips=tuple(_clip(cid, raw_clip_sha256=_DIGEST_C) for cid in ids),
    )


def _session_document_from_binding(
    binding: ReviewSetBinding, **overrides: object
) -> dict[str, object]:
    session = create_animation_review_session(binding)
    doc = animation_review_session_to_document(session)
    doc.update(overrides)
    return doc


def test_create_initial_state() -> None:
    binding = _binding_two()
    session = create_animation_review_session(binding)
    assert session.revision == 0
    assert session.production_eligible is False
    assert session.promotion_eligible is False
    assert len(session.clip_records) == 2
    assert session.clip_records[0].clip_id == "IdleA"
    assert session.clip_records[0].status == "unreviewed"
    assert session.clip_records[0].note == ""
    assert session.clip_records[0].bookmarks == ()


def test_create_rejects_single_clip() -> None:
    binding = ReviewSetBinding(
        review_root="root",
        raw_manifest_sha256=_DIGEST_A,
        root_payload_sha256=_DIGEST_B,
        clip_payload_sha256=_DIGEST_D,
        clips=(_clip("Only"),),
    )
    with pytest.raises(ValidationError, match="between 2 and 8"):
        create_animation_review_session(binding)


def test_create_accepts_eight_clips() -> None:
    session = create_animation_review_session(_binding_eight())
    assert len(session.clip_records) == 8


def test_parse_rejects_ninth_clip_in_binding() -> None:
    doc = _session_document_from_binding(_binding_eight())
    binding = dict(doc["binding"])  # type: ignore[arg-type]
    clips = list(binding["clips"])
    clips.append(
        {
            "clip_id": "ExtraI",
            "raw_clip_sha256": _DIGEST_A,
            "duration_seconds": 1.0,
        }
    )
    binding["clips"] = clips
    doc["binding"] = binding
    with pytest.raises(ValidationError, match="between 2 and 8"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]


def test_set_status_note_bookmark_and_revision() -> None:
    session = create_animation_review_session(_binding_two())
    session = apply_animation_review_session_operation(
        session,
        {"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
    )
    assert session.revision == 1
    assert session.clip_records[0].status == "keep"
    assert session.clip_records[1].status == "unreviewed"

    session = apply_animation_review_session_operation(
        session,
        {"op": "SetNote", "clip_id": "WalkB", "note": "needs polish"},
    )
    assert session.revision == 2
    assert session.clip_records[1].note == "needs polish"

    session = apply_animation_review_session_operation(
        session,
        {"op": "AddBookmark", "clip_id": "WalkB", "timestamp": 0.5},
    )
    assert session.revision == 3
    assert session.clip_records[1].bookmarks == (0.5,)


def test_immutability_binding_and_other_records() -> None:
    before = create_animation_review_session(_binding_two())
    after = apply_animation_review_session_operation(
        before,
        {"op": "SetStatus", "clip_id": "IdleA", "status": "revise"},
    )
    assert before.revision == 0
    assert before.clip_records[0].status == "unreviewed"
    assert after.binding is before.binding
    assert after.clip_records[1] is before.clip_records[1]


def test_unknown_clip_id_and_status_and_operation() -> None:
    session = create_animation_review_session(_binding_two())
    with pytest.raises(ValidationError, match="unknown clip_id"):
        apply_animation_review_session_operation(
            session,
            {"op": "SetStatus", "clip_id": "Missing", "status": "keep"},
        )
    with pytest.raises(ValidationError, match="status must be"):
        apply_animation_review_session_operation(
            session,
            {"op": "SetStatus", "clip_id": "IdleA", "status": "approved"},
        )
    with pytest.raises(ValidationError, match="unsupported operation"):
        apply_animation_review_session_operation(
            session,
            {"op": "DeleteAll", "clip_id": "IdleA"},
        )


def test_operation_closed_keys() -> None:
    session = create_animation_review_session(_binding_two())
    with pytest.raises(ValidationError, match="unknown fields"):
        apply_animation_review_session_operation(
            session,
            {"op": "SetStatus", "clip_id": "IdleA", "status": "keep", "extra": True},
        )
    with pytest.raises(ValidationError, match="missing"):
        apply_animation_review_session_operation(
            session,
            {"op": "SetNote", "clip_id": "IdleA"},
        )


def test_bookmark_limits_and_duplicates() -> None:
    binding = ReviewSetBinding(
        review_root="root",
        raw_manifest_sha256=_DIGEST_A,
        root_payload_sha256=_DIGEST_B,
        clip_payload_sha256=_DIGEST_D,
        clips=(_clip("IdleA", duration=2.0), _clip("WalkB")),
    )
    session = create_animation_review_session(binding)
    session = apply_animation_review_session_operation(
        session,
        {"op": "AddBookmark", "clip_id": "IdleA", "timestamp": 2.0},
    )
    with pytest.raises(ValidationError, match="unique"):
        apply_animation_review_session_operation(
            session,
            {"op": "AddBookmark", "clip_id": "IdleA", "timestamp": 2.0},
        )

    session = create_animation_review_session(binding)
    for index in range(32):
        session = apply_animation_review_session_operation(
            session,
            {"op": "AddBookmark", "clip_id": "IdleA", "timestamp": float(index) * 0.05},
        )
    with pytest.raises(ValidationError, match="at most 32"):
        apply_animation_review_session_operation(
            session,
            {"op": "AddBookmark", "clip_id": "IdleA", "timestamp": 1.7},
        )


def test_bookmark_rejects_bool_nan_inf_and_out_of_range() -> None:
    session = create_animation_review_session(_binding_two())
    with pytest.raises(ValidationError, match="finite number"):
        apply_animation_review_session_operation(
            session,
            {"op": "AddBookmark", "clip_id": "IdleA", "timestamp": True},
        )
    with pytest.raises(ValidationError, match="finite number"):
        apply_animation_review_session_operation(
            session,
            {"op": "AddBookmark", "clip_id": "IdleA", "timestamp": math.nan},
        )
    with pytest.raises(ValidationError, match="within"):
        apply_animation_review_session_operation(
            session,
            {"op": "AddBookmark", "clip_id": "IdleA", "timestamp": 1.5},
        )


def test_note_utf8_multibyte_and_surrogate() -> None:
    session = create_animation_review_session(_binding_two())
    note = "Ω" * 1024
    session = apply_animation_review_session_operation(
        session,
        {"op": "SetNote", "clip_id": "IdleA", "note": note},
    )
    assert session.clip_records[0].note == note

    with pytest.raises(ValidationError, match="2048"):
        apply_animation_review_session_operation(
            session,
            {"op": "SetNote", "clip_id": "IdleA", "note": "x" * 2049},
        )

    with pytest.raises(ValidationError, match="UTF-8"):
        parse_animation_review_session_document(
            _session_document_with_record_note("\ud800"),
        )


def _session_document_with_record_note(note: str) -> dict[str, object]:
    binding = _binding_two()
    doc = _session_document_from_binding(binding)
    records = list(doc["clip_records"])  # type: ignore[arg-type]
    records[0] = dict(records[0])
    records[0]["note"] = note
    doc["clip_records"] = records
    return doc


def test_parse_malformed_records() -> None:
    binding = _binding_two()
    doc = _session_document_from_binding(binding)
    with pytest.raises(ValidationError, match="unknown fields"):
        records = list(doc["clip_records"])  # type: ignore[arg-type]
        records[0] = dict(records[0])
        records[0]["rogue"] = 1
        doc["clip_records"] = records
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]

    doc = _session_document_from_binding(binding)
    doc["clip_records"] = list(doc["clip_records"])[:1]  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="length must match"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]

    doc = _session_document_from_binding(binding)
    records = list(doc["clip_records"])  # type: ignore[arg-type]
    records[1] = dict(records[1])
    records[1]["clip_id"] = "IdleA"
    doc["clip_records"] = records
    with pytest.raises(ValidationError, match="binding clip order"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]


def test_parse_rejects_bool_as_revision_and_true_eligibility() -> None:
    binding = _binding_two()
    doc = _session_document_from_binding(binding, revision=True)
    with pytest.raises(ValidationError, match="revision"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]

    doc = _session_document_from_binding(binding, production_eligible=True)
    with pytest.raises(ValidationError, match="production_eligible"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]

    doc = _session_document_from_binding(binding, promotion_eligible=1)
    with pytest.raises(ValidationError, match="promotion_eligible"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]


def test_parse_rejects_non_string_document_key() -> None:
    doc = _session_document_from_binding(_binding_two())
    bad: dict[object, object] = dict(doc)
    bad[1] = True
    with pytest.raises(ValidationError, match="keys must be strings"):
        parse_animation_review_session_document(bad)  # type: ignore[arg-type]


def test_parse_rejects_surrogate_in_review_root() -> None:
    doc = _session_document_from_binding(_binding_two())
    binding = dict(doc["binding"])  # type: ignore[arg-type]
    binding["review_root"] = "bad\ud800root"
    doc["binding"] = binding
    with pytest.raises(ValidationError, match="review_root"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]


def test_binding_duplicate_clip_and_sha_case() -> None:
    doc = _session_document_from_binding(_binding_two())
    binding = dict(doc["binding"])  # type: ignore[arg-type]
    clips = list(binding["clips"])
    clips.append(dict(clips[0]))
    binding["clips"] = clips
    doc["binding"] = binding
    with pytest.raises(ValidationError, match="duplicate clip_id"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]

    doc = _session_document_from_binding(_binding_two())
    binding = dict(doc["binding"])  # type: ignore[arg-type]
    binding["raw_manifest_sha256"] = "A" * 64
    doc["binding"] = binding
    with pytest.raises(ValidationError, match="lowercase hex"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]

    doc = _session_document_from_binding(_binding_two())
    binding = dict(doc["binding"])  # type: ignore[arg-type]
    binding["clip_payload_sha256"] = "not-a-valid-digest"
    doc["binding"] = binding
    with pytest.raises(ValidationError, match="binding.clip_payload_sha256"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]

    doc = _session_document_from_binding(_binding_two())
    binding = dict(doc["binding"])  # type: ignore[arg-type]
    del binding["clip_payload_sha256"]
    doc["binding"] = binding
    with pytest.raises(ValidationError, match="clip_payload_sha256"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]


def test_parse_rejects_missing_per_clip_raw_sha256() -> None:
    doc = _session_document_from_binding(_binding_two())
    binding = dict(doc["binding"])  # type: ignore[arg-type]
    clips = list(binding["clips"])
    clips[0] = {"clip_id": "IdleA", "duration_seconds": 1.0}
    binding["clips"] = clips
    doc["binding"] = binding
    with pytest.raises(ValidationError, match="raw_clip_sha256"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]


def test_canonical_roundtrip_and_raw_bytes() -> None:
    binding = _binding_two()
    session = create_animation_review_session(binding)
    session = apply_animation_review_session_operation(
        session,
        {"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
    )
    session = apply_animation_review_session_operation(
        session,
        {"op": "SetNote", "clip_id": "IdleA", "note": "café"},
    )
    session = apply_animation_review_session_operation(
        session,
        {"op": "AddBookmark", "clip_id": "WalkB", "timestamp": 0},
    )
    session = apply_animation_review_session_operation(
        session,
        {"op": "AddBookmark", "clip_id": "WalkB", "timestamp": 1},
    )

    doc = animation_review_session_to_document(session)
    raw = serialize_animation_review_session(session)
    assert b"\\u00" not in raw
    assert raw == serialize_animation_review_session(parse_animation_review_session_document(doc))

    reparsed = parse_animation_review_session_document(json.loads(raw.decode("utf-8")))
    assert reparsed.clip_records[1].bookmarks == (0.0, 1.0)
    assert reparsed.clip_records[0].note == "café"


def test_parse_bookmark_order_preserved() -> None:
    doc = _session_document_from_binding(_binding_two())
    records = list(doc["clip_records"])  # type: ignore[arg-type]
    records[0] = dict(records[0])
    records[0]["bookmarks"] = [0.75, 0.25, 0.5]
    doc["clip_records"] = records
    session = parse_animation_review_session_document(doc)  # type: ignore[arg-type]
    assert session.clip_records[0].bookmarks == (0.75, 0.25, 0.5)


def test_create_rejects_oversized_document_before_use() -> None:
    binding = ReviewSetBinding(
        review_root="r" * 65_000,
        raw_manifest_sha256=_DIGEST_A,
        root_payload_sha256=_DIGEST_B,
        clip_payload_sha256=_DIGEST_D,
        clips=(_clip("IdleA"), _clip("WalkB")),
    )
    with pytest.raises(ValidationError, match="exceeds"):
        create_animation_review_session(binding)


def test_serialize_enforces_total_size_on_valid_session() -> None:
    session = create_animation_review_session(_binding_eight())
    huge_note = "\x01" * 2048
    records = tuple(dataclasses.replace(record, note=huge_note) for record in session.clip_records)
    oversized = dataclasses.replace(session, clip_records=records)
    with pytest.raises(ValidationError, match="exceeds"):
        serialize_animation_review_session(oversized)


def test_apply_rejects_cumulative_oversized_notes_immutably() -> None:
    session = create_animation_review_session(_binding_eight())
    huge_note = "\x01" * 2048
    clip_ids = [clip.clip_id for clip in session.binding.clips]
    for clip_id in clip_ids[:5]:
        session = apply_animation_review_session_operation(
            session,
            {"op": "SetNote", "clip_id": clip_id, "note": huge_note},
        )
    before_revision = session.revision
    with pytest.raises(ValidationError, match="exceeds"):
        apply_animation_review_session_operation(
            session,
            {"op": "SetNote", "clip_id": clip_ids[5], "note": huge_note},
        )
    assert session.revision == before_revision


def test_duration_must_be_positive_finite() -> None:
    doc = _session_document_from_binding(_binding_two())
    binding = dict(doc["binding"])  # type: ignore[arg-type]
    clips = list(binding["clips"])
    clips[0] = dict(clips[0])
    clips[0]["duration_seconds"] = 0
    binding["clips"] = clips
    doc["binding"] = binding
    with pytest.raises(ValidationError, match="> 0"):
        parse_animation_review_session_document(doc)  # type: ignore[arg-type]


def _forged_session(**changes: object) -> AnimationReviewSession:
    session = create_animation_review_session(_binding_two())
    return dataclasses.replace(session, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (
            lambda s: dataclasses.replace(s, production_eligible=True),  # type: ignore[arg-type]
            "production_eligible",
        ),
        (
            lambda s: dataclasses.replace(s, promotion_eligible=True),  # type: ignore[arg-type]
            "promotion_eligible",
        ),
        (lambda s: dataclasses.replace(s, revision=True), "revision"),  # type: ignore[arg-type]
        (
            lambda s: dataclasses.replace(
                s,
                clip_records=(
                    AnimationReviewClipRecord(
                        clip_id="IdleA",
                        status="bogus",
                        note="",
                        bookmarks=(),
                    ),
                    s.clip_records[1],
                ),
            ),
            "status must be",
        ),
        (
            lambda s: dataclasses.replace(
                s,
                clip_records=(
                    AnimationReviewClipRecord(
                        clip_id="WalkB",
                        status="unreviewed",
                        note="",
                        bookmarks=(),
                    ),
                    s.clip_records[1],
                ),
            ),
            "binding clip order",
        ),
        (
            lambda s: dataclasses.replace(
                s,
                binding=ReviewSetBinding(
                    review_root="root",
                    raw_manifest_sha256=_DIGEST_A,
                    root_payload_sha256=_DIGEST_B,
                    clip_payload_sha256=_DIGEST_D,
                    clips=(
                        _clip("IdleA"),
                        ReviewSetClipBinding(
                            clip_id="WalkB",
                            raw_clip_sha256="Z" * 64,
                            duration_seconds=1.0,
                        ),
                    ),
                ),
            ),
            "lowercase hex",
        ),
        (
            lambda s: dataclasses.replace(
                s,
                binding=ReviewSetBinding(
                    review_root=s.binding.review_root,
                    raw_manifest_sha256=s.binding.raw_manifest_sha256,
                    root_payload_sha256=s.binding.root_payload_sha256,
                    clip_payload_sha256="A" * 64,
                    clips=s.binding.clips,
                ),
            ),
            "lowercase hex",
        ),
    ],
)
def test_public_apis_reject_forged_session(
    mutator: object,
    match: str,
) -> None:
    session = mutator(_forged_session())
    with pytest.raises(ValidationError, match=match):
        animation_review_session_to_document(session)
    with pytest.raises(ValidationError, match=match):
        serialize_animation_review_session(session)
    with pytest.raises(ValidationError, match=match):
        apply_animation_review_session_operation(
            session,
            {"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
        )


def test_to_document_preserves_binding_digest_fields() -> None:
    session = create_animation_review_session(_binding_two())
    doc = animation_review_session_to_document(session)
    binding = doc["binding"]
    assert binding["root_payload_sha256"] == _DIGEST_B
    assert binding["raw_manifest_sha256"] == _DIGEST_A
    assert binding["clip_payload_sha256"] == _DIGEST_D
    clip0 = binding["clips"][0]
    assert clip0["raw_clip_sha256"] == _DIGEST_A
    assert "clip_payload_sha256" not in clip0


def test_canonical_roundtrip_eight_clip_binding() -> None:
    session = create_animation_review_session(_binding_eight())
    doc = animation_review_session_to_document(session)
    raw = serialize_animation_review_session(session)
    assert raw == serialize_animation_review_session(parse_animation_review_session_document(doc))
    assert doc["binding"]["clip_payload_sha256"] == _DIGEST_D
    assert len(doc["binding"]["clips"]) == 8
    assert all("raw_clip_sha256" in clip for clip in doc["binding"]["clips"])


def test_integer_duration_binding_canonical_bytes_match_float_equivalent() -> None:
    binding_int = ReviewSetBinding(
        review_root="/tmp/review-set",
        raw_manifest_sha256=_DIGEST_A,
        root_payload_sha256=_DIGEST_B,
        clip_payload_sha256=_DIGEST_D,
        clips=(
            ReviewSetClipBinding("IdleA", _DIGEST_A, 1),
            ReviewSetClipBinding("WalkB", _DIGEST_C, 2),
        ),
    )
    session_int = create_animation_review_session(binding_int)
    raw_int = serialize_animation_review_session(session_int)
    doc = animation_review_session_to_document(session_int)
    reparsed_from_json = parse_animation_review_session_document(
        json.loads(raw_int.decode("utf-8"))
    )
    assert raw_int == serialize_animation_review_session(reparsed_from_json)
    assert raw_int == serialize_animation_review_session(
        parse_animation_review_session_document(doc)
    )

    binding_float = ReviewSetBinding(
        review_root="/tmp/review-set",
        raw_manifest_sha256=_DIGEST_A,
        root_payload_sha256=_DIGEST_B,
        clip_payload_sha256=_DIGEST_D,
        clips=(
            _clip("IdleA", duration=1.0),
            _clip("WalkB", duration=2.0, raw_clip_sha256=_DIGEST_C),
        ),
    )
    raw_float = serialize_animation_review_session(create_animation_review_session(binding_float))
    assert raw_int == raw_float
    assert b'"duration_seconds":1.0' in raw_int
    assert b'"duration_seconds":2.0' in raw_int


def test_forged_integer_bookmarks_serialize_to_canonical_float_bytes() -> None:
    session = create_animation_review_session(_binding_two())
    session = apply_animation_review_session_operation(
        session,
        {"op": "AddBookmark", "clip_id": "WalkB", "timestamp": 0},
    )
    session = apply_animation_review_session_operation(
        session,
        {"op": "AddBookmark", "clip_id": "WalkB", "timestamp": 1},
    )
    session_int_bookmarks = dataclasses.replace(
        session,
        clip_records=(
            session.clip_records[0],
            dataclasses.replace(session.clip_records[1], bookmarks=(0, 1)),
        ),
    )
    raw_int = serialize_animation_review_session(session_int_bookmarks)
    assert raw_int == serialize_animation_review_session(
        parse_animation_review_session_document(json.loads(raw_int.decode("utf-8")))
    )
    assert raw_int == serialize_animation_review_session(session)
    assert b'"bookmarks":[0.0,1.0]' in raw_int
