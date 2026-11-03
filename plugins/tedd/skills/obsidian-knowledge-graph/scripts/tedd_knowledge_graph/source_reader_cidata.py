"""Bounded validation of one reviewed Lima cidata ISO."""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from threading import Event
from types import MappingProxyType

from . import source_files
from .source_contracts import SourceArchiveError

_BLOCK = 2048
_MAX_BYTES = 64 * 1024 * 1024
_MAX_ENTRIES = 128
_HASH = re.compile(r"[0-9a-f]{64}")
_IID = re.compile(rb"iid-[0-9]{1,20}")
_METADATA_IID = re.compile(rb"(?m)^instance-id: (iid-[0-9]{1,20})$")


def _valid_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


@dataclass(frozen=True, repr=False)
class ColdIsoBaseline:
    entries: Mapping[str, str]
    meta_data_normalized_sha256: str
    lima_env_normalized_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.entries, Mapping)
            or not self.entries
            or len(self.entries) > _MAX_ENTRIES - 2
            or any(
                not isinstance(name, str)
                or not name
                or name in {"meta-data", "lima.env"}
                or name.startswith("/")
                or ".." in Path(name).parts
                or not _valid_hash(value)
                for name, value in self.entries.items()
            )
            or not _valid_hash(self.meta_data_normalized_sha256)
            or not _valid_hash(self.lima_env_normalized_sha256)
        ):
            raise SourceArchiveError()
        object.__setattr__(self, "entries", MappingProxyType(dict(self.entries)))


@dataclass(frozen=True, repr=False)
class IsoSnapshot:
    device: int
    inode: int
    size: int
    sha256: str
    iid: str


def _active(deadline: float, cancelled: Event | None) -> None:
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
        or time.monotonic() >= deadline
        or (cancelled is not None and cancelled.is_set())
    ):
        raise SourceArchiveError("source_unavailable")


def _read(path: Path, deadline: float, cancelled: Event | None) -> tuple[bytes, os.stat_result]:
    _active(deadline, cancelled)
    try:
        source_files.check_entry(path.parent, directory=True)
        before = source_files.check_entry(path)
        if not 0 < before.st_size <= _MAX_BYTES:
            raise SourceArchiveError("limit_exceeded")
        fd = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev,
                before.st_ino,
                before.st_size,
            ) or not stat.S_ISREG(opened.st_mode):
                raise SourceArchiveError("source_unavailable")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(fd, 64 * 1024):
                _active(deadline, cancelled)
                total += len(chunk)
                if total > _MAX_BYTES:
                    raise SourceArchiveError("limit_exceeded")
                chunks.append(chunk)
            # Compare each API to its own baseline: Windows Python versions
            # can expose different ctime clocks through fstat and lstat.
            for current, expected in ((os.fstat(fd), opened), (path.lstat(), before)):
                if (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                ) != (
                    expected.st_dev,
                    expected.st_ino,
                    expected.st_size,
                    expected.st_mtime_ns,
                    expected.st_ctime_ns,
                ):
                    raise SourceArchiveError("source_unavailable")
            _active(deadline, cancelled)
            return b"".join(chunks), opened
        finally:
            os.close(fd)
    except OSError:
        raise SourceArchiveError("source_unavailable") from None


def _dual(data: bytes, offset: int, width: int) -> int:
    little = int.from_bytes(data[offset : offset + width], "little")
    big = int.from_bytes(data[offset + width : offset + 2 * width], "big")
    if little != big:
        raise SourceArchiveError()
    return little


def _name(record: bytes) -> str:
    length = record[32]
    if 33 + length > len(record):
        raise SourceArchiveError()
    raw = record[33 : 33 + length]
    start = 33 + length + (length % 2 == 0)
    alternate: str | None = None
    while start < len(record):
        # Lima's Rock Ridge records may pad an odd system-use length to even.
        if start % 2 and record[start:] == b"\0":
            break
        if len(record) - start < 4:
            raise SourceArchiveError()
        tag, size, version = record[start : start + 2], record[start + 2], record[start + 3]
        if size < 4 or start + size > len(record) or version != 1:
            raise SourceArchiveError()
        if tag in {b"SL", b"CL", b"PL", b"CE"}:
            raise SourceArchiveError()
        if tag == b"NM":
            if alternate is not None or size < 5 or record[start + 4] != 0:
                raise SourceArchiveError()
            try:
                alternate = record[start + 5 : start + size].decode("utf-8")
            except UnicodeError:
                raise SourceArchiveError() from None
        start += size
    try:
        iso_name = raw.decode("ascii")
    except UnicodeError:
        raise SourceArchiveError() from None
    if iso_name.endswith(";1"):
        iso_name = iso_name[:-2]
    name = alternate if alternate is not None else iso_name.lower()
    if (
        not name
        or name in {".", ".."}
        or len(name.encode("utf-8")) > 128
        or any(character in name for character in "/\\\x00")
    ):
        raise SourceArchiveError()
    return name


def _record(data: bytes, offset: int) -> tuple[int, int, bool, bytes]:
    length = data[offset]
    if length < 34 or offset + length > len(data):
        raise SourceArchiveError()
    record = data[offset : offset + length]
    if record[1] or record[25] not in (0, 2) or record[26:28] != b"\0\0":
        raise SourceArchiveError()
    if _dual(record, 28, 2) != 1:
        raise SourceArchiveError()
    return _dual(record, 2, 4), _dual(record, 10, 4), record[25] == 2, record


