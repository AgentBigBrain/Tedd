#!/usr/bin/env python3
"""Focused compatibility tests for the extracted v4 validation kernel."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import cast
from unittest import mock

import knowledge_graph_v4 as v4  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402

PLAN_KEY = bytes.fromhex("33" * 32)


def validation_counts(result: dict[str, object]) -> dict[str, int]:
    counts = result.get("counts")
    if not isinstance(counts, dict) or not all(
        isinstance(key, str) and isinstance(value, int) for key, value in counts.items()
    ):
        raise AssertionError("Validation counts must map strings to integers")
    return cast(dict[str, int], counts)


class V4ValidationKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        private_parent = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
        self.temporary = tempfile.TemporaryDirectory(
            prefix="kg-v4-validation-test-",
            dir=private_parent,
        )
        self.root = Path(self.temporary.name).resolve()
        set_private_path_mode(self.root, 0o700)
        self.store_path = self.root / "graph.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_populated_store(self) -> v4.KnowledgeGraphV4:
        store = v4.create_store(self.store_path, plan_hmac_key=PLAN_KEY)
        store.apply_changes(
            [
                {
                    "record_id": "entity_validation",
                    "collection": "entities",
                    "payload": {
                        "id": "entity_validation",
                        "namespace": "validation",
                        "type": "fixture",
                        "name": "Validation fixture",
                        "aliases": [],
                        "identity_keys": [],
                        "lifecycle": "active",
                        "status": "current",
                        "sensitive": False,
                    },
                    "change_kind": "upsert",
                    "search_text": "entity validation fixture",
                }
            ],
            fingerprint="validation-fixture",
            action="capture",
            discovery_kind="maintenance",
            producer_agent_id="validation-test-agent",
            task_id="validation-test-task",
            based_on_watermark=0,
            expected_revisions={},
        )
        return store

    def _set_legacy_append_only_schema(self) -> None:
        with closing(sqlite3.connect(self.store_path)) as connection, connection:
            for trigger_name, _ in v4._v4_schema.APPEND_ONLY_UPGRADE_TRIGGERS:
                connection.execute(f'DROP TRIGGER "{trigger_name}"')
            connection.execute(f"PRAGMA user_version={v4._v4_schema.PREVIOUS_SCHEMA_USER_VERSION}")

    def test_readonly_legacy_schema_fails_with_writable_upgrade_instruction(self) -> None:
        with self._create_populated_store():
            pass
        self._set_legacy_append_only_schema()

        with self.assertRaisesRegex(
            v4.StoreFormatError,
            "requires a one-time writable upgrade; reopen this store with readonly=False",
        ):
            v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY)

        with closing(sqlite3.connect(self.store_path)) as connection, connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                v4._v4_schema.PREVIOUS_SCHEMA_USER_VERSION,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_schema "
                    "WHERE type='trigger' AND name IN ({})".format(
                        ",".join("?" for _ in v4._v4_schema.APPEND_ONLY_UPGRADE_TRIGGERS)
                    ),
                    tuple(name for name, _ in v4._v4_schema.APPEND_ONLY_UPGRADE_TRIGGERS),
                ).fetchone()[0],
                0,
            )

    def test_writable_legacy_schema_upgrade_is_atomic_and_idempotent(self) -> None:
        with self._create_populated_store():
            pass
        self._set_legacy_append_only_schema()

        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as upgraded:
            self.assertEqual(upgraded.get_record("entity_validation")["revision"], 1)
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY):
            pass
        with v4.open_store(
            self.store_path,
            readonly=True,
            plan_hmac_key=PLAN_KEY,
        ):
            pass

        expected = {
            name: " ".join(statement.split())
            for name, statement in v4._v4_schema.APPEND_ONLY_TRIGGERS
        }
        with closing(sqlite3.connect(self.store_path)) as connection, connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                v4.SCHEMA_USER_VERSION,
            )
            observed = {
                str(name): " ".join(str(statement).split())
                for name, statement in connection.execute(
                    "SELECT name,sql FROM sqlite_schema "
                    "WHERE type='trigger' AND name LIKE 'immutable_%'"
                )
            }
        self.assertEqual(observed, expected)

    def test_failed_legacy_schema_upgrade_rolls_back_trigger_and_version(self) -> None:
        with self._create_populated_store():
            pass
        self._set_legacy_append_only_schema()
        valid = v4._v4_schema.APPEND_ONLY_UPGRADE_TRIGGERS
        broken = (valid[0], (valid[1][0], "CREATE TRIGGER deliberately invalid"))

        with (
            mock.patch.object(
                v4._v4_schema,
                "APPEND_ONLY_UPGRADE_TRIGGERS",
                broken,
            ),
            self.assertRaisesRegex(
                v4.StoreFormatError,
                "upgrade failed and was rolled back",
            ),
        ):
            v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY)

        with closing(sqlite3.connect(self.store_path)) as connection, connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                v4._v4_schema.PREVIOUS_SCHEMA_USER_VERSION,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_schema "
                    "WHERE type='trigger' AND name IN ({})".format(
                        ",".join("?" for _ in v4._v4_schema.APPEND_ONLY_UPGRADE_TRIGGERS)
                    ),
                    tuple(name for name, _ in v4._v4_schema.APPEND_ONLY_UPGRADE_TRIGGERS),
                ).fetchone()[0],
                0,
            )

    def test_legacy_schema_rejects_a_tampered_existing_append_only_trigger(self) -> None:
        with self._create_populated_store():
            pass
        self._set_legacy_append_only_schema()
        trigger_name = v4._v4_schema.LEGACY_APPEND_ONLY_TRIGGERS[0][0]
        with closing(sqlite3.connect(self.store_path)) as connection, connection:
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
            connection.execute(
                f"CREATE TRIGGER {trigger_name} "
                "BEFORE UPDATE ON record_revision_headers BEGIN SELECT 1; END"
            )

        with self.assertRaisesRegex(
            v4.StoreFormatError,
            "not an exact legacy append-only schema",
        ):
            v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY)

        with closing(sqlite3.connect(self.store_path)) as connection, connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                v4._v4_schema.PREVIOUS_SCHEMA_USER_VERSION,
            )
            for name, _ in v4._v4_schema.APPEND_ONLY_UPGRADE_TRIGGERS:
                self.assertIsNone(
                    connection.execute(
                        "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
                        (name,),
                    ).fetchone()
                )

    def test_current_schema_rejects_tampered_payload_trigger(self) -> None:
        with self._create_populated_store():
            pass
        trigger_name = v4._v4_schema.APPEND_ONLY_TRIGGERS[0][0]
        with closing(sqlite3.connect(self.store_path)) as connection, connection:
            connection.execute(f'DROP TRIGGER "{trigger_name}"')
            connection.execute(
                f"CREATE TRIGGER {trigger_name} "
                "BEFORE UPDATE ON record_revision_payloads BEGIN SELECT 1; END"
            )

        with self.assertRaisesRegex(
            v4.StoreFormatError,
            "append-only trigger is missing or incompatible",
        ):
            v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY)

    def test_wrapper_resolves_hash_and_private_callbacks_at_call_time(self) -> None:
        with self._create_populated_store() as store:
            real_stable_hash = v4.stable_hash
            real_entry_material = store._v4_entry_material
            with (
                mock.patch.object(v4, "stable_hash", wraps=real_stable_hash) as stable_hash,
                mock.patch.object(
                    store,
                    "_v4_entry_material",
                    wraps=real_entry_material,
                ) as entry_material,
                mock.patch.object(
                    store,
                    "_state_root",
                    return_value="adapter-state-root",
                ) as state_root,
            ):
                result = store.validate(full=True)

        self.assertTrue(result["valid"], result)
        self.assertEqual(result["state_root"], "adapter-state-root")
        self.assertGreaterEqual(stable_hash.call_count, 1)
        entry_material.assert_called_once()
        state_root.assert_called_once_with()
        self.assertEqual(
            list(result),
            ["valid", "full", "errors", "warnings", "state", "state_root", "counts"],
        )
        self.assertEqual(
            list(validation_counts(result)),
            [*v4.COLLECTIONS, "records", "revisions", "mutations"],
        )

    def test_fast_validation_preserves_state_and_fts_error_order(self) -> None:
        with self._create_populated_store() as store:
            with store.connection:
                store.connection.execute(
                    "UPDATE graph_state SET committed_watermark=2,journal_head_hash='bad' "
                    "WHERE singleton=1"
                )
                store.connection.execute("DELETE FROM records_fts")

            result = store.validate(full=False)

        self.assertFalse(result["valid"])
        self.assertIsNone(result["state_root"])
        self.assertEqual(
            result["errors"],
            [
                "graph_state watermark disagrees with mutation tail",
                "graph_state journal head disagrees with mutation tail",
                "FTS count 0 differs from record count 1",
                "1 current FTS documents disagree with records",
            ],
        )
        self.assertEqual(result["warnings"], [])
        counts = validation_counts(result)
        self.assertEqual(counts["records"], 1)
        self.assertEqual(counts["revisions"], 1)
        self.assertEqual(counts["mutations"], 1)

    def test_full_validation_reports_payload_hash_mismatch_exactly(self) -> None:
        with self._create_populated_store() as store:
            with store.connection:
                # Simulate external corruption only after deliberately disabling
                # the SQL invariant whose behavior has separate regression tests.
                store.connection.execute("DROP TRIGGER immutable_revision_payloads_update")
                store.connection.execute(
                    "UPDATE record_revision_payloads SET payload_json=? "
                    "WHERE record_id=? AND revision=1",
                    ('{"id":"tampered"}', "entity_validation"),
                )

            result = store.validate(full=True)

        self.assertFalse(result["valid"])
        self.assertEqual(
            result["errors"],
            ["revision payload hash mismatch: entity_validation@1"],
        )
        self.assertIsInstance(result["state_root"], str)


if __name__ == "__main__":
    unittest.main()
