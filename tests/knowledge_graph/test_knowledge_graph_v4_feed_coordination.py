#!/usr/bin/env python3
"""Facade and characterization tests for v4 feed and coordination services."""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import sqlite3
import uuid
from pathlib import Path
from typing import cast
from unittest import mock

import knowledge_graph as v3  # noqa: E402
import knowledge_graph_v4 as v4  # noqa: E402
from tedd_knowledge_graph import v4_coordination, v4_feed, v4_import  # noqa: E402
from tedd_knowledge_graph.v4_queries import GraphState  # noqa: E402

from tests.support.graph_cases import (  # noqa: E402
    PLAN_KEY,
    V4TestCase,
    applied,
    canonical_v3_bytes,
    change,
    make_v3_graph,
    scheduled_commit,  # noqa: E402
)

FIXED_TIME = "2026-01-01T12:00:00+00:00"
FIXED_STORE_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
CHARACTERIZATION_PLAN_KEY = bytes.fromhex("55" * 32)
IMPORT_PLAN_KEY = bytes.fromhex("66" * 32)
IMPORT_STORE_ID = uuid.UUID("87654321-4321-8765-4321-876543218765")
EXPECTED_CHARACTERIZATION_DIGEST = (
    "9eda35cfe1ae88044f93ce76496f0ddf66718a160358004ce8c834eecb5169c7"
)
EXPECTED_IMPORT_DIGEST = "dcbbe9cdb40321861fa9b5cd2cb4ef3412f3a9bc5839c397b5ab953ccff6a788"

EXPECTED_METHOD_SIGNATURES = {
    "_feed_predicate": (
        "(self, selector: 'Mapping[str, object] | None') -> "
        "'tuple[str, list[object], dict[str, object] | None]'"
    ),
    "mutation_feed": (
        "(self, since_watermark: 'int', *, through_watermark: 'int | None' = None, "
        "cursor: 'str | None' = None, limit: 'int' = 100, selector: "
        "'Mapping[str, object] | None' = None) -> 'dict[str, object]'"
    ),
    "consumer_status": "(self, consumer_id: 'str') -> 'dict[str, object]'",
    "subscription_identity": "(self, subscription_id: 'str') -> 'dict[str, object] | None'",
    "register_consumer": (
        "(self, consumer_id: 'str', *, agent_id: 'str', task_id: 'str', "
        "parent_task_id: 'str | None' = None, metadata: 'Mapping[str, object] | None' = "
        "None) -> 'dict[str, object]'"
    ),
    "rebind_consumer": (
        "(self, consumer_id: 'str', *, task_id: 'str', expected_old_agent_id: 'str', "
        "new_agent_id: 'str', parent_task_id: 'str | None' = None, metadata: "
        "'Mapping[str, object] | None' = None) -> 'dict[str, object]'"
    ),
    "subscribe": (
        "(self, consumer_id: 'str', selector: 'Mapping[str, object]', *, subscription_id: "
        "'str | None' = None, start_watermark: 'int | None' = None) -> 'dict[str, object]'"
    ),
    "_subscription": "(self, subscription_id: 'str') -> 'sqlite3.Row'",
    "inbox": (
        "(self, subscription_id: 'str', *, limit: 'int' = 100, cursor: 'str | None' "
        "= None, through_watermark: 'int | None' = None) -> 'dict[str, object]'"
    ),
    "ack": (
        "(self, subscription_id: 'str', through_watermark: 'int', *, ack_token: "
        "'str | None' = None, force: 'bool' = False) -> 'dict[str, object]'"
    ),
    "context_pack": (
        "(self, subscription_id: 'str', *, max_items: 'int' = 100, max_chars: "
        "'int' = 50000) -> 'dict[str, object]'"
    ),
}


