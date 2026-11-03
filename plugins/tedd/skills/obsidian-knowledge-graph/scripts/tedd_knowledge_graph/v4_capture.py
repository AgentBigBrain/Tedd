#!/usr/bin/env python3
"""Packet-sized semantic capture adapter for the SQLite v4 graph.

The mature semantic normalizer remains in ``knowledge_graph.py``.  This adapter
loads only the indexed records that can affect one packet, runs that normalizer
against a closed mini-graph, and commits the resulting record diff with
per-record revisions.  It never exports or validates the full SQLite store in a
normal write path.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol, TypedDict, cast

from tedd_knowledge_graph.handoff_receipts import validate_prompt_id
from tedd_knowledge_graph.preferences import is_preference_scope, validate_preference_record
from tedd_knowledge_graph.v4_cli_contracts import CaptureRequest

MAX_CAPTURE_SLICE_RECORDS = 2_000
MAX_CAPTURE_RELATED_RECORDS = 2_000


class CaptureStore(Protocol):
    connection: sqlite3.Connection

    @property
    def watermark(self) -> int: ...

    def state(self) -> Mapping[str, object]: ...

    def get_records(self, record_ids: Iterable[str]) -> Sequence[Mapping[str, object]]: ...

    def find_entities(
        self,
        *,
        identity_keys: Sequence[Mapping[str, object]] = (),
        namespace: str | None = None,
        entity_type: str | None = None,
        names: Sequence[str] = (),
        limit: int = 100,
    ) -> Sequence[Mapping[str, object]]: ...

    def find_claim_surfaces(
        self,
        *,
        surface_id: str | None = None,
        family: str | None = None,
        key: str | None = None,
        subject_signature: str | None = None,
        subject_ids: Sequence[str] | None = None,
        limit: int = 100,
    ) -> Sequence[Mapping[str, object]]: ...

    def records_referencing(
        self,
        target_ids: Iterable[str],
        *,
        ref_kinds: Iterable[str] | None = None,
        limit: int = 500,
    ) -> Sequence[Mapping[str, object]]: ...

    def semantic_key_owner(self, collection: str, key_kind: str, key: str) -> str | None: ...

    def create_plan_token(
        self,
        read_record_ids: Iterable[str],
        fingerprint: str,
        *,
        constraint_expectations: Sequence[Mapping[str, object]] = (),
    ) -> str: ...

    def apply_changes(
        self,
        changes: Sequence[Mapping[str, object]],
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
        plan_token: str,
        idempotency_key: str,
        source_ref: str | None,
        capture_policy: Mapping[str, object] | None,
        details: Mapping[str, object],
        redaction_count: int,
        preflight: Callable[[], None] | None = None,
    ) -> dict[str, object]: ...


class LegacyCapturePolicy(Protocol):
    COLLECTIONS: tuple[str, ...]
    MAX_CAPTURE_BYTES: int
    SAFE_RECORD_ID: re.Pattern[str]
    _CAPTURE_FINGERPRINT_ONLY: object

    def read_packet(self, path: str) -> dict[str, object]: ...

    def packet_records(
        self, packet: dict[str, object], collection: str
    ) -> list[dict[str, object]]: ...

    def explicit_record_id(self, value: object) -> str: ...

    def normalize_space(self, value: object) -> str: ...

    def normalize_key(self, value: object) -> str: ...

    def blank_graph(self) -> dict[str, object]: ...

    def parse_time(self, value: object, fallback: str | None = None) -> str: ...

    def utc_now(self) -> str: ...

    def source_tier(self, value: object, fallback: str = "assistant_inference") -> str: ...

    def upsert_entity(
        self,
        graph: dict[str, object],
        raw: dict[str, object],
        source_id: str,
        observed_at: str,
        incoming_tier: str,
    ) -> tuple[str, bool]: ...

    def record_entity_refs(
        self,
        raw: dict[str, object],
        local_refs: dict[str, str],
        graph: dict[str, object],
    ) -> list[str]: ...

    def stable_id(self, prefix: str, value: object) -> str: ...

    def resolve_entity_ref(
        self,
        ref: object,
        local_refs: dict[str, str],
        graph: dict[str, object],
    ) -> str: ...

    def scrub_value(
        self,
        value: object,
        *,
        redact_paths: bool = True,
        depth: int = 0,
    ) -> tuple[object, int]: ...

    def record_text(
        self,
        kind: str,
        record: dict[str, object],
        entities: dict[str, dict[str, object]],
    ) -> str: ...

    def index_by_id(self, records: list[dict[str, object]]) -> dict[str, dict[str, object]]: ...

    def unique_strings(self, values: Iterable[object]) -> list[str]: ...

    def capture_packet(
        self,
        graph: dict[str, object],
        raw_packet: dict[str, object],
        authority_ceiling: str = "assistant_inference",
        allow_replace: bool = False,
        allow_redirect: bool = False,
        producer: dict[str, object] | None = None,
        based_on_watermark: int | None = None,
        discovery_kind: str | None = None,
    ) -> dict[str, object]: ...

    def validate_graph(self, graph: dict[str, object]) -> object: ...


class StorageCapturePolicy(Protocol):
    COLLECTIONS: tuple[str, ...]
    SubscriptionError: type[Exception]
    ConcurrencyConflict: type[Exception]
    PlanTokenError: type[Exception]
    ValidationError: type[Exception]

    def stable_hash(self, value: object) -> str: ...

    def canonical_json(self, value: object) -> str: ...


@dataclass(frozen=True)
class CaptureDependencies:
    legacy: LegacyCapturePolicy
    storage: StorageCapturePolicy
    store_type: type[object]
    validate_source_binding: SourceBindingValidator | None = None
    validate_source_binding_local: SourceBindingValidator | None = None
    source_commit_reservation: Callable[[], AbstractContextManager[None]] | None = None


class SourceBindingValidator(Protocol):
    def __call__(
        self,
        store: object,
        packet: Mapping[str, object],
        request: CaptureRequest,
        *,
        changes: Sequence[Mapping[str, object]] = (),
    ) -> None: ...


_CAPTURE_DEPENDENCIES: ContextVar[CaptureDependencies | None] = ContextVar(
    "v4_capture_dependencies", default=None
)


def _dependencies() -> CaptureDependencies:
    dependencies = _CAPTURE_DEPENDENCIES.get()
    if dependencies is None:
        raise RuntimeError("Capture dependencies are not bound")
    return dependencies


def _legacy() -> LegacyCapturePolicy:
    return _dependencies().legacy


def _storage() -> StorageCapturePolicy:
    return _dependencies().storage


def _validate_source_binding(
    store: object,
    packet: Mapping[str, object],
    request: CaptureRequest,
    *,
    changes: Sequence[Mapping[str, object]] = (),
    local_only: bool = False,
) -> None:
    from .source_contracts import SourceArchiveError

    dependencies = _dependencies()
    validator = (
        dependencies.validate_source_binding_local
        if local_only and dependencies.validate_source_binding_local is not None
        else dependencies.validate_source_binding
    )
    if validator is None:
        raise SourceArchiveError("scope_denied")
    validator(store, packet, request, changes=changes)


class PriorRecord(TypedDict):
    collection: str
    revision: int
    payload_hash: str
    payload: dict[str, object]


class CaptureSource(TypedDict):
    id: str
    observed_at: str
    captured_at: str
    source_tier: str


def _object_dict(value: object, *, label: str) -> dict[str, object]:
    """Narrow a validated JSON/legacy result to a string-keyed object."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    raw = cast(Mapping[object, object], value)
    if not all(isinstance(key, str) for key in raw):
        raise ValueError(f"{label} keys must be strings")
    return {str(key): child for key, child in raw.items()}


