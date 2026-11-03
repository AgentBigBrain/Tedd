"""Private reader-job journal and cross-process reservation behavior."""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Protocol
from unittest import mock

import platform_support as platform
from benchmark_support import private_temporary_parent
from platform_support import set_private_path_mode
from tedd_knowledge_graph.source_contracts import SourceArchiveError
from tedd_knowledge_graph.source_reader_jobs import ReaderJobs
from tedd_knowledge_graph.source_reader_policy import ReaderRuntime

OWNER_ID = "12345678-1234-4234-8234-123456789abc"
OTHER_OWNER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
JOB_ID = "87654321-4321-4321-8321-cba987654321"
OTHER_JOB_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CONTAINER_ID = "b" * 64
IMAGE_ID = "sha256:" + "a" * 64
DAEMON_ID = "51fb0ef1-7c8d-4b79-b551-b607c2dbce27"
HOST_ROOT = Path(tempfile.gettempdir()).resolve() / "tedd-reader-jobs-host"
DOCKER = HOST_ROOT / "bin" / "docker"
SOCKET = HOST_ROOT / "run" / "docker.sock"
# Spawn/import startup is separate from the short lock deadlines under test.
WORKER_READY_TIMEOUT = 30.0


class Sender(Protocol):
    def send(self, value: str) -> None: ...

    def close(self) -> None: ...


class Receiver(Protocol):
    def recv(self) -> str: ...

    def close(self) -> None: ...


class Waitable(Protocol):
    def wait(self, timeout: float | None = None) -> bool: ...


def runtime(root: Path, *, owner_id: str = OWNER_ID) -> ReaderRuntime:
    return ReaderRuntime(
        DOCKER,
        SOCKET,
        root,
        owner_id,
        DAEMON_ID,
        IMAGE_ID,
    )


def pending_record(*, owner_id: str = OWNER_ID) -> dict[str, object]:
    return {
        "version": 1,
        "phase": "pending",
        "docker": str(DOCKER),
        "socket": str(SOCKET),
        "owner_id": owner_id,
        "daemon_id": DAEMON_ID,
        "image_id": IMAGE_ID,
        "job_id": JOB_ID,
        "name": "tedd-reader-" + JOB_ID,
    }


def _hold_pending(current: ReaderRuntime, sender: Sender, release: Waitable) -> None:
    try:
        jobs = ReaderJobs(current)
        with jobs.locked(time.monotonic() + 10.0):
            jobs.pending(JOB_ID)
            sender.send("pending")
            release.wait(10.0)
    except BaseException:
        sender.send("error")
    finally:
        sender.close()


def _interrupt_bind(
    current: ReaderRuntime,
    sender: Sender,
    release: Waitable,
    after_publication: bool,
) -> None:
    from tedd_knowledge_graph import source_files

    real_publish = source_files.publish_staged_file

    def interrupted_publish(source: Path, destination: Path, *, overwrite: bool) -> tuple[int, int]:
        if not after_publication:
            sender.send("publication-boundary")
            release.wait(10.0)
        identity = real_publish(source, destination, overwrite=overwrite)
        if after_publication:
            sender.send("publication-boundary")
            release.wait(10.0)
        return identity

    try:
        with mock.patch.object(source_files, "publish_staged_file", interrupted_publish):
            jobs = ReaderJobs(current)
            with jobs.locked(time.monotonic() + 10.0):
                jobs.bind(JOB_ID, CONTAINER_ID)
    except BaseException:
        try:
            sender.send("error")
        except (BrokenPipeError, OSError):
            pass
    finally:
        sender.close()


