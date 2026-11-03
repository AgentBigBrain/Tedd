"""Initialization and reviewed lifecycle-mutation services for v4.

The flat integration module supplies legacy and storage policy at call time.
Keeping those dependencies explicit makes this module acyclic and lets the
compatibility facade preserve its historical monkeypatch seams.
"""

from __future__ import annotations

import argparse
import copy
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict, cast

from tedd_knowledge_graph import v4_projection
from tedd_knowledge_graph.v4_cli_contracts import InitializeRequest


class ProducerArguments(TypedDict):
    producer_agent_id: str
    task_id: str
    parent_task_id: str | None
    run_id: str
    based_on_watermark: int


class PriorRecord(TypedDict):
    collection: str
    revision: int
    payload: dict[str, object]


class LifecycleStore(Protocol):
    connection: sqlite3.Connection
    readonly: bool

    def __enter__(self) -> LifecycleStore: ...

    def __exit__(self, *_: object) -> None: ...

    def close(self) -> None: ...

    @property
    def watermark(self) -> int: ...

    def validate(self, *, full: bool = False) -> dict[str, object]: ...

    def state(self) -> Mapping[str, object]: ...

    def checkpoint(self, mode: str = "PASSIVE") -> Mapping[str, object]: ...

    def get_record(self, record_id: str, revision: int | None = None) -> Mapping[str, object]: ...

    def records_referencing(
        self,
        target_ids: Sequence[str],
        *,
        ref_kinds: Sequence[str] | None = None,
        limit: int = 500,
    ) -> Sequence[Mapping[str, object]]: ...

    def apply_changes(
        self,
        changes: Sequence[object],
        *,
        fingerprint: str,
        action: str,
        discovery_kind: str,
        producer_agent_id: str,
        task_id: str,
        parent_task_id: str | None,
        run_id: str,
        based_on_watermark: int,
        expected_revisions: Mapping[str, object],
        idempotency_key: str,
        redaction_count: int = 0,
    ) -> dict[str, object]: ...


class StoreLock(Protocol):
    def __call__(self, path: Path, timeout_seconds: float) -> AbstractContextManager[None]: ...


class OpenStore(Protocol):
    def __call__(
        self,
        path: str | Path,
        *,
        readonly: bool = False,
        plan_hmac_key: bytes | str | None = None,
    ) -> LifecycleStore: ...


class CreateStore(Protocol):
    def __call__(
        self, path: str | Path, *, plan_hmac_key: bytes | str | None = None
    ) -> LifecycleStore: ...


class RecordChangeFactory(Protocol):
    def __call__(
        self,
        record_id: str,
        collection: str,
        payload: Mapping[str, object],
        change_kind: str = "upsert",
        expected_revision: object = ...,
        refs: Sequence[Mapping[str, object]] = (),
        semantic_keys: Sequence[object] = (),
        search_text: str | None = None,
    ) -> object: ...


@dataclass(frozen=True)
class InitializeDependencies:
    config_schema_version: str
    store_lock: StoreLock
    load_config: Callable[[Path], object]
    normalize_space: Callable[[object], str]
    secure_state_path: Callable[[str, str], Path]
    detect_store: Callable[[str | Path], str]
    create_store: CreateStore
    open_store: OpenStore
    atomic_write: Callable[[Path, str, int], None]
    utc_now: Callable[[], str]


@dataclass(frozen=True)
class MutationDependencies:
    collections: tuple[str, ...]
    producer_from_args: Callable[[argparse.Namespace], object]
    normalize_space: Callable[[object], str]
    normalize_key: Callable[[object], str]
    blank_graph: Callable[[], object]
    resolve_loop: Callable[[dict[str, object], str, str], object]
    redact_record: Callable[[dict[str, object], str], object]
    stable_hash: Callable[[object, int], str]
    canonical_json: Callable[[object], str]
    open_store: OpenStore
    record_change: RecordChangeFactory
    projection_dependencies: v4_projection.ProjectionDependencies


