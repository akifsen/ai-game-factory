"""Integration tests for Blender asset processing pipeline (Sections 12-17, 32, 48)."""

import json
import os
import struct
from pathlib import Path

import pytest

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.dcc.blender_processor import (
    BLENDER_PYTHON_FAILURE_EXIT_CODE,
    BlenderAssetProcessor,
)
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import parse_asset_specification
from gamefactory.core.domain.errors import DccFailedError
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

_LOD1_ENVELOPE_TOLERANCE_M = 0.01
_PROCESS_ASSET_REL = Path("src/gamefactory/resources/blender/process_asset.py")


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _resolve_blender_executable() -> str | None:
    override = os.environ.get("GAMEFACTORY_BLENDER_PATH")
    if override and Path(override).is_file():
        return override
    detection = BlenderAdapter().detect_tool()
    return detection.executable_path if detection.available else None


def _max_lod_extrema_drift_m(
    ref_min: list[float],
    ref_max: list[float],
    cur_min: list[float],
    cur_max: list[float],
) -> tuple[float, int]:
    worst_axis = 0
    worst = 0.0
    for axis in range(3):
        drift = max(
            abs(cur_min[axis] - ref_min[axis]),
            abs(cur_max[axis] - ref_max[axis]),
        )
        if drift > worst:
            worst = drift
            worst_axis = axis
    return worst, worst_axis


def _assert_bounds_within_tolerance(
    ref_min: list[float],
    ref_max: list[float],
    cur_min: list[float],
    cur_max: list[float],
    tolerance_m: float,
) -> None:
    for axis in range(3):
        assert abs(cur_min[axis] - ref_min[axis]) <= tolerance_m
        assert abs(cur_max[axis] - ref_max[axis]) <= tolerance_m


def _asymmetric_fin_regression_spec():
    base = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    values = base.model_dump(mode="python")
    values["geometry_budget"]["lod_ratio"] = 0.05
    return parse_asset_specification(values)


def _create_asymmetric_fin_raw_glb(
    output_path: Path,
    *,
    mesh_name: str = "SM_asymmetric_fin_probe",
    fin_steps: int = 6,
) -> None:
    """Raw GLB: low-poly box plus a dense +X fin (decimation drops fin extrema before align)."""
    hw, hd = 0.45, 0.45
    y_min, y_max = 0.0, 1.0
    vertices: list[list[float]] = []
    indices: list[int] = []

    def add_vertex(x: float, y: float, z: float) -> int:
        vertices.append([x, y, z])
        return len(vertices) - 1

    def add_quad(a: int, b: int, c: int, d: int) -> None:
        indices.extend((a, b, c, a, c, d))

    [
        add_vertex(-hw, y_min, -hd),
        add_vertex(hw, y_min, -hd),
        add_vertex(hw, y_min, hd),
        add_vertex(-hw, y_min, hd),
        add_vertex(-hw, y_max, -hd),
        add_vertex(hw, y_max, -hd),
        add_vertex(hw, y_max, hd),
        add_vertex(-hw, y_max, hd),
    ]
    for face in (
        (0, 1, 2, 3),
        (4, 6, 5, 7),
        (0, 4, 5, 1),
        (2, 6, 7, 3),
        (0, 3, 7, 4),
        (1, 5, 6, 2),
    ):
        add_quad(*face)

    fin_x0, fin_x1 = hw + 0.02, hw + 0.55
    fin_z = 0.0
    grid: list[list[int]] = []
    for row in range(fin_steps + 1):
        y = y_min + (y_max - y_min) * row / fin_steps
        grid.append(
            [
                add_vertex(fin_x0 + (fin_x1 - fin_x0) * col / fin_steps, y, fin_z)
                for col in range(fin_steps + 1)
            ]
        )
    for row in range(fin_steps):
        for col in range(fin_steps):
            add_quad(
                grid[row][col],
                grid[row][col + 1],
                grid[row + 1][col + 1],
                grid[row + 1][col],
            )

    min_pos = [
        min(v[0] for v in vertices),
        min(v[1] for v in vertices),
        min(v[2] for v in vertices),
    ]
    max_pos = [
        max(v[0] for v in vertices),
        max(v[1] for v in vertices),
        max(v[2] for v in vertices),
    ]
    pos_bytes = b"".join(struct.pack("<fff", *v) for v in vertices)
    idx_bytes = b"".join(struct.pack("<H", i) for i in indices)
    json_doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": mesh_name, "mesh": 0}],
        "meshes": [
            {
                "name": mesh_name,
                "primitives": [
                    {
                        "attributes": {"POSITION": 0},
                        "indices": 1,
                        "material": 0,
                    }
                ],
            }
        ],
        "materials": [
            {"name": "M_probe", "pbrMetallicRoughness": {"baseColorFactor": [0.5, 0.5, 0.5, 1.0]}}
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": len(vertices),
                "type": "VEC3",
                "min": min_pos,
                "max": max_pos,
            },
            {
                "bufferView": 1,
                "componentType": 5123,
                "count": len(indices),
                "type": "SCALAR",
                "min": [0],
                "max": [len(vertices) - 1],
            },
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes), "target": 34962},
            {
                "buffer": 0,
                "byteOffset": len(pos_bytes),
                "byteLength": len(idx_bytes),
                "target": 34963,
            },
        ],
        "buffers": [{"byteLength": len(pos_bytes) + len(idx_bytes)}],
    }
    bin_chunk = pos_bytes + idx_bytes
    pad = (4 - (len(bin_chunk) % 4)) % 4
    if pad:
        bin_chunk += b"\x00" * pad
    json_chunk = json.dumps(json_doc, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(bin_chunk), 0x004E4942)
        + bin_chunk
    )


