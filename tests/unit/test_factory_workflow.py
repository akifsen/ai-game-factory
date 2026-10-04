"""Contract regressions for operator-authored Factory workflows."""

import pytest
from pydantic import ValidationError

from gamefactory.core.accounting.ledger import EntryType, OperationAccount, plan_settlement
from gamefactory.core.domain.factory_workflow import FactoryWorkflowManifest, WorkflowInput


def _task(task_id: str, **overrides):
    value = {
        "task_id": task_id,
        "name": task_id,
        "family": "design",
        "kind": "design",
        "executor_id": "codex",
        "agent_id": "codex",
        "objective": "Produce a bounded proposal",
        "prompt_template_id": "factory-design",
        "prompt_template_version": "1.0.0",
    }
    value.update(overrides)
    return value


def _manifest(*tasks):
    import json

    return FactoryWorkflowManifest.model_validate_json(
        json.dumps(
            {
                "schema_version": "factory-workflow-1.0.0",
                "workflow_id": "wf-review",
                "project_id": "project-review",
                "name": "Reviewable proposal",
                "tasks": list(tasks),
            }
        )
    )


def test_game_write_requires_exact_outputs_and_deterministic_gate():
    with pytest.raises(ValidationError):
        _manifest(
            _task(
                "write",
                game_write=True,
                output_scopes=("scripts/player.gd",),
                expected_artifacts=("scripts/player.gd",),
            )
        )
    with pytest.raises(ValidationError):
        _manifest(
            _task(
                "write",
                game_write=True,
                output_scopes=("scripts/player.gd",),
                expected_artifacts=("scripts/player.gd",),
                required_gates=("visual",),
            )
        )


def test_manifest_rejects_overlapping_output_paths_before_provider_dispatch():
    first = _task("first", output_scopes=("content/level.json",))
    second = _task("second", output_scopes=("content/level.json",))
    with pytest.raises(ValidationError, match="overlap"):
        _manifest(first, second)


@pytest.mark.parametrize(
    "scopes",
    [
        ("assets/Main.gd", "assets/main.gd"),
        ("levels", "levels/arena.json"),
        ("data/a?.png",),
    ],
)
def test_output_scopes_reject_windows_aliases_parent_conflicts_and_invalid_names(scopes):
    with pytest.raises(ValidationError):
        _manifest(_task("writer", output_scopes=scopes))


def test_generated_input_must_bind_declared_predecessor_output():
    producer = _task("producer", output_scopes=("design/level.json",))
    consumer = _task(
        "consumer",
        dependencies=("producer",),
        inputs=(
            {
                "source_task_id": "producer",
                "artifact_path": "design/other.json",
                "purpose": "Use a prior design",
            },
        ),
    )
    with pytest.raises(ValidationError, match="declared by its producer"):
        _manifest(producer, consumer)


@pytest.mark.parametrize(
    "path", ["../secret", "C:/outside", "assets/foo:bar.png", "CON/level.json", ".aws/credentials"]
)
def test_input_paths_reject_escape_ads_device_and_secret_locations(path):
    with pytest.raises(ValidationError):
        WorkflowInput(path=path, purpose="context")


def test_factory_ids_with_colons_are_valid_domain_ids_but_not_used_as_raw_paths():
    manifest = _manifest(_task("feature:generate"))
    assert manifest.tasks[0].task_id == "feature:generate"


def test_vision_agent_kind_cannot_bypass_vision_family_manifest_rules():
    with pytest.raises(ValidationError, match="vision family"):
        _manifest(_task("mislabelled", family="design", kind="vision"))


@pytest.mark.parametrize(
    "selector",
    [
        {
            "path": "screens/capture.png",
            "source_gate": "visual",
            "evidence_name": "capture.png",
            "source_gate_scope": "combined_candidate",
        },
        {
            "source_task_id": "writer",
            "artifact_path": "source.json",
            "source_gate_scope": "predecessor",
        },
    ],
)
def test_input_selectors_reject_dangling_gate_fields(selector):
    with pytest.raises(ValidationError):
        WorkflowInput(purpose="context", **selector)


def test_actual_provider_overage_is_settled_at_observed_amount_not_capped_reservation():
    account = OperationAccount(
        task_id="paid-task",
        reserved_total=1.0,
        project_id="p",
        workflow_id="w",
        cost_unit="USD:request",
    )
    entries = plan_settlement(
        account, 2.75, cost_unit="USD:request", source="operator_reconciliation"
    )
    settlement = next(entry for entry in entries if entry.entry_type == EntryType.SETTLE)
    release = next(entry for entry in entries if entry.entry_type == EntryType.RELEASE)
    assert settlement.amount == 2.75
    assert release.amount == 1.0
