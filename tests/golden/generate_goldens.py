"""Deterministic golden generator for V0.4-V0.6 backward compatibility.

Freezes externally observable V0.4-V0.6 behavior before any V0.7 refactors:
- validate_glb ordered findings for static_prop@1, pickup@1, modular_piece@1
- spec_fingerprint and model_dump for representative 0.4.0 and 0.5.0 specs
- profile contracts for all built-in profiles
- builtin_registry().availability() and parsed profile documents
"""

from __future__ import annotations

import json
import struct
import tempfile
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    parse_asset_specification,
    spec_fingerprint,
)
from gamefactory.core.domain.asset_profiles import (
    AssetProfile,
    builtin_registry,
)

GOLDEN_FILE = Path(__file__).parent / "golden_data.json"


def _document(raw: bytes) -> tuple[dict[str, Any], bytes]:
    json_len = struct.unpack_from("<I", raw, 12)[0]
    doc = json.loads(raw[20 : 20 + json_len].decode().rstrip())
    bin_start = 20 + json_len + 8
    return doc, raw[bin_start:]


def _write_glb(path: Path, doc: dict[str, Any], binary: bytes) -> None:
    json_data = json.dumps(doc, separators=(",", ":")).encode()
    json_data += b" " * ((-len(json_data)) % 4)
    binary += b"\0" * ((-len(binary)) % 4)
    total = 12 + 8 + len(json_data) + 8 + len(binary)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_data), 0x4E4F534A)
        + json_data
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def normalize_validation_result(result: Any) -> dict[str, Any]:
    """Normalize nondeterministic fields (artifact path, evaluated_at).

    Every normalization is documented:
    - artifact: replaced with '<ARTIFACT>' because it is a temporary file path
    - evaluated_at: omitted because it is a live timestamp (utc_now_iso)
    All other fields (status, summary, rule_id, severity, expected, actual, message) are preserved.
    """
    return {
        "status": result.status.value,
        "summary": result.summary,
        "findings": [
            {
                "rule_id": f.rule_id,
                "severity": f.severity.value,
                "expected": f.expected,
                "actual": f.actual,
                "artifact": "<ARTIFACT>",
                "message": f.message,
            }
            for f in result.findings
        ],
    }


