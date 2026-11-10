"""Read-only query kernels for the v4 knowledge graph.

The public storage facade owns connection lifetime and supplies its live policy
callbacks on every invocation.  This module performs no writes, starts no
transactions, and never imports the facade back into itself.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import AbstractSet, Protocol, TypeAlias, TypedDict, cast

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class GraphState(TypedDict):
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
    record_id: str
    collection: str
    revision: int
    payload_hash: str
    last_watermark: int


class RecordSnapshot(TypedDict):
    record_id: str
    collection: str
    revision: int
    watermark: int
    payload_hash: str
    payload_available: bool
    payload: JsonValue | None


class Producer(TypedDict, total=False):
    agent_id: str
    task_id: str
    parent_task_id: str
    run_id: str


class HistoryEntry(TypedDict):
    revision: int
    watermark: int
    change_kind: str
    discovery_kind: str
    payload_hash: str
    payload_available: bool
    payload: JsonValue | None
    producer: Producer
    based_on_watermark: int | None
    created_at: str


class HistoryResult(TypedDict):
    record_id: str
    history: list[HistoryEntry]
    has_more: bool
    next_before_revision: int | None


class SearchResult(TypedDict):
    query: str
    watermark: int
    result_count: int
    results: list[RecordSnapshot]


class ScopedRecordSnapshot(RecordSnapshot, total=False):
    """A scoped match; partial matches name the query terms they contain."""

    matched_terms: list[str]


class ScopedSearchResult(TypedDict):
    subscription_id: str
    query: str
    watermark: int
    result_count: int
    results: list[ScopedRecordSnapshot]
    truncated: bool
    match_mode: str
    considered_terms: list[str]


class RecordsLoader(Protocol):
    def __call__(self, record_ids: Iterable[str]) -> list[RecordSnapshot]: ...


@dataclass(frozen=True)
class QueryDependencies:
    """Facade policy resolved at the time of each public query."""

    collection_set: AbstractSet[str]
    max_history_limit: int
    valid_record_id: Callable[[object], str]
    normalize_key: Callable[[object], str]
    normalize_space: Callable[[object], str]
    stable_hash: Callable[[object], str]
    store_format_error_type: type[Exception]
    validation_error_type: type[Exception]


def _load_json(value: str) -> JsonValue:
    """Narrow the standard-library JSON decoder's dynamic stub result."""

    return cast(JsonValue, json.loads(value))


def _record_payload(record: RecordSnapshot) -> dict[str, JsonValue]:
    """Narrow payloads guaranteed to be objects by canonical validation."""

    return cast(dict[str, JsonValue], record["payload"] or {})


def _record_snapshot(row: sqlite3.Row) -> RecordSnapshot:
    payload_text = row["payload_json"]
    return {
        "record_id": str(row["record_id"]),
        "collection": str(row["collection"]),
        "revision": int(row["revision"]),
        "watermark": int(row["watermark"]),
        "payload_hash": str(row["payload_hash"]),
        "payload_available": payload_text is not None,
        "payload": _load_json(str(payload_text)) if payload_text else None,
    }


