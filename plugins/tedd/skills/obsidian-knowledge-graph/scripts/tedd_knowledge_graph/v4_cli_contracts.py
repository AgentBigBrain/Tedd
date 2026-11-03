"""Typed contracts shared by the v4 CLI command-family adapters."""

from __future__ import annotations

import argparse
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence, TypeAlias

from .v4_application_contracts import (
    BrainStatusRequest,
    GraphReadResult,
    LoopsReviewRequest,
    PullChangesRequest,
    RecallContextRequest,
)

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

# File transport is bounded separately from the 16 KiB command-line token limit.
MAX_CAPTURE_PLAN_BYTES = 2 * 1024 * 1024


class V4CLIError(ValueError):
    """A caller-correctable command-boundary error."""


@dataclass(frozen=True)
class ProducerEnvelope:
    producer_agent_id: str
    task_id: str
    parent_task_id: str | None
    producer_run_id: str
    discovery_kind: str
    based_on_watermark: int

    def as_dict(self) -> dict[str, object]:
        """Return the stable storage-facing producer representation."""

        return {
            "producer_agent_id": self.producer_agent_id,
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "producer_run_id": self.producer_run_id,
            "discovery_kind": self.discovery_kind,
            "based_on_watermark": self.based_on_watermark,
        }


@dataclass(frozen=True)
class CaptureRequest:
    """Validated contract passed to a native, transactional capture adapter.

    For file inputs, the callback must parse ``input_bytes``: the immutable,
    bounded snapshot validated by the command boundary. ``input_path`` remains
    provenance for backward-compatible programmatic requests; stdin is still
    consumed by the callback. The callback normalizes with canonical capture
    policy and calls ``store.apply_changes`` once. Dry runs must not mutate
    canonical or operational state.
    """

    input_path: str
    dry_run: bool
    consumer_id: str
    envelope: ProducerEnvelope
    authority_ceiling: str
    allow_replace: bool
    allow_redirect: bool
    expected_fingerprint: str | None
    expected_global_watermark: int | None
    plan_token: str | None
    expected_revisions: Mapping[str, int]
    expected_absent: tuple[str, ...]
    input_bytes: bytes | None = None
    source_config_path: Path | None = None
    source_subscription_id: str | None = None
    source_subject_ids: tuple[str, ...] = ()
    handoff_prompt_id: str | None = None


@dataclass(frozen=True)
class CaptureInputSnapshot:
    path: str
    content: bytes | None


@dataclass(frozen=True)
class UpgradeRequest:
    """Digest-gated v3-to-v4 planning request.

    The upgrade callback owns reading the v3 source and calling
    ``import_v3_graph``. A dry run must be read-only. Apply verifies both values
    returned by dry-run, publishes a durable recovery manifest, activates the
    reviewed configuration, and replaces the v3 source with a recovery tombstone.
    """

    source_path: Path
    target_path: Path
    dry_run: bool
    expected_source_sha256: str | None
    expected_plan_fingerprint: str | None
    backup_output: Path | None
    envelope: ProducerEnvelope | None


@dataclass(frozen=True)
class InitializeRequest:
    """Explicit, seed-free initialization of one configured v4 store."""

    target_path: Path


class CaptureHandler(Protocol):
    def __call__(self, store: object, request: CaptureRequest) -> Mapping[str, object]: ...


class UpgradeHandler(Protocol):
    def __call__(self, request: UpgradeRequest) -> Mapping[str, object]: ...


class InitializeHandler(Protocol):
    def __call__(self, request: InitializeRequest) -> Mapping[str, object]: ...


class ReviewSanitizer(Protocol):
    """Sanitize one complete JSON-shaped result for review-safe disclosure."""

    def __call__(self, value: object) -> object: ...


