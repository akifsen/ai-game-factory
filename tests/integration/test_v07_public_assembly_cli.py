"""Public CLI acceptance for the three explicitly available V0.7 assemblies."""

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
import test_vehicle_profile_runtime as vehicle_fixture
import test_weapon_aircraft_profiles_runtime as weapon_aircraft_fixture
from PIL import Image

import gamefactory
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.engines.godot import GodotAdapter
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    ExecutionRepository,
    TaskRepository,
)
from gamefactory.core.domain.models import Execution, ExecutionStatus, generate_id, utc_now_iso

ROOT = Path(__file__).resolve().parents[2]
PARENT_PACKAGE_PATH = Path(gamefactory.__file__).resolve()
ISOLATED_PARENT = sys.flags.isolated == 1


def _configured_executable(
    environment_keys: tuple[str, ...], detected: str | None, windows_fallback: str
) -> Path | None:
    for key in environment_keys:
        value = os.environ.get(key)
        if value:
            return Path(value).expanduser()
    if detected:
        return Path(detected).expanduser()
    if os.name == "nt":
        fallback = Path(windows_fallback)
        if fallback.is_file():
            return fallback
    return None


_blender_result = BlenderAdapter().detect_tool()
BLENDER = _configured_executable(
    ("GAMEFACTORY_TEST_BLENDER", "GAMEFACTORY_BLENDER_PATH"),
    _blender_result.executable_path if _blender_result.available else None,
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.EXE",
)
GODOT = _configured_executable(
    ("GAMEFACTORY_TEST_GODOT", "GAMEFACTORY_GODOT_PATH"),
    GodotAdapter().find_candidate_executable(),
    r"C:\Users\lenovo\devel\godot\Godot_v4.7.2-stable_win64.exe\Godot_v4.7.2-stable_win64_console.exe",
)
SPEC_RESOURCES = {
    "vehicle": "resources/specs/armored_vehicle_test.yml",
    "weapon": "resources/specs/weapon_test.yml",
    "aircraft": "resources/specs/aircraft_test.yml",
}


def _child_environment() -> dict[str, str]:
    env = dict(os.environ)
    if ISOLATED_PARENT:
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
    else:
        src = str(ROOT / "src")
        env["PYTHONPATH"] = os.pathsep.join(
            [src, env["PYTHONPATH"]] if env.get("PYTHONPATH") else [src]
        )
    env["GAMEFACTORY_TEST_BLENDER"] = str(BLENDER)
    env["GAMEFACTORY_TEST_GODOT"] = str(GODOT)
    return env


def _python_prefix() -> list[str]:
    return [sys.executable, *(["-I"] if ISOLATED_PARENT else [])]


