#!/usr/bin/env python3
"""Focused contracts for the extracted v4 mutation transaction."""

from __future__ import annotations

import inspect
import os
import sqlite3
import tempfile
import unittest
from collections.abc import Iterable
from pathlib import Path
from unittest import mock

import knowledge_graph_v4 as v4  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402
from tedd_knowledge_graph import v4_queries  # noqa: E402

PLAN_KEY = bytes.fromhex("44" * 32)
TRANSACTION_STATEMENTS = {"BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"}
EXPECTED_APPLY_SIGNATURE = (
    "(self, changes: 'Sequence[RecordChange | Mapping[str, object]]', *, fingerprint: 'str', "
    "action: 'str' = 'capture', discovery_kind: 'str' = 'durable_discovery', "
    "producer_agent_id: 'str | None' = None, task_id: 'str | None' = None, "
    "parent_task_id: 'str | None' = None, run_id: 'str | None' = None, "
    "based_on_watermark: 'int | None' = None, expected_revisions: "
    "'Mapping[str, int | None] | None' = None, plan_token: 'str | None' = None, "
    "idempotency_key: 'str | None' = None, source_ref: 'str | None' = None, "
    "capture_policy: 'Mapping[str, object] | None' = None, details: "
    "'Mapping[str, object] | None' = None, redaction_count: 'int' = 0, "
    "preflight: 'Callable[[], None] | None' = None) -> "
    "'_v4_transactions.MutationResult'"
)


class UnexpectedMutationFailure(Exception):
    """Failure deliberately outside the legacy transaction handler tuple."""


def entity_change(record_id: str, *, source_ref: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": record_id,
        "namespace": "transaction-tests",
        "type": "fixture",
        "name": record_id,
        "aliases": [],
        "identity_keys": [],
        "lifecycle": "active",
        "status": "current",
        "sensitive": False,
    }
    if source_ref is not None:
        payload["source_ref"] = source_ref
    return {
        "record_id": record_id,
        "collection": "entities",
        "payload": payload,
        "expected_revision": None,
    }


def event_change(record_id: str) -> dict[str, object]:
    return {
        "record_id": record_id,
        "collection": "events",
        "payload": {
            "id": record_id,
            "namespace": "transaction-tests",
            "type": "fixture",
            "title": record_id,
            "lifecycle": "active",
            "sensitive": False,
        },
        "expected_revision": None,
    }


