"""Path safety, publication, key, and connection lifecycle kernels for v4.

This leaf deliberately imports no flat knowledge-graph facade.  The public
``knowledge_graph_v4`` module resolves its live filesystem, security, schema,
clock, and construction callbacks for every call so historical monkeypatch
seams remain effective.
"""

from __future__ import annotations

import contextlib
import hashlib
import mmap
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict

PublishedIdentity = tuple[int, int]
SecuritySubject = Path | int
SchemaTriggerDefinitions = tuple[tuple[str, str], ...]


class StorePathAssessment(TypedDict):
    path: str
    filesystem_type: str | None
    safe: bool
    reasons: list[str]
    coordination_scope: str


class OwnerCheck(Protocol):
    def __call__(self, subject: SecuritySubject, *, allow_root: bool = False) -> bool: ...


class ModeCheck(Protocol):
    def __call__(self, subject: SecuritySubject, mask: int) -> bool: ...


class StoreBuilder(Protocol):
    def __call__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        *,
        readonly: bool,
        plan_hmac_key: bytes | None,
    ) -> object: ...


class OpenStore(Protocol):
    def __call__(
        self,
        path: str | Path,
        *,
        readonly: bool = False,
        plan_hmac_key: bytes | str | None = None,
    ) -> object: ...


class Connect(Protocol):
    def __call__(
        self,
        database: str | Path,
        timeout: float = 5.0,
        *,
        uri: bool = False,
    ) -> sqlite3.Connection: ...


class TemporaryFileFactory(Protocol):
    def __call__(
        self,
        *,
        prefix: str,
        suffix: str = "",
        dir: str | Path | None = None,
    ) -> tuple[int, str]: ...


class AtomicBytes(Protocol):
    def __call__(
        self,
        path: Path,
        content: bytes,
        mode: int = 0o600,
        *,
        overwrite: bool = True,
    ) -> None: ...


class PublishStagedFile(Protocol):
    def __call__(
        self,
        source: Path,
        destination: Path,
        *,
        overwrite: bool,
    ) -> PublishedIdentity: ...


class ReadPublishedBytes(Protocol):
    def __call__(
        self,
        path: Path,
        expected_identity: PublishedIdentity,
        *,
        maximum_bytes: int,
    ) -> bytes: ...


class LoadPlanKey(Protocol):
    def __call__(
        self,
        store_path: Path,
        supplied: bytes | str | None,
        *,
        create: bool,
    ) -> bytes | None: ...


class ValidatePlanKeyStat(Protocol):
    def __call__(
        self,
        key_path: Path,
        key_stat: os.stat_result,
        descriptor: int | None = None,
        *,
        allow_publication_link: bool = False,
    ) -> None: ...


class ConfigureConnection(Protocol):
    def __call__(
        self,
        connection: sqlite3.Connection,
        *,
        readonly: bool,
        wal: bool = True,
    ) -> None: ...


@dataclass(frozen=True)
class MountDependencies:
    nearest_existing_parent: Callable[[Path], Path]
    sys_platform_is_macos: Callable[[], bool]
    windows_volume_type: Callable[[Path], str | None]


@dataclass(frozen=True)
class PathInspectionDependencies:
    network_fs_types: frozenset[str]
    cloud_path_markers: tuple[str, ...]
    is_link_or_reparse_point: Callable[[Path], bool]
    mode_has_any: ModeCheck
    owner_is_current_user: OwnerCheck
    uses_posix_permissions: Callable[[], bool]
    mount_type: Callable[[Path], str | None]
    nearest_existing_parent: Callable[[Path], Path]


@dataclass(frozen=True)
class PrivateDirectoryDependencies:
    mode_has_any: ModeCheck
    owner_is_current_user: OwnerCheck
    set_private_path_mode: Callable[[Path, int], None]
    path_safety_error_type: type[Exception]