def generate_glb_cases(work_dir: Path) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}

    # 1. static_prop@1
    sp_spec = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    sp_cases: dict[str, Any] = {}

    # static_prop: pass
    pass_path = work_dir / "sp_pass.glb"
    create_box_glb(
        width_m=sp_spec.dimensions.width_m,
        depth_m=sp_spec.dimensions.depth_m,
        height_m=sp_spec.dimensions.height_m,
        mesh_name=f"SM_{sp_spec.asset_id}",
        collider_name=f"COL_{sp_spec.asset_id}",
        include_lod1=True,
        include_collider=True,
        include_texture=True,
        origin=sp_spec.origin_policy,
        output_path=pass_path,
    )
    sp_cases["pass"] = normalize_validation_result(validate_glb(pass_path, sp_spec))

    # static_prop: missing_lod1
    no_lod1_path = work_dir / "sp_no_lod1.glb"
    create_box_glb(
        width_m=sp_spec.dimensions.width_m,
        depth_m=sp_spec.dimensions.depth_m,
        height_m=sp_spec.dimensions.height_m,
        mesh_name=f"SM_{sp_spec.asset_id}",
        collider_name=f"COL_{sp_spec.asset_id}",
        include_lod1=False,
        include_collider=True,
        include_texture=True,
        origin=sp_spec.origin_policy,
        output_path=no_lod1_path,
    )
    sp_cases["missing_lod1"] = normalize_validation_result(validate_glb(no_lod1_path, sp_spec))

    # static_prop: collider_mismatch (missing collider)
    no_col_path = work_dir / "sp_no_col.glb"
    create_box_glb(
        width_m=sp_spec.dimensions.width_m,
        depth_m=sp_spec.dimensions.depth_m,
        height_m=sp_spec.dimensions.height_m,
        mesh_name=f"SM_{sp_spec.asset_id}",
        collider_name=f"COL_{sp_spec.asset_id}",
        include_lod1=True,
        include_collider=False,
        include_texture=True,
        origin=sp_spec.origin_policy,
        output_path=no_col_path,
    )
    sp_cases["collider_mismatch"] = normalize_validation_result(validate_glb(no_col_path, sp_spec))

    # static_prop: non_identity_transform
    non_ident_path = work_dir / "sp_non_ident.glb"
    doc, binary = _document(pass_path.read_bytes())
    doc["nodes"][0]["translation"] = [0.0, 1.0, 0.0]
    _write_glb(non_ident_path, doc, binary)
    sp_cases["non_identity_transform"] = normalize_validation_result(
        validate_glb(non_ident_path, sp_spec)
    )

    # static_prop: wrong_origin
    wrong_orig_path = work_dir / "sp_wrong_origin.glb"
    create_box_glb(
        width_m=sp_spec.dimensions.width_m,
        depth_m=sp_spec.dimensions.depth_m,
        height_m=sp_spec.dimensions.height_m,
        mesh_name=f"SM_{sp_spec.asset_id}",
        collider_name=f"COL_{sp_spec.asset_id}",
        include_lod1=True,
        include_collider=True,
        include_texture=True,
        origin="center",  # spec is bottom_center
        output_path=wrong_orig_path,
    )
    sp_cases["wrong_origin"] = normalize_validation_result(validate_glb(wrong_orig_path, sp_spec))

    # static_prop: skin_present
    skin_path = work_dir / "sp_skin.glb"
    doc, binary = _document(pass_path.read_bytes())
    doc["skins"] = [{"joints": [0]}]
    _write_glb(skin_path, doc, binary)
    sp_cases["skin_present"] = normalize_validation_result(validate_glb(skin_path, sp_spec))

    # static_prop: animation_present
    anim_path = work_dir / "sp_anim.glb"
    doc, binary = _document(pass_path.read_bytes())
    doc["animations"] = [{"channels": [], "samplers": []}]
    _write_glb(anim_path, doc, binary)
    sp_cases["animation_present"] = normalize_validation_result(validate_glb(anim_path, sp_spec))

    # static_prop: over_budget
    budget_path = work_dir / "sp_budget.glb"
    create_box_glb(
        width_m=sp_spec.dimensions.width_m,
        depth_m=sp_spec.dimensions.depth_m,
        height_m=sp_spec.dimensions.height_m,
        mesh_name=f"SM_{sp_spec.asset_id}",
        collider_name=f"COL_{sp_spec.asset_id}",
        include_lod1=True,
        include_collider=True,
        num_materials=3,  # spec max_materials: 2
        include_texture=True,
        texture_size=(4096, 4096),  # spec max_dimension: 2048
        origin=sp_spec.origin_policy,
        output_path=budget_path,
    )
    sp_cases["over_budget"] = normalize_validation_result(validate_glb(budget_path, sp_spec))

    cases["static_prop"] = sp_cases

    # 2. pickup@1
    pickup_spec = parse_asset_specification(
        Path("src/gamefactory/resources/specs/pickup_energy_cell_01.yml")
    )
    pu_cases: dict[str, Any] = {}

    # pickup: pass
    pu_pass_path = work_dir / "pu_pass.glb"
    create_box_glb(
        width_m=pickup_spec.dimensions.width_m,
        depth_m=pickup_spec.dimensions.depth_m,
        height_m=pickup_spec.dimensions.height_m,
        mesh_name=f"SM_{pickup_spec.asset_id}",
        collider_name=f"COL_{pickup_spec.asset_id}",
        include_lod1=False,
        include_collider=True,
        include_texture=True,
        origin=pickup_spec.origin_policy,
        output_path=pu_pass_path,
    )
    pu_cases["pass"] = normalize_validation_result(validate_glb(pu_pass_path, pickup_spec))

    # pickup: collider_mismatch
    pu_no_col = work_dir / "pu_no_col.glb"
    create_box_glb(
        width_m=pickup_spec.dimensions.width_m,
        depth_m=pickup_spec.dimensions.depth_m,
        height_m=pickup_spec.dimensions.height_m,
        mesh_name=f"SM_{pickup_spec.asset_id}",
        collider_name=f"COL_{pickup_spec.asset_id}",
        include_lod1=False,
        include_collider=False,
        include_texture=True,
        origin=pickup_spec.origin_policy,
        output_path=pu_no_col,
    )
    pu_cases["collider_mismatch"] = normalize_validation_result(
        validate_glb(pu_no_col, pickup_spec)
    )

    # pickup: non_identity_transform
    pu_non_ident = work_dir / "pu_non_ident.glb"
    doc, binary = _document(pu_pass_path.read_bytes())
    doc["nodes"][0]["translation"] = [0.0, 1.0, 0.0]
    _write_glb(pu_non_ident, doc, binary)
    pu_cases["non_identity_transform"] = normalize_validation_result(
        validate_glb(pu_non_ident, pickup_spec)
    )

    # pickup: wrong_origin
    pu_wrong_orig = work_dir / "pu_wrong_origin.glb"
    create_box_glb(
        width_m=pickup_spec.dimensions.width_m,
        depth_m=pickup_spec.dimensions.depth_m,
        height_m=pickup_spec.dimensions.height_m,
        mesh_name=f"SM_{pickup_spec.asset_id}",
        collider_name=f"COL_{pickup_spec.asset_id}",
        include_lod1=False,
        include_collider=True,
        include_texture=True,
        origin="bottom_center",  # spec is center
        output_path=pu_wrong_orig,
    )
    pu_cases["wrong_origin"] = normalize_validation_result(validate_glb(pu_wrong_orig, pickup_spec))

    # pickup: unexpected_lod1 (lod0_only spec, but LOD1 present)
    pu_extra_lod1 = work_dir / "pu_extra_lod1.glb"
    create_box_glb(
        width_m=pickup_spec.dimensions.width_m,
        depth_m=pickup_spec.dimensions.depth_m,
        height_m=pickup_spec.dimensions.height_m,
        mesh_name=f"SM_{pickup_spec.asset_id}",
        collider_name=f"COL_{pickup_spec.asset_id}",
        include_lod1=True,
        include_collider=True,
        include_texture=True,
        origin=pickup_spec.origin_policy,
        output_path=pu_extra_lod1,
    )
    pu_cases["unexpected_lod1"] = normalize_validation_result(
        validate_glb(pu_extra_lod1, pickup_spec)
    )

    # pickup: skin_present
    pu_skin = work_dir / "pu_skin.glb"
    doc, binary = _document(pu_pass_path.read_bytes())
    doc["skins"] = [{"joints": [0]}]
    _write_glb(pu_skin, doc, binary)
    pu_cases["skin_present"] = normalize_validation_result(validate_glb(pu_skin, pickup_spec))

    # pickup: animation_present
    pu_anim = work_dir / "pu_anim.glb"
    doc, binary = _document(pu_pass_path.read_bytes())
    doc["animations"] = [{"channels": [], "samplers": []}]
    _write_glb(pu_anim, doc, binary)
    pu_cases["animation_present"] = normalize_validation_result(validate_glb(pu_anim, pickup_spec))

    # pickup: over_budget
    pu_budget = work_dir / "pu_budget.glb"
    create_box_glb(
        width_m=pickup_spec.dimensions.width_m,
        depth_m=pickup_spec.dimensions.depth_m,
        height_m=pickup_spec.dimensions.height_m,
        mesh_name=f"SM_{pickup_spec.asset_id}",
        collider_name=f"COL_{pickup_spec.asset_id}",
        include_lod1=False,
        include_collider=True,
        num_materials=3,
        include_texture=True,
        texture_size=(4096, 4096),
        origin=pickup_spec.origin_policy,
        output_path=pu_budget,
    )
    pu_cases["over_budget"] = normalize_validation_result(validate_glb(pu_budget, pickup_spec))

    cases["pickup"] = pu_cases

    # 3. modular_piece@1
    modular_spec = parse_asset_specification(
        Path("src/gamefactory/resources/fixtures/wall_panel_test.yml")
    )
    mod_cases: dict[str, Any] = {}

    # modular_piece: pass
    mod_pass_path = work_dir / "mod_pass.glb"
    create_box_glb(
        width_m=modular_spec.dimensions.width_m,
        depth_m=modular_spec.dimensions.depth_m,
        height_m=modular_spec.dimensions.height_m,
        mesh_name=f"SM_{modular_spec.asset_id}",
        collider_name=f"COL_{modular_spec.asset_id}",
        include_lod1=False,
        include_collider=True,
        include_texture=True,
        origin=modular_spec.origin_policy,
        output_path=mod_pass_path,
    )
    mod_cases["pass"] = normalize_validation_result(validate_glb(mod_pass_path, modular_spec))

    # modular_piece: off_grid_dimensions
    mod_off_grid = work_dir / "mod_off_grid.glb"
    create_box_glb(
        width_m=1.1,  # off-grid (snap_grid is 0.25)
        depth_m=modular_spec.dimensions.depth_m,
        height_m=modular_spec.dimensions.height_m,
        mesh_name=f"SM_{modular_spec.asset_id}",
        collider_name=f"COL_{modular_spec.asset_id}",
        include_lod1=False,
        include_collider=True,
        include_texture=True,
        origin=modular_spec.origin_policy,
        output_path=mod_off_grid,
    )
    mod_cases["off_grid_dimensions"] = normalize_validation_result(
        validate_glb(mod_off_grid, modular_spec)
    )

    # modular_piece: collider_mismatch
    mod_no_col = work_dir / "mod_no_col.glb"
    create_box_glb(
        width_m=modular_spec.dimensions.width_m,
        depth_m=modular_spec.dimensions.depth_m,
        height_m=modular_spec.dimensions.height_m,
        mesh_name=f"SM_{modular_spec.asset_id}",
        collider_name=f"COL_{modular_spec.asset_id}",
        include_lod1=False,
        include_collider=False,
        include_texture=True,
        origin=modular_spec.origin_policy,
        output_path=mod_no_col,
    )
    mod_cases["collider_mismatch"] = normalize_validation_result(
        validate_glb(mod_no_col, modular_spec)
    )

    # modular_piece: non_identity_transform
    mod_non_ident = work_dir / "mod_non_ident.glb"
    doc, binary = _document(mod_pass_path.read_bytes())
    doc["nodes"][0]["translation"] = [0.0, 1.0, 0.0]
    _write_glb(mod_non_ident, doc, binary)
    mod_cases["non_identity_transform"] = normalize_validation_result(
        validate_glb(mod_non_ident, modular_spec)
    )

    # modular_piece: wrong_origin
    mod_wrong_orig = work_dir / "mod_wrong_origin.glb"
    create_box_glb(
        width_m=modular_spec.dimensions.width_m,
        depth_m=modular_spec.dimensions.depth_m,
        height_m=modular_spec.dimensions.height_m,
        mesh_name=f"SM_{modular_spec.asset_id}",
        collider_name=f"COL_{modular_spec.asset_id}",
        include_lod1=False,
        include_collider=True,
        include_texture=True,
        origin="center",  # spec is bottom_center
        output_path=mod_wrong_orig,
    )
    mod_cases["wrong_origin"] = normalize_validation_result(
        validate_glb(mod_wrong_orig, modular_spec)
    )

    # modular_piece: skin_present
    mod_skin = work_dir / "mod_skin.glb"
    doc, binary = _document(mod_pass_path.read_bytes())
    doc["skins"] = [{"joints": [0]}]
    _write_glb(mod_skin, doc, binary)
    mod_cases["skin_present"] = normalize_validation_result(validate_glb(mod_skin, modular_spec))

    # modular_piece: animation_present
    mod_anim = work_dir / "mod_anim.glb"
    doc, binary = _document(mod_pass_path.read_bytes())
    doc["animations"] = [{"channels": [], "samplers": []}]
    _write_glb(mod_anim, doc, binary)
    mod_cases["animation_present"] = normalize_validation_result(
        validate_glb(mod_anim, modular_spec)
    )

    # modular_piece: over_budget
    mod_budget = work_dir / "mod_budget.glb"
    create_box_glb(
        width_m=modular_spec.dimensions.width_m,
        depth_m=modular_spec.dimensions.depth_m,
        height_m=modular_spec.dimensions.height_m,
        mesh_name=f"SM_{modular_spec.asset_id}",
        collider_name=f"COL_{modular_spec.asset_id}",
        include_lod1=False,
        include_collider=True,
        num_materials=5,  # modular max_materials is 4
        include_texture=True,
        texture_size=(64, 64),
        origin=modular_spec.origin_policy,
        output_path=mod_budget,
    )
    mod_cases["over_budget"] = normalize_validation_result(validate_glb(mod_budget, modular_spec))

    cases["modular_piece"] = mod_cases
    return cases


