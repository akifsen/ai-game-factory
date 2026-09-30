"""Deterministic processing of operator-authored assemblies (ADR 0014, ADR 0016).

The source GLB is already authored in the factory naming contract. Processing
is transform-only: when ``source_front`` is ``+Z`` exactly one 180 degree
rotation about +Y is applied at the assembly root, by baking it into the
root-level children so ``ROOT`` stays identity. Deeper parent-relative
transforms, mesh data, accessors and the BIN chunk are never touched. A ``-Z``
source is retained byte-for-byte.

The assembly is not round-tripped through Blender: the V0.7 spike showed a
Blender import/export collapsing every part pivot to the origin while the
hierarchy imported cleanly (ADR 0013). Blender remains the declared authoring
tool of the source.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_io import write_glb
from gamefactory.adapters.assets.glb_validator import _read_glb, parse_glb
from gamefactory.adapters.assets.validation_rules import (
    NormalizationRecord,
    ParsedGLB,
    normalization_for,
)
from gamefactory.core.domain import transforms as tf
from gamefactory.core.domain.assembly_source import AssemblySourceRegistration
from gamefactory.core.domain.errors import SpecInvalidError

PROCESSING_REPORT_SCHEMA = "asset-processing-report-0.7.0"
PROCESSOR_NAME = "gamefactory.assembly_processor"
PROCESSOR_VERSION = "0.7.0"


class AssemblyProcessingError(SpecInvalidError):
    """The source cannot be normalized without guessing."""


@dataclass(frozen=True)
class AssemblyProcessingResult:
    processed_path: Path
    processed_sha256: str
    source_sha256: str
    record: NormalizationRecord
    report: dict[str, Any]


def _local_trs(matrix: tf.Matrix) -> dict[str, list[float]]:
    scale = tf.scale_of(matrix)
    rotation = tf.rotation_to_quat(tf.rotation_of(matrix))

    def clean(value: float) -> float:
        rounded = round(value, 9)
        return 0.0 if rounded == 0 else rounded

    return {
        "translation": [clean(v) for v in tf.translation_of(matrix)],
        "rotation": [clean(v) for v in rotation],
        "scale": [clean(v) for v in scale],
    }


def _require_root(parsed: ParsedGLB) -> int:
    root = parsed.node_named("ROOT")
    if root is None:
        raise AssemblyProcessingError("assembly source has no unique ROOT node")
    if parsed.scene_roots != (root.index,):
        raise AssemblyProcessingError("assembly source scene must contain exactly ROOT")
    if not tf.matrices_close(root.local, tf.identity(), 1e-9):
        raise AssemblyProcessingError(
            "assembly source ROOT must be identity; no correction is inferred"
        )
    return root.index


def normalize_assembly(
    source_path: Path,
    registration: AssemblySourceRegistration,
    output_path: Path,
    *,
    asset_id: str,
    processing_contract: dict[str, Any] | None = None,
) -> AssemblyProcessingResult:
    """Normalize one registered source into ``output_path``. Nothing else is written."""
    data = source_path.read_bytes()
    registration.check_artifact(data)
    source = parse_glb(source_path)
    root_index = _require_root(source)
    applied, quat = normalization_for(registration.source_front)
    changed: list[str] = []
    if not applied:
        output_path.write_bytes(data)
    else:
        document, binary = _read_glb(source_path, len(data) + 1)
        rotation = tf.trs_matrix(rotation=quat)
        for child in document["nodes"][root_index].get("children", []):
            node = document["nodes"][child]
            local = source.nodes[child].local
            for key in ("matrix", "translation", "rotation", "scale"):
                node.pop(key, None)
            node.update(_local_trs(tf.mat_mul(rotation, local)))
            changed.append(str(node.get("name") or child))
        output_path.write_bytes(write_glb(document, binary))
    processed_bytes = output_path.read_bytes()
    processed_sha = hashlib.sha256(processed_bytes).hexdigest()
    record = NormalizationRecord(
        source_front=registration.source_front,
        normalization_applied=applied,
        quaternion_xyzw=quat,
        resulting_front="-Z",
        source=source,
    )
    contract_sha = (
        hashlib.sha256(
            json.dumps(processing_contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if processing_contract is not None
        else None
    )
    report = {
        "schema_version": PROCESSING_REPORT_SCHEMA,
        "asset_id": asset_id,
        "processor": {"name": PROCESSOR_NAME, "version": PROCESSOR_VERSION},
        "source_kind": "local_operator_assembly",
        "source_sha256": registration.artifact_sha256,
        "processed_sha256": processed_sha,
        "processing_contract_sha256": contract_sha,
        "normalization": record.as_dict(),
        "changed_root_level_nodes": sorted(changed),
        "geometry_retained": True,
    }
    return AssemblyProcessingResult(
        processed_path=output_path,
        processed_sha256=processed_sha,
        source_sha256=registration.artifact_sha256,
        record=record,
        report=report,
    )


def normalization_record_from(
    report_normalization: dict[str, Any], source_path: Path | None
) -> NormalizationRecord:
    """Rebuild the record (with the retained source parsed) from a stored report."""
    transform = report_normalization.get("normalization_transform") or {}
    quat = transform.get("quaternion_xyzw") or [0.0, 0.0, 0.0, 1.0]
    return NormalizationRecord(
        source_front=report_normalization.get("source_front"),
        normalization_applied=bool(report_normalization.get("normalization_applied")),
        quaternion_xyzw=tf.normalize_quat(quat),
        resulting_front=str(report_normalization.get("resulting_front")),
        source=parse_glb(source_path) if source_path is not None else None,
    )
