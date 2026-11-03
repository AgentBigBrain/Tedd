"""Identity registries and reviewed-plan policy for the v4 store.

The public :mod:`knowledge_graph_v4` module remains the compatibility facade.
It resolves live callbacks for every invocation so tests and downstream code
can continue to patch facade policy without this service importing the facade
back into itself.

Registry writers intentionally do not open or commit transactions.  The
mutation coordinator owns that boundary and invokes them inside the same
transaction as revision and journal updates.  Identity-scheme registration is
the one standalone registry operation and therefore owns its short transaction.
"""

from __future__ import annotations

import copy
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypedDict, cast


class CurrentRevision(TypedDict):
    revision: int
    payload_hash: str


class IdentitySchemeResult(TypedDict):
    scheme: str
    normalization: str
    governed: bool


class CurrentRevisionsLoader(Protocol):
    def __call__(self, record_ids: Iterable[str]) -> Mapping[str, Mapping[str, object] | None]: ...


class ConstraintOwner(Protocol):
    def __call__(self, constraint: Mapping[str, object]) -> str | None: ...


class TokenSigner(Protocol):
    def __call__(self, payload: Mapping[str, object], key: bytes | str) -> str: ...


class TokenVerifier(Protocol):
    def __call__(self, token: str, key: bytes | str) -> dict[str, object]: ...


class JsonLoader(Protocol):
    def __call__(self, payload: str) -> object: ...


class CurrentPayloadLoader(Protocol):
    def __call__(self, record_id: str) -> Mapping[str, object] | None: ...


@dataclass(frozen=True)
class ConstraintDependencies:
    """Live lookup policy used to resolve one uniqueness predicate."""

    normalize_key: Callable[[object], str]
    semantic_key_owner: Callable[[str, str, str], str | None]
    plan_token_error_type: type[Exception]


@dataclass(frozen=True)
class PlanDependencies:
    """Live facade callbacks used by plan creation and verification."""

    valid_record_id: Callable[[object], str]
    normalize_space: Callable[[object], str]
    utc_now: Callable[[], str]
    current_revisions_loader: CurrentRevisionsLoader
    state_loader: Callable[[], Mapping[str, object]]
    constraint_owner: ConstraintOwner
    require_token_key: Callable[[], bytes]
    sign_plan_token: TokenSigner
    verify_plan_token: TokenVerifier
    compare_digest: Callable[[str, str], bool]
    plan_token_error_type: type[Exception]
    concurrency_conflict_type: type[Exception]


@dataclass(frozen=True)
class RegistryDependencies:
    """Normalization and conflict policy for registry writes."""

    normalize_key: Callable[[object], str]
    normalize_space: Callable[[object], str]
    stable_hash: Callable[[object], str]
    valid_record_id: Callable[[object], str]
    current_payload_loader: CurrentPayloadLoader
    identity_conflict_type: type[Exception]


def constraint_owner(
    connection: sqlite3.Connection,
    constraint: Mapping[str, object],
    *,
    dependencies: ConstraintDependencies,
) -> str | None:
    """Return the current owner of a reviewed uniqueness predicate."""

    kind = dependencies.normalize_key(constraint.get("kind"))
    if kind == "semantic_owner":
        return dependencies.semantic_key_owner(
            str(constraint.get("collection")),
            str(constraint.get("key_kind") or "semantic"),
            str(constraint.get("key") or ""),
        )
    if kind == "identity_owner":
        scheme = dependencies.normalize_key(constraint.get("scheme"))
        namespace = dependencies.normalize_key(constraint.get("namespace")) or "global"
        raw_value = unicodedata.normalize("NFC", str(constraint.get("value") or "").strip())
        scheme_row = connection.execute(
            "SELECT normalization FROM identity_schemes WHERE scheme=?", (scheme,)
        ).fetchone()
        value_key = (
            raw_value.casefold()
            if scheme_row is not None and scheme_row[0] == "casefold"
            else raw_value
        )
        row = connection.execute(
            "SELECT entity_id FROM entity_identity_keys "
            "WHERE scheme=? AND namespace=? AND value_key=?",
            (scheme, namespace, value_key),
        ).fetchone()
        return str(row[0]) if row is not None else None
    if kind == "surface_signature_owner":
        row = connection.execute(
            "SELECT surface_id FROM claim_surface_constraints "
            "WHERE family=? AND key_name=? AND subject_signature=?",
            (
                dependencies.normalize_key(constraint.get("family")),
                dependencies.normalize_key(constraint.get("key")),
                str(constraint.get("subject_signature") or ""),
            ),
        ).fetchone()
        return str(row[0]) if row is not None else None
    raise dependencies.plan_token_error_type(f"Unsupported plan constraint kind: {kind!r}")