class ReaderJobsTests(unittest.TestCase):
    def setUp(self) -> None:
        configured_parent = private_temporary_parent()
        temporary_parent = Path(configured_parent or tempfile.gettempdir()).resolve()
        temporary = tempfile.TemporaryDirectory(prefix="tedd-reader-jobs-", dir=temporary_parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        set_private_path_mode(self.root, 0o700)
        self.runtime = runtime(self.root)

    def private_child(self, name: str) -> Path:
        root = self.root / name
        root.mkdir(mode=0o700)
        set_private_path_mode(root, 0o700)
        return root

    def test_initializes_private_content_free_idle_journal_without_source_data(self) -> None:
        ReaderJobs(self.runtime)

        self.assertEqual(
            json.loads((self.root / "reader-job.json").read_bytes()),
            {
                "version": 1,
                "phase": "idle",
                "docker": str(DOCKER),
                "socket": str(SOCKET),
                "owner_id": OWNER_ID,
                "daemon_id": DAEMON_ID,
                "image_id": IMAGE_ID,
            },
        )
        self.assertFalse(platform.mode_has_any(self.root / "reader-job.json", 0o077))
        self.assertFalse(platform.mode_has_any(self.root / "reader-job.lock", 0o077))
        journal = (self.root / "reader-job.json").read_text(encoding="utf-8")
        for forbidden in ("source", "sha256_original", "payload", "output", "document"):
            self.assertNotIn(forbidden, journal)

    def test_pending_bind_clear_are_locked_atomic_closed_state_transitions(self) -> None:
        jobs = ReaderJobs(self.runtime)
        with jobs.locked(time.monotonic() + 2.0):
            self.assertEqual(jobs.receipts(), ())
            jobs.pending(JOB_ID)
            self.assertEqual(jobs.receipts(), (pending_record(),))
            with self.assertRaises(SourceArchiveError):
                jobs.pending(OTHER_JOB_ID)

            jobs.bind(JOB_ID, CONTAINER_ID)
            self.assertEqual(
                jobs.receipts(),
                ({**pending_record(), "phase": "bound", "container_id": CONTAINER_ID},),
            )
            with self.assertRaises(SourceArchiveError):
                jobs.bind(JOB_ID, "c" * 64)
            with self.assertRaises(SourceArchiveError):
                jobs.clear(OTHER_JOB_ID)

            jobs.clear(JOB_ID)
            self.assertEqual(jobs.receipts(), ())
            with self.assertRaises(SourceArchiveError):
                jobs.clear(JOB_ID)

        reopened = ReaderJobs(self.runtime)
        with reopened.locked(time.monotonic() + 2.0):
            self.assertEqual(reopened.receipts(), ())

    def test_state_methods_require_the_owned_lock_and_do_not_clean_up_implicitly(self) -> None:
        jobs = ReaderJobs(self.runtime)
        for operation in (
            jobs.receipts,
            lambda: jobs.pending(JOB_ID),
            lambda: jobs.bind(JOB_ID, CONTAINER_ID),
            lambda: jobs.clear(JOB_ID),
        ):
            with self.subTest(operation=operation), self.assertRaises(SourceArchiveError):
                operation()

        with jobs.locked(time.monotonic() + 2.0):
            jobs.pending(JOB_ID)
        replacement = ReaderJobs(self.runtime)
        with replacement.locked(time.monotonic() + 2.0):
            self.assertEqual(replacement.receipts(), (pending_record(),))

    def test_failed_nested_waiter_cannot_clear_the_active_lock_ownership(self) -> None:
        jobs = ReaderJobs(self.runtime)
        with jobs.locked(time.monotonic() + 2.0):
            with self.assertRaisesRegex(SourceArchiveError, "source_unavailable"):
                with jobs.locked(time.monotonic()):
                    self.fail("nested waiter entered a non-reentrant reservation")
            self.assertEqual(jobs.receipts(), ())

    def test_refuses_unknown_malformed_oversized_or_runtime_mismatched_journal(self) -> None:
        corruptions: tuple[bytes, ...] = (
            b'{"version":1,"phase":',
            json.dumps({**pending_record(), "unknown": "field"}).encode("utf-8"),
            json.dumps({**pending_record(), "owner_id": OTHER_OWNER_ID}).encode("utf-8"),
            json.dumps({**pending_record(), "name": "tedd-reader-wrong"}).encode("utf-8"),
            json.dumps({**pending_record(), "container_id": CONTAINER_ID}).encode("utf-8"),
            b" " * 8193,
        )
        for index, payload in enumerate(corruptions):
            with self.subTest(size=len(payload)):
                current_root = self.private_child(f"corrupt-{index}")
                current = runtime(current_root)
                jobs = ReaderJobs(current)
                journal = current_root / "reader-job.json"
                journal.write_bytes(payload)
                set_private_path_mode(journal, 0o600)
                with jobs.locked(time.monotonic() + 2.0), self.assertRaises(SourceArchiveError):
                    jobs.receipts()

    def test_refuses_journal_links_and_replacement_while_lock_is_held(self) -> None:
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                jobs = ReaderJobs(self.runtime)
                journal = self.root / "reader-job.json"
                original = self.root / "original.json"
                os.replace(journal, original)
                if kind == "symlink":
                    journal.symlink_to(original)
                else:
                    os.link(original, journal)
                with self.assertRaises(SourceArchiveError):
                    with jobs.locked(time.monotonic() + 2.0):
                        jobs.receipts()
                journal.unlink()
                original.unlink()

                # A missing journal beside an existing initialization lock is
                # an unresolved state, not permission to synthesize idle.
                with self.assertRaises(SourceArchiveError):
                    ReaderJobs(self.runtime)
                (self.root / "reader-job.lock").unlink()

        jobs = ReaderJobs(self.runtime)
        with jobs.locked(time.monotonic() + 2.0):
            self.assertEqual(jobs.receipts(), ())
            replacement = self.root / "replacement.json"
            replacement.write_bytes((self.root / "reader-job.json").read_bytes())
            set_private_path_mode(replacement, 0o600)
            os.replace(replacement, self.root / "reader-job.json")
            with self.assertRaises(SourceArchiveError):
                jobs.pending(JOB_ID)

    def test_refuses_lock_replacement_and_unavailable_lock_primitive(self) -> None:
        jobs = ReaderJobs(self.runtime)
        if os.name == "nt":
            with self.assertRaisesRegex(SourceArchiveError, "source_unavailable"):
                with jobs.locked(time.monotonic() + 2.0):
                    replacement = self.root / "replacement.lock"
                    replacement.write_bytes(b"")
                    set_private_path_mode(replacement, 0o600)
                    os.replace(replacement, self.root / "reader-job.lock")
                    with self.assertRaises(SourceArchiveError):
                        jobs.receipts()
        else:
            with jobs.locked(time.monotonic() + 2.0):
                replacement = self.root / "replacement.lock"
                replacement.write_bytes(b"")
                set_private_path_mode(replacement, 0o600)
                os.replace(replacement, self.root / "reader-job.lock")
                with self.assertRaises(SourceArchiveError):
                    jobs.receipts()

        fresh_root = self.private_child("fresh")
        unavailable = ReaderJobs(runtime(fresh_root))
        with mock.patch(
            "tedd_knowledge_graph.source_reader_jobs.try_file_lock",
            side_effect=NotImplementedError,
        ):
            with self.assertRaisesRegex(SourceArchiveError, "source_unavailable"):
                with unavailable.locked(time.monotonic() + 1.0):
                    self.fail("unavailable lock primitive admitted a caller")

        unlock_root = self.private_child("unlock")
        unavailable_unlock = ReaderJobs(runtime(unlock_root))
        with mock.patch(
            "tedd_knowledge_graph.source_reader_jobs.unlock_file",
            side_effect=NotImplementedError,
        ):
            with self.assertRaisesRegex(SourceArchiveError, "source_unavailable"):
                with unavailable_unlock.locked(time.monotonic() + 1.0):
                    pass

    def test_requires_an_existing_private_state_root_and_never_creates_it(self) -> None:
        missing = self.root / "missing"
        with self.assertRaises(SourceArchiveError):
            ReaderJobs(runtime(missing))
        self.assertFalse(missing.exists())

        public = self.root / "public"
        public.mkdir(mode=0o700)
        if os.name == "nt":
            platform._set_windows_dacl_from_sddl(
                public,
                f"D:P(A;;FA;;;{platform._current_user_sid()})(A;;FA;;;WD)",
            )
        else:
            public.chmod(0o755)
        with self.assertRaises(SourceArchiveError):
            ReaderJobs(runtime(public))
        self.assertEqual(list(public.iterdir()), [])

        link = self.root / "linked"
        link.symlink_to(public, target_is_directory=True)
        with self.assertRaises(SourceArchiveError):
            ReaderJobs(runtime(link))

        with self.assertRaises(SourceArchiveError):
            ReaderJobs(runtime(Path.home()))

        repository = self.root / "repository"
        repository.mkdir(mode=0o700)
        (repository / ".git").mkdir(mode=0o700)
        repository_state = repository / "state"
        repository_state.mkdir(mode=0o700)
        with self.assertRaises(SourceArchiveError):
            ReaderJobs(runtime(repository_state))
        self.assertEqual(list(repository_state.iterdir()), [])

        cloud_state = self.root / "Library" / "CloudStorage" / "state"
        cloud_state.mkdir(mode=0o700, parents=True)
        with self.assertRaises(SourceArchiveError):
            ReaderJobs(runtime(cloud_state))
        self.assertEqual(list(cloud_state.iterdir()), [])

    def test_refuses_source_archive_root_at_initialization_and_use_time(self) -> None:
        archive_root = self.private_child("archive-root")
        marker = archive_root / "archive.json"
        marker.write_bytes(b"{}")
        set_private_path_mode(marker, 0o600)

        with self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"):
            ReaderJobs(runtime(archive_root))
        self.assertEqual(list(archive_root.iterdir()), [marker])

        later_root = self.private_child("later-archive-root")
        jobs = ReaderJobs(runtime(later_root))
        journal_before = (later_root / "reader-job.json").read_bytes()
        later_marker = later_root / "archive.json"
        later_marker.write_bytes(b"{}")
        set_private_path_mode(later_marker, 0o600)

        with self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"):
            with jobs.locked(time.monotonic() + 2.0):
                self.fail("source archive root admitted at use time")
        self.assertEqual((later_root / "reader-job.json").read_bytes(), journal_before)

        active_root = self.private_child("active-archive-root")
        active_jobs = ReaderJobs(runtime(active_root))
        with active_jobs.locked(time.monotonic() + 2.0):
            active_marker = active_root / "archive.json"
            active_marker.write_bytes(b"{}")
            set_private_path_mode(active_marker, 0o600)
            with self.assertRaisesRegex(SourceArchiveError, "privacy_rejected"):
                active_jobs.receipts()

    def test_waiting_for_process_lock_is_deadline_and_cancellation_bounded(self) -> None:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        release = context.Event()
        process = context.Process(target=_hold_pending, args=(self.runtime, sender, release))
        process_started = False
        try:
            process.start()
            process_started = True
            sender.close()
            self.assertTrue(
                receiver.poll(WORKER_READY_TIMEOUT), "lock owner did not publish readiness"
            )
            self.assertEqual(receiver.recv(), "pending")

            jobs = ReaderJobs(self.runtime)
            started = time.monotonic()
            with self.assertRaisesRegex(SourceArchiveError, "source_unavailable"):
                with jobs.locked(started + 0.15):
                    self.fail("second process entered the owned lock")
            self.assertLess(time.monotonic() - started, 1.0)

            cancelled = threading.Event()
            timer = threading.Timer(0.05, cancelled.set)
            timer.start()
            self.addCleanup(timer.cancel)
            started = time.monotonic()
            with self.assertRaisesRegex(SourceArchiveError, "source_unavailable"):
                with jobs.locked(started + 2.0, cancelled):
                    self.fail("cancelled waiter entered the owned lock")
            self.assertLess(time.monotonic() - started, 1.0)

            release.set()
            process.join(5.0)
            self.assertEqual(process.exitcode, 0)
        finally:
            sender.close()
            if process_started:
                if process.is_alive():
                    process.kill()
                process.join(5.0)
            receiver.close()

    def test_killing_lock_owner_releases_reservation_but_preserves_pending_receipt(self) -> None:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        release = context.Event()
        process = context.Process(target=_hold_pending, args=(self.runtime, sender, release))
        process_started = False
        try:
            process.start()
            process_started = True
            sender.close()
            self.assertTrue(
                receiver.poll(WORKER_READY_TIMEOUT), "lock owner did not publish readiness"
            )
            self.assertEqual(receiver.recv(), "pending")

            process.kill()
            process.join(5.0)
            self.assertIsNotNone(process.exitcode)
        finally:
            sender.close()
            if process_started:
                if process.is_alive():
                    process.kill()
                process.join(5.0)
            receiver.close()

        jobs = ReaderJobs(self.runtime)
        with jobs.locked(time.monotonic() + 2.0):
            self.assertEqual(jobs.receipts(), (pending_record(),))

    def test_interrupted_bind_keeps_exactly_one_valid_old_or_new_record(self) -> None:
        context = multiprocessing.get_context("spawn")
        for after_publication, expected_phase in ((False, "pending"), (True, "bound")):
            with self.subTest(after_publication=after_publication):
                current_root = self.private_child("after" if after_publication else "before")
                current = runtime(current_root)
                jobs = ReaderJobs(current)
                with jobs.locked(time.monotonic() + 2.0):
                    jobs.pending(JOB_ID)

                receiver, sender = context.Pipe(duplex=False)
                release = context.Event()
                process = context.Process(
                    target=_interrupt_bind,
                    args=(current, sender, release, after_publication),
                )
                process_started = False
                try:
                    process.start()
                    process_started = True
                    sender.close()
                    self.assertTrue(
                        receiver.poll(WORKER_READY_TIMEOUT), "bind worker did not publish readiness"
                    )
                    self.assertEqual(receiver.recv(), "publication-boundary")
                    process.kill()
                    process.join(5.0)
                finally:
                    sender.close()
                    if process_started:
                        if process.is_alive():
                            process.kill()
                        process.join(5.0)
                    receiver.close()

                recovered = ReaderJobs(current)
                with recovered.locked(time.monotonic() + 2.0):
                    receipts = recovered.receipts()
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0]["phase"], expected_phase)
                self.assertEqual(receipts[0]["job_id"], JOB_ID)
                if expected_phase == "bound":
                    self.assertEqual(receipts[0]["container_id"], CONTAINER_ID)
                else:
                    self.assertNotIn("container_id", receipts[0])


if __name__ == "__main__":
    unittest.main()
