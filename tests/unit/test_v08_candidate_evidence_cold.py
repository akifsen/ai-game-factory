"""Cold verifier tests for V0.8-3C candidate evidence envelopes (unit unsigned fixture)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import runpy
import shutil
import struct
import subprocess
import sys
import zlib
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from gamefactory.adapters.assets.internal_rig_canonical import reviewed_text_sha256
from gamefactory.adapters.assets.v08_candidate_runtime_digest import (
    RUNTIME_REQUEST_TRANSIENT_KEYS,
    candidate_runtime_bound_payload_canonical,
    candidate_runtime_request_digest,
)
from gamefactory.adapters.assets.v08_candidate_runtime_request import (
    build_bound_candidate_runtime_request,
)
from gamefactory.adapters.assets.v08_candidate_validate import validate_v08_candidate_glb
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.domain.v08_candidate_contracts import (
    candidate_spec_fingerprint,
    load_packaged_candidate_profile,
    load_packaged_candidate_specification,
    parse_asset_specification_v08_candidate,
    profile_document_hash,
)
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner
from gamefactory.workflows.v08_candidate_workflow import create_v08_candidate_workflow
from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace
from tests.unit.test_internal_rig_cold import _build_offline_bundle
from tests.unit.test_v08_candidate_runtime_verify import (
    _observation_from_bound,
    _populate_captures,
    _view_framing_from_bound,
)
from tests.unit.test_v08_candidate_workflow import (
    _fake_capsule_runtime,
    _run_to_test_only_gate,
)
from tests.unit.test_v08_candidate_workflow import (
    _handlers as _candidate_workflow_handlers,
)

REPO = Path(__file__).resolve().parents[2]
VERIFIER_CHECKOUT = REPO / "scripts" / "verify_candidate_bundle.py"
VERIFIER = Path(
    str(resources.files("gamefactory.resources.scripts").joinpath("verify_candidate_bundle.py"))
)
SCHEMA_PATH = REPO / "src" / "gamefactory" / "schemas" / "candidate-evidence-0.8.0.schema.json"
UNIT_UNSIGNED_FIXTURE = "unit_unsigned_fixture_v083c2a"
NESTED_PACKAGED_PATH = "rig/nested"
HISTORICAL_NESTED_DIR = ".gf/rig/EXEC-COLD-UNIT"
_NINE_VIEWS = frozenset(
    {
        "front",
        "rear",
        "left",
        "right",
        "side",
        "three_quarter",
        "three_quarter_front",
        "three_quarter_rear",
        "top",
    }
)
_SINGLE_MANIFEST_ROLES = frozenset(
    {
        "snapshot",
        "candidate_specification",
        "candidate_profile_document",
        "source_glb",
        "raw_glb",
        "processed_glb",
        "static_validation_report",
        "runtime_request",
        "runtime_observation",
        "runtime_provenance",
        "runtime_import_log",
        "runtime_render_log",
        "rig_attempt_wrapper",
        "test_only_receipt",
        "candidate_runtime_contract",
        "reviewed_candidate_harness",
        "identity_report",
        "approval_scope",
    }
)


class _FakeRunner(ProcessRunner):
    def run(self, request: CommandRequest) -> CommandResult:
        if "--version" in request.args:
            return CommandResult(
                exit_code=0, stdout="4.7.2.stable.official.test\n", stderr="", timed_out=False
            )
        return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _snapshot_fingerprint(payload: dict[str, Any]) -> str:
    if "snapshot_fingerprint" in payload:
        raise ValueError("snapshot payload must not embed snapshot_fingerprint")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return _digest(raw.encode("utf-8"))


def _json_file(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _spec_for_glb(path: Path):
    spec = load_packaged_candidate_specification()
    data = spec.model_dump(mode="json")
    data["processed_glb_sha256"] = _digest(path.read_bytes())
    return parse_asset_specification_v08_candidate(data)


def _refresh_manifest(bundle: Path, rel: str, raw: bytes, *, angle: str | None = None) -> None:
    (bundle / rel).write_bytes(raw)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == rel:
            entry["sha256"] = _digest(raw)
            entry["size"] = len(raw)
            if angle is not None:
                entry["angle"] = angle
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _rehash_manifest(bundle: Path) -> None:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        rel = entry["path"]
        raw = (bundle / rel).read_bytes()
        entry["sha256"] = _digest(raw)
        entry["size"] = len(raw)
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _resync_scope_and_receipt(bundle: Path) -> None:
    scope = json.loads((bundle / "approval/scope.json").read_text(encoding="utf-8"))
    snap = json.loads((bundle / "snapshot/snapshot.json").read_text(encoding="utf-8"))
    snap.pop("snapshot_fingerprint", None)
    for entry in scope["entries"]:
        bundle_path = entry.get("bundle_path") or entry["relative_path"]
        if entry.get("role_or_nested_manifest") == "rig/manifest.json":
            raw = (bundle / bundle_path).read_bytes()
        else:
            raw = (bundle / bundle_path).read_bytes()
        entry["content_hash"] = _digest(raw)
        entry["size"] = len(raw)
        entry["bundle_path"] = bundle_path
        for row in snap["pre_review_artifact_bindings"]:
            if row.get("artifact_id") == entry.get("artifact_id"):
                row["content_sha256"] = entry["content_hash"]
                row["size_bytes"] = entry["size"]
    review_task = scope["review_task"]
    snap_fp = _snapshot_fingerprint(snap)
    handler_context = {
        "workflow_id": snap["workflow_id"],
        "revision": review_task["parameters"]["revision_number"],
        "specification_hash": review_task["parameters"]["specification_hash"],
        "profile_document_hash": review_task["parameters"]["profile_document_hash"],
        "snapshot_fingerprint": snap_fp,
        "receipt_scope": "candidate_test_only",
        "production_eligible": False,
        "promotion_eligible": False,
        "candidate_test_only": True,
    }
    operation_inputs = {
        "parameters": review_task["parameters"],
        "scope": {
            "workflow_id": snap["workflow_id"],
            "task_type": review_task["task_type"],
            "cost_class": review_task["cost_class"],
            "provider": None,
            "artifacts": sorted((e["artifact_id"], e["content_hash"]) for e in scope["entries"]),
            "estimated_cost": 0.0,
            "handler_context": handler_context,
        },
    }
    op_hash = compute_operation_hash(
        review_task["id"], "candidate_test_only_review", operation_inputs
    )
    scope["operation_hash"] = op_hash
    _refresh_manifest(bundle, "snapshot/snapshot.json", _json_file(snap))
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["snapshot_fingerprint"] = snap_fp
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _refresh_manifest(bundle, "approval/scope.json", _json_file(scope))
    receipt = json.loads((bundle / "receipt/test-only.json").read_text(encoding="utf-8"))
    receipt["approval_operation_hash"] = op_hash
    receipt["fingerprint"] = op_hash
    receipt["snapshot_fingerprint"] = snap_fp
    receipt["status"] = "APPROVED"
    _refresh_manifest(bundle, "receipt/test-only.json", _json_file(receipt))


def _sync_glb_bytes(bundle: Path, raw: bytes) -> None:
    sha = _digest(raw)
    for rel in ("glb/source.glb", "glb/raw.glb", "glb/processed.glb"):
        _refresh_manifest(bundle, rel, raw)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["source_glb_sha256"] = sha
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    nested_glb = bundle / "rig/nested/evidence/skinned.glb"
    nested_glb.write_bytes(raw)
    nested_manifest_path = bundle / "rig/nested/manifest.json"
    nested_manifest = json.loads(nested_manifest_path.read_text(encoding="utf-8"))
    nested_manifest["glb_sha256"] = sha
    for entry in nested_manifest["files"]:
        if entry.get("role") == "skinned_glb":
            entry["sha256"] = sha
            entry["size"] = len(raw)
    nested_manifest_path.write_text(
        json.dumps(nested_manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    wrapper = json.loads((bundle / "rig/wrapper.json").read_text(encoding="utf-8"))
    wrapper["processed_glb_sha256"] = sha
    wrapper["nested_manifest_sha256"] = _digest(nested_manifest_path.read_bytes())
    _refresh_manifest(bundle, "rig/wrapper.json", _json_file(wrapper))
    snap = json.loads((bundle / "snapshot/snapshot.json").read_text(encoding="utf-8"))
    snap["source_glb_hash"] = sha
    snap["raw_glb_sha256"] = sha
    snap["processed_glb_sha256"] = sha
    snap.pop("snapshot_fingerprint", None)
    for row in snap["pre_review_artifact_bindings"]:
        if row["relative_path"] in {"glb/source.glb", "glb/raw.glb", "glb/processed.glb"}:
            row["content_sha256"] = sha
            row["size_bytes"] = len(raw)
    snap_fp = _snapshot_fingerprint(snap)
    _refresh_manifest(bundle, "snapshot/snapshot.json", _json_file(snap))
    manifest["snapshot_fingerprint"] = snap_fp
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _rehash_manifest(bundle)
    _resync_scope_and_receipt(bundle)


def _build_candidate_evidence_bundle(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    glb = build_humanoid_skinned_glb("positive")
    glb_path = tmp_path / "processed.glb"
    glb_path.write_bytes(glb)
    glb_sha = _digest(glb)
    spec_model = _spec_for_glb(glb_path)
    profile = load_packaged_candidate_profile()
    profile_doc = profile.document.model_dump(mode="json")
    spec_doc = spec_model.model_dump(mode="json")
    spec_fp = candidate_spec_fingerprint(spec_model)
    profile_hash = profile_document_hash(profile.document)

    static = validate_v08_candidate_glb(glb_path, spec_model, profile=profile).to_dict()

    harness = Path(
        str(resources.files("gamefactory.resources.godot").joinpath("candidate_capsule_harness.gd"))
    )
    bound = build_bound_candidate_runtime_request(
        glb_path,
        spec_model,
        profile,
        workflow_id="WF-COLD-UNIT",
        revision=1,
        execution_id="E-CAP",
        strict_attempt_number=1,
        godot_executable=Path("godot"),
        runner=_FakeRunner(),
        harness_path=harness,
    )
    bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(bound)
    bound["request_digest"] = candidate_runtime_request_digest(bound)
    observation = _observation_from_bound(bound)
    observation["view_framing"] = _view_framing_from_bound(bound)
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    _populate_captures(capture_dir, bound, observation)
    _, harness_raw = reviewed_text_sha256(harness)
    observation["harness_sha256_raw"] = harness_raw

    rig_build = tmp_path / "rig_build"
    rig_build.mkdir(parents=True, exist_ok=True)
    nested = _build_offline_bundle(rig_build)
    nested_rel = NESTED_PACKAGED_PATH
    historical_nested_dir = HISTORICAL_NESTED_DIR
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    nested_dest = bundle / nested_rel
    nested_dest.parent.mkdir(parents=True, exist_ok=True)
    nested.rename(nested_dest)

    contract_bytes = (
        resources.files("gamefactory.resources.v08_candidate")
        .joinpath("candidate-runtime-contract-0.8.0.json")
        .read_bytes()
    )
    harness_bytes = harness.read_bytes()

    historical_stage = tmp_path / "historical_stage"
    historical_stage.mkdir(exist_ok=True)
    (historical_stage / "captures").mkdir(exist_ok=True)
    stage_request = {
        **bound,
        "glb": "res://Main.tscn",
        "capture_dir": str(historical_stage / "captures"),
        "observation_path": str(historical_stage / "observation.json"),
    }
    provenance = {
        "stage_dir": str(historical_stage),
        "request_path": str(historical_stage / "request.json"),
        "import_log": "godot-import.log",
        "render_log": "godot-render.log",
        "bound_request": dict(bound),
        "import_exit_code": 0,
        "process_exit_code": 0,
        "import_timed_out": False,
        "process_timed_out": False,
        "verification": {"integrity_outcome": "VERIFIED"},
    }

    wrapper = {
        "schema_version": "candidate-rig-attempt-0.8.0",
        "workflow_id": "WF-COLD-UNIT",
        "revision_number": 1,
        "execution_id": "E-ORA",
        "attempt_number": 1,
        "processed_glb_sha256": glb_sha,
        "specification_hash": spec_fp,
        "profile_document_hash": profile_hash,
        "nested_bundle_dir": historical_nested_dir,
        "nested_bundle_id": json.loads((nested_dest / "manifest.json").read_text())["bundle_id"],
        "nested_manifest_sha256": _digest((nested_dest / "manifest.json").read_bytes()),
        "candidate_graph_version": "0.8.0-candidate",
    }

    bindings: list[dict[str, Any]] = []
    artifact_rows: list[dict[str, Any]] = []

    def _art(
        artifact_id: str,
        artifact_type: str,
        rel: str,
        raw: bytes,
        *,
        role: str,
        task_id: str,
        execution_id: str,
        attempt: int = 1,
        role_or_nested: str | None = None,
        angle: str | None = None,
        binding_relative_path: str | None = None,
        bundle_path: str | None = None,
    ) -> None:
        binding_rel = binding_relative_path or rel
        bundle_rel = bundle_path or rel
        bindings.append(
            {
                "artifact_id": artifact_id,
                "task_id": task_id,
                "role": artifact_type,
                "relative_path": binding_rel,
                "content_sha256": _digest(raw),
                "size_bytes": len(raw),
                "producing_execution_id": execution_id,
            }
        )
        artifact_rows.append(
            {
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "content_hash": _digest(raw),
                "task_id": task_id,
                "execution_id": execution_id,
                "attempt_number": attempt,
                "relative_path": binding_rel,
                "bundle_path": bundle_rel,
                "size": len(raw),
                "role_or_nested_manifest": role_or_nested or role,
                "manifest_role": role,
                "angle": angle,
            }
        )

    files: list[dict[str, Any]] = []

    def _write(rel: str, role: str, raw: bytes, **meta: Any) -> None:
        dest = bundle / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        entry = {"path": rel, "role": role, "size": len(raw), "sha256": _digest(raw)}
        if "angle" in meta:
            entry["angle"] = meta["angle"]
        files.append(entry)

    spec_raw = _json_file(spec_doc)
    profile_raw = _json_file(profile_doc)
    _write("spec/specification.json", "candidate_specification", spec_raw)
    _art(
        "ART-SPEC",
        "candidate-specification",
        "spec/specification.json",
        spec_raw,
        role="candidate_specification",
        task_id="T-PREP",
        execution_id="E-PREP",
    )
    _write("profile/profile.json", "candidate_profile_document", profile_raw)
    _art(
        "ART-PROF",
        "candidate-profile-document",
        "profile/profile.json",
        profile_raw,
        role="candidate_profile_document",
        task_id="T-PREP",
        execution_id="E-PREP",
    )
    _write("glb/source.glb", "source_glb", glb)
    _art(
        "ART-SRC",
        "candidate-source-retained",
        "glb/source.glb",
        glb,
        role="source_glb",
        task_id="T-PREP",
        execution_id="E-PREP",
    )
    _write("glb/raw.glb", "raw_glb", glb)
    _art(
        "ART-RAW",
        "candidate-raw-glb",
        "glb/raw.glb",
        glb,
        role="raw_glb",
        task_id="T-ID",
        execution_id="E-ID",
    )
    _write("glb/processed.glb", "processed_glb", glb)
    _art(
        "ART-PROC",
        "candidate-processed-glb",
        "glb/processed.glb",
        glb,
        role="processed_glb",
        task_id="T-ID",
        execution_id="E-ID",
    )

    static_raw = _json_file(static)
    _write("static/report.json", "static_validation_report", static_raw)
    _art(
        "ART-STATIC",
        "candidate-static-validation-report",
        "static/report.json",
        static_raw,
        role="static_validation_report",
        task_id="T-STATIC",
        execution_id="E-STATIC",
    )

    req_raw = _json_file(stage_request)
    obs_raw = _json_file(observation)
    prov_raw = _json_file(provenance)
    import_log = b"import ok\n"
    render_log = b"render ok\n"
    _write("runtime/request.json", "runtime_request", req_raw)
    _art(
        "ART-REQ",
        "candidate-runtime-request",
        "runtime/request.json",
        req_raw,
        role="runtime_request",
        task_id="T-CAP",
        execution_id="E-CAP",
    )
    _write("runtime/observation.json", "runtime_observation", obs_raw)
    _art(
        "ART-OBS",
        "candidate-runtime-observation",
        "runtime/observation.json",
        obs_raw,
        role="runtime_observation",
        task_id="T-CAP",
        execution_id="E-CAP",
    )
    _write("runtime/provenance.json", "runtime_provenance", prov_raw)
    _art(
        "ART-PROV",
        "candidate-runtime-provenance",
        "runtime/provenance.json",
        prov_raw,
        role="runtime_provenance",
        task_id="T-CAP",
        execution_id="E-CAP",
    )
    _write("runtime/godot-import.log", "runtime_import_log", import_log)
    _art(
        "ART-IMP",
        "candidate-runtime-import-log",
        "runtime/godot-import.log",
        import_log,
        role="runtime_import_log",
        task_id="T-CAP",
        execution_id="E-CAP",
    )
    _write("runtime/godot-render.log", "runtime_render_log", render_log)
    _art(
        "ART-REN",
        "candidate-runtime-render-log",
        "runtime/godot-render.log",
        render_log,
        role="runtime_render_log",
        task_id="T-CAP",
        execution_id="E-CAP",
    )

    for entry in observation["captures"]:
        view = entry["view"]
        png_path = capture_dir / f"{view}.png"
        png_raw = png_path.read_bytes()
        rel = f"runtime/captures/{view}.png"
        _write(rel, "runtime_capture", png_raw, angle=view)
        _art(
            f"ART-CAP-{view}",
            "candidate-runtime-capture",
            rel,
            png_raw,
            role="runtime_capture",
            task_id="T-CAP",
            execution_id="E-CAP",
            angle=view,
        )

    wrapper_raw = _json_file(wrapper)
    _write("rig/wrapper.json", "rig_attempt_wrapper", wrapper_raw)
    _art(
        "ART-WRAP",
        "candidate-rig-attempt-wrapper",
        "rig/wrapper.json",
        wrapper_raw,
        role="rig_attempt_wrapper",
        task_id="T-ORA",
        execution_id="E-ORA",
    )
    nested_manifest_raw = (nested_dest / "manifest.json").read_bytes()
    _art(
        "ART-NMAN",
        "candidate-nested-rig-manifest",
        f"{nested_rel}/manifest.json",
        nested_manifest_raw,
        role="rig_attempt_wrapper",
        role_or_nested="rig/manifest.json",
        task_id="T-ORA",
        execution_id="E-ORA",
        binding_relative_path=f"{historical_nested_dir}/manifest.json",
        bundle_path=f"{nested_rel}/manifest.json",
    )

    identity = {
        "schema_version": "candidate-identity-report-0.8.0",
        "raw_sha256": glb_sha,
        "processed_sha256": glb_sha,
        "byte_identity": True,
        "execution_id": "E-ID",
        "attempt_number": 1,
    }
    identity_raw = _json_file(identity)
    _write("identity/report.json", "identity_report", identity_raw)
    _art(
        "ART-IDREP",
        "candidate-identity-report",
        "identity/report.json",
        identity_raw,
        role="identity_report",
        task_id="T-ID",
        execution_id="E-ID",
    )

    bindings.sort(key=lambda row: row["artifact_id"])
    assert len(bindings) == 23

    snapshot = {
        "graph_version": "0.8.0-candidate",
        "workflow_id": "WF-COLD-UNIT",
        "asset_id": spec_model.asset_id,
        "revision_number": 1,
        "specification_hash": spec_fp,
        "profile_document_hash": profile_hash,
        "source_glb_hash": glb_sha,
        "authoritative_source_glb_relative_path": "glb/source.glb",
        "pre_review_artifact_bindings": bindings,
        "profile_id": profile.profile_id,
        "profile_version": profile.version,
        "asset_revision_spec_hash": spec_fp,
        "asset_revision_raw_glb_hash": glb_sha,
        "asset_revision_processed_glb_hash": glb_sha,
        "retained_specification_sha256": _digest(spec_raw),
        "retained_profile_document_sha256": _digest(profile_raw),
        "retained_source_sha256": glb_sha,
        "raw_glb_sha256": glb_sha,
        "processed_glb_sha256": glb_sha,
        "static_validation_report_sha256": _digest(static_raw),
        "runtime_request_sha256": _digest(req_raw),
        "runtime_observation_sha256": _digest(obs_raw),
        "runtime_capture_hashes": sorted(
            _digest((capture_dir / f"{entry['view']}.png").read_bytes())
            for entry in observation["captures"]
        ),
        "runtime_provenance_sha256": _digest(prov_raw),
        "runtime_import_log_sha256": _digest(import_log),
        "runtime_render_log_sha256": _digest(render_log),
        "rig_attempt_wrapper_sha256": _digest(wrapper_raw),
        "nested_rig_manifest_sha256": _digest(nested_manifest_raw),
        "nested_rig_manifest_on_disk_sha256": _digest(nested_manifest_raw),
        "identity_report_sha256": _digest(identity_raw),
        "prepare_execution": {"id": "E-PREP", "attempt_number": 1, "status": "COMPLETED"},
        "identity_execution": {"id": "E-ID", "attempt_number": 1, "status": "COMPLETED"},
        "static_execution": {"id": "E-STATIC", "attempt_number": 1, "status": "COMPLETED"},
        "capture_execution": {"id": "E-CAP", "attempt_number": 1, "status": "COMPLETED"},
        "oracle_execution": {"id": "E-ORA", "attempt_number": 1, "status": "COMPLETED"},
        "runtime_request_digest": bound["request_digest"],
        "nested_bundle_id": wrapper["nested_bundle_id"],
    }
    snapshot_fp = _snapshot_fingerprint(snapshot)
    snapshot_raw = _json_file(snapshot)
    _write("snapshot/snapshot.json", "snapshot", snapshot_raw)

    review_task = {
        "id": "T-REVIEW",
        "task_type": "v08_candidate_test_only_review",
        "cost_class": "LOCAL",
        "parameters": {
            "graph_version": "0.8.0-candidate",
            "source_kind": "local_verified_rig",
            "asset_id": spec_model.asset_id,
            "revision_number": 1,
            "specification": spec_doc,
            "specification_hash": spec_fp,
            "profile_document_hash": profile_hash,
            "source_glb": "glb/source.glb",
            "source_glb_hash": glb_sha,
            "asset_dir": f".gamefactory/assets/{spec_model.asset_id}/r001",
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "paid_provider_invocations": 0,
        },
    }
    scope_entries = [
        {
            "artifact_id": row["artifact_id"],
            "artifact_type": row["artifact_type"],
            "content_hash": row["content_hash"],
            "task_id": row["task_id"],
            "execution_id": row["execution_id"],
            "attempt_number": row["attempt_number"],
            "relative_path": row["relative_path"],
            "bundle_path": row["bundle_path"],
            "size": row["size"],
            "role_or_nested_manifest": row["role_or_nested_manifest"],
        }
        for row in sorted(artifact_rows, key=lambda r: r["artifact_id"])
    ]
    handler_context = {
        "workflow_id": "WF-COLD-UNIT",
        "revision": 1,
        "specification_hash": spec_fp,
        "profile_document_hash": profile_hash,
        "snapshot_fingerprint": snapshot_fp,
        "receipt_scope": "candidate_test_only",
        "production_eligible": False,
        "promotion_eligible": False,
        "candidate_test_only": True,
    }
    operation_inputs = {
        "parameters": review_task["parameters"],
        "scope": {
            "workflow_id": "WF-COLD-UNIT",
            "task_type": review_task["task_type"],
            "cost_class": review_task["cost_class"],
            "provider": None,
            "artifacts": sorted((e["artifact_id"], e["content_hash"]) for e in scope_entries),
            "estimated_cost": 0.0,
            "handler_context": handler_context,
        },
    }
    op_hash = compute_operation_hash("T-REVIEW", "candidate_test_only_review", operation_inputs)
    scope = {
        "schema_version": "candidate-approval-scope-0.8.0",
        "approval_id": "APP-UNIT",
        "approval_status": "APPROVED",
        "review_execution_id": "E-REVIEW",
        "review_task": review_task,
        "persisted_approval_artifact_ids": [e["artifact_id"] for e in scope_entries],
        "entries": scope_entries,
        "operation_hash": op_hash,
    }
    scope_raw = _json_file(scope)
    _write("approval/scope.json", "approval_scope", scope_raw)

    receipt = {
        "approval_id": "APP-UNIT",
        "approval_type": "candidate_test_only_review",
        "status": "APPROVED",
        "receipt_scope": "candidate_test_only",
        "production_eligible": False,
        "promotion_eligible": False,
        "fingerprint": op_hash,
        "snapshot_fingerprint": snapshot_fp,
        "approval_operation_hash": op_hash,
        "workflow_id": "WF-COLD-UNIT",
        "task_id": "T-REVIEW",
        "review_execution_id": "E-REVIEW",
        "approval_status": "APPROVED",
    }
    receipt_raw = _json_file(receipt)
    _write("receipt/test-only.json", "test_only_receipt", receipt_raw)

    _write("contract/runtime-contract.json", "candidate_runtime_contract", contract_bytes)
    _write("reviewed/candidate_capsule_harness.gd", "reviewed_candidate_harness", harness_bytes)

    manifest = {
        "schema_version": "candidate-evidence-0.8.0",
        "bundle_id": "candidate-cold-unit",
        "workflow_id": "WF-COLD-UNIT",
        "specification_hash": spec_fp,
        "profile_document_hash": profile_hash,
        "source_glb_sha256": glb_sha,
        "validation_status": "PASS",
        "runtime_status": "PASS",
        "reviewed_pins_match": True,
        "snapshot_fingerprint": snapshot_fp,
        "fixture_label": UNIT_UNSIGNED_FIXTURE,
        "nested_bundle_path": NESTED_PACKAGED_PATH,
        "files": sorted(files, key=lambda item: item["path"]),
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return bundle


_C1_PACKAGED_PATHS: dict[str, tuple[str, str]] = {
    "candidate-specification": ("candidate_specification", "spec/specification.json"),
    "candidate-profile-document": ("candidate_profile_document", "profile/profile.json"),
    "candidate-source-retained": ("source_glb", "glb/source.glb"),
    "candidate-raw-glb": ("raw_glb", "glb/raw.glb"),
    "candidate-processed-glb": ("processed_glb", "glb/processed.glb"),
    "candidate-static-validation-report": ("static_validation_report", "static/report.json"),
    "candidate-runtime-request": ("runtime_request", "runtime/request.json"),
    "candidate-runtime-observation": ("runtime_observation", "runtime/observation.json"),
    "candidate-runtime-provenance": ("runtime_provenance", "runtime/provenance.json"),
    "candidate-runtime-import-log": ("runtime_import_log", "runtime/godot-import.log"),
    "candidate-runtime-render-log": ("runtime_render_log", "runtime/godot-render.log"),
    "candidate-rig-attempt-wrapper": ("rig_attempt_wrapper", "rig/wrapper.json"),
    "candidate-identity-report": ("identity_report", "identity/report.json"),
}


def _scope_role_or_nested_manifest(artifact_type: str) -> str:
    if artifact_type == "candidate-nested-rig-manifest":
        return "rig/manifest.json"
    if artifact_type == "candidate-runtime-capture":
        return "runtime_capture"
    role, _path = _C1_PACKAGED_PATHS[artifact_type]
    return role


def _packaged_relpath(artifact_type: str, relative_path: str) -> tuple[str, str]:
    if artifact_type == "candidate-runtime-capture":
        view = Path(relative_path.replace("\\", "/")).stem
        return "runtime_capture", f"runtime/captures/{view}.png"
    if artifact_type == "candidate-nested-rig-manifest":
        return "rig_attempt_wrapper", f"{NESTED_PACKAGED_PATH}/manifest.json"
    return _C1_PACKAGED_PATHS[artifact_type]


def _snapshot_attempt_number(snapshot: dict[str, Any], execution_id: str) -> int:
    for key in (
        "prepare_execution",
        "identity_execution",
        "static_execution",
        "capture_execution",
        "oracle_execution",
    ):
        block = snapshot.get(key)
        if isinstance(block, dict) and block.get("id") == execution_id:
            attempt = block.get("attempt_number")
            if isinstance(attempt, int) and not isinstance(attempt, bool):
                return attempt
    raise ValueError(f"unknown execution id in snapshot: {execution_id}")


def _nested_tree_file_bytes(root_dir: Path) -> dict[str, bytes]:
    inventory: dict[str, bytes] = {}
    for path in sorted(root_dir.rglob("*")):
        if path.is_file():
            inventory[path.relative_to(root_dir).as_posix()] = path.read_bytes()
    return inventory


def _copy_actual_nested_bundle(*, root: Path, nested_bundle_dir: str, bundle: Path) -> Path:
    nested_src = root / nested_bundle_dir
    if not nested_src.is_dir():
        raise AssertionError(f"actual nested bundle missing: {nested_src}")
    nested_dest = bundle / NESTED_PACKAGED_PATH
    if nested_dest.exists():
        shutil.rmtree(nested_dest)
    nested_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(nested_src, nested_dest)
    return nested_dest


def _staged_shaped_capsule_runtime(
    godot: Path,
    glb_path: Path,
    spec: Any,
    profile: Any,
    output_dir: Path,
    *,
    workflow_id: str,
    revision: int,
    execution_id: str,
    strict_attempt_number: int,
    runner: ProcessRunner | None = None,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    observation = _fake_capsule_runtime(
        godot,
        glb_path,
        spec,
        profile,
        output_dir,
        workflow_id=workflow_id,
        revision=revision,
        execution_id=execution_id,
        strict_attempt_number=strict_attempt_number,
        runner=runner,
        timeout_seconds=timeout_seconds,
    )
    stage_dirs = [
        p
        for p in output_dir.iterdir()
        if p.is_dir() and p.name.startswith("godot_candidate_stage_")
    ]
    if len(stage_dirs) != 1:
        raise AssertionError("expected exactly one capsule runtime stage directory")
    stage = stage_dirs[0]
    request_path = stage / "request.json"
    observation_path = stage / "observation.json"
    capture_dir = stage / "captures"
    request_doc = json.loads(request_path.read_text(encoding="utf-8"))
    request_doc["glb"] = "res://Main.tscn"
    request_doc["capture_dir"] = str(capture_dir.resolve())
    request_doc["observation_path"] = str(observation_path.resolve())
    request_doc["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(request_doc)
    request_doc["request_digest"] = candidate_runtime_request_digest(request_doc)
    request_path.write_text(json.dumps(request_doc, sort_keys=True) + "\n", encoding="utf-8")
    observation_doc = json.loads(observation_path.read_text(encoding="utf-8"))
    observation_doc["request_digest"] = request_doc["request_digest"]
    observation_path.write_text(
        json.dumps(observation_doc, sort_keys=True) + "\n", encoding="utf-8"
    )
    provenance_path = stage / "provenance.json"
    if provenance_path.is_file():
        provenance_doc = json.loads(provenance_path.read_text(encoding="utf-8"))
        bound_request = {
            k: v
            for k, v in request_doc.items()
            if k not in {"glb", "capture_dir", "observation_path"}
        }
        provenance_doc["bound_request"] = bound_request
        provenance_path.write_text(
            json.dumps(provenance_doc, sort_keys=True) + "\n", encoding="utf-8"
        )
    observation["request_digest"] = request_doc["request_digest"]
    return observation


def _assert_c1_derived_bundle_copy_fidelity(
    *,
    workspace: Any,
    workflow_id: str,
    bundle: Path,
    original_snapshot: dict[str, Any],
) -> None:
    root = workspace.root
    arts = {a.id: a for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)}
    snap = json.loads((bundle / "snapshot/snapshot.json").read_text(encoding="utf-8"))
    original = json.loads(json.dumps(original_snapshot))
    orig_bindings = original["pre_review_artifact_bindings"]
    assert len(orig_bindings) == 23
    bundle_bindings = snap["pre_review_artifact_bindings"]
    assert len(bundle_bindings) == 23
    orig_by_id = {row["artifact_id"]: row for row in orig_bindings}
    bundle_by_id = {row["artifact_id"]: row for row in bundle_bindings}
    for artifact_id, row in orig_by_id.items():
        assert bundle_by_id[artifact_id] == row
    identity_art = next(a for a in arts.values() if a.artifact_type == "candidate-identity-report")
    identity_row = bundle_by_id[identity_art.id]
    assert identity_row["role"] == "candidate-identity-report"
    assert identity_row["relative_path"] == identity_art.relative_path.replace("\\", "/")
    assert identity_row["content_sha256"] == identity_art.content_hash
    assert identity_row["size_bytes"] == identity_art.file_size
    assert snap["identity_report_sha256"] == identity_art.content_hash
    for key, value in original.items():
        if key == "pre_review_artifact_bindings":
            continue
        assert snap[key] == value, key

    wrapper_art = next(
        a for a in arts.values() if a.artifact_type == "candidate-rig-attempt-wrapper"
    )
    wrapper_src = (root / wrapper_art.relative_path).read_bytes()
    assert (bundle / "rig/wrapper.json").read_bytes() == wrapper_src
    wrapper_doc = json.loads(wrapper_src.decode("utf-8"))
    nested_src = root / str(wrapper_doc["nested_bundle_dir"])
    nested_dest = bundle / NESTED_PACKAGED_PATH
    assert _nested_tree_file_bytes(nested_src) == _nested_tree_file_bytes(nested_dest)

    for row in bundle_bindings:
        artifact = arts[row["artifact_id"]]
        rel_path = artifact.relative_path.replace("\\", "/")
        _role, bundle_rel = _packaged_relpath(artifact.artifact_type, rel_path)
        if artifact.artifact_type == "candidate-rig-attempt-wrapper":
            copied = (bundle / bundle_rel).read_bytes()
            assert copied == wrapper_src
            continue
        if artifact.artifact_type == "candidate-nested-rig-manifest":
            manifest_src = (root / rel_path).read_bytes()
            assert (bundle / f"{NESTED_PACKAGED_PATH}/manifest.json").read_bytes() == manifest_src
            continue
        assert (bundle / bundle_rel).read_bytes() == (root / rel_path).read_bytes()

    scope = json.loads((bundle / "approval/scope.json").read_text(encoding="utf-8"))
    nested_expected_rel = f"{str(wrapper_doc['nested_bundle_dir']).rstrip('/')}/manifest.json"
    for entry in scope["entries"]:
        artifact = arts[entry["artifact_id"]]
        rel_path = entry["relative_path"].replace("\\", "/")
        bundle_path = entry["bundle_path"]
        if artifact.artifact_type == "candidate-nested-rig-manifest":
            assert rel_path == nested_expected_rel.replace("\\", "/")
            assert bundle_path == f"{NESTED_PACKAGED_PATH}/manifest.json"
            file_raw = (bundle / bundle_path).read_bytes()
        elif artifact.artifact_type == "candidate-rig-attempt-wrapper":
            file_raw = wrapper_src
            assert bundle_path == "rig/wrapper.json"
            assert rel_path == wrapper_art.relative_path.replace("\\", "/")
        else:
            file_raw = (root / artifact.relative_path).read_bytes()
        assert entry["content_hash"] == _digest(file_raw)
        assert entry["size"] == len(file_raw)
        assert (bundle / bundle_path).read_bytes() == file_raw


def _assert_c1_derived_cold_success(bundle: Path) -> dict[str, Any]:
    code, out, err = _run_cold(bundle)
    assert code == 0, out + err
    lines = out.strip().splitlines()
    assert lines[0] == "CONSISTENT_BUT_UNAUTHENTICATED"
    result = json.loads(lines[1])
    assert result["integrity_outcome"] == "VERIFIED"
    assert result["validation_status"] == "PASS"
    assert result["runtime_status"] == "PASS"
    assert result["reviewed_pins_match"] is True
    assert result["production_eligible"] is False
    assert result["promotion_eligible"] is False
    return result


def _build_c1_derived_candidate_evidence_bundle(
    tmp_path: Path,
    *,
    workspace: Any,
    workflow_id: str,
    handlers: Any,
) -> Path:
    root = workspace.root
    db = workspace.db
    workflow = WorkflowRepository(db).get(workflow_id)
    if workflow is None:
        raise AssertionError("workflow missing")
    prepare = next(
        t
        for t in TaskRepository(db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_prepare"
    )
    review = next(
        t
        for t in TaskRepository(db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_test_only_review"
    )
    bound = handlers._bound_snapshot(workflow, prepare)
    snapshot = json.loads(json.dumps(bound.payload))
    artifacts = {item.id: item for item in ArtifactRepository(db).list_by_workflow(workflow_id)}
    identity_art = next(
        a for a in artifacts.values() if a.artifact_type == "candidate-identity-report"
    )
    bindings = list(snapshot.get("pre_review_artifact_bindings", []))
    assert len(bindings) == 23
    assert snapshot.get("identity_report_sha256") == identity_art.content_hash

    wrapper_art = next(
        a for a in artifacts.values() if a.artifact_type == "candidate-rig-attempt-wrapper"
    )
    wrapper_raw = (root / wrapper_art.relative_path).read_bytes()
    wrapper_doc = json.loads(wrapper_raw.decode("utf-8"))

    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True, exist_ok=True)
    _copy_actual_nested_bundle(
        root=root,
        nested_bundle_dir=str(wrapper_doc["nested_bundle_dir"]),
        bundle=bundle,
    )
    nested_manifest_raw = (bundle / NESTED_PACKAGED_PATH / "manifest.json").read_bytes()

    contract_bytes = (
        resources.files("gamefactory.resources.v08_candidate")
        .joinpath("candidate-runtime-contract-0.8.0.json")
        .read_bytes()
    )
    harness_bytes = Path(
        str(resources.files("gamefactory.resources.godot").joinpath("candidate_capsule_harness.gd"))
    ).read_bytes()

    files: list[dict[str, Any]] = []

    def _write(rel: str, role: str, raw: bytes, **meta: Any) -> None:
        dest = bundle / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        entry: dict[str, Any] = {
            "path": rel,
            "role": role,
            "size": len(raw),
            "sha256": _digest(raw),
        }
        if "angle" in meta:
            entry["angle"] = meta["angle"]
        files.append(entry)

    for row in bindings:
        artifact = artifacts.get(row["artifact_id"])
        if artifact is None:
            raise AssertionError(f"missing artifact {row['artifact_id']}")
        rel_path = artifact.relative_path.replace("\\", "/")
        raw = (root / rel_path).read_bytes()
        assert raw == (root / artifact.relative_path).read_bytes()
        role, bundle_rel = _packaged_relpath(artifact.artifact_type, rel_path)
        if artifact.artifact_type == "candidate-nested-rig-manifest":
            continue
        if artifact.artifact_type == "candidate-rig-attempt-wrapper":
            continue
        angle = (
            Path(rel_path).stem if artifact.artifact_type == "candidate-runtime-capture" else None
        )
        _write(bundle_rel, role, raw, angle=angle)

    _write("rig/wrapper.json", "rig_attempt_wrapper", wrapper_raw)
    _write("contract/runtime-contract.json", "candidate_runtime_contract", contract_bytes)
    _write("reviewed/candidate_capsule_harness.gd", "reviewed_candidate_harness", harness_bytes)

    snapshot_fp = _snapshot_fingerprint(snapshot)
    snapshot_raw = _json_file(snapshot)
    _write("snapshot/snapshot.json", "snapshot", snapshot_raw)

    scope_entries: list[dict[str, Any]] = []
    for row in bindings:
        artifact = artifacts[row["artifact_id"]]
        rel_path = row["relative_path"].replace("\\", "/")
        role, bundle_rel = _packaged_relpath(artifact.artifact_type, rel_path)
        if artifact.artifact_type == "candidate-rig-attempt-wrapper":
            file_raw = wrapper_raw
        elif artifact.artifact_type == "candidate-nested-rig-manifest":
            file_raw = nested_manifest_raw
            bundle_rel = f"{NESTED_PACKAGED_PATH}/manifest.json"
        else:
            file_raw = (bundle / bundle_rel).read_bytes()
        scope_entries.append(
            {
                "artifact_id": row["artifact_id"],
                "artifact_type": artifact.artifact_type,
                "content_hash": _digest(file_raw),
                "task_id": row["task_id"],
                "execution_id": row["producing_execution_id"],
                "attempt_number": _snapshot_attempt_number(snapshot, row["producing_execution_id"]),
                "relative_path": rel_path,
                "bundle_path": bundle_rel,
                "size": len(file_raw),
                "role_or_nested_manifest": _scope_role_or_nested_manifest(artifact.artifact_type),
            }
        )
    scope_entries.sort(key=lambda item: item["artifact_id"])

    handler_context = {
        "workflow_id": workflow_id,
        "revision": review.parameters["revision_number"],
        "specification_hash": review.parameters["specification_hash"],
        "profile_document_hash": review.parameters["profile_document_hash"],
        "snapshot_fingerprint": snapshot_fp,
        "receipt_scope": "candidate_test_only",
        "production_eligible": False,
        "promotion_eligible": False,
        "candidate_test_only": True,
    }
    operation_inputs = {
        "parameters": review.parameters,
        "scope": {
            "workflow_id": workflow_id,
            "task_type": review.task_type,
            "cost_class": review.cost_class,
            "provider": None,
            "artifacts": sorted((e["artifact_id"], e["content_hash"]) for e in scope_entries),
            "estimated_cost": 0.0,
            "handler_context": handler_context,
        },
    }
    op_hash = compute_operation_hash(review.id, "candidate_test_only_review", operation_inputs)
    scope = {
        "schema_version": "candidate-approval-scope-0.8.0",
        "approval_id": "APP-C1-UNIT",
        "approval_status": "APPROVED",
        "review_execution_id": "E-REVIEW-C1-UNIT",
        "review_task": {
            "id": review.id,
            "task_type": review.task_type,
            "cost_class": review.cost_class,
            "parameters": review.parameters,
        },
        "persisted_approval_artifact_ids": [e["artifact_id"] for e in scope_entries],
        "entries": scope_entries,
        "operation_hash": op_hash,
    }
    _write("approval/scope.json", "approval_scope", _json_file(scope))
    receipt = {
        "approval_id": "APP-C1-UNIT",
        "approval_type": "candidate_test_only_review",
        "status": "APPROVED",
        "receipt_scope": "candidate_test_only",
        "production_eligible": False,
        "promotion_eligible": False,
        "fingerprint": op_hash,
        "snapshot_fingerprint": snapshot_fp,
        "approval_operation_hash": op_hash,
        "workflow_id": workflow_id,
        "task_id": review.id,
        "review_execution_id": "E-REVIEW-C1-UNIT",
        "approval_status": "APPROVED",
    }
    _write("receipt/test-only.json", "test_only_receipt", _json_file(receipt))

    manifest = {
        "schema_version": "candidate-evidence-0.8.0",
        "bundle_id": f"candidate-c1-derived-{workflow_id}",
        "workflow_id": workflow_id,
        "specification_hash": review.parameters["specification_hash"],
        "profile_document_hash": review.parameters["profile_document_hash"],
        "source_glb_sha256": snapshot["source_glb_hash"],
        "validation_status": "PASS",
        "runtime_status": "PASS",
        "reviewed_pins_match": True,
        "snapshot_fingerprint": snapshot_fp,
        "fixture_label": "unit_c1_derived_unsigned_fixture_v083c2a",
        "nested_bundle_path": NESTED_PACKAGED_PATH,
        "files": sorted(files, key=lambda item: item["path"]),
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return bundle


def _run_cold(bundle: Path, verifier: Path = VERIFIER) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-I", str(verifier), str(bundle)],
        capture_output=True,
        text=True,
        cwd=bundle.parent,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _failure_reason(out: str) -> str:
    lines = out.strip().splitlines()
    return json.loads(lines[1])["error"]


def _load_verifier_module() -> Any:
    spec = importlib.util.spec_from_file_location("verify_candidate_bundle_mod", VERIFIER_CHECKOUT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _resync_snapshot_observation_chain(bundle: Path) -> None:
    obs_raw = (bundle / "runtime/observation.json").read_bytes()
    snap = json.loads((bundle / "snapshot/snapshot.json").read_text(encoding="utf-8"))
    snap.pop("snapshot_fingerprint", None)
    obs_sha = _digest(obs_raw)
    snap["runtime_observation_sha256"] = obs_sha
    for row in snap["pre_review_artifact_bindings"]:
        if row.get("role") == "candidate-runtime-observation":
            row["content_sha256"] = obs_sha
            row["size_bytes"] = len(obs_raw)
    snap_fp = _snapshot_fingerprint(snap)
    _refresh_manifest(bundle, "snapshot/snapshot.json", _json_file(snap))
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["snapshot_fingerprint"] = snap_fp
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _rehash_manifest(bundle)
    _resync_scope_and_receipt(bundle)


def _load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate_manifest_against_schema(manifest: dict[str, Any]) -> None:
    schema = _load_schema()
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(manifest)
    paths: set[str] = set()
    for entry in manifest["files"]:
        rel = entry["path"]
        if rel in paths:
            raise ValidationError("duplicate manifest inventory path")
        paths.add(rel)
    roles: dict[str, int] = {}
    angles: set[str] = set()
    paths: set[str] = set()
    for entry in manifest["files"]:
        role = entry["role"]
        roles[role] = roles.get(role, 0) + 1
        rel = entry["path"]
        if rel in paths:
            raise AssertionError("duplicate manifest inventory path")
        paths.add(rel)
        if role == "runtime_capture":
            angle = entry.get("angle")
            if not isinstance(angle, str) or angle in angles:
                raise AssertionError("runtime_capture view angle invalid or duplicate")
            angles.add(angle)
    for role in _SINGLE_MANIFEST_ROLES:
        if roles.get(role, 0) != 1:
            raise AssertionError(f"manifest role cardinality for {role}")
    if roles.get("runtime_capture", 0) != 9 or angles != _NINE_VIEWS:
        raise AssertionError("manifest runtime_capture views incomplete")


def test_schema_file_exists() -> None:
    schema = _load_schema()
    assert schema["properties"]["schema_version"]["const"] == "candidate-evidence-0.8.0"
    assert schema.get("additionalProperties") is False


def test_schema_accepts_positive_fixture_manifest(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    _validate_manifest_against_schema(manifest)


@pytest.mark.parametrize(
    ("mutator", "needle"),
    [
        (lambda m: m.pop("nested_bundle_path"), "nested_bundle_path"),
        (lambda m: m.pop("snapshot_fingerprint"), "snapshot_fingerprint"),
        (lambda m: m.update({"extra_field": True}), "unexpected"),
        (lambda m: m.__setitem__("reviewed_pins_match", 1), "boolean"),
        (
            lambda m: m["files"].__setitem__(1, {**m["files"][1], "path": m["files"][0]["path"]}),
            "duplicate",
        ),
        (lambda m: m["files"][0].update({"bogus": 1}), "unexpected"),
        (lambda m: m["files"][0].__setitem__("role", "not_a_role"), "is not one of"),
    ],
)
def test_schema_rejects_invalid_manifests(
    tmp_path: Path, mutator: Callable[[dict[str, Any]], None], needle: str
) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    mutator(manifest)
    with pytest.raises(ValidationError, match=needle):
        _validate_manifest_against_schema(manifest)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _png_paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_filter_encode(filter_type: int, row: bytes, previous: bytes, bpp: int) -> bytes:
    if filter_type == 0:
        return row
    encoded = bytearray(len(row))
    for index, raw in enumerate(row):
        left = row[index - bpp] if index >= bpp else 0
        up = previous[index] if previous else 0
        up_left = previous[index - bpp] if previous and index >= bpp else 0
        if filter_type == 1:
            encoded[index] = (raw - left) & 0xFF
        elif filter_type == 2:
            encoded[index] = (raw - up) & 0xFF
        elif filter_type == 3:
            encoded[index] = (raw - ((left + up) // 2)) & 0xFF
        else:
            encoded[index] = (raw - _png_paeth(left, up, up_left)) & 0xFF
    return bytes(encoded)


def _make_capture_png(
    width: int,
    height: int,
    rgb: tuple[int, int, int],
    *,
    rgba: bool = False,
    alpha: int = 255,
    filter_type: int = 0,
    vary_row: bool = False,
) -> bytes:
    channels = 4 if rgba else 3
    color_type = 6 if rgba else 2
    bpp = channels
    rows = bytearray()
    previous = b""
    for row in range(height):
        raw_row = bytearray()
        for column in range(width):
            red, green, blue = rgb
            if vary_row and column == row % width:
                red = min(255, red + 1)
            if rgba:
                raw_row.extend((red, green, blue, alpha))
            else:
                raw_row.extend((red, green, blue))
        filtered = _png_filter_encode(filter_type, bytes(raw_row), previous, bpp)
        rows.append(filter_type)
        rows.extend(filtered)
        previous = bytes(raw_row)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + _png_chunk(b"IEND", b"")
    )


def _decode_capture_png(raw: bytes) -> tuple[int, int, bytes, int]:
    module = runpy.run_path(str(VERIFIER_CHECKOUT))
    return module["_decode_runtime_capture_png"](raw)


def _sync_capture_pngs(bundle: Path, png_raw: bytes, view: str | None = None) -> None:
    targets = [view] if view else sorted(_NINE_VIEWS)
    for angle in targets:
        rel = f"runtime/captures/{angle}.png"
        _refresh_manifest(bundle, rel, png_raw, angle=angle)
    obs = json.loads((bundle / "runtime/observation.json").read_text(encoding="utf-8"))
    for entry in obs["captures"]:
        if view is None or entry["view"] == view:
            entry["png_sha256"] = _digest(png_raw)
            entry["png_width"] = 1280
            entry["png_height"] = 720
    _refresh_manifest(bundle, "runtime/observation.json", _json_file(obs))
    _rehash_manifest(bundle)
    _resync_scope_and_receipt(bundle)


@pytest.mark.parametrize("filter_type", [0, 1, 2, 3, 4])
def test_png_decoder_accepts_filters_rgb8(filter_type: int) -> None:
    raw = _make_capture_png(1280, 720, (40, 80, 120), filter_type=filter_type, vary_row=True)
    width, height, pixels, channels = _decode_capture_png(raw)
    assert (width, height, channels) == (1280, 720, 3)
    assert len(pixels) == width * height * channels


def test_png_nonblank_rejects_uniform_rgb8() -> None:
    raw = _make_capture_png(1280, 720, (10, 20, 30))
    module = runpy.run_path(str(VERIFIER_CHECKOUT))
    _, _, pixels, channels = module["_decode_runtime_capture_png"](raw)
    with pytest.raises(ValueError, match="blank or uniform"):
        module["_png_nonblank"](pixels, 1280, 720, channels)


def test_png_decoder_rejects_palette_chunk() -> None:
    ihdr = struct.pack(">IIBBBBB", 1280, 720, 8, 2, 0, 0, 0)
    raw = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"PLTE", b"\x00\x00\x00")
        + _png_chunk(b"IDAT", zlib.compress(b"\x00" + bytes([1, 2, 3] * 1280)))
        + _png_chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError, match="palette"):
        _decode_capture_png(raw)


def test_png_decoder_rejects_inflate_bomb_dimensions() -> None:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(b"\x00" + b"x" * 500_000))
        + _png_chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError, match="1280x720"):
        _decode_capture_png(raw)


def test_full_envelope_rejects_uniform_rgb_captures(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    uniform = _make_capture_png(1280, 720, (10, 20, 30))
    _sync_capture_pngs(bundle, uniform)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "blank or uniform" in _failure_reason(out).lower()


def test_full_envelope_rejects_uniform_rgba_captures(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    uniform = _make_capture_png(1280, 720, (10, 20, 30), rgba=True, alpha=40)
    _sync_capture_pngs(bundle, uniform)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "blank or uniform" in _failure_reason(out).lower()


def test_full_envelope_rejects_fully_transparent_rgba_captures(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    transparent = _make_capture_png(1280, 720, (10, 20, 30), rgba=True, alpha=0, vary_row=True)
    _sync_capture_pngs(bundle, transparent)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "transparent" in _failure_reason(out).lower()


def test_positive_unit_unsigned_fixture(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    code, out, err = _run_cold(bundle)
    assert code == 0, out + err
    lines = out.strip().splitlines()
    assert lines[0] == "CONSISTENT_BUT_UNAUTHENTICATED"
    payload = json.loads(lines[1])
    assert payload["integrity_outcome"] == "VERIFIED"
    assert payload["validation_status"] == "PASS"
    assert payload["runtime_status"] == "PASS"
    assert payload["reviewed_pins_match"] is True
    assert payload["candidate_evidence_complete"] is True
    assert payload["production_eligible"] is False
    assert payload["promotion_eligible"] is False
    assert "cold_attestation_limits" in payload


def test_nested_packaged_path_differs_from_historical_wrapper_dir(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    wrapper_before = (bundle / "rig/wrapper.json").read_bytes()
    wrapper_doc = json.loads(wrapper_before.decode("utf-8"))
    assert wrapper_doc["nested_bundle_dir"] == HISTORICAL_NESTED_DIR
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["nested_bundle_path"] == NESTED_PACKAGED_PATH
    assert wrapper_doc["nested_bundle_dir"] != manifest["nested_bundle_path"]
    code, out, err = _run_cold(bundle)
    assert code == 0, out + err
    assert (bundle / "rig/wrapper.json").read_bytes() == wrapper_before


def test_positive_fixture_matches_package_static_validation(tmp_path: Path) -> None:
    glb_path = tmp_path / "humanoid.glb"
    glb_path.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb_path)
    profile = load_packaged_candidate_profile()
    package_result = validate_v08_candidate_glb(glb_path, spec, profile=profile)
    assert package_result.status == "PASS"
    bundle = _build_candidate_evidence_bundle(tmp_path)
    code, out, err = _run_cold(bundle)
    assert code == 0, out + err


def test_rejects_view_framing_inside_margin_observation_field(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    obs = json.loads((bundle / "runtime/observation.json").read_text(encoding="utf-8"))
    obs["view_framing"]["front"]["inside_margin"] = True
    _refresh_manifest(bundle, "runtime/observation.json", _json_file(obs))
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "unknown keys" in _failure_reason(out).lower()


def test_rejects_outside_margin_framing(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    obs = json.loads((bundle / "runtime/observation.json").read_text(encoding="utf-8"))
    entry = obs["view_framing"]["front"]
    rect = entry["projected_rect_pixels"]
    rect["x"] = 0.0
    rect["y"] = 0.0
    entry["ok"] = True
    _refresh_manifest(bundle, "runtime/observation.json", _json_file(obs))
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _failure_reason(out).lower()
    assert "margin" in reason or "center_offset" in reason


def test_rejects_wrong_manifest_nested_bundle_path(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["nested_bundle_path"] = "rig/wrong-nested"
    (bundle / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _failure_reason(out).lower()
    assert "unlisted" in reason or "missing" in reason


def test_rejects_scope_missing_approval_id(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    scope = json.loads((bundle / "approval/scope.json").read_text(encoding="utf-8"))
    scope.pop("approval_id")
    _refresh_manifest(bundle, "approval/scope.json", _json_file(scope))
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "approval_id" in _failure_reason(out).lower()


def test_packaged_copy_parity() -> None:
    assert VERIFIER.read_bytes() == VERIFIER_CHECKOUT.read_bytes()


def test_outside_checkout_positive_and_negative(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    outside = tmp_path / "isolated"
    outside.mkdir()
    isolated_script = outside / "verify_candidate_bundle.py"
    isolated_script.write_bytes(VERIFIER_CHECKOUT.read_bytes())
    isolated_rig = outside / "verify_rig_bundle.py"
    isolated_rig.write_bytes((REPO / "scripts" / "verify_rig_bundle.py").read_bytes())
    code, out, err = _run_cold(bundle, isolated_script)
    assert code == 0, out + err
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "manifest.json").write_text("{}", encoding="utf-8")
    code2, out2, _ = _run_cold(bad, isolated_script)
    assert code2 == 1
    assert out2.splitlines()[0] == "FAILED"


def test_expanded_approval_scope_fail_closed(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    scope_path = bundle / "approval/scope.json"
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    scope["persisted_approval_artifact_ids"].append("ART-EXTRA")
    scope["entries"].append(
        {
            "artifact_id": "ART-EXTRA",
            "artifact_type": "candidate-static-validation-report",
            "content_hash": "0" * 64,
            "task_id": "T-STATIC",
            "execution_id": "E-STATIC",
            "attempt_number": 1,
            "relative_path": "static/report.json",
            "bundle_path": "static/report.json",
            "size": 1,
            "role_or_nested_manifest": "static_validation_report",
        }
    )
    _refresh_manifest(bundle, "approval/scope.json", _json_file(scope))
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "expanded" in _failure_reason(out).lower()


def test_prior_receipt_in_scope_fail_closed(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    scope_path = bundle / "approval/scope.json"
    scope = json.loads(scope_path.read_text(encoding="utf-8"))
    scope["persisted_approval_artifact_ids"].append("ART-PRIOR-RECEIPT")
    scope["entries"].append(
        {
            "artifact_id": "ART-PRIOR-RECEIPT",
            "artifact_type": "candidate-test-only-receipt",
            "content_hash": "a" * 64,
            "task_id": "T-REVIEW",
            "execution_id": "E-OLD",
            "attempt_number": 1,
            "relative_path": "receipt/old.json",
            "bundle_path": "receipt/old.json",
            "size": 10,
            "role_or_nested_manifest": "test_only_receipt",
        }
    )
    _refresh_manifest(bundle, "approval/scope.json", _json_file(scope))
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "receipt" in _failure_reason(out).lower()


def _mutate_bundle(tmp_path: Path, mutator: Callable[[Path], None]) -> Path:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    mutator(bundle)
    return bundle


def _tamper_glb(bundle: Path, variant: str) -> None:
    raw = build_humanoid_skinned_glb(variant)
    _sync_glb_bytes(bundle, raw)
    report = json.loads((bundle / "static/report.json").read_text(encoding="utf-8"))
    report["status"] = "PASS"
    report["passed"] = True
    _refresh_manifest(bundle, "static/report.json", _json_file(report))
    _rehash_manifest(bundle)
    _resync_scope_and_receipt(bundle)


def _neg_weight_sum(b: Path) -> None:
    _tamper_glb(b, "weight_sum")


def _neg_wrong_parent(b: Path) -> None:
    _tamper_glb(b, "wrong_parent")


def _neg_inverse_bind(b: Path) -> None:
    _tamper_glb(b, "inverse_bind_mismatch")


def _neg_rest(b: Path) -> None:
    _tamper_glb(b, "rest_mismatch")


def _neg_animation(b: Path) -> None:
    _tamper_glb(b, "animation_present")


def _neg_contract_pin(b: Path) -> None:
    contract = json.loads((b / "contract/runtime-contract.json").read_text(encoding="utf-8"))
    contract["framing"]["fov_degrees"] = 41.0
    _refresh_manifest(b, "contract/runtime-contract.json", _json_file(contract))


def _neg_harness_pin(b: Path) -> None:
    _refresh_manifest(b, "reviewed/candidate_capsule_harness.gd", b"# tamper\n")


def _neg_obs_capsule(b: Path) -> None:
    obs = json.loads((b / "runtime/observation.json").read_text(encoding="utf-8"))
    obs["capsule"] = {
        "observed_radius_m": 9.9,
        "observed_height_m": 9.9,
        "center_m": [0, 0, 0],
        "shape_class": "CapsuleShape3D",
    }
    _refresh_manifest(b, "runtime/observation.json", _json_file(obs))


def _neg_req_center(b: Path) -> None:
    req = json.loads((b / "runtime/request.json").read_text(encoding="utf-8"))
    req["capsule_center_m"] = [9.0, 9.0, 9.0]
    req["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(req)
    req["request_digest"] = candidate_runtime_request_digest(req)
    _refresh_manifest(b, "runtime/request.json", _json_file(req))
    obs = json.loads((b / "runtime/observation.json").read_text(encoding="utf-8"))
    obs["request_digest"] = req["request_digest"]
    _refresh_manifest(b, "runtime/observation.json", _json_file(obs))
    prov = json.loads((b / "runtime/provenance.json").read_text(encoding="utf-8"))
    prov["bound_request"] = req
    _refresh_manifest(b, "runtime/provenance.json", _json_file(prov))
    _resync_scope_and_receipt(b)


def _neg_missing_capture(b: Path) -> None:
    manifest = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"] = [f for f in manifest["files"] if f.get("angle") != "front"]
    (b / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _neg_bad_png(b: Path) -> None:
    _refresh_manifest(b, "runtime/captures/front.png", b"not a png")


def _neg_snapshot_attempt(b: Path) -> None:
    snap = json.loads((b / "snapshot/snapshot.json").read_text(encoding="utf-8"))
    snap["prepare_execution"] = {"id": "X", "attempt_number": 9, "status": "FAILED"}
    _refresh_manifest(b, "snapshot/snapshot.json", _json_file(snap))


def _neg_stale_receipt(b: Path) -> None:
    rec = json.loads((b / "receipt/test-only.json").read_text(encoding="utf-8"))
    rec["approval_operation_hash"] = "0" * 64
    _refresh_manifest(b, "receipt/test-only.json", _json_file(rec))


def _neg_spec_hash(b: Path) -> None:
    spec = json.loads((b / "spec/specification.json").read_text(encoding="utf-8"))
    spec["asset_id"] = "asset-tampered"
    _refresh_manifest(b, "spec/specification.json", _json_file(spec))
    manifest = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    manifest["specification_hash"] = _digest(_json_file(spec))
    (b / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _neg_source_chain(b: Path) -> None:
    _refresh_manifest(b, "glb/source.glb", build_humanoid_skinned_glb("positive") + b"x")


def _neg_unsafe_path(b: Path) -> None:
    manifest = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../escape.glb"
    (b / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _neg_wrapper_hash(b: Path) -> None:
    wrapper = json.loads((b / "rig/wrapper.json").read_text(encoding="utf-8"))
    wrapper["processed_glb_sha256"] = "0" * 64
    _refresh_manifest(b, "rig/wrapper.json", _json_file(wrapper))


def _neg_scope_foreign_artifact_id(b: Path) -> None:
    scope = json.loads((b / "approval/scope.json").read_text(encoding="utf-8"))
    scope["entries"][0]["artifact_id"] = "ART-FOREIGN"
    scope["persisted_approval_artifact_ids"][0] = "ART-FOREIGN"
    _refresh_manifest(b, "approval/scope.json", _json_file(scope))
    _resync_scope_and_receipt(b)


def _neg_scope_missing_binding_id(b: Path) -> None:
    scope = json.loads((b / "approval/scope.json").read_text(encoding="utf-8"))
    removed = scope["entries"].pop(0)
    scope["persisted_approval_artifact_ids"] = [
        e for e in scope["persisted_approval_artifact_ids"] if e != removed["artifact_id"]
    ]
    _refresh_manifest(b, "approval/scope.json", _json_file(scope))
    _resync_scope_and_receipt(b)


def _neg_forged_review_parameters(b: Path) -> None:
    scope = json.loads((b / "approval/scope.json").read_text(encoding="utf-8"))
    scope["review_task"]["parameters"]["revision_number"] = 99
    _refresh_manifest(b, "approval/scope.json", _json_file(scope))


def _neg_identity_foreign_workflow(b: Path) -> None:
    ident = json.loads((b / "identity/report.json").read_text(encoding="utf-8"))
    ident["workflow_id"] = "WF-FOREIGN"
    _refresh_manifest(b, "identity/report.json", _json_file(ident))


def _neg_scope_foreign_task(b: Path) -> None:
    scope = json.loads((b / "approval/scope.json").read_text(encoding="utf-8"))
    scope["entries"][0]["task_id"] = "T-FOREIGN"
    _refresh_manifest(b, "approval/scope.json", _json_file(scope))
    _resync_scope_and_receipt(b)


def _neg_receipt_foreign_workflow(b: Path) -> None:
    rec = json.loads((b / "receipt/test-only.json").read_text(encoding="utf-8"))
    rec["workflow_id"] = "WF-FOREIGN"
    _refresh_manifest(b, "receipt/test-only.json", _json_file(rec))


def _neg_receipt_snapshot_drift(b: Path) -> None:
    rec = json.loads((b / "receipt/test-only.json").read_text(encoding="utf-8"))
    rec["snapshot_fingerprint"] = "f" * 64
    _refresh_manifest(b, "receipt/test-only.json", _json_file(rec))


def _neg_receipt_dual_hash_conflict(b: Path) -> None:
    rec = json.loads((b / "receipt/test-only.json").read_text(encoding="utf-8"))
    rec["fingerprint"] = "a" * 64
    _refresh_manifest(b, "receipt/test-only.json", _json_file(rec))


def _neg_forged_review_parameters_rehash(b: Path) -> None:
    scope = json.loads((b / "approval/scope.json").read_text(encoding="utf-8"))
    scope["review_task"]["parameters"]["asset_id"] = "ASSET-FORGED"
    _refresh_manifest(b, "approval/scope.json", _json_file(scope))
    _resync_scope_and_receipt(b)


def _neg_manifest_unknown_root_field(b: Path) -> None:
    manifest = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    manifest["unexpected_root"] = True
    (b / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _neg_duplicate_inventory_path(b: Path) -> None:
    manifest = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    dup = dict(manifest["files"][1])
    dup["role"] = manifest["files"][2]["role"]
    manifest["files"].insert(2, dup)
    (b / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _neg_false_oracle_status(b: Path) -> None:
    nested = b / "rig/nested/evidence/rig_runtime_observation.json"
    obs = json.loads(nested.read_text(encoding="utf-8"))
    obs["status"] = "FAIL"
    nested.write_text(json.dumps(obs, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    _rehash_manifest(b)


def _neg_unexpected_executable_role(b: Path) -> None:
    evil = b"print('untrusted')\n"
    (b / "evil.py").write_bytes(evil)
    manifest = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"].append(
        {
            "path": "evil.py",
            "role": "unexpected_executable",
            "size": len(evil),
            "sha256": _digest(evil),
        }
    )
    (b / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _neg_bool_obs_capsule_center_rehash(b: Path) -> None:
    obs = json.loads((b / "runtime/observation.json").read_text(encoding="utf-8"))
    center = obs["capsule"]["center_m"]
    obs["capsule"]["center_m"] = [center[0], center[1], False]
    _refresh_manifest(b, "runtime/observation.json", _json_file(obs))
    _resync_snapshot_observation_chain(b)


def _neg_numeric_string_capsule_center_rehash(b: Path) -> None:
    obs = json.loads((b / "runtime/observation.json").read_text(encoding="utf-8"))
    center = obs["capsule"]["center_m"]
    obs["capsule"]["center_m"] = [str(center[0]), center[1], center[2]]
    _refresh_manifest(b, "runtime/observation.json", _json_file(obs))
    _resync_snapshot_observation_chain(b)


def _neg_length4_capsule_center_rehash(b: Path) -> None:
    obs = json.loads((b / "runtime/observation.json").read_text(encoding="utf-8"))
    obs["capsule"]["center_m"] = obs["capsule"]["center_m"] + [0.0]
    _refresh_manifest(b, "runtime/observation.json", _json_file(obs))
    _resync_snapshot_observation_chain(b)


def _neg_numeric_string_projected_rect_rehash(b: Path) -> None:
    obs = json.loads((b / "runtime/observation.json").read_text(encoding="utf-8"))
    view = next(iter(obs["view_framing"]))
    obs["view_framing"][view]["projected_rect_pixels"]["width"] = "128"
    _refresh_manifest(b, "runtime/observation.json", _json_file(obs))
    _resync_snapshot_observation_chain(b)


def _neg_snapshot_capture_hash_map_shape(b: Path) -> None:
    snap = json.loads((b / "snapshot/snapshot.json").read_text(encoding="utf-8"))
    snap.pop("snapshot_fingerprint", None)
    obs = json.loads((b / "runtime/observation.json").read_text(encoding="utf-8"))
    snap["runtime_capture_hashes"] = {
        entry["view"]: entry["png_sha256"] for entry in obs["captures"]
    }
    snap_fp = _snapshot_fingerprint(snap)
    _refresh_manifest(b, "snapshot/snapshot.json", _json_file(snap))
    manifest = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    manifest["snapshot_fingerprint"] = snap_fp
    (b / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _rehash_manifest(b)
    _resync_scope_and_receipt(b)


def _neg_angle_on_non_capture_role(b: Path) -> None:
    manifest = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry.get("role") == "source_glb":
            entry["angle"] = "front"
            break
    (b / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _neg_swap_scope_capture_bundle_path_front_left(b: Path) -> None:
    scope = json.loads((b / "approval/scope.json").read_text(encoding="utf-8"))
    front = next(
        e
        for e in scope["entries"]
        if e.get("artifact_type") == "candidate-runtime-capture"
        and str(e.get("relative_path", "")).endswith("front.png")
    )
    left = next(
        e
        for e in scope["entries"]
        if e.get("artifact_type") == "candidate-runtime-capture"
        and str(e.get("relative_path", "")).endswith("left.png")
    )
    front["bundle_path"], left["bundle_path"] = left["bundle_path"], front["bundle_path"]
    _refresh_manifest(b, "approval/scope.json", _json_file(scope))
    _resync_scope_and_receipt(b)


def _neg_duplicate_capture_view_binding(b: Path) -> None:
    snap = json.loads((b / "snapshot/snapshot.json").read_text(encoding="utf-8"))
    snap.pop("snapshot_fingerprint", None)
    captures = [
        row
        for row in snap["pre_review_artifact_bindings"]
        if row.get("role") == "candidate-runtime-capture"
    ]
    left = next(row for row in captures if row["relative_path"].endswith("left.png"))
    front = next(row for row in captures if row["relative_path"].endswith("front.png"))
    left["relative_path"] = front["relative_path"]
    scope = json.loads((b / "approval/scope.json").read_text(encoding="utf-8"))
    for entry in scope["entries"]:
        if entry.get("artifact_id") == left["artifact_id"]:
            entry["relative_path"] = front["relative_path"]
    _refresh_manifest(b, "snapshot/snapshot.json", _json_file(snap))
    _refresh_manifest(b, "approval/scope.json", _json_file(scope))
    _resync_scope_and_receipt(b)


def _neg_wrong_capture_original_filename(b: Path) -> None:
    snap = json.loads((b / "snapshot/snapshot.json").read_text(encoding="utf-8"))
    snap.pop("snapshot_fingerprint", None)
    row = next(
        r
        for r in snap["pre_review_artifact_bindings"]
        if r.get("role") == "candidate-runtime-capture" and r["relative_path"].endswith("front.png")
    )
    row["relative_path"] = row["relative_path"].replace("front.png", "not_a_view.png")
    scope = json.loads((b / "approval/scope.json").read_text(encoding="utf-8"))
    entry = next(e for e in scope["entries"] if e.get("artifact_id") == row["artifact_id"])
    entry["relative_path"] = row["relative_path"]
    _refresh_manifest(b, "snapshot/snapshot.json", _json_file(snap))
    _refresh_manifest(b, "approval/scope.json", _json_file(scope))
    _resync_scope_and_receipt(b)


def _neg_observation_capture_view_hash_swap_rehash(b: Path) -> None:
    obs = json.loads((b / "runtime/observation.json").read_text(encoding="utf-8"))
    front = next(c for c in obs["captures"] if c["view"] == "front")
    left = next(c for c in obs["captures"] if c["view"] == "left")
    front_hash = front["png_sha256"]
    left_hash = left["png_sha256"]
    if front_hash == left_hash:
        left_hash = "f" * 64
    front["png_sha256"] = left_hash
    left["png_sha256"] = front_hash
    snap = json.loads((b / "snapshot/snapshot.json").read_text(encoding="utf-8"))
    snap.pop("snapshot_fingerprint", None)
    snap["runtime_capture_hashes"] = sorted(
        entry["png_sha256"] for entry in obs["captures"] if isinstance(entry, dict)
    )
    _refresh_manifest(b, "runtime/observation.json", _json_file(obs))
    _refresh_manifest(b, "snapshot/snapshot.json", _json_file(snap))
    manifest = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    manifest["snapshot_fingerprint"] = _snapshot_fingerprint(snap)
    (b / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _rehash_manifest(b)
    _resync_scope_and_receipt(b)


def _neg_unlisted_outer_regular_file(b: Path) -> None:
    (b / "outer_extra.txt").write_bytes(b"unlisted\n")


def _neg_unlisted_nested_regular_file(b: Path) -> None:
    (b / "rig/nested/evidence/unlisted_nested.txt").write_bytes(b"nested\n")


@pytest.mark.parametrize(
    ("mutator", "needle"),
    [
        (_neg_weight_sum, "static"),
        (_neg_wrong_parent, "static"),
        (_neg_inverse_bind, "static"),
        (_neg_rest, "static"),
        (_neg_animation, "animation"),
        (_neg_contract_pin, "contract"),
        (_neg_harness_pin, "harness"),
        (_neg_obs_capsule, "capsule"),
        (_neg_req_center, "capsule center"),
        (_neg_missing_capture, "runtime_capture"),
        (_neg_bad_png, "png"),
        (_neg_snapshot_attempt, "snapshot"),
        (_neg_stale_receipt, "receipt"),
        (_neg_spec_hash, "specification"),
        (_neg_source_chain, "source"),
        (_neg_unsafe_path, "path"),
        (_neg_wrapper_hash, "wrapper"),
        (_neg_false_oracle_status, "runtime"),
        (_neg_unexpected_executable_role, "not allowed"),
        (_neg_bool_obs_capsule_center_rehash, "finite number"),
        (_neg_numeric_string_capsule_center_rehash, "finite number"),
        (_neg_length4_capsule_center_rehash, "length-3"),
        (_neg_numeric_string_projected_rect_rehash, "finite number"),
        (_neg_snapshot_capture_hash_map_shape, "nine-entry list"),
        (_neg_angle_on_non_capture_role, "angle is only valid"),
        (_neg_swap_scope_capture_bundle_path_front_left, "view identity"),
        (_neg_duplicate_capture_view_binding, "duplicate snapshot capture view"),
        (_neg_wrong_capture_original_filename, "view identity"),
        (_neg_observation_capture_view_hash_swap_rehash, "png_sha256 mismatch"),
        (_neg_unlisted_outer_regular_file, "unlisted"),
        (_neg_unlisted_nested_regular_file, "unlisted"),
    ],
)
def test_semantic_negatives(tmp_path: Path, mutator: Callable[[Path], None], needle: str) -> None:
    bundle = _mutate_bundle(tmp_path, mutator)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert out.splitlines()[0] == "FAILED"
    reason = _failure_reason(out).lower()
    assert needle.lower() in reason


@pytest.mark.parametrize(
    ("mutator", "needle", "invariant"),
    [
        (_neg_scope_foreign_artifact_id, "artifact id", "scope IDs must equal snapshot 23-id set"),
        (_neg_scope_missing_binding_id, "artifact id", "scope must not omit snapshot binding IDs"),
        (
            _neg_forged_review_parameters,
            "revision_number",
            "review_task parameters bind to snapshot",
        ),
        (
            _neg_identity_foreign_workflow,
            "unknown keys",
            "identity_report rejects foreign workflow fields",
        ),
        (_neg_scope_foreign_task, "task_id", "scope task_id must match snapshot binding"),
        (_neg_receipt_foreign_workflow, "workflow", "receipt workflow_id must match manifest"),
        (_neg_receipt_snapshot_drift, "snapshot_fingerprint", "receipt binds snapshot fingerprint"),
        (_neg_receipt_dual_hash_conflict, "conflicts", "receipt dual hash fields must agree"),
        (
            _neg_forged_review_parameters_rehash,
            "asset_id",
            "forged review parameters fail semantic snapshot binding",
        ),
        (_neg_manifest_unknown_root_field, "unknown keys", "manifest rejects unknown root fields"),
        (_neg_duplicate_inventory_path, "duplicate", "inventory paths must be unique"),
    ],
)
def test_contract_semantic_negatives(
    tmp_path: Path,
    mutator: Callable[[Path], None],
    needle: str,
    invariant: str,
) -> None:
    bundle = _mutate_bundle(tmp_path, mutator)
    code, out, _ = _run_cold(bundle)
    assert code == 1, invariant
    assert needle.lower() in _failure_reason(out).lower()


def test_review_parameters_match_production_factory(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path / "ws")
    glb = workspace.root / "source.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb)
    _workflow, tasks = create_v08_candidate_workflow(workspace, glb, spec)
    review = next(t for t in tasks if t.task_type == "v08_candidate_test_only_review")
    bundle = _build_candidate_evidence_bundle(tmp_path / "evidence")
    scope = json.loads((bundle / "approval/scope.json").read_text(encoding="utf-8"))
    params = scope["review_task"]["parameters"]
    assert set(params) == set(review.parameters)
    assert params["graph_version"] == review.parameters["graph_version"]
    assert params["source_kind"] == review.parameters["source_kind"]
    assert params["paid_provider_invocations"] == review.parameters["paid_provider_invocations"]
    assert params["specification_hash"] == review.parameters["specification_hash"]


def test_stale_snapshot_workflow_scalar_fails_after_rehash(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    snap = json.loads((bundle / "snapshot/snapshot.json").read_text(encoding="utf-8"))
    snap["workflow_id"] = "WF-STALE-SCALAR"
    snap.pop("snapshot_fingerprint", None)
    snap_fp = _snapshot_fingerprint(snap)
    _refresh_manifest(bundle, "snapshot/snapshot.json", _json_file(snap))
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["snapshot_fingerprint"] = snap_fp
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _resync_scope_and_receipt(bundle)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "workflow_id" in _failure_reason(out).lower()


def test_json_rejects_nonfinite_number(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    bad = b'{"schema_version": "candidate-runtime-request-0.8.0", "value": 1e999}'
    (bundle / "runtime/request.json").write_bytes(bad)
    _rehash_manifest(bundle)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert (
        "invalid numeric token" in _failure_reason(out).lower()
        or "json" in _failure_reason(out).lower()
    )


def test_runtime_request_rejects_unknown_nested_field(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    req = json.loads((bundle / "runtime/request.json").read_text(encoding="utf-8"))
    req["capsule"]["bogus"] = 1.0
    _refresh_manifest(bundle, "runtime/request.json", _json_file(req))
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "unknown keys" in _failure_reason(out).lower()


def test_spec_rejects_bool_material_budget(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    spec = json.loads((bundle / "spec/specification.json").read_text(encoding="utf-8"))
    spec["material_budget"]["max_materials"] = True
    spec_raw = _json_file(spec)
    _refresh_manifest(bundle, "spec/specification.json", spec_raw)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["specification_hash"] = hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    scope = json.loads((bundle / "approval/scope.json").read_text(encoding="utf-8"))
    scope["review_task"]["parameters"]["specification"] = spec
    scope["review_task"]["parameters"]["specification_hash"] = manifest["specification_hash"]
    _refresh_manifest(bundle, "approval/scope.json", _json_file(scope))
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _failure_reason(out).lower()
    assert "strict integer" in reason or "material" in reason


def test_modified_trusted_rig_sibling_rejected(tmp_path: Path) -> None:
    """Invariant: trusted verify_rig_bundle.py pin is not bundle-selectable."""
    bundle = _build_candidate_evidence_bundle(tmp_path)
    outside = tmp_path / "isolated"
    outside.mkdir()
    isolated_script = outside / "verify_candidate_bundle.py"
    isolated_script.write_bytes(VERIFIER_CHECKOUT.read_bytes())
    isolated_rig = outside / "verify_rig_bundle.py"
    rig_bytes = (REPO / "scripts" / "verify_rig_bundle.py").read_bytes()
    isolated_rig.write_bytes(rig_bytes + b"\n")
    code, out, _ = _run_cold(bundle, isolated_script)
    assert code == 1
    assert "digest mismatch" in _failure_reason(out).lower()


def test_aggregate_bundle_size_rejects_before_nested_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    mod = _load_verifier_module()
    total = 0
    for current, _dirnames, filenames in os.walk(bundle, followlinks=False):
        base = Path(current)
        for name in filenames:
            child = base / name
            if child.is_file():
                total += child.stat().st_size
    monkeypatch.setattr(mod, "_MAX_BUNDLE_BYTES", total - 1)
    with pytest.raises(ValueError, match="aggregate size"):
        mod.verify_bundle(bundle)


def test_aggregate_bundle_size_allows_exact_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    mod = _load_verifier_module()
    total = 0
    for current, _dirnames, filenames in os.walk(bundle, followlinks=False):
        base = Path(current)
        for name in filenames:
            child = base / name
            if child.is_file():
                total += child.stat().st_size
    monkeypatch.setattr(mod, "_MAX_BUNDLE_BYTES", total)
    result = mod.verify_bundle(bundle)
    assert result["integrity_outcome"] == "VERIFIED"


def test_json_rejects_oversized_string(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    huge = "x" * (1_048_576 + 1)
    bad = json.dumps(
        {"schema_version": "candidate-runtime-request-0.8.0", "blob": huge},
        sort_keys=True,
    ).encode("utf-8")
    (bundle / "runtime/request.json").write_bytes(bad)
    _rehash_manifest(bundle)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "string exceeds length" in _failure_reason(out).lower()


def test_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    (bundle / "runtime/request.json").write_bytes(
        b'{"schema_version":"candidate-runtime-request-0.8.0","schema_version":"x"}'
    )
    _rehash_manifest(bundle)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "duplicate json key" in _failure_reason(out).lower()


def test_json_rejects_oversized_object_key(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    key = "k" * (_load_verifier_module()._MAX_JSON_STRING_LEN + 1)
    (bundle / "runtime/request.json").write_bytes(("{" + json.dumps(key) + ": 1}").encode("utf-8"))
    _rehash_manifest(bundle)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "string length" in _failure_reason(out).lower()


def test_json_rejects_excessive_depth(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    nested = "null"
    for _ in range(_load_verifier_module()._MAX_JSON_DEPTH + 2):
        nested = "{" + nested + "}"
    (bundle / "runtime/request.json").write_bytes(nested.encode("utf-8"))
    _rehash_manifest(bundle)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _failure_reason(out).lower()
    assert "depth" in reason or "json" in reason


def test_c1_pipeline_derived_full_envelope_cold_verified(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path / "ws")
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    workflow = WorkflowRepository(workspace.db).get(workflow_id)
    assert workflow is not None
    prepare = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_prepare"
    )
    review = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_test_only_review"
    )
    bound = handlers._bound_snapshot(workflow, prepare)
    payload = bound.payload
    capture_hashes = payload["runtime_capture_hashes"]
    assert isinstance(capture_hashes, list)
    assert len(capture_hashes) == 9
    assert capture_hashes == sorted(capture_hashes)
    original_snapshot = json.loads(json.dumps(payload))

    bundle = _build_c1_derived_candidate_evidence_bundle(
        tmp_path / "evidence",
        workspace=workspace,
        workflow_id=workflow_id,
        handlers=handlers,
    )
    arts = {a.id: a for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)}
    req_art = next(a for a in arts.values() if a.artifact_type == "candidate-runtime-request")
    req_bytes = (workspace.root / req_art.relative_path).read_bytes()
    assert (bundle / "runtime/request.json").read_bytes() == req_bytes
    req_doc = json.loads(req_bytes.decode("utf-8"))
    assert "glb" not in req_doc
    assert "capture_dir" not in req_doc
    assert "observation_path" not in req_doc
    scope = json.loads((bundle / "approval/scope.json").read_text(encoding="utf-8"))
    assert scope["review_task"]["parameters"] == review.parameters
    snap = json.loads((bundle / "snapshot/snapshot.json").read_text(encoding="utf-8"))
    assert len(snap["pre_review_artifact_bindings"]) == 23
    identity_art = next(a for a in arts.values() if a.artifact_type == "candidate-identity-report")
    assert snap["identity_report_sha256"] == identity_art.content_hash
    assert isinstance(snap["runtime_capture_hashes"], list)
    _assert_c1_derived_bundle_copy_fidelity(
        workspace=workspace,
        workflow_id=workflow_id,
        bundle=bundle,
        original_snapshot=original_snapshot,
    )
    _assert_c1_derived_cold_success(bundle)


def test_c1_pipeline_derived_staged_runtime_request_cold_verified(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path / "ws-staged")
    handlers = _candidate_workflow_handlers(workspace.root, workspace.db)
    object.__setattr__(handlers, "capsule_runtime", _staged_shaped_capsule_runtime)
    _engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    workflow = WorkflowRepository(workspace.db).get(workflow_id)
    assert workflow is not None
    prepare = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_prepare"
    )
    review = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_test_only_review"
    )
    bound = handlers._bound_snapshot(workflow, prepare)
    original_snapshot = json.loads(json.dumps(bound.payload))

    bundle = _build_c1_derived_candidate_evidence_bundle(
        tmp_path / "evidence-staged",
        workspace=workspace,
        workflow_id=workflow_id,
        handlers=handlers,
    )
    arts = {a.id: a for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)}
    req_art = next(a for a in arts.values() if a.artifact_type == "candidate-runtime-request")
    req_bytes = (workspace.root / req_art.relative_path).read_bytes()
    assert (bundle / "runtime/request.json").read_bytes() == req_bytes
    req_doc = json.loads(req_bytes.decode("utf-8"))
    assert req_doc["glb"] == "res://Main.tscn"
    assert Path(req_doc["capture_dir"]).is_dir()
    assert Path(req_doc["observation_path"]).is_file()
    transient_free = {k: v for k, v in req_doc.items() if k not in RUNTIME_REQUEST_TRANSIENT_KEYS}
    assert req_doc["bound_payload_canonical"] == candidate_runtime_bound_payload_canonical(
        transient_free
    )
    assert req_doc["request_digest"] == candidate_runtime_request_digest(transient_free)
    scope = json.loads((bundle / "approval/scope.json").read_text(encoding="utf-8"))
    assert scope["review_task"]["parameters"] == review.parameters
    _assert_c1_derived_bundle_copy_fidelity(
        workspace=workspace,
        workflow_id=workflow_id,
        bundle=bundle,
        original_snapshot=original_snapshot,
    )
    _assert_c1_derived_cold_success(bundle)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction rejection requires NTFS junctions")
def test_rejects_windows_junction_in_bundle(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path)
    target = bundle / "glb"
    link = bundle / "junction_probe"
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' -Force | Out-Null",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"could not create junction for test: {proc.stderr.strip()}")
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _failure_reason(out).lower()
    assert "junction" in reason or "link" in reason or "inventory" in reason