def _graph_records(graph: Mapping[str, object], collection: str) -> list[dict[str, object]]:
    """Return one validated mutable legacy collection."""

    value = graph.get(collection, [])
    if not isinstance(value, list):
        raise ValueError(f"Graph collection {collection!r} must be a list")
    records = cast(list[object], value)
    if not all(isinstance(item, dict) for item in records):
        raise ValueError(f"Graph collection {collection!r} must contain objects")
    return cast(list[dict[str, object]], records)


def _packet(path: str, content: bytes | None = None) -> dict[str, object]:
    if content is None:
        return _object_dict(_legacy().read_packet(path), label="Capture packet")
    if len(content) > _legacy().MAX_CAPTURE_BYTES:
        raise ValueError(f"Capture packet exceeds {_legacy().MAX_CAPTURE_BYTES} bytes")
    packet = json.loads(content.decode("utf-8"))
    return _object_dict(packet, label="Capture packet")


def _packet_records(packet: Mapping[str, object]) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for collection in (
        "entities",
        "entity_redirects",
        "relations",
        "observations",
        "claims",
        "events",
        "decisions",
        "open_loops",
        "receipts",
    ):
        value = _legacy().packet_records(dict(packet), collection)
        if not isinstance(value, list):
            raise ValueError(f"Packet collection {collection!r} must be a list")
        result[collection] = [
            _object_dict(item, label=f"Packet {collection} record")
            for item in cast(Sequence[object], value)
        ]
    return result


def _record_ids_from_packet(records: Mapping[str, Sequence[Mapping[str, object]]]) -> set[str]:
    result: set[str] = set()
    for collection in ("entities", "events", "decisions", "open_loops", "receipts"):
        for item in records[collection]:
            record_id = _legacy().explicit_record_id(item.get("id"))
            if record_id:
                result.add(record_id)
    for claim in records["claims"]:
        surface_id = _legacy().explicit_record_id(claim.get("surface_id"))
        if surface_id:
            result.add(surface_id)
    for collection in ("relations", "entity_redirects"):
        for item in records[collection]:
            for field in ("source", "target", "from", "to"):
                value = _legacy().normalize_space(item.get(field))
                if value and _legacy().SAFE_RECORD_ID.fullmatch(value):
                    result.add(value)
    for record_collection in records.values():
        for item in record_collection:
            for field in ("entity_refs", "subject_refs", "owner_refs"):
                values = item.get(field, [])
                if isinstance(values, list):
                    for value in values:
                        text = _legacy().normalize_space(value)
                        if text and _legacy().SAFE_RECORD_ID.fullmatch(text):
                            result.add(text)
    return result