def _object_dict(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise ValueError(f"{label} keys must be strings")
    return {str(key): child for key, child in raw.items()}


def _integer(value: object, *, label: str) -> int:
    if not isinstance(value, (str, bytes, bytearray, int, float)):
        raise ValueError(f"{label} must be numeric")
    return int(value)


def _empty_store_operational_counts(store: LifecycleStore) -> dict[str, int]:
    tables = (
        "idempotency_keys",
        "identity_schemes",
        "consumers",
        "subscriptions",
        "audit_checkpoints",
        "schema_migrations",
        "migration_metadata",
    )
    return {
        table: int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def initialize_v4(
    request: InitializeRequest,
    *,
    config_path: Path,
    lock_timeout: float,
    dependencies: InitializeDependencies,
) -> dict[str, object]:
    """Create and bind one valid, empty v4 store without seeding knowledge."""

    target = request.target_path
    created = False
    store: LifecycleStore | None = None
    cleanup = (
        target,
        Path(f"{target}-wal"),
        Path(f"{target}-shm"),
        target.with_name(f"{target.name}.plan.key"),
    )
    preexisting_artifacts = {candidate: candidate.exists() for candidate in cleanup}
    with dependencies.store_lock(config_path, lock_timeout):
        current_config = _object_dict(
            dependencies.load_config(config_path),
            label="Knowledge graph configuration",
        )
        configured_raw = dependencies.normalize_space(current_config.get("store_path"))
        if configured_raw:
            configured = dependencies.secure_state_path(
                configured_raw, "Configured canonical store"
            )
            if configured != target:
                raise ValueError("Configuration no longer points at the requested fresh v4 target")

        with dependencies.store_lock(target, lock_timeout):
            detected = dependencies.detect_store(target)
            if detected == "json_v3":
                raise ValueError(
                    "init-v4 is only for a fresh installation; use upgrade-v4 for a v3 JSON graph"
                )
            if detected not in {"missing", "sqlite_v4"}:
                raise ValueError(f"Refusing to initialize over existing {detected} state: {target}")
            try:
                if detected == "missing":
                    created = True
                    store = dependencies.create_store(target)
                    status = "initialized_empty_store"
                else:
                    store = dependencies.open_store(target, readonly=True)
                    status = "already_initialized"

                validation = store.validate(full=True)
                operational_counts = _empty_store_operational_counts(store)
                canonical_counts = _object_dict(
                    validation.get("counts", {}), label="Validation counts"
                )
                empty = (
                    validation.get("valid") is True
                    and _integer(canonical_counts.get("records", -1), label="record count") == 0
                    and _integer(canonical_counts.get("revisions", -1), label="revision count") == 0
                    and _integer(canonical_counts.get("mutations", -1), label="mutation count") == 0
                    and all(count == 0 for count in operational_counts.values())
                )
                if not validation.get("valid"):
                    errors_value = validation.get("errors", [])
                    if not isinstance(errors_value, list):
                        raise ValueError("Fresh v4 validation errors must be a list")
                    errors = [str(item) for item in cast(list[object], errors_value)]
                    raise ValueError("Fresh v4 store failed full validation: " + "; ".join(errors))
                if not configured_raw and not empty:
                    raise ValueError(
                        "Refusing to adopt an unbound v4 store that contains canonical or coordination state"
                    )
                if created and not empty:
                    raise ValueError("Fresh v4 initialization unexpectedly created graph data")

                state = dict(store.state())
                if not store.readonly:
                    store.checkpoint("TRUNCATE")
                store.close()
                store = None

                plan_key_path = target.with_name(f"{target.name}.plan.key")
                if not plan_key_path.exists():
                    with dependencies.open_store(target, readonly=False) as writable_store:
                        writable_store.checkpoint("TRUNCATE")

                activated_config = copy.deepcopy(current_config)
                activated_config.update(
                    {
                        "schema_version": dependencies.config_schema_version,
                        "store_path": str(target),
                        "canonical_store_format": "sqlite_v4_wal",
                        "updated_at": dependencies.utc_now(),
                    }
                )
                config_changed = any(
                    current_config.get(key) != activated_config.get(key)
                    for key in (
                        "schema_version",
                        "store_path",
                        "canonical_store_format",
                    )
                )
                if config_changed:
                    dependencies.atomic_write(
                        config_path,
                        json.dumps(activated_config, indent=2, sort_keys=True) + "\n",
                        0o600,
                    )
                if detected == "sqlite_v4" and not configured_raw:
                    status = "recovered_empty_store"
                return {
                    "status": status,
                    "target": str(target),
                    "config": str(config_path),
                    "config_changed": config_changed,
                    "seeded_records": 0,
                    "plan_key_created": (
                        not preexisting_artifacts[plan_key_path] and plan_key_path.exists()
                    ),
                    "operational_counts": operational_counts,
                    "target_state": state,
                    "target_validation": validation,
                }
            except Exception:
                if store is not None:
                    store.close()
                    store = None
                if created:
                    for candidate in cleanup:
                        if not preexisting_artifacts[candidate] and candidate.exists():
                            candidate.unlink()
                raise


def producer_arguments(
    args: argparse.Namespace,
    store: LifecycleStore,
    *,
    dependencies: MutationDependencies,
) -> ProducerArguments:
    producer = _object_dict(dependencies.producer_from_args(args), label="Mutation producer")
    agent_id = dependencies.normalize_space(producer.get("agent_id"))
    task_id = dependencies.normalize_space(producer.get("task_id"))
    run_id = dependencies.normalize_space(producer.get("run_id"))
    if not agent_id or not task_id or not run_id:
        raise ValueError("v4 maintenance mutations require producer agent, task, and run IDs")
    parent_task_id = dependencies.normalize_space(producer.get("parent_task_id")) or None
    based_on = getattr(args, "based_on_watermark", None)
    if based_on is None:
        raise ValueError("v4 maintenance mutations require --based-on-watermark")
    based_on_watermark = _integer(based_on, label="based-on watermark")
    if not 0 <= based_on_watermark <= store.watermark:
        raise ValueError("based-on watermark is outside committed v4 state")
    return {
        "producer_agent_id": agent_id,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "run_id": run_id,
        "based_on_watermark": based_on_watermark,
    }


def resolve_loop_v4(
    store_path: Path,
    args: argparse.Namespace,
    *,
    dependencies: MutationDependencies,
) -> dict[str, object]:
    with dependencies.open_store(store_path, readonly=False) as store:
        producer = producer_arguments(args, store, dependencies=dependencies)
        record_id = dependencies.normalize_space(getattr(args, "id", None))
        resolution = dependencies.normalize_space(getattr(args, "resolution", None))
        if not record_id or not resolution:
            raise ValueError("Loop ID and resolution are required")
        item = store.get_record(record_id)
        if item["collection"] != "open_loops":
            raise ValueError(f"Record is not an open loop: {record_id}")
        payload = _object_dict(copy.deepcopy(item["payload"]), label="Open-loop payload")
        result = _object_dict(
            dependencies.resolve_loop(
                {
                    **_object_dict(dependencies.blank_graph(), label="Legacy graph"),
                    "open_loops": [payload],
                },
                record_id,
                resolution,
            ),
            label="Loop-resolution result",
        )
        if not result.get("changed"):
            return {
                "status": "unchanged",
                "result": result,
                "watermark": store.watermark,
            }
        fingerprint = dependencies.stable_hash(
            {
                "action": "resolve_loop",
                "record_id": record_id,
                "revision": item["revision"],
                "resolution": resolution,
            },
            64,
        )
        change = dependencies.record_change(
            record_id,
            "open_loops",
            payload,
            change_kind="resolve_loop",
            expected_revision=int(cast(int, item["revision"])),
        )
        applied = store.apply_changes(
            [change],
            fingerprint=fingerprint,
            action="resolve_loop",
            discovery_kind=(
                dependencies.normalize_key(getattr(args, "discovery_kind", None))
                or "loop_resolution"
            ),
            producer_agent_id=producer["producer_agent_id"],
            task_id=producer["task_id"],
            parent_task_id=producer["parent_task_id"],
            run_id=producer["run_id"],
            based_on_watermark=producer["based_on_watermark"],
            expected_revisions={record_id: item["revision"]},
            idempotency_key=f"resolve_loop:{record_id}:{fingerprint}",
        )
        return {"status": "updated", "result": result, **applied}


def redact_v4(
    store_path: Path,
    args: argparse.Namespace,
    *,
    dependencies: MutationDependencies,
) -> dict[str, object]:
    with dependencies.open_store(store_path, readonly=False) as store:
        producer = producer_arguments(args, store, dependencies=dependencies)
        record_id = dependencies.normalize_space(getattr(args, "id", None))
        if not record_id:
            raise ValueError("Redaction record ID is required")
        store.get_record(record_id)
        selected = {record_id}
        related = store.records_referencing([record_id], limit=2_000)
        selected.update(str(item["record_id"]) for item in related)
        projection_store = cast(v4_projection.ProjectionStore, store)
        selected = v4_projection.forward_closure(projection_store, sorted(selected))
        if len(selected) > 2_000:
            raise ValueError("Redaction closure exceeds the reviewed maintenance bound")
        graph = v4_projection.records_graph(
            projection_store,
            selected,
            dependencies=dependencies.projection_dependencies,
        )
        before: dict[str, PriorRecord] = {}
        for collection in dependencies.collections:
            for payload in v4_projection.graph_records(graph, collection):
                payload_id = dependencies.normalize_space(payload.get("id"))
                if not payload_id:
                    raise ValueError(f"Graph {collection} record is missing an ID")
                record = store.get_record(payload_id)
                before[payload_id] = {
                    "collection": collection,
                    "revision": int(cast(int, record["revision"])),
                    "payload": copy.deepcopy(payload),
                }
        # An archived source is an independently retained original, not part of
        # one claim's text. A claim-only redaction blocks that claim's historical
        # evidence path, but must not erase shared originals or source records.
        # Whole-source erasure is the separate, explicit archive-forget operation.
        retained_sources = {
            key
            for key, prior in before.items()
            if key != record_id
            and prior["collection"] == "sources"
            and str(prior["payload"].get("uri", "")).startswith("tedd-source:")
        }
        result = _object_dict(
            dependencies.redact_record(graph, record_id), label="Redaction result"
        )
        if retained_sources:
            for source in v4_projection.graph_records(graph, "sources"):
                source_id = str(source.get("id", ""))
                if source_id in retained_sources:
                    source.clear()
                    source.update(copy.deepcopy(before[source_id]["payload"]))
            cascaded = result.get("cascaded_ids", [])
            if isinstance(cascaded, list):
                result["cascaded_ids"] = [item for item in cascaded if item not in retained_sources]
            result["source_retention"] = {
                "status": "shared_source_retained",
                "retained_source_records": len(retained_sources),
                "redacted_lineage_readable": False,
                "archive_erased": False,
                "whole_source_forget_requires_explicit_request": True,
            }
        if not result.get("changed"):
            return {
                "status": "unchanged",
                "result": result,
                "watermark": store.watermark,
            }
        changes: list[object] = []
        changed_record_ids: list[str] = []
        for collection in dependencies.collections:
            for payload in v4_projection.graph_records(graph, collection):
                payload_id = dependencies.normalize_space(payload.get("id"))
                if not payload_id:
                    raise ValueError(f"Graph {collection} record is missing an ID")
                prior = before[payload_id]
                if dependencies.canonical_json(payload) == dependencies.canonical_json(
                    prior["payload"]
                ):
                    continue
                changes.append(
                    dependencies.record_change(
                        payload_id,
                        collection,
                        payload,
                        change_kind="redact",
                        expected_revision=prior["revision"],
                    )
                )
                changed_record_ids.append(payload_id)
        expected_revisions = {
            changed_id: before[changed_id]["revision"] for changed_id in changed_record_ids
        }
        fingerprint = dependencies.stable_hash(
            {
                "action": "redact",
                "record_id": record_id,
                "revisions": expected_revisions,
            },
            64,
        )
        applied = store.apply_changes(
            changes,
            fingerprint=fingerprint,
            action="redact",
            discovery_kind=(
                dependencies.normalize_key(getattr(args, "discovery_kind", None)) or "redaction"
            ),
            producer_agent_id=producer["producer_agent_id"],
            task_id=producer["task_id"],
            parent_task_id=producer["parent_task_id"],
            run_id=producer["run_id"],
            based_on_watermark=producer["based_on_watermark"],
            expected_revisions=expected_revisions,
            idempotency_key=f"redact:{record_id}:{fingerprint}",
            redaction_count=len(changes),
        )
        return {"status": "updated", "result": result, **applied}


__all__ = [
    "CreateStore",
    "InitializeDependencies",
    "LifecycleStore",
    "MutationDependencies",
    "OpenStore",
    "RecordChangeFactory",
    "StoreLock",
    "initialize_v4",
    "producer_arguments",
    "redact_v4",
    "resolve_loop_v4",
]
