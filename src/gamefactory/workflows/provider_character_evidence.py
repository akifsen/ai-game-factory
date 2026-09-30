"""Portable evidence export for the narrowly supported paid V0.7 character path.

The byte exporter is deliberately separate from local-assembly evidence. The
workflow adapter is responsible for selecting the authoritative live snapshot;
this module rejects malformed or incomplete selections and never changes task,
approval, provider, or accounting state.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, cast

from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.domain.errors import ArtifactError, ValidationError

_SCHEMA = "asset-evidence-0.7.0"
_ROLES = frozenset(
    {
        "specification",
        "concept",
        "concept_provenance",
        "concept_approval",
        "paid_approval",
        "paid_request_snapshot",
        "production_readiness_report",
        "provider_operation",
        "cost_record",
        "provider_generated_glb",
        "processed_glb",
        "processing_report",
        "processing_script",
        "validation",
        "runtime_request",
        "runtime_observation",
        "runtime_harness",
        "runtime_capture",
        "final_approval",
        "production_receipt",
        "review_html",
        "bound_profile",
    }
)
_GENERATED = frozenset(
    {"concept_approval", "paid_approval", "final_approval", "production_receipt", "review_html"}
)
_VIEWS = frozenset(
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
_HEX = re.compile(r"^[0-9a-f]{64}$")
_ROLE_LIMITS = {
    "provider_generated_glb": 50 * 1024 * 1024,
    "processed_glb": 50 * 1024 * 1024,
    "concept": 25_000_000,
    "runtime_capture": 25_000_000,
    "processing_script": 4_000_000,
    "runtime_harness": 4_000_000,
}
_DEFAULT_FILE_LIMIT = 1_000_000


class ProviderCharacterEvidenceFile:
    """One immutable input selected from the current handler snapshot."""

    __slots__ = (
        "role",
        "path",
        "data",
        "view",
        "artifact_id",
        "source_relative_path",
        "expected_sha256",
        "expected_size",
    )

    def __init__(
        self,
        role: str,
        *,
        path: Path | None = None,
        data: bytes | None = None,
        view: str | None = None,
        artifact_id: str | None = None,
        source_relative_path: str | None = None,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> None:
        if not isinstance(role, str) or (path is None) == (data is None):
            raise ValueError("Evidence file needs a role and exactly one data source")
        self.role, self.path, self.data = role, path, data
        self.view, self.artifact_id, self.source_relative_path = (
            view,
            artifact_id,
            source_relative_path,
        )
        self.expected_sha256, self.expected_size = expected_sha256, expected_size


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: Any, *, pretty: bool = False) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
        + ("\n" if pretty else "")
    ).encode("utf-8")


def _relpath(value: str) -> str:
    pure = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} or ":" in part for part in value.split("/"))
    ):
        raise ArtifactError("Unsafe provider-character bundle path")
    return value


def _read(item: ProviderCharacterEvidenceFile) -> bytes:
    limit = _ROLE_LIMITS.get(item.role, _DEFAULT_FILE_LIMIT)
    if item.data is not None:
        if not isinstance(item.data, bytes):
            raise ArtifactError("Provider-character in-memory evidence must be immutable bytes")
        raw = item.data
        if len(raw) > limit:
            raise ArtifactError("Provider-character input exceeds its role-specific limit")
    else:
        assert item.path is not None
        path = item.path
        try:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or path.is_symlink()
                or getattr(info, "st_nlink", 1) != 1
                or info.st_size > limit
            ):
                raise ArtifactError("Provider-character input is not a bounded regular file")
            for ancestor in (path, *path.parents):
                junction = getattr(ancestor, "is_junction", None)
                if ancestor.is_symlink() or (junction and junction()):
                    raise ArtifactError("Provider-character input path contains a link")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            fd = os.open(path, flags)
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                ) != (info.st_dev, info.st_ino, info.st_size):
                    raise ArtifactError("Provider-character input changed while it was opened")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(limit + 1)
                after_open = os.fstat(fd)
                after_path = path.lstat()
                if (after_open.st_dev, after_open.st_ino, after_open.st_size) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                ) or (after_path.st_dev, after_path.st_ino, after_path.st_size) != (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                ):
                    raise ArtifactError("Provider-character input changed while it was read")
            finally:
                os.close(fd)
        except OSError as exc:
            raise ArtifactError("Cannot safely read a provider-character input file") from exc
    if len(raw) > limit:
        raise ArtifactError("Provider-character input exceeds its role-specific limit")
    if item.expected_size is not None and len(raw) != item.expected_size:
        raise ArtifactError("Provider-character input size differs from its live row")
    if item.expected_sha256 is not None and _sha(raw) != item.expected_sha256:
        raise ArtifactError("Provider-character input hash differs from its live row")
    return raw


def _snapshot_fingerprint(snapshot: Mapping[str, Any]) -> str:
    """Fingerprint the exact current live selection and its bounded bytes."""
    required = {
        "files",
        "binding",
        "concept_review_receipt",
        "paid_review_receipt",
        "final_review_receipt",
        "attempt_history",
    }
    if set(snapshot) != required or not isinstance(snapshot["files"], Sequence):
        raise ValidationError("Provider-character current snapshot is malformed")
    rows = []
    for item in snapshot["files"]:
        if not isinstance(item, ProviderCharacterEvidenceFile):
            raise ValidationError(
                "Provider-character current snapshot contains an invalid file row"
            )
        raw = _read(item)
        rows.append(
            {
                "role": item.role,
                "view": item.view,
                "artifact_id": item.artifact_id,
                "source_relative_path": item.source_relative_path,
                "sha256": _sha(raw),
                "size": len(raw),
            }
        )
    rows.sort(key=lambda row: (row["role"], row["view"] or "", row["artifact_id"] or ""))
    frozen = {key: snapshot[key] for key in required - {"files"}}
    return _sha(_canonical({**frozen, "files": rows}))


def _cold_gate(bundle: Path) -> dict[str, Any]:
    from importlib.resources import files as resource_files

    packaged = Path(
        str(resource_files("gamefactory").joinpath("resources/scripts/verify_asset_bundle.py"))
    )
    checkout = Path(__file__).resolve().parents[3] / "scripts" / "verify_asset_bundle.py"
    verifiers = [packaged]
    if checkout.is_file():
        verifiers.insert(0, checkout)
    hashes = [_sha(path.read_bytes()) for path in verifiers]
    if len(hashes) == 2 and hashes[0] != hashes[1]:
        raise ArtifactError("Source and packaged cold verifier copies differ")
    reports = []
    for verifier in verifiers:
        try:
            result = subprocess.run(
                [sys.executable, "-I", str(verifier), str(bundle)],
                cwd=Path(tempfile.gettempdir()),
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArtifactError(f"Cold verifier could not complete: {type(exc).__name__}") from exc
        if result.returncode != 0:
            text = " ".join((result.stdout or result.stderr).split())[:1200]
            raise ArtifactError(f"Cold verifier rejected provider-character bundle: {text}")
        try:
            report_value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ArtifactError("Cold verifier returned invalid JSON") from exc
        if not isinstance(report_value, dict):
            raise ArtifactError("Cold verifier returned a malformed report")
        report = cast(dict[str, Any], report_value)
        if report.get("status") != "PASS" or report.get("product_ready") is not True:
            raise ArtifactError("Cold verifier did not confirm product-ready consistency")
        reports.append(report)
    if len(reports) == 2 and reports[0] != reports[1]:
        raise ArtifactError("Source and packaged verifier reports differ")
    return reports[0]


def export_provider_character_evidence(
    destination: Path | str,
    *,
    files: Sequence[ProviderCharacterEvidenceFile],
    binding: Mapping[str, Any],
    concept_review_receipt: dict[str, Any],
    paid_review_receipt: dict[str, Any],
    final_review_receipt: dict[str, Any],
    attempt_history: Mapping[str, Sequence[Mapping[str, Any]]],
    _live_snapshot_recheck: Callable[[], Mapping[str, Any]] | None = None,
    _expected_snapshot_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Stage, cold-verify, and exclusively publish an immutable paid-character bundle."""
    required_binding = {
        "workflow_id",
        "revision",
        "source_version",
        "asset_id",
        "spec_sha256",
        "profile_id",
        "profile_version",
        "profile_document_sha256",
        "review_views",
        "concept_sha256",
        "paid_request_snapshot_sha256",
        "production_readiness_report_sha256",
        "provider_operation_sha256",
        "cost_record_sha256",
        "cost_ledger_row_count",
        "cost_ledger_slice_sha256",
        "raw_glb_sha256",
        "processed_glb_sha256",
        "processing_execution_id",
        "processing_attempt_number",
        "processing_report_sha256",
        "processing_script_sha256",
        "validation_execution_id",
        "validation_attempt_number",
        "validation_sha256",
        "runtime_execution_id",
        "runtime_attempt_number",
        "runtime_request_digest",
        "runtime_observation_sha256",
        "runtime_harness_sha256",
        "capture_sha256",
    }
    if set(binding) != required_binding:
        raise ArtifactError("Provider-character binding fields are incomplete or unsupported")
    if (
        type(binding["revision"]) is not int
        or binding["revision"] < 1
        or type(binding["source_version"]) is not int
        or binding["source_version"] != binding["revision"]
        or not isinstance(binding["workflow_id"], str)
        or not binding["workflow_id"]
        or type(binding["profile_version"]) is not int
    ):
        raise ArtifactError("Provider-character identity binding is invalid")
    for key in ("processing_attempt_number", "validation_attempt_number", "runtime_attempt_number"):
        if type(binding[key]) is not int or binding[key] < 1:
            raise ArtifactError(f"Provider-character attempt binding is invalid: {key}")
    for key in (
        "processing_execution_id",
        "validation_execution_id",
        "runtime_execution_id",
        "asset_id",
        "profile_id",
    ):
        if not isinstance(binding[key], str) or not binding[key]:
            raise ArtifactError(f"Provider-character identity binding is invalid: {key}")
    for key in (
        "spec_sha256",
        "profile_document_sha256",
        "concept_sha256",
        "paid_request_snapshot_sha256",
        "production_readiness_report_sha256",
        "provider_operation_sha256",
        "cost_record_sha256",
        "cost_ledger_slice_sha256",
        "raw_glb_sha256",
        "processed_glb_sha256",
        "processing_report_sha256",
        "processing_script_sha256",
        "validation_sha256",
        "runtime_request_digest",
        "runtime_observation_sha256",
        "runtime_harness_sha256",
    ):
        if not isinstance(binding[key], str) or not _HEX.fullmatch(binding[key]):
            raise ArtifactError(f"Provider-character binding hash is malformed: {key}")
    if type(binding["cost_ledger_row_count"]) is not int or binding["cost_ledger_row_count"] < 1:
        raise ArtifactError("Provider-character cost ledger row count is invalid")
    review_views = binding["review_views"]
    if (
        not isinstance(review_views, (list, tuple))
        or len(review_views) not in (5, 9)
        or len(set(review_views)) != len(review_views)
        or any(x not in _VIEWS for x in review_views)
    ):
        raise ArtifactError("Provider-character review views are unsupported")
    capture_pins = binding["capture_sha256"]
    if (
        not isinstance(capture_pins, Mapping)
        or set(capture_pins) != set(review_views)
        or any(
            not isinstance(value, str) or not _HEX.fullmatch(value)
            for value in capture_pins.values()
        )
    ):
        raise ArtifactError("Provider-character capture digest map is malformed")
    receipts = (
        ("concept_review", concept_review_receipt),
        ("paid_generation", paid_review_receipt),
        ("final_visual_review", final_review_receipt),
    )
    for kind, receipt in receipts:
        if (
            not isinstance(receipt, dict)
            or receipt.get("approval_type") != kind
            or receipt.get("status") != "APPROVED"
        ):
            raise ArtifactError(f"Current {kind} approval is required")
        if (
            receipt.get("workflow_id") != binding["workflow_id"]
            or receipt.get("revision") != binding["revision"]
        ):
            raise ArtifactError(f"{kind} receipt is stale")
        digest = compute_operation_hash(receipt.get("task_id", ""), kind, receipt.get("inputs", {}))
        if receipt.get("operation_hash") != digest or receipt.get("fingerprint") != digest:
            raise ArtifactError(f"{kind} operation hash is invalid")
    rows: list[dict[str, Any]] = []
    payloads: list[tuple[str, bytes]] = []
    seen: set[tuple[str, str | None]] = set()
    total = 0
    for item in files:
        if (
            not isinstance(item, ProviderCharacterEvidenceFile)
            or item.role not in _ROLES - _GENERATED
        ):
            raise ArtifactError(
                "Provider-character snapshot contains an unexpected or generated role"
            )
        identity = (item.role, item.view)
        if identity in seen:
            raise ArtifactError("Provider-character evidence role is duplicated")
        seen.add(identity)
        raw = _read(item)
        total += len(raw)
        if len(payloads) >= 91 or total > 512_000_000:
            raise ArtifactError("Provider-character evidence bundle exceeds its bounds")
        suffix = (
            "png"
            if item.role in {"concept", "runtime_capture"}
            else {
                "provider_generated_glb": "glb",
                "processed_glb": "glb",
                "processing_script": "py",
                "runtime_harness": "gd",
            }.get(item.role, "json")
        )
        relative = _relpath(f"files/{item.role}/{item.view or item.role}.{suffix}")
        row = {"role": item.role, "path": relative, "sha256": _sha(raw), "size": len(raw)}
        if item.view is not None:
            row["view"] = item.view
        if item.artifact_id is not None:
            row["artifact_id"] = item.artifact_id
        if item.source_relative_path is not None:
            row["source_relative_path"] = _relpath(item.source_relative_path)
        rows.append(row)
        payloads.append((relative, raw))
    expected = _ROLES - _GENERATED - {"runtime_capture"}
    role_names = {row["role"] for row in rows}
    if expected - role_names:
        raise ArtifactError(
            f"Provider-character snapshot is missing roles: {', '.join(sorted(expected - role_names))}"
        )
    capture_views = {row.get("view") for row in rows if row["role"] == "runtime_capture"}
    if capture_views != set(review_views):
        raise ArtifactError("Provider-character captures differ from the trusted profile views")
    if (
        rows[[row["role"] for row in rows].index("provider_generated_glb")]["sha256"]
        != binding["raw_glb_sha256"]
    ):
        raise ArtifactError("Provider raw GLB does not match the pinned successful intent")
    for role, expected_hash in (
        ("processed_glb", binding["processed_glb_sha256"]),
        ("processing_report", binding["processing_report_sha256"]),
        ("processing_script", binding["processing_script_sha256"]),
        ("validation", binding["validation_sha256"]),
        ("runtime_observation", binding["runtime_observation_sha256"]),
        ("runtime_harness", binding["runtime_harness_sha256"]),
    ):
        row = next(value for value in rows if value["role"] == role)
        if row["sha256"] != expected_hash:
            raise ArtifactError(f"Provider-character {role} differs from the live pinned hash")
    runtime_request_row = next(row for row in rows if row["role"] == "runtime_request")
    runtime_request = json.loads(dict(payloads)[runtime_request_row["path"]].decode("utf-8"))
    unsigned_request = dict(runtime_request)
    request_digest = unsigned_request.pop("request_digest", None)
    if (
        request_digest != binding["runtime_request_digest"]
        or _sha(_canonical(unsigned_request)) != request_digest
    ):
        raise ArtifactError("Runtime request differs from the current Godot execution digest")
    capture_hashes = {
        row["view"]: row["sha256"] for row in rows if row["role"] == "runtime_capture"
    }
    if capture_hashes != dict(capture_pins):
        raise ArtifactError("Runtime captures differ from the final-approval fingerprint")
    concept_hash = next(row["sha256"] for row in rows if row["role"] == "concept")
    if concept_hash != binding["concept_sha256"]:
        raise ArtifactError("Concept bytes differ from the active approved concept")
    approval_rows: dict[str, dict[str, Any]] = {}
    for kind, receipt in receipts:
        approval_rows[kind] = receipt
    if (
        not isinstance(attempt_history, Mapping)
        or set(attempt_history) != {"process", "validate", "godot"}
        or any(
            not isinstance(entries, Sequence)
            or isinstance(entries, (str, bytes))
            or len(entries) > 256
            or any(not isinstance(item, Mapping) for item in entries)
            for entries in attempt_history.values()
        )
    ):
        raise ArtifactError("Provider-character attempt history is malformed")
    history_bindings = {
        "process": (binding["processing_execution_id"], binding["processing_attempt_number"]),
        "validate": (binding["validation_execution_id"], binding["validation_attempt_number"]),
        "godot": (binding["runtime_execution_id"], binding["runtime_attempt_number"]),
    }
    for stage_name, (execution_id, attempt_number) in history_bindings.items():
        entries = list(attempt_history[stage_name])
        if not entries:
            raise ArtifactError(f"Provider-character {stage_name} attempt history is empty")
        numbers = [item.get("attempt_number") for item in entries]
        if any(type(value) is not int or value < 1 for value in numbers):
            raise ArtifactError(f"Provider-character {stage_name} attempt history is not monotonic")
        typed_numbers = [cast(int, value) for value in numbers]
        if typed_numbers != sorted(set(typed_numbers)):
            raise ArtifactError(f"Provider-character {stage_name} attempt history is not monotonic")
        latest = entries[-1]
        if (
            latest.get("id") != execution_id
            or latest.get("attempt_number") != attempt_number
            or latest.get("status") != "COMPLETED"
        ):
            raise ArtifactError(
                f"Provider-character {stage_name} latest attempt is not the pinned completed attempt"
            )
    history_json = json.loads(_canonical(attempt_history))
    manifest = {
        **dict(binding),
        "schema_version": _SCHEMA,
        "generation_mode": "provider_generated_character",
        "paid": True,
        "product_ready": True,
        "concept_sha256": concept_hash,
        "concept_provenance_sha256": next(
            row["sha256"] for row in rows if row["role"] == "concept_provenance"
        ),
        "concept_approval_sha256": _sha(_canonical(concept_review_receipt, pretty=True)),
        "paid_approval_sha256": _sha(_canonical(paid_review_receipt, pretty=True)),
        "provider_operation_sha256": next(
            row["sha256"] for row in rows if row["role"] == "provider_operation"
        ),
        "cost_record_sha256": next(row["sha256"] for row in rows if row["role"] == "cost_record"),
        "current_attempt_history": history_json,
        "files": rows,
        "final_review": {
            "decision": "APPROVED",
            "fingerprint": final_review_receipt["fingerprint"],
        },
        "human_reviews": {
            "concept_review": concept_review_receipt["operation_hash"],
            "paid_generation": paid_review_receipt["operation_hash"],
            "final_visual_review": final_review_receipt["operation_hash"],
        },
    }
    target = Path(destination).absolute()
    if target.exists() or target.is_symlink():
        raise ArtifactError("Provider-character destination already exists; refusing overwrite")
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ArtifactError("Provider-character destination parent must be a regular directory")
    stage = parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    stage_info = stage.lstat()
    stage_dirs: dict[Path, tuple[int, int]] = {stage: (stage_info.st_dev, stage_info.st_ino)}
    owned: list[tuple[Path, int, int, int, str | None]] = []
    published: list[tuple[Path, int, int, int, str | None]] = []
    target_identity: tuple[int, int] | None = None
    target_dirs: dict[Path, tuple[int, int]] = {}

    def mkdir_owned(path: Path, registry: dict[Path, tuple[int, int]]) -> None:
        try:
            path.mkdir()
        except FileExistsError as exc:
            raise ArtifactError(
                f"Unexpected evidence directory already exists: {path.name}"
            ) from exc
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            raise ArtifactError(f"Evidence path is not a regular directory: {path.name}")
        registry[path] = (info.st_dev, info.st_ino)

    def ensure_parent(root: Path, file_path: Path, registry: dict[Path, tuple[int, int]]) -> None:
        parent_path = file_path.parent
        relative_parts = parent_path.relative_to(root).parts
        current = root
        for part in relative_parts:
            current = current / part
            if current not in registry:
                mkdir_owned(current, registry)

    def still_owned(path: Path, device: int, inode: int, size: int, digest: str | None) -> bool:
        try:
            before = path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino) != (device, inode)
                or (digest is not None and before.st_size != size)
            ):
                return False
            if digest is None:
                # This inode was created with O_EXCL inside our private stage;
                # identity is the ownership proof for a failed partial write.
                return True
            fd = os.open(
                path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino, opened.st_size) != (device, inode, size):
                    return False
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(size + 1)
                after = os.fstat(fd)
                current = path.lstat()
                return (
                    len(raw) == size
                    and _sha(raw) == digest
                    and (after.st_dev, after.st_ino, after.st_size) == (device, inode, size)
                    and (current.st_dev, current.st_ino, current.st_size) == (device, inode, size)
                )
            finally:
                os.close(fd)
        except OSError:
            return False

    def cleanup_files(rows: list[tuple[Path, int, int, int, str | None]]) -> None:
        for path, device, inode, size, digest in reversed(rows):
            try:
                if still_owned(path, device, inode, size, digest):
                    path.unlink()
            except OSError:
                pass

    def cleanup_dirs(registry: dict[Path, tuple[int, int]]) -> None:
        for path, identity in sorted(
            registry.items(), key=lambda pair: len(pair[0].parts), reverse=True
        ):
            try:
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == identity:
                    path.rmdir()
            except OSError:
                pass

    def assert_live_snapshot_current() -> None:
        if _live_snapshot_recheck is None:
            return
        current = _live_snapshot_recheck()
        if (
            _expected_snapshot_fingerprint is None
            or _snapshot_fingerprint(current) != _expected_snapshot_fingerprint
        ):
            raise ArtifactError(
                "Provider-character live snapshot changed during evidence publication"
            )

    def write_staged_file(path: Path, raw: bytes) -> None:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        opened = os.fstat(fd)
        owner_index = len(owned)
        owned.append((path, opened.st_dev, opened.st_ino, 0, None))
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            completed = os.fstat(fd)
            if completed.st_size != len(raw):
                raise OSError("staged evidence file has an incomplete write")
            owned[owner_index] = (
                path,
                completed.st_dev,
                completed.st_ino,
                completed.st_size,
                _sha(raw),
            )
        finally:
            os.close(fd)

    try:
        for relative, raw in payloads:
            path = stage.joinpath(*PurePosixPath(relative).parts)
            ensure_parent(stage, path, stage_dirs)
            write_staged_file(path, raw)

        def write_generated(relative: str, raw: bytes, role: str) -> None:
            path = stage.joinpath(*PurePosixPath(relative).parts)
            ensure_parent(stage, path, stage_dirs)
            write_staged_file(path, raw)
            rows.append({"role": role, "path": relative, "sha256": _sha(raw), "size": len(raw)})

        for role, receipt in (
            ("concept_approval", concept_review_receipt),
            ("paid_approval", paid_review_receipt),
            ("final_approval", final_review_receipt),
        ):
            write_generated(f"files/{role}/{role}.json", _canonical(receipt, pretty=True), role)
        manifest["files"] = rows
        review_items = "".join(
            f'<li><a href="{html.escape(row["path"], quote=True)}">{html.escape(row["role"] + (" (" + row["view"] + ")" if row.get("view") else ""))}</a> SHA-256 {row["sha256"]}</li>'
            for row in rows
        )
        title = html.escape(str(binding["asset_id"]), quote=True)
        review_html = (
            f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title} evidence</title></head>'
            f"<body><main><h1>{title} character evidence</h1><p>Paid provider snapshot; external trust is required to authenticate execution and approvals.</p><ul>{review_items}</ul></main></body></html>\n"
        ).encode()
        write_generated("index.html", review_html, "review_html")
        production = {
            "schema_version": "production-receipt-0.7.0",
            "status": "PASS",
            "workflow_id": binding["workflow_id"],
            "revision": binding["revision"],
            "source_version": binding["source_version"],
            "asset_id": binding["asset_id"],
            "paid": True,
            "generation_mode": "provider_generated_character",
            "binding": {
                key: value
                for key, value in manifest.items()
                if key not in {"product_ready", "files", "final_review", "human_reviews"}
            },
            "human_reviews": {
                "concept_review": {
                    "operation_hash": concept_review_receipt["operation_hash"],
                    "status": "APPROVED",
                },
                "paid_generation": {
                    "operation_hash": paid_review_receipt["operation_hash"],
                    "status": "APPROVED",
                },
                "final_visual_review": {
                    "operation_hash": final_review_receipt["operation_hash"],
                    "status": "APPROVED",
                },
            },
            "role_sha256": {
                f"{row['role']}:{row.get('view', '')}": row["sha256"]
                for row in rows
                if row["role"] not in {"review_html", "production_receipt"}
            },
        }
        receipt_bytes = _canonical(production, pretty=True)
        write_generated(
            "files/production_receipt/production_receipt.json", receipt_bytes, "production_receipt"
        )
        manifest["files"] = rows
        manifest_bytes = _canonical(manifest, pretty=True)
        manifest_path = stage / "manifest.json"
        write_staged_file(manifest_path, manifest_bytes)
        result = _cold_gate(stage)
        assert_live_snapshot_current()
        target.mkdir(mode=0o700)
        root_info = target.lstat()
        target_identity = (root_info.st_dev, root_info.st_ino)
        for source, _device, _inode, _size, _digest in owned:
            if source.name == "manifest.json":
                continue
            if _digest is None:
                raise ArtifactError("Staged evidence write is not complete")
            relative_path = source.relative_to(stage)
            destination_path = target / relative_path
            ensure_parent(target, destination_path, target_dirs)
            os.link(source, destination_path)
            # Record the link as owned immediately. Any later failure can then
            # remove it only if it still has the staged inode and pinned bytes.
            published.append((destination_path, _device, _inode, _size, _digest))
        source_manifest = stage / "manifest.json"
        destination_manifest = target / "manifest.json"
        os.link(source_manifest, destination_manifest)
        manifest_owner = next(row for row in owned if row[0] == source_manifest)
        published.append(
            (
                destination_manifest,
                manifest_owner[1],
                manifest_owner[2],
                manifest_owner[3],
                manifest_owner[4],
            )
        )
        cleanup_files(owned)
        cleanup_dirs(stage_dirs)
        result = _cold_gate(target)
        assert_live_snapshot_current()
        return {**result, "bundle_path": str(target)}
    except Exception:
        cleanup_files(published)
        cleanup_files(owned)
        if target_identity is not None:
            target_dirs[target] = target_identity
        cleanup_dirs(target_dirs)
        cleanup_dirs(stage_dirs)
        raise


