#!/usr/bin/env python3
"""Shared durable-publication primitives for knowledge-graph storage.

This module deliberately knows nothing about graph schemas, command-line policy,
configuration, or projection.  Versioned callers retain their own staging and
permission-hardening order, then delegate only the filesystem operations whose
correctness contract is identical.
"""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Collection
from pathlib import Path
from typing import Protocol, cast

DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS = frozenset(
    value
    for value in (
        errno.EINVAL,
        errno.ENOSYS,
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
    )
    if value is not None
)


class DirectorySyncOperations(Protocol):
    """Minimal filesystem interface needed to durably sync a directory."""

    name: str
    O_RDONLY: int

    def open(self, path: Path, flags: int) -> int: ...

    def fsync(self, descriptor: int) -> None: ...

    def close(self, descriptor: int) -> None: ...


class FileIdentity(Protocol):
    st_dev: int
    st_ino: int


class PublicationOperations(Protocol):
    """Minimal atomic-publication interface shared by backup and export paths."""

    def stat(self, path: Path) -> FileIdentity: ...

    def replace(self, source: Path, destination: Path) -> None: ...

    def link(self, source: Path, destination: Path) -> None: ...

    def unlink(self, path: Path) -> None: ...


class ReplaceOperations(Protocol):
    name: str

    def replace(self, source: Path, destination: Path) -> None: ...


class RetryClock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


_DIRECTORY_SYNC_OS = cast(DirectorySyncOperations, os)
_PUBLICATION_OS = cast(PublicationOperations, os)
_REPLACE_OS = cast(ReplaceOperations, os)
_RETRY_CLOCK = cast(RetryClock, time)


def fsync_directory(
    path: Path,
    *,
    os_module: DirectorySyncOperations = _DIRECTORY_SYNC_OS,
    unsupported_errnos: Collection[int] = DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS,
) -> None:
    """Persist directory-entry changes when the platform supports it.

    ``os_module`` is an explicit compatibility seam for the existing callers'
    fault-injection tests.  Only an ``fsync`` error whose errno is known to mean
    "unsupported" is suppressed; directory-open, permission, and I/O failures
    remain observable.
    """

    if os_module.name == "nt":
        # CPython cannot open Windows directories as fsync-able descriptors.
        return
    directory_flag = getattr(os_module, "O_DIRECTORY", 0)
    if not isinstance(directory_flag, int):
        raise TypeError("O_DIRECTORY must be an integer flag")
    flags = os_module.O_RDONLY | directory_flag
    descriptor = os_module.open(path, flags)
    try:
        try:
            os_module.fsync(descriptor)
        except OSError as error:
            if error.errno not in unsupported_errnos:
                raise
    except BaseException:
        # Preserve the operation that establishes durability as the primary
        # failure if descriptor cleanup also fails.
        try:
            os_module.close(descriptor)
        except OSError:
            pass
        raise
    else:
        os_module.close(descriptor)


def sqlite_readonly_uri(path: Path) -> str:
    """Return a percent-escaped SQLite URI for one resolved local path."""

    return f"{path.expanduser().resolve().as_uri()}?mode=ro"


def publish_staged_file(
    source: Path,
    destination: Path,
    *,
    overwrite: bool,
    os_module: PublicationOperations = _PUBLICATION_OS,
) -> tuple[int, int]:
    """Publish a same-directory staged file and return its device/inode identity.

    No-overwrite publication uses one hard-link operation, so a racing winner is
    never replaced.  Cleanup remains the caller's responsibility when publication
    fails, which keeps transaction-specific rollback policy outside this leaf.
    """

    source_stat = os_module.stat(source)
    if overwrite:
        os_module.replace(source, destination)
    else:
        os_module.link(source, destination)
        os_module.unlink(source)
    return source_stat.st_dev, source_stat.st_ino


def replace_file_with_retry(
    source: Path,
    destination: Path,
    *,
    os_module: ReplaceOperations = _REPLACE_OS,
    time_module: RetryClock = _RETRY_CLOCK,
    timeout_seconds: float = 0.0,
    poll_seconds: float = 0.0,
    retry_winerrors: Collection[int] = (),
) -> None:
    """Atomically replace a file, optionally retrying Windows sharing conflicts.

    Callers opt into the bounded Windows policy explicitly.  Other platforms and
    callers that supply no retry codes perform exactly one ``replace`` operation.
    """

    if os_module.name != "nt" or not retry_winerrors:
        os_module.replace(source, destination)
        return

    deadline = time_module.monotonic() + timeout_seconds
    while True:
        try:
            os_module.replace(source, destination)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in retry_winerrors:
                raise
            remaining = deadline - time_module.monotonic()
            if remaining <= 0:
                raise
            time_module.sleep(min(poll_seconds, remaining))
            if time_module.monotonic() >= deadline:
                raise
