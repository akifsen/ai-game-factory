"""V0.4 golden crate stays immutable under the static_prop profile reader.

The historical files live outside git. CI without that directory skips. The
test never writes those files and never calls a provider.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from gamefactory.adapters.assets.glb_validator import validate_glb
from gamefactory.core.domain.asset_contracts import parse_asset_specification
from gamefactory.core.domain.production_receipt import read_production_receipt

GOLDEN = Path(".verification/v04-human-review/.gamefactory")
PROCESSED = "d74f69b010d8bd13ed34d646b0708adf97cc59706642ad75a3d5bc48581bdfe5"
RAW = "12d8d514f9c74451352510ac1132af3d745631bcb32ee5ad1e84537e4fc4d51c"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.mark.skipif(not GOLDEN.is_dir(), reason="local V0.4 golden workspace is not present")
def test_golden_crate_processed_hash_and_static_prop_validation() -> None:
    processed = GOLDEN / "assets/prop_energy_crate_01/r001/processed-attempt-1.glb"
    raw = GOLDEN / "assets/prop_energy_crate_01/r001/raw-attempt-4.glb"
    before_processed = processed.stat().st_mtime_ns
    before_raw = raw.stat().st_mtime_ns
    assert _sha256(processed) == PROCESSED
    assert _sha256(raw) == RAW
    spec = parse_asset_specification(
        Path("src/gamefactory/resources/specs/prop_energy_crate_01.yml")
    )
    assert spec.bound_profile().qualified == "static_prop@1"
    result = validate_glb(processed, spec)
    assert result.status.value == "PASS", result.to_dict()
    assert processed.stat().st_mtime_ns == before_processed
    assert raw.stat().st_mtime_ns == before_raw


@pytest.mark.skipif(not GOLDEN.is_dir(), reason="local V0.4 golden workspace is not present")
def test_golden_crate_historical_paid_invocation_is_one() -> None:
    """The durable paid record is the succeeded Meshy operation intent.

    This historical database stores the one paid call there, with 15 credits
    and the known task id. The test opens the file read-only.
    """
    database = GOLDEN / "state/factory.db"
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        intents = list(connection.execute("SELECT * FROM provider_operation_intents"))
    finally:
        connection.close()
    assert len(intents) == 1
    intent = intents[0]
    assert intent["provider"] == "meshy"
    assert intent["status"] == "SUCCEEDED"
    assert intent["actual_cost"] == 15.0
    assert intent["external_task_id"] == "01a0e359-fd9b-7389-845a-04381954da4f"
    assert intent["asset_id"] == "prop_energy_crate_01"


@pytest.mark.skipif(not GOLDEN.is_dir(), reason="local V0.4 golden workspace is not present")
def test_golden_v04_manifest_reads_without_rewrite() -> None:
    manifest_path = GOLDEN / "reports/WF-ASSET-96f68a4f/snapshot-002/manifest.json"
    before = manifest_path.read_bytes()
    manifest = json.loads(before)
    receipt = read_production_receipt(manifest)
    assert manifest_path.read_bytes() == before
    assert receipt["asset_id"] == "prop_energy_crate_01"
    assert receipt["profile_qualified"] == "static_prop@1"
    assert receipt["processed_artifact_hash"] == PROCESSED
    assert set(receipt["render_hashes"]) >= {"front", "three_quarter", "side"}
