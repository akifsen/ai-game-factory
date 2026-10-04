"""Finite, operator-authorized native Godot editor/run/export operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from gamefactory.adapters.engines.godot_staging import GodotStager, _is_reparse, sha256_file
from gamefactory.core.domain.errors import ToolExecutionError, ValidationError
from gamefactory.core.execution.path_guard import PathGuard, assert_managed_directory
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

_PRESET = re.compile(r'(?m)^\[preset\.\d+\]\s*\n(?:(?!^\[).)*?^name\s*=\s*"([^"\n]+)"', re.S)
_TIMEOUT = 300.0


def _bounded_sha256(path: Path, limit: int) -> str:
    if _is_reparse(path):
        raise ValidationError("Output cannot be a symbolic link or junction")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValidationError("Output must be a regular file")
        digest = hashlib.sha256()
        consumed = 0
        while consumed <= limit:
            chunk = os.read(fd, min(1024 * 1024, limit + 1 - consumed))
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > limit:
                raise ValidationError("Output exceeds its byte limit")
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _reject_reparse_components(path: Path) -> None:
    current = path.absolute()
    while current != current.parent:
        if _is_reparse(current):
            raise ValidationError("Operation path cannot contain a symbolic link or junction")
        current = current.parent


def _exe(executable: Path | str) -> Path:
    original = Path(executable)
    _reject_reparse_components(original)
    path = original.resolve(strict=True)
    if not path.is_file():
        raise ValidationError("Godot executable must be an operator-selected regular file")
    return path


def _project(root: Path | str) -> Path:
    original = Path(root)
    _reject_reparse_components(original)
    path = original.resolve(strict=True)
    if not path.is_dir() or not (path / "project.godot").is_file():
        raise ValidationError("A Godot project.godot is required")
    return path


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 900:
        raise ValidationError("Process timeout must be finite and between 0 and 900 seconds")
    return float(value)


def _has_nonempty_size(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    size = entry.get("size")
    return isinstance(size, int) and not isinstance(size, bool) and size > 0


def _scene(project: Path, scene: str | None) -> str:
    value = scene or "res://main.tscn"
    if not value.startswith("res://") or ".." in Path(value[6:]).parts:
        raise ValidationError("Scene must be a safe project-relative res:// path")
    resolved = PathGuard(project).resolve_safe_path(value[6:])
    if not resolved.is_file() or resolved.suffix.lower() not in {".tscn", ".scn"}:
        raise ValidationError("Selected scene is missing or has an unsupported extension")
    return value


def read_export_presets(project_root: Path | str) -> list[str]:
    project = _project(project_root)
    path = project / "export_presets.cfg"
    if _is_reparse(path) or not path.is_file():
        return []
    content = _read_export_presets_text(path)
    names = _PRESET.findall(content)
    if not names or len(set(names)) != len(names):
        raise ValidationError("export_presets.cfg must contain unique named presets")
    return names


def _read_export_presets_text(path: Path) -> str:
    _reject_reparse_components(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError("export_presets.cfg must be a regular file")
        data = os.read(descriptor, 1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValidationError("export_presets.cfg exceeds the parse limit")
        return data.decode("utf-8", "replace")
    finally:
        os.close(descriptor)


def export_preset_platform(project_root: Path | str, preset: str) -> str:
    project = _project(project_root)
    content = _read_export_presets_text(project / "export_presets.cfg")
    blocks = re.split(r"(?m)(?=^\[preset\.\d+\]\s*$)", content)
    for block in blocks:
        name = re.search(r'(?m)^name\s*=\s*"([^"\n]+)"', block)
        platform = re.search(r'(?m)^platform\s*=\s*"([^"\n]+)"', block)
        if name and name.group(1) == preset and platform:
            return platform.group(1)
    raise ValidationError("Selected export preset has no platform identity")


def build_editor_request(
    executable: Path | str, project_root: Path | str, *, timeout_seconds: float = _TIMEOUT
) -> CommandRequest:
    exe, project = _exe(executable), _project(project_root)
    return CommandRequest(
        [str(exe), "--editor", "--path", str(project)],
        project,
        timeout_seconds=_timeout(timeout_seconds),
    )


def build_run_request(
    executable: Path | str,
    project_root: Path | str,
    scene: str | None = None,
    *,
    timeout_seconds: float = _TIMEOUT,
) -> CommandRequest:
    exe, project = _exe(executable), _project(project_root)
    args = [str(exe), "--path", str(project)]
    if scene is not None:
        args.append(_scene(project, scene))
    return CommandRequest(args, project, timeout_seconds=_timeout(timeout_seconds))


def build_export_request(
    executable: Path | str,
    project_root: Path | str,
    preset: str,
    output: Path | str,
    *,
    output_root: Path | str | None = None,
    timeout_seconds: float = _TIMEOUT,
) -> CommandRequest:
    exe, project = _exe(executable), _project(project_root)
    presets = read_export_presets(project)
    if not isinstance(preset, str) or preset not in presets:
        raise ValidationError(
            "Export preset must exactly match a named entry in export_presets.cfg"
        )
    target_original = Path(output)
    _reject_reparse_components(target_original)
    target = target_original.resolve(strict=False)
    if output_root is not None:
        _reject_reparse_components(Path(output_root))
    allowed_root = (
        Path(output_root).resolve(strict=True)
        if output_root is not None
        else (project / ".gamefactory" / "attempts").resolve(strict=False)
    )
    try:
        target.relative_to(allowed_root)
    except ValueError as exc:
        raise ValidationError(
            "Export output must be inside a fresh managed attempt directory"
        ) from exc
    _reject_reparse_components(target.parent)
    if target.exists() or _is_reparse(target) or not target.parent.is_dir():
        raise ValidationError("Export output must be a new file in an existing attempt directory")
    return CommandRequest(
        [str(exe), "--headless", "--path", str(project), "--export-release", preset, str(target)],
        project,
        timeout_seconds=_timeout(timeout_seconds),
    )


def _stage(root: Path, attempt: Path) -> tuple[Path, str]:
    stage = attempt / "project"
    assert_managed_directory(root, ".gamefactory")
    files, fingerprint = GodotStager(root, attempt).source_manifest()
    stage.mkdir(parents=True, exist_ok=False)
    for item in files:
        src, dst = root / Path(item.relative_path), stage / Path(item.relative_path)
        PathGuard(stage).resolve_safe_path(item.relative_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        with src.open("rb") as source, dst.open("xb") as dest:
            while chunk := source.read(min(1024 * 1024, item.size + 1 - copied)):
                copied += len(chunk)
                if copied > item.size:
                    raise ValidationError("Project source grew beyond its staged size")
                dest.write(chunk)
        if copied != item.size:
            raise ValidationError("Project source changed size during staging")
        if sha256_file(src) != item.sha256 or sha256_file(dst) != item.sha256:
            raise ValidationError("Project source changed during staging")
    return stage, fingerprint


def execute_godot_operation(
    operation: str,
    executable: Path | str,
    project_root: Path | str,
    *,
    attempt_id: str,
    workflow_id: str | None = None,
    task_id: str | None = None,
    expected_source_hash: str | None = None,
    expected_executable_sha256: str | None = None,
    scene: str | None = None,
    preset: str | None = None,
    output_name: str | None = None,
    runner: ProcessRunner | Any | None = None,
    timeout_seconds: float = _TIMEOUT,
) -> dict[str, Any]:
    """Run one finite process in a fresh managed attempt; export success requires a real output."""
    if operation not in {"editor", "run", "export"}:
        raise ValidationError("operation must be editor, run, or export")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", attempt_id):
        raise ValidationError("attempt_id has invalid characters")
    root, exe = _project(project_root), _exe(executable)
    exe_hash = sha256_file(exe)
    timeout_seconds = _timeout(timeout_seconds)
    attempts = assert_managed_directory(root, ".gamefactory/attempts")
    if _is_reparse(root / ".gamefactory") or _is_reparse(attempts):
        raise ValidationError("Managed attempt directory cannot be a link or junction")
    attempts.mkdir(parents=True, exist_ok=True)
    attempt = attempts / attempt_id
    if attempt.exists() or _is_reparse(attempt):
        raise ValidationError("Attempt directory already exists")
    attempt.mkdir()
    stage, source_hash = _stage(root, attempt)
    if expected_source_hash is not None and source_hash != expected_source_hash:
        raise ValidationError("Staged source manifest differs from the reviewed operation inputs")
    if expected_executable_sha256 is not None and sha256_file(exe) != expected_executable_sha256:
        raise ValidationError("Godot executable changed while the operation was being staged")
    if operation == "editor":
        request = build_editor_request(exe, stage, timeout_seconds=timeout_seconds)
    elif operation == "run":
        request = build_run_request(exe, stage, scene, timeout_seconds=timeout_seconds)
    else:
        if not preset:
            raise ValidationError("A named export preset is required")
        platform = export_preset_platform(stage, preset)
        if output_name is None:
            output_name = {
                "Windows Desktop": "build.exe",
                "Linux/X11": "build.x86_64",
                "macOS": "build.zip",
                "Web": "build.zip",
                "Android": "build.apk",
                "iOS": "build.zip",
            }.get(platform)
        if output_name is None:
            raise ValidationError(
                f"No default export filename is defined for platform {platform!r}; supply output_name"
            )
        if Path(output_name).name != output_name or output_name in {".", ".."}:
            raise ValidationError("output_name must be a single safe filename")
        output = attempt / "outputs" / output_name
        PathGuard(attempt).resolve_safe_path(output.relative_to(attempt).as_posix())
        output.parent.mkdir()
        request = build_export_request(
            exe, stage, preset, output, output_root=attempt, timeout_seconds=timeout_seconds
        )
    intent = {
        "schema_version": 1,
        "operation": operation,
        "attempt_id": attempt_id,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "execution_id": attempt_id,
        "source_manifest_sha256": source_hash,
        "executable_sha256": exe_hash,
        "preset": preset,
    }
    (attempt / "operation-intent.json").write_text(
        json.dumps(intent, sort_keys=True), encoding="utf-8"
    )
    try:
        result = (runner or ProcessRunner(sanitize_output=True)).run(request)
    except Exception as exc:
        failure = {**intent, "status": "runner-error", "error_type": type(exc).__name__}
        (attempt / "operation-result.json").write_text(
            json.dumps(failure, sort_keys=True), encoding="utf-8"
        )
        raise
    output_info = None
    output_failure = None
    if operation == "export" and result.exit_code == 0 and not result.timed_out:
        try:
            output = attempt / "outputs"
            entries: list[dict[str, str | int]] = []
            total_output_bytes = 0
            entry_count = 0
            for current, dirs, names in os.walk(output, topdown=True, followlinks=False):
                base = Path(current)
                entry_count += len(dirs) + len(names)
                if entry_count > 20000:
                    raise ValidationError("Export output entry limit exceeded")
                for name in list(dirs):
                    path = base / name
                    if _is_reparse(path):
                        raise ValidationError("Export output contains a link or junction")
                for name in sorted(names):
                    path = base / name
                    if _is_reparse(path):
                        raise ValidationError("Export output contains a link or junction")
                    if len(entries) >= 10000:
                        raise ValidationError("Export output file limit exceeded")
                    size = path.stat().st_size
                    total_output_bytes += size
                    if total_output_bytes > 4 * 1024 * 1024 * 1024:
                        raise ValidationError("Export output byte limit exceeded")
                    entries.append(
                        {
                            "path": path.relative_to(attempt).as_posix(),
                            "size": size,
                            "sha256": _bounded_sha256(path, 4 * 1024 * 1024 * 1024),
                        }
                    )
            output_info = {"files": entries}
        except Exception as exc:
            output_failure = type(exc).__name__
    output_files = output_info.get("files") if isinstance(output_info, dict) else None
    output_valid = operation != "export" or (
        isinstance(output_files, list)
        and bool(output_files)
        and all(_has_nonempty_size(item) for item in output_files)
    )
    receipt = {
        "schema_version": 1,
        "operation": operation,
        "attempt_id": attempt_id,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "execution_id": attempt_id,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "process": result.to_dict(),
        "source_manifest_sha256": source_hash,
        "executable_sha256": exe_hash,
        "preset": preset,
        "output": output_info,
        "output_validation_error_type": output_failure,
        "status": "succeeded"
        if output_valid
        and result.exit_code == 0
        and not result.timed_out
        and result.cleanup_completed
        else "failed",
    }
    receipt_path = attempt / "operation-result.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2), encoding="utf-8")
    if receipt["status"] != "succeeded":
        raise ToolExecutionError("Godot operation did not complete successfully", details=receipt)
    return receipt
