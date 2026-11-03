"""Operation snapshot contracts and deterministic WAL schedules."""

from __future__ import annotations

import sqlite3
import unittest


class ReadSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        from tedd_knowledge_graph.v4_read_snapshot import read_snapshot

        self.snapshot = read_snapshot
        self.connection = sqlite3.connect(":memory:", factory=_FailingRollbackConnection)
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE fixture (value INTEGER)")
        self.connection.execute("INSERT INTO fixture VALUES (1)")
        self.connection.commit()

    def test_success_ends_only_its_read_transaction_without_writes(self) -> None:
        statements: list[str] = []
        self.connection.set_trace_callback(statements.append)
        changes = self.connection.total_changes
        with self.snapshot(self.connection):
            self.assertTrue(self.connection.in_transaction)
            self.assertEqual(self.connection.execute("SELECT value FROM fixture").fetchone(), (1,))
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(self.connection.total_changes, changes)
        self.assertEqual(statements, ["BEGIN", "SELECT value FROM fixture", "ROLLBACK"])

    def test_operation_failure_and_cancellation_release_owned_transaction(self) -> None:
        for error in (ValueError("operation"), KeyboardInterrupt("cancelled")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)) as raised:
                    with self.snapshot(self.connection):
                        raise error
                self.assertIs(raised.exception, error)
                self.assertFalse(self.connection.in_transaction)

    def test_nested_success_and_failure_preserve_caller_uncommitted_work(self) -> None:
        self.connection.execute("UPDATE fixture SET value=2")
        for fails in (False, True):
            with self.subTest(fails=fails):
                try:
                    with self.snapshot(self.connection), self.snapshot(self.connection):
                        self.assertEqual(
                            self.connection.execute("SELECT value FROM fixture").fetchone(), (2,)
                        )
                        if fails:
                            raise ValueError("operation")
                except ValueError:
                    pass
                self.assertTrue(self.connection.in_transaction)
                self.assertEqual(
                    self.connection.execute("SELECT value FROM fixture").fetchone(), (2,)
                )
        self.connection.rollback()
        self.assertEqual(self.connection.execute("SELECT value FROM fixture").fetchone(), (1,))

    def test_cleanup_failure_propagates(self) -> None:
        self.connection.fail_rollback = True
        with self.assertRaises(sqlite3.OperationalError) as raised:
            with self.snapshot(self.connection):
                self.connection.execute("SELECT value FROM fixture").fetchall()
        self.assertIs(raised.exception, self.connection.cleanup_error)

    def test_operation_error_remains_primary_when_cleanup_also_fails(self) -> None:
        self.connection.fail_rollback = True
        operation_error = KeyboardInterrupt("cancelled")
        with self.assertRaises(KeyboardInterrupt) as raised:
            with self.snapshot(self.connection):
                raise operation_error
        self.assertIs(raised.exception, operation_error)
        self.assertIs(raised.exception.__cause__, self.connection.cleanup_error)


class _FailingRollbackConnection(sqlite3.Connection):
    fail_rollback = False
    cleanup_error = sqlite3.OperationalError("synthetic rollback failure")

    def rollback(self) -> None:
        if self.fail_rollback:
            raise self.cleanup_error
        super().rollback()
