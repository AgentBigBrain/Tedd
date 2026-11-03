"""Canonical SQLite schema definition for the v4 knowledge graph.

The public :mod:`knowledge_graph_v4` module re-exports these constants.  This
leaf owns only the persisted schema contract; it opens no connections and has
no filesystem or migration side effects.
"""

from __future__ import annotations

SCHEMA_VERSION = "v4"
PREVIOUS_SCHEMA_USER_VERSION = 4
SCHEMA_USER_VERSION = 5

RECORD_REVISION_PAYLOADS_TABLE_SQL = r"""CREATE TABLE record_revision_payloads (
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    PRIMARY KEY(record_id, revision),
    FOREIGN KEY(record_id, revision)
      REFERENCES record_revision_headers(record_id, revision)
) STRICT"""

LEGACY_APPEND_ONLY_TRIGGERS = (
    (
        "immutable_revision_headers_update",
        r"""CREATE TRIGGER immutable_revision_headers_update
BEFORE UPDATE ON record_revision_headers BEGIN
  SELECT RAISE(ABORT, 'record revision headers are append-only');
END""",
    ),
    (
        "immutable_revision_headers_delete",
        r"""CREATE TRIGGER immutable_revision_headers_delete
BEFORE DELETE ON record_revision_headers BEGIN
  SELECT RAISE(ABORT, 'record revision headers are append-only');
END""",
    ),
    (
        "immutable_mutations_update",
        r"""CREATE TRIGGER immutable_mutations_update
BEFORE UPDATE ON mutations BEGIN
  SELECT RAISE(ABORT, 'mutations are append-only');
END""",
    ),
    (
        "immutable_mutations_delete",
        r"""CREATE TRIGGER immutable_mutations_delete
BEFORE DELETE ON mutations BEGIN
  SELECT RAISE(ABORT, 'mutations are append-only');
END""",
    ),
    (
        "immutable_mutation_changes_update",
        r"""CREATE TRIGGER immutable_mutation_changes_update
BEFORE UPDATE ON mutation_changes BEGIN
  SELECT RAISE(ABORT, 'mutation changes are append-only');
END""",
    ),
    (
        "immutable_mutation_changes_delete",
        r"""CREATE TRIGGER immutable_mutation_changes_delete
BEFORE DELETE ON mutation_changes BEGIN
  SELECT RAISE(ABORT, 'mutation changes are append-only');
END""",
    ),
)

