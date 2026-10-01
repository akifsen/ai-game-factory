"""READINESS-CONTROLS pure-intrinsic contract (PRIVATE result/marker JSON + semantics)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.assets.v08_candidate_evidence import (
    MARKER_SCHEMA,
    PUBLICATION_RESULT_SCHEMA,
    assert_completion_marker_controls,
    assert_publication_result_bindings,
    assert_trusted_cold_result_semantics,
    load_bounded_publication_control_json,
    parse_bounded_publication_json,
)
from gamefactory.adapters.persistence.repositories import ArtifactRepository
from gamefactory.core.domain.errors import ValidationError
from tests.unit.v08_candidate_c2b_readiness_fixtures import (
    C2_MARKER,
    C2_RESULT,
    CompletedEvidenceContext,
    run_completed_managed_evidence,
)

pytestmark = pytest.mark.candidate_slow


@pytest.fixture(scope="module")
def controls_managed_baseline(
    tmp_path_factory: pytest.TempPathFactory,
) -> CompletedEvidenceContext:
    root = tmp_path_factory.mktemp("readiness-controls-managed-baseline")
    return run_completed_managed_evidence(root)


@pytest.fixture(scope="module")
def controls_result_doc(controls_managed_baseline: CompletedEvidenceContext) -> dict[str, Any]:
    ctx = controls_managed_baseline
    result = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == C2_RESULT
    )
    path = ctx.workspace.root / result.relative_path
    return copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def controls_marker_doc(controls_managed_baseline: CompletedEvidenceContext) -> dict[str, Any]:
    ctx = controls_managed_baseline
    marker = next(
        a
        for a in ArtifactRepository(ctx.workspace.db).list_by_workflow(ctx.workflow_id)
        if a.artifact_type == C2_MARKER
    )
    path = ctx.workspace.root / marker.relative_path
    return copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def controls_trusted_cold_template(controls_result_doc: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(controls_result_doc["trusted_cold_result"])


@pytest.fixture(scope="module")
def controls_result_binding_args(
    controls_managed_baseline: CompletedEvidenceContext,
    controls_result_doc: dict[str, Any],
) -> dict[str, Any]:
    ctx = controls_managed_baseline
    doc = controls_result_doc
    return {
        "workflow_id": ctx.workflow_id,
        "task_id": str(doc["evidence_task_id"]),
        "execution_id": ctx.evidence_execution_id,
        "attempt_number": int(doc["evidence_attempt_number"]),
        "snapshot_fingerprint": str(doc["snapshot_fingerprint"]),
        "cold_bundle_payload_digest": str(doc["cold_bundle_payload_digest"]),
    }


def _assert_result_bindings_positive(doc: dict[str, Any], binding_args: dict[str, Any]) -> None:
    assert_publication_result_bindings(doc, **binding_args)


def _assert_marker_controls_positive(doc: dict[str, Any], workflow_id: str) -> None:
    assert_completion_marker_controls(doc, workflow_id=workflow_id)


def _assert_trusted_cold_positive(trusted: dict[str, Any]) -> None:
    assert_trusted_cold_result_semantics(trusted)


def test_controls_result_and_marker_positive_seed(
    controls_result_doc: dict[str, Any],
    controls_marker_doc: dict[str, Any],
    controls_trusted_cold_template: dict[str, Any],
    controls_result_binding_args: dict[str, Any],
    controls_managed_baseline: CompletedEvidenceContext,
) -> None:
    _assert_result_bindings_positive(controls_result_doc, controls_result_binding_args)
    _assert_marker_controls_positive(controls_marker_doc, controls_managed_baseline.workflow_id)
    _assert_trusted_cold_positive(controls_trusted_cold_template)


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (b"[]", r"root must be an object"),
        (b'{"x": NaN}', r"invalid JSON constant"),
        (b'{"x": Infinity}', r"invalid JSON constant"),
        (b"-Infinity", r"invalid JSON constant"),
    ],
    ids=["root-array", "nan", "infinity", "neg-infinity-root"],
)
def test_bounded_publication_json_rejects_non_object_and_nonfinite(raw: bytes, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        parse_bounded_publication_json(raw)


def test_load_bounded_publication_json_rejects_duplicate_keys_on_regular_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "control.json"
    path.write_bytes(b'{"schema_version": "a", "schema_version": "b"}')
    with pytest.raises(ValidationError, match="duplicate JSON key"):
        load_bounded_publication_control_json(path, label="control.json")


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda d: d.pop("schema_version"), r"schema_version is invalid"),
        (lambda d: d.update({"schema_version": "wrong-schema"}), r"schema_version is invalid"),
        (lambda d: d.pop("workflow_id"), r"workflow_id must be a non-empty string id"),
        (lambda d: d.update({"extra_control_field": True}), r"unknown control fields"),
        (lambda d: d.update({"evidence_attempt_number": True}), r"must be a JSON integer"),
        (lambda d: d.update({"evidence_attempt_number": 1.0}), r"must be a JSON integer"),
        (lambda d: d.update({"evidence_attempt_number": "1"}), r"must be a JSON integer"),
        (lambda d: d.update({"evidence_attempt_number": 0}), r"evidence_attempt_number"),
        (lambda d: d.update({"evidence_attempt_number": -1}), r"evidence_attempt_number"),
        (lambda d: d.update({"trusted_cold_result": []}), r"trusted_cold_result"),
    ],
    ids=[
        "missing-schema",
        "wrong-schema",
        "missing-workflow-id",
        "unknown-field",
        "attempt-bool",
        "attempt-float",
        "attempt-string",
        "attempt-zero",
        "attempt-negative",
        "trusted-not-object",
    ],
)
def test_intrinsic_publication_result_controls_reject(
    controls_result_doc: dict[str, Any],
    controls_result_binding_args: dict[str, Any],
    mutator: Any,
    match: str,
) -> None:
    doc = copy.deepcopy(controls_result_doc)
    _assert_result_bindings_positive(doc, controls_result_binding_args)
    mutator(doc)
    with pytest.raises(ValidationError, match=match):
        _assert_result_bindings_positive(doc, controls_result_binding_args)


@pytest.mark.parametrize(
    ("binding_kwarg", "wrong_value", "match"),
    [
        ("workflow_id", "WF-INTRINSIC-FOREIGN", r"workflow_id"),
        ("task_id", "TASK-INTRINSIC-FOREIGN", r"evidence_task_id"),
        ("execution_id", "EXEC-INTRINSIC-FOREIGN", r"evidence_execution_id"),
        ("attempt_number", 99, r"evidence_attempt_number"),
        ("snapshot_fingerprint", "c" * 64, r"snapshot_fingerprint"),
        ("cold_bundle_payload_digest", "d" * 64, r"cold_bundle_payload_digest"),
    ],
)
def test_intrinsic_publication_result_binding_identity_mismatch(
    controls_result_doc: dict[str, Any],
    controls_result_binding_args: dict[str, Any],
    binding_kwarg: str,
    wrong_value: object,
    match: str,
) -> None:
    doc = copy.deepcopy(controls_result_doc)
    args = dict(controls_result_binding_args)
    _assert_result_bindings_positive(doc, args)
    args[binding_kwarg] = wrong_value
    with pytest.raises(ValidationError, match=match):
        _assert_result_bindings_positive(doc, args)


def test_intrinsic_publication_result_rejects_wrong_manifest_bundle_id(
    controls_result_doc: dict[str, Any],
    controls_result_binding_args: dict[str, Any],
) -> None:
    doc = copy.deepcopy(controls_result_doc)
    _assert_result_bindings_positive(doc, controls_result_binding_args)
    with pytest.raises(ValidationError, match="bundle_id does not match workflow binding"):
        assert_publication_result_bindings(
            doc,
            manifest_bundle_id="candidate-live-FOREIGN",
            **controls_result_binding_args,
        )


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda d: d.pop("schema_version"), r"schema_version is invalid"),
        (
            lambda d: d.update({"schema_version": "wrong-marker-schema"}),
            r"schema_version is invalid",
        ),
        (lambda d: d.pop("manifest_sha256"), r"manifest_sha256 must be a 64-character"),
        (lambda d: d.update({"unknown_marker_field": 1}), r"unknown control fields"),
        (lambda d: d.update({"workflow_id": "WF-MARKER-FOREIGN"}), r"workflow_id mismatch"),
        (lambda d: d.update({"evidence_attempt_number": True}), r"must be a JSON integer"),
        (lambda d: d.update({"evidence_attempt_number": 1.0}), r"must be a JSON integer"),
        (lambda d: d.update({"evidence_attempt_number": "1"}), r"must be a JSON integer"),
        (
            lambda d: d.update({"candidate_evidence_complete": False}),
            r"does not attest evidence completion",
        ),
        (lambda d: d.update({"production_eligible": True}), r"production_eligible must be false"),
        (lambda d: d.update({"promotion_eligible": True}), r"promotion_eligible must be false"),
        (lambda d: d.update({"production_eligible": 1}), r"production_eligible must be false"),
        (lambda d: d.update({"promotion_eligible": 0}), r"promotion_eligible must be false"),
    ],
    ids=[
        "missing-schema",
        "wrong-schema",
        "missing-manifest-sha",
        "unknown-field",
        "foreign-workflow-id",
        "attempt-bool",
        "attempt-float",
        "attempt-string",
        "complete-false",
        "production-true",
        "promotion-true",
        "production-int-one",
        "promotion-int-zero",
    ],
)
def test_intrinsic_completion_marker_controls_reject(
    controls_marker_doc: dict[str, Any],
    controls_managed_baseline: CompletedEvidenceContext,
    mutator: Any,
    match: str,
) -> None:
    doc = copy.deepcopy(controls_marker_doc)
    _assert_marker_controls_positive(doc, controls_managed_baseline.workflow_id)
    mutator(doc)
    with pytest.raises(ValidationError, match=match):
        _assert_marker_controls_positive(doc, controls_managed_baseline.workflow_id)


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda t: t.update({"outcome": "FAILED"}), r"cold result outcome"),
        (
            lambda t: t.update({"execution_provenance": "AUTHENTICATED_PRODUCTION"}),
            r"execution_provenance",
        ),
        (lambda t: t.update({"integrity_outcome": "BROKEN"}), r"integrity_outcome"),
        (lambda t: t.update({"validation_status": "FAIL"}), r"validation_status"),
        (lambda t: t.update({"runtime_status": "FAIL"}), r"runtime_status"),
        (lambda t: t.update({"production_eligible": True}), r"eligibility flags"),
        (lambda t: t.update({"promotion_eligible": True}), r"eligibility flags"),
        (
            lambda t: t.update({"candidate_evidence_complete": False}),
            r"candidate_evidence_complete",
        ),
        (lambda t: t.update({"reviewed_pins_match": False}), r"reviewed_pins_match"),
        (lambda t: t.update({"reviewed_pins_match": 1}), r"reviewed_pins_match"),
        (lambda t: t.update({"reviewed_pins_match": 0}), r"reviewed_pins_match"),
    ],
    ids=[
        "outcome-failed",
        "provenance-authenticated-production",
        "integrity-not-verified",
        "validation-fail",
        "runtime-fail",
        "production-eligible-true",
        "promotion-eligible-true",
        "complete-false",
        "reviewed-pins-false",
        "reviewed-pins-int-one",
        "reviewed-pins-int-zero",
    ],
)
def test_intrinsic_trusted_cold_unsigned_semantics_reject(
    controls_trusted_cold_template: dict[str, Any],
    mutator: Any,
    match: str,
) -> None:
    trusted = copy.deepcopy(controls_trusted_cold_template)
    _assert_trusted_cold_positive(trusted)
    mutator(trusted)
    with pytest.raises(ValidationError, match=match):
        _assert_trusted_cold_positive(trusted)


def test_intrinsic_marker_and_result_schema_constants_match_product() -> None:
    assert MARKER_SCHEMA == "candidate-evidence-completion-marker-0.8.0"
    assert PUBLICATION_RESULT_SCHEMA == "candidate-evidence-publication-result-0.8.0"
