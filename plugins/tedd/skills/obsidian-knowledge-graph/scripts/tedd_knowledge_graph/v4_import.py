"""Validated v3-to-v4 SQLite import transaction.

The side-by-side upgrade service owns review, activation, and rollback policy.
This lower-level service owns only the deterministic SQL materialization of one
already reviewed v3 graph into a newly created v4 store.  The public facade
injects schema, hashing, registry, path, and error policy at call time.
"""

from __future__ import annotations

import contextlib
import copy
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypeAlias, TypedDict, cast

JsonObject: TypeAlias = dict[str, object]
LegacyRecord: TypeAlias = tuple[str, str, JsonObject]


class RevisionReference(TypedDict):
    target_id: str
    kind: str
    ordinal: int


class ImportDescriptor(TypedDict):
    record_id: str
    collection: str
    revision: int
    payload_hash: str
    change_kind: str
    ordinal: int


class ImportStore(Protocol):
    @property
    def connection(self) -> sqlite3.Connection: ...

    def _apply_entity_registry(
        self,
        record_id: str,
        payload: Mapping[str, object],
        watermark: int,
    ) -> None: ...

    def _apply_surface_registry(
        self,
        record_id: str,
        payload: Mapping[str, object],
        watermark: int,
    ) -> None: ...

    def validate(self, *, full: bool = False) -> Mapping[str, object]: ...

    def close(self) -> None: ...


class CreateStore(Protocol):
    def __call__(
        self,
        path: str | Path,
        *,
        plan_hmac_key: bytes | str | None = None,
    ) -> ImportStore: ...


class ImportValidator(Protocol):
    def __call__(self, graph: Mapping[str, object]) -> tuple[int, str, int, str]: ...


class ReferenceExtractor(Protocol):
    def __call__(
        self,
        payload: Mapping[str, object],
        explicit: Sequence[Mapping[str, object]],
    ) -> list[RevisionReference]: ...


class PathFactory(Protocol):
    def __call__(self, value: str | Path) -> Path: ...


@dataclass(frozen=True)
class V3ValidationDependencies:
    """Live v3 identity and journal validation policy."""

    collections: Sequence[str]
    valid_record_id: Callable[[object], str]
    normalize_space: Callable[[object], str]
    stable_hash: Callable[[object], str]
    compare_digest: Callable[[str, str], bool]
    validation_error_type: type[Exception]


@dataclass(frozen=True)
class ImportDependencies:
    """Call-time storage, schema, hash, and registry policy for one import."""

    collections: Sequence[str]
    schema_version: str
    hash_algorithm: str
    legacy_hash_algorithm: str
    transform_version: str
    validate_v3_import: ImportValidator
    valid_source_sha256: Callable[[str], bool]
    normalize_space: Callable[[object], str]
    normalize_key: Callable[[object], str]
    valid_record_id: Callable[[object], str]
    stable_hash: Callable[[object], str]
    canonical_json: Callable[[object], str]
    sha256_text: Callable[[str], str]
    payload_search_text: Callable[[str, Mapping[str, object]], str]
    extract_refs: ReferenceExtractor
    create_store: CreateStore
    path_factory: PathFactory
    key_path: Callable[[Path], Path]
    utc_now: Callable[[], str]
    validation_error_type: type[Exception]


