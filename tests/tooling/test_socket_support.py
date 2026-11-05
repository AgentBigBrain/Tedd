"""Capability checks must skip unavailable sockets without hiding test defects."""

from __future__ import annotations

import errno
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support.sockets import require_unix_socket


class UnixSocketCapabilityTests(unittest.TestCase):
    def test_missing_socket_family_skips_without_creating_a_socket(self) -> None:
        with (
            mock.patch.object(socket, "AF_UNIX", None, create=True),
            mock.patch.object(socket, "socket", autospec=True) as constructor,
            self.assertRaisesRegex(unittest.SkipTest, "Unix-domain sockets"),
        ):
            require_unix_socket(Path("unused"))
        constructor.assert_not_called()

    def test_creation_and_bind_capability_refusals_skip_with_a_reason(self) -> None:
        for code in (
            errno.EPERM,
            errno.EACCES,
            errno.EAFNOSUPPORT,
            errno.EPROTONOSUPPORT,
            errno.ENOSYS,
            errno.EOPNOTSUPP,
        ):
            for operation in ("create", "bind"):
                with self.subTest(code=code, operation=operation):
                    listener = mock.Mock(spec=socket.socket)
                    error = OSError(code, "synthetic capability refusal")
                    if operation == "bind":
                        listener.bind.side_effect = error
                    with (
                        mock.patch.object(socket, "AF_UNIX", 1, create=True),
                        mock.patch.object(
                            socket,
                            "socket",
                            side_effect=error if operation == "create" else None,
                            return_value=listener,
                        ),
                        self.assertRaisesRegex(unittest.SkipTest, "synthetic capability refusal"),
                    ):
                        require_unix_socket(Path("unused"))
                    self.assertEqual(listener.close.call_count, int(operation == "bind"))

    def test_unexpected_socket_errors_remain_failures(self) -> None:
        for code in (errno.EIO, errno.EMFILE, errno.ENAMETOOLONG, errno.EADDRINUSE):
            for operation in ("create", "bind"):
                with self.subTest(code=code, operation=operation):
                    listener = mock.Mock(spec=socket.socket)
                    error = OSError(code, "synthetic unexpected failure")
                    if operation == "bind":
                        listener.bind.side_effect = error
                    with (
                        mock.patch.object(socket, "AF_UNIX", 1, create=True),
                        mock.patch.object(
                            socket,
                            "socket",
                            side_effect=error if operation == "create" else None,
                            return_value=listener,
                        ),
                        self.assertRaises(OSError) as raised,
                    ):
                        require_unix_socket(Path("unused"))
                    self.assertIs(raised.exception, error)
                    self.assertEqual(listener.close.call_count, int(operation == "bind"))

    def test_cleanup_failure_is_not_classified_as_missing_capability(self) -> None:
        listener = mock.Mock(spec=socket.socket)
        error = OSError(errno.EACCES, "synthetic cleanup failure")
        with (
            mock.patch.object(socket, "AF_UNIX", 1, create=True),
            mock.patch.object(socket, "socket", return_value=listener),
            mock.patch.object(Path, "unlink", side_effect=error),
            self.assertRaises(OSError) as raised,
        ):
            require_unix_socket(Path("unused"))
        self.assertIs(raised.exception, error)
        listener.close.assert_called_once_with()

    @unittest.skipIf(os.name == "nt", "POSIX reader socket fixture")
    def test_real_probe_closes_and_removes_only_its_bound_socket(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".us-", dir=Path.home()) as raw:
            path = Path(raw) / "s"
            require_unix_socket(path)
            self.assertFalse(path.exists())
            # A second bind proves the first probe released the address.
            require_unix_socket(path)
            self.assertFalse(path.exists())

    @unittest.skipIf(os.name == "nt", "POSIX reader socket fixture")
    def test_bind_collision_preserves_the_existing_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".us-", dir=Path.home()) as raw:
            path = Path(raw) / "s"
            require_unix_socket(path)
            path.write_bytes(b"owner file")
            with self.assertRaises(OSError):
                require_unix_socket(path)
            self.assertEqual(path.read_bytes(), b"owner file")