class V4TransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        private_parent = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
        self.temporary = tempfile.TemporaryDirectory(
            prefix="kg-v4-transaction-test-",
            dir=private_parent,
        )
        self.root = Path(self.temporary.name).resolve()
        set_private_path_mode(self.root, 0o700)
        self.store_path = self.root / "graph.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_store(self) -> v4.KnowledgeGraphV4:
        return v4.create_store(self.store_path, plan_hmac_key=PLAN_KEY)

    def test_public_apply_signature_remains_exact(self) -> None:
        self.assertEqual(
            str(inspect.signature(v4.KnowledgeGraphV4.apply_changes)),
            EXPECTED_APPLY_SIGNATURE,
        )

    def test_optional_preflight_runs_under_writer_lock_even_for_replay(self) -> None:
        with self.create_store() as store:
            calls: list[bool] = []

            def preflight() -> None:
                calls.append(store.connection.in_transaction)

            for _ in range(2):
                store.apply_changes(
                    [entity_change("entity_preflight")],
                    fingerprint="preflight",
                    preflight=preflight,
                )
            self.assertEqual(calls, [True, True])
            self.assertEqual(store.watermark, 1)

            def denied() -> None:
                raise UnexpectedMutationFailure("neutral denied preflight")

            with self.assertRaises(UnexpectedMutationFailure):
                store.apply_changes(
                    [entity_change("entity_denied")], fingerprint="denied", preflight=denied
                )
            self.assertFalse(store.connection.in_transaction)
            self.assertEqual(store.watermark, 1)
            with self.assertRaises(KeyError):
                store.get_record("entity_denied")

    @staticmethod
    def transaction_statements(trace: list[str]) -> list[str]:
        return [
            statement.strip().upper()
            for statement in trace
            if statement.strip().upper() in TRANSACTION_STATEMENTS
        ]

    @staticmethod
    def mutation_counts(store: v4.KnowledgeGraphV4) -> dict[str, int]:
        tables = (
            "records",
            "record_revision_headers",
            "record_revision_payloads",
            "revision_refs",
            "entity_aliases",
            "entity_identity_keys",
            "semantic_registry",
            "records_fts",
            "mutations",
            "mutation_changes",
            "idempotency_keys",
        )
        return {
            table: int(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    def test_success_commits_and_duplicate_rolls_back_exactly_once(self) -> None:
        with self.create_store() as store:
            trace: list[str] = []
            store.connection.set_trace_callback(trace.append)
            first = store.apply_changes(
                [entity_change("entity_once")],
                fingerprint="transaction-once",
                idempotency_key="transaction-once",
            )
            self.assertEqual(
                self.transaction_statements(trace),
                ["BEGIN IMMEDIATE", "COMMIT"],
            )

            trace.clear()
            duplicate = store.apply_changes(
                [entity_change("entity_once")],
                fingerprint="transaction-once",
                idempotency_key="transaction-once",
            )
            self.assertEqual(
                self.transaction_statements(trace),
                ["BEGIN IMMEDIATE", "ROLLBACK"],
            )
            self.assertEqual(first["status"], "applied")
            self.assertEqual(duplicate["status"], "duplicate")
            self.assertEqual(duplicate["watermark"], first["watermark"])
            self.assertEqual(duplicate["mutation_id"], first["mutation_id"])
            self.assertEqual(store.get_record("entity_once")["revision"], 1)

    def test_unexpected_post_write_exception_rolls_back_everything(self) -> None:
        with self.create_store() as store:
            before_state = store.state()
            before_counts = self.mutation_counts(store)
            with mock.patch.object(
                store,
                "_apply_semantic_registry",
                side_effect=UnexpectedMutationFailure("injected transaction failure"),
            ):
                with self.assertRaisesRegex(
                    UnexpectedMutationFailure,
                    "injected transaction failure",
                ):
                    store.apply_changes(
                        [entity_change("entity_rollback")],
                        fingerprint="transaction-rollback",
                    )

            self.assertFalse(store.connection.in_transaction)
            self.assertEqual(store.state(), before_state)
            self.assertEqual(self.mutation_counts(store), before_counts)
            self.assertIsNone(store.current_revisions(["entity_rollback"])["entity_rollback"])

    def test_infinite_redaction_count_fails_before_transaction_or_writes(self) -> None:
        with self.create_store() as store:
            trace: list[str] = []
            store.connection.set_trace_callback(trace.append)
            with self.assertRaises(OverflowError):
                store.apply_changes(
                    [entity_change("entity_infinite_redaction")],
                    fingerprint="transaction-infinite-redaction",
                    # Deliberately violate the typed API to exercise runtime boundary validation.
                    redaction_count=float("inf"),  # type: ignore[arg-type]
                )

            self.assertEqual(self.transaction_statements(trace), [])
            self.assertFalse(store.connection.in_transaction)
            self.assertEqual(store.state()["watermark"], 0)
            self.assertIsNone(
                store.current_revisions(["entity_infinite_redaction"])["entity_infinite_redaction"]
            )

    def test_revision_payload_update_is_rejected_by_sql(self) -> None:
        with self.create_store() as store:
            store.apply_changes(
                [event_change("event_immutable_update")],
                fingerprint="transaction-immutable-update",
            )

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "record revision payloads are immutable",
            ):
                store.connection.execute(
                    "UPDATE record_revision_payloads SET payload_json=? "
                    "WHERE record_id=? AND revision=1",
                    ('{"id":"tampered"}', "event_immutable_update"),
                )

            payload = store.connection.execute(
                "SELECT payload_json FROM record_revision_payloads "
                "WHERE record_id=? AND revision=1",
                ("event_immutable_update",),
            ).fetchone()[0]
            self.assertIn("event_immutable_update", payload)

    def test_revision_payload_replace_and_upsert_are_rejected_by_sql(self) -> None:
        record_id = "event_immutable_insert_conflict"
        replacement = '{"id":"tampered"}'
        statements = (
            "INSERT OR REPLACE INTO record_revision_payloads"
            "(record_id,revision,payload_json) VALUES(?,?,?)",
            "INSERT INTO record_revision_payloads"
            "(record_id,revision,payload_json) VALUES(?,?,?) "
            "ON CONFLICT(record_id,revision) DO UPDATE SET "
            "payload_json=excluded.payload_json",
        )
        with self.create_store() as store:
            store.apply_changes(
                [event_change(record_id)],
                fingerprint="transaction-immutable-insert-conflict",
            )

            for statement in statements:
                with self.subTest(statement=statement):
                    with self.assertRaisesRegex(
                        sqlite3.IntegrityError,
                        "record revision payloads are immutable",
                    ):
                        store.connection.execute(
                            statement,
                            (record_id, 1, replacement),
                        )

            payload = store.connection.execute(
                "SELECT payload_json FROM record_revision_payloads "
                "WHERE record_id=? AND revision=1",
                (record_id,),
            ).fetchone()[0]
            self.assertIn(record_id, payload)

    def test_replace_cannot_bypass_any_append_only_table(self) -> None:
        record_id = "event_append_only_replace"
        protected_tables = {
            "record_revision_headers": "record revision headers are append-only",
            "record_revision_payloads": "record revision payloads are immutable",
            "mutations": "mutations are append-only",
            "mutation_changes": "mutation changes are append-only",
        }
        with self.create_store() as store:
            result = store.apply_changes(
                [event_change(record_id)],
                fingerprint="transaction-append-only-replace",
            )
            self.assertEqual(result["status"], "applied")
            before = {
                table: [tuple(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
                for table in protected_tables
            }

            for table, message in protected_tables.items():
                with self.subTest(table=table):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, message):
                        store.connection.execute(
                            f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"
                        )
                    store.connection.rollback()

            after = {
                table: [tuple(row) for row in store.connection.execute(f"SELECT * FROM {table}")]
                for table in protected_tables
            }
            self.assertEqual(after, before)

            second = store.apply_changes(
                [event_change("event_append_only_new_insert")],
                fingerprint="transaction-append-only-new-insert",
            )
            self.assertEqual(second["status"], "applied")

    def test_revision_payload_delete_requires_historical_redaction(self) -> None:
        with self.create_store() as store:
            store.apply_changes(
                [event_change("event_immutable_delete")],
                fingerprint="transaction-immutable-delete",
            )

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "payload deletion requires a current redaction revision",
            ):
                store.connection.execute(
                    "DELETE FROM record_revision_payloads WHERE record_id=? AND revision=1",
                    ("event_immutable_delete",),
                )

            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM record_revision_payloads WHERE record_id=?",
                    ("event_immutable_delete",),
                ).fetchone()[0],
                1,
            )

    def test_historical_redaction_remains_the_only_payload_delete_path(self) -> None:
        record_id = "event_authorized_redaction"
        with self.create_store() as store:
            store.apply_changes(
                [event_change(record_id)],
                fingerprint="transaction-authorized-redaction-create",
            )
            current = store.get_record(record_id)
            current_payload = current["payload"]
            if not isinstance(current_payload, dict):
                self.fail("current record payload must be an object")
            redacted = dict(current_payload)
            redacted["title"] = "[redacted]"
            redacted["lifecycle"] = " REDACTED "

            result = store.apply_changes(
                [
                    {
                        "record_id": record_id,
                        "collection": "events",
                        "payload": redacted,
                        "change_kind": " ReDaCt ",
                        "expected_revision": 1,
                    }
                ],
                fingerprint="transaction-authorized-redaction-apply",
                action="redact",
                expected_revisions={record_id: 1},
                redaction_count=1,
            )

            self.assertEqual(result["status"], "applied")
            self.assertEqual(
                store.connection.execute(
                    "SELECT change_kind FROM record_revision_headers "
                    "WHERE record_id=? AND revision=2",
                    (record_id,),
                ).fetchone()[0],
                "redact",
            )
            self.assertEqual(
                [
                    row[0]
                    for row in store.connection.execute(
                        "SELECT revision FROM record_revision_payloads WHERE record_id=?",
                        (record_id,),
                    )
                ],
                [2],
            )
            history = store.history(record_id)
            self.assertEqual(
                [item["payload_available"] for item in history["history"]],
                [True, False],
            )

    def test_caller_owned_transaction_is_rejected_and_preserved(self) -> None:
        with self.create_store() as store:
            store.connection.execute(
                "INSERT INTO identity_schemes"
                "(scheme,normalization,governed,created_watermark) "
                "VALUES('outer-transaction','binary',0,0)"
            )
            self.assertTrue(store.connection.in_transaction)

            with self.assertRaisesRegex(
                v4.V4Error,
                "Mutation requires a connection with no active transaction",
            ):
                store.apply_changes(
                    [entity_change("entity_nested")],
                    fingerprint="transaction-nested",
                )

            self.assertTrue(store.connection.in_transaction)
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM identity_schemes WHERE scheme='outer-transaction'"
                ).fetchone()[0],
                1,
            )
            store.connection.rollback()

    def test_dynamic_state_boundary_rejects_invalid_watermark_and_rolls_back(self) -> None:
        with self.create_store() as store:
            invalid_state = {**store.state(), "watermark": "not-an-integer"}
            trace: list[str] = []
            store.connection.set_trace_callback(trace.append)

            with mock.patch.object(store, "state", return_value=invalid_state):
                with self.assertRaisesRegex(
                    TypeError,
                    "Graph state watermark must be an integer",
                ):
                    store.apply_changes(
                        [entity_change("entity_invalid_state")],
                        fingerprint="transaction-invalid-state",
                    )

            self.assertEqual(
                self.transaction_statements(trace),
                ["BEGIN IMMEDIATE", "ROLLBACK"],
            )
            self.assertFalse(store.connection.in_transaction)
            self.assertEqual(store.state()["watermark"], 0)

    def test_authenticated_plan_boundary_rejects_invalid_read_set_and_rolls_back(self) -> None:
        with self.create_store() as store:
            trace: list[str] = []
            store.connection.set_trace_callback(trace.append)

            with mock.patch.object(
                store,
                "_verify_capture_plan",
                return_value={"read_set": "not-a-list"},
            ):
                with self.assertRaisesRegex(
                    TypeError,
                    "Capture plan read_set must be a list",
                ):
                    store.apply_changes(
                        [entity_change("entity_invalid_plan")],
                        fingerprint="transaction-invalid-plan",
                        plan_token="boundary-test-token",
                    )

            self.assertEqual(
                self.transaction_statements(trace),
                ["BEGIN IMMEDIATE", "ROLLBACK"],
            )
            self.assertFalse(store.connection.in_transaction)
            self.assertEqual(store.state()["watermark"], 0)

    def test_integrity_error_maps_exactly_and_rolls_back(self) -> None:
        with self.create_store() as store:
            with self.assertRaises(v4.IdentityConflict) as raised:
                store.apply_changes(
                    [
                        entity_change(
                            "entity_missing_reference",
                            source_ref="source_does_not_exist",
                        )
                    ],
                    fingerprint="transaction-missing-reference",
                )

            self.assertEqual(
                str(raised.exception),
                "Canonical constraint rejected mutation: FOREIGN KEY constraint failed",
            )
            self.assertIsInstance(raised.exception.__cause__, sqlite3.IntegrityError)
            self.assertFalse(store.connection.in_transaction)
            self.assertEqual(store.state()["watermark"], 0)
            self.assertIsNone(
                store.current_revisions(["entity_missing_reference"])["entity_missing_reference"]
            )

    def test_large_packet_current_revision_reads_are_chunked(self) -> None:
        with self.create_store() as store:
            query_sizes: list[int] = []
            real_current_revisions = store.current_revisions

            def tracked_current_revisions(
                record_ids: Iterable[str],
            ) -> dict[str, v4_queries.CurrentRevision | None]:
                materialized = list(record_ids)
                query_sizes.append(len(materialized))
                return real_current_revisions(materialized)

            changes = [event_change(f"event_chunk_{index:04d}") for index in range(2_001)]
            with mock.patch.object(
                store,
                "current_revisions",
                side_effect=tracked_current_revisions,
            ):
                result = store.apply_changes(
                    changes,
                    fingerprint="transaction-chunked-current-revisions",
                )

            self.assertEqual(query_sizes, [2_000, 1])
            self.assertEqual(result["status"], "applied")
            self.assertEqual(len(result["changed"]), 2_001)
            self.assertEqual(store.state()["watermark"], 1)


if __name__ == "__main__":
    unittest.main()