class StorageModule(Protocol):
    """The lazy-loaded portion of ``knowledge_graph_v4`` used by the CLI."""

    def inspect_store_path(self, path: Path) -> object: ...

    def detect_store(self, path: Path) -> str: ...

    def open_store(
        self,
        path: Path,
        *,
        readonly: bool,
        plan_hmac_key: bytes | str | None,
    ) -> AbstractContextManager[object]: ...


class BackendInvoker(Protocol):
    def __call__(self, store: object, method_name: str, /, **kwargs: object) -> object: ...


class StateReader(Protocol):
    def __call__(self, store: object) -> object: ...


class BoundedText(Protocol):
    def __call__(self, value: object, label: str, maximum: int) -> str: ...


class OpaqueId(Protocol):
    def __call__(self, value: object, label: str, *, record: bool = False) -> str: ...


class OptionalOpaqueId(Protocol):
    def __call__(self, value: object, label: str, *, record: bool = False) -> str | None: ...


class NonnegativeInt(Protocol):
    def __call__(self, value: object, label: str) -> int: ...


class PositiveInt(Protocol):
    def __call__(self, value: object, label: str, maximum: int | None = None) -> int: ...


class UniqueIds(Protocol):
    def __call__(
        self,
        values: Sequence[object] | None,
        label: str,
        *,
        record: bool = True,
    ) -> tuple[str, ...]: ...


class ExpectedRevisions(Protocol):
    def __call__(self, values: Sequence[str] | None) -> dict[str, int]: ...


class ProducerEnvelopeBuilder(Protocol):
    def __call__(
        self, args: argparse.Namespace, *, require_discovery: bool
    ) -> ProducerEnvelope: ...


class CaptureInputSnapshotter(Protocol):
    def __call__(self, value: str) -> CaptureInputSnapshot: ...


class SafeOutputPath(Protocol):
    def __call__(
        self,
        value: object,
        label: str,
        *,
        force: bool = False,
        replacement_supported: bool = True,
    ) -> Path: ...


class JsonWriter(Protocol):
    def __call__(self, path: Path, payload: object, *, overwrite: bool = True) -> int: ...


class ReviewSanitization(Protocol):
    def __call__(self, value: object, sanitizer: ReviewSanitizer | None) -> object: ...


class MaintenanceFingerprint(Protocol):
    def __call__(self, value: Mapping[str, object]) -> str: ...


class ReindexStore(Protocol):
    def __call__(self, store: object) -> Mapping[str, object]: ...


class GraphReadOperations(Protocol):
    """The application-service operations used by the CLI read transports."""

    def brain_status(self, request: BrainStatusRequest) -> GraphReadResult: ...

    def recall_context(self, request: RecallContextRequest) -> GraphReadResult: ...

    def loops_review(self, request: LoopsReviewRequest) -> GraphReadResult: ...

    def pull_changes(self, request: PullChangesRequest) -> GraphReadResult: ...


@dataclass(frozen=True)
class CommandServices:
    """Call-time dependencies that preserve the flat CLI's monkeypatch seams."""

    inspect_store: Callable[[Path], object]
    invoke: BackendInvoker
    state: StateReader
    bounded_text: BoundedText
    opaque_id: OpaqueId
    optional_opaque_id: OptionalOpaqueId
    nonnegative_int: NonnegativeInt
    positive_int: PositiveInt
    unique_ids: UniqueIds
    expected_revisions: ExpectedRevisions
    producer_envelope: ProducerEnvelopeBuilder
    snapshot_capture_input: CaptureInputSnapshotter
    snapshot_plan_input: CaptureInputSnapshotter
    write_plan_file: Callable[[str, Mapping[str, object]], None]
    read_handoff_file: Callable[[Path], object]
    write_handoff_file: Callable[[Path, Mapping[str, object]], None]
    safe_output_path: SafeOutputPath
    write_json_atomic: JsonWriter
    apply_review_sanitizer: ReviewSanitization
    maintenance_fingerprint: MaintenanceFingerprint
    reindex_store: ReindexStore
