#!/usr/bin/env python3
"""Build and cold-verify a bounded, portable closeout evidence directory.

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


_FIGURE = re.compile(r"<figure>(.*?)</figure>", re.IGNORECASE | re.DOTALL)
_SRC = re.compile(r'\bsrc="([^"]+)"', re.IGNORECASE)
_ALT = re.compile(r'\balt="([^"]*)"', re.IGNORECASE)
_SHA = re.compile(r"SHA-256 ([0-9a-f]{64})")


def _is_capture_validation(doc: object) -> bool:
    if not isinstance(doc, dict) or not isinstance(doc.get("execution_id"), str):
        return False
    images = doc.get("images")
    if not isinstance(images, list) or not images:
        return False
    return all(
        isinstance(image, dict)
        and isinstance(image.get("id"), str)
        and image.get("id")
        and isinstance(image.get("sha256"), str)
        and len(image["sha256"]) == 64
        for image in images
    )


def _capture_validation_documents(bundle: Path, files: list[dict]) -> list[dict]:
    documents: list[dict] = []
    seen: set[str] = set()
    for item in files:
        path = str(item.get("path", ""))
        if not path.endswith(".json"):
            continue
        try:
            doc = _read_json(bundle / path)
        except EvidenceError:
            continue
        if not _is_capture_validation(doc):
            continue
        execution = str(doc["execution_id"])
        if execution in seen:
            raise EvidenceError("capture bundle has two validation reports for one execution")
        seen.add(execution)
        documents.append(doc)
    return documents


def _validation_index(validations: list[dict]) -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for doc in validations:
        execution = str(doc["execution_id"])
        for image in doc["images"]:
            key = (execution, str(image["id"]))
            if key in index:
                raise EvidenceError("duplicate capture id in a validation report")
            index[key] = str(image["sha256"])
    return index


def _bind_manifest_images(bundle: Path, image_rows: list[dict], validations: list[dict]) -> None:
    index = _validation_index(validations)
    for item in image_rows:
        digest = hashlib.sha256((bundle / item["path"]).read_bytes()).hexdigest()
        if digest != item.get("sha256"):
            raise EvidenceError("rendered image is not bound to the capture validation report")
        capture_id = PurePosixPath(str(item["path"])).stem
        matches = [key for key, sha in index.items() if key[1] == capture_id and sha == digest]
        if len(matches) != 1:
            raise EvidenceError("rendered image is not bound to the capture validation report")


def _resolve_review_src(html_path: str, src: str) -> str:
    if (
        not src
        or src.startswith(("/", "\\"))
        or "\\" in src
        or ":" in src
        or ".." in src.split("/")
        or src.lower().startswith(("http:", "https:", "file:"))
    ):
        raise EvidenceError(f"review image reference is not a bundle-relative path: {src}")
    parts = list(PurePosixPath(html_path).parent.parts)
    for part in src.split("/"):
        if part in ("", ".", ".."):
            raise EvidenceError(f"review image reference is not a bundle-relative path: {src}")
        parts.append(part)
    return "/".join(parts)


def _bind_review_html(bundle: Path, files: list[dict], validations: list[dict]) -> None:
    """Bind every image a review page displays to that capture's validation hash."""
    listed = {str(item["path"]): item for item in files}
    index = _validation_index(validations)
    ids_by_execution: dict[str, list[str]] = {}
    for doc in validations:
        ids_by_execution[str(doc["execution_id"])] = [str(image["id"]) for image in doc["images"]]
    displayed: dict[str, list[str]] = {execution: [] for execution in ids_by_execution}
    saw_figure = False
    for item in files:
        path = str(item["path"])
        if not path.endswith(".html"):
            continue
        text = (bundle / path).read_text(encoding="utf-8")
        figures = _FIGURE.findall(text)
        if not figures:
            continue
        saw_figure = True
        page_execution: str | None = None
        for figure in figures:
            src_match = _SRC.search(figure)
            alt_match = _ALT.search(figure)
            sha_match = _SHA.search(figure)
            if src_match is None or alt_match is None or sha_match is None:
                raise EvidenceError("review figure does not identify its capture image")
            capture_id = alt_match.group(1).split()[0]
            src = src_match.group(1)
            resolved = _resolve_review_src(path, src)
            if resolved not in listed:
                raise EvidenceError(f"review HTML displays a file outside the manifest: {resolved}")
            if PurePosixPath(src).name != f"{capture_id}.png":
                raise EvidenceError(
                    "review HTML points at a different capture than the figure caption"
                )
            digest = hashlib.sha256((bundle / resolved).read_bytes()).hexdigest()
            if digest != sha_match.group(1) or digest != listed[resolved].get("sha256"):
                raise EvidenceError("displayed PNG does not match the review caption or manifest")
            owners = [
                execution
                for (execution, image_id), sha in index.items()
                if image_id == capture_id and sha == digest
            ]
            if len(owners) != 1:
                raise EvidenceError("displayed PNG is not the validation image for this capture")
            execution = owners[0]
            if page_execution is None:
                page_execution = execution
            elif page_execution != execution:
                raise EvidenceError("review HTML mixes captures from different executions")
            if capture_id in displayed[execution]:
                raise EvidenceError("review HTML repeats a capture id")
            displayed[execution].append(capture_id)
    if not saw_figure:
        return
    for execution, ids in ids_by_execution.items():
        if not displayed[execution]:
            continue
        if displayed[execution] != ids:
            raise EvidenceError("review HTML does not display each validated capture once")


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
    capture = bool(execute_task) and execute_task.get("task_type") == "godot_capture_execute"
    if capture:
        if (
            not isinstance(val, dict)
            or val.get("status") not in ("PASS", "FAIL")
            or val.get("execution_id") != expected["execution_id"]
            or not isinstance(val.get("images"), list)
        ):
            raise EvidenceError("capture validation is not bound to this execution")
        image_rows = [item for item in files if item.get("role") == "image"]
        if not image_rows:
            raise EvidenceError("capture bundle requires the rendered PNG files")
        validations = _capture_validation_documents(bundle, files)
        if not any(
            doc is val or doc.get("execution_id") == val.get("execution_id") for doc in validations
        ):
            validations.insert(0, val)
        _bind_manifest_images(bundle, image_rows, validations)
        _bind_review_html(bundle, files, validations)
        execute_type = "godot_capture_execute"
        validate_type = "godot_capture_validate"
        validation_artifact = "godot-capture-validation"
    else:
        if (
            not isinstance(val, dict)
            or val.get("status") not in ("PASS", "FAIL")
            or not isinstance(val.get("findings"), list)
        ):
            raise EvidenceError("validation report does not match the raw V0.2 report schema")
        execute_type = "godot_execute"
        validate_type = "godot_validate"
        validation_artifact = "godot-validation-report"
    if (
        not execute_task
        or execute_task.get("task_type") != execute_type
        or execute_task.get("workflow_id") != expected["workflow_id"]
    ):
        raise EvidenceError("inspect does not associate the selected execution task with workflow")
    if (
        not validation_task
        or validation_task.get("task_type") != validate_type
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
        ("validation", validation_artifact, expected["validation_task_id"]),
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
