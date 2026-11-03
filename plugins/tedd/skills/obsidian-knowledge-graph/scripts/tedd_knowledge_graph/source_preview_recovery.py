"""Recoverable Linux preview mutations, under the existing publication locks.

Source-preview and explicitly confirmed export cleanup callers opt in. The private recovery area has the same local,
same-user threat boundary as source_files; it is not safe against an administrator
or an owner-privileged program attacking that area. No recovery publishes content
or restores a vault path. Unknown, edited or busy objects stay for owner recovery.
"""

from __future__ import annotations

import hmac
import importlib
import os
import re
import secrets
import signal
import stat
import struct
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast

from knowledge_graph_file_io import FileIdentity

from . import source_files
from .source_contracts import SourceArchiveError, digest, encode, parse_json, sha256
from .source_presentation import MAX_PREVIEW_BATCH_BYTES, MAX_PREVIEW_FILE_BYTES, MAX_PREVIEW_FILES

_OPERATION = re.compile(r"[0-9a-f]{32}")
_INTENT_BYTES = 4096
IS_LINUX = sys.platform == "linux"


class _Leases(Protocol):
    F_SETLEASE: int
    F_GETLEASE: int
    F_RDLCK: int

    def fcntl(self, fd: int, operation: int, argument: int | bytes = 0) -> int | bytes: ...


class _ThreadSignals(Protocol):
    SIG_BLOCK: int
    SIGIO: int

    def pthread_sigmask(self, how: int, mask: set[int]) -> set[int]: ...


@dataclass(frozen=True)
class _FileState:
    device: int
    inode: int
    size: int
    sha256: str

    def wire(self) -> list[int | str]:
        return [self.device, self.inode, self.size, self.sha256]

    @classmethod
    def read(cls, value: object) -> _FileState:
        if (
            not isinstance(value, list)
            or len(value) != 4
            or any(type(number) is not int or number < 0 for number in value[:3])
            or value[2] > 64 * 1024**2
        ):
            raise SourceArchiveError()
        return cls(value[0], value[1], value[2], sha256(value[3]))


def _state(path: Path, payload: bytes) -> _FileState:
    info = source_files.check_entry(path)
    return _FileState(info.st_dev, info.st_ino, len(payload), digest(payload))


def _export_entry(path: Path, *, directory: bool = False) -> os.stat_result:
    """Shared input mode is allowed only here; ownership and link checks remain."""
    info = path.lstat()
    if (
        any(source_files.is_link_or_reparse_point(part) for part in (path, *path.parents))
        or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or not source_files.owner_is_current_user(path)
        or (not directory and info.st_nlink != 1)
    ):
        raise SourceArchiveError("privacy_rejected")
    return info


def _harden_relocated_export(path: Path) -> None:
    # Only after relocation into a private directory. Never chmod a shared-cache
    # pathname or a hard-linked/substituted target. Open writers are not revoked;
    # the existing lease/digest checks will retain and report their object.
    with source_files.guarded_input_descriptor(path) as fd:
        info = os.fstat(fd)
        if info.st_nlink != 1 or not source_files.owner_is_current_user(fd):
            raise SourceArchiveError("privacy_rejected")
        source_files.set_private_fd_mode(fd, 0o600)
        os.fsync(fd)


def _entries(root: Path) -> list[Path]:
    if not os.path.lexists(root):
        return []
    source_files.private_root(root)
    with os.scandir(root) as entries:
        result = []
        for entry in entries:
            result.append(Path(entry.path))
            if len(result) > MAX_PREVIEW_FILES:
                raise SourceArchiveError("limit_exceeded")
    return sorted(result)


def recovery_report(root: Path) -> dict[str, object]:
    """Read-only inventory, independent of current keys, scope or vault state."""
    try:
        entries = [
            path.name if _OPERATION.fullmatch(path.name) else digest(os.fsencode(path.name))[:32]
            for path in _entries(root)
        ]
        return {
            "recovery_status": "required" if entries else "clean",
            "recovery_entries": entries,
            "recovery_may_contain_relocated_files": bool(entries),
        }
    except (OSError, ValueError):
        return {"recovery_status": "required", "recovery_entries": [], "recovery_unreadable": True}