def _coverage_existing_ids(packet: Mapping[str, object]) -> set[str]:
    """Select exact current records declared by a packet coverage ledger."""

    coverage = packet.get("coverage")
    if coverage is None:
        return set()
    if not isinstance(coverage, Mapping):
        raise ValueError("coverage must be an object")
    items = coverage.get("items")
    if not isinstance(items, list):
        raise ValueError("coverage.items must be a list")
    result: set[str] = set()
    for index, raw in enumerate(cast(Sequence[object], items)):
        if not isinstance(raw, Mapping):
            raise ValueError(f"coverage.items[{index}] must be an object")
        if _legacy().normalize_key(raw.get("disposition")) != "existing":
            continue
        record_id = _legacy().explicit_record_id(raw.get("record_id"))
        if not record_id:
            raise ValueError(f"coverage.items[{index}].record_id must be an exact canonical ID")
        result.add(record_id)
    return result


def _reference_names(records: Mapping[str, Sequence[Mapping[str, object]]]) -> set[str]:
    names: set[str] = set()
    for item in records["entities"]:
        name = _legacy().normalize_space(item.get("name"))
        if name:
            names.add(name)
    for collection in records.values():
        for item in collection:
            for field in ("source", "target", "from", "to"):
                value = _legacy().normalize_space(item.get(field))
                if value:
                    names.add(value)
            for field in ("entity_refs", "subject_refs", "owner_refs"):
                values = item.get(field, [])
                if isinstance(values, list):
                    names.update(
                        _legacy().normalize_space(value)
                        for value in values
                        if _legacy().normalize_space(value)
                    )
    return names


def _add_result_ids(target: set[str], results: Iterable[Mapping[str, object]]) -> None:
    for item in results:
        record_id = _legacy().normalize_space(item.get("record_id"))
        if record_id:
            target.add(record_id)


def _entity_candidates(
    store: CaptureStore,
    records: Mapping[str, Sequence[Mapping[str, object]]],
    selected: set[str],
) -> None:
    for raw in records["entities"]:
        namespace = _legacy().normalize_key(raw.get("namespace")) or "global"
        entity_type = _legacy().normalize_key(raw.get("type")) or "thing"
        identity_keys = raw.get("identity_keys", [])
        if not isinstance(identity_keys, list):
            raise ValueError("Entity identity_keys must be a list")
        found = store.find_entities(
            identity_keys=[item for item in identity_keys if isinstance(item, Mapping)],
            namespace=namespace,
            entity_type=entity_type,
            names=[_legacy().normalize_space(raw.get("name"))],
            limit=100,
        )
        _add_result_ids(selected, found)
    # References often omit namespace. Load every exact alias candidate and let
    # the mature resolver reject ambiguity instead of guessing.
    names = sorted(_reference_names(records))
    for start in range(0, len(names), 100):
        found = store.find_entities(names=names[start : start + 100], limit=500)
        _add_result_ids(selected, found)


def _surface_candidates(
    store: CaptureStore,
    records: Mapping[str, Sequence[Mapping[str, object]]],
    selected: set[str],
) -> set[str]:
    surface_ids: set[str] = set()
    for raw in records["claims"]:
        explicit = _legacy().explicit_record_id(raw.get("surface_id"))
        if explicit:
            matches = store.find_claim_surfaces(surface_id=explicit, limit=10)
            _add_result_ids(surface_ids, matches)
        family = _legacy().normalize_key(raw.get("family"))
        key = _legacy().normalize_key(raw.get("key"))
        if family and key:
            matches = store.find_claim_surfaces(family=family, key=key, limit=500)
            _add_result_ids(surface_ids, matches)
    selected.update(surface_ids)
    if surface_ids:
        related = store.records_referencing(
            surface_ids, ref_kinds=["surface"], limit=MAX_CAPTURE_RELATED_RECORDS
        )
        _add_result_ids(selected, related)
    return surface_ids


