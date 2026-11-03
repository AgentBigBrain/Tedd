"""Snapshot export, backup publication, and store-maintenance kernels for v4.

The flat storage module remains the compatibility facade.  It supplies live
filesystem and policy callbacks for every call so tests and embedders can still
replace those seams without this module importing the facade back into itself.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from tedd_knowledge_graph.v4_queries import GraphState, JsonValue

JsonObject = dict[str, JsonValue]
ExportGraph = dict[str, JsonValue]
PublishedIdentity = tuple[int, int]


class SnapshotLoader(Protocol):
    """Call shape of the facade-owned snapshot method."""

    def __call__(
        self,
        *,
        schema_version: str,
        include_journal: bool,
    ) -> Mapping[str, JsonValue]: ...


class TemporaryFileFactory(Protocol):
    """Narrow call shape used to stage one same-directory backup."""

    def __call__(
        self,
        *,
        prefix: str,
        suffix: str,
        dir: Path,
    ) -> tuple[int, str]: ...


class StageBytes(Protocol):
    def __call__(self, path: Path, content: bytes, mode: int = 0o600) -> Path: ...


class PublishStagedFile(Protocol):
    def __call__(
        self,
        source: Path,
        destination: Path,
        *,
        overwrite: bool,
    ) -> PublishedIdentity: ...


class ReadPublishedBytes(Protocol):
    def __call__(
        self,
        path: Path,
        expected_identity: PublishedIdentity,
        *,
        maximum_bytes: int,
    ) -> bytes: ...


class FileDigest(Protocol):
    def update(self, value: bytes) -> None: ...

    def hexdigest(self) -> str: ...


class JsonDumper(Protocol):
    def __call__(
        self,
        value: object,
        *,
        indent: int,
        sort_keys: bool,
    ) -> str: ...


class PathFactory(Protocol):
    def __call__(self, value: str | Path) -> Path: ...


@dataclass(frozen=True)
class ExportDependencies:
    """Facade policy resolved for one deterministic export snapshot."""

    collections: Sequence[str]
    current_schema_version: str
    json_loads: Callable[[str], object]
    stable_hash: Callable[[object], str]
    state_loader: Callable[[], GraphState]


@dataclass(frozen=True)
class CheckpointDependencies:
    """Facade policy resolved for one audit-checkpoint write."""

    state_loader: Callable[[], GraphState]
    state_root_loader: Callable[[], str]
    normalize_space: Callable[[object], str]
    utc_now: Callable[[], str]
    v4_error_type: type[Exception]
    validation_error_type: type[Exception]


@dataclass(frozen=True)
class BackupDependencies:
    """All filesystem and serialization policy used by online backup."""

    path_factory: PathFactory
    assert_safe_path: Callable[[Path], Path]
    ensure_private_directory: Callable[[Path], None]
    make_temporary_file: TemporaryFileFactory
    close_descriptor: Callable[[int], None]
    connect: Callable[[Path], sqlite3.Connection]
    set_private_path_mode: Callable[[Path, int], None]
    fsync_descriptor: Callable[[int], None]
    digest_factory: Callable[[], FileDigest]
    json_dumps: JsonDumper
    stage_bytes: StageBytes
    publish_staged_file: PublishStagedFile
    fsync_parent: Callable[[Path], None]
    sha256_verified_published_file: Callable[[Path, PublishedIdentity], tuple[str, int]]
    read_verified_published_bytes: ReadPublishedBytes
    utc_now: Callable[[], str]
    v4_error_type: type[Exception]
    store_format_error_type: type[Exception]
    validation_error_type: type[Exception]


def _load_json(value: str, loader: Callable[[str], object]) -> JsonValue:
    """Narrow the validated canonical JSON crossing SQLite's dynamic boundary."""

    return cast(JsonValue, loader(value))


