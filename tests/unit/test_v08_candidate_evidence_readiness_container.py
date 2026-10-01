"""MANAGED publication-container readiness: layout, physical payload, foreign bundle identity."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from gamefactory.workflows.v08_candidate_evidence_readiness import candidate_evidence_readiness
from gamefactory.workflows.v08_candidate_gates import MAX_CANDIDATE_JSON_ARTIFACT_BYTES
from tests.unit.v08_candidate_container_fixtures import (
    add_unexpected_root_directory_junction,
    add_unexpected_root_file,
    assert_foreign_trusted_cold_unsigned_pass,
    copy_foreign_cold_bundle_into_own_published_namespace,
    count_publication_triplet_artifacts,
    delete_cold_bundle_member,
    delete_control_file,
    drift_control_bytes_without_registration_update,
    managed_container_baseline,
    pad_control_json_past_bounded_limit,
    publication_container_paths,
    relocate_control_outside_published_container,
    remove_owned_container_junction,
    repoint_triplet_through_container_junction,
    tamper_cold_bundle_member,
)
from tests.unit.v08_candidate_managed_control_fixtures import (
    assert_managed_baseline_passes_readiness,
    assert_on_disk_registration_coherent,
)

pytestmark = pytest.mark.candidate_slow


def test_managed_container_baseline_passes_readiness(tmp_path: Path) -> None:
    ctx = managed_container_baseline(tmp_path)
    assert_managed_baseline_passes_readiness(ctx)
    assert_on_disk_registration_coherent(ctx)


def test_rejects_result_relocated_outside_published_container(tmp_path: Path) -> None:
    ctx = managed_container_baseline(tmp_path)
    relocate_control_outside_published_container(ctx, control="result")
    with pytest.raises(CandidateCurrentnessError, match=r"same container"):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_rejects_marker_relocated_outside_published_container(tmp_path: Path) -> None:
    ctx = managed_container_baseline(tmp_path)
    relocate_control_outside_published_container(ctx, control="marker")
    with pytest.raises(CandidateCurrentnessError, match=r"same container"):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_rejects_unexpected_root_file_in_publication_container(tmp_path: Path) -> None:
    ctx = managed_container_baseline(tmp_path)
    add_unexpected_root_file(ctx)
    with pytest.raises(CandidateCurrentnessError, match=r"unexpected top-level"):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_rejects_unexpected_root_junction_and_preserves_foreign_probe(tmp_path: Path) -> None:
    if sys.platform not in ("win32", "linux"):
        pytest.skip("junction probe requires Windows or Linux")
    ctx = managed_container_baseline(tmp_path)
    foreign_probe = tmp_path / "foreign_junction_target" / "probe.txt"
    link = add_unexpected_root_directory_junction(ctx, foreign_probe=foreign_probe)
    try:
        with pytest.raises(CandidateCurrentnessError, match=r"unexpected top-level"):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    finally:
        remove_owned_container_junction(link)
    assert foreign_probe.read_text(encoding="utf-8") == "FOREIGN_PROBE_BYTES"


def test_rejects_lexical_junction_on_publication_container_path(tmp_path: Path) -> None:
    if sys.platform not in ("win32", "linux"):
        pytest.skip("junction probe requires Windows or Linux")
    ctx = managed_container_baseline(tmp_path)
    junction = repoint_triplet_through_container_junction(ctx)
    try:
        with pytest.raises(
            (CandidateCurrentnessError, ValidationError),
            match=r"bounded lexical path|not a bounded lexical",
        ):
            candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    finally:
        remove_owned_container_junction(junction)


def test_rejects_missing_cold_bundle_payload_file(tmp_path: Path) -> None:
    ctx = managed_container_baseline(tmp_path)
    delete_cold_bundle_member(ctx, "snapshot/snapshot.json")
    with pytest.raises(
        CandidateCurrentnessError,
        match=r"cold bundle digest drifted|not a regular file|missing",
    ):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_rejects_tampered_cold_bundle_without_marker_rehash(tmp_path: Path) -> None:
    ctx = managed_container_baseline(tmp_path)
    tamper_cold_bundle_member(ctx, "snapshot/snapshot.json")
    with pytest.raises(CandidateCurrentnessError, match=r"cold bundle digest drifted"):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


@pytest.mark.parametrize("control", ["result", "marker"])
def test_rejects_missing_publication_control_file(tmp_path: Path, control: str) -> None:
    ctx = managed_container_baseline(tmp_path)
    delete_control_file(ctx, control=control)
    with pytest.raises(
        (CandidateCurrentnessError, ArtifactError),
        match=r"path missing on disk|file missing on disk",
    ):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_rejects_on_disk_control_hash_drift_without_db_update(tmp_path: Path) -> None:
    ctx = managed_container_baseline(tmp_path)
    drift_control_bytes_without_registration_update(ctx, control="result")
    with pytest.raises(
        (CandidateCurrentnessError, ArtifactError),
        match=r"registered content_hash mismatch|content hash mismatch",
    ):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_rejects_oversized_result_control_without_extra_artifact_rows(tmp_path: Path) -> None:
    ctx = managed_container_baseline(tmp_path)
    before = count_publication_triplet_artifacts(ctx)
    pad_control_json_past_bounded_limit(ctx, control="result")
    assert count_publication_triplet_artifacts(ctx) == before
    layout = publication_container_paths(ctx)
    result_bytes = layout.result_path.read_bytes()
    assert len(result_bytes) > MAX_CANDIDATE_JSON_ARTIFACT_BYTES
    assert_on_disk_registration_coherent(ctx)
    with pytest.raises(
        CandidateCurrentnessError,
        match=r"exceeds bounded (read limit|byte limit)|publication control JSON exceeds bounded byte limit",
    ):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)


def test_readiness_invokes_trusted_cold_on_each_call_not_cached(tmp_path: Path) -> None:
    from gamefactory.workflows import v08_candidate_evidence_readiness as readiness_mod

    ctx = managed_container_baseline(tmp_path)
    original = readiness_mod.trusted_cold_verify_candidate_bundle
    calls = 0

    def _counting(bundle_dir: Path) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return original(bundle_dir)

    with patch.object(readiness_mod, "trusted_cold_verify_candidate_bundle", _counting):
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
        candidate_evidence_readiness(ctx.handlers, ctx.workflow_id)
    assert calls == 2


def test_rejects_foreign_coherent_bundle_copied_into_own_namespace(tmp_path: Path) -> None:
    from tests.unit.v08_candidate_c2b_readiness_fixtures import run_completed_managed_evidence

    own = managed_container_baseline(tmp_path / "own")
    foreign = run_completed_managed_evidence(tmp_path / "foreign")
    assert_foreign_trusted_cold_unsigned_pass(foreign)
    assert_managed_baseline_passes_readiness(own)

    copy_foreign_cold_bundle_into_own_published_namespace(own, foreign)
    assert_on_disk_registration_coherent(own)

    with pytest.raises(
        CandidateCurrentnessError,
        match=(
            r"bundle_id does not match workflow binding|"
            r"published bundle snapshot does not match live upstream|"
            r"cold manifest workflow_id does not match"
        ),
    ):
        candidate_evidence_readiness(own.handlers, own.workflow_id)

    layout = publication_container_paths(own)
    manifest_doc = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
    assert manifest_doc.get("bundle_id", "").startswith("candidate-live-")
    assert manifest_doc.get("bundle_id") != f"candidate-live-{own.workflow_id}"