def _forward_reference_ids(store: CaptureStore, record_ids: Sequence[str]) -> set[str]:
    if not record_ids:
        return set()
    result: set[str] = set()
    for start in range(0, len(record_ids), 400):
        batch = record_ids[start : start + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = store.connection.execute(
            f"""
            SELECT DISTINCT rr.target_record_id
            FROM revision_refs rr
            JOIN records r ON r.record_id=rr.record_id
                          AND r.current_revision=rr.revision
            WHERE rr.record_id IN ({placeholders})
            """,
            batch,
        )
        result.update(str(row[0]) for row in rows)
    return result


def _close_slice(store: CaptureStore, selected: set[str]) -> None:
    """Add forward reference closure and redirect targets with a hard bound."""
    frontier = set(selected)
    while frontier:
        if len(selected) > MAX_CAPTURE_SLICE_RECORDS:
            raise ValueError(
                "Capture scope exceeds the bounded semantic slice; split the packet or run reviewed maintenance"
            )
        targets = _forward_reference_ids(store, sorted(frontier)) - selected
        selected.update(targets)
        frontier = targets


def _slice_graph(
    store: CaptureStore, selected: set[str]
) -> tuple[dict[str, object], dict[str, PriorRecord]]:
    _close_slice(store, selected)
    graph = _object_dict(_legacy().blank_graph(), label="Legacy graph")
    state = store.state()
    watermark_value = state.get("watermark")
    if not isinstance(watermark_value, (str, bytes, bytearray, int, float)):
        raise ValueError("Store watermark must be numeric")
    watermark = int(watermark_value)
    graph["updated_at"] = state["updated_at"]
    graph["next_watermark"] = watermark + 1
    graph["journal_anchor"] = {
        "watermark": watermark,
        "entry_hash": state["journal_head_hash"],
    }
    before: dict[str, PriorRecord] = {}
    for item in store.get_records(sorted(selected)):
        payload = item.get("payload")
        if not isinstance(payload, dict):
            raise ValueError(f"Current payload is unavailable for {item['record_id']}")
        collection_value = item.get("collection")
        record_id_value = item.get("record_id")
        revision_value = item.get("revision")
        payload_hash_value = item.get("payload_hash")
        if not isinstance(collection_value, str) or not isinstance(record_id_value, str):
            raise ValueError("Current record identity must contain strings")
        if not isinstance(revision_value, (str, bytes, bytearray, int, float)):
            raise ValueError("Current record revision must be numeric")
        if not isinstance(payload_hash_value, str):
            raise ValueError("Current record payload hash must be a string")
        collection = collection_value
        if collection not in _legacy().COLLECTIONS or collection == "journal":
            raise ValueError(f"Unsupported v4 collection in capture slice: {collection}")
        payload_object = _object_dict(copy.deepcopy(payload), label=f"Current {collection} payload")
        _graph_records(graph, collection).append(payload_object)
        before[record_id_value] = {
            "collection": collection,
            "revision": int(revision_value),
            "payload_hash": payload_hash_value,
            "payload": copy.deepcopy(payload_object),
        }
    for collection in _legacy().COLLECTIONS:
        graph[collection] = sorted(
            _graph_records(graph, collection),
            key=lambda item: _legacy().normalize_space(item.get("id")),
        )
    return graph, before


def _source_for_entity_phase(packet: Mapping[str, object]) -> CaptureSource:
    source = packet.get("source") or {}
    if not isinstance(source, Mapping):
        raise ValueError("source must be an object")
    observed_at = _legacy().parse_time(source.get("observed_at"))
    captured_at = _legacy().utc_now()
    return {
        "id": "source_capture_planning",
        "observed_at": observed_at,
        "captured_at": captured_at,
        "source_tier": _legacy().source_tier(source.get("source_tier")),
    }


def _resolved_local_refs(
    graph: dict[str, object],
    records: Mapping[str, Sequence[Mapping[str, object]]],
    packet: Mapping[str, object],
) -> dict[str, str]:
    """Resolve packet-local entities on a copy to derive generic identities."""
    work = copy.deepcopy(graph)
    source = _source_for_entity_phase(packet)
    local: dict[str, str] = {}
    local_names: dict[str, set[str]] = {}
    for raw in records["entities"]:
        entity_id, _ = _legacy().upsert_entity(
            work,
            dict(raw),
            source["id"],
            source["observed_at"],
            source["source_tier"],
        )
        ref = _legacy().normalize_space(raw.get("ref"))
        if ref:
            prior = local.get(ref)
            if prior and prior != entity_id:
                raise ValueError(f"Packet-local ref {ref!r} is ambiguous")
            local[ref] = entity_id
        local_names.setdefault(_legacy().normalize_space(raw.get("name")), set()).add(entity_id)
    for name, ids in local_names.items():
        if name and len(ids) == 1:
            local.setdefault(name, next(iter(ids)))
    # Copy phase entities back so callers can resolve new packet-local refs.
    graph["entities"] = work["entities"]
    return local


def _generic_identity(
    collection: str,
    raw: Mapping[str, object],
    local_refs: Mapping[str, str],
    graph: dict[str, object],
    source: CaptureSource,
) -> tuple[str, str]:
    if collection == "events":
        kind = _legacy().normalize_key(raw.get("kind")) or "event"
        if kind == "reusable_fix":
            summary = raw.get("summary")
            labels = ("Symptom:", "Cause:", "Fix:", "Verified by:", "Applies when:")
            if not isinstance(summary, str):
                raise ValueError("reusable_fix requires the five-field summary")
            remainder = summary.strip()
            for index, label in enumerate(labels):
                if not remainder.startswith(label):
                    raise ValueError(
                        "reusable_fix requires Symptom, Cause, Fix, Verified by, Applies when"
                    )
                remainder = remainder[len(label) :].strip()
                if index < len(labels) - 1:
                    value, separator, remainder = remainder.partition(labels[index + 1])
                    if not value.strip() or not separator:
                        raise ValueError("reusable_fix requires five nonempty summary fields")
                    remainder = labels[index + 1] + remainder
                elif not remainder:
                    raise ValueError("reusable_fix requires five nonempty summary fields")
        title = _legacy().normalize_space(raw.get("title"))
        timestamp = _legacy().parse_time(raw.get("occurred_at"), source["observed_at"])
        prefix = "event"
    elif collection == "decisions":
        kind = "decision"
        title = _legacy().normalize_space(raw.get("title"))
        timestamp = _legacy().parse_time(raw.get("decided_at"), source["observed_at"])
        prefix = "decision"
    elif collection == "open_loops":
        kind = "open_loop"
        title = _legacy().normalize_space(raw.get("title"))
        timestamp = _legacy().parse_time(raw.get("created_at"), source["observed_at"])
        prefix = "loop"
    elif collection == "receipts":
        kind = _legacy().normalize_key(raw.get("kind")) or "receipt"
        title = _legacy().normalize_space(raw.get("title"))
        timestamp = _legacy().parse_time(raw.get("verified_at"), source["observed_at"])
        prefix = "receipt"
    else:
        raise ValueError(collection)
    refs = _legacy().record_entity_refs(dict(raw), dict(local_refs), graph)
    identity = {
        "kind": kind,
        "title": _legacy().normalize_key(title),
        "timestamp": timestamp,
        "entities": sorted(refs),
    }
    display = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    record_id = _legacy().explicit_record_id(raw.get("id")) or _legacy().stable_id(prefix, identity)
    return record_id, display


def _add_generic_candidates(
    store: CaptureStore,
    graph: dict[str, object],
    packet: Mapping[str, object],
    records: Mapping[str, Sequence[Mapping[str, object]]],
    selected: set[str],
) -> dict[tuple[str, str], str]:
    local = _resolved_local_refs(graph, records, packet)
    source = _source_for_entity_phase(packet)
    identities: dict[tuple[str, str], str] = {}
    for raw in records["relations"]:
        source_id = _legacy().resolve_entity_ref(raw.get("source"), local, graph)
        target_id = _legacy().resolve_entity_ref(raw.get("target"), local, graph)
        relation_type = _legacy().normalize_key(raw.get("type"))
        if not relation_type:
            raise ValueError("Relation type is required")
        relation_id = _legacy().stable_id(
            "relation",
            {"source": source_id, "target": target_id, "type": relation_type},
        )
        selected.add(relation_id)
        owner = store.semantic_key_owner("relations", "relation", relation_id)
        if owner:
            selected.add(owner)
    for collection in ("events", "decisions", "open_loops", "receipts"):
        for raw in records[collection]:
            record_id, display = _generic_identity(collection, raw, local, graph, source)
            identities[(collection, record_id)] = display
            selected.add(record_id)
            owner = store.semantic_key_owner(collection, "episode", display)
            if owner:
                selected.add(owner)
    return identities


def _build_capture_slice(
    store: CaptureStore,
    packet: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, PriorRecord], dict[tuple[str, str], str]]:
    scrubbed, _ = _legacy().scrub_value(dict(packet), redact_paths=False)
    scrubbed_packet = _object_dict(scrubbed, label="Scrubbed capture packet")
    records = _packet_records(scrubbed_packet)
    selected = _record_ids_from_packet(records)
    selected.update(_coverage_existing_ids(scrubbed_packet))
    _entity_candidates(store, records, selected)
    surface_ids = _surface_candidates(store, records, selected)
    if records["entity_redirects"] and selected:
        redirect_surfaces = store.records_referencing(
            selected, ref_kinds=["subject"], limit=MAX_CAPTURE_RELATED_RECORDS
        )
        for item in redirect_surfaces:
            if item.get("collection") == "claim_surfaces":
                surface_ids.add(str(item["record_id"]))
        selected.update(surface_ids)
        if surface_ids:
            _add_result_ids(
                selected,
                store.records_referencing(
                    surface_ids,
                    ref_kinds=["surface"],
                    limit=MAX_CAPTURE_RELATED_RECORDS,
                ),
            )
    graph, before = _slice_graph(store, selected)
    identities = _add_generic_candidates(store, graph, scrubbed_packet, records, selected)
    # Generic candidates may have added exact IDs or semantic owners. Rebuild
    # the bounded slice once with their full reference closure.
    graph, before = _slice_graph(store, selected)
    return graph, before, identities


