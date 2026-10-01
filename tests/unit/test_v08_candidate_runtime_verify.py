"""Independent verification of candidate runtime observations."""

from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from gamefactory.adapters.assets.internal_rig_canonical import reviewed_text_sha256
from gamefactory.adapters.assets.v08_candidate_runtime_digest import (
    candidate_runtime_bound_payload_canonical,
    candidate_runtime_request_digest,
    candidate_runtime_rest_aabb_canonical_sha256,
)
from gamefactory.adapters.assets.v08_candidate_runtime_request import (
    build_bound_candidate_runtime_request,
)
from gamefactory.adapters.assets.v08_candidate_runtime_verify import (
    CandidateRuntimeObservationError,
    verify_candidate_runtime_observation,
)
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.core.domain.camera_framing import (
    BoundsAABB,
    framing_geometry,
    framing_metrics_from_projected_rect,
    view_axis_label,
)
from gamefactory.core.domain.v08_candidate_contracts import (
    candidate_spec_fingerprint,
    load_packaged_candidate_profile,
    load_packaged_candidate_specification,
    parse_asset_specification_v08_candidate,
)
from gamefactory.core.domain.v08_candidate_runtime_contract import (
    load_packaged_candidate_runtime_contract,
)
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner


class _FakeRunner(ProcessRunner):
    def run(self, request: CommandRequest) -> CommandResult:
        if "--version" in request.args:
            return CommandResult(
                exit_code=0, stdout="4.7.2.stable.official.test\n", stderr="", timed_out=False
            )
        return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)