def _rewrite_glb(source: Path, destination: Path, mutate) -> None:
    raw = source.read_bytes()
    json_length = struct.unpack_from("<I", raw, 12)[0]
    document = json.loads(raw[20 : 20 + json_length].decode("utf-8").rstrip())
    bin_offset = 20 + json_length + 8
    binary = raw[bin_offset:]
    mutate(document)
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary)
    destination.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


@pytest.fixture
def blender_available() -> bool:
    detect = BlenderAdapter().detect_tool()
    return detect.available


def test_blender_process_asset_end_to_end(tmp_path: Path, blender_available: bool) -> None:
    if not blender_available:
        pytest.skip("Blender executable not available on host")

    raw_glb = tmp_path / "raw.glb"
    # Create raw GLB with dimensions 2.0 x 1.5 x 1.8 (different from target 1.2 x 1.0 x 1.0)
    create_box_glb(width_m=2.0, depth_m=1.5, height_m=1.8, output_path=raw_glb)
    raw_size_before = raw_glb.stat().st_size
    raw_bytes_before = raw_glb.read_bytes()

    spec_path = Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    spec = parse_asset_specification(spec_path)

    processed_glb = tmp_path / "out dir" / "processed.glb"
    report_file = tmp_path / "out dir" / "report.json"

    processor = BlenderAssetProcessor()
    result = processor.process_asset(
        raw_glb_path=raw_glb,
        processed_glb_path=processed_glb,
        spec=spec,
        report_path=report_file,
        timeout_seconds=45.0,
    )

    assert result.status == "SUCCESS"
    assert result.exit_code == 0
    assert processed_glb.is_absolute()
    assert " " in processed_glb.name or " " in str(processed_glb.parent)
    assert processed_glb.is_file()
    assert processed_glb.stat().st_size > 0
    assert report_file.is_file()

    # Invariant: Raw GLB must remain byte-for-byte immutable
    assert raw_glb.read_bytes() == raw_bytes_before
    assert raw_glb.stat().st_size == raw_size_before

    # Verify report metrics
    report = result.report_data
    assert "processed_metrics" in report
    metrics = report["processed_metrics"]
    assert "lod0_triangles" in metrics
    assert "lod1_triangles" in metrics
    assert "collider_triangles" in metrics
    assert metrics["lod1_triangles"] <= metrics["lod0_triangles"]
    assert "COL_prop_energy_crate_01" in metrics.get("nodes", [])
    validation = validate_glb(processed_glb, spec)
    assert validation.status.value == "PASS", validation.to_dict()
    assert result.raw_glb_sha256 != result.processed_glb_sha256
    assert len(result.script_sha256) == 64

    # Mutate actual Blender output copies: geometry-derived rules must catch both.
    no_collider = tmp_path / "mutated-no-collider.glb"

    def remove_collider(document) -> None:
        collider = next(n for n in document["nodes"] if n["name"] == f"COL_{spec.asset_id}")
        mesh_id = collider["mesh"]
        node_id = document["nodes"].index(collider)
        document["nodes"].pop(node_id)
        document["scenes"][0]["nodes"].remove(node_id)
        for root_index, value in enumerate(document["scenes"][0]["nodes"]):
            if value > node_id:
                document["scenes"][0]["nodes"][root_index] -= 1
        document["meshes"].pop(mesh_id)
        for node in document["nodes"]:
            if node.get("mesh", len(document["meshes"])) > mesh_id:
                node["mesh"] -= 1

    _rewrite_glb(processed_glb, no_collider, remove_collider)
    no_collider_rules = {
        f.rule_id: f.severity.value for f in validate_glb(no_collider, spec).findings
    }
    assert no_collider_rules["collider.geometry"] == "FAIL"

    oversized = tmp_path / "mutated-scale-x10.glb"

    def scale_visual(document) -> None:
        node = next(n for n in document["nodes"] if n["name"] == f"SM_{spec.asset_id}_LOD0")
        node["scale"] = [10.0, 10.0, 10.0]

    _rewrite_glb(processed_glb, oversized, scale_visual)
    oversized_rules = {f.rule_id: f.severity.value for f in validate_glb(oversized, spec).findings}
    assert oversized_rules["scale.bounds"] == "FAIL"


