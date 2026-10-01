"""Managed-workflow helpers for coherent private result/marker readiness negatives."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from gamefactory.adapters.assets.v08_candidate_evidence import fingerprint_cold_bundle_payload
from gamefactory.adapters.engines.godot_staging import sha256_file
from gamefactory.adapters.persistence.repositories import ArtifactRepository
from gamefactory.workflows.v08_candidate_evidence_readiness import candidate_evidence_readiness
from tests.unit.v08_candidate_c2b_readiness_fixtures import (
    C2_MANIFEST,
    C2_MARKER,
    C2_RESULT,
    CompletedEvidenceContext,
    coherent_rehash_result_trusted,
)


def assert_managed_baseline_passes_readiness(ctx: CompletedEvidenceContext) -> None:
    readiness = candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert readiness.candidate_evidence_complete is True


def _artifact_triplet(ctx: CompletedEvidenceContext) -> tuple[Any, Any, Any]:
    repo = ArtifactRepository(ctx.workspace.db)
    rows = repo.list_by_workflow(ctx.workflow_id)
    result = next(a for a in rows if a.artifact_type == C2_RESULT)
    marker = next(a for a in rows if a.artifact_type == C2_MARKER)
    manifest = next(a for a in rows if a.artifact_type == C2_MANIFEST)
    return result, marker, manifest


def assert_on_disk_registration_coherent(ctx: CompletedEvidenceContext) -> None:
    """Disk bytes, DB registration, and marker cross-hashes must agree."""
    result, marker, manifest = _artifact_triplet(ctx)
    repo = ArtifactRepository(ctx.workspace.db)
    result_path = ctx.workspace.root / result.relative_path
    marker_path = ctx.workspace.root / marker.relative_path
    manifest_path = ctx.workspace.root / manifest.relative_path

    result_bytes = result_path.read_bytes()
    marker_bytes = marker_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()

    result_hash = hashlib.sha256(result_bytes).hexdigest()
    marker_hash = hashlib.sha256(marker_bytes).hexdigest()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()

    reloaded_result = repo.get(result.id)
    reloaded_marker = repo.get(marker.id)
    reloaded_manifest = repo.get(manifest.id)
    assert reloaded_result is not None
    assert reloaded_marker is not None
    assert reloaded_manifest is not None

    assert reloaded_result.content_hash == result_hash
    assert reloaded_marker.content_hash == marker_hash
    assert reloaded_manifest.content_hash == manifest_hash
    assert reloaded_result.file_size == len(result_bytes)
    assert reloaded_marker.file_size == len(marker_bytes)
    assert reloaded_manifest.file_size == len(manifest_bytes)

    marker_doc = json.loads(marker_bytes.decode("utf-8"))
    assert marker_doc["result_sha256"] == result_hash
    assert marker_doc["manifest_sha256"] == manifest_hash
    assert sha256_file(result_path) == result_hash
    assert sha256_file(marker_path) == marker_hash
    assert sha256_file(manifest_path) == manifest_hash


def _persist_json_artifact(
    ctx: CompletedEvidenceContext,
    artifact: Any,
    path: Any,
) -> Any:
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    updated = replace(
        artifact,
        content_hash=content_hash,
        file_size=path.stat().st_size,
    )
    ArtifactRepository(ctx.workspace.db).save(updated)
    return updated


def mutate_coherent_publication_result_doc(
    ctx: CompletedEvidenceContext,
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    result, marker, _manifest = _artifact_triplet(ctx)
    result_path = ctx.workspace.root / result.relative_path
    marker_path = ctx.workspace.root / marker.relative_path

    doc = json.loads(result_path.read_text(encoding="utf-8"))
    mutator(doc)
    result_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    result = _persist_json_artifact(ctx, result, result_path)

    marker_doc = json.loads(marker_path.read_text(encoding="utf-8"))
    marker_doc["result_sha256"] = result.content_hash
    marker_path.write_text(
        json.dumps(marker_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _persist_json_artifact(ctx, marker, marker_path)
    assert_on_disk_registration_coherent(ctx)


def mutate_coherent_completion_marker_doc(
    ctx: CompletedEvidenceContext,
    mutator: Callable[[dict[str, Any]], None],
) -> None:
    _, marker, _manifest = _artifact_triplet(ctx)
    marker_path = ctx.workspace.root / marker.relative_path
    doc = json.loads(marker_path.read_text(encoding="utf-8"))
    mutator(doc)
    marker_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    _persist_json_artifact(ctx, marker, marker_path)
    assert_on_disk_registration_coherent(ctx)


def mutate_coherent_manifest_bundle_id(
    ctx: CompletedEvidenceContext,
    foreign_bundle_id: str,
) -> None:
    result, marker, manifest = _artifact_triplet(ctx)
    manifest_path = ctx.workspace.root / manifest.relative_path
    marker_path = ctx.workspace.root / marker.relative_path
    result_path = ctx.workspace.root / result.relative_path
    cold_bundle_dir = manifest_path.parent

    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    doc["bundle_id"] = foreign_bundle_id
    manifest_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    manifest = _persist_json_artifact(ctx, manifest, manifest_path)

    live_d1 = fingerprint_cold_bundle_payload(cold_bundle_dir)

    marker_doc = json.loads(marker_path.read_text(encoding="utf-8"))
    marker_doc["manifest_sha256"] = manifest.content_hash
    marker_doc["cold_bundle_payload_digest"] = live_d1
    marker_path.write_text(
        json.dumps(marker_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    marker = _persist_json_artifact(ctx, marker, marker_path)

    result_doc = json.loads(result_path.read_text(encoding="utf-8"))
    result_doc["cold_bundle_payload_digest"] = live_d1
    result_path.write_text(
        json.dumps(result_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    result = _persist_json_artifact(ctx, result, result_path)

    marker_doc = json.loads(marker_path.read_text(encoding="utf-8"))
    marker_doc["result_sha256"] = result.content_hash
    marker_path.write_text(
        json.dumps(marker_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    _persist_json_artifact(ctx, marker, marker_path)
    assert_on_disk_registration_coherent(ctx)


def mutate_coherent_trusted_cold(
    ctx: CompletedEvidenceContext,
    patch_trusted: Callable[[dict[str, Any], dict[str, Any]], None],
) -> None:
    coherent_rehash_result_trusted(ctx, patch_trusted=patch_trusted)
    assert_on_disk_registration_coherent(ctx)
