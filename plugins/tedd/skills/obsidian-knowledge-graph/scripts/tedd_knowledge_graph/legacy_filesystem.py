"""Filesystem trust, atomic publication, and lock policy for legacy stores."""

from __future__ import annotations

import contextlib
import stat
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

JsonObject = dict[str, object]


class ModeHasAny(Protocol):
    def __call__(self, subject: str | Path | int, mask: int) -> bool: ...


class OwnerIsCurrentUser(Protocol):
    def __call__(
        self,
        subject: str | Path | int,
        *,
        allow_root: bool = False,
    ) -> bool: ...


class WaitForWindowsInitialization(Protocol):
    def __call__(
        self,
        descriptor: int,
        lock_path: Path,
        label: str,
        flags: int,
        acquisition_deadline: float,
    ) -> int | None: ...


@dataclass(frozen=True)
class PathSecurityDependencies:
    """Platform hooks used to validate and harden canonical state paths."""

    absolute_path: Callable[[str], str]
    uses_posix_permissions: Callable[[], bool]
    mode_has_any: ModeHasAny
    owner_is_current_user: OwnerIsCurrentUser
    is_link_or_reparse_point: Callable[[Path], bool]
    set_private_path_mode: Callable[[Path, int], None]


@dataclass(frozen=True)
class AtomicWriteDependencies:
    """Hooks required to stage, sync, and atomically publish one file."""

    sync_descriptor: Callable[[int], None]
    set_private_path_mode: Callable[[Path, int], None]
    open_temp_text: Callable[[int], AbstractContextManager[IO[str]]]
    make_temp_file: Callable[[str, str], tuple[int, str]]
    ensure_private_directory: Callable[[Path], None]
    replace_file_atomically: Callable[[Path, Path], None]
    fsync_parent_directory: Callable[[Path], None]


@dataclass(frozen=True)
class LockDependencies:
    """Hooks required to create, validate, wait for, and release one lock."""

    windows_lock_initialization_timeout_seconds: float
    windows_lock_initialization_poll_seconds: float
    open_read_write: int
    open_create: int
    open_exclusive: int
    open_no_follow: int
    uses_posix_permissions: Callable[[], bool]
    owner_is_current_user: OwnerIsCurrentUser
    is_link_or_reparse_point: Callable[[Path], bool]
    set_private_fd_mode: Callable[[int, int], None]
    try_file_lock: Callable[[IO[str]], None]
    unlock_file: Callable[[IO[str]], None]
    descriptor_stat: Callable[[int], object]
    open_existing_descriptor: Callable[[Path, int], int]
    create_descriptor: Callable[[Path, int, int], int]
    close_descriptor: Callable[[int], None]
    open_lock_text: Callable[[int], AbstractContextManager[IO[str]]]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    ensure_private_directory: Callable[[Path], None]
    validate_open_lock_descriptor: Callable[[int, Path, str], None]
    open_existing_lock_descriptor: Callable[[Path, str, int], int]
    wait_before_lock_path_retry: Callable[[Path, str, float], None]
    wait_for_windows_lock_initialization: WaitForWindowsInitialization
    open_private_lock_descriptor: Callable[[Path, str, float], int]


def _descriptor_mode(stat_result: object) -> int:
    mode = getattr(stat_result, "st_mode", None)
    if not isinstance(mode, int):
        raise TypeError("Descriptor stat must include an integer st_mode")
    return mode


def _descriptor_identity(stat_result: object) -> tuple[int, int]:
    device = getattr(stat_result, "st_dev", None)
    inode = getattr(stat_result, "st_ino", None)
    if not isinstance(device, int) or not isinstance(inode, int):
        raise TypeError("Descriptor stat must include integer device and inode values")
    return device, inode


def ensure_private_directory(path: Path, dependencies: PathSecurityDependencies) -> None:
    if path.exists():
        if not path.is_dir():
            raise ValueError(f"Expected a directory: {path}")
        return
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    dependencies.set_private_path_mode(path, 0o700)
    if dependencies.mode_has_any(path, 0o077):
        raise ValueError(f"Could not make directory private: {path}")


