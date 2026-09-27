"""Synthetic tests for concept image ingestion and bound provenance."""

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from gamefactory.adapters.images.base import ConceptGenerationRequest
from gamefactory.adapters.images.concept_ingest import (
    FakeImageGenerationProvider,
    ingest_concept_image,
    verify_png_image,
)
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import CostClass


def _write_png(path: Path, size: tuple[int, int] = (64, 64)) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", size, color=(25, 90, 150, 255)).save(path, format="PNG")
    return path.read_bytes()


def _write_sidecar(path: Path, image_bytes: bytes, **fields: object) -> bytes:
    payload = {"sha256": hashlib.sha256(image_bytes).hexdigest(), **fields}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    return raw


def test_verify_png_image_valid_and_bounded(tmp_path: Path) -> None:
    png_path = tmp_path / "valid.png"
    _write_png(png_path, (256, 128))
    assert verify_png_image(png_path) == (256, 128)


def test_verify_png_image_invalid_header(tmp_path: Path) -> None:
    bad_path = tmp_path / "corrupt.png"
    bad_path.write_bytes(b"NOT_A_PNG_FILE_CONTENT")
    with pytest.raises(ValidationError, match="not a valid PNG"):
        verify_png_image(bad_path)


def test_verify_png_image_nonexistent(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="does not exist"):
        verify_png_image(tmp_path / "missing.png")


def test_verify_png_image_rejects_size_and_pixel_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gamefactory.adapters.images import concept_ingest

    image_path = tmp_path / "bounded.png"
    _write_png(image_path, (64, 64))
    monkeypatch.setattr(concept_ingest, "MAX_CONCEPT_IMAGE_BYTES", 10)
    with pytest.raises(ValidationError, match="size limit"):
        verify_png_image(image_path)

    monkeypatch.setattr(concept_ingest, "MAX_CONCEPT_IMAGE_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(concept_ingest, "MAX_CONCEPT_IMAGE_PIXELS", 100)
    with pytest.raises(ValidationError, match="decoded pixels"):
        verify_png_image(image_path)


def test_fake_image_generation_provider(tmp_path: Path) -> None:
    provider = FakeImageGenerationProvider(name="test_fake", cost_class=CostClass.LOCAL)
    output_path = tmp_path / "concept.png"
    req = ConceptGenerationRequest(
        prompt="Sci-Fi Energy Crate",
        asset_spec_hash="a" * 64,
        output_path=output_path,
        dimensions=(128, 128),
    )
    resp = provider.generate(req)
    assert resp.artifact_path == output_path
    assert output_path.is_file()
    assert resp.provenance.provider == "test_fake"
    assert resp.provenance.dimensions == {"width": 128, "height": 128}
    assert resp.provenance.source_type == "local_generation"
    assert provider.invocation_count == 1
    assert verify_png_image(output_path) == (128, 128)


def test_ingest_without_sidecar_has_unknown_generation_metadata(tmp_path: Path) -> None:
    src_path = tmp_path / "source.png"
    source_bytes = _write_png(src_path, (80, 60))
    target_path = tmp_path / "artifacts" / "ingested.png"
    target, provenance = ingest_concept_image(
        source_png_path=src_path,
        target_artifact_path=target_path,
        asset_spec_hash="b" * 64,
    )
    assert target == target_path
    assert target.read_bytes() == source_bytes
    assert provenance.dimensions == {"width": 80, "height": 60}
    assert provenance.generation_timestamp == "UNKNOWN"
    assert provenance.imported_at != "UNKNOWN"
    assert provenance.provider == "UNKNOWN"
    assert provenance.source_type == "imported"
    assert provenance.sidecar_hash is None


def test_ingest_matches_sidecar_image_hash_and_records_sidecar_hash(tmp_path: Path) -> None:
    src = tmp_path / "source.png"
    source_bytes = _write_png(src, (96, 72))
    sidecar = src.with_suffix(".json")
    sidecar_bytes = _write_sidecar(
        sidecar,
        source_bytes,
        created_at=1700000000.25,
        prompt="A generated test prop",
        generation={"provider": "diffusers_sdxl"},
        model={"id": "sdxl-test"},
    )
    target, provenance = ingest_concept_image(
        source_png_path=src,
        target_artifact_path=tmp_path / "artifacts" / "source.png",
        asset_spec_hash="c" * 64,
    )
    assert target.read_bytes() == source_bytes
    assert provenance.provider == "diffusers_sdxl"
    assert provenance.model == "sdxl-test"
    assert provenance.prompt == "A generated test prop"
    assert provenance.generation_timestamp.startswith("2023-11-14T22:13:20")
    assert provenance.imported_at != provenance.generation_timestamp
    assert provenance.source_type == "imported"
    assert provenance.sidecar_path == str(sidecar)
    assert provenance.sidecar_hash == hashlib.sha256(sidecar_bytes).hexdigest()


def test_epoch_zero_timestamp_is_valid_not_treated_as_missing(tmp_path: Path) -> None:
    src = tmp_path / "source.png"
    image_bytes = _write_png(src)
    sidecar = src.with_suffix(".json")
    _write_sidecar(sidecar, image_bytes, created_at=0, generation={"provider": "diffusers_sdxl"})
    _, provenance = ingest_concept_image(src, tmp_path / "target.png", "a" * 64)
    assert provenance.generation_timestamp.startswith("1970-01-01T00:00:00")


def test_local_generation_claim_requires_provider_and_timestamp(tmp_path: Path) -> None:
    src = tmp_path / "source.png"
    _write_png(src)
    with pytest.raises(ValidationError, match="matching hashed sidecar"):
        ingest_concept_image(
            src,
            tmp_path / "target.png",
            "a" * 64,
            source_type="local_generation",
        )


def test_unrelated_named_sidecar_is_not_guessed(tmp_path: Path) -> None:
    src = tmp_path / "source.png"
    _write_png(src)
    _write_sidecar(
        tmp_path / "local-sdxl-provenance.json",
        src.read_bytes(),
        created_at=1700000000,
        generation={"provider": "diffusers_sdxl"},
    )
    _, provenance = ingest_concept_image(
        source_png_path=src,
        target_artifact_path=tmp_path / "target.png",
        asset_spec_hash="d" * 64,
    )
    assert provenance.provider == "UNKNOWN"
    assert provenance.generation_timestamp == "UNKNOWN"
    assert provenance.sidecar_path is None


def test_unknown_sidecar_provider_remains_unknown_and_imported(tmp_path: Path) -> None:
    src = tmp_path / "source.png"
    image_bytes = _write_png(src)
    sidecar = src.with_suffix(".json")
    _write_sidecar(
        sidecar,
        image_bytes,
        created_at=1700000000,
        generation={"provider": "unrecognized-provider"},
    )
    _, provenance = ingest_concept_image(src, tmp_path / "target.png", "a" * 64)
    assert provenance.provider == "UNKNOWN"
    assert provenance.source_type == "imported"


@pytest.mark.parametrize("sidecar_state", ["missing", "malformed", "mismatch", "bad_hash"])
def test_explicit_invalid_sidecar_fails(tmp_path: Path, sidecar_state: str) -> None:
    src = tmp_path / "source.png"
    source_bytes = _write_png(src)
    sidecar = tmp_path / "explicit.json"
    if sidecar_state == "malformed":
        sidecar.write_text("{bad json", encoding="utf-8")
    elif sidecar_state == "mismatch":
        _write_sidecar(sidecar, b"different image bytes", generation={"provider": "diffusers_sdxl"})
    elif sidecar_state == "bad_hash":
        sidecar.write_text(json.dumps({"sha256": "not-a-hash"}), encoding="utf-8")

    with pytest.raises(ValidationError):
        ingest_concept_image(
            source_png_path=src,
            target_artifact_path=tmp_path / "target.png",
            asset_spec_hash="e" * 64,
            sidecar_provenance_path=sidecar,
        )
    assert source_bytes == src.read_bytes()
    assert not (tmp_path / "target.png").exists()


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), "not a timestamp"])
def test_invalid_sidecar_timestamp_fails(tmp_path: Path, timestamp: object) -> None:
    src = tmp_path / "source.png"
    image_bytes = _write_png(src)
    sidecar = tmp_path / "provenance.json"
    _write_sidecar(sidecar, image_bytes, created_at=timestamp)
    with pytest.raises(ValidationError, match="timestamp"):
        ingest_concept_image(
            source_png_path=src,
            target_artifact_path=tmp_path / "target.png",
            asset_spec_hash="f" * 64,
            sidecar_provenance_path=sidecar,
        )


