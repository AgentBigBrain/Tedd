"""Atomic current-record mutation kernel for the v4 knowledge graph.

The public storage facade supplies its live policy callbacks on every call.
This leaf owns the complete write transaction, but does not import the facade,
open connections, or expose command-line policy.
"""

from __future__ import annotations

import copy
import sqlite3
from dataclasses import dataclass
from typing import (
    AbstractSet,
    Callable,
    Iterable,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    TypeAlias,
    TypedDict,
    cast,
)

MAX_CURRENT_REVISION_QUERY_IDS = 2_000

CanonicalJsonScalar: TypeAlias = None | bool | int | float | str
CanonicalJsonValue: TypeAlias = (
    CanonicalJsonScalar
    | list["CanonicalJsonValue"]
    | tuple["CanonicalJsonValue", ...]
    | dict[str, "CanonicalJsonValue"]
)
CanonicalJsonObject: TypeAlias = dict[str, CanonicalJsonValue]


class RevisionReference(TypedDict):
    target_id: str
    kind: str
    ordinal: int


class JournalChange(TypedDict):
    record_id: str
    collection: str
    revision: int
    payload_hash: str
    change_kind: str
    ordinal: int


class AppliedChange(TypedDict):
    record_id: str
    collection: str
    revision: int
    payload_hash: str
    change_kind: str


class AppliedMutationResult(TypedDict):
    status: Literal["applied"]
    watermark: int
    mutation_id: str
    entry_hash: str
    changed: list[AppliedChange]


class DuplicateMutationResult(TypedDict):
    status: Literal["duplicate"]
    watermark: int
    mutation_id: str | None
    changed: list[AppliedChange]


MutationResult: TypeAlias = AppliedMutationResult | DuplicateMutationResult


class PlanReadEntry(TypedDict):
    record_id: str


class CapturePlan(TypedDict):
    read_set: list[PlanReadEntry]


class TransactionGraphState(TypedDict):
    schema_version: str
    store_id: str
    generation: int
    watermark: int
    journal_head_hash: str
    journal_anchor_watermark: int
    journal_anchor_hash: str
    created_at: str
    updated_at: str


class CurrentRevision(TypedDict):
    collection: str
    revision: int


class RecordChangeLike(Protocol):
    """Structural view of one facade-owned record change."""

    @property
    def record_id(self) -> str: ...

    @property
    def collection(self) -> str: ...

    @property
    def payload(self) -> Mapping[str, object]: ...

    @property
    def change_kind(self) -> str: ...

    @property
    def expected_revision(self) -> object: ...

    @property
    def refs(self) -> Sequence[Mapping[str, object]]: ...

    @property
    def semantic_keys(self) -> Sequence[object]: ...

    @property
    def search_text(self) -> str | None: ...


CurrentRevisions: TypeAlias = dict[str, CurrentRevision | None]


class ChangeCoercer(Protocol):
    def __call__(self, raw: object) -> RecordChangeLike: ...


class CurrentRevisionsLoader(Protocol):
    def __call__(self, record_ids: Iterable[str]) -> CurrentRevisions: ...


class CapturePlanVerifier(Protocol):
    def __call__(
        self,
        token: str,
        fingerprint: str,
        state: Mapping[str, object],
    ) -> Mapping[str, object]: ...


class CurrentPayloadLoader(Protocol):
    def __call__(self, record_id: str) -> Mapping[str, object] | None: ...


class ReferenceExtractor(Protocol):
    def __call__(
        self,
        payload: Mapping[str, object],
        explicit: Sequence[Mapping[str, object]],
    ) -> list[RevisionReference]: ...


