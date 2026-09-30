"""Concept image verification, provenance ingestion, and test fake provider."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import warnings
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from gamefactory.adapters.images.base import (
    ConceptGenerationRequest,
    ConceptGenerationResponse,
    ConceptProvenance,
    ImageGenerationProvider,
)
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import CostClass, utc_now_iso

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_HEX_64_REGEX = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_CONCEPT_IMAGE_BYTES = 32 * 1024 * 1024
MAX_CONCEPT_IMAGE_DIMENSION = 16_384
MAX_CONCEPT_IMAGE_PIXELS = 50_000_000
MAX_PROVENANCE_SIDECAR_BYTES = 1024 * 1024
RECOGNIZED_CONCEPT_PROVENANCE_TYPES = frozenset(
    {
        "diffusers_sdxl",
        "local_blender_render",
        "local_operator_drawing",
        "external_image",
    }
)
KNOWN_CONCEPT_PROVIDERS = RECOGNIZED_CONCEPT_PROVENANCE_TYPES


def _read_bounded(path: Path, max_bytes: int, label: str) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
    except OSError as exc:
        raise ValidationError(f"Cannot read {label}: {path}: {exc}") from exc
    if len(data) > max_bytes:
        raise ValidationError(f"{label} exceeds size limit of {max_bytes} bytes: {path}")
    return data


def verify_png_image(image_path: Path) -> tuple[int, int]:
    """Verify that a file is a valid, readable PNG image and return (width, height).

    Raises ValidationError if the file does not exist, has an invalid header,
    or fails Pillow decoding.
    """
    if not image_path.is_file():
        raise ValidationError(f"Concept image file does not exist: {image_path}")

    image_bytes = _read_bounded(image_path, MAX_CONCEPT_IMAGE_BYTES, "Concept image")
    # 1. Header magic check
    header = image_bytes[:8]
    if header != PNG_MAGIC:
        raise ValidationError(
            f"Concept image at {image_path} is not a valid PNG (invalid header bytes)"
        )

    # 2. Pillow full decode check
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(image_bytes))
            w, h = img.size
            _validate_dimensions(w, h)
            img.verify()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(image_bytes))
            _validate_dimensions(*img.size)
            img.load()
            w, h = img.size
            return w, h
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(
            f"Concept image at {image_path} failed PNG integrity verification: {exc}"
        ) from exc


def _validate_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ValidationError(f"Invalid image dimensions: {width}x{height}")
    if width > MAX_CONCEPT_IMAGE_DIMENSION or height > MAX_CONCEPT_IMAGE_DIMENSION:
        raise ValidationError(
            f"Concept image dimension exceeds {MAX_CONCEPT_IMAGE_DIMENSION}px: {width}x{height}"
        )
    if width * height > MAX_CONCEPT_IMAGE_PIXELS:
        raise ValidationError(
            f"Concept image exceeds {MAX_CONCEPT_IMAGE_PIXELS} decoded pixels: {width}x{height}"
        )


def ingest_concept_image(
    source_png_path: Path,
    target_artifact_path: Path,
    asset_spec_hash: str,
    sidecar_provenance_path: Path | None = None,
    prompt: str | None = None,
    provider_name: str | None = None,
    model_name: str | None = None,
    source_type: str = "imported",
    cost_classification: CostClass = CostClass.LOCAL,
    sidecar_provenance_bytes: bytes | None = None,
) -> tuple[Path, ConceptProvenance]:
    """Ingest an existing concept PNG, verify its integrity, copy to target, and record honest provenance.

    Never fabricates generation timestamps: generation_timestamp is drawn from a hashed,
    matching sidecar or set explicitly to 'UNKNOWN'. The ingestion time is recorded
    separately in imported_at. The caller must validate that the destination parent is
    inside its managed artifact root; this function protects the destination leaf from
    overwrite and symlink replacement.
    """
    # 1. Validate spec hash fingerprint format
    clean_spec_hash = asset_spec_hash
    if not _HEX_64_REGEX.fullmatch(clean_spec_hash):
        raise ValidationError(
            f"Invalid asset_spec_hash '{asset_spec_hash}': must be a 64-character SHA-256 hex string",
            details={"asset_spec_hash": asset_spec_hash},
        )

    if source_type not in {"imported", "local_generation"}:
        raise ValidationError(f"Unsupported concept source_type: {source_type}")
    if sidecar_provenance_bytes is not None and (
        not isinstance(sidecar_provenance_bytes, bytes)
        or len(sidecar_provenance_bytes) > MAX_PROVENANCE_SIDECAR_BYTES
    ):
        raise ValidationError("Provenance sidecar snapshot exceeds its bounded size")

    # Read a single bounded byte snapshot so verification, hash binding, and copy all
    # refer to identical bytes even if the source changes concurrently.
    source_bytes = _read_bounded(source_png_path, MAX_CONCEPT_IMAGE_BYTES, "Concept image")
    if source_bytes[:8] != PNG_MAGIC:
        raise ValidationError(f"Concept image at {source_png_path} is not a valid PNG")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source_bytes)) as img:
                width, height = img.size
                _validate_dimensions(width, height)
                img.verify()
            with Image.open(io.BytesIO(source_bytes)) as img:
                _validate_dimensions(*img.size)
                img.load()
                width, height = img.size
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(
            f"Concept image at {source_png_path} failed PNG integrity verification: {exc}"
        ) from exc

    # 3. Locate and parse honest sidecar metadata if available
    sidecar_file: Path | None = None
    if sidecar_provenance_path is not None:
        if not sidecar_provenance_path.is_file():
            raise ValidationError(
                f"Explicit concept provenance sidecar does not exist: {sidecar_provenance_path}"
            )
        sidecar_file = sidecar_provenance_path
    else:
        # Only consider a basename-matched sidecar; directory-wide guesses can bind
        # another image's generation record to this image.
        candidate = source_png_path.with_suffix(".json")
        if candidate.exists() or candidate.is_symlink():
            sidecar_file = candidate

    generation_timestamp = "UNKNOWN"
    resolved_prompt = prompt or "Imported concept"
    resolved_model = model_name or "unknown"
    resolved_provider = provider_name or "UNKNOWN"
    sidecar_hash: str | None = None
    paid = False
    source_script_sha256: str | None = None

    if sidecar_file is not None:
        sidecar_bytes = (
            sidecar_provenance_bytes
            if sidecar_provenance_bytes is not None
            else _read_bounded(
                sidecar_file, MAX_PROVENANCE_SIDECAR_BYTES, "Provenance sidecar"
            )
        )
        try:
            sidecar_data = json.loads(
                sidecar_bytes.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"Invalid JSON constant {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValidationError(
                f"Invalid concept provenance sidecar {sidecar_file}: {exc}"
            ) from exc
        if not isinstance(sidecar_data, dict):
            raise ValidationError(
                f"Concept provenance sidecar must contain a JSON object: {sidecar_file}"
            )
        bound_hash = sidecar_data.get("sha256")
        image_hash = hashlib.sha256(source_bytes).hexdigest()
        if not isinstance(bound_hash, str) or not _HEX_64_REGEX.fullmatch(bound_hash):
            raise ValidationError(
                f"Concept provenance sidecar lacks a valid sha256 field: {sidecar_file}"
            )
        if bound_hash.lower() != image_hash:
            raise ValidationError(
                f"Concept provenance sidecar sha256 does not match image: {sidecar_file}"
            )
        sidecar_hash = hashlib.sha256(sidecar_bytes).hexdigest()

        raw_ts = sidecar_data.get("created_at")
        if raw_ts is None:
            raw_ts = sidecar_data.get("generation_timestamp")
        if raw_ts is not None:
            generation_timestamp = _parse_generation_timestamp(raw_ts, sidecar_file)

        if not prompt:
            sidecar_prompt = sidecar_data.get("prompt")
            recipe = sidecar_data.get("recipe")
            if not sidecar_prompt and isinstance(recipe, dict):
                brief = recipe.get("brief")
                if isinstance(brief, dict):
                    sidecar_prompt = brief.get("subject")
            if isinstance(sidecar_prompt, str) and sidecar_prompt.strip():
                resolved_prompt = sidecar_prompt.strip()

        if not model_name:
            sidecar_model = sidecar_data.get("model")
            recipe = sidecar_data.get("recipe")
            if isinstance(sidecar_model, dict):
                model_value = sidecar_model.get("id") or sidecar_model.get("repo")
                if isinstance(model_value, str) and model_value.strip():
                    resolved_model = model_value.strip()
            elif isinstance(sidecar_model, str) and sidecar_model.strip():
                resolved_model = sidecar_model.strip()
            elif isinstance(recipe, dict) and isinstance(recipe.get("model_id"), str):
                resolved_model = recipe["model_id"]

        gen_info = sidecar_data.get("generation")
        if isinstance(gen_info, dict):
            if not provider_name:
                sidecar_provider = gen_info.get("provider")
                if isinstance(sidecar_provider, str) and sidecar_provider.strip():
                    candidate_provider = sidecar_provider.strip().lower()
                    if candidate_provider in RECOGNIZED_CONCEPT_PROVENANCE_TYPES:
                        resolved_provider = candidate_provider

            if "paid" in gen_info:
                raw_paid = gen_info["paid"]
                if not isinstance(raw_paid, bool):
                    raise ValidationError(f"generation.paid in {sidecar_file} must be a boolean")
                paid = raw_paid

            if "source_script_sha256" in gen_info:
                raw_script_sha = gen_info["source_script_sha256"]
                if raw_script_sha is not None:
                    if not isinstance(raw_script_sha, str) or not _HEX_64_REGEX.fullmatch(
                        raw_script_sha
                    ):
                        raise ValidationError(
                            f"generation.source_script_sha256 in {sidecar_file} must be a 64-character hex string"
                        )
                    source_script_sha256 = raw_script_sha.lower()

    if source_type == "local_generation" and (
        sidecar_file is None or generation_timestamp == "UNKNOWN" or resolved_provider == "UNKNOWN"
    ):
        raise ValidationError(
            "local_generation provenance requires a matching hashed sidecar with provider and timestamp"
        )

    # 4. Copy to target destination
    target_artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if target_artifact_path.is_symlink() or target_artifact_path.exists():
        raise ValidationError(
            f"Concept destination already exists or is a symlink: {target_artifact_path}"
        )
    try:
        fd = os.open(target_artifact_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(source_bytes)
    except FileExistsError as exc:
        raise ValidationError(
            f"Concept destination already exists: {target_artifact_path}"
        ) from exc
    except OSError as exc:
        raise ValidationError(
            f"Cannot create concept destination {target_artifact_path}: {exc}"
        ) from exc

    image_bytes = source_bytes
    artifact_hash = hashlib.sha256(image_bytes).hexdigest()

    provenance_type = (
        resolved_provider if resolved_provider in RECOGNIZED_CONCEPT_PROVENANCE_TYPES else "UNKNOWN"
    )

    provenance = ConceptProvenance(
        provider=resolved_provider,
        model=resolved_model,
        prompt=resolved_prompt,
        prompt_version="1.0",
        asset_spec_hash=clean_spec_hash,
        generation_timestamp=generation_timestamp,
        imported_at=utc_now_iso(),
        artifact_hash=artifact_hash,
        dimensions={"width": width, "height": height},
        cost_classification=cost_classification.value,
        source_type=source_type,
        sidecar_path=str(sidecar_file) if sidecar_file else None,
        sidecar_hash=sidecar_hash,
        provenance_type=provenance_type,
        paid=paid,
        source_script_sha256=source_script_sha256,
    )
    return target_artifact_path, provenance


def _parse_generation_timestamp(value: object, sidecar_path: Path) -> str:
    try:
        if isinstance(value, bool):
            raise ValueError("boolean timestamp")
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if not math.isfinite(timestamp):
                raise ValueError("timestamp must be finite")
            parsed = datetime.fromtimestamp(timestamp, UTC)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp string must include a timezone")
            parsed = parsed.astimezone(UTC)
        else:
            raise ValueError("timestamp must be an epoch number or timezone-aware ISO-8601 string")
    except (OverflowError, OSError, ValueError) as exc:
        raise ValidationError(
            f"Invalid generation timestamp in sidecar {sidecar_path}: {exc}"
        ) from exc
    return parsed.isoformat()


class FakeImageGenerationProvider(ImageGenerationProvider):
    """Deterministic fake concept image generator for unit and integration tests."""

    def __init__(
        self,
        name: str = "fake_concept_gen",
        cost_class: CostClass = CostClass.LOCAL,
        default_dimensions: tuple[int, int] = (512, 512),
    ) -> None:
        self._name = name
        self._cost_class = cost_class
        self._default_dimensions = default_dimensions
        self.invocation_count = 0
        self.recorded_requests: list[ConceptGenerationRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def cost_class(self) -> CostClass:
        return self._cost_class

    def is_configured(self) -> bool:
        return True

    def generate(self, request: ConceptGenerationRequest) -> ConceptGenerationResponse:
        """Create a deterministic valid PNG artifact and return provenance."""
        self.invocation_count += 1
        self.recorded_requests.append(request)

        width, height = request.dimensions or self._default_dimensions
        request.output_path.parent.mkdir(parents=True, exist_ok=True)

        img = Image.new("RGBA", (width, height), color=(30, 35, 45, 255))
        from PIL import ImageDraw

        draw = ImageDraw.Draw(img)
        margin = 32
        draw.rectangle(
            [margin, margin, width - margin, height - margin],
            outline=(0, 200, 255, 255),
            width=6,
        )
        draw.rectangle(
            [margin + 20, margin + 20, width - margin - 20, height - margin - 20],
            outline=(255, 165, 0, 255),
            width=3,
        )
        img.save(request.output_path, format="PNG")

        image_bytes = request.output_path.read_bytes()
        artifact_hash = hashlib.sha256(image_bytes).hexdigest()
        now = utc_now_iso()

        provenance = ConceptProvenance(
            provider=self._name,
            model="fake_sdxl_v1",
            prompt=request.prompt,
            prompt_version="1.0",
            asset_spec_hash=request.asset_spec_hash,
            generation_timestamp=now,
            imported_at=now,
            artifact_hash=artifact_hash,
            dimensions={"width": width, "height": height},
            cost_classification=self._cost_class.value,
            source_type="local_generation",
            provenance_type=(
                self._name if self._name in RECOGNIZED_CONCEPT_PROVENANCE_TYPES else "UNKNOWN"
            ),
            paid=False,
            source_script_sha256=None,
        )

        return ConceptGenerationResponse(
            artifact_path=request.output_path,
            provenance=provenance,
            cost=0.0,
            cost_unit="credits",
            details={"width": width, "height": height},
        )
