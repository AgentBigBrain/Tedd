"""SQLite recall-index coordination for the legacy JSON knowledge graph.

The index is disposable and always bound to a canonical graph digest. The flat
compatibility facade supplies filesystem, locking, reload, and query hooks at
call time so security checks and established monkeypatch surfaces remain live.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import sqlite3
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ContextManager, Protocol, cast

from tedd_knowledge_graph import legacy_query

JsonObject = legacy_query.JsonObject


class LoadGraphWithDigest(Protocol):
    def __call__(self, store_path: Path) -> tuple[JsonObject, str, str]: ...


@dataclass(frozen=True)
class LegacyQueryIndexDependencies:
    """Call-time facade hooks used to build and query the disposable index."""

    index_schema_version: str
    graph_schema_version: str
    collections: Sequence[str]
    default_lock_timeout_seconds: float
    query: legacy_query.LegacyQueryDependencies
    utc_now: Callable[[], str]
    ensure_private_directory: Callable[[Path], None]
    set_private_path_mode: Callable[[Path, int], None]
    replace_file_atomically: Callable[[Path, Path], None]
    fsync_parent_directory: Callable[[Path], None]
    store_lock: Callable[[Path, float], ContextManager[None]]
    load_graph_with_digest: LoadGraphWithDigest
    require_current_graph_schema: Callable[[str], None]
    recall_index_path: Callable[[Path], Path]
    read_recall_index_meta: Callable[[Path], dict[str, str]]
    recall_index_healthy: Callable[[Path], bool]
    create_recall_index: Callable[[JsonObject, Path, str], JsonObject]


def _records(graph: JsonObject, collection: str) -> list[JsonObject]:
    return cast(list[JsonObject], graph[collection])


def recall_index_path(store_path: Path) -> Path:
    return store_path.with_name(f"{store_path.name}.index.sqlite3")


def create_recall_index(
    graph: JsonObject,
    index_path: Path,
    canonical_digest: str,
    *,
    dependencies: LegacyQueryIndexDependencies,
) -> JsonObject:
    query = dependencies.query
    validation = query.validate_graph(graph)
    if not validation["valid"]:
        raise ValueError("Refusing to index an invalid graph: " + "; ".join(validation["errors"]))
    dependencies.ensure_private_directory(index_path.parent)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{index_path.name}.",
        suffix=".tmp",
        dir=str(index_path.parent),
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    with contextlib.suppress(FileNotFoundError):
        temp_path.unlink()
    entities = query.index_by_id(_records(graph, "entities"))
    try:
        connection = sqlite3.connect(temp_path)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA secure_delete=ON")
            connection.executescript(
                """
                CREATE TABLE meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE records (
                    record_id TEXT PRIMARY KEY,
                    collection TEXT NOT NULL,
                    section TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    lifecycle TEXT,
                    status TEXT,
                    active INTEGER,
                    source_tier TEXT,
                    verified INTEGER,
                    sensitive INTEGER NOT NULL,
                    search_text TEXT NOT NULL,
                    content_hash TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE records_fts USING fts5(
                    record_id UNINDEXED,
                    search_text,
                    tokenize='unicode61'
                );
                CREATE TABLE record_entities (
                    record_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    PRIMARY KEY (record_id, entity_id, role)
                );
                CREATE INDEX record_entities_by_entity
                    ON record_entities(entity_id, record_id);
                CREATE TABLE entity_labels (
                    entity_id TEXT NOT NULL,
                    label_norm TEXT NOT NULL,
                    label_kind TEXT NOT NULL,
                    namespace_norm TEXT NOT NULL,
                    PRIMARY KEY (entity_id, label_norm, label_kind)
                );
                CREATE INDEX entity_labels_by_label
                    ON entity_labels(label_norm, namespace_norm);
                CREATE TABLE edges (
                    relation_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    review_safe_visible INTEGER NOT NULL
                );
                CREATE INDEX edges_by_source ON edges(source_id, target_id);
                CREATE INDEX edges_by_target ON edges(target_id, source_id);
                CREATE TABLE entity_redirects (
                    source_id TEXT PRIMARY KEY,
                    target_id TEXT NOT NULL
                );
                CREATE INDEX entity_redirects_by_target
                    ON entity_redirects(target_id, source_id);
                """
            )
            for collection, record in query.iter_graph_records(graph):
                search_text = query.record_text(collection, record, entities)
                connection.execute(
                    """
                    INSERT INTO records (
                        record_id, collection, section, timestamp, lifecycle,
                        status, active, source_tier, verified, sensitive,
                        search_text, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["id"],
                        collection,
                        query.recall_section(collection, record),
                        query.record_timestamp(record),
                        record.get("lifecycle"),
                        record.get("status"),
                        (None if record.get("active") is None else int(bool(record.get("active")))),
                        record.get("source_tier"),
                        (
                            None
                            if record.get("verified") is None
                            else int(bool(record.get("verified")))
                        ),
                        int(bool(record.get("sensitive"))),
                        search_text,
                        query.stable_hash(record, 64),
                    ),
                )
                if collection != "journal" and record.get("lifecycle") != "redacted":
                    connection.execute(
                        "INSERT INTO records_fts(record_id, search_text) VALUES (?, ?)",
                        (record["id"], search_text),
                    )
                for entity_id, role in query.indexed_entity_refs(collection, record):
                    connection.execute(
                        "INSERT OR IGNORE INTO record_entities"
                        "(record_id, entity_id, role) VALUES (?, ?, ?)",
                        (record["id"], entity_id, role),
                    )
                if collection == "relations":
                    visible = int(query.relation_visible_in_mode(record, entities, "review_safe"))
                    connection.execute(
                        "INSERT INTO edges"
                        "(relation_id, source_id, target_id, review_safe_visible) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            record["id"],
                            record.get("source_entity_id"),
                            record.get("target_entity_id"),
                            visible,
                        ),
                    )
            for entity in _records(graph, "entities"):
                namespace = query.normalize_key(entity.get("namespace")) or "global"
                labels = [("name", entity.get("name", ""))] + [
                    ("alias", item) for item in cast(list[object], entity.get("aliases", []))
                ]
                for label_kind, label in labels:
                    normalized = query.normalize_key(label)
                    if normalized:
                        connection.execute(
                            "INSERT OR IGNORE INTO entity_labels"
                            "(entity_id, label_norm, label_kind, namespace_norm) "
                            "VALUES (?, ?, ?, ?)",
                            (entity["id"], normalized, label_kind, namespace),
                        )
                if entity.get("lifecycle") == "redirected" and entity.get("redirect_to"):
                    connection.execute(
                        "INSERT INTO entity_redirects(source_id, target_id) VALUES (?, ?)",
                        (entity["id"], entity["redirect_to"]),
                    )
            record_count = sum(
                len(cast(list[object], graph[item])) for item in dependencies.collections
            )
            metadata = {
                "index_schema_version": dependencies.index_schema_version,
                "graph_schema_version": dependencies.graph_schema_version,
                "canonical_sha256": canonical_digest,
                "canonical_watermark": str(query.committed_watermark(graph)),
                "built_at": dependencies.utc_now(),
                "record_count": str(record_count),
            }
            connection.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", metadata.items())
            connection.commit()
        finally:
            connection.close()
        dependencies.set_private_path_mode(temp_path, 0o600)
        dependencies.replace_file_atomically(temp_path, index_path)
        dependencies.fsync_parent_directory(index_path.parent)
        dependencies.set_private_path_mode(index_path, 0o600)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
    return {
        "status": "rebuilt",
        "path": str(index_path),
        "watermark": query.committed_watermark(graph),
        "record_count": record_count,
    }


