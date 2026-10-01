"""Reviewed resource pins for V0.8-3B candidate capsule runtime (ADR 0021)."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

PINNED_PACKAGED_PROFILE_DOCUMENT_HASH = (
    "dffd7f9f61d3f524e042f4d6aa6882ec7de703d5886243a7f4a69cb1b3a4d08c"
)
PINNED_RUNTIME_CONTRACT_SHA256 = "d814f0bca505e53f0966c4eeb04bf40bb79bfe424bd7fa1c414e66d2db5f3572"
PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256 = (
    "9977cbc306389f178dc939603b9687f4c367e46f19c8bbbd6917dc39142eed27"
)


def packaged_candidate_harness_path() -> Path:
    return Path(
        str(resources.files("gamefactory.resources.godot").joinpath("candidate_capsule_harness.gd"))
    )


def assert_packaged_harness_path(harness_path: Path) -> None:
    if harness_path.resolve() != packaged_candidate_harness_path().resolve():
        raise ValueError(
            "candidate runtime requires the packaged reviewed candidate_capsule_harness.gd"
        )
