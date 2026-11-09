#!/usr/bin/env python3
"""Canonical SQLite/WAL storage primitives for Knowledge Graph v4.

This module is intentionally independent from ``knowledge_graph.py`` so v4 can
be exercised, migrated, and rolled back without changing the live v3 graph.
It exposes a Python storage API; command-line policy belongs in a separate CLI
adapter.

Public API
==========

``detect_store(path)``
    Return ``missing``, ``sqlite_v4``, ``sqlite_other``, ``json_v3``,
    ``json_other``, or ``unknown`` without mutating the path.

``inspect_store_path(path)``
    Return a local/cloud/network safety assessment. ``create_store`` and
    ``open_store`` reject cloud-synchronised and network-mounted canonical
    stores. SQLite WAL is a same-machine coordination mechanism.

``create_store(path, plan_hmac_key=None)`` / ``open_store(...)``
    Create or open a :class:`KnowledgeGraphV4` store.

``import_v3_graph(graph, target_path, source_sha256, ...)``
    Side-by-side import of a validated v3 dictionary. Legacy journal headers
    and hashes are retained, and a v4 baseline mutation makes every imported
    current record available as revision 1. The source graph is never edited.

``KnowledgeGraphV4``
    Provides state inspection, exact get/history, current export, FTS search,
    fast/full validation, online backup, WAL checkpoints, task consumers,
    versioned subscriptions, inbox/ack/context packs, exact revision mutation
    feeds, read-set plan tokens, and packet-sized transactional writes.

The canonical logical value of a record is its revision payload. Typed tables
enforce identity, reference, claim-surface, and semantic uniqueness. FTS is a
transactionally maintained but rebuildable current-record search index.
Obsidian remains a derived projection and is deliberately absent from this
storage layer.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
import unicodedata
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence, cast

import knowledge_graph_v4_validation as _v4_validation
from knowledge_graph_file_io import (
    DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS,
)
from knowledge_graph_file_io import (
    PublicationOperations as _PublicationOperations,
)
from knowledge_graph_file_io import (
    fsync_directory as _fsync_directory,
)
from knowledge_graph_file_io import (
    publish_staged_file as _shared_publish_staged_file,
)
from knowledge_graph_file_io import (
    sqlite_readonly_uri as _shared_sqlite_readonly_uri,
)
from platform_support import (
    is_link_or_reparse_point,
    mode_has_any,
    owner_is_current_user,
    set_private_path_mode,
    uses_posix_permissions,
    windows_volume_type,
)
from tedd_knowledge_graph import v4_backup as _v4_backup
from tedd_knowledge_graph import v4_contracts as _v4_contracts
from tedd_knowledge_graph import v4_coordination as _v4_coordination
from tedd_knowledge_graph import v4_feed as _v4_feed
from tedd_knowledge_graph import v4_import as _v4_import
from tedd_knowledge_graph import v4_queries as _v4_queries
from tedd_knowledge_graph import v4_registry as _v4_registry
from tedd_knowledge_graph import v4_schema as _v4_schema
from tedd_knowledge_graph import v4_store as _v4_store
from tedd_knowledge_graph import v4_transactions as _v4_transactions
from tedd_knowledge_graph.v4_read_snapshot import read_snapshot as _read_snapshot

SCHEMA_VERSION = _v4_schema.SCHEMA_VERSION
SCHEMA_USER_VERSION = _v4_schema.SCHEMA_USER_VERSION
HASH_ALGORITHM = "v4-canonical-json-sha256"
LEGACY_HASH_ALGORITHM = "v3-json-sha256"
DEFAULT_BUSY_TIMEOUT_MS = 120_000
MAX_FEED_LIMIT = 500
MAX_CONTEXT_ITEMS = 500
MAX_CONTEXT_CHARS = 100_000
MAX_CONSUMER_STATUS_SUBSCRIPTIONS = 500
PLAN_KEY_LINK_RETRY_ATTEMPTS = 50
PLAN_KEY_LINK_RETRY_DELAY_SECONDS = 0.002
SAFE_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SQLITE_HEADER = b"SQLite format 3\x00"
COLLECTIONS = (
    "entities",
    "relations",
    "observations",
    "claim_surfaces",
    "claims",
    "events",
    "decisions",
    "open_loops",
    "receipts",
    "sources",
)
COLLECTION_SET = frozenset(COLLECTIONS)

NETWORK_FS_TYPES = set(_v4_contracts.NETWORK_FS_TYPES)
CLOUD_PATH_MARKERS = _v4_contracts.CLOUD_PATH_MARKERS


class V4Error(RuntimeError):
    """Base exception for the v4 storage adapter."""


class PathSafetyError(V4Error):
    """The canonical store path is not suitable for local SQLite WAL."""


class StoreFormatError(V4Error):
    """The file is missing, corrupt, or is not a compatible v4 store."""


class ConcurrencyConflict(V4Error):
    """A relevant record or uniqueness predicate changed after review."""


# Compatibility name used by early v4 callers and tests.
RevisionConflictError = ConcurrencyConflict


class IdentityConflict(V4Error):
    """An immutable identity, semantic key, or claim surface conflicts."""


class PlanTokenError(V4Error):
    """A reviewed read-set token is invalid or belongs to another store."""


class SubscriptionError(V4Error):
    """A consumer subscription or acknowledgement is invalid."""


class ValidationError(V4Error):
    """Canonical SQL state failed deterministic validation."""


StorePathAssessment = _v4_store.StorePathAssessment


class _ImportV3Callable(Protocol):
    def __call__(
        self,
        graph: Mapping[str, object],
        target_path: str | Path,
        source_sha256: str,
        **kwargs: object,
    ) -> "KnowledgeGraphV4": ...


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def normalize_space(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_key(value: object) -> str:
    return unicodedata.normalize("NFC", normalize_space(value)).casefold()


def canonical_json(value: object) -> str:
    return _v4_contracts.canonical_json(value)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_hash(value: object) -> str:
    return sha256_text(canonical_json(value))


def _b64encode(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _b64decode(payload: str) -> bytes:
    return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))


def _key_bytes(key: bytes | str) -> bytes:
    if isinstance(key, bytes):
        result = key
    elif isinstance(key, str):
        text = key.strip()
        try:
            result = _b64decode(text)
        except (binascii.Error, ValueError):
            result = text.encode("utf-8")
    else:
        raise TypeError("HMAC key must be bytes or string")
    if len(result) < 32:
        raise ValueError("HMAC key must contain at least 32 bytes")
    return result


def sign_plan_token(payload: Mapping[str, object], key: bytes | str) -> str:
    """Sign a bounded read-set or cursor payload with a local private key."""
    encoded = _b64encode(canonical_json(dict(payload)).encode("utf-8"))
    signature = hmac.new(_key_bytes(key), encoded.encode("ascii"), hashlib.sha256)
    return f"{encoded}.{signature.hexdigest()}"


def verify_plan_token(token: str, key: bytes | str) -> dict[str, object]:
    """Verify and decode a plan/cursor token."""
    try:
        encoded, supplied = normalize_space(token).rsplit(".", 1)
        expected = hmac.new(_key_bytes(key), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise PlanTokenError("Token authentication failed")
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
    except PlanTokenError:
        raise
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as error:
        raise PlanTokenError("Malformed signed token") from error
    if not isinstance(payload, dict):
        raise PlanTokenError("Signed token payload must be an object")
    return payload


def _nearest_existing_parent(path: Path) -> Path:
    return _v4_store.nearest_existing_parent(path)


def _mount_type(path: Path) -> str | None:
    """Best-effort local filesystem type detection on supported hosts."""
    return _v4_store.mount_type(
        path,
        dependencies=_v4_store.MountDependencies(
            nearest_existing_parent=_nearest_existing_parent,
            sys_platform_is_macos=sys_platform_is_macos,
            windows_volume_type=windows_volume_type,
        ),
    )


def sys_platform_is_macos() -> bool:
    return _v4_store.sys_platform_is_macos()


def inspect_store_path(path: str | Path) -> StorePathAssessment:
    """Return a read-only suitability assessment for a canonical WAL store."""
    return _v4_store.inspect_store_path(
        path,
        dependencies=_v4_store.PathInspectionDependencies(
            network_fs_types=frozenset(NETWORK_FS_TYPES),
            cloud_path_markers=CLOUD_PATH_MARKERS,
            is_link_or_reparse_point=is_link_or_reparse_point,
            mode_has_any=cast(_v4_store.ModeCheck, mode_has_any),
            owner_is_current_user=cast(_v4_store.OwnerCheck, owner_is_current_user),
            uses_posix_permissions=uses_posix_permissions,
            mount_type=_mount_type,
            nearest_existing_parent=_nearest_existing_parent,
        ),
    )


def _assert_safe_path(path: Path) -> Path:
    return _v4_store.assert_safe_path(
        path,
        inspect_path=inspect_store_path,
        path_safety_error_type=PathSafetyError,
    )


def _ensure_private_directory(path: Path) -> None:
    _v4_store.ensure_private_directory(
        path,
        dependencies=_v4_store.PrivateDirectoryDependencies(
            mode_has_any=cast(_v4_store.ModeCheck, mode_has_any),
            owner_is_current_user=cast(_v4_store.OwnerCheck, owner_is_current_user),
            set_private_path_mode=set_private_path_mode,
            path_safety_error_type=PathSafetyError,
        ),
    )


def _fsync_parent(path: Path) -> None:
    _fsync_directory(
        path,
        os_module=os,
        unsupported_errnos=DIRECTORY_FSYNC_UNSUPPORTED_ERRNOS,
    )


def _sqlite_readonly_uri(path: Path) -> str:
    """Return a percent-escaped SQLite URI for one resolved local path."""

    return _shared_sqlite_readonly_uri(path)


def _publish_staged_file(source: Path, destination: Path, *, overwrite: bool) -> tuple[int, int]:
    """Publish a same-directory staged file without racing no-overwrite checks."""

    return _shared_publish_staged_file(
        source,
        destination,
        overwrite=overwrite,
        os_module=cast(_PublicationOperations, os),
    )


def _stage_bytes(path: Path, content: bytes, mode: int = 0o600) -> Path:
    """Write and sync private bytes beside their eventual destination."""
    return _v4_store.stage_bytes(
        path,
        content,
        mode,
        dependencies=_v4_store.StageDependencies(
            ensure_private_directory=_ensure_private_directory,
            set_private_path_mode=set_private_path_mode,
        ),
    )


def _atomic_bytes(path: Path, content: bytes, mode: int = 0o600, *, overwrite: bool = True) -> None:
    _v4_store.atomic_bytes(
        path,
        content,
        mode,
        overwrite=overwrite,
        dependencies=_v4_store.AtomicDependencies(
            stage_bytes=_stage_bytes,
            publish_staged_file=_publish_staged_file,
            fsync_parent=_fsync_parent,
        ),
    )


def _published_file_descriptor(
    path: Path, expected_identity: tuple[int, int]
) -> tuple[int, os.stat_result]:
    """Open one published regular file and bind it to its staged identity."""
    return _v4_store.published_file_descriptor(
        path,
        expected_identity,
        dependencies=_v4_store.PublishedFileDependencies(
            owner_is_current_user=cast(_v4_store.OwnerCheck, owner_is_current_user),
            mode_has_any=cast(_v4_store.ModeCheck, mode_has_any),
            concurrency_conflict_type=ConcurrencyConflict,
            path_safety_error_type=PathSafetyError,
        ),
    )


def _read_verified_published_bytes(
    path: Path, expected_identity: tuple[int, int], *, maximum_bytes: int
) -> bytes:
    """Read bounded bytes from the exact file inode that was just published."""
    return _v4_store.read_verified_published_bytes(
        path,
        expected_identity,
        maximum_bytes=maximum_bytes,
        dependencies=_v4_store.ReadPublishedDependencies(
            published_file_descriptor=_published_file_descriptor,
            concurrency_conflict_type=ConcurrencyConflict,
            validation_error_type=ValidationError,
        ),
    )


def _sha256_verified_published_file(
    path: Path, expected_identity: tuple[int, int]
) -> tuple[str, int]:
    """Hash the exact staged inode and reject concurrent in-place changes."""
    return _v4_store.sha256_verified_published_file(
        path,
        expected_identity,
        dependencies=_v4_store.ReadPublishedDependencies(
            published_file_descriptor=_published_file_descriptor,
            concurrency_conflict_type=ConcurrencyConflict,
            validation_error_type=ValidationError,
        ),
    )


def detect_store(path: str | Path) -> str:
    """Detect a canonical store format without writing or opening WAL files."""
    return _v4_store.detect_store(
        path,
        dependencies=_v4_store.DetectStoreDependencies(
            sqlite_header=SQLITE_HEADER,
            schema_version=SCHEMA_VERSION,
            sqlite_readonly_uri=_sqlite_readonly_uri,
        ),
    )


DDL = _v4_schema.DDL


class _Unset:
    pass


_UNSET = _Unset()


@dataclasses.dataclass(frozen=True)
class RecordChange:
    """One current-record transition committed within a single mutation.

    ``expected_revision`` uses a private sentinel when omitted. Explicit
    ``None`` means the record must not yet exist. Updating an existing record
    without an expectation or a valid plan token is rejected.

    ``refs`` items are ``{"target_id": id, "kind": kind, "ordinal": n?}``.
    ``semantic_keys`` items are strings (kind ``semantic``) or
    ``{"kind": kind, "key": stable_value}``.
    """

    record_id: str
    collection: str
    payload: Mapping[str, object]
    change_kind: str = "upsert"
    expected_revision: object = dataclasses.field(default_factory=lambda: _UNSET)
    refs: Sequence[Mapping[str, object]] = ()
    semantic_keys: Sequence[object] = ()
    search_text: str | None = None


def _coerce_change(raw: RecordChange | Mapping[str, object]) -> RecordChange:
    if isinstance(raw, RecordChange):
        return raw
    if not isinstance(raw, Mapping):
        raise TypeError("Each changed record must be a RecordChange or object")
    payload_value = raw.get("payload") or {}
    if not isinstance(payload_value, Mapping):
        raise TypeError("Changed-record payload must be an object")
    payload = cast(Mapping[str, object], payload_value)
    refs_value = raw.get("refs") or ()
    if not isinstance(refs_value, Sequence) or isinstance(refs_value, (str, bytes)):
        raise TypeError("Changed-record refs must be a sequence of objects")
    refs: list[Mapping[str, object]] = []
    for item in cast(Sequence[object], refs_value):
        if not isinstance(item, Mapping):
            raise TypeError("Changed-record refs must contain objects")
        refs.append(cast(Mapping[str, object], item))
    semantic_keys_value = raw.get("semantic_keys") or ()
    if not isinstance(semantic_keys_value, Sequence) or isinstance(
        semantic_keys_value, (str, bytes)
    ):
        raise TypeError("Changed-record semantic_keys must be a sequence")
    semantic_keys = tuple(cast(Sequence[object], semantic_keys_value))
    search_text_value = raw.get("search_text")
    if search_text_value is not None and not isinstance(search_text_value, str):
        raise TypeError("Changed-record search_text must be a string")
    expected = raw["expected_revision"] if "expected_revision" in raw else _UNSET
    return RecordChange(
        record_id=str(raw.get("record_id") or raw.get("id") or ""),
        collection=str(raw.get("collection") or ""),
        payload=payload,
        change_kind=str(raw.get("change_kind") or "upsert"),
        expected_revision=expected,
        refs=tuple(refs),
        semantic_keys=semantic_keys,
        search_text=search_text_value,
    )


def _valid_record_id(value: object) -> str:
    record_id = normalize_space(value)
    if not SAFE_RECORD_ID.fullmatch(record_id):
        raise ValueError(f"Invalid opaque record ID: {record_id!r}")
    return record_id


def _key_path(store_path: Path) -> Path:
    return _v4_store.key_path(store_path)


def _validate_plan_key_stat(
    key_path: Path,
    key_stat: os.stat_result,
    descriptor: int | None = None,
    *,
    allow_publication_link: bool = False,
) -> None:
    _v4_store.validate_plan_key_stat(
        key_path,
        key_stat,
        descriptor,
        allow_publication_link=allow_publication_link,
        dependencies=_v4_store.PlanKeyStatDependencies(
            owner_is_current_user=cast(_v4_store.OwnerCheck, owner_is_current_user),
            mode_has_any=cast(_v4_store.ModeCheck, mode_has_any),
            path_safety_error_type=PathSafetyError,
        ),
    )


def _read_private_plan_key(key_path: Path) -> bytes:
    return _v4_store.read_private_plan_key(
        key_path,
        dependencies=_v4_store.ReadPlanKeyDependencies(
            validate_plan_key_stat=_validate_plan_key_stat,
            key_bytes=_key_bytes,
            path_safety_error_type=PathSafetyError,
        ),
    )


def _read_private_plan_key_after_publication(key_path: Path) -> bytes:
    """Boundedly wait out the publisher's transient two-link window."""
    return _v4_store.read_private_plan_key_after_publication(
        key_path,
        dependencies=_v4_store.PublishedPlanKeyDependencies(
            retry_attempts=PLAN_KEY_LINK_RETRY_ATTEMPTS,
            retry_delay_seconds=PLAN_KEY_LINK_RETRY_DELAY_SECONDS,
            sleep=time.sleep,
            read_private_plan_key=_read_private_plan_key,
            validate_plan_key_stat=_validate_plan_key_stat,
            path_safety_error_type=PathSafetyError,
        ),
    )


