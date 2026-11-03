"""Private recovery journal and cross-process lock for isolated reader jobs.

State changes require :meth:`ReaderJobs.locked`; the lock is held by the caller
across the full create/run/recovery lifecycle. ``clear`` records idle only after
the caller has verified cleanup of the same job. This module never discovers,
starts, removes, or otherwise cleans up containers implicitly.

The journal and advisory lock coordinate cooperating processes owned by the same
installation. They are not a boundary against a malicious same-user administrator.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from threading import Event
from typing import BinaryIO, cast

from platform_support import try_file_lock, unlock_file

from . import source_files
from .source_contracts import SourceArchiveError, encode, parse_json
from .source_reader_policy import ReaderRuntime

_JOURNAL_BYTES = 8 * 1024
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_COMMON_FIELDS = {
    "version",
    "phase",
    "docker",
    "socket",
    "owner_id",
    "daemon_id",
    "image_id",
}
_PENDING_FIELDS = _COMMON_FIELDS | {"job_id", "name"}
_BOUND_FIELDS = _PENDING_FIELDS | {"container_id"}


def _uuid(value: object) -> str:
    if not isinstance(value, str):
        raise SourceArchiveError()
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise SourceArchiveError() from None
    if str(parsed) != value:
        raise SourceArchiveError()
    return value


def _container(value: object) -> str:
    if not isinstance(value, str) or _CONTAINER_ID.fullmatch(value) is None:
        raise SourceArchiveError()
    return value


def _reader_root(root: Path) -> None:
    """Require a private non-archive root for reader-owned runtime state."""

    source_files.private_root(root, create=False)
    try:
        (root / "archive.json").lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    raise SourceArchiveError("privacy_rejected")


class ReaderJobs:
    """Own one closed recovery record and its process-wide reservation lock."""

    def __init__(self, runtime: ReaderRuntime):
        if not isinstance(runtime, ReaderRuntime):
            raise SourceArchiveError()
        self.runtime = runtime
        self.root = runtime.state_root
        self.lock_path = self.root / "reader-job.lock"
        self.journal_path = self.root / "reader-job.json"
        self._guard = threading.Lock()
        self._handle: BinaryIO | None = None
        self._thread_id: int | None = None
        self._lock_identity: tuple[int, int] | None = None
        self._journal_identity: tuple[int, int] | None = None
        self._initialize()

    def _idle(self) -> dict[str, object]:
        return {
            "version": 1,
            "phase": "idle",
            "docker": str(self.runtime.docker),
            "socket": str(self.runtime.socket),
            "owner_id": self.runtime.owner_id,
            "daemon_id": self.runtime.daemon_id,
            "image_id": self.runtime.image_id,
        }

    def _initialize(self) -> None:
        """Provision a fresh pair once; an incomplete pair remains unresolved."""

        try:
            _reader_root(self.root)
            try:
                source_files.check_entry(self.lock_path)
                created = False
            except FileNotFoundError:
                try:
                    _reader_root(self.root)
                    source_files.write_bytes(self.lock_path, b"", overwrite=False)
                    created = True
                except FileExistsError:
                    source_files.check_entry(self.lock_path)
                    created = False
            if created:
                _reader_root(self.root)
                source_files.write_bytes(
                    self.journal_path,
                    encode(self._idle()),
                    overwrite=False,
                )
            else:
                source_files.check_entry(self.journal_path)
        except SourceArchiveError:
            raise
        except OSError:
            raise SourceArchiveError("source_unavailable") from None

    def locked(
        self, deadline: float, cancelled: Event | None = None
    ) -> AbstractContextManager[None]:
        """Reserve this runtime until the context exits, deadline, or cancellation."""

        return self._locked(deadline, cancelled)

    @contextmanager
    def _locked(self, deadline: float, cancelled: Event | None) -> Iterator[None]:
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise SourceArchiveError("source_unavailable")
        if cancelled is not None and not callable(getattr(cancelled, "is_set", None)):
            raise SourceArchiveError("source_unavailable")

        acquired_guard = False
        handle: BinaryIO | None = None
        lock_acquired = False
        unlock_failed = False
        try:
            while not self._guard.acquire(blocking=False):
                if self._stopped(float(deadline), cancelled):
                    raise SourceArchiveError("source_unavailable")
                time.sleep(min(0.01, max(0.0, float(deadline) - time.monotonic())))
            acquired_guard = True
            _reader_root(self.root)
            lock_info = source_files.check_entry(self.lock_path)
            descriptor, _ = source_files.descriptor(
                self.lock_path, (lock_info.st_dev, lock_info.st_ino)
            )
            handle = cast(BinaryIO, os.fdopen(descriptor, "rb"))
            while True:
                if self._stopped(float(deadline), cancelled):
                    raise SourceArchiveError("source_unavailable")
                try:
                    try_file_lock(handle, shared=False)
                    lock_acquired = True
                    break
                except BlockingIOError:
                    time.sleep(min(0.01, max(0.0, float(deadline) - time.monotonic())))
                except (OSError, NotImplementedError):
                    raise SourceArchiveError("source_unavailable") from None

            current_lock = source_files.check_entry(self.lock_path)
            opened_lock = os.fstat(handle.fileno())
            identity = (opened_lock.st_dev, opened_lock.st_ino)
            if identity != (lock_info.st_dev, lock_info.st_ino) or identity != (
                current_lock.st_dev,
                current_lock.st_ino,
            ):
                raise SourceArchiveError("source_unavailable")
            journal_info = source_files.check_entry(self.journal_path)
            self._handle = handle
            self._thread_id = threading.get_ident()
            self._lock_identity = identity
            self._journal_identity = (journal_info.st_dev, journal_info.st_ino)
            yield
        except SourceArchiveError:
            raise
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        finally:
            if acquired_guard:
                self._handle = None
                self._thread_id = None
                self._lock_identity = None
                self._journal_identity = None
                if lock_acquired and handle is not None:
                    try:
                        unlock_file(handle)
                    except (OSError, NotImplementedError):
                        unlock_failed = True
                if handle is not None:
                    handle.close()
                self._guard.release()
            if unlock_failed:
                raise SourceArchiveError("source_unavailable") from None

    @staticmethod
    def _stopped(deadline: float, cancelled: Event | None) -> bool:
        return time.monotonic() >= deadline or (cancelled is not None and bool(cancelled.is_set()))

    def _require_lock(self) -> None:
        handle = self._handle
        if (
            handle is None
            or self._thread_id != threading.get_ident()
            or self._lock_identity is None
            or self._journal_identity is None
        ):
            raise SourceArchiveError("source_unavailable")
        try:
            _reader_root(self.root)
            opened = os.fstat(handle.fileno())
            current_lock = source_files.check_entry(self.lock_path)
            if (opened.st_dev, opened.st_ino) != self._lock_identity or (
                current_lock.st_dev,
                current_lock.st_ino,
            ) != self._lock_identity:
                raise SourceArchiveError("source_unavailable")
            current_journal = source_files.check_entry(self.journal_path)
            if (current_journal.st_dev, current_journal.st_ino) != self._journal_identity:
                raise SourceArchiveError("source_unavailable")
        except SourceArchiveError:
            raise
        except OSError:
            raise SourceArchiveError("source_unavailable") from None

    def _read(self) -> dict[str, object]:
        self._require_lock()
        try:
            value = parse_json(
                source_files.read_bytes(self.journal_path, _JOURNAL_BYTES),
                maximum_bytes=_JOURNAL_BYTES,
            )
        except SourceArchiveError:
            raise
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        self._require_lock()
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise SourceArchiveError()
        record = cast(dict[str, object], value)
        phase = record.get("phase")
        expected = (
            _COMMON_FIELDS
            if phase == "idle"
            else _PENDING_FIELDS
            if phase == "pending"
            else _BOUND_FIELDS
            if phase == "bound"
            else set()
        )
        if set(record) != expected or type(record.get("version")) is not int:
            raise SourceArchiveError()
        if record.get("version") != 1 or any(
            record.get(field) != expected_value
            for field, expected_value in (
                ("docker", str(self.runtime.docker)),
                ("socket", str(self.runtime.socket)),
                ("owner_id", self.runtime.owner_id),
                ("daemon_id", self.runtime.daemon_id),
                ("image_id", self.runtime.image_id),
            )
        ):
            raise SourceArchiveError()
        if phase in ("pending", "bound"):
            job_id = _uuid(record.get("job_id"))
            if record.get("name") != "tedd-reader-" + job_id:
                raise SourceArchiveError()
        if phase == "bound":
            _container(record.get("container_id"))
        return record

    def _write(self, record: dict[str, object]) -> None:
        self._require_lock()
        payload = encode(record)
        if len(payload) > _JOURNAL_BYTES:
            raise SourceArchiveError()
        try:
            source_files.write_bytes(self.journal_path, payload, overwrite=True)
            info = source_files.check_entry(self.journal_path)
        except SourceArchiveError:
            raise
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        self._journal_identity = (info.st_dev, info.st_ino)

    def pending(self, job_id: str) -> None:
        """Durably reserve one job before any container-create request."""

        _uuid(job_id)
        record = self._read()
        if record["phase"] != "idle":
            raise SourceArchiveError("source_unavailable")
        pending = {
            **self._idle(),
            "phase": "pending",
            "job_id": job_id,
            "name": "tedd-reader-" + job_id,
        }
        self._write(pending)

    def bind(self, job_id: str, container_id: str) -> None:
        """Durably bind the full container ID before the container is started."""

        _uuid(job_id)
        _container(container_id)
        record = self._read()
        if record["phase"] != "pending" or record.get("job_id") != job_id:
            raise SourceArchiveError("source_unavailable")
        self._write({**record, "phase": "bound", "container_id": container_id})

    def receipts(self) -> tuple[dict[str, object], ...]:
        """Return zero or one active recovery record; idle is never a receipt."""

        record = self._read()
        if record["phase"] == "idle":
            return ()
        return (dict(record),)

    def clear(self, job_id: str) -> None:
        """Record idle after the caller verifies cleanup of this exact job."""

        _uuid(job_id)
        record = self._read()
        if record["phase"] not in ("pending", "bound") or record.get("job_id") != job_id:
            raise SourceArchiveError("source_unavailable")
        self._write(self._idle())
