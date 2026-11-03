#!/usr/bin/env python3
"""Read-only validation kernel for the SQLite v4 knowledge graph.

The storage facade keeps the public methods and supplies its live constants and
callbacks on every call.  This leaf owns validation ordering and SQL checks but
does not open connections, start transactions, mutate storage, or import the v4
facade back into itself.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypedDict, cast

from tedd_knowledge_graph.v4_queries import GraphState, JsonValue

JsonObject = dict[str, JsonValue]


class ValidationResult(TypedDict):
    """Stable public result produced by v4 store validation."""

    valid: bool
    full: bool
    errors: list[str]
    warnings: list[str]
    state: GraphState
    state_root: str | None
    counts: dict[str, int]


class Digest(Protocol):
    """Minimum hashing interface needed by :func:`state_root`."""

    def update(self, data: bytes, /) -> None: ...

    def hexdigest(self) -> str: ...


DigestFactory = Callable[[], Digest]
JsonLoader = Callable[[str], object]


@dataclass(frozen=True)
class ValidationDependencies:
    """Runtime-resolved v4 policy and compatibility callbacks."""

    collections: Sequence[str]
    hash_algorithm: str
    legacy_hash_algorithm: str
    sha256_text: Callable[[str], str]
    stable_hash: Callable[[object], str]
    compare_digest: Callable[[str, str], bool]
    json_loads: JsonLoader
    state_loader: Callable[[], Mapping[str, object]]
    state_root_loader: Callable[[], str]
    entry_material_loader: Callable[[sqlite3.Row], Mapping[str, object]]


def _require_json_value(value: object) -> JsonValue:
    """Narrow a decoded value after recursively proving JSON compatibility."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        for item in value:
            _require_json_value(item)
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("Decoded JSON objects must use string keys")
        for item in value.values():
            _require_json_value(item)
        return cast(JsonObject, value)
    raise TypeError(f"Decoded value is not JSON-compatible: {type(value).__name__}")


def _require_json_object(value: object, label: str) -> JsonObject:
    narrowed = _require_json_value(value)
    if not isinstance(narrowed, dict):
        raise TypeError(f"{label} must be a JSON object")
    return narrowed


def _require_graph_state(value: Mapping[str, object]) -> GraphState:
    """Validate the exact graph-state fields consumed and disclosed here."""

    text_fields = (
        "schema_version",
        "store_id",
        "journal_head_hash",
        "journal_anchor_hash",
        "created_at",
        "updated_at",
    )
    integer_fields = ("generation", "watermark", "journal_anchor_watermark")
    for field_name in text_fields:
        if not isinstance(value.get(field_name), str):
            raise TypeError(f"Graph state {field_name} must be a string")
    for field_name in integer_fields:
        field_value = value.get(field_name)
        if not isinstance(field_value, int) or isinstance(field_value, bool):
            raise TypeError(f"Graph state {field_name} must be an integer")
    return cast(GraphState, dict(value))


