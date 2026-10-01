"""Managed publication-container helpers for readiness container matrix (test-only)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.v08_candidate_evidence import (
    fingerprint_cold_bundle_payload,
    trusted_cold_verify_candidate_bundle,
)
from gamefactory.adapters.persistence.repositories import ArtifactRepository
from gamefactory.workflows.v08_candidate_gates import MAX_CANDIDATE_JSON_ARTIFACT_BYTES
from gamefactory.workflows.v08_candidate_workflow import candidate_workflow_readiness
from tests.unit.v08_candidate_c2b_readiness_fixtures import (
    C2_MANIFEST,
    C2_MARKER,
    C2_RESULT,
    CompletedEvidenceContext,
    cold_bundle_dir_for_workflow,
    fresh_trusted_cold_dict,
    run_completed_managed_evidence,
)
from tests.unit.v08_candidate_managed_control_fixtures import (
    assert_managed_baseline_passes_readiness,
    assert_on_disk_registration_coherent,
)


@dataclass(frozen=True)
class PublicationContainerPaths:
    container: Path
    cold_bundle: Path
    manifest_path: Path
    result_path: Path
    marker_path: Path
    manifest_art: Any
    result_art: Any
    marker_art: Any


def managed_container_baseline(tmp_path: Path) -> CompletedEvidenceContext:
    ctx = run_completed_managed_evidence(tmp_path)
    assert_managed_baseline_passes_readiness(ctx)
    assert_on_disk_registration_coherent(ctx)
    return ctx


def publication_container_paths(ctx: CompletedEvidenceContext) -> PublicationContainerPaths:
    repo = ArtifactRepository(ctx.workspace.db)
    rows = repo.list_by_workflow(ctx.workflow_id)
    manifest_art = next(a for a in rows if a.artifact_type == C2_MANIFEST)
    result_art = next(a for a in rows if a.artifact_type == C2_RESULT)
    marker_art = next(a for a in rows if a.artifact_type == C2_MARKER)
    manifest_path = ctx.workspace.root / manifest_art.relative_path
    cold_bundle = manifest_path.parent
    container = cold_bundle.parent
    return PublicationContainerPaths(
        container=container,
        cold_bundle=cold_bundle,
        manifest_path=manifest_path,
        result_path=ctx.workspace.root / result_art.relative_path,
        marker_path=ctx.workspace.root / marker_art.relative_path,
        manifest_art=manifest_art,
        result_art=result_art,
        marker_art=marker_art,
    )


def count_publication_triplet_artifacts(ctx: CompletedEvidenceContext) -> int:
    rows = ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
    types = {C2_MANIFEST, C2_RESULT, C2_MARKER}
    return sum(1 for row in rows if row.artifact_type in types)


def _persist_path_artifact(ctx: CompletedEvidenceContext, artifact: Any, path: Path) -> Any:
    raw = path.read_bytes()
    updated = replace(
        artifact,
        content_hash=hashlib.sha256(raw).hexdigest(),
        file_size=len(raw),
    )
    ArtifactRepository(ctx.workspace.db).save(updated)
    return updated


def _update_relative_path(
    ctx: CompletedEvidenceContext, artifact_id: str, relative_path: str
) -> None:
    with ctx.workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE artifacts SET relative_path = ? WHERE id = ?;",
            (relative_path, artifact_id),
        )


def relocate_control_outside_published_container(
    ctx: CompletedEvidenceContext,
    *,
    control: str,
) -> Path:
    """Move result or marker bytes outside the publication container; DB paths follow."""
    layout = publication_container_paths(ctx)
    if control == "result":
        source = layout.result_path
        art = layout.result_art
    elif control == "marker":
        source = layout.marker_path
        art = layout.marker_art
    else:
        raise ValueError(f"unknown control: {control}")
    stray_parent = (
        ctx.workspace.root
        / ".gamefactory"
        / "candidate-evidence"
        / ctx.workflow_id
        / "relocated-controls"
    )
    stray_parent.mkdir(parents=True, exist_ok=True)
    dest = stray_parent / source.name
    if dest.exists():
        dest.unlink()
    shutil.move(str(source), str(dest))
    new_rel = dest.relative_to(ctx.workspace.root).as_posix()
    _update_relative_path(ctx, art.id, new_rel)
    assert dest.is_file()
    assert not source.exists()
    assert hashlib.sha256(dest.read_bytes()).hexdigest() == art.content_hash
    return dest


def add_unexpected_root_file(
    ctx: CompletedEvidenceContext, name: str = "extra-root-probe.txt"
) -> Path:
    layout = publication_container_paths(ctx)
    probe = layout.container / name
    probe.write_text("unregistered-root-file", encoding="utf-8")
    return probe


def add_unexpected_root_directory_junction(
    ctx: CompletedEvidenceContext,
    *,
    foreign_probe: Path,
) -> Path:
    """Add a junction/symlink directory entry at the publication container root."""
    layout = publication_container_paths(ctx)
    link_path = layout.container / "foreign-root-junction"
    foreign_probe.parent.mkdir(parents=True, exist_ok=True)
    foreign_probe.write_text("FOREIGN_PROBE_BYTES", encoding="utf-8")
    before = foreign_probe.read_text(encoding="utf-8")
    target_dir = foreign_probe.parent
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            check=True,
            capture_output=True,
        )
    elif sys.platform == "linux":
        os.symlink(target_dir, link_path, target_is_directory=True)
    else:
        raise OSError("junction probe requires Windows or Linux")
    _assert_directory_link_alias(link_path, target_dir)
    assert foreign_probe.read_text(encoding="utf-8") == before
    return link_path


def remove_owned_container_junction(link_path: Path) -> None:
    if sys.platform == "win32":
        link_path.rmdir()
    else:
        link_path.unlink()


def repoint_triplet_through_container_junction(ctx: CompletedEvidenceContext) -> Path:
    """Re-register artifact paths through a junction alias of the workflow evidence folder."""
    layout = publication_container_paths(ctx)
    workflow_dir = layout.container.parent
    junction = workflow_dir.parent / f"{workflow_dir.name}_lexical"
    if junction.exists():
        raise AssertionError(f"junction slot already exists: {junction}")
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(workflow_dir)],
            check=True,
            capture_output=True,
        )
    elif sys.platform == "linux":
        os.symlink(workflow_dir, junction, target_is_directory=True)
    else:
        raise OSError("lexical junction probe requires Windows or Linux")
    _assert_directory_link_alias(junction, workflow_dir)
    token = f"/{workflow_dir.name}/"
    replacement = f"/{junction.name}/"
    for art in (layout.manifest_art, layout.result_art, layout.marker_art):
        rel = art.relative_path.replace("\\", "/")
        if token not in rel:
            raise AssertionError(f"unexpected artifact path layout: {rel}")
        new_rel = rel.replace(token, replacement, 1)
        _update_relative_path(ctx, art.id, new_rel)
    return junction


def delete_cold_bundle_member(ctx: CompletedEvidenceContext, relative_inside_bundle: str) -> Path:
    layout = publication_container_paths(ctx)
    target = layout.cold_bundle / relative_inside_bundle
    target.unlink()
    return target


def tamper_cold_bundle_member(ctx: CompletedEvidenceContext, relative_inside_bundle: str) -> Path:
    layout = publication_container_paths(ctx)
    target = layout.cold_bundle / relative_inside_bundle
    target.write_bytes(target.read_bytes() + b"TAMPER")
    return target


def delete_control_file(ctx: CompletedEvidenceContext, *, control: str) -> Path:
    layout = publication_container_paths(ctx)
    path = layout.result_path if control == "result" else layout.marker_path
    path.unlink()
    return path


def drift_control_bytes_without_registration_update(
    ctx: CompletedEvidenceContext,
    *,
    control: str,
) -> None:
    layout = publication_container_paths(ctx)
    path = layout.result_path if control == "result" else layout.marker_path
    path.write_bytes(path.read_bytes() + b" ")


def _assert_directory_link_alias(link_path: Path, expected_target: Path) -> None:
    resolved_target = expected_target.resolve()
    assert link_path.is_dir()
    if sys.platform == "win32":
        junction_check = getattr(link_path, "is_junction", None)
        if callable(junction_check):
            assert junction_check()
    elif sys.platform == "linux":
        assert link_path.is_symlink()
    assert link_path.resolve() == resolved_target


def pad_control_json_past_bounded_limit(ctx: CompletedEvidenceContext, *, control: str) -> None:
    """Grow a control JSON file past MAX while keeping DB + marker cross-hashes coherent."""
    if control != "result":
        raise ValueError("bounded oversize probe supports result control only")
    layout = publication_container_paths(ctx)
    path = layout.result_path
    doc = json.loads(path.read_text(encoding="utf-8"))
    trusted = doc.get("trusted_cold_result")
    if not isinstance(trusted, dict):
        raise AssertionError("result control missing trusted_cold_result object")
    pad_key = "__bounded_size_probe_pad__"
    chunk = "x" * 65536
    pad_value = ""
    while True:
        pad_value += chunk
        trusted[pad_key] = pad_value
        serialized = json.dumps(doc, sort_keys=True, indent=2) + "\n"
        if len(serialized.encode("utf-8")) > MAX_CANDIDATE_JSON_ARTIFACT_BYTES:
            break
    path.write_text(serialized, encoding="utf-8")
    json.loads(path.read_text(encoding="utf-8"))
    result_art = _persist_path_artifact(ctx, layout.result_art, path)
    marker_doc = json.loads(layout.marker_path.read_text(encoding="utf-8"))
    marker_doc["result_sha256"] = result_art.content_hash
    layout.marker_path.write_text(
        json.dumps(marker_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _persist_path_artifact(ctx, layout.marker_art, layout.marker_path)
    assert_on_disk_registration_coherent(ctx)
    result_bytes = path.read_bytes()
    assert len(result_bytes) > MAX_CANDIDATE_JSON_ARTIFACT_BYTES
    assert result_art.file_size == len(result_bytes)
    assert result_art.content_hash == hashlib.sha256(result_bytes).hexdigest()


def _clear_directory(path: Path) -> None:
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def copy_foreign_cold_bundle_into_own_published_namespace(
    own: CompletedEvidenceContext,
    foreign: CompletedEvidenceContext,
) -> None:
    own_bundle = cold_bundle_dir_for_workflow(own)
    foreign_bundle = cold_bundle_dir_for_workflow(foreign)
    _clear_directory(own_bundle)
    for child in foreign_bundle.iterdir():
        dest = own_bundle / child.name
        if child.is_dir():
            shutil.copytree(child, dest)
        else:
            shutil.copy2(child, dest)
    synchronize_own_controls_to_live_bundle_bytes(own)


def synchronize_own_controls_to_live_bundle_bytes(ctx: CompletedEvidenceContext) -> None:
    """Rebind own result/marker to actual on-disk bundle bytes (no foreign identity rewrite)."""
    layout = publication_container_paths(ctx)
    manifest_art = _persist_path_artifact(ctx, layout.manifest_art, layout.manifest_path)
    live_d1 = fingerprint_cold_bundle_payload(layout.cold_bundle)
    trusted = trusted_cold_verify_candidate_bundle(layout.cold_bundle)
    upstream = candidate_workflow_readiness(ctx.handlers, ctx.workflow_id)

    result_doc = json.loads(layout.result_path.read_text(encoding="utf-8"))
    result_doc["trusted_cold_result"] = trusted
    result_doc["cold_bundle_payload_digest"] = live_d1
    result_doc["snapshot_fingerprint"] = upstream.fingerprint()
    result_doc["workflow_id"] = ctx.workflow_id
    result_doc["evidence_task_id"] = ctx.evidence_task_id
    result_doc["evidence_execution_id"] = ctx.evidence_execution_id
    layout.result_path.write_text(
        json.dumps(result_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    result_art = _persist_path_artifact(ctx, layout.result_art, layout.result_path)

    marker_doc = json.loads(layout.marker_path.read_text(encoding="utf-8"))
    marker_doc["manifest_sha256"] = manifest_art.content_hash
    marker_doc["cold_bundle_payload_digest"] = live_d1
    marker_doc["result_sha256"] = result_art.content_hash
    marker_doc["workflow_id"] = ctx.workflow_id
    marker_doc["evidence_task_id"] = ctx.evidence_task_id
    marker_doc["evidence_execution_id"] = ctx.evidence_execution_id
    marker_doc["snapshot_fingerprint"] = upstream.fingerprint()
    layout.marker_path.write_text(
        json.dumps(marker_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _persist_path_artifact(ctx, layout.marker_art, layout.marker_path)
    assert_on_disk_registration_coherent(ctx)


def assert_foreign_trusted_cold_unsigned_pass(foreign: CompletedEvidenceContext) -> dict[str, Any]:
    trusted = fresh_trusted_cold_dict(foreign)
    assert trusted.get("outcome") == "CONSISTENT_BUT_UNAUTHENTICATED"
    assert trusted.get("execution_provenance") == "CONSISTENT_BUT_UNAUTHENTICATED"
    assert trusted.get("validation_status") == "PASS"
    assert trusted.get("runtime_status") == "PASS"
    assert trusted.get("production_eligible") is False
    assert trusted.get("promotion_eligible") is False
    return trusted
