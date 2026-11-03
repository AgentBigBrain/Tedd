"""Measured private binding for the supported warm Lima macOS reader runtime."""

from __future__ import annotations

import ctypes
import hashlib
import math
import os
import re
import stat
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import cast

from . import source_files
from .source_contracts import SourceArchiveError, digest, encode, object_fields, parse_json
from .source_reader_policy import ReaderRuntime
from .source_reader_process import run_bounded

_MANIFEST_BYTES = 64 * 1024
_STATIC_BYTES = 64 * 1024 * 1024
_CONTROL_BYTES = 1024 * 1024
_FORMAT = "tedd-mac-reader-runtime"
_VERSION = 1
_LIMA_VERSION = "2.2.0"
_PROFILE = re.compile(r"colima-[a-z0-9](?:[a-z0-9-]{0,62})")
_HASH = re.compile(r"[0-9a-f]{64}")
_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
_MNT_IGNORE_OWNERSHIP = 0x00200000

_MANIFEST_FIELDS = {
    "format",
    "version",
    "private_runtime_root",
    "lima_home",
    "profile",
    "limactl",
    "runtime",
    "effective_config_sha256",
    "files",
}
_RUNTIME_FIELDS = {"docker", "socket", "state_root", "owner_id", "daemon_id", "image_id"}
_LIMACTL_FIELDS = {"path", "version", "identity"}
_IDENTITY_FIELDS = {"path", "type", "device", "inode", "owner", "links", "sha256"}
_IDENTITY_TYPES = {"directory", "regular", "socket"}
_PORT_FORWARD_FIELDS = {
    "guestIPMustBeZero",
    "guestIP",
    "guestPort",
    "guestPortRange",
    "guestSocket",
    "hostIP",
    "hostPort",
    "hostPortRange",
    "hostSocket",
    "proto",
    "reverse",
    "ignore",
    "static",
}


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise SourceArchiveError("capability_unavailable")
    return cast(Callable[[], int], getuid)()


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


def _mapping(value: object, fields: set[str] | None = None) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise SourceArchiveError()
    result = cast(dict[str, object], value)
    if fields is not None and set(result) != fields:
        raise SourceArchiveError()
    return result


def _integer(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise SourceArchiveError()
    return value


def _path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SourceArchiveError()
    path = Path(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or str(path) != os.path.abspath(path)
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)
    ):
        raise SourceArchiveError()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SourceArchiveError("source_unavailable") from None
    if resolved != path:
        raise SourceArchiveError("privacy_rejected")
    return path


def _inside(path: Path, root: Path) -> None:
    if path != root and root not in path.parents:
        raise SourceArchiveError("privacy_rejected")


def _trusted_ancestry(path: Path) -> None:
    for parent in (path, *path.parents):
        try:
            info = parent.lstat()
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, _current_uid()}
            or info.st_mode & 0o022
        ):
            raise SourceArchiveError("privacy_rejected")


@dataclass(frozen=True, repr=False)
class _FileIdentity:
    path: Path
    kind: str
    device: int
    inode: int
    owner: int
    links: int
    sha256: str | None


def _decode_identity(value: object, *, private_root: Path | None) -> _FileIdentity:
    item = _mapping(value, _IDENTITY_FIELDS)
    path = _path(item["path"])
    if private_root is not None:
        _inside(path, private_root)
    kind = item["type"]
    sha256 = item["sha256"]
    if not isinstance(kind, str) or kind not in _IDENTITY_TYPES:
        raise SourceArchiveError()
    if sha256 is not None and (not isinstance(sha256, str) or _HASH.fullmatch(sha256) is None):
        raise SourceArchiveError()
    if kind != "regular" and sha256 is not None:
        raise SourceArchiveError()
    return _FileIdentity(
        path,
        kind,
        _integer(item["device"], minimum=1),
        _integer(item["inode"], minimum=1),
        _integer(item["owner"]),
        _integer(item["links"], minimum=1),
        sha256,
    )


