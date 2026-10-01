"""V0.8-3B candidate runtime request digest and strict identity."""

from __future__ import annotations

import copy

import pytest

from gamefactory.adapters.assets.v08_candidate_runtime_digest import (
    JSON_SAFE_INTEGER_MAX,
    CandidateRuntimeDigestError,
    candidate_runtime_request_digest,
    candidate_runtime_rest_aabb_canonical_sha256,
    canonicalize_candidate_runtime_request_for_digest,
    strict_runtime_int,
)
from gamefactory.core.domain.v08_candidate_runtime_integers import (
    JSON_SAFE_INTEGER_MAX as CORE_JSON_SAFE_INTEGER_MAX,
)


def _sample() -> dict:
    return {
        "schema_version": "candidate-runtime-request-0.8.0",
        "asset_id": "humanoid_skin_candidate_01",
        "workflow_id": "WF-TEST",
        "revision": 1,
        "execution_id": "EXEC-1",
        "strict_attempt_number": 1,
        "processed_glb_sha256": "a" * 64,
        "profile_document_hash": "b" * 64,
        "spec_fingerprint": "c" * 64,
        "runtime_contract_sha256": "d" * 64,
        "visual_mesh_name": "SM_HumanoidSkin",
        "rest_aabb": {"min": [-0.5, 0.0, -0.1], "max": [0.5, 1.0, 0.1]},
        "rest_aabb_canonical_sha256": candidate_runtime_rest_aabb_canonical_sha256(
            {"min": [-0.5, 0.0, -0.1], "max": [0.5, 1.0, 0.1]}
        ),
        "capsule_center_m": [0.0, 0.5, 0.0],
        "capsule": {"radius_m": 0.12, "height_m": 0.65},
        "harness_sha256": "e" * 64,
        "nine_view_set": [
            "front",
            "left",
            "rear",
            "right",
            "side",
            "three_quarter",
            "three_quarter_front",
            "three_quarter_rear",
            "top",
        ],
        "framing": {
            "fov_degrees": 38.0,
            "min_screen_fraction": 0.55,
            "max_screen_fraction": 0.75,
            "target_screen_fraction": 0.65,
            "margin_fraction": 0.04,
        },
        "viewport": {"width": 1280, "height": 720},
        "renderer_profile": "gl_compatibility",
        "godot_version": "4.7.2.stable.official.test",
        "candidate_state": "CLOSED",
        "public_status": "UNSUPPORTED",
        "production_eligible": False,
        "glb": "res://Main.tscn",
        "capture_dir": "/tmp/captures",
    }


def test_digest_excludes_transient_paths() -> None:
    base = _sample()
    d1 = candidate_runtime_request_digest(base)
    mutated = copy.deepcopy(base)
    mutated["glb"] = "res://other.tscn"
    mutated["capture_dir"] = "/other"
    assert candidate_runtime_request_digest(mutated) == d1


def test_digest_rejects_bool_attempt_counter() -> None:
    bad = _sample()
    bad["strict_attempt_number"] = True
    with pytest.raises(CandidateRuntimeDigestError):
        canonicalize_candidate_runtime_request_for_digest(bad)


def test_digest_changes_when_rest_aabb_changes() -> None:
    a = _sample()
    b = copy.deepcopy(a)
    b["rest_aabb"]["max"][1] = 1.0000001
    assert candidate_runtime_request_digest(a) != candidate_runtime_request_digest(b)


def test_blind_echo_digest_fails_verification() -> None:
    req = _sample()
    req["request_digest"] = "f" * 64
    assert candidate_runtime_request_digest(req) != req["request_digest"]


def test_strict_runtime_int_preserves_max_safe_value() -> None:
    assert strict_runtime_int(JSON_SAFE_INTEGER_MAX, "revision") == JSON_SAFE_INTEGER_MAX


def test_strict_runtime_int_rejects_max_plus_one_int() -> None:
    with pytest.raises(CandidateRuntimeDigestError):
        strict_runtime_int(JSON_SAFE_INTEGER_MAX + 1, "revision")


def test_strict_runtime_int_rejects_unsafe_float_rounding() -> None:
    with pytest.raises(CandidateRuntimeDigestError):
        strict_runtime_int(float(JSON_SAFE_INTEGER_MAX + 1), "revision")


def test_json_safe_max_reexported_from_core_domain() -> None:
    assert JSON_SAFE_INTEGER_MAX == CORE_JSON_SAFE_INTEGER_MAX


def test_adjacent_revision_values_change_digest() -> None:
    a = _sample()
    b = copy.deepcopy(a)
    b["revision"] = JSON_SAFE_INTEGER_MAX
    a["revision"] = JSON_SAFE_INTEGER_MAX - 1
    assert candidate_runtime_request_digest(a) != candidate_runtime_request_digest(b)