def export_current_graph(
    connection: sqlite3.Connection,
    *,
    schema_version: str,
    include_journal: bool,
    snapshot_loader: SnapshotLoader,
    v4_error_type: type[Exception],
) -> Mapping[str, JsonValue]:
    """Read one historic SQLite snapshot without leaving a transaction open."""

    if connection.in_transaction:
        raise v4_error_type("Export requires a connection with no active transaction")
    connection.execute("BEGIN")
    try:
        return snapshot_loader(
            schema_version=schema_version,
            include_journal=include_journal,
        )
    finally:
        # A read-only rollback ends the historic view without implying a write.
        connection.rollback()


def export_current_graph_snapshot(
    connection: sqlite3.Connection,
    *,
    schema_version: str,
    include_journal: bool,
    dependencies: ExportDependencies,
) -> ExportGraph:
    """Build the deterministic v3/v4 JSON view for an active read snapshot."""

    if schema_version not in {"v3", dependencies.current_schema_version}:
        raise ValueError("Export schema must be v3 or v4")

    collection_records: dict[str, list[JsonValue]] = {
        collection: [] for collection in dependencies.collections
    }
    rows = connection.execute(
        """
        SELECT r.collection,p.payload_json FROM records r
        JOIN record_revision_payloads p
          ON p.record_id=r.record_id AND p.revision=r.current_revision
        ORDER BY r.collection,r.record_id
        """
    )
    for row in rows:
        collection_records[str(row["collection"])].append(
            _load_json(str(row["payload_json"]), dependencies.json_loads)
        )

    state = dependencies.state_loader()
    journal: list[JsonValue] = []
    graph: ExportGraph = {
        **collection_records,
        "schema_version": schema_version,
        "updated_at": state["updated_at"],
        "next_watermark": int(state["watermark"]) + 1,
        "journal_anchor": {
            "watermark": state["journal_anchor_watermark"],
            "entry_hash": state["journal_anchor_hash"],
        },
        "journal": journal,
    }
    if include_journal:
        for mutation in connection.execute("SELECT * FROM mutations ORDER BY watermark"):
            legacy_header_json = mutation["legacy_header_json"]
            if legacy_header_json:
                journal.append(_load_json(str(legacy_header_json), dependencies.json_loads))
                continue
            changed_ids: list[JsonValue] = [
                str(change_row[0])
                for change_row in connection.execute(
                    "SELECT record_id FROM mutation_changes WHERE watermark=? ORDER BY ordinal",
                    (mutation["watermark"],),
                )
            ]
            entry: JsonObject = {
                "id": cast(JsonValue, mutation["mutation_id"]),
                "watermark": cast(JsonValue, mutation["watermark"]),
                "action": cast(JsonValue, mutation["action"]),
                "discovery_kind": cast(JsonValue, mutation["discovery_kind"]),
                "fingerprint": cast(JsonValue, mutation["fingerprint"]),
                "fingerprint_version": cast(JsonValue, mutation["fingerprint_version"]),
                "captured_at": cast(JsonValue, mutation["captured_at"]),
                "changed_ids": changed_ids,
                "previous_entry_hash": cast(JsonValue, mutation["previous_entry_hash"]),
                "entry_hash": cast(JsonValue, mutation["entry_hash"]),
                "hash_algorithm": cast(JsonValue, mutation["hash_algorithm"]),
                "redaction_count": cast(JsonValue, mutation["redaction_count"]),
            }
            producer: JsonObject = {
                key: cast(JsonValue, mutation[column])
                for key, column in (
                    ("agent_id", "producer_agent_id"),
                    ("task_id", "task_id"),
                    ("parent_task_id", "parent_task_id"),
                    ("run_id", "run_id"),
                )
                if mutation[column]
            }
            if producer:
                entry["producer"] = producer
            if mutation["based_on_watermark"] is not None:
                entry["based_on_watermark"] = cast(JsonValue, mutation["based_on_watermark"])
            if mutation["source_ref"]:
                entry["source_ref"] = cast(JsonValue, mutation["source_ref"])
            if mutation["capture_policy_json"]:
                entry["capture_policy"] = _load_json(
                    str(mutation["capture_policy_json"]),
                    dependencies.json_loads,
                )
            if mutation["details_json"]:
                entry["details"] = _load_json(
                    str(mutation["details_json"]),
                    dependencies.json_loads,
                )
            journal.append(entry)

    if schema_version == "v3" and journal:
        anchor = cast(JsonObject, graph["journal_anchor"])
        previous_hash = str(anchor["entry_hash"])
        for value in journal:
            entry = cast(JsonObject, value)
            entry.pop("entry_hash", None)
            entry.pop("hash_algorithm", None)
            entry["previous_entry_hash"] = previous_hash
            entry_hash = dependencies.stable_hash({"previous_hash": previous_hash, "entry": entry})
            entry["entry_hash"] = entry_hash
            previous_hash = entry_hash
    if schema_version == dependencies.current_schema_version:
        graph["store_id"] = state["store_id"]
        graph["store_generation"] = state["generation"]
    return graph


