from pathlib import Path


def test_bundled_cold_verifier_matches_source_script() -> None:
    repository = Path(__file__).resolve().parents[2]
    source = repository / "scripts" / "verify_asset_bundle.py"
    packaged = (
        repository / "src" / "gamefactory" / "resources" / "scripts" / "verify_asset_bundle.py"
    )
    assert packaged.read_bytes() == source.read_bytes()
