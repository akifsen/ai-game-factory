"""Real Blender integration for bounded V0.7 static character processing."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest

import gamefactory.adapters.dcc.character_processor as character_processor_module
from gamefactory.adapters.assets.glb_validator import _read_glb_bytes
from gamefactory.adapters.assets.v07_geometry_validation import (
    validate_v07_geometry,
)
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.dcc.character_processor import CharacterProcessor, CharacterProcessResult
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import (
    AssetSpecificationV07,
    parse_asset_specification_v07,
)
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    builtin_registry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import DccFailedError
from gamefactory.core.execution.process_runner import CommandResult


def _profile() -> AssetProfileV07:
    base = builtin_registry().get("static_prop").document.model_dump(mode="json")
    data = {
        "schema_version": "asset-profile-0.7.0",
        "profile_id": "test_blender_character",
        "version": 1,
        "categories": ["character"],
        "review_views": ["front"],
        "framing": base["framing"],
        "dimension_rules": {"min_m": 0.1, "max_m": 10.0},
        "processing": {
            "lod0_required": True,
            "lod1_required": True,
            "allowed_lod_policies": ["lod0_lod1"],
            "allowed_collider_policies": ["capsule"],
            "allowed_origin_policies": ["bottom_center"],
            "default_origin_policy": "bottom_center",
            "dimension_tolerance_m": 0.02,
            "snap_grid_m": None,
            "rig_forbidden": True,
            "animation_forbidden": True,
            "max_materials": 8,
            "max_texture_dimension": 4096,
            "max_triangles_lod0": 20000,
        },
        "godot": base["godot"],
        "runtime": base["runtime"],
        "geometry_mode": "single_mesh",
        "accepted_source_kinds": ["provider_generated"],
        "assembly": None,
    }
    return AssetProfileV07(parse_profile_document_v07(data))


def _spec(profile: AssetProfileV07) -> AssetSpecificationV07:
    data: dict[str, Any] = {
        "schema_version": "0.7.0",
        "asset_id": "test_humanoid_character",
        "category": "character",
        "profile": profile.profile_id,
        "profile_version": profile.version,
        "intent": "translated static geometry processor fixture",
        "source_kind": "provider_generated",
        "dimensions": {"width_m": 0.6, "height_m": 1.8, "depth_m": 0.4},
        "orientation": {"up": "+Y", "front": "-Z"},
        "origin_policy": "bottom_center",
        "lod_policy": "lod0_lod1",
        "geometry_budget": {
            "max_triangles_lod0": 20000,
            "max_triangles_lod1": 10000,
            "lod_ratio": 0.5,
        },
        "material_budget": {"max_materials": 8},
        "texture_budget": {"max_dimension": 4096},
        "collider": {"policy": "capsule", "capsule": {"radius_m": 0.3, "height_m": 1.7}},
    }
    registry = ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))
    return parse_asset_specification_v07(data, registry=registry)


def _source(path: Path) -> None:
    create_box_glb(
        width_m=0.6,
        depth_m=0.4,
        height_m=1.8,
        mesh_name="source_character_block",
        origin="center",
        include_collider=False,
        output_path=path,
    )
    raw = path.read_bytes()
    json_length = struct.unpack_from("<I", raw, 12)[0]
    document = json.loads(raw[20 : 20 + json_length].decode("utf-8").rstrip())
    binary = raw[20 + json_length + 8 :]
    mesh_id = document["nodes"][0]["mesh"]
    document["nodes"] = [
        {
            "name": "source_body",
            "mesh": mesh_id,
            "translation": [0.0, 0.45, 0.0],
            "scale": [1.0, 0.5, 1.0],
        },
        {
            "name": "source_head",
            "mesh": mesh_id,
            "translation": [0.0, 1.35, 0.0],
            "scale": [0.5, 0.5, 0.75],
        },
    ]
    document["scenes"] = [{"nodes": [0, 1]}]
    document["scene"] = 0
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    binary += b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def _blender_available() -> bool:
    return BlenderAdapter().detect_tool().available


class _FailingRunner:
    def run(self, request: Any) -> CommandResult:
        return CommandResult(exit_code=17, stdout="", stderr="simulated initial DCC failure")


class _BoolScaleReportRunner:
    def __init__(
        self,
        source_output: Path,
        source_report: Path,
        *,
        corrupt_scale: bool = True,
        scale_value: Any = True,
    ) -> None:
        self.source_output = source_output
        self.source_report = source_report
        self.corrupt_scale = corrupt_scale
        self.scale_value = scale_value

    def run(self, request: Any) -> CommandResult:
        arguments = list(request.args)
        output_path = Path(arguments[arguments.index("--output-glb") + 1])
        report_path = Path(arguments[arguments.index("--report-path") + 1])
        contract_path = Path(arguments[arguments.index("--contract") + 1])
        output_path.write_bytes(self.source_output.read_bytes())
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        report = json.loads(self.source_report.read_text(encoding="utf-8"))
        report["attempt_id"] = contract["attempt_id"]
        if self.corrupt_scale:
            report["transform"]["uniform_scale"] = self.scale_value
        report_path.write_text(json.dumps(report), encoding="utf-8")
        return CommandResult(exit_code=0, stdout="", stderr="")


@pytest.mark.skipif(not _blender_available(), reason="Blender executable not available")
def test_real_blender_processes_unrigged_character_and_preserves_raw_source(tmp_path: Path) -> None:
    profile = _profile()
    spec = _spec(profile)
    raw = tmp_path / "character-source.glb"
    _source(raw)
    raw_before = raw.read_bytes()
    raw_hash = hashlib.sha256(raw_before).hexdigest()
    blender_executable = BlenderAdapter().detect_tool().executable_path
    assert blender_executable
    retry_output = tmp_path / "character-processed.glb"
    retry_report = tmp_path / "test_humanoid_character_character_report.json"
    failing = CharacterProcessor(
        blender_executable=blender_executable,
        runner=_FailingRunner(),  # type: ignore[arg-type]
        dependency_preflight=False,
    )
    with pytest.raises(DccFailedError, match="exit 17"):
        failing.process_character(
            raw,
            spec,
            profile,
            expected_raw_glb_sha256=raw_hash,
            processed_glb_path=retry_output,
            report_path=retry_report,
        )
    assert not retry_output.exists() and not retry_report.exists()
    assert not list(tmp_path.glob(".*.contract.json"))
    processor = CharacterProcessor(blender_executable=blender_executable)

    result = processor.process_character(
        raw,
        spec,
        profile,
        expected_raw_glb_sha256=raw_hash,
        processed_glb_path=retry_output,
        report_path=retry_report,
    )

    assert isinstance(result, CharacterProcessResult)
    assert result.status == "SUCCESS"
    assert result.raw_glb_sha256 == raw_hash
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == raw_hash
    assert result.uniform_scale == pytest.approx(1.0, abs=1e-5)
    assert result.translation_gltf_m == pytest.approx((0.0, 0.0, 0.0), abs=1e-5)
    assert result.processed_glb_path.is_file() and result.report_path.is_file()
    assert not list(tmp_path.glob(".*.contract.json"))
    assert not list(tmp_path.glob(".*.stage.glb"))
    assert not list(tmp_path.glob(".*.stage.json"))
    assert (
        hashlib.sha256(result.processed_glb_path.read_bytes()).hexdigest()
        == result.processed_glb_sha256
    )
    assert hashlib.sha256(result.report_path.read_bytes()).hexdigest() == result.report_sha256
    decoded = validate_v07_geometry(result.processed_glb_path, spec, profile)
    assert decoded.passed, [finding.message for finding in decoded.findings if not finding.passed]
    processed_document, _ = _read_glb_bytes(
        result.processed_glb_path.read_bytes(), 50 * 1024 * 1024
    )
    names = {node.get("name") for node in processed_document["nodes"]}
    assert f"SM_{spec.asset_id}_LOD0" in names
    assert f"SM_{spec.asset_id}_LOD1" in names
    assert not any(str(name).startswith("COL_") for name in names)
    assert result.report_data["runtime_collider"] == {
        "policy": "capsule",
        "radius_m": 0.3,
        "height_m": 1.7,
    }
    assert "source_glb_bytes" not in json.dumps(result.to_dict())
    with pytest.raises(TypeError):
        result.report_data["status"] = "tampered"  # type: ignore[index]


@pytest.mark.skipif(not _blender_available(), reason="Blender executable not available")
def test_two_fresh_character_runs_have_matching_geometry_and_transform(tmp_path: Path) -> None:
    profile = _profile()
    spec = _spec(profile)
    raw = tmp_path / "character-source.glb"
    _source(raw)
    raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    processor = CharacterProcessor()
    results = [
        processor.process_character(
            raw,
            spec,
            profile,
            expected_raw_glb_sha256=raw_hash,
            processed_glb_path=tmp_path / f"run-{index}.glb",
            report_path=tmp_path / f"run-{index}.json",
        )
        for index in range(2)
    ]
    for result in results:
        validation = validate_v07_geometry(result.processed_glb_path, spec, profile)
        assert validation.passed
    assert results[0].uniform_scale == pytest.approx(results[1].uniform_scale, abs=1e-7)
    assert results[0].translation_gltf_m == pytest.approx(results[1].translation_gltf_m, abs=1e-7)
    assert (
        results[0].report_data["processed_metrics"] == results[1].report_data["processed_metrics"]
    )


@pytest.mark.skipif(not _blender_available(), reason="Blender executable not available")
@pytest.mark.parametrize("forged_scale", [True, 10**400])
def test_rejects_invalid_uniform_scale_in_fully_bound_valid_output_report(
    tmp_path: Path, forged_scale: Any
) -> None:
    profile = _profile()
    spec = _spec(profile)
    raw = tmp_path / "character-source.glb"
    _source(raw)
    raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    blender_executable = BlenderAdapter().detect_tool().executable_path
    assert blender_executable

    baseline = CharacterProcessor(blender_executable=blender_executable).process_character(
        raw,
        spec,
        profile,
        expected_raw_glb_sha256=raw_hash,
        processed_glb_path=tmp_path / "baseline.glb",
        report_path=tmp_path / "baseline.json",
    )
    assert validate_v07_geometry(baseline.processed_glb_path, spec, profile).passed

    forged = CharacterProcessor(
        blender_executable=blender_executable,
        runner=_BoolScaleReportRunner(
            baseline.processed_glb_path,
            baseline.report_path,
            scale_value=forged_scale,
        ),  # type: ignore[arg-type]
        dependency_preflight=False,
    )
    with pytest.raises(DccFailedError, match="transformation report"):
        forged.process_character(
            raw,
            spec,
            profile,
            expected_raw_glb_sha256=raw_hash,
            processed_glb_path=tmp_path / "forged.glb",
            report_path=tmp_path / "forged.json",
        )
    assert not (tmp_path / "forged.glb").exists()
    assert not (tmp_path / "forged.json").exists()


@pytest.mark.skipif(not _blender_available(), reason="Blender executable not available")
@pytest.mark.parametrize(
    ("failure_target", "replace_with_foreign"),
    [("output", False), ("report", False), ("output", True)],
)
def test_publication_verification_failure_cleans_only_owned_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_target: str,
    replace_with_foreign: bool,
) -> None:
    profile = _profile()
    spec = _spec(profile)
    raw = tmp_path / "character-source.glb"
    _source(raw)
    raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    blender_executable = BlenderAdapter().detect_tool().executable_path
    assert blender_executable
    baseline = CharacterProcessor(blender_executable=blender_executable).process_character(
        raw,
        spec,
        profile,
        expected_raw_glb_sha256=raw_hash,
        processed_glb_path=tmp_path / "baseline.glb",
        report_path=tmp_path / "baseline.json",
    )
    assert validate_v07_geometry(baseline.processed_glb_path, spec, profile).passed

    output = tmp_path / "published.glb"
    report = tmp_path / "published.json"
    original_identity = character_processor_module._identity
    target = output if failure_target == "output" else report
    injected = False

    def fail_after_publication(path: Path, label: str, maximum: int = 50 * 1024 * 1024) -> Any:
        nonlocal injected
        identity = original_identity(path, label, maximum)
        if path == target and not injected:
            injected = True
            if replace_with_foreign:
                path.unlink()
                path.write_bytes(b"foreign replacement")
            raise DccFailedError("injected published path identity failure")
        return identity

    monkeypatch.setattr(character_processor_module, "_identity", fail_after_publication)
    processor = CharacterProcessor(
        blender_executable=blender_executable,
        runner=_BoolScaleReportRunner(
            baseline.processed_glb_path,
            baseline.report_path,
            corrupt_scale=False,
        ),  # type: ignore[arg-type]
        dependency_preflight=False,
    )
    with pytest.raises(DccFailedError, match="injected published path identity failure"):
        processor.process_character(
            raw,
            spec,
            profile,
            expected_raw_glb_sha256=raw_hash,
            processed_glb_path=output,
            report_path=report,
        )
    assert injected
    if replace_with_foreign:
        assert target.read_bytes() == b"foreign replacement"
    else:
        assert not output.exists()
    assert not report.exists() or (failure_target == "report" and replace_with_foreign)
