#!/usr/bin/env python3
"""Build and cold-verify a bounded, portable V0.2 closeout evidence directory.

Catalog format: {"source_identity": <path>, "acceptance": <path>,
"recovery": <path>, "relationships": {"workflow_id": ..., "task_id": ...,
"execution_id": ..., "scenario_id": ...}, "files": [{"source": <path>,
"path": <safe relative destination>, "role": "observation|validation|inspect|
stdout|stderr|launch|terminal|other"}]}. Paths are explicit; no directory
recursion is performed. The bundle is integrity-checked, not signed or attested.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

MANIFEST = "manifest.json"
REQUIRED_ROLES = {"observation", "validation", "inspect"}
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
REQUIRED_RELATIONSHIPS = {
    "workflow_id",
    "execution_id",
    "scenario_id",
    "execution_task_id",
    "validation_task_id",
}


class EvidenceError(ValueError):
    pass


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _safe_path(value: str) -> PurePosixPath:
    p = PurePosixPath(value)
    parts = value.split("/")
    if (
        not value
        or p.is_absolute()
        or value.startswith("//")
        or "\\" in value
        or re.match(r"^[A-Za-z]:", value)
        or any(
            part in ("", ".", "..") or ":" in part or part.endswith((".", " ")) for part in parts
        )
    ):
        raise EvidenceError(f"unsafe bundle path: {value!r}")
    return p


def _is_link(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    try:
        return bool(path.stat(follow_symlinks=False).st_file_attributes & 0x400)
    except (OSError, AttributeError):
        return False


def _assert_no_linked_ancestors(path: Path) -> None:
    absolute = path.absolute()
    chain = [absolute, *absolute.parents]
    for component in reversed(chain):
        if component.exists() and _is_link(component):
            raise EvidenceError(f"linked or reparse-point path component: {component}")


def _reject_linked_components(root: Path, rel: PurePosixPath) -> Path:
    current = root
    for part in rel.parts:
        current = current / part
        if _is_link(current):
            raise EvidenceError(f"linked bundle path component: {rel}")
    try:
        current.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise EvidenceError(f"bundle path escapes root: {rel}") from exc
    return current


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"invalid JSON {path}: {exc}") from exc


def _walk_records(obj: object):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_records(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_records(value)


def _check_relationships(bundle: Path, files: list[dict], expected: dict) -> None:
    by_role: dict[str, list[tuple[object, bytes]]] = {}
    for item in files:
        role = item["role"]
        if role in ("observation", "validation", "inspect", "acceptance"):
            data = (bundle / item["path"]).read_bytes()
            by_role.setdefault(role, []).append((_read_json(bundle / item["path"]), data))
    missing = REQUIRED_ROLES - by_role.keys()
    if missing:
        raise EvidenceError("missing required evidence roles: " + ", ".join(sorted(missing)))
    if not isinstance(expected, dict) or not REQUIRED_RELATIONSHIPS.issubset(expected):
        raise EvidenceError(
            "relationships must declare workflow, execution, scenario, and execute/validate task IDs"
        )
    for key, value in expected.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise EvidenceError(f"invalid relationship identity {key!r}")
    obs_docs = [doc for doc, _ in by_role["observation"]]
    val_docs = [doc for doc, _ in by_role["validation"]]
    inspect_docs = [doc for doc, _ in by_role["inspect"]]
    acceptance_docs = [doc for doc, _ in by_role.get("acceptance", [])]
    if len(obs_docs) != 1 or len(val_docs) != 1:
        raise EvidenceError(
            "exactly one raw observation and one raw validation report are required"
        )
    obs, val = obs_docs[0], val_docs[0]
    if (
        not isinstance(obs, dict)
        or obs.get("execution_id") != expected["execution_id"]
        or obs.get("scenario_id") != expected["scenario_id"]
    ):
        raise EvidenceError("observation execution/scenario identity mismatch")
    if (
        not isinstance(val, dict)
        or val.get("status") not in ("PASS", "FAIL")
        or not isinstance(val.get("findings"), list)
    ):
        raise EvidenceError("validation report does not match the raw V0.2 report schema")
    if len(inspect_docs) != 1 or not isinstance(inspect_docs[0], dict):
        raise EvidenceError("exactly one actual inspect record is required")
    inspect = inspect_docs[0]
    if len(acceptance_docs) != 1 or not isinstance(acceptance_docs[0], dict):
        raise EvidenceError("exactly one acceptance run record is required")
    acceptance = acceptance_docs[0]
    if acceptance.get("status") != "PASSED" or not isinstance(acceptance.get("started_at"), str):
        raise EvidenceError("acceptance run must be a completed passing run with a start identity")
    artifact_documents = (acceptance.get("checks") or {}).get("artifact_documents")
    accepted_rows = (
        artifact_documents.get(expected["workflow_id"])
        if isinstance(artifact_documents, dict)
        else None
    )
    if not isinstance(accepted_rows, list):
        raise EvidenceError("acceptance run does not contain selected workflow artifact records")
    # The inspect result must be a raw JSON result captured by this acceptance
    # command log; this binds the graph to this run rather than manifest labels.
    command_records = acceptance.get("commands")
    if not isinstance(command_records, list) or not any(
        isinstance(command, dict) and command.get("json") == inspect for command in command_records
    ):
        raise EvidenceError("inspect graph is not present in the acceptance run command records")
    workflow = inspect.get("workflow")
    tasks, executions, artifacts = (
        inspect.get("tasks"),
        inspect.get("executions"),
        inspect.get("artifacts"),
    )
    if not isinstance(workflow, dict) or workflow.get("id") != expected["workflow_id"]:
        raise EvidenceError("inspect workflow identity mismatch")
    if not all(isinstance(x, list) for x in (tasks, executions, artifacts)):
        raise EvidenceError("inspect record must contain actual tasks, executions, and artifacts")
    task_by_id = {t.get("id"): t for t in tasks if isinstance(t, dict)}
    execute_task = task_by_id.get(expected["execution_task_id"])
    validation_task = task_by_id.get(expected["validation_task_id"])
    if (
        not execute_task
        or execute_task.get("task_type") != "godot_execute"
        or execute_task.get("workflow_id") != expected["workflow_id"]
    ):
        raise EvidenceError("inspect does not associate the selected execution task with workflow")
    if (
        not validation_task
        or validation_task.get("task_type") != "godot_validate"
        or validation_task.get("workflow_id") != expected["workflow_id"]
    ):
        raise EvidenceError("inspect does not associate the selected validation task with workflow")
    if expected["execution_task_id"] not in validation_task.get("depends_on", []):
        raise EvidenceError("validation task is not dependent on the selected execution task")
    if not any(
        isinstance(row, dict)
        and row.get("id") == expected["execution_id"]
        and row.get("task_id") == expected["execution_task_id"]
        for row in executions
    ):
        raise EvidenceError("inspect execution record does not link execution to its task")
    hashes = {
        role: hashlib.sha256(data).hexdigest() for role, rows in by_role.items() for _, data in rows
    }
    for role, artifact_type, task_id in (
        ("observation", "godot-runtime-observation", expected["execution_task_id"]),
        ("validation", "godot-validation-report", expected["validation_task_id"]),
    ):
        matches = [
            row
            for row in artifacts
            if isinstance(row, dict)
            and row.get("workflow_id") == expected["workflow_id"]
            and row.get("task_id") == task_id
            and row.get("artifact_type") == artifact_type
            and row.get("content_hash") == hashes[role]
        ]
        if not matches:
            raise EvidenceError(
                f"inspect artifact record does not link raw {role} bytes to selected workflow/task"
            )
        expected_hash = hashes[role]
        accepted_match = any(
            isinstance(row, dict)
            and isinstance(row.get("artifact"), dict)
            and row["artifact"].get("workflow_id") == expected["workflow_id"]
            and row["artifact"].get("task_id") == task_id
            and row["artifact"].get("artifact_type") == artifact_type
            and row["artifact"].get("content_hash") == expected_hash
            for row in accepted_rows
        )
        if not accepted_match:
            raise EvidenceError(
                f"acceptance run does not bind raw {role} bytes to selected workflow/task"
            )
    if "run_id" in expected:
        candidates = set()
        for document in [*obs_docs, *val_docs, *inspect_docs]:
            for record in _walk_records(document):
                if isinstance(record.get("run_id"), str):
                    candidates.add(record["run_id"])
        if expected["run_id"] not in candidates:
            raise EvidenceError("declared run_id is absent from included actual records")


def build(catalog_path: Path, output: Path) -> None:
    catalog = _read_json(catalog_path)
    if not isinstance(catalog, dict):
        raise EvidenceError("catalog must be an object")
    _assert_no_linked_ancestors(output)
    if _is_link(output):
        raise EvidenceError("output directory must not be a link or junction")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise EvidenceError("output directory must be empty")
    entries = []
    declared = [
        ("source_identity", "metadata/source-identity.json"),
        ("acceptance", "records/acceptance.json"),
        ("recovery", "records/recovery.json"),
    ]
    for key, dest in declared:
        if key not in catalog:
            raise EvidenceError(f"catalog missing {key}")
        entries.append({"source": catalog[key], "path": dest, "role": key})
    entries.extend(catalog.get("files", []))
    entries.append(
        {
            "source": str(Path(__file__).resolve()),
            "path": "verify_closeout_evidence.py",
            "role": "verifier",
        }
    )
    paths = set()
    manifest_files = []
    for item in entries:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("source"), str)
            or not isinstance(item.get("path"), str)
        ):
            raise EvidenceError("each file entry requires source and path strings")
        rel = _safe_path(item["path"])
        if str(rel) == MANIFEST or str(rel) in paths:
            raise EvidenceError(f"duplicate or reserved destination: {rel}")
        paths.add(str(rel))
        source = Path(item["source"])
        _assert_no_linked_ancestors(source)
        if _is_link(source) or not source.is_file():
            raise EvidenceError(f"source must be a regular non-symlink file: {source}")
        source = source.resolve(strict=True)
        data = source.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise EvidenceError(f"source exceeds per-file evidence bound: {source}")
        dest = _reject_linked_components(output.resolve(), rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        manifest_files.append(
            {
                "path": str(rel),
                "role": item.get("role", "other"),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
        if sum(row["size"] for row in manifest_files) > MAX_TOTAL_BYTES:
            raise EvidenceError("selected evidence exceeds total package size bound")
    manifest = {
        "schema_version": 1,
        "source_identity": "metadata/source-identity.json",
        "acceptance": "records/acceptance.json",
        "recovery": "records/recovery.json",
        "relationships": catalog.get("relationships", {}),
        "files": manifest_files,
    }
    (output / MANIFEST).write_bytes(_json_bytes(manifest))
    verify(output)


def verify(bundle: Path) -> None:
    _assert_no_linked_ancestors(bundle)
    root = bundle.resolve(strict=True)
    if _is_link(bundle):
        raise EvidenceError("bundle root must not be a link or junction")
    manifest_path = root / MANIFEST
    if _is_link(manifest_path):
        raise EvidenceError("manifest must not be a link or junction")
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise EvidenceError("unsupported or malformed manifest")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise EvidenceError("manifest files must be a list")
    paths = set()
    total_size = 0
    roles = {entry.get("role") for entry in files if isinstance(entry, dict)}
    if not {"source_identity", "acceptance", "recovery"}.issubset(roles):
        raise EvidenceError("source identity, acceptance, and recovery records are mandatory")
    for item in files:
        rel = _safe_path(item["path"])
        if str(rel) in paths or str(rel) == MANIFEST:
            raise EvidenceError(f"duplicate or reserved manifest path: {rel}")
        paths.add(str(rel))
        path = _reject_linked_components(root, rel)
        if path.is_symlink() or not path.is_file():
            raise EvidenceError(f"missing or linked required file: {rel}")
        data = path.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise EvidenceError(f"file exceeds per-file evidence bound: {rel}")
        total_size += len(data)
        if total_size > MAX_TOTAL_BYTES:
            raise EvidenceError("manifest exceeds total package size bound")
        if len(data) != item.get("size") or hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise EvidenceError(f"size/hash mismatch: {rel}")
    _check_relationships(root, files, manifest.get("relationships", {}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_build = sub.add_parser("build", help="copy explicitly catalogued files and verify")
    p_build.add_argument("--catalog", type=Path, required=True)
    p_build.add_argument("--out", type=Path, required=True)
    p_verify = sub.add_parser("verify", help="cold verify a bundle directory")
    p_verify.add_argument("bundle", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            build(args.catalog, args.out)
        else:
            verify(args.bundle)
        print("PASS: evidence bundle is complete, hash-valid, and internally consistent")
        return 0
    except (EvidenceError, OSError, KeyError, TypeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