def secure_state_path(
    value: object,
    label: str,
    dependencies: PathSecurityDependencies,
) -> Path:
    lexical = Path(dependencies.absolute_path(str(value)))
    if dependencies.is_link_or_reparse_point(lexical):
        raise ValueError(f"{label} path cannot be a symlink: {lexical}")
    current = Path(lexical.anchor)
    for part in lexical.parent.parts[1:]:
        candidate_part = current / part
        if dependencies.is_link_or_reparse_point(candidate_part):
            link_stat = candidate_part.lstat()
            trusted_system_link = (
                dependencies.uses_posix_permissions()
                and link_stat.st_uid == 0
                and not dependencies.mode_has_any(current, 0o022)
            )
            if not trusted_system_link:
                raise ValueError(
                    f"{label} parent path contains an untrusted symlink: {candidate_part}"
                )
        if candidate_part.exists() and not dependencies.is_link_or_reparse_point(candidate_part):
            part_stat = candidate_part.stat()
            sticky_system_directory = bool(
                dependencies.uses_posix_permissions()
                and part_stat.st_uid == 0
                and dependencies.mode_has_any(candidate_part, stat.S_ISVTX)
            )
            if not dependencies.owner_is_current_user(candidate_part, allow_root=True) or (
                dependencies.mode_has_any(candidate_part, 0o022) and not sticky_system_directory
            ):
                raise ValueError(f"{label} parent path is not trusted: {candidate_part}")
        current = candidate_part
    resolved_parent = lexical.parent.resolve()
    candidate = resolved_parent / lexical.name
    if dependencies.is_link_or_reparse_point(candidate):
        raise ValueError(f"{label} path cannot be a symlink: {candidate}")
    existing_parent = resolved_parent
    while not existing_parent.exists() and existing_parent != existing_parent.parent:
        existing_parent = existing_parent.parent
    if not existing_parent.is_dir():
        raise ValueError(f"{label} parent is not a directory: {existing_parent}")
    if not dependencies.owner_is_current_user(existing_parent):
        raise ValueError(f"{label} parent is not owned by the current user: {existing_parent}")
    if dependencies.mode_has_any(existing_parent, 0o022):
        raise ValueError(f"{label} parent is group/world writable: {existing_parent}")
    if candidate.exists():
        candidate_stat = candidate.stat()
        if not stat.S_ISREG(candidate_stat.st_mode):
            raise ValueError(f"{label} must be a regular file: {candidate}")
        if not dependencies.owner_is_current_user(candidate):
            raise ValueError(f"{label} is not owned by the current user: {candidate}")
        if dependencies.mode_has_any(candidate, 0o077):
            raise ValueError(f"{label} permissions are too broad; require mode 0600: {candidate}")
    return candidate


def path_is_within(path: Path, parent: Path) -> bool:
    resolved_path = path.resolve()
    resolved_parent = parent.resolve()
    return resolved_path == resolved_parent or resolved_parent in resolved_path.parents


def state_privacy_warnings(
    config_path: Path,
    store_path: Path,
    config: JsonObject,
    *,
    normalize_space: Callable[[object], str],
) -> list[str]:
    raw_vault = normalize_space(config.get("vault_path"))
    if not raw_vault:
        return []
    vault = Path(raw_vault).expanduser().resolve()
    warnings: list[str] = []
    if path_is_within(config_path, vault):
        warnings.append("configuration path is inside the Obsidian vault")
    if path_is_within(store_path, vault):
        warnings.append("canonical store path is inside the Obsidian vault")
    return warnings


def atomic_write(
    path: Path,
    content: str,
    mode: int,
    dependencies: AtomicWriteDependencies,
) -> None:
    dependencies.ensure_private_directory(path.parent)
    descriptor, temp_name = dependencies.make_temp_file(f".{path.name}.", str(path.parent))
    temp_path = Path(temp_name)
    try:
        with dependencies.open_temp_text(descriptor) as handle:
            handle.write(content)
            handle.flush()
            dependencies.sync_descriptor(handle.fileno())
        dependencies.set_private_path_mode(temp_path, mode)
        dependencies.replace_file_atomically(temp_path, path)
        dependencies.fsync_parent_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def validate_open_lock_descriptor(
    descriptor: int,
    lock_path: Path,
    label: str,
    dependencies: LockDependencies,
) -> None:
    opened_stat = dependencies.descriptor_stat(descriptor)
    if not stat.S_ISREG(_descriptor_mode(opened_stat)):
        raise ValueError(f"{label} lock must be a regular file: {lock_path}")
    if dependencies.is_link_or_reparse_point(lock_path):
        raise ValueError(f"{label} lock cannot be a symlink: {lock_path}")
    path_stat = lock_path.stat()
    if not stat.S_ISREG(path_stat.st_mode):
        raise ValueError(f"{label} lock must be a regular file: {lock_path}")
    if _descriptor_identity(opened_stat) != (path_stat.st_dev, path_stat.st_ino):
        raise ValueError(f"{label} lock changed while opening: {lock_path}")


def open_existing_lock_descriptor(
    lock_path: Path,
    label: str,
    flags: int,
    dependencies: LockDependencies,
) -> int:
    if dependencies.is_link_or_reparse_point(lock_path):
        raise ValueError(f"{label} lock cannot be a symlink: {lock_path}")
    descriptor = dependencies.open_existing_descriptor(lock_path, flags)
    try:
        dependencies.validate_open_lock_descriptor(descriptor, lock_path, label)
    except BaseException:
        dependencies.close_descriptor(descriptor)
        raise
    return descriptor