def _connection_pragmas(
    connection: sqlite3.Connection, *, readonly: bool, wal: bool = True
) -> None:
    _v4_store.connection_pragmas(
        connection,
        readonly=readonly,
        wal=wal,
        busy_timeout_ms=DEFAULT_BUSY_TIMEOUT_MS,
        store_format_error_type=StoreFormatError,
    )


def _load_or_create_plan_key(
    store_path: Path, supplied: bytes | str | None, *, create: bool
) -> bytes | None:
    return _v4_store.load_or_create_plan_key(
        store_path,
        supplied,
        create=create,
        dependencies=_v4_store.LoadPlanKeyDependencies(
            key_path=_key_path,
            key_bytes=_key_bytes,
            b64encode=_b64encode,
            atomic_bytes=_atomic_bytes,
            read_private_plan_key_after_publication=(_read_private_plan_key_after_publication),
            token_bytes=secrets.token_bytes,
            compare_digest=hmac.compare_digest,
            path_safety_error_type=PathSafetyError,
        ),
    )


def _payload_search_text(collection: str, payload: Mapping[str, object]) -> str:
    """Build a bounded, deterministic current-record FTS document."""
    parts: list[str] = [collection, normalize_space(payload.get("id"))]
    for key in (
        "type",
        "namespace",
        "name",
        "title",
        "kind",
        "family",
        "key",
        "value",
        "summary",
        "rationale",
        "status",
        "resolution",
        "next_action",
        "terminal_condition",
        "uri",
    ):
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)):
            parts.append(normalize_space(value))
    aliases = payload.get("aliases", [])
    if isinstance(aliases, list):
        parts.extend(normalize_space(item) for item in aliases if isinstance(item, str))
    return normalize_space(" ".join(item for item in parts if item))[:32_000]


