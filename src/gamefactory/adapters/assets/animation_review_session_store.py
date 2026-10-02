"""Durable local animation review session storage (V0.8-9a).

Guarantees apply only against other writers that use this store with the same
review-root identity and sidecar layout. Session creation publishes via atomic
same-directory rename under the writer lock; coordinated writers never overwrite
an existing session file at the target path. Arbitrary external processes may
still mutate or replace files without coordination.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from gamefactory.adapters.assets.v08_candidate_evidence import candidate_evidence_lexical_unsafe
from gamefactory.adapters.assets.v08_candidate_runtime_json import (
    CandidateRuntimeJsonError,
    parse_strict_runtime_json_object,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.core.domain.animation_review_session import (
    AnimationReviewSession,
    ReviewSetBinding,
    apply_animation_review_session_operation,
    create_animation_review_session,
    parse_animation_review_session_document,
    serialize_animation_review_session,
)
from gamefactory.core.domain.errors import FactoryError, LockError, ValidationError

_MAX_SESSION_DOCUMENT_BYTES = 64 * 1024

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SIDECAR_DIR_NAME = ".animation-review-session"
_SESSION_FILE_NAME = "session.json"
_LOCK_FILE_NAME = "session.lock"
_LOCK_BYTE_LENGTH = 1
_LOCK_ATTEMPTS = 64
_LOCK_SLEEP_SECONDS = 0.02


@dataclass(frozen=True)
class StoredAnimationReviewSession:
    """Historical session payload plus the exact on-disk byte digest."""

    session: AnimationReviewSession
    raw_sha256: str


class AnimationReviewSessionStoreConflictError(FactoryError):
    """Raised when the expected raw session digest does not match durable state."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            message,
            code="ANIMATION_REVIEW_SESSION_STORE_CONFLICT",
            details=details,
        )


def _same_absolute_path(left: Path, right: Path) -> bool:
    if sys.platform == "win32":
        return os.path.normcase(str(left)) == os.path.normcase(str(right))
    return left == right


def _identity_path_string(resolved: Path) -> str:
    text = resolved.as_posix()
    if sys.platform == "win32":
        text = os.path.normcase(text.replace("/", os.sep)).replace(os.sep, "/")
    return text


def canonical_review_root_identity(review_root: Path | str) -> str:
    """Resolved absolute review-root identity string (forward slashes)."""
    lexical = Path(review_root).absolute()
    if candidate_evidence_lexical_unsafe(lexical):
        raise ValidationError("review_root is not a bounded lexical path")
    if path_crosses_link(lexical):
        raise ValidationError("review_root crosses a symlink or junction")
    try:
        resolved = lexical.resolve(strict=False)
    except OSError as exc:
        raise ValidationError(f"review_root cannot be resolved: {exc}") from exc
    if candidate_evidence_lexical_unsafe(resolved) or path_crosses_link(resolved):
        raise ValidationError("review_root resolves through a link or reparse point")
    return _identity_path_string(resolved)


def _review_root_storage_key(review_root_identity: str) -> str:
    return hashlib.sha256(os.path.normcase(review_root_identity).encode("utf-8")).hexdigest()


def _validate_expected_raw_sha256(expected_raw_sha256: str) -> str:
    if not isinstance(expected_raw_sha256, str):
        raise ValidationError("expected_raw_sha256 must be a string")
    if not _SHA256_HEX_RE.fullmatch(expected_raw_sha256):
        raise ValidationError("expected_raw_sha256 must be a 64-character lowercase hex digest")
    return expected_raw_sha256


def _resolved_path_inside_root(path_resolved: Path, root_resolved: Path) -> bool:
    if sys.platform == "win32":
        path_s = os.path.normcase(str(path_resolved))
        root_s = os.path.normcase(str(root_resolved))
        if path_s == root_s:
            return True
        return path_s.startswith(root_s + os.sep)
    try:
        path_resolved.relative_to(root_resolved)
        return True
    except ValueError:
        return False


