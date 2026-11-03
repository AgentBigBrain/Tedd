"""Stopped-compatible private authority for the reviewed Mac reader profile."""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from typing import BinaryIO, cast

from platform_support import try_file_lock, unlock_file

from . import source_files
from .source_contracts import SourceArchiveError, encode, object_fields, parse_json
from .source_reader_cidata import ColdIsoBaseline, IsoSnapshot, validate_cidata
from .source_reader_client import _RuntimeClient
from .source_reader_jobs import ReaderJobs
from .source_reader_policy import ReaderRuntime
from .source_reader_process import run_bounded
from .source_reader_runtime import (
    MacRuntimeBinding,
    _check_identity,
    _current_uid,
    _decode_identity,
    _file_hash,
    _FileIdentity,
    _inside,
    _layout_paths,
    _mapping,
    _ownership_enforced,
    _path,
    _profile_parts,
    _profile_root,
    _trusted_ancestry,
    _validate_config,
)

_MAX_MANIFEST = 64 * 1024
_MAX_CONTROL = 64 * 1024
_HASH = re.compile(r"[0-9a-f]{64}")
_SSH_PORT = re.compile(rb"(?mi)^[ \t]+Port[ \t]+([1-9][0-9]{0,4})[ \t]*$")
_FIELDS = {
    "format",
    "version",
    "private_runtime_root",
    "lima_home",
    "profile",
    "colima",
    "limactl",
    "guest_agent",
    "runtime",
    "effective_config_sha256",
    "persistent_files",
    "store_semantic_sha256",
    "docker_config_state",
    "profile_owners_sha256",
    "cidata_entries",
    "meta_data_normalized_sha256",
    "lima_env_normalized_sha256",
    "ssh_config_normalized_sha256",
}
_TOOL_FIELDS = {"path", "version", "identity"}
_RUNTIME_FIELDS = {"docker", "socket", "state_root", "owner_id", "daemon_id", "image_id"}
_DOCKER_FIELDS = {"directory", "present", "sha256", "current_context"}
_ROSTER_FIELDS = {"profile", "version", "cutover_generation", "owners"}
_PERSISTENT_FIELDS = {"path", "type", "device", "inode", "owner", "links", "sha256", "size"}
_START_FIELDS = {
    "version",
    "profile",
    "roster_sha256",
    "cold_entitlement_sha256",
    "owner_id",
    "state_root",
    "daemon_id",
    "image_id",
    "phase",
    "attempt",
    "cutover_generation",
}
_CONTROL_LIMIT = 1024 * 1024
_PROFILE_GUARD = Lock()


def profile_locked(
    root: Path, profile: str, deadline: float, cancelled: Event | None = None
) -> AbstractContextManager[None]:
    """Reserve the existing dedicated profile across processes and state roots."""

    return _profile_locked(root, profile, deadline, cancelled)


@contextmanager
def _profile_locked(
    root: Path, profile: str, deadline: float, cancelled: Event | None
) -> Iterator[None]:
    _active(deadline, cancelled)
    if profile != "colima-tedd-reader-spike" or root.name != profile.removeprefix("colima-"):
        raise SourceArchiveError("capability_unavailable")
    source_files.private_root(root, create=False)
    lock = root / "profile-start.lock"
    acquired_guard = False
    handle: BinaryIO | None = None
    locked = False
    unlock_failed = False
    try:
        while not _PROFILE_GUARD.acquire(blocking=False):
            _active(deadline, cancelled)
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        acquired_guard = True
        before = source_files.check_entry(lock)
        descriptor, _ = source_files.descriptor(lock, (before.st_dev, before.st_ino))
        handle = cast(BinaryIO, os.fdopen(descriptor, "rb"))
        while True:
            _active(deadline, cancelled)
            try:
                try_file_lock(handle, shared=False)
                locked = True
                break
            except BlockingIOError:
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            except (OSError, NotImplementedError):
                raise SourceArchiveError("source_unavailable") from None
        after = source_files.check_entry(lock)
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or (
            after.st_dev,
            after.st_ino,
        ) != (before.st_dev, before.st_ino):
            raise SourceArchiveError("source_unavailable")
        _active(deadline, cancelled)
        yield
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    finally:
        if locked and handle is not None:
            try:
                unlock_file(handle)
            except (OSError, NotImplementedError):
                unlock_failed = True
        try:
            if handle is not None:
                handle.close()
        finally:
            if acquired_guard:
                _PROFILE_GUARD.release()
        if unlock_failed:
            raise SourceArchiveError("source_unavailable") from None


def _hash(value: object) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise SourceArchiveError()
    return value