def _semantic_keys_for_record(
    collection: str,
    record_id: str,
    identities: Mapping[tuple[str, str], str],
) -> list[dict[str, str]]:
    if collection in {"events", "decisions", "open_loops", "receipts"}:
        display = identities.get((collection, record_id))
        return [{"kind": "episode", "key": display}] if display else []
    if collection == "relations":
        return [{"kind": "relation", "key": record_id}]
    return []


def _changed_records(
    graph: Mapping[str, object],
    before: Mapping[str, PriorRecord],
    identities: Mapping[tuple[str, str], str],
) -> list[dict[str, object]]:
    changes: list[dict[str, object]] = []
    for collection in _storage().COLLECTIONS:
        for payload in _graph_records(graph, collection):
            record_id = _legacy().normalize_space(payload.get("id"))
            if not record_id:
                continue
            prior = before.get(record_id)
            payload_hash = _storage().stable_hash(payload)
            if prior and prior.get("payload_hash") == payload_hash:
                continue
            changes.append(
                {
                    "record_id": record_id,
                    "collection": collection,
                    "payload": copy.deepcopy(payload),
                    "change_kind": "create" if prior is None else "revise",
                    "expected_revision": None if prior is None else int(prior["revision"]),
                    "semantic_keys": _semantic_keys_for_record(collection, record_id, identities),
                    "search_text": _legacy().record_text(
                        collection,
                        payload,
                        _legacy().index_by_id(_graph_records(graph, "entities")),
                    ),
                }
            )
    return sorted(changes, key=lambda item: (item["collection"], item["record_id"]))