def state(
    connection: sqlite3.Connection,
    *,
    dependencies: QueryDependencies,
) -> GraphState:
    row = connection.execute("SELECT * FROM graph_state WHERE singleton=1").fetchone()
    if row is None:
        raise dependencies.store_format_error_type("Missing graph_state singleton")
    return {
        "schema_version": str(row["schema_version"]),
        "store_id": str(row["store_id"]),
        "generation": int(row["generation"]),
        "watermark": int(row["committed_watermark"]),
        "journal_head_hash": str(row["journal_head_hash"]),
        "journal_anchor_watermark": int(row["journal_anchor_watermark"]),
        "journal_anchor_hash": str(row["journal_anchor_hash"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def current_revisions(
    connection: sqlite3.Connection,
    record_ids: Iterable[str],
    *,
    dependencies: QueryDependencies,
) -> dict[str, CurrentRevision | None]:
    ids = list(dict.fromkeys(dependencies.valid_record_id(item) for item in record_ids))
    if len(ids) > 2_000:
        raise ValueError("current_revisions is bounded to 2,000 record IDs")
    result: dict[str, CurrentRevision | None] = {item: None for item in ids}
    if not ids:
        return result
    placeholders = ",".join("?" for _ in ids)
    for row in connection.execute(
        f"SELECT record_id, collection, current_revision, current_payload_hash, "
        f"last_watermark FROM records WHERE record_id IN ({placeholders})",
        ids,
    ):
        record_id = str(row["record_id"])
        result[record_id] = {
            "record_id": record_id,
            "collection": str(row["collection"]),
            "revision": int(row["current_revision"]),
            "payload_hash": str(row["current_payload_hash"]),
            "last_watermark": int(row["last_watermark"]),
        }
    return result


def get_record(
    connection: sqlite3.Connection,
    record_id: str,
    revision: int | None = None,
    *,
    dependencies: QueryDependencies,
) -> RecordSnapshot:
    record_id = dependencies.valid_record_id(record_id)
    if revision is None:
        row = connection.execute(
            """
            SELECT r.record_id, r.collection, r.current_revision AS revision,
                   r.last_watermark AS watermark, r.current_payload_hash AS payload_hash,
                   p.payload_json
            FROM records r
            LEFT JOIN record_revision_payloads p
              ON p.record_id=r.record_id AND p.revision=r.current_revision
            WHERE r.record_id=?
            """,
            (record_id,),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT r.record_id, r.collection, h.revision, h.watermark,
                   h.payload_hash, p.payload_json
            FROM records r
            JOIN record_revision_headers h ON h.record_id=r.record_id
            LEFT JOIN record_revision_payloads p
              ON p.record_id=h.record_id AND p.revision=h.revision
            WHERE r.record_id=? AND h.revision=?
            """,
            (record_id, int(revision)),
        ).fetchone()
    if row is None:
        raise KeyError(record_id if revision is None else f"{record_id}@{revision}")
    return _record_snapshot(row)


def get_records(
    connection: sqlite3.Connection,
    record_ids: Iterable[str],
    *,
    dependencies: QueryDependencies,
) -> list[RecordSnapshot]:
    ids = list(dict.fromkeys(dependencies.valid_record_id(item) for item in record_ids))
    if len(ids) > 2_000:
        raise ValueError("get_records is bounded to 2,000 record IDs")
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT r.record_id, r.collection, r.current_revision AS revision,
               r.last_watermark AS watermark, r.current_payload_hash AS payload_hash,
               p.payload_json
        FROM records r
        LEFT JOIN record_revision_payloads p
          ON p.record_id=r.record_id AND p.revision=r.current_revision
        WHERE r.record_id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    snapshots = {str(row["record_id"]): _record_snapshot(row) for row in rows}
    return [snapshots[item] for item in ids if item in snapshots]


def history(
    connection: sqlite3.Connection,
    record_id: str,
    *,
    limit: int = 100,
    before_revision: int | None = None,
    dependencies: QueryDependencies,
) -> HistoryResult:
    record_id = dependencies.valid_record_id(record_id)
    limit = max(1, min(int(limit), dependencies.max_history_limit))
    clauses = ["h.record_id=?"]
    parameters: list[object] = [record_id]
    if before_revision is not None:
        clauses.append("h.revision < ?")
        parameters.append(int(before_revision))
    parameters.append(limit + 1)
    rows = connection.execute(
        f"""
        SELECT h.*, p.payload_json
        FROM record_revision_headers h
        LEFT JOIN record_revision_payloads p
          ON p.record_id=h.record_id AND p.revision=h.revision
        WHERE {" AND ".join(clauses)}
        ORDER BY h.revision DESC LIMIT ?
        """,
        parameters,
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    entries: list[HistoryEntry] = []
    for row in rows:
        producer: Producer = {}
        if row["producer_agent_id"]:
            producer["agent_id"] = str(row["producer_agent_id"])
        if row["task_id"]:
            producer["task_id"] = str(row["task_id"])
        if row["parent_task_id"]:
            producer["parent_task_id"] = str(row["parent_task_id"])
        if row["run_id"]:
            producer["run_id"] = str(row["run_id"])
        payload_text = row["payload_json"]
        entries.append(
            {
                "revision": int(row["revision"]),
                "watermark": int(row["watermark"]),
                "change_kind": str(row["change_kind"]),
                "discovery_kind": str(row["discovery_kind"]),
                "payload_hash": str(row["payload_hash"]),
                "payload_available": payload_text is not None,
                "payload": _load_json(str(payload_text)) if payload_text else None,
                "producer": producer,
                "based_on_watermark": (
                    int(row["based_on_watermark"])
                    if row["based_on_watermark"] is not None
                    else None
                ),
                "created_at": str(row["created_at"]),
            }
        )
    return {
        "record_id": record_id,
        "history": entries,
        "has_more": more,
        "next_before_revision": entries[-1]["revision"] if more and entries else None,
    }


def find_entities(
    connection: sqlite3.Connection,
    *,
    identity_keys: Sequence[Mapping[str, object]] = (),
    namespace: str | None = None,
    entity_type: str | None = None,
    names: Sequence[str] = (),
    limit: int = 100,
    records_loader: RecordsLoader,
    dependencies: QueryDependencies,
) -> list[RecordSnapshot]:
    limit = max(1, min(int(limit), 500))
    ids: set[str] = set()
    for item in identity_keys:
        scheme = dependencies.normalize_key(item.get("scheme"))
        key_namespace = (
            dependencies.normalize_key(item.get("namespace"))
            or dependencies.normalize_key(namespace)
            or "global"
        )
        value = unicodedata.normalize("NFC", str(item.get("value") or "").strip())
        scheme_row = connection.execute(
            "SELECT normalization FROM identity_schemes WHERE scheme=?", (scheme,)
        ).fetchone()
        value_key = value.casefold() if scheme_row and scheme_row[0] == "casefold" else value
        row = connection.execute(
            "SELECT entity_id FROM entity_identity_keys "
            "WHERE scheme=? AND namespace=? AND value_key=?",
            (scheme, key_namespace, value_key),
        ).fetchone()
        if row:
            ids.add(str(row[0]))
    normalized_names = [
        dependencies.normalize_key(item) for item in names if dependencies.normalize_space(item)
    ]
    if normalized_names:
        placeholders = ",".join("?" for _ in normalized_names)
        parameters: list[object] = [*normalized_names]
        clause = f"label_norm IN ({placeholders})"
        if namespace is not None:
            clause += " AND namespace=?"
            parameters.append(dependencies.normalize_key(namespace) or "global")
        parameters.append(limit)
        ids.update(
            str(row[0])
            for row in connection.execute(
                f"SELECT entity_id FROM entity_aliases WHERE {clause} LIMIT ?",
                parameters,
            )
        )
    records = records_loader(sorted(ids)[:limit])
    if entity_type is not None:
        target = dependencies.normalize_key(entity_type)
        records = [
            item
            for item in records
            if dependencies.normalize_key(_record_payload(item).get("type")) == target
        ]
    return records


def records_referencing(
    connection: sqlite3.Connection,
    target_ids: Iterable[str],
    *,
    ref_kinds: Iterable[str] | None = None,
    limit: int = 500,
    records_loader: RecordsLoader,
    dependencies: QueryDependencies,
) -> list[RecordSnapshot]:
    targets = list(dict.fromkeys(dependencies.valid_record_id(item) for item in target_ids))
    if not targets:
        return []
    if len(targets) > 500:
        raise ValueError("records_referencing is bounded to 500 targets")
    parameters: list[object] = [*targets]
    target_placeholders = ",".join("?" for _ in targets)
    clause = f"rr.target_record_id IN ({target_placeholders})"
    kinds = sorted(
        {
            dependencies.normalize_key(item)
            for item in ref_kinds or []
            if dependencies.normalize_space(item)
        }
    )
    if kinds:
        clause += f" AND rr.ref_kind IN ({','.join('?' for _ in kinds)})"
        parameters.extend(kinds)
    parameters.append(max(1, min(int(limit), 2_000)))
    ids = [
        str(row[0])
        for row in connection.execute(
            f"""
            SELECT DISTINCT rr.record_id
            FROM revision_refs rr
            JOIN records r ON r.record_id=rr.record_id
                          AND r.current_revision=rr.revision
            WHERE {clause}
            ORDER BY r.last_watermark DESC, rr.record_id
            LIMIT ?
            """,
            parameters,
        )
    ]
    return records_loader(ids)


def find_claim_surfaces(
    connection: sqlite3.Connection,
    *,
    surface_id: str | None = None,
    family: str | None = None,
    key: str | None = None,
    subject_signature: str | None = None,
    subject_ids: Sequence[str] | None = None,
    limit: int = 100,
    records_loader: RecordsLoader,
    dependencies: QueryDependencies,
) -> list[RecordSnapshot]:
    clauses: list[str] = []
    parameters: list[object] = []
    if surface_id:
        clauses.append("surface_id=?")
        parameters.append(dependencies.valid_record_id(surface_id))
    if family is not None:
        clauses.append("family=?")
        parameters.append(dependencies.normalize_key(family))
    if key is not None:
        clauses.append("key_name=?")
        parameters.append(dependencies.normalize_key(key))
    if subject_ids is not None:
        subject_signature = dependencies.stable_hash(
            sorted(dependencies.valid_record_id(item) for item in subject_ids)
        )
    if subject_signature is not None:
        clauses.append("subject_signature=?")
        parameters.append(subject_signature)
    if not clauses:
        raise ValueError("find_claim_surfaces requires an identifying predicate")
    parameters.append(max(1, min(int(limit), 500)))
    ids = [
        str(row[0])
        for row in connection.execute(
            f"SELECT surface_id FROM claim_surface_constraints "
            f"WHERE {' AND '.join(clauses)} ORDER BY surface_id LIMIT ?",
            parameters,
        )
    ]
    return records_loader(ids)


def current_claims(
    connection: sqlite3.Connection,
    surface_ids: Iterable[str],
    *,
    limit: int = 500,
    dependencies: QueryDependencies,
) -> list[RecordSnapshot]:
    surfaces = sorted({dependencies.valid_record_id(item) for item in surface_ids})
    if not surfaces:
        return []
    if len(surfaces) > 500:
        raise ValueError("current_claims is bounded to 500 surfaces")
    limit = max(1, min(int(limit), 2_000))
    placeholders = ",".join("?" for _ in surfaces)
    rows = connection.execute(
        f"""
        SELECT r.record_id, r.collection, r.current_revision AS revision,
               r.last_watermark AS watermark, r.current_payload_hash AS payload_hash,
               p.payload_json
        FROM claim_current_registry c
        JOIN records r ON r.record_id=c.claim_id
        LEFT JOIN record_revision_payloads p
          ON p.record_id=r.record_id AND p.revision=r.current_revision
        WHERE c.surface_id IN ({placeholders})
          AND r.collection='claims'
          AND r.lifecycle='current'
        ORDER BY r.last_watermark DESC, r.record_id
        LIMIT ?
        """,
        [*surfaces, limit],
    ).fetchall()
    return [_record_snapshot(row) for row in rows]


def semantic_key_owner(
    connection: sqlite3.Connection,
    collection: str,
    key_kind: str,
    key: str,
    *,
    dependencies: QueryDependencies,
) -> str | None:
    if collection not in dependencies.collection_set:
        raise ValueError(f"Unknown collection: {collection}")
    row = connection.execute(
        "SELECT record_id FROM semantic_registry WHERE collection=? AND key_kind=? AND key_hash=?",
        (
            collection,
            dependencies.normalize_key(key_kind) or "semantic",
            dependencies.stable_hash(str(key)),
        ),
    ).fetchone()
    return str(row[0]) if row else None


SCOPED_MATCH_MODES = ("all", "any", "auto")

# Function words dropped only when ranking partial matches. An all-terms search
# still requires every literal word, so this list never widens a strict match.
_PARTIAL_MATCH_STOP_WORDS = frozenset(
    "a about after am an and any are as at be been before but by can could did do does for from "
    "had has have how i if in into is it its may me my of on or our shall should so than that the "
    "their them then there these they this those to us was we were what when where which who whom "
    "whose why will with would you your".split()
)


def _partial_match_terms(terms: Sequence[str]) -> list[str]:
    """Terms worth ranking on: prefixes, multi-token literals and non-function words."""

    kept: list[str] = []
    for term in terms:
        literal = term[1:-2] if term.endswith("*") else term[1:-1]
        if term.endswith("*") or (len(literal) > 1 and literal not in _PARTIAL_MATCH_STOP_WORDS):
            kept.append(term)
    return kept


def _display_term(term: str) -> str:
    return f"{term[1:-2]}*" if term.endswith("*") else term[1:-1]


def _scoped_search_terms(query: str) -> list[str]:
    """Keep literal tokenization; admit only explicit, unquoted word prefixes."""

    terms: list[str] = []
    quote: str | None = None
    for word in query.split():
        if "*" in word:
            if quote is not None or re.fullmatch(r"[a-z0-9]{4,}\*", word) is None:
                raise ValueError(
                    "A search prefix must be an unquoted word of at least four ASCII letters or digits followed by one star"
                )
            terms.append(f'"{word[:-1]}"*')
        else:
            terms.extend(f'"{token}"' for token in re.findall(r"[\w][\w._:-]*", word))
        # Quotes remain ordinary literal punctuation, but cannot enclose a
        # prefix or let a partially quoted phrase widen its search silently.
        for index, character in enumerate(word):
            if character in {"'", '"'}:
                # Apostrophes inside or after words are literal punctuation,
                # not the start/end of a single-quoted phrase.
                if quote is None and (character == '"' or index == 0):
                    quote = character
                elif character == quote and (character == '"' or index == len(word) - 1):
                    quote = None
    terms = list(dict.fromkeys(terms))
    if not terms or len(terms) > 32:
        raise ValueError("Search query must contain 1 to 32 terms")
    return terms


def search_scoped(
    connection: sqlite3.Connection,
    subscription_id: str,
    query: str,
    predicate: str,
    parameters: Sequence[object],
    *,
    limit: int = 10,
    match: str = "all",
    records_loader: RecordsLoader,
    watermark_loader: Callable[[], int],
    dependencies: QueryDependencies,
) -> ScopedSearchResult:
    """Literal and explicit prefix matches within a resolved subscription scope.

    ``all`` requires every term. ``any`` ranks records that contain at least one
    meaningful term. ``auto`` returns the all-terms result and ranks partial
    matches only when no eligible record contains every term.
    """

    if not isinstance(query, str) or not query.strip() or len(query) > 2_000:
        raise ValueError("Search query must contain 1 to 2,000 characters")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("Search limit must be between 1 and 100")
    if match not in SCOPED_MATCH_MODES:
        raise ValueError("Search match must be all, any or auto")
    query = dependencies.normalize_space(query)
    terms = _scoped_search_terms(dependencies.normalize_key(query))
    current = """
        JOIN record_revision_headers h
          ON h.record_id=r.record_id AND h.revision=r.current_revision
        JOIN record_revision_payloads p
          ON p.record_id=r.record_id AND p.revision=r.current_revision
    """
    eligible = f"""
        WHERE ({predicate}) AND r.sensitive=0
          AND COALESCE(r.lifecycle,'') NOT IN ('redacted','tombstoned')
    """

    def result(
        records: list[ScopedRecordSnapshot], found: int, mode: str, considered: Sequence[str]
    ) -> ScopedSearchResult:
        return {
            "subscription_id": subscription_id,
            "query": query,
            "watermark": watermark_loader(),
            "result_count": len(records),
            "results": records,
            "truncated": found > limit,
            "match_mode": mode,
            "considered_terms": [_display_term(term) for term in considered],
        }

    if match != "any":
        # Do not use corpus-wide BM25 here: unrelated records must not affect the
        # ordering of authorized matches. Exact IDs lead, then matched recency.
        rows = connection.execute(
            f"""
            SELECT r.record_id
            FROM records r
            {current}
            {eligible}
              AND (r.record_id=? OR r.record_id IN (
                SELECT record_id FROM records_fts WHERE records_fts MATCH ?))
            ORDER BY CASE WHEN r.record_id=? THEN 0 ELSE 1 END,
                     r.last_watermark DESC,r.record_id
            LIMIT ?
            """,
            [*parameters, query, " AND ".join(terms), query, limit + 1],
        ).fetchall()
        partial_terms = _partial_match_terms(terms)
        # A one-term query has no partial form: the strict pass already ran it.
        if match == "all" or rows or not partial_terms or len(terms) == 1:
            strict = [
                cast(ScopedRecordSnapshot, record)
                for record in records_loader(str(row[0]) for row in rows[:limit])
            ]
            return result(strict, len(rows), "all_terms", terms)
    else:
        partial_terms = _partial_match_terms(terms)
        if not partial_terms:
            return result([], 0, "any_terms", [])
    # Rank by how many distinct query terms one record contains. That count is a
    # property of the record alone, so no other record, in or out of scope, can
    # change it. Eligibility is applied before the limit, as in the strict path.
    # Validation keeps each FTS rowid equal to its record's rowid.
    arms = " UNION ALL ".join(
        f"SELECT rowid AS hit, {index} AS term FROM records_fts WHERE records_fts MATCH ?"
        for index in range(len(partial_terms))
    )
    rows = connection.execute(
        f"""
        WITH hits(hit, term) AS ({arms}),
        matched(hit, matched_count, matched_terms) AS (
          SELECT hit, COUNT(*), GROUP_CONCAT(term) FROM hits GROUP BY hit
        )
        SELECT r.record_id, m.matched_terms
        FROM matched m
        JOIN records r ON r.rowid=m.hit
        {current}
        {eligible}
        ORDER BY m.matched_count DESC, r.last_watermark DESC, r.record_id
        LIMIT ?
        """,
        [*partial_terms, *parameters, limit + 1],
    ).fetchall()
    matched_terms = {
        str(row[0]): [
            _display_term(partial_terms[index])
            for index in sorted(int(item) for item in str(row[1]).split(","))
        ]
        for row in rows[:limit]
    }
    partial = [
        cast(
            ScopedRecordSnapshot,
            {**record, "matched_terms": matched_terms[record["record_id"]]},
        )
        for record in records_loader(matched_terms)
    ]
    return result(partial, len(rows), "any_terms", partial_terms)


def search(
    connection: sqlite3.Connection,
    query: str,
    *,
    limit: int = 20,
    collections: Iterable[str] | None = None,
    records_loader: RecordsLoader,
    watermark_loader: Callable[[], int],
    dependencies: QueryDependencies,
) -> SearchResult:
    query = dependencies.normalize_space(query)
    if not query:
        raise ValueError("Search query is required")
    limit = max(1, min(int(limit), 100))
    collection_filter = sorted(set(collections or []))
    if any(item not in dependencies.collection_set for item in collection_filter):
        raise ValueError("Search collection filter contains an unknown collection")
    ids: list[str] = []
    exact = connection.execute(
        "SELECT record_id FROM records WHERE record_id=?", (query,)
    ).fetchone()
    if exact:
        ids.append(str(exact[0]))
    tokens = re.findall(
        r"[\w][\w._:-]{1,}",
        dependencies.normalize_key(query),
        flags=re.UNICODE,
    )
    if tokens:
        expression = " OR ".join(
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens[:32]
        )
        parameters: list[object] = [expression]
        clause = "records_fts MATCH ?"
        if collection_filter:
            clause += f" AND collection IN ({','.join('?' for _ in collection_filter)})"
            parameters.extend(collection_filter)
        parameters.append(max(limit * 5, 100))
        try:
            rows = connection.execute(
                f"SELECT record_id FROM records_fts WHERE {clause} "
                "ORDER BY bm25(records_fts) LIMIT ?",
                parameters,
            )
            for row in rows:
                record_id = str(row[0])
                if record_id not in ids:
                    ids.append(record_id)
        except sqlite3.OperationalError as error:
            raise dependencies.validation_error_type(
                f"FTS query failed; run validation/reindex: {error}"
            ) from error
    records = records_loader(ids[:limit])
    return {
        "query": query,
        "watermark": watermark_loader(),
        "result_count": len(records),
        "results": records,
    }


def _preference_time(value: object) -> str | None:
    """Comparable UTC instants without SQLite's millisecond rounding."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Preference dates must be ISO text")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="microseconds")


def preferences(
    connection: sqlite3.Connection,
    predicate: str,
    parameters: Sequence[object],
    *,
    subject_ids: Sequence[str],
    all_subjects: bool,
    now: str,
) -> dict[str, object]:
    """Current stated preferences on explicitly approved, visible subjects."""
    connection.create_function("tedd_preference_time", 1, _preference_time, deterministic=True)
    rows = connection.execute(
        f"""
        SELECT r.record_id,r.current_revision,p.payload_json,
               COUNT(*) OVER (PARTITION BY json_extract(p.payload_json,'$.surface_ref')) AS visible_values,
               json_extract(sp.payload_json,'$.subject_refs[0]') AS subject_id,
               CASE WHEN substr(er.record_id,1,34)='entity_scope_operator_preferences_' THEN 2
                    WHEN json_extract(ep.payload_json,'$.type')='client' THEN 1 ELSE 0 END AS priority
        FROM records r
        JOIN record_revision_headers h ON h.record_id=r.record_id AND h.revision=r.current_revision
        JOIN record_revision_payloads p ON p.record_id=r.record_id AND p.revision=r.current_revision
        JOIN records sr ON sr.record_id=json_extract(p.payload_json,'$.surface_ref')
        JOIN record_revision_payloads sp ON sp.record_id=sr.record_id AND sp.revision=sr.current_revision
        JOIN records er ON er.record_id=json_extract(sp.payload_json,'$.subject_refs[0]')
        JOIN record_revision_payloads ep ON ep.record_id=er.record_id AND ep.revision=er.current_revision
        WHERE ({predicate}) AND r.collection='claims' AND r.sensitive=0
          AND (r.lifecycle IN ('current','conflicting') AND json_extract(p.payload_json,'$.active')=1
               OR r.lifecycle='superseded')
          AND json_extract(p.payload_json,'$.family')='preference'
          AND tedd_preference_time(json_extract(p.payload_json,'$.valid_from'))<=?
          AND (json_extract(p.payload_json,'$.valid_to') IS NULL
               OR tedd_preference_time(json_extract(p.payload_json,'$.valid_to'))>?)
          AND sr.collection='claim_surfaces' AND sr.sensitive=0 AND sr.lifecycle='active'
          AND json_array_length(sp.payload_json,'$.subject_refs')=1
          AND er.collection='entities' AND er.sensitive=0 AND er.lifecycle='active'
          AND json_extract(ep.payload_json,'$.redirect_to') IS NULL
          AND (? OR er.record_id IN (SELECT value FROM json_each(?)))
        ORDER BY priority,tedd_preference_time(json_extract(p.payload_json,'$.valid_from')) DESC,r.record_id
        LIMIT 13
        """,
        [
            *parameters,
            _preference_time(now),
            _preference_time(now),
            all_subjects,
            json.dumps(list(subject_ids)),
        ],
    ).fetchall()
    items: list[dict[str, object]] = []
    for row in rows[:12]:
        payload = cast(dict[str, JsonValue], _load_json(str(row["payload_json"])))
        items.append(
            {
                "id": str(row["record_id"]),
                "revision": int(row["current_revision"]),
                "subject_id": str(row["subject_id"]),
                "surface_ref": payload.get("surface_ref"),
                "value": payload.get("value"),
                "valid_from": payload.get("valid_from"),
                "valid_to": payload.get("valid_to"),
                "source_tier": payload.get("source_tier"),
                "lifecycle": payload.get("lifecycle"),
                "effective_conflict": int(row["visible_values"]) > 1
                or payload.get("lifecycle") == "conflicting",
            }
        )
    return {
        "label": "stated preferences, with dates",
        "items": items,
        "truncated": len(rows) > 12,
        "omitted": False,
    }


def review_loops(
    connection: sqlite3.Connection,
    subscription_id: str,
    predicate: str,
    parameters: Sequence[object],
    *,
    now: str,
    overdue_days: int = 0,
    age_days: int = 30,
    limit: int = 20,
) -> dict[str, object]:
    """Select permitted unfinished obligations before applying the display cap."""

    for value, minimum, maximum, label in (
        (limit, 1, 20, "loop limit"),
        (age_days, 1, 36_500, "loop age"),
        (overdue_days, 0, 36_500, "overdue days"),
    ):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{label} is outside its supported range")
    rows = connection.execute(
        f"""
        WITH scoped AS (
            SELECT r.record_id,p.payload_json,
                   json_extract(p.payload_json,'$.checkpoint_at') AS checkpoint,
                   julianday(?) - julianday(COALESCE(
                       json_extract(p.payload_json,'$.checkpoint_at'),
                       json_extract(p.payload_json,'$.first_observed_at'))) AS elapsed
            FROM records r
            JOIN record_revision_headers h
              ON h.record_id=r.record_id AND h.revision=r.current_revision
            JOIN record_revision_payloads p
              ON p.record_id=r.record_id AND p.revision=r.current_revision
            WHERE ({predicate}) AND r.collection='open_loops' AND r.sensitive=0
              AND COALESCE(r.lifecycle,'') NOT IN ('redacted','tombstoned')
              AND json_extract(p.payload_json,'$.status') IN ('open','candidate')
        )
        SELECT record_id,payload_json,
               CAST(elapsed - CASE WHEN checkpoint IS NULL THEN ? ELSE 0 END AS INTEGER)
                   AS days_overdue
        FROM scoped
        WHERE (checkpoint IS NOT NULL AND elapsed>?)
           OR (checkpoint IS NULL AND elapsed>?)
        ORDER BY days_overdue DESC,record_id
        LIMIT ?
        """,
        [now, *parameters, age_days, overdue_days, age_days, limit + 1],
    ).fetchall()
    items: list[dict[str, object]] = []
    for row in rows[:limit]:
        payload = cast(dict[str, JsonValue], _load_json(str(row["payload_json"])))
        items.append(
            {
                "id": str(row["record_id"]),
                "title": payload.get("title"),
                "status": payload.get("status"),
                "source_tier": payload.get("source_tier"),
                "authority_state": payload.get("authority_state"),
                "owner_refs": payload.get("owner_refs", []),
                "next_action": payload.get("next_action"),
                "checkpoint_at": payload.get("checkpoint_at"),
                "days_overdue": int(row["days_overdue"]),
            }
        )
    return {"subscription_id": subscription_id, "items": items, "truncated": len(rows) > limit}


__all__ = [
    "CurrentRevision",
    "GraphState",
    "HistoryResult",
    "JsonValue",
    "QueryDependencies",
    "RecordSnapshot",
    "SearchResult",
    "ScopedSearchResult",
    "current_claims",
    "current_revisions",
    "find_claim_surfaces",
    "find_entities",
    "get_record",
    "get_records",
    "history",
    "records_referencing",
    "review_loops",
    "search",
    "search_scoped",
    "semantic_key_owner",
    "state",
]