def generate_spec_documents() -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {}

    # Representative 0.4.0 spec (static_prop)
    sp_040_path = Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    sp_040 = parse_asset_specification(sp_040_path)
    docs["prop_energy_crate_01_v040"] = {
        "fingerprint": spec_fingerprint(sp_040),
        "model_dump": sp_040.model_dump(mode="json"),
    }

    # Representative 0.5.0 spec (static_prop)
    sp_050_dict = sp_040.model_dump(mode="json")
    sp_050_dict["schema_version"] = "0.5.0"
    sp_050_dict["profile_version"] = 1
    sp_050 = parse_asset_specification(sp_050_dict)
    docs["prop_energy_crate_01_v050"] = {
        "fingerprint": spec_fingerprint(sp_050),
        "model_dump": sp_050.model_dump(mode="json"),
    }

    # Representative 0.5.0 spec (pickup)
    pu_path = Path("src/gamefactory/resources/specs/pickup_energy_cell_01.yml")
    pu_spec = parse_asset_specification(pu_path)
    docs["pickup_energy_cell_01_v050"] = {
        "fingerprint": spec_fingerprint(pu_spec),
        "model_dump": pu_spec.model_dump(mode="json"),
    }

    # Representative 0.5.0 fixture (pickup)
    ep_path = Path("src/gamefactory/resources/fixtures/energy_pickup_test.yml")
    ep_spec = parse_asset_specification(ep_path)
    docs["energy_pickup_test_v050"] = {
        "fingerprint": spec_fingerprint(ep_spec),
        "model_dump": ep_spec.model_dump(mode="json"),
    }

    # Representative 0.5.0 spec (modular_piece)
    mod_path = Path("src/gamefactory/resources/fixtures/wall_panel_test.yml")
    mod_spec = parse_asset_specification(mod_path)
    docs["wall_panel_test_v050"] = {
        "fingerprint": spec_fingerprint(mod_spec),
        "model_dump": mod_spec.model_dump(mode="json"),
    }

    return docs


