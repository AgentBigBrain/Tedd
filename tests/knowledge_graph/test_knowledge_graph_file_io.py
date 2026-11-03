#!/usr/bin/env python3
"""Focused contract tests for shared durable-publication primitives."""

from __future__ import annotations

import errno
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import knowledge_graph_file_io as file_io  # noqa: E402


class DirectorySyncTests(unittest.TestCase):
    def operations(self, fsync: object) -> SimpleNamespace:
        return SimpleNamespace(
            name="posix",
            O_RDONLY=os.O_RDONLY,
            O_DIRECTORY=getattr(os, "O_DIRECTORY", 0),
            open=mock.Mock(return_value=41),
            fsync=fsync,
            close=mock.Mock(),
        )

    def test_unsupported_fsync_is_suppressed_and_descriptor_is_closed(self) -> None:
        unsupported = OSError(errno.EINVAL, "unsupported")
        operations = self.operations(mock.Mock(side_effect=unsupported))

        file_io.fsync_directory(Path("parent"), os_module=operations)

        operations.close.assert_called_once_with(41)

    def test_real_fsync_failure_propagates_and_descriptor_is_closed(self) -> None:
        failure = OSError(errno.EIO, "storage failure")
        operations = self.operations(mock.Mock(side_effect=failure))

        with self.assertRaises(OSError) as raised:
            file_io.fsync_directory(Path("parent"), os_module=operations)

        self.assertIs(raised.exception, failure)
        operations.close.assert_called_once_with(41)

    def test_close_failure_does_not_mask_real_fsync_failure(self) -> None:
        failure = OSError(errno.EIO, "storage failure")
        operations = self.operations(mock.Mock(side_effect=failure))
        operations.close.side_effect = OSError(errno.EBADF, "close failure")

        with self.assertRaises(OSError) as raised:
            file_io.fsync_directory(Path("parent"), os_module=operations)

        self.assertIs(raised.exception, failure)

    def test_close_failure_propagates_after_successful_fsync(self) -> None:
        failure = OSError(errno.EBADF, "close failure")
        operations = self.operations(mock.Mock())
        operations.close.side_effect = failure

        with self.assertRaises(OSError) as raised:
            file_io.fsync_directory(Path("parent"), os_module=operations)

        self.assertIs(raised.exception, failure)

    def test_open_failure_is_never_classified_as_unsupported(self) -> None:
        failure = OSError(errno.EINVAL, "open failed")
        operations = self.operations(mock.Mock())
        operations.open.side_effect = failure

        with self.assertRaises(OSError) as raised:
            file_io.fsync_directory(Path("parent"), os_module=operations)

        self.assertIs(raised.exception, failure)
        operations.close.assert_not_called()


class PublicationTests(unittest.TestCase):
    def test_publication_uses_one_injected_os_boundary_in_order(self) -> None:
        operations: list[tuple[str, Path, Path | None]] = []
        source = Path("staged")
        destination = Path("published")

        def stat_path(path: Path) -> SimpleNamespace:
            operations.append(("stat", path, None))
            return SimpleNamespace(st_dev=7, st_ino=11)

        os_module = SimpleNamespace(
            stat=stat_path,
            link=lambda left, right: operations.append(("link", left, right)),
            unlink=lambda path: operations.append(("unlink", path, None)),
        )

        identity = file_io.publish_staged_file(
            source,
            destination,
            overwrite=False,
            os_module=os_module,
        )

        self.assertEqual(identity, (7, 11))
        self.assertEqual(
            operations,
            [
                ("stat", source, None),
                ("link", source, destination),
                ("unlink", source, None),
            ],
        )

    def test_no_overwrite_publication_preserves_a_racing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "staged"
            destination = root / "published"
            source.write_bytes(b"candidate")
            destination.write_bytes(b"winner")

            with self.assertRaises(FileExistsError):
                file_io.publish_staged_file(source, destination, overwrite=False)

            self.assertEqual(destination.read_bytes(), b"winner")
            self.assertEqual(source.read_bytes(), b"candidate")

    def test_no_overwrite_publication_returns_staged_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "staged"
            destination = root / "published"
            source.write_bytes(b"candidate")
            expected = source.stat()

            identity = file_io.publish_staged_file(source, destination, overwrite=False)

            self.assertEqual(identity, (expected.st_dev, expected.st_ino))
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"candidate")

    def test_overwrite_publication_replaces_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "staged"
            destination = root / "published"
            source.write_bytes(b"candidate")
            destination.write_bytes(b"old")

            file_io.publish_staged_file(source, destination, overwrite=True)

            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"candidate")


