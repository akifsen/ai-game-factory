#!/usr/bin/env python3
"""Cold, standard-library-only verification of internal rig evidence (V0.8-2)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXCLUDED = {"manifest.json"}
_MAX_FILE_BYTES = 50_000_000
_MAX_BUNDLE_BYTES = 256_000_000
_MAX_JSON_BYTES = 4_000_000
_MAX_JSON_DEPTH = 64
_MAX_JSON_ARRAY_LEN = 100_000
_MAX_JSON_OBJECT_KEYS = 10_000
_MAX_RUNTIME_VERTICES = 4096
_WEIGHT_SUM_TOL = 1e-3
_IBM_TOL = 1e-3
_BASIS_TOL = 0.08

VALIDATOR_CONTRACT_VERSION = "humanoid_12bone_v1"
PINNED_CONTRACT_CANONICAL_SHA256 = (
    "670e55fc5ea867bb546ff29f03c10bd05e65a6707a79954eb3f1d3e81070245e"
)
PINNED_BLENDER_EXPORT_SCRIPT_SHA256 = (
    "43f321887b2ffba010e5876d3a5fd21d88fa04d8a0cec3e09bd5996f30cbe769"
)
PINNED_GODOT_HARNESS_REVIEWED_SHA256 = (
    "d4f406daded207808e5bbdb8d4501c23803136a7f7449a59dc9a47ae0fc1fcea"
)

_SINGLE_ROLES = {
    "skinned_glb",
    "rig_verification_contract",
    "rig_validation_report",
    "rig_runtime_request",
    "rig_runtime_observation",
    "reviewed_blender_export_script",
    "reviewed_godot_harness",
    "source_declaration",
}


class InvalidGLB(ValueError):
    pass