def generate_profile_contracts() -> dict[str, dict[str, Any]]:
    contracts: dict[str, dict[str, Any]] = {}
    reg = builtin_registry()

    # static_prop@1
    sp_prof: AssetProfile = reg.get("static_prop", 1)
    sp_spec: AssetSpecification = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    contracts["static_prop"] = {
        "processing_contract": sp_prof.processing_contract(sp_spec),
        "scene_contract": sp_prof.scene_contract(),
        "runtime_requirements": sp_prof.runtime_requirements(),
        "capture_request_profile": sp_prof.capture_request_profile(),
    }

    # pickup@1
    pu_prof: AssetProfile = reg.get("pickup", 1)
    pu_spec: AssetSpecification = parse_asset_specification(
        Path("src/gamefactory/resources/specs/pickup_energy_cell_01.yml")
    )
    contracts["pickup"] = {
        "processing_contract": pu_prof.processing_contract(pu_spec),
        "scene_contract": pu_prof.scene_contract(),
        "runtime_requirements": pu_prof.runtime_requirements(),
        "capture_request_profile": pu_prof.capture_request_profile(),
    }

    # modular_piece@1
    mod_prof: AssetProfile = reg.get("modular_piece", 1)
    mod_spec: AssetSpecification = parse_asset_specification(
        Path("src/gamefactory/resources/fixtures/wall_panel_test.yml")
    )
    contracts["modular_piece"] = {
        "processing_contract": mod_prof.processing_contract(mod_spec),
        "scene_contract": mod_prof.scene_contract(),
        "runtime_requirements": mod_prof.runtime_requirements(),
        "capture_request_profile": mod_prof.capture_request_profile(),
    }

    return contracts


def generate_registry_data() -> dict[str, Any]:
    reg = builtin_registry()
    return {
        "availability": reg.availability(),
        "parsed_profiles": {
            prof.profile_id: prof.document.model_dump(mode="json") for prof in reg.available
        },
    }


def generate_all() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        glb_cases = generate_glb_cases(Path(tmp_dir))
    spec_docs = generate_spec_documents()
    profile_contracts = generate_profile_contracts()
    registry_data = generate_registry_data()

    return {
        "glb_validation": glb_cases,
        "spec_documents": spec_docs,
        "profile_contracts": profile_contracts,
        "registry": registry_data,
    }


def main() -> None:
    data = generate_all()
    GOLDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_FILE.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Generated golden data successfully: {GOLDEN_FILE}")


if __name__ == "__main__":
    main()
