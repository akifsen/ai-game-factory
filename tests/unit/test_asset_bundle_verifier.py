"""Cold verification of portable asset evidence, including tamper cases."""

from __future__ import annotations

import hashlib
import json
import runpy
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_asset_bundle.py"


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def approval_hash(task_id: str, kind: str, inputs: dict) -> str:
    return digest(
        json.dumps(
            {"task_id": task_id, "approval_type": kind, "inputs": inputs},
            sort_keys=True,
            allow_nan=False,
        ).encode()
    )


def png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x80\x00"))
        + chunk(b"IEND", b"")
    )


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def make_bundle(root: Path, final_status: str = "PENDING") -> dict:
    root.mkdir()
    content = {
        "specification": (
            "spec.json",
            json.dumps(
                {
                    "asset_id": "prop_energy_crate_01",
                    "revision": 1,
                    "dimensions": {"width_m": 1.0, "height_m": 1.0, "depth_m": 1.0},
                }
            ).encode(),
        ),
        "concept": ("concept.png", png()),
        "concept_provenance": ("concept-provenance.json", b"{}"),
        "raw_glb": ("raw.glb", b"raw model"),
        "processed_glb": ("processed.glb", b"processed model"),
        "processing_report": ("processing.json", b""),
        "validation": ("validation.json", json.dumps({"status": "PASS", "passed": True}).encode()),
    }
    spec_hash = digest(
        json.dumps(
            json.loads(content["specification"][1]), sort_keys=True, separators=(",", ":")
        ).encode()
    )
    concept_hash = digest(content["concept"][1])
    processed_hash = digest(content["processed_glb"][1])
    content["concept_provenance"] = (
        "concept-provenance.json",
        json.dumps({"asset_spec_hash": spec_hash, "artifact_hash": concept_hash}).encode(),
    )
    content["processing_report"] = (
        "processing.json",
        json.dumps(
            {
                "status": "SUCCESS",
                "exit_code": 0,
                "input_raw_glb_sha256": digest(content["raw_glb"][1]),
                "output_sha256": processed_hash,
                "processing_script_sha256": "a" * 64,
            }
        ).encode(),
    )
    inputs = {
        "parameters": {
            "asset_id": "prop_energy_crate_01",
            "revision_number": 1,
            "specification_hash": spec_hash,
            "concept_source_hash": concept_hash,
        },
        "scope": {
            "workflow_id": "WF-1",
            "artifacts": [
                ["spec-artifact", digest(content["specification"][1])],
                ["concept-artifact", concept_hash],
            ],
        },
    }
    receipts = {}
    for role, kind in (
        ("concept_approval", "concept_review"),
        ("paid_approval", "paid_generation"),
    ):
        task_id = f"task-{role}"
        receipt = {
            "workflow_id": "WF-1",
            "revision": 1,
            "approval_type": kind,
            "task_id": task_id,
            "status": "APPROVED",
            "inputs": inputs,
            "fingerprint": approval_hash(task_id, kind, inputs),
        }
        receipts[role] = receipt
        content[role] = (f"{role}.json", json.dumps(receipt).encode())
    observation = {
        "workflow_id": "WF-1",
        "revision": 1,
        "asset_id": "prop_energy_crate_01",
        "execution_id": "EXEC-1",
        "attempt_number": 1,
        "processed_glb_sha256": processed_hash,
        "status": "PASS",
        "mesh_visible": True,
        "collision_shape_present": True,
        "physics_body_present": True,
        "physics_ray_hit": True,
        "errors": [],
        "mesh_bounds": {"size": [1.0, 1.0, 1.0]},
    }
    content["runtime_observation"] = ("observation.json", json.dumps(observation).encode())
    cost = {"estimate": "UNKNOWN", "actual": "UNKNOWN", "unit": "credits", "budget_reservation": 5}
    content["cost_record"] = ("cost.json", json.dumps(cost).encode())
    provider = {
        "workflow_id": "WF-1",
        "revision": 1,
        "provider": "meshy",
        "operation": "image-to-3d",
        "status": "SUCCEEDED",
        "task_id": receipts["paid_approval"]["task_id"],
        "external_task_id": "meshy-123",
        "request_fingerprint": receipts["paid_approval"]["fingerprint"],
        "concept_sha256": concept_hash,
        "actual_cost": "UNKNOWN",
    }
    content["provider_operation"] = ("provider.json", json.dumps(provider).encode())
    captures = []
    for angle in ("front", "three_quarter", "side"):
        captures.append((f"{angle}.png", png()))
    final_inputs = {
        "scope": {
            "handler_context": {
                "workflow_id": "WF-1",
                "revision": 1,
                "specification_hash": spec_hash,
                "artifacts": {
                    "asset-concept": concept_hash,
                    "asset-processed-glb": processed_hash,
                    "asset-validation-report": digest(content["validation"][1]),
                    "asset-runtime-observation": digest(content["runtime_observation"][1]),
                },
                "runtime_capture_hashes": [digest(raw) for _, raw in captures],
            }
        }
    }
    final = {
        "workflow_id": "WF-1",
        "revision": 1,
        "approval_type": "final_visual_review",
        "task_id": "task-final",
        "status": final_status,
        "inputs": final_inputs,
        "fingerprint": approval_hash("task-final", "final_visual_review", final_inputs),
    }
    content["final_approval"] = ("final_approval.json", json.dumps(final).encode())
    html = "<html><body>" + "".join(
        f'<a href="{path}">{role}</a>' for role, (path, _) in content.items()
    )
    html += "".join(f'<img src="{path}">' for path, _ in captures) + "</body></html>"
    content["review_html"] = ("index.html", html.encode())
    files = []
    for role, (path, raw) in content.items():
        (root / path).write_bytes(raw)
        files.append({"path": path, "role": role, "sha256": digest(raw), "size": len(raw)})
    for angle, (path, raw) in zip(("front", "three_quarter", "side"), captures, strict=True):
        (root / path).write_bytes(raw)
        files.append(
            {
                "path": path,
                "role": "runtime_capture",
                "sha256": digest(raw),
                "size": len(raw),
                "angle": angle,
                "workflow_id": "WF-1",
                "revision": 1,
                "asset_id": "prop_energy_crate_01",
                "execution_id": "EXEC-1",
                "attempt_number": 1,
                "processed_glb_sha256": processed_hash,
            }
        )
    manifest = {
        "schema_version": "asset-evidence-0.4.0",
        "workflow_id": "WF-1",
        "revision": 1,
        "files": files,
        "final_review": {"decision": final_status, "fingerprint": final["fingerprint"]},
    }
    write_json(root / "manifest.json", manifest)
    return manifest