def test_blender_python_exception_exits_with_configured_code(
    tmp_path: Path, blender_available: bool
) -> None:
    if not blender_available:
        pytest.skip("Blender executable not available on host")
    detection = BlenderAdapter().detect_tool()
    assert detection.executable_path
    script = tmp_path / "raise_processing_error.py"
    script.write_text("raise RuntimeError('factory-probe')\n", encoding="utf-8")
    result = ProcessRunner().run(
        CommandRequest(
            args=[
                detection.executable_path,
                "--background",
                "--factory-startup",
                "--python-exit-code",
                str(BLENDER_PYTHON_FAILURE_EXIT_CODE),
                "--python",
                str(script),
            ],
            cwd=tmp_path,
            timeout_seconds=60,
        )
    )
    assert result.exit_code == BLENDER_PYTHON_FAILURE_EXIT_CODE
    assert result.exit_code != 0
    combined = f"{result.stdout}\n{result.stderr}"
    assert "Traceback (most recent call last):" in combined
    assert "factory-probe" in combined


def test_blender_process_nonexistent_raw_raises(tmp_path: Path, blender_available: bool) -> None:
    if not blender_available:
        pytest.skip("Blender executable not available on host")

    processor = BlenderAssetProcessor()
    spec = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    with pytest.raises(DccFailedError, match="Input raw GLB does not exist"):
        processor.process_asset(
            raw_glb_path=tmp_path / "missing.glb",
            processed_glb_path=tmp_path / "out.glb",
            spec=spec,
        )


def test_real_blender_lod1_envelope_preserved_after_asymmetric_decimation(
    tmp_path: Path,
    blender_available: bool,
) -> None:
    """Regression: asymmetric decimation must align LOD1 extrema to LOD0 within ±0.01 m."""
    if not blender_available:
        pytest.skip("Blender executable not available on host")
    blender_exe = _resolve_blender_executable()
    if not blender_exe:
        pytest.skip("Blender executable not available on host")

    spec = _asymmetric_fin_regression_spec()
    raw = tmp_path / "asymmetric-fin-raw.glb"
    processed = tmp_path / "asymmetric-fin-processed.glb"
    report_path = tmp_path / "asymmetric-fin-report.json"
    _create_asymmetric_fin_raw_glb(raw, mesh_name=f"SM_{spec.asset_id}", fin_steps=6)
    raw_bytes_before = raw.read_bytes()

    result = BlenderAssetProcessor(blender_exe).process_asset(
        raw,
        processed,
        spec,
        report_path=report_path,
        timeout_seconds=45.0,
    )
    assert result.status == "SUCCESS", result.stderr
    assert raw.read_bytes() == raw_bytes_before

    metrics = result.report_data.get("processed_metrics", {})
    lod0_min = metrics["lod0_renderable_bounds_min"]
    lod0_max = metrics["lod0_renderable_bounds_max"]
    pre_min = metrics["lod1_pre_align_bounds_min"]
    pre_max = metrics["lod1_pre_align_bounds_max"]
    post_min = metrics["lod1_post_align_bounds_min"]
    post_max = metrics["lod1_post_align_bounds_max"]

    pre_drift_m, pre_axis = _max_lod_extrema_drift_m(lod0_min, lod0_max, pre_min, pre_max)
    assert pre_drift_m > _LOD1_ENVELOPE_TOLERANCE_M, (
        f"expected pre-align LOD1 drift > {_LOD1_ENVELOPE_TOLERANCE_M}m on synthetic fin "
        f"(axis {pre_axis}, drift {pre_drift_m:.6f}m)"
    )
    _assert_bounds_within_tolerance(
        lod0_min, lod0_max, post_min, post_max, _LOD1_ENVELOPE_TOLERANCE_M
    )
    assert metrics.get("lod1_envelope_aligned") is True
    assert metrics["lod1_triangles"] < metrics["lod0_triangles"]

    validation = validate_glb(processed, spec)
    rules = {f.rule_id: f.severity.value for f in validation.findings}
    assert rules.get("lod1.bounds") == "PASS", validation.to_dict()
    assert validation.status.value == "PASS", validation.to_dict()