class V4FeedAndCoordinationFacadeTests(V4TestCase):
    def test_context_records_and_watermark_share_a_snapshot(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_one")], fingerprint="context-before")
            store.register_consumer("consumer_one", agent_id="agent", task_id="task")
            store.subscribe(
                "consumer_one", {"all": True}, subscription_id="subscription_one", start_watermark=0
            )

        def write(writer: v4.KnowledgeGraphV4) -> None:
            applied(writer, [change("entity_two")], fingerprint="context-after")

        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as reader:
            original = reader.state
            with scheduled_commit(self.store_path, write) as pause:

                def pause_then_state() -> GraphState:
                    pause()
                    return original()

                with mock.patch.object(reader, "state", side_effect=pause_then_state):
                    result = reader.context_pack("subscription_one")
            self.assertEqual(result["watermark"], 1)
            self.assertEqual(result["item_count"], 1)
            self.assertEqual(reader.context_pack("subscription_one")["watermark"], 2)

    def test_inbox_subscription_and_feed_share_a_snapshot(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_one")], fingerprint="inbox-before")
            store.register_consumer("consumer_one", agent_id="agent", task_id="task")
            store.subscribe(
                "consumer_one", {"all": True}, subscription_id="subscription_one", start_watermark=0
            )

        def write(writer: v4.KnowledgeGraphV4) -> None:
            applied(writer, [change("entity_two")], fingerprint="inbox-after")

        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as reader:
            original = reader._subscription
            with scheduled_commit(self.store_path, write) as pause:

                def subscription_then_pause(subscription_id: str) -> sqlite3.Row:
                    subscription = original(subscription_id)
                    pause()
                    return subscription

                with mock.patch.object(
                    reader, "_subscription", side_effect=subscription_then_pause
                ):
                    result = reader.inbox("subscription_one")
            self.assertEqual(result["snapshot_watermark"], 1)
            self.assertEqual(result["returned_count"], 1)
            token = v4.verify_plan_token(str(result["ack_token"]), PLAN_KEY)
            self.assertEqual(token["through_watermark"], 1)
            self.assertEqual(reader._subscription("subscription_one")["last_ack_watermark"], 0)
            self.assertEqual(reader.inbox("subscription_one")["snapshot_watermark"], 2)

    def test_signed_pagination_stays_pinned_while_writers_commit_between_pages(self) -> None:
        with self.create_store() as store:
            applied(
                store,
                [change("entity_one"), change("entity_two"), change("entity_three")],
                fingerprint="pages-before",
            )
            store.register_consumer("consumer_one", agent_id="agent", task_id="task")
            store.subscribe(
                "consumer_one", {"all": True}, subscription_id="subscription_one", start_watermark=0
            )

        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as reader:
            first = reader.inbox("subscription_one", limit=1)
            self.assertFalse(first["complete"])
            self.assertIsNone(first["ack_token"])
            self.assertIsInstance(first["next_cursor"], str)
            pages = [first]
            original = reader.state
            for revision in (2, 3):

                def write(writer: v4.KnowledgeGraphV4, revision: int = revision) -> None:
                    applied(
                        writer,
                        [change("entity_one", name=f"Revision {revision}")],
                        fingerprint=f"page-write-{revision}",
                        expected_revisions={"entity_one": revision - 1},
                    )

                with scheduled_commit(self.store_path, write) as pause:

                    def state_then_pause() -> GraphState:
                        state = original()
                        pause()
                        return state

                    with mock.patch.object(reader, "state", side_effect=state_then_pause):
                        pages.append(
                            reader.inbox(
                                "subscription_one", limit=1, cursor=str(pages[-1]["next_cursor"])
                            )
                        )
                self.assertFalse(reader.connection.in_transaction)
            changes = [
                item for page in pages for item in cast(list[dict[str, object]], page["changes"])
            ]
            self.assertEqual([page["snapshot_watermark"] for page in pages], [1, 1, 1])
            self.assertEqual(
                [item["record_id"] for item in changes],
                ["entity_one", "entity_two", "entity_three"],
            )
            self.assertEqual([item["revision"] for item in changes], [1, 1, 1])
            self.assertTrue(pages[-1]["complete"])
            token = v4.verify_plan_token(str(pages[-1]["ack_token"]), PLAN_KEY)
            self.assertEqual(token["through_watermark"], 1)
            self.assertEqual(reader._subscription("subscription_one")["last_ack_watermark"], 0)
            self.assertEqual(reader.mutation_feed(0)["snapshot_watermark"], 3)

    def test_nested_facade_reads_preserve_caller_owned_work_on_success_and_error(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_one")], fingerprint="caller-before")
            store.register_consumer("consumer_one", agent_id="agent", task_id="task")
            store.subscribe(
                "consumer_one", {"all": True}, subscription_id="subscription_one", start_watermark=0
            )
            before = store.state()["updated_at"]
            store.connection.execute("UPDATE graph_state SET updated_at='caller-pending'")
            changes = store.connection.total_changes
            operations = (
                lambda: store.search("entity_one"),
                lambda: store.find_entities(names=["entity_one"]),
                lambda: store.records_referencing(["entity_one"]),
                lambda: store.find_claim_surfaces(surface_id="surface_missing"),
                lambda: store.create_plan_token(["entity_one"], fingerprint="caller"),
                lambda: store.mutation_feed(0),
                lambda: store.inbox("subscription_one"),
                lambda: store.context_pack("subscription_one"),
                lambda: store.validate(full=True),
            )
            for operation in operations:
                operation()
                self.assertTrue(store.connection.in_transaction)
                self.assertEqual(store.state()["updated_at"], "caller-pending")
            with self.assertRaises(v4.SubscriptionError):
                store.context_pack("subscription_missing")
            self.assertTrue(store.connection.in_transaction)
            self.assertEqual(store.connection.total_changes, changes)
            with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as observer:
                self.assertEqual(observer.state()["updated_at"], before)
            store.connection.rollback()
            self.assertEqual(store.state()["updated_at"], before)

    def test_public_method_signatures_remain_exact(self) -> None:
        observed = {
            name: str(inspect.signature(getattr(v4.KnowledgeGraphV4, name)))
            for name in EXPECTED_METHOD_SIGNATURES
        }
        self.assertEqual(observed, EXPECTED_METHOD_SIGNATURES)

    def test_feed_facade_resolves_live_policy_at_call_time(self) -> None:
        expected: v4_feed.MutationFeedResult = {
            "since_watermark": 0,
            "snapshot_watermark": 0,
            "selector": None,
            "selector_hash": "delegated",
            "returned_count": 0,
            "complete": True,
            "next_cursor": None,
            "advance_to_watermark": 0,
            "changes": [],
            "record_payload_semantics": "exact_revision_at_mutation",
        }
        observed_dependencies: list[v4_feed.FeedDependencies] = []

        with self.create_store() as store:
            real_hash = v4.stable_hash

            def delegated(
                connection: object,
                since_watermark: int,
                *,
                through_watermark: int | None,
                cursor: str | None,
                limit: int,
                selector: object,
                dependencies: v4_feed.FeedDependencies,
            ) -> v4_feed.MutationFeedResult:
                self.assertIs(connection, store.connection)
                self.assertEqual(since_watermark, 0)
                self.assertIsNone(through_watermark)
                self.assertIsNone(cursor)
                self.assertEqual(limit, 7)
                self.assertIsNone(selector)
                observed_dependencies.append(dependencies)
                return expected

            with (
                mock.patch.object(
                    v4,
                    "stable_hash",
                    autospec=True,
                    side_effect=real_hash,
                ) as hasher,
                mock.patch.object(
                    store,
                    "_feed_predicate",
                    autospec=True,
                ) as predicate_builder,
                mock.patch.object(
                    v4_feed,
                    "mutation_feed",
                    autospec=True,
                    side_effect=delegated,
                ) as feed_kernel,
            ):
                observed = store.mutation_feed(0, limit=7)

        self.assertIs(observed, expected)
        feed_kernel.assert_called_once()
        self.assertEqual(len(observed_dependencies), 1)
        self.assertIs(observed_dependencies[0].stable_hash, hasher)
        self.assertIs(observed_dependencies[0].predicate_builder, predicate_builder)

    def test_consumer_facade_resolves_live_policy_at_call_time(self) -> None:
        expected: dict[str, object] = {
            "consumer_id": "consumer_delegated",
            "agent_id": "agent-delegated",
            "task_id": "task-delegated",
        }
        observed_dependencies: list[v4_coordination.ConsumerDependencies] = []

        with self.create_store() as store:
            real_validator = v4._valid_record_id

            def delegated(
                connection: object,
                consumer_id: str,
                *,
                agent_id: str,
                task_id: str,
                parent_task_id: str | None,
                metadata: object,
                dependencies: v4_coordination.ConsumerDependencies,
            ) -> dict[str, object]:
                self.assertIs(connection, store.connection)
                self.assertEqual(consumer_id, "consumer_delegated")
                self.assertEqual(agent_id, "agent-delegated")
                self.assertEqual(task_id, "task-delegated")
                self.assertIsNone(parent_task_id)
                self.assertIsNone(metadata)
                observed_dependencies.append(dependencies)
                return expected

            with (
                mock.patch.object(
                    v4,
                    "_valid_record_id",
                    autospec=True,
                    side_effect=real_validator,
                ) as validator,
                mock.patch.object(
                    v4_coordination,
                    "register_consumer",
                    autospec=True,
                    side_effect=delegated,
                ) as consumer_kernel,
            ):
                observed = store.register_consumer(
                    "consumer_delegated",
                    agent_id="agent-delegated",
                    task_id="task-delegated",
                )

        self.assertIs(observed, expected)
        consumer_kernel.assert_called_once()
        self.assertEqual(len(observed_dependencies), 1)
        self.assertIs(observed_dependencies[0].valid_record_id, validator)


class V4FeedAndCoordinationCharacterizationTests(V4TestCase):
    def test_feed_delivery_context_and_ack_match_pre_extraction_digest(self) -> None:
        with (
            mock.patch.object(v4.uuid, "uuid4", return_value=FIXED_STORE_ID),
            mock.patch.object(v4, "utc_now", return_value=FIXED_TIME),
        ):
            with v4.create_store(
                self.store_path,
                plan_hmac_key=CHARACTERIZATION_PLAN_KEY,
            ) as store:
                applied(
                    store,
                    [
                        change(
                            "entity_feed",
                            refs=[{"target_id": "entity_feed", "kind": "entity"}],
                        )
                    ],
                    fingerprint="feed-coordination-characterization",
                )
                status_missing = store.consumer_status("consumer_feed")
                consumer = store.register_consumer(
                    "consumer_feed",
                    agent_id="agent-feed",
                    task_id="task-feed",
                    metadata={"scope": "test"},
                )
                subscription = store.subscribe(
                    "consumer_feed",
                    {"entity_ids": ["entity_feed"]},
                    subscription_id="subscription_feed",
                    start_watermark=0,
                )
                feed = store.mutation_feed(
                    0,
                    selector={"entity_ids": ["entity_feed"]},
                    limit=100,
                )
                inbox = store.inbox("subscription_feed", limit=100)
                context = store.context_pack(
                    "subscription_feed",
                    max_items=10,
                    max_chars=50_000,
                )
                advance_to_watermark = inbox["advance_to_watermark"]
                ack_token = inbox["ack_token"]
                if not isinstance(advance_to_watermark, int):
                    raise AssertionError("inbox watermark must be an integer")
                if not isinstance(ack_token, str):
                    raise AssertionError("inbox acknowledgement token must be a string")
                ack = store.ack(
                    "subscription_feed",
                    advance_to_watermark,
                    ack_token=ack_token,
                )
                status = store.consumer_status("consumer_feed")

        characterized = {
            "missing": status_missing,
            "consumer": consumer,
            "subscription": subscription,
            "feed": feed,
            "inbox": inbox,
            "context": context,
            "ack": ack,
            "status": status,
        }
        encoded = json.dumps(
            characterized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(encoded).hexdigest(),
            EXPECTED_CHARACTERIZATION_DIGEST,
        )


class V4ImportFacadeTests(V4TestCase):
    def test_import_signatures_remain_exact(self) -> None:
        self.assertEqual(
            str(inspect.signature(v4._validate_v3_import)),
            "(graph: 'Mapping[str, object]') -> 'tuple[int, str, int, str]'",
        )
        self.assertEqual(
            str(inspect.signature(v4.import_v3_graph)),
            "(graph: 'Mapping[str, object]', target_path: 'str | Path', source_sha256: "
            "'str', *, producer_agent_id: 'str' = 'migration', task_id: 'str | None' = "
            "None, parent_task_id: 'str | None' = None, discovery_kind: 'str' = "
            "'migration', plan_hmac_key: 'bytes | str | None' = None) -> "
            "'KnowledgeGraphV4'",
        )

    def test_import_facade_resolves_live_policy_at_call_time(self) -> None:
        observed_dependencies: list[v4_import.ImportDependencies] = []
        target = self.root / "delegated.sqlite3"

        with self.create_store() as expected_store:
            real_validator = v4._validate_v3_import
            real_create_store = v4.create_store

            def delegated(
                graph: object,
                target_path: str | Path,
                source_sha256: str,
                *,
                producer_agent_id: str,
                task_id: str | None,
                parent_task_id: str | None,
                discovery_kind: str,
                plan_hmac_key: bytes | str | None,
                dependencies: v4_import.ImportDependencies,
            ) -> v4_import.ImportStore:
                self.assertEqual(graph, {})
                self.assertEqual(target_path, target)
                self.assertEqual(source_sha256, "0" * 64)
                self.assertEqual(producer_agent_id, "agent-delegated")
                self.assertEqual(task_id, "task-delegated")
                self.assertIsNone(parent_task_id)
                self.assertEqual(discovery_kind, "migration")
                self.assertIsNone(plan_hmac_key)
                observed_dependencies.append(dependencies)
                return cast(v4_import.ImportStore, expected_store)

            with (
                mock.patch.object(
                    v4,
                    "_validate_v3_import",
                    autospec=True,
                    side_effect=real_validator,
                ) as validator,
                mock.patch.object(
                    v4,
                    "create_store",
                    autospec=True,
                    side_effect=real_create_store,
                ) as store_factory,
                mock.patch.object(
                    v4_import,
                    "import_v3_graph",
                    autospec=True,
                    side_effect=delegated,
                ) as import_kernel,
            ):
                observed = v4.import_v3_graph(
                    {},
                    target,
                    "0" * 64,
                    producer_agent_id="agent-delegated",
                    task_id="task-delegated",
                )

        self.assertIs(observed, expected_store)
        import_kernel.assert_called_once()
        self.assertEqual(len(observed_dependencies), 1)
        self.assertIs(observed_dependencies[0].validate_v3_import, validator)
        self.assertIs(observed_dependencies[0].create_store, store_factory)


class V4ImportCharacterizationTests(V4TestCase):
    def test_imported_state_matches_pre_extraction_digest(self) -> None:
        with mock.patch.object(v3, "utc_now", return_value=FIXED_TIME):
            graph = make_v3_graph()
        source_sha256 = hashlib.sha256(canonical_v3_bytes(graph)).hexdigest()

        with (
            mock.patch.object(v4.uuid, "uuid4", return_value=IMPORT_STORE_ID),
            mock.patch.object(v4, "utc_now", return_value=FIXED_TIME),
        ):
            with v4.import_v3_graph(
                graph,
                self.store_path,
                source_sha256,
                producer_agent_id="agent-import",
                task_id="task-import",
                parent_task_id="parent-import",
                discovery_kind="migration",
                plan_hmac_key=IMPORT_PLAN_KEY,
            ) as store:
                characterized = {
                    "state": store.state(),
                    "export": store.export_current_graph(schema_version="v4"),
                    "migration_rows": [
                        dict(row)
                        for row in store.connection.execute(
                            "SELECT * FROM schema_migrations ORDER BY migration_id"
                        )
                    ],
                    "metadata": [
                        dict(row)
                        for row in store.connection.execute(
                            "SELECT * FROM migration_metadata ORDER BY key"
                        )
                    ],
                }

        encoded = json.dumps(
            characterized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(encoded).hexdigest(),
            EXPECTED_IMPORT_DIGEST,
        )

    def test_failed_import_rolls_back_and_removes_every_store_artifact(self) -> None:
        with mock.patch.object(v3, "utc_now", return_value=FIXED_TIME):
            graph = make_v3_graph()
        duplicate_claim = copy.deepcopy(graph["claims"][0])
        duplicate_claim["id"] = "claim_duplicate_current"
        graph["claims"].append(duplicate_claim)
        source_sha256 = hashlib.sha256(canonical_v3_bytes(graph)).hexdigest()

        with self.assertRaises(v4.ValidationError):
            v4.import_v3_graph(
                graph,
                self.store_path,
                source_sha256,
                plan_hmac_key=IMPORT_PLAN_KEY,
            )

        self.assertFalse(self.store_path.exists())
        self.assertFalse(Path(f"{self.store_path}-wal").exists())
        self.assertFalse(Path(f"{self.store_path}-shm").exists())
        self.assertFalse(v4._key_path(self.store_path).exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
