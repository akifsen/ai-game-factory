"""V0.6 audit evidence for the paid production lifecycle (fake provider only)."""

from __future__ import annotations

from pathlib import Path

from test_accounting_v06 import _approve_both_gates, _setup_accounting_env

from gamefactory.adapters.persistence.repositories import (
    AuditLogRepository,
    ProviderOperationIntentRepository,
)


def test_paid_lifecycle_has_stable_audit_evidence(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    _approve_both_gates(engine, db, workflow_id)
    engine.run_workflow(workflow_id)

    assert fake.invocation_count == 1
    audit = AuditLogRepository(db)
    intents = ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)
    assert len(intents) == 1
    intent = intents[0]
    intent_actions = [e.action for e in audit.list_by_entity("ProviderOperationIntent", intent.id)]
    assert intent_actions[0] == "PROVIDER_INTENT_RECORDED"
    assert "PROVIDER_TASK_ID_PERSISTED" in intent_actions
    transitions = [
        (e.previous_state, e.new_state)
        for e in audit.list_by_entity("ProviderOperationIntent", intent.id)
        if e.action == "PROVIDER_STATUS_CHANGED"
    ]
    assert transitions[-1][1] == "SUCCEEDED"
    assert intent.updated_at > intent.created_at

    snapshot_actions = [
        e.action for e in audit.list_by_entity("Task", f"{workflow_id}-PAID-REQUEST")
    ]
    assert "PAID_REQUEST_SNAPSHOT_CREATED" in snapshot_actions
    readiness_actions = [e.action for e in audit.list_by_entity("Task", f"{workflow_id}-READINESS")]
    assert "PRODUCTION_READINESS_EVALUATED" in readiness_actions
    paid_actions = [
        e.action for e in audit.list_by_entity("Task", f"{workflow_id}-PAID-GENERATION")
    ]
    assert "COST_SETTLED" in paid_actions


def test_saving_an_unchanged_intent_adds_no_audit_noise(tmp_path: Path) -> None:
    engine, db, workflow_id, _, _ = _setup_accounting_env(tmp_path)
    _approve_both_gates(engine, db, workflow_id)
    engine.run_workflow(workflow_id)
    repo = ProviderOperationIntentRepository(db)
    intent = repo.list_by_workflow(workflow_id)[0]
    audit = AuditLogRepository(db)
    before = len(audit.list_by_entity("ProviderOperationIntent", intent.id))

    repo.save(intent)

    assert len(audit.list_by_entity("ProviderOperationIntent", intent.id)) == before