@dataclass(frozen=True)
class StageDependencies:
    ensure_private_directory: Callable[[Path], None]
    set_private_path_mode: Callable[[Path, int], None]


@dataclass(frozen=True)
class AtomicDependencies:
    stage_bytes: Callable[[Path, bytes, int], Path]
    publish_staged_file: PublishStagedFile
    fsync_parent: Callable[[Path], None]


@dataclass(frozen=True)
class PublishedFileDependencies:
    owner_is_current_user: OwnerCheck
    mode_has_any: ModeCheck
    concurrency_conflict_type: type[Exception]
    path_safety_error_type: type[Exception]


@dataclass(frozen=True)
class ReadPublishedDependencies:
    published_file_descriptor: Callable[[Path, PublishedIdentity], tuple[int, os.stat_result]]
    concurrency_conflict_type: type[Exception]
    validation_error_type: type[Exception]


@dataclass(frozen=True)
class DetectStoreDependencies:
    sqlite_header: bytes
    schema_version: str
    sqlite_readonly_uri: Callable[[Path], str]


@dataclass(frozen=True)
class PlanKeyStatDependencies:
    owner_is_current_user: OwnerCheck
    mode_has_any: ModeCheck
    path_safety_error_type: type[Exception]


@dataclass(frozen=True)
class ReadPlanKeyDependencies:
    validate_plan_key_stat: ValidatePlanKeyStat
    key_bytes: Callable[[bytes | str], bytes]
    path_safety_error_type: type[Exception]


@dataclass(frozen=True)
class PublishedPlanKeyDependencies:
    retry_attempts: int
    retry_delay_seconds: float
    sleep: Callable[[float], None]
    read_private_plan_key: Callable[[Path], bytes]
    validate_plan_key_stat: ValidatePlanKeyStat
    path_safety_error_type: type[Exception]


@dataclass(frozen=True)
class LoadPlanKeyDependencies:
    key_path: Callable[[Path], Path]
    key_bytes: Callable[[bytes | str], bytes]
    b64encode: Callable[[bytes], str]
    atomic_bytes: AtomicBytes
    read_private_plan_key_after_publication: Callable[[Path], bytes]
    token_bytes: Callable[[int], bytes]
    compare_digest: Callable[[bytes, bytes], bool]
    path_safety_error_type: type[Exception]


@dataclass(frozen=True)
class CreateStoreDependencies:
    assert_safe_path: Callable[[Path], Path]
    detect_store: Callable[[str | Path], str]
    ensure_private_directory: Callable[[Path], None]
    make_temporary_file: TemporaryFileFactory
    close_descriptor: Callable[[int], None]
    connect: Connect
    configure_connection: ConfigureConnection
    ddl: str
    schema_version: str
    schema_user_version: int
    utc_now: Callable[[], str]
    uuid4: Callable[[], object]
    set_private_path_mode: Callable[[Path, int], None]
    publish_staged_file: PublishStagedFile
    fsync_parent: Callable[[Path], None]
    load_or_create_plan_key: LoadPlanKey
    open_store: OpenStore
    validation_error_type: type[Exception]


@dataclass(frozen=True)
class OpenStoreDependencies:
    assert_safe_path: Callable[[Path], Path]
    detect_store: Callable[[str | Path], str]
    owner_is_current_user: OwnerCheck
    mode_has_any: ModeCheck
    connect: Connect
    sqlite_readonly_uri: Callable[[Path], str]
    configure_connection: ConfigureConnection
    busy_timeout_ms: int
    schema_version: str
    previous_schema_user_version: int
    schema_user_version: int
    record_revision_payloads_table_sql: str
    legacy_trigger_sql: SchemaTriggerDefinitions
    upgrade_trigger_sql: SchemaTriggerDefinitions
    required_trigger_sql: SchemaTriggerDefinitions
    load_or_create_plan_key: LoadPlanKey
    build_store: StoreBuilder
    path_safety_error_type: type[Exception]
    store_format_error_type: type[Exception]


