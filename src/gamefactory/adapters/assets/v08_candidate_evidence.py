"""Staging-only candidate evidence envelope export (V0.8-3C C2)."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_validator import _MAX_FILE_BYTES
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    ExecutionRepository,
    TaskRepository,
)
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import ApprovalStatus
from gamefactory.workflows.v08_candidate_currentness import (
    verify_artifact_bytes_and_hash,
)
from gamefactory.workflows.v08_candidate_gates import (
    MAX_CANDIDATE_JSON_ARTIFACT_BYTES,
    MAX_CANDIDATE_PNG_BYTES,
    build_test_only_operation_inputs,
)
from gamefactory.workflows.v08_candidate_snapshot import (
    CANDIDATE_TEST_ONLY_APPROVAL,
    CandidateBoundSnapshot,
)
from gamefactory.workflows.v08_candidate_workspace import (
    _MANAGED_WORKSPACE_REGISTRY,
    reject_unmanaged_candidate_database,
)

MAX_NESTED_BUNDLE_FILES = 512
MAX_NESTED_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_PUBLICATION_CONTAINER_FILES = 640
MAX_PUBLICATION_CONTAINER_DIRECTORIES = 256
MAX_PUBLICATION_CONTAINER_TOTAL_BYTES = MAX_NESTED_BUNDLE_BYTES + 8 * 1024 * 1024
MARKER_SCHEMA = "candidate-evidence-completion-marker-0.8.0"
PUBLICATION_RESULT_SCHEMA = "candidate-evidence-publication-result-0.8.0"
_MARKER_CONTROL_KEYS = frozenset(
    {
        "schema_version",
        "workflow_id",
        "evidence_task_id",
        "evidence_execution_id",
        "evidence_attempt_number",
        "snapshot_fingerprint",
        "manifest_sha256",
        "result_sha256",
        "cold_bundle_payload_digest",
        "candidate_evidence_complete",
        "production_eligible",
        "promotion_eligible",
    }
)
_RESULT_CONTROL_KEYS = frozenset(
    {
        "schema_version",
        "workflow_id",
        "evidence_task_id",
        "evidence_execution_id",
        "evidence_attempt_number",
        "snapshot_fingerprint",
        "cold_bundle_payload_digest",
        "trusted_cold_result",
    }
)
NESTED_PACKAGED_PATH = "rig/nested"
COLD_BUNDLE_DIRNAME = "bundle"
RESULT_FILENAME = "cold-verify-result.json"
MARKER_FILENAME = "completion-marker.json"

_PACKAGED_PATHS: dict[str, tuple[str, str]] = {
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


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _lexical_ancestors(path: Path) -> list[Path]:
    lexical = Path(path).absolute()
    ancestors: list[Path] = []
    for current in [lexical, *lexical.parents]:
        ancestors.append(current)
        if current.anchor == current:
            break
    return ancestors


def candidate_evidence_lexical_unsafe(path: Path) -> bool:
    """Fail-closed lexical link/junction/reparse detection (candidate-evidence owned)."""
    for ancestor in _lexical_ancestors(path):
        try:
            st = os.lstat(ancestor)
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if stat.S_ISLNK(st.st_mode):
            return True
        reparse_tag = int(getattr(st, "st_reparse_tag", 0) or 0)
        if reparse_tag != 0:
            return True
        if os.name == "nt":
            attrs = getattr(st, "st_file_attributes", None)
            if attrs is None:
                return True
            if int(attrs) & 0x400 and reparse_tag == 0:
                return True
    return False


def _reject_nonfinite_json_constant(token: str) -> Any:
    raise ValidationError(f"invalid JSON constant: {token}")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValidationError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def parse_bounded_publication_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_CANDIDATE_JSON_ARTIFACT_BYTES:
        raise ValidationError("publication control JSON exceeds bounded byte limit")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"publication control JSON is not valid UTF-8: {exc}") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValidationError(f"publication control JSON is malformed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("publication control JSON root must be an object")
    return parsed


def load_bounded_publication_control_json(path: Path, *, label: str) -> dict[str, Any]:
    raw = _assert_regular_file_bounded(path, label=label)
    return parse_bounded_publication_json(raw)


def _reject_unknown_control_keys(
    doc: dict[str, Any], allowed: frozenset[str], *, label: str
) -> None:
    unknown = set(doc) - allowed
    if unknown:
        raise ValidationError(f"{label} has unknown control fields: {sorted(unknown)}")


def _require_sha256_hex(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValidationError(f"{field} must be a 64-character sha256 hex digest")
    int(value, 16)
    return value


def _require_nonempty_id(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string id")
    return value


def _require_exact_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValidationError(f"{field} must be a JSON integer")
    return value


def _linux_rename_noreplace_directory(src: Path, dst: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ArtifactError("renameat2 is unavailable on this Linux host")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    if (
        renameat2(
            at_fdcwd,
            os.fsencode(str(src)),
            at_fdcwd,
            os.fsencode(str(dst)),
            rename_noreplace,
        )
        != 0
    ):
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise ArtifactError("publication destination already exists") from None
        raise ArtifactError(f"publication rename failed with errno {err}") from None


def _reject_candidate_evidence_lexical_path(path: Path, *, label: str) -> None:
    if candidate_evidence_lexical_unsafe(path):
        raise ValidationError(f"{label} is not a bounded lexical path: {path}")


def _bounded_read_file_digest(path: Path, *, rel: str) -> tuple[int, str, bytes]:
    _reject_candidate_evidence_lexical_path(path, label=rel)
    if not path.is_file():
        raise ValidationError(f"{rel} is not a regular file")
    size = path.stat().st_size
    if size <= 0:
        raise ValidationError(f"{rel} is empty")
    limit = _max_bytes_for_path(rel)
    if size > limit:
        raise ValidationError(f"{rel} exceeds bounded read limit")
    hasher = hashlib.sha256()
    read_total = 0
    chunks: list[bytes] = []
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            read_total += len(chunk)
            if read_total > limit:
                raise ValidationError(f"{rel} exceeded bounded stream read limit")
            hasher.update(chunk)
            chunks.append(chunk)
    if read_total != size:
        raise ValidationError(f"{rel} size changed during bounded read")
    return size, hasher.hexdigest(), b"".join(chunks)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _snapshot_fingerprint(payload: dict[str, Any]) -> str:
    if "snapshot_fingerprint" in payload:
        raise ValueError("snapshot payload must not embed snapshot_fingerprint")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return _digest(raw.encode("utf-8"))


def _packaged_relpath(artifact_type: str, relative_path: str) -> tuple[str, str]:
    if artifact_type == "candidate-runtime-capture":
        view = Path(relative_path.replace("\\", "/")).stem
        return "runtime_capture", f"runtime/captures/{view}.png"
    if artifact_type == "candidate-nested-rig-manifest":
        return "rig_attempt_wrapper", f"{NESTED_PACKAGED_PATH}/manifest.json"
    mapped = _PACKAGED_PATHS.get(artifact_type)
    if mapped is None:
        raise ValidationError(f"unsupported candidate evidence artifact type: {artifact_type}")
    return mapped


def _scope_role_or_nested_manifest(artifact_type: str) -> str:
    if artifact_type == "candidate-nested-rig-manifest":
        return "rig/manifest.json"
    if artifact_type == "candidate-runtime-capture":
        return "runtime_capture"
    role, _path = _PACKAGED_PATHS[artifact_type]
    return role


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


def _max_bytes_for_path(rel_path: str) -> int:
    lowered = rel_path.casefold()
    if lowered.endswith(".png"):
        return MAX_CANDIDATE_PNG_BYTES
    if lowered.endswith(".glb"):
        return _MAX_FILE_BYTES
    return MAX_CANDIDATE_JSON_ARTIFACT_BYTES


def _assert_regular_file_bounded(path: Path, *, label: str) -> bytes:
    size, digest, raw = _bounded_read_file_digest(path, rel=label)
    if len(raw) != size or _digest(raw) != digest:
        raise ValidationError(f"{label} byte identity drifted during bounded read")
    return raw


def _bounded_lexical_inventory(
    root: Path,
    *,
    max_files: int,
    max_directories: int,
    max_total_bytes: int,
) -> list[tuple[str, int, str]]:
    if not root.is_dir():
        raise ValidationError("bounded inventory root is not a directory")
    _reject_candidate_evidence_lexical_path(root, label="bounded inventory root")
    entries: list[tuple[str, int, str]] = []
    total_bytes = 0
    file_count = 0
    dir_count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        current = Path(dirpath)
        _reject_candidate_evidence_lexical_path(current, label="inventory directory")
        for name in dirnames:
            child = current / name
            _reject_candidate_evidence_lexical_path(
                child, label=f"inventory child directory {name}"
            )
            try:
                child_stat = os.lstat(child)
            except OSError as exc:
                raise ValidationError(f"inventory child directory unreadable: {name}") from exc
            if stat.S_ISLNK(child_stat.st_mode) or not stat.S_ISDIR(child_stat.st_mode):
                raise ValidationError(f"inventory child is not a bounded directory: {name}")
        rel_dir = current.relative_to(root).as_posix()
        if rel_dir != ".":
            dir_count += 1
            if dir_count > max_directories:
                raise ValidationError("bounded inventory directory count exceeds bound")
            entries.append((f"D:{rel_dir}", 0, ""))
        for name in filenames:
            file_path = current / name
            rel = file_path.relative_to(root).as_posix()
            size, digest, _raw = _bounded_read_file_digest(file_path, rel=rel)
            total_bytes += size
            file_count += 1
            if file_count > max_files:
                raise ValidationError("bounded inventory file count exceeds bound")
            if total_bytes > max_total_bytes:
                raise ValidationError("bounded inventory total bytes exceed bound")
            entries.append((f"F:{rel}", size, digest))
    entries.sort(key=lambda item: item[0])
    return entries


def _nested_bundle_inventory(root: Path) -> list[tuple[str, int, str]]:
    return _bounded_lexical_inventory(
        root,
        max_files=MAX_NESTED_BUNDLE_FILES,
        max_directories=MAX_PUBLICATION_CONTAINER_DIRECTORIES,
        max_total_bytes=MAX_NESTED_BUNDLE_BYTES,
    )


def _publication_container_inventory(root: Path) -> list[tuple[str, int, str]]:
    return _bounded_lexical_inventory(
        root,
        max_files=MAX_PUBLICATION_CONTAINER_FILES,
        max_directories=MAX_PUBLICATION_CONTAINER_DIRECTORIES,
        max_total_bytes=MAX_PUBLICATION_CONTAINER_TOTAL_BYTES,
    )


def fingerprint_cold_bundle_payload(cold_bundle_dir: Path) -> str:
    """D0/D1 digest: sorted cold bundle tree (paths, dirs, sizes, sha256)."""
    lexical = Path(cold_bundle_dir).absolute()
    _reject_candidate_evidence_lexical_path(lexical, label="cold bundle root")
    root = lexical.resolve(strict=True)
    entries = _nested_bundle_inventory(root)
    canonical = "\n".join(
        f"{kind}:{size}:{digest}" if digest else kind for kind, size, digest in entries
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fingerprint_publish_container(container_root: Path) -> str:
    """Dready / pre-post rename digest for the full publication container."""
    lexical = Path(container_root).absolute()
    _reject_candidate_evidence_lexical_path(lexical, label="publication container root")
    root = lexical.resolve(strict=True)
    inventory = _publication_container_inventory(root)
    canonical = "\n".join(
        f"{kind}:{size}:{digest}" if digest else kind for kind, size, digest in inventory
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def atomic_publish_staged_container(
    staging_container: Path,
    publish_parent: Path,
    execution_id: str,
) -> Path:
    """Same-volume directory publish without replacing an existing destination (Linux/Windows)."""
    publish_parent_lexical = Path(publish_parent).absolute()
    _reject_candidate_evidence_lexical_path(publish_parent_lexical, label="publication parent")
    publish_parent_lexical.mkdir(parents=True, exist_ok=True)
    final_container_lexical = publish_parent_lexical / execution_id
    _reject_candidate_evidence_lexical_path(
        final_container_lexical, label="publication destination"
    )
    stage_lexical = Path(staging_container).absolute()
    _reject_candidate_evidence_lexical_path(stage_lexical, label="publication staging container")
    if not stage_lexical.is_dir():
        raise ArtifactError("publication staging container is missing")
    if os.path.lexists(str(final_container_lexical)):
        raise ArtifactError("publication destination already exists")
    if os.name == "nt":
        try:
            os.rename(str(stage_lexical), str(final_container_lexical))
        except FileExistsError as exc:
            raise ArtifactError("publication destination already exists") from exc
        except OSError as exc:
            raise ArtifactError("publication rename failed") from exc
    elif sys.platform == "linux":
        _linux_rename_noreplace_directory(stage_lexical, final_container_lexical)
    else:
        raise ArtifactError("atomic publication requires Linux or Windows")
    return final_container_lexical


def assert_trusted_cold_result_semantics(cold_result: dict[str, Any]) -> None:
    """Reject coherent rehashes that keep PASS flags but contradict provenance/outcome."""
    if cold_result.get("outcome") != "CONSISTENT_BUT_UNAUTHENTICATED":
        raise ValidationError("cold result outcome is not unsigned-consistent")
    if cold_result.get("execution_provenance") != "CONSISTENT_BUT_UNAUTHENTICATED":
        raise ValidationError("cold result execution_provenance is not unsigned-consistent")
    if cold_result.get("integrity_outcome") != "VERIFIED":
        raise ValidationError("cold result integrity_outcome is not VERIFIED")
    if cold_result.get("candidate_evidence_complete") is not True:
        raise ValidationError("cold result did not attest candidate_evidence_complete")
    if (
        cold_result.get("production_eligible") is not False
        or cold_result.get("promotion_eligible") is not False
    ):
        raise ValidationError("cold result eligibility flags must remain false")
    if cold_result.get("validation_status") != "PASS":
        raise ValidationError("cold result validation_status is not PASS")
    if cold_result.get("runtime_status") != "PASS":
        raise ValidationError("cold result runtime_status is not PASS")
    if cold_result.get("reviewed_pins_match") is not True:
        raise ValidationError("cold result reviewed_pins_match is not true")


def build_publication_result_document(
    cold_result: dict[str, Any],
    *,
    workflow_id: str,
    task_id: str,
    execution_id: str,
    attempt_number: int,
    snapshot_fingerprint: str,
    cold_bundle_payload_digest: str,
) -> dict[str, Any]:
    assert_trusted_cold_result_semantics(cold_result)
    return {
        "schema_version": PUBLICATION_RESULT_SCHEMA,
        "workflow_id": workflow_id,
        "evidence_task_id": task_id,
        "evidence_execution_id": execution_id,
        "evidence_attempt_number": attempt_number,
        "snapshot_fingerprint": snapshot_fingerprint,
        "cold_bundle_payload_digest": cold_bundle_payload_digest,
        "trusted_cold_result": cold_result,
    }


def assert_publication_result_bindings(
    result_doc: dict[str, Any],
    *,
    workflow_id: str,
    task_id: str,
    execution_id: str,
    attempt_number: int,
    snapshot_fingerprint: str,
    cold_bundle_payload_digest: str,
    manifest_bundle_id: str | None = None,
) -> dict[str, Any]:
    _reject_unknown_control_keys(result_doc, _RESULT_CONTROL_KEYS, label="publication result")
    if result_doc.get("schema_version") != PUBLICATION_RESULT_SCHEMA:
        raise ValidationError("publication result schema_version is invalid")
    _require_nonempty_id(result_doc.get("workflow_id"), field="workflow_id")
    _require_nonempty_id(result_doc.get("evidence_task_id"), field="evidence_task_id")
    _require_nonempty_id(result_doc.get("evidence_execution_id"), field="evidence_execution_id")
    _require_exact_int(result_doc.get("evidence_attempt_number"), field="evidence_attempt_number")
    _require_sha256_hex(result_doc.get("snapshot_fingerprint"), field="snapshot_fingerprint")
    _require_sha256_hex(
        result_doc.get("cold_bundle_payload_digest"), field="cold_bundle_payload_digest"
    )
    for key, expected in (
        ("workflow_id", workflow_id),
        ("evidence_task_id", task_id),
        ("evidence_execution_id", execution_id),
        ("evidence_attempt_number", attempt_number),
        ("snapshot_fingerprint", snapshot_fingerprint),
        ("cold_bundle_payload_digest", cold_bundle_payload_digest),
    ):
        if result_doc.get(key) != expected:
            raise ValidationError(f"publication result binding mismatch for {key}")
    if manifest_bundle_id is not None and manifest_bundle_id != f"candidate-live-{workflow_id}":
        raise ValidationError("cold manifest bundle_id does not match workflow binding")
    trusted = result_doc.get("trusted_cold_result")
    if not isinstance(trusted, dict):
        raise ValidationError("publication result missing trusted_cold_result")
    assert_trusted_cold_result_semantics(trusted)
    return trusted


def assert_completion_marker_controls(
    marker_doc: dict[str, Any],
    *,
    workflow_id: str,
) -> str:
    _reject_unknown_control_keys(marker_doc, _MARKER_CONTROL_KEYS, label="completion marker")
    if marker_doc.get("schema_version") != MARKER_SCHEMA:
        raise ValidationError("completion marker schema_version is invalid")
    _require_nonempty_id(marker_doc.get("workflow_id"), field="workflow_id")
    if marker_doc.get("workflow_id") != workflow_id:
        raise ValidationError("completion marker workflow_id mismatch")
    if marker_doc.get("candidate_evidence_complete") is not True:
        raise ValidationError("completion marker does not attest evidence completion")
    if marker_doc.get("production_eligible") is not False:
        raise ValidationError("completion marker production_eligible must be false")
    if marker_doc.get("promotion_eligible") is not False:
        raise ValidationError("completion marker promotion_eligible must be false")
    _require_nonempty_id(marker_doc.get("evidence_task_id"), field="evidence_task_id")
    _require_nonempty_id(marker_doc.get("evidence_execution_id"), field="evidence_execution_id")
    _require_exact_int(marker_doc.get("evidence_attempt_number"), field="evidence_attempt_number")
    _require_sha256_hex(marker_doc.get("snapshot_fingerprint"), field="snapshot_fingerprint")
    _require_sha256_hex(marker_doc.get("manifest_sha256"), field="manifest_sha256")
    _require_sha256_hex(marker_doc.get("result_sha256"), field="result_sha256")
    return _require_sha256_hex(
        marker_doc.get("cold_bundle_payload_digest"), field="cold_bundle_payload_digest"
    )


def canonical_trusted_cold_result_json(cold_result: dict[str, Any]) -> str:
    """Canonical JSON for comparing stored vs freshly verified cold outcomes."""
    return json.dumps(cold_result, sort_keys=True, separators=(",", ":"), allow_nan=False)


def assert_known_published_evidence_container_layout(
    container_root: Path,
    *,
    execution_id: str,
) -> None:
    """Published container must contain only bundle/ and execution-bound control siblings."""
    if not container_root.is_dir():
        raise ValidationError("published evidence container is missing")
    expected_names = frozenset(
        {
            COLD_BUNDLE_DIRNAME,
            f"c2-evidence-result-{execution_id}.json",
            f"c2-export-marker-{execution_id}.json",
        }
    )
    children = list(container_root.iterdir())
    actual_names = {child.name for child in children}
    if actual_names != expected_names:
        raise ValidationError(
            f"published evidence container has unexpected top-level entries: {sorted(actual_names)}"
        )
    for child in children:
        if child.name == COLD_BUNDLE_DIRNAME:
            if not child.is_dir():
                raise ValidationError("published cold bundle path is not a directory")
        elif not child.is_file():
            raise ValidationError("published evidence control path is not a regular file")


def assert_publication_controls_same_container(
    *,
    manifest_path: Path,
    result_path: Path,
    marker_path: Path,
) -> Path:
    container_root = manifest_path.parent.parent
    if manifest_path.parent.name != COLD_BUNDLE_DIRNAME:
        raise ValidationError("evidence manifest must live under published bundle/")
    if result_path.parent != container_root or marker_path.parent != container_root:
        raise ValidationError(
            "publication controls must live in the same container as the cold bundle"
        )
    if not container_root.is_dir():
        raise ValidationError("published evidence container is missing")
    return container_root


def assert_cold_verification_live_binding(
    *,
    cold_result: dict[str, Any],
    cold_bundle_dir: Path,
    snapshot: CandidateBoundSnapshot,
    workflow_id: str,
    snapshot_payload_canonical_bytes: bytes,
    review_approval_id: str,
) -> None:
    """Unsigned cold PASS must still match live workflow/snapshot bindings."""
    assert_trusted_cold_result_semantics(cold_result)
    manifest_path = cold_bundle_dir / "manifest.json"
    manifest = json.loads(
        _assert_regular_file_bounded(manifest_path, label="cold manifest").decode("utf-8")
    )
    payload = snapshot.payload
    snapshot_fp = snapshot.fingerprint()
    bundled_snapshot_bytes = _assert_regular_file_bounded(
        cold_bundle_dir / "snapshot" / "snapshot.json", label="bundled snapshot"
    )
    bundled_canonical = json.dumps(
        json.loads(bundled_snapshot_bytes.decode("utf-8")),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if bundled_canonical != snapshot_payload_canonical_bytes:
        raise ValidationError("bundled snapshot bytes do not match immutable BEFORE snapshot")
    scope = json.loads(
        _assert_regular_file_bounded(
            cold_bundle_dir / "approval" / "scope.json", label="approval scope"
        ).decode("utf-8")
    )
    if scope.get("approval_id") != review_approval_id:
        raise ValidationError(
            "cold approval scope approval_id does not match live APPROVED receipt"
        )
    if manifest.get("workflow_id") != workflow_id:
        raise ValidationError("cold manifest workflow_id does not match live workflow")
    if manifest.get("snapshot_fingerprint") != snapshot_fp:
        raise ValidationError(
            "cold manifest snapshot_fingerprint does not match immutable snapshot"
        )
    if manifest.get("source_glb_sha256") != payload.get("source_glb_hash"):
        raise ValidationError("cold manifest source hash does not match bound snapshot")
    bundle_id = manifest.get("bundle_id")
    if not isinstance(bundle_id, str) or not bundle_id.startswith("candidate-live-"):
        raise ValidationError("cold manifest bundle_id is not a live candidate bundle")
    bindings = payload.get("pre_review_artifact_bindings")
    bundled = json.loads(bundled_snapshot_bytes.decode("utf-8"))
    bundled_bindings = bundled.get("pre_review_artifact_bindings")
    if not isinstance(bindings, list) or not isinstance(bundled_bindings, list):
        raise ValidationError("snapshot bindings are missing from immutable BEFORE snapshot")
    if len(bindings) != 23 or len(bundled_bindings) != 23:
        raise ValidationError("snapshot must contain exactly 23 pre-review artifact bindings")
    orig_by_id = {str(row["artifact_id"]): row for row in bindings if isinstance(row, dict)}
    copy_by_id = {str(row["artifact_id"]): row for row in bundled_bindings if isinstance(row, dict)}
    for artifact_id, row in orig_by_id.items():
        if copy_by_id.get(artifact_id) != row:
            raise ValidationError(f"bundled snapshot binding drift for artifact {artifact_id}")


def _copy_tree_bounded(src: Path, dest: Path) -> None:
    if not src.is_dir():
        raise ValidationError("nested rig bundle source is missing")
    if path_crosses_link(src):
        raise ValidationError("nested rig bundle source crosses a link")
    if dest.exists():
        raise ValidationError("refusing to replace existing nested rig bundle destination")
    dest.parent.mkdir(parents=True, exist_ok=True)
    before = _nested_bundle_inventory(src)
    dest.mkdir(parents=True, exist_ok=False)
    for kind, size, digest in before:
        if not kind.startswith("F:"):
            continue
        rel = kind[2:]
        source_file = src / rel
        target_file = dest / rel
        target_file.parent.mkdir(parents=True, exist_ok=True)
        if target_file.exists():
            raise ValidationError("nested rig copy would overwrite an existing path")
        shutil.copy2(source_file, target_file)
        if sha256_file(target_file) != digest or target_file.stat().st_size != size:
            raise ValidationError("nested rig bundle byte copy did not preserve content")
    after = _nested_bundle_inventory(dest)
    if before != after:
        raise ValidationError("nested rig bundle inventory changed during copy")


@dataclass(frozen=True)
class CandidateEvidenceStagingLayout:
    """Staging container: cold bundle/ plus siblings outside the cold root."""

    container_root: Path
    cold_bundle_dir: Path

    @property
    def result_path(self) -> Path:
        return self.container_root / RESULT_FILENAME

    @property
    def marker_path(self) -> Path:
        return self.container_root / MARKER_FILENAME


class CandidateEvidenceExporter:
    """Builds staging-only candidate evidence envelopes; never publishes final paths."""

    def __init__(
        self,
        *,
        project_root: Path,
        db: Database,
        artifacts: ArtifactRepository,
        artifact_manager: ArtifactManager,
        approvals: ApprovalRepository,
        executions: ExecutionRepository,
        tasks: TaskRepository,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        managed_workspace = None
        for managed_id, binding in _MANAGED_WORKSPACE_REGISTRY.items():
            if binding.db is db and binding.root == self.project_root:
                from gamefactory.workflows.v08_candidate_workspace import V08CandidateWorkspace

                managed_workspace = V08CandidateWorkspace(
                    project_id=binding.project_id,
                    root=binding.root,
                    db=db,
                    managed_workspace_id=managed_id,
                )
                break
        if managed_workspace is None:
            raise ValidationError(
                "Candidate evidence exporter requires a factory-managed V08CandidateWorkspace"
            )
        reject_unmanaged_candidate_database(
            db, workspace=managed_workspace, project_root=self.project_root
        )
        for repo, label in (
            (artifacts, "artifacts"),
            (approvals, "approvals"),
            (executions, "executions"),
            (tasks, "tasks"),
        ):
            if repo.db is not db:
                raise ValidationError(
                    f"Candidate evidence exporter requires a single managed database ({label})"
                )
        if artifact_manager.base_dir.resolve() != self.project_root:
            raise ValidationError("artifact manager base_dir must match candidate project_root")
        self.db = db
        self.artifacts = artifacts
        self.artifact_manager = artifact_manager
        self.approvals = approvals
        self.executions = executions
        self.tasks = tasks

    def _fresh_staging_container(self) -> CandidateEvidenceStagingLayout:
        staging_parent = self.project_root / ".gf" / "candidate_evidence_staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(16)
        container = staging_parent / f"stage_{token}"
        if container.exists():
            raise ArtifactError("staging collision while allocating candidate evidence container")
        container.mkdir(parents=True, exist_ok=False)
        cold = container / COLD_BUNDLE_DIRNAME
        cold.mkdir(parents=True, exist_ok=False)
        return CandidateEvidenceStagingLayout(container_root=container, cold_bundle_dir=cold)

    def stage_from_request(self, request: Any) -> Path:
        """Callback entry: copy approved bytes into a fresh staging container."""
        workflow_id = str(request.workflow_id)
        request_root = Path(request.project_root).resolve(strict=True)
        if request_root != self.project_root:
            raise ValidationError("candidate evidence export project_root does not match exporter")
        snapshot: CandidateBoundSnapshot = request.snapshot
        nested_rig_bundle_dir = Path(request.nested_rig_bundle_dir)
        receipt_path = Path(request.test_only_receipt_path)
        if not receipt_path.is_file():
            raise ArtifactError("TEST_ONLY receipt path is missing for candidate evidence export")

        layout = self._fresh_staging_container()
        self._build_cold_bundle(
            workflow_id=workflow_id,
            snapshot=snapshot,
            nested_rig_bundle_dir=nested_rig_bundle_dir,
            receipt_path=receipt_path,
            bundle=layout.cold_bundle_dir,
        )
        return layout.container_root

    def _build_cold_bundle(
        self,
        *,
        workflow_id: str,
        snapshot: CandidateBoundSnapshot,
        nested_rig_bundle_dir: Path,
        receipt_path: Path,
        bundle: Path,
    ) -> None:
        payload = json.loads(
            json.dumps(snapshot.payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        )
        bindings = payload.get("pre_review_artifact_bindings")
        if not isinstance(bindings, list) or len(bindings) != 23:
            raise ValidationError("snapshot must contain exactly 23 pre-review artifact bindings")
        identity_sha = payload.get("identity_report_sha256")
        if not isinstance(identity_sha, str) or len(identity_sha) != 64:
            raise ValidationError("snapshot identity_report_sha256 is missing or invalid")

        artifact_rows = self.artifacts.list_by_workflow(workflow_id)
        artifacts = {item.id: item for item in artifact_rows}
        for row in bindings:
            if not isinstance(row, dict):
                raise ValidationError("invalid pre_review_artifact_bindings row")
            art = artifacts.get(str(row.get("artifact_id")))
            if art is None:
                raise ArtifactError(
                    f"snapshot binding references missing artifact {row.get('artifact_id')}"
                )
            verify_artifact_bytes_and_hash(
                art, root_path=self.project_root, artifact_manager=self.artifact_manager
            )

        review_task = next(
            (
                t
                for t in self.tasks.list_by_workflow(workflow_id)
                if t.task_type == "v08_candidate_test_only_review"
            ),
            None,
        )
        if review_task is None:
            raise ArtifactError("TEST_ONLY review task missing for evidence export")
        review_exec = self.executions.get_latest_attempt(review_task.id)
        if review_exec is None or review_exec.status.value != "COMPLETED":
            raise ArtifactError("TEST_ONLY review execution is not completed")

        approval = next(
            (
                a
                for a in self.approvals.list_by_workflow(workflow_id)
                if a.task_id == review_task.id
                and a.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
                and a.status == ApprovalStatus.APPROVED
            ),
            None,
        )
        if approval is None:
            raise ArtifactError("APPROVED TEST_ONLY approval missing for evidence export")

        wrapper_art = None
        for row in bindings:
            if not isinstance(row, dict):
                continue
            art = artifacts.get(str(row.get("artifact_id")))
            if art is not None and art.artifact_type == "candidate-rig-attempt-wrapper":
                wrapper_art = art
                break
        if wrapper_art is None:
            raise ArtifactError("snapshot bindings omit candidate-rig-attempt-wrapper")
        wrapper_src = _assert_regular_file_bounded(
            self.project_root / wrapper_art.relative_path, label="rig wrapper"
        )
        wrapper_doc = json.loads(wrapper_src.decode("utf-8"))
        nested_src = nested_rig_bundle_dir.resolve(strict=True)
        if nested_src != (self.project_root / str(wrapper_doc["nested_bundle_dir"])).resolve():
            raise ValidationError("nested rig bundle dir does not match wrapper binding")

        nested_dest = bundle / NESTED_PACKAGED_PATH
        _copy_tree_bounded(nested_src, nested_dest)
        nested_manifest_raw = _assert_regular_file_bounded(
            nested_dest / "manifest.json", label="nested rig manifest"
        )

        contract_bytes = (
            resources.files("gamefactory.resources.v08_candidate")
            .joinpath("candidate-runtime-contract-0.8.0.json")
            .read_bytes()
        )
        harness_bytes = Path(
            str(
                resources.files("gamefactory.resources.godot").joinpath(
                    "candidate_capsule_harness.gd"
                )
            )
        ).read_bytes()

        files: list[dict[str, Any]] = []

        def _write(rel: str, role: str, raw: bytes, **meta: Any) -> None:
            dest = bundle / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                raise ValidationError(f"refusing to overwrite staged evidence path {rel}")
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
            artifact = artifacts[str(row["artifact_id"])]
            rel_path = artifact.relative_path.replace("\\", "/")
            if artifact.content_hash != row.get("content_sha256"):
                raise ValidationError("snapshot binding hash does not match registered artifact")
            raw = _assert_regular_file_bounded(
                self.project_root / artifact.relative_path, label=rel_path
            )
            if _digest(raw) != artifact.content_hash:
                raise ValidationError(f"registered artifact bytes drifted for {artifact.id}")
            role, bundle_rel = _packaged_relpath(artifact.artifact_type, rel_path)
            if artifact.artifact_type == "candidate-nested-rig-manifest":
                continue
            if artifact.artifact_type == "candidate-rig-attempt-wrapper":
                _write(bundle_rel, role, wrapper_src)
                continue
            angle = (
                Path(rel_path).stem
                if artifact.artifact_type == "candidate-runtime-capture"
                else None
            )
            _write(bundle_rel, role, raw, angle=angle)

        _write("contract/runtime-contract.json", "candidate_runtime_contract", contract_bytes)
        _write("reviewed/candidate_capsule_harness.gd", "reviewed_candidate_harness", harness_bytes)

        snapshot_fp = _snapshot_fingerprint(payload)
        _write("snapshot/snapshot.json", "snapshot", _json_bytes(payload))

        scope_entries: list[dict[str, Any]] = []
        for row in bindings:
            artifact = artifacts[str(row["artifact_id"])]
            rel_path = row["relative_path"].replace("\\", "/")
            role, bundle_rel = _packaged_relpath(artifact.artifact_type, rel_path)
            if artifact.artifact_type == "candidate-rig-attempt-wrapper":
                file_raw = wrapper_src
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
                    "attempt_number": _snapshot_attempt_number(
                        payload, str(row["producing_execution_id"])
                    ),
                    "relative_path": rel_path,
                    "bundle_path": bundle_rel,
                    "size": len(file_raw),
                    "role_or_nested_manifest": _scope_role_or_nested_manifest(
                        artifact.artifact_type
                    ),
                }
            )
        scope_entries.sort(key=lambda item: item["artifact_id"])

        handler_context = {
            "workflow_id": workflow_id,
            "revision": review_task.parameters["revision_number"],
            "specification_hash": review_task.parameters["specification_hash"],
            "profile_document_hash": review_task.parameters["profile_document_hash"],
            "snapshot_fingerprint": snapshot_fp,
            "receipt_scope": "candidate_test_only",
            "production_eligible": False,
            "promotion_eligible": False,
            "candidate_test_only": True,
        }
        from gamefactory.adapters.persistence.repositories import WorkflowRepository

        workflow = WorkflowRepository(self.artifacts.db).get(workflow_id)
        if workflow is None:
            raise ArtifactError("workflow missing during evidence export")
        operation_inputs = build_test_only_operation_inputs(
            workflow, review_task, artifact_rows, approval, handler_context
        )
        op_hash = compute_operation_hash(
            review_task.id, CANDIDATE_TEST_ONLY_APPROVAL, operation_inputs
        )
        scope = {
            "schema_version": "candidate-approval-scope-0.8.0",
            "approval_id": approval.id,
            "approval_status": approval.status.value,
            "review_execution_id": review_exec.id,
            "review_task": {
                "id": review_task.id,
                "task_type": review_task.task_type,
                "cost_class": review_task.cost_class,
                "parameters": review_task.parameters,
            },
            "persisted_approval_artifact_ids": [e["artifact_id"] for e in scope_entries],
            "entries": scope_entries,
            "operation_hash": op_hash,
        }
        _write("approval/scope.json", "approval_scope", _json_bytes(scope))

        receipt_doc = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt_doc.get("snapshot_fingerprint") != snapshot_fp:
            raise ValidationError("receipt snapshot_fingerprint does not match export snapshot")
        _write("receipt/test-only.json", "test_only_receipt", _json_bytes(receipt_doc))

        manifest = {
            "schema_version": "candidate-evidence-0.8.0",
            "bundle_id": f"candidate-live-{workflow_id}",
            "workflow_id": workflow_id,
            "specification_hash": review_task.parameters["specification_hash"],
            "profile_document_hash": review_task.parameters["profile_document_hash"],
            "source_glb_sha256": payload["source_glb_hash"],
            "validation_status": "PASS",
            "runtime_status": "PASS",
            "reviewed_pins_match": True,
            "snapshot_fingerprint": snapshot_fp,
            "fixture_label": "live_workflow_unsigned_v083c2b",
            "nested_bundle_path": NESTED_PACKAGED_PATH,
            "files": sorted(files, key=lambda item: item["path"]),
        }
        manifest_path = bundle / "manifest.json"
        if manifest_path.exists():
            raise ValidationError("manifest path already exists in cold bundle")
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )


def prepare_evidence_publication_container(
    layout: CandidateEvidenceStagingLayout,
    *,
    workflow_id: str,
    task_id: str,
    execution_id: str,
    attempt_number: int,
    snapshot_before: CandidateBoundSnapshot,
    cold_result: dict[str, Any],
    cold_bundle_payload_digest: str,
) -> tuple[CandidateEvidenceStagingLayout, str]:
    """Write publication siblings outside the cold payload and return Dready."""
    if layout.result_path.is_file() or layout.marker_path.is_file():
        raise ValidationError("staging container already contains publication siblings")
    result_name = f"c2-evidence-result-{execution_id}.json"
    marker_name = f"c2-export-marker-{execution_id}.json"
    result_path = layout.container_root / result_name
    marker_path = layout.container_root / marker_name
    if result_path.exists() or marker_path.exists():
        raise ValidationError("publication sibling paths already exist in staging container")
    publication_result = build_publication_result_document(
        cold_result,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
        attempt_number=attempt_number,
        snapshot_fingerprint=snapshot_before.fingerprint(),
        cold_bundle_payload_digest=cold_bundle_payload_digest,
    )
    result_path.write_text(
        json.dumps(publication_result, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    manifest_path = layout.cold_bundle_dir / "manifest.json"
    manifest_hash = sha256_file(manifest_path)
    result_hash = sha256_file(result_path)
    marker_payload = {
        "schema_version": MARKER_SCHEMA,
        "workflow_id": workflow_id,
        "evidence_task_id": task_id,
        "evidence_execution_id": execution_id,
        "evidence_attempt_number": attempt_number,
        "snapshot_fingerprint": snapshot_before.fingerprint(),
        "manifest_sha256": manifest_hash,
        "result_sha256": result_hash,
        "cold_bundle_payload_digest": cold_bundle_payload_digest,
        "candidate_evidence_complete": True,
        "production_eligible": False,
        "promotion_eligible": False,
    }
    marker_path.write_text(
        json.dumps(marker_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    assert_known_published_evidence_container_layout(
        layout.container_root, execution_id=execution_id
    )
    d_ready = fingerprint_publish_container(layout.container_root)
    return layout, d_ready


def trusted_cold_verify_candidate_bundle(cold_bundle_dir: Path) -> dict[str, Any]:
    """Run packaged stdlib verifier with python -I; require structured PASS outcome."""
    verifier = Path(
        str(resources.files("gamefactory.resources.scripts").joinpath("verify_candidate_bundle.py"))
    )
    bundle = cold_bundle_dir.resolve(strict=True)
    proc = subprocess.run(
        [sys.executable, "-I", str(verifier), str(bundle)],
        capture_output=True,
        text=True,
        cwd=str(bundle.parent),
    )
    if proc.returncode != 0:
        first = proc.stdout.splitlines()[0] if proc.stdout else "FAILED"
        detail = proc.stdout.strip() or proc.stderr.strip()
        raise ValidationError(f"candidate evidence cold verification failed: {first}: {detail}")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValidationError("candidate evidence cold verifier produced no structured result")
    if lines[0] != "CONSISTENT_BUT_UNAUTHENTICATED":
        raise ValidationError("candidate evidence cold verifier outcome is not unsigned-consistent")
    result = json.loads(lines[1])
    if result.get("integrity_outcome") != "VERIFIED":
        raise ValidationError("candidate evidence integrity_outcome is not VERIFIED")
    if result.get("validation_status") != "PASS":
        raise ValidationError("candidate evidence validation_status is not PASS")
    if result.get("runtime_status") != "PASS":
        raise ValidationError("candidate evidence runtime_status is not PASS")
    if result.get("reviewed_pins_match") is not True:
        raise ValidationError("candidate evidence reviewed_pins_match is not true")
    if (
        result.get("production_eligible") is not False
        or result.get("promotion_eligible") is not False
    ):
        raise ValidationError("candidate evidence eligibility flags must remain false")
    return dict(result)


def assert_fresh_managed_staging_container(project_root: Path, container_root: Path) -> None:
    """Reject reused publication paths or containers outside managed staging."""
    root = project_root.resolve(strict=True)
    staging_parent = (root / ".gf" / "candidate_evidence_staging").resolve()
    container = container_root.resolve(strict=True)
    if container.parent != staging_parent:
        raise ValidationError("candidate evidence staging container is outside managed staging")
    published_parent = (root / ".gamefactory" / "candidate-evidence").resolve()
    try:
        container.relative_to(published_parent)
        raise ValidationError("candidate evidence staging path must not reuse published namespace")
    except ValueError:
        pass
    if not container.name.startswith("stage_"):
        raise ValidationError("candidate evidence staging container name is not fresh")


def parse_staging_container(container_root: Path) -> CandidateEvidenceStagingLayout:
    lexical = Path(container_root).absolute()
    if candidate_evidence_lexical_unsafe(lexical):
        raise ValidationError("staging container path crosses a link or reparse point")
    root = lexical.resolve(strict=True)
    if candidate_evidence_lexical_unsafe(root):
        raise ValidationError(
            "staging container path crosses a link or reparse point after resolve"
        )
    cold = root / COLD_BUNDLE_DIRNAME
    if not cold.is_dir():
        raise ValidationError("staging container is missing cold bundle directory")
    if not (cold / "manifest.json").is_file():
        raise ValidationError("staging cold bundle is missing manifest.json")
    return CandidateEvidenceStagingLayout(container_root=root, cold_bundle_dir=cold)


def bind_production_candidate_evidence_exporter(handlers: Any) -> None:
    """Attach the production staging exporter to workflow handlers (internal integration hook)."""
    exporter = CandidateEvidenceExporter(
        project_root=handlers.root,
        db=handlers.artifacts.db,
        artifacts=handlers.artifacts,
        artifact_manager=handlers.artifact_manager,
        approvals=handlers.approvals,
        executions=handlers.executions,
        tasks=handlers.tasks,
    )
    handlers.c2_export_callback = exporter.stage_from_request


__all__ = [
    "CandidateEvidenceExporter",
    "CandidateEvidenceStagingLayout",
    "PUBLICATION_RESULT_SCHEMA",
    "assert_completion_marker_controls",
    "assert_cold_verification_live_binding",
    "assert_known_published_evidence_container_layout",
    "assert_publication_controls_same_container",
    "canonical_trusted_cold_result_json",
    "assert_publication_result_bindings",
    "assert_trusted_cold_result_semantics",
    "load_bounded_publication_control_json",
    "parse_bounded_publication_json",
    "atomic_publish_staged_container",
    "bind_production_candidate_evidence_exporter",
    "assert_fresh_managed_staging_container",
    "build_publication_result_document",
    "candidate_evidence_lexical_unsafe",
    "fingerprint_cold_bundle_payload",
    "fingerprint_publish_container",
    "parse_staging_container",
    "prepare_evidence_publication_container",
    "trusted_cold_verify_candidate_bundle",
]