def checkpoint(
    connection: sqlite3.Connection,
    store_path: Path,
    mode: str = "PASSIVE",
    *,
    path_factory: PathFactory = Path,
) -> dict[str, int | str]:
    """Checkpoint the WAL using the facade-validated writable connection."""

    normalized_mode = str(mode).upper()
    if normalized_mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
        raise ValueError("Checkpoint mode must be PASSIVE, FULL, RESTART, or TRUNCATE")
    row = connection.execute(f"PRAGMA wal_checkpoint({normalized_mode})").fetchone()
    wal_path = path_factory(f"{store_path}-wal")
    return {
        "mode": normalized_mode,
        "busy": int(row[0]),
        "wal_pages": int(row[1]),
        "checkpointed_pages": int(row[2]),
        "wal_bytes": wal_path.stat().st_size if wal_path.exists() else 0,
    }


def create_audit_checkpoint(
    connection: sqlite3.Connection,
    *,
    backup_manifest_hash: str | None,
    dependencies: CheckpointDependencies,
) -> dict[str, JsonValue]:
    """Record one idempotent checkpoint of the current durable graph state."""

    if connection.in_transaction:
        raise dependencies.v4_error_type(
            "Audit checkpoint requires a connection with no active transaction"
        )
    backup_hash = dependencies.normalize_space(backup_manifest_hash) or None
    transaction_started = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        state = dependencies.state_loader()
        if int(state["watermark"]) <= 0:
            raise dependencies.v4_error_type("Cannot checkpoint an empty mutation journal")
        root = dependencies.state_root_loader()
        now = dependencies.utc_now()
        existing = connection.execute(
            "SELECT journal_head_hash,current_state_root,backup_manifest_hash "
            "FROM audit_checkpoints WHERE watermark=?",
            (state["watermark"],),
        ).fetchone()
        expected = (
            state["journal_head_hash"],
            root,
            backup_hash,
        )
        if existing and tuple(existing) != expected:
            raise dependencies.validation_error_type(
                "Checkpoint already exists with different state"
            )
        connection.execute(
            "INSERT OR IGNORE INTO audit_checkpoints "
            "(watermark,journal_head_hash,current_state_root,backup_manifest_hash,created_at) "
            "VALUES(?,?,?,?,?)",
            (*((state["watermark"],) + expected), now),
        )
        connection.commit()
        transaction_started = False
    except BaseException:
        if transaction_started:
            connection.rollback()
        raise
    return {
        "watermark": state["watermark"],
        "journal_head_hash": state["journal_head_hash"],
        "current_state_root": root,
        "backup_manifest_hash": backup_hash,
        "created_at": now,
    }


