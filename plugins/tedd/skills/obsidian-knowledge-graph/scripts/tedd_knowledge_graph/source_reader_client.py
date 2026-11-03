"""Private Docker client shared by warm and cold reader admission."""

from __future__ import annotations

import ctypes
import os
import stat
import sys
from pathlib import Path
from threading import Event
from typing import cast

from platform_support import owner_is_current_user

from . import source_files
from .source_contracts import READING_BYTES, SourceArchiveError, parse_json
from .source_reader_policy import ReaderRuntime
from .source_reader_process import ProcessResult, _ManagedProcess, _start_bounded, run_bounded

_CONTROL_BYTES = 1024 * 1024
_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
_MNT_IGNORE_OWNERSHIP = 0x00200000


class _DarwinFsid(ctypes.Structure):
    _fields_ = [("value", ctypes.c_int32 * 2)]


class _DarwinStatfs(ctypes.Structure):
    _fields_ = [
        ("block_size", ctypes.c_uint32),
        ("io_size", ctypes.c_int32),
        ("blocks", ctypes.c_uint64),
        ("blocks_free", ctypes.c_uint64),
        ("blocks_available", ctypes.c_uint64),
        ("files", ctypes.c_uint64),
        ("files_free", ctypes.c_uint64),
        ("filesystem_id", _DarwinFsid),
        ("owner", ctypes.c_uint32),
        ("filesystem_type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("subtype", ctypes.c_uint32),
        ("filesystem_name", ctypes.c_char * 16),
        ("mounted_on", ctypes.c_char * 1024),
        ("mounted_from", ctypes.c_char * 1024),
        ("flags_extended", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 7),
    ]


def _darwin_mount_flags(path: Path) -> int:
    try:
        library = ctypes.CDLL(None, use_errno=True)
        call = library.statfs
        call.argtypes = [ctypes.c_char_p, ctypes.POINTER(_DarwinStatfs)]
        call.restype = ctypes.c_int
        result = _DarwinStatfs()
        if call(os.fsencode(path), ctypes.byref(result)) != 0:
            raise OSError(ctypes.get_errno(), "statfs")
        return int(result.flags)
    except (AttributeError, OSError, TypeError, ValueError):
        raise SourceArchiveError("privacy_rejected") from None


def _ownership_enforced(path: Path) -> None:
    if sys.platform == "darwin" and _darwin_mount_flags(path) & _MNT_IGNORE_OWNERSHIP:
        raise SourceArchiveError("privacy_rejected")


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _trusted_parents(path: Path) -> None:
    for parent in (path.parent, *path.parent.parents):
        try:
            info = parent.lstat()
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or not owner_is_current_user(parent, allow_root=True)
            or info.st_mode & 0o022
        ):
            raise SourceArchiveError("privacy_rejected")


def _binary_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    _trusted_parents(path)
    try:
        info = path.lstat()
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or not owner_is_current_user(path, allow_root=True)
        or info.st_mode & 0o022
        or not info.st_mode & 0o111
    ):
        raise SourceArchiveError("privacy_rejected")
    return _identity(info)


def _socket_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    _trusted_parents(path)
    try:
        info = path.lstat()
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISSOCK(info.st_mode)
        or not owner_is_current_user(path)
        or info.st_mode & 0o077
    ):
        raise SourceArchiveError("privacy_rejected")
    return _identity(info)


def _docker_config(runtime: ReaderRuntime) -> Path:
    root: Path = Path(runtime.state_root) / "docker-config"
    try:
        info = root.lstat()
    except FileNotFoundError:
        return root
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or not owner_is_current_user(root)
        or info.st_mode & 0o077
    ):
        raise SourceArchiveError("privacy_rejected")
    config = root / "config.json"
    try:
        payload = source_files.read_bytes(config, 16)
    except FileNotFoundError:
        return root
    if payload not in (b"{}", b"{}\n"):
        raise SourceArchiveError("privacy_rejected")
    return root


class _RuntimeClient:
    def __init__(self, runtime: ReaderRuntime):
        _ownership_enforced(runtime.state_root)
        _ownership_enforced(runtime.socket)
        _ownership_enforced(runtime.docker)
        source_files.private_root(runtime.state_root, create=False)
        self.runtime = runtime
        self.config = _docker_config(runtime)
        self._binary = _binary_identity(runtime.docker)
        self._socket = _socket_identity(runtime.socket)

    def _check_identity(self) -> None:
        _ownership_enforced(self.runtime.state_root)
        _ownership_enforced(self.runtime.socket)
        _ownership_enforced(self.runtime.docker)
        if (
            _binary_identity(self.runtime.docker) != self._binary
            or _socket_identity(self.runtime.socket) != self._socket
        ):
            raise SourceArchiveError("source_unavailable")

    def arguments(self, *arguments: str) -> tuple[str, ...]:
        return (
            str(self.runtime.docker),
            "--config",
            str(self.config),
            "--host",
            "unix://" + str(self.runtime.socket),
            *arguments,
        )

    def control(
        self,
        arguments: tuple[str, ...],
        deadline: float,
        cancelled: Event | None = None,
    ) -> ProcessResult:
        self._check_identity()
        try:
            return run_bounded(
                self.arguments(*arguments),
                b"",
                deadline=deadline,
                output_limit=_CONTROL_BYTES,
                cancelled=cancelled,
                environment=_ENVIRONMENT,
            )
        finally:
            self._check_identity()

    def start(
        self,
        arguments: tuple[str, ...],
        payload: bytes,
        deadline: float,
        cancelled: Event | None,
    ) -> _ManagedProcess:
        self._check_identity()
        process = _start_bounded(
            self.arguments(*arguments),
            payload,
            output_limit=READING_BYTES,
            environment=_ENVIRONMENT,
            deadline=deadline,
            cancelled=cancelled,
        )
        try:
            self._check_identity()
        except BaseException:
            process.terminate_and_reap(deadline)
            raise
        return process

    def daemon(self, deadline: float, cancelled: Event | None = None) -> None:
        result = self.control(("system", "info", "--format", "{{json .ID}}"), deadline, cancelled)
        if result.returncode != 0:
            raise SourceArchiveError("source_unavailable")
        try:
            value = parse_json(result.stdout, maximum_bytes=_CONTROL_BYTES)
        except SourceArchiveError:
            raise SourceArchiveError("source_unavailable") from None
        if value != self.runtime.daemon_id:
            raise SourceArchiveError("cleanup_incomplete")

    def preflight(self, deadline: float, cancelled: Event | None = None) -> None:
        self.daemon(deadline, cancelled)
        result = self.control(("image", "inspect", self.runtime.image_id), deadline, cancelled)
        if result.returncode != 0:
            raise SourceArchiveError("capability_unavailable")
        try:
            value = parse_json(result.stdout, maximum_bytes=_CONTROL_BYTES)
        except SourceArchiveError:
            raise SourceArchiveError("capability_unavailable") from None
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise SourceArchiveError("capability_unavailable")
        image = cast(dict[object, object], value[0])
        if (
            image.get("Id") != self.runtime.image_id
            or image.get("Os") != "linux"
            or image.get("Architecture") != "arm64"
        ):
            raise SourceArchiveError("capability_unavailable")