def _file_hash(
    path: Path,
    expected: os.stat_result,
    deadline: float,
    cancelled: Event | None,
) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    try:
        opened = os.fstat(fd)
        if (
            opened.st_dev,
            opened.st_ino,
            stat.S_IFMT(opened.st_mode),
            opened.st_uid,
            opened.st_nlink,
        ) != (
            expected.st_dev,
            expected.st_ino,
            stat.S_IFMT(expected.st_mode),
            expected.st_uid,
            expected.st_nlink,
        ):
            raise SourceArchiveError("source_unavailable")
        total = 0
        result = hashlib.sha256()
        while True:
            if _stopped(deadline, cancelled):
                raise SourceArchiveError("source_unavailable")
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _STATIC_BYTES:
                raise SourceArchiveError("limit_exceeded")
            result.update(chunk)
        after = os.fstat(fd)
        current = path.lstat()
        for info in (after, current):
            if (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            ):
                raise SourceArchiveError("source_unavailable")
        return result.hexdigest()
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _check_identity(
    identity: _FileIdentity,
    *,
    private: bool,
    root_device: int,
    deadline: float,
    cancelled: Event | None,
) -> None:
    if _stopped(deadline, cancelled):
        raise SourceArchiveError("source_unavailable")
    try:
        info = identity.path.lstat()
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    actual_kind = (
        "directory"
        if stat.S_ISDIR(info.st_mode)
        else "regular"
        if stat.S_ISREG(info.st_mode)
        else "socket"
        if stat.S_ISSOCK(info.st_mode)
        else "unsupported"
    )
    links_changed = identity.kind != "directory" and info.st_nlink != identity.links
    if (
        actual_kind != identity.kind
        or info.st_dev != identity.device
        or info.st_ino != identity.inode
        or info.st_uid != identity.owner
        or links_changed
        or stat.S_ISLNK(info.st_mode)
    ):
        raise SourceArchiveError("source_unavailable")
    if identity.kind == "regular" and info.st_nlink != 1:
        raise SourceArchiveError("privacy_rejected")
    if private:
        if info.st_uid != _current_uid() or info.st_dev != root_device or info.st_mode & 0o077:
            raise SourceArchiveError("privacy_rejected")
    elif info.st_uid not in {0, _current_uid()} or info.st_mode & 0o022:
        raise SourceArchiveError("privacy_rejected")
    if (
        identity.sha256 is not None
        and _file_hash(identity.path, info, deadline, cancelled) != identity.sha256
    ):
        raise SourceArchiveError("source_unavailable")


def _profile_parts(root: Path, lima_home: Path, profile: str) -> tuple[Path, Path]:
    if _PROFILE.fullmatch(profile) is None:
        raise SourceArchiveError()
    _inside(lima_home, root)
    return lima_home / profile, lima_home / "_disks" / profile


def _profile_root(root: Path, profile: str, runtime: ReaderRuntime) -> Path:
    short = profile.removeprefix("colima-")
    profile_root = runtime.socket.parent
    if profile_root.name != short:
        raise SourceArchiveError("privacy_rejected")
    _inside(profile_root, root)
    _inside(runtime.state_root, root)
    return profile_root


def _layout_paths(
    root: Path,
    lima_home: Path,
    profile: str,
    runtime: ReaderRuntime,
) -> tuple[set[Path], set[Path], set[Path]]:
    instance, disk_root = _profile_parts(root, lima_home, profile)
    profile_root = _profile_root(root, profile, runtime)
    if runtime.socket != profile_root / "docker.sock":
        raise SourceArchiveError("privacy_rejected")
    static = {
        profile_root / "colima.yaml",
        instance / "lima.yaml",
        instance / "colima.yaml",
        instance / "cloud-config.yaml",
        instance / "cidata.iso",
        instance / "vz-identifier",
        instance / "ssh.config",
        lima_home / "_config" / "user",
        lima_home / "_config" / "user.pub",
        lima_home / "_config" / "networks.yaml",
        profile_root.parent / "ssh_config",
    }
    usernet_root = lima_home / "_networks" / "user-v2"
    mutable = {
        instance / "disk",
        disk_root / "datadisk",
        instance / "vz-efi",
        runtime.socket,
        profile_root / "containerd.sock",
        usernet_root / "user-v2_ep.sock",
        usernet_root / "user-v2_fd.sock",
        usernet_root / "user-v2_qemu.sock",
    }
    directories = {
        root,
        lima_home,
        instance,
        lima_home / "_config",
        lima_home / "_disks",
        disk_root,
        profile_root,
        runtime.state_root,
        lima_home / "_networks",
        usernet_root,
    }
    for path in (*static, *mutable):
        for parent in path.parents:
            if parent == root:
                directories.add(parent)
                break
            if root in parent.parents:
                directories.add(parent)
    return directories, static, mutable