def verify(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def update_entry(root: Path, manifest: dict, role: str, value: dict) -> None:
    item = next(item for item in manifest["files"] if item["role"] == role)
    raw = json.dumps(value).encode()
    (root / item["path"]).write_bytes(raw)
    item["sha256"] = digest(raw)
    item["size"] = len(raw)
    write_json(root / "manifest.json", manifest)


def test_cold_bundle_passes_in_isolated_python(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    make_bundle(root)
    result = verify(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "PASS"
    assert json.loads(result.stdout)["final_review_decision"] == "PENDING"


def test_approved_bundle_reports_decision_separately(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    make_bundle(root, final_status="APPROVED")
    result = verify(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["final_review_decision"] == "APPROVED"


@pytest.mark.parametrize("role", ["concept_approval", "paid_approval", "final_approval"])
def test_missing_receipt_fails(tmp_path: Path, role: str) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    item = next(item for item in manifest["files"] if item["role"] == role)
    (root / item["path"]).unlink()
    assert verify(root).returncode == 1


def test_approval_fingerprint_tamper_fails_even_with_updated_file_hash(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    item = next(item for item in manifest["files"] if item["role"] == "paid_approval")
    receipt = json.loads((root / item["path"]).read_text())
    receipt["inputs"]["concept_hash"] = "0" * 64
    update_entry(root, manifest, "paid_approval", receipt)
    assert "fingerprint" in verify(root).stdout


def test_unlisted_file_and_remote_html_fail(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    (root / "secret.txt").write_text("unlisted")
    assert "unlisted" in verify(root).stdout
    (root / "secret.txt").unlink()
    html_item = next(item for item in manifest["files"] if item["role"] == "review_html")
    html = (root / "index.html").read_text() + '<img src="https://example.test/pixel.png">'
    (root / "index.html").write_text(html)
    html_item["sha256"] = digest(html.encode())
    html_item["size"] = len(html.encode())
    write_json(root / "manifest.json", manifest)
    assert "unsafe bundle path" in verify(root).stdout


@pytest.mark.parametrize(
    "injection",
    [
        '<body background="https://example.test/pixel.png">',
        '<a href="concept.png" ping="https://example.test/track">track</a>',
    ],
)
def test_unrecognized_remote_html_attributes_fail(tmp_path: Path, injection: str) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    html_item = next(item for item in manifest["files"] if item["role"] == "review_html")
    html = (root / "index.html").read_text() + injection
    (root / "index.html").write_text(html)
    html_item["sha256"] = digest(html.encode())
    html_item["size"] = len(html.encode())
    write_json(root / "manifest.json", manifest)
    assert "HTML attribute" in verify(root).stdout


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", 2),
        ("processed_glb_sha256", "0" * 64),
        ("collision_shape_present", False),
    ],
)
def test_runtime_observation_tamper_fails(tmp_path: Path, field: str, value: object) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    item = next(item for item in manifest["files"] if item["role"] == "runtime_observation")
    observation = json.loads((root / item["path"]).read_text())
    observation[field] = value
    update_entry(root, manifest, "runtime_observation", observation)
    assert verify(root).returncode == 1


def test_capture_wrong_attempt_fails(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    capture = next(item for item in manifest["files"] if item["role"] == "runtime_capture")
    capture["attempt_number"] = 2
    write_json(root / "manifest.json", manifest)
    assert "wrong revision or attempt" in verify(root).stdout


@pytest.mark.parametrize(
    ("views", "expected_error"),
    [
        (["front", "front_typo"], "review_views are missing or unsupported"),
        ([["front"]], "review_views are missing or unsupported"),
        ([None], "review_views are missing or unsupported"),
        (
            [
                "front",
                "rear",
                "left",
                "right",
                "side",
                "three_quarter",
                "three_quarter_front",
                "three_quarter_rear",
                "top",
            ],
            "bundle requires exactly one production_receipt entry",
        ),
    ],
)
def test_cold_verifier_rejects_unknown_view_and_accepts_full_view_vocabulary(
    tmp_path: Path, views: list[object], expected_error: str
) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    manifest["schema_version"] = "asset-evidence-0.5.0"
    manifest["review_views"] = views
    write_json(root / "manifest.json", manifest)
    verify_bundle = runpy.run_path(str(SCRIPT))["verify_bundle"]
    with pytest.raises(ValueError, match=expected_error):
        verify_bundle(root)


def test_failed_validation_report_cannot_be_rehashed_into_pass(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    update_entry(root, manifest, "validation", {"status": "FAIL", "passed": False})
    assert "validation did not pass" in verify(root).stdout


def test_processing_report_cannot_claim_different_raw_asset(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    item = next(item for item in manifest["files"] if item["role"] == "processing_report")
    report = json.loads((root / item["path"]).read_text())
    report["input_raw_glb_sha256"] = "0" * 64
    update_entry(root, manifest, "processing_report", report)
    assert "raw-to-processed" in verify(root).stdout


def test_duplicate_manifest_key_fails(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    make_bundle(root)
    manifest = (root / "manifest.json").read_text()
    (root / "manifest.json").write_text(
        manifest.replace('"revision": 1,', '"revision": 1, "revision": 1,', 1)
    )
    assert "duplicate JSON key" in verify(root).stdout


def test_rehashed_non_png_concept_fails(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    item = next(item for item in manifest["files"] if item["role"] == "concept")
    raw = b"not a PNG"
    (root / item["path"]).write_bytes(raw)
    item["size"] = len(raw)
    item["sha256"] = digest(raw)
    write_json(root / "manifest.json", manifest)
    assert "not a PNG" in verify(root).stdout


def test_png_decoder_rejects_large_inflate_after_small_ihdr() -> None:
    """A tiny declared image must not let zlib flush the rest of an inflate bomb."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    raw = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00" + b"x" * 1_000_000))
        + chunk(b"IEND", b"")
    )
    decoder = runpy.run_path(str(SCRIPT))["_png_dimensions"]
    with pytest.raises(ValueError, match="decoded image"):
        decoder(raw)


def _side_correction(processed_sha: str, execution_id: str = "EXEC-SIDE", attempt: int = 2) -> dict:
    return {
        "workflow_id": "WF-1",
        "revision": 1,
        "asset_id": "prop_energy_crate_01",
        "execution_id": execution_id,
        "attempt_number": attempt,
        "processed_glb_sha256": processed_sha,
        "status": "PASS",
        "mesh_visible": True,
        "physics_ray_hit": True,
        "errors": [],
        "side_framing": {
            "inside_viewport": True,
            "margin_ok": True,
            "height_ratio": 0.65,
            "horizontally_centered": True,
            "reference_between_camera_and_asset": False,
            "view_axis": "+X",
        },
    }


def _bind_side_correction(root: Path, manifest: dict, record: dict) -> None:
    side = next(item for item in manifest["files"] if item.get("angle") == "side")
    side["execution_id"] = record["execution_id"]
    side["attempt_number"] = record["attempt_number"]
    raw = json.dumps(record).encode()
    (root / "evidence").mkdir(exist_ok=True)
    (root / "evidence" / "side-correction.json").write_bytes(raw)
    manifest["files"].append(
        {
            "path": "evidence/side-correction.json",
            "role": "side_correction",
            "sha256": digest(raw),
            "size": len(raw),
        }
    )
    html_item = next(item for item in manifest["files"] if item["role"] == "review_html")
    html = (root / html_item["path"]).read_text(encoding="utf-8")
    html = html.replace(
        "</body>",
        '<a href="evidence/side-correction.json">side_correction</a></body>',
    )
    html_raw = html.encode()
    (root / html_item["path"]).write_bytes(html_raw)
    html_item["sha256"] = digest(html_raw)
    html_item["size"] = len(html_raw)
    write_json(root / "manifest.json", manifest)


def test_substituted_side_attempt_without_correction_fails(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    side = next(item for item in manifest["files"] if item.get("angle") == "side")
    side["execution_id"] = "EXEC-SIDE"
    side["attempt_number"] = 2
    write_json(root / "manifest.json", manifest)
    assert "corrected side" in verify(root).stdout


def test_corrected_side_record_binds_attempt_and_framing(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    side = next(item for item in manifest["files"] if item.get("angle") == "side")
    _bind_side_correction(root, manifest, _side_correction(side["processed_glb_sha256"]))
    result = verify(root)
    assert result.returncode == 0, result.stdout + result.stderr


def test_corrected_side_rejects_bad_framing_and_wrong_attempt(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    side = next(item for item in manifest["files"] if item.get("angle") == "side")
    record = _side_correction(side["processed_glb_sha256"])
    record["side_framing"]["height_ratio"] = 0.2
    _bind_side_correction(root, manifest, record)
    assert "corrected side" in verify(root).stdout

    record["side_framing"]["height_ratio"] = 0.65
    record["attempt_number"] = 9
    raw = json.dumps(record).encode()
    path = root / "evidence" / "side-correction.json"
    path.write_bytes(raw)
    item = next(entry for entry in manifest["files"] if entry["role"] == "side_correction")
    item["sha256"] = digest(raw)
    item["size"] = len(raw)
    write_json(root / "manifest.json", manifest)
    assert verify(root).returncode == 1


def test_modified_side_png_fails_even_when_correction_record_matches(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    manifest = make_bundle(root)
    side = next(item for item in manifest["files"] if item.get("angle") == "side")
    _bind_side_correction(root, manifest, _side_correction(side["processed_glb_sha256"]))
    (root / side["path"]).write_bytes(png() + b"\x00")
    assert "SHA-256" in verify(root).stdout