@dataclass(frozen=True)
class _PinnedPublication:
    """Use the existing no-clobber publisher with pinned Linux parent handles."""

    source: int
    destination: int

    def stat(self, path: Path) -> FileIdentity:
        return cast(FileIdentity, os.stat(path.name, dir_fd=self.source, follow_symlinks=False))

    def link(self, source: Path, destination: Path) -> None:
        os.link(
            source.name,
            destination.name,
            src_dir_fd=self.source,
            dst_dir_fd=self.destination,
            follow_symlinks=False,
        )

    def unlink(self, path: Path) -> None:
        os.unlink(path.name, dir_fd=self.source)

    def replace(self, source: Path, destination: Path) -> None:
        raise SourceArchiveError("capability_unavailable")


def _discard_private(path: Path, states: tuple[_FileState, ...]) -> bool:
    """Run only on a disposable worker thread, never alter process signal handlers.

    Linux leases exclude existing writers, not pathname replacement. The path is
    already inside private recovery. SIGIO stays blocked until this thread exits;
    F_SETOWN_EX targets this exact thread before any lease is acquired. A break or
    unavailable lease retains the object. No timed-out worker outlives our locks.
    """
    fcntl = cast(_Leases, importlib.import_module("fcntl"))
    signals = cast(_ThreadSignals, signal)

    if threading.current_thread() is threading.main_thread():
        raise SourceArchiveError("capability_unavailable")
    signals.pthread_sigmask(signals.SIG_BLOCK, {signals.SIGIO})
    info = source_files.check_entry(path)
    fd, opened = source_files.descriptor(path, (info.st_dev, info.st_ino))
    try:
        # Linux UAPI f_owner_ex = {int type, pid_t pid}. Python exposed these
        # constants only in 3.13. Verify the kernel's round trip on older Python.
        owner = struct.pack("ii", 0, threading.get_native_id())  # F_OWNER_TID
        fcntl.fcntl(fd, 15, owner)  # F_SETOWN_EX
        if fcntl.fcntl(fd, 16, bytes(len(owner))) != owner:  # F_GETOWN_EX
            return False
        fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_RDLCK)
        candidates = tuple(
            state
            for state in states
            if (state.device, state.inode, state.size)
            == (opened.st_dev, opened.st_ino, opened.st_size)
        )
        if not candidates:
            return False
        with os.fdopen(os.dup(fd), "rb") as handle:
            payload = handle.read(opened.st_size + 1)
        current = source_files.check_entry(path)
        if (
            (current.st_dev, current.st_ino, current.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
            or len(payload) != opened.st_size
            or not any(hmac.compare_digest(digest(payload), item.sha256) for item in candidates)
            or fcntl.fcntl(fd, fcntl.F_GETLEASE) != fcntl.F_RDLCK
        ):
            return False
        path.unlink()
        source_files.fsync_directory(path.parent)
        return True
    finally:
        # Closing the last descriptor releases the lease, including error paths.
        os.close(fd)


@dataclass(frozen=True, repr=False)
class PreviewRecovery:
    """One stable private location, with exact scope/root-bound signed intents.

    Callers hold the configuration lock, and the projection lock for previews.
    Reports contain opaque operation IDs only. The intent binds the original
    path by digest, not by storing its spelling. Retained payloads are not an
    archive, retrieval input, or stable snapshot while an editor still holds them.
    """

    root: Path
    key: str
    binding: str
    export_root: Path | None = field(default=None, kw_only=True)
    _relocated: list[str] = field(default_factory=list, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or len(self.key) < 32:
            raise SourceArchiveError()
        sha256(self.binding)

    def report(self) -> dict[str, object]:
        """Read-only, bounded inventory, including unknown/unreadable state."""
        result = recovery_report(self.root)
        entries = result["recovery_entries"]
        assert isinstance(entries, list)
        if result.get("recovery_unreadable"):
            entries.extend(entry for entry in self._relocated if entry not in entries)
        result["relocated_files"] = sum(entry in entries for entry in self._relocated)
        return result

    def _intent(self, entry: Path) -> dict[str, object]:
        source_files.check_entry(entry, directory=True)
        value = parse_json(source_files.read_bytes(entry / "intent.json", _INTENT_BYTES))
        if not isinstance(value, dict) or set(value) != {"data", "signature"}:
            raise SourceArchiveError()
        data = value["data"]
        if (
            not isinstance(data, dict)
            or set(data)
            != {"format", "id", "binding", "target", "original", "placeholder", "replacement"}
            or type(data["format"]) is not int
            or data["format"] != 1
            or data["id"] != entry.name
            or not _OPERATION.fullmatch(entry.name)
            or data["binding"] != self.binding
            or not hmac.compare_digest(
                sha256(value["signature"]),
                hmac.digest(self.key.encode("utf-8"), encode(data), "sha256").hex(),
            )
        ):
            raise SourceArchiveError()
        sha256(data["target"])
        for name in ("original", "placeholder", "replacement"):
            if data[name] is not None:
                state = _FileState.read(data[name])
                if self.export_root is None and state.size > MAX_PREVIEW_FILE_BYTES:
                    raise SourceArchiveError()
        # No recursive traversal, and no deleting an unexpected/stage artifact.
        with os.scandir(entry) as children:
            for child in children:
                if child.name not in {"payload", "replacement", "intent.json"}:
                    raise SourceArchiveError()
        return data

    @staticmethod
    def _discard(path: Path, states: tuple[_FileState, ...]) -> bool:
        if not os.path.lexists(path):
            return True
        try:
            # One joined, short-lived thread isolates blocked SIGIO. No timeout,
            # reusable executor, background job, or mutation after caller return.
            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(_discard_private, path, states).result()
        except (OSError, ValueError):
            return False

    def _reconcile_entry(self, entry: Path) -> None:
        source_files.check_entry(entry, directory=True)
        if not _OPERATION.fullmatch(entry.name):
            raise SourceArchiveError()
        with os.scandir(entry) as children:
            empty = next(children, None) is None
        if empty:
            # A crash after final intent removal leaves no bytes to authenticate.
            # rmdir is nonrecursive and refuses any newly created owner content.
            entry.rmdir()
            source_files.fsync_directory(self.root)
            if entry.name in self._relocated:
                self._relocated.remove(entry.name)
            return
        data = self._intent(entry)
        if self.export_root is not None and os.path.lexists(entry / "payload"):
            _harden_relocated_export(entry / "payload")
        payload_done = self._discard(
            entry / "payload",
            (_FileState.read(data["original"]), _FileState.read(data["placeholder"])),
        )
        replacement_done = True
        if data["replacement"] is not None:
            replacement_done = self._discard(
                entry / "replacement", (_FileState.read(data["replacement"]),)
            )
        elif os.path.lexists(entry / "replacement"):
            replacement_done = False
        if payload_done and replacement_done:
            # Sync again on recovery after an interrupted previous disposal.
            source_files.fsync_directory(entry)
            (entry / "intent.json").unlink()
            source_files.fsync_directory(entry)
            entry.rmdir()
            source_files.fsync_directory(self.root)
            if entry.name in self._relocated:
                self._relocated.remove(entry.name)

    def reconcile(self) -> dict[str, object]:
        """Dispose only authenticated unchanged private copies; never restore."""
        if not IS_LINUX:
            return self.report()
        failed = False
        try:
            for entry in _entries(self.root):
                try:
                    self._reconcile_entry(entry)
                except (OSError, ValueError):
                    failed = True
            if os.path.lexists(self.root):
                source_files.fsync_directory(self.root)
        except (OSError, ValueError):
            failed = True
        result = self.report()
        if failed:
            result.update(recovery_status="required", recovery_failed=True)
        return result

    def mutate(
        self, path: Path, identity: tuple[int, int], expected: bytes, replacement: bytes | None
    ) -> bool:
        if not IS_LINUX:
            raise source_files.MutationGuardUnavailable("Linux recovery unavailable")
        maximum = MAX_PREVIEW_FILE_BYTES
        read = source_files.read_bytes
        if self.export_root is not None:
            if (
                replacement is not None
                or not path.is_absolute()
                or ".." in path.parts
                or not path.is_relative_to(self.export_root)
                or path == self.export_root
                or self.root.is_relative_to(self.export_root)
                or self.export_root.is_relative_to(self.root)
            ):
                raise SourceArchiveError("privacy_rejected")
            _export_entry(self.export_root, directory=True)
            maximum = 64 * 1024**2
            read = source_files.read_input
        if (
            len(expected) > maximum
            or (replacement is not None and len(replacement) > MAX_PREVIEW_FILE_BYTES)
            or (
                self.export_root is None
                and len(expected) + len(replacement or b"") > MAX_PREVIEW_BATCH_BYTES
            )
        ):
            raise SourceArchiveError("limit_exceeded")
        if self.reconcile()["recovery_status"] != "clean":
            return False
        current = self._check_input(path)
        if (current.st_dev, current.st_ino) != identity or read(path, maximum) != expected:
            return False
        source_files.private_root(self.root, create=not os.path.lexists(self.root))
        # The recovery root's own directory entry must precede a durable removal
        # from the vault, including a retry after an interrupted first creation.
        source_files.fsync_directory(self.root.parent)
        if self.root.stat().st_dev != current.st_dev:
            raise OSError("cross-filesystem preview recovery refused")
        entry = self.root / secrets.token_hex(16)
        entry.mkdir(mode=0o700)
        source_files.write_bytes(entry / "payload", b"")
        if replacement is not None:
            source_files.write_bytes(entry / "replacement", replacement)
        original = _FileState(*identity, len(expected), digest(expected))
        data: dict[str, object] = {
            "format": 1,
            "id": entry.name,
            "binding": self.binding,
            "target": digest(os.fsencode(path)),
            "original": original.wire(),
            "placeholder": _state(entry / "payload", b"").wire(),
            "replacement": _state(entry / "replacement", replacement).wire()
            if replacement is not None
            else None,
        }
        source_files.write_bytes(
            entry / "intent.json",
            encode(
                {
                    "data": data,
                    "signature": hmac.digest(
                        self.key.encode("utf-8"), encode(data), "sha256"
                    ).hex(),
                }
            ),
        )
        source_files.fsync_directory(entry)
        source_files.fsync_directory(self.root)
        parent = self._check_input(path.parent, directory=True)
        # Attribute lookup is deliberately required, not a zero-flag fallback.
        flags = os.O_RDONLY
        for name in ("O_DIRECTORY", "O_NOFOLLOW"):
            flag = getattr(os, name, None)
            if not isinstance(flag, int) or not flag:
                raise SourceArchiveError("capability_unavailable")
            flags |= flag
        source_fd = os.open(path.parent, flags)
        try:
            opened = os.fstat(source_fd)
            if (opened.st_dev, opened.st_ino) != (parent.st_dev, parent.st_ino):
                raise SourceArchiveError("source_unavailable")
            target_fd = os.open(entry, flags)
            try:
                # A regular placeholder prevents a substituted directory from
                # being renamed into recovery. EXDEV propagates; never copy/delete.
                try:
                    os.replace(path.name, "payload", src_dir_fd=source_fd, dst_dir_fd=target_fd)
                finally:
                    # Even a post-rename error retains a discoverable move receipt.
                    moved = (entry / "payload").lstat()
                    placeholder = _FileState.read(data["placeholder"])
                    if (moved.st_dev, moved.st_ino) != (placeholder.device, placeholder.inode):
                        self._relocated.append(entry.name)
                os.fsync(source_fd)
                os.fsync(target_fd)
                current_parent = self._check_input(path.parent, directory=True)
                if (current_parent.st_dev, current_parent.st_ino) != (parent.st_dev, parent.st_ino):
                    raise SourceArchiveError("source_unavailable")
                if self.export_root is not None:
                    _harden_relocated_export(entry / "payload")
                if not self._discard(entry / "payload", (original,)):
                    return False
                if replacement is not None:
                    published = source_files.publish_staged_file(
                        entry / "replacement",
                        path,
                        overwrite=False,
                        os_module=_PinnedPublication(target_fd, source_fd),
                    )
                    os.fsync(source_fd)
                    os.fsync(target_fd)
                    # The directory handles remain pinned through publication.
                    # A renamed parent is reported, never followed into its substitute.
                    current_parent = source_files.check_entry(path.parent, directory=True)
                    if (current_parent.st_dev, current_parent.st_ino) != (
                        parent.st_dev,
                        parent.st_ino,
                    ):
                        raise SourceArchiveError("source_unavailable")
                    fd, _ = source_files.descriptor(path, published)
                    os.close(fd)
                elif os.path.lexists(path):
                    raise FileExistsError("preview removal incomplete")
            finally:
                os.close(target_fd)
        finally:
            os.close(source_fd)
        self._reconcile_entry(entry)
        return True

    def _check_input(self, path: Path, *, directory: bool = False) -> os.stat_result:
        if self.export_root is not None:
            return _export_entry(path, directory=directory)
        return source_files.check_entry(path, directory=directory)