def _extract_refs(
    payload: Mapping[str, object], explicit: Sequence[Mapping[str, object]]
) -> list[dict[str, object]]:
    raw_refs: list[tuple[str, str, int | None]] = []
    list_fields = {
        "entity_refs": "entity",
        "owner_refs": "owner",
        "subject_refs": "subject",
        "evidence_refs": "evidence",
        "observation_refs": "observation",
    }
    scalar_fields = {
        "source_ref": "source",
        "surface_ref": "surface",
        "superseded_by": "superseded_by",
        "redirect_to": "redirect_target",
        "redirect_source_ref": "redirect_source",
        "source_entity_id": "relation_source",
        "target_entity_id": "relation_target",
    }
    for field, kind in list_fields.items():
        values = payload.get(field, [])
        if values is None:
            continue
        if not isinstance(values, list):
            raise ValueError(f"{field} must be a list")
        for ordinal, value in enumerate(values):
            if value:
                raw_refs.append((_valid_record_id(value), kind, ordinal))
    for field, kind in scalar_fields.items():
        value = payload.get(field)
        if value:
            raw_refs.append((_valid_record_id(value), kind, 0))
    identity_keys = payload.get("identity_keys", [])
    if isinstance(identity_keys, list):
        ordinal = 0
        for identity in identity_keys:
            if not isinstance(identity, Mapping):
                continue
            evidence = identity.get("evidence_refs", [])
            if not isinstance(evidence, list):
                continue
            for value in evidence:
                if value:
                    raw_refs.append((_valid_record_id(value), "identity_evidence", ordinal))
                    ordinal += 1
    for item in explicit:
        if not isinstance(item, Mapping):
            raise ValueError("Explicit refs must be objects")
        target = _valid_record_id(item.get("target_id"))
        kind = normalize_key(item.get("kind")) or "related"
        requested_ordinal_value = item.get("ordinal")
        if requested_ordinal_value is not None and not isinstance(
            requested_ordinal_value, (str, bytes, bytearray, int, float)
        ):
            raise ValueError("Reference ordinal must be numeric")
        requested_ordinal = (
            int(requested_ordinal_value) if requested_ordinal_value is not None else None
        )
        if requested_ordinal is not None:
            if requested_ordinal < 0:
                raise ValueError("Reference ordinals must be nonnegative")
        raw_refs.append((target, kind, requested_ordinal))
    # Deduplicate by semantic pair and then assign dense per-kind ordinals.
    unique: list[tuple[str, str, int | None]] = []
    seen: set[tuple[str, str]] = set()
    for target, kind, requested in raw_refs:
        signature = (target, kind)
        if signature not in seen:
            unique.append((target, kind, requested))
            seen.add(signature)
    counters: dict[str, int] = {}
    result: list[dict[str, object]] = []
    occupied: set[tuple[str, int]] = set()
    for target, kind, requested in unique:
        assigned_ordinal = requested
        if assigned_ordinal is None or (kind, assigned_ordinal) in occupied:
            assigned_ordinal = counters.get(kind, 0)
            while (kind, assigned_ordinal) in occupied:
                assigned_ordinal += 1
        occupied.add((kind, assigned_ordinal))
        counters[kind] = max(counters.get(kind, 0), assigned_ordinal + 1)
        result.append({"target_id": target, "kind": kind, "ordinal": assigned_ordinal})
    return sorted(result, key=lambda item: (item["kind"], item["ordinal"], item["target_id"]))