def test_real_blender_lod1_align_rejects_collapsed_decimation_axis(
    tmp_path: Path, blender_available: bool
) -> None:
    """Collapsed LOD1 axis on a wide LOD0 envelope must fail without mutating LOD0."""
    if not blender_available:
        pytest.skip("Blender executable not available on host")
    blender_exe = _resolve_blender_executable()
    if not blender_exe:
        pytest.skip("Blender executable not available on host")

    result_path = tmp_path / "degenerate_axis_probe.json"
    probe = tmp_path / "degenerate_axis_probe.py"
    process_asset_rel = _PROCESS_ASSET_REL.as_posix()
    probe.write_text(
        f"""
import importlib.util
import json
import os
from pathlib import Path

repo_root = Path(os.environ["GAMEFACTORY_REPO_ROOT"])
output_path = Path(os.environ["GAMEFACTORY_PROBE_OUTPUT"])
spec = importlib.util.spec_from_file_location(
    "process_asset_under_test",
    repo_root / "{process_asset_rel}",
)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

import bpy

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.5))
lod0 = bpy.context.object
lod0.scale = (0.25, 0.25, 1.0)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
lod0_snapshot = [tuple(v.co) for v in lod0.data.vertices]

bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.5))
lod1 = bpy.context.object
lod1.scale = (1e-9, 1.0, 1.0)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)

error_message = None
try:
    module._align_lod1_envelope_to_lod0(lod0, lod1)
except RuntimeError as exc:
    error_message = str(exc)

lod0_after = [tuple(v.co) for v in lod0.data.vertices]
output_path.write_text(
    json.dumps(
        {{
            "error_message": error_message,
            "lod0_unchanged": lod0_snapshot == lod0_after,
        }}
    ),
    encoding="utf-8",
)
""",
        encoding="utf-8",
    )
    repo_root = _repository_root()
    run = ProcessRunner().run(
        CommandRequest(
            args=[
                blender_exe,
                "--background",
                "--factory-startup",
                "--python-exit-code",
                str(BLENDER_PYTHON_FAILURE_EXIT_CODE),
                "--python",
                str(probe),
            ],
            cwd=repo_root,
            env_overrides={
                "GAMEFACTORY_REPO_ROOT": str(repo_root),
                "GAMEFACTORY_PROBE_OUTPUT": str(result_path),
            },
            timeout_seconds=60.0,
        )
    )
    assert run.exit_code == 0, f"{run.stdout}\n{run.stderr}"
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["lod0_unchanged"] is True
    assert payload["error_message"] is not None
    assert "collapsed axis" in payload["error_message"]


