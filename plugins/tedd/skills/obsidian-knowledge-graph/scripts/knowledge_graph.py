#!/usr/bin/env python3
"""Local canonical knowledge graph with a deterministic Obsidian projection."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import os
import secrets
import sys
import tempfile
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import IO, Iterable, Iterator, NoReturn, cast

import knowledge_graph_v3_projection as _v3_projection
from knowledge_graph_file_io import (
    DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS,
)
from knowledge_graph_file_io import (
    fsync_directory as _fsync_directory,
)
from knowledge_graph_file_io import (
    replace_file_with_retry as _replace_file_with_retry,
)
from knowledge_graph_file_io import (
    sqlite_readonly_uri as _shared_sqlite_readonly_uri,
)
from knowledge_graph_v3_validation import (
    JsonObject as _V3ValidationJsonObject,
)
from knowledge_graph_v3_validation import (
    ValidationDependencies as _V3ValidationDependencies,
)
from knowledge_graph_v3_validation import (
    ValidationResult as _V3ValidationResult,
)
from knowledge_graph_v3_validation import (
    validate_graph as _validate_v3_graph,
)
from platform_support import (
    configure_utf8_standard_streams,
    is_link_or_reparse_point,
    is_trusted_windows_volume_root,
    mode_has_any,
    owner_is_current_user,
    set_private_fd_mode,
    set_private_path_mode,
    try_file_lock,
    unlock_file,
    uses_posix_permissions,
)
from tedd_knowledge_graph import legacy_cli as _legacy_cli
from tedd_knowledge_graph import legacy_configuration as _legacy_configuration
from tedd_knowledge_graph import legacy_coordination as _legacy_coordination
from tedd_knowledge_graph import legacy_filesystem as _legacy_filesystem
from tedd_knowledge_graph import legacy_maintenance as _legacy_maintenance
from tedd_knowledge_graph import legacy_migrations as _legacy_migrations
from tedd_knowledge_graph import legacy_mutations as _legacy_mutations
from tedd_knowledge_graph import legacy_privacy as _legacy_privacy
from tedd_knowledge_graph import legacy_projection as _legacy_projection
from tedd_knowledge_graph import legacy_query as _legacy_query
from tedd_knowledge_graph import legacy_query_index as _legacy_query_index
from tedd_knowledge_graph import legacy_store as _legacy_store
from tedd_knowledge_graph import legacy_values as _legacy_values
from tedd_knowledge_graph.legacy_dispatch import (
    run_cli as _run_legacy_cli,
)
from tedd_knowledge_graph.legacy_dispatch_contracts import (
    LegacyDispatchDependencies as _LegacyDispatchDependencies,
)
from tedd_knowledge_graph.legacy_dispatch_contracts import (
    V4Bindings as _V4Bindings,
)
from tedd_knowledge_graph.v4_contracts import DISCOVERY_KINDS

GRAPH_SCHEMA_VERSION = _legacy_migrations.GRAPH_SCHEMA_VERSION
LEGACY_GRAPH_SCHEMA_VERSIONS = set(_legacy_migrations.LEGACY_GRAPH_SCHEMA_VERSIONS)
CONFIG_SCHEMA_VERSION = "v2"
PROJECTION_FORMAT_VERSION = "v4"
# Keep the v2 marker stable so existing authenticated projection files remain owned.
PROJECTION_MARKER_VERSION = "v2"
INDEX_SCHEMA_VERSION = "v3"
MIGRATION_TRANSFORM_VERSION = _legacy_migrations.MIGRATION_TRANSFORM_VERSION
# Backward-compatible import surface for callers that referenced the old constant.
SCHEMA_VERSION = GRAPH_SCHEMA_VERSION
DEFAULT_ROOT_NAME = "Tedd Knowledge"
LEGACY_IMPLICIT_ROOT_NAME = _legacy_configuration.LEGACY_IMPLICIT_ROOT_NAME
DEFAULT_HOME = Path.home() / ".codex" / "knowledge-graph"
DEFAULT_CONFIG_PATH = DEFAULT_HOME / "config.json"
# Preserve the former implicit JSON location only for discovery/migration.
DEFAULT_STORE_PATH = DEFAULT_HOME / "graph.json"
DEFAULT_V4_STORE_PATH = DEFAULT_HOME / "graph.sqlite3"
DEFAULT_LOCK_TIMEOUT_SECONDS = 120.0
_CAPTURE_FINGERPRINT_ONLY = object()
WINDOWS_LOCK_INITIALIZATION_TIMEOUT_SECONDS = 1.0
WINDOWS_LOCK_INITIALIZATION_POLL_SECONDS = 0.01
WINDOWS_ATOMIC_REPLACE_TIMEOUT_SECONDS = 1.0
WINDOWS_ATOMIC_REPLACE_POLL_SECONDS = 0.01
WINDOWS_ATOMIC_REPLACE_RETRY_WINERRORS = frozenset({5, 32})
PROJECTION_MANIFEST = ".codex-projection-manifest.json"
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_PROJECTION_FILES = 20_000
MAX_PROJECTION_DIR_ENTRIES = 25_000
MAX_PROJECTION_PATH_LENGTH = 240
PROJECTION_MARKER_TAIL_BYTES = 512
SAFE_RECORD_ID = _legacy_values.SAFE_RECORD_ID
GENERATED_PROJECTION_DIRS = {
    "10 Entities",
    "11 Claims",
    "12 Incidents and Events",
    "13 Open Loops",
    "14 Decisions",
    "15 Releases",
    "20 Evidence",
    "21 Receipts",
    "22 Observations",
    "30 Bases",
}
SOURCE_TIERS = _legacy_migrations.SOURCE_TIERS
SCOPED_ENTITY_TYPES = {
    "component",
    "environment",
    "instance",
    "monitor",
    "person",
    "repository",
    "service",
    "site",
}
SENSITIVE_FIELD_KEYS = _legacy_values.SENSITIVE_FIELD_KEYS
SECRET_PATTERNS = _legacy_values.SECRET_PATTERNS
PATH_PATTERNS = _legacy_values.PATH_PATTERNS
SENSITIVE_QUERY_KEYS = _legacy_values.SENSITIVE_QUERY_KEYS
COLLECTIONS = _legacy_migrations.COLLECTIONS


def utc_now() -> str:
    return _legacy_values.utc_now()


def normalize_space(value: object) -> str:
    return _legacy_values.normalize_space(value)


def normalize_key(value: object) -> str:
    return _legacy_values.normalize_key(value)


def normalize_field_key(value: object) -> str:
    return _legacy_values.normalize_field_key(value)


def explicit_record_id(value: object) -> str:
    return _legacy_values.explicit_record_id(value)


def sensitive_field(value: object) -> bool:
    return _legacy_values.sensitive_field(value)


def parse_time(value: object, fallback: str | None = None) -> str:
    return _legacy_values.parse_time(value, fallback, clock=utc_now)


def time_value(value: str) -> dt.datetime:
    return _legacy_values.time_value(value)


def latest_time(left: str | None, right: str) -> str:
    return _legacy_values.latest_time(left, right)


def earliest_time(left: str | None, right: str) -> str:
    return _legacy_values.earliest_time(left, right)


def stable_hash(value: object, length: int = 20) -> str:
    return _legacy_values.stable_hash(value, length)


def stable_id(prefix: str, value: object) -> str:
    return f"{prefix}_{stable_hash(value)}"


def unique_strings(values: Iterable[object]) -> list[str]:
    return _legacy_values.unique_strings(values)


def _record_collection(container: dict[str, object], key: str) -> list[dict[str, object]]:
    """Narrow a collection after graph validation without changing runtime behavior."""

    return cast(list[dict[str, object]], container[key])


def _integer_value(value: object) -> int:
    return int(cast(int | str, value))


def redact_uri(value: str) -> str:
    return _legacy_values.redact_uri(value)


def redact_text(
    value: str,
    *,
    redact_paths: bool = True,
    max_length: int = 4000,
) -> tuple[str, int]:
    return _legacy_privacy.redact_text(
        value,
        _legacy_privacy_dependencies(),
        redact_paths=redact_paths,
        max_length=max_length,
    )


_ORIGINAL_REDACT_TEXT = redact_text


def _legacy_privacy_dependencies() -> _legacy_privacy.PrivacyDependencies:
    """Resolve mutable facade privacy policy for one redaction operation."""

    return _legacy_privacy.PrivacyDependencies(
        secret_patterns=SECRET_PATTERNS,
        path_patterns=PATH_PATTERNS,
        redact_uri=redact_uri,
        sensitive_field=sensitive_field,
        redact_text_override=(None if redact_text is _ORIGINAL_REDACT_TEXT else redact_text),
    )


def scrub_value(
    value: object,
    *,
    redact_paths: bool = True,
    depth: int = 0,
) -> tuple[object, int]:
    return _legacy_privacy.scrub_value(
        value,
        _legacy_privacy_dependencies(),
        redact_paths=redact_paths,
        depth=depth,
    )


def secret_hits(value: object) -> list[str]:
    return _legacy_values.secret_hits(value)


def blank_graph() -> dict[str, object]:
    return _legacy_store.blank_graph(clock=utc_now)


def committed_watermark(graph: dict[str, object]) -> int:
    return _legacy_store.committed_watermark(graph)


def _open_temp_text_descriptor(descriptor: int) -> AbstractContextManager[IO[str]]:
    return os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")


def _open_lock_text_descriptor(descriptor: int) -> AbstractContextManager[IO[str]]:
    return os.fdopen(descriptor, "a+", encoding="utf-8")


def _legacy_path_security_dependencies() -> _legacy_filesystem.PathSecurityDependencies:
    """Resolve path-validation hooks for one compatibility operation."""

    return _legacy_filesystem.PathSecurityDependencies(
        absolute_path=lambda value: os.path.abspath(os.path.expanduser(value)),
        uses_posix_permissions=uses_posix_permissions,
        mode_has_any=mode_has_any,
        owner_is_current_user=owner_is_current_user,
        is_link_or_reparse_point=is_link_or_reparse_point,
        set_private_path_mode=set_private_path_mode,
    )


def _legacy_atomic_write_dependencies() -> _legacy_filesystem.AtomicWriteDependencies:
    """Resolve atomic-publication hooks for one compatibility operation."""

    return _legacy_filesystem.AtomicWriteDependencies(
        sync_descriptor=os.fsync,
        set_private_path_mode=set_private_path_mode,
        open_temp_text=_open_temp_text_descriptor,
        make_temp_file=lambda prefix, directory: tempfile.mkstemp(
            prefix=prefix,
            dir=directory,
        ),
        ensure_private_directory=ensure_private_directory,
        replace_file_atomically=replace_file_atomically,
        fsync_parent_directory=fsync_parent_directory,
    )


def _legacy_lock_dependencies() -> _legacy_filesystem.LockDependencies:
    """Resolve lock acquisition and release hooks for one operation."""

    return _legacy_filesystem.LockDependencies(
        windows_lock_initialization_timeout_seconds=(WINDOWS_LOCK_INITIALIZATION_TIMEOUT_SECONDS),
        windows_lock_initialization_poll_seconds=WINDOWS_LOCK_INITIALIZATION_POLL_SECONDS,
        open_read_write=os.O_RDWR,
        open_create=os.O_CREAT,
        open_exclusive=os.O_EXCL,
        open_no_follow=getattr(os, "O_NOFOLLOW", 0),
        uses_posix_permissions=uses_posix_permissions,
        owner_is_current_user=owner_is_current_user,
        is_link_or_reparse_point=is_link_or_reparse_point,
        set_private_fd_mode=set_private_fd_mode,
        try_file_lock=try_file_lock,
        unlock_file=unlock_file,
        descriptor_stat=os.fstat,
        open_existing_descriptor=lambda path, flags: os.open(path, flags),
        create_descriptor=lambda path, flags, mode: os.open(path, flags, mode),
        close_descriptor=os.close,
        open_lock_text=_open_lock_text_descriptor,
        monotonic=time.monotonic,
        sleep=time.sleep,
        ensure_private_directory=ensure_private_directory,
        validate_open_lock_descriptor=_validate_open_lock_descriptor,
        open_existing_lock_descriptor=_open_existing_lock_descriptor,
        wait_before_lock_path_retry=_wait_before_lock_path_retry,
        wait_for_windows_lock_initialization=_wait_for_windows_lock_initialization,
        open_private_lock_descriptor=_open_private_lock_descriptor,
    )


def ensure_private_directory(path: Path) -> None:
    _legacy_filesystem.ensure_private_directory(
        path,
        _legacy_path_security_dependencies(),
    )


def secure_state_path(value: object, label: str) -> Path:
    return _legacy_filesystem.secure_state_path(
        value,
        label,
        _legacy_path_security_dependencies(),
    )


def path_is_within(path: Path, parent: Path) -> bool:
    return _legacy_filesystem.path_is_within(path, parent)


def state_privacy_warnings(
    config_path: Path, store_path: Path, config: dict[str, object]
) -> list[str]:
    return _legacy_filesystem.state_privacy_warnings(
        config_path,
        store_path,
        config,
        normalize_space=normalize_space,
    )


def fsync_parent_directory(path: Path) -> None:
    """Persist a completed rename when the platform supports directory fsync."""

    _fsync_directory(
        path,
        os_module=os,
        unsupported_errnos=DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS,
    )


def sqlite_readonly_uri(path: Path) -> str:
    """Return a percent-escaped SQLite URI for one resolved local path."""

    return _shared_sqlite_readonly_uri(path)


def replace_file_atomically(source: Path, destination: Path) -> None:
    """Replace once on POSIX and retry bounded Windows sharing conflicts.

    Python's Windows read handles do not share delete access.  A short-lived
    canonical reader can therefore make an otherwise atomic replacement fail
    with access-denied or sharing-violation even though graph writers serialize
    correctly.  Retrying the same rename preserves atomicity; this deliberately
    never falls back to deleting or rewriting the destination in place.
    """

    _replace_file_with_retry(
        source,
        destination,
        os_module=os,
        time_module=time,
        timeout_seconds=WINDOWS_ATOMIC_REPLACE_TIMEOUT_SECONDS,
        poll_seconds=WINDOWS_ATOMIC_REPLACE_POLL_SECONDS,
        retry_winerrors=WINDOWS_ATOMIC_REPLACE_RETRY_WINERRORS,
    )


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    _legacy_filesystem.atomic_write(
        path,
        content,
        mode,
        _legacy_atomic_write_dependencies(),
    )


def _validate_open_lock_descriptor(descriptor: int, lock_path: Path, label: str) -> None:
    _legacy_filesystem.validate_open_lock_descriptor(
        descriptor,
        lock_path,
        label,
        _legacy_lock_dependencies(),
    )


def _open_existing_lock_descriptor(lock_path: Path, label: str, flags: int) -> int:
    return _legacy_filesystem.open_existing_lock_descriptor(
        lock_path,
        label,
        flags,
        _legacy_lock_dependencies(),
    )


def _wait_before_lock_path_retry(lock_path: Path, label: str, acquisition_deadline: float) -> None:
    _legacy_filesystem.wait_before_lock_path_retry(
        lock_path,
        label,
        acquisition_deadline,
        _legacy_lock_dependencies(),
    )


def _wait_for_windows_lock_initialization(
    descriptor: int,
    lock_path: Path,
    label: str,
    flags: int,
    acquisition_deadline: float,
) -> int | None:
    return _legacy_filesystem.wait_for_windows_lock_initialization(
        descriptor,
        lock_path,
        label,
        flags,
        acquisition_deadline,
        _legacy_lock_dependencies(),
    )


def _open_private_lock_descriptor(lock_path: Path, label: str, acquisition_deadline: float) -> int:
    return _legacy_filesystem.open_private_lock_descriptor(
        lock_path,
        label,
        acquisition_deadline,
        _legacy_lock_dependencies(),
    )


@contextlib.contextmanager
def store_lock(
    store_path: Path, timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    with _legacy_filesystem.store_lock(
        store_path,
        timeout_seconds,
        _legacy_lock_dependencies(),
    ):
        yield


def load_json(path: Path, default: object) -> object:
    return _legacy_store.load_json(path, default)


def load_config(config_path: Path) -> dict[str, object]:
    return _legacy_store.load_config(config_path)


def _legacy_configuration_dependencies() -> _legacy_configuration.ConfigurationDependencies:
    """Resolve mutable facade configuration policy for one operation."""

    return _legacy_configuration.ConfigurationDependencies(
        config_schema_version=CONFIG_SCHEMA_VERSION,
        default_root_name=DEFAULT_ROOT_NAME,
        default_store_path=DEFAULT_STORE_PATH,
        default_v4_store_path=DEFAULT_V4_STORE_PATH,
        is_link_or_reparse_point=is_link_or_reparse_point,
        ensure_projection_target=ensure_projection_target,
        load_config=load_config,
        secure_state_path=secure_state_path,
        state_privacy_warnings=state_privacy_warnings,
        normalize_space=normalize_space,
        token_hex=secrets.token_hex,
        utc_now=utc_now,
        atomic_write=atomic_write,
    )


def default_unconfigured_store_path() -> Path:
    """Prefer v4 only when no legacy default path exists at all.

    object file, directory, or symlink at the former canonical location is state
    that requires inspection.  Selecting a new database beside unreadable or
    corrupted legacy state would silently fork the brain.
    """

    return _legacy_configuration.default_unconfigured_store_path(
        _legacy_configuration_dependencies()
    )


def resolve_store(args: argparse.Namespace, config: dict[str, object]) -> Path:
    return _legacy_configuration.resolve_store(
        args,
        config,
        _legacy_configuration_dependencies(),
    )


def journal_entry_hash(entry: dict[str, object], previous_hash: str) -> str:
    return _legacy_migrations.journal_entry_hash(entry, previous_hash)


def _legacy_surface_id(family: object, key: object, refs: Iterable[object]) -> str:
    return _legacy_migrations.legacy_surface_id(family, key, refs)


def migrate_graph_v2_to_v3(raw_graph: dict[str, object]) -> dict[str, object]:
    """Return a deterministic compatibility-preserving v3 copy of a v2 graph."""
    return _legacy_migrations.migrate_graph_v2_to_v3(raw_graph)


def prepare_graph(raw_graph: object) -> tuple[dict[str, object], str]:
    return _legacy_migrations.prepare_graph(raw_graph)


def load_graph_with_digest(store_path: Path) -> tuple[dict[str, object], str, str]:
    return _legacy_store.load_graph_with_digest(
        store_path,
        blank_factory=blank_graph,
        prepare=prepare_graph,
    )


def load_graph(store_path: Path) -> dict[str, object]:
    return _legacy_store.load_graph(
        store_path,
        blank_factory=blank_graph,
        prepare=prepare_graph,
    )


def save_graph(store_path: Path, graph: dict[str, object]) -> str:
    if not isinstance(graph, dict):
        raise ValueError(f"Graph at {store_path} must be a JSON object")
    return _legacy_store.save_graph(
        store_path,
        graph,
        clock=utc_now,
        writer=atomic_write,
    )


def _legacy_identity_service() -> _legacy_mutations.LegacyIdentityService:
    return _legacy_mutations.LegacyIdentityService(
        _legacy_mutations.IdentityDependencies(
            source_tiers=SOURCE_TIERS,
            scoped_entity_types=SCOPED_ENTITY_TYPES,
            normalize_space=normalize_space,
            normalize_key=normalize_key,
            explicit_record_id=explicit_record_id,
            time_value=time_value,
            latest_time=latest_time,
            earliest_time=earliest_time,
            stable_id=stable_id,
            unique_strings=unique_strings,
            index_by_id=index_by_id,
            identity_key_signature=identity_key_signature,
            normalized_identity_keys=normalized_identity_keys,
            canonical_entity_id=canonical_entity_id,
            resolve_entity_ref=resolve_entity_ref,
            source_tier=source_tier,
            source_tier_rank=source_tier_rank,
            resolved_entity_refs=resolved_entity_refs,
        )
    )


def _legacy_record_service() -> _legacy_mutations.LegacyRecordService:
    return _legacy_mutations.LegacyRecordService(
        _legacy_mutations.RecordDependencies(
            source_tiers=SOURCE_TIERS,
            normalize_space=normalize_space,
            normalize_key=normalize_key,
            explicit_record_id=explicit_record_id,
            parse_time=parse_time,
            time_value=time_value,
            latest_time=latest_time,
            earliest_time=earliest_time,
            stable_id=stable_id,
            unique_strings=unique_strings,
            _legacy_surface_id=_legacy_surface_id,
            index_by_id=index_by_id,
            canonical_entity_refs=canonical_entity_refs,
            resolved_entity_refs=resolved_entity_refs,
            record_entity_refs=record_entity_refs,
            source_tier=source_tier,
            source_tier_rank=source_tier_rank,
            upsert_claim_surface=upsert_claim_surface,
        )
    )


def _legacy_capture_service() -> _legacy_mutations.LegacyCaptureService:
    return _legacy_mutations.LegacyCaptureService(
        _legacy_mutations.CaptureDependencies(
            source_tiers=SOURCE_TIERS,
            discovery_kinds=DISCOVERY_KINDS,
            fingerprint_only_marker=_CAPTURE_FINGERPRINT_ONLY,
            blank_graph=blank_graph,
            utc_now=utc_now,
            normalize_space=normalize_space,
            normalize_key=normalize_key,
            explicit_record_id=explicit_record_id,
            sensitive_field=sensitive_field,
            parse_time=parse_time,
            stable_hash=stable_hash,
            stable_id=stable_id,
            unique_strings=unique_strings,
            scrub_value=scrub_value,
            committed_watermark=committed_watermark,
            journal_entry_hash=journal_entry_hash,
            index_by_id=index_by_id,
            normalized_identity_keys=normalized_identity_keys,
            source_tier=source_tier,
            capped_source_tier=capped_source_tier,
            upsert_entity=upsert_entity,
            upsert_relation=upsert_relation,
            add_entity_redirect=add_entity_redirect,
            add_observation=add_observation,
            add_claim=add_claim,
            add_generic_record=add_generic_record,
            packet_records=packet_records,
            canonicalize_packet_timestamps=canonicalize_packet_timestamps,
            canonical_fingerprint_packet=canonical_fingerprint_packet,
            normalize_producer=normalize_producer,
            append_journal_entry=append_journal_entry,
        )
    )


def index_by_id(records: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return _legacy_identity_service().index_by_id(records)


def identity_key_signature(raw: dict[str, object], default_namespace: str) -> tuple[str, str, str]:
    return _legacy_identity_service().identity_key_signature(raw, default_namespace)


def normalized_identity_keys(
    raw: dict[str, object], default_namespace: str
) -> list[tuple[str, str, str]]:
    return _legacy_identity_service().normalized_identity_keys(raw, default_namespace)


def canonical_entity_id(graph: dict[str, object], entity_id: str) -> str:
    return _legacy_identity_service().canonical_entity_id(graph, entity_id)


def canonical_entity_refs(graph: dict[str, object], refs: Iterable[object]) -> list[str]:
    return _legacy_identity_service().canonical_entity_refs(graph, refs)


def resolve_entity_ref(ref: object, local_refs: dict[str, str], graph: dict[str, object]) -> str:
    return _legacy_identity_service().resolve_entity_ref(ref, local_refs, graph)


def source_tier(value: object, fallback: str = "assistant_inference") -> str:
    return _legacy_identity_service().source_tier(value, fallback)


def source_tier_rank(value: object) -> int:
    return _legacy_identity_service().source_tier_rank(value)


def capped_source_tier(
    value: object, ceiling: str, fallback: str = "assistant_inference"
) -> tuple[str, bool]:
    return _legacy_identity_service().capped_source_tier(value, ceiling, fallback)


def upsert_entity(
    graph: dict[str, object],
    raw: dict[str, object],
    source_id: str,
    observed_at: str,
    incoming_tier: str,
) -> tuple[str, bool]:
    return _legacy_identity_service().upsert_entity(
        graph,
        raw,
        source_id,
        observed_at,
        incoming_tier,
    )


def upsert_relation(
    graph: dict[str, object],
    raw: dict[str, object],
    local_refs: dict[str, str],
    source_id: str,
    observed_at: str,
    incoming_tier: str,
) -> tuple[str, bool]:
    return _legacy_identity_service().upsert_relation(
        graph,
        raw,
        local_refs,
        source_id,
        observed_at,
        incoming_tier,
    )


def resolved_entity_refs(
    raw: dict[str, object], field: str, local_refs: dict[str, str], graph: dict[str, object]
) -> list[str]:
    return _legacy_identity_service().resolved_entity_refs(
        raw,
        field,
        local_refs,
        graph,
    )


def record_entity_refs(
    raw: dict[str, object], local_refs: dict[str, str], graph: dict[str, object]
) -> list[str]:
    return _legacy_identity_service().record_entity_refs(
        raw,
        local_refs,
        graph,
    )


def add_entity_redirect(
    graph: dict[str, object],
    raw: dict[str, object],
    local_refs: dict[str, str],
    source: dict[str, object],
) -> tuple[str, bool]:
    return _legacy_identity_service().add_entity_redirect(
        graph,
        raw,
        local_refs,
        source,
    )


def upsert_claim_surface(
    graph: dict[str, object],
    raw: dict[str, object],
    local_refs: dict[str, str],
    source: dict[str, object],
    family: str,
    key: str,
    context_refs: list[str],
) -> tuple[str, bool, bool]:
    return _legacy_record_service().upsert_claim_surface(
        graph,
        raw,
        local_refs,
        source,
        family,
        key,
        context_refs,
    )


def add_observation(
    graph: dict[str, object],
    raw: dict[str, object],
    local_refs: dict[str, str],
    source: dict[str, object],
) -> tuple[str, bool]:
    return _legacy_record_service().add_observation(
        graph,
        raw,
        local_refs,
        source,
    )


def add_claim(
    graph: dict[str, object],
    raw: dict[str, object],
    local_refs: dict[str, str],
    source: dict[str, object],
) -> tuple[str, str]:
    return _legacy_record_service().add_claim(
        graph,
        raw,
        local_refs,
        source,
    )


def add_generic_record(
    graph: dict[str, object],
    collection: str,
    raw: dict[str, object],
    local_refs: dict[str, str],
    source: dict[str, object],
) -> tuple[str, bool]:
    return _legacy_record_service().add_generic_record(
        graph,
        collection,
        raw,
        local_refs,
        source,
    )


def packet_records(packet: dict[str, object], collection: str) -> list[dict[str, object]]:
    return _legacy_capture_service().packet_records(packet, collection)


def canonicalize_packet_timestamps(
    source_raw: dict[str, object], records: dict[str, list[dict[str, object]]]
) -> None:
    return _legacy_capture_service().canonicalize_packet_timestamps(
        source_raw,
        records,
    )


def canonical_fingerprint_packet(
    source_raw: dict[str, object], records: dict[str, list[dict[str, object]]]
) -> dict[str, object]:
    return _legacy_capture_service().canonical_fingerprint_packet(
        source_raw,
        records,
    )


def normalize_producer(value: dict[str, object] | None) -> dict[str, str]:
    return _legacy_capture_service().normalize_producer(value)


def append_journal_entry(graph: dict[str, object], entry: dict[str, object]) -> int:
    return _legacy_capture_service().append_journal_entry(graph, entry)


def capture_packet(
    graph: dict[str, object],
    raw_packet: dict[str, object],
    authority_ceiling: str = "assistant_inference",
    allow_replace: bool = False,
    allow_redirect: bool = False,
    producer: dict[str, object] | None = None,
    based_on_watermark: int | None = None,
    discovery_kind: str | None = None,
) -> dict[str, object]:
    return _legacy_capture_service().capture_packet(
        graph,
        raw_packet,
        authority_ceiling,
        allow_replace,
        allow_redirect,
        producer,
        based_on_watermark,
        discovery_kind,
    )


def _normalize_query_producer(value: object) -> dict[str, str]:
    if value is not None and not isinstance(value, dict):
        raise ValueError("Producer provenance must be an object")
    return normalize_producer(value)


def _validate_query_graph(
    graph: _legacy_query.JsonObject,
) -> _legacy_query.ValidationResult:
    result = validate_graph(graph)
    valid = result.get("valid")
    errors = result.get("errors")
    if (
        not isinstance(valid, bool)
        or not isinstance(errors, list)
        or not all(isinstance(error, str) for error in errors)
    ):
        raise TypeError("Graph validation must return valid and string-list errors")
    return {"valid": valid, "errors": errors}


def _legacy_query_dependencies() -> _legacy_query.LegacyQueryDependencies:
    return _legacy_query.LegacyQueryDependencies(
        collections=COLLECTIONS,
        normalize_space=normalize_space,
        normalize_key=normalize_key,
        stable_hash=stable_hash,
        scrub_value=scrub_value,
        validate_graph=_validate_query_graph,
        index_by_id=index_by_id,
        canonical_entity_id=canonical_entity_id,
        source_tier_rank=source_tier_rank,
        committed_watermark=committed_watermark,
        normalize_producer=_normalize_query_producer,
        sqlite_readonly_uri=sqlite_readonly_uri,
        now_utc=lambda: dt.datetime.now(dt.timezone.utc),
        tokenize=tokenize,
        record_text=record_text,
        record_timestamp=record_timestamp,
        timestamp_rank=timestamp_rank,
        recall_record=recall_record,
        recall_section=recall_section,
        relation_visible_in_mode=relation_visible_in_mode,
        iter_graph_records=iter_graph_records,
        indexed_entity_refs=indexed_entity_refs,
        journal_entries_between=journal_entries_between,
        change_cursor_key=change_cursor_key,
        encode_change_cursor=encode_change_cursor,
        decode_change_cursor=decode_change_cursor,
        get_graph_record=get_graph_record,
    )


def _query_index_store_lock(
    path: Path,
    timeout_seconds: float,
) -> contextlib.AbstractContextManager[None]:
    return store_lock(path, timeout_seconds)


def _load_query_index_graph(store_path: Path) -> tuple[dict[str, object], str, str]:
    graph, digest, stored_version = load_graph_with_digest(store_path)
    if not isinstance(graph, dict):
        raise TypeError("Canonical graph loader must return an object")
    if not isinstance(digest, str) or not isinstance(stored_version, str):
        raise TypeError("Canonical graph loader must return string digest and version")
    return graph, digest, stored_version


def _legacy_query_index_dependencies() -> _legacy_query_index.LegacyQueryIndexDependencies:
    return _legacy_query_index.LegacyQueryIndexDependencies(
        index_schema_version=INDEX_SCHEMA_VERSION,
        graph_schema_version=GRAPH_SCHEMA_VERSION,
        collections=COLLECTIONS,
        default_lock_timeout_seconds=DEFAULT_LOCK_TIMEOUT_SECONDS,
        query=_legacy_query_dependencies(),
        utc_now=utc_now,
        ensure_private_directory=ensure_private_directory,
        set_private_path_mode=set_private_path_mode,
        replace_file_atomically=replace_file_atomically,
        fsync_parent_directory=fsync_parent_directory,
        store_lock=_query_index_store_lock,
        load_graph_with_digest=_load_query_index_graph,
        require_current_graph_schema=require_current_graph_schema,
        recall_index_path=recall_index_path,
        read_recall_index_meta=read_recall_index_meta,
        recall_index_healthy=recall_index_healthy,
        create_recall_index=_create_recall_index,
    )


def tokenize(value: object) -> set[str]:
    return _legacy_query.tokenize(value, normalize_key=normalize_key)


def record_text(
    kind: str,
    record: dict[str, object],
    entities: dict[str, dict[str, object]],
) -> str:
    return _legacy_query.record_text(
        kind,
        record,
        entities,
        normalize_space=normalize_space,
    )


def record_timestamp(record: dict[str, object]) -> str:
    return _legacy_query.record_timestamp(record)


def timestamp_rank(value: str) -> float:
    return _legacy_query.timestamp_rank(value, time_value=time_value)


def recall_record(record: dict[str, object], mode: str) -> dict[str, object]:
    return _legacy_query.recall_record(
        record,
        mode,
        scrub_value=scrub_value,
    )


def recall_section(collection: str, record: dict[str, object]) -> str:
    return _legacy_query.recall_section(collection, record)


def relation_visible_in_mode(
    relation: dict[str, object],
    entities: dict[str, dict[str, object]],
    mode: str,
) -> bool:
    return _legacy_query.relation_visible_in_mode(relation, entities, mode)


def recall_graph(
    graph: dict[str, object],
    query: str,
    limit: int,
    max_chars: int,
    mode: str = "review_safe",
    *,
    candidate_ids: set[str] | None = None,
    skip_validation: bool = False,
    backend: str = "scan",
    since_watermark: int | None = None,
) -> dict[str, object]:
    return _legacy_query.recall_graph(
        graph,
        query,
        limit,
        max_chars,
        mode,
        candidate_ids=candidate_ids,
        skip_validation=skip_validation,
        backend=backend,
        since_watermark=since_watermark,
        dependencies=_legacy_query_dependencies(),
    )


def recall_index_path(store_path: Path) -> Path:
    return _legacy_query_index.recall_index_path(store_path)


def iter_graph_records(
    graph: dict[str, object],
) -> Iterable[tuple[str, dict[str, object]]]:
    return _legacy_query.iter_graph_records(graph, collections=COLLECTIONS)


def indexed_entity_refs(
    collection: str,
    record: dict[str, object],
) -> list[tuple[str, str]]:
    return _legacy_query.indexed_entity_refs(collection, record)


def _create_recall_index(
    graph: dict[str, object],
    index_path: Path,
    canonical_digest: str,
) -> dict[str, object]:
    return _legacy_query_index.create_recall_index(
        graph,
        index_path,
        canonical_digest,
        dependencies=_legacy_query_index_dependencies(),
    )


def read_recall_index_meta(index_path: Path) -> dict[str, str]:
    return _legacy_query_index.read_recall_index_meta(
        index_path,
        sqlite_readonly_uri=sqlite_readonly_uri,
    )


def recall_index_healthy(index_path: Path, *, full: bool = False) -> bool:
    return _legacy_query_index.recall_index_healthy(
        index_path,
        full=full,
        sqlite_readonly_uri=sqlite_readonly_uri,
    )


def ensure_recall_index(
    graph: dict[str, object],
    store_path: Path,
    canonical_digest: str,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    force: bool = False,
) -> dict[str, object]:
    return _legacy_query_index.ensure_recall_index(
        graph,
        store_path,
        canonical_digest,
        timeout_seconds,
        force,
        dependencies=_legacy_query_index_dependencies(),
    )


def indexed_recall_candidates(
    index_path: Path,
    query: str,
    limit: int,
    expected_canonical_digest: str | None = None,
    *,
    exhaustive: bool = False,
    mode: str = "review_safe",
) -> set[str]:
    return _legacy_query_index.indexed_recall_candidates(
        index_path,
        query,
        limit,
        expected_canonical_digest,
        exhaustive=exhaustive,
        mode=mode,
        dependencies=_legacy_query_index_dependencies(),
    )


def journal_entries_between(
    graph: dict[str, object],
    since_watermark: int,
    through_watermark: int | None = None,
) -> list[dict[str, object]]:
    return _legacy_query.journal_entries_between(
        graph,
        since_watermark,
        through_watermark,
        committed_watermark=committed_watermark,
    )


def changed_ids_since(
    graph: dict[str, object],
    since_watermark: int,
    through_watermark: int | None = None,
) -> set[str]:
    return _legacy_query.changed_ids_since(
        graph,
        since_watermark,
        through_watermark,
        journal_entries_between=journal_entries_between,
    )


def scan_query_candidate_ids(
    graph: dict[str, object],
    query: str,
    mode: str = "review_safe",
) -> set[str]:
    """Return an exhaustive scan candidate set for filtered catch-up recovery."""

    return _legacy_query.scan_query_candidate_ids(
        graph,
        query,
        mode,
        dependencies=_legacy_query_dependencies(),
    )


def change_cursor_key(graph: dict[str, object], through_watermark: int) -> bytes:
    return _legacy_query.change_cursor_key(
        graph,
        through_watermark,
        normalize_space=normalize_space,
    )


def encode_change_cursor(
    graph: dict[str, object],
    *,
    since_watermark: int,
    through_watermark: int,
    next_offset: int,
    filter_fingerprint: str,
) -> str:
    return _legacy_query.encode_change_cursor(
        graph,
        since_watermark=since_watermark,
        through_watermark=through_watermark,
        next_offset=next_offset,
        filter_fingerprint=filter_fingerprint,
        change_cursor_key=change_cursor_key,
    )


def decode_change_cursor(
    graph: dict[str, object],
    cursor: str,
    *,
    since_watermark: int,
    through_watermark: int | None,
    filter_fingerprint: str,
) -> tuple[int, int]:
    return _legacy_query.decode_change_cursor(
        graph,
        cursor,
        since_watermark=since_watermark,
        through_watermark=through_watermark,
        filter_fingerprint=filter_fingerprint,
        normalize_space=normalize_space,
        change_cursor_key=change_cursor_key,
    )


def change_feed_since(
    graph: dict[str, object],
    since_watermark: int,
    *,
    through_watermark: int | None = None,
    offset: int = 0,
    limit: int = 50,
    candidate_ids: set[str] | None = None,
    mode: str = "review_safe",
    cursor: str | None = None,
) -> dict[str, object]:
    """Build a stable, ordered and paginated mutation feed for other agents."""

    return _legacy_query.change_feed_since(
        graph,
        since_watermark,
        through_watermark=through_watermark,
        offset=offset,
        limit=limit,
        candidate_ids=candidate_ids,
        mode=mode,
        cursor=cursor,
        dependencies=_legacy_query_dependencies(),
    )


def get_graph_record(
    graph: dict[str, object],
    record_id: str,
    mode: str,
) -> dict[str, object]:
    return _legacy_query.get_graph_record(
        graph,
        record_id,
        mode,
        dependencies=_legacy_query_dependencies(),
    )


def get_indexed_graph_record(
    graph: dict[str, object],
    index_path: Path,
    record_id: str,
    mode: str,
    expected_canonical_digest: str | None = None,
) -> dict[str, object]:
    return _legacy_query.get_indexed_graph_record(
        graph,
        index_path,
        record_id,
        mode,
        expected_canonical_digest,
        dependencies=_legacy_query_dependencies(),
    )


def _projection_validate_graph(value: object) -> _v3_projection.ProjectionValidationResult:
    if not isinstance(value, dict):
        return {"valid": False, "errors": ["graph must be an object"]}
    result = validate_graph(cast(dict[str, object], value))
    return {"valid": result["valid"], "errors": result["errors"]}


def _projection_index_by_id(
    records: list[_v3_projection.ProjectionRecord],
) -> dict[str, _v3_projection.ProjectionRecord]:
    indexed = index_by_id(cast(list[dict[str, object]], records))
    return cast(dict[str, _v3_projection.ProjectionRecord], indexed)


def _projection_relation_visible_in_mode(
    relation: _v3_projection.ProjectionRecord,
    entities: dict[str, _v3_projection.ProjectionRecord],
    mode: str,
) -> bool:
    return relation_visible_in_mode(
        cast(dict[str, object], relation),
        cast(dict[str, dict[str, object]], entities),
        mode,
    )


def _projection_dependencies() -> _v3_projection.ProjectionDependencies:
    return _v3_projection.ProjectionDependencies(
        collections=COLLECTIONS,
        max_projection_path_length=MAX_PROJECTION_PATH_LENGTH,
        normalize_space=normalize_space,
        stable_hash=stable_hash,
        redact_text=redact_text,
        validate_graph=_projection_validate_graph,
        index_by_id=_projection_index_by_id,
        relation_visible_in_mode=_projection_relation_visible_in_mode,
    )


def _projection_text_hooks() -> _v3_projection.ProjectionTextHooks:
    return _v3_projection.ProjectionTextHooks(
        frontmatter=frontmatter,
        markdown_literal=markdown_literal,
        wiki=wiki,
        bullet=bullet,
        projection_value=projection_value,
        projection_refs=projection_refs,
    )


def _projection_build_hooks() -> _v3_projection.ProjectionBuildHooks:
    return _v3_projection.ProjectionBuildHooks(
        frontmatter=frontmatter,
        markdown_literal=markdown_literal,
        wiki=wiki,
        bullet=bullet,
        projection_value=projection_value,
        projection_refs=projection_refs,
        note_path=note_path,
        assert_unique_projection_paths=assert_unique_projection_paths,
        render_record_note=render_record_note,
        base_file=base_file,
        loop_base_file=loop_base_file,
    )


def yaml_scalar(value: object) -> str:
    return _v3_projection.yaml_scalar(value)


def frontmatter(properties: dict[str, object]) -> str:
    return _v3_projection.frontmatter(properties, render_scalar=yaml_scalar)


def safe_segment(value: object, max_length: int = 96) -> str:
    return _v3_projection.safe_segment(
        value,
        max_length,
        dependencies=_projection_dependencies(),
    )


def note_path(folder: str, record: _v3_projection.ProjectionRecord, label: str) -> str:
    return _v3_projection.note_path(
        folder,
        record,
        label,
        dependencies=_projection_dependencies(),
        render_segment=safe_segment,
    )


def assert_unique_projection_paths(paths: Iterable[str]) -> None:
    _v3_projection.assert_unique_projection_paths(
        paths,
        max_path_length=_projection_dependencies().max_projection_path_length,
    )


def markdown_literal(value: object) -> str:
    return _v3_projection.markdown_literal(value, normalize_space=normalize_space)


def wiki(relative_path: str, label: str | None = None) -> str:
    return _v3_projection.wiki(
        relative_path,
        label,
        escape_markdown=markdown_literal,
    )


def bullet(values: Iterable[object], empty: str = "None") -> str:
    return _v3_projection.bullet(
        values,
        empty,
        normalize_space=normalize_space,
    )


def projection_value(
    record: _v3_projection.ProjectionRecord,
    value: object,
    mode: str,
) -> str:
    return _v3_projection.projection_value(
        record,
        value,
        mode,
        dependencies=_projection_dependencies(),
    )


def projection_refs(
    record: _v3_projection.ProjectionRecord,
    key: str,
    mode: str,
) -> list[str]:
    return _v3_projection.projection_refs(record, key, mode)


def build_projection_notes(
    graph: dict[str, object],
    mode: str,
    root_name: str,
    *,
    skip_validation: bool = False,
) -> dict[str, str]:
    return _v3_projection.build_projection_notes(
        graph,
        mode,
        root_name,
        dependencies=_projection_dependencies(),
        hooks=_projection_build_hooks(),
        skip_validation=skip_validation,
    )


def render_record_note(
    record: _v3_projection.ProjectionRecord,
    kind: str,
    entity_paths: dict[str, str],
    entities: dict[str, _v3_projection.ProjectionRecord],
    mode: str,
    timestamp_key: str,
) -> str:
    return _v3_projection.render_record_note(
        record,
        kind,
        entity_paths,
        entities,
        mode,
        timestamp_key,
        hooks=_projection_text_hooks(),
    )


def base_file(name: str, folder: str, record_type: str, properties: list[str]) -> str:
    return _v3_projection.base_file(name, folder, record_type, properties)


def loop_base_file(folder: str) -> str:
    return _v3_projection.loop_base_file(folder)


def _legacy_projection_service() -> _legacy_projection.LegacyProjectionService:
    return _legacy_projection.LegacyProjectionService(
        _legacy_projection.ProjectionDependencies(
            generated_projection_dirs=GENERATED_PROJECTION_DIRS,
            projection_format_version=PROJECTION_FORMAT_VERSION,
            projection_marker_version=PROJECTION_MARKER_VERSION,
            graph_schema_version=GRAPH_SCHEMA_VERSION,
            projection_manifest=PROJECTION_MANIFEST,
            projection_marker_tail_bytes=PROJECTION_MARKER_TAIL_BYTES,
            max_manifest_bytes=MAX_MANIFEST_BYTES,
            max_projection_files=MAX_PROJECTION_FILES,
            max_projection_dir_entries=MAX_PROJECTION_DIR_ENTRIES,
            max_projection_path_length=MAX_PROJECTION_PATH_LENGTH,
            owner_is_current_user=owner_is_current_user,
            uses_posix_permissions=uses_posix_permissions,
            mode_has_any=mode_has_any,
            is_trusted_windows_volume_root=is_trusted_windows_volume_root,
            is_link_or_reparse_point=is_link_or_reparse_point,
            _open_private_lock_descriptor=_open_private_lock_descriptor,
            try_file_lock=try_file_lock,
            unlock_file=unlock_file,
            normalize_space=normalize_space,
            stable_id=stable_id,
            stable_hash=stable_hash,
            committed_watermark=committed_watermark,
            build_projection_notes=build_projection_notes,
            set_private_path_mode=set_private_path_mode,
            atomic_write=atomic_write,
            utc_now=utc_now,
            validate_graph=validate_graph,
            load_graph_with_digest=load_graph_with_digest,
            assert_trusted_directory_chain=assert_trusted_directory_chain,
            ensure_projection_target=ensure_projection_target,
            safe_projection_path=safe_projection_path,
            generated_projection_path=generated_projection_path,
            projection_manifest_signature=projection_manifest_signature,
            projection_file_marker=projection_file_marker,
            marked_projection_content=marked_projection_content,
            projection_file_is_owned=projection_file_is_owned,
            load_projection_manifest=load_projection_manifest,
            recoverable_unmanifested_projection=recoverable_unmanifested_projection,
            projection_lock=projection_lock,
            _project_graph_locked=_project_graph_locked,
        )
    )


def assert_trusted_directory_chain(path: Path, label: str) -> None:
    return _legacy_projection_service().assert_trusted_directory_chain(path, label)


def ensure_projection_target(vault: Path, root_name: str) -> Path:
    return _legacy_projection_service().ensure_projection_target(vault, root_name)


def safe_projection_path(root: Path, relative: str) -> Path:
    return _legacy_projection_service().safe_projection_path(root, relative)


def generated_projection_path(relative: str) -> bool:
    return _legacy_projection_service().generated_projection_path(relative)


def projection_manifest_signature(
    manifest_key: str,
    root_id: str,
    mode: str,
    generated_files: list[str],
    *,
    schema_version: str = PROJECTION_FORMAT_VERSION,
    graph_watermark: int | None = None,
    graph_sha256: str | None = None,
    content_hashes: dict[str, str] | None = None,
    canonical_store_schema_version: str | None = None,
    projection_payload_schema_version: str | None = None,
) -> str:
    return _legacy_projection_service().projection_manifest_signature(
        manifest_key,
        root_id,
        mode,
        generated_files,
        schema_version=schema_version,
        graph_watermark=graph_watermark,
        graph_sha256=graph_sha256,
        content_hashes=content_hashes,
        canonical_store_schema_version=canonical_store_schema_version,
        projection_payload_schema_version=projection_payload_schema_version,
    )


def projection_file_marker(manifest_key: str, root_id: str, relative: str) -> str:
    return _legacy_projection_service().projection_file_marker(manifest_key, root_id, relative)


def marked_projection_content(manifest_key: str, root_id: str, relative: str, content: str) -> str:
    return _legacy_projection_service().marked_projection_content(
        manifest_key, root_id, relative, content
    )


def projection_file_is_owned(path: Path, manifest_key: str, root_id: str, relative: str) -> bool:
    return _legacy_projection_service().projection_file_is_owned(
        path, manifest_key, root_id, relative
    )


def load_projection_manifest(path: Path) -> dict[str, object] | None:
    return _legacy_projection_service().load_projection_manifest(path)


def recoverable_unmanifested_projection(root: Path, manifest_key: str, root_id: str) -> bool:
    return _legacy_projection_service().recoverable_unmanifested_projection(
        root, manifest_key, root_id
    )


@contextlib.contextmanager
def projection_lock(
    vault: Path, root_name: str, timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS
) -> Iterator[None]:
    with _legacy_projection_service().projection_lock(vault, root_name, timeout_seconds):
        yield


def _project_graph_locked(
    graph: dict[str, object],
    vault: Path,
    root_name: str,
    mode: str,
    manifest_key: str,
    root_id: str | None = None,
    graph_sha256: str | None = None,
    force_current_digest: str | None = None,
    canonical_store_schema_version: str | None = None,
    projection_payload_schema_version: str | None = None,
) -> dict[str, object]:
    return _legacy_projection_service()._project_graph_locked(
        graph,
        vault,
        root_name,
        mode,
        manifest_key,
        root_id,
        graph_sha256,
        force_current_digest,
        canonical_store_schema_version,
        projection_payload_schema_version,
    )


def project_graph(
    graph: dict[str, object],
    vault: Path,
    root_name: str,
    mode: str,
    manifest_key: str,
    root_id: str | None = None,
    graph_sha256: str | None = None,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    force_current_digest: str | None = None,
    canonical_store_schema_version: str | None = None,
    projection_payload_schema_version: str | None = None,
) -> dict[str, object]:
    return _legacy_projection_service().project_graph(
        graph,
        vault,
        root_name,
        mode,
        manifest_key,
        root_id,
        graph_sha256,
        timeout_seconds,
        force_current_digest,
        canonical_store_schema_version,
        projection_payload_schema_version,
    )


def project_latest_from_store(
    store_path: Path,
    vault: Path,
    root_name: str,
    mode: str,
    manifest_key: str,
    root_id: str | None = None,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    force_current_digest: str | None = None,
) -> dict[str, object]:
    return _legacy_projection_service().project_latest_from_store(
        store_path,
        vault,
        root_name,
        mode,
        manifest_key,
        root_id,
        timeout_seconds,
        force_current_digest,
    )


def _validation_canonical_entity_id(
    graph: _V3ValidationJsonObject,
    entity_id: str,
) -> str:
    return canonical_entity_id(cast(dict[str, object], graph), entity_id)


def _validation_canonical_entity_refs(
    graph: _V3ValidationJsonObject,
    refs: Iterable[object],
) -> list[str]:
    return canonical_entity_refs(cast(dict[str, object], graph), refs)


def _validation_identity_key_signature(
    raw: _V3ValidationJsonObject,
    default_namespace: str,
) -> tuple[str, str, str]:
    return identity_key_signature(cast(dict[str, object], raw), default_namespace)


def _validation_index_by_id(
    records: list[_V3ValidationJsonObject],
) -> dict[str, _V3ValidationJsonObject]:
    indexed = index_by_id(cast(list[dict[str, object]], records))
    return cast(dict[str, _V3ValidationJsonObject], indexed)


def _validation_journal_entry_hash(
    entry: _V3ValidationJsonObject,
    previous_hash: str,
) -> str:
    return journal_entry_hash(cast(dict[str, object], entry), previous_hash)


def _validation_dependencies() -> _V3ValidationDependencies:
    return _V3ValidationDependencies(
        collections=COLLECTIONS,
        discovery_kinds=DISCOVERY_KINDS,
        graph_schema_version=GRAPH_SCHEMA_VERSION,
        safe_record_id=SAFE_RECORD_ID,
        source_tiers=SOURCE_TIERS,
        canonical_entity_id=_validation_canonical_entity_id,
        canonical_entity_refs=_validation_canonical_entity_refs,
        identity_key_signature=_validation_identity_key_signature,
        index_by_id=_validation_index_by_id,
        journal_entry_hash=_validation_journal_entry_hash,
        normalize_key=normalize_key,
        normalize_space=normalize_space,
        parse_time=parse_time,
        secret_hits=secret_hits,
        time_value=time_value,
    )


def validate_graph(graph: dict[str, object]) -> _V3ValidationResult:
    """Validate a legacy v3 graph through the focused validation module."""
    return _validate_v3_graph(
        cast(_V3ValidationJsonObject, graph),
        _validation_dependencies(),
    )


def configure(
    args: argparse.Namespace,
    config_path: Path,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
) -> _legacy_configuration.ConfigurationOutcome:
    """Mutate projection/store configuration under the shared config lock."""

    with store_lock(config_path, lock_timeout):
        return _configure_locked(args, config_path)


def _configure_locked(
    args: argparse.Namespace, config_path: Path
) -> _legacy_configuration.ConfigurationOutcome:
    return _legacy_configuration.configure_locked(
        args,
        config_path,
        _legacy_configuration_dependencies(),
    )


def resolve_projection(args: argparse.Namespace, config: dict[str, object]) -> tuple[Path, str]:
    return _legacy_configuration.resolve_projection(
        args,
        config,
        _legacy_configuration_dependencies(),
    )


def _legacy_maintenance_dependencies() -> _legacy_maintenance.MaintenanceDependencies:
    """Resolve mutable facade hooks for one legacy maintenance operation."""

    return _legacy_maintenance.MaintenanceDependencies(
        collections=COLLECTIONS,
        discovery_kinds=DISCOVERY_KINDS,
        normalize_space=normalize_space,
        normalize_key=normalize_key,
        integer_value=_integer_value,
        stable_hash=stable_hash,
        unique_strings=unique_strings,
        utc_now=utc_now,
        committed_watermark=committed_watermark,
        append_journal_entry=append_journal_entry,
        normalize_producer=normalize_producer,
        index_by_id=index_by_id,
        redact_payload=redact_payload,
    )


def redact_payload(record: dict[str, object], *, include_identity: bool) -> None:
    _legacy_maintenance.redact_payload(
        record,
        include_identity=include_identity,
        utc_now=utc_now,
    )


def redact_record(graph: dict[str, object], record_id: str) -> dict[str, object]:
    return _legacy_maintenance.redact_record(
        graph,
        record_id,
        _legacy_maintenance_dependencies(),
    )


def resolve_loop(graph: dict[str, object], loop_id: str, resolution: str) -> dict[str, object]:
    return _legacy_maintenance.resolve_loop(
        graph,
        loop_id,
        resolution,
        _legacy_maintenance_dependencies(),
    )


def mutation_journal(
    graph: dict[str, object],
    action: str,
    changed_ids: list[str],
    *,
    producer: dict[str, object] | None = None,
    based_on_watermark: int | None = None,
    discovery_kind: str | None = None,
    details: dict[str, object] | None = None,
) -> int:
    return _legacy_maintenance.mutation_journal(
        graph,
        action,
        changed_ids,
        producer=producer,
        based_on_watermark=based_on_watermark,
        discovery_kind=discovery_kind,
        details=details,
        dependencies=_legacy_maintenance_dependencies(),
    )


class CLIArgumentError(ValueError):
    """A caller-correctable command-line parsing error."""


class JSONArgumentParser(argparse.ArgumentParser):
    """Raise parse failures so the CLI can preserve its JSON contract."""

    def error(self, message: str) -> NoReturn:
        raise CLIArgumentError(message)


def build_parser() -> argparse.ArgumentParser:
    # Import lazily to preserve the v4 integration cycle and legacy import surface.
    import knowledge_graph_v4_cli

    return _legacy_cli.build_parser(
        JSONArgumentParser,
        _legacy_cli.ParserPolicy(
            description=__doc__,
            default_config_path=DEFAULT_CONFIG_PATH,
            default_lock_timeout_seconds=DEFAULT_LOCK_TIMEOUT_SECONDS,
            default_root_name=DEFAULT_ROOT_NAME,
            discovery_kinds=DISCOVERY_KINDS,
            source_tiers=SOURCE_TIERS,
        ),
        knowledge_graph_v4_cli.register_v4_subcommands,
    )


def read_packet(path: str) -> dict[str, object]:
    return _legacy_cli.read_packet(
        path,
        stdin=sys.stdin.buffer,
        max_capture_bytes=MAX_CAPTURE_BYTES,
    )


def producer_from_args(args: argparse.Namespace) -> dict[str, str]:
    return normalize_producer(
        {
            "agent_id": getattr(args, "producer_agent_id", None),
            "task_id": getattr(args, "task_id", None),
            "parent_task_id": getattr(args, "parent_task_id", None),
            "run_id": getattr(args, "producer_run_id", None),
        }
    )


def require_current_graph_schema(stored_version: str) -> None:
    _legacy_cli.require_current_graph_schema(
        stored_version,
        current_version=GRAPH_SCHEMA_VERSION,
    )


def _legacy_coordination_dependencies() -> _legacy_coordination.CoordinationDependencies:
    """Resolve mutable facade hooks for one coordination operation."""

    return _legacy_coordination.CoordinationDependencies(
        index_schema_version=INDEX_SCHEMA_VERSION,
        graph_schema_version=GRAPH_SCHEMA_VERSION,
        default_lock_timeout_seconds=DEFAULT_LOCK_TIMEOUT_SECONDS,
        recall_index_path=recall_index_path,
        read_recall_index_meta=read_recall_index_meta,
        recall_index_healthy=recall_index_healthy,
        committed_watermark=committed_watermark,
        load_graph_with_digest=load_graph_with_digest,
        ensure_recall_index=ensure_recall_index,
        resolve_projection=resolve_projection,
        project_latest_from_store=project_latest_from_store,
        normalize_space=normalize_space,
    )


def index_status(
    graph: dict[str, object], store_path: Path, canonical_digest: str
) -> dict[str, object]:
    return _legacy_coordination.index_status(
        graph,
        store_path,
        canonical_digest,
        _legacy_coordination_dependencies(),
    )


def ensure_latest_recall_index(store_path: Path, timeout_seconds: float) -> dict[str, object]:
    return _legacy_coordination.ensure_latest_recall_index(
        store_path,
        timeout_seconds,
        _legacy_coordination_dependencies(),
    )


def maybe_project_latest(
    args: argparse.Namespace,
    config: dict[str, object],
    store_path: Path,
) -> dict[str, object] | None:
    return _legacy_coordination.maybe_project_latest(
        args,
        config,
        store_path,
        _legacy_coordination_dependencies(),
    )


def migration_backup_path(
    store_path: Path, stored_version: str, watermark: int, digest: str
) -> Path:
    return _legacy_coordination.migration_backup_path(
        store_path,
        stored_version,
        watermark,
        digest,
    )


def _load_cli_v4_bindings() -> _V4Bindings:
    """Import the cyclic v4 integration surface only after the facade is ready."""

    import knowledge_graph_v4 as graph_v4
    import knowledge_graph_v4_capture as graph_v4_capture
    import knowledge_graph_v4_cli as graph_v4_cli
    import knowledge_graph_v4_integration as graph_v4_integration

    return _V4Bindings(
        detect_store=graph_v4.detect_store,
        should_handle=graph_v4_cli.should_handle_v4,
        handle_command=graph_v4_cli.handle_v4_command,
        initialize=graph_v4_integration.initialize_v4,
        upgrade=graph_v4_integration.upgrade_v3_to_v4,
        migration_recovery_source=(graph_v4_integration.migration_recovery_source),
        upgrade_transform_version=graph_v4_integration.UPGRADE_TRANSFORM_VERSION,
        capture=graph_v4_capture.execute_capture,
        sanitize_review_safe=graph_v4_integration.sanitize_review_safe_value,
        project=graph_v4_integration.project_v4,
        redact=graph_v4_integration.redact_v4,
        resolve_loop=graph_v4_integration.resolve_loop_v4,
        open_store=graph_v4.open_store,
    )


def _cli_dispatch_dependencies() -> _LegacyDispatchDependencies:
    """Bind the facade's current globals for one invocation.

    Constructing this object at call time preserves the historical lookup and
    monkeypatch surface while keeping the private dispatcher independent from
    this monolithic compatibility module.
    """

    return _LegacyDispatchDependencies(
        argument_error=CLIArgumentError,
        collections=COLLECTIONS,
        default_root_name=DEFAULT_ROOT_NAME,
        graph_schema_version=GRAPH_SCHEMA_VERSION,
        legacy_graph_schema_versions=LEGACY_GRAPH_SCHEMA_VERSIONS,
        migration_transform_version=MIGRATION_TRANSFORM_VERSION,
        atomic_write=atomic_write,
        capture_packet=capture_packet,
        change_feed_since=change_feed_since,
        committed_watermark=committed_watermark,
        configure=configure,
        ensure_latest_recall_index=ensure_latest_recall_index,
        ensure_projection_target=ensure_projection_target,
        ensure_recall_index=ensure_recall_index,
        get_graph_record=get_graph_record,
        get_indexed_graph_record=get_indexed_graph_record,
        index_by_id=index_by_id,
        index_status=index_status,
        indexed_recall_candidates=indexed_recall_candidates,
        load_config=load_config,
        load_graph_with_digest=load_graph_with_digest,
        load_v4_bindings=_load_cli_v4_bindings,
        maybe_project_latest=maybe_project_latest,
        migrate_graph_v2_to_v3=migrate_graph_v2_to_v3,
        migration_backup_path=migration_backup_path,
        mutation_journal=mutation_journal,
        normalize_space=normalize_space,
        producer_from_args=producer_from_args,
        project_latest_from_store=project_latest_from_store,
        read_packet=read_packet,
        recall_graph=recall_graph,
        recall_index_path=recall_index_path,
        redact_record=redact_record,
        require_current_graph_schema=require_current_graph_schema,
        resolve_loop=resolve_loop,
        resolve_projection=resolve_projection,
        resolve_store=resolve_store,
        save_graph=save_graph,
        scan_query_candidate_ids=scan_query_candidate_ids,
        secure_state_path=secure_state_path,
        stable_hash=stable_hash,
        state_privacy_warnings=state_privacy_warnings,
        store_lock=store_lock,
        unique_strings=unique_strings,
        validate_graph=validate_graph,
    )


def main() -> int:
    configure_utf8_standard_streams()
    parser = build_parser()
    return _run_legacy_cli(parser, _cli_dispatch_dependencies())


if __name__ == "__main__":
    raise SystemExit(main())