def _validate_file_inventory(
    identities: tuple[_FileIdentity, ...],
    root: Path,
    lima_home: Path,
    profile: str,
    runtime: ReaderRuntime,
) -> None:
    directories, static, mutable = _layout_paths(root, lima_home, profile, runtime)
    indexed = {identity.path: identity for identity in identities}
    cache_parent = root / "cache" / "caches"
    cache_files = {
        path
        for path in indexed
        if path.parent == cache_parent and _HASH.fullmatch(path.name) is not None
    }
    cache_directories = {root / "cache", cache_parent}
    expected = directories | static | mutable | cache_directories | cache_files
    if len(cache_files) != 1 or len(indexed) != len(identities) or set(indexed) != expected:
        raise SourceArchiveError()
    for path in directories | cache_directories:
        identity = indexed[path]
        if identity.kind != "directory" or identity.sha256 is not None:
            raise SourceArchiveError()
    for path in static:
        identity = indexed[path]
        if identity.kind != "regular" or identity.sha256 is None:
            raise SourceArchiveError()
    for path in mutable:
        identity = indexed[path]
        expected_kind = "socket" if path.suffix == ".sock" else "regular"
        if identity.kind != expected_kind or identity.sha256 is not None:
            raise SourceArchiveError()
    cache_identity = indexed[next(iter(cache_files))]
    if cache_identity.kind != "regular" or cache_identity.sha256 is not None:
        raise SourceArchiveError()


def _stopped(deadline: float, cancelled: Event | None) -> bool:
    if cancelled is not None:
        check = getattr(cancelled, "is_set", None)
        if not callable(check) or bool(check()):
            return True
    return time.monotonic() >= deadline


def _sequence(value: object, expected: tuple[int, int]) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(type(item) is int for item in value)
        and tuple(value) == expected
    )


def _socket_forward(
    value: object,
    *,
    guest_socket: str,
    host_socket: Path,
) -> None:
    item = _mapping(value)
    if not set(item) <= _PORT_FORWARD_FIELDS:
        raise SourceArchiveError("capability_unavailable")
    if (
        item.get("guestIPMustBeZero", False) is not False
        or item.get("guestIP") != "127.0.0.1"
        or not _sequence(item.get("guestPortRange"), (1, 65535))
        or item.get("guestPort", 0) != 0
        or item.get("guestSocket") != guest_socket
        or item.get("hostIP") != "127.0.0.1"
        or not _sequence(item.get("hostPortRange"), (0, 0))
        or item.get("hostPort", 0) != 0
        or item.get("hostSocket") != str(host_socket)
        or item.get("proto", "tcp") != "tcp"
        or item.get("reverse", False) is not False
        or item.get("ignore", False) is not False
        or item.get("static", False) is not False
    ):
        raise SourceArchiveError("capability_unavailable")


def _ignore_forward(value: object) -> None:
    item = _mapping(value)
    if not set(item) <= _PORT_FORWARD_FIELDS:
        raise SourceArchiveError("capability_unavailable")
    if (
        item.get("guestIPMustBeZero") is not True
        or item.get("guestIP") != "0.0.0.0"
        or not _sequence(item.get("guestPortRange"), (1, 65535))
        or item.get("guestPort", 0) != 0
        or item.get("guestSocket", "") != ""
        or item.get("hostIP") != "127.0.0.1"
        or not _sequence(item.get("hostPortRange"), (1, 65535))
        or item.get("hostPort", 0) != 0
        or item.get("hostSocket", "") != ""
        or item.get("proto") != "any"
        or item.get("reverse", False) is not False
        or item.get("ignore") is not True
        or item.get("static", False) is not False
    ):
        raise SourceArchiveError("capability_unavailable")


def _disabled_rosetta(config: dict[str, object]) -> bool:
    legacy = config.get("rosetta")
    if legacy not in (None, {}):
        if not isinstance(legacy, dict) or any(value is not False for value in legacy.values()):
            return False
    vm_opts = config.get("vmOpts")
    if vm_opts is None:
        return True
    if not isinstance(vm_opts, dict) or set(vm_opts) - {"vz"}:
        return False
    vz = vm_opts.get("vz")
    if vz in (None, {}):
        return True
    if not isinstance(vz, dict) or set(vz) - {"rosetta", "diskImageFormat"}:
        return False
    rosetta = vz.get("rosetta")
    return rosetta in (None, {}) or (
        isinstance(rosetta, dict) and all(value is False for value in rosetta.values())
    )


def _validate_networks(value: object) -> list[object]:
    if not isinstance(value, list) or len(value) != 1:
        raise SourceArchiveError("capability_unavailable")
    network = _mapping(value[0])
    mac = network.get("macAddress")
    if (
        set(network) != {"lima", "macAddress", "interface", "metric"}
        or network.get("lima") != "user-v2"
        or not isinstance(mac, str)
        or re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", mac) is None
        or network.get("interface") != "lima0"
        or network.get("metric") != 100
    ):
        raise SourceArchiveError("capability_unavailable")
    return value


