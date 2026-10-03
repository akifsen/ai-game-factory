"""CLI handler for bounded natural-language static_prop planning."""

from __future__ import annotations

import json
import os
import secrets
from argparse import Namespace
from pathlib import Path
from typing import Any

from gamefactory.adapters.planning.codex_cli_agent import CodexCliAgentProvider
from gamefactory.cli.exit_codes import EXIT_SUCCESS
from gamefactory.core.domain.errors import ConfigurationError
from gamefactory.core.execution.path_guard import PathGuard


def _resolve_output_path(root: Path, raw_output: str) -> Path:
    path = Path(raw_output).expanduser()
    candidate = path if path.is_absolute() else (root / path)
    guard = PathGuard(root.resolve())
    return guard.resolve_safe_path(candidate)


def _stage_plan_payload(parent: Path, output_name: str, payload: bytes) -> Path:
    token = secrets.token_hex(8)
    temp_path = parent / f".{output_name}.{os.getpid()}.{token}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temp_path, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return temp_path


def _unlink_plan_temp(temp_path: Path) -> None:
    if temp_path.exists():
        try:
            temp_path.unlink()
        except OSError:
            pass


def write_plan_output_atomic(output_path: Path, spec: dict[str, Any]) -> None:
    """Publish JSON atomically without clobbering an existing destination."""
    parent = output_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(spec, indent=2, sort_keys=True) + "\n").encode("utf-8")
    staged = _stage_plan_payload(parent, output_path.name, payload)
    try:
        try:
            os.link(staged, output_path)
        except FileExistsError as exc:
            raise ConfigurationError(
                f"Refusing to overwrite existing plan output: {output_path}"
            ) from exc
    finally:
        _unlink_plan_temp(staged)


def run_plan_command(root: Path, args: Namespace) -> tuple[Any, int, str | None]:
    """Plan a static_prop specification via Codex and write JSON to --output."""
    output_path = _resolve_output_path(root, args.output)
    if output_path.exists():
        raise ConfigurationError(f"Refusing to overwrite existing plan output: {output_path}")
    provider = CodexCliAgentProvider(
        codex_path=getattr(args, "codex_path", None),
        timeout_seconds=float(args.timeout),
    )
    spec = provider.plan(args.request)
    write_plan_output_atomic(output_path, spec)
    asset_id = spec.get("asset_id", "")
    profile = "static_prop@1"
    payload = {
        "output": str(output_path),
        "asset_id": asset_id,
        "profile": profile,
        "specification": spec,
    }
    human = (
        f"Wrote static_prop plan to {output_path} (asset_id={asset_id}, profile={profile}). "
        "This command does not create assets; use asset create --spec when ready."
    )
    return payload, EXIT_SUCCESS, human
