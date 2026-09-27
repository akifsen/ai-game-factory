import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify_closeout_evidence import EvidenceError, _safe_path, build, verify


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    data = path.read_bytes()
    return {
        "path": path.relative_to(path.parents[1]).as_posix(),
        "role": "",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _bundle(root):
    records = root / "records"
    identity = _write_json(
        root / "metadata" / "source-identity.json", {"head": "abc", "dirty": True}
    )
    identity.update(path="metadata/source-identity.json", role="source_identity")
    recovery = _write_json(records / "recovery.json", {"workflow_id": "WF-1"})
    recovery.update(path="records/recovery.json", role="recovery")
    rows = [identity, recovery]
    observation = {"execution_id": "EXEC-1", "scenario_id": "SCEN-1", "completion": "COMPLETED"}
    validation = {"status": "PASS", "findings": [{"status": "PASS"}]}
    obs_path = records / "observation.json"
    val_path = records / "validation.json"
    obs_path.write_text(json.dumps(observation), encoding="utf-8")
    val_path.write_text(json.dumps(validation), encoding="utf-8")
    obs_hash = hashlib.sha256(obs_path.read_bytes()).hexdigest()
    val_hash = hashlib.sha256(val_path.read_bytes()).hexdigest()
    inspection = {
        "workflow": {"id": "WF-1"},
        "tasks": [
            {
                "id": "EXEC-TASK",
                "workflow_id": "WF-1",
                "task_type": "godot_execute",
                "depends_on": [],
            },
            {
                "id": "VALIDATE-TASK",
                "workflow_id": "WF-1",
                "task_type": "godot_validate",
                "depends_on": ["EXEC-TASK"],
            },
        ],
        "executions": [{"id": "EXEC-1", "task_id": "EXEC-TASK"}],
        "artifacts": [
            {
                "workflow_id": "WF-1",
                "task_id": "EXEC-TASK",
                "artifact_type": "godot-runtime-observation",
                "content_hash": obs_hash,
            },
            {
                "workflow_id": "WF-1",
                "task_id": "VALIDATE-TASK",
                "artifact_type": "godot-validation-report",
                "content_hash": val_hash,
            },
        ],
    }
    for role, doc in [
        ("observation", observation),
        ("validation", validation),
        ("inspect", inspection),
    ]:
        row = _write_json(records / f"{role}.json", doc)
        row.update(path=f"records/{role}.json", role=role)
        rows.append(row)
    acceptance_doc = {
        "status": "PASSED",
        "started_at": "2026-01-01T00:00:00Z",
        "checks": {
            "artifact_documents": {
                "WF-1": [
                    {
                        "artifact": {
                            "workflow_id": "WF-1",
                            "task_id": "EXEC-TASK",
                            "artifact_type": "godot-runtime-observation",
                            "content_hash": obs_hash,
                        }
                    },
                    {
                        "artifact": {
                            "workflow_id": "WF-1",
                            "task_id": "VALIDATE-TASK",
                            "artifact_type": "godot-validation-report",
                            "content_hash": val_hash,
                        }
                    },
                ]
            }
        },
        "commands": [{"json": inspection}],
    }
    acceptance = _write_json(records / "acceptance.json", acceptance_doc)
    acceptance.update(path="records/acceptance.json", role="acceptance")
    rows.insert(1, acceptance)
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "relationships": {
                    "workflow_id": "WF-1",
                    "execution_id": "EXEC-1",
                    "scenario_id": "SCEN-1",
                    "execution_task_id": "EXEC-TASK",
                    "validation_task_id": "VALIDATE-TASK",
                },
                "files": rows,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_valid_bundle_verifies_cold(tmp_path):
    root = _bundle(tmp_path / "bundle")
    verifier = Path(__file__).parents[2] / "scripts" / "verify_closeout_evidence.py"
    copied = root / "verify_closeout_evidence.py"
    shutil.copyfile(verifier, copied)
    data = copied.read_bytes()
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["files"].append(
        {
            "path": copied.name,
            "role": "verifier",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )
    (root / "manifest.json").write_text(json.dumps(manifest))
    # Run isolated from both the checkout and its import/site configuration.
    outside = tmp_path / "empty-cold-directory"
    outside.mkdir()
    result = subprocess.run(
        [sys.executable, "-I", str(copied), "verify", str(root)],
        cwd=outside,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_builder_copies_only_catalogued_files_and_rejects_nonempty_output(tmp_path):
    source_bundle = _bundle(tmp_path / "source")
    catalog = {
        "source_identity": str(source_bundle / "metadata/source-identity.json"),
        "acceptance": str(source_bundle / "records/acceptance.json"),
        "recovery": str(source_bundle / "records/recovery.json"),
        "relationships": {
            "workflow_id": "WF-1",
            "execution_id": "EXEC-1",
            "scenario_id": "SCEN-1",
            "execution_task_id": "EXEC-TASK",
            "validation_task_id": "VALIDATE-TASK",
        },
        "files": [
            {
                "source": str(source_bundle / f"records/{role}.json"),
                "path": f"records/{role}.json",
                "role": role,
            }
            for role in ("observation", "validation", "inspect")
        ],
    }
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    target = tmp_path / "portable"
    build(catalog_path, target)
    verify(target)
    assert (target / "verify_closeout_evidence.py").is_file()
    assert not (target / "records/unlisted.json").exists()
    with pytest.raises(EvidenceError, match="must be empty"):
        build(catalog_path, target)


def test_missing_mandatory_observation_is_rejected(tmp_path):
    root = _bundle(tmp_path / "bundle")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["files"] = [row for row in manifest["files"] if row["role"] != "observation"]
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(EvidenceError, match="roles"):
        verify(root)


def test_modified_log_is_rejected_by_hash(tmp_path):
    root = _bundle(tmp_path / "bundle")
    with (root / "records/inspect.json").open("a") as stream:
        stream.write("tampered")
    with pytest.raises(EvidenceError, match="hash"):
        verify(root)


def test_mixed_execution_fails_relationship_even_with_updated_hash(tmp_path):
    root = _bundle(tmp_path / "bundle")
    path = root / "records/validation.json"
    doc = json.loads(path.read_text())
    doc["findings"].append({"assertion_id": "different-run", "status": "FAIL"})
    data = (json.dumps(doc) + "\n").encode()
    path.write_bytes(data)
    manifest = json.loads((root / "manifest.json").read_text())
    row = next(row for row in manifest["files"] if row["role"] == "validation")
    row["size"] = len(data)
    row["sha256"] = hashlib.sha256(data).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(EvidenceError, match="link raw validation"):
        verify(root)


@pytest.mark.parametrize(
    "path", ["../outside", "C:/outside", "a//b", "a/./b", "a/../b", "a/file:stream", "a\\b"]
)
def test_unsafe_bundle_paths_rejected(path):
    with pytest.raises(EvidenceError):
        _safe_path(path)


def _capture_bundle(root):
    root = _bundle(root)
    png = root / "images" / "cp-000.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    png_hash = hashlib.sha256(png.read_bytes()).hexdigest()
    validation = {
        "status": "PASS",
        "execution_id": "EXEC-1",
        "images": [{"id": "cp-000", "sha256": png_hash}],
    }
    observation = {"execution_id": "EXEC-1", "scenario_id": "SCEN-1", "completion": "COMPLETED"}
    val_path = root / "records" / "validation.json"
    obs_path = root / "records" / "observation.json"
    val_path.write_text(json.dumps(validation), encoding="utf-8")
    obs_path.write_text(json.dumps(observation), encoding="utf-8")
    val_hash = hashlib.sha256(val_path.read_bytes()).hexdigest()
    obs_hash = hashlib.sha256(obs_path.read_bytes()).hexdigest()
    inspection = {
        "workflow": {"id": "WF-1"},
        "tasks": [
            {
                "id": "EXEC-TASK",
                "workflow_id": "WF-1",
                "task_type": "godot_capture_execute",
                "depends_on": [],
            },
            {
                "id": "VALIDATE-TASK",
                "workflow_id": "WF-1",
                "task_type": "godot_capture_validate",
                "depends_on": ["EXEC-TASK"],
            },
        ],
        "executions": [{"id": "EXEC-1", "task_id": "EXEC-TASK"}],
        "artifacts": [
            {
                "workflow_id": "WF-1",
                "task_id": "EXEC-TASK",
                "artifact_type": "godot-runtime-observation",
                "content_hash": obs_hash,
            },
            {
                "workflow_id": "WF-1",
                "task_id": "VALIDATE-TASK",
                "artifact_type": "godot-capture-validation",
                "content_hash": val_hash,
            },
        ],
    }
    (root / "records" / "inspect.json").write_text(json.dumps(inspection), encoding="utf-8")
    acceptance = {
        "status": "PASSED",
        "started_at": "2026-01-01T00:00:00Z",
        "checks": {
            "artifact_documents": {
                "WF-1": [
                    {
                        "artifact": {
                            "workflow_id": "WF-1",
                            "task_id": "EXEC-TASK",
                            "artifact_type": "godot-runtime-observation",
                            "content_hash": obs_hash,
                        }
                    },
                    {
                        "artifact": {
                            "workflow_id": "WF-1",
                            "task_id": "VALIDATE-TASK",
                            "artifact_type": "godot-capture-validation",
                            "content_hash": val_hash,
                        }
                    },
                ]
            }
        },
        "commands": [{"json": inspection}],
    }
    (root / "records" / "acceptance.json").write_text(json.dumps(acceptance), encoding="utf-8")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for row in manifest["files"]:
        path = root / row["path"]
        data = path.read_bytes()
        row["size"] = len(data)
        row["sha256"] = hashlib.sha256(data).hexdigest()
    image_row = {
        "path": "images/cp-000.png",
        "role": "image",
        "size": png.stat().st_size,
        "sha256": png_hash,
    }
    manifest["files"].append(image_row)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_capture_bundle_binds_images_and_rejects_broken_evidence(tmp_path):
    root = _capture_bundle(tmp_path / "capture")
    verify(root)

    missing = _capture_bundle(tmp_path / "missing-png")
    (missing / "images" / "cp-000.png").unlink()
    with pytest.raises(EvidenceError, match="missing"):
        verify(missing)

    modified = _capture_bundle(tmp_path / "modified")
    image = modified / "images" / "cp-000.png"
    image.write_bytes(image.read_bytes() + b"x")
    with pytest.raises(EvidenceError, match="hash"):
        verify(modified)

    wrong = _capture_bundle(tmp_path / "wrong-execution")
    manifest = json.loads((wrong / "manifest.json").read_text(encoding="utf-8"))
    manifest["relationships"]["execution_id"] = "EXEC-OTHER"
    (wrong / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(EvidenceError, match="identity mismatch"):
        verify(wrong)
