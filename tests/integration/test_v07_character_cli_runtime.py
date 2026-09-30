"""Public CLI acceptance for the packaged nine-view V0.7 character workflow."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image

import gamefactory
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.engines.godot import GodotAdapter
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    CostLedgerRepository,
    ExecutionRepository,
    ProductionReadinessRepository,
    ProviderInvocationRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
)
from gamefactory.core.domain.asset_profiles import AssetProfileV07, parse_profile_document_v07
from gamefactory.core.domain.models import Execution, ExecutionStatus, generate_id, utc_now_iso

ROOT = Path(__file__).resolve().parents[2]
PARENT_PACKAGE_PATH = Path(gamefactory.__file__).resolve()
PARENT_PROFILE_PATH = Path(
    str(files("gamefactory").joinpath("resources/profiles/character.yml"))
).resolve()
ISOLATED_PARENT = sys.flags.isolated == 1
PROFILE_RESOURCE = "resources/profiles/character.yml"
SPEC_RESOURCE = "resources/specs/character_test.yml"


def _configured_executable(environment_keys: tuple[str, ...], detected: str | None) -> Path | None:
    for key in environment_keys:
        value = os.environ.get(key)
        if value:
            return Path(value).expanduser()
    return Path(detected).expanduser() if detected else None


_blender_detection = BlenderAdapter().detect_tool()
BLENDER = _configured_executable(
    ("GAMEFACTORY_TEST_BLENDER", "GAMEFACTORY_BLENDER_PATH"),
    _blender_detection.executable_path if _blender_detection.available else None,
)
GODOT = _configured_executable(
    ("GAMEFACTORY_TEST_GODOT", "GAMEFACTORY_GODOT_PATH"),
    GodotAdapter().find_candidate_executable(),
)


def _child_environment() -> dict[str, str]:
    env = dict(os.environ)
    if ISOLATED_PARENT:
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
    else:
        source_path = str(ROOT / "src")
        env["PYTHONPATH"] = os.pathsep.join(
            [source_path, env["PYTHONPATH"]] if env.get("PYTHONPATH") else [source_path]
        )
    if BLENDER is not None:
        env["GAMEFACTORY_TEST_BLENDER"] = str(BLENDER)
    if GODOT is not None:
        env["GAMEFACTORY_TEST_GODOT"] = str(GODOT)
    return env


def _python_prefix() -> list[str]:
    return [sys.executable, *(["-I"] if ISOLATED_PARENT else [])]


def _assert_child_package_provenance(project: Path) -> None:
    script = (
        "import json; from pathlib import Path; import gamefactory; "
        "from importlib.resources import files; "
        "print(json.dumps({'module': str(Path(gamefactory.__file__).resolve()), "
        "'profile': str(Path(files('gamefactory').joinpath('resources/profiles/character.yml')).resolve()), "
        "'spec': str(Path(files('gamefactory').joinpath('resources/specs/character_test.yml')).resolve())}))"
    )
    result = subprocess.run(
        [*_python_prefix(), "-c", script],
        cwd=project,
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    provenance = json.loads(result.stdout)
    assert Path(provenance["module"]).resolve() == PARENT_PACKAGE_PATH
    assert Path(provenance["profile"]).resolve() == PARENT_PROFILE_PATH
    assert (
        Path(provenance["spec"]).resolve()
        == Path(str(files("gamefactory").joinpath(SPEC_RESOURCE))).resolve()
    )
    if ISOLATED_PARENT:
        with pytest.raises(ValueError):
            Path(provenance["module"]).resolve().relative_to((ROOT / "src").resolve())
        assert "PYTHONPATH" not in _child_environment()


def _cli(project: Path, *arguments: str) -> tuple[int, Any]:
    result = subprocess.run(
        [
            *_python_prefix(),
            "-m",
            "gamefactory",
            "--json",
            "--project",
            str(project),
            *arguments,
        ],
        cwd=project,
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    output = result.stdout.strip() or result.stderr.strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"CLI returned non-JSON output ({result.returncode}):\n{result.stdout}\n{result.stderr}"
        ) from exc
    assert isinstance(payload, (dict, list)), payload
    return result.returncode, payload


def _common_tools() -> tuple[str, ...]:
    assert BLENDER is not None and GODOT is not None
    return "--blender-path", str(BLENDER), "--godot-path", str(GODOT)


def _managed_snapshot(project: Path) -> dict[str, str]:
    managed = project / ".gamefactory"
    transient_sqlite_paths = {"state/factory.db-wal", "state/factory.db-shm"}
    return {
        path.relative_to(managed).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(managed.rglob("*"))
        if path.is_file()
        # SQLite may create these transient shared-memory sidecars while reading
        # a live WAL database. The database file and all project artifacts remain
        # in the snapshot and must stay byte-for-byte unchanged.
        and path.relative_to(managed).as_posix() not in transient_sqlite_paths
    }


def _database_digest(project: Path) -> str:
    database = project / ".gamefactory/state/factory.db"
    return hashlib.sha256(database.read_bytes()).hexdigest()


def _assert_readonly_revalidation(project: Path, workflow_id: str) -> None:
    before_db = _database_digest(project)
    before_files = _managed_snapshot(project)
    for arguments in (("report", "--workflow", workflow_id), ("asset", "export", workflow_id)):
        code, result = _cli(project, *arguments)
        assert code == 0 and result["status"] == "COMPLETED", result
        verification = result["evidence_verification"]
        assert verification["status"] == "PASS" and verification["product_ready"] is True
        assert verification["human_identity_authenticated"] is False
        assert verification["provider_execution_authenticated"] is False
        assert verification["runtime_origin_authenticated"] is False
        assert verification["capture_origin_authenticated"] is False
        assert result["workflow_mutated"] is False
        assert result["verification_as_of"]
        assert result["evidence_manifest_artifact_id"]
        assert result["evidence_manifest_sha256"]
    assert _database_digest(project) == before_db
    assert _managed_snapshot(project) == before_files


def _assert_readonly_failure(project: Path, workflow_id: str) -> None:
    before_db = _database_digest(project)
    before_files = _managed_snapshot(project)
    for arguments in (("report", "--workflow", workflow_id), ("asset", "export", workflow_id)):
        code, result = _cli(project, *arguments)
        assert code != 0, result
        assert "error" in result or result.get("status") != "COMPLETED", result
        assert _database_digest(project) == before_db
        assert _managed_snapshot(project) == before_files


def _profile_hash() -> tuple[AssetProfileV07, str]:
    resource = files("gamefactory").joinpath(PROFILE_RESOURCE)
    profile = AssetProfileV07(parse_profile_document_v07(resource.read_text(encoding="utf-8")))
    canonical = json.dumps(
        profile.document.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return profile, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _seed_profile_pin_mismatch(project: Path, workflow_id: str) -> str:
    path = project / ".gamefactory/state/factory.db"
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT id, parameters_json FROM tasks WHERE workflow_id = ?", (workflow_id,)
        ).fetchall()
        assert rows
        original = str(rows[0][1])
        for task_id, parameters_json in rows:
            parameters = json.loads(parameters_json)
            assert parameters["profile_id"] == "character"
            parameters["profile_document_hash"] = "0" * 64
            conn.execute(
                "UPDATE tasks SET parameters_json = ? WHERE id = ?",
                (json.dumps(parameters, sort_keys=True, separators=(",", ":")), task_id),
            )
        conn.commit()
    finally:
        conn.close()
    return original


@pytest.mark.real_godot
def test_public_cli_completes_and_readonly_revalidates_nine_view_character(
    tmp_path: Path,
) -> None:
    if BLENDER is None or GODOT is None:
        pytest.skip("Requires configured Blender and Godot executables")
    if not BLENDER.is_file() or not GODOT.is_file():
        pytest.fail(f"Configured Blender/Godot executable is missing: {BLENDER}, {GODOT}")

    # The short root keeps the real DCC's generated report/staging paths within MAX_PATH.
    project = tmp_path / "p"
    project.mkdir()
    _assert_child_package_provenance(project)
    init_code, init_result = _cli(project, "init")
    assert init_code == 0, init_result

    profile, expected_profile_hash = _profile_hash()
    profile_rows_code, profile_rows = _cli(
        project, "asset", "profiles", "--contract-version", "0.7.0"
    )
    assert profile_rows_code == 0, profile_rows
    available = {
        row["qualified"] for row in profile_rows["asset_profiles"] if row["status"] == "AVAILABLE"
    }
    assert "character@1" in available, profile_rows

    spec_dir = project / "specs"
    concept_dir = project / "concepts"
    spec_dir.mkdir()
    concept_dir.mkdir()
    spec_path = spec_dir / "character.yml"
    spec_path.write_text(
        files("gamefactory").joinpath(SPEC_RESOURCE).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    concept = concept_dir / "character.png"
    Image.new("RGB", (64, 64), (62, 81, 92)).save(concept, format="PNG")
    provenance = concept_dir / "character-provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "origin": "isolated-public-cli-character-fixture",
                "sha256": hashlib.sha256(concept.read_bytes()).hexdigest(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    config_path = project / ".gamefactory/factory.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config.setdefault("policies", {}).update({"project_budget": 5.0, "max_operation_cost": 5.0})
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    tools = _common_tools()
    create_code, state = _cli(
        project,
        "asset",
        "create",
        "--spec",
        "specs/character.yml",
        "--concept",
        "concepts/character.png",
        "--provenance",
        "concepts/character-provenance.json",
        "--provider",
        "fake",
        *tools,
    )
    assert create_code == 3 and state["status"] == "BLOCKED", state
    workflow_id = state["workflow_id"]
    expected_gates = ("concept_review", "paid_generation", "final_visual_review")
    for index, expected_type in enumerate(expected_gates):
        listed_code, approvals = _cli(project, "approvals", "--workflow", workflow_id)
        assert listed_code == 0 and len(approvals) == 1, approvals
        approval = approvals[0]
        assert approval["approval_type"] == expected_type, approval
        assert approval["cost_class"] == ("PAID" if expected_type == "paid_generation" else "LOCAL")
        approved_code, decision = _cli(
            project,
            "approve",
            approval["id"],
            "--actor",
            "isolated-public-cli-character-fixture-reviewer",
            "--comment",
            f"Explicit test-only {expected_type} approval",
            *tools,
        )
        assert approved_code == 0 and decision["status"] == "APPROVED", decision
        advance_code, state = _cli(project, "resume", workflow_id, *tools)
        if index < len(expected_gates) - 1:
            assert advance_code == 3 and state["status"] == "BLOCKED", state
        else:
            assert advance_code == 0 and state["status"] == "COMPLETED", state

    db = Database(project / ".gamefactory/state/factory.db")
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    assert len(tasks) == 10
    assert all(task.parameters["graph_version"] == "0.7.0" for task in tasks)
    assert all(task.parameters["profile_id"] == "character" for task in tasks)
    assert all(task.parameters["profile_version"] == 1 for task in tasks)
    assert all(task.parameters["profile_document_hash"] == expected_profile_hash for task in tasks)
    prepare = next(task for task in tasks if task.task_type == "asset_prepare")
    specification = prepare.parameters["specification"]
    assert specification["schema_version"] == "0.7.0"
    assert specification["category"] == "character"
    assert specification["source_kind"] == "provider_generated"
    assert specification["profile"] == "character" and specification["profile_version"] == 1
    assert prepare.parameters["profile_document"]["review_views"] == list(profile.review_views)

    godot_task = next(task for task in tasks if task.task_type == "asset_godot")
    godot_attempt = ExecutionRepository(db).get_latest_attempt(godot_task.id)
    assert godot_attempt is not None and godot_attempt.status == ExecutionStatus.COMPLETED
    artifacts = ArtifactRepository(db).list_by_workflow(workflow_id)
    captures = [
        row
        for row in artifacts
        if row.task_id == godot_task.id and row.artifact_type == "asset-runtime-capture"
    ]
    assert len(captures) == 9
    view_for_row = {
        row.id: Path(row.relative_path).stem.removeprefix(f"{godot_attempt.id}-")
        for row in captures
    }
    captured_views = set(view_for_row.values())
    assert captured_views == set(profile.review_views)
    capture_hashes = {
        view_for_row[row.id]: hashlib.sha256((project / row.relative_path).read_bytes()).hexdigest()
        for row in captures
    }
    assert capture_hashes == {view_for_row[row.id]: row.content_hash for row in captures}

    output_code, completed = _cli(project, "asset", "export", workflow_id, *tools)
    assert output_code == 0 and completed["status"] == "COMPLETED", completed
    manifest_path = Path(completed["evidence_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["profile_id"] == "character"
    assert manifest["profile_version"] == 1
    assert manifest["profile_document_sha256"] == expected_profile_hash
    assert manifest["review_views"] == list(profile.review_views)
    assert manifest["spec_sha256"] == prepare.parameters["specification_hash"]
    assert manifest["capture_sha256"] == capture_hashes
    packaged_capture_views = {
        item["view"] for item in manifest["files"] if item["role"] == "runtime_capture"
    }
    assert packaged_capture_views == set(profile.review_views)
    assert manifest["product_ready"] is True and manifest["paid"] is True

    invocations = ProviderInvocationRepository(db)
    # Fake-provider calls are represented by the durable paid intent and execution;
    # unlike external provider contacts, they do not create provider_invocations rows.
    assert invocations.count(workflow_id) == 0
    intents = ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)
    assert len(intents) == 1
    intent = intents[0]
    assert intent.status == "SUCCEEDED"
    assert intent.actual_cost == 5.0 and intent.cost_unit == "fake_credits"
    paid_task = next(task for task in tasks if task.task_type == "asset_paid_generation")
    paid_attempts = ExecutionRepository(db).list_by_task(paid_task.id)
    assert len(paid_attempts) == 1
    assert paid_attempts[0].status == ExecutionStatus.COMPLETED
    assert paid_attempts[0].cost == 5.0
    account = CostLedgerRepository(db).operation_account(paid_task.id)
    assert account.held == 0.0 and account.settled_total == 5.0
    assert account.net == 5.0 and account.cost_unit == "fake_credits"
    readiness = ProductionReadinessRepository(db).get_active_for_workflow(workflow_id)
    assert readiness is not None and readiness.result == "PASS"

    _assert_readonly_revalidation(project, workflow_id)

    # Each adversarial fixture mutation is made before the snapshot; the public
    # report/export calls must reject it without changing the database or files.
    capture_path = project / captures[0].relative_path
    capture_original = capture_path.read_bytes()
    capture_path.unlink()
    _assert_readonly_failure(project, workflow_id)
    capture_path.write_bytes(capture_original)

    capture_path.write_bytes(capture_original + b"tamper")
    _assert_readonly_failure(project, workflow_id)
    capture_path.write_bytes(capture_original)

    original_parameters = _seed_profile_pin_mismatch(project, workflow_id)
    _assert_readonly_failure(project, workflow_id)
    conn = sqlite3.connect(project / ".gamefactory/state/factory.db")
    try:
        conn.execute(
            "UPDATE tasks SET parameters_json = ? WHERE workflow_id = ?",
            (original_parameters, workflow_id),
        )
        conn.commit()
    finally:
        conn.close()

    godot_execution = ExecutionRepository(db).get_latest_attempt(godot_task.id)
    assert godot_execution is not None and godot_execution.status == ExecutionStatus.COMPLETED
    ExecutionRepository(db).save(
        Execution(
            id=generate_id("TEST-CHARACTER-FAILED-GODOT"),
            task_id=godot_task.id,
            attempt_number=godot_execution.attempt_number + 1,
            status=ExecutionStatus.FAILED,
            completed_at=utc_now_iso(),
            error_message="test-only newer failed Godot attempt",
        )
    )
    _assert_readonly_failure(project, workflow_id)
