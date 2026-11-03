#!/usr/bin/env python3
"""Compatibility facade for bounded SQLite-v4 semantic capture."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Protocol, cast

from tedd_knowledge_graph import v4_capture as _v4_capture
from tedd_knowledge_graph.source_contracts import SourceArchiveError
from tedd_knowledge_graph.source_host import SourceHost
from tedd_knowledge_graph.v4_cli_contracts import CaptureRequest

MAX_CAPTURE_SLICE_RECORDS = _v4_capture.MAX_CAPTURE_SLICE_RECORDS
MAX_CAPTURE_RELATED_RECORDS = _v4_capture.MAX_CAPTURE_RELATED_RECORDS


class _LegacyModule(_v4_capture.LegacyCapturePolicy, Protocol):
    pass


class _StorageModule(_v4_capture.StorageCapturePolicy, Protocol):
    KnowledgeGraphV4: type[object]


def _legacy_module() -> _LegacyModule:
    import knowledge_graph

    # Mypy does not structurally narrow modules with mutable class/constants
    # (notably exception classes and the fixed COLLECTIONS tuple) to Protocols.
    # Boundary tests lock every callback's call shape plus critical returns.
    return cast(_LegacyModule, knowledge_graph)


def _storage_module() -> _StorageModule:
    import knowledge_graph_v4

    return cast(_StorageModule, knowledge_graph_v4)


def __getattr__(name: str) -> object:
    """Retain historical module attributes without eager facade imports."""

    if name == "legacy":
        return _legacy_module()
    if name == "v4":
        return _storage_module()
    raise AttributeError(name)


def execute_capture(
    store: object,
    request: CaptureRequest,
    *,
    source_host: SourceHost | None = None,
) -> dict[str, object]:
    """Execute the CLI capture contract through request-local dependencies."""

    storage = _storage_module()
    expected_config: bytes | None = None

    def check_source(
        store: object,
        packet: Mapping[str, object],
        request: CaptureRequest,
        *,
        changes: Sequence[Mapping[str, object]] = (),
    ) -> None:
        from tedd_sources import validate_capture_binding

        nonlocal expected_config
        if source_host is None or source_host.provider_check is None:
            raise SourceArchiveError("capability_unavailable")
        expected_config = validate_capture_binding(
            store,
            packet,
            request,
            changes=changes,
            provider_check=source_host.provider_check,
            check_retention=source_host.check_retention,
        )

    def check_source_local(
        store: object,
        packet: Mapping[str, object],
        request: CaptureRequest,
        *,
        changes: Sequence[Mapping[str, object]] = (),
    ) -> None:
        from tedd_sources import validate_capture_binding

        if expected_config is None:
            raise SourceArchiveError("scope_denied")
        validate_capture_binding(
            store,
            packet,
            request,
            changes=changes,
            expected_config=expected_config,
            local_only=True,
            check_retention=source_host.check_retention if source_host is not None else None,
        )

    @contextmanager
    def reserve_source_configuration() -> Iterator[None]:
        from tedd_knowledge_graph.source_authority import authorized_scope
        from tedd_knowledge_graph.v4_application_contracts import (
            GraphReadScope,
            GraphReadSession,
            ReadScopeKind,
        )
        from tedd_sources import graph, load_config, v4

        if request.source_config_path is None:
            raise SourceArchiveError("scope_denied")
        reservation: AbstractContextManager[None] = nullcontext()
        if source_host is not None and source_host.collecting is not None:
            if not isinstance(store, v4.KnowledgeGraphV4):
                raise SourceArchiveError("scope_denied")
            session = GraphReadSession(
                request.envelope.producer_agent_id,
                request.envelope.task_id,
                request.envelope.producer_run_id,
                request.consumer_id,
            )
            with v4.open_store(store.path, readonly=True) as reader:
                scope = authorized_scope(
                    reader,
                    session,
                    GraphReadScope(ReadScopeKind.SUBSCRIPTION, request.source_subscription_id),
                    request.source_subject_ids,
                )
            reservation = source_host.collecting(session, scope)
        with reservation, graph.store_lock(request.source_config_path, 5):
            if source_host is not None and (
                expected_config is None
                or load_config(request.source_config_path)[0] != expected_config
            ):
                raise SourceArchiveError("scope_denied")
            yield

    return _v4_capture.execute_capture(
        store,
        request,
        dependencies=_v4_capture.CaptureDependencies(
            legacy=_legacy_module(),
            storage=storage,
            store_type=storage.KnowledgeGraphV4,
            validate_source_binding=check_source
            if source_host is not None
            else _validate_source_binding,
            validate_source_binding_local=check_source_local if source_host is not None else None,
            source_commit_reservation=reserve_source_configuration,
        ),
    )


def _validate_source_binding(
    store: object,
    packet: Mapping[str, object],
    request: CaptureRequest,
    *,
    changes: Sequence[Mapping[str, object]] = (),
) -> None:
    from tedd_sources import validate_capture_binding

    validate_capture_binding(store, packet, request, changes=changes)


__all__ = ["execute_capture"]
