"""Independent boundary and report-binding tests for the V0.7 character Godot proof."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from test_character_processor import _profile, _read_glb, _source, _spec, _write_glb

from gamefactory.adapters.dcc.godot_character import (
    _actual_glb_facts,
    _validate_character_inputs,
    _validate_observation,
    verify_godot_character,
)
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_profiles import AssetProfileV07, parse_profile_document_v07
from gamefactory.core.domain.camera_framing import PLACED_VIEWS
from gamefactory.core.domain.errors import ValidationError


def _character_profile() -> Any:
    profile = _profile()
    document = profile.document.model_dump(mode="json")
    document["review_views"] = ["front", "rear", "left", "right", "three_quarter"]
    return AssetProfileV07(parse_profile_document_v07(document))


def _with_views(profile: Any, views: list[str]) -> Any:
    document = profile.document.model_dump(mode="json")
    document["review_views"] = views
    return AssetProfileV07(parse_profile_document_v07(document))


def _with_godot(profile: Any, **updates: Any) -> Any:
    document = profile.document.model_dump(mode="json")
    document["godot"].update(updates)
    return AssetProfileV07(parse_profile_document_v07(document))


def _processed(path: Path, spec: Any, *, root: str = "ROOT") -> Path:
    create_box_glb(
        width_m=spec.dimensions.width_m,
        height_m=spec.dimensions.height_m,
        depth_m=spec.dimensions.depth_m,
        mesh_name=f"SM_{spec.asset_id}_LOD0",
        origin=spec.origin_policy,
        output_path=path,
    )
    document, binary = _read_glb(path)
    document["meshes"].append(dict(document["meshes"][0]))
    document["meshes"][0]["name"] = f"SM_{spec.asset_id}_LOD0"
    document["meshes"][1]["name"] = f"SM_{spec.asset_id}_LOD1"
    document["nodes"] = [
        {"name": root, "children": [1, 2]},
        {"name": f"SM_{spec.asset_id}_LOD0", "mesh": 0},
        {"name": f"SM_{spec.asset_id}_LOD1", "mesh": 1},
    ]
    document["scenes"] = [{"nodes": [0]}]
    document["scene"] = 0
    _write_glb(path, document, binary)
    return path


class _NoCallRunner:
    calls = 0

    def run(self, _request: Any) -> Any:
        self.calls += 1
        raise AssertionError("Godot must not launch after deterministic input rejection")


def test_packaged_character_runtime_harness_is_available() -> None:
    from gamefactory.adapters.dcc.godot_character import _harness_resource

    raw = _harness_resource()
    assert len(raw) > 1000
    assert b"character-runtime-observation-0.7.0" in raw
    assert b"CapsuleShape3D" in raw


def test_exact_bound_provider_character_contract_and_capsule_are_required(tmp_path: Path) -> None:
    profile = _character_profile()
    spec = _spec(profile)
    assert _validate_character_inputs(spec, profile)[0] is spec

    mismatched = _profile(profile_id="different_character_profile")
    with pytest.raises(ValidationError, match="specification-bound"):
        verify_godot_character(
            spec, mismatched, tmp_path / "raw.glb", "0" * 64,
            tmp_path / "processed.glb", "0" * 64, "exec-profile",
        )
    with pytest.raises(ValidationError, match="exact typed"):
        _validate_character_inputs(object(), profile)
    wrong_source = spec.model_copy(update={"source_kind": "local_operator_assembly"})
    with pytest.raises(ValidationError, match="provider_generated"):
        _validate_character_inputs(wrong_source, profile)
    with pytest.raises(ValidationError, match="provider_generated"):
        _validate_character_inputs(spec.model_copy(update={"parts": []}), profile)


@pytest.mark.parametrize(
    "updates",
    [
        {"body_kind": "area", "require_ray_hit": False, "require_area": True},
        {"require_ray_hit": False},
    ],
)
def test_character_profile_must_match_static_capsule_runtime_contract_without_dispatch(
    updates: dict[str, Any],
) -> None:
    valid_profile = _character_profile()
    profile = _with_godot(valid_profile, **updates)
    spec = _spec(profile)
    runner = _NoCallRunner()
    with pytest.raises(ValidationError, match="provider_generated"):
        verify_godot_character(
            spec, profile, "unused-raw.glb", "0" * 64,
            "unused-processed.glb", "0" * 64, "exec-profile-contract", runner=runner,
        )
    assert runner.calls == 0


def test_processed_glb_requires_exact_lods_identity_transforms_and_capsule_only(tmp_path: Path) -> None:
    profile = _character_profile()
    spec = _spec(profile)
    processed = _processed(tmp_path / "processed.glb", spec)
    facts = _actual_glb_facts(processed.read_bytes(), spec, profile, processed)
    assert facts["mesh_names"] == [f"SM_{spec.asset_id}_LOD0", f"SM_{spec.asset_id}_LOD1"]
    assert facts["dimensions"] == pytest.approx([0.6, 1.8, 0.4])

    document, binary = _read_glb(processed)
    document["nodes"][1]["translation"] = [0.1, 0.0, 0.0]
    _write_glb(processed, document, binary)
    with pytest.raises(ValidationError, match="identity local transform"):
        _actual_glb_facts(processed.read_bytes(), spec, profile, processed)

    _processed(processed, spec)
    document, binary = _read_glb(processed)
    document["nodes"].append({"name": "COL_test_character", "mesh": 0})
    document["scenes"][0]["nodes"].append(3)
    _write_glb(processed, document, binary)
    with pytest.raises(ValidationError, match="inventory|V0.7 geometry"):
        _actual_glb_facts(processed.read_bytes(), spec, profile, processed)


@pytest.mark.parametrize("forbidden", ["skins", "animations", "camera", "parts"])
def test_forbidden_rig_or_assembly_content_fails_before_engine_dispatch(
    tmp_path: Path, forbidden: str
) -> None:
    profile = _character_profile()
    spec = _spec(profile)
    source = tmp_path / "raw.glb"
    _source(source)
    processed = _processed(tmp_path / "processed.glb", spec)
    document, binary = _read_glb(processed)
    if forbidden == "skins":
        document["skins"] = [{"joints": [1]}]
    elif forbidden == "animations":
        document["animations"] = [{}]
    elif forbidden == "camera":
        document["cameras"] = [{}]
        document["nodes"].append({"name": "Camera", "camera": 0})
        document["scenes"][0]["nodes"].append(3)
    else:
        document["nodes"].append({"name": "PART_body"})
        document["scenes"][0]["nodes"].append(3)
    _write_glb(processed, document, binary)
    runner = _NoCallRunner()
    with pytest.raises(ValidationError):
        verify_godot_character(
            spec,
            profile,
            source,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            processed,
            hashlib.sha256(processed.read_bytes()).hexdigest(),
            "exec-forbidden",
            runner=runner,
        )
    assert runner.calls == 0


@pytest.mark.parametrize("bad_field", ["raw_pin", "processed_pin", "attempt_bool", "attempt_zero"])
def test_hash_and_attempt_guards_prevent_engine_launch(tmp_path: Path, bad_field: str) -> None:
    profile = _character_profile()
    spec = _spec(profile)
    source = tmp_path / "raw.glb"
    _source(source)
    processed = _processed(tmp_path / "processed.glb", spec)
    raw_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    processed_hash = hashlib.sha256(processed.read_bytes()).hexdigest()
    if bad_field == "raw_pin":
        raw_hash = "0" * 64
    elif bad_field == "processed_pin":
        processed_hash = "f" * 64
    attempt: Any = True if bad_field == "attempt_bool" else 0 if bad_field == "attempt_zero" else 1
    runner = _NoCallRunner()
    with pytest.raises(ValidationError):
        verify_godot_character(
            spec, profile, source, raw_hash, processed, processed_hash,
            "exec-invalid", attempt_number=attempt, runner=runner,
        )
    assert runner.calls == 0


def test_review_views_are_profile_declared_and_empty_or_duplicates_reject() -> None:
    profile = _character_profile()
    spec = _spec(profile)
    from gamefactory.adapters.dcc.godot_assembly import _validate_review_views

    for invalid in ([], ["front", "front"], ["unknown"], [""]):
        with pytest.raises(ValidationError):
            _validate_review_views(invalid)  # type: ignore[arg-type]
    assert _validate_character_inputs(spec, profile)[2] == ["front", "rear", "left", "right", "three_quarter"]
    all_views_profile = _with_views(profile, sorted(PLACED_VIEWS))
    assert _validate_character_inputs(_spec(all_views_profile), all_views_profile)[2] == sorted(PLACED_VIEWS)
    unsupported_profile = _with_views(profile, ["front"])
    with pytest.raises(ValidationError, match="required coverage"):
        _validate_character_inputs(_spec(unsupported_profile), unsupported_profile)


def test_failed_observation_still_requires_exact_hash_and_attempt_bindings() -> None:
    profile = _character_profile()
    spec = _spec(profile)
    request = {
        "workflow_id": "workflow-1",
        "revision": 1,
        "asset_id": spec.asset_id,
        "execution_id": "execution-1",
        "attempt_number": 2,
        "raw_glb_sha256": "a" * 64,
        "processed_glb_sha256": "b" * 64,
        "harness_sha256": "c" * 64,
        "profile_sha256": "d" * 64,
        "specification_sha256": "e" * 64,
        "request_digest": "f" * 64,
    }
    observation = {
        **request,
        "schema_version": "character-runtime-observation-0.7.0",
        "status": "FAIL",
        "errors": ["controlled fixture failure"],
    }
    _validate_observation(
        observation,
        spec=spec,
        profile=profile,
        views=["front"],
        request=request,
        facts={},
    )
    for key, tampered in (("request_digest", "0" * 64), ("attempt_number", True), ("revision", True)):
        changed = {**observation, key: tampered}
        with pytest.raises(ValidationError):
            _validate_observation(
                changed,
                spec=spec,
                profile=profile,
                views=["front"],
                request=request,
                facts={},
            )

