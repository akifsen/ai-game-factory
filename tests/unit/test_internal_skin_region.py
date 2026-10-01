"""Region membership and runtime request digest canonicalization (V0.8-2)."""

from __future__ import annotations

import pytest

from gamefactory.adapters.assets.internal_rig_canonical import (
    _godot_compatible_runtime_json_bytes,
    canonicalize_runtime_request_for_digest,
    runtime_request_digest,
)
from gamefactory.adapters.assets.internal_skin_region import (
    REGION_BOUNDARY_TOLERANCE,
    strict_region_boundary_tolerance,
    vertex_inside_region_box,
)
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract

CONTRACT = load_internal_skin_contract()


def _sample_request() -> dict:
    oracle = CONTRACT.oracle
    return {
        "schema_version": "rig-runtime-request-0.8.0",
        "glb_sha256": "a" * 64,
        "contract_sha256": "b" * 64,
        "harness_sha256": "c" * 64,
        "godot_version": "4.7.2.stable.official.test",
        "pose_bone": oracle.pose_bone,
        "rotation_axis": list(oracle.rotation_axis),
        "rotation_degrees": oracle.rotation_degrees,
        "affected": {
            "min": list(oracle.affected.min_xyz),
            "max": list(oracle.affected.max_xyz),
        },
        "unaffected": {
            "min": list(oracle.unaffected.min_xyz),
            "max": list(oracle.unaffected.max_xyz),
        },
        "min_affected_displacement": oracle.min_affected_displacement,
        "max_unaffected_displacement": oracle.max_unaffected_displacement,
        "max_affected_displacement": oracle.max_affected_displacement,
        "vertex_count": 16,
        "affected_vertex_count": 8,
        "unaffected_vertex_count": 8,
        "region_boundary_tolerance": REGION_BOUNDARY_TOLERANCE,
    }


def test_runtime_digest_unchanged_when_counts_are_json_floats() -> None:
    strict = _sample_request()
    loose = {**strict}
    loose["vertex_count"] = 16.0
    loose["affected_vertex_count"] = 8.0
    loose["unaffected_vertex_count"] = 8.0
    assert runtime_request_digest(strict) == runtime_request_digest(loose)


def test_runtime_digest_rejects_weaker_region_tolerance() -> None:
    bad = _sample_request()
    bad["region_boundary_tolerance"] = 0.1
    with pytest.raises(ValueError, match="region_boundary_tolerance"):
        runtime_request_digest(bad)


def test_region_boundary_epsilon_includes_near_max() -> None:
    oracle = CONTRACT.oracle
    box = {
        "min": list(oracle.affected.min_xyz),
        "max": list(oracle.affected.max_xyz),
    }
    y_max = float(oracle.affected.max_xyz[1])
    x_mid = (float(oracle.affected.min_xyz[0]) + float(oracle.affected.max_xyz[0])) / 2.0
    inside = (x_mid, y_max + 5e-7, 0.0)
    outside = (x_mid, y_max + 1e-4, 0.0)
    assert vertex_inside_region_box(inside, box)
    assert not vertex_inside_region_box(outside, box)


def test_canonicalize_runtime_request_for_digest_strict_ints() -> None:
    req = _sample_request()
    req["vertex_count"] = 16.0
    out = canonicalize_runtime_request_for_digest(req)
    assert out["vertex_count"] == 16
    assert type(out["vertex_count"]) is int
    assert strict_region_boundary_tolerance(out["region_boundary_tolerance"]) == (
        REGION_BOUNDARY_TOLERANCE
    )


def test_godot_runtime_json_preserves_unrelated_1e06_substrings() -> None:
    req = _sample_request()
    req["godot_version"] = "offline-1e-06"
    canonical = canonicalize_runtime_request_for_digest(req)
    raw = _godot_compatible_runtime_json_bytes(canonical)
    assert b"offline-1e-06" in raw
    assert b'"region_boundary_tolerance":0.000001' in raw


def test_godot_runtime_json_does_not_rewrite_other_numeric_fields() -> None:
    req = _sample_request()
    req["min_affected_displacement"] = 0.04
    canonical = canonicalize_runtime_request_for_digest(req)
    raw = _godot_compatible_runtime_json_bytes(canonical)
    assert b'"min_affected_displacement":0.04' in raw
    assert runtime_request_digest(req) == runtime_request_digest(_sample_request())


def test_runtime_digest_sensitive_to_nested_region_boundary_tolerance() -> None:
    base = _sample_request()
    with_nested_two = {
        **base,
        "extra": {"region_boundary_tolerance": 2.0},
    }
    with_nested_three = {
        **base,
        "extra": {"region_boundary_tolerance": 3.0},
    }
    d2 = runtime_request_digest(with_nested_two)
    d3 = runtime_request_digest(with_nested_three)
    assert d2 != d3
    raw_two = _godot_compatible_runtime_json_bytes(
        canonicalize_runtime_request_for_digest(with_nested_two)
    )
    assert b'"region_boundary_tolerance":2' in raw_two
    assert raw_two.count(b'"region_boundary_tolerance":0.000001') == 1


def test_canonicalize_preserves_unknown_top_level_nested_values() -> None:
    req = _sample_request()
    req["extra"] = {
        "region_boundary_tolerance": 2.0,
        "label": "pinned-1e-06",
        "ratio": 0.125,
    }
    canonical = canonicalize_runtime_request_for_digest(req)
    assert canonical["extra"] == req["extra"]
    raw = _godot_compatible_runtime_json_bytes(canonical)
    assert b'"region_boundary_tolerance":2' in raw
    assert b'"label":"pinned-1e-06"' in raw
    assert b'"ratio":0.125' in raw
