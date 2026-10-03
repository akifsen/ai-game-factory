"""V0.8-9a durable animation review session store."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.assets.animation_review_session_store import (
    AnimationReviewSessionStore,
    AnimationReviewSessionStoreConflictError,
    StoredAnimationReviewSession,
    canonical_review_root_identity,
)
from gamefactory.core.domain.animation_review_session import (
    ReviewSetBinding,
    ReviewSetClipBinding,
    create_animation_review_session,
    serialize_animation_review_session,
)
from gamefactory.core.domain.errors import LockError, ValidationError

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64
_DIGEST_D = "d" * 64


def _clip(clip_id: str, *, duration: float = 1.0, raw: str = _DIGEST_A) -> ReviewSetClipBinding:
    return ReviewSetClipBinding(
        clip_id=clip_id,
        raw_clip_sha256=raw,
        duration_seconds=duration,
    )


def _binding_for_review_root(review_root: Path) -> ReviewSetBinding:
    identity = canonical_review_root_identity(review_root)
    return ReviewSetBinding(
        review_root=identity,
        raw_manifest_sha256=_DIGEST_A,
        root_payload_sha256=_DIGEST_B,
        clip_payload_sha256=_DIGEST_D,
        clips=(_clip("IdleA"), _clip("WalkB", raw=_DIGEST_C)),
    )


def _seed_review_tree(review_root: Path) -> dict[str, bytes]:
    review_root.mkdir(parents=True, exist_ok=True)
    files = {
        "manifest.json": b'{"clips":["IdleA","WalkB"]}',
        "clips/IdleA.json": b'{"clip_id":"IdleA"}',
        "clips/WalkB.json": b'{"clip_id":"WalkB"}',
    }
    for relative, payload in files.items():
        path = review_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return {str(review_root / rel): payload for rel, payload in files.items()}


def _snapshot_review_tree(review_root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(review_root.rglob("*")):
        if path.is_file():
            out[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _require_symlink(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"platform cannot create symlink: {exc}")


def _store_for(
    review_root: Path,
    *,
    session_path: Path | None = None,
) -> AnimationReviewSessionStore:
    if session_path is None:
        return AnimationReviewSessionStore(review_root=review_root)
    return AnimationReviewSessionStore(review_root=review_root, session_path=session_path)


def test_create_load_and_reopen(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    binding = _binding_for_review_root(review_root)
    created = store.create(binding)
    assert created.session.revision == 0
    assert (
        created.raw_sha256
        == hashlib.sha256(serialize_animation_review_session(created.session)).hexdigest()
    )

    loaded = store.load()
    assert loaded.session == created.session
    assert loaded.raw_sha256 == created.raw_sha256

    reopened = _store_for(review_root)
    again = reopened.load()
    assert again == loaded


def test_status_note_and_bookmark_persistence(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    created = store.create(_binding_for_review_root(review_root))
    sha = created.raw_sha256

    updated = store.apply_operation(
        expected_raw_sha256=sha,
        operation={"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
    )
    updated = store.apply_operation(
        expected_raw_sha256=updated.raw_sha256,
        operation={"op": "SetNote", "clip_id": "WalkB", "note": "polish"},
    )
    updated = store.apply_operation(
        expected_raw_sha256=updated.raw_sha256,
        operation={"op": "AddBookmark", "clip_id": "WalkB", "timestamp": 0.25},
    )
    loaded = store.load()
    assert loaded.session.revision == 3
    assert loaded.session.clip_records[0].status == "keep"
    assert loaded.session.clip_records[1].note == "polish"
    assert loaded.session.clip_records[1].bookmarks == (0.25,)
    assert loaded.raw_sha256 == updated.raw_sha256


def test_raw_sha_cas_requires_exact_bytes_including_whitespace(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    created = store.create(_binding_for_review_root(review_root))
    canonical = serialize_animation_review_session(created.session)
    padded = canonical + b" "
    store.session_path.write_bytes(padded)
    padded_sha = hashlib.sha256(padded).hexdigest()

    loaded = store.load()
    assert loaded.raw_sha256 == padded_sha
    assert loaded.session.revision == 0

    with pytest.raises(AnimationReviewSessionStoreConflictError):
        store.apply_operation(
            expected_raw_sha256=created.raw_sha256,
            operation={"op": "SetStatus", "clip_id": "IdleA", "status": "revise"},
        )

    updated = store.apply_operation(
        expected_raw_sha256=padded_sha,
        operation={"op": "SetStatus", "clip_id": "IdleA", "status": "revise"},
    )
    assert updated.session.clip_records[0].status == "revise"
    assert (
        updated.raw_sha256
        == hashlib.sha256(serialize_animation_review_session(updated.session)).hexdigest()
    )


def test_stale_expected_sha_conflict_preserves_bytes(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    store.create(_binding_for_review_root(review_root))
    before = store.session_path.read_bytes()

    stale = "f" * 64
    with pytest.raises(AnimationReviewSessionStoreConflictError):
        store.apply_operation(
            expected_raw_sha256=stale,
            operation={"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
        )
    assert store.session_path.read_bytes() == before

    with pytest.raises(ValidationError, match="lowercase hex"):
        store.apply_operation(
            expected_raw_sha256="NOT_A_VALID_DIGEST",
            operation={"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
        )


def test_rejects_malformed_invalid_and_oversized_session_file(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    store._sidecar_dir.mkdir(parents=True, exist_ok=True)

    store.session_path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValidationError, match="UTF-8|invalid"):
        store.load()

    store.session_path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ValidationError, match="JSON"):
        store.load()

    store.session_path.write_text('{"a": 1, "a": 2}', encoding="utf-8")
    with pytest.raises(ValidationError, match="duplicate"):
        store.load()

    store.session_path.write_text('{"x": NaN}', encoding="utf-8")
    with pytest.raises(ValidationError, match="constant|finite|JSON"):
        store.load()

    oversized = b"x" * (64 * 1024 + 1)
    store.session_path.write_bytes(oversized)
    with pytest.raises(ValidationError, match="exceeds"):
        store.load()


def test_create_rejects_existing_session(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    store.create(_binding_for_review_root(review_root))
    with pytest.raises(ValidationError, match="already exists"):
        store.create(_binding_for_review_root(review_root))


def test_precommit_abort_preserves_previous_bytes(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    created = store.create(_binding_for_review_root(review_root))
    before = store.session_path.read_bytes()

    def _abort(_session: object) -> None:
        raise ValidationError("precommit rejected publication")

    with pytest.raises(ValidationError, match="precommit"):
        store.apply_operation(
            expected_raw_sha256=created.raw_sha256,
            operation={"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
            precommit=_abort,
        )
    assert store.session_path.read_bytes() == before


def test_replace_failure_preserves_old_bytes_and_cleans_temp(
    tmp_path: Path, monkeypatch: Any
) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    created = store.create(_binding_for_review_root(review_root))
    before = store.session_path.read_bytes()

    original_replace = os.replace

    def _fail_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) == store.session_path:
            raise OSError("injected replace failure")
        original_replace(src, dst)

    monkeypatch.setattr(os, "replace", _fail_replace)
    with pytest.raises(OSError, match="injected"):
        store.apply_operation(
            expected_raw_sha256=created.raw_sha256,
            operation={"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
        )
    assert store.session_path.read_bytes() == before
    temps = list(store._sidecar_dir.glob(f".{store.session_path.name}.*.tmp"))
    assert temps == []


def test_canonical_review_tree_bytes_unchanged(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    before = _snapshot_review_tree(review_root)
    store = _store_for(review_root)
    created = store.create(_binding_for_review_root(review_root))
    current = created
    for operation in (
        {"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
        {"op": "SetNote", "clip_id": "WalkB", "note": "note"},
        {"op": "AddBookmark", "clip_id": "WalkB", "timestamp": 0.5},
    ):
        current = store.apply_operation(
            expected_raw_sha256=current.raw_sha256,
            operation=operation,
        )
    assert _snapshot_review_tree(review_root) == before
    assert not store.session_path.is_relative_to(review_root)


def _concurrent_apply_worker(
    review_root: str,
    expected_sha: str,
    barrier: Any,
    results: Any,
) -> None:
    store = AnimationReviewSessionStore(review_root=review_root)
    barrier.wait()
    try:
        store.apply_operation(
            expected_raw_sha256=expected_sha,
            operation={"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
        )
        results.put("ok")
    except AnimationReviewSessionStoreConflictError:
        results.put("conflict")
    except Exception as exc:  # pragma: no cover - surfaced by parent
        results.put(f"error:{type(exc).__name__}:{exc}")


def test_two_processes_with_same_expected_sha_yield_one_success(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    created = store.create(_binding_for_review_root(review_root))

    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    results: Any = ctx.Queue()
    processes = [
        ctx.Process(
            target=_concurrent_apply_worker,
            args=(str(review_root), created.raw_sha256, barrier, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    outcomes = sorted(results.get(timeout=15) for _ in processes)
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 1
    loaded = store.load()
    assert loaded.session.revision == 1
    assert loaded.session.clip_records[0].status == "keep"


def test_rejects_session_symlink_leaf(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    store._sidecar_dir.mkdir(parents=True, exist_ok=True)
    real = store._sidecar_dir / "real-session.json"
    real.write_text("{}", encoding="utf-8")
    if store.session_path.exists():
        store.session_path.unlink()
    _require_symlink(store.session_path, real)
    with pytest.raises(ValidationError, match="link|junction|regular|not a bounded lexical path"):
        store.load()


def test_rejects_session_hardlink_leaf(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    store._sidecar_dir.mkdir(parents=True, exist_ok=True)
    store.session_path.write_text("{}", encoding="utf-8")
    other = store._sidecar_dir / "other-session.json"
    os.link(store.session_path, other)
    with pytest.raises(ValidationError, match="hard link"):
        store.load()


def test_rejects_symlink_review_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-review-set"
    _seed_review_tree(real_root)
    link_root = tmp_path / "linked-review-set"
    _require_symlink(link_root, real_root, target_is_directory=True)
    with pytest.raises(ValidationError, match="link|junction|reparse|not a bounded lexical path"):
        AnimationReviewSessionStore(review_root=link_root)


def test_binding_review_root_mismatch_rejected(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    binding = _binding_for_review_root(review_root)
    wrong = ReviewSetBinding(
        review_root="/other/root",
        raw_manifest_sha256=binding.raw_manifest_sha256,
        root_payload_sha256=binding.root_payload_sha256,
        clip_payload_sha256=binding.clip_payload_sha256,
        clips=binding.clips,
    )
    with pytest.raises(ValidationError, match="review_root"):
        store.create(wrong)
    assert not store._sidecar_dir.exists()


def test_create_invalid_binding_rejected_without_sidecar_io(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    binding = _binding_for_review_root(review_root)
    invalid = ReviewSetBinding(
        review_root=binding.review_root,
        raw_manifest_sha256="not-a-valid-digest",
        root_payload_sha256=binding.root_payload_sha256,
        clip_payload_sha256=binding.clip_payload_sha256,
        clips=binding.clips,
    )
    with pytest.raises(ValidationError, match="sha256|digest|hex"):
        store.create(invalid)
    assert not store._sidecar_dir.exists()
    assert not store._lock_path.exists()


def test_load_rejects_wrong_stored_review_root(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    session = create_animation_review_session(
        ReviewSetBinding(
            review_root="/wrong/identity",
            raw_manifest_sha256=_DIGEST_A,
            root_payload_sha256=_DIGEST_B,
            clip_payload_sha256=_DIGEST_D,
            clips=(_clip("IdleA"), _clip("WalkB", raw=_DIGEST_C)),
        )
    )
    store._sidecar_dir.mkdir(parents=True, exist_ok=True)
    store.session_path.write_bytes(serialize_animation_review_session(session))
    with pytest.raises(ValidationError, match="review_root"):
        store.load()


def test_lock_file_retained_after_operations(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    created = store.create(_binding_for_review_root(review_root))
    lock_inode = store._lock_path.stat().st_ino
    store.apply_operation(
        expected_raw_sha256=created.raw_sha256,
        operation={"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
    )
    assert store._lock_path.exists()
    assert store._lock_path.stat().st_ino == lock_inode


def test_contended_lock_raises_lock_error(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    created = store.create(_binding_for_review_root(review_root))
    store._lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(store._lock_path, flags, 0o600)
    handle = os.fdopen(fd, "r+b")
    try:
        if sys.platform == "win32":
            if store._lock_path.stat().st_size == 0:
                handle.write(b"\0")
                handle.flush()
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        with pytest.raises(LockError, match="contended|locked"):
            store.apply_operation(
                expected_raw_sha256=created.raw_sha256,
                operation={"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
            )
    finally:
        handle.close()


def test_sibling_review_roots_use_distinct_default_session_paths(tmp_path: Path) -> None:
    parent = tmp_path / "pack"
    review_a = parent / "review-a"
    review_b = parent / "review-b"
    _seed_review_tree(review_a)
    _seed_review_tree(review_b)
    store_a = _store_for(review_a)
    store_b = _store_for(review_b)
    assert store_a.session_path != store_b.session_path
    store_a.create(_binding_for_review_root(review_a))
    store_b.create(_binding_for_review_root(review_b))
    assert store_a.load().session.binding.review_root != store_b.load().session.binding.review_root


def test_explicit_session_path_outside_review_root(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    explicit = tmp_path / "custom-sidecar" / "session.json"
    store = _store_for(review_root, session_path=explicit)
    assert store.session_path == explicit.absolute()
    created = store.create(_binding_for_review_root(review_root))
    assert explicit.read_bytes() == serialize_animation_review_session(created.session)


def test_rejects_session_path_inside_review_tree(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    inside = review_root / "sidecar" / "session.json"
    with pytest.raises(ValidationError, match="outside"):
        AnimationReviewSessionStore(review_root=review_root, session_path=inside)


def test_rejects_session_path_equal_to_reserved_lock_path(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    lock_path = tmp_path / "custom-sidecar" / "session.lock"
    with pytest.raises(ValidationError, match="lock path"):
        AnimationReviewSessionStore(review_root=review_root, session_path=lock_path)
    assert not lock_path.parent.exists()


def test_load_rejects_json_integer_exceeding_python_digit_limit(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    store._sidecar_dir.mkdir(parents=True, exist_ok=True)
    huge_revision = "1" * 5000
    store.session_path.write_text(f'{{"revision": {huge_revision}}}', encoding="utf-8")
    with pytest.raises(ValidationError):
        store.load()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path identity normalization")
def test_windows_review_root_identity_is_normcase_stable(tmp_path: Path) -> None:
    review_root = tmp_path / "Review-Set"
    _seed_review_tree(review_root)
    mixed = tmp_path / "review-set"
    identity_a = canonical_review_root_identity(review_root)
    identity_b = canonical_review_root_identity(mixed)
    assert identity_a == identity_b
    store_a = _store_for(review_root)
    store_b = _store_for(mixed)
    assert store_a.review_root_identity == store_b.review_root_identity
    assert store_a.session_path == store_b.session_path


def test_create_precommit_abort_leaves_session_absent(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)

    def _abort(_session: object) -> None:
        raise ValidationError("precommit rejected creation")

    with pytest.raises(ValidationError, match="precommit"):
        store.create(_binding_for_review_root(review_root), precommit=_abort)
    assert not store.session_path.exists()
    temps = list(store._sidecar_dir.glob(f".{store.session_path.name}.*.tmp"))
    assert temps == []


def test_create_staging_keeps_final_target_absent_during_write(
    tmp_path: Path, monkeypatch: Any
) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    observed_absent = False
    original_fdopen = os.fdopen

    def _fdopen_checking_absent(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = original_fdopen(fd, mode, *args, **kwargs)
        if mode == "wb":
            original_write = handle.write

            def _write_checking_absent(data: bytes) -> int:
                nonlocal observed_absent
                observed_absent = not store.session_path.exists()
                return original_write(data)

            handle.write = _write_checking_absent  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr(os, "fdopen", _fdopen_checking_absent)
    store.create(_binding_for_review_root(review_root))
    assert observed_absent
    assert store.session_path.is_file()


def test_create_rename_exposes_readable_single_link_before_return(
    tmp_path: Path, monkeypatch: Any
) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    binding = _binding_for_review_root(review_root)
    original_rename = os.rename
    mid_commit_loaded: StoredAnimationReviewSession | None = None

    def _rename_then_load(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        original_rename(src, dst)
        if Path(dst) == store.session_path:
            nonlocal mid_commit_loaded
            assert Path(dst).lstat().st_nlink == 1
            mid_commit_loaded = store.load()

    monkeypatch.setattr(os, "rename", _rename_then_load)
    created = store.create(binding)
    assert mid_commit_loaded is not None
    assert mid_commit_loaded.session == created.session
    assert mid_commit_loaded.raw_sha256 == created.raw_sha256


def test_failed_create_rename_cleans_owned_temp_only(tmp_path: Path, monkeypatch: Any) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    original_rename = os.rename

    def _fail_rename(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst) == store.session_path:
            raise OSError("injected rename failure")
        original_rename(src, dst)

    monkeypatch.setattr(os, "rename", _fail_rename)
    with pytest.raises(OSError, match="injected"):
        store.create(_binding_for_review_root(review_root))
    assert not store.session_path.exists()
    temps = list(store._sidecar_dir.glob(f".{store.session_path.name}.*.tmp"))
    assert temps == []


def _concurrent_create_worker(
    review_root: str,
    barrier: Any,
    results: Any,
) -> None:
    store = AnimationReviewSessionStore(review_root=review_root)
    binding = _binding_for_review_root(Path(review_root))
    barrier.wait()
    try:
        store.create(binding)
        results.put("ok")
    except ValidationError:
        results.put("exists")
    except Exception as exc:  # pragma: no cover
        results.put(f"error:{type(exc).__name__}:{exc}")


def test_two_processes_concurrent_create_yield_one_success(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    results: Any = ctx.Queue()
    processes = [
        ctx.Process(target=_concurrent_create_worker, args=(str(review_root), barrier, results))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    outcomes = sorted(results.get(timeout=15) for _ in processes)
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert outcomes.count("ok") == 1
    assert outcomes.count("exists") == 1
    store = _store_for(review_root)
    assert store.session_path.is_file()


def test_load_rejects_deeply_nested_json(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    store._sidecar_dir.mkdir(parents=True, exist_ok=True)
    depth = 4000
    nested = b'{"a":' * depth + b"0" + b"}" * depth
    store.session_path.write_bytes(nested)
    with pytest.raises(ValidationError):
        store.load()


def test_load_rejects_simulated_post_read_growth(tmp_path: Path, monkeypatch: Any) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    created = store.create(_binding_for_review_root(review_root))
    payload = store.session_path.read_bytes()
    assert created.raw_sha256 == hashlib.sha256(payload).hexdigest()
    original_fstat = os.fstat
    seen_fds: set[int] = set()

    def _fstat_grow_after_read(fd: int) -> os.stat_result:
        info = original_fstat(fd)
        if fd in seen_fds:
            mutated = list(info)
            mutated[6] = info.st_size + 1
            return os.stat_result(tuple(mutated))
        seen_fds.add(fd)
        return info

    monkeypatch.setattr(os, "fstat", _fstat_grow_after_read)
    with pytest.raises(ValidationError, match="grew"):
        store.load()


def test_rejects_oversized_lock_file(tmp_path: Path) -> None:
    review_root = tmp_path / "review-set"
    _seed_review_tree(review_root)
    store = _store_for(review_root)
    store._sidecar_dir.mkdir(parents=True, exist_ok=True)
    store._lock_path.write_bytes(b"\0\0")
    with pytest.raises(LockError, match="exceeds|allowed"):
        store.create(_binding_for_review_root(review_root))