def test_malformed_automatic_sidecar_is_not_ignored(tmp_path: Path) -> None:
    src = tmp_path / "source.png"
    _write_png(src)
    src.with_suffix(".json").write_text("{invalid", encoding="utf-8")
    with pytest.raises(ValidationError, match="Invalid concept provenance sidecar"):
        ingest_concept_image(
            source_png_path=src,
            target_artifact_path=tmp_path / "target.png",
            asset_spec_hash="a" * 64,
        )


def test_destination_must_not_exist_or_be_a_symlink(tmp_path: Path) -> None:
    src = tmp_path / "source.png"
    _write_png(src)
    target = tmp_path / "already.png"
    target.write_bytes(b"preserve me")
    with pytest.raises(ValidationError, match="already exists"):
        ingest_concept_image(src, target, "a" * 64)
    assert target.read_bytes() == b"preserve me"

    linked_target = tmp_path / "linked.png"
    try:
        linked_target.symlink_to(src)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unavailable on this host")
    with pytest.raises(ValidationError, match="symlink"):
        ingest_concept_image(src, linked_target, "a" * 64)
    assert src.is_file()


def test_ingest_rejects_invalid_spec_fingerprint(tmp_path: Path) -> None:
    src = tmp_path / "source.png"
    _write_png(src)
    with pytest.raises(ValidationError, match="Invalid asset_spec_hash"):
        ingest_concept_image(src, tmp_path / "target.png", "not_a_64_char_hex_hash")