def _selector(selector: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(selector, Mapping):
        raise SubscriptionError("Subscription selector must be an object")
    result: dict[str, object] = {"all": bool(selector.get("all", False))}
    for field in ("record_ids", "entity_ids", "collections", "discovery_kinds"):
        values = selector.get(field, [])
        if not isinstance(values, list):
            raise SubscriptionError(f"selector.{field} must be a list")
        normalized: list[str] = []
        for value in values:
            text = normalize_space(value)
            if field in {"record_ids", "entity_ids"} and text:
                text = _valid_record_id(text)
            elif field == "collections" and text not in COLLECTION_SET:
                raise SubscriptionError(f"Unknown selector collection: {text}")
            elif field == "discovery_kinds":
                text = normalize_key(text)
            if text and text not in normalized:
                normalized.append(text)
        result[field] = sorted(normalized)
    if not result["all"] and not any(result[field] for field in result if field != "all"):
        raise SubscriptionError("Selector must name a scope or explicitly set all=true")
    return result


class KnowledgeGraphV4:
    """A process-safe same-machine SQLite/WAL canonical graph store."""

    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        *,
        readonly: bool,
        plan_hmac_key: bytes | None,
    ) -> None:
        self._session = _v4_store.StoreSession(
            path,
            connection,
            readonly=readonly,
            plan_hmac_key=plan_hmac_key,
        )
        # Preserve the facade's historical mutable instance attributes.
        self.path = path
        self.readonly = readonly

    @classmethod
    def create(
        cls, path: str | Path, plan_hmac_key: bytes | str | None = None
    ) -> "KnowledgeGraphV4":
        return create_store(path, plan_hmac_key=plan_hmac_key)

    @classmethod
    def open(
        cls,
        path: str | Path,
        readonly: bool = False,
        plan_hmac_key: bytes | str | None = None,
    ) -> "KnowledgeGraphV4":
        return open_store(path, readonly=readonly, plan_hmac_key=plan_hmac_key)

    @classmethod
    def import_v3(
        cls,
        graph: Mapping[str, object],
        target_path: str | Path,
        source_sha256: str,
        **kwargs: object,
    ) -> "KnowledgeGraphV4":
        importer = cast(_ImportV3Callable, import_v3_graph)
        return importer(graph, target_path, source_sha256, **kwargs)

    def __enter__(self) -> "KnowledgeGraphV4":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._session.connection(closed_error_type=V4Error)

    def read_snapshot(self) -> AbstractContextManager[None]:
        """Pin one materialized read, preserving any caller-owned transaction."""

        return _read_snapshot(self.connection)

    def _query_dependencies(self) -> _v4_queries.QueryDependencies:
        """Resolve facade policy at call time for read-only query kernels."""

        return _v4_queries.QueryDependencies(
            collection_set=COLLECTION_SET,
            max_history_limit=MAX_FEED_LIMIT,
            valid_record_id=_valid_record_id,
            normalize_key=normalize_key,
            normalize_space=normalize_space,
            stable_hash=stable_hash,
            store_format_error_type=StoreFormatError,
            validation_error_type=ValidationError,
        )

    def state(self) -> _v4_queries.GraphState:
        return _v4_queries.state(
            self.connection,
            dependencies=self._query_dependencies(),
        )

    @property
    def watermark(self) -> int:
        return int(self.state()["watermark"])

    def current_revisions(
        self, record_ids: Iterable[str]
    ) -> dict[str, _v4_queries.CurrentRevision | None]:
        return _v4_queries.current_revisions(
            self.connection,
            record_ids,
            dependencies=self._query_dependencies(),
        )

    def get_record(self, record_id: str, revision: int | None = None) -> _v4_queries.RecordSnapshot:
        return _v4_queries.get_record(
            self.connection,
            record_id,
            revision,
            dependencies=self._query_dependencies(),
        )

    def get_records(self, record_ids: Iterable[str]) -> list[_v4_queries.RecordSnapshot]:
        return _v4_queries.get_records(
            self.connection,
            record_ids,
            dependencies=self._query_dependencies(),
        )

    def history(
        self,
        record_id: str,
        *,
        limit: int = 100,
        before_revision: int | None = None,
    ) -> _v4_queries.HistoryResult:
        return _v4_queries.history(
            self.connection,
            record_id,
            limit=limit,
            before_revision=before_revision,
            dependencies=self._query_dependencies(),
        )

    def find_entities(
        self,
        *,
        identity_keys: Sequence[Mapping[str, object]] = (),
        namespace: str | None = None,
        entity_type: str | None = None,
        names: Sequence[str] = (),
        limit: int = 100,
    ) -> list[_v4_queries.RecordSnapshot]:
        with self.read_snapshot():
            return _v4_queries.find_entities(
                self.connection,
                identity_keys=identity_keys,
                namespace=namespace,
                entity_type=entity_type,
                names=names,
                limit=limit,
                records_loader=self.get_records,
                dependencies=self._query_dependencies(),
            )

    def records_referencing(
        self,
        target_ids: Iterable[str],
        *,
        ref_kinds: Iterable[str] | None = None,
        limit: int = 500,
    ) -> list[_v4_queries.RecordSnapshot]:
        with self.read_snapshot():
            return _v4_queries.records_referencing(
                self.connection,
                target_ids,
                ref_kinds=ref_kinds,
                limit=limit,
                records_loader=self.get_records,
                dependencies=self._query_dependencies(),
            )

    def find_claim_surfaces(
        self,
        *,
        surface_id: str | None = None,
        family: str | None = None,
        key: str | None = None,
        subject_signature: str | None = None,
        subject_ids: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[_v4_queries.RecordSnapshot]:
        with self.read_snapshot():
            return _v4_queries.find_claim_surfaces(
                self.connection,
                surface_id=surface_id,
                family=family,
                key=key,
                subject_signature=subject_signature,
                subject_ids=subject_ids,
                limit=limit,
                records_loader=self.get_records,
                dependencies=self._query_dependencies(),
            )

    def current_claims(
        self, surface_ids: Iterable[str], *, limit: int = 500
    ) -> list[_v4_queries.RecordSnapshot]:
        return _v4_queries.current_claims(
            self.connection,
            surface_ids,
            limit=limit,
            dependencies=self._query_dependencies(),
        )

    def semantic_key_owner(self, collection: str, key_kind: str, key: str) -> str | None:
        return _v4_queries.semantic_key_owner(
            self.connection,
            collection,
            key_kind,
            key,
            dependencies=self._query_dependencies(),
        )

    def _require_writable(self) -> None:
        if self.readonly:
            raise V4Error("Store was opened read-only")

    def _require_token_key(self) -> bytes:
        if self._session.plan_hmac_key is None:
            raise PlanTokenError(
                f"No plan key is available; supply it or restore {_key_path(self.path)}"
            )
        return self._session.plan_hmac_key

    def _constraint_owner(self, constraint: Mapping[str, object]) -> str | None:
        return _v4_registry.constraint_owner(
            self.connection,
            constraint,
            dependencies=_v4_registry.ConstraintDependencies(
                normalize_key=normalize_key,
                semantic_key_owner=self.semantic_key_owner,
                plan_token_error_type=PlanTokenError,
            ),
        )

    def _plan_dependencies(self) -> _v4_registry.PlanDependencies:
        """Resolve live plan-token policy for one facade call."""

        return _v4_registry.PlanDependencies(
            valid_record_id=_valid_record_id,
            normalize_space=normalize_space,
            utc_now=utc_now,
            current_revisions_loader=cast(
                _v4_registry.CurrentRevisionsLoader,
                self.current_revisions,
            ),
            state_loader=cast(Callable[[], Mapping[str, object]], self.state),
            constraint_owner=self._constraint_owner,
            require_token_key=self._require_token_key,
            sign_plan_token=sign_plan_token,
            verify_plan_token=verify_plan_token,
            compare_digest=hmac.compare_digest,
            plan_token_error_type=PlanTokenError,
            concurrency_conflict_type=ConcurrencyConflict,
        )

    def create_plan_token(
        self,
        record_ids: Iterable[str],
        fingerprint: str,
        *,
        constraint_expectations: Sequence[Mapping[str, object]] = (),
    ) -> str:
        """Sign current record revisions and relevant absence predicates."""
        with self.read_snapshot():
            return _v4_registry.create_plan_token(
                record_ids,
                fingerprint,
                constraint_expectations=constraint_expectations,
                dependencies=self._plan_dependencies(),
            )

    def _verify_capture_plan(
        self, token: str, fingerprint: str, state: Mapping[str, object]
    ) -> dict[str, object]:
        return _v4_registry.verify_capture_plan(
            token,
            fingerprint,
            state,
            dependencies=self._plan_dependencies(),
        )

    def register_identity_scheme(
        self,
        scheme: str,
        *,
        normalization: str = "binary",
        governed: bool = False,
    ) -> _v4_registry.IdentitySchemeResult:
        self._require_writable()
        return _v4_registry.register_identity_scheme(
            self.connection,
            scheme,
            normalization=normalization,
            governed=governed,
            state_loader=cast(Callable[[], Mapping[str, object]], self.state),
            normalize_key=normalize_key,
            identity_conflict_type=IdentityConflict,
        )

    def _current_payload(self, record_id: str) -> dict[str, object] | None:
        return _v4_registry.current_payload(
            self.connection,
            record_id,
            json_loads=cast(_v4_registry.JsonLoader, json.loads),
        )

    def _registry_dependencies(self) -> _v4_registry.RegistryDependencies:
        """Resolve live normalization and conflict policy for one write."""

        return _v4_registry.RegistryDependencies(
            normalize_key=normalize_key,
            normalize_space=normalize_space,
            stable_hash=stable_hash,
            valid_record_id=_valid_record_id,
            current_payload_loader=self._current_payload,
            identity_conflict_type=IdentityConflict,
        )

    def _apply_entity_registry(
        self,
        record_id: str,
        payload: Mapping[str, object],
        watermark: int,
        pending_payloads: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        _v4_registry.apply_entity_registry(
            self.connection,
            record_id,
            payload,
            watermark,
            pending_payloads,
            dependencies=self._registry_dependencies(),
        )

    def _apply_surface_registry(
        self, record_id: str, payload: Mapping[str, object], watermark: int
    ) -> None:
        _v4_registry.apply_surface_registry(
            self.connection,
            record_id,
            payload,
            watermark,
            dependencies=self._registry_dependencies(),
        )

    def _apply_semantic_registry(
        self,
        record_id: str,
        collection: str,
        semantic_keys: Sequence[object],
        watermark: int,
    ) -> None:
        _v4_registry.apply_semantic_registry(
            self.connection,
            record_id,
            collection,
            semantic_keys,
            watermark,
            dependencies=self._registry_dependencies(),
        )

    def apply_changes(
        self,
        changes: Sequence[RecordChange | Mapping[str, object]],
        *,
        fingerprint: str,
        action: str = "capture",
        discovery_kind: str = "durable_discovery",
        producer_agent_id: str | None = None,
        task_id: str | None = None,
        parent_task_id: str | None = None,
        run_id: str | None = None,
        based_on_watermark: int | None = None,
        expected_revisions: Mapping[str, int | None] | None = None,
        plan_token: str | None = None,
        idempotency_key: str | None = None,
        source_ref: str | None = None,
        capture_policy: Mapping[str, object] | None = None,
        details: Mapping[str, object] | None = None,
        redaction_count: int = 0,
        preflight: Callable[[], None] | None = None,
    ) -> _v4_transactions.MutationResult:
        """Atomically apply a packet-sized set of current-record transitions."""

        self._require_writable()
        dependencies = _v4_transactions.TransactionDependencies(
            collections=COLLECTION_SET,
            hash_algorithm=HASH_ALGORITHM,
            unset=_UNSET,
            coerce_change=cast(_v4_transactions.ChangeCoercer, _coerce_change),
            valid_record_id=_valid_record_id,
            normalize_space=normalize_space,
            normalize_key=normalize_key,
            canonical_json=canonical_json,
            sha256_text=sha256_text,
            stable_hash=stable_hash,
            compare_digest=hmac.compare_digest,
            utc_now=utc_now,
            payload_search_text=_payload_search_text,
            extract_refs=cast(_v4_transactions.ReferenceExtractor, _extract_refs),
            state_loader=self.state,
            current_revisions_loader=cast(
                _v4_transactions.CurrentRevisionsLoader,
                self.current_revisions,
            ),
            verify_capture_plan=self._verify_capture_plan,
            current_payload_loader=self._current_payload,
            apply_entity_registry=self._apply_entity_registry,
            apply_surface_registry=self._apply_surface_registry,
            apply_semantic_registry=self._apply_semantic_registry,
            v4_error_type=V4Error,
            concurrency_conflict_type=ConcurrencyConflict,
            identity_conflict_type=IdentityConflict,
        )
        return _v4_transactions.apply_changes(
            self.connection,
            changes,
            fingerprint=fingerprint,
            action=action,
            discovery_kind=discovery_kind,
            producer_agent_id=producer_agent_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            run_id=run_id,
            based_on_watermark=based_on_watermark,
            expected_revisions=expected_revisions,
            plan_token=plan_token,
            idempotency_key=idempotency_key,
            source_ref=source_ref,
            capture_policy=capture_policy,
            details=details,
            redaction_count=redaction_count,
            preflight=preflight,
            dependencies=dependencies,
        )

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        collections: Iterable[str] | None = None,
    ) -> dict[str, object]:
        with self.read_snapshot():
            return cast(
                dict[str, object],
                _v4_queries.search(
                    self.connection,
                    query,
                    limit=limit,
                    collections=collections,
                    records_loader=self.get_records,
                    watermark_loader=lambda: self.watermark,
                    dependencies=self._query_dependencies(),
                ),
            )

    def search_subscription(
        self, subscription_id: str, query: str, *, limit: int = 10, match: str = "all"
    ) -> _v4_queries.ScopedSearchResult:
        """Search current, non-sensitive records in one existing active scope."""

        with self.read_snapshot():
            subscription = self._subscription(subscription_id)
            selector = cast(_v4_feed.Selector, json.loads(subscription["selector_json"]))
            predicate, parameters = _v4_coordination.current_record_predicate(selector)
            return _v4_queries.search_scoped(
                self.connection,
                subscription_id,
                query,
                predicate,
                parameters,
                limit=limit,
                match=match,
                records_loader=self.get_records,
                watermark_loader=lambda: self.watermark,
                dependencies=self._query_dependencies(),
            )

    def preferences(self, subscription_id: str) -> dict[str, object]:
        """Read stated preferences without broadening the existing task scope."""
        with self.read_snapshot():
            subscription = self._subscription(subscription_id)
            selector = cast(_v4_feed.Selector, json.loads(subscription["selector_json"]))
            predicate, parameters = _v4_coordination.current_record_predicate(selector)
            return _v4_queries.preferences(
                self.connection,
                predicate,
                parameters,
                subject_ids=sorted(set(selector["entity_ids"]) | set(selector["record_ids"])),
                all_subjects=selector["all"],
                now=utc_now(),
            )

    def review_loops(
        self, subscription_id: str, *, overdue_days: int = 0, age_days: int = 30, limit: int = 20
    ) -> dict[str, object]:
        """Read unfinished, overdue loops within one existing subscription."""

        with self.read_snapshot():
            subscription = self._subscription(subscription_id)
            selector = cast(_v4_feed.Selector, json.loads(subscription["selector_json"]))
            predicate, parameters = _v4_coordination.current_record_predicate(selector)
            return _v4_queries.review_loops(
                self.connection,
                subscription_id,
                predicate,
                parameters,
                now=utc_now(),
                overdue_days=overdue_days,
                age_days=age_days,
                limit=limit,
            )

    def _feed_predicate(
        self, selector: Mapping[str, object] | None
    ) -> tuple[str, list[object], dict[str, object] | None]:
        predicate, parameters, normalized = _v4_feed.feed_predicate(
            selector,
            dependencies=_v4_feed.FeedPredicateDependencies(
                selector_normalizer=cast(_v4_feed.SelectorNormalizer, _selector),
            ),
        )
        return (
            predicate,
            cast(list[object], parameters),
            cast(dict[str, object] | None, normalized),
        )

    def mutation_feed(
        self,
        since_watermark: int,
        *,
        through_watermark: int | None = None,
        cursor: str | None = None,
        limit: int = 100,
        selector: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Return snapshot-pinned exact record revisions ordered by mutation."""
        with self.read_snapshot():
            return cast(
                dict[str, object],
                _v4_feed.mutation_feed(
                    self.connection,
                    since_watermark,
                    through_watermark=through_watermark,
                    cursor=cursor,
                    limit=limit,
                    selector=selector,
                    dependencies=_v4_feed.FeedDependencies(
                        max_feed_limit=MAX_FEED_LIMIT,
                        state_loader=self.state,
                        predicate_builder=cast(
                            _v4_feed.FeedPredicateBuilder,
                            self._feed_predicate,
                        ),
                        stable_hash=stable_hash,
                        verify_plan_token=verify_plan_token,
                        sign_plan_token=sign_plan_token,
                        require_token_key=self._require_token_key,
                        json_loads=cast(_v4_feed.JsonLoader, json.loads),
                        plan_token_error_type=PlanTokenError,
                    ),
                ),
            )

    def _consumer_dependencies(self) -> _v4_coordination.ConsumerDependencies:
        """Resolve live consumer policy for one facade call."""

        return _v4_coordination.ConsumerDependencies(
            max_status_subscriptions=MAX_CONSUMER_STATUS_SUBSCRIPTIONS,
            valid_record_id=_valid_record_id,
            normalize_space=normalize_space,
            canonical_json=canonical_json,
            json_loads=cast(_v4_feed.JsonLoader, json.loads),
            utc_now=utc_now,
            subscription_error_type=SubscriptionError,
        )

    def consumer_status(self, consumer_id: str) -> dict[str, object]:
        """Inspect one persisted consumer owner and its active cursors read-only."""
        return _v4_coordination.consumer_status(
            self.connection,
            consumer_id,
            dependencies=self._consumer_dependencies(),
        )

    def register_consumer(
        self,
        consumer_id: str,
        *,
        agent_id: str,
        task_id: str,
        parent_task_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self._require_writable()
        return _v4_coordination.register_consumer(
            self.connection,
            consumer_id,
            agent_id=agent_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            metadata=metadata,
            dependencies=self._consumer_dependencies(),
        )

    def rebind_consumer(
        self,
        consumer_id: str,
        *,
        task_id: str,
        expected_old_agent_id: str,
        new_agent_id: str,
        parent_task_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        """Move one task consumer to a new logical agent without losing cursors.

        The existing task ID and old agent ID are explicit ownership
        preconditions.  A retry after the first successful transition is
        accepted only when the consumer is already bound to ``new_agent_id``.
        Subscription rows are deliberately untouched, so selector versions and
        acknowledgement watermarks remain continuous across an agent rename.
        """
        self._require_writable()
        return _v4_coordination.rebind_consumer(
            self.connection,
            consumer_id,
            task_id=task_id,
            expected_old_agent_id=expected_old_agent_id,
            new_agent_id=new_agent_id,
            parent_task_id=parent_task_id,
            metadata=metadata,
            dependencies=self._consumer_dependencies(),
        )

    def subscribe(
        self,
        consumer_id: str,
        selector: Mapping[str, object],
        *,
        subscription_id: str | None = None,
        start_watermark: int | None = None,
    ) -> dict[str, object]:
        self._require_writable()
        return _v4_coordination.subscribe(
            self.connection,
            consumer_id,
            selector,
            subscription_id=subscription_id,
            start_watermark=start_watermark,
            dependencies=_v4_coordination.SubscriptionDependencies(
                valid_record_id=_valid_record_id,
                selector_normalizer=cast(_v4_feed.SelectorNormalizer, _selector),
                stable_hash=stable_hash,
                state_loader=self.state,
                canonical_json=canonical_json,
                utc_now=utc_now,
                subscription_error_type=SubscriptionError,
            ),
        )

    def _subscription(self, subscription_id: str) -> sqlite3.Row:
        return _v4_coordination.subscription(
            self.connection,
            subscription_id,
            valid_record_id=_valid_record_id,
            subscription_error_type=SubscriptionError,
        )

    def subscription_identity(self, subscription_id: str) -> dict[str, object] | None:
        """Read one exact active subscription owner without a status projection."""

        return _v4_coordination.subscription_identity(
            self.connection,
            subscription_id,
            valid_record_id=_valid_record_id,
        )

    def inbox(
        self,
        subscription_id: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
        through_watermark: int | None = None,
    ) -> dict[str, object]:
        with self.read_snapshot():
            subscription = self._subscription(subscription_id)
            return _v4_coordination.inbox(
                subscription,
                limit=limit,
                cursor=cursor,
                through_watermark=through_watermark,
                dependencies=_v4_coordination.InboxDependencies(
                    json_loads=cast(_v4_feed.JsonLoader, json.loads),
                    mutation_feed=cast(
                        _v4_coordination.MutationFeedLoader,
                        self.mutation_feed,
                    ),
                    state_loader=self.state,
                    sign_plan_token=sign_plan_token,
                    require_token_key=self._require_token_key,
                ),
            )

    def ack(
        self,
        subscription_id: str,
        through_watermark: int,
        *,
        ack_token: str | None = None,
        force: bool = False,
    ) -> dict[str, object]:
        self._require_writable()
        subscription = self._subscription(subscription_id)
        return _v4_coordination.ack(
            self.connection,
            subscription,
            subscription_id,
            through_watermark,
            ack_token=ack_token,
            force=force,
            dependencies=_v4_coordination.AcknowledgementDependencies(
                state_loader=self.state,
                subscription_loader=self._subscription,
                verify_plan_token=verify_plan_token,
                require_token_key=self._require_token_key,
                utc_now=utc_now,
                subscription_error_type=SubscriptionError,
            ),
        )

    def context_pack(
        self,
        subscription_id: str,
        *,
        max_items: int = 100,
        max_chars: int = 50_000,
    ) -> dict[str, object]:
        with self.read_snapshot():
            subscription = self._subscription(subscription_id)
            return _v4_coordination.context_pack(
                self.connection,
                subscription,
                subscription_id,
                max_items=max_items,
                max_chars=max_chars,
                dependencies=_v4_coordination.ContextPackDependencies(
                    max_context_items=MAX_CONTEXT_ITEMS,
                    max_context_chars=MAX_CONTEXT_CHARS,
                    json_loads=cast(_v4_feed.JsonLoader, json.loads),
                    watermark_loader=lambda: self.watermark,
                ),
            )

    def export_current_graph(
        self,
        *,
        schema_version: str = SCHEMA_VERSION,
        include_journal: bool = True,
    ) -> dict[str, object]:
        """Export one deterministic committed SQLite snapshot; never used by writes."""
        return cast(
            dict[str, object],
            _v4_backup.export_current_graph(
                self.connection,
                schema_version=schema_version,
                include_journal=include_journal,
                snapshot_loader=cast(
                    _v4_backup.SnapshotLoader,
                    self._export_current_graph_snapshot,
                ),
                v4_error_type=V4Error,
            ),
        )

    def _export_current_graph_snapshot(
        self,
        *,
        schema_version: str,
        include_journal: bool,
    ) -> dict[str, object]:
        return cast(
            dict[str, object],
            _v4_backup.export_current_graph_snapshot(
                self.connection,
                schema_version=schema_version,
                include_journal=include_journal,
                dependencies=_v4_backup.ExportDependencies(
                    collections=COLLECTIONS,
                    current_schema_version=SCHEMA_VERSION,
                    json_loads=json.loads,
                    stable_hash=stable_hash,
                    state_loader=self.state,
                ),
            ),
        )

    def _state_root(self) -> str:
        return _v4_validation.state_root(
            self.connection,
            digest_factory=hashlib.sha256,
        )

    def _v4_entry_material(self, mutation: sqlite3.Row) -> dict[str, object]:
        return cast(
            dict[str, object],
            _v4_validation.entry_material(
                self.connection,
                mutation,
                json_loads=json.loads,
            ),
        )

    def validate(self, *, full: bool = False) -> dict[str, object]:
        """Validate SQL, referential, revision, index, and optional hash-chain state."""
        with self.read_snapshot():
            dependencies = _v4_validation.ValidationDependencies(
                collections=COLLECTIONS,
                hash_algorithm=HASH_ALGORITHM,
                legacy_hash_algorithm=LEGACY_HASH_ALGORITHM,
                sha256_text=sha256_text,
                stable_hash=stable_hash,
                compare_digest=hmac.compare_digest,
                json_loads=json.loads,
                state_loader=self.state,
                state_root_loader=self._state_root,
                entry_material_loader=self._v4_entry_material,
            )
            return cast(
                dict[str, object],
                _v4_validation.validate_store(
                    self.connection,
                    full=full,
                    dependencies=dependencies,
                ),
            )

    def checkpoint(self, mode: str = "PASSIVE") -> dict[str, object]:
        self._require_writable()
        return cast(
            dict[str, object],
            _v4_backup.checkpoint(
                self.connection,
                self.path,
                mode,
                path_factory=Path,
            ),
        )

    def create_audit_checkpoint(
        self, *, backup_manifest_hash: str | None = None
    ) -> dict[str, object]:
        self._require_writable()
        return cast(
            dict[str, object],
            _v4_backup.create_audit_checkpoint(
                self.connection,
                backup_manifest_hash=backup_manifest_hash,
                dependencies=_v4_backup.CheckpointDependencies(
                    state_loader=self.state,
                    state_root_loader=self._state_root,
                    normalize_space=normalize_space,
                    utc_now=utc_now,
                    v4_error_type=V4Error,
                    validation_error_type=ValidationError,
                ),
            ),
        )

    def backup(self, destination: str | Path, *, overwrite: bool = False) -> dict[str, object]:
        """Create a snapshot backup whose manifest is its completion marker."""
        return cast(
            dict[str, object],
            _v4_backup.backup(
                self.connection,
                self.path,
                destination,
                overwrite=overwrite,
                dependencies=_v4_backup.BackupDependencies(
                    path_factory=Path,
                    assert_safe_path=_assert_safe_path,
                    ensure_private_directory=_ensure_private_directory,
                    make_temporary_file=tempfile.mkstemp,
                    close_descriptor=os.close,
                    connect=sqlite3.connect,
                    set_private_path_mode=set_private_path_mode,
                    fsync_descriptor=os.fsync,
                    digest_factory=hashlib.sha256,
                    json_dumps=cast(_v4_backup.JsonDumper, json.dumps),
                    stage_bytes=_stage_bytes,
                    publish_staged_file=_publish_staged_file,
                    fsync_parent=_fsync_parent,
                    sha256_verified_published_file=_sha256_verified_published_file,
                    read_verified_published_bytes=_read_verified_published_bytes,
                    utc_now=utc_now,
                    v4_error_type=V4Error,
                    store_format_error_type=StoreFormatError,
                    validation_error_type=ValidationError,
                ),
            ),
        )


def create_store(path: str | Path, *, plan_hmac_key: bytes | str | None = None) -> KnowledgeGraphV4:
    """Create a private empty v4 store atomically, then enable WAL."""
    return cast(
        KnowledgeGraphV4,
        _v4_store.create_store(
            path,
            plan_hmac_key=plan_hmac_key,
            dependencies=_v4_store.CreateStoreDependencies(
                assert_safe_path=_assert_safe_path,
                detect_store=detect_store,
                ensure_private_directory=_ensure_private_directory,
                make_temporary_file=cast(_v4_store.TemporaryFileFactory, tempfile.mkstemp),
                close_descriptor=os.close,
                connect=cast(_v4_store.Connect, sqlite3.connect),
                configure_connection=_connection_pragmas,
                ddl=DDL,
                schema_version=SCHEMA_VERSION,
                schema_user_version=SCHEMA_USER_VERSION,
                utc_now=utc_now,
                uuid4=uuid.uuid4,
                set_private_path_mode=set_private_path_mode,
                publish_staged_file=_publish_staged_file,
                fsync_parent=_fsync_parent,
                load_or_create_plan_key=_load_or_create_plan_key,
                open_store=cast(_v4_store.OpenStore, open_store),
                validation_error_type=ValidationError,
            ),
        ),
    )


def open_store(
    path: str | Path,
    *,
    readonly: bool = False,
    plan_hmac_key: bytes | str | None = None,
) -> KnowledgeGraphV4:
    """Open an existing local v4 store with safe per-connection pragmas."""
    return cast(
        KnowledgeGraphV4,
        _v4_store.open_store(
            path,
            readonly=readonly,
            plan_hmac_key=plan_hmac_key,
            dependencies=_v4_store.OpenStoreDependencies(
                assert_safe_path=_assert_safe_path,
                detect_store=detect_store,
                owner_is_current_user=cast(_v4_store.OwnerCheck, owner_is_current_user),
                mode_has_any=cast(_v4_store.ModeCheck, mode_has_any),
                connect=cast(_v4_store.Connect, sqlite3.connect),
                sqlite_readonly_uri=_sqlite_readonly_uri,
                configure_connection=_connection_pragmas,
                busy_timeout_ms=DEFAULT_BUSY_TIMEOUT_MS,
                schema_version=SCHEMA_VERSION,
                previous_schema_user_version=(_v4_schema.PREVIOUS_SCHEMA_USER_VERSION),
                schema_user_version=SCHEMA_USER_VERSION,
                record_revision_payloads_table_sql=(_v4_schema.RECORD_REVISION_PAYLOADS_TABLE_SQL),
                legacy_trigger_sql=_v4_schema.LEGACY_APPEND_ONLY_TRIGGERS,
                upgrade_trigger_sql=_v4_schema.APPEND_ONLY_UPGRADE_TRIGGERS,
                required_trigger_sql=_v4_schema.APPEND_ONLY_TRIGGERS,
                load_or_create_plan_key=_load_or_create_plan_key,
                build_store=cast(_v4_store.StoreBuilder, KnowledgeGraphV4),
                path_safety_error_type=PathSafetyError,
                store_format_error_type=StoreFormatError,
            ),
        ),
    )


def _validate_v3_import(graph: Mapping[str, object]) -> tuple[int, str, int, str]:
    return _v4_import.validate_v3_import(
        graph,
        dependencies=_v4_import.V3ValidationDependencies(
            collections=COLLECTIONS,
            valid_record_id=_valid_record_id,
            normalize_space=normalize_space,
            stable_hash=stable_hash,
            compare_digest=hmac.compare_digest,
            validation_error_type=ValidationError,
        ),
    )


def import_v3_graph(
    graph: Mapping[str, object],
    target_path: str | Path,
    source_sha256: str,
    *,
    producer_agent_id: str = "migration",
    task_id: str | None = None,
    parent_task_id: str | None = None,
    discovery_kind: str = "migration",
    plan_hmac_key: bytes | str | None = None,
) -> KnowledgeGraphV4:
    """Import validated v3 state into a new side-by-side v4 store."""
    return cast(
        KnowledgeGraphV4,
        _v4_import.import_v3_graph(
            graph,
            target_path,
            source_sha256,
            producer_agent_id=producer_agent_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            discovery_kind=discovery_kind,
            plan_hmac_key=plan_hmac_key,
            dependencies=_v4_import.ImportDependencies(
                collections=COLLECTIONS,
                schema_version=SCHEMA_VERSION,
                hash_algorithm=HASH_ALGORITHM,
                legacy_hash_algorithm=LEGACY_HASH_ALGORITHM,
                transform_version="v3-to-v4.sqlite-wal.1",
                validate_v3_import=_validate_v3_import,
                valid_source_sha256=lambda value: (
                    re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None
                ),
                normalize_space=normalize_space,
                normalize_key=normalize_key,
                valid_record_id=_valid_record_id,
                stable_hash=stable_hash,
                canonical_json=canonical_json,
                sha256_text=sha256_text,
                payload_search_text=_payload_search_text,
                extract_refs=cast(_v4_import.ReferenceExtractor, _extract_refs),
                create_store=cast(_v4_import.CreateStore, create_store),
                path_factory=cast(_v4_import.PathFactory, Path),
                key_path=_key_path,
                utc_now=utc_now,
                validation_error_type=ValidationError,
            ),
        ),
    )


__all__ = [
    "SCHEMA_VERSION",
    "SCHEMA_USER_VERSION",
    "COLLECTIONS",
    "RecordChange",
    "KnowledgeGraphV4",
    "V4Error",
    "PathSafetyError",
    "StoreFormatError",
    "ConcurrencyConflict",
    "RevisionConflictError",
    "IdentityConflict",
    "PlanTokenError",
    "SubscriptionError",
    "ValidationError",
    "detect_store",
    "inspect_store_path",
    "create_store",
    "open_store",
    "import_v3_graph",
    "sign_plan_token",
    "verify_plan_token",
]