class EntityRegistryWriter(Protocol):
    """Call shape retained for the facade's entity-registry patch seam."""

    def __call__(
        self,
        record_id: str,
        payload: Mapping[str, object],
        watermark: int,
        pending_payloads: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None: ...


class SurfaceRegistryWriter(Protocol):
    def __call__(
        self,
        record_id: str,
        payload: Mapping[str, object],
        watermark: int,
    ) -> None: ...


class SemanticRegistryWriter(Protocol):
    def __call__(
        self,
        record_id: str,
        collection: str,
        semantic_keys: Sequence[object],
        watermark: int,
    ) -> None: ...


@dataclass(frozen=True)
class TransactionDependencies:
    """Facade policy and store callbacks resolved at mutation call time."""

    collections: AbstractSet[str]
    hash_algorithm: str
    unset: object
    coerce_change: ChangeCoercer
    valid_record_id: Callable[[object], str]
    normalize_space: Callable[[object], str]
    normalize_key: Callable[[object], str]
    canonical_json: Callable[[object], str]
    sha256_text: Callable[[str], str]
    stable_hash: Callable[[object], str]
    compare_digest: Callable[[str, str], bool]
    utc_now: Callable[[], str]
    payload_search_text: Callable[[str, Mapping[str, object]], str]
    extract_refs: ReferenceExtractor
    state_loader: Callable[[], Mapping[str, object]]
    current_revisions_loader: CurrentRevisionsLoader
    verify_capture_plan: CapturePlanVerifier
    current_payload_loader: CurrentPayloadLoader
    apply_entity_registry: EntityRegistryWriter
    apply_surface_registry: SurfaceRegistryWriter
    apply_semantic_registry: SemanticRegistryWriter
    v4_error_type: type[Exception]
    concurrency_conflict_type: type[Exception]
    identity_conflict_type: type[Exception]


@dataclass(frozen=True)
class MutationRequest:
    """Validated inputs that remain stable for one transaction attempt."""

    changes: list[RecordChangeLike]
    fingerprint: str
    action: str
    discovery_kind: str
    producer_agent_id: str | None
    task_id: str | None
    parent_task_id: str | None
    run_id: str | None
    based_on_watermark: int | None
    expectations: dict[str, int | None]
    plan_token: str | None
    idempotency_key: str
    source_ref: str | None
    capture_policy: Mapping[str, object] | None
    details: Mapping[str, object] | None
    redaction_count: int


@dataclass(frozen=True)
class PreparedMutation:
    """Derived current-record material ready for transactional publication."""

    current: CurrentRevisions
    payloads: dict[str, CanonicalJsonObject]
    revisions: dict[str, int]
    hashes: dict[str, str]
    search_documents: dict[str, str]
    refs: dict[str, list[RevisionReference]]
    watermark: int
    captured_at: str


def apply_changes(
    connection: sqlite3.Connection,
    changes: Sequence[RecordChangeLike | Mapping[str, object]],
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
    dependencies: TransactionDependencies,
) -> MutationResult:
    """Apply one packet atomically while owning its complete transaction."""

    request = _normalize_request(
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
        dependencies=dependencies,
    )
    if connection.in_transaction:
        raise dependencies.v4_error_type(
            "Mutation requires a connection with no active transaction"
        )

    transaction_started = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        state = _require_graph_state(dependencies.state_loader())
        # Optional trusted-local authorization check inside the writer boundary.
        # Ordinary mutations have no callback and retain their original path.
        if preflight is not None:
            preflight()
        duplicate = _duplicate_result(
            connection,
            idempotency_key=request.idempotency_key,
            fingerprint=request.fingerprint,
            dependencies=dependencies,
        )
        if duplicate is not None:
            connection.rollback()
            transaction_started = False
            return duplicate

        plan: CapturePlan | None = None
        if request.plan_token:
            plan = _require_capture_plan(
                dependencies.verify_capture_plan(
                    request.plan_token,
                    request.fingerprint,
                    state,
                )
            )
        if request.based_on_watermark is not None and not (
            0 <= int(request.based_on_watermark) <= int(state["watermark"])
        ):
            raise ValueError("based_on_watermark exceeds current committed state")

        prepared = _prepare_mutation(
            request,
            state=state,
            plan=plan,
            dependencies=dependencies,
        )
        _insert_new_record_identities(
            connection,
            request=request,
            prepared=prepared,
            dependencies=dependencies,
        )
        _release_current_claim_slots(connection, request.changes)
        changed = _write_record_revisions(
            connection,
            request=request,
            prepared=prepared,
            dependencies=dependencies,
        )
        _reconcile_current_claims(
            connection,
            changes=request.changes,
            prepared=prepared,
            dependencies=dependencies,
        )
        mutation_id, entry_hash = _publish_mutation_journal(
            connection,
            request=request,
            prepared=prepared,
            changed=changed,
            state=state,
            dependencies=dependencies,
        )
        connection.commit()
        transaction_started = False
    except sqlite3.IntegrityError as error:
        if transaction_started:
            connection.rollback()
        raise dependencies.identity_conflict_type(
            f"Canonical constraint rejected mutation: {error}"
        ) from error
    except BaseException:
        if transaction_started:
            connection.rollback()
        raise

    public_changes: list[AppliedChange] = [
        {
            "record_id": item["record_id"],
            "collection": item["collection"],
            "revision": item["revision"],
            "payload_hash": item["payload_hash"],
            "change_kind": item["change_kind"],
        }
        for item in changed
    ]
    return {
        "status": "applied",
        "watermark": prepared.watermark,
        "mutation_id": mutation_id,
        "entry_hash": entry_hash,
        "changed": public_changes,
    }


def _require_graph_state(value: Mapping[str, object]) -> TransactionGraphState:
    """Validate the facade's dynamic state mapping once at the service boundary."""

    text_fields = (
        "schema_version",
        "store_id",
        "journal_head_hash",
        "journal_anchor_hash",
        "created_at",
        "updated_at",
    )
    integer_fields = (
        "generation",
        "watermark",
        "journal_anchor_watermark",
    )
    for field in text_fields:
        if not isinstance(value.get(field), str):
            raise TypeError(f"Graph state {field} must be a string")
    for field in integer_fields:
        field_value = value.get(field)
        if not isinstance(field_value, int) or isinstance(field_value, bool):
            raise TypeError(f"Graph state {field} must be an integer")
    return cast(TransactionGraphState, dict(value))


def _require_capture_plan(value: Mapping[str, object]) -> CapturePlan:
    """Narrow the already authenticated plan JSON used by mutation preparation."""

    raw_read_set = value.get("read_set", [])
    if not isinstance(raw_read_set, list):
        raise TypeError("Capture plan read_set must be a list")
    read_set: list[PlanReadEntry] = []
    for raw_item in raw_read_set:
        if not isinstance(raw_item, Mapping):
            raise TypeError("Capture plan read-set items must be objects")
        record_id = raw_item.get("record_id")
        if not isinstance(record_id, str):
            raise TypeError("Capture plan record_id must be a string")
        read_set.append({"record_id": record_id})
    return {"read_set": read_set}


def _normalize_request(
    changes: Sequence[RecordChangeLike | Mapping[str, object]],
    *,
    fingerprint: str,
    action: str,
    discovery_kind: str,
    producer_agent_id: str | None,
    task_id: str | None,
    parent_task_id: str | None,
    run_id: str | None,
    based_on_watermark: int | None,
    expected_revisions: Mapping[str, int | None] | None,
    plan_token: str | None,
    idempotency_key: str | None,
    source_ref: str | None,
    capture_policy: Mapping[str, object] | None,
    details: Mapping[str, object] | None,
    redaction_count: int,
    dependencies: TransactionDependencies,
) -> MutationRequest:
    normalized = [dependencies.coerce_change(item) for item in changes]
    if not normalized:
        raise ValueError("At least one changed record is required")
    if len(normalized) > 10_000:
        raise ValueError("A normal mutation is bounded to 10,000 changed records")
    if len({item.record_id for item in normalized}) != len(normalized):
        raise ValueError("A mutation may revise each record at most once")
    normalized_fingerprint = dependencies.normalize_space(fingerprint)
    if not normalized_fingerprint or len(normalized_fingerprint) > 256:
        raise ValueError("A bounded mutation fingerprint is required")
    normalized_action = dependencies.normalize_key(action) or "capture"
    normalized_discovery_kind = dependencies.normalize_key(discovery_kind) or "durable_discovery"
    normalized_idempotency_key = (
        dependencies.normalize_space(idempotency_key)
        or f"{normalized_action}:{normalized_fingerprint}"
    )
    expectations = {
        dependencies.valid_record_id(key): value
        for key, value in (expected_revisions or {}).items()
    }
    normalized_redaction_count = int(redaction_count)
    return MutationRequest(
        changes=normalized,
        fingerprint=normalized_fingerprint,
        action=normalized_action,
        discovery_kind=normalized_discovery_kind,
        producer_agent_id=producer_agent_id,
        task_id=task_id,
        parent_task_id=parent_task_id,
        run_id=run_id,
        based_on_watermark=based_on_watermark,
        expectations=expectations,
        plan_token=plan_token,
        idempotency_key=normalized_idempotency_key,
        source_ref=source_ref,
        capture_policy=capture_policy,
        details=details,
        redaction_count=normalized_redaction_count,
    )


def _duplicate_result(
    connection: sqlite3.Connection,
    *,
    idempotency_key: str,
    fingerprint: str,
    dependencies: TransactionDependencies,
) -> DuplicateMutationResult | None:
    duplicate = connection.execute(
        "SELECT fingerprint,watermark FROM idempotency_keys WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if duplicate is None:
        return None
    if not dependencies.compare_digest(str(duplicate["fingerprint"]), fingerprint):
        raise dependencies.concurrency_conflict_type(
            "Idempotency key was already used for another fingerprint"
        )
    mutation = connection.execute(
        "SELECT mutation_id FROM mutations WHERE watermark=?",
        (duplicate["watermark"],),
    ).fetchone()
    return {
        "status": "duplicate",
        "watermark": cast(int, duplicate["watermark"]),
        "mutation_id": cast(str | None, mutation[0] if mutation else None),
        "changed": [],
    }


def _current_revisions(
    changes: Sequence[RecordChangeLike],
    *,
    dependencies: TransactionDependencies,
) -> CurrentRevisions:
    record_ids = [item.record_id for item in changes]
    current: CurrentRevisions = {}
    for offset in range(0, len(record_ids), MAX_CURRENT_REVISION_QUERY_IDS):
        current.update(
            dependencies.current_revisions_loader(
                record_ids[offset : offset + MAX_CURRENT_REVISION_QUERY_IDS]
            )
        )
    return current


def _prepare_mutation(
    request: MutationRequest,
    *,
    state: TransactionGraphState,
    plan: CapturePlan | None,
    dependencies: TransactionDependencies,
) -> PreparedMutation:
    current = _current_revisions(request.changes, dependencies=dependencies)
    plan_ids = {item["record_id"] for item in plan["read_set"]} if plan else set()
    payloads: dict[str, CanonicalJsonObject] = {}
    revisions: dict[str, int] = {}
    hashes: dict[str, str] = {}
    search_documents: dict[str, str] = {}
    all_refs: dict[str, list[RevisionReference]] = {}
    for change in request.changes:
        record_id = dependencies.valid_record_id(change.record_id)
        if change.collection not in dependencies.collections:
            raise ValueError(f"Unknown collection: {change.collection}")
        actual = current[record_id]
        expected = request.expectations.get(record_id, change.expected_revision)
        if actual is None:
            if expected is not dependencies.unset and expected is not None:
                raise dependencies.concurrency_conflict_type(
                    f"Record {record_id} was expected at revision {expected} but is absent"
                )
            revision = 1
        else:
            if actual["collection"] != change.collection:
                raise dependencies.identity_conflict_type(
                    f"Record {record_id} is already in {actual['collection']}"
                )
            if expected is dependencies.unset and record_id not in plan_ids:
                raise dependencies.concurrency_conflict_type(
                    f"Updating {record_id} requires expected_revision or a plan token"
                )
            if expected is not dependencies.unset and expected != actual["revision"]:
                raise dependencies.concurrency_conflict_type(
                    f"Record {record_id} advanced from expected revision {expected} "
                    f"to {actual['revision']}"
                )
            revision = int(actual["revision"]) + 1
        if not isinstance(change.payload, Mapping):
            raise ValueError(f"Payload for {record_id} must be an object")
        payload: dict[str, object] = copy.deepcopy(dict(change.payload))
        supplied_id = payload.get("id")
        if supplied_id and dependencies.valid_record_id(supplied_id) != record_id:
            raise dependencies.identity_conflict_type("Payload ID disagrees with RecordChange ID")
        payload["id"] = record_id
        if actual and change.collection == "entities":
            previous = dependencies.current_payload_loader(record_id) or {}
            old_name = dependencies.normalize_space(previous.get("name"))
            new_name = dependencies.normalize_space(payload.get("name"))
            if old_name and new_name and old_name != new_name:
                aliases = payload.setdefault("aliases", [])
                if not isinstance(aliases, list):
                    raise ValueError("Entity aliases must be a list")
                if old_name not in aliases:
                    aliases.append(old_name)
        encoded = dependencies.canonical_json(payload)
        if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError(f"Record payload exceeds 2 MiB: {record_id}")
        # Successful canonical serialization is the runtime validation boundary
        # for the recursive JSON-compatible record value.
        canonical_payload = cast(CanonicalJsonObject, payload)
        payloads[record_id] = canonical_payload
        revisions[record_id] = revision
        hashes[record_id] = dependencies.sha256_text(encoded)
        search_documents[record_id] = dependencies.normalize_space(
            change.search_text
            if change.search_text is not None
            else dependencies.payload_search_text(change.collection, canonical_payload)
        )[:32_000]
        refs = dependencies.extract_refs(canonical_payload, change.refs)
        if change.collection == "entities":
            refs.append({"target_id": record_id, "kind": "self", "ordinal": 0})
        all_refs[record_id] = refs
    return PreparedMutation(
        current=current,
        payloads=payloads,
        revisions=revisions,
        hashes=hashes,
        search_documents=search_documents,
        refs=all_refs,
        watermark=int(state["watermark"]) + 1,
        captured_at=dependencies.utc_now(),
    )


def _insert_new_record_identities(
    connection: sqlite3.Connection,
    *,
    request: MutationRequest,
    prepared: PreparedMutation,
    dependencies: TransactionDependencies,
) -> None:
    for change in request.changes:
        record_id = change.record_id
        payload = prepared.payloads[record_id]
        if prepared.current[record_id] is not None:
            continue
        connection.execute(
            """
            INSERT INTO records(
              record_id,collection,current_revision,first_watermark,
              last_watermark,lifecycle,source_tier,sensitive,
              current_payload_hash,search_text,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_id,
                change.collection,
                prepared.revisions[record_id],
                prepared.watermark,
                prepared.watermark,
                dependencies.normalize_space(payload.get("lifecycle")) or None,
                dependencies.normalize_key(payload.get("source_tier")) or None,
                int(bool(payload.get("sensitive"))),
                prepared.hashes[record_id],
                prepared.search_documents[record_id],
                prepared.captured_at,
                prepared.captured_at,
            ),
        )


def _release_current_claim_slots(
    connection: sqlite3.Connection,
    changes: Sequence[RecordChangeLike],
) -> None:
    for change in changes:
        if change.collection == "claims":
            connection.execute(
                "DELETE FROM claim_current_registry WHERE claim_id=?",
                (change.record_id,),
            )


def _write_record_revisions(
    connection: sqlite3.Connection,
    *,
    request: MutationRequest,
    prepared: PreparedMutation,
    dependencies: TransactionDependencies,
) -> list[JournalChange]:
    changed: list[JournalChange] = []
    for ordinal, change in enumerate(request.changes):
        record_id = change.record_id
        payload = prepared.payloads[record_id]
        revision = prepared.revisions[record_id]
        connection.execute(
            """
            INSERT INTO record_revision_headers(
              record_id,revision,watermark,change_kind,discovery_kind,
              payload_hash,producer_agent_id,task_id,parent_task_id,run_id,
              based_on_watermark,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_id,
                revision,
                prepared.watermark,
                dependencies.normalize_key(change.change_kind) or "upsert",
                request.discovery_kind,
                prepared.hashes[record_id],
                dependencies.normalize_space(request.producer_agent_id) or None,
                dependencies.normalize_space(request.task_id) or None,
                dependencies.normalize_space(request.parent_task_id) or None,
                dependencies.normalize_space(request.run_id) or None,
                request.based_on_watermark,
                prepared.captured_at,
            ),
        )
        connection.execute(
            "INSERT INTO record_revision_payloads(record_id,revision,payload_json) VALUES(?,?,?)",
            (record_id, revision, dependencies.canonical_json(payload)),
        )
        for ref in prepared.refs[record_id]:
            connection.execute(
                "INSERT INTO revision_refs "
                "(record_id,revision,target_record_id,ref_kind,ordinal) "
                "VALUES(?,?,?,?,?)",
                (
                    record_id,
                    revision,
                    ref["target_id"],
                    ref["kind"],
                    ref["ordinal"],
                ),
            )
        if prepared.current[record_id] is not None:
            connection.execute(
                """
                UPDATE records SET current_revision=?,last_watermark=?,
                  lifecycle=?,source_tier=?,sensitive=?,current_payload_hash=?,
                  search_text=?,updated_at=? WHERE record_id=?
                """,
                (
                    revision,
                    prepared.watermark,
                    dependencies.normalize_space(payload.get("lifecycle")) or None,
                    dependencies.normalize_key(payload.get("source_tier")) or None,
                    int(bool(payload.get("sensitive"))),
                    prepared.hashes[record_id],
                    prepared.search_documents[record_id],
                    prepared.captured_at,
                    record_id,
                ),
            )
        if change.collection == "entities":
            dependencies.apply_entity_registry(
                record_id,
                payload,
                prepared.watermark,
                pending_payloads=prepared.payloads,
            )
        if change.collection == "claim_surfaces":
            dependencies.apply_surface_registry(record_id, payload, prepared.watermark)
        dependencies.apply_semantic_registry(
            record_id,
            change.collection,
            change.semantic_keys,
            prepared.watermark,
        )
        rowid_row = connection.execute(
            "SELECT rowid FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        if rowid_row is None:
            raise RuntimeError(f"New record identity disappeared during mutation: {record_id}")
        rowid = rowid_row[0]
        connection.execute("DELETE FROM records_fts WHERE rowid=?", (rowid,))
        connection.execute(
            "INSERT INTO records_fts(rowid,record_id,collection,search_text) VALUES(?,?,?,?)",
            (
                rowid,
                record_id,
                change.collection,
                prepared.search_documents[record_id],
            ),
        )
        changed.append(
            {
                "record_id": record_id,
                "collection": change.collection,
                "revision": revision,
                "payload_hash": prepared.hashes[record_id],
                "change_kind": dependencies.normalize_key(change.change_kind) or "upsert",
                "ordinal": ordinal,
            }
        )
        if (
            dependencies.normalize_key(change.change_kind) == "redact"
            and dependencies.normalize_key(payload.get("lifecycle")) == "redacted"
        ):
            connection.execute(
                "DELETE FROM record_revision_payloads WHERE record_id=? AND revision<?",
                (record_id, revision),
            )
    return changed


def _reconcile_current_claims(
    connection: sqlite3.Connection,
    *,
    changes: Sequence[RecordChangeLike],
    prepared: PreparedMutation,
    dependencies: TransactionDependencies,
) -> None:
    for change in changes:
        if change.collection != "claims":
            continue
        record_id = change.record_id
        payload = prepared.payloads[record_id]
        if not (
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
            raise dependencies.identity_conflict_type(
                f"Current claim {record_id} references an unknown surface {surface_id}"
            )
        owner = connection.execute(
            "SELECT claim_id FROM claim_current_registry WHERE surface_id=?",
            (surface_id,),
        ).fetchone()
        if owner and owner[0] != record_id:
            raise dependencies.identity_conflict_type(
                f"Claim surface {surface_id} already has current claim {owner[0]}"
            )
        connection.execute(
            "INSERT INTO claim_current_registry(surface_id,claim_id,updated_watermark) "
            "VALUES(?,?,?) ON CONFLICT(surface_id) DO UPDATE SET "
            "claim_id=excluded.claim_id,updated_watermark=excluded.updated_watermark",
            (surface_id, record_id, prepared.watermark),
        )


def _publish_mutation_journal(
    connection: sqlite3.Connection,
    *,
    request: MutationRequest,
    prepared: PreparedMutation,
    changed: list[JournalChange],
    state: TransactionGraphState,
    dependencies: TransactionDependencies,
) -> tuple[str, str]:
    entry_material = {
        "watermark": prepared.watermark,
        "action": request.action,
        "discovery_kind": request.discovery_kind,
        "fingerprint": request.fingerprint,
        "fingerprint_version": "v4",
        "producer_agent_id": dependencies.normalize_space(request.producer_agent_id) or None,
        "task_id": dependencies.normalize_space(request.task_id) or None,
        "parent_task_id": dependencies.normalize_space(request.parent_task_id) or None,
        "run_id": dependencies.normalize_space(request.run_id) or None,
        "based_on_watermark": request.based_on_watermark,
        "captured_at": prepared.captured_at,
        "source_ref": dependencies.normalize_space(request.source_ref) or None,
        "capture_policy": dict(request.capture_policy) if request.capture_policy else None,
        "details": dict(request.details) if request.details else None,
        "redaction_count": request.redaction_count,
        "changes": changed,
    }
    previous_hash = str(state["journal_head_hash"])
    entry_hash = dependencies.stable_hash({"previous_hash": previous_hash, "entry": entry_material})
    mutation_id = (
        "mutation_"
        + dependencies.stable_hash(
            {
                "watermark": prepared.watermark,
                "fingerprint": request.fingerprint,
                "entry_hash": entry_hash,
            }
        )[:32]
    )
    connection.execute(
        """
        INSERT INTO mutations(
          watermark,mutation_id,action,discovery_kind,fingerprint,
          fingerprint_version,producer_agent_id,task_id,parent_task_id,
          run_id,based_on_watermark,captured_at,previous_entry_hash,
          entry_hash,hash_algorithm,source_ref,capture_policy_json,
          details_json,legacy_header_json,redaction_count
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
        """,
        (
            prepared.watermark,
            mutation_id,
            request.action,
            request.discovery_kind,
            request.fingerprint,
            "v4",
            dependencies.normalize_space(request.producer_agent_id) or None,
            dependencies.normalize_space(request.task_id) or None,
            dependencies.normalize_space(request.parent_task_id) or None,
            dependencies.normalize_space(request.run_id) or None,
            request.based_on_watermark,
            prepared.captured_at,
            previous_hash,
            entry_hash,
            dependencies.hash_algorithm,
            dependencies.normalize_space(request.source_ref) or None,
            (
                dependencies.canonical_json(dict(request.capture_policy))
                if request.capture_policy
                else None
            ),
            (dependencies.canonical_json(dict(request.details)) if request.details else None),
            request.redaction_count,
        ),
    )
    for item in changed:
        connection.execute(
            """
            INSERT INTO mutation_changes(
              watermark,ordinal,record_id,collection,revision,
              change_kind,payload_hash
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                prepared.watermark,
                item["ordinal"],
                item["record_id"],
                item["collection"],
                item["revision"],
                item["change_kind"],
                item["payload_hash"],
            ),
        )
    connection.execute(
        "INSERT INTO idempotency_keys(idempotency_key,fingerprint,watermark) VALUES(?,?,?)",
        (request.idempotency_key, request.fingerprint, prepared.watermark),
    )
    connection.execute(
        "UPDATE graph_state SET committed_watermark=?,journal_head_hash=?,"
        "updated_at=? WHERE singleton=1",
        (prepared.watermark, entry_hash, prepared.captured_at),
    )
    return mutation_id, entry_hash


__all__ = ["TransactionDependencies", "apply_changes"]
