"""V0.6 pre-spend production readiness gate and pre-POST critical recheck.

Real probe logic is exercised with injected Blender/Godot adapters so the suite
needs neither tool installed. No provider is contacted.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_accounting_v06 import ConfigurableCostFake, _decision, _setup_accounting_env

from gamefactory.adapters.dcc.base import DccDetectionResult
from gamefactory.adapters.engines.base import EngineDetectionResult
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    AuditLogRepository,
    CostLedgerRepository,
    ProductionReadinessRepository,
    ProviderOperationIntentRepository,
)
from gamefactory.cli.main import _doctor_text
from gamefactory.core.domain.models import ApprovalStatus, WorkflowStatus
from gamefactory.workflows.production_readiness import READINESS_SCHEMA, DefaultReadinessProbes


class FakeBlender:
    def __init__(self, version: str = "Blender 4.2.3") -> None:
        self.version = version

    def detect_tool(self, custom_path: str | None = None) -> DccDetectionResult:
        return DccDetectionResult(
            available=True,
            tool_name="blender",
            version=self.version,
            executable_path=custom_path,
            status="AVAILABLE",
        )


class FakeGodot:
    def __init__(self, version: str = "4.7.2.stable.official.ed1daf0bf") -> None:
        self.version = version

    def detect_engine(self, custom_path: str | None = None) -> EngineDetectionResult:
        return EngineDetectionResult(
            available=True,
            engine_type="godot",
            version=self.version,
            executable_path=custom_path,
            status="AVAILABLE",
        )


def _passing_preflight(**_: Any) -> Any:
    return SimpleNamespace(status="PASS", reason=None, reason_code=None, python_version="3.11.9")


def _probes(**overrides: Any) -> DefaultReadinessProbes:
    options: dict[str, Any] = {
        "blender_adapter": FakeBlender(),
        "godot_adapter": FakeGodot(),
        "dependency_preflight": _passing_preflight,
        "min_free_bytes": 1,
        "blender_python_paths": (),
    }
    options.update(overrides)
    return DefaultReadinessProbes(**options)


def _tool(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"fake executable")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _prepared(
    tmp_path: Path, *, blender: str | None, godot: str | None, probes: Any = None
) -> tuple[Any, Any, str, ConfigurableCostFake, Any]:
    engine, db, workflow_id, fake, handlers = _setup_accounting_env(tmp_path)
    handlers.readiness_probes = probes or _probes()
    handlers.blender_path = blender
    handlers.godot_path = godot
    return engine, db, workflow_id, fake, handlers


def _through_concept(engine: Any, db: Any, workflow_id: str) -> Any:
    concept = engine.run_workflow(workflow_id)
    assert concept.status == WorkflowStatus.BLOCKED
    _decision(engine, db, concept.pending_approval_id or "", approve=True)
    return engine.run_workflow(workflow_id)


def _assert_no_spend(db: Any, workflow_id: str, fake: ConfigurableCostFake) -> None:
    approvals = ApprovalRepository(db).list_by_workflow(workflow_id)
    assert not [a for a in approvals if a.approval_type == "paid_generation"]
    assert fake.invocation_count == 0
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow_id) == []
    assert CostLedgerRepository(db).project_net("asset-test") == 0.0
    report = ProductionReadinessRepository(db).get_active_for_workflow(workflow_id)
    assert report is not None and report.result == "FAIL"


def _failing_checks(db: Any, workflow_id: str) -> set[str]:
    report = ProductionReadinessRepository(db).get_active_for_workflow(workflow_id)
    assert report is not None
    return {c["name"] for c in json.loads(report.report_json)["checks"] if c["status"] == "FAIL"}


def test_all_requirements_available_passes(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    engine, db, workflow_id, fake, _ = _prepared(
        tmp_path,
        blender=str(_tool(tools, "blender.exe")),
        godot=str(_tool(tools, "godot.exe")),
    )
    result = _through_concept(engine, db, workflow_id)

    assert result.status == WorkflowStatus.BLOCKED
    pending = ApprovalRepository(db).get(result.pending_approval_id or "")
    assert pending is not None and pending.approval_type == "paid_generation"
    report = ProductionReadinessRepository(db).get_active_for_workflow(workflow_id)
    assert report is not None and report.result == "PASS"
    assert fake.invocation_count == 0


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("blender_absent", "blender_configured"),
        ("godot_absent", "godot_configured"),
        ("blender_invalid_path", "blender_executable"),
        ("godot_path_is_directory", "godot_executable"),
    ],
)
def test_missing_or_invalid_tools_fail_before_provider(
    tmp_path: Path, case: str, expected: str
) -> None:
    tools = tmp_path / "tools"
    blender: str | None = str(_tool(tools, "blender.exe"))
    godot: str | None = str(_tool(tools, "godot.exe"))
    if case == "blender_absent":
        blender = None
    elif case == "godot_absent":
        godot = None
    elif case == "blender_invalid_path":
        blender = str(tools / "missing-blender.exe")
    elif case == "godot_path_is_directory":
        godot = str(tools)
    engine, db, workflow_id, fake, _ = _prepared(tmp_path, blender=blender, godot=godot)

    result = _through_concept(engine, db, workflow_id)

    assert result.status == WorkflowStatus.FAILED
    assert result.error_code == "PRODUCTION_READINESS_FAILED"
    assert expected in _failing_checks(db, workflow_id)
    _assert_no_spend(db, workflow_id, fake)


def test_v051_class_regression_godot_missing_found_before_spend(tmp_path: Path) -> None:
    """Valid concept and snapshot, Blender available, Godot missing: no credits spent."""
    tools = tmp_path / "tools"
    engine, db, workflow_id, fake, _ = _prepared(
        tmp_path, blender=str(_tool(tools, "blender.exe")), godot=None
    )
    result = _through_concept(engine, db, workflow_id)

    assert result.error_code == "PRODUCTION_READINESS_FAILED"
    failing = _failing_checks(db, workflow_id)
    assert "godot_configured" in failing
    assert not any(name.startswith("blender") for name in failing)
    _assert_no_spend(db, workflow_id, fake)


def test_unsupported_review_view_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gamefactory.core.domain.camera_framing as framing

    monkeypatch.setattr(framing, "PLACED_VIEWS", ("top",))
    tools = tmp_path / "tools"
    engine, db, workflow_id, fake, _ = _prepared(
        tmp_path, blender=str(_tool(tools, "b.exe")), godot=str(_tool(tools, "g.exe"))
    )
    result = _through_concept(engine, db, workflow_id)

    assert result.error_code == "PRODUCTION_READINESS_FAILED"
    assert "profile_review_views_implemented" in _failing_checks(db, workflow_id)
    _assert_no_spend(db, workflow_id, fake)


def test_unsupported_profile_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gamefactory.core.domain.asset_profiles as profiles

    real_registry = profiles.builtin_registry()

    class Unsupported:
        def get(self, profile_id: str, version: int | None = None) -> Any:
            return real_registry.get(profile_id, version)

        def availability(self) -> list[dict[str, str]]:
            return [dict(row, status="UNSUPPORTED") for row in real_registry.availability()]

    tools = tmp_path / "tools"
    engine, db, workflow_id, fake, _ = _prepared(
        tmp_path, blender=str(_tool(tools, "b.exe")), godot=str(_tool(tools, "g.exe"))
    )
    monkeypatch.setattr(profiles, "builtin_registry", lambda: Unsupported())
    result = _through_concept(engine, db, workflow_id)

    assert result.error_code == "PRODUCTION_READINESS_FAILED"
    assert "profile_supported" in _failing_checks(db, workflow_id)
    _assert_no_spend(db, workflow_id, fake)


def test_unwritable_workspace_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file", encoding="utf-8")
    original = DefaultReadinessProbes.workspace_checks

    def blocked(self: DefaultReadinessProbes, root: Path, asset_dir: Path | str) -> Any:
        return original(self, root, blocker / "asset")

    monkeypatch.setattr(DefaultReadinessProbes, "workspace_checks", blocked)
    tools = tmp_path / "tools"
    engine, db, workflow_id, fake, _ = _prepared(
        tmp_path, blender=str(_tool(tools, "b.exe")), godot=str(_tool(tools, "g.exe"))
    )
    result = _through_concept(engine, db, workflow_id)

    assert result.error_code == "PRODUCTION_READINESS_FAILED"
    assert "workspace_writable" in _failing_checks(db, workflow_id)
    _assert_no_spend(db, workflow_id, fake)


def test_unsupported_tool_versions_and_failed_dependency_preflight(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    probes = _probes(
        blender_adapter=FakeBlender("Blender 3.6.0"), godot_adapter=FakeGodot("3.5.2.stable")
    )
    checks = {c.name: c.status for c in probes.blender_checks(str(_tool(tools, "b.exe")))}
    assert checks["blender_version_supported"] == "FAIL"
    assert checks["blender_python_dependencies"] == "FAIL"  # prerequisite failed
    godot = {c.name: c.status for c in probes.godot_checks(str(_tool(tools, "g.exe")))}
    assert godot["godot_version_supported"] == "FAIL"

    failing_preflight = _probes(
        dependency_preflight=lambda **_: SimpleNamespace(
            status="FAIL", reason="numpy missing", reason_code="MODULE_MISSING", python_version=None
        )
    )
    blender = {c.name: c.status for c in failing_preflight.blender_checks(str(tools / "b.exe"))}
    assert blender["blender_python_dependencies"] == "FAIL"


def test_readiness_drift_godot_removed_after_approval_stops_before_post(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    godot = _tool(tools, "godot.exe")
    engine, db, workflow_id, fake, _ = _prepared(
        tmp_path, blender=str(_tool(tools, "blender.exe")), godot=str(godot)
    )
    gate = _through_concept(engine, db, workflow_id)
    approval_id = gate.pending_approval_id or ""
    _decision(engine, db, approval_id, approve=True)

    moved = tools / "godot.moved"
    godot.rename(moved)
    stopped = engine.run_workflow(workflow_id)

    assert stopped.error_code == "PRODUCTION_READINESS_FAILED"
    assert fake.invocation_count == 0
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow_id) == []
    approval = ApprovalRepository(db).get(approval_id)
    assert approval is not None and approval.status == ApprovalStatus.APPROVED
    events = AuditLogRepository(db).list_by_entity("Task", f"{workflow_id}-PAID-GENERATION")
    assert any(event.action == "PRODUCTION_READINESS_RECHECK_FAILED" for event in events)
    assert CostLedgerRepository(db).project_net("asset-test") == 0.0

    moved.rename(godot)
    engine.retry_task(workflow_id, f"{workflow_id}-PAID-GENERATION")

    assert fake.invocation_count == 1
    intents = ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)
    assert len(intents) == 1 and intents[0].approval_id == approval_id


def test_recheck_does_not_block_query_only_recovery(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    godot = _tool(tools, "godot.exe")
    engine, db, workflow_id, fake, _ = _prepared(
        tmp_path, blender=str(_tool(tools, "blender.exe")), godot=str(godot)
    )
    gate = _through_concept(engine, db, workflow_id)
    _decision(engine, db, gate.pending_approval_id or "", approve=True)
    fake.simulate_crash = True
    crashed = engine.run_workflow(workflow_id)
    assert crashed.status == WorkflowStatus.BLOCKED
    assert fake.invocation_count == 1

    godot.unlink()
    fake.simulate_crash = False
    engine.run_workflow(workflow_id)

    assert fake.invocation_count == 1
    intents = ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)
    assert len(intents) == 1 and intents[0].status == "SUCCEEDED"


def test_report_content_is_versioned_and_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "msy_" + "Q7wLm2Rz9Kx4Tb6Vn8Hc3Jd5"
    monkeypatch.setenv("READINESS_TEST_API_KEY", secret)
    tools = tmp_path / "tools"
    engine, db, workflow_id, _, _ = _prepared(
        tmp_path, blender=str(_tool(tools, "b.exe")), godot=str(_tool(tools, "g.exe"))
    )
    _through_concept(engine, db, workflow_id)

    row = ProductionReadinessRepository(db).get_active_for_workflow(workflow_id)
    assert row is not None
    report = json.loads(row.report_json)
    assert report["schema"] == READINESS_SCHEMA
    assert report["paid_request_snapshot_sha256"] == row.snapshot_sha256
    identities = {c["name"]: c["observed"] for c in report["checks"]}
    assert identities["godot_executable"]["size_bytes"] > 0
    assert secret not in row.report_json
    assert os.environ["READINESS_TEST_API_KEY"] not in json.dumps(report)


def test_doctor_capability_summary_uses_the_same_probes(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    (tmp_path / ".gamefactory").mkdir()
    summary = _probes().capability_summary(
        tmp_path, str(_tool(tools, "b.exe")), str(tools / "missing-godot")
    )
    assert summary["provider"]["status"] == "NOT_EVALUATED"
    assert summary["blender"]["status"] == "PASS"
    assert summary["godot"]["status"] == "FAIL"
    assert summary["workspace"]["status"] == "PASS"

    text = _doctor_text({"production_readiness": summary})
    assert "Production Readiness Capabilities" in text
    assert "Godot" in text and "FAIL" in text
