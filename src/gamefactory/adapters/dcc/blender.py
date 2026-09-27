"""Blender DCC tool adapter for detection and version querying.

Uses safe subprocess execution without shell interpolation.
Core domain code does not import this adapter directly.
"""

import os
import shutil
from pathlib import Path

from gamefactory.adapters.dcc.base import DccAdapter, DccDetectionResult
from gamefactory.core.execution.process_runner import (
    CommandRequest,
    ProcessRunner,
)


class BlenderAdapter(DccAdapter):
    """Adapter for Blender 3D DCC software."""

    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self.runner = runner or ProcessRunner(sanitize_output=True)

    def find_candidate_executable(self, custom_path: str | None = None) -> str | None:
        """Locate Blender executable via custom path, env var, PATH, or standard install dirs."""
        # 1. Custom path
        if custom_path:
            p = Path(custom_path).resolve()
            if p.is_file():
                return str(p)

        # 2. Environment override
        env_path = os.environ.get("GAMEFACTORY_BLENDER_PATH")
        if env_path:
            p = Path(env_path).resolve()
            if p.is_file():
                return str(p)

        # 3. System PATH search
        for name in ("blender", "blender.exe"):
            found_which = shutil.which(name)
            if found_which:
                return found_which

        # 4. Standard Windows / Linux installation directories
        common_locations = [
            Path(r"C:\Program Files\Blender Foundation"),
            Path.home() / "blender",
        ]
        for loc in common_locations:
            if loc.is_dir():
                for f in loc.glob("**/blender.exe"):
                    if f.is_file():
                        return str(f)

        return None

    def detect_tool(self, custom_path: str | None = None) -> DccDetectionResult:
        """Detect Blender on the host system and query its version."""
        exe_path: str | None
        if custom_path:
            p = Path(custom_path).resolve()
            if not p.is_file():
                return DccDetectionResult(
                    available=False,
                    tool_name="blender",
                    executable_path=custom_path,
                    status="MISCONFIGURED",
                    details={
                        "reason": f"Configured Blender executable path does not exist: '{custom_path}'"
                    },
                )
            exe_path = str(p)
        else:
            exe_path = self.find_candidate_executable()

        if not exe_path:
            return DccDetectionResult(
                available=False,
                tool_name="blender",
                status="UNAVAILABLE",
                details={
                    "reason": "Blender executable was not found on PATH or configured locations."
                },
            )

        try:
            req = CommandRequest(
                args=[exe_path, "--version"],
                cwd=Path.cwd(),
                timeout_seconds=10.0,
            )
            res = self.runner.run(req)
            if res.exit_code == 0 and res.stdout.strip():
                version_str = res.stdout.strip().splitlines()[0]
                return DccDetectionResult(
                    available=True,
                    tool_name="blender",
                    version=version_str,
                    executable_path=exe_path,
                    status="AVAILABLE",
                    details={"raw_version": version_str},
                )
            else:
                return DccDetectionResult(
                    available=False,
                    tool_name="blender",
                    executable_path=exe_path,
                    status="MISCONFIGURED",
                    details={
                        "reason": "Executable failed version check",
                        "exit_code": res.exit_code,
                        "stderr": res.stderr,
                    },
                )
        except Exception as exc:
            return DccDetectionResult(
                available=False,
                tool_name="blender",
                executable_path=exe_path,
                status="MISCONFIGURED",
                details={"reason": f"Execution error during version check: {exc}"},
            )