def _reject_path_under_review_root(path: Path, review_root_lexical: Path, *, label: str) -> None:
    path_lexical = path.absolute()
    root_lexical = review_root_lexical.absolute()
    try:
        path_lexical.relative_to(root_lexical)
        raise ValidationError(f"{label} must be outside the canonical review-set directory")
    except ValueError:
        pass
    try:
        path_resolved = path_lexical.resolve(strict=False)
        root_resolved = root_lexical.resolve(strict=False)
    except OSError as exc:
        raise ValidationError(f"{label} cannot be resolved safely: {exc}") from exc
    if _resolved_path_inside_root(path_resolved, root_resolved):
        raise ValidationError(
            f"{label} must be outside the canonical review-set directory (resolved)"
        )


def _reject_unsafe_mutation_path(path: Path, *, label: str) -> None:
    if candidate_evidence_lexical_unsafe(path):
        raise ValidationError(f"{label} is not a bounded lexical path")
    if path_crosses_link(path):
        raise ValidationError(f"{label} crosses a symlink or junction")
    try:
        resolved = path.resolve(strict=False)
    except OSError as exc:
        raise ValidationError(f"{label} cannot be resolved: {exc}") from exc
    if candidate_evidence_lexical_unsafe(resolved) or path_crosses_link(resolved):
        raise ValidationError(f"{label} resolves through a link or reparse point")


def _not_regular_file_type_message(label: str) -> str:
    return f"{label} must be a regular file, not a link, junction, or reparse point"


