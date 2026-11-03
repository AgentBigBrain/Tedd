"""Snapshot-pinned mutation-feed queries for the v4 knowledge graph.

The public storage facade owns connection and plan-key lifetimes.  This module
owns selector SQL, authenticated cursor semantics, and exact-revision feed
materialization without importing the facade or starting write transactions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, TypeAlias, TypedDict, cast

from tedd_knowledge_graph.v4_queries import GraphState, JsonValue

SqlParameter: TypeAlias = None | int | float | str | bytes


class Selector(TypedDict):
    all: bool
    record_ids: list[str]
    entity_ids: list[str]
    collections: list[str]
    discovery_kinds: list[str]


class MutationFeedChange(TypedDict):
    watermark: int
    ordinal: int
    mutation_id: str
    action: str
    discovery_kind: str
    record_id: str
    collection: str
    revision: int
    change_kind: str
    payload_hash: str
    payload_available: bool
    payload: JsonValue | None
    producer: dict[str, str]
    based_on_watermark: int | None
    captured_at: str
    entry_hash: str


class MutationFeedResult(TypedDict):
    since_watermark: int
    snapshot_watermark: int
    selector: Selector | None
    selector_hash: str
    returned_count: int
    complete: bool
    next_cursor: str | None
    advance_to_watermark: int | None
    changes: list[MutationFeedChange]
    record_payload_semantics: str


class SelectorNormalizer(Protocol):
    def __call__(self, selector: Mapping[str, object]) -> Selector: ...


class FeedPredicateBuilder(Protocol):
    def __call__(
        self,
        selector: Mapping[str, object] | None,
    ) -> tuple[str, list[SqlParameter], Selector | None]: ...


class StateLoader(Protocol):
    def __call__(self) -> GraphState: ...


class TokenVerifier(Protocol):
    def __call__(self, token: str, key: bytes | str) -> Mapping[str, object]: ...


class TokenSigner(Protocol):
    def __call__(self, payload: Mapping[str, object], key: bytes | str) -> str: ...


class JsonLoader(Protocol):
    def __call__(self, value: str) -> object: ...


@dataclass(frozen=True)
class FeedPredicateDependencies:
    """Call-time selector policy used to build one feed predicate."""

    selector_normalizer: SelectorNormalizer


@dataclass(frozen=True)
class FeedDependencies:
    """Call-time facade policy for one authenticated feed query."""

    max_feed_limit: int
    state_loader: StateLoader
    predicate_builder: FeedPredicateBuilder
    stable_hash: Callable[[object], str]
    verify_plan_token: TokenVerifier
    sign_plan_token: TokenSigner
    require_token_key: Callable[[], bytes]
    json_loads: JsonLoader
    plan_token_error_type: type[Exception]


def feed_predicate(
    selector: Mapping[str, object] | None,
    *,
    dependencies: FeedPredicateDependencies,
) -> tuple[str, list[SqlParameter], Selector | None]:
    """Normalize a selector and return its parameterized SQL predicate."""

    if selector is None:
        return "1=1", [], None
    normalized = dependencies.selector_normalizer(selector)
    if normalized["all"]:
        return "1=1", [], normalized
    clauses: list[str] = []
    parameters: list[SqlParameter] = []
    for values, column in (
        (normalized["record_ids"], "mc.record_id"),
        (normalized["collections"], "mc.collection"),
        (normalized["discovery_kinds"], "m.discovery_kind"),
    ):
        if values:
            clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
            parameters.extend(values)
    entities = normalized["entity_ids"]
    if entities:
        clauses.append(
            "EXISTS (SELECT 1 FROM revision_refs rr "
            "WHERE rr.record_id=mc.record_id AND rr.revision=mc.revision "
            f"AND rr.target_record_id IN ({','.join('?' for _ in entities)}))"
        )
        parameters.extend(entities)
    return f"({' OR '.join(clauses)})", parameters, normalized


def mutation_feed(
    connection: sqlite3.Connection,
    since_watermark: int,
    *,
    through_watermark: int | None = None,
    cursor: str | None = None,
    limit: int = 100,
    selector: Mapping[str, object] | None = None,
    dependencies: FeedDependencies,
) -> MutationFeedResult:
    """Return snapshot-pinned exact record revisions ordered by mutation."""

    limit = int(limit)
    if not 1 <= limit <= dependencies.max_feed_limit:
        raise ValueError(f"Feed limit must be 1-{dependencies.max_feed_limit}")
    state = dependencies.state_loader()
    since = int(since_watermark)
    if since < state["journal_anchor_watermark"]:
        raise ValueError("Requested changes precede the retained journal anchor")
    if since > state["watermark"]:
        raise ValueError("since_watermark exceeds current state")
    predicate, selector_parameters, normalized_selector = dependencies.predicate_builder(selector)
    selector_hash = dependencies.stable_hash(normalized_selector or {"all": True})
    last_watermark = since
    last_ordinal = -1
    if cursor:
        token = dependencies.verify_plan_token(cursor, dependencies.require_token_key())
        if token.get("type") != "mutation_feed" or token.get("version") != 1:
            raise dependencies.plan_token_error_type("Unsupported mutation-feed cursor")
        if (
            token.get("store_id") != state["store_id"]
            or token.get("generation") != state["generation"]
        ):
            raise dependencies.plan_token_error_type(
                "Mutation-feed cursor belongs to another store generation"
            )
        if token.get("since_watermark") != since:
            raise dependencies.plan_token_error_type(
                "Mutation-feed cursor has a different starting watermark"
            )
        if token.get("selector_hash") != selector_hash:
            raise dependencies.plan_token_error_type(
                "Mutation-feed cursor has a different selector"
            )
        snapshot = int(cast(str | int, token.get("through_watermark")))
        if through_watermark is not None and int(through_watermark) != snapshot:
            raise dependencies.plan_token_error_type(
                "Mutation-feed cursor has a different snapshot"
            )
        last_watermark = int(cast(str | int, token.get("last_watermark")))
        last_ordinal = int(cast(str | int, token.get("last_ordinal")))
    else:
        snapshot = state["watermark"] if through_watermark is None else int(through_watermark)
    if not since <= snapshot <= state["watermark"]:
        raise ValueError("through_watermark is outside the available range")
    parameters: list[SqlParameter] = [
        since,
        snapshot,
        last_watermark,
        last_watermark,
        last_ordinal,
    ]
    parameters.extend(selector_parameters)
    parameters.append(limit + 1)
    rows = connection.execute(
        f"""
        SELECT mc.watermark,mc.ordinal,mc.record_id,mc.collection,mc.revision,
               mc.change_kind,mc.payload_hash,p.payload_json,
               m.mutation_id,m.action,m.discovery_kind,m.producer_agent_id,
               m.task_id,m.parent_task_id,m.run_id,m.based_on_watermark,
               m.captured_at,m.entry_hash
        FROM mutation_changes mc
        JOIN mutations m ON m.watermark=mc.watermark
        LEFT JOIN record_revision_payloads p
          ON p.record_id=mc.record_id AND p.revision=mc.revision
        WHERE mc.watermark>? AND mc.watermark<=?
          AND (mc.watermark>? OR (mc.watermark=? AND mc.ordinal>?))
          AND {predicate}
        ORDER BY mc.watermark,mc.ordinal LIMIT ?
        """,
        parameters,
    ).fetchall()
    more = len(rows) > limit
    page = rows[:limit]
    changes: list[MutationFeedChange] = []
    for row in page:
        raw_payload = row["payload_json"]
        producer = {
            key: cast(str, row[column])
            for key, column in (
                ("agent_id", "producer_agent_id"),
                ("task_id", "task_id"),
                ("parent_task_id", "parent_task_id"),
                ("run_id", "run_id"),
            )
            if row[column]
        }
        changes.append(
            {
                "watermark": cast(int, row["watermark"]),
                "ordinal": cast(int, row["ordinal"]),
                "mutation_id": cast(str, row["mutation_id"]),
                "action": cast(str, row["action"]),
                "discovery_kind": cast(str, row["discovery_kind"]),
                "record_id": cast(str, row["record_id"]),
                "collection": cast(str, row["collection"]),
                "revision": cast(int, row["revision"]),
                "change_kind": cast(str, row["change_kind"]),
                "payload_hash": cast(str, row["payload_hash"]),
                "payload_available": raw_payload is not None,
                "payload": (
                    cast(JsonValue, dependencies.json_loads(cast(str, raw_payload)))
                    if raw_payload
                    else None
                ),
                "producer": producer,
                "based_on_watermark": cast(int | None, row["based_on_watermark"]),
                "captured_at": cast(str, row["captured_at"]),
                "entry_hash": cast(str, row["entry_hash"]),
            }
        )
    if more and page:
        tail = page[-1]
        next_cursor = dependencies.sign_plan_token(
            {
                "type": "mutation_feed",
                "version": 1,
                "store_id": state["store_id"],
                "generation": state["generation"],
                "since_watermark": since,
                "through_watermark": snapshot,
                "selector_hash": selector_hash,
                "last_watermark": cast(int, tail["watermark"]),
                "last_ordinal": cast(int, tail["ordinal"]),
            },
            dependencies.require_token_key(),
        )
    else:
        next_cursor = None
    complete = not more
    return {
        "since_watermark": since,
        "snapshot_watermark": snapshot,
        "selector": normalized_selector,
        "selector_hash": selector_hash,
        "returned_count": len(changes),
        "complete": complete,
        "next_cursor": next_cursor,
        "advance_to_watermark": snapshot if complete else None,
        "changes": changes,
        "record_payload_semantics": "exact_revision_at_mutation",
    }