def wait_before_lock_path_retry(
    lock_path: Path,
    label: str,
    acquisition_deadline: float,
    dependencies: LockDependencies,
) -> None:
    remaining = acquisition_deadline - dependencies.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            f"Timed out waiting for {label.lower()} lock path to stabilize: {lock_path}"
        )
    dependencies.sleep(min(dependencies.windows_lock_initialization_poll_seconds, remaining))
    if dependencies.monotonic() >= acquisition_deadline:
        raise TimeoutError(
            f"Timed out waiting for {label.lower()} lock path to stabilize: {lock_path}"
        )


def wait_for_windows_lock_initialization(
    descriptor: int,
    lock_path: Path,
    label: str,
    flags: int,
    acquisition_deadline: float,
    dependencies: LockDependencies,
) -> int | None:
    current: int | None = descriptor
    try:
        assert current is not None
        if dependencies.uses_posix_permissions() or not dependencies.owner_is_current_user(
            current, allow_root=True
        ):
            return current
        initialization_deadline = min(
            acquisition_deadline,
            dependencies.monotonic() + dependencies.windows_lock_initialization_timeout_seconds,
        )
        while True:
            remaining = initialization_deadline - dependencies.monotonic()
            if remaining <= 0:
                assert current is not None
                dependencies.close_descriptor(current)
                return None
            assert current is not None
            dependencies.close_descriptor(current)
            current = None
            dependencies.sleep(
                min(dependencies.windows_lock_initialization_poll_seconds, remaining)
            )
            if dependencies.monotonic() >= initialization_deadline:
                return None
            current = dependencies.open_existing_lock_descriptor(lock_path, label, flags)
            if dependencies.owner_is_current_user(current):
                return current
            if not dependencies.owner_is_current_user(current, allow_root=True):
                return current
    except BaseException:
        if current is not None:
            dependencies.close_descriptor(current)
        raise


def open_private_lock_descriptor(
    lock_path: Path,
    label: str,
    acquisition_deadline: float,
    dependencies: LockDependencies,
) -> int:
    flags = dependencies.open_read_write | dependencies.open_no_follow
    while True:
        try:
            descriptor = dependencies.create_descriptor(
                lock_path,
                flags | dependencies.open_create | dependencies.open_exclusive,
                0o600,
            )
            created = True
        except FileExistsError:
            try:
                descriptor = dependencies.open_existing_lock_descriptor(lock_path, label, flags)
            except FileNotFoundError:
                dependencies.wait_before_lock_path_retry(lock_path, label, acquisition_deadline)
                continue
            created = False
        try:
            if created:
                dependencies.validate_open_lock_descriptor(descriptor, lock_path, label)
            if not created and not dependencies.owner_is_current_user(descriptor):
                waiting_descriptor = descriptor
                descriptor = -1
                try:
                    reopened = dependencies.wait_for_windows_lock_initialization(
                        waiting_descriptor,
                        lock_path,
                        label,
                        flags,
                        acquisition_deadline,
                    )
                except FileNotFoundError:
                    dependencies.wait_before_lock_path_retry(lock_path, label, acquisition_deadline)
                    continue
                descriptor = -1 if reopened is None else reopened
                if descriptor == -1 or not dependencies.owner_is_current_user(descriptor):
                    raise ValueError(f"{label} lock is not owned by the current user: {lock_path}")
            dependencies.set_private_fd_mode(descriptor, 0o600)
            if not dependencies.owner_is_current_user(descriptor):
                raise ValueError(f"{label} lock is not owned by the current user: {lock_path}")
            dependencies.validate_open_lock_descriptor(descriptor, lock_path, label)
        except BaseException:
            if descriptor != -1:
                dependencies.close_descriptor(descriptor)
            raise
        return descriptor


@contextlib.contextmanager
def store_lock(
    store_path: Path,
    timeout_seconds: float,
    dependencies: LockDependencies,
) -> Iterator[None]:
    lock_path = store_path.with_suffix(store_path.suffix + ".lock")
    dependencies.ensure_private_directory(lock_path.parent)
    deadline = dependencies.monotonic() + max(0.0, timeout_seconds)
    descriptor = dependencies.open_private_lock_descriptor(lock_path, "Store", deadline)
    with dependencies.open_lock_text(descriptor) as handle:
        while True:
            try:
                dependencies.try_file_lock(handle)
                break
            except BlockingIOError:
                if dependencies.monotonic() >= deadline:
                    raise TimeoutError(
                        "Timed out waiting for canonical graph lock after "
                        f"{timeout_seconds:g}s: {lock_path}"
                    ) from None
                dependencies.sleep(min(0.05, max(0.0, deadline - dependencies.monotonic())))
        try:
            yield
        finally:
            dependencies.unlock_file(handle)


__all__ = [
    "AtomicWriteDependencies",
    "LockDependencies",
    "PathSecurityDependencies",
    "atomic_write",
    "ensure_private_directory",
    "open_existing_lock_descriptor",
    "open_private_lock_descriptor",
    "path_is_within",
    "secure_state_path",
    "state_privacy_warnings",
    "store_lock",
    "validate_open_lock_descriptor",
    "wait_before_lock_path_retry",
    "wait_for_windows_lock_initialization",
]
