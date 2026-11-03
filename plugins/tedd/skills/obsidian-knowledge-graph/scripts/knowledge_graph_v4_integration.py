#!/usr/bin/env python3
"""Compatibility facade for v4 migration, projection, and lifecycle policy.

The implementation services live under ``tedd_knowledge_graph``.  Legacy and
v4 facade modules are imported lazily and their callbacks are rebound for every
operation, preserving existing monkeypatch seams without a static import cycle.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import tempfile
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol, cast

from knowledge_graph_file_io import (
    PublicationOperations,
    fsync_directory,
    publish_staged_file,
)
from platform_support import mode_has_any, owner_is_current_user, set_private_fd_mode
from tedd_knowledge_graph import v4_lifecycle as _v4_lifecycle
from tedd_knowledge_graph import v4_migrations as _v4_migrations
from tedd_knowledge_graph import v4_projection as _v4_projection
from tedd_knowledge_graph.v4_cli_contracts import InitializeRequest

UPGRADE_TRANSFORM_VERSION = "v3-to-v4.sqlite-wal.3"
MAX_PROJECTION_ROOT_RECORDS = 1_500
MAX_PROJECTION_RECORDS = 7_500


class _LegacyPolicy(Protocol):
    COLLECTIONS: tuple[str, ...]
    CONFIG_SCHEMA_VERSION: str
    GRAPH_SCHEMA_VERSION: str

    def ensure_private_directory(self, path: Path) -> None: ...

    def validate_graph(self, graph: dict[str, object]) -> _v4_migrations.LegacyValidationResult: ...

    def committed_watermark(self, graph: dict[str, object]) -> int: ...

    def stable_hash(self, value: object, length: int = 20) -> str: ...

    def store_lock(self, path: Path, timeout_seconds: float) -> object: ...

    def load_config(self, config_path: Path) -> dict[str, object]: ...

    def atomic_write(self, path: Path, content: str, mode: int = 0o600) -> None: ...

    def utc_now(self) -> str: ...

    def recall_record(self, record: dict[str, object], mode: str) -> dict[str, object]: ...

    def scrub_value(
        self,
        value: object,
        *,
        redact_paths: bool = True,
        depth: int = 0,
    ) -> tuple[object, int]: ...

    def normalize_space(self, value: object) -> str: ...

    def normalize_key(self, value: object) -> str: ...

    def secure_state_path(self, value: object, label: str) -> Path: ...

    def blank_graph(self) -> dict[str, object]: ...

    def project_graph(
        self,
        graph: dict[str, object],
        vault: Path,
        root_name: str,
        mode: str,
        manifest_key: str,
        root_id: str | None = None,
        graph_sha256: str | None = None,
        timeout_seconds: float = 30.0,
        force_current_digest: str | None = None,
        canonical_store_schema_version: str | None = None,
        projection_payload_schema_version: str | None = None,
    ) -> dict[str, object]: ...

    def producer_from_args(self, args: argparse.Namespace) -> dict[str, str]: ...

    def resolve_loop(
        self, graph: dict[str, object], loop_id: str, resolution: str
    ) -> dict[str, object]: ...

    def redact_record(self, graph: dict[str, object], record_id: str) -> dict[str, object]: ...


class _StoragePolicy(Protocol):
    COLLECTIONS: tuple[str, ...]
    DDL: str
    SCHEMA_USER_VERSION: int
    SCHEMA_VERSION: str
    RecordChange: _v4_lifecycle.RecordChangeFactory

    def canonical_json(self, value: object) -> str: ...

    def _read_verified_published_bytes(
        self,
        path: Path,
        expected_identity: tuple[int, int],
        *,
        maximum_bytes: int,
    ) -> bytes: ...

    def _sha256_verified_published_file(
        self,
        path: Path,
        expected_identity: tuple[int, int],
    ) -> tuple[str, int]: ...

    def detect_store(self, path: str | Path) -> str: ...

    def create_store(
        self, path: str | Path, *, plan_hmac_key: bytes | str | None = None
    ) -> object: ...

    def open_store(
        self,
        path: str | Path,
        *,
        readonly: bool = False,
        plan_hmac_key: bytes | str | None = None,
    ) -> object: ...

    def import_v3_graph(
        self,
        graph: Mapping[str, object],
        target_path: str | Path,
        source_sha256: str,
        **kwargs: object,
    ) -> object: ...


def _legacy_module() -> _LegacyPolicy:
    import knowledge_graph

    # Module attributes such as exception classes, dataclass constructors, and
    # fixed-length tuples are mutable to mypy and therefore invariant against
    # our read-only callback Protocol. Facade call shape and late binding are
    # characterized separately; this cast is limited to that module boundary.
    return cast(_LegacyPolicy, knowledge_graph)


def _storage_module() -> _StoragePolicy:
    import knowledge_graph_v4

    return cast(_StoragePolicy, knowledge_graph_v4)


def __getattr__(name: str) -> object:
    """Preserve historical ``integration.legacy``/``.v4`` patch access."""

    if name == "legacy":
        return _legacy_module()
    if name == "v4":
        return _storage_module()
    raise AttributeError(name)


def _object_dict(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise ValueError(f"{label} keys must be strings")
    return {str(key): child for key, child in raw.items()}


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_parent(path: Path) -> None:
    fsync_directory(path, os_module=os)


def _finalize_publication_link(
    staged: Path,
    destination: Path,
    expected_identity: tuple[int, int],
    expected_sha256: str,
) -> None:
    """Durably finish one interrupted same-directory hard-link publication."""

    if staged.parent != destination.parent:
        raise ValueError("Interrupted publication paths must share one parent directory")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptors: list[int] = []
    try:
        for path in (staged, destination):
            descriptor = os.open(path, flags)
            descriptors.append(descriptor)
        staged_stat = os.fstat(descriptors[0])
        destination_stat = os.fstat(descriptors[1])
        for path, descriptor, entry in (
            (staged, descriptors[0], staged_stat),
            (destination, descriptors[1], destination_stat),
        ):
            if not stat.S_ISREG(entry.st_mode):
                raise ValueError(f"Interrupted publication is not a regular file: {path}")
            if (entry.st_dev, entry.st_ino) != expected_identity:
                raise ValueError(f"Interrupted publication identity changed: {path}")
            if entry.st_nlink != 2:
                raise ValueError(f"Interrupted publication must have exactly two links: {path}")
            if not owner_is_current_user(descriptor) or mode_has_any(descriptor, 0o077):
                raise ValueError(f"Interrupted publication is not private: {path}")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptors[0], 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptors[0])
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        ) != (
            staged_stat.st_dev,
            staged_stat.st_ino,
            staged_stat.st_size,
            staged_stat.st_mtime_ns,
            staged_stat.st_nlink,
        ):
            raise ValueError("Interrupted publication changed during verification")
        if digest.hexdigest() != expected_sha256:
            raise ValueError("Interrupted publication content differs from durable intent")
    finally:
        for descriptor in descriptors:
            os.close(descriptor)

    staged_entry = staged.lstat()
    destination_entry = destination.lstat()
    if (
        (staged_entry.st_dev, staged_entry.st_ino) != expected_identity
        or (destination_entry.st_dev, destination_entry.st_ino) != expected_identity
        or staged_entry.st_nlink != 2
        or destination_entry.st_nlink != 2
    ):
        raise ValueError("Interrupted publication changed before durable cleanup")
    staged.unlink()
    _fsync_parent(staged.parent)
    published_entry = destination.lstat()
    if (
        published_entry.st_dev,
        published_entry.st_ino,
    ) != expected_identity or published_entry.st_nlink != 1:
        raise ValueError("Interrupted publication cleanup failed identity verification")


def _atomic_bytes(path: Path, payload: bytes) -> None:
    _legacy_module().ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        set_private_fd_mode(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        publish_staged_file(
            temporary_path,
            path,
            overwrite=True,
            os_module=cast(PublicationOperations, os),
        )
        _fsync_parent(path.parent)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _migration_dependencies() -> _v4_migrations.MigrationDependencies:
    legacy = _legacy_module()
    storage = _storage_module()
    return _v4_migrations.MigrationDependencies(
        collections=storage.COLLECTIONS,
        ddl=storage.DDL,
        schema_user_version=storage.SCHEMA_USER_VERSION,
        transform_version=UPGRADE_TRANSFORM_VERSION,
        validate_legacy=legacy.validate_graph,
        committed_watermark=legacy.committed_watermark,
        stable_hash=legacy.stable_hash,
        canonical_json=storage.canonical_json,
        store_lock=cast(_v4_migrations.StoreLock, legacy.store_lock),
        load_config=legacy.load_config,
        atomic_write=legacy.atomic_write,
        utc_now=legacy.utc_now,
        import_v3_graph=cast(_v4_migrations.ImportV3Graph, storage.import_v3_graph),
        open_store=cast(_v4_migrations.OpenStore, storage.open_store),
        atomic_bytes=_atomic_bytes,
        sha256_bytes=_sha256_bytes,
        sha256_file=_sha256_file,
        fsync_parent=_fsync_parent,
        publish_staged_file=publish_staged_file,
        read_verified_published_bytes=storage._read_verified_published_bytes,
        sha256_verified_published_file=storage._sha256_verified_published_file,
        finalize_publication_link=_finalize_publication_link,
        uuid_hex=lambda: uuid.uuid4().hex,
    )


def _record_count(graph: Mapping[str, object]) -> int:
    return _v4_migrations.record_count(graph, _migration_dependencies())


def _upgrade_plan(
    source_path: Path, target_path: Path, payload: bytes
) -> tuple[dict[str, object], dict[str, object], str]:
    return _v4_migrations.upgrade_plan(source_path, target_path, payload, _migration_dependencies())


def _collections_equal(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    return _v4_migrations.collections_equal(left, right, _migration_dependencies())


def migration_recovery_source(
    manifest_path: Path,
    *,
    config_path: Path,
    target_path: Path,
    expected_source_sha256: str,
    expected_plan_fingerprint: str,
    transform_version: str,
) -> Path:
    secured_manifest = _legacy_module().secure_state_path(
        manifest_path,
        "v4 migration manifest",
    )
    return _v4_migrations.migration_recovery_source(
        secured_manifest,
        config_path=config_path,
        target_path=target_path,
        expected_source_sha256=expected_source_sha256,
        expected_plan_fingerprint=expected_plan_fingerprint,
        transform_version=transform_version,
        read_verified_published_bytes=_storage_module()._read_verified_published_bytes,
        finalize_publication_link=_finalize_publication_link,
    )


def sanitize_review_safe_value(value: object) -> object:
    """Apply established review-safe policy to any JSON-shaped v4 result."""

    legacy = _legacy_module()
    hash_fields = {
        "entry_hash",
        "previous_entry_hash",
        "journal_head_hash",
        "journal_anchor_hash",
        "payload_hash",
        "current_payload_hash",
        "state_root",
    }

    def walk(item: object) -> object:
        if isinstance(item, Mapping):
            current = _object_dict(item, label="Review-safe value")
            if isinstance(current.get("id"), str):
                current = _object_dict(
                    legacy.recall_record(current, "review_safe"),
                    label="Review-safe record",
                )
            cleaned: dict[str, object] = {}
            for key, child in current.items():
                if key in hash_fields and child not in (None, ""):
                    cleaned[key] = "[withheld in review_safe mode]"
                elif key in {"evidence_refs", "observation_refs"} and child:
                    cleaned[key] = ["[withheld in review_safe mode]"]
                elif key in {"source_ref", "redirect_source_ref"} and child:
                    cleaned[key] = "[withheld in review_safe mode]"
                else:
                    cleaned[key] = walk(child)
            return cleaned
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, tuple):
            return [walk(child) for child in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise TypeError(f"Review-safe sanitizer received {type(item).__name__}")

    scrubbed, _ = legacy.scrub_value(walk(value), redact_paths=True)
    return scrubbed


def _projection_dependencies() -> _v4_projection.ProjectionDependencies:
    legacy = _legacy_module()
    storage = _storage_module()
    return _v4_projection.ProjectionDependencies(
        collections=legacy.COLLECTIONS,
        max_root_records=MAX_PROJECTION_ROOT_RECORDS,
        max_records=MAX_PROJECTION_RECORDS,
        blank_graph=legacy.blank_graph,
        validate_graph=cast(Callable[[Mapping[str, object]], object], legacy.validate_graph),
        stable_hash=legacy.stable_hash,
        project_graph=cast(_v4_projection.ProjectGraph, legacy.project_graph),
        canonical_schema_version=storage.SCHEMA_VERSION,
        projection_schema_version=legacy.GRAPH_SCHEMA_VERSION,
    )


def _forward_closure(store: _v4_projection.ProjectionStore, roots: list[str]) -> set[str]:
    return _v4_projection.forward_closure(store, roots)


def _records_graph(
    store: _v4_projection.ProjectionStore,
    record_ids: set[str],
    *,
    projection_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return _v4_projection.records_graph(
        store,
        record_ids,
        projection_metadata=projection_metadata,
        dependencies=_projection_dependencies(),
    )


def _projection_graph(
    store: _v4_projection.ProjectionStore,
) -> tuple[dict[str, object], dict[str, object]]:
    return _v4_projection.projection_graph(store, dependencies=_projection_dependencies())


def project_v4(
    store_path: Path,
    *,
    vault: Path,
    root_name: str,
    mode: str,
    manifest_key: str,
    root_id: str | None,
    timeout_seconds: float,
    force_current_digest: str | None = None,
) -> dict[str, object]:
    storage = _storage_module()
    with cast(
        _v4_lifecycle.LifecycleStore,
        storage.open_store(store_path, readonly=True),
    ) as store:
        return _v4_projection.project_v4(
            cast(_v4_projection.ProjectionStore, store),
            vault=vault,
            root_name=root_name,
            mode=mode,
            manifest_key=manifest_key,
            root_id=root_id,
            timeout_seconds=timeout_seconds,
            force_current_digest=force_current_digest,
            dependencies=_projection_dependencies(),
        )


def _initialize_dependencies() -> _v4_lifecycle.InitializeDependencies:
    legacy = _legacy_module()
    storage = _storage_module()
    return _v4_lifecycle.InitializeDependencies(
        config_schema_version=legacy.CONFIG_SCHEMA_VERSION,
        store_lock=cast(_v4_lifecycle.StoreLock, legacy.store_lock),
        load_config=legacy.load_config,
        normalize_space=legacy.normalize_space,
        secure_state_path=legacy.secure_state_path,
        detect_store=storage.detect_store,
        create_store=cast(_v4_lifecycle.CreateStore, storage.create_store),
        open_store=cast(_v4_lifecycle.OpenStore, storage.open_store),
        atomic_write=legacy.atomic_write,
        utc_now=legacy.utc_now,
    )


def initialize_v4(
    request: InitializeRequest,
    *,
    config_path: Path,
    lock_timeout: float,
) -> dict[str, object]:
    return _v4_lifecycle.initialize_v4(
        request,
        config_path=config_path,
        lock_timeout=lock_timeout,
        dependencies=_initialize_dependencies(),
    )


def upgrade_v3_to_v4(
    request: object,
    *,
    config_path: Path,
    lock_timeout: float,
) -> dict[str, object]:
    return _v4_migrations.upgrade_v3_to_v4(
        cast(_v4_migrations.UpgradeRequest, request),
        config_path=config_path,
        lock_timeout=lock_timeout,
        dependencies=_migration_dependencies(),
    )


def _mutation_dependencies() -> _v4_lifecycle.MutationDependencies:
    legacy = _legacy_module()
    storage = _storage_module()
    return _v4_lifecycle.MutationDependencies(
        collections=legacy.COLLECTIONS,
        producer_from_args=legacy.producer_from_args,
        normalize_space=legacy.normalize_space,
        normalize_key=legacy.normalize_key,
        blank_graph=legacy.blank_graph,
        resolve_loop=legacy.resolve_loop,
        redact_record=legacy.redact_record,
        stable_hash=legacy.stable_hash,
        canonical_json=storage.canonical_json,
        open_store=cast(_v4_lifecycle.OpenStore, storage.open_store),
        record_change=storage.RecordChange,
        projection_dependencies=_projection_dependencies(),
    )


def _producer_arguments(
    args: argparse.Namespace, store: _v4_lifecycle.LifecycleStore
) -> _v4_lifecycle.ProducerArguments:
    return _v4_lifecycle.producer_arguments(args, store, dependencies=_mutation_dependencies())


def resolve_loop_v4(store_path: Path, args: argparse.Namespace) -> dict[str, object]:
    return _v4_lifecycle.resolve_loop_v4(store_path, args, dependencies=_mutation_dependencies())


def redact_v4(store_path: Path, args: argparse.Namespace) -> dict[str, object]:
    return _v4_lifecycle.redact_v4(store_path, args, dependencies=_mutation_dependencies())


__all__ = [
    "MAX_PROJECTION_RECORDS",
    "MAX_PROJECTION_ROOT_RECORDS",
    "migration_recovery_source",
    "project_v4",
    "redact_v4",
    "resolve_loop_v4",
    "sanitize_review_safe_value",
    "upgrade_v3_to_v4",
]