def _constraint_expectations(graph: Mapping[str, object]) -> list[dict[str, object]]:
    constraints: list[dict[str, object]] = []
    for entity in _graph_records(graph, "entities"):
        namespace = _legacy().normalize_key(entity.get("namespace")) or "global"
        identities_value = entity.get("identity_keys", [])
        if not isinstance(identities_value, list):
            raise ValueError("Entity identity_keys must be a list")
        for identity in cast(Sequence[object], identities_value):
            if isinstance(identity, Mapping):
                constraints.append(
                    {
                        "kind": "identity_owner",
                        "scheme": _legacy().normalize_key(identity.get("scheme")),
                        "namespace": _legacy().normalize_key(identity.get("namespace"))
                        or namespace,
                        "value": str(identity.get("value") or "").strip(),
                    }
                )
    for surface in _graph_records(graph, "claim_surfaces"):
        subjects_value = surface.get("subject_refs", [])
        if not isinstance(subjects_value, list):
            raise ValueError("Claim surface subject_refs must be a list")
        subjects = sorted(_legacy().unique_strings(cast(Sequence[object], subjects_value)))
        constraints.append(
            {
                "kind": "surface_signature_owner",
                "family": _legacy().normalize_key(surface.get("family")),
                "key": _legacy().normalize_key(surface.get("key")),
                "subject_signature": _storage().stable_hash(subjects),
            }
        )
    # Stable ordering also makes dry-run output easy to diff.
    return sorted(constraints, key=_storage().canonical_json)


def _existing_fingerprint(store: CaptureStore, fingerprint: str) -> dict[str, object] | None:
    row = store.connection.execute(
        "SELECT watermark,mutation_id,details_json FROM mutations WHERE fingerprint=? "
        "ORDER BY watermark LIMIT 1",
        (fingerprint,),
    ).fetchone()
    if row is None:
        return None
    result: dict[str, object] = {
        "status": "duplicate",
        "fingerprint": fingerprint,
        "watermark": int(row["watermark"]),
        "mutation_id": row["mutation_id"],
        "changed_ids": [],
    }
    details_raw = row["details_json"]
    if isinstance(details_raw, str) and details_raw:
        details = json.loads(details_raw)
        if isinstance(details, Mapping) and isinstance(details.get("coverage"), Mapping):
            result["coverage"] = dict(cast(Mapping[str, object], details["coverage"]))
    return result