APPEND_ONLY_UPGRADE_TRIGGERS = (
    (
        "immutable_revision_headers_insert",
        r"""CREATE TRIGGER immutable_revision_headers_insert
BEFORE INSERT ON record_revision_headers
WHEN EXISTS (
  SELECT 1 FROM record_revision_headers
  WHERE record_id=NEW.record_id AND revision=NEW.revision
) BEGIN
  SELECT RAISE(ABORT, 'record revision headers are append-only');
END""",
    ),
    (
        "immutable_revision_payloads_insert",
        r"""CREATE TRIGGER immutable_revision_payloads_insert
BEFORE INSERT ON record_revision_payloads
WHEN EXISTS (
  SELECT 1 FROM record_revision_payloads
  WHERE record_id=NEW.record_id AND revision=NEW.revision
) BEGIN
  SELECT RAISE(ABORT, 'record revision payloads are immutable');
END""",
    ),
    (
        "immutable_revision_payloads_update",
        r"""CREATE TRIGGER immutable_revision_payloads_update
BEFORE UPDATE ON record_revision_payloads BEGIN
  SELECT RAISE(ABORT, 'record revision payloads are immutable');
END""",
    ),
    (
        "immutable_revision_payloads_delete",
        r"""CREATE TRIGGER immutable_revision_payloads_delete
BEFORE DELETE ON record_revision_payloads
WHEN NOT EXISTS (
  SELECT 1
  FROM records AS current
  JOIN record_revision_headers AS header
    ON header.record_id=current.record_id
   AND header.revision=current.current_revision
  JOIN record_revision_payloads AS payload
    ON payload.record_id=current.record_id
   AND payload.revision=current.current_revision
  WHERE current.record_id=OLD.record_id
    AND OLD.revision<current.current_revision
    AND header.change_kind='redact'
    AND lower(trim(CAST(
      json_extract(payload.payload_json, '$.lifecycle') AS TEXT
    )))='redacted'
) BEGIN
  SELECT RAISE(ABORT, 'payload deletion requires a current redaction revision');
END""",
    ),
    (
        "immutable_mutations_insert",
        r"""CREATE TRIGGER immutable_mutations_insert
BEFORE INSERT ON mutations
WHEN EXISTS (
  SELECT 1 FROM mutations
  WHERE watermark=NEW.watermark
     OR mutation_id=NEW.mutation_id
     OR entry_hash=NEW.entry_hash
) BEGIN
  SELECT RAISE(ABORT, 'mutations are append-only');
END""",
    ),
    (
        "immutable_mutation_changes_insert",
        r"""CREATE TRIGGER immutable_mutation_changes_insert
BEFORE INSERT ON mutation_changes
WHEN EXISTS (
  SELECT 1 FROM mutation_changes
  WHERE watermark=NEW.watermark AND (
    ordinal=NEW.ordinal OR record_id=NEW.record_id
  )
) BEGIN
  SELECT RAISE(ABORT, 'mutation changes are append-only');
END""",
    ),
)
APPEND_ONLY_TRIGGERS = LEGACY_APPEND_ONLY_TRIGGERS + APPEND_ONLY_UPGRADE_TRIGGERS
APPEND_ONLY_DDL = ";\n".join(statement for _, statement in APPEND_ONLY_TRIGGERS) + ";"

