"""Real Blender + Godot UI acceptance for V0.8-9 animation review session overlay."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from contextlib import ExitStack
from importlib import resources
from pathlib import Path

import pytest

import gamefactory
from gamefactory.adapters.assets.v08_candidate_evidence import (
    bind_production_candidate_evidence_exporter,
)
from gamefactory.adapters.assets.v08_candidate_geometry import (
    envelope_size_from_aabb,
    visual_aabb_in_reference_root_frame,
)
from gamefactory.adapters.assets.v08_candidate_rules import decode_candidate_glb
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ExecutionRepository,
    ProviderInvocationRepository,
    TaskRepository,
)
from gamefactory.cli.animation_review_session_bridge import (
    BRIDGE_SCHEMA_VERSION,
    CONTEXT_SCHEMA_VERSION,
    _parse_context_document,
)
from gamefactory.cli.animation_review_session_viewer import (
    prepare_animation_review_session_viewer,
)
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.animation_clip import ANIMATION_CLIP_SCHEMA_VERSION
from gamefactory.core.domain.models import AuditEvent, WorkflowStatus, generate_id
from gamefactory.core.domain.v08_candidate_contracts import (
    load_packaged_candidate_profile,
    parse_asset_specification_v08_candidate,
    profile_document_hash,
)
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner
from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
from gamefactory.workflows.animation_review_handoff import (
    ANIMATION_REVIEW_HANDOFF_REPORT_SCHEMA,
    REVIEW_REPORT_JSON_NAME,
    REVIEW_REPORT_MD_NAME,
    export_animation_review_handoff,
)
from gamefactory.workflows.animation_review_session import AnimationReviewSessionWorkflowContext
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.v08_candidate_animation_clip_preview import (
    _CLIP_PACKAGE_FILES,
    acceptance_arm_wave_clip_raw_bytes,
    animation_clip_preview_current,
    export_rigged_character_animation_clip_preview,
)
from gamefactory.workflows.v08_candidate_animation_review_set import (
    CandidateAnimationReviewSetSource,
    animation_review_set_current,
    export_rigged_character_animation_review_set,
)
from gamefactory.workflows.v08_candidate_currentness import assert_zero_provider_activity
from gamefactory.workflows.v08_candidate_evidence_readiness import (
    assert_candidate_workflow_engine_finalized,
)
from gamefactory.workflows.v08_candidate_preview import (
    candidate_preview_current,
    export_rigged_character_candidate_preview,
)
from gamefactory.workflows.v08_candidate_snapshot import CANDIDATE_TEST_ONLY_APPROVAL
from gamefactory.workflows.v08_candidate_workflow import (
    CandidateWorkflowHandlers,
    create_fresh_v08_candidate_workspace,
    create_v08_candidate_workflow,
    register_v08_candidate_handlers,
)
from gamefactory.workflows.v08_candidate_workspace import (
    CANDIDATE_DB_FILENAME,
    CANDIDATE_STATE_DIR,
)
from tests.helpers.v08_candidate_workflow_failure_diagnostic import (
    assert_workflow_completed_or_diagnose,
    workflow_failure_diagnostics,
)

_FIXTURE_ASSET_ID = "humanoid_review_session_01"
_SESSION_INSPECT_SUBPROCESS_TIMEOUT_SECONDS = 600.0

BLENDER = os.environ.get(
    "GAMEFACTORY_TEST_BLENDER",
    r"C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
)
GODOT = os.environ.get(
    "GAMEFACTORY_TEST_GODOT",
    r"C:\Users\lenovo\devel\godot\Godot_v4.7.2-stable_win64.exe\Godot_v4.7.2-stable_win64_console.exe",
)

_V08_PACKAGE = resources.files("gamefactory.resources.v08_candidate")
_HANDSHAKE = "gf_session_inspect_handshake.json"
_EXTERNAL_DONE = "gf_session_inspect_external_complete.json"
_SESSION_INSPECT_LEAF = "animation_review_session_inspect.gd"
_VIEWER_PREPARED_EXTRA_ROOT_FILES = frozenset(
    {
        "animation_review_session.tscn",
        "animation_review_session_controller.gd",
        "context.json",
        _SESSION_INSPECT_LEAF,
    }
)


def _y_rotation_quaternion_xyzw(degrees: float) -> tuple[float, float, float, float]:
    half = math.radians(degrees) * 0.5
    return (0.0, math.sin(half), 0.0, math.cos(half))


def _authored_arm_reverse_02_clip_raw_bytes() -> bytes:
    identity = [0.0, 0.0, 0.0, 1.0]
    y_neg30 = list(_y_rotation_quaternion_xyzw(-30.0))
    doc = {
        "schema_version": ANIMATION_CLIP_SCHEMA_VERSION,
        "clip_id": "arm_reverse_02",
        "duration_seconds": 2.0,
        "loop": True,
        "tracks": [
            {
                "bone": "Spine",
                "keyframes": [
                    {"time": 0.0, "rotation_xyzw": identity},
                    {"time": 0.75, "rotation_xyzw": y_neg30},
                    {"time": 1.5, "rotation_xyzw": identity},
                ],
            },
        ],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _export_script_path() -> Path:
    return Path(
        str(
            resources.files("gamefactory.resources.blender").joinpath(
                "export_humanoid_12bone_fixture.py"
            )
        )
    )


def _run_blender_export(out: Path) -> None:
    cmd = [BLENDER, "--background", "--python", str(_export_script_path()), "--", str(out)]
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert out.is_file()


def _spec_for_glb(glb: Path):
    decoded = decode_candidate_glb(glb)
    mins, maxs = visual_aabb_in_reference_root_frame(decoded)
    width_m, height_m, depth_m = envelope_size_from_aabb(mins, maxs)
    profile = load_packaged_candidate_profile()
    data = {
        "schema_version": "0.8.0-candidate",
        "asset_id": _FIXTURE_ASSET_ID,
        "category": "character",
        "profile": "rigged_character",
        "profile_version": 1,
        "intent": "CLOSED candidate animation review session UI integration",
        "source_kind": "local_verified_rig",
        "dimensions": {"width_m": width_m, "depth_m": depth_m, "height_m": height_m},
        "orientation": {"up": "+Y", "front": "-Z"},
        "origin_contract": "humanoid_reference_root",
        "rig_contract_id": "humanoid_12bone_v1",
        "visual_mesh_name": "SM_HumanoidSkin",
        "geometry_budget": {
            "max_triangles_lod0": 20000,
            "max_triangles_lod1": 10000,
            "lod_ratio": 0.5,
        },
        "material_budget": {"max_materials": 4},
        "texture_budget": {"max_dimension": 2048},
        "lod_policy": "lod0_only",
        "target_engine": "godot",
        "target_import_path": f"assets/generated/character/{_FIXTURE_ASSET_ID}/",
        "collider": {"policy": "capsule", "capsule": {"radius_m": 0.1, "height_m": 0.5}},
        "profile_document_hash": profile_document_hash(profile.document),
        "processed_glb_sha256": hashlib.sha256(glb.read_bytes()).hexdigest(),
    }
    return parse_asset_specification_v08_candidate(data)


def _review_set_leaf_paths(review_dir: Path) -> list[Path]:
    root_names = {
        "character.glb",
        "character.tscn",
        "candidate.json",
        "collider.json",
        "preview-manifest.json",
        "animation_clip_preview_player.gd",
        "animation_review_controller.gd",
        "animation_review_set_player.gd",
        "animation_review_set_controller.gd",
        "animation_review_set.tscn",
        "animation_review_set_preview.tscn",
        "animation_review_set_manifest.json",
        "project.godot",
    }
    paths = [review_dir / name for name in sorted(root_names)]
    for slot in ("000", "001"):
        for name in ("animation_clip.json", "animation_clip_manifest.json"):
            paths.append(review_dir / "clips" / slot / name)
    return paths


def _godot_harness_strip_main_scene(project_godot: Path) -> None:
    text = project_godot.read_text(encoding="utf-8")
    stripped = re.sub(r"^run/main_scene=.*\n", "", text, flags=re.MULTILINE)
    if stripped != text:
        project_godot.write_text(stripped, encoding="utf-8", newline="\n")


def _gamefactory_import_root() -> Path:
    return Path(gamefactory.__file__).resolve().parent.parent


def _godot_rendered_executable() -> Path:
    configured = Path(GODOT)
    name = configured.name
    if name.lower().endswith("_console.exe"):
        gui = configured.with_name(name[: -len("_console.exe")] + ".exe")
        if gui.is_file():
            return gui
    return configured


def _read_owned_log_tail(path: Path, max_bytes: int = 12_000) -> str:
    if not path.is_file():
        return ""
    payload = path.read_bytes()
    if len(payload) > max_bytes:
        payload = payload[-max_bytes:]
    return payload.decode("utf-8", errors="replace")


def _assert_bridge_subprocess_gamefactory_origin() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import gamefactory; from pathlib import Path; "
            "print(Path(gamefactory.__file__).resolve().parent.parent)",
        ],
        capture_output=True,
        text=True,
        env=_bridge_env(),
        timeout=60,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    child_root = Path(proc.stdout.strip())
    assert child_root == _gamefactory_import_root()


def _terminate_owned_godot_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        ProcessRunner._kill_tree(proc)  # noqa: SLF001
    proc.wait(timeout=60)


def _stage_owned_inspect_script(package_dir: Path) -> None:
    target = package_dir / _SESSION_INSPECT_LEAF
    target.write_bytes(_V08_PACKAGE.joinpath(_SESSION_INSPECT_LEAF).read_bytes())


def _assert_owned_viewer_bytes(
    consumer: Path,
    review_byte_snapshot: dict[str, bytes],
) -> None:
    for rel, payload in review_byte_snapshot.items():
        if rel == "project.godot":
            continue
        assert (consumer / rel).read_bytes() == payload
    review_project = review_byte_snapshot["project.godot"].decode("utf-8")
    stripped_expected = re.sub(r"^run/main_scene=.*\n", "", review_project, flags=re.MULTILINE)
    assert (consumer / "project.godot").read_text(encoding="utf-8") == stripped_expected
    consumer_only = {
        path.relative_to(consumer).as_posix() for path in consumer.rglob("*") if path.is_file()
    }
    review_rels = set(review_byte_snapshot)
    extra = consumer_only - review_rels
    assert extra == set(_VIEWER_PREPARED_EXTRA_ROOT_FILES)


def _write_minimal_inspect_syntax_project(project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    project_dir.joinpath("project.godot").write_text(
        'config_version=5\n\n[application]\nconfig/name="SessionInspectSyntax"\n',
        encoding="utf-8",
        newline="\n",
    )
    (project_dir / "animation_review_session_inspect.gd").write_bytes(
        _V08_PACKAGE.joinpath("animation_review_session_inspect.gd").read_bytes()
    )


def _assert_no_script_errors(combined_output: str) -> None:
    if "SCRIPT ERROR" in combined_output or "ParseError" in combined_output:
        pytest.fail(combined_output[-2500:])
    for line in combined_output.splitlines():
        if re.search(r"\bERROR:\s", line):
            pytest.fail(combined_output[-2500:])


def _bridge_env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(_gamefactory_import_root())}


def _assert_blocked_on_test_only_approval_or_diagnose(
    db: object,
    project_root: Path,
    workflow_id: str,
    result,
) -> str:
    approval_id = result.pending_approval_id
    if approval_id is not None:
        approval = ApprovalRepository(db).get(approval_id)
        if (
            result.status == WorkflowStatus.BLOCKED
            and approval is not None
            and approval.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
        ):
            return approval_id
    diagnostics = workflow_failure_diagnostics(project_root, db, workflow_id, result)
    raise AssertionError(
        "candidate workflow did not reach TEST_ONLY approval gate: "
        f"status={result.status.value} "
        f"pending_approval_id={approval_id!r} "
        f"error_message={result.error_message!r} "
        f"error_code={result.error_code!r} "
        f"failed_tasks={diagnostics['failed_tasks']}"
    )


def _context_document(
    *,
    project_root: Path,
    workflow_id: str,
    preview: Path,
    review_set: Path,
    sources: tuple[CandidateAnimationReviewSetSource, ...],
    session_path: Path,
    exchange_dir: Path,
) -> dict[str, object]:
    clip_packages = [
        {
            "animation_dir": str(source.animation_dir.resolve()),
            "clip_path": str(source.clip_path.resolve()),
        }
        for source in sources
    ]
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "project_root": str(project_root.resolve()),
        "workflow_id": workflow_id,
        "preview_dir": str(preview.resolve()),
        "review_dir": str(review_set.resolve()),
        "clip_packages": clip_packages,
        "session_path": str(session_path.resolve()),
        "exchange_dir": str(exchange_dir.resolve()),
    }


def _invoke_bridge(
    *,
    exchange_dir: Path,
    context_doc: dict[str, object],
    request_doc: dict[str, object],
) -> tuple[int, dict[str, object]]:
    exchange_dir.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(json.dumps(request_doc, sort_keys=True).encode()).hexdigest()[:12]
    context_path = exchange_dir / f"context-{token}.json"
    request_path = exchange_dir / f"request-{token}.json"
    response_path = exchange_dir / f"response-{token}.json"
    bridge_context = {**context_doc, "exchange_dir": str(exchange_dir.resolve())}
    context_path.write_text(
        json.dumps(bridge_context, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    request_path.write_text(
        json.dumps(request_doc, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    if response_path.exists():
        response_path.unlink()
    _assert_bridge_subprocess_gamefactory_origin()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "gamefactory.cli.animation_review_session_bridge",
            "--context-file",
            str(context_path),
            "--request-file",
            str(request_path),
            "--response-file",
            str(response_path),
        ],
        capture_output=True,
        text=True,
        env=_bridge_env(),
        timeout=600,
    )
    if not response_path.is_file():
        return proc.returncode, {}
    return proc.returncode, json.loads(response_path.read_text(encoding="utf-8"))


def _run_godot_import(review_dir: Path, runner: ProcessRunner) -> None:
    import_result = runner.run(
        CommandRequest(
            args=[str(GODOT), "--headless", "--path", str(review_dir), "--import", "--quit"],
            cwd=review_dir,
            timeout_seconds=180.0,
        )
    )
    import_combined = (import_result.stdout or "") + (import_result.stderr or "")
    assert import_result.exit_code == 0, import_combined[-1500:]
    _assert_no_script_errors(import_combined)


def _run_session_inspect(
    package_dir: Path,
    *,
    phase: str,
    result_path: Path,
    context_path: Path,
    exchange_dir: Path,
    runner: ProcessRunner,
    timeout: float = _SESSION_INSPECT_SUBPROCESS_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    _run_godot_import(package_dir, runner)
    user_args = [
        f"--session-python-executable={sys.executable}",
        f"--session-context-file={context_path}",
        f"--session-exchange-dir={exchange_dir}",
        f"--gf-session-inspect-phase={phase}",
        f"--gf-session-inspect-result={result_path}",
    ]
    cmd = [
        str(_godot_rendered_executable()),
        "--rendering-method",
        "gl_compatibility",
        "--audio-driver",
        "Dummy",
        "--path",
        str(package_dir),
        "--script",
        "res://animation_review_session_inspect.gd",
        "--",
        *user_args,
    ]
    return subprocess.run(
        cmd,
        cwd=package_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_bridge_env(),
    )


def _load_inspect_result(result_path: Path) -> dict[str, object]:
    assert result_path.is_file(), "inspect result missing"
    return json.loads(result_path.read_text(encoding="utf-8"))


def _session_document(session_path: Path) -> dict[str, object]:
    return json.loads(session_path.read_text(encoding="utf-8"))


def _clip_record(session_doc: dict[str, object], clip_id: str) -> dict[str, object]:
    records = session_doc.get("clip_records")
    assert isinstance(records, list)
    for entry in records:
        if isinstance(entry, dict) and entry.get("clip_id") == clip_id:
            return entry
    raise AssertionError(f"clip record missing for {clip_id!r}")


def _assert_handoff_module_under_imported_gamefactory() -> None:
    import gamefactory
    import gamefactory.workflows.animation_review_handoff as handoff_module

    package_root = Path(gamefactory.__file__).resolve().parent
    origin = Path(handoff_module.__file__).resolve()
    assert package_root in origin.parents


def _real_ui_handoff_source_digests(
    *,
    preview_dir: Path,
    review_set_dir: Path,
    clip_preview_a: Path,
    clip_preview_b: Path,
    clip_a_path: Path,
    clip_b_path: Path,
    session_path: Path,
) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in _review_set_leaf_paths(review_set_dir):
        rel = path.relative_to(review_set_dir).as_posix()
        digests[f"review/{rel}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(preview_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(preview_dir).as_posix()
            digests[f"preview/{rel}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in sorted(_CLIP_PACKAGE_FILES):
        digests[f"clip-preview-a/{name}"] = hashlib.sha256(
            (clip_preview_a / name).read_bytes()
        ).hexdigest()
        digests[f"clip-preview-b/{name}"] = hashlib.sha256(
            (clip_preview_b / name).read_bytes()
        ).hexdigest()
    digests["authored/arm_wave_01.json"] = hashlib.sha256(clip_a_path.read_bytes()).hexdigest()
    digests["authored/arm_reverse_02.json"] = hashlib.sha256(clip_b_path.read_bytes()).hexdigest()
    digests["session/raw"] = hashlib.sha256(session_path.read_bytes()).hexdigest()
    return digests


def _assert_handoff_report_files(root: Path) -> None:
    names = {path.name for path in root.iterdir()}
    assert names == {REVIEW_REPORT_JSON_NAME, REVIEW_REPORT_MD_NAME}


def _assert_handoff_reports_identical(dir_a: Path, dir_b: Path) -> None:
    _assert_handoff_report_files(dir_a)
    _assert_handoff_report_files(dir_b)
    for name in (REVIEW_REPORT_JSON_NAME, REVIEW_REPORT_MD_NAME):
        assert (dir_a / name).read_bytes() == (dir_b / name).read_bytes()


_MAX_HANDOFF_REPORT_BYTES = 128 * 1024


def _assert_bound_handoff_report(
    report_dir: Path,
    *,
    expected_raw_sha: str,
    expected_revision: int,
    session_current: bool,
    captured_session_document: dict[str, object],
    reverse_note: str = "",
) -> None:
    json_path = report_dir / REVIEW_REPORT_JSON_NAME
    md_path = report_dir / REVIEW_REPORT_MD_NAME
    assert json_path.stat().st_size <= _MAX_HANDOFF_REPORT_BYTES
    assert md_path.stat().st_size <= _MAX_HANDOFF_REPORT_BYTES

    doc = json.loads(json_path.read_text(encoding="utf-8"))
    binding = captured_session_document.get("binding")
    assert isinstance(binding, dict)

    assert doc["schema_version"] == ANIMATION_REVIEW_HANDOFF_REPORT_SCHEMA
    assert doc["review_root"] == binding["review_root"]
    assert doc["raw_manifest_sha256"] == binding["raw_manifest_sha256"]
    assert doc["root_payload_sha256"] == binding["root_payload_sha256"]
    assert doc["clip_payload_sha256"] == binding["clip_payload_sha256"]
    assert doc["raw_session_sha256"] == expected_raw_sha
    assert doc["session_revision"] == expected_revision
    assert doc["session_current"] is session_current
    assert doc["production_eligible"] is False
    assert doc["promotion_eligible"] is False
    assert doc["keep_status_is_annotation_not_approval"] is True
    assert [entry["clip_id"] for entry in doc["clips"]] == [
        "arm_wave_01",
        "arm_reverse_02",
    ]
    wave = doc["clips"][0]
    reverse = doc["clips"][1]
    assert wave["status"] == "revise"
    assert wave["note"] == "review A"
    assert wave["bookmarks"] == [0.75]
    assert wave["duration_seconds"] == 1.5
    assert reverse["status"] == "keep"
    assert reverse["note"] == reverse_note
    assert reverse["bookmarks"] == []
    assert reverse["duration_seconds"] == 2.0

    md = md_path.read_text(encoding="utf-8")
    assert "keep status is annotation only, not approval." in md
    assert "production_eligible: false" in md
    assert "promotion_eligible: false" in md
    assert str(binding["review_root"]) in md
    assert f"raw_session_sha256: {expected_raw_sha}" in md
    assert f"session_revision: {expected_revision}" in md
    assert f"- session_current: {str(session_current).lower()}" in md
    assert f"raw_manifest_sha256: {binding['raw_manifest_sha256']}" in md
    assert f"root_payload_sha256: {binding['root_payload_sha256']}" in md
    assert f"clip_payload_sha256: {binding['clip_payload_sha256']}" in md
    wave_pos = md.find("arm_wave_01")
    reverse_pos = md.find("arm_reverse_02")
    assert wave_pos != -1 and reverse_pos != -1 and wave_pos < reverse_pos
    assert "review A" in md
    assert "- bookmarks: 0.75" in md
    assert "revise" in md
    assert "keep" in md
    if reverse_note:
        assert reverse_note in md
    if session_current:
        assert not md.startswith("# SESSION NOT CURRENT\n")
    else:
        assert md.startswith("# SESSION NOT CURRENT\n")


@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
def test_animation_review_session_inspect_script_check_only(tmp_path: Path) -> None:
    project_dir = tmp_path / "inspect-syntax"
    _write_minimal_inspect_syntax_project(project_dir)
    completed = subprocess.run(
        [
            str(GODOT),
            "--headless",
            "--path",
            str(project_dir),
            "--script",
            "res://animation_review_session_inspect.gd",
            "--check-only",
        ],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=180,
    )
    combined = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined[-2000:]
    _assert_no_script_errors(combined)


@pytest.mark.skipif(not Path(BLENDER).is_file(), reason="Blender executable not available")
@pytest.mark.skipif(not Path(GODOT).is_file(), reason="Godot executable not available")
@pytest.mark.real_godot
def test_real_animation_review_session_ui_lifecycle(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    root, db = workspace.root, workspace.db
    glb = root / "canonical.glb"
    _run_blender_export(glb)
    spec = _spec_for_glb(glb)
    workflow, tasks = create_v08_candidate_workflow(workspace, glb, spec)
    handlers = CandidateWorkflowHandlers(
        root,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ExecutionRepository(db),
        TaskRepository(db),
        ArtifactManager(root),
        godot_path=GODOT,
        runner=ProcessRunner(sanitize_output=True),
    )
    bind_production_candidate_evidence_exporter(handlers)
    engine = WorkflowEngine(
        root,
        db,
        policy_engine=PolicyEngine(
            PolicyRule(require_approval_for_paid=True, require_approval_for_process_execution=False)
        ),
        asset_provider=None,
    )
    register_v08_candidate_handlers(engine.handler_registry, handlers)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)

    result = engine.run_workflow(workflow.id)
    for _ in range(40):
        if result.status == WorkflowStatus.BLOCKED and result.pending_approval_id:
            approval = ApprovalRepository(db).get(result.pending_approval_id)
            if approval and approval.approval_type == CANDIDATE_TEST_ONLY_APPROVAL:
                break
        if result.status in (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED):
            break
        result = engine.run_workflow(workflow.id)

    pending_approval_id = _assert_blocked_on_test_only_approval_or_diagnose(
        db, root, workflow.id, result
    )
    approval = ApprovalRepository(db).get(pending_approval_id)
    assert approval is not None
    task = TaskRepository(db).get(approval.task_id)
    wf = engine.wf_repo.get(workflow.id)
    assert task is not None and wf is not None
    decided = ApprovalService.approve(
        approval,
        "integration-animation-review-session-actor",
        current_inputs=engine.approval_inputs(wf, task),
    )
    ApprovalRepository(db).decide_if_pending(
        decided,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=decided.id,
            action="APPROVED",
            actor="integration-animation-review-session-actor",
            previous_state="PENDING",
            new_state="APPROVED",
        ),
    )
    after = engine.run_workflow(workflow.id)
    for _ in range(8):
        if after.status == WorkflowStatus.COMPLETED:
            break
        after = engine.run_workflow(workflow.id)
    assert_workflow_completed_or_diagnose(db, root, workflow.id, after)
    assert_zero_provider_activity(db, workflow.id)
    assert ProviderInvocationRepository(db).count(workflow.id) == 0
    assert_candidate_workflow_engine_finalized(handlers, workflow.id)

    preview_dir = tmp_path / "candidate-preview-out"
    export_rigged_character_candidate_preview(handlers, workflow.id, preview_dir)
    clip_a_path = tmp_path / "arm_wave_01.json"
    clip_b_path = tmp_path / "arm_reverse_02.json"
    clip_a_path.write_bytes(acceptance_arm_wave_clip_raw_bytes())
    clip_b_path.write_bytes(_authored_arm_reverse_02_clip_raw_bytes())
    clip_b_before = clip_b_path.read_bytes()
    clip_preview_a = tmp_path / "candidate-clip-preview-a"
    clip_preview_b = tmp_path / "candidate-clip-preview-b"
    export_rigged_character_animation_clip_preview(
        handlers, workflow.id, preview_dir, clip_a_path, clip_preview_a
    )
    export_rigged_character_animation_clip_preview(
        handlers, workflow.id, preview_dir, clip_b_path, clip_preview_b
    )
    v086_a_before = {
        name: (clip_preview_a / name).read_bytes() for name in sorted(_CLIP_PACKAGE_FILES)
    }
    v086_b_before = {
        name: (clip_preview_b / name).read_bytes() for name in sorted(_CLIP_PACKAGE_FILES)
    }
    sources = (
        CandidateAnimationReviewSetSource(clip_preview_a, clip_a_path),
        CandidateAnimationReviewSetSource(clip_preview_b, clip_b_path),
    )
    review_set_dir = tmp_path / "candidate-animation-review-set-out"
    review_result = export_rigged_character_animation_review_set(
        handlers,
        workflow.id,
        preview_dir,
        sources,
        review_set_dir,
    )
    assert review_result.production_eligible is False
    assert review_result.promotion_eligible is False
    review_byte_snapshot = {
        path.relative_to(review_set_dir).as_posix(): path.read_bytes()
        for path in _review_set_leaf_paths(review_set_dir)
    }

    session_path = tmp_path / "session-sidecar" / "review_session.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    exchange_dir = tmp_path / "ui-session-exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    context_doc = _context_document(
        project_root=root,
        workflow_id=workflow.id,
        preview=preview_dir,
        review_set=review_set_dir,
        sources=sources,
        session_path=session_path,
        exchange_dir=exchange_dir,
    )
    bridge_context = _parse_context_document(context_doc)
    prepared = prepare_animation_review_session_viewer(
        bridge_context,
        tmp_path / "session-ui-consumer",
        python_executable=Path(sys.executable),
    )
    consumer = prepared.overlay_dir
    _godot_harness_strip_main_scene(consumer / "project.godot")
    _stage_owned_inspect_script(consumer)
    _assert_owned_viewer_bytes(consumer, review_byte_snapshot)
    context_path = prepared.context_file
    viewer_exchange_dir = prepared.exchange_dir
    runner = ProcessRunner(sanitize_output=True)

    write_result_path = tmp_path / "inspect-write.json"
    write_proc = _run_session_inspect(
        consumer,
        phase="write",
        result_path=write_result_path,
        context_path=context_path,
        exchange_dir=viewer_exchange_dir,
        runner=runner,
    )
    write_combined = write_proc.stdout + write_proc.stderr
    assert write_proc.returncode == 0, write_combined[-3000:]
    _assert_no_script_errors(write_combined)
    assert "PASS: animation_review_session_write" in write_proc.stdout
    write_payload = _load_inspect_result(write_result_path)
    assert write_payload.get("ok") is True
    write_snap = write_payload.get("snapshot")
    assert isinstance(write_snap, dict)
    assert write_snap.get("stored_revision") == 4
    session_after_write = _session_document(session_path)
    clip_a_after_write = _clip_record(session_after_write, "arm_wave_01")
    assert clip_a_after_write.get("note") == "review A"
    assert clip_a_after_write.get("status") == "revise"
    assert clip_a_after_write.get("bookmarks") == [0.75]
    clip_b_after_write = _clip_record(session_after_write, "arm_reverse_02")
    assert clip_b_after_write.get("status") == "keep"
    session_sha_after_write = hashlib.sha256(session_path.read_bytes()).hexdigest()

    reopen_result_path = tmp_path / "inspect-reopen.json"
    reopen_proc = _run_session_inspect(
        consumer,
        phase="reopen",
        result_path=reopen_result_path,
        context_path=context_path,
        exchange_dir=viewer_exchange_dir,
        runner=runner,
    )
    reopen_combined = reopen_proc.stdout + reopen_proc.stderr
    assert reopen_proc.returncode == 0, reopen_combined[-3000:]
    _assert_no_script_errors(reopen_combined)
    reopen_payload = _load_inspect_result(reopen_result_path)
    assert reopen_payload.get("ok") is True
    assert abs(float(reopen_payload.get("playback_position", -1.0)) - 0.75) < 0.0001
    reopen_snap = reopen_payload.get("snapshot")
    assert isinstance(reopen_snap, dict)
    assert reopen_snap.get("stored_revision") == 4
    session_sha_after_reopen = hashlib.sha256(session_path.read_bytes()).hexdigest()
    assert session_sha_after_reopen == session_sha_after_write
    session_after_reopen = _session_document(session_path)
    clip_b_after_reopen = _clip_record(session_after_reopen, "arm_reverse_02")
    assert clip_b_after_reopen.get("status") == "keep"

    _assert_handoff_module_under_imported_gamefactory()
    handoff_ctx = AnimationReviewSessionWorkflowContext(
        handlers=handlers,
        workflow_id=workflow.id,
        preview_dir=preview_dir,
        clip_packages=sources,
        review_dir=review_set_dir,
        session_path=session_path,
    )
    handoff_source_before = _real_ui_handoff_source_digests(
        preview_dir=preview_dir,
        review_set_dir=review_set_dir,
        clip_preview_a=clip_preview_a,
        clip_preview_b=clip_preview_b,
        clip_a_path=clip_a_path,
        clip_b_path=clip_b_path,
        session_path=session_path,
    )
    handoff_root_a = (tmp_path / "real-ui-handoff-a").resolve()
    handoff_root_b = (tmp_path / "real-ui-handoff-b").resolve()
    handoff_result_a = export_animation_review_handoff(handoff_ctx, handoff_root_a)
    handoff_result_b = export_animation_review_handoff(handoff_ctx, handoff_root_b)
    assert handoff_result_a.production_eligible is False
    assert handoff_result_a.promotion_eligible is False
    assert handoff_result_a.session_current is True
    assert handoff_result_a.session_revision == 4
    assert handoff_result_a.raw_session_sha256 == session_sha_after_reopen
    assert handoff_result_b.session_current is True
    assert handoff_result_b.raw_session_sha256 == session_sha_after_reopen
    _assert_handoff_reports_identical(handoff_root_a, handoff_root_b)
    _assert_bound_handoff_report(
        handoff_root_a,
        expected_raw_sha=session_sha_after_reopen,
        expected_revision=4,
        session_current=True,
        captured_session_document=session_after_reopen,
    )
    assert (
        _real_ui_handoff_source_digests(
            preview_dir=preview_dir,
            review_set_dir=review_set_dir,
            clip_preview_a=clip_preview_a,
            clip_preview_b=clip_preview_b,
            clip_a_path=clip_a_path,
            clip_b_path=clip_b_path,
            session_path=session_path,
        )
        == handoff_source_before
    )

    conflict_result_path = tmp_path / "inspect-conflict.json"
    handshake_path = viewer_exchange_dir / _HANDSHAKE
    done_path = viewer_exchange_dir / _EXTERNAL_DONE
    session_sha_after_conflict = ""
    session_bytes_after_conflict = b""
    conflict_proc: subprocess.Popen[str] | None = None
    if handshake_path.exists():
        handshake_path.unlink()
    if done_path.exists():
        done_path.unlink()
    conflict_log_dir = tmp_path / "inspect-conflict-logs"
    conflict_log_dir.mkdir(parents=True, exist_ok=True)
    conflict_stdout_log = conflict_log_dir / "godot.stdout.log"
    conflict_stderr_log = conflict_log_dir / "godot.stderr.log"
    conflict_cmd = [
        str(_godot_rendered_executable()),
        "--rendering-method",
        "gl_compatibility",
        "--audio-driver",
        "Dummy",
        "--path",
        str(consumer),
        "--script",
        "res://animation_review_session_inspect.gd",
        "--",
        f"--session-python-executable={sys.executable}",
        f"--session-context-file={context_path}",
        f"--session-exchange-dir={viewer_exchange_dir}",
        "--gf-session-inspect-phase=conflict",
        f"--gf-session-inspect-result={conflict_result_path}",
    ]
    _assert_bridge_subprocess_gamefactory_origin()
    _log_stack = ExitStack()
    stdout_handle = _log_stack.enter_context(conflict_stdout_log.open("w", encoding="utf-8"))
    stderr_handle = _log_stack.enter_context(conflict_stderr_log.open("w", encoding="utf-8"))
    try:
        conflict_proc = subprocess.Popen(
            conflict_cmd,
            cwd=consumer,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            env=_bridge_env(),
            start_new_session=(sys.platform != "win32"),
        )
    except OSError:
        _log_stack.close()
        raise
    _log_stack.pop_all()
    external_committed_sha = ""
    external_committed_bytes = b""
    try:
        deadline = time.monotonic() + 600.0
        external_exchange = tmp_path / "external-bridge-exchange"
        external_exchange.mkdir(parents=True, exist_ok=True)
        while conflict_proc.poll() is None and time.monotonic() < deadline:
            if handshake_path.is_file():
                handshake = json.loads(handshake_path.read_text(encoding="utf-8"))
                expected_sha = handshake["raw_sha256"]
                assert expected_sha == session_sha_after_reopen
                code, response = _invoke_bridge(
                    exchange_dir=external_exchange,
                    context_doc=context_doc,
                    request_doc={
                        "schema_version": BRIDGE_SCHEMA_VERSION,
                        "action": "update",
                        "expected_raw_sha256": expected_sha,
                        "operation": {
                            "op": "SetNote",
                            "clip_id": "arm_reverse_02",
                            "note": "external B note",
                        },
                    },
                )
                assert code == 0 and response.get("ok") is True
                stored = response.get("stored")
                assert isinstance(stored, dict)
                external_committed_sha = str(stored.get("raw_sha256", ""))
                assert len(external_committed_sha) == 64
                external_committed_bytes = session_path.read_bytes()
                assert (
                    hashlib.sha256(external_committed_bytes).hexdigest() == external_committed_sha
                )
                done_path.write_text("ok", encoding="utf-8")
                break
            time.sleep(0.05)
        else:
            stdout_handle.close()
            stderr_handle.close()
            _terminate_owned_godot_process(conflict_proc)
            conflict_combined = _read_owned_log_tail(conflict_stdout_log) + _read_owned_log_tail(
                conflict_stderr_log
            )
            pytest.fail(
                "conflict harness timed out waiting for external handshake: "
                f"handshake_exists={handshake_path.is_file()} "
                f"done_exists={done_path.is_file()} "
                f"result_exists={conflict_result_path.is_file()} "
                f"godot={_godot_rendered_executable()} "
                f"logs={conflict_log_dir} "
                f"{conflict_combined[-2500:]}"
            )
        stdout_handle.close()
        stderr_handle.close()
        conflict_proc.wait(timeout=60)
        conflict_combined = _read_owned_log_tail(conflict_stdout_log) + _read_owned_log_tail(
            conflict_stderr_log
        )
        assert conflict_proc.returncode == 0, conflict_combined[-3000:]
        _assert_no_script_errors(conflict_combined)
        conflict_payload = _load_inspect_result(conflict_result_path)
        assert conflict_payload.get("ok") is True
        assert conflict_payload.get("cached_sha") == session_sha_after_reopen
        conflict_snap = conflict_payload.get("snapshot")
        assert isinstance(conflict_snap, dict)
        assert conflict_snap.get("reload_required") is True
        assert conflict_snap.get("conflict_active") is True
        assert conflict_snap.get("raw_sha256") == session_sha_after_reopen
        session_sha_after_conflict = hashlib.sha256(session_path.read_bytes()).hexdigest()
        assert session_sha_after_conflict == external_committed_sha
        session_bytes_after_conflict = session_path.read_bytes()
        assert session_bytes_after_conflict == external_committed_bytes
        session_after_conflict = _session_document(session_path)
        assert session_after_conflict.get("revision") == 5
        clip_a_after_conflict = _clip_record(session_after_conflict, "arm_wave_01")
        assert clip_a_after_conflict == clip_a_after_write
        clip_b_after_conflict = _clip_record(session_after_conflict, "arm_reverse_02")
        assert clip_b_after_conflict.get("status") == "keep"
        assert clip_b_after_conflict.get("note") == "external B note"
        assert session_after_conflict.get("production_eligible") is False
        assert session_after_conflict.get("promotion_eligible") is False
    finally:
        if not stdout_handle.closed:
            stdout_handle.close()
        if not stderr_handle.closed:
            stderr_handle.close()
        if conflict_proc is not None:
            _terminate_owned_godot_process(conflict_proc)

    drift_doc = json.loads(clip_b_before.decode("utf-8"))
    drift_doc["duration_seconds"] = 2.25
    drift_bytes = json.dumps(
        drift_doc, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    clip_b_path.write_bytes(drift_bytes)
    handoff_source_at_drift = _real_ui_handoff_source_digests(
        preview_dir=preview_dir,
        review_set_dir=review_set_dir,
        clip_preview_a=clip_preview_a,
        clip_preview_b=clip_preview_b,
        clip_a_path=clip_a_path,
        clip_b_path=clip_b_path,
        session_path=session_path,
    )
    try:
        handoff_hist_root = (tmp_path / "real-ui-handoff-hist").resolve()
        handoff_hist = export_animation_review_handoff(handoff_ctx, handoff_hist_root)
        assert handoff_hist.production_eligible is False
        assert handoff_hist.promotion_eligible is False
        assert handoff_hist.session_current is False
        assert handoff_hist.session_revision == 5
        assert handoff_hist.raw_session_sha256 == session_sha_after_conflict
        _assert_bound_handoff_report(
            handoff_hist_root,
            expected_raw_sha=session_sha_after_conflict,
            expected_revision=5,
            session_current=False,
            captured_session_document=session_after_conflict,
            reverse_note="external B note",
        )
        assert (
            _real_ui_handoff_source_digests(
                preview_dir=preview_dir,
                review_set_dir=review_set_dir,
                clip_preview_a=clip_preview_a,
                clip_preview_b=clip_preview_b,
                clip_a_path=clip_a_path,
                clip_b_path=clip_b_path,
                session_path=session_path,
            )
            == handoff_source_at_drift
        )

        stale_result_path = tmp_path / "inspect-stale.json"
        stale_proc = _run_session_inspect(
            consumer,
            phase="stale",
            result_path=stale_result_path,
            context_path=context_path,
            exchange_dir=viewer_exchange_dir,
            runner=runner,
        )
        stale_combined = stale_proc.stdout + stale_proc.stderr
        assert stale_proc.returncode == 0, stale_combined[-3000:]
        stale_payload = _load_inspect_result(stale_result_path)
        assert stale_payload.get("ok") is True
        assert stale_payload.get("session_sha") == session_sha_after_conflict
        stale_snap = stale_payload.get("snapshot")
        assert isinstance(stale_snap, dict)
        assert stale_snap.get("authority_current") is False
        assert stale_snap.get("mutations_enabled") is False
        assert hashlib.sha256(session_path.read_bytes()).hexdigest() == session_sha_after_conflict
        assert session_path.read_bytes() == session_bytes_after_conflict

        clip_b_path.write_bytes(clip_b_before)
        assert (
            animation_review_set_current(
                handlers, workflow.id, preview_dir, sources, review_set_dir
            )
            is True
        )

        candidate_db = root / CANDIDATE_STATE_DIR / CANDIDATE_DB_FILENAME
        db_backup = tmp_path / "candidate-factory.db.offline-backup"
        candidate_db.rename(db_backup)
        try:
            offline_result_path = tmp_path / "inspect-db-offline.json"
            offline_proc = _run_session_inspect(
                consumer,
                phase="db_offline",
                result_path=offline_result_path,
                context_path=context_path,
                exchange_dir=viewer_exchange_dir,
                runner=runner,
            )
            offline_combined = offline_proc.stdout + offline_proc.stderr
            assert offline_proc.returncode == 0, offline_combined[-3000:]
            offline_payload = _load_inspect_result(offline_result_path)
            assert offline_payload.get("ok") is True
            assert offline_payload.get("session_sha") == session_sha_after_conflict
            offline_snap = offline_payload.get("snapshot")
            assert isinstance(offline_snap, dict)
            assert offline_snap.get("authority_current") is False
            assert offline_snap.get("mutations_enabled") is False
            assert offline_snap.get("stored_revision") == 5
            assert offline_snap.get("raw_sha256") == session_sha_after_conflict
            assert (
                hashlib.sha256(session_path.read_bytes()).hexdigest() == session_sha_after_conflict
            )
            assert session_path.read_bytes() == session_bytes_after_conflict
        finally:
            if db_backup.is_file():
                db_backup.rename(candidate_db)
    finally:
        clip_b_path.write_bytes(clip_b_before)

    assert session_bytes_after_conflict
    assert session_sha_after_conflict
    recovered_result_path = tmp_path / "inspect-recovered.json"
    recovered_proc = _run_session_inspect(
        consumer,
        phase="recovered",
        result_path=recovered_result_path,
        context_path=context_path,
        exchange_dir=viewer_exchange_dir,
        runner=runner,
    )
    recovered_combined = recovered_proc.stdout + recovered_proc.stderr
    assert recovered_proc.returncode == 0, recovered_combined[-3000:]
    _assert_no_script_errors(recovered_combined)
    assert "PASS: animation_review_session_recovered" in recovered_proc.stdout
    recovered_payload = _load_inspect_result(recovered_result_path)
    assert recovered_payload.get("ok") is True
    assert recovered_payload.get("recovered_sha") == session_sha_after_conflict
    recovered_snap = recovered_payload.get("snapshot")
    assert isinstance(recovered_snap, dict)
    assert recovered_snap.get("authority_current") is True
    assert recovered_snap.get("mutations_enabled") is True
    assert recovered_snap.get("stored_revision") == 5
    assert recovered_snap.get("raw_sha256") == session_sha_after_conflict
    assert session_path.read_bytes() == session_bytes_after_conflict

    for rel, payload in review_byte_snapshot.items():
        assert (review_set_dir / rel).read_bytes() == payload
    for name, payload in v086_a_before.items():
        assert (clip_preview_a / name).read_bytes() == payload
    for name, payload in v086_b_before.items():
        assert (clip_preview_b / name).read_bytes() == payload
    assert clip_b_path.read_bytes() == clip_b_before
    assert (
        animation_review_set_current(handlers, workflow.id, preview_dir, sources, review_set_dir)
        is True
    )
    assert (
        animation_clip_preview_current(
            handlers, workflow.id, preview_dir, clip_a_path, clip_preview_a
        )
        is True
    )
    assert (
        animation_clip_preview_current(
            handlers, workflow.id, preview_dir, clip_b_path, clip_preview_b
        )
        is True
    )
    assert candidate_preview_current(handlers, workflow.id, preview_dir) is True
