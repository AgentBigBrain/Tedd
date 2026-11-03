"""Private filesystem boundary for source files, using existing graph primitives.

The archive is local and same-user. This does not defend against an administrator
or another program acting with the owner's unrestricted privileges.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, cast

from knowledge_graph_file_io import fsync_directory, publish_staged_file
from platform_support import (
    MutationGuardUnavailable as MutationGuardUnavailable,
)
from platform_support import (
    delete_guarded_file,
    guarded_file_mutation,
    guarded_input_descriptor,
    is_link_or_reparse_point,
    mode_has_any,
    owner_is_current_user,
    set_private_fd_mode,
    set_private_path_mode,
    try_file_lock,
    unlock_file,
    uses_posix_permissions,
    windows_volume_type,
)

from . import v4_store
from .source_contracts import SourceArchiveError
from .v4_contracts import CLOUD_PATH_MARKERS, NETWORK_FS_TYPES


class _FilePrivacyError(Exception):
    """A descriptor failed ownership, access, or link-count validation."""


class _FileChangedError(Exception):
    """A descriptor's identity, type, or contents changed during verification."""


class _ReadLimitExceeded(Exception):
    """A published file exceeds the content reader's byte bound."""


def _mount_type(path: Path) -> str | None:
    return v4_store.mount_type(
        path,
        dependencies=v4_store.MountDependencies(
            nearest_existing_parent=v4_store.nearest_existing_parent,
            sys_platform_is_macos=v4_store.sys_platform_is_macos,
            windows_volume_type=windows_volume_type,
        ),
    )


def inspect_store_path(path: Path) -> v4_store.StorePathAssessment:
    return v4_store.inspect_store_path(
        path,
        dependencies=v4_store.PathInspectionDependencies(
            network_fs_types=NETWORK_FS_TYPES,
            cloud_path_markers=CLOUD_PATH_MARKERS,
            is_link_or_reparse_point=is_link_or_reparse_point,
            mode_has_any=cast(v4_store.ModeCheck, mode_has_any),
            owner_is_current_user=cast(v4_store.OwnerCheck, owner_is_current_user),
            uses_posix_permissions=uses_posix_permissions,
            mount_type=_mount_type,
            nearest_existing_parent=v4_store.nearest_existing_parent,
        ),
    )


def private_root(root: Path, *, create: bool = False) -> Path:
    path = Path(os.path.abspath(root))
    if not path.parent.is_dir() or not inspect_store_path(path)["safe"]:
        raise SourceArchiveError("privacy_rejected")
    if path == Path(path.anchor) or path == Path.home():
        raise SourceArchiveError("privacy_rejected")
    for parent in (path, *path.parents):
        if is_link_or_reparse_point(parent):
            raise SourceArchiveError("privacy_rejected")
        if any((parent / marker).exists() for marker in (".git", ".obsidian")):
            raise SourceArchiveError("privacy_rejected")
        if parent != path and (parent / "archive.json").exists():
            raise SourceArchiveError("privacy_rejected")
    if create:
        path.mkdir(mode=0o700, exist_ok=False)
        set_private_path_mode(path, 0o700)
    check_entry(path, directory=True)
    if create:
        fsync_directory(path.parent)
    return path


def check_entry(
    path: Path, *, directory: bool = False, recovery_links: bool = False
) -> os.stat_result:
    info = path.lstat()
    if (
        is_link_or_reparse_point(path)
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or not owner_is_current_user(path)
        or mode_has_any(path, 0o077)
        or (not directory and info.st_nlink != 1 and not recovery_links)
    ):
        raise SourceArchiveError("privacy_rejected")
    return info


def directory(path: Path) -> None:
    if not path.exists():
        path.mkdir(mode=0o700)
        set_private_path_mode(path, 0o700)
        fsync_directory(path.parent)
    check_entry(path, directory=True)