def _execute_capture(
    store: object,
    request: CaptureRequest,
) -> dict[str, object]:
    """Execute the ``knowledge_graph_v4_cli.CaptureHandler`` contract."""
    if not isinstance(store, _dependencies().store_type):
        raise TypeError("Capture handler requires an open KnowledgeGraphV4 store")
    capture_store = cast(CaptureStore, store)
    if request.handoff_prompt_id is not None:
        validate_prompt_id(request.handoff_prompt_id)
    consumer = capture_store.connection.execute(
        "SELECT agent_id,task_id,parent_task_id FROM consumers WHERE consumer_id=?",
        (request.consumer_id,),
    ).fetchone()
    if consumer is None:
        raise _storage().SubscriptionError("Capture requires a registered task-local consumer")
    expected_owner = (
        request.envelope.producer_agent_id,
        request.envelope.task_id,
        request.envelope.parent_task_id or None,
    )
    actual_owner = (
        str(consumer["agent_id"]),
        str(consumer["task_id"]),
        str(consumer["parent_task_id"]) if consumer["parent_task_id"] else None,
    )
    if actual_owner != expected_owner:
        raise _storage().SubscriptionError(
            "Capture producer/task provenance does not own the supplied consumer"
        )
    if request.expected_global_watermark is not None and int(
        request.expected_global_watermark
    ) != int(capture_store.watermark):
        raise _storage().ConcurrencyConflict(
            "Global watermark differs from the explicitly requested strict boundary"
        )
    if request.expected_absent:
        raise ValueError(
            "Use the signed v4 plan's identity/semantic absence predicates; "
            "free-form --expect-absent is not accepted by semantic capture"
        )
    if not 0 <= int(request.envelope.based_on_watermark) <= int(capture_store.watermark):
        raise ValueError("based_on_watermark exceeds current committed state")
    raw_packet = _packet(request.input_path, getattr(request, "input_bytes", None))
    source_record = raw_packet.get("source")
    if isinstance(source_record, Mapping):
        validate_preference_record("sources", source_record)
    for collection, records in _packet_records(raw_packet).items():
        if collection != "claims":
            for record in records:
                validate_preference_record(collection, record)
    # The reserved archive scheme needs exact current authorization even on an
    # idempotent replay. Ordinary sources do not open configuration or archives.
    source = raw_packet.get("source")
    source_uri = source.get("uri") if isinstance(source, dict) else None
    archived_source = isinstance(source_uri, str) and _legacy().normalize_space(
        source_uri
    ).casefold().startswith("tedd-source")
    if archived_source:
        _validate_source_binding(store, raw_packet, request)
    fingerprint_graph = _object_dict(_legacy().blank_graph(), label="Fingerprint graph")
    fingerprint_graph["_capture_fingerprint_only"] = _legacy()._CAPTURE_FINGERPRINT_ONLY
    producer: dict[str, object] = {
        "agent_id": request.envelope.producer_agent_id,
        "task_id": request.envelope.task_id,
        "parent_task_id": request.envelope.parent_task_id,
        "run_id": request.envelope.producer_run_id,
    }
    fingerprint_result = _object_dict(
        _legacy().capture_packet(
            fingerprint_graph,
            raw_packet,
            authority_ceiling=request.authority_ceiling,
            allow_replace=request.allow_replace,
            allow_redirect=request.allow_redirect,
            producer=producer,
            based_on_watermark=request.envelope.based_on_watermark,
            discovery_kind=request.envelope.discovery_kind,
        ),
        label="Capture fingerprint planning result",
    )
    preliminary_fingerprint = fingerprint_result.get("fingerprint")
    if not isinstance(preliminary_fingerprint, str) or not preliminary_fingerprint:
        raise ValueError("Capture fingerprint must be a nonempty string")
    expected = request.expected_fingerprint
    if expected and expected != preliminary_fingerprint:
        raise _storage().PlanTokenError("Capture fingerprint does not match the reviewed dry run")
    duplicate = _existing_fingerprint(capture_store, preliminary_fingerprint)
    if duplicate:
        if not request.dry_run and not request.plan_token:
            raise _storage().PlanTokenError(
                "v4 capture apply requires --expect-plan-token from the reviewed dry run"
            )
        duplicate.update(
            {
                "redactions": fingerprint_result.get("redactions", 0),
                "policy_adjustments": fingerprint_result.get("policy_adjustments", []),
            }
        )
        return duplicate
    graph, before, identities = _build_capture_slice(capture_store, raw_packet)
    result = _object_dict(
        _legacy().capture_packet(
            graph,
            raw_packet,
            authority_ceiling=request.authority_ceiling,
            allow_replace=request.allow_replace,
            allow_redirect=request.allow_redirect,
            producer=producer,
            based_on_watermark=request.envelope.based_on_watermark,
            discovery_kind=request.envelope.discovery_kind,
        ),
        label="Capture result",
    )
    fingerprint_value = result.get("fingerprint")
    if not isinstance(fingerprint_value, str) or not fingerprint_value:
        raise ValueError("Capture result fingerprint must be a nonempty string")
    fingerprint = fingerprint_value
    if fingerprint != preliminary_fingerprint:
        raise ValueError("Capture fingerprint changed between planning phases")
    changes = _changed_records(graph, before, identities)
    surfaces = {str(item["id"]): item for item in _graph_records(graph, "claim_surfaces")}
    for change in changes:
        payload = _object_dict(change["payload"], label="Preference admission payload")
        validate_preference_record(str(change["collection"]), payload)
        if change["collection"] == "claims" and payload.get("family") == "preference":
            surface = surfaces[str(payload["surface_ref"])]
            validate_preference_record("claim_surfaces", surface)
            if any(
                is_preference_scope(ref)
                for ref in cast(list[object], payload.get("entity_refs", []))
            ) and payload.get("entity_refs") != surface.get("subject_refs"):
                raise ValueError("A preference can cite the global scope only as its sole subject")
    if any(
        item["collection"] == "open_loops"
        and item["record_id"] not in before
        and _object_dict(item["payload"], label="New loop").get("checkpoint_at") is None
        and _object_dict(item["payload"], label="New loop").get("status") in {"open", "candidate"}
        for item in changes
    ):
        adjustments = result.get("policy_adjustments", [])
        if not isinstance(adjustments, list):
            raise ValueError("Capture policy adjustments must be a list")
        result["policy_adjustments"] = [*adjustments, "loop_without_checkpoint"]
    if archived_source:
        _validate_source_binding(store, raw_packet, request, changes=changes)
    if not changes:
        raise ValueError("Capture produced no durable record changes")
    validation = _object_dict(_legacy().validate_graph(graph), label="Capture validation")
    if not bool(validation.get("valid")):
        errors_value = validation.get("errors", [])
        if not isinstance(errors_value, list):
            raise _storage().ValidationError("Bounded semantic capture returned invalid errors")
        errors = [str(item) for item in cast(Sequence[object], errors_value)]
        raise _storage().ValidationError(
            "Bounded semantic capture failed validation: " + "; ".join(errors)
        )
    read_ids = sorted(before)
    constraints = _constraint_expectations(graph)
    plan_token = capture_store.create_plan_token(
        read_ids,
        fingerprint,
        constraint_expectations=constraints,
    )
    expected_revisions = {record_id: int(item["revision"]) for record_id, item in before.items()}
    for record_id, expected_revision in request.expected_revisions.items():
        actual_revision = expected_revisions.get(record_id)
        if actual_revision != int(expected_revision):
            raise _storage().ConcurrencyConflict(
                f"Explicit expected revision for {record_id} differs from the capture read set"
            )
    summary = {
        **result,
        "status": "would_capture" if request.dry_run else "ready",
        "base_watermark": capture_store.watermark,
        "next_watermark": capture_store.watermark + 1,
        "plan_token": plan_token,
        "read_set_count": len(read_ids),
        "constraint_count": len(constraints),
        "expected_revisions": expected_revisions,
        "changed_ids": [item["record_id"] for item in changes],
        "validation": validation,
    }
    if request.dry_run:
        return summary
    if not request.plan_token:
        raise _storage().PlanTokenError(
            "v4 capture apply requires --expect-plan-token from the reviewed dry run"
        )
    journal_records = _graph_records(graph, "journal")
    if not journal_records:
        raise _storage().ValidationError("Capture produced no journal entry")
    journal = journal_records[-1]
    redactions_value = result.get("redactions", 0)
    if not isinstance(redactions_value, (str, bytes, bytearray, int, float)):
        raise ValueError("Capture redaction count must be numeric")
    source_ref_value = journal.get("source_ref")
    if source_ref_value is not None and not isinstance(source_ref_value, str):
        raise ValueError("Capture journal source_ref must be a string")
    capture_policy_value = journal.get("capture_policy")
    capture_policy = (
        _object_dict(capture_policy_value, label="Capture journal policy")
        if capture_policy_value is not None
        else None
    )
    mutation_details: dict[str, object] = {
        "claim_outcomes": result.get("claim_outcomes", {}),
        "policy_adjustments": result.get("policy_adjustments", []),
        "consumer_id": request.consumer_id,
    }
    if request.handoff_prompt_id is not None:
        mutation_details["handoff_prompt_id"] = request.handoff_prompt_id
    if isinstance(result.get("coverage"), Mapping):
        mutation_details["coverage"] = result["coverage"]
    if archived_source:
        _validate_source_binding(store, raw_packet, request, changes=changes)
    reserve = _dependencies().source_commit_reservation
    with reserve() if archived_source and reserve is not None else nullcontext():
        applied = capture_store.apply_changes(
            changes,
            fingerprint=fingerprint,
            action="capture",
            discovery_kind=request.envelope.discovery_kind,
            producer_agent_id=request.envelope.producer_agent_id,
            task_id=request.envelope.task_id,
            parent_task_id=request.envelope.parent_task_id,
            run_id=request.envelope.producer_run_id,
            based_on_watermark=request.envelope.based_on_watermark,
            expected_revisions=expected_revisions,
            plan_token=request.plan_token,
            idempotency_key=f"capture:{fingerprint}",
            source_ref=source_ref_value,
            capture_policy=capture_policy,
            details=mutation_details,
            redaction_count=int(redactions_value),
            preflight=(
                lambda: _validate_source_binding(
                    store, raw_packet, request, changes=changes, local_only=True
                )
            )
            if archived_source
            else None,
        )
    return {
        **summary,
        **applied,
        "status": "captured" if applied.get("status") == "applied" else applied.get("status"),
        "plan_token": None,
    }


def execute_capture(
    store: object,
    request: CaptureRequest,
    *,
    dependencies: CaptureDependencies,
) -> dict[str, object]:
    """Run semantic capture with request-local, explicitly injected policy."""

    token = _CAPTURE_DEPENDENCIES.set(dependencies)
    try:
        return _execute_capture(store, request)
    finally:
        _CAPTURE_DEPENDENCIES.reset(token)


__all__ = ["CaptureDependencies", "execute_capture"]