def validate_v3_import(
    graph: object,
    *,
    dependencies: V3ValidationDependencies,
) -> tuple[int, str, int, str]:
    """Validate the exact legacy collection and journal invariants used by import."""

    if not isinstance(graph, Mapping) or graph.get("schema_version") != "v3":
        raise dependencies.validation_error_type(
            "Side-by-side import requires a validated v3 graph"
        )
    known_ids: set[str] = set()
    for collection in dependencies.collections:
        records = graph.get(collection)
        if not isinstance(records, list):
            raise dependencies.validation_error_type(f"v3 collection {collection} must be a list")
        for record in records:
            if not isinstance(record, Mapping):
                raise dependencies.validation_error_type(f"v3 {collection} contains a non-object")
            record_id = dependencies.valid_record_id(record.get("id"))
            if record_id in known_ids:
                raise dependencies.validation_error_type(f"Duplicate v3 record ID: {record_id}")
            known_ids.add(record_id)
    anchor = graph.get("journal_anchor") or {"watermark": 0, "entry_hash": ""}
    if not isinstance(anchor, Mapping):
        raise dependencies.validation_error_type("v3 journal_anchor must be an object")
    anchor_watermark = int(cast(str | int, anchor.get("watermark", 0)))
    previous_hash = dependencies.normalize_space(anchor.get("entry_hash"))
    previous_watermark = anchor_watermark
    journal = graph.get("journal", [])
    if not isinstance(journal, list):
        raise dependencies.validation_error_type("v3 journal must be a list")
    for entry in journal:
        if not isinstance(entry, Mapping):
            raise dependencies.validation_error_type("v3 journal contains a non-object")
        watermark = int(cast(str | int, entry.get("watermark", -1)))
        if watermark != previous_watermark + 1:
            raise dependencies.validation_error_type(f"v3 journal watermark gap at {watermark}")
        if dependencies.normalize_space(entry.get("previous_entry_hash")) != previous_hash:
            raise dependencies.validation_error_type(
                f"v3 journal previous hash mismatch at {watermark}"
            )
        body = {key: value for key, value in entry.items() if key != "entry_hash"}
        expected = dependencies.stable_hash({"previous_hash": previous_hash, "entry": body})
        if not dependencies.compare_digest(
            dependencies.normalize_space(entry.get("entry_hash")),
            expected,
        ):
            raise dependencies.validation_error_type(
                f"v3 journal entry hash mismatch at {watermark}"
            )
        previous_hash = expected
        previous_watermark = watermark
    expected_next = int(cast(str | int, graph.get("next_watermark", previous_watermark + 1)))
    if expected_next != previous_watermark + 1:
        raise dependencies.validation_error_type("v3 next_watermark disagrees with journal tail")
    return (
        anchor_watermark,
        dependencies.normalize_space(anchor.get("entry_hash")),
        previous_watermark,
        previous_hash,
    )