def test_real_blender_glb_axis_mapping_with_unequal_height_and_depth(tmp_path: Path) -> None:
    detection = BlenderAdapter().detect_tool()
    if not detection.available or not detection.executable_path:
        pytest.skip("Blender executable is unavailable")
    base = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    values = base.model_dump(mode="python")
    values["dimensions"] = {"width_m": 1.2, "depth_m": 0.8, "height_m": 1.6}
    spec = parse_asset_specification(values)
    raw = tmp_path / "axis-probe-raw.glb"
    processed = tmp_path / "axis-probe-processed.glb"
    create_box_glb(
        width_m=2.0,
        depth_m=1.7,
        height_m=2.4,
        mesh_name=f"SM_{spec.asset_id}",
        collider_name=f"COL_{spec.asset_id}",
        output_path=raw,
    )
    result = BlenderAssetProcessor(detection.executable_path).process_asset(raw, processed, spec)
    validation = validate_glb(processed, spec)
    assert result.exit_code == 0
    assert validation.status.value == "PASS", validation.to_dict()
    dimensions = json.loads(
        next(f for f in validation.findings if f.rule_id == "scale.bounds").actual
    )
    assert all(
        abs(actual - expected) <= 0.01
        for actual, expected in zip(dimensions, [1.2, 1.6, 0.8], strict=True)
    )


def test_real_blender_dependency_preflight(blender_available: bool) -> None:
    if not blender_available:
        pytest.skip("Blender executable not available on host")
    detection = BlenderAdapter().detect_tool()
    assert detection.executable_path

    from gamefactory.adapters.dcc.blender_environment import (
        BLENDER_PYTHONPATH_ENV,
        parse_blender_python_paths,
        run_blender_dependency_preflight,
    )

    # Same explicit contract as production: only the Factory variable is honored.
    preflight = run_blender_dependency_preflight(
        detection.executable_path,
        ProcessRunner(sanitize_output=True),
        python_paths=parse_blender_python_paths(os.environ.get(BLENDER_PYTHONPATH_ENV)),
    )
    assert preflight.status == "PASS"
    assert preflight.blender_version is not None
    assert preflight.python_executable is not None
    assert preflight.modules["numpy"]["available"] is True
    assert preflight.runtime_modules.get("ctypes", {}).get("available") is True
    assert preflight.exit_code == 0


def test_real_blender_contaminated_prefix_negative_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not os.environ.get("GAMEFACTORY_TEST_BLENDER") or not os.environ.get(
        "GAMEFACTORY_TEST_CONTAMINATING_PYTHON_BIN"
    ):
        pytest.skip(
            "Opt-in test requires GAMEFACTORY_TEST_BLENDER and "
            "GAMEFACTORY_TEST_CONTAMINATING_PYTHON_BIN environment variables"
        )
    contaminating_bin = os.environ["GAMEFACTORY_TEST_CONTAMINATING_PYTHON_BIN"]
    current_path = os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", f"{contaminating_bin}{os.pathsep}{current_path}")

    detection = BlenderAdapter().detect_tool()
    if not detection.available or not detection.executable_path:
        pytest.skip("Blender executable not available on host")

    from gamefactory.adapters.dcc.blender_environment import (
        BLENDER_PYTHONPATH_ENV,
        parse_blender_python_paths,
        run_blender_dependency_preflight,
    )

    preflight = run_blender_dependency_preflight(
        detection.executable_path,
        ProcessRunner(sanitize_output=True),
        python_paths=parse_blender_python_paths(os.environ.get(BLENDER_PYTHONPATH_ENV)),
    )
    assert preflight.status == "FAIL"
    assert preflight.reason_code == "BLENDER_PYTHON_RUNTIME_UNAVAILABLE"
    prefix = preflight.python_prefix or ""
    assert not prefix.startswith("/usr")


def test_real_blender_runtime_integrity_positive_integration() -> None:
    if not os.environ.get("GAMEFACTORY_TEST_BLENDER"):
        pytest.skip("Opt-in test requires GAMEFACTORY_TEST_BLENDER environment variable")

    detection = BlenderAdapter().detect_tool()
    if not detection.available or not detection.executable_path:
        pytest.skip("Blender executable not available on host")

    from gamefactory.adapters.dcc.blender_environment import (
        BLENDER_PYTHONPATH_ENV,
        parse_blender_python_paths,
        run_blender_dependency_preflight,
    )

    preflight = run_blender_dependency_preflight(
        detection.executable_path,
        ProcessRunner(sanitize_output=True),
        python_paths=parse_blender_python_paths(os.environ.get(BLENDER_PYTHONPATH_ENV)),
    )
    assert preflight.status == "PASS"
    assert preflight.reason_code is None
    assert preflight.runtime_modules.get("ctypes", {}).get("available") is True