def _validate_images(binding: MacRuntimeBinding, value: object) -> None:
    if not isinstance(value, list) or len(value) != 1:
        raise SourceArchiveError("capability_unavailable")
    image = _mapping(value[0])
    location = image.get("location")
    if (
        set(image) != {"location", "arch"}
        or image.get("arch") != "aarch64"
        or not isinstance(location, str)
    ):
        raise SourceArchiveError("capability_unavailable")
    path = Path(location)
    enrolled = tuple(
        identity.path
        for identity in binding.files
        if identity.path.parent == binding.private_runtime_root / "cache" / "caches"
        and _HASH.fullmatch(identity.path.name) is not None
    )
    if not path.is_absolute() or ".." in path.parts or enrolled != (path,):
        raise SourceArchiveError("capability_unavailable")


def _validate_config(binding: MacRuntimeBinding, value: object) -> None:
    config = _mapping(value)
    additional = [{"name": binding.profile, "format": False, "fsType": "ext4"}]
    firmware = config.get("firmware")
    ssh = config.get("ssh")
    forwards = config.get("portForwards")
    if (
        config.get("vmType") != "vz"
        or config.get("arch") != "aarch64"
        or config.get("cpus") != 2
        or config.get("memory") not in ("3GiB", "3072MiB")
        or config.get("disk") != "10GiB"
        or config.get("additionalDisks") != additional
        or config.get("mounts") not in (None, [])
        or config.get("copyToHost") not in (None, [])
        or config.get("nestedVirtualization", False) is not False
        or config.get("plain", False) is not False
        or not isinstance(firmware, dict)
        or set(firmware) - {"legacyBIOS", "images"}
        or firmware.get("legacyBIOS") is not False
        or firmware.get("images") not in (None, [])
        or not isinstance(ssh, dict)
        or any(
            ssh.get(field, False) is not False
            for field in (
                "loadDotSSHPubKeys",
                "forwardAgent",
                "forwardX11",
                "forwardX11Trusted",
            )
        )
        or not _disabled_rosetta(config)
        or not isinstance(forwards, list)
        or len(forwards) != 3
    ):
        raise SourceArchiveError("capability_unavailable")
    _validate_networks(config.get("networks"))
    _validate_images(binding, config.get("images"))
    profile_root = _profile_root(binding.private_runtime_root, binding.profile, binding.runtime)
    _socket_forward(
        forwards[0], guest_socket="/var/run/docker.sock", host_socket=binding.runtime.socket
    )
    _socket_forward(
        forwards[1],
        guest_socket="/var/run/containerd/containerd.sock",
        host_socket=profile_root / "containerd.sock",
    )
    _ignore_forward(forwards[2])
    if digest(encode(config)) != binding.effective_config_sha256:
        raise SourceArchiveError("capability_unavailable")


