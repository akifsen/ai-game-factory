#!/usr/bin/env python3
"""Run scoped internal rig evidence acceptance checks (V0.8-2)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    tests = [
        "tests/unit/test_internal_rig_cold.py",
        "tests/unit/test_internal_rig_schemas.py",
        "tests/unit/test_skin_oracle_verify.py",
    ]
    cmd = [sys.executable, "-m", "pytest", *[str(repo / t) for t in tests], "-q"]
    completed = subprocess.run(cmd, cwd=repo, check=False)
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