def create_plan_token(
    record_ids: Iterable[str],
    fingerprint: str,
    *,
    constraint_expectations: Sequence[Mapping[str, object]] = (),
    dependencies: PlanDependencies,
) -> str:
    """Sign current record revisions and relevant absence predicates."""

    ids = list(dict.fromkeys(dependencies.valid_record_id(item) for item in record_ids))
    if len(ids) > 2_000 or len(constraint_expectations) > 2_000:
        raise dependencies.plan_token_error_type("Plan read sets are bounded to 2,000 items")
    revisions = dependencies.current_revisions_loader(ids)
    constraints: list[dict[str, object]] = []
    for raw in constraint_expectations:
        item = copy.deepcopy(dict(raw))
        actual = dependencies.constraint_owner(item)
        if "expected_owner" in item and item["expected_owner"] != actual:
            raise dependencies.concurrency_conflict_type(
                "Constraint already differs from requested expectation"
            )
        item["expected_owner"] = actual
        constraints.append(item)
    state = dependencies.state_loader()
    read_set: list[dict[str, object]] = []
    for record_id in ids:
        revision = revisions[record_id]
        read_set.append(
            {
                "record_id": record_id,
                "revision": revision["revision"] if revision is not None else None,
                "payload_hash": (revision["payload_hash"] if revision is not None else None),
            }
        )
    payload: dict[str, object] = {
        "type": "capture_plan",
        "version": 1,
        "store_id": state["store_id"],
        "generation": state["generation"],
        "basis_watermark": state["watermark"],
        "fingerprint": dependencies.normalize_space(fingerprint),
        "read_set": read_set,
        "constraints": constraints,
        "created_at": dependencies.utc_now(),
    }
    return dependencies.sign_plan_token(payload, dependencies.require_token_key())


def verify_capture_plan(
    token: str,
    fingerprint: str,
    state: Mapping[str, object],
    *,
    dependencies: PlanDependencies,
) -> dict[str, object]:
    """Validate a reviewed plan against current revisions and constraints."""

    payload = dependencies.verify_plan_token(token, dependencies.require_token_key())
    if payload.get("type") != "capture_plan" or payload.get("version") != 1:
        raise dependencies.plan_token_error_type("Token is not a supported capture plan")
    if payload.get("store_id") != state["store_id"]:
        raise dependencies.plan_token_error_type("Capture plan belongs to another store")
    if payload.get("generation") != state["generation"]:
        raise dependencies.plan_token_error_type(
            "Capture plan was invalidated by restore or migration"
        )
    expected_fingerprint = dependencies.normalize_space(fingerprint)
    if not dependencies.compare_digest(str(payload.get("fingerprint") or ""), expected_fingerprint):
        raise dependencies.plan_token_error_type(
            "Capture plan belongs to another packet fingerprint"
        )
    read_set_value = payload.get("read_set", [])
    if not isinstance(read_set_value, list) or len(read_set_value) > 2_000:
        raise dependencies.plan_token_error_type("Capture plan read set is invalid")
    read_set = cast(Sequence[object], read_set_value)
    current = dependencies.current_revisions_loader(
        str(item.get("record_id")) for item in read_set if isinstance(item, Mapping)
    )
    for raw_item in read_set:
        if not isinstance(raw_item, Mapping):
            raise dependencies.plan_token_error_type("Capture plan read-set item is invalid")
        item = cast(Mapping[str, object], raw_item)
        record_id = dependencies.valid_record_id(item.get("record_id"))
        actual = current[record_id]
        actual_revision = actual["revision"] if actual is not None else None
        actual_hash = actual["payload_hash"] if actual is not None else None
        if item.get("revision") != actual_revision or item.get("payload_hash") != actual_hash:
            raise dependencies.concurrency_conflict_type(
                f"Relevant record changed after review: {record_id}"
            )
    constraints_value = payload.get("constraints", [])
    if not isinstance(constraints_value, list) or len(constraints_value) > 2_000:
        raise dependencies.plan_token_error_type("Capture plan constraints are invalid")
    constraints = cast(Sequence[object], constraints_value)
    for raw_item in constraints:
        if not isinstance(raw_item, Mapping):
            raise dependencies.plan_token_error_type("Capture plan constraint is invalid")
        item = cast(Mapping[str, object], raw_item)
        if dependencies.constraint_owner(item) != item.get("expected_owner"):
            raise dependencies.concurrency_conflict_type(
                f"Relevant uniqueness predicate changed: {item.get('kind')}"
            )
    return payload