def _stopped_socket(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SourceArchiveError()
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise SourceArchiveError()
    _path(str(path.parent))
    return path


def _active(deadline: float, cancelled: Event | None) -> None:
    if (
        isinstance(deadline, bool)
        or not isinstance(deadline, (int, float))
        or not math.isfinite(deadline)
        or time.monotonic() >= deadline
        or (cancelled is not None and cancelled.is_set())
    ):
        raise SourceArchiveError("source_unavailable")


def _tool(value: object, version: str, root: Path) -> _FileIdentity:
    record = object_fields(value, _TOOL_FIELDS)
    path = _path(record["path"])
    _inside(path, root)
    if record["version"] != version:
        raise SourceArchiveError("capability_unavailable")
    identity = _decode_identity(record["identity"], private_root=root)
    if identity.path != path or identity.kind != "regular" or identity.sha256 is None:
        raise SourceArchiveError()
    return identity


def _inventory(
    files: tuple[_FileIdentity, ...],
    root: Path,
    lima_home: Path,
    profile: str,
    runtime: ReaderRuntime,
    docker_directory: Path,
) -> None:
    directories, static, mutable = _layout_paths(root, lima_home, profile, runtime)
    instance, _ = _profile_parts(root, lima_home, profile)
    profile_root = _profile_root(root, profile, runtime)
    generated = {
        instance / "cidata.iso",
        instance / "ssh.config",
        profile_root.parent / "ssh_config",
    }
    persistent_static = (static - generated) | {
        root / "bin" / "lima",
        root / "share" / "lima" / "templates" / "default.yaml",
        root / "colima" / "_store" / f"{profile}.json",
        profile_root / "profile-owners.json",
        runtime.docker,
    }
    persistent_mutable = {path for path in mutable if path.suffix != ".sock"}
    cache_root = root / "cache" / "caches"
    indexed = {item.path: item for item in files}
    cache_files = {
        path for path in indexed if path.parent == cache_root and _HASH.fullmatch(path.name)
    }
    directory_paths = directories | {
        root / "share",
        root / "share" / "lima",
        root / "share" / "lima" / "templates",
        root / "cache",
        cache_root,
        root / "colima" / "_store",
        docker_directory,
        runtime.docker.parent,
    }
    expected = directory_paths | persistent_static | persistent_mutable | cache_files
    if len(files) != len(indexed) or len(cache_files) != 1 or set(indexed) != expected:
        raise SourceArchiveError()
    for path in directory_paths:
        if indexed[path].kind != "directory" or indexed[path].sha256 is not None:
            raise SourceArchiveError()
    for path in persistent_static:
        if indexed[path].kind != "regular" or indexed[path].sha256 is None:
            raise SourceArchiveError()
    for path in persistent_mutable | cache_files:
        if indexed[path].kind != "regular" or indexed[path].sha256 is not None:
            raise SourceArchiveError()


def _persistent(value: object, root: Path) -> tuple[_FileIdentity, int | None]:
    record = object_fields(value, _PERSISTENT_FIELDS)
    size = record.pop("size")
    identity = _decode_identity(record, private_root=root)
    if (identity.kind == "regular" and (type(size) is not int or size < 0)) or (
        identity.kind != "regular" and size is not None
    ):
        raise SourceArchiveError()
    return identity, cast(int | None, size)


def _store_mapping(path: Path, deadline: float, cancelled: Event | None) -> dict[str, object]:
    _active(deadline, cancelled)
    try:
        data = source_files.read_bytes(path, _MAX_CONTROL)
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    _active(deadline, cancelled)
    value = _mapping(parse_json(data, maximum_bytes=_MAX_CONTROL))
    if value.get("disk_formatted") is not True or value.get("disk_runtime") != "docker":
        raise SourceArchiveError("capability_unavailable")
    return value


def _store_semantics(path: Path, deadline: float, cancelled: Event | None) -> str:
    return hashlib.sha256(encode(_store_mapping(path, deadline, cancelled))).hexdigest()


def _owners(
    path: Path,
    profile: str,
    root: Path,
    selected: ReaderRuntime,
) -> tuple[str, tuple[ReaderRuntime, ...]]:
    try:
        roster = object_fields(
            parse_json(source_files.read_bytes(path, 32 * 1024), maximum_bytes=32 * 1024),
            _ROSTER_FIELDS,
        )
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    generation = roster["cutover_generation"]
    try:
        if not isinstance(generation, str) or str(uuid.UUID(generation)) != generation:
            raise SourceArchiveError()
    except ValueError:
        raise SourceArchiveError() from None
    entries = roster["owners"]
    if (
        roster["profile"] != profile
        or type(roster["version"]) is not int
        or roster["version"] != 1
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= 16
    ):
        raise SourceArchiveError()
    owners: list[ReaderRuntime] = []
    for value in entries:
        fields = object_fields(value, _RUNTIME_FIELDS)
        owner = ReaderRuntime(
            _path(fields["docker"]),
            _stopped_socket(fields["socket"]),
            _path(fields["state_root"]),
            cast(str, fields["owner_id"]),
            cast(str, fields["daemon_id"]),
            cast(str, fields["image_id"]),
        )
        for internal in (owner.docker, owner.socket, owner.state_root):
            _inside(internal, root)
        if (owner.docker, owner.socket, owner.daemon_id, owner.image_id) != (
            selected.docker,
            selected.socket,
            selected.daemon_id,
            selected.image_id,
        ):
            raise SourceArchiveError()
        owners.append(owner)
    if (
        selected not in owners
        or len({owner.owner_id for owner in owners}) != len(owners)
        or len({owner.state_root for owner in owners}) != len(owners)
        or [str(owner.state_root) for owner in owners]
        != sorted(str(owner.state_root) for owner in owners)
    ):
        raise SourceArchiveError()
    return generation, tuple(owners)


@dataclass(frozen=True, repr=False)
class ProfileOwners:
    """Pinned finite owners and startup receipt for one dedicated profile."""

    profile_root: Path
    private_runtime_root: Path
    profile: str
    runtime: ReaderRuntime
    roster_sha256: str
    cold_entitlement_sha256: str
    cutover_generation: str
    owners: tuple[ReaderRuntime, ...]

    def check_roster(self, deadline: float, cancelled: Event | None) -> None:
        _active(deadline, cancelled)
        roster = self.profile_root / "profile-owners.json"
        try:
            data = source_files.read_bytes(roster, 32 * 1024)
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        if hashlib.sha256(data).hexdigest() != self.roster_sha256:
            raise SourceArchiveError("source_unavailable")
        generation, owners = _owners(roster, self.profile, self.private_runtime_root, self.runtime)
        if generation != self.cutover_generation or owners != self.owners:
            raise SourceArchiveError("source_unavailable")
        _active(deadline, cancelled)

    def read_receipt(self, deadline: float, cancelled: Event | None) -> dict[str, object]:
        self.check_roster(deadline, cancelled)
        try:
            receipt = object_fields(
                parse_json(
                    source_files.read_bytes(self.profile_root / "profile-start.json", _MAX_CONTROL)
                ),
                _START_FIELDS,
            )
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        owner = next(
            (
                item
                for item in self.owners
                if (item.owner_id, str(item.state_root))
                == (receipt["owner_id"], receipt["state_root"])
            ),
            None,
        )
        if (
            owner is None
            or type(receipt["version"]) is not int
            or receipt["version"] != 1
            or receipt["profile"] != self.profile
            or receipt["roster_sha256"] != self.roster_sha256
            or receipt["cold_entitlement_sha256"] != self.cold_entitlement_sha256
            or receipt["cutover_generation"] != self.cutover_generation
            or receipt["daemon_id"] != owner.daemon_id
            or receipt["image_id"] != owner.image_id
        ):
            raise SourceArchiveError("source_unavailable")
        phase, attempt = receipt["phase"], receipt["attempt"]
        if phase == "idle":
            if attempt is not None:
                raise SourceArchiveError("source_unavailable")
        elif phase == "starting":
            try:
                if not isinstance(attempt, str) or str(uuid.UUID(attempt)) != attempt:
                    raise SourceArchiveError("source_unavailable")
            except ValueError:
                raise SourceArchiveError("source_unavailable") from None
        else:
            raise SourceArchiveError("source_unavailable")
        _active(deadline, cancelled)
        return receipt

    def preflight_jobs(
        self, deadline: float, cancelled: Event | None, *, require_all_idle: bool = False
    ) -> ReaderJobs:
        """Inspect existing pairs in order, without provisioning missing state."""

        self.check_roster(deadline, cancelled)
        for owner in self.owners:
            _active(deadline, cancelled)
            try:
                source_files.private_root(owner.state_root, create=False)
                source_files.check_entry(owner.state_root / "reader-job.lock")
                source_files.check_entry(owner.state_root / "reader-job.json")
            except OSError:
                raise SourceArchiveError("source_unavailable") from None
        selected: ReaderJobs | None = None
        for owner in self.owners:
            jobs = ReaderJobs(owner)
            with jobs.locked(deadline, cancelled):
                if jobs.receipts() and (require_all_idle or owner != self.runtime):
                    raise SourceArchiveError("source_unavailable")
            if owner == self.runtime:
                selected = jobs
        if selected is None:
            raise SourceArchiveError("source_unavailable")
        return selected


@dataclass(frozen=True, repr=False)
class MacColdEntitlement:
    runtime: ReaderRuntime
    private_runtime_root: Path
    lima_home: Path
    profile: str
    files: tuple[_FileIdentity, ...]
    file_sizes: tuple[int | None, ...]
    colima: _FileIdentity
    limactl: _FileIdentity
    guest_agent: _FileIdentity
    docker_directory: Path
    docker_present: bool
    docker_hash: str | None
    docker_current_context: str | None
    store_path: Path
    store_semantic_sha256: str
    iso_baseline: ColdIsoBaseline
    ssh_config_normalized_sha256: str
    profile_owners_sha256: str
    effective_config_sha256: str
    cutover_generation: str
    profile_owners: tuple[ReaderRuntime, ...]
    entitlement_sha256: str
    manifest_path: Path

    def profile_authority(self) -> ProfileOwners:
        return ProfileOwners(
            _profile_root(self.private_runtime_root, self.profile, self.runtime),
            self.private_runtime_root,
            self.profile,
            self.runtime,
            self.profile_owners_sha256,
            self.entitlement_sha256,
            self.cutover_generation,
            self.profile_owners,
        )

    def check_persistent(
        self,
        deadline: float,
        cancelled: Event | None,
        *,
        post_start: bool = False,
    ) -> None:
        """Check stopped-safe pins; postwrite store changes need identical semantics."""

        _active(deadline, cancelled)
        root = self.private_runtime_root
        _ownership_enforced(root)
        _trusted_ancestry(root)
        try:
            root_device = root.lstat().st_dev
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        instance, _ = _profile_parts(root, self.lima_home, self.profile)
        profile_root = _profile_root(root, self.profile, self.runtime)
        rewritten = {
            instance / "lima.yaml",
            instance / "colima.yaml",
            instance / "cloud-config.yaml",
            profile_root / "colima.yaml",
        }
        network_root = self.lima_home / "_networks"
        network_directories = {network_root, network_root / "user-v2"}
        # Colima recreates these on each stopped start. Their current private
        # descriptor identities belong to the session, not the enrollment.
        self._network_directories(deadline, cancelled)
        for item, size in zip(self.files, self.file_sizes, strict=True):
            if item.path in network_directories:
                if (item.device, item.owner) != (root_device, _current_uid()):
                    raise SourceArchiveError("source_unavailable")
                continue
            if post_start and item.path == self.store_path:
                continue
            if item.path in rewritten and (
                post_start or item.path == instance / "cloud-config.yaml"
            ):
                try:
                    info = source_files.check_entry(item.path)
                except OSError:
                    raise SourceArchiveError("source_unavailable") from None
                if (
                    info.st_dev != root_device
                    or info.st_size != size
                    or _file_hash(item.path, info, deadline, cancelled) != item.sha256
                ):
                    raise SourceArchiveError("source_unavailable")
                continue
            _check_identity(
                item, private=True, root_device=root_device, deadline=deadline, cancelled=cancelled
            )
            if size is not None:
                try:
                    if item.path.lstat().st_size != size:
                        raise SourceArchiveError("source_unavailable")
                except OSError:
                    raise SourceArchiveError("source_unavailable") from None
        # Lima prefers local templates and mixes these configs into every load.
        for path in (
            self.lima_home / "_templates",
            self.lima_home / "_config" / "default.yaml",
            self.lima_home / "_config" / "override.yaml",
        ):
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise SourceArchiveError("source_unavailable") from None
            raise SourceArchiveError("source_unavailable")
        for item in (self.colima, self.limactl, self.guest_agent):
            _check_identity(
                item, private=True, root_device=root_device, deadline=deadline, cancelled=cancelled
            )
        for executable in (
            root / "bin" / "lima",
            self.runtime.docker,
            self.colima.path,
            self.limactl.path,
            self.guest_agent.path,
        ):
            try:
                mode = executable.lstat().st_mode
            except OSError:
                raise SourceArchiveError("source_unavailable") from None
            if mode & 0o111 == 0:
                raise SourceArchiveError("privacy_rejected")
        if post_start:
            try:
                info = source_files.check_entry(self.store_path)
            except OSError:
                raise SourceArchiveError("source_unavailable") from None
            if (
                info.st_dev != root_device
                or _store_semantics(self.store_path, deadline, cancelled)
                != self.store_semantic_sha256
            ):
                raise SourceArchiveError("source_unavailable")
        else:
            if _store_semantics(self.store_path, deadline, cancelled) != self.store_semantic_sha256:
                raise SourceArchiveError("source_unavailable")
        config = self.docker_directory / "config.json"
        if self.docker_present:
            try:
                data = source_files.read_bytes(config, _MAX_CONTROL)
            except OSError:
                raise SourceArchiveError("source_unavailable") from None
            if hashlib.sha256(data).hexdigest() != self.docker_hash:
                raise SourceArchiveError("source_unavailable")
            parsed = _mapping(parse_json(data, maximum_bytes=_MAX_CONTROL))
            if parsed.get("currentContext") != self.docker_current_context:
                raise SourceArchiveError("source_unavailable")
        elif config.exists() or config.is_symlink():
            raise SourceArchiveError("source_unavailable")
        _active(deadline, cancelled)

    def _receipt_path(self) -> Path:
        return (
            _profile_root(self.private_runtime_root, self.profile, self.runtime)
            / "profile-start.json"
        )

    def _idle_receipt(self) -> dict[str, object]:
        return {
            "version": 1,
            "profile": self.profile,
            "roster_sha256": self.profile_owners_sha256,
            "cold_entitlement_sha256": self.entitlement_sha256,
            "owner_id": self.runtime.owner_id,
            "state_root": str(self.runtime.state_root),
            "daemon_id": self.runtime.daemon_id,
            "image_id": self.runtime.image_id,
            "phase": "idle",
            "attempt": None,
            "cutover_generation": self.cutover_generation,
        }

    def _read_idle(self) -> None:
        try:
            receipt = object_fields(
                parse_json(source_files.read_bytes(self._receipt_path(), _MAX_CONTROL)),
                _START_FIELDS,
            )
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        if receipt != self._idle_receipt() or type(receipt["version"]) is not int:
            raise SourceArchiveError("source_unavailable")

    def _write_receipt(self, phase: str, attempt: str | None) -> None:
        record = {**self._idle_receipt(), "phase": phase, "attempt": attempt}
        try:
            source_files.write_bytes(self._receipt_path(), encode(record), overwrite=True)
        except OSError:
            raise SourceArchiveError("source_unavailable") from None

    def reconcile_start(self, deadline: float, cancelled: Event | None) -> None:
        """Clear an interrupted start only after exact Running and idle-journal proof."""

        _active(deadline, cancelled)
        try:
            manifest = source_files.read_bytes(self.manifest_path, _MAX_MANIFEST)
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        if hashlib.sha256(manifest).hexdigest() != self.entitlement_sha256:
            raise SourceArchiveError("source_unavailable")
        authority = self.profile_authority()
        receipt = authority.read_receipt(deadline, cancelled)
        if receipt["phase"] == "idle":
            return
        if any(
            receipt[field] != value
            for field, value in (
                ("owner_id", self.runtime.owner_id),
                ("state_root", str(self.runtime.state_root)),
                ("daemon_id", self.runtime.daemon_id),
                ("image_id", self.runtime.image_id),
            )
        ):
            raise SourceArchiveError("source_unavailable")
        authority.preflight_jobs(deadline, cancelled, require_all_idle=True)
        session = MacColdSession(self, self._snapshot(deadline, cancelled))
        session.qualify(deadline, cancelled)
        _active(deadline, cancelled)
        self._write_receipt("idle", None)

    def _environment(self) -> dict[str, str]:
        root = self.private_runtime_root
        return {
            "PATH": str(root / "bin") + ":/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "HOME": str(root),
            "TMPDIR": str(root),
            "COLIMA_HOME": str(root / "colima"),
            "COLIMA_CACHE_HOME": str(root / "cache"),
            "LIMA_HOME": str(self.lima_home),
            "DOCKER_CONFIG": str(self.docker_directory),
        }

    def _record(self, deadline: float, cancelled: Event | None) -> dict[str, object]:
        _active(deadline, cancelled)
        result = run_bounded(
            (str(self.limactl.path), "list", "--json", self.profile),
            b"",
            deadline=deadline,
            output_limit=_CONTROL_LIMIT,
            cancelled=cancelled,
            environment=self._environment(),
        )
        if result.returncode != 0:
            raise SourceArchiveError("source_unavailable")
        record = _mapping(parse_json(result.stdout, maximum_bytes=_CONTROL_LIMIT))
        instance, _ = _profile_parts(self.private_runtime_root, self.lima_home, self.profile)
        if (
            record.get("name") != self.profile
            or record.get("status") not in ("Stopped", "Running")
            or record.get("dir") != str(instance)
            or record.get("vmType") != "vz"
            or record.get("arch") != "aarch64"
            or record.get("cpus") != 2
            or record.get("memory") != 3221225472
            or record.get("disk") != 10737418240
            or record.get("additionalDisks")
            != [{"name": self.profile, "format": False, "fsType": "ext4"}]
            or record.get("limaVersion") != "2.2.0"
            or record.get("errors") not in (None, [])
        ):
            raise SourceArchiveError("capability_unavailable")
        binding = MacRuntimeBinding(
            self.runtime,
            self.private_runtime_root,
            self.lima_home,
            self.profile,
            self.limactl.path,
            "2.2.0",
            self.effective_config_sha256,
            self.files,
            self.limactl,
        )
        _validate_config(binding, record.get("config"))
        if record.get("network") not in (None, _mapping(record["config"]).get("networks")):
            raise SourceArchiveError("capability_unavailable")
        return record

    def _context(self, *, running: bool) -> None:
        context_root = self.docker_directory / "contexts"
        if not context_root.exists() and not context_root.is_symlink():
            if running:
                raise SourceArchiveError("source_unavailable")
            return
        name = self.profile
        digest = hashlib.sha256(name.encode()).hexdigest()
        meta_root = context_root / "meta"
        metadata_root = meta_root / digest
        try:
            _repair_private_generated(context_root, self.private_runtime_root, directory=True)
            entries = set(os.listdir(context_root))
            if not entries <= {"meta", "tls"} or (running and "meta" not in entries):
                raise SourceArchiveError("source_unavailable")
            if "tls" in entries:
                tls_root = context_root / "tls"
                _repair_private_generated(tls_root, self.private_runtime_root, directory=True)
                if os.listdir(tls_root):
                    raise SourceArchiveError("source_unavailable")
            if "meta" not in entries:
                return
            _repair_private_generated(meta_root, self.private_runtime_root, directory=True)
            if set(os.listdir(meta_root)) != ({digest} if running else set()):
                raise SourceArchiveError("source_unavailable")
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        if not running:
            return
        try:
            _repair_private_generated(metadata_root, self.private_runtime_root, directory=True)
            source_files.check_entry(metadata_root, directory=True)
            if set(os.listdir(metadata_root)) != {"meta.json"}:
                raise SourceArchiveError("source_unavailable")
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        try:
            _repair_private_generated(
                metadata_root / "meta.json", self.private_runtime_root, directory=False
            )
            metadata = object_fields(
                parse_json(source_files.read_bytes(metadata_root / "meta.json", _MAX_CONTROL)),
                {"Name", "Metadata", "Endpoints"},
            )
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        if (
            metadata["Name"] != name
            or metadata["Metadata"]
            != {"Description": "colima [profile=" + name.removeprefix("colima-") + "]"}
            or metadata["Endpoints"]
            != {"docker": {"Host": "unix://" + str(self.runtime.socket), "SkipTLSVerify": False}}
        ):
            raise SourceArchiveError("source_unavailable")

    def ensure_ready(self, deadline: float, cancelled: Event | None) -> MacColdSession:
        """Resume only the reviewed stopped instance; caller holds profile authority."""

        _active(deadline, cancelled)
        try:
            manifest = source_files.read_bytes(self.manifest_path, _MAX_MANIFEST)
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        if hashlib.sha256(manifest).hexdigest() != self.entitlement_sha256:
            raise SourceArchiveError("source_unavailable")
        self._read_idle()
        self.profile_authority().preflight_jobs(deadline, cancelled, require_all_idle=True)
        self._repair_generated(deadline, cancelled)
        # A user may already have started the profile. Validate tools and backing
        # identities before querying it, allowing only the reviewed rewrites.
        self.check_persistent(deadline, cancelled, post_start=True)
        before = self._record(deadline, cancelled)
        stopped = before["status"] == "Stopped"
        self._context(running=not stopped)
        if stopped:
            # Recheck the store immediately before the irreversible CLI boundary.
            self.check_persistent(deadline, cancelled)
            self._read_idle()
            _active(deadline, cancelled)
            self._write_receipt("starting", str(uuid.uuid4()))
            result = run_bounded(
                (
                    str(self.colima.path),
                    "--profile",
                    self.profile.removeprefix("colima-"),
                    "start",
                    "--save-config=false",
                    "--template=false",
                    "--activate=false",
                    "--ssh-config=false",
                    "--ssh-agent=false",
                    "--mount",
                    "none",
                    "--binfmt=false",
                    "--vz-rosetta=false",
                    "--port-forwarder",
                    "none",
                ),
                b"",
                deadline=deadline,
                output_limit=_CONTROL_LIMIT,
                cancelled=cancelled,
                environment=self._environment(),
                umask=0o077,
            )
            if result.returncode != 0:
                raise SourceArchiveError("source_unavailable")
        session = MacColdSession(self, self._snapshot(deadline, cancelled))
        session.qualify(deadline, cancelled)
        if stopped:
            _active(deadline, cancelled)
            self.profile_authority().preflight_jobs(deadline, cancelled, require_all_idle=True)
            self._write_receipt("idle", None)
        _active(deadline, cancelled)
        return session

    def _snapshot(self, deadline: float, cancelled: Event | None) -> ColdSnapshot:
        self._repair_generated(deadline, cancelled)
        self.check_persistent(deadline, cancelled, post_start=True)
        record = self._record(deadline, cancelled)
        if record["status"] != "Running":
            raise SourceArchiveError("source_unavailable")
        self._context(running=True)
        port = record.get("sshLocalPort")
        if type(port) is not int:
            raise SourceArchiveError("source_unavailable")
        instance, _ = _profile_parts(self.private_runtime_root, self.lima_home, self.profile)
        validate_ssh_configs(
            instance / "ssh.config",
            self.private_runtime_root / "colima" / "ssh_config",
            self.profile,
            port,
            self.ssh_config_normalized_sha256,
            deadline,
            cancelled,
        )
        iso = validate_cidata(instance / "cidata.iso", self.iso_baseline, deadline, cancelled)
        sockets = _sockets(self, deadline, cancelled)
        _preflight(self.runtime, deadline, cancelled)
        try:
            store = source_files.check_entry(self.store_path)
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        context_meta = (
            self.docker_directory
            / "contexts"
            / "meta"
            / hashlib.sha256(self.profile.encode()).hexdigest()
            / "meta.json"
        )
        snapshot = ColdSnapshot(
            iso,
            port,
            sockets,
            (
                store.st_dev,
                store.st_ino,
                store.st_size,
                _file_hash(self.store_path, store, deadline, cancelled),
            ),
            tuple(
                _generated_identity(path, deadline, cancelled)
                for path in (
                    instance / "ssh.config",
                    self.private_runtime_root / "colima" / "ssh_config",
                    context_meta,
                )
            ),
            self._network_directories(deadline, cancelled),
        )
        _active(deadline, cancelled)
        return snapshot

    def _network_directories(
        self, deadline: float, cancelled: Event | None
    ) -> tuple[tuple[int, int], ...]:
        root = self.lima_home / "_networks"
        identities = []
        for path in (root, root / "user-v2"):
            _active(deadline, cancelled)
            try:
                source_files.check_entry(path, directory=True)
            except OSError:
                raise SourceArchiveError("source_unavailable") from None
            identities.append(
                _repair_private_generated(path, self.private_runtime_root, directory=True)
            )
            _active(deadline, cancelled)
        return tuple(identities)

    def _repair_generated(self, deadline: float, cancelled: Event | None) -> None:
        instance, _ = _profile_parts(self.private_runtime_root, self.lima_home, self.profile)
        profile_root = _profile_root(self.private_runtime_root, self.profile, self.runtime)
        for path in (
            instance / "lima.yaml",
            instance / "colima.yaml",
            instance / "cloud-config.yaml",
            instance / "cidata.iso",
            instance / "ssh.config",
            profile_root / "colima.yaml",
            self.private_runtime_root / "colima" / "ssh_config",
        ):
            _active(deadline, cancelled)
            _repair_private_generated(path, self.private_runtime_root, directory=False)


@dataclass(frozen=True, repr=False)
class ColdSnapshot:
    iso: IsoSnapshot
    ssh_port: int
    sockets: tuple[tuple[int, int], ...]
    store: tuple[int, int, int, str]
    generated: tuple[tuple[int, int, int, str], ...]
    networks: tuple[tuple[int, int], ...]


def _generated_identity(
    path: Path, deadline: float, cancelled: Event | None
) -> tuple[int, int, int, str]:
    try:
        info = source_files.check_entry(path)
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    if info.st_size > _MAX_CONTROL:
        raise SourceArchiveError("limit_exceeded")
    return info.st_dev, info.st_ino, info.st_size, _file_hash(path, info, deadline, cancelled)


def _repair_private_generated(path: Path, root: Path, *, directory: bool) -> tuple[int, int]:
    """Narrowly harden helper output already enclosed by a private parent."""

    nonblocking = getattr(os, "O_NONBLOCK", None)
    close_on_exec = getattr(os, "O_CLOEXEC", None)
    if nonblocking is None or close_on_exec is None:
        raise SourceArchiveError("capability_unavailable")
    try:
        source_files.check_entry(path.parent, directory=True)
        flags = os.O_RDONLY | nonblocking | close_on_exec | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, flags)
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        if (
            (not stat.S_ISDIR(opened.st_mode) if directory else not stat.S_ISREG(opened.st_mode))
            or opened.st_uid != _current_uid()
            or opened.st_dev != root.stat().st_dev
            or (not directory and opened.st_nlink != 1)
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise SourceArchiveError("privacy_rejected")
        if opened.st_mode & 0o077:
            source_files.set_private_fd_mode(fd, 0o700 if directory else 0o600)
            current = path.lstat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise SourceArchiveError("source_unavailable")
        after = os.fstat(fd)
        current = path.lstat()
        if after.st_mode & 0o077 or (after.st_dev, after.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise SourceArchiveError("privacy_rejected")
        return after.st_dev, after.st_ino
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    finally:
        os.close(fd)


@dataclass(frozen=True, repr=False)
class MacColdSession:
    entitlement: MacColdEntitlement
    snapshot: ColdSnapshot

    def qualify(self, deadline: float, cancelled: Event | None) -> None:
        """Recheck current generated state without learning a new baseline."""

        if self.entitlement._snapshot(deadline, cancelled) != self.snapshot:
            raise SourceArchiveError("source_unavailable")


def _sockets(
    entitlement: MacColdEntitlement, deadline: float, cancelled: Event | None
) -> tuple[tuple[int, int], ...]:
    instance, _ = _profile_parts(
        entitlement.private_runtime_root, entitlement.lima_home, entitlement.profile
    )
    profile_root = _profile_root(
        entitlement.private_runtime_root, entitlement.profile, entitlement.runtime
    )
    paths = (
        entitlement.runtime.socket,
        profile_root / "containerd.sock",
        *(
            entitlement.lima_home / "_networks" / "user-v2" / name
            for name in ("user-v2_ep.sock", "user-v2_fd.sock", "user-v2_qemu.sock")
        ),
    )
    try:
        root_device = instance.stat().st_dev
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    identities = []
    for path in paths:
        _active(deadline, cancelled)
        try:
            source_files.check_entry(path.parent, directory=True)
            info = path.lstat()
        except OSError:
            raise SourceArchiveError("source_unavailable") from None
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != _current_uid()
            or info.st_mode & 0o077
            or info.st_dev != root_device
        ):
            raise SourceArchiveError("privacy_rejected")
        identities.append((info.st_dev, info.st_ino))
    return tuple(identities)


def _preflight(runtime: ReaderRuntime, deadline: float, cancelled: Event | None) -> None:
    _RuntimeClient(runtime).preflight(deadline, cancelled)


def load_mac_cold_entitlement(path: Path) -> MacColdEntitlement:
    """Load a closed private cold manifest without requiring a live socket."""

    if not isinstance(path, Path):
        raise SourceArchiveError()
    try:
        payload = source_files.read_bytes(path, _MAX_MANIFEST)
        document = object_fields(parse_json(payload), _FIELDS)
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    if (
        document["format"] != "tedd-mac-reader-cold"
        or type(document["version"]) is not int
        or document["version"] != 1
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
    _profile_parts(root, lima_home, profile)
    runtime_value = object_fields(document["runtime"], _RUNTIME_FIELDS)
    runtime = ReaderRuntime(
        _path(runtime_value["docker"]),
        _stopped_socket(runtime_value["socket"]),
        _path(runtime_value["state_root"]),
        cast(str, runtime_value["owner_id"]),
        cast(str, runtime_value["daemon_id"]),
        cast(str, runtime_value["image_id"]),
    )
    for internal in (runtime.docker, runtime.socket, runtime.state_root):
        _inside(internal, root)
    docker = object_fields(document["docker_config_state"], _DOCKER_FIELDS)
    docker_directory = _path(docker["directory"])
    _inside(docker_directory, root)
    if type(docker["present"]) is not bool:
        raise SourceArchiveError()
    present = docker["present"]
    if present:
        docker_hash = _hash(docker["sha256"])
        context = docker["current_context"]
        if context is not None and not isinstance(context, str):
            raise SourceArchiveError()
    else:
        if docker["sha256"] is not None or docker["current_context"] is not None:
            raise SourceArchiveError()
        docker_hash = None
        context = None
    raw_files = document["persistent_files"]
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > 128:
        raise SourceArchiveError()
    decoded = tuple(_persistent(value, root) for value in raw_files)
    files = tuple(item for item, _ in decoded)
    sizes = tuple(size for _, size in decoded)
    _inventory(files, root, lima_home, profile, runtime, docker_directory)
    indexed = {item.path: item for item in files}
    enrolled_sizes = {item.path: size for item, size in decoded}
    instance, disk_root = _profile_parts(root, lima_home, profile)
    cache = next(path for path in indexed if path.parent == root / "cache" / "caches")
    for storage in (instance / "disk", instance / "vz-efi", disk_root / "datadisk", cache):
        size = enrolled_sizes[storage]
        if size is None or size <= 0:
            raise SourceArchiveError()
    store = root / "colima" / "_store" / f"{profile}.json"
    roster = _profile_root(root, profile, runtime) / "profile-owners.json"
    if indexed[roster].sha256 != _hash(document["profile_owners_sha256"]):
        raise SourceArchiveError()
    generation, owners = _owners(roster, profile, root, runtime)
    iso = ColdIsoBaseline(
        cast(dict[str, str], document["cidata_entries"]),
        _hash(document["meta_data_normalized_sha256"]),
        _hash(document["lima_env_normalized_sha256"]),
    )
    # Lima's ISO-backed layout embeds these files, without instance-root siblings.
    if not {"user-data", "network-config"} <= iso.entries.keys():
        raise SourceArchiveError()
    entitlement = MacColdEntitlement(
        runtime,
        root,
        lima_home,
        profile,
        files,
        sizes,
        _tool(document["colima"], "0.10.3", root),
        _tool(document["limactl"], "2.2.0", root),
        _tool(document["guest_agent"], "2.2.0", root),
        docker_directory,
        present,
        docker_hash,
        context,
        store,
        _hash(document["store_semantic_sha256"]),
        iso,
        _hash(document["ssh_config_normalized_sha256"]),
        _hash(document["profile_owners_sha256"]),
        _hash(document["effective_config_sha256"]),
        generation,
        owners,
        hashlib.sha256(payload).hexdigest(),
        path,
    )
    entitlement.check_persistent(time.monotonic() + 10, None, post_start=True)
    return entitlement


def validate_ssh_configs(
    lima: Path,
    aggregate: Path,
    profile: str,
    port: int,
    normalized_sha256: str,
    deadline: float,
    cancelled: Event | None,
) -> None:
    """Accept one reviewed SSH stanza with only a dynamic loopback port."""

    _active(deadline, cancelled)
    if type(port) is not int or not 0 < port <= 65535 or not isinstance(profile, str):
        raise SourceArchiveError()
    expected = _hash(normalized_sha256)
    try:
        profile_bytes = profile.encode("ascii")
    except UnicodeError:
        raise SourceArchiveError() from None
    try:
        lima_bytes = source_files.read_bytes(lima, _MAX_CONTROL)
        aggregate_bytes = source_files.read_bytes(aggregate, _MAX_CONTROL)
    except OSError:
        raise SourceArchiveError("source_unavailable") from None
    _active(deadline, cancelled)
    lines = lima_bytes.splitlines(keepends=True)
    directives = [line for line in lines if line.strip() and not line.lstrip().startswith(b"#")]
    fields = [re.split(rb"[ \t=]+", line.strip(), maxsplit=1) for line in directives]
    host = b"Host lima-" + profile_bytes + b"\n"
    if (
        not directives
        or directives[0] != host
        or any(len(field) != 2 for field in fields)
        or any(not line.endswith(b"\n") or b"\r" in line or b"\0" in line for line in lines)
    ):
        raise SourceArchiveError()
    keys = [field[0].lower() for field in fields[1:]]
    if (
        any(key in {b"host", b"match", b"include", b"proxycommand", b"proxyjump"} for key in keys)
        or keys.count(b"hostname") != 1
        or keys.count(b"port") != 1
    ):
        raise SourceArchiveError()
    # Colima 0.10.3 preserves comments, renames Host, then Fprintln adds a newline.
    if aggregate_bytes != lima_bytes.replace(host, b"Host " + profile_bytes + b"\n", 1) + b"\n":
        raise SourceArchiveError()
    if next(field[1] for field in fields if field[0].lower() == b"hostname") != b"127.0.0.1":
        raise SourceArchiveError()
    matches = list(_SSH_PORT.finditer(lima_bytes))
    if len(matches) != 1 or int(matches[0].group(1)) != port:
        raise SourceArchiveError()
    normalized = lima_bytes[: matches[0].start(1)] + b"<port>" + lima_bytes[matches[0].end(1) :]
    if hashlib.sha256(normalized).hexdigest() != expected:
        raise SourceArchiveError("source_unavailable")
