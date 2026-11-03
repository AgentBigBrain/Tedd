#!/usr/bin/env python3
"""Snapshot and publication fault tests for the v4 storage adapter."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
import time
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

import knowledge_graph_v4 as v4  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402

from tests.support.graph_cases import (  # noqa: E402
    PLAN_KEY,
    V4TestCase,
    applied,
    change,
)


class SnapshotConsistencyTests(V4TestCase):
    def test_create_store_never_replaces_a_racing_winner(self) -> None:
        winner_path = self.root / "winner.sqlite3"
        with self.create_store(winner_path):
            pass
        winner_bytes = winner_path.read_bytes()
        real_set_private_path_mode = v4.set_private_path_mode
        winner_published = False

        def publish_winner_before_loser(source: Path, mode: int) -> None:
            nonlocal winner_published
            real_set_private_path_mode(source, mode)
            if source.suffix == ".create" and not winner_published:
                self.store_path.write_bytes(winner_bytes)
                real_set_private_path_mode(self.store_path, 0o600)
                winner_published = True

        losing_store = None
        try:
            with mock.patch.object(
                v4,
                "set_private_path_mode",
                side_effect=publish_winner_before_loser,
            ):
                with self.assertRaises(FileExistsError):
                    losing_store = self.create_store()
        finally:
            if losing_store is not None:
                losing_store.close()

        self.assertTrue(winner_published)
        self.assertEqual(self.store_path.read_bytes(), winner_bytes)
        self.assertEqual(v4.detect_store(self.store_path), "sqlite_v4")

    def test_export_uses_one_explicit_read_snapshot(self) -> None:
        with self.create_store() as reader:
            applied(reader, [change("entity_before")], fingerprint="export-before")
            with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as concurrent_writer:
                original_state = reader.state
                injected = False

                def state_after_concurrent_commit() -> dict[str, object]:
                    nonlocal injected
                    if not injected:
                        injected = True
                        applied(
                            concurrent_writer,
                            [change("entity_after")],
                            fingerprint="export-after",
                        )
                    return cast(dict[str, object], original_state())

                with mock.patch.object(reader, "state", side_effect=state_after_concurrent_commit):
                    exported = reader.export_current_graph(include_journal=True)

            self.assertTrue(injected)
            self.assertFalse(reader.connection.in_transaction)
            self.assertEqual(exported["next_watermark"], 2)
            self.assertEqual({item["id"] for item in exported["entities"]}, {"entity_before"})
            self.assertEqual([item["watermark"] for item in exported["journal"]], [1])
            self.assertEqual(reader.state()["watermark"], 2)

    def test_backup_manifest_describes_the_backup_snapshot_not_a_later_tip(self) -> None:
        backup_path = self.root / "snapshot.sqlite3"
        with self.create_store() as source:
            applied(source, [change("entity_before")], fingerprint="backup-before")
            with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as concurrent_writer:
                real_publish = v4._publish_staged_file
                injected = False

                def publish_then_commit(
                    staged: Path, destination: Path, *, overwrite: bool
                ) -> tuple[int, int]:
                    nonlocal injected
                    identity = real_publish(staged, destination, overwrite=overwrite)
                    if destination == backup_path and not injected:
                        injected = True
                        applied(
                            concurrent_writer,
                            [change("entity_after")],
                            fingerprint="backup-after",
                        )
                    return cast(tuple[int, int], identity)

                with mock.patch.object(v4, "_publish_staged_file", side_effect=publish_then_commit):
                    manifest = source.backup(backup_path)

            self.assertTrue(injected)
            self.assertEqual(source.state()["watermark"], 2)
            self.assertEqual(manifest["watermark"], 1)

        with v4.open_store(backup_path, readonly=True, plan_hmac_key=PLAN_KEY) as restored:
            restored_state = restored.state()
            self.assertEqual(restored_state["watermark"], manifest["watermark"])
            self.assertEqual(restored_state["journal_head_hash"], manifest["journal_head_hash"])
            with self.assertRaises(KeyError):
                restored.get_record("entity_after")

    def test_export_and_backup_reject_an_existing_source_transaction(self) -> None:
        backup_path = self.root / "transaction.sqlite3"
        with self.create_store() as store:
            store.connection.execute("BEGIN")
            try:
                with self.assertRaisesRegex(v4.V4Error, "active transaction"):
                    store.export_current_graph()
                with self.assertRaisesRegex(v4.V4Error, "active transaction"):
                    store.backup(backup_path)
                self.assertFalse(backup_path.exists())
            finally:
                store.connection.rollback()


class BackupPublicationTests(V4TestCase):
    def test_final_verification_requests_a_nonblocking_descriptor(self) -> None:
        published = self.root / "published.bin"
        published.write_bytes(b"verified")
        set_private_path_mode(published, 0o600)
        published_stat = published.stat()
        real_open = os.open
        sentinel_nonblocking = 1 << 29

        def inspect_then_open(path: Path, flags: int) -> int:
            self.assertTrue(flags & sentinel_nonblocking)
            return real_open(path, flags & ~sentinel_nonblocking)

        with (
            mock.patch.object(v4.os, "O_NONBLOCK", sentinel_nonblocking, create=True),
            mock.patch.object(v4.os, "open", side_effect=inspect_then_open),
        ):
            descriptor, _ = v4._published_file_descriptor(
                published, (published_stat.st_dev, published_stat.st_ino)
            )
        os.close(descriptor)

    def test_overwrite_is_rejected_without_changing_an_existing_pair(self) -> None:
        backup_path = self.root / "existing.sqlite3"
        manifest_path = backup_path.with_name(f"{backup_path.name}.manifest.json")
        with self.create_store() as store:
            applied(store, [change("entity_old")], fingerprint="backup-old")
            store.backup(backup_path)
            old_backup = backup_path.read_bytes()
            old_manifest = manifest_path.read_bytes()
            applied(store, [change("entity_new")], fingerprint="backup-new")

            with self.assertRaisesRegex(ValueError, "overwrite is not supported"):
                store.backup(backup_path, overwrite=True)

        self.assertEqual(backup_path.read_bytes(), old_backup)
        self.assertEqual(manifest_path.read_bytes(), old_manifest)

    def test_failed_backup_verification_never_publishes_completion_manifest(self) -> None:
        backup_path = self.root / "corrupted.sqlite3"
        manifest_path = backup_path.with_name(f"{backup_path.name}.manifest.json")
        real_verify = v4._sha256_verified_published_file

        def corrupt_then_verify(path: Path, identity: tuple[int, int]) -> tuple[str, int]:
            path.write_bytes(b"corrupted after publication\n")
            return cast(tuple[str, int], real_verify(path, identity))

        with self.create_store() as store:
            applied(store, [change("entity_backup")], fingerprint="backup-corruption")
            with mock.patch.object(
                v4,
                "_sha256_verified_published_file",
                side_effect=corrupt_then_verify,
            ):
                with self.assertRaisesRegex(v4.ValidationError, "does not match its manifest"):
                    store.backup(backup_path)

        self.assertTrue(backup_path.is_file())
        self.assertFalse(manifest_path.exists())

    def test_manifest_directory_fsync_failure_is_reported_without_unsafe_delete(self) -> None:
        backup_path = self.root / "fsync-failure.sqlite3"
        manifest_path = backup_path.with_name(f"{backup_path.name}.manifest.json")
        real_fsync_parent = v4._fsync_parent
        call_count = 0

        def fail_second_parent_sync(path: Path) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError(errno.EIO, "injected manifest directory sync failure")
            real_fsync_parent(path)

        with self.create_store() as store:
            applied(store, [change("entity_backup")], fingerprint="backup-fsync")
            with mock.patch.object(v4, "_fsync_parent", side_effect=fail_second_parent_sync):
                with self.assertRaises(OSError):
                    store.backup(backup_path)

        self.assertTrue(backup_path.is_file())
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(hashlib.sha256(backup_path.read_bytes()).hexdigest(), manifest["sha256"])


class PlanKeyPublicationTests(V4TestCase):
    def test_plan_key_read_requests_a_nonblocking_descriptor(self) -> None:
        key_path = self.store_path.with_name(f"{self.store_path.name}.plan.key")
        key_path.write_bytes((v4._b64encode(PLAN_KEY) + "\n").encode("ascii"))
        set_private_path_mode(key_path, 0o600)
        real_open = os.open
        sentinel_nonblocking = 1 << 29

        def inspect_then_open(path: Path, flags: int) -> int:
            self.assertTrue(flags & sentinel_nonblocking)
            return real_open(path, flags & ~sentinel_nonblocking)

        with (
            mock.patch.object(v4.os, "O_NONBLOCK", sentinel_nonblocking, create=True),
            mock.patch.object(v4.os, "open", side_effect=inspect_then_open),
        ):
            self.assertEqual(v4._read_private_plan_key(key_path), PLAN_KEY)

    def test_losing_creator_converges_after_two_link_publication_window(self) -> None:
        key_path = self.store_path.with_name(f"{self.store_path.name}.plan.key")
        winner = bytes.fromhex("33" * 32)
        publisher_ready = threading.Event()
        loser_at_publish = threading.Event()
        winner_linked = threading.Event()
        release_publisher = threading.Event()
        publisher_errors: list[BaseException] = []
        real_link = os.link

        def coordinated_publish(
            source: Path, destination: Path, *, overwrite: bool
        ) -> tuple[int, int]:
            self.assertFalse(overwrite)
            source_stat = source.stat()
            if threading.current_thread().name == "plan-key-publisher":
                publisher_ready.set()
                if not loser_at_publish.wait(timeout=5):
                    raise AssertionError("losing creator did not reach publication")
                real_link(source, destination)
                winner_linked.set()
                if not release_publisher.wait(timeout=5):
                    raise AssertionError("loser did not release the paused key publisher")
                source.unlink()
                return source_stat.st_dev, source_stat.st_ino
            loser_at_publish.set()
            if not winner_linked.wait(timeout=5):
                raise AssertionError("winner did not publish its first link")
            real_link(source, destination)
            raise AssertionError("losing publication unexpectedly succeeded")

        def publish() -> None:
            try:
                v4._atomic_bytes(
                    key_path,
                    (v4._b64encode(winner) + "\n").encode("ascii"),
                    overwrite=False,
                )
            except BaseException as error:  # pragma: no cover - asserted below
                publisher_errors.append(error)

        real_sleep = time.sleep

        def release_during_retry(_: float) -> None:
            release_publisher.set()
            real_sleep(0.01)

        with mock.patch.object(v4, "_publish_staged_file", side_effect=coordinated_publish):
            publisher = threading.Thread(target=publish, name="plan-key-publisher")
            publisher.start()
            self.assertTrue(publisher_ready.wait(timeout=5))
            with mock.patch.object(v4.time, "sleep", side_effect=release_during_retry):
                loaded = v4._load_or_create_plan_key(self.store_path, None, create=True)
            publisher.join(timeout=5)

        self.assertFalse(publisher.is_alive())
        self.assertEqual(publisher_errors, [])
        self.assertEqual(loaded, winner)
        self.assertEqual(key_path.stat().st_nlink, 1)

    def test_existing_reader_waits_out_two_link_publication_window(self) -> None:
        key_path = self.store_path.with_name(f"{self.store_path.name}.plan.key")
        staged_path = self.root / ".staged-plan-key"
        staged_path.write_bytes((v4._b64encode(PLAN_KEY) + "\n").encode("ascii"))
        set_private_path_mode(staged_path, 0o600)
        try:
            os.link(staged_path, key_path)
        except OSError as error:
            self.skipTest(f"hard links unavailable: {error}")
        self.assertEqual(key_path.stat().st_nlink, 2)

        def complete_publication(_: float) -> None:
            staged_path.unlink()

        with mock.patch.object(v4.time, "sleep", side_effect=complete_publication):
            loaded = v4._load_or_create_plan_key(self.store_path, None, create=False)

        self.assertEqual(loaded, PLAN_KEY)
        self.assertEqual(key_path.stat().st_nlink, 1)

    def test_single_link_malformed_key_preserves_its_real_diagnostic(self) -> None:
        key_path = self.store_path.with_name(f"{self.store_path.name}.plan.key")
        key_path.write_bytes(b"\xff\n")
        set_private_path_mode(key_path, 0o600)

        with mock.patch.object(v4.time, "sleep") as retry_sleep:
            with self.assertRaisesRegex(v4.PathSafetyError, "not ASCII"):
                v4._read_private_plan_key_after_publication(key_path)
        retry_sleep.assert_not_called()

    def test_persistent_hard_link_still_fails_closed_after_bounded_retry(self) -> None:
        key_path = self.store_path.with_name(f"{self.store_path.name}.plan.key")
        other_link = self.root / "persisted-key-link"
        key_path.write_bytes((v4._b64encode(PLAN_KEY) + "\n").encode("ascii"))
        set_private_path_mode(key_path, 0o600)
        try:
            os.link(key_path, other_link)
        except OSError as error:
            self.skipTest(f"hard links unavailable: {error}")

        with (
            mock.patch.object(v4, "PLAN_KEY_LINK_RETRY_ATTEMPTS", 3),
            mock.patch.object(v4, "PLAN_KEY_LINK_RETRY_DELAY_SECONDS", 0),
        ):
            with self.assertRaisesRegex(v4.PathSafetyError, "bounded publication retry"):
                v4._read_private_plan_key_after_publication(key_path)


if __name__ == "__main__":
    unittest.main()