def register_identity_scheme(
    connection: sqlite3.Connection,
    scheme: str,
    *,
    normalization: str = "binary",
    governed: bool = False,
    state_loader: Callable[[], Mapping[str, object]],
    normalize_key: Callable[[object], str],
    identity_conflict_type: type[Exception],
) -> IdentitySchemeResult:
    """Register one immutable identity-key normalization policy."""

    scheme = normalize_key(scheme)
    if not scheme:
        raise ValueError("Identity scheme is required")
    if normalization not in {"binary", "casefold"}:
        raise ValueError("Identity normalization must be binary or casefold")
    with connection:
        state = state_loader()
        existing = connection.execute(
            "SELECT normalization, governed FROM identity_schemes WHERE scheme=?",
            (scheme,),
        ).fetchone()
        if existing is not None and (
            existing["normalization"] != normalization
            or bool(existing["governed"]) != bool(governed)
        ):
            raise identity_conflict_type(f"Identity scheme {scheme!r} is immutable once registered")
        connection.execute(
            "INSERT OR IGNORE INTO identity_schemes "
            "(scheme,normalization,governed,created_watermark) VALUES(?,?,?,?)",
            (scheme, normalization, int(governed), state["watermark"]),
        )
    return {
        "scheme": scheme,
        "normalization": normalization,
        "governed": bool(governed),
    }


def current_payload(
    connection: sqlite3.Connection,
    record_id: str,
    *,
    json_loads: JsonLoader,
) -> dict[str, object] | None:
    """Load the current object payload used by cross-record constraints."""

    row = connection.execute(
        """
        SELECT p.payload_json FROM records r
        JOIN record_revision_payloads p
          ON p.record_id=r.record_id AND p.revision=r.current_revision
        WHERE r.record_id=?
        """,
        (record_id,),
    ).fetchone()
    if row is None:
        return None
    decoded = json_loads(str(row[0]))
    if not isinstance(decoded, Mapping):
        raise ValueError("Current record payload must be an object")
    raw_mapping = cast(Mapping[object, object], decoded)
    if not all(isinstance(key, str) for key in raw_mapping):
        raise ValueError("Current record payload keys must be strings")
    return {str(key): value for key, value in raw_mapping.items()}


