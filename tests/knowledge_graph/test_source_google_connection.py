"""Synthetic durable Google generation/credential races; no provider calls."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import tempfile
import threading
import unittest
from multiprocessing.connection import Connection
from pathlib import Path
from unittest import mock

from benchmark_support import private_temporary_parent
from platform_support import LockableFile
from tedd_knowledge_graph import source_files
from tedd_knowledge_graph.source_contracts import SourceArchiveError
from tedd_knowledge_graph.source_credentials import CredentialStore
from tedd_knowledge_graph.source_google_connection import ConnectionStamp, PreparedDisconnect
from tedd_knowledge_graph.v4_application_contracts import JsonObject, JsonValue


def _late_publication(
    root: str, key: str, stamp: ConnectionStamp, pipe: Connection[object, object]
) -> None:
    from tedd_knowledge_graph.source_google_connection import GoogleConnection

    connection = GoogleConnection(CredentialStore(Path(root)), key)
    try:
        pipe.send("acquired")
        if not pipe.poll(10) or pipe.recv() != "continue":
            raise RuntimeError("synthetic coordination failed")
        try:
            with connection.hold(stamp):
                pipe.send("published")
        except SourceArchiveError:
            pipe.send("refused")
    finally:
        pipe.close()


def _crash_sender(root: str, key: str, prepared: PreparedDisconnect) -> None:
    from tedd_knowledge_graph.source_google_connection import GoogleConnection

    def sender(_payload: bytes) -> str:
        # Simulate lost process after the durable dispatch boundary, not a known
        # unsent network error. The parent's private fixture owns all cleanup.
        os._exit(23)

    GoogleConnection(CredentialStore(Path(root)), key).dispatch(prepared, sender)


def _crash_sender_lock(root: str, key: str, phase: str) -> None:
    from tedd_knowledge_graph import source_google_connection as lifecycle

    connection = lifecycle.GoogleConnection(CredentialStore(Path(root)), key)
    intent = connection.intent("f" * 64)
    empty_lock = lifecycle._empty_lock
    create_file = lifecycle.create_private_exclusive_file
    sync_file = os.fsync
    sync_directory = lifecycle.fsync_directory

    def created(path: Path) -> int:
        create_file(path)
        os._exit(24)

    def file_synced(descriptor: int) -> None:
        sync_file(descriptor)
        os._exit(24)

    def directory_synced(path: Path) -> None:
        sync_directory(path)
        os._exit(24)

    def interrupt_lock(path: Path) -> None:
        if phase == "created":
            with mock.patch.object(lifecycle, "create_private_exclusive_file", side_effect=created):
                empty_lock(path)
        elif phase == "file_synced":
            with mock.patch.object(os, "fsync", side_effect=file_synced):
                empty_lock(path)
        elif phase == "directory_synced":
            with mock.patch.object(lifecycle, "fsync_directory", side_effect=directory_synced):
                empty_lock(path)
        else:
            raise AssertionError("unknown synthetic crash boundary")

    with mock.patch.object(lifecycle, "_empty_lock", side_effect=interrupt_lock):
        connection.disable(intent)


def _held_boundary(
    root: str,
    key: str,
    value: ConnectionStamp | PreparedDisconnect,
    pipe: Connection[object, object],
) -> None:
    from tedd_knowledge_graph.source_google_connection import GoogleConnection

    connection = GoogleConnection(CredentialStore(Path(root)), key)

    def pause() -> None:
        pipe.send("held")
        if not pipe.poll(15) or pipe.recv() != "release":
            raise RuntimeError("synthetic held-boundary coordination failed")

    def sender(_payload: bytes) -> str:
        pause()
        return "uncertain"

    try:
        if isinstance(value, ConnectionStamp):
            with connection.hold(value):
                pause()
        else:
            connection.dispatch(value, sender)
    finally:
        pipe.close()


class GoogleConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=private_temporary_parent())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # Only this new synthetic directory receives private permissions.
        from platform_support import set_private_path_mode

        set_private_path_mode(self.root, 0o700)
        self.store = CredentialStore(self.root)
        self.key = "a" * 64
        self.saved: dict[str, bytes] = {}
        # Mock OS secret storage, not generation/state files or their locks.
        self.get = mock.patch.object(self.store, "get", side_effect=lambda key: self.saved.get(key))
        self.put = mock.patch.object(self.store, "put", side_effect=self.saved.__setitem__)
        self.delete = mock.patch.object(
            self.store, "delete", side_effect=lambda key: self.saved.pop(key, None)
        )
        for patcher in (self.get, self.put, self.delete):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_refresh_compare_and_swap_rejects_old_success_and_old_failure(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        initial = connection.snapshot()
        current = connection.replace(initial, b"synthetic-grant", new_grant=True)
        old = connection.snapshot()
        refreshed = connection.replace(current, b"synthetic-refresh")
        with self.assertRaises(SourceArchiveError):
            connection.replace(old, b"late-refresh")
        with self.assertRaises(SourceArchiveError):
            connection.reject(old)
        self.assertEqual(connection.snapshot(), refreshed)
        self.assertEqual(self.saved[self.key], b"synthetic-refresh")

    def test_new_grant_fences_old_reservations_and_other_roots(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        old = connection.snapshot()
        current = connection.replace(old, b"synthetic-grant", new_grant=True)
        with self.assertRaises(SourceArchiveError), connection.hold(old.stamp):
            self.fail("stale generation entered publication")
        with connection.hold(current.stamp):
            pass
        other_root = source_files.private_root(self.root / "other", create=True)
        other = GoogleConnection(CredentialStore(other_root), self.key)
        with self.assertRaises(SourceArchiveError), other.hold(current.stamp):
            self.fail("another credential root entered publication")

    def test_failed_credential_publication_stays_disabled_and_survives_restart(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        initial = connection.snapshot()
        with mock.patch.object(self.store, "put", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                connection.replace(initial, b"synthetic-grant", new_grant=True)
        restarted = GoogleConnection(self.store, self.key)
        self.assertFalse(restarted.snapshot().stamp.enabled)
        with self.assertRaises(SourceArchiveError), restarted.hold(initial.stamp):
            self.fail("failed grant publication retained access")

    def test_corrupt_or_missing_existing_state_is_not_reinitialized(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        path = connection.root / "state.json"
        source_files.write_bytes(path, b"{}", overwrite=True)
        with self.assertRaises(SourceArchiveError):
            GoogleConnection(self.store, self.key)
        path.unlink()
        with self.assertRaises(SourceArchiveError):
            GoogleConnection(self.store, self.key)

    def test_disconnect_is_durable_idempotent_and_fences_publication(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        granted = connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        prepared = connection.disable(intent)
        self.assertFalse(connection.snapshot().stamp.enabled)
        self.assertNotIn(self.key, self.saved)
        self.assertTrue(prepared.outcome.credentials_removed)
        self.assertEqual(prepared.payload, b"synthetic-grant")
        repeated = GoogleConnection(self.store, self.key).disable(intent)
        self.assertIsNone(repeated.payload)
        self.assertEqual(repeated.outcome, prepared.outcome)
        with self.assertRaises(SourceArchiveError), connection.hold(granted.stamp):
            self.fail("disconnected grant published")
        state = (connection.root / "state.json").read_bytes()
        self.assertNotIn(b"synthetic-grant", state)

    def test_failed_removal_preserves_disable_and_never_removes_replacement(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        with mock.patch.object(self.store, "delete", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                connection.disable(intent)
        disabled = connection.snapshot().stamp
        self.assertFalse(disabled.enabled)
        retired = connection.retire(intent)
        self.assertEqual(retired.revocation, "proven_not_sent")
        reopened = connection.reopen(disabled)
        replacement = connection.replace(reopened, b"replacement", new_grant=True)
        repeated = connection.disable(intent)
        self.assertIsNone(repeated.payload)
        self.assertEqual(connection.snapshot(), replacement)

    def test_failed_secret_read_still_durably_disables_cached_access(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        granted = connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        with mock.patch.object(
            self.store, "get", side_effect=SourceArchiveError("privacy_rejected")
        ):
            with self.assertRaises(SourceArchiveError):
                connection.disable(intent)
        stamp, history = connection.history()
        self.assertFalse(stamp.enabled)
        self.assertEqual(history[0].intent, intent)
        self.assertFalse(history[0].credentials_removed)
        self.assertEqual(history[0].revocation, "not_started")
        with self.assertRaises(SourceArchiveError), connection.hold(granted.stamp):
            self.fail("unreadable credentials left a cached grant active")

    def test_interrupted_sender_preparation_reconciles_only_owned_empty_lock_directory(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        with mock.patch.object(
            connection, "_write", side_effect=OSError("synthetic publication failure")
        ):
            with self.assertRaises(OSError):
                connection.disable(intent)
        self.assertTrue(connection.history()[0].enabled)
        restarted = GoogleConnection(self.store, self.key)
        prepared = restarted.disable(intent)
        self.assertTrue(prepared.outcome.credentials_removed)

    def test_sender_orphan_with_unrecognized_contents_is_not_adopted(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        with mock.patch.object(connection, "_write", side_effect=OSError("synthetic crash")):
            with self.assertRaises(OSError):
                connection.disable(intent)
        orphan = connection.root / (intent.operation_id + ".sender")
        source_files.write_bytes(orphan / "unexpected.txt", b"synthetic unrelated bytes")
        before = (connection.root / "state.json").read_bytes()
        with self.assertRaises(SourceArchiveError):
            connection.disable(intent)
        self.assertEqual((connection.root / "state.json").read_bytes(), before)
        self.assertEqual((orphan / "unexpected.txt").read_bytes(), b"synthetic unrelated bytes")

    def test_empty_locks_do_not_have_staging_or_link_unlink_crash_windows(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        stage = source_files.stage

        def no_staged_lock(path: Path, payload: bytes) -> Path:
            if path.name == "archive.lock":
                raise AssertionError("empty lock still passes through staged publication")
            return stage(path, payload)

        with mock.patch.object(source_files, "stage", side_effect=no_staged_lock):
            connection = GoogleConnection(self.store, self.key)
            connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
            self.assertTrue(
                connection.disable(connection.intent("b" * 64)).outcome.credentials_removed
            )

    def test_history_exhaustion_never_enables_a_grant_without_disconnect_capacity(self) -> None:
        from tedd_knowledge_graph import source_google_connection as lifecycle

        with mock.patch.object(lifecycle, "_MAX_OPERATIONS", 1):
            connection = lifecycle.GoogleConnection(self.store, self.key)
            original = connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
            intent = connection.intent("b" * 64)
            connection.dispatch(connection.disable(intent), lambda _payload: "uncertain")
            disabled = connection.history()[0]
            with self.assertRaises(SourceArchiveError) as refused:
                connection.reopen(disabled)
            self.assertEqual(refused.exception.code, "limit_exceeded")
            self.assertFalse(connection.history()[0].enabled)
            self.assertEqual(connection.history()[1][0].revocation, "uncertain")
            self.assertNotIn(self.key, self.saved)
            with self.assertRaises(SourceArchiveError), connection.hold(original.stamp):
                self.fail("history exhaustion retained cached access")

    def test_process_exit_during_exclusive_lock_creation_is_recoverable(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        for index, phase in enumerate(("created", "file_synced", "directory_synced")):
            with self.subTest(phase=phase):
                key = str(index + 1) * 64
                connection = GoogleConnection(self.store, key)
                connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
                before = (connection.root / "state.json").read_bytes()
                process = multiprocessing.get_context("spawn").Process(
                    target=_crash_sender_lock, args=(str(self.root), key, phase)
                )
                process.start()
                try:
                    process.join(10)
                    self.assertFalse(process.is_alive(), "synthetic crash child did not exit")
                    self.assertEqual(process.exitcode, 24)
                finally:
                    if process.is_alive():
                        process.terminate()
                    process.join(10)
                    process.close()
                # The OS closes the killed process's descriptor. There is no
                # staged payload to identify, delete, or mistake for user data.
                sender_root = connection.root / ("f" * 64 + ".sender")
                self.assertEqual([item.name for item in sender_root.iterdir()], ["archive.lock"])
                self.assertEqual(source_files.read_bytes(sender_root / "archive.lock", 1), b"")
                self.assertEqual((connection.root / "state.json").read_bytes(), before)
                restarted = GoogleConnection(self.store, key)
                prepared = restarted.disable(restarted.intent("f" * 64))
                self.assertTrue(prepared.outcome.credentials_removed)
                self.assertFalse(restarted.history()[0].enabled)

    def test_duplicate_prepared_receipt_does_not_retire_original_sender(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        original = connection.disable(intent)
        duplicate = connection.disable(intent)
        sender = mock.Mock(return_value="confirmed")
        pending = connection.dispatch(duplicate, sender)
        self.assertFalse(pending.sender_retired)
        sender.assert_not_called()
        self.assertEqual(connection.dispatch(original, sender).revocation, "confirmed")
        sender.assert_called_once_with(b"synthetic-grant")

    def test_control_duplicate_after_confirmation_does_not_cancel_original_send(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection
        from tedd_knowledge_graph.source_google_control import disconnect_google

        connection = GoogleConnection(self.store, self.key)
        connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        prepared: list[PreparedDisconnect] = []

        def confirm(_description: str, _action: str) -> bool:
            prepared.append(connection.disable(intent))
            return True

        sender = mock.Mock(return_value="confirmed")
        receipt = disconnect_google(
            connection,
            "owner@example.com",
            "neutral-client",
            "b" * 64,
            threading.Event(),
            confirm,
            sender=sender,
        )
        self.assertFalse(receipt["sender_retired"])
        sender.assert_not_called()
        self.assertEqual(connection.dispatch(prepared[0], sender).revocation, "confirmed")
        sender.assert_called_once()

    def test_disconnect_acknowledgment_waits_for_held_cross_process_publication(self) -> None:
        self._contended_boundary(sender=False)

    def test_sender_retirement_waits_for_active_cross_process_sender(self) -> None:
        self._contended_boundary(sender=True)

    def _contended_boundary(self, *, sender: bool) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        grant = connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        value = connection.disable(intent) if sender else grant.stamp
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_held_boundary, args=(str(self.root), self.key, value, child)
        )
        contested, completed = threading.Event(), threading.Event()
        failures: list[BaseException] = []
        try_lock = source_files.try_file_lock

        def observe_lock(handle: LockableFile, *, shared: bool = False) -> None:
            try:
                try_lock(handle, shared=shared)
            except BlockingIOError:
                contested.set()
                raise

        def act() -> None:
            try:
                if sender:
                    connection.retire(intent)
                else:
                    connection.disable(intent)
            except BaseException as error:
                failures.append(error)
            finally:
                completed.set()

        actor = threading.Thread(target=act)
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(10))
            self.assertEqual(parent.recv(), "held")
            with mock.patch.object(source_files, "try_file_lock", side_effect=observe_lock):
                actor.start()
                self.assertTrue(contested.wait(3), "actual cross-process lock was not contested")
                self.assertFalse(completed.is_set())
                if sender:
                    with self.assertRaises(SourceArchiveError):
                        connection.reopen(connection.history()[0])
                parent.send("release")
                self.assertTrue(completed.wait(5))
            actor.join(5)
            process.join(10)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(failures, [])
            self.assertFalse(connection.history()[0].enabled)
            if sender:
                self.assertTrue(connection.retire(intent).sender_retired)
        finally:
            if process.is_alive():
                process.terminate()
            process.join(10)
            process.close()
            parent.close()
            if actor.ident is not None:
                actor.join(6)

    def test_confirmed_send_is_once_and_old_outcome_survives_new_consent(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        prepared = connection.disable(intent)
        sender = mock.Mock(return_value="confirmed")
        outcome = connection.dispatch(prepared, sender)
        self.assertEqual(outcome.revocation, "confirmed")
        self.assertTrue(outcome.sender_retired)
        self.assertEqual(connection.dispatch(prepared, sender), outcome)
        sender.assert_called_once_with(b"synthetic-grant")
        reopened = connection.reopen(connection.snapshot().stamp)
        replacement = connection.replace(reopened, b"replacement", new_grant=True)
        self.assertEqual(connection.disable(intent).outcome, outcome)
        self.assertEqual(connection.snapshot(), replacement)
        # A previously displayed but not accepted disconnect cannot hit this grant.
        from dataclasses import replace

        stale = replace(intent, operation_id="c" * 64)
        with self.assertRaises(SourceArchiveError):
            connection.disable(stale)
        fresh = connection.intent("c" * 64)
        self.assertTrue(connection.disable(fresh).outcome.credentials_removed)

    def test_retiring_unstarted_sender_prevents_late_dispatch(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        prepared = connection.disable(intent)
        outcome = GoogleConnection(self.store, self.key).retire(intent)
        self.assertEqual(outcome.revocation, "proven_not_sent")
        connection.reopen(connection.snapshot().stamp)
        sender = mock.Mock(side_effect=AssertionError("retired sender ran"))
        self.assertEqual(connection.dispatch(prepared, sender), outcome)
        sender.assert_not_called()

    def test_uncertainty_survives_restart_and_requires_sender_retirement(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        prepared = connection.disable(intent)
        disabled = connection.snapshot().stamp
        with self.assertRaises(SourceArchiveError):
            connection.reopen(disabled)
        sender = mock.Mock(side_effect=OSError("synthetic transport failure"))
        with self.assertRaises(OSError):
            connection.dispatch(prepared, sender)
        restarted = GoogleConnection(self.store, self.key)
        outcome = restarted.retire(intent)
        self.assertEqual(outcome.revocation, "uncertain")
        self.assertTrue(outcome.sender_retired)
        reopened = restarted.reopen(disabled)
        self.assertTrue(reopened.stamp.enabled)
        self.assertIsNone(reopened.payload)
        self.assertEqual(restarted.disable(intent).outcome, outcome)

    def test_other_process_cannot_publish_after_disconnect_acknowledgment(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        granted = connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_late_publication, args=(str(self.root), self.key, granted.stamp, child)
        )
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(10), "child acquisition did not reach its barrier")
            self.assertEqual(parent.recv(), "acquired")
            connection.disable(connection.intent("b" * 64))
            parent.send("continue")
            self.assertTrue(parent.poll(10), "child did not check publication")
            self.assertEqual(parent.recv(), "refused")
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        finally:
            parent.close()
            if process.is_alive():
                process.terminate()
            process.join(10)
            process.close()

    def test_crashed_sender_is_uncertain_and_cannot_retry_after_recovery(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        intent = connection.intent("b" * 64)
        prepared = connection.disable(intent)
        process = multiprocessing.get_context("spawn").Process(
            target=_crash_sender, args=(str(self.root), self.key, prepared)
        )
        process.start()
        try:
            process.join(15)
            self.assertEqual(process.exitcode, 23)
        finally:
            if process.is_alive():
                process.terminate()
            process.join(10)
            process.close()
        restarted = GoogleConnection(self.store, self.key)
        self.assertEqual(restarted.retire(intent).revocation, "uncertain")
        reopened = restarted.reopen(restarted.snapshot().stamp)
        replacement = restarted.replace(reopened, b"replacement", new_grant=True)
        sender = mock.Mock(side_effect=AssertionError("crashed sender retried"))
        self.assertEqual(restarted.dispatch(prepared, sender).revocation, "uncertain")
        self.assertEqual(restarted.snapshot(), replacement)

    def test_late_oauth_success_cannot_replace_new_consent(self) -> None:
        self._oauth_refresh_race(invalid_grant=False)

    def test_late_oauth_failure_cannot_delete_new_consent(self) -> None:
        self._oauth_refresh_race(invalid_grant=True)

    def test_cached_session_checks_generation_before_request_and_after_transfer(self) -> None:
        from tedd_knowledge_graph import source_google
        from tedd_knowledge_graph.source_contracts import SourceScope
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        connection = GoogleConnection(self.store, self.key)
        granted = connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        session = source_google.GoogleSourceSession(
            "neutral-access",
            "google:123",
            frozenset({"selected"}),
            SourceScope("store", "generation", ("subject",), "personal_task"),
            float("inf"),
            _connection=connection,
            _stamp=granted.stamp,
        )

        def request(*_args: object, **_kwargs: object) -> bytes:
            connection.disable(connection.intent("b" * 64))
            return b"synthetic private evidence"

        with mock.patch.object(source_google, "google_request", side_effect=request) as transport:
            with self.assertRaises(SourceArchiveError):
                session._get("https://www.googleapis.com/drive/v3/files/selected", 65536)
            with self.assertRaises(SourceArchiveError):
                session._get("https://www.googleapis.com/drive/v3/files/selected", 65536)
        transport.assert_called_once()

    def test_disconnect_control_cancellation_has_no_effect_and_confirmation_is_once(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection
        from tedd_knowledge_graph.source_google_control import disconnect_google

        connection = GoogleConnection(self.store, self.key)
        granted = connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        confirm = mock.Mock(return_value=False)
        sender = mock.Mock(return_value="confirmed")
        result = disconnect_google(
            connection,
            "owner@example.com",
            "neutral-client",
            "b" * 64,
            threading.Event(),
            confirm,
            sender=sender,
        )
        self.assertEqual(result["status"], "google_disconnect_cancelled")
        self.assertEqual(connection.snapshot(), granted)
        sender.assert_not_called()
        confirm.return_value = True
        result = disconnect_google(
            connection,
            "owner@example.com",
            "neutral-client",
            "b" * 64,
            threading.Event(),
            confirm,
            sender=sender,
        )
        self.assertTrue(result["credentials_removed"])
        self.assertTrue(result["local_access_disabled"])
        self.assertEqual(result["provider_revocation"], "confirmed")
        repeated = disconnect_google(
            connection,
            "owner@example.com",
            "neutral-client",
            "b" * 64,
            threading.Event(),
            confirm,
            sender=sender,
        )
        self.assertEqual(repeated, result)
        self.assertEqual(confirm.call_count, 2)
        sender.assert_called_once()
        self.assertNotIn("synthetic-grant", repr(result))
        repeated_action = disconnect_google(
            connection,
            "owner@example.com",
            "neutral-client",
            "c" * 64,
            threading.Event(),
            confirm,
            sender=sender,
        )
        self.assertEqual(repeated_action, result)
        self.assertEqual(confirm.call_count, 2)

    def test_recovery_is_separate_confirmed_action_and_preserves_uncertainty(self) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection
        from tedd_knowledge_graph.source_google_control import disconnect_google, reconnect_google

        connection = GoogleConnection(self.store, self.key)
        connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        confirm = mock.Mock(return_value=True)
        disconnect_google(
            connection,
            "owner@example.com",
            "neutral-client",
            "b" * 64,
            threading.Event(),
            confirm,
            sender=lambda _payload: "uncertain",
        )
        disabled = connection.snapshot()
        confirm.return_value = False
        result = reconnect_google(
            connection, "owner@example.com", "neutral-client", threading.Event(), confirm
        )
        self.assertEqual(result["status"], "google_recovery_cancelled")
        self.assertEqual(connection.snapshot(), disabled)
        confirm.return_value = True
        result = reconnect_google(
            connection, "owner@example.com", "neutral-client", threading.Event(), confirm
        )
        self.assertEqual(result["status"], "google_recovery_awaiting_consent")
        self.assertTrue(result["earlier_revocation_uncertain"])
        self.assertIn("invalidate", confirm.call_args.args[0])
        self.assertEqual(confirm.call_args.args[1], "reconnect")
        self.assertIsNone(connection.snapshot().payload)
        self.assertEqual(connection.history()[1][0].revocation, "uncertain")

    def test_same_endpoint_disconnect_remains_responsive_and_fences_late_output(self) -> None:
        self._endpoint_disconnect(cancel_confirmation=False)

    def test_same_endpoint_cancelled_disconnect_preserves_ordinary_operation(self) -> None:
        self._endpoint_disconnect(cancel_confirmation=True)

    def test_disconnect_after_preparation_blocks_actual_mcp_transport_delivery(self) -> None:
        self._endpoint_disconnect(cancel_confirmation=False, after_read=True)

    def _endpoint_disconnect(self, *, cancel_confirmation: bool, after_read: bool = False) -> None:
        from tedd_knowledge_graph.source_google_connection import GoogleConnection
        from tedd_knowledge_graph.source_google_control import disconnect_google
        from tedd_knowledge_graph.source_mcp import SourceMcpServer, SourceTurn

        connection = GoogleConnection(self.store, self.key)
        grant = connection.replace(connection.snapshot(), b"synthetic-grant", new_grant=True)
        entered, release = threading.Event(), threading.Event()
        published: list[bool] = []

        def operation(command: str, _argv: list[str], turn: SourceTurn) -> JsonObject:
            if command == "lookup":
                return {"status": "local_unchanged"}
            turn.provider_hold = lambda: connection.hold(grant.stamp)
            if not after_read:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("synthetic acquisition barrier timed out")
                with turn.delivery():
                    published.append(True)
            return {"evidence": "synthetic private Google wording"}

        def control(action: str, operation_id: str, turn: SourceTurn) -> JsonObject:
            self.assertEqual(action, "disconnect_google")
            return disconnect_google(
                connection,
                "owner@example.com",
                "neutral-client",
                operation_id,
                turn.cancelled,
                lambda _description, _action: not cancel_confirmation,
                sender=lambda _payload: "confirmed",
            )

        server = SourceMcpServer(operation, control=control)
        self.addCleanup(server.close)
        call_tool = server._call_tool

        def prepared_reply(request_id: str | int | None, params: JsonValue | None) -> JsonObject:
            response = call_tool(request_id, params)
            if after_read and request_id == 1:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("synthetic delivery barrier timed out")
            return response

        patcher = mock.patch.object(server, "_call_tool", side_effect=prepared_reply)
        patcher.start()
        self.addCleanup(patcher.stop)
        server.dispatch_line(
            b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}'
        )
        server.lifecycle.begin({"session_id": "thread", "turn_id": "turn"}, {"threadId": "thread"})
        replies: queue.Queue[JsonObject] = queue.Queue()

        def send(name: str, request_id: int, arguments: JsonObject) -> None:
            server.receive_line(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {
                            "name": name,
                            "arguments": arguments,
                            "_meta": {
                                "threadId": "thread",
                                "callId": str(request_id),
                                "x-codex-turn-metadata": {
                                    "thread_id": "thread",
                                    "session_id": "thread",
                                    "turn_id": "turn",
                                },
                            },
                        },
                    }
                ).encode(),
                replies.put,
            )

        try:
            send("capture_google", 1, {"argv": []})
            self.assertTrue(entered.wait(5))
            send("disconnect_google", 2, {})
            disconnected = replies.get(timeout=5)
            self.assertEqual(disconnected["id"], 2)
            self.assertIn(
                "google_disconnect_cancelled" if cancel_confirmation else "google_disconnected",
                json.dumps(disconnected),
            )
            self.assertFalse(release.is_set())
            release.set()
            evidence = replies.get(timeout=5)
            self.assertEqual(evidence["id"], 1)
            self.assertEqual(published, [True] if cancel_confirmation else [])
            self.assertEqual(
                "synthetic private Google wording" in json.dumps(evidence), cancel_confirmation
            )
            self.assertTrue(server._busy.acquire(timeout=5))
            server._busy.release()
            send("lookup", 3, {"argv": []})
            local = replies.get(timeout=5)
            self.assertIn("local_unchanged", json.dumps(local))
        finally:
            release.set()
            self.assertTrue(server._busy.acquire(timeout=5))
            server._busy.release()

    def _oauth_fixture(self) -> tuple[Path, dict[str, str]]:
        from tedd_knowledge_graph import source_google_oauth as oauth
        from tedd_knowledge_graph.source_contracts import encode
        from tedd_knowledge_graph.source_google import DRIVE_READONLY_SCOPE

        client = oauth.DesktopClient("neutral.apps.googleusercontent.com", "")
        self.key = oauth.credential_key(client, "owner@example.com")
        client_path = self.root / "client.json"
        source_files.write_bytes(
            client_path,
            encode(
                {
                    "installed": {
                        "client_id": client.client_id,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
            ),
        )
        saved = {
            "schema": "tedd-google-credential-v1",
            "client_id": client.client_id,
            "account": "owner@example.com",
            "scope": DRIVE_READONLY_SCOPE,
            "principal": "google:123",
            "refresh_token": "neutral-old",
        }
        return client_path, saved

    def test_warned_recovery_requires_fresh_consent_and_later_invalidation_stops(self) -> None:
        from tedd_knowledge_graph import source_google_oauth as oauth
        from tedd_knowledge_graph.source_contracts import SourceScope, encode
        from tedd_knowledge_graph.source_google_connection import GoogleConnection
        from tedd_knowledge_graph.source_google_control import reconnect_google

        path, saved = self._oauth_fixture()
        connection = GoogleConnection(self.store, self.key)
        original = connection.replace(connection.snapshot(), encode(saved), new_grant=True)
        prepared = connection.disable(connection.intent("b" * 64))
        connection.dispatch(prepared, lambda _payload: "uncertain")
        reconnect_google(
            connection,
            saved["account"],
            saved["client_id"],
            threading.Event(),
            lambda _description, _action: True,
        )
        scope = SourceScope("store", "generation", ("subject",), "personal_task")
        with (
            mock.patch.object(
                oauth, "_authorization_form", return_value=({"code": "neutral"}, "")
            ) as consent,
            mock.patch.object(
                oauth,
                "google_request",
                side_effect=[
                    encode(
                        {
                            "access_token": "neutral-new",
                            "token_type": "Bearer",
                            "scope": saved["scope"],
                            "expires_in": 3600,
                            "refresh_token": "neutral-new-refresh",
                        }
                    ),
                    encode(
                        {
                            "user": {
                                "me": True,
                                "emailAddress": saved["account"],
                                "permissionId": "123",
                            }
                        }
                    ),
                ],
            ),
        ):
            session = oauth.sign_in(
                path,
                account=saved["account"],
                scope=scope,
                cancelled=threading.Event(),
                credentials=self.store,
                selected_ids=frozenset({"selected"}),
            )
        consent.assert_called_once()
        self.assertGreater(connection.snapshot().stamp.generation, original.stamp.generation)
        with session.hold():
            pass
        with (
            mock.patch.object(oauth, "_authorization_form") as repeated_consent,
            mock.patch.object(
                oauth, "google_request", side_effect=SourceArchiveError("reauthentication_required")
            ),
            self.assertRaises(SourceArchiveError) as refused,
        ):
            oauth.sign_in(
                path,
                account=saved["account"],
                scope=scope,
                cancelled=threading.Event(),
                credentials=self.store,
                selected_ids=frozenset({"selected"}),
            )
        self.assertEqual(refused.exception.code, "reauthentication_required")
        repeated_consent.assert_not_called()
        self.assertFalse(connection.history()[0].enabled)
        self.assertEqual(connection.history()[1][0].revocation, "uncertain")
        self.assertNotIn(self.key, self.saved)
        with self.assertRaises(SourceArchiveError), session.hold():
            self.fail("invalidated grant still delivered")

    def _oauth_refresh_race(self, *, invalid_grant: bool) -> None:
        from tedd_knowledge_graph import source_google_oauth as oauth
        from tedd_knowledge_graph.source_contracts import SourceScope, encode
        from tedd_knowledge_graph.source_google import DRIVE_READONLY_SCOPE
        from tedd_knowledge_graph.source_google_connection import GoogleConnection

        client_path, saved = self._oauth_fixture()
        replacement_bytes = encode({**saved, "refresh_token": "neutral-replacement"})
        connection = GoogleConnection(self.store, self.key)
        connection.replace(connection.snapshot(), encode(saved), new_grant=True)

        def request(url: str, **_kwargs: object) -> bytes:
            if url == "https://oauth2.googleapis.com/token":
                intent = connection.intent("b" * 64)
                connection.disable(intent)
                connection.retire(intent)
                recovered = connection.reopen(connection.snapshot().stamp)
                connection.replace(recovered, replacement_bytes, new_grant=True)
                if invalid_grant:
                    raise SourceArchiveError("reauthentication_required")
                return encode(
                    {
                        "access_token": "neutral-access",
                        "token_type": "Bearer",
                        "scope": DRIVE_READONLY_SCOPE,
                        "expires_in": 3600,
                        "refresh_token": "neutral-late-refresh",
                    }
                )
            return encode(
                {"user": {"me": True, "emailAddress": "owner@example.com", "permissionId": "123"}}
            )

        with mock.patch.object(oauth, "google_request", side_effect=request):
            with self.assertRaises(SourceArchiveError):
                oauth.sign_in(
                    client_path,
                    account="owner@example.com",
                    scope=SourceScope("store", "generation", ("subject",), "personal_task"),
                    cancelled=threading.Event(),
                    credentials=self.store,
                    selected_ids=frozenset({"selected"}),
                )
        self.assertTrue(connection.snapshot().stamp.enabled)
        self.assertEqual(self.saved[self.key], replacement_bytes)


if __name__ == "__main__":
    unittest.main()