def read_recall_index_meta(
    index_path: Path,
    *,
    sqlite_readonly_uri: Callable[[Path], str],
) -> dict[str, str]:
    if not index_path.is_file():
        return {}
    try:
        connection = sqlite3.connect(sqlite_readonly_uri(index_path), uri=True)
        try:
            return {
                str(key): str(value)
                for key, value in connection.execute("SELECT key, value FROM meta")
            }
        finally:
            connection.close()
    except sqlite3.Error:
        return {}


def recall_index_healthy(
    index_path: Path,
    *,
    full: bool,
    sqlite_readonly_uri: Callable[[Path], str],
) -> bool:
    if not index_path.is_file():
        return False
    required_tables = {
        "meta",
        "records",
        "records_fts",
        "record_entities",
        "entity_labels",
        "edges",
        "entity_redirects",
    }
    try:
        connection = sqlite3.connect(sqlite_readonly_uri(index_path), uri=True)
        try:
            if full:
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                if quick_check is None or str(quick_check[0]).lower() != "ok":
                    return False
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            if not required_tables <= tables:
                return False
            connection.execute("SELECT rowid FROM records_fts LIMIT 1").fetchone()
            return True
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def ensure_recall_index(
    graph: JsonObject,
    store_path: Path,
    canonical_digest: str,
    timeout_seconds: float,
    force: bool,
    *,
    dependencies: LegacyQueryIndexDependencies,
) -> JsonObject:
    def metadata_int(metadata: dict[str, str], key: str, default: int = 0) -> int:
        try:
            return int(metadata.get(key, str(default)))
        except (TypeError, ValueError):
            return default

    query = dependencies.query
    index_path = dependencies.recall_index_path(store_path)
    meta = dependencies.read_recall_index_meta(index_path)
    if (
        not force
        and dependencies.recall_index_healthy(index_path)
        and meta.get("index_schema_version") == dependencies.index_schema_version
        and meta.get("graph_schema_version") == dependencies.graph_schema_version
        and meta.get("canonical_sha256") == canonical_digest
        and meta.get("canonical_watermark") == str(query.committed_watermark(graph))
    ):
        return {
            "status": "current",
            "path": str(index_path),
            "watermark": query.committed_watermark(graph),
            "record_count": metadata_int(meta, "record_count"),
        }
    with dependencies.store_lock(index_path, timeout_seconds):
        latest_graph, latest_digest, latest_stored_version = dependencies.load_graph_with_digest(
            store_path
        )
        dependencies.require_current_graph_schema(latest_stored_version)
        if not hmac.compare_digest(latest_digest, canonical_digest):
            raise ValueError(
                "Canonical graph changed while the recall index lock was acquired; reload and retry"
            )
        graph = latest_graph
        latest_meta = dependencies.read_recall_index_meta(index_path)
        if (
            not force
            and dependencies.recall_index_healthy(index_path)
            and latest_meta.get("canonical_sha256") == canonical_digest
            and latest_meta.get("index_schema_version") == dependencies.index_schema_version
            and latest_meta.get("graph_schema_version") == dependencies.graph_schema_version
            and latest_meta.get("canonical_watermark") == str(query.committed_watermark(graph))
        ):
            return {
                "status": "current",
                "path": str(index_path),
                "watermark": query.committed_watermark(graph),
                "record_count": metadata_int(latest_meta, "record_count"),
            }
        return dependencies.create_recall_index(graph, index_path, canonical_digest)


