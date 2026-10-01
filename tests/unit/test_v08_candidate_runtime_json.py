"""Strict JSON parsing policy for V0.8-3B candidate runtime artifacts."""

from __future__ import annotations

import pytest

from gamefactory.adapters.assets.v08_candidate_runtime_json import (
    CandidateRuntimeJsonError,
    parse_strict_runtime_json_object,
)


def test_rejects_nan_constant() -> None:
    with pytest.raises(CandidateRuntimeJsonError, match="invalid JSON constant"):
        parse_strict_runtime_json_object('{"x": NaN}', max_bytes=1024)


def test_rejects_infinity_constant() -> None:
    with pytest.raises(CandidateRuntimeJsonError, match="invalid JSON constant"):
        parse_strict_runtime_json_object('{"x": Infinity}', max_bytes=1024)


def test_rejects_invalid_utf8() -> None:
    with pytest.raises(CandidateRuntimeJsonError, match="invalid UTF-8"):
        parse_strict_runtime_json_object(b"\xff\xfe", max_bytes=1024)


def test_rejects_duplicate_keys() -> None:
    with pytest.raises(CandidateRuntimeJsonError, match="duplicate JSON key"):
        parse_strict_runtime_json_object('{"a": 1, "a": 2}', max_bytes=1024)


def test_accepts_finite_nested_object() -> None:
    parsed = parse_strict_runtime_json_object('{"nested": {"ok": true, "n": 1.5}}', max_bytes=1024)
    assert parsed["nested"]["n"] == 1.5
