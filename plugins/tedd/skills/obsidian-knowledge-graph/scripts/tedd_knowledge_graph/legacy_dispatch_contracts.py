"""Typed contracts shared by the legacy command-family dispatch modules.

The v3 graph remains a validated, heterogeneous JSON document.  Mutable graph
objects use ``dict[str, object]`` at that compatibility boundary; decoded JSON
values have a recursive, read-only shape until a command validates and narrows
the fields it consumes.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from typing import ContextManager, Protocol, TypeAlias, TypedDict

from .legacy_configuration import ConfigurationOutcome
from .v4_cli_contracts import (
    CaptureHandler,
    InitializeHandler,
    InitializeRequest,
    ReviewSanitizer,
    UpgradeHandler,
    UpgradeRequest,
)
from .v4_cli_contracts import (
    JsonObject as V4JsonObject,
)

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
JSONMap: TypeAlias = dict[str, object]
ProducerMap: TypeAlias = Mapping[str, object]


class ValidationResult(TypedDict):
    """Stable legacy validation payload consumed by command dispatch."""

    valid: bool
    errors: list[str]
    warnings: list[str]
    counts: dict[str, int]


class AtomicWrite(Protocol):
    def __call__(self, path: Path, content: str, mode: int = 0o600) -> None: ...


class CapturePacket(Protocol):
    def __call__(
        self,
        graph: JSONMap,
        raw_packet: JSONMap,
        authority_ceiling: str = "assistant_inference",
        allow_replace: bool = False,
        allow_redirect: bool = False,
        producer: JSONMap | None = None,
        based_on_watermark: int | None = None,
        discovery_kind: str | None = None,
    ) -> JSONMap: ...


class ChangeFeedSince(Protocol):
    def __call__(
        self,
        graph: JSONMap,
        since_watermark: int,
        *,
        through_watermark: int | None,
        limit: int,
        candidate_ids: set[str] | None,
        mode: str,
        cursor: str | None,
    ) -> JSONMap: ...


class Configure(Protocol):
    def __call__(
        self,
        args: argparse.Namespace,
        config_path: Path,
        *,
        lock_timeout: float,
    ) -> ConfigurationOutcome: ...


class EnsureRecallIndex(Protocol):
    def __call__(
        self,
        graph: JSONMap,
        store_path: Path,
        canonical_digest: str,
        timeout_seconds: float,
        *,
        force: bool = False,
    ) -> JSONMap: ...


class GetIndexedGraphRecord(Protocol):
    def __call__(
        self,
        graph: JSONMap,
        index_path: Path,
        record_id: str,
        mode: str,
        expected_canonical_digest: str | None = None,
    ) -> JSONMap: ...


class IndexedRecallCandidates(Protocol):
    def __call__(
        self,
        index_path: Path,
        query: str,
        limit: int,
        expected_canonical_digest: str | None = None,
        *,
        exhaustive: bool = False,
        mode: str = "review_safe",
    ) -> set[str]: ...


class MaybeProjectLatest(Protocol):
    def __call__(
        self,
        args: argparse.Namespace,
        config: JSONMap,
        store_path: Path,
    ) -> JSONMap | None: ...


class MutationJournalCallback(Protocol):
    def __call__(
        self,
        graph: JSONMap,
        action: str,
        changed_ids: list[str],
        *,
        producer: JSONMap | None = None,
        based_on_watermark: int | None = None,
        discovery_kind: str | None = None,
        details: JSONMap | None = None,
    ) -> int: ...


class ProducerFromArgs(Protocol):
    def __call__(self, args: argparse.Namespace) -> ProducerMap: ...


class _DispatchCallbackAdapters:
    """Bridge covariant read-only producers to the mutable legacy callback."""

    _mutation_journal: MutationJournalCallback
    _producer_from_args: ProducerFromArgs

    def mutation_journal(
        self,
        graph: JSONMap,
        action: str,
        changed_ids: list[str],
        *,
        producer: ProducerMap,
        based_on_watermark: int | None,
        discovery_kind: str,
        details: JSONMap | None = None,
    ) -> int:
        return self._mutation_journal(
            graph,
            action,
            changed_ids,
            producer=dict(producer),
            based_on_watermark=based_on_watermark,
            discovery_kind=discovery_kind,
            details=details,
        )

    def producer_from_args(self, args: argparse.Namespace) -> ProducerMap:
        return self._producer_from_args(args)


class ProjectLatestFromStore(Protocol):
    def __call__(
        self,
        store_path: Path,
        vault: Path,
        root_name: str,
        mode: str,
        manifest_key: str,
        root_id: str | None,
        timeout_seconds: float,
        force_current_digest: str | None,
    ) -> JSONMap: ...


class RecallGraph(Protocol):
    def __call__(
        self,
        graph: JSONMap,
        query: str,
        limit: int,
        max_chars: int,
        mode: str,
        *,
        candidate_ids: set[str] | None,
        skip_validation: bool,
        backend: str,
        since_watermark: int | None,
    ) -> JSONMap: ...


class StoreLock(Protocol):
    def __call__(self, store_path: Path, timeout_seconds: float) -> ContextManager[None]: ...


class V4HandleCommand(Protocol):
    def __call__(
        self,
        args: argparse.Namespace,
        *,
        db_path: str | Path,
        plan_hmac_key: bytes | str | None = None,
        v3_store_path: str | Path | None = None,
        capture_handler: CaptureHandler | None = None,
        initialize_handler: InitializeHandler | None = None,
        upgrade_handler: UpgradeHandler | None = None,
        review_sanitizer: ReviewSanitizer | None = None,
    ) -> V4JsonObject | None: ...


class V4Initialize(Protocol):
    def __call__(
        self,
        request: InitializeRequest,
        *,
        config_path: Path,
        lock_timeout: float,
    ) -> JSONMap: ...


class V4Upgrade(Protocol):
    def __call__(
        self,
        request: UpgradeRequest,
        *,
        config_path: Path,
        lock_timeout: float,
    ) -> JSONMap: ...


class V4Project(Protocol):
    def __call__(
        self,
        store_path: Path,
        *,
        vault: Path,
        root_name: str,
        mode: str,
        manifest_key: str,
        root_id: str | None,
        timeout_seconds: float,
        force_current_digest: str | None = None,
    ) -> JSONMap: ...


class V4Mutation(Protocol):
    def __call__(self, store_path: Path, args: argparse.Namespace) -> JSONMap: ...


class V4ReadableStore(Protocol):
    def validate(self, *, full: bool) -> JSONMap: ...


class V4OpenStore(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        readonly: bool,
    ) -> ContextManager[V4ReadableStore]: ...


class V4ShouldHandle(Protocol):
    def __call__(self, args: argparse.Namespace, *, v4_enabled: bool) -> bool: ...


class V4MigrationRecoverySource(Protocol):
    def __call__(
        self,
        manifest_path: Path,
        *,
        config_path: Path,
        target_path: Path,
        expected_source_sha256: str,
        expected_plan_fingerprint: str,
        transform_version: str,
    ) -> Path: ...


@dataclass(frozen=True)
class CommandResponse:
    """One successful CLI result plus its observable rendering policy."""

    payload: object
    exit_code: int = 0
    ensure_ascii: bool = True


@dataclass(frozen=True)
class V4Bindings:
    """Lazily imported v4 command boundary used by the legacy executable."""

    detect_store: Callable[[Path], str]
    should_handle: V4ShouldHandle
    handle_command: V4HandleCommand
    initialize: V4Initialize
    upgrade: V4Upgrade
    migration_recovery_source: V4MigrationRecoverySource
    upgrade_transform_version: str
    capture: CaptureHandler
    sanitize_review_safe: ReviewSanitizer
    project: V4Project
    redact: V4Mutation
    resolve_loop: V4Mutation
    open_store: V4OpenStore


@dataclass(frozen=True)
class LegacyDispatchDependencies(_DispatchCallbackAdapters):
    """Call-time graph policy and services required by command dispatch."""

    argument_error: type[Exception]
    collections: Sequence[str]
    default_root_name: str
    graph_schema_version: str
    legacy_graph_schema_versions: Collection[str]
    migration_transform_version: str
    atomic_write: AtomicWrite
    capture_packet: CapturePacket
    change_feed_since: ChangeFeedSince
    committed_watermark: Callable[[JSONMap], int]
    configure: Configure
    ensure_latest_recall_index: Callable[[Path, float], JSONMap]
    ensure_projection_target: Callable[[Path, str], Path]
    ensure_recall_index: EnsureRecallIndex
    get_graph_record: Callable[[JSONMap, str, str], JSONMap]
    get_indexed_graph_record: GetIndexedGraphRecord
    index_by_id: Callable[[list[JSONMap]], dict[str, JSONMap]]
    index_status: Callable[[JSONMap, Path, str], JSONMap]
    indexed_recall_candidates: IndexedRecallCandidates
    load_config: Callable[[Path], JSONMap]
    load_graph_with_digest: Callable[[Path], tuple[JSONMap, str, str]]
    load_v4_bindings: Callable[[], V4Bindings]
    maybe_project_latest: MaybeProjectLatest
    migrate_graph_v2_to_v3: Callable[[JSONMap], JSONMap]
    migration_backup_path: Callable[[Path, str, int, str], Path]
    mutation_journal: InitVar[MutationJournalCallback] = field()
    normalize_space: Callable[[object], str]
    producer_from_args: InitVar[ProducerFromArgs] = field()
    project_latest_from_store: ProjectLatestFromStore
    read_packet: Callable[[str], JSONMap]
    recall_graph: RecallGraph
    recall_index_path: Callable[[Path], Path]
    redact_record: Callable[[JSONMap, str], JSONMap]
    require_current_graph_schema: Callable[[str], None]
    resolve_loop: Callable[[JSONMap, str, str], JSONMap]
    resolve_projection: Callable[[argparse.Namespace, JSONMap], tuple[Path, str]]
    resolve_store: Callable[[argparse.Namespace, JSONMap], Path]
    save_graph: Callable[[Path, JSONMap], str]
    scan_query_candidate_ids: Callable[[JSONMap, str, str], set[str]]
    secure_state_path: Callable[[str | Path, str], Path]
    stable_hash: Callable[[object, int], str]
    state_privacy_warnings: Callable[[Path, Path, JSONMap], list[str]]
    store_lock: StoreLock
    unique_strings: Callable[[Iterable[object]], list[str]]
    validate_graph: Callable[[JSONMap], ValidationResult]
    _mutation_journal: MutationJournalCallback = field(init=False, repr=False)
    _producer_from_args: ProducerFromArgs = field(init=False, repr=False)

    def __post_init__(
        self,
        mutation_journal: MutationJournalCallback,
        producer_from_args: ProducerFromArgs,
    ) -> None:
        object.__setattr__(self, "_mutation_journal", mutation_journal)
        object.__setattr__(self, "_producer_from_args", producer_from_args)