def _copy_legacy_journal(
    connection: sqlite3.Connection,
    graph: Mapping[str, object],
    *,
    now: str,
    dependencies: ImportDependencies,
) -> None:
    def structural_coverage(value: Mapping[str, object]) -> JsonObject:
        structural: JsonObject = {
            key: copy.deepcopy(value[key])
            for key in (
                "version",
                "coverage_digest",
                "disposition_counts",
                "category_counts",
            )
            if key in value
        }
        items = value.get("items")
        if isinstance(items, list):
            structural["items"] = [
                {
                    key: copy.deepcopy(item[key])
                    for key in (
                        "category",
                        "disposition",
                        "path",
                        "record_id",
                        "record_ids",
                        "reason",
                    )
                    if key in item
                }
                for item in items
                if isinstance(item, Mapping)
            ]
        return structural

    journal = cast(list[object], graph.get("journal", []))
    for raw_entry in journal:
        entry = cast(Mapping[str, object], raw_entry)
        raw_producer = entry.get("producer")
        producer = (
            cast(Mapping[str, object], raw_producer) if isinstance(raw_producer, Mapping) else {}
        )
        capture_policy = entry.get("capture_policy")
        details = entry.get("details")
        coverage = entry.get("coverage")
        if isinstance(coverage, Mapping):
            coverage = structural_coverage(coverage)
            details_object = dict(details) if isinstance(details, Mapping) else {}
            details_object["coverage"] = dict(coverage)
            details = details_object
        connection.execute(
            """
            INSERT INTO mutations(
              watermark,mutation_id,action,discovery_kind,fingerprint,
              fingerprint_version,producer_agent_id,task_id,parent_task_id,
              run_id,based_on_watermark,captured_at,previous_entry_hash,
              entry_hash,hash_algorithm,source_ref,capture_policy_json,
              details_json,legacy_header_json,redaction_count
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(cast(str | int, entry["watermark"])),
                dependencies.valid_record_id(entry.get("id")),
                dependencies.normalize_key(entry.get("action")) or "capture",
                "legacy",
                dependencies.normalize_space(entry.get("fingerprint"))
                or dependencies.stable_hash({"legacy_watermark": entry["watermark"]}),
                dependencies.normalize_space(entry.get("fingerprint_version")) or "v1",
                dependencies.normalize_space(producer.get("agent_id")) or None,
                dependencies.normalize_space(producer.get("task_id")) or None,
                dependencies.normalize_space(producer.get("parent_task_id")) or None,
                dependencies.normalize_space(producer.get("run_id")) or None,
                entry.get("based_on_watermark"),
                dependencies.normalize_space(entry.get("captured_at")) or now,
                dependencies.normalize_space(entry.get("previous_entry_hash")),
                dependencies.normalize_space(entry.get("entry_hash")),
                dependencies.legacy_hash_algorithm,
                dependencies.normalize_space(entry.get("source_ref")) or None,
                (
                    dependencies.canonical_json(capture_policy)
                    if isinstance(capture_policy, Mapping)
                    else None
                ),
                (dependencies.canonical_json(details) if isinstance(details, Mapping) else None),
                dependencies.canonical_json(dict(entry)),
                int(cast(str | int, entry.get("redaction_count", 0))),
            ),
        )


def _collect_records(
    graph: Mapping[str, object],
    *,
    dependencies: ImportDependencies,
) -> list[LegacyRecord]:
    records: list[LegacyRecord] = []
    for collection in dependencies.collections:
        raw_records = cast(list[object], graph.get(collection, []))
        for raw in raw_records:
            payload = copy.deepcopy(dict(cast(Mapping[str, object], raw)))
            record_id = dependencies.valid_record_id(payload.get("id"))
            payload["id"] = record_id
            records.append((record_id, collection, payload))
    records.sort(key=lambda item: (item[1], item[0]))
    return records


def _insert_record_revisions(
    connection: sqlite3.Connection,
    records: Sequence[LegacyRecord],
    *,
    baseline_watermark: int,
    legacy_watermark: int,
    now: str,
    producer_agent_id: str,
    task_id: str | None,
    parent_task_id: str | None,
    dependencies: ImportDependencies,
) -> list[ImportDescriptor]:
    descriptors: list[ImportDescriptor] = []
    for ordinal, (record_id, collection, payload) in enumerate(records):
        encoded = dependencies.canonical_json(payload)
        payload_hash = dependencies.sha256_text(encoded)
        search_text = dependencies.payload_search_text(collection, payload)
        connection.execute(
            """
            INSERT INTO records(
              record_id,collection,current_revision,first_watermark,last_watermark,
              lifecycle,source_tier,sensitive,current_payload_hash,search_text,
              created_at,updated_at
            ) VALUES(?,?,1,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_id,
                collection,
                baseline_watermark,
                baseline_watermark,
                dependencies.normalize_space(payload.get("lifecycle")) or None,
                dependencies.normalize_key(payload.get("source_tier")) or None,
                int(bool(payload.get("sensitive"))),
                payload_hash,
                search_text,
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO record_revision_headers(
              record_id,revision,watermark,change_kind,discovery_kind,payload_hash,
              producer_agent_id,task_id,parent_task_id,run_id,based_on_watermark,created_at
            ) VALUES(?,1,?,'legacy_import',?,?,?,?,?,?,?,?)
            """,
            (
                record_id,
                baseline_watermark,
                "migration",
                payload_hash,
                dependencies.normalize_space(producer_agent_id) or None,
                dependencies.normalize_space(task_id) or None,
                dependencies.normalize_space(parent_task_id) or None,
                None,
                legacy_watermark,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO record_revision_payloads(record_id,revision,payload_json) VALUES(?,1,?)",
            (record_id, encoded),
        )
        descriptors.append(
            {
                "record_id": record_id,
                "collection": collection,
                "revision": 1,
                "payload_hash": payload_hash,
                "change_kind": "legacy_import",
                "ordinal": ordinal,
            }
        )
    return descriptors


def _rebuild_record_indexes(
    store: ImportStore,
    records: Sequence[LegacyRecord],
    *,
    baseline_watermark: int,
    dependencies: ImportDependencies,
) -> None:
    connection = store.connection
    for record_id, collection, payload in records:
        refs = dependencies.extract_refs(payload, ())
        if collection == "entities":
            refs.append({"target_id": record_id, "kind": "self", "ordinal": 0})
        for ref in refs:
            connection.execute(
                "INSERT INTO revision_refs(record_id,revision,target_record_id,ref_kind,ordinal) "
                "VALUES(?,1,?,?,?)",
                (record_id, ref["target_id"], ref["kind"], ref["ordinal"]),
            )
        if collection == "entities":
            store._apply_entity_registry(record_id, payload, baseline_watermark)
        if collection == "claim_surfaces":
            store._apply_surface_registry(record_id, payload, baseline_watermark)
        rowid = connection.execute(
            "SELECT rowid FROM records WHERE record_id=?", (record_id,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO records_fts(rowid,record_id,collection,search_text) VALUES(?,?,?,?)",
            (
                rowid,
                record_id,
                collection,
                dependencies.payload_search_text(collection, payload),
            ),
        )


def _rebuild_current_claim_registry(
    connection: sqlite3.Connection,
    records: Sequence[LegacyRecord],
    *,
    baseline_watermark: int,
    dependencies: ImportDependencies,
) -> None:
    for record_id, collection, payload in records:
        if collection != "claims" or not (
            bool(payload.get("active"))
            and dependencies.normalize_key(payload.get("lifecycle")) == "current"
        ):
            continue
        surface_id = dependencies.valid_record_id(payload.get("surface_ref"))
        surface = connection.execute(
            "SELECT 1 FROM claim_surface_constraints WHERE surface_id=?",
            (surface_id,),
        ).fetchone()
        if surface is None:
            raise dependencies.validation_error_type(
                f"Imported current claim {record_id} references unknown surface {surface_id}"
            )
        try:
            connection.execute(
                "INSERT INTO claim_current_registry(surface_id,claim_id,updated_watermark) "
                "VALUES(?,?,?)",
                (surface_id, record_id, baseline_watermark),
            )
        except sqlite3.IntegrityError as error:
            raise dependencies.validation_error_type(
                f"Imported graph has multiple current claims for surface {surface_id}"
            ) from error


def _publish_migration_mutation(
    connection: sqlite3.Connection,
    records: Sequence[LegacyRecord],
    descriptors: Sequence[ImportDescriptor],
    *,
    baseline_watermark: int,
    legacy_watermark: int,
    legacy_head: str,
    source_sha256: str,
    producer_agent_id: str,
    task_id: str | None,
    parent_task_id: str | None,
    discovery_kind: str,
    now: str,
    dependencies: ImportDependencies,
) -> str:
    fingerprint = dependencies.stable_hash(
        {
            "transform": dependencies.transform_version,
            "source_sha256": source_sha256,
            "legacy_watermark": legacy_watermark,
            "record_count": len(records),
        }
    )
    details: dict[str, object] = {
        "from_schema_version": "v3",
        "to_schema_version": dependencies.schema_version,
        "transform_version": dependencies.transform_version,
        "source_sha256": source_sha256,
        "legacy_watermark": legacy_watermark,
        "legacy_payload_semantics": (
            "journal_headers_preserved; current records baseline at migration"
        ),
    }
    entry_material: dict[str, object] = {
        "watermark": baseline_watermark,
        "action": "migrate_storage",
        "discovery_kind": dependencies.normalize_key(discovery_kind) or "migration",
        "fingerprint": fingerprint,
        "fingerprint_version": "v4",
        "producer_agent_id": dependencies.normalize_space(producer_agent_id) or None,
        "task_id": dependencies.normalize_space(task_id) or None,
        "parent_task_id": dependencies.normalize_space(parent_task_id) or None,
        "run_id": None,
        "based_on_watermark": legacy_watermark,
        "captured_at": now,
        "source_ref": None,
        "capture_policy": None,
        "details": details,
        "redaction_count": 0,
        "changes": list(descriptors),
    }
    entry_hash = dependencies.stable_hash({"previous_hash": legacy_head, "entry": entry_material})
    mutation_id = (
        "mutation_"
        + dependencies.stable_hash({"baseline": baseline_watermark, "fingerprint": fingerprint})[
            :32
        ]
    )
    connection.execute(
        """
        INSERT INTO mutations(
          watermark,mutation_id,action,discovery_kind,fingerprint,
          fingerprint_version,producer_agent_id,task_id,parent_task_id,run_id,
          based_on_watermark,captured_at,previous_entry_hash,entry_hash,
          hash_algorithm,source_ref,capture_policy_json,details_json,
          legacy_header_json,redaction_count
        ) VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,NULL,NULL,?,NULL,0)
        """,
        (
            baseline_watermark,
            mutation_id,
            "migrate_storage",
            dependencies.normalize_key(discovery_kind) or "migration",
            fingerprint,
            "v4",
            dependencies.normalize_space(producer_agent_id) or None,
            dependencies.normalize_space(task_id) or None,
            dependencies.normalize_space(parent_task_id) or None,
            legacy_watermark,
            now,
            legacy_head,
            entry_hash,
            dependencies.hash_algorithm,
            dependencies.canonical_json(details),
        ),
    )
    for item in descriptors:
        connection.execute(
            "INSERT INTO mutation_changes(watermark,ordinal,record_id,collection,revision,"
            "change_kind,payload_hash) VALUES(?,?,?,?,?,?,?)",
            (
                baseline_watermark,
                item["ordinal"],
                item["record_id"],
                item["collection"],
                1,
                "legacy_import",
                item["payload_hash"],
            ),
        )
    connection.execute(
        "INSERT INTO idempotency_keys(idempotency_key,fingerprint,watermark) VALUES(?,?,?)",
        (f"migrate_storage:{source_sha256}", fingerprint, baseline_watermark),
    )
    migration_id = f"migration_{fingerprint[:32]}"
    connection.execute(
        "INSERT INTO schema_migrations(migration_id,from_version,to_version,"
        "transform_version,plan_fingerprint,source_sha256,applied_watermark,applied_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            migration_id,
            "v3",
            dependencies.schema_version,
            dependencies.transform_version,
            fingerprint,
            source_sha256,
            baseline_watermark,
            now,
        ),
    )
    for key, value in {
        "v3_source_sha256": source_sha256,
        "v3_legacy_watermark": legacy_watermark,
        "v4_baseline_watermark": baseline_watermark,
        "legacy_revision_history_available": False,
    }.items():
        connection.execute(
            "INSERT INTO migration_metadata(key,value_json) VALUES(?,?)",
            (key, dependencies.canonical_json(value)),
        )
    connection.execute(
        "UPDATE graph_state SET committed_watermark=?,journal_head_hash=?,updated_at=? "
        "WHERE singleton=1",
        (baseline_watermark, entry_hash, now),
    )
    return entry_hash


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
    dependencies: ImportDependencies,
) -> ImportStore:
    """Import validated v3 state into a new side-by-side v4 store."""

    anchor_watermark, anchor_hash, legacy_watermark, legacy_head = dependencies.validate_v3_import(
        graph
    )
    normalized_source_sha256 = dependencies.normalize_space(source_sha256)
    if not dependencies.valid_source_sha256(normalized_source_sha256):
        raise ValueError("source_sha256 must be the reviewed 64-character v3 digest")
    source_digest = normalized_source_sha256.lower()
    target = dependencies.path_factory(target_path)
    store = dependencies.create_store(target, plan_hmac_key=plan_hmac_key)
    connection = store.connection
    try:
        # The target is not live yet; use rollback journal for a single bulk import.
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("BEGIN IMMEDIATE")
        now = dependencies.utc_now()
        connection.execute(
            "UPDATE graph_state SET journal_anchor_watermark=?,journal_anchor_hash=?,"
            "committed_watermark=?,journal_head_hash=?,updated_at=? WHERE singleton=1",
            (anchor_watermark, anchor_hash, anchor_watermark, anchor_hash, now),
        )
        _copy_legacy_journal(connection, graph, now=now, dependencies=dependencies)
        baseline_watermark = legacy_watermark + 1
        records = _collect_records(graph, dependencies=dependencies)
        descriptors = _insert_record_revisions(
            connection,
            records,
            baseline_watermark=baseline_watermark,
            legacy_watermark=legacy_watermark,
            now=now,
            producer_agent_id=producer_agent_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            dependencies=dependencies,
        )
        _rebuild_record_indexes(
            store,
            records,
            baseline_watermark=baseline_watermark,
            dependencies=dependencies,
        )
        _rebuild_current_claim_registry(
            connection,
            records,
            baseline_watermark=baseline_watermark,
            dependencies=dependencies,
        )
        _publish_migration_mutation(
            connection,
            records,
            descriptors,
            baseline_watermark=baseline_watermark,
            legacy_watermark=legacy_watermark,
            legacy_head=legacy_head,
            source_sha256=source_digest,
            producer_agent_id=producer_agent_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            discovery_kind=discovery_kind,
            now=now,
            dependencies=dependencies,
        )
        connection.commit()
        connection.execute("PRAGMA journal_mode=WAL")
        validation = store.validate(full=True)
        if not validation["valid"]:
            errors = cast(Sequence[str], validation["errors"])
            raise dependencies.validation_error_type(
                "Imported v4 store failed validation: " + "; ".join(errors)
            )
        return store
    except Exception:
        with contextlib.suppress(Exception):
            connection.rollback()
        store.close()
        for candidate in (
            target,
            dependencies.path_factory(f"{target}-wal"),
            dependencies.path_factory(f"{target}-shm"),
            dependencies.key_path(target),
        ):
            with contextlib.suppress(FileNotFoundError):
                candidate.unlink()
        raise
