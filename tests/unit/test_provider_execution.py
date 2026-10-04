from __future__ import annotations

import base64
import hashlib

import pytest
from pydantic import ValidationError

from gamefactory.core.domain.agent_contracts import AgentOutcome, AgentResultProposal
from gamefactory.core.domain.provider_execution import (
    ProviderExecutionStatus,
    ProviderOutputFile,
    ProviderRun,
)


def proposal():
    return AgentResultProposal(
        schema_version="1.0.0",
        task_id="task-1",
        agent_id="agent.test",
        outcome=AgentOutcome.PROPOSED,
        summary="Untrusted proposal",
    )


def test_provider_run_round_trips_json_arrays_and_strict_enums():
    content = b"proposed text"
    run = ProviderRun(
        schema_version="provider-run-1.0.0",
        status=ProviderExecutionStatus.COMPLETED,
        proposal=proposal(),
        files=(
            ProviderOutputFile(
                path="src/new.txt",
                content_base64=base64.b64encode(content).decode(),
                sha256=hashlib.sha256(content).hexdigest(),
                media_type="text/plain",
            ),
        ),
    )
    decoded = ProviderRun.from_value(run.to_dict())
    assert decoded.files[0].content_bytes() == content
    assert decoded.status is ProviderExecutionStatus.COMPLETED
    assert decoded.proposal.outcome is AgentOutcome.PROPOSED


def test_provider_output_rejects_hash_mismatch_and_escape_path():
    encoded = base64.b64encode(b"x").decode()
    with pytest.raises(ValidationError):
        ProviderOutputFile(
            path="../outside",
            content_base64=encoded,
            sha256=hashlib.sha256(b"x").hexdigest(),
            media_type="text/plain",
        )
    with pytest.raises(ValidationError):
        ProviderOutputFile(
            path="src/x", content_base64=encoded, sha256="0" * 64, media_type="text/plain"
        )


def test_provider_run_wire_mapping_does_not_claim_gate_authority():
    raw = {
        "schema_version": "provider-run-1.0.0",
        "status": "COMPLETED",
        "proposal": proposal().model_dump(mode="json"),
        "files": [],
        "metadata": {"test_passed": True},
    }
    loaded = ProviderRun.from_value(raw)
    assert loaded.proposal.outcome is AgentOutcome.PROPOSED
    assert loaded.metadata["test_passed"] is True  # opaque claim, not a Factory verification


def test_proposed_media_content_bound_exceeds_one_megabyte():
    content = b"x" * (1_125_001)
    from gamefactory.core.domain.agent_contracts import ProposedFile

    file = ProposedFile(
        path="assets/large.wav",
        operation="CREATE",
        content_base64=base64.b64encode(content).decode("ascii"),
        output_sha256=hashlib.sha256(content).hexdigest(),
    )
    assert len(file.content_base64) > 1_500_000
    assert base64.b64decode(file.content_base64, validate=True) == content