def backup(
    connection: sqlite3.Connection,
    source_path: Path,
    destination: str | Path,
    *,
    overwrite: bool = False,
    dependencies: BackupDependencies,
) -> dict[str, JsonValue]:
    """Create a verified SQLite snapshot whose manifest is the completion marker."""

    if overwrite:
        raise ValueError("Backup overwrite is not supported; choose a new destination")
    if connection.in_transaction:
        raise dependencies.v4_error_type("Backup requires a connection with no active transaction")
    destination_path = dependencies.assert_safe_path(dependencies.path_factory(destination))
    if destination_path.resolve() == source_path.resolve():
        raise ValueError("Backup destination cannot be the live store")
    dependencies.ensure_private_directory(destination_path.parent)
    manifest_path = dependencies.assert_safe_path(
        destination_path.with_name(f"{destination_path.name}.manifest.json")
    )
    for output_path in (destination_path, manifest_path):
        if output_path.exists():
            raise FileExistsError(output_path)

    descriptor, temp_name = dependencies.make_temporary_file(
        prefix=f".{destination_path.name}.",
        suffix=".backup",
        dir=destination_path.parent,
    )
    dependencies.close_descriptor(descriptor)
    temp_path = dependencies.path_factory(temp_name)
    with contextlib.suppress(FileNotFoundError):
        temp_path.unlink()
    manifest_temp_path: Path | None = None
    try:
        backup_connection = dependencies.connect(temp_path)
        backup_connection.row_factory = sqlite3.Row
        try:
            connection.backup(backup_connection)
            backup_connection.execute("PRAGMA journal_mode=DELETE")
            backup_connection.execute("PRAGMA synchronous=FULL")
            check = backup_connection.execute("PRAGMA quick_check").fetchone()
            if check is None or str(check[0]).lower() != "ok":
                raise dependencies.validation_error_type("Online backup failed SQLite quick_check")
            snapshot = backup_connection.execute(
                "SELECT store_id,generation,committed_watermark,journal_head_hash "
                "FROM graph_state WHERE singleton=1"
            ).fetchone()
            if snapshot is None:
                raise dependencies.store_format_error_type("Online backup is missing graph_state")
        finally:
            backup_connection.close()

        dependencies.set_private_path_mode(temp_path, 0o600)
        with temp_path.open("rb+") as handle:
            dependencies.fsync_descriptor(handle.fileno())
        digest = dependencies.digest_factory()
        with temp_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        backup_size = temp_path.stat().st_size
        digest_text = digest.hexdigest()
        manifest: JsonObject = {
            "format": "knowledge-graph-v4-sqlite-backup",
            "created_at": dependencies.utc_now(),
            "source_path": str(source_path),
            "backup_path": str(destination_path),
            "sha256": digest_text,
            "bytes": backup_size,
            "store_id": cast(JsonValue, snapshot["store_id"]),
            "generation": cast(JsonValue, snapshot["generation"]),
            "watermark": cast(JsonValue, snapshot["committed_watermark"]),
            "journal_head_hash": cast(JsonValue, snapshot["journal_head_hash"]),
            "plan_key_included": False,
        }
        manifest_bytes = (
            dependencies.json_dumps(manifest, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        # Stage and sync both files before publishing either final pathname.
        manifest_temp_path = dependencies.stage_bytes(manifest_path, manifest_bytes)
        backup_identity = dependencies.publish_staged_file(
            temp_path,
            destination_path,
            overwrite=False,
        )
        dependencies.fsync_parent(destination_path.parent)

        # The manifest is the completion marker. Verify the durable backup
        # before publishing it so failed verification leaves an explicit orphan.
        final_digest, final_size = dependencies.sha256_verified_published_file(
            destination_path,
            backup_identity,
        )
        if final_digest != digest_text or final_size != backup_size:
            raise dependencies.validation_error_type("Published backup does not match its manifest")

        manifest_identity = dependencies.publish_staged_file(
            manifest_temp_path,
            manifest_path,
            overwrite=False,
        )
        dependencies.fsync_parent(destination_path.parent)
        published_manifest = dependencies.read_verified_published_bytes(
            manifest_path,
            manifest_identity,
            maximum_bytes=max(4_096, len(manifest_bytes)),
        )
        if published_manifest != manifest_bytes:
            raise dependencies.validation_error_type(
                "Published backup manifest changed before verification"
            )
        return {**manifest, "manifest_path": str(manifest_path)}
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()
        if manifest_temp_path is not None:
            with contextlib.suppress(FileNotFoundError):
                manifest_temp_path.unlink()


__all__ = [
    "BackupDependencies",
    "CheckpointDependencies",
    "ExportDependencies",
    "backup",
    "checkpoint",
    "create_audit_checkpoint",
    "export_current_graph",
    "export_current_graph_snapshot",
]