def _assert_stat_regular_single_link(info: os.stat_result, *, label: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise ValidationError(_not_regular_file_type_message(label))
    reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
    if reparse:
        raise ValidationError(_not_regular_file_type_message(label))
    if stat.S_ISDIR(info.st_mode):
        raise ValidationError(_not_regular_file_type_message(label))
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError(_not_regular_file_type_message(label))
    if info.st_nlink > 1:
        raise ValidationError(f"{label} must not be a hard link")


def _assert_regular_single_link_file(path: Path, *, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ValidationError(f"{label} does not exist") from None
    except OSError as exc:
        raise ValidationError(f"{label} cannot be inspected: {exc}") from exc
    _assert_stat_regular_single_link(info, label=label)
    return info


def _read_bounded_session_bytes(path: Path) -> bytes:
    entry = _assert_regular_single_link_file(path, label="animation review session file")
    limit = _MAX_SESSION_DOCUMENT_BYTES
    size = entry.st_size
    if size > limit:
        raise ValidationError(
            f"animation review session file exceeds {_MAX_SESSION_DOCUMENT_BYTES} bytes"
        )
    with path.open("rb") as handle:
        fd = handle.fileno()
        opened = os.fstat(fd)
        _assert_stat_regular_single_link(opened, label="animation review session file")
        if opened.st_nlink > 1:
            raise ValidationError("animation review session file must not be a hard link")
        entry_now = path.lstat()
        if (opened.st_dev, opened.st_ino) != (entry_now.st_dev, entry_now.st_ino):
            raise ValidationError("animation review session file identity changed during read")
        if opened.st_size != size:
            raise ValidationError("animation review session file size changed during read")
        payload = handle.read(limit + 1)
        if len(payload) > limit:
            raise ValidationError(
                f"animation review session file exceeds {_MAX_SESSION_DOCUMENT_BYTES} bytes"
            )
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValidationError("animation review session file identity changed during read")
        if after.st_size != opened.st_size:
            raise ValidationError("animation review session file grew during bounded read")
        if len(payload) != after.st_size:
            raise ValidationError("animation review session file grew during bounded read")
        leaf_after = path.lstat()
        if (leaf_after.st_dev, leaf_after.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValidationError("animation review session file identity changed during read")
    return payload


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_session_bytes(raw: bytes) -> AnimationReviewSession:
    try:
        document = parse_strict_runtime_json_object(
            raw,
            max_bytes=_MAX_SESSION_DOCUMENT_BYTES,
        )
    except RecursionError as exc:
        raise ValidationError("animation review session JSON is too deeply nested") from exc
    except CandidateRuntimeJsonError as exc:
        raise ValidationError(str(exc)) from exc
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    try:
        return parse_animation_review_session_document(document)
    except RecursionError as exc:
        raise ValidationError("animation review session document is too deeply nested") from exc
    except ValidationError:
        raise
    except FactoryError as exc:
        raise ValidationError(str(exc)) from exc


def _lock_path_inside_directory(lock_path: Path, lock_dir: Path) -> bool:
    if lock_path.name != _LOCK_FILE_NAME:
        return False
    parent_lexical = lock_path.parent.absolute()
    dir_lexical = lock_dir.absolute()
    if sys.platform == "win32":
        return os.path.normcase(str(parent_lexical)) == os.path.normcase(str(dir_lexical))
    if parent_lexical == dir_lexical:
        return True
    try:
        parent_lexical.resolve(strict=False).relative_to(dir_lexical.resolve(strict=False))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _validate_lock_leaf(lock_path: Path, lock_dir: Path) -> None:
    if not _lock_path_inside_directory(lock_path, lock_dir):
        raise LockError("session lock path resolves outside its directory")
    try:
        info = lock_path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise LockError("session lock path must be a regular single-link file")
    reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
    if reparse:
        raise LockError("session lock path must be a regular single-link file")
    if stat.S_ISDIR(info.st_mode):
        raise LockError("session lock path must be a regular single-link file")
    if not stat.S_ISREG(info.st_mode):
        raise LockError("session lock path must be a regular single-link file")
    if info.st_nlink > 1:
        raise LockError("session lock path must be a regular single-link file")
    if info.st_size > _LOCK_BYTE_LENGTH:
        raise LockError("session lock file exceeds allowed size")


def _validate_open_lock_handle(lock_path: Path, handle: BinaryIO) -> None:
    _validate_lock_leaf(lock_path, lock_path.parent)
    try:
        opened = os.fstat(handle.fileno())
        entry = lock_path.lstat()
    except OSError as exc:
        raise LockError("session lock path changed while opening") from exc
    if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
        raise LockError("session lock path changed while opening")
    if (
        stat.S_ISLNK(opened.st_mode)
        or stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISREG(opened.st_mode)
    ):
        raise LockError("session lock path must be a regular single-link file")
    reparse = bool(getattr(opened, "st_file_attributes", 0) & 0x400)
    if reparse or opened.st_nlink > 1:
        raise LockError("session lock path must be a regular single-link file")
    if opened.st_size > _LOCK_BYTE_LENGTH:
        raise LockError("session lock file exceeds allowed size")


def _ensure_lock_byte_invariant(lock_path: Path, handle: BinaryIO) -> None:
    size = lock_path.stat().st_size
    if size > _LOCK_BYTE_LENGTH:
        raise LockError("session lock file exceeds allowed size")
    if size == 0:
        handle.seek(0)
        handle.write(b"\0")
        handle.flush()
        os.fsync(handle.fileno())
        _validate_open_lock_handle(lock_path, handle)


@contextmanager
def _bounded_exclusive_lock(
    lock_path: Path,
    *,
    revalidate: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Cross-process writer lock; retains the lock inode (never unlinked)."""
    lock_dir = lock_path.parent
    _reject_unsafe_mutation_path(lock_dir, label="session lock directory")
    _reject_unsafe_mutation_path(lock_path, label="session lock file")
    _validate_lock_leaf(lock_path, lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    if revalidate is not None:
        revalidate()
    _validate_lock_leaf(lock_path, lock_dir)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    handle = os.fdopen(descriptor, "r+b")
    acquired = False
    try:
        _validate_open_lock_handle(lock_path, handle)
        _ensure_lock_byte_invariant(lock_path, handle)
        if sys.platform == "win32":
            import msvcrt

            for _ in range(_LOCK_ATTEMPTS):
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTE_LENGTH)
                    acquired = True
                    break
                except OSError:
                    time.sleep(_LOCK_SLEEP_SECONDS)
            if not acquired:
                raise LockError("animation review session writer lock is contended")
        else:
            import fcntl

            for _ in range(_LOCK_ATTEMPTS):
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    time.sleep(_LOCK_SLEEP_SECONDS)
            if not acquired:
                raise LockError("animation review session writer lock is contended")
        if revalidate is not None:
            revalidate()
        _validate_open_lock_handle(lock_path, handle)
        yield
    finally:
        if acquired:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTE_LENGTH)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _stage_session_payload(parent: Path, session_file_name: str, payload: bytes) -> Path:
    token = secrets.token_hex(8)
    temp_path = parent / f".{session_file_name}.{os.getpid()}.{token}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temp_path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return temp_path


def _unlink_owned_temp(temp_path: Path) -> None:
    if temp_path.exists():
        try:
            temp_path.unlink()
        except OSError:
            pass


def _commit_staged_session_create(target: Path, staged: Path) -> None:
    """Publish a new session file by renaming staged into place.

    Same-directory ``os.rename`` exposes a complete readable file with a single
    link. The caller must hold the store writer lock. Among managed writers, the
    lexical absence check immediately before rename prevents overwriting an
    existing target on POSIX; Windows ``rename`` refuses an existing destination.
    Arbitrary external mutators are out of scope for this module.
    """
    owned_temp: Path | None = staged
    try:
        if target.exists():
            raise ValidationError("animation review session already exists")
        os.rename(staged, target)
        owned_temp = None
    finally:
        if owned_temp is not None:
            _unlink_owned_temp(owned_temp)


def _commit_staged_session_replace(target: Path, staged: Path) -> None:
    owned_temp: Path | None = staged
    try:
        os.replace(staged, target)
        owned_temp = None
    finally:
        if owned_temp is not None:
            _unlink_owned_temp(owned_temp)


class AnimationReviewSessionStore:
    """Filesystem-backed durable store for one review-set session sidecar."""

    def __init__(
        self,
        *,
        review_root: Path | str,
        session_path: Path | str | None = None,
    ) -> None:
        self._review_root_lexical = Path(review_root).absolute()
        _reject_unsafe_mutation_path(self._review_root_lexical, label="review_root")
        self._review_root_identity = canonical_review_root_identity(self._review_root_lexical)
        if session_path is None:
            storage_key = _review_root_storage_key(self._review_root_identity)
            sidecar_dir = self._review_root_lexical.parent / _SIDECAR_DIR_NAME / storage_key
            resolved_session = sidecar_dir / _SESSION_FILE_NAME
        else:
            resolved_session = Path(session_path).absolute()
            sidecar_dir = resolved_session.parent
        lock_path = sidecar_dir / _LOCK_FILE_NAME
        if _same_absolute_path(resolved_session, lock_path):
            raise ValidationError("session sidecar file must not be the reserved session lock path")
        _reject_path_under_review_root(
            sidecar_dir,
            self._review_root_lexical,
            label="session sidecar directory",
        )
        _reject_path_under_review_root(
            resolved_session,
            self._review_root_lexical,
            label="session sidecar file",
        )
        _reject_path_under_review_root(
            lock_path,
            self._review_root_lexical,
            label="session lock file",
        )
        _reject_unsafe_mutation_path(sidecar_dir, label="session sidecar directory")
        _reject_unsafe_mutation_path(resolved_session, label="session sidecar file")
        _reject_unsafe_mutation_path(lock_path, label="session lock file")
        self._sidecar_dir = sidecar_dir
        self._session_path = resolved_session
        self._lock_path = lock_path
        self._revalidate_authorized_layout()

    def _revalidate_authorized_layout(self) -> None:
        _reject_unsafe_mutation_path(self._review_root_lexical, label="review_root")
        _reject_path_under_review_root(
            self._sidecar_dir,
            self._review_root_lexical,
            label="session sidecar directory",
        )
        _reject_path_under_review_root(
            self._session_path,
            self._review_root_lexical,
            label="session sidecar file",
        )
        _reject_path_under_review_root(
            self._lock_path,
            self._review_root_lexical,
            label="session lock file",
        )
        _reject_unsafe_mutation_path(self._sidecar_dir, label="session sidecar directory")
        _reject_unsafe_mutation_path(self._session_path, label="session sidecar file")
        _reject_unsafe_mutation_path(self._lock_path, label="session lock file")

    @property
    def review_root_identity(self) -> str:
        return self._review_root_identity

    @property
    def session_path(self) -> Path:
        return self._session_path

    def load(self) -> StoredAnimationReviewSession:
        """Load the durable session without acquiring the writer lock."""
        self._revalidate_authorized_layout()
        _reject_unsafe_mutation_path(self._session_path, label="session sidecar file")
        raw = _read_bounded_session_bytes(self._session_path)
        session = _parse_session_bytes(raw)
        if session.binding.review_root != self._review_root_identity:
            raise ValidationError("stored session review_root does not match review_root identity")
        return StoredAnimationReviewSession(session=session, raw_sha256=_sha256_hex(raw))

    def create(
        self,
        binding: ReviewSetBinding,
        *,
        precommit: Callable[[AnimationReviewSession], None] | None = None,
    ) -> StoredAnimationReviewSession:
        """Create a fresh session file; raises if durable state already exists."""
        session = create_animation_review_session(binding)
        if session.binding.review_root != self._review_root_identity:
            raise ValidationError("binding.review_root does not match review_root identity")
        payload = serialize_animation_review_session(session)
        self._revalidate_authorized_layout()
        with _bounded_exclusive_lock(
            self._lock_path, revalidate=self._revalidate_authorized_layout
        ):
            if self._session_path.exists():
                raise ValidationError("animation review session already exists")
            staged = _stage_session_payload(
                self._sidecar_dir,
                self._session_path.name,
                payload,
            )
            owned_staged: Path | None = staged
            try:
                if precommit is not None:
                    precommit(session)
                self._revalidate_authorized_layout()
                if self._session_path.exists():
                    raise ValidationError("animation review session already exists")
                _commit_staged_session_create(self._session_path, staged)
                owned_staged = None
            finally:
                if owned_staged is not None:
                    _unlink_owned_temp(owned_staged)
        return StoredAnimationReviewSession(session=session, raw_sha256=_sha256_hex(payload))

    def apply_operation(
        self,
        *,
        expected_raw_sha256: str,
        operation: dict[str, Any],
        precommit: Callable[[AnimationReviewSession], None] | None = None,
    ) -> StoredAnimationReviewSession:
        """Apply one domain operation with compare-and-swap on the raw session bytes."""
        expected = _validate_expected_raw_sha256(expected_raw_sha256)
        self._revalidate_authorized_layout()
        _reject_unsafe_mutation_path(self._session_path, label="session sidecar file")
        with _bounded_exclusive_lock(
            self._lock_path, revalidate=self._revalidate_authorized_layout
        ):
            if not self._session_path.is_file():
                raise ValidationError("animation review session does not exist")
            current_raw = _read_bounded_session_bytes(self._session_path)
            current_sha = _sha256_hex(current_raw)
            if current_sha != expected:
                raise AnimationReviewSessionStoreConflictError(
                    "animation review session raw SHA-256 does not match expected value",
                    details={
                        "expected_raw_sha256": expected,
                        "actual_raw_sha256": current_sha,
                    },
                )
            current_session = _parse_session_bytes(current_raw)
            if current_session.binding.review_root != self._review_root_identity:
                raise ValidationError(
                    "stored session review_root does not match review_root identity"
                )
            updated = apply_animation_review_session_operation(current_session, operation)
            staged = _stage_session_payload(
                self._sidecar_dir,
                self._session_path.name,
                serialize_animation_review_session(updated),
            )
            owned_staged: Path | None = staged
            try:
                if precommit is not None:
                    precommit(updated)
                recheck_raw = _read_bounded_session_bytes(self._session_path)
                recheck_sha = _sha256_hex(recheck_raw)
                if recheck_sha != current_sha:
                    raise AnimationReviewSessionStoreConflictError(
                        "animation review session changed before publication",
                        details={
                            "expected_raw_sha256": expected,
                            "actual_raw_sha256": recheck_sha,
                        },
                    )
                self._revalidate_authorized_layout()
                _commit_staged_session_replace(self._session_path, staged)
                owned_staged = None
            finally:
                if owned_staged is not None:
                    _unlink_owned_temp(owned_staged)
        payload = serialize_animation_review_session(updated)
        return StoredAnimationReviewSession(session=updated, raw_sha256=_sha256_hex(payload))