def descriptor(path: Path, identity: tuple[int, int]) -> tuple[int, os.stat_result]:
    try:
        return v4_store.published_file_descriptor(
            path,
            identity,
            dependencies=v4_store.PublishedFileDependencies(
                owner_is_current_user=cast(v4_store.OwnerCheck, owner_is_current_user),
                mode_has_any=cast(v4_store.ModeCheck, mode_has_any),
                concurrency_conflict_type=_FileChangedError,
                path_safety_error_type=_FilePrivacyError,
            ),
        )
    except _FilePrivacyError:
        raise SourceArchiveError("privacy_rejected") from None
    except _FileChangedError:
        raise SourceArchiveError("source_unavailable") from None


def read_bytes(path: Path, maximum: int) -> bytes:
    check_entry(path.parent, directory=True)
    before = check_entry(path)
    try:
        return v4_store.read_verified_published_bytes(
            path,
            (before.st_dev, before.st_ino),
            maximum_bytes=maximum,
            dependencies=v4_store.ReadPublishedDependencies(
                published_file_descriptor=descriptor,
                concurrency_conflict_type=_FileChangedError,
                validation_error_type=_ReadLimitExceeded,
            ),
        )
    except _FileChangedError:
        raise SourceArchiveError("source_unavailable") from None
    except _ReadLimitExceeded:
        # Content limits do not turn descriptor trust failures into recoverable
        # corruption. Helper exceptions contain paths, so never expose their text.
        raise SourceArchiveError() from None


def claim_private_generated_file(path: Path) -> None:
    """Claim a helper-created file only if its existing access was already private.

    Windows helpers can use an elevated token's default owner even beneath our
    private output directory. Pin the file against replacement while transferring
    ownership; never harden an output that was broadly accessible first.
    """
    check_entry(path.parent, directory=True)
    try:
        with guarded_input_descriptor(path) as descriptor:
            if (
                os.fstat(descriptor).st_nlink != 1
                or not owner_is_current_user(descriptor, allow_root=True)
                or mode_has_any(descriptor, 0o077)
            ):
                raise SourceArchiveError("privacy_rejected")
            set_private_fd_mode(descriptor, 0o600)
            check_entry(path)
    except (OSError, ValueError):
        raise SourceArchiveError("privacy_rejected") from None


