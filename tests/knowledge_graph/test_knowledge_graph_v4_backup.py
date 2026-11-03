#!/usr/bin/env python3
"""Compatibility and characterization tests for extracted v4 maintenance kernels."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import unittest
import uuid
from pathlib import Path
from typing import cast
from unittest import mock

import knowledge_graph_v4 as v4  # noqa: E402
from tedd_knowledge_graph import v4_backup  # noqa: E402

from tests.support.graph_cases import V4TestCase, applied, change  # noqa: E402

FIXED_TIME = "2026-01-01T12:00:00+00:00"
FIXED_STORE_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")

EXPECTED_MAINTENANCE_SIGNATURES = {
    "export_current_graph": (
        "(self, *, schema_version: 'str' = 'v4', include_journal: 'bool' = True) "
        "-> 'dict[str, object]'"
    ),
    "_export_current_graph_snapshot": (
        "(self, *, schema_version: 'str', include_journal: 'bool') -> 'dict[str, object]'"
    ),
    "checkpoint": "(self, mode: 'str' = 'PASSIVE') -> 'dict[str, object]'",
    "create_audit_checkpoint": (
        "(self, *, backup_manifest_hash: 'str | None' = None) -> 'dict[str, object]'"
    ),
    "backup": (
        "(self, destination: 'str | Path', *, overwrite: 'bool' = False) -> 'dict[str, object]'"
    ),
}

# These canonical JSON digests were recorded against the pre-extraction facade.
# They cover both schema variants and both journal modes without freezing paths
# or SQLite's platform-specific physical file representation.
EXPECTED_EXPORT_DIGESTS = {
    ("v4", True): "b4e3d49c6781b1419f2b3121d6e4f3d5dc002bf3aef078c9eb215362c383c62c",
    ("v4", False): "2535d2bb6661367b1eec22bcd13dd127080c738d1742c89cb094096075f2ed96",
    ("v3", True): "667d2dab00852dc29007de1ce87732302496b839002e76098b3a3274c5e11fb5",
    ("v3", False): "741b3287a3afde46428ea8fae3aaae472ed6b65245ff8c8c13a56dbf59f90fb1",
}


class V4MaintenanceFacadeTests(V4TestCase):
    def test_maintenance_method_signatures_remain_exact(self) -> None:
        observed = {
            name: str(inspect.signature(getattr(v4.KnowledgeGraphV4, name)))
            for name in EXPECTED_MAINTENANCE_SIGNATURES
        }
        self.assertEqual(observed, EXPECTED_MAINTENANCE_SIGNATURES)

    def test_export_resolves_the_private_snapshot_seam_at_call_time(self) -> None:
        expected = {"schema_version": "v4", "journal": []}
        with self.create_store() as store:
            with mock.patch.object(
                store,
                "_export_current_graph_snapshot",
                autospec=True,
                return_value=expected,
            ) as snapshot_loader:
                observed = store.export_current_graph(
                    schema_version="v4",
                    include_journal=False,
                )

            snapshot_loader.assert_called_once_with(
                schema_version="v4",
                include_journal=False,
            )
            self.assertFalse(store.connection.in_transaction)
        self.assertIs(observed, expected)

    def test_audit_checkpoint_samples_and_writes_inside_one_immediate_transaction(
        self,
    ) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_checkpoint")], fingerprint="checkpoint-state")
            real_state = store.state
            real_state_root = store._state_root
            sampled_transaction_state: list[tuple[str, bool]] = []
            transaction_statements: list[str] = []

            def state_in_transaction() -> dict[str, object]:
                sampled_transaction_state.append(("state", store.connection.in_transaction))
                return cast(dict[str, object], real_state())

            def root_in_transaction() -> str:
                sampled_transaction_state.append(("root", store.connection.in_transaction))
                return cast(str, real_state_root())

            def trace(statement: str) -> None:
                normalized = statement.strip().upper()
                if normalized in {"BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"}:
                    transaction_statements.append(normalized)

            store.connection.set_trace_callback(trace)
            try:
                with (
                    mock.patch.object(store, "state", side_effect=state_in_transaction),
                    mock.patch.object(store, "_state_root", side_effect=root_in_transaction),
                ):
                    checkpoint = store.create_audit_checkpoint()
            finally:
                store.connection.set_trace_callback(None)

            self.assertEqual(
                sampled_transaction_state,
                [("state", True), ("root", True)],
            )
            self.assertEqual(transaction_statements, ["BEGIN IMMEDIATE", "COMMIT"])
            self.assertEqual(checkpoint["watermark"], store.state()["watermark"])
            self.assertFalse(store.connection.in_transaction)

    def test_audit_checkpoint_rolls_back_when_snapshot_sampling_fails(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_checkpoint")], fingerprint="checkpoint-rollback")
            with mock.patch.object(
                store,
                "_state_root",
                side_effect=RuntimeError("injected state-root failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "state-root failure"):
                    store.create_audit_checkpoint()

            count = store.connection.execute("SELECT COUNT(*) FROM audit_checkpoints").fetchone()
            self.assertIsNotNone(count)
            self.assertEqual(int(count[0]), 0)
            self.assertFalse(store.connection.in_transaction)

    def test_audit_checkpoint_rejects_an_existing_transaction(self) -> None:
        with self.create_store() as store:
            applied(store, [change("entity_checkpoint")], fingerprint="checkpoint-active")
            store.connection.execute("BEGIN")
            try:
                with self.assertRaisesRegex(v4.V4Error, "no active transaction"):
                    store.create_audit_checkpoint()
                self.assertTrue(store.connection.in_transaction)
            finally:
                store.connection.rollback()

    def test_backup_resolves_facade_filesystem_policy_at_call_time(self) -> None:
        destination = self.root / "delegated.sqlite3"
        expected: dict[str, v4_backup.JsonValue] = {"format": "delegated"}
        observed_dependencies: list[v4_backup.BackupDependencies] = []

        def delegated_backup(
            connection: sqlite3.Connection,
            source_path: Path,
            requested_destination: str | Path,
            *,
            overwrite: bool,
            dependencies: v4_backup.BackupDependencies,
        ) -> dict[str, v4_backup.JsonValue]:
            self.assertIs(connection, store.connection)
            self.assertEqual(source_path, self.store_path)
            self.assertEqual(requested_destination, destination)
            self.assertFalse(overwrite)
            observed_dependencies.append(dependencies)
            return expected

        with self.create_store() as store:
            with (
                mock.patch.object(v4, "Path", autospec=True) as path_factory,
                mock.patch.object(v4, "_publish_staged_file", autospec=True) as publisher,
                mock.patch.object(v4, "utc_now", autospec=True) as clock,
                mock.patch.object(
                    v4_backup,
                    "backup",
                    autospec=True,
                    side_effect=delegated_backup,
                ) as backup_kernel,
            ):
                observed = store.backup(destination)

        self.assertIs(observed, expected)
        backup_kernel.assert_called_once()
        self.assertEqual(len(observed_dependencies), 1)
        self.assertIs(observed_dependencies[0].path_factory, path_factory)
        self.assertIs(observed_dependencies[0].publish_staged_file, publisher)
        self.assertIs(observed_dependencies[0].utc_now, clock)


class V4MaintenanceCharacterizationTests(V4TestCase):
    def test_export_bytes_match_the_pre_extraction_characterization(self) -> None:
        with (
            mock.patch.object(v4.uuid, "uuid4", return_value=FIXED_STORE_ID),
            mock.patch.object(v4, "utc_now", return_value=FIXED_TIME),
        ):
            with self.create_store() as store:
                applied(
                    store,
                    [change("entity_characterized")],
                    fingerprint="backup-export-characterization",
                )
                observed = {
                    (schema_version, include_journal): hashlib.sha256(
                        json.dumps(
                            store.export_current_graph(
                                schema_version=schema_version,
                                include_journal=include_journal,
                            ),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode("utf-8")
                    ).hexdigest()
                    for schema_version in ("v4", "v3")
                    for include_journal in (True, False)
                }

        self.assertEqual(observed, EXPECTED_EXPORT_DIGESTS)

    def test_backup_manifest_keeps_canonical_bytes_and_file_digest(self) -> None:
        destination = self.root / "characterized.sqlite3"
        with self.create_store() as store:
            applied(store, [change("entity_backup")], fingerprint="backup-characterization")
            with mock.patch.object(v4, "utc_now", return_value=FIXED_TIME):
                result = store.backup(destination)

        manifest_path = Path(str(result["manifest_path"]))
        published_bytes = manifest_path.read_bytes()
        manifest = {key: value for key, value in result.items() if key != "manifest_path"}
        expected_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.assertEqual(published_bytes, expected_bytes)
        self.assertEqual(
            hashlib.sha256(destination.read_bytes()).hexdigest(),
            result["sha256"],
        )
        self.assertEqual(destination.stat().st_size, result["bytes"])


if __name__ == "__main__":
    unittest.main()