def indexed_recall_candidates(
    index_path: Path,
    query_text: str,
    limit: int,
    expected_canonical_digest: str | None,
    *,
    exhaustive: bool,
    mode: str,
    dependencies: LegacyQueryIndexDependencies,
) -> set[str]:
    if mode not in {"review_safe", "operator_full"}:
        raise ValueError("Recall mode must be review_safe or operator_full")
    query = dependencies.query
    candidates: set[str] = set()
    connection = sqlite3.connect(query.sqlite_readonly_uri(index_path), uri=True)
    try:
        if expected_canonical_digest is not None:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'canonical_sha256'"
            ).fetchone()
            if row is None or not hmac.compare_digest(str(row[0]), expected_canonical_digest):
                raise ValueError("Recall index changed to a different canonical snapshot")
        exact = connection.execute(
            "SELECT record_id FROM records WHERE record_id = ?",
            (query.normalize_space(query_text),),
        ).fetchone()
        if exact:
            candidates.add(str(exact[0]))
        candidates.update(
            str(row[0])
            for row in connection.execute(
                "SELECT entity_id FROM entity_labels WHERE label_norm = ?",
                (query.normalize_key(query_text),),
            )
        )
        tokens = sorted(query.tokenize(query_text))
        if tokens:
            expression = " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)
            if exhaustive:
                rows = connection.execute(
                    "SELECT record_id FROM records_fts WHERE records_fts MATCH ? "
                    "ORDER BY bm25(records_fts)",
                    (expression,),
                )
            else:
                rows = connection.execute(
                    "SELECT record_id FROM records_fts WHERE records_fts MATCH ? "
                    "ORDER BY bm25(records_fts) LIMIT ?",
                    (expression, max(200, limit * 30)),
                )
            candidates.update(str(row[0]) for row in rows)
        seed_entities = {
            str(record_id)
            for record_id, collection in connection.execute(
                "SELECT record_id, collection FROM records WHERE record_id IN ({})".format(
                    ",".join("?" for _ in candidates) or "NULL"
                ),
                tuple(candidates),
            )
            if collection == "entities"
        }
        if seed_entities:
            placeholders = ",".join("?" for _ in seed_entities)
            redirected_targets = {
                str(row[0])
                for row in connection.execute(
                    f"SELECT target_id FROM entity_redirects WHERE source_id IN ({placeholders})",
                    tuple(seed_entities),
                )
            }
            seed_entities.update(redirected_targets)
            candidates.update(redirected_targets)
            placeholders = ",".join("?" for _ in seed_entities)
            candidates.update(
                str(row[0])
                for row in connection.execute(
                    f"SELECT record_id FROM record_entities WHERE entity_id IN ({placeholders})",
                    tuple(seed_entities),
                )
            )
            neighbors: set[str] = set()
            visibility_clause = " AND review_safe_visible = 1" if mode == "review_safe" else ""
            for source_id, target_id in connection.execute(
                f"SELECT source_id, target_id FROM edges WHERE "
                f"(source_id IN ({placeholders}) OR target_id IN ({placeholders}))"
                f"{visibility_clause}",
                (*seed_entities, *seed_entities),
            ):
                neighbors.update((str(source_id), str(target_id)))
            if neighbors:
                neighbor_placeholders = ",".join("?" for _ in neighbors)
                candidates.update(
                    str(row[0])
                    for row in connection.execute(
                        f"SELECT record_id FROM record_entities "
                        f"WHERE entity_id IN ({neighbor_placeholders})",
                        tuple(neighbors),
                    )
                )
    finally:
        connection.close()
    return candidates