@dataclass(frozen=True, repr=False)
class MacRuntimeBinding:
    """Immutable enrollment for one measured Lima 2.2.0/VZ warm runtime."""

    runtime: ReaderRuntime
    private_runtime_root: Path
    lima_home: Path
    profile: str
    limactl: Path
    limactl_version: str
    effective_config_sha256: str
    files: tuple[_FileIdentity, ...]
    limactl_identity: _FileIdentity

    def _check_files(
        self,
        deadline: float = math.inf,
        cancelled: Event | None = None,
    ) -> None:
        try:
            _ownership_enforced(self.private_runtime_root)
            _trusted_ancestry(self.private_runtime_root)
            _trusted_ancestry(self.limactl.parent)
            root_info = self.private_runtime_root.lstat()
            for identity in self.files:
                _check_identity(
                    identity,
                    private=True,
                    root_device=root_info.st_dev,
                    deadline=deadline,
                    cancelled=cancelled,
                )
            _check_identity(
                self.limactl_identity,
                private=False,
                root_device=root_info.st_dev,
                deadline=deadline,
                cancelled=cancelled,
            )
            if self.limactl.lstat().st_mode & 0o111 == 0:
                raise SourceArchiveError("privacy_rejected")
        except (OSError, RuntimeError):
            raise SourceArchiveError("source_unavailable") from None

    def qualify(self, deadline: float, cancelled: Event | None = None) -> None:
        """Revalidate enrollment and the exact running profile within ``deadline``."""

        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)
        ):
            raise SourceArchiveError("source_unavailable")
        if _stopped(float(deadline), cancelled):
            raise SourceArchiveError("source_unavailable")
        self._check_files(float(deadline), cancelled)
        if _stopped(float(deadline), cancelled):
            raise SourceArchiveError("source_unavailable")
        home = _path(os.environ.get("HOME"))
        _trusted_ancestry(home)
        environment = {
            **_ENVIRONMENT,
            "HOME": str(home),
            "LIMA_HOME": str(self.lima_home),
        }
        result = run_bounded(
            (str(self.limactl), "list", "--json", self.profile),
            b"",
            deadline=float(deadline),
            output_limit=_CONTROL_BYTES,
            cancelled=cancelled,
            environment=environment,
        )
        if result.returncode != 0:
            raise SourceArchiveError("source_unavailable")
        record = _mapping(parse_json(result.stdout, maximum_bytes=_CONTROL_BYTES))
        instance, _ = _profile_parts(self.private_runtime_root, self.lima_home, self.profile)
        expected_disk = [{"name": self.profile, "format": False, "fsType": "ext4"}]
        if (
            record.get("name") != self.profile
            or record.get("status") != "Running"
            or record.get("dir") != str(instance)
            or record.get("vmType") != "vz"
            or record.get("arch") != "aarch64"
            or record.get("cpus") != 2
            or record.get("memory") != 3221225472
            or record.get("disk") != 10737418240
            or record.get("additionalDisks") != expected_disk
            or record.get("limaVersion") != self.limactl_version
            or record.get("errors") not in (None, [])
        ):
            raise SourceArchiveError("capability_unavailable")
        config = record.get("config")
        _validate_config(self, config)
        config_mapping = _mapping(config)
        if record.get("network") not in (None, config_mapping.get("networks")):
            raise SourceArchiveError("capability_unavailable")
        self._check_files(float(deadline), cancelled)
        if _stopped(float(deadline), cancelled):
            raise SourceArchiveError("source_unavailable")


def load_mac_runtime_binding(path: Path) -> MacRuntimeBinding:
    """Load one closed private manifest without creating or refreshing trust state."""

    if not isinstance(path, Path):
        raise SourceArchiveError()
    try:
        payload = source_files.read_bytes(path, _MANIFEST_BYTES)
    except SourceArchiveError:
        raise
    except (OSError, RuntimeError):
        raise SourceArchiveError("source_unavailable") from None
    document = object_fields(parse_json(payload, maximum_bytes=_MANIFEST_BYTES), _MANIFEST_FIELDS)
    if (
        document["format"] != _FORMAT
        or type(document["version"]) is not int
        or document["version"] != _VERSION
    ):
        raise SourceArchiveError()
    root = _path(document["private_runtime_root"])
    source_files.private_root(root, create=False)
    _inside(_path(str(path)), root)
    lima_home = _path(document["lima_home"])
    _inside(lima_home, root)
    profile = document["profile"]
    if not isinstance(profile, str):
        raise SourceArchiveError()

    runtime_document = _mapping(document["runtime"], _RUNTIME_FIELDS)
    runtime = ReaderRuntime(
        _path(runtime_document["docker"]),
        _path(runtime_document["socket"]),
        _path(runtime_document["state_root"]),
        cast(str, runtime_document["owner_id"]),
        cast(str, runtime_document["daemon_id"]),
        cast(str, runtime_document["image_id"]),
    )
    for internal in (runtime.socket, runtime.state_root):
        _inside(internal, root)

    limactl_document = _mapping(document["limactl"], _LIMACTL_FIELDS)
    limactl = _path(limactl_document["path"])
    version = limactl_document["version"]
    if version != _LIMA_VERSION:
        raise SourceArchiveError("capability_unavailable")
    limactl_identity = _decode_identity(limactl_document["identity"], private_root=None)
    if (
        limactl_identity.path != limactl
        or limactl_identity.kind != "regular"
        or limactl_identity.sha256 is None
    ):
        raise SourceArchiveError()
    config_hash = document["effective_config_sha256"]
    if not isinstance(config_hash, str) or _HASH.fullmatch(config_hash) is None:
        raise SourceArchiveError()
    raw_files = document["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise SourceArchiveError()
    files = tuple(_decode_identity(value, private_root=root) for value in raw_files)
    _validate_file_inventory(files, root, lima_home, profile, runtime)
    binding = MacRuntimeBinding(
        runtime,
        root,
        lima_home,
        profile,
        limactl,
        version,
        config_hash,
        files,
        limactl_identity,
    )
    binding._check_files()
    return binding
