#!/usr/bin/env python3
"""Behavior, concurrency, recovery, and migration tests for knowledge_graph_v4.

The suite is intentionally self-contained: every test creates a private temporary
store and never reads or writes the configured live knowledge graph.
"""

from __future__ import annotations

import concurrent.futures
import copy
import errno
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from typing import Any, Callable, TypeVar
from unittest import mock

import knowledge_graph as v3  # noqa: E402
import knowledge_graph_v4 as v4  # noqa: E402
import knowledge_graph_v4_cli as v4_cli  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402

from tests.support.graph_cases import (
    FIXED_TIME,
    PLAN_KEY,
    V4TestCase,
    applied,
    canonical_v3_bytes,
    change,
    current_claim_key,
    make_v3_graph,
    surface_key,
)
from tests.support.paths import GRAPH_SCRIPTS as SCRIPT_DIR

_T = TypeVar("_T")


def require_mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AssertionError(f"{label} must be an object")
    return value


def identity_key(namespace: str, value: str) -> dict[str, Any]:
    return {
        "kind": "entity_identity",
        "key": f"client_slug\0{namespace}\0{value}",
    }


def normalize_export(graph: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(graph)
    result.pop("updated_at", None)
    return result


class PathBoundaryTests(V4TestCase):
    def _plan_key_publication_race(self, winner: bytes) -> Any:
        real_link = os.link

        def race(source: str | Path, destination: str | Path) -> None:
            destination_path = Path(destination)
            destination_path.write_bytes((v4._b64encode(winner) + "\n").encode("ascii"))
            set_private_path_mode(destination_path, 0o600)
            real_link(source, destination)

        return mock.patch.object(v4.os, "link", side_effect=race)

    def test_safe_output_path_hardens_a_new_parent_before_validation(self) -> None:
        output = self.root / "new-output" / "export.json"
        real_harden = v4_cli.set_private_path_mode
        hardened: list[Path] = []

        def harden(path: Path, mode: int) -> None:
            hardened.append(path)
            real_harden(path, mode)

        with mock.patch.object(v4_cli, "set_private_path_mode", harden):
            resolved = v4_cli._safe_output_path(output, "test output")

        self.assertEqual(resolved, output.resolve())
        self.assertIn(output.parent, hardened)

    def test_directory_fsync_suppresses_only_unsupported_errors(self) -> None:
        helpers = (
            (v3, v3.fsync_parent_directory),
            (v4, v4._fsync_parent),
            (v4_cli, v4_cli._fsync_parent_directory),
        )
        for module, helper in helpers:
            with self.subTest(module=module.__name__, error="unsupported"):
                with (
                    mock.patch.object(module.os, "name", "posix"),
                    mock.patch.object(module.os, "open", return_value=101),
                    mock.patch.object(
                        module.os,
                        "fsync",
                        side_effect=OSError(errno.EINVAL, "unsupported"),
                    ),
                    mock.patch.object(module.os, "close") as close,
                ):
                    helper(self.root)
                    close.assert_called_once_with(101)
            with self.subTest(module=module.__name__, error="permission"):
                with (
                    mock.patch.object(module.os, "name", "posix"),
                    mock.patch.object(module.os, "open", return_value=102),
                    mock.patch.object(
                        module.os,
                        "fsync",
                        side_effect=PermissionError(errno.EACCES, "denied"),
                    ),
                    mock.patch.object(module.os, "close"),
                    self.assertRaises(PermissionError),
                ):
                    helper(self.root)
            with self.subTest(module=module.__name__, error="bad_descriptor"):
                with (
                    mock.patch.object(module.os, "name", "posix"),
                    mock.patch.object(module.os, "open", return_value=103),
                    mock.patch.object(
                        module.os,
                        "fsync",
                        side_effect=OSError(errno.EBADF, "bad descriptor"),
                    ),
                    mock.patch.object(module.os, "close") as close,
                    self.assertRaises(OSError) as raised,
                ):
                    helper(self.root)
                self.assertEqual(raised.exception.errno, errno.EBADF)
                close.assert_called_once_with(103)
            with self.subTest(module=module.__name__, error="open_failure"):
                with (
                    mock.patch.object(module.os, "name", "posix"),
                    mock.patch.object(
                        module.os,
                        "open",
                        side_effect=OSError(errno.EINVAL, "invalid open"),
                    ),
                    mock.patch.object(module.os, "fsync") as fsync,
                    mock.patch.object(module.os, "close") as close,
                    self.assertRaises(OSError) as raised,
                ):
                    helper(self.root)
                self.assertEqual(raised.exception.errno, errno.EINVAL)
                fsync.assert_not_called()
                close.assert_not_called()

    def test_readonly_sqlite_uris_escape_reserved_path_characters(self) -> None:
        reserved = "?#%" if os.name != "nt" else "#%"
        index_path = self.root / f"recall {reserved}.sqlite3"
        connection = sqlite3.connect(index_path)
        try:
            connection.execute("CREATE TABLE meta(key TEXT, value TEXT)")
            connection.execute("INSERT INTO meta VALUES('marker', 'escaped')")
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(v3.read_recall_index_meta(index_path)["marker"], "escaped")

        store_path = self.root / f"graph {reserved}.sqlite3"
        with self.create_store(store_path):
            pass
        self.assertEqual(v4.detect_store(store_path), "sqlite_v4")
        with v4.open_store(store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(store.state()["schema_version"], "v4")

    def test_export_no_overwrite_is_atomic_against_a_racing_writer(self) -> None:
        output = self.root / "export.json"
        competitor = b"competitor output\n"
        real_link = os.link

        def race(source: str | Path, destination: str | Path) -> None:
            Path(destination).write_bytes(competitor)
            real_link(source, destination)

        with mock.patch.object(v4_cli.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError):
                v4_cli._write_json_atomic(output, {"ours": True}, overwrite=False)
        self.assertEqual(output.read_bytes(), competitor)

    def test_capture_cli_passes_an_immutable_validated_snapshot(self) -> None:
        self.assertEqual(v4_cli.MAX_CAPTURE_BYTES, v3.MAX_CAPTURE_BYTES)
        packet_path = self.root / "capture.json"
        original = {"source": {"title": "original"}}
        replacement = {"source": {"title": "replacement"}}
        packet_path.write_text(json.dumps(original), encoding="utf-8")
        set_private_path_mode(packet_path, 0o600)
        with self.create_store():
            pass
        parser = v3.build_parser()
        args = parser.parse_args(
            [
                "capture",
                "--input",
                str(packet_path),
                "--dry-run",
                "--consumer-id",
                "consumer-snapshot",
                "--producer-agent-id",
                "agent-snapshot",
                "--task-id",
                "task-snapshot",
                "--producer-run-id",
                "run-snapshot",
                "--discovery-kind",
                "verification",
                "--based-on-watermark",
                "0",
            ]
        )

        def handler(store: Any, request: v4_cli.CaptureRequest) -> dict[str, Any]:
            del store
            packet_path.write_text(json.dumps(replacement), encoding="utf-8")
            return {
                "status": "snapshotted",
                "packet": json.loads(request.input_bytes or b"null"),
            }

        result = require_mapping(
            v4_cli.handle_v4_command(
                args,
                db_path=self.store_path,
                plan_hmac_key=PLAN_KEY,
                capture_handler=handler,
                review_sanitizer=lambda value: value,
            ),
            label="capture result",
        )
        self.assertEqual(result["packet"], original)

    def test_generated_plan_key_converges_on_a_concurrent_winner(self) -> None:
        with self.create_store():
            pass
        key_path = self.store_path.with_name(f"{self.store_path.name}.plan.key")
        key_path.unlink()
        winner = bytes.fromhex("22" * 32)

        with self._plan_key_publication_race(winner):
            with v4.open_store(self.store_path) as store:
                token = store.create_plan_token([], "concurrent-winner")

        self.assertEqual(v4._read_private_plan_key(key_path), winner)
        self.assertEqual(
            v4.verify_plan_token(token, winner)["fingerprint"],
            "concurrent-winner",
        )
        with self.assertRaises(v4.PlanTokenError):
            v4.verify_plan_token(token, PLAN_KEY)

    def test_supplied_plan_key_rejects_a_different_concurrent_winner(self) -> None:
        with self.create_store():
            pass
        key_path = self.store_path.with_name(f"{self.store_path.name}.plan.key")
        key_path.unlink()
        winner = bytes.fromhex("22" * 32)

        with self._plan_key_publication_race(winner):
            with self.assertRaises(v4.PathSafetyError):
                v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY)

        self.assertEqual(v4._read_private_plan_key(key_path), winner)


class SchemaAndMigrationTests(V4TestCase):
    def test_ddl_and_durability_pragmas(self) -> None:
        with self.create_store() as store:
            state = store.state()
            self.assertEqual(state["schema_version"], "v4")
            self.assertEqual(
                store.connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                "wal",
            )
            self.assertEqual(store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertGreaterEqual(store.connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertGreater(store.connection.execute("PRAGMA busy_timeout").fetchone()[0], 0)
            self.assertEqual(store.connection.execute("PRAGMA secure_delete").fetchone()[0], 1)
        connection = sqlite3.connect(self.store_path)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertTrue(
                {
                    "graph_state",
                    "records",
                    "record_revision_headers",
                    "record_revision_payloads",
                    "mutations",
                    "mutation_changes",
                    "semantic_registry",
                    "consumers",
                    "subscriptions",
                }
                <= tables,
                tables,
            )
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        finally:
            connection.close()

    def test_v3_side_by_side_import_and_export_equivalence(self) -> None:
        graph = make_v3_graph()
        source_bytes = canonical_v3_bytes(graph)
        source_digest = hashlib.sha256(source_bytes).hexdigest()
        store = v4.import_v3_graph(
            graph,
            self.store_path,
            source_sha256=source_digest,
            producer_agent_id="migration-test",
            task_id="v4-import",
            discovery_kind="migration",
            plan_hmac_key=PLAN_KEY,
        )
        with store:
            exported = require_mapping(
                store.export_current_graph(schema_version="v3", include_journal=True),
                label="v3 export",
            )
            for collection in v3.COLLECTIONS:
                if collection == "journal":
                    continue
                self.assertEqual(
                    sorted(exported[collection], key=lambda item: item["id"]),
                    sorted(graph[collection], key=lambda item: item["id"]),
                    collection,
                )
            self.assertEqual(exported["journal"][: len(graph["journal"])], graph["journal"])
            self.assertEqual(len(exported["journal"]), len(graph["journal"]) + 1)
            self.assertEqual(store.state()["watermark"], v3.committed_watermark(graph) + 1)
            self.assertTrue(store.validate(full=True)["valid"])


class RevisionAndConflictTests(V4TestCase):
    def test_per_record_cas_ignores_unrelated_mutation(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_a")], fingerprint="create-a")
            applied(
                store,
                [change("entity_b", namespace="client_beta")],
                fingerprint="create-b",
            )
            a_revision = store.get_record("entity_a")["revision"]
            b_revision = store.get_record("entity_b")["revision"]
            applied(
                store,
                [
                    change(
                        "entity_b",
                        namespace="client_beta",
                        name="Client Beta updated",
                    )
                ],
                fingerprint="update-b",
                expected_revisions={"entity_b": b_revision},
            )
            applied(
                store,
                [change("entity_a", name="Client Alpha updated")],
                fingerprint="update-a",
                expected_revisions={"entity_a": a_revision},
            )
            self.assertEqual(store.get_record("entity_a")["revision"], a_revision + 1)

    def test_relevant_stale_revision_is_rejected(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_a")], fingerprint="create-a")
            revision = store.get_record("entity_a")["revision"]
            applied(
                store,
                [change("entity_a", name="First update")],
                fingerprint="first-update",
                expected_revisions={"entity_a": revision},
            )
            with self.assertRaises(v4.ConcurrencyConflict):
                applied(
                    store,
                    [change("entity_a", name="Stale update")],
                    fingerprint="stale-update",
                    expected_revisions={"entity_a": revision},
                )

    def test_duplicate_idempotency_is_exactly_once(self) -> None:
        with self.create_store() as store:
            first = applied(
                store,
                [change("entity_once")],
                fingerprint="once",
                idempotency_key="idempotency-once",
            )
            second = applied(
                store,
                [change("entity_once")],
                fingerprint="once",
                idempotency_key="idempotency-once",
            )
            self.assertEqual(first["status"], "applied")
            self.assertEqual(second["status"], "duplicate")
            self.assertEqual(first["watermark"], second["watermark"])
            self.assertEqual(store.get_record("entity_once")["revision"], 1)

    def test_exact_historical_feed_returns_written_revisions(self) -> None:
        with self.create_store() as store:
            applied(
                store,
                [change("entity_history", name="Original")],
                fingerprint="history-1",
            )
            first_watermark = store.state()["watermark"]
            first_revision = store.get_record("entity_history")["revision"]
            applied(
                store,
                [change("entity_history", name="Updated")],
                fingerprint="history-2",
                expected_revisions={"entity_history": first_revision},
            )
            feed = store.mutation_feed(0, limit=100)
            changes = [item for item in feed["changes"] if item["record_id"] == "entity_history"]
            self.assertEqual([item["revision"] for item in changes], [1, 2])
            self.assertEqual(changes[0]["payload"]["name"], "Original")
            self.assertEqual(changes[1]["payload"]["name"], "Updated")
            self.assertEqual(
                store.get_record("entity_history", revision=1)["payload"]["name"],
                "Original",
            )
            self.assertEqual(first_watermark, changes[0]["watermark"])

    def test_surface_and_current_claim_uniqueness(self) -> None:
        with self.create_store() as store:
            applied(
                store,
                [change("entity_subject", namespace="client_alpha", name="Subject")],
                fingerprint="surface-subject",
            )
            surface_one = change(
                "surface_one",
                collection="claim_surfaces",
                namespace="entity_subject",
                name="state",
                semantic_keys=[surface_key("test", "state", ["entity_subject"])],
            )
            applied(store, [surface_one], fingerprint="surface-one")
            surface_two = change(
                "surface_two",
                collection="claim_surfaces",
                namespace="entity_subject",
                name="state",
                semantic_keys=[surface_key("test", "state", ["entity_subject"])],
            )
            with self.assertRaises(v4.IdentityConflict):
                applied(store, [surface_two], fingerprint="surface-two")
            claim_one = change(
                "claim_one",
                collection="claims",
                namespace="surface_one",
                name="state",
                value="one",
                semantic_keys=[current_claim_key("surface_one")],
            )
            applied(store, [claim_one], fingerprint="claim-one")
            claim_two = change(
                "claim_two",
                collection="claims",
                namespace="surface_one",
                name="state",
                value="two",
                semantic_keys=[current_claim_key("surface_one")],
            )
            with self.assertRaises(v4.IdentityConflict):
                applied(store, [claim_two], fingerprint="claim-two")


class ConcurrencyTests(V4TestCase):
    def _run_writers(self, writers: int, worker: Callable[[int], _T]) -> list[_T]:
        gate = threading.Barrier(writers)

        def gated(index: int) -> _T:
            gate.wait(timeout=30)
            return worker(index)

        with concurrent.futures.ThreadPoolExecutor(max_workers=writers) as executor:
            futures = [executor.submit(gated, index) for index in range(writers)]
            return [future.result(timeout=120) for future in futures]

    def test_16_and_32_independent_concurrent_writers(self) -> None:
        with self.create_store():
            pass
        for writers in (16, 32):
            prefix = f"batch_{writers}"

            def worker(index: int, batch_prefix: str = prefix) -> str:
                with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
                    result = applied(
                        store,
                        [change(f"entity_{batch_prefix}_{index:02d}")],
                        fingerprint=f"{batch_prefix}-{index}",
                        idempotency_key=f"{batch_prefix}-{index}",
                    )
                    return result["status"]

            statuses = self._run_writers(writers, worker)
            self.assertEqual(statuses.count("applied"), writers)
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertTrue(store.validate(full=True)["valid"])

    def test_concurrent_same_identity_has_one_canonical_owner(self) -> None:
        with self.create_store():
            pass
        writers = 32

        def worker(index: int) -> str:
            try:
                with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
                    contender = change(
                        f"entity_identity_contender_{index:02d}",
                        name=f"Same identity {index}",
                        semantic_keys=[identity_key("clients", "client-alpha")],
                    )
                    contender["payload"]["identity_keys"] = [
                        {
                            "scheme": "client_slug",
                            "namespace": "clients",
                            "value": "client-alpha",
                        }
                    ]
                    result = applied(
                        store,
                        [contender],
                        fingerprint=f"identity-{index}",
                    )
                    return result["status"]
            except v4.IdentityConflict:
                return "semantic_conflict"

        statuses = self._run_writers(writers, worker)
        self.assertEqual(statuses.count("applied"), 1, statuses)
        self.assertEqual(statuses.count("semantic_conflict"), writers - 1, statuses)

    def test_concurrent_consumer_registration_has_one_coherent_owner(self) -> None:
        with self.create_store():
            pass
        writers = 16

        def worker(index: int) -> tuple[str, int, dict[str, Any] | None]:
            try:
                with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
                    result = store.register_consumer(
                        consumer_id="consumer-contended-owner",
                        agent_id=f"agent-owner-{index:02d}",
                        task_id=f"task-owner-{index:02d}",
                        parent_task_id=f"parent-owner-{index:02d}",
                        metadata={"owner": f"agent-owner-{index:02d}"},
                    )
                return ("registered", index, result)
            except v4.SubscriptionError:
                return ("ownership_conflict", index, None)

        outcomes = self._run_writers(writers, worker)
        winners = [outcome for outcome in outcomes if outcome[0] == "registered"]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(
            sum(outcome[0] == "ownership_conflict" for outcome in outcomes),
            writers - 1,
            outcomes,
        )
        winner_index = winners[0][1]
        expected_agent = f"agent-owner-{winner_index:02d}"

        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            row = store.connection.execute(
                "SELECT agent_id,task_id,parent_task_id,metadata_json "
                "FROM consumers WHERE consumer_id=?",
                ("consumer-contended-owner",),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["agent_id"], expected_agent)
            self.assertEqual(row["task_id"], f"task-owner-{winner_index:02d}")
            self.assertEqual(row["parent_task_id"], f"parent-owner-{winner_index:02d}")
            self.assertEqual(json.loads(row["metadata_json"]), {"owner": expected_agent})


class ConsumerRegistrationTests(V4TestCase):
    def test_new_registration_can_omit_parent_and_explicit_parent_can_change(self) -> None:
        with self.create_store() as store:
            created = store.register_consumer(
                consumer_id="consumer-parent-api",
                agent_id="agent-parent-api",
                task_id="task-parent-api",
            )
            assigned = store.register_consumer(
                consumer_id="consumer-parent-api",
                agent_id="agent-parent-api",
                task_id="task-parent-api",
                parent_task_id="parent-api-first",
            )
            preserved = store.register_consumer(
                consumer_id="consumer-parent-api",
                agent_id="agent-parent-api",
                task_id="task-parent-api",
            )
            changed = store.register_consumer(
                consumer_id="consumer-parent-api",
                agent_id="agent-parent-api",
                task_id="task-parent-api",
                parent_task_id="parent-api-second",
            )

            self.assertIsNone(created["parent_task_id"])
            self.assertEqual(assigned["parent_task_id"], "parent-api-first")
            self.assertEqual(preserved["parent_task_id"], "parent-api-first")
            self.assertEqual(changed["parent_task_id"], "parent-api-second")
            self.assertEqual(
                store.consumer_status("consumer-parent-api")["owner"]["parent_task_id"],
                "parent-api-second",
            )

    def test_consumer_register_cli_omission_preserves_existing_parent(self) -> None:
        with self.create_store() as store:
            store.register_consumer(
                consumer_id="consumer-parent-cli",
                agent_id="agent-parent-cli",
                task_id="task-parent-cli",
                parent_task_id="parent-cli-first",
            )

        parser = v3.build_parser()
        preserved_args = parser.parse_args(
            [
                "consumer-register",
                "--consumer-id",
                "consumer-parent-cli",
                "--producer-agent-id",
                "agent-parent-cli",
                "--task-id",
                "task-parent-cli",
                "--producer-run-id",
                "run-parent-cli-preserve",
                "--based-on-watermark",
                "0",
            ]
        )
        preserved = require_mapping(
            v4_cli.handle_v4_command(
                preserved_args,
                db_path=self.store_path,
                plan_hmac_key=PLAN_KEY,
            ),
            label="consumer registration result",
        )

        changed_args = parser.parse_args(
            [
                "consumer-register",
                "--consumer-id",
                "consumer-parent-cli",
                "--producer-agent-id",
                "agent-parent-cli",
                "--task-id",
                "task-parent-cli",
                "--parent-task-id",
                "parent-cli-second",
                "--producer-run-id",
                "run-parent-cli-change",
                "--based-on-watermark",
                "0",
            ]
        )
        changed = require_mapping(
            v4_cli.handle_v4_command(
                changed_args,
                db_path=self.store_path,
                plan_hmac_key=PLAN_KEY,
            ),
            label="consumer registration result",
        )

        self.assertEqual(preserved["parent_task_id"], "parent-cli-first")
        self.assertEqual(changed["parent_task_id"], "parent-cli-second")
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            status = require_mapping(
                store.consumer_status("consumer-parent-cli"),
                label="consumer status",
            )
            owner = require_mapping(status["owner"], label="consumer owner")
            self.assertEqual(owner["parent_task_id"], "parent-cli-second")


class ConsumerRebindTests(V4TestCase):
    OLD_AGENT_ID = "tedd"
    NEW_AGENT_ID = "agent_instance_0123456789abcdef0123456789abcdef"

    @staticmethod
    def _consumer(store: Any, consumer_id: str) -> dict[str, Any]:
        row = store.connection.execute(
            "SELECT * FROM consumers WHERE consumer_id=?", (consumer_id,)
        ).fetchone()
        if row is None:
            raise AssertionError(f"Missing consumer {consumer_id}")
        result = dict(row)
        result["metadata"] = (
            json.loads(result.pop("metadata_json")) if result["metadata_json"] else None
        )
        return result

    @staticmethod
    def _subscriptions(store: Any, consumer_id: str) -> list[tuple[Any, ...]]:
        return [
            tuple(row)
            for row in store.connection.execute(
                "SELECT subscription_id,version,selector_json,selector_hash,"
                "last_ack_watermark,active,created_at,updated_at "
                "FROM subscriptions WHERE consumer_id=? ORDER BY version",
                (consumer_id,),
            ).fetchall()
        ]

    def test_consumer_status_is_read_only_exact_and_bounded(self) -> None:
        with self.create_store() as store:
            store.register_consumer(
                consumer_id="consumer-status",
                agent_id=self.OLD_AGENT_ID,
                task_id="task-status",
                parent_task_id="parent-status",
            )
            selector_json = json.dumps({"all": True})
            active_rows = [
                (
                    f"subscription-status-{version:04d}",
                    "consumer-status",
                    version,
                    selector_json,
                    f"selector-status-{version:04d}",
                    0,
                    1,
                    FIXED_TIME,
                    FIXED_TIME,
                )
                for version in range(1, v4.MAX_CONSUMER_STATUS_SUBSCRIPTIONS + 2)
            ]
            inactive_row = (
                "subscription-status-inactive",
                "consumer-status",
                v4.MAX_CONSUMER_STATUS_SUBSCRIPTIONS + 2,
                selector_json,
                "selector-status-inactive",
                0,
                0,
                FIXED_TIME,
                FIXED_TIME,
            )
            with store.connection:
                store.connection.executemany(
                    "INSERT INTO subscriptions(subscription_id,consumer_id,version,"
                    "selector_json,selector_hash,last_ack_watermark,active,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [*active_rows, inactive_row],
                )
            before_changes = store.connection.total_changes

            found = store.consumer_status("consumer-status")
            missing = store.consumer_status("consumer-status-missing")

            self.assertEqual(store.connection.total_changes, before_changes)
            self.assertEqual(found["status"], "found")
            self.assertTrue(found["found"])
            self.assertEqual(
                found["owner"],
                {
                    "agent_id": self.OLD_AGENT_ID,
                    "task_id": "task-status",
                    "parent_task_id": "parent-status",
                },
            )
            self.assertEqual(
                found["active_subscription_count"],
                v4.MAX_CONSUMER_STATUS_SUBSCRIPTIONS + 1,
            )
            self.assertEqual(
                len(found["active_subscriptions"]),
                v4.MAX_CONSUMER_STATUS_SUBSCRIPTIONS,
            )
            self.assertTrue(found["active_subscriptions_truncated"])
            self.assertEqual(
                found["active_subscriptions"][0],
                {
                    "subscription_id": "subscription-status-0001",
                    "version": 1,
                    "last_ack_watermark": 0,
                },
            )
            self.assertNotIn(
                "subscription-status-inactive",
                {item["subscription_id"] for item in found["active_subscriptions"]},
            )
            self.assertEqual(
                missing,
                {
                    "status": "not_found",
                    "found": False,
                    "consumer_id": "consumer-status-missing",
                    "owner": None,
                    "active_subscription_count": 0,
                    "active_subscriptions": [],
                    "active_subscriptions_truncated": False,
                },
            )

    def test_consumer_status_cli_registration_and_dispatch(self) -> None:
        with self.create_store() as store:
            store.register_consumer(
                consumer_id="consumer-status-cli",
                agent_id=self.OLD_AGENT_ID,
                task_id="task-status-cli",
                parent_task_id="parent-status-cli",
            )
            store.subscribe(
                consumer_id="consumer-status-cli",
                selector={"all": True},
                subscription_id="subscription-status-cli",
                start_watermark=0,
            )

        parser = v3.build_parser()
        args = parser.parse_args(["consumer-status", "--consumer-id", "consumer-status-cli"])
        self.assertTrue(v4_cli.should_handle_v4(args, v4_enabled=True))
        result = require_mapping(
            v4_cli.handle_v4_command(
                args,
                db_path=self.store_path,
                plan_hmac_key=PLAN_KEY,
            ),
            label="consumer status result",
        )
        self.assertEqual(result["command"], "consumer-status")
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["owner"]["agent_id"], self.OLD_AGENT_ID)
        self.assertEqual(
            result["active_subscriptions"],
            [
                {
                    "subscription_id": "subscription-status-cli",
                    "version": 1,
                    "last_ack_watermark": 0,
                }
            ],
        )

        missing_args = parser.parse_args(
            ["consumer-status", "--consumer-id", "consumer-status-cli-missing"]
        )
        missing = require_mapping(
            v4_cli.handle_v4_command(
                missing_args,
                db_path=self.store_path,
                plan_hmac_key=PLAN_KEY,
            ),
            label="missing consumer status result",
        )
        self.assertEqual(missing["status"], "not_found")
        self.assertFalse(missing["found"])

    def test_rebind_preserves_consumer_and_subscription_state(self) -> None:
        with self.create_store() as store:
            consumer = store.register_consumer(
                consumer_id="consumer-rebind",
                agent_id=self.OLD_AGENT_ID,
                task_id="task-rebind",
                parent_task_id="parent-before",
                metadata={"installation_id": "install-alpha", "producer_run_id": "run-before"},
            )
            first = store.subscribe(
                consumer_id=consumer["consumer_id"],
                selector={"all": True},
                subscription_id="subscription-rebind-1",
                start_watermark=0,
            )
            mutation = applied(
                store,
                [change("entity_rebind_cursor")],
                fingerprint="rebind-cursor-fixture",
            )
            delivery = store.inbox(first["subscription_id"])
            store.ack(
                first["subscription_id"],
                through_watermark=delivery["advance_to_watermark"],
                ack_token=delivery["ack_token"],
            )
            store.subscribe(
                consumer_id=consumer["consumer_id"],
                selector={"entity_ids": ["entity_rebind_cursor"]},
                subscription_id="subscription-rebind-2",
                start_watermark=mutation["watermark"],
            )
            before_consumer = self._consumer(store, consumer["consumer_id"])
            before_subscriptions = self._subscriptions(store, consumer["consumer_id"])
            before_state = store.state()

            result = store.rebind_consumer(
                consumer["consumer_id"],
                task_id="task-rebind",
                expected_old_agent_id=self.OLD_AGENT_ID,
                new_agent_id=self.NEW_AGENT_ID,
                parent_task_id="parent-after",
                metadata={
                    "producer_run_id": "run-after",
                    "based_on_watermark": mutation["watermark"],
                },
            )

            after_consumer = self._consumer(store, consumer["consumer_id"])
            self.assertEqual(result["status"], "rebound")
            self.assertEqual(result["subscription_count"], 2)
            self.assertEqual(after_consumer["consumer_id"], before_consumer["consumer_id"])
            self.assertEqual(after_consumer["created_at"], before_consumer["created_at"])
            self.assertEqual(after_consumer["agent_id"], self.NEW_AGENT_ID)
            self.assertEqual(after_consumer["task_id"], "task-rebind")
            self.assertEqual(after_consumer["parent_task_id"], "parent-after")
            self.assertEqual(after_consumer["metadata"]["installation_id"], "install-alpha")
            self.assertEqual(after_consumer["metadata"]["producer_run_id"], "run-after")
            self.assertEqual(
                after_consumer["metadata"]["based_on_watermark"], mutation["watermark"]
            )
            self.assertEqual(
                self._subscriptions(store, consumer["consumer_id"]), before_subscriptions
            )
            self.assertEqual(store.state(), before_state)

    def test_rebind_rejects_wrong_old_agent_and_task(self) -> None:
        with self.create_store() as store:
            consumer = store.register_consumer(
                consumer_id="consumer-rebind-reject",
                agent_id="agent-owner",
                task_id="task-owner",
                parent_task_id="parent-owner",
                metadata={"owner": "original"},
            )
            before = self._consumer(store, consumer["consumer_id"])
            with self.assertRaises(v4.SubscriptionError):
                store.rebind_consumer(
                    consumer["consumer_id"],
                    task_id="task-owner",
                    expected_old_agent_id="agent-third-party",
                    new_agent_id="agent-new",
                    parent_task_id="parent-attacker",
                    metadata={"owner": "changed"},
                )
            with self.assertRaises(v4.SubscriptionError):
                store.rebind_consumer(
                    consumer["consumer_id"],
                    task_id="task-other",
                    expected_old_agent_id="agent-owner",
                    new_agent_id="agent-new",
                    parent_task_id="parent-attacker",
                    metadata={"owner": "changed"},
                )
            self.assertEqual(self._consumer(store, consumer["consumer_id"]), before)

    def test_rebind_retry_is_idempotent_for_requested_new_agent(self) -> None:
        with self.create_store() as store:
            consumer = store.register_consumer(
                consumer_id="consumer-rebind-retry",
                agent_id="agent-old",
                task_id="task-retry",
                parent_task_id="parent-original",
            )
            first = store.rebind_consumer(
                consumer["consumer_id"],
                task_id="task-retry",
                expected_old_agent_id="agent-old",
                new_agent_id="agent-new",
                metadata={"producer_run_id": "run-first"},
            )
            retry = store.rebind_consumer(
                consumer["consumer_id"],
                task_id="task-retry",
                expected_old_agent_id="agent-old",
                new_agent_id="agent-new",
                metadata={"producer_run_id": "run-retry"},
            )
            current = self._consumer(store, consumer["consumer_id"])
            self.assertEqual(first["status"], "rebound")
            self.assertEqual(retry["status"], "already_bound")
            self.assertEqual(current["agent_id"], "agent-new")
            self.assertEqual(current["task_id"], "task-retry")
            self.assertEqual(first["parent_task_id"], "parent-original")
            self.assertEqual(retry["parent_task_id"], "parent-original")
            self.assertEqual(current["parent_task_id"], "parent-original")
            self.assertEqual(current["metadata"]["producer_run_id"], "run-retry")
            self.assertEqual(store.watermark, 0)

    def test_consumer_rebind_cli_registration_and_dispatch(self) -> None:
        with self.create_store() as store:
            store.register_consumer(
                consumer_id="consumer-rebind-cli",
                agent_id=self.OLD_AGENT_ID,
                task_id="task-rebind-cli",
                parent_task_id="parent-rebind-existing",
            )

        parser = v3.build_parser()
        args = parser.parse_args(
            [
                "consumer-rebind",
                "--consumer-id",
                "consumer-rebind-cli",
                "--existing-task-id",
                "task-rebind-cli",
                "--expected-old-agent-id",
                self.OLD_AGENT_ID,
                "--producer-agent-id",
                self.NEW_AGENT_ID,
                "--task-id",
                "task-rebind-cli",
                "--producer-run-id",
                "run-rebind-cli",
                "--based-on-watermark",
                "0",
            ]
        )
        self.assertTrue(v4_cli.should_handle_v4(args, v4_enabled=True))
        result = require_mapping(
            v4_cli.handle_v4_command(
                args,
                db_path=self.store_path,
                plan_hmac_key=PLAN_KEY,
            ),
            label="consumer rebind result",
        )
        self.assertEqual(result["command"], "consumer-rebind")
        self.assertEqual(result["status"], "rebound")
        self.assertEqual(result["agent_id"], self.NEW_AGENT_ID)
        self.assertEqual(result["task_id"], "task-rebind-cli")
        self.assertEqual(result["parent_task_id"], "parent-rebind-existing")
        self.assertEqual(result["metadata"]["producer_run_id"], "run-rebind-cli")
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(
                self._consumer(store, "consumer-rebind-cli")["parent_task_id"],
                "parent-rebind-existing",
            )

        mismatch = parser.parse_args(
            [
                "consumer-rebind",
                "--consumer-id",
                "consumer-rebind-cli",
                "--existing-task-id",
                "task-rebind-cli",
                "--expected-old-agent-id",
                self.OLD_AGENT_ID,
                "--producer-agent-id",
                self.NEW_AGENT_ID,
                "--task-id",
                "task-other",
                "--producer-run-id",
                "run-rebind-mismatch",
                "--based-on-watermark",
                "0",
            ]
        )
        with self.assertRaises(v4_cli.V4CLIError):
            v4_cli.handle_v4_command(
                mismatch,
                db_path=self.store_path,
                plan_hmac_key=PLAN_KEY,
            )


class AwarenessAndPlanTests(V4TestCase):
    def _subscription(self, store: Any, record_id: str, suffix: str) -> dict[str, Any]:
        consumer = store.register_consumer(
            consumer_id=f"consumer-{suffix}",
            agent_id=f"agent-{suffix}",
            task_id=f"task-{suffix}",
        )
        return store.subscribe(
            consumer_id=consumer["consumer_id"],
            selector={"entity_ids": [record_id]},
            subscription_id=f"subscription-{suffix}",
        )

    def test_subscription_isolation_and_ack_redelivery(self) -> None:
        with self.create_store() as store:
            client_alpha = self._subscription(store, "entity_client_alpha", "client-alpha")
            client_beta = self._subscription(store, "entity_client_beta", "client-beta")
            applied(
                store,
                [
                    change(
                        "entity_client_alpha",
                        namespace="client_alpha",
                        refs=[{"target_id": "entity_client_alpha", "kind": "entity"}],
                    )
                ],
                fingerprint="client-alpha",
            )
            applied(
                store,
                [
                    change(
                        "entity_client_beta",
                        namespace="client_beta",
                        refs=[{"target_id": "entity_client_beta", "kind": "entity"}],
                    )
                ],
                fingerprint="client-beta",
            )
            client_alpha_first = store.inbox(client_alpha["subscription_id"], limit=100)
            client_alpha_second = store.inbox(client_alpha["subscription_id"], limit=100)
            self.assertEqual(client_alpha_first["changes"], client_alpha_second["changes"])
            self.assertEqual(
                {item["record_id"] for item in client_alpha_first["changes"]},
                {"entity_client_alpha"},
            )
            client_beta_inbox = store.inbox(client_beta["subscription_id"], limit=100)
            self.assertEqual(
                {item["record_id"] for item in client_beta_inbox["changes"]},
                {"entity_client_beta"},
            )
            context = store.context_pack(
                client_alpha["subscription_id"], max_items=1, max_chars=2_000
            )
            self.assertLessEqual(context["item_count"], 1)
            self.assertLessEqual(context["char_count"], 2_000)
            self.assertEqual(
                {item["record_id"] for item in context["records"]},
                {"entity_client_alpha"},
            )
            ack = store.ack(
                client_alpha["subscription_id"],
                through_watermark=client_alpha_first["advance_to_watermark"],
                ack_token=client_alpha_first.get("ack_token"),
            )
            self.assertEqual(
                ack["last_ack_watermark"],
                client_alpha_first["advance_to_watermark"],
            )
            retry = store.ack(
                client_alpha["subscription_id"],
                through_watermark=client_alpha_first["advance_to_watermark"],
                ack_token=client_alpha_first.get("ack_token"),
            )
            self.assertEqual(retry, ack)
            self.assertEqual(
                store.inbox(client_alpha["subscription_id"], limit=100)["changes"],
                [],
            )

    def test_concurrent_acknowledgements_cannot_regress_cursor(self) -> None:
        with self.create_store() as store:
            consumer = store.register_consumer(
                consumer_id="consumer-ack-race",
                agent_id="agent-ack-race",
                task_id="task-ack-race",
            )
            subscription = store.subscribe(
                consumer_id=consumer["consumer_id"],
                selector={"all": True},
                subscription_id="subscription-ack-race",
                start_watermark=0,
            )
            low_mutation = applied(
                store,
                [change("entity_ack_race_low")],
                fingerprint="ack-race-low",
            )
            low_delivery = store.inbox(
                subscription["subscription_id"],
                through_watermark=low_mutation["watermark"],
            )
            high_mutation = applied(
                store,
                [change("entity_ack_race_high")],
                fingerprint="ack-race-high",
            )
            high_delivery = store.inbox(subscription["subscription_id"])

        verification_barrier = threading.Barrier(2)
        high_ack_complete = threading.Event()
        real_verify_plan_token = v4.verify_plan_token

        def synchronized_verify(token: str, key: bytes | str) -> dict[str, Any]:
            payload = real_verify_plan_token(token, key)
            verification_barrier.wait(timeout=5)
            if payload["through_watermark"] == low_mutation["watermark"]:
                self.assertTrue(high_ack_complete.wait(timeout=5))
            return payload

        def acknowledge(delivery: dict[str, Any], *, high: bool) -> str:
            try:
                with v4.open_store(
                    self.store_path,
                    readonly=False,
                    plan_hmac_key=PLAN_KEY,
                ) as worker_store:
                    worker_store.ack(
                        subscription["subscription_id"],
                        through_watermark=delivery["advance_to_watermark"],
                        ack_token=delivery["ack_token"],
                    )
                return "acknowledged"
            except v4.SubscriptionError:
                return "stale"
            finally:
                if high:
                    high_ack_complete.set()

        v4.verify_plan_token = synchronized_verify
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                high_future = executor.submit(acknowledge, high_delivery, high=True)
                low_future = executor.submit(acknowledge, low_delivery, high=False)
                self.assertEqual(high_future.result(timeout=10), "acknowledged")
                self.assertEqual(low_future.result(timeout=10), "stale")
        finally:
            v4.verify_plan_token = real_verify_plan_token

        with v4.open_store(
            self.store_path,
            readonly=True,
            plan_hmac_key=PLAN_KEY,
        ) as store:
            current = store._subscription(subscription["subscription_id"])
            self.assertEqual(int(current["last_ack_watermark"]), high_mutation["watermark"])

    def test_plan_token_tamper_and_store_generation_mismatch(self) -> None:
        with self.create_store() as store:
            changes = [change("entity_planned")]
            token = store.create_plan_token(["entity_planned"], fingerprint="planned-change")
            tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
            with self.assertRaises(v4.PlanTokenError):
                applied(
                    store,
                    changes,
                    fingerprint="planned-change",
                    plan_token=tampered,
                )
            store.connection.execute(
                "UPDATE graph_state SET generation=generation+1 WHERE singleton=1"
            )
            store.connection.commit()
            with self.assertRaises(v4.PlanTokenError):
                applied(
                    store,
                    changes,
                    fingerprint="planned-change",
                    plan_token=token,
                )
        other_path = self.root / "other.sqlite3"
        with self.create_store(other_path) as other:
            with self.assertRaises(v4.PlanTokenError):
                applied(
                    other,
                    [change("entity_planned")],
                    fingerprint="planned-change",
                    plan_token=token,
                )


class SearchRecoveryAndSafetyTests(V4TestCase):
    def test_fts_is_fresh_at_commit_watermark(self) -> None:
        with self.create_store() as store:
            result = applied(
                store,
                [change("entity_fts", name="Saffron needle discovery")],
                fingerprint="fts-create",
            )
            found = store.search("saffron needle", limit=10)
            self.assertEqual(found["watermark"], result["watermark"])
            self.assertIn("entity_fts", {item["record_id"] for item in found["results"]})

    def test_backup_restore_and_validation(self) -> None:
        backup_path = self.root / "backup.sqlite3"
        with self.create_store() as store:
            applied(store, [change("entity_backup")], fingerprint="backup-create")
            source_state = store.state()
            backup = store.backup(backup_path)
            self.assertEqual(backup["watermark"], source_state["watermark"])
        with v4.open_store(backup_path, readonly=True, plan_hmac_key=PLAN_KEY) as restored:
            self.assertEqual(restored.state()["watermark"], source_state["watermark"])
            restored_payload = require_mapping(
                restored.get_record("entity_backup")["payload"],
                label="restored record payload",
            )
            self.assertEqual(restored_payload["id"], "entity_backup")
            self.assertTrue(restored.validate(full=True)["valid"])
        manifest_path = backup_path.with_suffix(backup_path.suffix + ".manifest.json")
        self.assertTrue(manifest_path.is_file())

    def test_backup_no_overwrite_is_atomic_against_a_racing_writer(self) -> None:
        backup_path = self.root / "racing-backup.sqlite3"
        competitor = b"competitor backup\n"
        real_link = os.link

        def race(source: str | Path, destination: str | Path) -> None:
            Path(destination).write_bytes(competitor)
            real_link(source, destination)

        with self.create_store() as store:
            with mock.patch.object(v4.os, "link", side_effect=race):
                with self.assertRaises(FileExistsError):
                    store.backup(backup_path)
        self.assertEqual(backup_path.read_bytes(), competitor)

    def test_backup_leaves_an_orphan_when_manifest_publication_loses_a_race(self) -> None:
        backup_path = self.root / "manifest-race.sqlite3"
        manifest_path = backup_path.with_name(f"{backup_path.name}.manifest.json")
        competitor = b"competitor manifest\n"
        real_link = os.link
        link_count = 0

        def race(source: str | Path, destination: str | Path) -> None:
            nonlocal link_count
            link_count += 1
            if link_count == 2:
                Path(destination).write_bytes(competitor)
            real_link(source, destination)

        with self.create_store() as store:
            with mock.patch.object(v4.os, "link", side_effect=race):
                with self.assertRaises(FileExistsError):
                    store.backup(backup_path)
        # A valid sidecar is the completion marker. Portable Python cannot safely
        # delete this final pathname after a non-cooperative writer wins the
        # manifest name, so the unaccompanied database remains an explicit orphan.
        self.assertEqual(v4.detect_store(backup_path), "sqlite_v4")
        self.assertEqual(manifest_path.read_bytes(), competitor)

    def test_fast_and_full_validation(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_validate")], fingerprint="validate-create")
            fast = store.validate(full=False)
            full = store.validate(full=True)
            self.assertTrue(fast["valid"], fast)
            self.assertTrue(full["valid"], full)
            self.assertEqual(fast["state"]["watermark"], full["state"]["watermark"])

    def test_wal_allows_writer_while_long_reader_is_open_and_recovers(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_before_reader")], fingerprint="reader-before")
        reader = sqlite3.connect(self.store_path)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM records").fetchone()
        try:
            with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as writer:
                applied(
                    writer,
                    [change("entity_during_reader")],
                    fingerprint="reader-during",
                )
                checkpoint = require_mapping(
                    writer.checkpoint("PASSIVE"), label="checkpoint result"
                )
                self.assertGreaterEqual(checkpoint["wal_pages"], checkpoint["checkpointed_pages"])
        finally:
            reader.rollback()
            reader.close()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as recovered:
            recovered_payload = require_mapping(
                recovered.get_record("entity_during_reader")["payload"],
                label="recovered record payload",
            )
            self.assertEqual(
                recovered_payload["id"],
                "entity_during_reader",
            )
            self.assertTrue(recovered.validate(full=True)["valid"])

    def test_committed_wal_survives_abrupt_process_exit(self) -> None:
        with self.create_store():
            pass
        child = """
import os
import sys
sys.path.insert(0, sys.argv[1])
import knowledge_graph_v4 as v4
store = v4.open_store(sys.argv[2], plan_hmac_key=bytes.fromhex('11' * 32))
store.apply_changes(
    [{
        'record_id': 'entity_crash_committed',
        'collection': 'entities',
        'payload': {
            'id': 'entity_crash_committed',
            'namespace': 'client_alpha',
            'type': 'site',
            'name': 'Crash committed record',
            'aliases': ['Crash committed record'],
            'identity_keys': [{
                'scheme': 'benchmark',
                'namespace': 'client_alpha',
                'value': 'crash-committed'
            }],
            'lifecycle': 'active',
            'status': 'current',
            'sensitive': False,
        },
        'change_kind': 'upsert',
        'expected_revision': None,
        'search_text': 'crash committed record',
    }],
    fingerprint='crash-child',
    action='capture',
    discovery_kind='maintenance',
    producer_agent_id='crash-child',
    task_id='wal-recovery-test',
    based_on_watermark=store.state()['watermark'],
    expected_revisions={},
    idempotency_key='crash-child',
)
os._exit(17)
"""
        result = subprocess.run(
            [sys.executable, "-c", child, str(SCRIPT_DIR), str(self.store_path)],
            cwd=SCRIPT_DIR,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as recovered:
            recovered_payload = require_mapping(
                recovered.get_record("entity_crash_committed")["payload"],
                label="crash-recovered record payload",
            )
            self.assertEqual(
                recovered_payload["name"],
                "Crash committed record",
            )
            self.assertTrue(recovered.validate(full=True)["valid"])

    def test_cloud_and_network_paths_are_rejected(self) -> None:
        cloud_root = self.root / "Library" / "Mobile Documents"
        cloud_root.mkdir(parents=True)
        cloud_path = cloud_root / "graph.sqlite3"
        inspection = v4.inspect_store_path(cloud_path)
        self.assertFalse(inspection["safe"])
        self.assertIn("cloud", " ".join(inspection["reasons"]).lower())
        with self.assertRaises(v4.PathSafetyError):
            v4.create_store(cloud_path, plan_hmac_key=PLAN_KEY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