def read_input(path: Path, maximum: int, *, byte_range: tuple[int, int] | None = None) -> bytes:
    """Read one explicitly approved original; never harden or modify its location.

    Inputs may be shared or synced. Archive/config reads use the separate private
    descriptor boundary above. This refuses links, devices, oversized files, and
    a changed descriptor/path identity; it does not crawl the containing folder.
    POSIX ctime also detects in-place writes that restore mtime. Timestamp checks
    are bounded change detection, not an atomic snapshot against an uncooperative
    writer or changes below the filesystem's timestamp resolution. Windows uses
    its existing no-write-sharing handle; its ctime is not a content-change clock.
    """
    path = Path(os.path.abspath(path))
    if type(maximum) is not int or not 1 <= maximum <= 64 * 1024**2:
        raise SourceArchiveError("limit_exceeded")
    if byte_range is not None and (
        type(byte_range) is not tuple
        or len(byte_range) != 2
        or any(type(value) is not int for value in byte_range)
        or not 0 <= byte_range[0] < byte_range[1] <= (1 << 63) - 1
        or byte_range[1] - byte_range[0] > maximum
    ):
        raise SourceArchiveError("limit_exceeded")
    try:
        with guarded_input_descriptor(path) as fd, os.fdopen(fd, "rb", closefd=False) as handle:
            if any(is_link_or_reparse_point(parent) for parent in (path, *path.parents)):
                raise SourceArchiveError("privacy_rejected")
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise SourceArchiveError("privacy_rejected")
            if byte_range is None and before.st_size > maximum:
                raise SourceArchiveError("limit_exceeded")
            if byte_range is not None and byte_range[1] > before.st_size:
                raise SourceArchiveError("capture_incomplete")
            opened = os.fstat(handle.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise SourceArchiveError()
            if byte_range is None:
                expected_size = opened.st_size
                payload = handle.read(maximum + 1)
            else:
                expected_size = byte_range[1] - byte_range[0]
                handle.seek(byte_range[0])
                payload = handle.read(expected_size)
            after = os.fstat(handle.fileno())
            current = path.lstat()
            for info in (after, current):
                if (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns if os.name != "nt" else None,
                ) != (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_size,
                    opened.st_mtime_ns,
                    opened.st_ctime_ns if os.name != "nt" else None,
                ):
                    raise SourceArchiveError()
            if len(payload) > maximum:
                raise SourceArchiveError("limit_exceeded")
            if len(payload) != expected_size:
                raise SourceArchiveError()
            return payload
    except OSError:
        raise SourceArchiveError("source_unavailable") from None


def stage(path: Path, payload: bytes) -> Path:
    """Keep the hardened descriptor open through write and durability checks."""
    check_entry(path.parent, directory=True)
    fd, raw = tempfile.mkstemp(prefix=".stage-", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            set_private_fd_mode(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        check_entry(temporary)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_bytes(path: Path, payload: bytes, *, overwrite: bool = False) -> None:
    temporary = stage(path, payload)
    try:
        identity = publish_staged_file(temporary, path, overwrite=overwrite)
        fsync_directory(path.parent)
        fd, _ = descriptor(path, identity)
        os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def mutate_owned_preview(
    path: Path, identity: tuple[int, int], expected: bytes, replacement: bytes | None
) -> bool:
    return _mutate_owned_file(path, identity, expected, replacement, private=True)


def remove_confirmed_export(path: Path, identity: tuple[int, int], expected: bytes) -> bool:
    """Remove only a same-owner exact export under the approved shared-cache exception.

    This is delete-only. It cannot rewrite a shared file or relax preview privacy.
    Unsupported guarded deletion remains an explicit incomplete-cleanup result.
    """
    return _mutate_owned_file(path, identity, expected, None, private=False)


def _mutate_owned_file(
    path: Path,
    identity: tuple[int, int],
    expected: bytes,
    replacement: bytes | None,
    *,
    private: bool,
) -> bool:
    """Mutate an authenticated preview only while its exact object is guarded.

    Stage a rewrite first. After guarded deletion, publication is no-clobber:
    a new owner-created destination wins and leaves cleanup incomplete. There is
    deliberately no unconditional replacement, rollback overwrite, or POSIX
    advisory-lock fallback. A sync failure propagates before success is counted.
    """
    temporary: Path | None = None
    try:
        with guarded_file_mutation(path) as fd:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != identity:
                return False
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or is_link_or_reparse_point(path)
                or not owner_is_current_user(fd)
                or (private and mode_has_any(fd, 0o077))
            ):
                raise SourceArchiveError("privacy_rejected")
            if opened.st_size != len(expected):
                return False
            chunks: list[bytes] = []
            remaining = len(expected) + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if b"".join(chunks) != expected:
                return False
            if replacement is not None:
                temporary = stage(path, replacement)
            delete_guarded_file(fd)
        # Closing the pinned handle completes its deletion. Never unlink(path).
        fsync_directory(path.parent)
        if temporary is None and path.exists():
            # A compatible reader may keep deletion pending, or an owner may
            # already have created a new destination. Neither is cleaned.
            raise FileExistsError("preview removal incomplete")
        if temporary is not None:
            published = publish_staged_file(temporary, path, overwrite=False)
            fsync_directory(path.parent)
            published_fd, _ = descriptor(path, published)
            os.close(published_fd)
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def locked(root: Path, *, shared: bool) -> Iterator[None]:
    private_root(root)
    path = root / "archive.lock"
    info = check_entry(path)
    fd, _ = descriptor(path, (info.st_dev, info.st_ino))
    # Windows LockFileEx works on read-only handles too; no lock-file writes.
    with os.fdopen(fd, "rb") as handle:
        lock_handle = cast(BinaryIO, handle)
        deadline = time.monotonic() + 5.0
        while True:
            try:
                try_file_lock(lock_handle, shared=shared)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SourceArchiveError("source_unavailable") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            unlock_file(lock_handle)
