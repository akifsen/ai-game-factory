"""Portability and trust-pin tests for trusted verify_rig_bundle.py sibling loading."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.unit.test_v08_candidate_evidence_cold import (
    REPO,
    VERIFIER_CHECKOUT,
    _build_candidate_evidence_bundle,
    _failure_reason,
    _run_cold,
)

pytestmark = pytest.mark.candidate_slow

_FROZEN_RIG_PATH = REPO / "scripts" / "verify_rig_bundle.py"
_TRUSTED_CRLF_SHA = "3e4849b9b0781d45f805a462400b019732a0d8946bce83c6c883064d3973130e"
_TRUSTED_LF_SHA = "708e87ba18bba6972a332c2135d80c18cd416345bbfc030398c6ca9db4647291"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _rig_crlf_bytes() -> bytes:
    return _FROZEN_RIG_PATH.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _rig_lf_bytes() -> bytes:
    crlf = _rig_crlf_bytes()
    lf = crlf.replace(b"\r\n", b"\n")
    assert lf.replace(b"\n", b"\r\n") == crlf
    return lf


def _candidate_bytes() -> bytes:
    return VERIFIER_CHECKOUT.read_bytes()


def _place_root_scripts(base: Path, *, rig: bytes, candidate: bytes) -> Path:
    scripts = base / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "verify_candidate_bundle.py").write_bytes(candidate)
    (scripts / "verify_rig_bundle.py").write_bytes(rig)
    return scripts / "verify_candidate_bundle.py"


def _place_packaged_scripts(base: Path, *, rig: bytes, candidate: bytes) -> Path:
    packaged = base / "src" / "gamefactory" / "resources" / "scripts"
    packaged.mkdir(parents=True)
    (packaged / "verify_candidate_bundle.py").write_bytes(candidate)
    (packaged / "verify_rig_bundle.py").write_bytes(rig)
    return packaged / "verify_candidate_bundle.py"


def _mirrored_checkout(base: Path, *, rig: bytes, candidate: bytes) -> tuple[Path, Path]:
    root_script = _place_root_scripts(base, rig=rig, candidate=candidate)
    packaged_script = _place_packaged_scripts(base, rig=rig, candidate=candidate)
    return root_script, packaged_script


def _load_verifier_module() -> object:
    spec = importlib.util.spec_from_file_location(
        "verify_candidate_bundle_portability", VERIFIER_CHECKOUT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_frozen_rig_fixture_lengths_and_trust_pins() -> None:
    crlf = _rig_crlf_bytes()
    lf = _rig_lf_bytes()
    assert len(crlf) == 87565
    assert len(lf) == 85512
    assert crlf.count(b"\r\n") == 2053
    assert _sha256(crlf) == _TRUSTED_CRLF_SHA
    assert _sha256(lf) == _TRUSTED_LF_SHA
    mod = _load_verifier_module()
    assert mod.TRUSTED_RIG_VERIFIER_SHA256 == _TRUSTED_CRLF_SHA
    assert mod.TRUSTED_RIG_VERIFIER_SHA256_LF == _TRUSTED_LF_SHA
    assert mod.TRUSTED_RIG_VERIFIER_SHA256_FIXED == frozenset({_TRUSTED_CRLF_SHA, _TRUSTED_LF_SHA})
    wrong_len = lf + b"x"
    assert len(wrong_len) != len(lf)
    assert not mod._trusted_rig_verifier_digest_allowed(wrong_len)


@pytest.mark.parametrize("rig_factory", [_rig_lf_bytes, _rig_crlf_bytes])
def test_isolated_single_sibling_positive(tmp_path: Path, rig_factory: Callable[[], bytes]) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path / "bundle")
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    script = isolated / "verify_candidate_bundle.py"
    script.write_bytes(_candidate_bytes())
    (isolated / "verify_rig_bundle.py").write_bytes(rig_factory())
    code, out, err = _run_cold(bundle, script)
    assert code == 0, out + err


@pytest.mark.parametrize(
    ("rig_factory", "verifier_selector"),
    [
        (_rig_lf_bytes, lambda base: base / "scripts" / "verify_candidate_bundle.py"),
        (
            _rig_lf_bytes,
            lambda base: (
                base
                / "src"
                / "gamefactory"
                / "resources"
                / "scripts"
                / "verify_candidate_bundle.py"
            ),
        ),
        (_rig_crlf_bytes, lambda base: base / "scripts" / "verify_candidate_bundle.py"),
        (
            _rig_crlf_bytes,
            lambda base: (
                base
                / "src"
                / "gamefactory"
                / "resources"
                / "scripts"
                / "verify_candidate_bundle.py"
            ),
        ),
    ],
)
def test_mirrored_checkout_matching_eol_positive(
    tmp_path: Path,
    rig_factory: Callable[[], bytes],
    verifier_selector: Callable[[Path], Path],
) -> None:
    layout = tmp_path / "checkout"
    layout.mkdir()
    rig = rig_factory()
    _mirrored_checkout(layout, rig=rig, candidate=_candidate_bytes())
    bundle = _build_candidate_evidence_bundle(tmp_path / "bundle")
    verifier = verifier_selector(layout)
    code, out, err = _run_cold(bundle, verifier)
    assert code == 0, out + err


def test_mirrored_checkout_root_lf_packaged_crlf_parity_fail(tmp_path: Path) -> None:
    layout = tmp_path / "checkout"
    layout.mkdir()
    candidate = _candidate_bytes()
    _place_root_scripts(layout, rig=_rig_lf_bytes(), candidate=candidate)
    _place_packaged_scripts(layout, rig=_rig_crlf_bytes(), candidate=candidate)
    bundle = _build_candidate_evidence_bundle(tmp_path / "bundle")
    for selector in (
        layout / "scripts" / "verify_candidate_bundle.py",
        layout / "src" / "gamefactory" / "resources" / "scripts" / "verify_candidate_bundle.py",
    ):
        code, out, _ = _run_cold(bundle, selector)
        assert code == 1
        assert "parity mismatch" in _failure_reason(out).lower()


def test_mirrored_checkout_root_crlf_packaged_lf_parity_fail(tmp_path: Path) -> None:
    layout = tmp_path / "checkout"
    layout.mkdir()
    candidate = _candidate_bytes()
    _place_root_scripts(layout, rig=_rig_crlf_bytes(), candidate=candidate)
    _place_packaged_scripts(layout, rig=_rig_lf_bytes(), candidate=candidate)
    bundle = _build_candidate_evidence_bundle(tmp_path / "bundle")
    code, out, _ = _run_cold(bundle, layout / "scripts" / "verify_candidate_bundle.py")
    assert code == 1
    assert "parity mismatch" in _failure_reason(out).lower()


@pytest.mark.parametrize(
    "tampered",
    [
        lambda raw: raw[:100] + (b"#" if raw[100:101] != b"#" else b"!") + raw[101:],
        lambda raw: raw + b"\n",
        lambda raw: raw.replace(b"\r\n", b"\n", 1),
        lambda raw: raw.replace(b"\n", b"\r"),
    ],
    ids=["one_byte", "appended_newline", "mixed_eol", "mac_style_newlines"],
)
def test_tampered_rig_sibling_rejected_before_success(
    tmp_path: Path, tampered: Callable[[bytes], bytes]
) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path / "bundle")
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    script = isolated / "verify_candidate_bundle.py"
    script.write_bytes(_candidate_bytes())
    base = _rig_crlf_bytes()
    bad = tampered(base)
    assert bad not in {_rig_crlf_bytes(), _rig_lf_bytes()}
    (isolated / "verify_rig_bundle.py").write_bytes(bad)
    code, out, _ = _run_cold(bundle, script)
    assert code == 1
    reason = _failure_reason(out).lower()
    assert "digest mismatch" in reason or "parity mismatch" in reason


def test_bundle_unlisted_evil_rig_not_imported(tmp_path: Path) -> None:
    bundle = _build_candidate_evidence_bundle(tmp_path / "bundle")
    marker = tmp_path / "evil_rig_imported.marker"
    evil = f"open(r'{marker}', 'w').write('1')\n".encode()
    (bundle / "verify_rig_bundle.py").write_bytes(evil)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / "verify_candidate_bundle.py").write_bytes(_candidate_bytes())
    (isolated / "verify_rig_bundle.py").write_bytes(_rig_crlf_bytes())
    code, out, _ = _run_cold(bundle, isolated / "verify_candidate_bundle.py")
    assert code == 1
    assert "unlisted" in _failure_reason(out).lower()
    assert not marker.exists()


def test_counterpart_path_recognition(tmp_path: Path) -> None:
    mod = _load_verifier_module()
    checkout = tmp_path / "repo"
    root = checkout / "scripts" / "verify_candidate_bundle.py"
    packaged = (
        checkout / "src" / "gamefactory" / "resources" / "scripts" / "verify_candidate_bundle.py"
    )
    assert mod._counterpart_rig_verifier_path(root) == (
        checkout / "src" / "gamefactory" / "resources" / "scripts" / "verify_rig_bundle.py"
    )
    assert mod._counterpart_rig_verifier_path(packaged) == (
        checkout / "scripts" / "verify_rig_bundle.py"
    )
    assert (
        mod._counterpart_rig_verifier_path(tmp_path / "isolated" / "verify_candidate_bundle.py")
        is None
    )