def export_current_provider_character_evidence(
    handlers: Any,
    workflow: Any,
    task: Any,
    execution: Any,
    destination: Path | str,
) -> dict[str, Any]:
    """Export only an authoritative current handler snapshot; never mutates workflow state."""
    selector = getattr(handlers, "current_provider_character_evidence_inputs", None)
    if not callable(selector):
        raise ValidationError(
            "Provider-character workflow does not expose current evidence selection"
        )
    snapshot = selector(workflow, task, execution)
    if not isinstance(snapshot, Mapping):
        raise ValidationError("Provider-character evidence snapshot is malformed")
    required = {
        "files",
        "binding",
        "concept_review_receipt",
        "paid_review_receipt",
        "final_review_receipt",
        "attempt_history",
    }
    if set(snapshot) != required:
        raise ValidationError("Provider-character evidence snapshot fields are incomplete")
    expected_fingerprint = _snapshot_fingerprint(snapshot)

    def reselect() -> Mapping[str, Any]:
        current = selector(workflow, task, execution)
        if not isinstance(current, Mapping) or set(current) != required:
            raise ValidationError("Provider-character current evidence selection became malformed")
        return current

    return export_provider_character_evidence(
        destination,
        **snapshot,
        _live_snapshot_recheck=reselect,
        _expected_snapshot_fingerprint=expected_fingerprint,
    )