def apply_entity_registry(
    connection: sqlite3.Connection,
    record_id: str,
    payload: Mapping[str, object],
    watermark: int,
    pending_payloads: Mapping[str, Mapping[str, object]] | None = None,
    *,
    dependencies: RegistryDependencies,
) -> None:
    """Apply entity aliases, identity keys, and immutable redirects."""

    namespace = dependencies.normalize_key(payload.get("namespace")) or "global"
    labels: list[tuple[str, str]] = [("name", dependencies.normalize_space(payload.get("name")))]
    aliases_value = payload.get("aliases", [])
    if aliases_value is not None and not isinstance(aliases_value, list):
        raise ValueError("Entity aliases must be a list")
    aliases = cast(Sequence[object], aliases_value) if aliases_value is not None else ()
    labels.extend(("alias", dependencies.normalize_space(item)) for item in aliases)
    for kind, display in labels:
        norm = dependencies.normalize_key(display)
        if not norm:
            continue
        connection.execute(
            """
            INSERT INTO entity_aliases(
              entity_id,namespace,label_norm,label_display,label_kind,
              first_watermark,last_watermark
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(entity_id,label_norm,label_kind) DO UPDATE SET
              label_display=excluded.label_display,
              last_watermark=excluded.last_watermark
            """,
            (record_id, namespace, norm, display, kind, watermark, watermark),
        )
    identities_value = payload.get("identity_keys", [])
    if identities_value is None:
        identities_value = []
    if not isinstance(identities_value, list):
        raise ValueError("Entity identity_keys must be a list")
    identities = cast(Sequence[object], identities_value)
    for raw_identity in identities:
        if not isinstance(raw_identity, Mapping):
            raise ValueError("Each identity key must be an object")
        raw = cast(Mapping[str, object], raw_identity)
        scheme = dependencies.normalize_key(raw.get("scheme"))
        key_namespace = dependencies.normalize_key(raw.get("namespace")) or namespace
        value_display = unicodedata.normalize("NFC", str(raw.get("value") or "").strip())
        if not scheme or not value_display:
            raise ValueError("Identity scheme and value are required")
        scheme_row = connection.execute(
            "SELECT normalization FROM identity_schemes WHERE scheme=?", (scheme,)
        ).fetchone()
        if scheme_row is None:
            connection.execute(
                "INSERT INTO identity_schemes "
                "(scheme,normalization,governed,created_watermark) VALUES(?,?,0,?)",
                (scheme, "binary", watermark),
            )
            normalization = "binary"
        else:
            normalization = str(scheme_row[0])
        value_key = value_display.casefold() if normalization == "casefold" else value_display
        owner = connection.execute(
            "SELECT entity_id FROM entity_identity_keys "
            "WHERE scheme=? AND namespace=? AND value_key=?",
            (scheme, key_namespace, value_key),
        ).fetchone()
        if owner is not None and owner[0] != record_id:
            raise dependencies.identity_conflict_type(
                f"Identity {scheme}:{key_namespace}:{value_display} belongs to {owner[0]}"
            )
        connection.execute(
            """
            INSERT INTO entity_identity_keys(
              scheme,namespace,value_key,value_display,entity_id,
              first_watermark,last_watermark,status
            ) VALUES(?,?,?,?,?,?,?,'active')
            ON CONFLICT(scheme,namespace,value_key) DO UPDATE SET
              value_display=excluded.value_display,
              last_watermark=excluded.last_watermark
            """,
            (
                scheme,
                key_namespace,
                value_key,
                value_display,
                record_id,
                watermark,
                watermark,
            ),
        )
    if payload.get("lifecycle") != "redirected" or not payload.get("redirect_to"):
        return
    target = dependencies.valid_record_id(payload.get("redirect_to"))
    reason_source = dependencies.valid_record_id(payload.get("redirect_source_ref"))
    target_row = connection.execute(
        "SELECT collection FROM records WHERE record_id=?", (target,)
    ).fetchone()
    reason_row = connection.execute(
        "SELECT collection FROM records WHERE record_id=?", (reason_source,)
    ).fetchone()
    if target_row is None or target_row[0] != "entities":
        raise dependencies.identity_conflict_type("Entity redirect target must be an entity")
    if reason_row is None or reason_row[0] != "sources":
        raise dependencies.identity_conflict_type(
            "Entity redirect requires a canonical source record"
        )
    target_payload = (
        dict(pending_payloads[target])
        if pending_payloads is not None and target in pending_payloads
        else dependencies.current_payload_loader(target)
    )
    if target_payload is None:
        raise dependencies.identity_conflict_type("Entity redirect target has no current payload")
    source_signature = (
        dependencies.normalize_key(payload.get("type")),
        dependencies.normalize_key(payload.get("namespace")) or "global",
    )
    target_signature = (
        dependencies.normalize_key(target_payload.get("type")),
        dependencies.normalize_key(target_payload.get("namespace")) or "global",
    )
    if source_signature != target_signature:
        raise dependencies.identity_conflict_type(
            "Entity redirects require matching immutable type and namespace"
        )
    cycle = connection.execute(
        """
        WITH RECURSIVE path(id) AS (
          SELECT ?
          UNION ALL
          SELECT er.target_entity_id FROM entity_redirects er
          JOIN path ON er.source_entity_id=path.id
        ) SELECT 1 FROM path WHERE id=? LIMIT 1
        """,
        (target, record_id),
    ).fetchone()
    if cycle is not None:
        raise dependencies.identity_conflict_type("Entity redirect would form a cycle")
    existing = connection.execute(
        "SELECT target_entity_id FROM entity_redirects WHERE source_entity_id=?",
        (record_id,),
    ).fetchone()
    if existing is not None and existing[0] != target:
        raise dependencies.identity_conflict_type("An entity redirect cannot be retargeted")
    connection.execute(
        "INSERT OR IGNORE INTO entity_redirects "
        "(source_entity_id,target_entity_id,reason_source_id,created_watermark) "
        "VALUES(?,?,?,?)",
        (record_id, target, reason_source, watermark),
    )