def _sqlite_json_scalar(value: object, field_name: str) -> JsonValue:
    """Validate one SQLite value before placing it in canonical hash material."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"Mutation {field_name} is not a JSON scalar")


def state_root(
    connection: sqlite3.Connection,
    *,
    digest_factory: DigestFactory = hashlib.sha256,
) -> str:
    """Hash the ordered current-record state without changing the connection."""

    digest = digest_factory()
    for row in connection.execute(
        "SELECT record_id,current_revision,current_payload_hash FROM records ORDER BY record_id"
    ):
        digest.update(str(row["record_id"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(row["current_revision"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(row["current_payload_hash"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def entry_material(
    connection: sqlite3.Connection,
    mutation: sqlite3.Row,
    *,
    json_loads: JsonLoader = json.loads,
) -> JsonObject:
    """Reconstruct the canonical material hashed for one v4 mutation."""

    changes: list[JsonValue] = [
        {
            "record_id": _sqlite_json_scalar(row["record_id"], "record_id"),
            "collection": _sqlite_json_scalar(row["collection"], "collection"),
            "revision": _sqlite_json_scalar(row["revision"], "revision"),
            "payload_hash": _sqlite_json_scalar(row["payload_hash"], "payload_hash"),
            "change_kind": _sqlite_json_scalar(row["change_kind"], "change_kind"),
            "ordinal": _sqlite_json_scalar(row["ordinal"], "ordinal"),
        }
        for row in connection.execute(
            "SELECT * FROM mutation_changes WHERE watermark=? ORDER BY ordinal",
            (mutation["watermark"],),
        )
    ]
    return {
        "watermark": _sqlite_json_scalar(mutation["watermark"], "watermark"),
        "action": _sqlite_json_scalar(mutation["action"], "action"),
        "discovery_kind": _sqlite_json_scalar(mutation["discovery_kind"], "discovery_kind"),
        "fingerprint": _sqlite_json_scalar(mutation["fingerprint"], "fingerprint"),
        "fingerprint_version": _sqlite_json_scalar(
            mutation["fingerprint_version"], "fingerprint_version"
        ),
        "producer_agent_id": _sqlite_json_scalar(
            mutation["producer_agent_id"], "producer_agent_id"
        ),
        "task_id": _sqlite_json_scalar(mutation["task_id"], "task_id"),
        "parent_task_id": _sqlite_json_scalar(mutation["parent_task_id"], "parent_task_id"),
        "run_id": _sqlite_json_scalar(mutation["run_id"], "run_id"),
        "based_on_watermark": _sqlite_json_scalar(
            mutation["based_on_watermark"], "based_on_watermark"
        ),
        "captured_at": _sqlite_json_scalar(mutation["captured_at"], "captured_at"),
        "source_ref": _sqlite_json_scalar(mutation["source_ref"], "source_ref"),
        "capture_policy": (
            _require_json_value(json_loads(str(mutation["capture_policy_json"])))
            if mutation["capture_policy_json"]
            else None
        ),
        "details": (
            _require_json_value(json_loads(str(mutation["details_json"])))
            if mutation["details_json"]
            else None
        ),
        "redaction_count": _sqlite_json_scalar(mutation["redaction_count"], "redaction_count"),
        "changes": changes,
    }


def _append_sqlite_errors(
    connection: sqlite3.Connection,
    *,
    full: bool,
    errors: list[str],
) -> None:
    pragma = "integrity_check" if full else "quick_check"
    integrity = [str(row[0]) for row in connection.execute(f"PRAGMA {pragma}")]
    if integrity != ["ok"]:
        errors.extend(f"sqlite:{item}" for item in integrity)
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        errors.extend(f"foreign_key:{row[0]}:{row[1]}:{row[2]}" for row in foreign_keys[:100])


def _append_state_tail_errors(
    connection: sqlite3.Connection,
    state: GraphState,
    errors: list[str],
) -> None:
    last = connection.execute(
        "SELECT watermark,entry_hash FROM mutations ORDER BY watermark DESC LIMIT 1"
    ).fetchone()
    expected_watermark = int(last["watermark"]) if last else int(state["journal_anchor_watermark"])
    expected_head = str(last["entry_hash"]) if last else str(state["journal_anchor_hash"])
    if int(state["watermark"]) != expected_watermark:
        errors.append("graph_state watermark disagrees with mutation tail")
    if str(state["journal_head_hash"]) != expected_head:
        errors.append("graph_state journal head disagrees with mutation tail")


def _append_current_record_errors(
    connection: sqlite3.Connection,
    errors: list[str],
) -> int:
    dangling_current = connection.execute(
        """
        SELECT COUNT(*) FROM records r
        LEFT JOIN record_revision_headers h
          ON h.record_id=r.record_id AND h.revision=r.current_revision
        LEFT JOIN record_revision_payloads p
          ON p.record_id=r.record_id AND p.revision=r.current_revision
        WHERE h.record_id IS NULL OR p.record_id IS NULL
        """
    ).fetchone()[0]
    if dangling_current:
        errors.append(f"{dangling_current} records lack a current revision/payload")
    fts_count = connection.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0]
    record_count = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    if fts_count != record_count:
        errors.append(f"FTS count {fts_count} differs from record count {record_count}")
    fts_mismatch = connection.execute(
        """
        SELECT COUNT(*) FROM records r
        LEFT JOIN records_fts f ON f.rowid=r.rowid
        WHERE f.rowid IS NULL OR f.record_id<>r.record_id
          OR f.collection<>r.collection OR f.search_text<>r.search_text
        """
    ).fetchone()[0]
    if fts_mismatch:
        errors.append(f"{fts_mismatch} current FTS documents disagree with records")
    return int(record_count)


def _append_revision_and_journal_errors(
    connection: sqlite3.Connection,
    state: GraphState,
    dependencies: ValidationDependencies,
    errors: list[str],
) -> None:
    for row in connection.execute(
        """
        SELECT h.record_id,h.revision,h.payload_hash,p.payload_json
        FROM record_revision_headers h
        LEFT JOIN record_revision_payloads p
          ON p.record_id=h.record_id AND p.revision=h.revision
        ORDER BY h.record_id,h.revision
        """
    ):
        if row["payload_json"] is None:
            # An explicitly erased historical payload retains only its header/hash.
            continue
        if dependencies.sha256_text(row["payload_json"]) != row["payload_hash"]:
            errors.append(f"revision payload hash mismatch: {row['record_id']}@{row['revision']}")
            if len(errors) >= 200:
                break

    previous = str(state["journal_anchor_hash"])
    previous_watermark = int(state["journal_anchor_watermark"])
    for mutation in connection.execute("SELECT * FROM mutations ORDER BY watermark"):
        if int(mutation["watermark"]) != previous_watermark + 1:
            errors.append(f"journal watermark gap before {mutation['watermark']}")
        if str(mutation["previous_entry_hash"]) != previous:
            errors.append(f"journal previous hash mismatch at {mutation['watermark']}")
        if mutation["hash_algorithm"] == dependencies.hash_algorithm:
            expected = dependencies.stable_hash(
                {
                    "previous_hash": previous,
                    "entry": dependencies.entry_material_loader(mutation),
                }
            )
        elif mutation["hash_algorithm"] == dependencies.legacy_hash_algorithm:
            legacy = _require_json_object(
                dependencies.json_loads(str(mutation["legacy_header_json"] or "{}")),
                "Legacy mutation header",
            )
            body = {key: value for key, value in legacy.items() if key != "entry_hash"}
            expected = dependencies.stable_hash({"previous_hash": previous, "entry": body})
        else:
            errors.append(f"unsupported journal hash algorithm at {mutation['watermark']}")
            expected = mutation["entry_hash"]
        if not dependencies.compare_digest(str(mutation["entry_hash"]), expected):
            errors.append(f"journal entry hash mismatch at {mutation['watermark']}")
        previous = str(mutation["entry_hash"])
        previous_watermark = int(mutation["watermark"])


def _append_registry_errors(
    connection: sqlite3.Connection,
    errors: list[str],
) -> None:
    bad_identity_owner = connection.execute(
        """
        SELECT COUNT(*) FROM entity_identity_keys k
        JOIN records r ON r.record_id=k.entity_id
        WHERE r.collection<>'entities'
        """
    ).fetchone()[0]
    if bad_identity_owner:
        errors.append(f"{bad_identity_owner} identity keys point to non-entities")
    bad_surface = connection.execute(
        """
        SELECT COUNT(*) FROM claim_surface_constraints c
        JOIN records r ON r.record_id=c.surface_id
        WHERE r.collection<>'claim_surfaces'
        """
    ).fetchone()[0]
    if bad_surface:
        errors.append(f"{bad_surface} surface constraints point to wrong collection")
    bad_current_claim = connection.execute(
        """
        SELECT COUNT(*) FROM claim_current_registry c
        JOIN records r ON r.record_id=c.claim_id
        JOIN record_revision_payloads p
          ON p.record_id=r.record_id AND p.revision=r.current_revision
        WHERE r.collection<>'claims'
           OR COALESCE(json_extract(p.payload_json,'$.active'),0)<>1
           OR COALESCE(json_extract(p.payload_json,'$.lifecycle'),'')<>'current'
           OR COALESCE(json_extract(p.payload_json,'$.surface_ref'),'')<>c.surface_id
        """
    ).fetchone()[0]
    if bad_current_claim:
        errors.append(f"{bad_current_claim} current-claim registry rows disagree with payloads")
    expected_current_claims = connection.execute(
        """
        SELECT COUNT(*) FROM records r
        JOIN record_revision_payloads p
          ON p.record_id=r.record_id AND p.revision=r.current_revision
        WHERE r.collection='claims'
          AND COALESCE(json_extract(p.payload_json,'$.active'),0)=1
          AND COALESCE(json_extract(p.payload_json,'$.lifecycle'),'')='current'
        """
    ).fetchone()[0]
    registered_current_claims = connection.execute(
        "SELECT COUNT(*) FROM claim_current_registry"
    ).fetchone()[0]
    if expected_current_claims != registered_current_claims:
        errors.append("current-claim registry count disagrees with active/current claim payloads")


def _counts(
    connection: sqlite3.Connection,
    collections: Sequence[str],
    record_count: int,
) -> dict[str, int]:
    counts = {
        collection: int(
            connection.execute(
                "SELECT COUNT(*) FROM records WHERE collection=?", (collection,)
            ).fetchone()[0]
        )
        for collection in collections
    }
    counts.update(
        {
            "records": record_count,
            "revisions": int(
                connection.execute("SELECT COUNT(*) FROM record_revision_headers").fetchone()[0]
            ),
            "mutations": int(connection.execute("SELECT COUNT(*) FROM mutations").fetchone()[0]),
        }
    )
    return counts


def validate_store(
    connection: sqlite3.Connection,
    *,
    full: bool,
    dependencies: ValidationDependencies,
) -> ValidationResult:
    """Validate SQL and logical state while preserving the facade's result contract."""

    errors: list[str] = []
    warnings: list[str] = []
    _append_sqlite_errors(connection, full=full, errors=errors)
    state = _require_graph_state(dependencies.state_loader())
    _append_state_tail_errors(connection, state, errors)
    record_count = _append_current_record_errors(connection, errors)
    if full:
        _append_revision_and_journal_errors(connection, state, dependencies, errors)
        _append_registry_errors(connection, errors)
    counts = _counts(connection, dependencies.collections, record_count)
    return {
        "valid": not errors,
        "full": bool(full),
        "errors": errors,
        "warnings": warnings,
        "state": state,
        "state_root": dependencies.state_root_loader() if full else None,
        "counts": counts,
    }


__all__ = [
    "ValidationDependencies",
    "entry_material",
    "state_root",
    "validate_store",
]
