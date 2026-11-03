"""Consumer, subscription, delivery, and acknowledgement services for v4.

The public storage facade supplies its live callbacks for each invocation.
This module keeps ownership checks, subscription transactions, cursor delivery,
and context-pack reads cohesive without importing the facade back into itself.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from tedd_knowledge_graph.v4_feed import (
    JsonLoader,
    MutationFeedResult,
    Selector,
    SelectorNormalizer,
    SqlParameter,
    StateLoader,
    TokenSigner,
    TokenVerifier,
)
from tedd_knowledge_graph.v4_queries import JsonValue


class MutationFeedLoader(Protocol):
    def __call__(
        self,
        since_watermark: int,
        *,
        through_watermark: int | None = None,
        cursor: str | None = None,
        limit: int = 100,
        selector: Mapping[str, object] | None = None,
    ) -> MutationFeedResult: ...


class SubscriptionLoader(Protocol):
    def __call__(self, subscription_id: str) -> sqlite3.Row: ...


@dataclass(frozen=True)
class ConsumerDependencies:
    """Policy used by persisted consumer ownership operations."""

    max_status_subscriptions: int
    valid_record_id: Callable[[object], str]
    normalize_space: Callable[[object], str]
    canonical_json: Callable[[object], str]
    json_loads: JsonLoader
    utc_now: Callable[[], str]
    subscription_error_type: type[Exception]


@dataclass(frozen=True)
class SubscriptionDependencies:
    """Policy used to create one immutable selector version."""

    valid_record_id: Callable[[object], str]
    selector_normalizer: SelectorNormalizer
    stable_hash: Callable[[object], str]
    state_loader: StateLoader
    canonical_json: Callable[[object], str]
    utc_now: Callable[[], str]
    subscription_error_type: type[Exception]


@dataclass(frozen=True)
class InboxDependencies:
    """Live collaborators required to turn a subscription into a delivery."""

    json_loads: JsonLoader
    mutation_feed: MutationFeedLoader
    state_loader: StateLoader
    sign_plan_token: TokenSigner
    require_token_key: Callable[[], bytes]


@dataclass(frozen=True)
class AcknowledgementDependencies:
    """Live collaborators required for monotonic safe acknowledgement."""

    state_loader: StateLoader
    subscription_loader: SubscriptionLoader
    verify_plan_token: TokenVerifier
    require_token_key: Callable[[], bytes]
    utc_now: Callable[[], str]
    subscription_error_type: type[Exception]


@dataclass(frozen=True)
class ContextPackDependencies:
    """Limits and read callbacks for one bounded context snapshot."""

    max_context_items: int
    max_context_chars: int
    json_loads: JsonLoader
    watermark_loader: Callable[[], int]


def consumer_status(
    connection: sqlite3.Connection,
    consumer_id: str,
    *,
    dependencies: ConsumerDependencies,
) -> dict[str, object]:
    """Inspect one persisted consumer owner and its active cursors read-only."""

    consumer_id = dependencies.valid_record_id(consumer_id)
    rows = connection.execute(
        "SELECT c.agent_id,c.task_id,c.parent_task_id,"
        "s.subscription_id,s.version,s.last_ack_watermark,"
        "COUNT(s.subscription_id) OVER () AS active_subscription_count "
        "FROM consumers c LEFT JOIN subscriptions s "
        "ON s.consumer_id=c.consumer_id AND s.active=1 "
        "WHERE c.consumer_id=? ORDER BY s.version,s.subscription_id LIMIT ?",
        (consumer_id, dependencies.max_status_subscriptions),
    ).fetchall()
    if not rows:
        return {
            "status": "not_found",
            "found": False,
            "consumer_id": consumer_id,
            "owner": None,
            "active_subscription_count": 0,
            "active_subscriptions": [],
            "active_subscriptions_truncated": False,
        }

    consumer = rows[0]
    active_subscription_count = int(consumer["active_subscription_count"])
    active_subscriptions: list[dict[str, object]] = [
        {
            "subscription_id": cast(str, row["subscription_id"]),
            "version": int(row["version"]),
            "last_ack_watermark": int(row["last_ack_watermark"]),
        }
        for row in rows
        if row["subscription_id"] is not None
    ]
    return {
        "status": "found",
        "found": True,
        "consumer_id": consumer_id,
        "owner": {
            "agent_id": consumer["agent_id"],
            "task_id": consumer["task_id"],
            "parent_task_id": consumer["parent_task_id"],
        },
        "active_subscription_count": active_subscription_count,
        "active_subscriptions": active_subscriptions,
        "active_subscriptions_truncated": (
            active_subscription_count > dependencies.max_status_subscriptions
        ),
    }


def register_consumer(
    connection: sqlite3.Connection,
    consumer_id: str,
    *,
    agent_id: str,
    task_id: str,
    parent_task_id: str | None = None,
    metadata: Mapping[str, object] | None = None,
    dependencies: ConsumerDependencies,
) -> dict[str, object]:
    """Atomically create or refresh one consumer without changing its owner."""

    consumer_id = dependencies.valid_record_id(consumer_id)
    agent_id = dependencies.normalize_space(agent_id)
    task_id = dependencies.normalize_space(task_id)
    if not agent_id or not task_id:
        raise dependencies.subscription_error_type("Consumer agent_id and task_id are required")
    requested_parent_task_id = dependencies.normalize_space(parent_task_id) or None
    now = dependencies.utc_now()
    existing: sqlite3.Row | None
    try:
        # Claim the writer slot before inspecting ownership. Otherwise two
        # first registrations can both observe absence and return mutually
        # inconsistent ownership even though only one row survives.
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT agent_id,task_id,parent_task_id,created_at FROM consumers WHERE consumer_id=?",
            (consumer_id,),
        ).fetchone()
        if existing and (existing["agent_id"], existing["task_id"]) != (agent_id, task_id):
            raise dependencies.subscription_error_type(
                "Consumer ID is already owned by another agent/task"
            )
        resolved_parent_task_id = requested_parent_task_id
        if resolved_parent_task_id is None and existing is not None:
            resolved_parent_task_id = cast(str | None, existing["parent_task_id"])
        connection.execute(
            """
            INSERT INTO consumers(
              consumer_id,agent_id,task_id,parent_task_id,created_at,last_seen_at,metadata_json
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(consumer_id) DO UPDATE SET
              parent_task_id=excluded.parent_task_id,
              last_seen_at=excluded.last_seen_at,
              metadata_json=excluded.metadata_json
            """,
            (
                consumer_id,
                agent_id,
                task_id,
                resolved_parent_task_id,
                existing["created_at"] if existing else now,
                now,
                dependencies.canonical_json(dict(metadata)) if metadata else None,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return {
        "consumer_id": consumer_id,
        "agent_id": agent_id,
        "task_id": task_id,
        "parent_task_id": resolved_parent_task_id,
        "created_at": existing["created_at"] if existing else now,
        "last_seen_at": now,
        "metadata": dict(metadata) if metadata else None,
    }


def rebind_consumer(
    connection: sqlite3.Connection,
    consumer_id: str,
    *,
    task_id: str,
    expected_old_agent_id: str,
    new_agent_id: str,
    parent_task_id: str | None = None,
    metadata: Mapping[str, object] | None = None,
    dependencies: ConsumerDependencies,
) -> dict[str, object]:
    """Move a task consumer to a new agent without changing its cursors."""

    consumer_id = dependencies.valid_record_id(consumer_id)
    task_id = dependencies.normalize_space(task_id)
    expected_old_agent_id = dependencies.normalize_space(expected_old_agent_id)
    new_agent_id = dependencies.normalize_space(new_agent_id)
    requested_parent_task_id = dependencies.normalize_space(parent_task_id) or None
    if not task_id or not expected_old_agent_id or not new_agent_id:
        raise dependencies.subscription_error_type(
            "Consumer task_id, expected_old_agent_id, and new_agent_id are required"
        )

    requested_metadata = dict(metadata) if metadata is not None else None
    now = dependencies.utc_now()
    existing: sqlite3.Row | None
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT agent_id,task_id,parent_task_id,created_at,last_seen_at,metadata_json "
            "FROM consumers WHERE consumer_id=?",
            (consumer_id,),
        ).fetchone()
        if existing is None:
            raise dependencies.subscription_error_type(f"Unknown consumer: {consumer_id}")
        if existing["task_id"] != task_id:
            raise dependencies.subscription_error_type(
                "Consumer task ownership does not match the existing task_id"
            )

        current_agent_id = str(existing["agent_id"])
        if current_agent_id == new_agent_id:
            status = "already_bound"
        elif current_agent_id == expected_old_agent_id:
            status = "rebound"
        else:
            raise dependencies.subscription_error_type(
                "Consumer agent ownership does not match the expected old or requested new agent"
            )

        merged_metadata: dict[str, object] = {}
        if existing["metadata_json"]:
            decoded = dependencies.json_loads(cast(str, existing["metadata_json"]))
            if not isinstance(decoded, Mapping):
                raise dependencies.subscription_error_type("Consumer metadata is not a JSON object")
            merged_metadata.update(cast(Mapping[str, object], decoded))
        if requested_metadata is not None:
            merged_metadata.update(requested_metadata)
        metadata_json = dependencies.canonical_json(merged_metadata) if merged_metadata else None
        resolved_parent_task_id = (
            requested_parent_task_id
            if requested_parent_task_id is not None
            else cast(str | None, existing["parent_task_id"])
        )

        updated = connection.execute(
            "UPDATE consumers SET agent_id=?,parent_task_id=?,last_seen_at=?,metadata_json=? "
            "WHERE consumer_id=? AND task_id=? AND agent_id=?",
            (
                new_agent_id,
                resolved_parent_task_id,
                now,
                metadata_json,
                consumer_id,
                task_id,
                current_agent_id,
            ),
        )
        if updated.rowcount != 1:
            raise dependencies.subscription_error_type(
                "Consumer ownership changed concurrently; inspect and retry explicitly"
            )
        subscription_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM subscriptions WHERE consumer_id=?",
                (consumer_id,),
            ).fetchone()[0]
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return {
        "status": status,
        "consumer_id": consumer_id,
        "previous_agent_id": current_agent_id,
        "agent_id": new_agent_id,
        "task_id": task_id,
        "parent_task_id": resolved_parent_task_id,
        "created_at": existing["created_at"],
        "last_seen_at": now,
        "metadata": merged_metadata or None,
        "subscription_count": subscription_count,
    }


def subscribe(
    connection: sqlite3.Connection,
    consumer_id: str,
    selector: Mapping[str, object],
    *,
    subscription_id: str | None = None,
    start_watermark: int | None = None,
    dependencies: SubscriptionDependencies,
) -> dict[str, object]:
    """Create the next immutable selector version for a known consumer."""

    consumer_id = dependencies.valid_record_id(consumer_id)
    normalized = dependencies.selector_normalizer(selector)
    selector_hash = dependencies.stable_hash(normalized)
    state = dependencies.state_loader()
    start = int(state["watermark"] if start_watermark is None else start_watermark)
    if not state["journal_anchor_watermark"] <= start <= state["watermark"]:
        raise dependencies.subscription_error_type("Subscription start watermark is unavailable")
    if not connection.execute(
        "SELECT 1 FROM consumers WHERE consumer_id=?", (consumer_id,)
    ).fetchone():
        raise dependencies.subscription_error_type(f"Unknown consumer: {consumer_id}")
    version = int(
        connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM subscriptions WHERE consumer_id=?",
            (consumer_id,),
        ).fetchone()[0]
    )
    subscription_id = dependencies.valid_record_id(
        subscription_id
        or f"subscription_{dependencies.stable_hash({'consumer': consumer_id, 'version': version, 'selector': selector_hash})[:32]}"
    )
    now = dependencies.utc_now()
    with connection:
        connection.execute(
            "INSERT INTO subscriptions(subscription_id,consumer_id,version,selector_json,"
            "selector_hash,last_ack_watermark,active,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,1,?,?)",
            (
                subscription_id,
                consumer_id,
                version,
                dependencies.canonical_json(normalized),
                selector_hash,
                start,
                now,
                now,
            ),
        )
    return {
        "subscription_id": subscription_id,
        "consumer_id": consumer_id,
        "version": version,
        "selector": normalized,
        "selector_hash": selector_hash,
        "last_ack_watermark": start,
        "active": True,
    }


def subscription(
    connection: sqlite3.Connection,
    subscription_id: str,
    *,
    valid_record_id: Callable[[object], str],
    subscription_error_type: type[Exception],
) -> sqlite3.Row:
    """Load one active subscription or map absence to the public error type."""

    row = connection.execute(
        "SELECT * FROM subscriptions WHERE subscription_id=? AND active=1",
        (valid_record_id(subscription_id),),
    ).fetchone()
    if row is None:
        raise subscription_error_type(f"Unknown active subscription: {subscription_id}")
    return cast(sqlite3.Row, row)


def subscription_identity(
    connection: sqlite3.Connection,
    subscription_id: str,
    *,
    valid_record_id: Callable[[object], str],
) -> dict[str, object] | None:
    """Read one exact active subscription and its immutable consumer owner.

    The join is authoritative.  In particular, callers must not infer this
    relationship from the intentionally capped consumer-status projection.
    """

    row = connection.execute(
        "SELECT s.subscription_id,s.consumer_id,s.active,c.agent_id,c.task_id "
        "FROM subscriptions s JOIN consumers c ON c.consumer_id=s.consumer_id "
        "WHERE s.subscription_id=? AND s.active=1",
        (valid_record_id(subscription_id),),
    ).fetchone()
    if row is None:
        return None
    return {
        "subscription_id": str(row["subscription_id"]),
        "consumer_id": str(row["consumer_id"]),
        "agent_id": str(row["agent_id"]),
        "task_id": str(row["task_id"]),
        "active": bool(row["active"]),
    }


def inbox(
    subscription_row: sqlite3.Row,
    *,
    limit: int = 100,
    cursor: str | None = None,
    through_watermark: int | None = None,
    dependencies: InboxDependencies,
) -> dict[str, object]:
    """Deliver one snapshot-pinned subscription page and safe ack token."""

    selector = cast(
        Mapping[str, object],
        dependencies.json_loads(cast(str, subscription_row["selector_json"])),
    )
    feed = dependencies.mutation_feed(
        int(subscription_row["last_ack_watermark"]),
        through_watermark=through_watermark,
        cursor=cursor,
        limit=limit,
        selector=selector,
    )
    ack_token = None
    if feed["complete"]:
        state = dependencies.state_loader()
        ack_token = dependencies.sign_plan_token(
            {
                "type": "subscription_ack",
                "version": 1,
                "store_id": state["store_id"],
                "generation": state["generation"],
                "subscription_id": subscription_row["subscription_id"],
                "selector_hash": subscription_row["selector_hash"],
                "from_watermark": subscription_row["last_ack_watermark"],
                "through_watermark": feed["snapshot_watermark"],
            },
            dependencies.require_token_key(),
        )
    result: dict[str, object] = {
        "subscription_id": subscription_row["subscription_id"],
        "consumer_id": subscription_row["consumer_id"],
    }
    result.update(cast(Mapping[str, object], feed))
    result["ack_token"] = ack_token
    return result


def ack(
    connection: sqlite3.Connection,
    subscription_row: sqlite3.Row,
    subscription_id: str,
    through_watermark: int,
    *,
    ack_token: str | None = None,
    force: bool = False,
    dependencies: AcknowledgementDependencies,
) -> dict[str, object]:
    """Advance one subscription monotonically after token verification."""

    target = int(through_watermark)
    state = dependencies.state_loader()
    current = int(subscription_row["last_ack_watermark"])
    if not current <= target <= state["watermark"]:
        raise dependencies.subscription_error_type(
            "Acknowledgement watermark is not monotonic/current"
        )
    if target == current:
        return {"subscription_id": subscription_id, "last_ack_watermark": target}
    if not force:
        if not ack_token:
            raise dependencies.subscription_error_type(
                "Safe acknowledgement requires inbox ack_token"
            )
        token = dependencies.verify_plan_token(
            ack_token,
            dependencies.require_token_key(),
        )
        expected: dict[str, object] = {
            "type": "subscription_ack",
            "version": 1,
            "store_id": state["store_id"],
            "generation": state["generation"],
            "subscription_id": subscription_row["subscription_id"],
            "selector_hash": subscription_row["selector_hash"],
            "from_watermark": current,
            "through_watermark": target,
        }
        if token != expected:
            raise dependencies.subscription_error_type(
                "Acknowledgement token does not match subscription state"
            )
    now = dependencies.utc_now()
    with connection:
        updated = connection.execute(
            "UPDATE subscriptions SET last_ack_watermark=?,updated_at=? "
            "WHERE subscription_id=? AND active=1 AND last_ack_watermark=?",
            (target, now, subscription_id, current),
        )
        if updated.rowcount != 1:
            refreshed = dependencies.subscription_loader(subscription_id)
            refreshed_current = int(refreshed["last_ack_watermark"])
            if target == refreshed_current:
                return {
                    "subscription_id": subscription_id,
                    "last_ack_watermark": target,
                }
            raise dependencies.subscription_error_type(
                "Acknowledgement state changed concurrently; pull a fresh inbox delivery"
            )
    return {"subscription_id": subscription_id, "last_ack_watermark": target}


def current_record_predicate(selector: Selector) -> tuple[str, list[SqlParameter]]:
    """Subscription membership for current records, using the r/h query aliases."""

    predicates: list[str] = []
    parameters: list[SqlParameter] = []
    if selector["all"]:
        predicates.append("1=1")
    if selector["record_ids"]:
        predicates.append(f"r.record_id IN ({','.join('?' for _ in selector['record_ids'])})")
        parameters.extend(selector["record_ids"])
    if selector["collections"]:
        predicates.append(f"r.collection IN ({','.join('?' for _ in selector['collections'])})")
        parameters.extend(selector["collections"])
    if selector["entity_ids"]:
        predicates.append(
            "EXISTS (SELECT 1 FROM revision_refs rr WHERE rr.record_id=r.record_id "
            "AND rr.revision=r.current_revision "
            f"AND rr.target_record_id IN ({','.join('?' for _ in selector['entity_ids'])}))"
        )
        parameters.extend(selector["entity_ids"])
    if selector["discovery_kinds"]:
        predicates.append(
            f"h.discovery_kind IN ({','.join('?' for _ in selector['discovery_kinds'])})"
        )
        parameters.extend(selector["discovery_kinds"])
    return " OR ".join(predicates) or "0=1", parameters


def context_pack(
    connection: sqlite3.Connection,
    subscription_row: sqlite3.Row,
    subscription_id: str,
    *,
    max_items: int = 100,
    max_chars: int = 50_000,
    dependencies: ContextPackDependencies,
) -> dict[str, object]:
    """Build one bounded current-record context snapshot for a subscription."""

    selector = cast(
        Selector,
        dependencies.json_loads(cast(str, subscription_row["selector_json"])),
    )
    max_items = max(1, min(int(max_items), dependencies.max_context_items))
    max_chars = max(1_000, min(int(max_chars), dependencies.max_context_chars))
    predicate, parameters = current_record_predicate(selector)
    parameters.append(max_items + 1)
    rows = connection.execute(
        f"""
        SELECT r.record_id,r.collection,r.current_revision,r.last_watermark,
               r.current_payload_hash,p.payload_json
        FROM records r
        JOIN record_revision_headers h
          ON h.record_id=r.record_id AND h.revision=r.current_revision
        JOIN record_revision_payloads p
          ON p.record_id=r.record_id AND p.revision=r.current_revision
        WHERE {predicate}
        ORDER BY r.last_watermark DESC,r.record_id LIMIT ?
        """,
        parameters,
    ).fetchall()
    records: list[dict[str, object]] = []
    used_chars = 0
    truncated = len(rows) > max_items
    for row in rows[:max_items]:
        payload_json = cast(str, row["payload_json"])
        payload = cast(JsonValue, dependencies.json_loads(payload_json))
        size = len(payload_json)
        if records and used_chars + size > max_chars:
            truncated = True
            break
        used_chars += size
        records.append(
            {
                "record_id": row["record_id"],
                "collection": row["collection"],
                "revision": row["current_revision"],
                "watermark": row["last_watermark"],
                "payload_hash": row["current_payload_hash"],
                "payload": payload,
            }
        )
    return {
        "subscription_id": subscription_id,
        "watermark": dependencies.watermark_loader(),
        "selector": selector,
        "item_count": len(records),
        "char_count": used_chars,
        "truncated": truncated,
        "records": records,
    }