def _entries(data: bytes, deadline: float, cancelled: Event | None) -> dict[str, bytes]:
    if len(data) % _BLOCK or not 20 * _BLOCK <= len(data) <= _MAX_BYTES:
        raise SourceArchiveError()
    primary = data[16 * _BLOCK : 17 * _BLOCK]
    terminator = data[17 * _BLOCK : 18 * _BLOCK]
    if (
        primary[:7] != b"\x01CD001\x01"
        or terminator[:7] != b"\xffCD001\x01"
        or primary[40:72] not in (b"cidata".ljust(32, b" "), b"cidata".ljust(32, b"\0"))
        or _dual(primary, 80, 4) != len(data) // _BLOCK
        or _dual(primary, 128, 2) != _BLOCK
    ):
        raise SourceArchiveError()
    if any(
        data[sector * _BLOCK + 1 : sector * _BLOCK + 7] == b"CD001\x01"
        for sector in range(len(data) // _BLOCK)
        if sector not in (16, 17)
    ):
        raise SourceArchiveError()
    root_sector, root_size, root_dir, _ = _record(primary, 156)
    if not root_dir or root_size < _BLOCK or root_sector < 18:
        raise SourceArchiveError()
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    extents: list[tuple[int, int]] = []

    def walk(prefix: str, sector: int, size: int, depth: int) -> None:
        _active(deadline, cancelled)
        if depth > 16 or size == 0:
            raise SourceArchiveError()
        start = sector * _BLOCK
        end = start + size
        allocated_end = (end + _BLOCK - 1) // _BLOCK * _BLOCK
        if start < 18 * _BLOCK or allocated_end > len(data):
            raise SourceArchiveError()
        extents.append((start, allocated_end))
        position = start
        while position < end:
            _active(deadline, cancelled)
            if data[position] == 0:
                position = (position // _BLOCK + 1) * _BLOCK
                continue
            file_sector, file_size, is_dir, record = _record(data, position)
            if position + len(record) > min(end, (position // _BLOCK + 1) * _BLOCK):
                raise SourceArchiveError()
            position += len(record)
            raw_name = record[33 : 33 + record[32]]
            if raw_name in (b"\0", b"\1"):
                if not is_dir:
                    raise SourceArchiveError()
                continue
            name = _name(record)
            path = f"{prefix}/{name}" if prefix else name
            if len(path.encode("utf-8")) > 256 or path in files or path in directories:
                raise SourceArchiveError()
            if len(files) + len(directories) >= _MAX_ENTRIES:
                raise SourceArchiveError("limit_exceeded")
            if is_dir:
                directories.add(path)
                walk(path, file_sector, file_size, depth + 1)
            else:
                begin = file_sector * _BLOCK
                finish = begin + file_size
                if begin < 18 * _BLOCK or finish > len(data):
                    raise SourceArchiveError()
                if file_size:
                    extents.append((begin, (finish + _BLOCK - 1) // _BLOCK * _BLOCK))
                files[path] = data[begin:finish]

    walk("", root_sector, root_size, 0)
    extents.sort()
    if any(right[0] < left[1] for left, right in pairwise(extents)):
        raise SourceArchiveError()
    expected_dirs = {part for path in files for part in _parents(path)}
    if directories != expected_dirs:
        raise SourceArchiveError()
    return files


def _parents(path: str) -> tuple[str, ...]:
    parts = path.split("/")
    return tuple("/".join(parts[:index]) for index in range(1, len(parts)))


def _normalized(data: bytes) -> tuple[bytes, bytes]:
    matches = list(_IID.finditer(data))
    if len(matches) != 1:
        raise SourceArchiveError()
    match = matches[0]
    return data[: match.start()] + b"<iid>" + data[match.end() :], match.group()


def validate_cidata(
    path: Path, baseline: ColdIsoBaseline, deadline: float, cancelled: Event | None
) -> IsoSnapshot:
    """Accept only the reviewed ISO entry set and per-boot IID variation."""

    if not isinstance(path, Path) or not isinstance(baseline, ColdIsoBaseline):
        raise SourceArchiveError()
    data, info = _read(path, deadline, cancelled)
    files = _entries(data, deadline, cancelled)
    if set(files) != set(baseline.entries) | {"meta-data", "lima.env"}:
        raise SourceArchiveError()
    for name, expected in baseline.entries.items():
        _active(deadline, cancelled)
        if hashlib.sha256(files[name]).hexdigest() != expected:
            raise SourceArchiveError("source_unavailable")
    meta = files["meta-data"]
    env = files["lima.env"]
    lines = env.splitlines()
    if [line for line in lines if line.startswith(b"LIMA_CIDATA_UDP_DNS_LOCAL_PORT=")] != [
        b"LIMA_CIDATA_UDP_DNS_LOCAL_PORT=0"
    ] or [line for line in lines if line.startswith(b"LIMA_CIDATA_TCP_DNS_LOCAL_PORT=")] != [
        b"LIMA_CIDATA_TCP_DNS_LOCAL_PORT=0"
    ]:
        raise SourceArchiveError()
    matches = list(_METADATA_IID.finditer(meta))
    if len(matches) != 1:
        raise SourceArchiveError()
    normalized_meta, iid = _normalized(meta)
    normalized_env, env_iid = _normalized(env)
    if iid != matches[0].group(1) or iid != env_iid:
        raise SourceArchiveError()
    if (
        hashlib.sha256(normalized_meta).hexdigest() != baseline.meta_data_normalized_sha256
        or hashlib.sha256(normalized_env).hexdigest() != baseline.lima_env_normalized_sha256
    ):
        raise SourceArchiveError("source_unavailable")
    return IsoSnapshot(
        info.st_dev,
        info.st_ino,
        info.st_size,
        hashlib.sha256(data).hexdigest(),
        iid.decode("ascii"),
    )