def _spec_for_glb(path: Path):
    spec = load_packaged_candidate_specification()
    data = spec.model_dump(mode="json")
    data["processed_glb_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return parse_asset_specification_v08_candidate(data)


def _write_nonblank_png(path: Path, *, width: int = 1280, height: int = 720) -> None:
    image = Image.new("RGB", (width, height), (40, 44, 52))
    for x in range(0, width, 64):
        for y in range(0, height, 64):
            image.putpixel((x, y), ((x + y) % 255, 80, 120))
    image.save(path, format="PNG")


def _build_bound_and_harness(glb_path: Path) -> tuple[dict, Path]:
    spec = _spec_for_glb(glb_path)
    profile = load_packaged_candidate_profile()
    harness = Path(
        str(resources.files("gamefactory.resources.godot").joinpath("candidate_capsule_harness.gd"))
    )
    bound = build_bound_candidate_runtime_request(
        glb_path,
        spec,
        profile,
        workflow_id="WF-TEST",
        revision=1,
        execution_id="EXEC-1",
        strict_attempt_number=1,
        godot_executable=Path("godot"),
        runner=_FakeRunner(),
        harness_path=harness,
    )
    return bound, harness


def _view_framing_from_bound(bound: dict) -> dict:
    contract = load_packaged_candidate_runtime_contract()
    mins = bound["rest_aabb"]["min"]
    maxs = bound["rest_aabb"]["max"]
    bounds = BoundsAABB(
        min_x=mins[0],
        min_y=mins[1],
        min_z=mins[2],
        max_x=maxs[0],
        max_y=maxs[1],
        max_z=maxs[2],
    )
    viewport = (contract.viewport.width, contract.viewport.height)
    framing_policy = contract.framing
    out: dict = {}
    for view in bound["nine_view_set"]:
        geom = framing_geometry(
            bounds,
            view,
            fov_degrees=framing_policy.fov_degrees,
            target_screen_fraction=framing_policy.target_screen_fraction,
            viewport=viewport,
        )
        # Synthetic unit fixture: pixel rect aligned with independent framing_geometry.
        vp_w, vp_h = viewport
        rect_h = geom.vertical_fraction * vp_h
        rect_w = geom.horizontal_fraction * vp_w
        rect_x = (vp_w - rect_w) * 0.5
        rect_y = (vp_h - rect_h) * 0.5
        metrics = framing_metrics_from_projected_rect(
            x=rect_x,
            y=rect_y,
            width=rect_w,
            height=rect_h,
            viewport_width=float(vp_w),
            viewport_height=float(vp_h),
            margin_fraction=framing_policy.margin_fraction,
        )
        out[view] = {
            "ok": True,
            "reason": "",
            "height_ratio": metrics.height_ratio,
            "fill_ratio": metrics.fill_ratio,
            "horizontally_centered": metrics.horizontally_centered,
            "view_axis": view_axis_label(view),
            "camera_distance": geom.distance,
            "projected_rect_pixels": {
                "x": rect_x,
                "y": rect_y,
                "width": rect_w,
                "height": rect_h,
            },
            "center_offset": metrics.center_offset,
        }
    return out


def _observation_from_bound(bound: dict, *, harness_raw: str = "0" * 64) -> dict:
    return {
        "schema_version": "candidate-runtime-observation-0.8.0",
        "workflow_id": bound["workflow_id"],
        "revision": bound["revision"],
        "execution_id": bound["execution_id"],
        "strict_attempt_number": bound["strict_attempt_number"],
        "asset_id": bound["asset_id"],
        "processed_glb_sha256": bound["processed_glb_sha256"],
        "observed_glb_sha256": bound["processed_glb_sha256"],
        "request_digest": bound["request_digest"],
        "rest_aabb_canonical_sha256": bound["rest_aabb_canonical_sha256"],
        "godot_version": bound["godot_version"],
        "harness_sha256": bound["harness_sha256"],
        "harness_sha256_raw": harness_raw,
        "status": "PASS",
        "candidate_state": "CLOSED",
        "public_status": "UNSUPPORTED",
        "production_eligible": False,
        "visual_mesh_name": "SM_HumanoidSkin",
        "runtime_body_kind": "static_body",
        "collision_shape_class": "CapsuleShape3D",
        "physics_ray_hit": True,
        "rest_mesh_bounds": bound["rest_aabb"],
        "capsule": {
            "shape_class": "CapsuleShape3D",
            "observed_radius_m": bound["capsule"]["radius_m"],
            "observed_height_m": bound["capsule"]["height_m"],
            "center_m": bound["capsule_center_m"],
        },
        "view_framing": _view_framing_from_bound(bound),
        "captures": [],
        "errors": [],
    }


def _populate_captures(tmp_path: Path, bound: dict, observation: dict) -> None:
    contract = load_packaged_candidate_runtime_contract()
    width = contract.viewport.width
    height = contract.viewport.height
    captures = []
    for view in bound["nine_view_set"]:
        png_path = tmp_path / f"{view}.png"
        _write_nonblank_png(png_path, width=width, height=height)
        raw = png_path.read_bytes()
        from gamefactory.adapters.engines.godot_image import decode_png

        digest = decode_png(raw, width, height).sha256
        captures.append(
            {
                "view": view,
                "png_sha256": digest,
                "png_width": width,
                "png_height": height,
                "execution_id": bound["execution_id"],
                "revision": bound["revision"],
                "strict_attempt_number": bound["strict_attempt_number"],
                "request_digest": bound["request_digest"],
            }
        )
    observation["captures"] = captures


def _verify_kwargs(
    tmp_path: Path,
    glb_path: Path,
    bound: dict,
    observation: dict,
    harness: Path,
    *,
    glb_bytes: bytes | None = None,
    spec=None,
) -> dict:
    spec = spec or _spec_for_glb(glb_path)
    profile = load_packaged_candidate_profile()
    _, harness_raw = reviewed_text_sha256(harness)
    observation = observation.copy()
    observation["harness_sha256_raw"] = harness_raw
    bound = bound.copy()
    if "bound_payload_canonical" not in bound:
        bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(bound)
    return verify_candidate_runtime_observation(
        observation,
        bound,
        spec=spec,
        profile=profile,
        capture_dir=tmp_path,
        glb_path=glb_path,
        glb_bytes=glb_bytes or glb_path.read_bytes(),
        harness_path=harness,
    )


def test_positive_fake_glb_passes_with_trusted_recompute(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    _populate_captures(tmp_path, bound, observation)
    result = _verify_kwargs(tmp_path, glb_path, bound, observation, harness)
    assert result["integrity_outcome"] == "VERIFIED"
    assert result["execution_provenance"] == "CONSISTENT_BUT_UNAUTHENTICATED"


def test_forged_non_glb_bytes_fail_even_with_matching_hash(tmp_path: Path) -> None:
    valid_path = tmp_path / "valid.glb"
    valid_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(valid_path)
    forged = b"not even a GLB"
    forged_path = tmp_path / "forged.glb"
    forged_path.write_bytes(forged)
    forged_spec_data = load_packaged_candidate_specification().model_dump(mode="json")
    forged_spec_data["processed_glb_sha256"] = hashlib.sha256(forged).hexdigest()
    forged_spec = parse_asset_specification_v08_candidate(forged_spec_data)
    bound = dict(bound)
    bound["processed_glb_sha256"] = hashlib.sha256(forged).hexdigest()
    bound["spec_fingerprint"] = candidate_spec_fingerprint(forged_spec)
    bound["rest_aabb"] = {"min": [100.0, 100.0, 100.0], "max": [101.0, 101.0, 101.0]}
    bound["capsule_center_m"] = [100.5, 100.5, 100.5]
    bound["rest_aabb_canonical_sha256"] = candidate_runtime_rest_aabb_canonical_sha256(
        bound["rest_aabb"]
    )
    bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(bound)
    bound["request_digest"] = candidate_runtime_request_digest(bound)
    observation = _observation_from_bound(bound)
    observation["processed_glb_sha256"] = bound["processed_glb_sha256"]
    observation["observed_glb_sha256"] = bound["processed_glb_sha256"]
    observation["request_digest"] = bound["request_digest"]
    observation["rest_mesh_bounds"] = bound["rest_aabb"]
    observation["capsule"]["center_m"] = bound["capsule_center_m"]
    observation["rest_aabb_canonical_sha256"] = bound["rest_aabb_canonical_sha256"]
    observation["view_framing"] = _view_framing_from_bound(bound)
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(
        CandidateRuntimeObservationError, match="static validation|GLB-derived|rest_aabb"
    ):
        _verify_kwargs(
            tmp_path,
            forged_path,
            bound,
            observation,
            harness,
            glb_bytes=forged,
            spec=forged_spec,
        )


def test_forged_rest_aabb_fails_after_full_rehash(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    bound = dict(bound)
    bound["rest_aabb"] = {"min": [100.0, 100.0, 100.0], "max": [101.0, 101.0, 101.0]}
    bound["capsule_center_m"] = [100.5, 100.5, 100.5]
    bound["rest_aabb_canonical_sha256"] = candidate_runtime_rest_aabb_canonical_sha256(
        bound["rest_aabb"]
    )
    bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(bound)
    bound["request_digest"] = candidate_runtime_request_digest(bound)
    observation = _observation_from_bound(bound)
    observation["rest_mesh_bounds"] = bound["rest_aabb"]
    observation["capsule"]["center_m"] = bound["capsule_center_m"]
    observation["rest_aabb_canonical_sha256"] = bound["rest_aabb_canonical_sha256"]
    observation["request_digest"] = bound["request_digest"]
    observation["view_framing"] = _view_framing_from_bound(bound)
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(CandidateRuntimeObservationError, match="rest_aabb|GLB-derived"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_pass_with_errors(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    observation["errors"] = ["physics ray missed"]
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(CandidateRuntimeObservationError, match="errors"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_unknown_observation_field(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    observation["unexpected"] = True
    with pytest.raises(CandidateRuntimeObservationError, match="schema rejected"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_bool_revision(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    bound = dict(bound)
    bound["revision"] = True
    with pytest.raises(CandidateRuntimeObservationError, match="schema rejected"):
        _verify_kwargs(tmp_path, glb_path, bound, _observation_from_bound(bound), harness)


def test_rejects_copied_harness_path_even_with_matching_hash(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    copied = tmp_path / "copied_harness.gd"
    copied.write_bytes(harness.read_bytes())
    observation = _observation_from_bound(bound)
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(CandidateRuntimeObservationError, match="packaged reviewed"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, copied)


def test_shifted_rest_aabb_with_rehash_fails_canonical_binding(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    bound = dict(bound)
    mins = bound["rest_aabb"]["min"]
    maxs = bound["rest_aabb"]["max"]
    bound["rest_aabb"] = {
        "min": [mins[0] + 1e-3, mins[1], mins[2]],
        "max": maxs,
    }
    bound["capsule_center_m"] = [
        (bound["rest_aabb"]["min"][0] + maxs[0]) / 2.0,
        (mins[1] + maxs[1]) / 2.0,
        (mins[2] + maxs[2]) / 2.0,
    ]
    bound["rest_aabb_canonical_sha256"] = candidate_runtime_rest_aabb_canonical_sha256(
        bound["rest_aabb"]
    )
    bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(bound)
    bound["request_digest"] = candidate_runtime_request_digest(bound)
    observation = _observation_from_bound(bound)
    observation["rest_mesh_bounds"] = bound["rest_aabb"]
    observation["capsule"]["center_m"] = bound["capsule_center_m"]
    observation["rest_aabb_canonical_sha256"] = bound["rest_aabb_canonical_sha256"]
    observation["request_digest"] = bound["request_digest"]
    observation["view_framing"] = _view_framing_from_bound(bound)
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(CandidateRuntimeObservationError, match="GLB-derived|rest_aabb"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_synthetic_positive_fixture_is_simulated_not_godot_provenance(
    tmp_path: Path,
) -> None:
    """Proves verifier negatives are exercised against a clearly synthetic observation."""
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    bound["workflow_id"] = "WF-SYNTHETIC-UNIT-NOT-REAL-GODOT"
    bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(bound)
    bound["request_digest"] = candidate_runtime_request_digest(bound)
    observation = _observation_from_bound(bound)
    observation["workflow_id"] = bound["workflow_id"]
    observation["request_digest"] = bound["request_digest"]
    _populate_captures(tmp_path, bound, observation)
    result = _verify_kwargs(tmp_path, glb_path, bound, observation, harness)
    assert result["execution_provenance"] == "CONSISTENT_BUT_UNAUTHENTICATED"


def test_rejects_extra_view_framing_key(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    observation["view_framing"]["bogus_view"] = observation["view_framing"]["front"]
    with pytest.raises(CandidateRuntimeObservationError, match="schema rejected|nine contract"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_tampered_capture_png(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    _populate_captures(tmp_path, bound, observation)
    first = bound["nine_view_set"][0]
    png_path = tmp_path / f"{first}.png"
    png_path.write_bytes(png_path.read_bytes() + b"tamper")
    with pytest.raises(CandidateRuntimeObservationError, match="png_sha256"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_substituted_profile_hash_even_after_rehash(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    bound = dict(bound)
    bound["profile_document_hash"] = "0" * 64
    bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(bound)
    bound["request_digest"] = candidate_runtime_request_digest(bound)
    observation = _observation_from_bound(bound)
    observation["request_digest"] = bound["request_digest"]
    with pytest.raises(CandidateRuntimeObservationError, match="profile_document_hash"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_tiny_midpoint_shift_after_full_rehash(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    bound = dict(bound)
    shifted = list(bound["capsule_center_m"])
    shifted[0] += 1e-7
    bound["capsule_center_m"] = shifted
    bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(bound)
    bound["request_digest"] = candidate_runtime_request_digest(bound)
    observation = _observation_from_bound(bound)
    observation["capsule"]["center_m"] = shifted
    observation["request_digest"] = bound["request_digest"]
    observation["view_framing"] = _view_framing_from_bound(bound)
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(CandidateRuntimeObservationError, match="GLB-derived|midpoint|rest_aabb"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_foreign_execution_id(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    observation["execution_id"] = "EXEC-FOREIGN"
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(CandidateRuntimeObservationError, match="execution_id"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_foreign_revision(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    observation["revision"] = bound["revision"] + 1
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(CandidateRuntimeObservationError, match="revision"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_foreign_strict_attempt(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    observation["strict_attempt_number"] = bound["strict_attempt_number"] + 1
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(CandidateRuntimeObservationError, match="strict_attempt_number"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_duplicate_capture_view(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    _populate_captures(tmp_path, bound, observation)
    observation["captures"][1] = observation["captures"][0].copy()
    observation["captures"][1]["view"] = observation["captures"][0]["view"]
    with pytest.raises(CandidateRuntimeObservationError, match="duplicate|invalid"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_missing_capture_png(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    _populate_captures(tmp_path, bound, observation)
    (tmp_path / f"{bound['nine_view_set'][0]}.png").unlink()
    with pytest.raises(CandidateRuntimeObservationError, match="missing PNG"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_wrong_capture_dimensions(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    _populate_captures(tmp_path, bound, observation)
    observation["captures"][0]["png_width"] = 640
    with pytest.raises(CandidateRuntimeObservationError, match="dimensions"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_altered_capsule_radius(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    observation["capsule"]["observed_radius_m"] = bound["capsule"]["radius_m"] * 1.01
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(CandidateRuntimeObservationError, match="radius"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_physics_ray_miss(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    observation["physics_ray_hit"] = False
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(CandidateRuntimeObservationError, match="physics ray"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_contradictory_framing_ok_flag(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    observation["view_framing"]["front"]["ok"] = False
    observation["view_framing"]["front"]["reason"] = ""
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(CandidateRuntimeObservationError, match="status|framing|ok"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_corrupt_observation_json(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    with pytest.raises(CandidateRuntimeObservationError, match="schema rejected"):
        verify_candidate_runtime_observation(
            {"schema_version": "candidate-runtime-observation-0.8.0", "not": "complete"},
            bound,
            spec=_spec_for_glb(glb_path),
            profile=load_packaged_candidate_profile(),
            capture_dir=tmp_path,
            glb_path=glb_path,
            glb_bytes=glb_path.read_bytes(),
            harness_path=harness,
        )


def test_rejects_monkeypatched_runtime_contract_while_pin_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    _populate_captures(tmp_path, bound, observation)
    contract = load_packaged_candidate_runtime_contract()
    mutated = contract.model_copy(update={"renderer_profile": "forward_plus"})
    monkeypatch.setattr(
        "gamefactory.adapters.assets.v08_candidate_runtime_verify.load_packaged_candidate_runtime_contract",
        lambda: mutated,
    )
    with pytest.raises(CandidateRuntimeObservationError, match="renderer|contract|framing"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def _set_view_framing_rect(
    entry: dict,
    *,
    rect_x: float,
    rect_y: float,
    rect_w: float,
    rect_h: float,
    viewport: tuple[int, int],
    margin_fraction: float,
    framing_policy: Any,
) -> None:
    vp_w, vp_h = viewport
    metrics = framing_metrics_from_projected_rect(
        x=rect_x,
        y=rect_y,
        width=rect_w,
        height=rect_h,
        viewport_width=float(vp_w),
        viewport_height=float(vp_h),
        margin_fraction=margin_fraction,
    )
    fill = metrics.fill_ratio
    policy_ok = (
        metrics.inside_margin
        and framing_policy.min_screen_fraction <= fill <= framing_policy.max_screen_fraction
        and metrics.horizontally_centered
    )
    entry.update(
        {
            "height_ratio": metrics.height_ratio,
            "fill_ratio": metrics.fill_ratio,
            "center_offset": metrics.center_offset,
            "horizontally_centered": metrics.horizontally_centered,
            "ok": policy_ok,
            "reason": "",
            "projected_rect_pixels": {
                "x": rect_x,
                "y": rect_y,
                "width": rect_w,
                "height": rect_h,
            },
        }
    )


def test_rejects_self_consistent_rect_fill_below_geometry_at_correct_distance(
    tmp_path: Path,
) -> None:
    """Broad-policy fill 0.56 can pass metrics while geometry at the claimed distance expects ~0.65."""
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    contract = load_packaged_candidate_runtime_contract()
    viewport = (contract.viewport.width, contract.viewport.height)
    vp_w, vp_h = viewport
    view = "front"
    entry = observation["view_framing"][view]
    assert entry["camera_distance"] > 0
    target_fill = 0.56
    rect_h = target_fill * vp_h
    rect_w = target_fill * vp_w
    _set_view_framing_rect(
        entry,
        rect_x=(vp_w - rect_w) * 0.5,
        rect_y=(vp_h - rect_h) * 0.5,
        rect_w=rect_w,
        rect_h=rect_h,
        viewport=viewport,
        margin_fraction=contract.framing.margin_fraction,
        framing_policy=contract.framing,
    )
    assert entry["ok"] is True
    assert entry["fill_ratio"] == pytest.approx(0.56, abs=1e-6)
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(
        CandidateRuntimeObservationError,
        match="projected_rect height fraction|independent framing geometry",
    ):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_width_only_rect_tamper_with_geometry_aligned_height(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    observation = _observation_from_bound(bound)
    contract = load_packaged_candidate_runtime_contract()
    viewport = (contract.viewport.width, contract.viewport.height)
    vp_w, vp_h = viewport
    mins = bound["rest_aabb"]["min"]
    maxs = bound["rest_aabb"]["max"]
    bounds = BoundsAABB(
        min_x=mins[0],
        min_y=mins[1],
        min_z=mins[2],
        max_x=maxs[0],
        max_y=maxs[1],
        max_z=maxs[2],
    )
    view = "front"
    geom = framing_geometry(
        bounds,
        view,
        fov_degrees=contract.framing.fov_degrees,
        target_screen_fraction=contract.framing.target_screen_fraction,
        viewport=viewport,
    )
    entry = observation["view_framing"][view]
    assert entry["camera_distance"] == pytest.approx(geom.distance, rel=0, abs=5e-3)
    rect_h = geom.vertical_fraction * vp_h
    wrong_width_frac = geom.horizontal_fraction * 0.85
    rect_w = wrong_width_frac * vp_w
    _set_view_framing_rect(
        entry,
        rect_x=(vp_w - rect_w) * 0.5,
        rect_y=(vp_h - rect_h) * 0.5,
        rect_w=rect_w,
        rect_h=rect_h,
        viewport=viewport,
        margin_fraction=contract.framing.margin_fraction,
        framing_policy=contract.framing,
    )
    assert entry["height_ratio"] == pytest.approx(geom.vertical_fraction, abs=1e-6)
    assert entry["fill_ratio"] == pytest.approx(geom.vertical_fraction, abs=1e-6)
    _populate_captures(tmp_path, bound, observation)
    with pytest.raises(
        CandidateRuntimeObservationError,
        match="projected_rect width fraction|independent framing geometry",
    ):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)


def test_rejects_substituted_runtime_contract_hash(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    bound, harness = _build_bound_and_harness(glb_path)
    bound = dict(bound)
    bound["runtime_contract_sha256"] = "0" * 64
    bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(bound)
    bound["request_digest"] = candidate_runtime_request_digest(bound)
    observation = _observation_from_bound(bound)
    observation["request_digest"] = bound["request_digest"]
    with pytest.raises(CandidateRuntimeObservationError, match="runtime_contract"):
        _verify_kwargs(tmp_path, glb_path, bound, observation, harness)