def _assert_child_package_provenance(project: Path) -> None:
    result = subprocess.run(
        [*_python_prefix(), "-c", "import gamefactory; print(gamefactory.__file__)"],
        cwd=project,
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    child_path = Path(result.stdout.strip()).resolve()
    assert child_path == PARENT_PACKAGE_PATH, (child_path, PARENT_PACKAGE_PATH)
    if ISOLATED_PARENT:
        with pytest.raises(ValueError):
            child_path.relative_to((ROOT / "src").resolve())


def _cli(project: Path, *args: str) -> tuple[int, Any]:
    result = subprocess.run(
        [
            *_python_prefix(),
            "-m",
            "gamefactory",
            "--json",
            "--project",
            str(project),
            *args,
        ],
        cwd=project,
        env=_child_environment(),
        capture_output=True,
        text=True,
        timeout=240,
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


def _fixture_spec(name: str) -> tuple[Any, Any]:
    if name == "vehicle":
        return vehicle_fixture._load_profile_spec()
    return weapon_aircraft_fixture._load_candidate(name)


def _write_source(name: str, path: Path, spec: Any) -> None:
    if name == "vehicle":
        vehicle_fixture._write_box_assembly(path, spec.asset_id)
    else:
        weapon_aircraft_fixture._write_authored_source(path, name, spec)


def _database_bytes(project: Path) -> str:
    path = project / ".gamefactory" / "state" / "factory.db"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _paid_state(project: Path) -> dict[str, int]:
    db_path = project / ".gamefactory" / "state" / "factory.db"
    tables = (
        "provider_invocations",
        "provider_operation_intents",
        "cost_ledger",
        "paid_request_snapshots",
        "production_readiness_reports",
    )
    with sqlite3.connect(db_path) as conn:
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        counts["nonlocal_approvals"] = int(
            conn.execute("SELECT COUNT(*) FROM approvals WHERE cost_class != 'LOCAL'").fetchone()[0]
        )
    return counts


@pytest.mark.real_godot
@pytest.mark.parametrize("name", ("vehicle", "weapon", "aircraft"))
def test_public_cli_completes_and_readonly_revalidates_v07_assembly(
    name: str, tmp_path: Path
) -> None:
    if BLENDER is None or GODOT is None:
        pytest.skip("Requires the approved Blender 5.2 and paired Godot 4.7.2 executables")
    if not BLENDER.is_file() or not GODOT.is_file():
        pytest.fail(f"Configured Blender/Godot executable is missing: {BLENDER}, {GODOT}")

    # Keep generated Blender staging files below Windows' legacy MAX_PATH
    # limit even when pytest uses a deeply nested .verification basetemp.
    # Each parametrized invocation already has its own tmp_path directory.
    project = tmp_path / "p"
    project.mkdir()
    _assert_child_package_provenance(project)
    init_code, init_payload = _cli(project, "init")
    assert init_code == 0, init_payload
    profiles_code, profiles = _cli(project, "asset", "profiles", "--contract-version", "0.7.0")
    assert profiles_code == 0
    assert isinstance(profiles, dict) and isinstance(profiles.get("asset_profiles"), list)
    profile_rows = profiles["asset_profiles"]
    assert [row["qualified"] for row in profile_rows if row["status"] == "AVAILABLE"] == [
        "vehicle@1",
        "weapon@1",
        "aircraft@1",
        "character@1",
    ]
    assert any(
        row["profile_id"] == "rigged_character" and row["status"] == "UNSUPPORTED"
        for row in profile_rows
    )

    spec_resource = SPEC_RESOURCES[name]
    spec_path = project / "specs" / f"{name}.yml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        files("gamefactory").joinpath(spec_resource).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _profile, spec = _fixture_spec(name)

    concept = project / "concepts" / f"{name}.png"
    concept.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (62, 81, 92)).save(concept, format="PNG")
    concept_provenance = project / "concepts" / f"{name}-provenance.json"
    concept_provenance.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "origin": "isolated-public-cli-fixture",
                "profile_id": name,
            }
        ),
        encoding="utf-8",
    )
    source = project / "authored" / f"{name}.glb"
    source.parent.mkdir(parents=True, exist_ok=True)
    _write_source(name, source, spec)
    original_source = source.read_bytes()

    common_tools = ("--blender-path", str(BLENDER), "--godot-path", str(GODOT))
    ingest_code, ingested = _cli(
        project,
        "assembly",
        "ingest",
        "--spec",
        f"specs/{name}.yml",
        "--source",
        f"authored/{name}.glb",
        "--package",
        f"sources/{name}/r001",
        "--actor",
        f"isolated-public-cli-{name}-author",
        "--reason",
        "Test-only local authored source fixture",
        "--authoring-tool",
        "Blender",
        "--authoring-tool-version",
        "5.2.1",
        "--source-front=-Z",
        *common_tools,
    )
    assert ingest_code == 0 and ingested["status"] == "INGESTED", ingested
    assert ingested["paid_provider_invocations"] == 0

    verify_code, verified = _cli(
        project,
        "assembly",
        "verify",
        "--spec",
        f"specs/{name}.yml",
        "--package",
        f"sources/{name}/r001",
        "--expected-provenance-sha256",
        ingested["provenance_sha256"],
        *common_tools,
    )
    assert verify_code == 0 and verified["status"] == "VERIFIED", verified

    workflow_id = f"public-cli-{name}-assembly"
    create_code, result = _cli(
        project,
        "assembly",
        "create",
        "--spec",
        f"specs/{name}.yml",
        "--package",
        f"sources/{name}/r001",
        "--expected-provenance-sha256",
        ingested["provenance_sha256"],
        "--concept",
        f"concepts/{name}.png",
        "--concept-provenance",
        f"concepts/{name}-provenance.json",
        "--workflow-id",
        workflow_id,
        *common_tools,
    )
    assert create_code == 3 and result["status"] == "BLOCKED", result
    expected_approvals = ("concept_review", "source_review", "final_visual_review")
    for index, approval_type in enumerate(expected_approvals):
        assert result.get("pending_approval_id"), result
        list_code, pending = _cli(project, "approvals", "--workflow", workflow_id)
        assert list_code == 0 and len(pending) == 1, pending
        approval = pending[0]
        assert approval["id"] == result["pending_approval_id"]
        assert approval["approval_type"] == approval_type
        assert approval["cost_class"] == "LOCAL"

        approve_code, decision = _cli(
            project,
            "approve",
            approval["id"],
            "--actor",
            f"isolated-public-cli-{name}-reviewer",
            "--comment",
            f"Explicit test-only {approval_type} approval",
            *common_tools,
        )
        assert approve_code == 0 and decision["status"] == "APPROVED", decision
        advance_code, result = _cli(project, "assembly", "export", workflow_id, *common_tools)
        if index + 1 < len(expected_approvals):
            assert advance_code == 3 and result["status"] == "BLOCKED", result
        else:
            assert advance_code == 0 and result["status"] == "COMPLETED", result
            assert result["evidence_manifest"]

    assert source.read_bytes() == original_source
    verification = result.get("evidence_verification")
    if verification is None:
        # The completing invocation exports and cold-checks the evidence before
        # returning the completed workflow result.
        verification_code, result = _cli(project, "assembly", "export", workflow_id, *common_tools)
        assert verification_code == 0 and result["status"] == "COMPLETED", result
        verification = result.get("evidence_verification")
    assert verification and verification["status"] == "PASS", verification
    assert verification["product_ready"] is True
    assert _paid_state(project) == {
        "provider_invocations": 0,
        "provider_operation_intents": 0,
        "cost_ledger": 0,
        "paid_request_snapshots": 0,
        "production_readiness_reports": 0,
        "nonlocal_approvals": 0,
    }

    # A repeat completed export is a read-only validation of the same evidence.
    db_before_repeat = _database_bytes(project)
    repeat_code, repeated = _cli(project, "assembly", "export", workflow_id, *common_tools)
    assert repeat_code == 0 and repeated["evidence_verification"]["status"] == "PASS", repeated
    assert _database_bytes(project) == db_before_repeat

    db = Database(project / ".gamefactory" / "state" / "factory.db")
    artifact = next(
        row
        for row in ArtifactRepository(db).list_by_workflow(workflow_id)
        if row.artifact_type == "processed-assembly-glb"
    )
    processed_path = project / artifact.relative_path
    processed_bytes = processed_path.read_bytes()
    processed_path.write_bytes(processed_bytes + b"\x00")
    db_before_tamper_check = _database_bytes(project)
    tamper_code, _ = _cli(project, "assembly", "export", workflow_id, *common_tools)
    assert tamper_code != 0
    assert _database_bytes(project) == db_before_tamper_check
    processed_path.write_bytes(processed_bytes)

    # Seed a newer failed attempt to prove completed export checks current
    # attempt history instead of falling back to the older successful run.
    task = next(
        item
        for item in TaskRepository(db).list_by_workflow(workflow_id)
        if item.task_type == "asset_v07_assembly_godot"
    )
    executions = ExecutionRepository(db)
    latest = executions.get_latest_attempt(task.id)
    assert latest is not None and latest.status == ExecutionStatus.COMPLETED
    executions.save(
        Execution(
            id=generate_id("TEST-FAILED-ATTEMPT"),
            task_id=task.id,
            attempt_number=latest.attempt_number + 1,
            status=ExecutionStatus.FAILED,
            completed_at=utc_now_iso(),
            error_message="test-only newer failed attempt",
        )
    )
    db_before_latest_failure_check = _database_bytes(project)
    latest_code, _ = _cli(project, "assembly", "export", workflow_id, *common_tools)
    assert latest_code != 0
    assert _database_bytes(project) == db_before_latest_failure_check
    assert _paid_state(project) == {
        "provider_invocations": 0,
        "provider_operation_intents": 0,
        "cost_ledger": 0,
        "paid_request_snapshots": 0,
        "production_readiness_reports": 0,
        "nonlocal_approvals": 0,
    }
