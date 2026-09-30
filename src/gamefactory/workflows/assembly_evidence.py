"""Portable evidence export for locally authored V0.7 assemblies.

The bundle is a consistency record for one already human-gated local workflow
snapshot.  It does not authenticate human identity; that requires an external
trust anchor.  No provider, paid generation, reservation, or readiness path is
used here.
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.domain.errors import ArtifactError, ValidationError

EVIDENCE_SCHEMA = "asset-evidence-0.7.0"
RECEIPT_SCHEMA = "production-receipt-0.7.0"
_HEX = re.compile(r"^[0-9a-f]{64}$")
_MAX_FILE = 100_000_000
_MAX_TOTAL = 512_000_000
_MAX_FILES = 96
_MAX_JSON = 4_000_000
_REQUIRED_ROLES = frozenset(
    {
        "specification",
        "concept",
        "concept_provenance",
        "concept_approval",
        "raw_glb",
        "source_provenance",
        "source_publication_marker",
        "source_approval",
        "bound_profile",
        "processed_glb",
        "processing_report",
        "validation",
        "runtime_observation",
        "runtime_request",
        "runtime_index",
        "processing_script",
        "runtime_harness",
        "runtime_capture",
        "review_html",
        "final_approval",
        "production_receipt",
    }
)
_GENERATED_ROLES = frozenset(
    {"concept_approval", "source_approval", "final_approval", "production_receipt", "review_html"}
)
_PAID_ROLES = frozenset(
    {
        "paid_approval",
        "paid_request_snapshot",
        "production_readiness_report",
        "provider_operation",
        "cost_record",
        "provider_generated_glb",
    }
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


@dataclass(frozen=True)
class EvidenceFile:
    """One role-bound immutable artifact selected by the live workflow adapter."""

    role: str
    path: Path | None = None
    data: bytes | None = None
    view: str | None = None
    artifact_id: str | None = None
    relative_path: str | None = None
    expected_sha256: str | None = None
    expected_size: int | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.data is None):
            raise ValueError("EvidenceFile requires exactly one of path or data")


def _canonical_json(value: Any, *, pretty: bool = False) -> bytes:
    separators = None if pretty else (",", ":")
    text = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        indent=2 if pretty else None,
        separators=separators,
    )
    return (text + ("\n" if pretty else "")).encode("utf-8")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _remember_directory(path: Path, owned: dict[Path, tuple[int, int]]) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ArtifactError(f"Evidence directory is not a regular directory: {path.name}")
    owned[path] = (info.st_dev, info.st_ino)


def _ensure_owned_parent(root: Path, relative: str, owned: dict[Path, tuple[int, int]]) -> Path:
    parent = root
    for part in PurePosixPath(relative).parts[:-1]:
        parent = parent / part
        if parent in owned:
            continue
        try:
            parent.mkdir()
        except FileExistsError as exc:
            raise ArtifactError(f"Unexpected evidence staging directory: {part}") from exc
        _remember_directory(parent, owned)
    return parent


def _remove_owned_directories(owned: Mapping[Path, tuple[int, int]]) -> None:
    for path, identity in sorted(owned.items(), key=lambda item: len(item[0].parts), reverse=True):
        try:
            info = path.lstat()
            if (
                stat.S_ISDIR(info.st_mode)
                and info.st_dev == identity[0]
                and info.st_ino == identity[1]
            ):
                path.rmdir()
        except OSError:
            pass


def _validate_relpath(value: str) -> str:
    pure = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} or ":" in part for part in value.split("/"))
    ):
        raise ArtifactError(f"Unsafe evidence path: {value!r}")
    return value


def _read_bounded(source: EvidenceFile) -> bytes:
    if source.data is not None:
        raw = bytes(source.data)
    else:
        path = source.path
        if path is None:
            raise ArtifactError("Evidence source path is missing")
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink():
                raise ArtifactError(f"Evidence source is not a regular file: {path.name}")
            if info.st_size > _MAX_FILE:
                raise ArtifactError(f"Evidence file exceeds {_MAX_FILE} bytes: {path.name}")
            for ancestor in path.parents:
                junction = getattr(ancestor, "is_junction", None)
                if ancestor.is_symlink() or (junction and junction()):
                    raise ArtifactError(f"Evidence source path contains a link: {path.name}")
            with path.open("rb") as stream:
                raw = stream.read(_MAX_FILE + 1)
        except OSError as exc:
            raise ArtifactError(f"Cannot read evidence source {path.name}: {exc}") from exc
    if len(raw) > _MAX_FILE:
        raise ArtifactError(f"Evidence file exceeds {_MAX_FILE} bytes")
    if source.expected_size is not None and len(raw) != source.expected_size:
        raise ArtifactError("Evidence source size differs from its pinned artifact row")
    if source.expected_sha256 is not None and _sha(raw) != source.expected_sha256:
        raise ArtifactError("Evidence source hash differs from its pinned artifact row")
    return raw


def _unique_role_files(
    files: Sequence[EvidenceFile],
) -> dict[str, list[tuple[EvidenceFile, bytes]]]:
    if len(files) > _MAX_FILES:
        raise ArtifactError("Evidence bundle exceeds the file-count limit")
    result: dict[str, list[tuple[EvidenceFile, bytes]]] = {}
    total = 0
    identities: set[tuple[str, str | None]] = set()
    for source in files:
        if not isinstance(source, EvidenceFile) or not source.role:
            raise ArtifactError("Evidence role entry is malformed")
        identity = (source.role, source.view)
        if identity in identities:
            raise ArtifactError(f"Duplicate evidence role: {identity}")
        identities.add(identity)
        raw = _read_bounded(source)
        total += len(raw)
        if total > _MAX_TOTAL:
            raise ArtifactError("Evidence bundle exceeds total payload limit")
        result.setdefault(source.role, []).append((source, raw))
    unknown = set(result) - _REQUIRED_ROLES - _PAID_ROLES
    if unknown:
        raise ArtifactError(f"Unknown evidence role(s): {', '.join(sorted(unknown))}")
    if set(result) & _PAID_ROLES:
        raise ArtifactError("Local assembly evidence rejects paid/provider evidence roles")
    unexpected_approvals = set(result) & {
        "concept_approval",
        "source_approval",
        "final_approval",
        "production_receipt",
        "review_html",
    }
    if unexpected_approvals:
        raise ArtifactError(
            "Approval, receipt, and review-page files are generated by the exporter"
        )
    missing = (_REQUIRED_ROLES - _GENERATED_ROLES) - set(result)
    if missing:
        raise ArtifactError(f"Required evidence roles are missing: {', '.join(sorted(missing))}")
    if any(
        len(result.get(role, ())) != 1
        for role in (_REQUIRED_ROLES - _GENERATED_ROLES - {"runtime_capture"})
    ):
        raise ArtifactError("Single-valued evidence role is duplicated or missing")
    captures = result["runtime_capture"]
    if {source.view for source, _raw in captures} != _VIEWS or len(captures) != len(_VIEWS):
        raise ArtifactError("Runtime captures must contain exactly the nine canonical review views")
    return result


def _receipt_ok(receipt: Any, kind: str, binding: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise ArtifactError(f"Human {kind} receipt must be an object")
    allowed_keys = {
        "schema_version",
        "workflow_id",
        "revision",
        "source_version",
        "approval_type",
        "status",
        "task_id",
        "inputs",
        "operation_hash",
        "fingerprint",
        "actor",
        "comment",
    }
    if set(receipt) - allowed_keys:
        raise ArtifactError(f"Human {kind} receipt has unsupported fields")
    if (
        receipt.get("workflow_id") != binding.get("workflow_id")
        or type(receipt.get("revision")) is not int
        or receipt.get("revision") != binding.get("revision")
        or type(receipt.get("source_version")) is not int
        or receipt.get("source_version") != binding.get("source_version")
        or receipt.get("approval_type") != kind
        or receipt.get("status") != "APPROVED"
        or not isinstance(receipt.get("task_id"), str)
        or not receipt["task_id"]
        or not isinstance(receipt.get("inputs"), dict)
    ):
        raise ArtifactError(f"Human {kind} approval is pending, rejected, or stale")
    digest = compute_operation_hash(receipt["task_id"], kind, receipt["inputs"])
    if digest != receipt.get("operation_hash") or digest != receipt.get("fingerprint"):
        raise ArtifactError(f"Human {kind} operation hash does not match its canonical inputs")
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise ArtifactError(f"Human {kind} receipt schema is invalid")
    return receipt


def _inert_review_html(rows: list[dict[str, Any]], binding: Mapping[str, Any]) -> bytes:
    title = html.escape(str(binding.get("asset_id", "Assembly")), quote=True)
    body = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>',
        title,
        " evidence</title></head><body><main><h1>",
        title,
        " assembly evidence</h1><p>Local evidence snapshot; approval authority is recorded in the receipts.</p><ul>",
    ]
    for item in rows:
        role = html.escape(item["role"], quote=True)
        path = html.escape(item["path"], quote=True)
        label = f"{role} ({item['view']})" if item.get("view") else role
        body.extend(
            (
                '<li><a href="',
                path,
                '">',
                html.escape(label),
                "</a> SHA-256 ",
                item["sha256"],
                "</li>",
            )
        )
    body.append("</ul></main></body></html>\n")
    return "".join(body).encode("utf-8")


def _cold_gate(bundle: Path) -> dict[str, Any]:
    """Run both shipped standard-library verifiers in isolated interpreters."""
    from importlib.resources import files as resource_files

    packaged = Path(
        str(resource_files("gamefactory").joinpath("resources/scripts/verify_asset_bundle.py"))
    )
    checkout = Path(__file__).resolve().parents[3] / "scripts" / "verify_asset_bundle.py"
    verifiers = [packaged]
    if checkout.is_file():
        verifiers.insert(0, checkout)
    run_directory = Path(tempfile.gettempdir()).resolve()
    if checkout.is_file():
        project_root = checkout.parent.parent.resolve()
        if run_directory == project_root or run_directory.is_relative_to(project_root):
            raise ArtifactError("Cold verifier working directory must be outside the project")
    hashes = [_sha(path.read_bytes()) for path in verifiers]
    if len(hashes) == 2 and hashes[0] != hashes[1]:
        raise ArtifactError("Source and packaged cold verifier copies differ")
    reports = []
    for verifier in verifiers:
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(verifier), str(bundle)],
                cwd=run_directory,
                capture_output=True,
                check=False,
                text=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired as exc:
            raise ArtifactError(
                f"Cold verifier timed out after 90 seconds ({verifier.name})"
            ) from exc
        except OSError as exc:
            raise ArtifactError(
                f"Cold verifier could not be launched ({verifier.name}): {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stdout.strip()
            stderr = completed.stderr.strip()
            try:
                parsed = json.loads(detail)
                if isinstance(parsed, dict):
                    detail = str(parsed.get("error", parsed.get("status", "")))
            except json.JSONDecodeError:
                pass
            detail = detail or stderr or "no diagnostic output"
            detail = " ".join(detail.split())[:1200]
            raise ArtifactError(
                f"Cold verifier {verifier.name} rejected bundle "
                f"{bundle.name} (exit {completed.returncode}): {detail}"
            )
        try:
            report = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            detail = " ".join(completed.stderr.split())[:800]
            suffix = f": {detail}" if detail else ""
            raise ArtifactError(
                f"Cold verifier {verifier.name} did not return a JSON report{suffix}"
            ) from exc
        if report.get("status") != "PASS" or report.get("product_ready") is not True:
            raise ArtifactError(
                f"Cold verifier {verifier.name} did not pass: "
                f"{json.dumps(report, ensure_ascii=False)[:1200]}"
            )
        reports.append(report)
    if len(reports) == 2 and reports[0] != reports[1]:
        raise ArtifactError("The two cold verifier copies produced different reports")
    return cast(dict[str, Any], reports[0])


def verify_local_assembly_evidence_bundle(bundle: Path | str) -> dict[str, Any]:
    """Read-only, isolated re-verification of an already published V0.7 bundle."""
    path = Path(bundle)
    if path.is_symlink() or not path.is_dir():
        raise ArtifactError("Evidence bundle must be an existing regular directory")
    return _cold_gate(path)


def export_local_assembly_evidence(
    destination: Path | str,
    *,
    files: Sequence[EvidenceFile],
    binding: Mapping[str, Any],
    source_review_receipt: dict[str, Any],
    final_review_receipt: dict[str, Any],
    concept_review_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Build, cold-verify, and publish one local V0.7 assembly evidence bundle.

    This byte-oriented primitive never marks approvals or changes workflow state.
    The production workflow must call :func:`export_current_workflow_evidence`,
    which selects pinned roles and approvals from the authoritative handler before
    reaching this stage. The destination must not exist.
    """
    if (
        binding.get("schema_version") != EVIDENCE_SCHEMA
        or binding.get("generation_mode") != "local_operator_assembly"
        or binding.get("paid") is not False
        or (binding.get("product_ready") is not None and binding.get("product_ready") is not False)
        or not isinstance(binding.get("workflow_id"), str)
        or not binding.get("workflow_id")
        or type(binding.get("revision")) is not int
        or binding["revision"] < 1
        or type(binding.get("source_version")) is not int
        or binding.get("source_version") != binding.get("revision")
    ):
        raise ArtifactError("Evidence binding is not a current local-only V0.7 assembly snapshot")
    for kind, receipt in (
        ("concept_review", concept_review_receipt),
        ("source_review", source_review_receipt),
        ("final_visual_review", final_review_receipt),
    ):
        _receipt_ok(receipt, kind, binding)
    roles = _unique_role_files(files)
    if set(binding.get("review_views", ())) != _VIEWS or len(
        binding.get("review_views", ())
    ) != len(_VIEWS):
        raise ArtifactError(
            "Evidence binding must name the exact nine canonical profile review views"
        )
    target = Path(destination).absolute()
    if target.exists() or target.is_symlink():
        raise ArtifactError("Evidence destination already exists; refusing overwrite")
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ArtifactError("Evidence destination parent must be an existing regular directory")
    stage = parent / f".{target.name}.staging-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    stage_dirs: dict[Path, tuple[int, int]] = {}
    _remember_directory(stage, stage_dirs)
    owned: list[tuple[Path, int, int, int, str]] = []
    published: list[tuple[Path, int, int, int, str]] = []
    target_dirs: dict[Path, tuple[int, int]] = {}
    target_created = False
    try:
        rows: list[dict[str, Any]] = []
        for role in sorted(roles):
            for source, raw in roles[role]:
                suffix = (
                    "png"
                    if role == "runtime_capture"
                    else {
                        "raw_glb": "glb",
                        "processed_glb": "glb",
                        "concept": "png",
                        "processing_script": "py",
                        "runtime_harness": "gd",
                        "review_html": "html",
                    }.get(role, "json")
                )
                stem = source.view or role
                rel = _validate_relpath(f"files/{role}/{stem}.{suffix}")
                out = stage.joinpath(*PurePosixPath(rel).parts)
                _ensure_owned_parent(stage, rel, stage_dirs)
                fd = os.open(out, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    with os.fdopen(fd, "wb", closefd=False) as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    info = os.fstat(fd)
                    owned.append((out, info.st_dev, info.st_ino, info.st_size, _sha(raw)))
                finally:
                    os.close(fd)
                rows.append(
                    {
                        "role": role,
                        "path": rel,
                        "sha256": _sha(raw),
                        "size": len(raw),
                        **({"view": source.view} if source.view else {}),
                        **({"artifact_id": source.artifact_id} if source.artifact_id else {}),
                        **(
                            {"source_relative_path": _validate_relpath(source.relative_path)}
                            if source.relative_path
                            else {}
                        ),
                    }
                )
        for role, receipt in (
            ("concept_approval", concept_review_receipt),
            ("source_approval", source_review_receipt),
            ("final_approval", final_review_receipt),
        ):
            raw = _canonical_json(receipt, pretty=True)
            rel = f"files/{role}/{role}.json"
            out = stage.joinpath(*PurePosixPath(rel).parts)
            _ensure_owned_parent(stage, rel, stage_dirs)
            fd = os.open(out, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                info = os.fstat(fd)
                owned.append((out, info.st_dev, info.st_ino, info.st_size, _sha(raw)))
            finally:
                os.close(fd)
            rows.append({"role": role, "path": rel, "sha256": _sha(raw), "size": len(raw)})
        # Receipts are copied into the package verbatim, and their exact bytes are
        # separately represented by role entries above.
        approvals = {
            "concept_review": concept_review_receipt,
            "source_review": source_review_receipt,
            "final_visual_review": final_review_receipt,
        }
        receipt_payload = {
            "schema_version": RECEIPT_SCHEMA,
            "status": "PASS",
            "workflow_id": binding["workflow_id"],
            "revision": binding["revision"],
            "source_version": binding["source_version"],
            "asset_id": binding["asset_id"],
            "paid": False,
            "generation_mode": "local_operator_assembly",
            "binding": dict(binding),
            "human_reviews": {
                key: {"operation_hash": value["operation_hash"], "status": value["status"]}
                for key, value in approvals.items()
            },
            "role_sha256": {
                f"{row['role']}:{row.get('view', '')}": row["sha256"]
                for row in rows
                if row["role"] != "review_html"
            },
        }
        receipt_bytes = _canonical_json(receipt_payload, pretty=True)
        receipt_file = stage / "files" / "production_receipt.json"
        receipt_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(receipt_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(receipt_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            info = os.fstat(fd)
            owned.append(
                (receipt_file, info.st_dev, info.st_ino, info.st_size, _sha(receipt_bytes))
            )
        finally:
            os.close(fd)
        receipt_row = {
            "role": "production_receipt",
            "path": "files/production_receipt.json",
            "sha256": _sha(receipt_bytes),
            "size": len(receipt_bytes),
        }
        rows.append(receipt_row)
        html_bytes = _inert_review_html(rows, binding)
        html_file = stage / "index.html"
        fd = os.open(html_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(html_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            info = os.fstat(fd)
            owned.append((html_file, info.st_dev, info.st_ino, info.st_size, _sha(html_bytes)))
        finally:
            os.close(fd)
        rows.append(
            {
                "role": "review_html",
                "path": "index.html",
                "sha256": _sha(html_bytes),
                "size": len(html_bytes),
            }
        )
        manifest = {
            **dict(binding),
            "schema_version": EVIDENCE_SCHEMA,
            "product_ready": True,
            "files": rows,
            "review_views": list(binding["review_views"]),
            "final_review": {
                "decision": final_review_receipt["status"],
                "fingerprint": final_review_receipt["fingerprint"],
            },
            "human_reviews": {key: value["operation_hash"] for key, value in approvals.items()},
        }
        manifest_bytes = _canonical_json(manifest, pretty=True)
        manifest_file = stage / "manifest.json"
        fd = os.open(manifest_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(manifest_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            info = os.fstat(fd)
            owned.append(
                (manifest_file, info.st_dev, info.st_ino, info.st_size, _sha(manifest_bytes))
            )
        finally:
            os.close(fd)
        # Verify the invisible stage, then claim the final name exclusively.
        cold = _cold_gate(stage)
        if cold.get("status") != "PASS":
            raise ArtifactError("Cold verification did not pass")
        try:
            target.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise ArtifactError("Evidence destination was claimed concurrently") from exc
        target_created = True
        _remember_directory(target, target_dirs)
        for source_path, _device, _inode, _size, _digest in owned:
            if source_path.name == "manifest.json":
                continue
            relative = source_path.relative_to(stage)
            dest_path = target / relative
            _ensure_owned_parent(target, relative.as_posix(), target_dirs)
            os.link(source_path, dest_path)
            info = dest_path.lstat()
            published.append(
                (dest_path, info.st_dev, info.st_ino, info.st_size, _sha(dest_path.read_bytes()))
            )
        manifest_source = next(path for path, *_rest in owned if path.name == "manifest.json")
        manifest_dest = target / "manifest.json"
        os.link(manifest_source, manifest_dest)
        info = manifest_dest.lstat()
        published.append(
            (
                manifest_dest,
                info.st_dev,
                info.st_ino,
                info.st_size,
                _sha(manifest_dest.read_bytes()),
            )
        )
        cold = _cold_gate(target)
        if cold.get("status") != "PASS":
            raise ArtifactError("Published evidence failed the cold verifier")
        for path, device, inode, size, digest in reversed(owned):
            try:
                info = path.lstat()
                if (
                    info.st_dev == device
                    and info.st_ino == inode
                    and info.st_size == size
                    and _sha(path.read_bytes()) == digest
                ):
                    path.unlink()
            except OSError:
                pass
        _remove_owned_directories(stage_dirs)
        return {
            **cold,
            "bundle_path": str(target),
            "production_receipt_sha256": _sha(receipt_bytes),
        }
    except Exception:
        # Remove only files created here and still byte-identical to their captured
        # file identities. Never recurse into a directory we did not create.
        for path, device, inode, size, digest in reversed(owned):
            try:
                info = path.lstat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_dev == device
                    and info.st_ino == inode
                    and info.st_size == size
                    and _sha(path.read_bytes()) == digest
                ):
                    path.unlink()
            except OSError:
                pass
        for path, device, inode, size, digest in reversed(published):
            try:
                info = path.lstat()
                if (
                    stat.S_ISREG(info.st_mode)
                    and info.st_dev == device
                    and info.st_ino == inode
                    and info.st_size == size
                    and _sha(path.read_bytes()) == digest
                ):
                    path.unlink()
            except OSError:
                pass
        if target_created:
            _remove_owned_directories(target_dirs)
        _remove_owned_directories(stage_dirs)
        raise


def export_current_workflow_evidence(
    handlers: Any,
    workflow: Any,
    task: Any,
    execution: Any,
    destination: Path | str,
) -> dict[str, Any]:
    """Select the current workflow snapshot before creating any evidence bytes."""
    provider = getattr(handlers, "current_evidence_inputs", None)
    if not callable(provider):
        raise ValidationError(
            "Assembly workflow does not expose an authoritative evidence snapshot"
        )
    snapshot = provider(workflow, task, execution)
    if not isinstance(snapshot, Mapping):
        raise ValidationError("Assembly evidence snapshot is malformed")
    return export_local_assembly_evidence(destination, **snapshot)
