"""Cold verification and tamper coverage for asset-evidence-0.7.0 assembly bundles.

The bundle is synthesized offline (no Godot), with every hash kept internally
consistent, so each test proves the verifier catches the one semantic defect it
introduces rather than a stale digest.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from gamefactory.adapters.assets.assembly_processor import normalize_assembly
from gamefactory.adapters.assets.glb_validator import composition_for, validate_glb
from gamefactory.adapters.fakes import assembly_generator as ag
from gamefactory.core.domain import transforms as tf
from gamefactory.core.domain.assembly_source import parse_source_registration
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07, spec_fingerprint

VERIFIER = Path("src/gamefactory/resources/scripts/verify_asset_bundle.py")
_spec = importlib.util.spec_from_file_location("bundle_verifier", VERIFIER)
assert _spec is not None and _spec.loader is not None
verifier = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verifier)

WF, REV, EXEC = "WF-ASSET-v07test", 1, "EXEC-v07test"


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1280, 720), (48, 52, 59)).save(buffer, format="PNG")
    return buffer.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _dump(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def build_bundle(
    root: Path,
    *,
    design: ag.AssemblyDesign = ag.VEHICLE_TANK,
    source_front: str = "+Z",
    mutate: Any = None,
) -> Path:
    """Write a consistent assembly bundle; ``mutate(files, docs)`` edits it first."""
    work = root / "work"
    work.mkdir(parents=True)
    spec_dict = ag.assembly_spec(design)
    spec = parse_asset_specification_v07(spec_dict)
    source = ag.assembly_glb(design, source_front=source_front)
    (work / "source.glb").write_bytes(source)
    registration = ag.source_registration(design, source, source_front=source_front)
    record = parse_source_registration(registration)
    result = normalize_assembly(
        work / "source.glb",
        record,
        work / "processed.glb",
        asset_id=spec.asset_id,
        processing_contract=spec.bound_profile().processing_contract(spec),
    )
    validation = validate_glb(work / "processed.glb", spec, normalization=result.record)
    assert validation.status.value == "PASS"
    composition = composition_for(spec)
    normalization = result.record.as_dict()
    docs: dict[str, Any] = {
        "specification": spec.model_dump(mode="json"),
        "source_provenance": record.model_dump(mode="json"),
        "normalization": normalization,
        "processing_report": {**result.report, "status": "SUCCESS"},
        "validation": {
            **validation.to_dict(),
            "schema_version": "asset-validation-report-0.7.0",
            "rule_groups": list(composition.groups),
            "composition": composition.name,
            "normalization": normalization,
        },
    }
    files: dict[str, bytes] = {
        "evidence/source_glb.glb": source,
        "evidence/processed_glb.glb": (work / "processed.glb").read_bytes(),
    }
    profile = spec.bound_profile()
    views = list(profile.review_views)
    for view in views:
        files[f"captures/{view}.png"] = _png()
    if mutate is not None:
        mutate(files, docs)
    processed_sha = _sha(files["evidence/processed_glb.glb"])
    parts = spec.parts or []
    docs["runtime_observation"] = {
        "schema_version": "asset-runtime-observation-0.7.0",
        "workflow_id": WF,
        "revision": REV,
        "asset_id": spec.asset_id,
        "execution_id": EXEC,
        "attempt_number": 1,
        "processed_glb_sha256": processed_sha,
        "status": "PASS",
        "errors": [],
        "mesh_visible": True,
        "collision_shape_present": True,
        "physics_body_present": True,
        "physics_ray_hit": True,
        "geometry_mode": "assembly",
        "collision_shape_class": "BoxShape3D",
        "mesh_bounds": {"position": [0, 0, 0], "size": [1, 1, 1]},
        "hierarchy": {
            "ok": True,
            "parts": [{"part_id": p.part_id, "ok": True} for p in parts],
            "sockets": [
                {"socket_id": s.socket_id, "ok": True, "node_class": "Marker3D"}
                for s in spec.sockets or []
            ],
        },
        "articulation": [
            {
                "part_id": p.part_id,
                "motion": p.pivot.motion.kind,
                "moved": True,
                "pivot_ok": True,
                "descendants_rigid": True,
                "others_unchanged": True,
                "restored": True,
                "ok": True,
            }
            for p in parts
            if p.pivot.motion.kind != "fixed"
        ],
        **docs.pop("observation_overrides", {}),
    }
    for role in (
        "specification",
        "source_provenance",
        "normalization",
        "processing_report",
        "validation",
        "runtime_observation",
    ):
        files[f"evidence/{role}.json"] = _dump(docs[role])
    spec_hash = spec_fingerprint(spec)
    captures = {view: _sha(files[f"captures/{view}.png"]) for view in views}
    context = {
        "workflow_id": WF,
        "revision": REV,
        "specification_hash": spec_hash,
        "artifacts": {
            "asset-source-glb": _sha(files["evidence/source_glb.glb"]),
            "asset-source-registration": _sha(files["evidence/source_provenance.json"]),
            "asset-processed-glb": processed_sha,
            "asset-processing-report": _sha(files["evidence/processing_report.json"]),
            "asset-validation-report": _sha(files["evidence/validation.json"]),
            "asset-runtime-observation": _sha(files["evidence/runtime_observation.json"]),
        },
        "runtime_capture_hashes": sorted(captures.values()),
    }
    inputs = {"scope": {"workflow_id": WF, "handler_context": context}, "parameters": {}}
    task_id = f"{WF}-FINAL-REVIEW"
    fingerprint = verifier._approval_hash(task_id, "final_visual_review", inputs)
    files["evidence/final_approval.json"] = _dump(
        {
            "approval_id": "APP-v07test",
            "workflow_id": WF,
            "revision": REV,
            "task_id": task_id,
            "approval_type": "final_visual_review",
            "status": "APPROVED",
            "actor": "tester",
            "decided_at": "2026-09-30T00:00:00+00:00",
            "inputs": inputs,
            "fingerprint": fingerprint,
        }
    )
    files["evidence/production-receipt.json"] = _dump(
        {
            "schema_version": "production-receipt-0.7.0",
            "source_kind": "local_operator_assembly",
            "paid": False,
            "paid_provider_invocations": 0,
            "spec_hash": spec_hash,
            "source_sha256": _sha(files["evidence/source_glb.glb"]),
            "source_registration_sha256": _sha(files["evidence/source_provenance.json"]),
            "normalization_sha256": _sha(files["evidence/normalization.json"]),
            "source_front": docs["normalization"].get("source_front"),
            "processed_artifact_hash": processed_sha,
            "validation_hash": _sha(files["evidence/validation.json"]),
            "runtime_hash": _sha(files["evidence/runtime_observation.json"]),
            "render_hashes": captures,
        }
    )
    roles = {
        "evidence/specification.json": "specification",
        "evidence/source_glb.glb": "source_glb",
        "evidence/source_provenance.json": "source_provenance",
        "evidence/normalization.json": "normalization",
        "evidence/processed_glb.glb": "processed_glb",
        "evidence/processing_report.json": "processing_report",
        "evidence/validation.json": "validation",
        "evidence/runtime_observation.json": "runtime_observation",
        "evidence/final_approval.json": "final_approval",
        "evidence/production-receipt.json": "production_receipt",
    }
    extra_roles = docs.pop("extra_roles", {})
    links = [*roles, *(f"captures/{v}.png" for v in views), *extra_roles]
    files["index.html"] = (
        '<!doctype html><html><head><meta charset="utf-8"><title>t</title></head><body><ul>'
        + "".join(f'<li><a href="{path}">x</a></li>' for path in links)
        + "</ul></body></html>\n"
    ).encode()
    bundle = root / "bundle"
    entries = []
    for path, data in files.items():
        target = bundle / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if path == "index.html":
            role = "review_html"
        elif path.startswith("captures/"):
            role = "runtime_capture"
        else:
            role = roles.get(path) or extra_roles[path]
        item: dict[str, Any] = {"role": role, "path": path, "size": len(data), "sha256": _sha(data)}
        if role == "runtime_capture":
            item.update(
                {
                    "workflow_id": WF,
                    "revision": REV,
                    "asset_id": spec.asset_id,
                    "execution_id": EXEC,
                    "attempt_number": 1,
                    "processed_glb_sha256": processed_sha,
                    "angle": Path(path).stem,
                }
            )
        entries.append(item)
    manifest = {
        "schema_version": "asset-evidence-0.7.0",
        "workflow_id": WF,
        "revision": REV,
        "asset_id": spec.asset_id,
        "execution_id": EXEC,
        "attempt_number": 1,
        "processed_glb_sha256": processed_sha,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "profile_qualified": profile.qualified,
        "source_kind": "local_operator_assembly",
        "geometry_mode": "assembly",
        "paid": False,
        "paid_provider_invocations": 0,
        "review_views": views,
        "runtime_requirements": profile.runtime_requirements(spec),
        "specification_fingerprint": spec_hash,
        "validator": {
            "composition": docs["validation"]["composition"],
            "rule_groups": docs["validation"]["rule_groups"],
        },
        "normalization": docs["normalization"],
        "final_review": {"decision": "APPROVED", "fingerprint": fingerprint},
        "files": entries,
        **docs.pop("manifest_overrides", {}),
    }
    (bundle / "manifest.json").write_bytes(_dump(manifest))
    return bundle


@pytest.mark.parametrize("source_front", ["-Z", "+Z"])
@pytest.mark.parametrize("asset_id", sorted(ag.ASSEMBLY_DESIGNS))
def test_consistent_assembly_bundle_passes(
    tmp_path: Path, asset_id: str, source_front: str
) -> None:
    bundle = build_bundle(tmp_path, design=ag.ASSEMBLY_DESIGNS[asset_id], source_front=source_front)
    result = verifier.verify_bundle(bundle)
    assert result["status"] == "PASS"
    assert result["source_front"] == source_front


def test_cold_verifier_runs_isolated_without_package_imports(tmp_path: Path) -> None:
    bundle = build_bundle(tmp_path)
    (bundle / "verify_asset_bundle.py").write_bytes(VERIFIER.read_bytes())
    proc = subprocess.run(
        [sys.executable, "-I", str(bundle / "verify_asset_bundle.py"), str(bundle)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["status"] == "PASS"


def _fails(tmp_path: Path, match: str, **kwargs: Any) -> None:
    bundle = build_bundle(tmp_path, **kwargs)
    with pytest.raises(ValueError, match=match):
        verifier.verify_bundle(bundle)


# --- tampering with the retained source and the normalization record -------------


def test_tampered_retained_source_is_detected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        edit = ag.GLBEdit.of(files["evidence/source_glb.glb"])
        edit.set("PART_turret", translation=[0.0, 0.9, 0.2])
        files["evidence/source_glb.glb"] = edit.build()

    _fails(tmp_path, "source registration does not bind", mutate=mutate)


def test_retained_source_swapped_with_consistent_registration_is_detected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        edit = ag.GLBEdit.of(files["evidence/source_glb.glb"])
        edit.set("PART_turret", translation=[0.0, 0.9, 0.2])
        swapped = edit.build()
        files["evidence/source_glb.glb"] = swapped
        docs["source_provenance"]["artifact_sha256"] = _sha(swapped)
        docs["source_provenance"]["artifact_bytes"] = len(swapped)
        docs["processing_report"]["source_sha256"] = _sha(swapped)

    _fails(tmp_path, "not source x the declared rotation", mutate=mutate)


def test_tampered_normalization_record_is_detected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        forged = json.loads(json.dumps(docs["normalization"]))
        forged["normalization_transform"]["quaternion_xyzw"] = [0.0, 0.0, 0.0, 1.0]
        for key in ("normalization", "processing_report", "validation"):
            target = docs[key] if key == "normalization" else docs[key]["normalization"]
            target.clear()
            target.update(forged)
        docs["manifest_overrides"] = {"normalization": forged}

    _fails(tmp_path, "does not match its declared source_front", mutate=mutate)


def test_normalization_record_that_differs_between_documents_is_detected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        docs["normalization"] = {**docs["normalization"], "resulting_front": "+Z"}

    _fails(tmp_path, "normalization record differs", mutate=mutate)


def test_result_with_an_extra_rotation_is_detected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        edit = ag.GLBEdit.of(files["evidence/processed_glb.glb"])
        hull = edit.node("PART_hull")
        extra = tf.mat_mul(
            tf.trs_matrix(rotation=ag.rotate_about_y(5.0)),
            tf.trs_matrix(hull["translation"], hull["rotation"], hull["scale"]),
        )
        hull["rotation"] = list(tf.rotation_to_quat(tf.rotation_of(extra)))
        files["evidence/processed_glb.glb"] = edit.build()
        docs["processing_report"]["processed_sha256"] = _sha(files["evidence/processed_glb.glb"])

    _fails(tmp_path, "not source x the declared rotation", mutate=mutate)


def test_minus_z_source_that_was_changed_is_detected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        edit = ag.GLBEdit.of(files["evidence/processed_glb.glb"])
        edit.set("SOCKET_muzzle", translation=[0.0, 0.0, -1.49])
        files["evidence/processed_glb.glb"] = edit.build()
        docs["processing_report"]["processed_sha256"] = _sha(files["evidence/processed_glb.glb"])

    _fails(tmp_path, "retained byte-for-byte", source_front="-Z", mutate=mutate)


def test_changed_mesh_data_is_detected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        edit = ag.GLBEdit.of(files["evidence/processed_glb.glb"])
        edit.document["materials"][0]["name"] = "M_other"
        files["evidence/processed_glb.glb"] = edit.build()
        docs["processing_report"]["processed_sha256"] = _sha(files["evidence/processed_glb.glb"])

    _fails(tmp_path, "normalization changed materials", mutate=mutate)


# --- paid / role set -----------------------------------------------------------------


def test_assembly_bundle_with_paid_true_is_rejected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        docs["manifest_overrides"] = {"paid": True}

    _fails(tmp_path, "paid: false", mutate=mutate)


def test_registration_with_paid_true_is_rejected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        docs["source_provenance"]["paid"] = True

    _fails(tmp_path, "source registration does not bind", mutate=mutate)


@pytest.mark.parametrize("role", ["concept", "paid_approval", "provider_operation", "raw_glb"])
def test_mixed_role_set_is_rejected(tmp_path: Path, role: str) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        files[f"evidence/{role}.json"] = b"{}\n"
        docs["extra_roles"] = {f"evidence/{role}.json": role}

    _fails(tmp_path, "mixed role set", mutate=mutate)


# --- views, validator groups and runtime ----------------------------------------------


def test_unknown_view_in_manifest_is_rejected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        docs["manifest_overrides"] = {"review_views": ["front", "underside"]}

    _fails(tmp_path, "view.unplaced", mutate=mutate)


def test_rule_groups_that_do_not_follow_capabilities_are_rejected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        docs["validation"]["rule_groups"] = ["core", "parts", "pivot", "collider_box"]

    _fails(tmp_path, "derived rule groups", mutate=mutate)


def test_missing_articulation_for_a_movable_part_is_rejected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        docs["observation_overrides"] = {"articulation": []}

    _fails(tmp_path, "part tree and articulation", mutate=mutate)


def test_socket_that_is_not_a_marker_is_rejected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        docs["observation_overrides"] = {
            "hierarchy": {
                "ok": True,
                "parts": [{"part_id": p, "ok": True} for p in ("hull", "turret", "barrel")],
                "sockets": [{"socket_id": "muzzle", "ok": True, "node_class": "Node3D"}],
            }
        }

    _fails(tmp_path, "part tree and articulation", mutate=mutate)


def test_part_map_that_differs_from_the_specification_is_rejected(tmp_path: Path) -> None:
    def mutate(files: dict[str, bytes], docs: dict[str, Any]) -> None:
        docs["source_provenance"]["part_map"][1]["parent"] = "root"

    _fails(tmp_path, "source registration does not bind", mutate=mutate)