DDL = (
    r"""
CREATE TABLE graph_state (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    schema_version TEXT NOT NULL,
    store_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    committed_watermark INTEGER NOT NULL CHECK(committed_watermark >= 0),
    journal_head_hash TEXT NOT NULL,
    journal_anchor_watermark INTEGER NOT NULL CHECK(journal_anchor_watermark >= 0),
    journal_anchor_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE records (
    record_id TEXT PRIMARY KEY,
    collection TEXT NOT NULL CHECK(collection IN (
      'entities','relations','observations','claim_surfaces','claims','events',
      'decisions','open_loops','receipts','sources'
    )),
    current_revision INTEGER NOT NULL CHECK(current_revision > 0),
    first_watermark INTEGER NOT NULL CHECK(first_watermark >= 0),
    last_watermark INTEGER NOT NULL CHECK(last_watermark >= first_watermark),
    lifecycle TEXT,
    source_tier TEXT,
    sensitive INTEGER NOT NULL CHECK(sensitive IN (0,1)),
    current_payload_hash TEXT NOT NULL,
    search_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(record_id, current_revision)
      REFERENCES record_revision_headers(record_id, revision)
      DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE INDEX records_by_collection
  ON records(collection, lifecycle, last_watermark DESC, record_id);
CREATE INDEX records_by_last_watermark
  ON records(last_watermark, record_id);

CREATE TABLE record_revision_headers (
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    watermark INTEGER NOT NULL CHECK(watermark > 0),
    change_kind TEXT NOT NULL,
    discovery_kind TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    producer_agent_id TEXT,
    task_id TEXT,
    parent_task_id TEXT,
    run_id TEXT,
    based_on_watermark INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY(record_id, revision),
    FOREIGN KEY(record_id) REFERENCES records(record_id)
      DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE INDEX revisions_by_watermark
  ON record_revision_headers(watermark, record_id, revision);

"""
    + RECORD_REVISION_PAYLOADS_TABLE_SQL
    + r""";

CREATE TABLE revision_refs (
    record_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    target_record_id TEXT NOT NULL,
    ref_kind TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    PRIMARY KEY(record_id, revision, ref_kind, ordinal),
    FOREIGN KEY(record_id, revision)
      REFERENCES record_revision_headers(record_id, revision),
    FOREIGN KEY(target_record_id) REFERENCES records(record_id)
) STRICT;
CREATE INDEX revision_refs_by_target
  ON revision_refs(target_record_id, ref_kind, record_id, revision);
CREATE INDEX revision_refs_by_target_record_revision
  ON revision_refs(target_record_id, record_id, revision);

CREATE TABLE mutations (
    watermark INTEGER PRIMARY KEY CHECK(watermark > 0),
    mutation_id TEXT NOT NULL UNIQUE,
    action TEXT NOT NULL,
    discovery_kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    fingerprint_version TEXT NOT NULL,
    producer_agent_id TEXT,
    task_id TEXT,
    parent_task_id TEXT,
    run_id TEXT,
    based_on_watermark INTEGER,
    captured_at TEXT NOT NULL,
    previous_entry_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL UNIQUE,
    hash_algorithm TEXT NOT NULL,
    source_ref TEXT,
    capture_policy_json TEXT CHECK(
      capture_policy_json IS NULL OR json_valid(capture_policy_json)
    ),
    details_json TEXT CHECK(details_json IS NULL OR json_valid(details_json)),
    legacy_header_json TEXT CHECK(
      legacy_header_json IS NULL OR json_valid(legacy_header_json)
    ),
    redaction_count INTEGER NOT NULL DEFAULT 0 CHECK(redaction_count >= 0)
) STRICT;
CREATE INDEX mutations_by_task ON mutations(task_id, watermark);
CREATE INDEX mutations_by_producer ON mutations(producer_agent_id, watermark);

CREATE TABLE mutation_changes (
    watermark INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    record_id TEXT NOT NULL,
    collection TEXT NOT NULL,
    revision INTEGER NOT NULL,
    change_kind TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY(watermark, ordinal),
    UNIQUE(watermark, record_id),
    FOREIGN KEY(watermark) REFERENCES mutations(watermark),
    FOREIGN KEY(record_id, revision)
      REFERENCES record_revision_headers(record_id, revision)
) STRICT;
CREATE INDEX mutation_changes_by_record
  ON mutation_changes(record_id, watermark, ordinal);

CREATE TABLE idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    watermark INTEGER NOT NULL,
    FOREIGN KEY(watermark) REFERENCES mutations(watermark)
) STRICT;

CREATE TABLE identity_schemes (
    scheme TEXT PRIMARY KEY,
    normalization TEXT NOT NULL CHECK(normalization IN ('binary','casefold')),
    governed INTEGER NOT NULL CHECK(governed IN (0,1)),
    created_watermark INTEGER NOT NULL
) STRICT;

CREATE TABLE entity_identity_keys (
    scheme TEXT NOT NULL,
    namespace TEXT NOT NULL,
    value_key TEXT NOT NULL,
    value_display TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    first_watermark INTEGER NOT NULL,
    last_watermark INTEGER NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(scheme, namespace, value_key),
    FOREIGN KEY(entity_id) REFERENCES records(record_id)
) STRICT;
CREATE INDEX entity_identity_keys_by_entity
  ON entity_identity_keys(entity_id, scheme, namespace);

CREATE TABLE entity_aliases (
    entity_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    label_norm TEXT NOT NULL,
    label_display TEXT NOT NULL,
    label_kind TEXT NOT NULL,
    first_watermark INTEGER NOT NULL,
    last_watermark INTEGER NOT NULL,
    PRIMARY KEY(entity_id, label_norm, label_kind),
    FOREIGN KEY(entity_id) REFERENCES records(record_id)
) STRICT;
CREATE INDEX entity_aliases_by_label
  ON entity_aliases(namespace, label_norm, entity_id);

CREATE TABLE entity_redirects (
    source_entity_id TEXT PRIMARY KEY,
    target_entity_id TEXT NOT NULL,
    reason_source_id TEXT NOT NULL,
    created_watermark INTEGER NOT NULL,
    CHECK(source_entity_id <> target_entity_id),
    FOREIGN KEY(source_entity_id) REFERENCES records(record_id),
    FOREIGN KEY(target_entity_id) REFERENCES records(record_id),
    FOREIGN KEY(reason_source_id) REFERENCES records(record_id)
) STRICT;
CREATE INDEX entity_redirects_by_target
  ON entity_redirects(target_entity_id, source_entity_id);

CREATE TABLE claim_surface_constraints (
    surface_id TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    key_name TEXT NOT NULL,
    subject_signature TEXT NOT NULL,
    cardinality TEXT NOT NULL,
    created_watermark INTEGER NOT NULL,
    UNIQUE(family, key_name, subject_signature),
    FOREIGN KEY(surface_id) REFERENCES records(record_id)
) STRICT;

CREATE TABLE claim_surface_subjects (
    surface_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    PRIMARY KEY(surface_id, ordinal),
    UNIQUE(surface_id, entity_id),
    FOREIGN KEY(surface_id) REFERENCES claim_surface_constraints(surface_id),
    FOREIGN KEY(entity_id) REFERENCES records(record_id)
) STRICT;

CREATE TABLE claim_current_registry (
    surface_id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL UNIQUE,
    updated_watermark INTEGER NOT NULL,
    FOREIGN KEY(surface_id) REFERENCES claim_surface_constraints(surface_id),
    FOREIGN KEY(claim_id) REFERENCES records(record_id)
) STRICT;

CREATE TABLE semantic_registry (
    collection TEXT NOT NULL,
    key_kind TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    key_display TEXT NOT NULL,
    record_id TEXT NOT NULL,
    created_watermark INTEGER NOT NULL,
    PRIMARY KEY(collection, key_kind, key_hash),
    FOREIGN KEY(record_id) REFERENCES records(record_id)
) STRICT;
CREATE INDEX semantic_registry_by_record
  ON semantic_registry(record_id, collection, key_kind);

CREATE VIRTUAL TABLE records_fts USING fts5(
    record_id UNINDEXED,
    collection UNINDEXED,
    search_text,
    tokenize='unicode61'
);

CREATE TABLE consumers (
    consumer_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    parent_task_id TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    metadata_json TEXT CHECK(metadata_json IS NULL OR json_valid(metadata_json))
) STRICT;
CREATE INDEX consumers_by_task ON consumers(agent_id, task_id);

CREATE TABLE subscriptions (
    subscription_id TEXT PRIMARY KEY,
    consumer_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    selector_json TEXT NOT NULL CHECK(json_valid(selector_json)),
    selector_hash TEXT NOT NULL,
    last_ack_watermark INTEGER NOT NULL CHECK(last_ack_watermark >= 0),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(consumer_id, version),
    FOREIGN KEY(consumer_id) REFERENCES consumers(consumer_id)
) STRICT;
CREATE INDEX subscriptions_by_consumer
  ON subscriptions(consumer_id, active, updated_at);
CREATE UNIQUE INDEX subscriptions_one_active_selector
  ON subscriptions(consumer_id, selector_hash) WHERE active=1;

CREATE TABLE audit_checkpoints (
    watermark INTEGER PRIMARY KEY,
    journal_head_hash TEXT NOT NULL,
    current_state_root TEXT NOT NULL,
    backup_manifest_hash TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(watermark) REFERENCES mutations(watermark)
) STRICT;

CREATE TABLE schema_migrations (
    migration_id TEXT PRIMARY KEY,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    transform_version TEXT NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    source_sha256 TEXT,
    applied_watermark INTEGER NOT NULL,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE migration_metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL CHECK(json_valid(value_json))
) STRICT;

"""
    + APPEND_ONLY_DDL
)

__all__ = [
    "APPEND_ONLY_DDL",
    "APPEND_ONLY_TRIGGERS",
    "APPEND_ONLY_UPGRADE_TRIGGERS",
    "DDL",
    "LEGACY_APPEND_ONLY_TRIGGERS",
    "PREVIOUS_SCHEMA_USER_VERSION",
    "RECORD_REVISION_PAYLOADS_TABLE_SQL",
    "SCHEMA_USER_VERSION",
    "SCHEMA_VERSION",
]