class StoreSession:
    """Own one facade store's connection and immutable opening policy."""

    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        *,
        readonly: bool,
        plan_hmac_key: bytes | None,
    ) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = connection
        self.readonly = readonly
        self.plan_hmac_key = plan_hmac_key

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def connection(self, *, closed_error_type: type[Exception]) -> sqlite3.Connection:
        if self._connection is None:
            raise closed_error_type("Store is closed")
        return self._connection


def nearest_existing_parent(path: Path) -> Path:
    candidate = path if path.exists() else path.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def sys_platform_is_macos() -> bool:
    return os.uname().sysname == "Darwin" if hasattr(os, "uname") else False


def mount_type(
    path: Path,
    *,
    dependencies: MountDependencies,
) -> str | None:
    """Best-effort local filesystem type detection on supported hosts."""

    existing = dependencies.nearest_existing_parent(path).resolve()
    detected_windows_type = dependencies.windows_volume_type(existing)
    if detected_windows_type:
        return detected_windows_type
    if Path("/proc/self/mountinfo").is_file():
        best: tuple[int, str] | None = None
        try:
            lines = (
                Path("/proc/self/mountinfo")
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
            )
            for line in lines:
                left, right = line.split(" - ", 1)
                fields = left.split()
                mountpoint = fields[4].replace("\\040", " ")
                fs_type = right.split()[0]
                try:
                    existing.relative_to(Path(mountpoint))
                except ValueError:
                    continue
                length = len(mountpoint)
                if best is None or length > best[0]:
                    best = (length, fs_type)
            return best[1].lower() if best else None
        except (OSError, ValueError, IndexError):
            return None
    if dependencies.sys_platform_is_macos():
        try:
            result = subprocess.run(
                ["/usr/bin/stat", "-f", "%T", str(existing)],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
            return result.stdout.strip().lower() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


def inspect_store_path(
    path: str | Path,
    *,
    dependencies: PathInspectionDependencies,
) -> StorePathAssessment:
    lexical = Path(os.path.abspath(os.path.expanduser(str(path))))
    reasons: list[str] = []
    if dependencies.is_link_or_reparse_point(lexical):
        reasons.append("store path is a symlink")
    current = Path(lexical.anchor)
    for part in lexical.parent.parts[1:]:
        candidate = current / part
        if dependencies.is_link_or_reparse_point(candidate):
            try:
                link_stat = candidate.lstat()
                parent_stat = current.stat()
                trusted_system_link = bool(
                    dependencies.uses_posix_permissions()
                    and link_stat.st_uid == 0
                    and not dependencies.mode_has_any(current, 0o022)
                )
            except OSError:
                trusted_system_link = False
            if not trusted_system_link:
                reasons.append(f"parent contains untrusted symlink: {candidate}")
                break
        current = candidate
    folded = f"/{lexical.as_posix().strip('/').casefold()}/"
    if any(marker in folded for marker in dependencies.cloud_path_markers):
        reasons.append("path is inside a known cloud-synchronised directory")
    fs_type = dependencies.mount_type(lexical)
    if fs_type and (
        fs_type in dependencies.network_fs_types
        or fs_type.startswith("fuse.")
        and fs_type not in {"fuse.apfs"}
    ):
        reasons.append(f"filesystem {fs_type!r} is not supported for SQLite WAL")
    existing_parent = dependencies.nearest_existing_parent(lexical)
    try:
        parent_stat = existing_parent.stat()
        if not dependencies.owner_is_current_user(existing_parent, allow_root=True):
            reasons.append("nearest existing parent is not owned by the current user")
        if dependencies.mode_has_any(existing_parent, 0o022) and not (
            dependencies.uses_posix_permissions()
            and parent_stat.st_uid == 0
            and dependencies.mode_has_any(existing_parent, stat.S_ISVTX)
        ):
            reasons.append("nearest existing parent is group/world writable")
    except OSError as error:
        reasons.append(f"cannot inspect parent: {error}")
    return {
        "path": str(lexical),
        "filesystem_type": fs_type,
        "safe": not reasons,
        "reasons": reasons,
        "coordination_scope": "same-machine-only",
    }


def assert_safe_path(
    path: Path,
    *,
    inspect_path: Callable[[str | Path], StorePathAssessment],
    path_safety_error_type: type[Exception],
) -> Path:
    assessment = inspect_path(path)
    if not assessment["safe"]:
        raise path_safety_error_type("; ".join(assessment["reasons"]))
    return Path(assessment["path"])


def ensure_private_directory(
    path: Path,
    *,
    dependencies: PrivateDirectoryDependencies,
) -> None:
    if path.exists():
        if not path.is_dir():
            raise dependencies.path_safety_error_type(f"Expected a directory: {path}")
        if not dependencies.owner_is_current_user(path):
            raise dependencies.path_safety_error_type(
                f"Directory is not owned by current user: {path}"
            )
        if dependencies.mode_has_any(path, 0o077):
            raise dependencies.path_safety_error_type(f"Directory permissions must be 0700: {path}")
        return
    path.mkdir(parents=True, mode=0o700)
    dependencies.set_private_path_mode(path, 0o700)


def stage_bytes(
    path: Path,
    content: bytes,
    mode: int = 0o600,
    *,
    dependencies: StageDependencies,
) -> Path:
    dependencies.ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        dependencies.set_private_path_mode(temp_path, mode)
        with temp_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        return temp_path
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        raise


def atomic_bytes(
    path: Path,
    content: bytes,
    mode: int = 0o600,
    *,
    overwrite: bool = True,
    dependencies: AtomicDependencies,
) -> None:
    temp_path = dependencies.stage_bytes(path, content, mode)
    try:
        dependencies.publish_staged_file(temp_path, path, overwrite=overwrite)
        dependencies.fsync_parent(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def published_file_descriptor(
    path: Path,
    expected_identity: PublishedIdentity,
    *,
    dependencies: PublishedFileDependencies,
) -> tuple[int, os.stat_result]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise dependencies.concurrency_conflict_type(
                f"Published backup path is not a regular file: {path}"
            )
        if (file_stat.st_dev, file_stat.st_ino) != expected_identity:
            raise dependencies.concurrency_conflict_type(
                f"Published backup path changed before verification: {path}"
            )
        if not dependencies.owner_is_current_user(descriptor):
            raise dependencies.path_safety_error_type(
                f"Published backup is not owned by the current user: {path}"
            )
        if dependencies.mode_has_any(descriptor, 0o077):
            raise dependencies.path_safety_error_type(
                f"Published backup permissions must be 0600: {path}"
            )
        if file_stat.st_nlink != 1:
            raise dependencies.path_safety_error_type(
                f"Published backup must not be hard-linked: {path}"
            )
        return descriptor, file_stat
    except Exception:
        os.close(descriptor)
        raise


def read_verified_published_bytes(
    path: Path,
    expected_identity: PublishedIdentity,
    *,
    maximum_bytes: int,
    dependencies: ReadPublishedDependencies,
) -> bytes:
    descriptor, before = dependencies.published_file_descriptor(path, expected_identity)
    try:
        if before.st_size > maximum_bytes:
            raise dependencies.validation_error_type(
                f"Published file is unexpectedly large: {path}"
            )
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise dependencies.validation_error_type(
                f"Published file is unexpectedly large: {path}"
            )
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise dependencies.concurrency_conflict_type(
                f"Published file changed while verifying: {path}"
            )
        return payload
    finally:
        os.close(descriptor)


def sha256_verified_published_file(
    path: Path,
    expected_identity: PublishedIdentity,
    *,
    dependencies: ReadPublishedDependencies,
) -> tuple[str, int]:
    descriptor, before = dependencies.published_file_descriptor(path, expected_identity)
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise dependencies.concurrency_conflict_type(
                f"Published backup changed while hashing: {path}"
            )
        return digest.hexdigest(), int(after.st_size)
    finally:
        os.close(descriptor)


def detect_store(
    path: str | Path,
    *,
    dependencies: DetectStoreDependencies,
) -> str:
    candidate = Path(path).expanduser()
    if not candidate.exists():
        return "missing"
    if not candidate.is_file():
        return "unknown"
    with candidate.open("rb") as handle:
        header = handle.read(16)
    if header == dependencies.sqlite_header:
        try:
            connection = sqlite3.connect(dependencies.sqlite_readonly_uri(candidate), uri=True)
            try:
                row = connection.execute(
                    "SELECT schema_version FROM graph_state WHERE singleton=1"
                ).fetchone()
            finally:
                connection.close()
            return "sqlite_v4" if row and row[0] == dependencies.schema_version else "sqlite_other"
        except sqlite3.Error:
            return "sqlite_other"
    try:
        with candidate.open("rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                if re.search(rb'"schema_version"\s*:\s*"v3"', mapped):
                    return "json_v3"
                if re.search(rb'"schema_version"\s*:', mapped):
                    return "json_other"
    except (OSError, ValueError):
        return "unknown"
    return "unknown"


def key_path(store_path: Path) -> Path:
    return store_path.with_name(f"{store_path.name}.plan.key")


def validate_plan_key_stat(
    key_path_value: Path,
    key_stat: os.stat_result,
    descriptor: int | None = None,
    *,
    allow_publication_link: bool = False,
    dependencies: PlanKeyStatDependencies,
) -> None:
    if not stat.S_ISREG(key_stat.st_mode):
        raise dependencies.path_safety_error_type(
            f"Plan key must be a regular file: {key_path_value}"
        )
    security_subject: SecuritySubject = key_path_value if descriptor is None else descriptor
    if not dependencies.owner_is_current_user(security_subject):
        raise dependencies.path_safety_error_type(
            f"Plan key is not owned by the current user: {key_path_value}"
        )
    if dependencies.mode_has_any(security_subject, 0o077):
        raise dependencies.path_safety_error_type(
            f"Plan key permissions must be 0600: {key_path_value}"
        )
    if key_stat.st_nlink != 1 and not (allow_publication_link and key_stat.st_nlink == 2):
        raise dependencies.path_safety_error_type(
            f"Plan key must not be hard-linked: {key_path_value}"
        )
    if key_stat.st_size > 4_096:
        raise dependencies.path_safety_error_type(
            f"Plan key is unexpectedly large: {key_path_value}"
        )


def read_private_plan_key(
    key_path_value: Path,
    *,
    dependencies: ReadPlanKeyDependencies,
) -> bytes:
    entry_stat = key_path_value.lstat()
    dependencies.validate_plan_key_stat(key_path_value, entry_stat)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(key_path_value, flags)
    try:
        opened_stat = os.fstat(descriptor)
        dependencies.validate_plan_key_stat(key_path_value, opened_stat, descriptor)
        if (entry_stat.st_dev, entry_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            raise dependencies.path_safety_error_type(
                f"Plan key changed while opening: {key_path_value}"
            )
        payload = os.read(descriptor, 4_097)
        if len(payload) > 4_096:
            raise dependencies.path_safety_error_type(
                f"Plan key is unexpectedly large: {key_path_value}"
            )
    finally:
        os.close(descriptor)
    try:
        encoded = payload.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise dependencies.path_safety_error_type(
            f"Plan key is not ASCII: {key_path_value}"
        ) from error
    return dependencies.key_bytes(encoded)


def read_private_plan_key_after_publication(
    key_path_value: Path,
    *,
    dependencies: PublishedPlanKeyDependencies,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(dependencies.retry_attempts):
        try:
            return dependencies.read_private_plan_key(key_path_value)
        except dependencies.path_safety_error_type as error:
            last_error = error
            try:
                key_stat = key_path_value.lstat()
                dependencies.validate_plan_key_stat(
                    key_path_value,
                    key_stat,
                    allow_publication_link=True,
                )
            except (FileNotFoundError, dependencies.path_safety_error_type):
                raise error from None
            if key_stat.st_nlink != 2:
                raise error
            if attempt + 1 < dependencies.retry_attempts:
                dependencies.sleep(dependencies.retry_delay_seconds)
    raise dependencies.path_safety_error_type(
        f"Plan key remained hard-linked after bounded publication retry: {key_path_value}"
    ) from last_error


def connection_pragmas(
    connection: sqlite3.Connection,
    *,
    readonly: bool,
    wal: bool = True,
    busy_timeout_ms: int,
    store_format_error_type: type[Exception],
) -> None:
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA cache_size=-65536")
    if readonly:
        connection.execute("PRAGMA query_only=ON")
        return
    if wal:
        mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        if mode.lower() != "wal":
            raise store_format_error_type(f"Could not enable SQLite WAL mode: {mode}")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA secure_delete=ON")
    connection.execute("PRAGMA journal_size_limit=67108864")


def load_or_create_plan_key(
    store_path: Path,
    supplied: bytes | str | None,
    *,
    create: bool,
    dependencies: LoadPlanKeyDependencies,
) -> bytes | None:
    key_path_value = dependencies.key_path(store_path)
    try:
        key_path_value.lstat()
    except FileNotFoundError:
        key_exists = False
    else:
        key_exists = True
    if supplied is not None:
        key = dependencies.key_bytes(supplied)
        if create and not key_exists:
            try:
                dependencies.atomic_bytes(
                    key_path_value,
                    (dependencies.b64encode(key) + "\n").encode("ascii"),
                    overwrite=False,
                )
            except FileExistsError:
                pass
            key_exists = True
        if key_exists:
            persisted = dependencies.read_private_plan_key_after_publication(key_path_value)
            if not dependencies.compare_digest(persisted, key):
                raise dependencies.path_safety_error_type(
                    f"Supplied plan key does not match the private key file: {key_path_value}"
                )
        return key
    if key_exists:
        return dependencies.read_private_plan_key_after_publication(key_path_value)
    if not create:
        return None
    key = dependencies.token_bytes(32)
    try:
        dependencies.atomic_bytes(
            key_path_value,
            (dependencies.b64encode(key) + "\n").encode("ascii"),
            overwrite=False,
        )
    except FileExistsError:
        return dependencies.read_private_plan_key_after_publication(key_path_value)
    persisted = dependencies.read_private_plan_key_after_publication(key_path_value)
    if not dependencies.compare_digest(persisted, key):
        raise dependencies.path_safety_error_type(
            f"Plan key changed while creating: {key_path_value}"
        )
    return persisted


def create_store(
    path: str | Path,
    *,
    plan_hmac_key: bytes | str | None,
    dependencies: CreateStoreDependencies,
) -> object:
    store_path = dependencies.assert_safe_path(Path(path))
    if dependencies.detect_store(store_path) != "missing":
        raise FileExistsError(store_path)
    dependencies.ensure_private_directory(store_path.parent)
    descriptor, temporary = dependencies.make_temporary_file(
        prefix=f".{store_path.name}.", suffix=".create", dir=store_path.parent
    )
    dependencies.close_descriptor(descriptor)
    temp_path = Path(temporary)
    with contextlib.suppress(FileNotFoundError):
        temp_path.unlink()
    connection: sqlite3.Connection | None = None
    try:
        connection = dependencies.connect(temp_path)
        connection.row_factory = sqlite3.Row
        dependencies.configure_connection(connection, readonly=False, wal=False)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.executescript(dependencies.ddl)
        now = dependencies.utc_now()
        connection.execute(
            "INSERT INTO graph_state(singleton,schema_version,store_id,generation,"
            "committed_watermark,journal_head_hash,journal_anchor_watermark,"
            "journal_anchor_hash,created_at,updated_at) VALUES(1,?,?,?,?,?,?,?,?,?)",
            (
                dependencies.schema_version,
                str(dependencies.uuid4()),
                1,
                0,
                "",
                0,
                "",
                now,
                now,
            ),
        )
        connection.execute(f"PRAGMA user_version={dependencies.schema_user_version}")
        connection.commit()
        check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            raise dependencies.validation_error_type("New v4 store failed SQLite quick_check")
        connection.close()
        connection = None
        dependencies.set_private_path_mode(temp_path, 0o600)
        with temp_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        dependencies.publish_staged_file(temp_path, store_path, overwrite=False)
        dependencies.fsync_parent(store_path.parent)
        key = dependencies.load_or_create_plan_key(store_path, plan_hmac_key, create=True)
        return dependencies.open_store(store_path, readonly=False, plan_hmac_key=key)
    except Exception:
        if connection is not None:
            connection.close()
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        raise


def _canonical_schema_sql(value: object) -> str:
    return " ".join(str(value or "").strip().removesuffix(";").split())


def _schema_object_sql(
    connection: sqlite3.Connection,
    *,
    object_type: str,
    name: str,
) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type=? AND name=?",
        (object_type, name),
    ).fetchone()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def _validate_append_only_triggers(
    connection: sqlite3.Connection,
    dependencies: OpenStoreDependencies,
) -> None:
    for name, expected_sql in dependencies.required_trigger_sql:
        observed_sql = _schema_object_sql(
            connection,
            object_type="trigger",
            name=name,
        )
        if _canonical_schema_sql(observed_sql) != _canonical_schema_sql(expected_sql):
            raise dependencies.store_format_error_type(
                "SQLite store append-only trigger is missing or incompatible: "
                f"{name}; restore a verified backup before reopening"
            )


def _legacy_append_only_schema_is_exact(
    connection: sqlite3.Connection,
    dependencies: OpenStoreDependencies,
) -> bool:
    payload_table_sql = _schema_object_sql(
        connection,
        object_type="table",
        name="record_revision_payloads",
    )
    if _canonical_schema_sql(payload_table_sql) != _canonical_schema_sql(
        dependencies.record_revision_payloads_table_sql
    ):
        return False
    legacy_triggers_are_exact = all(
        _canonical_schema_sql(_schema_object_sql(connection, object_type="trigger", name=name))
        == _canonical_schema_sql(expected_sql)
        for name, expected_sql in dependencies.legacy_trigger_sql
    )
    upgrade_triggers_are_absent = all(
        _schema_object_sql(connection, object_type="trigger", name=name) is None
        for name, _ in dependencies.upgrade_trigger_sql
    )
    return legacy_triggers_are_exact and upgrade_triggers_are_absent


def _upgrade_append_only_schema(
    connection: sqlite3.Connection,
    *,
    readonly: bool,
    dependencies: OpenStoreDependencies,
) -> None:
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if user_version == dependencies.schema_user_version:
        _validate_append_only_triggers(connection, dependencies)
        return
    if user_version != dependencies.previous_schema_user_version:
        raise dependencies.store_format_error_type(
            "SQLite store does not have a supported v4 schema user_version"
        )
    if readonly:
        raise dependencies.store_format_error_type(
            "SQLite v4 schema user_version 4 requires a one-time writable upgrade; "
            "reopen this store with readonly=False before using read-only mode"
        )

    transaction_started = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        row = connection.execute(
            "SELECT schema_version FROM graph_state WHERE singleton=1"
        ).fetchone()
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if row is None or row[0] != dependencies.schema_version:
            raise dependencies.store_format_error_type(
                "SQLite store does not have the expected v4 schema"
            )
        if user_version == dependencies.schema_user_version:
            _validate_append_only_triggers(connection, dependencies)
            connection.rollback()
            transaction_started = False
            return
        if user_version != dependencies.previous_schema_user_version or not (
            _legacy_append_only_schema_is_exact(connection, dependencies)
        ):
            raise dependencies.store_format_error_type(
                "SQLite v4 schema user_version 4 is not an exact legacy append-only schema; "
                "restore a verified backup before retrying the writable upgrade"
            )
        for _, trigger_sql in dependencies.upgrade_trigger_sql:
            connection.execute(trigger_sql)
        connection.execute(f"PRAGMA user_version={dependencies.schema_user_version}")
        _validate_append_only_triggers(connection, dependencies)
        connection.commit()
        transaction_started = False
    except sqlite3.Error as error:
        if transaction_started:
            connection.rollback()
        raise dependencies.store_format_error_type(
            "SQLite v4 append-only schema upgrade failed and was rolled back"
        ) from error
    except BaseException:
        if transaction_started:
            connection.rollback()
        raise


def open_store(
    path: str | Path,
    *,
    readonly: bool,
    plan_hmac_key: bytes | str | None,
    dependencies: OpenStoreDependencies,
) -> object:
    store_path = dependencies.assert_safe_path(Path(path))
    detected = dependencies.detect_store(store_path)
    if detected != "sqlite_v4":
        raise dependencies.store_format_error_type(
            f"Expected sqlite_v4 store, found {detected}: {store_path}"
        )
    if not dependencies.owner_is_current_user(store_path):
        raise dependencies.path_safety_error_type(
            f"Store is not owned by current user: {store_path}"
        )
    if dependencies.mode_has_any(store_path, 0o077):
        raise dependencies.path_safety_error_type(f"Store permissions must be 0600: {store_path}")
    if readonly:
        connection = dependencies.connect(
            dependencies.sqlite_readonly_uri(store_path),
            uri=True,
            timeout=dependencies.busy_timeout_ms / 1000,
        )
    else:
        connection = dependencies.connect(store_path, timeout=dependencies.busy_timeout_ms / 1000)
    connection.row_factory = sqlite3.Row
    try:
        dependencies.configure_connection(connection, readonly=readonly, wal=not readonly)
        row = connection.execute(
            "SELECT schema_version FROM graph_state WHERE singleton=1"
        ).fetchone()
        if row is None or row[0] != dependencies.schema_version:
            raise dependencies.store_format_error_type(
                "SQLite store does not have the expected v4 schema"
            )
        _upgrade_append_only_schema(
            connection,
            readonly=readonly,
            dependencies=dependencies,
        )
        key = dependencies.load_or_create_plan_key(store_path, plan_hmac_key, create=not readonly)
        return dependencies.build_store(
            store_path,
            connection,
            readonly=readonly,
            plan_hmac_key=key,
        )
    except Exception:
        connection.close()
        raise


__all__ = [
    "AtomicDependencies",
    "CreateStoreDependencies",
    "DetectStoreDependencies",
    "LoadPlanKeyDependencies",
    "MountDependencies",
    "OpenStoreDependencies",
    "PathInspectionDependencies",
    "PlanKeyStatDependencies",
    "PrivateDirectoryDependencies",
    "PublishedFileDependencies",
    "PublishedPlanKeyDependencies",
    "ReadPlanKeyDependencies",
    "ReadPublishedDependencies",
    "StageDependencies",
    "StorePathAssessment",
    "StoreSession",
    "assert_safe_path",
    "atomic_bytes",
    "connection_pragmas",
    "create_store",
    "detect_store",
    "ensure_private_directory",
    "inspect_store_path",
    "key_path",
    "load_or_create_plan_key",
    "mount_type",
    "open_store",
    "published_file_descriptor",
    "read_private_plan_key",
    "read_private_plan_key_after_publication",
    "read_verified_published_bytes",
    "sha256_verified_published_file",
    "stage_bytes",
]