def apply_surface_registry(
    connection: sqlite3.Connection,
    record_id: str,
    payload: Mapping[str, object],
    watermark: int,
    *,
    dependencies: RegistryDependencies,
) -> None:
    """Apply an immutable claim-surface signature and its ordered subjects."""

    family = dependencies.normalize_key(payload.get("family"))
    key_name = dependencies.normalize_key(payload.get("key"))
    subjects_value = payload.get("subject_refs", [])
    if not isinstance(subjects_value, list) or not subjects_value:
        raise dependencies.identity_conflict_type("Claim surfaces require nonempty subject_refs")
    subjects_raw = cast(Sequence[object], subjects_value)
    subjects = sorted(dict.fromkeys(dependencies.valid_record_id(item) for item in subjects_raw))
    signature = dependencies.stable_hash(subjects)
    cardinality = dependencies.normalize_key(payload.get("cardinality")) or "single"
    existing = connection.execute(
        "SELECT family,key_name,subject_signature,cardinality "
        "FROM claim_surface_constraints WHERE surface_id=?",
        (record_id,),
    ).fetchone()
    expected = (family, key_name, signature, cardinality)
    existing_values = (
        tuple(str(existing[index]) for index in range(4)) if existing is not None else None
    )
    if existing_values is not None and existing_values != expected:
        raise dependencies.identity_conflict_type(
            "A claim surface's semantic identity is immutable"
        )
    owner = connection.execute(
        "SELECT surface_id FROM claim_surface_constraints "
        "WHERE family=? AND key_name=? AND subject_signature=?",
        (family, key_name, signature),
    ).fetchone()
    if owner is not None and owner[0] != record_id:
        raise dependencies.identity_conflict_type(
            f"Claim surface signature already belongs to {owner[0]}"
        )
    connection.execute(
        "INSERT OR IGNORE INTO claim_surface_constraints "
        "(surface_id,family,key_name,subject_signature,cardinality,created_watermark) "
        "VALUES(?,?,?,?,?,?)",
        (record_id, family, key_name, signature, cardinality, watermark),
    )
    for ordinal, entity_id in enumerate(subjects):
        connection.execute(
            "INSERT OR IGNORE INTO claim_surface_subjects "
            "(surface_id,entity_id,ordinal) VALUES(?,?,?)",
            (record_id, entity_id, ordinal),
        )


def apply_semantic_registry(
    connection: sqlite3.Connection,
    record_id: str,
    collection: str,
    semantic_keys: Sequence[object],
    watermark: int,
    *,
    dependencies: RegistryDependencies,
) -> None:
    """Apply collection-scoped semantic uniqueness keys."""

    for raw_key in semantic_keys:
        if isinstance(raw_key, Mapping):
            raw = cast(Mapping[str, object], raw_key)
            kind = dependencies.normalize_key(raw.get("kind")) or "semantic"
            display = dependencies.normalize_space(raw.get("key"))
        else:
            kind = "semantic"
            display = dependencies.normalize_space(raw_key)
        if not display:
            raise ValueError("Semantic registry keys cannot be empty")
        key_hash = dependencies.stable_hash(display)
        owner = connection.execute(
            "SELECT record_id FROM semantic_registry "
            "WHERE collection=? AND key_kind=? AND key_hash=?",
            (collection, kind, key_hash),
        ).fetchone()
        if owner is not None and owner[0] != record_id:
            raise dependencies.identity_conflict_type(
                f"Semantic key {collection}:{kind} belongs to {owner[0]}"
            )
        connection.execute(
            "INSERT OR IGNORE INTO semantic_registry "
            "(collection,key_kind,key_hash,key_display,record_id,created_watermark) "
            "VALUES(?,?,?,?,?,?)",
            (collection, kind, key_hash, display, record_id, watermark),
        )