class SQLiteUriTests(unittest.TestCase):
    def test_reserved_characters_round_trip_through_readonly_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "graph #100% ready.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE sample(value TEXT NOT NULL)")
                connection.execute("INSERT INTO sample(value) VALUES (?)", ("ok",))
                connection.commit()

            uri = file_io.sqlite_readonly_uri(database)
            self.assertIn("%23", uri)
            self.assertIn("%25", uri)
            with closing(sqlite3.connect(uri, uri=True)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM sample").fetchone(), ("ok",))

            # ``?`` cannot name a Windows file, but it must still be encoded before
            # SQLite interprets URI query parameters on platforms where it is valid.
            self.assertIn("%3F", file_io.sqlite_readonly_uri(Path(temporary) / "graph?.sqlite3"))


class ReplaceRetryTests(unittest.TestCase):
    class WindowsReplaceError(OSError):
        def __init__(self, winerror: int) -> None:
            super().__init__(winerror, "replace failed")
            self.winerror = winerror

    class Clock:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleeps: list[float] = []

        def monotonic(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

    def test_windows_retry_then_success(self) -> None:
        failure = self.WindowsReplaceError(32)
        replace = mock.Mock(side_effect=[failure, None])
        clock = self.Clock()

        file_io.replace_file_with_retry(
            Path("source"),
            Path("destination"),
            os_module=SimpleNamespace(name="nt", replace=replace),
            time_module=clock,
            timeout_seconds=1.0,
            poll_seconds=0.1,
            retry_winerrors={32},
        )

        self.assertEqual(replace.call_count, 2)
        self.assertEqual(clock.sleeps, [0.1])

    def test_windows_retry_stops_at_deadline(self) -> None:
        failure = self.WindowsReplaceError(32)
        replace = mock.Mock(side_effect=failure)
        clock = self.Clock()

        with self.assertRaises(OSError) as raised:
            file_io.replace_file_with_retry(
                Path("source"),
                Path("destination"),
                os_module=SimpleNamespace(name="nt", replace=replace),
                time_module=clock,
                timeout_seconds=1.0,
                poll_seconds=0.25,
                retry_winerrors={32},
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(clock.now, 1.0)
        self.assertEqual(replace.call_count, 4)

    def test_windows_non_retry_error_is_immediate(self) -> None:
        failure = self.WindowsReplaceError(5)
        replace = mock.Mock(side_effect=failure)
        clock = self.Clock()

        with self.assertRaises(OSError) as raised:
            file_io.replace_file_with_retry(
                Path("source"),
                Path("destination"),
                os_module=SimpleNamespace(name="nt", replace=replace),
                time_module=clock,
                timeout_seconds=1.0,
                poll_seconds=0.1,
                retry_winerrors={32},
            )

        self.assertIs(raised.exception, failure)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(replace.call_count, 1)

    def test_posix_replace_is_one_shot(self) -> None:
        replace = mock.Mock()
        clock = self.Clock()

        file_io.replace_file_with_retry(
            Path("source"),
            Path("destination"),
            os_module=SimpleNamespace(name="posix", replace=replace),
            time_module=clock,
            timeout_seconds=1.0,
            poll_seconds=0.1,
            retry_winerrors={32},
        )

        replace.assert_called_once_with(Path("source"), Path("destination"))
        self.assertEqual(clock.sleeps, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
