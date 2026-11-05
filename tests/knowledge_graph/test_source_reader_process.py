"""Real-pipe tests for the bounded reader subprocess transport."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tedd_knowledge_graph.source_contracts import SourceArchiveError
from tedd_knowledge_graph.source_reader_process import _start_bounded, run_bounded

from tests.support.sockets import require_unix_socket


class _Pipe:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def fileno(self) -> int:
        return 7


class _Process:
    def __init__(self) -> None:
        self.stdin = _Pipe()
        self.stdout = _Pipe()
        self.returncode: int | None = None
        self.terminated = False
        self.reaped = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        del timeout
        self.reaped = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class BoundedProcessTests(unittest.TestCase):
    def child(self, body: str, *arguments: str) -> tuple[str, ...]:
        return (sys.executable, "-I", "-B", "-c", body, *arguments)

    @unittest.skipIf(os.name == "nt", "Child umask is a POSIX creation primitive")
    def test_private_child_mask_restricts_generated_files_not_parent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            before = root / "parent-before"
            before.write_bytes(b"")
            result = run_bounded(
                self.child(
                    "import sys; from pathlib import Path; "
                    "r=Path(sys.argv[1]); (r/'generated').mkdir(mode=0o755); "
                    "(r/'generated'/'file').write_bytes(b'private'); "
                    "print('done')",
                    raw,
                ),
                b"",
                deadline=time.monotonic() + 10,
                output_limit=16,
                umask=0o077,
            )
            after = root / "parent-after"
            after.write_bytes(b"")
            self.assertEqual((result.returncode, result.stdout), (0, b"done\n"))
            self.assertEqual((root / "generated").stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "generated/file").stat().st_mode & 0o777, 0o600)
            self.assertEqual(after.stat().st_mode & 0o777, before.stat().st_mode & 0o777)

    @unittest.skipIf(os.name == "nt", "Child umask is a POSIX creation primitive")
    def test_private_child_mask_restricts_socket_permissions(self) -> None:
        with tempfile.TemporaryDirectory(prefix=".us-", dir=Path.home()) as raw:
            root = Path(raw)
            require_unix_socket(root / "probe")
            result = run_bounded(
                self.child(
                    "import os,socket,sys; os.chdir(sys.argv[1]); "
                    "s=socket.socket(socket.AF_UNIX); s.bind('socket'); "
                    "s.close(); print('done')",
                    raw,
                ),
                b"",
                deadline=time.monotonic() + 10,
                output_limit=16,
                umask=0o077,
            )
            self.assertEqual((result.returncode, result.stdout), (0, b"done\n"))
            self.assertEqual((root / "socket").stat().st_mode & 0o777, 0o700)

    def test_invalid_child_mask_refuses_before_process_creation(self) -> None:
        with mock.patch(
            "tedd_knowledge_graph.source_reader_process.subprocess.Popen", autospec=True
        ) as child:
            for value in (True, -2, 0o1000):
                with self.subTest(mask=value), self.assertRaises(SourceArchiveError):
                    run_bounded(
                        self.child("pass"),
                        b"",
                        deadline=time.monotonic() + 5,
                        output_limit=0,
                        umask=value,
                    )
        child.assert_not_called()

    def test_concurrently_pumps_input_and_output_then_closes_stdin(self) -> None:
        payload = b"p" * (2 * 1024**2)
        prefix = b"o" * (2 * 1024**2)
        command = self.child(
            "import hashlib,sys; "
            "sys.stdout.buffer.write(b'o'*(2*1024**2)); sys.stdout.buffer.flush(); "
            "data=sys.stdin.buffer.read(); "
            "sys.stdout.buffer.write(hashlib.sha256(data).hexdigest().encode())"
        )

        result = run_bounded(
            command,
            payload,
            deadline=time.monotonic() + 10,
            output_limit=len(prefix) + 64,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, prefix + hashlib.sha256(payload).hexdigest().encode())

    def test_child_closes_stdin_while_writer_has_remaining_payload(self) -> None:
        with mock.patch("threading.excepthook") as thread_errors:
            process = _start_bounded(
                self.child(
                    "import os; first=os.read(0,1); os.close(0); "
                    "os.write(1, first+b':closed'); os._exit(0)"
                ),
                b"p" * (32 * 1024**2),
                output_limit=16,
            )
            try:
                result = process.wait(time.monotonic() + 10)
                self.assertEqual((result.returncode, result.stdout), (0, b"p:closed"))
                self.assertFalse(process._writer.is_alive())
                self.assertFalse(process._reader.is_alive())
                assert process._process.stdin is not None
                assert process._process.stdout is not None
                self.assertTrue(process._process.stdin.closed)
                self.assertTrue(process._process.stdout.closed)
                self.assertEqual(process._process.poll(), 0)
            finally:
                process.terminate_and_reap(time.monotonic() + 1)
            thread_errors.assert_not_called()

    def test_discards_overflow_but_keeps_draining_until_child_exits(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / "drained"
            command = self.child(
                "import pathlib,sys; "
                "sys.stdout.buffer.write(b'x'*(2*1024**2)); sys.stdout.buffer.flush(); "
                "pathlib.Path(sys.argv[1]).write_text('done')",
                str(marker),
            )

            with self.assertRaises(SourceArchiveError) as raised:
                run_bounded(
                    command,
                    b"",
                    deadline=time.monotonic() + 10,
                    output_limit=1024,
                )

            self.assertEqual(raised.exception.code, "limit_exceeded")
            self.assertEqual(str(raised.exception), "limit_exceeded")
            self.assertEqual(marker.read_text(), "done")

    def test_stderr_is_discarded_without_blocking_stdout(self) -> None:
        result = run_bounded(
            self.child(
                "import sys; "
                "sys.stderr.buffer.write(b'e'*(2*1024**2)); "
                "sys.stdout.buffer.write(b'ok')"
            ),
            b"",
            deadline=time.monotonic() + 10,
            output_limit=2,
        )

        self.assertEqual(result.stdout, b"ok")

    def test_cancellation_terminates_and_reaps_without_retaining_output(self) -> None:
        cancelled = threading.Event()
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        try:
            with self.assertRaises(SourceArchiveError) as raised:
                run_bounded(
                    self.child(
                        "import sys,time; sys.stdout.buffer.write(b'secret'); "
                        "sys.stdout.buffer.flush(); time.sleep(30)"
                    ),
                    b"",
                    deadline=time.monotonic() + 10,
                    output_limit=1024,
                    cancelled=cancelled,
                )
        finally:
            timer.cancel()

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertNotIn("secret", str(raised.exception))

    def test_absolute_deadline_terminates_and_reaps(self) -> None:
        started = time.monotonic()
        with self.assertRaises(SourceArchiveError) as raised:
            run_bounded(
                self.child("import time; time.sleep(30)"),
                b"",
                deadline=started + 0.1,
                output_limit=1024,
            )

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertLess(time.monotonic() - started, 5)

    def test_rejects_relative_executable_and_default_environment_is_closed(self) -> None:
        with self.assertRaises(SourceArchiveError):
            run_bounded(
                ("python", "-c", "pass"),
                b"",
                deadline=time.monotonic() + 1,
                output_limit=1024,
            )
        marker = "TEDD_READER_AMBIENT_TEST"
        os.environ[marker] = "must-not-cross"
        try:
            result = run_bounded(
                self.child(
                    "import os,sys; "
                    "sys.stdout.write(os.environ.get('TEDD_READER_AMBIENT_TEST','absent')); "
                    "sys.stdout.write('|' + ('present' if os.environ.get('SystemRoot') "
                    "else 'absent'))"
                ),
                b"",
                deadline=time.monotonic() + 5,
                output_limit=64,
            )
        finally:
            os.environ.pop(marker, None)
        expected_system_root = b"present" if os.name == "nt" else b"absent"
        self.assertEqual(result.stdout, b"absent|" + expected_system_root)

    def test_explicit_environment_adds_no_ambient_values_and_runs_on_windows(self) -> None:
        marker = "TEDD_READER_AMBIENT_TEST"
        os.environ[marker] = "must-not-cross"
        try:
            result = run_bounded(
                self.child(
                    "import os,sys; "
                    "sys.stdout.write(os.environ.get('TEDD_READER_EXPLICIT_TEST','missing')); "
                    "sys.stdout.write('|' + os.environ.get('TEDD_READER_AMBIENT_TEST','absent'))"
                ),
                b"",
                deadline=time.monotonic() + 5,
                output_limit=64,
                environment={"TEDD_READER_EXPLICIT_TEST": "visible"},
            )
        finally:
            os.environ.pop(marker, None)
        self.assertEqual(result.stdout, b"visible|absent")

    def test_invalid_deadline_and_precancelled_request_never_launch_a_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / "launched"
            command = self.child(
                "import pathlib,sys,time; "
                "pathlib.Path(sys.argv[1]).write_text('yes'); time.sleep(0.5)",
                str(marker),
            )
            with self.assertRaises(SourceArchiveError):
                run_bounded(command, b"", deadline=float("nan"), output_limit=1)
            time.sleep(0.1)
            self.assertFalse(marker.exists())

            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(SourceArchiveError):
                run_bounded(
                    command,
                    b"",
                    deadline=time.monotonic() + 1,
                    output_limit=1,
                    cancelled=cancelled,
                )
            time.sleep(0.1)
            self.assertFalse(marker.exists())

    def test_reader_thread_start_failure_closes_pipes_and_reaps_child(self) -> None:
        process = _Process()
        start_error = RuntimeError("private reader startup detail")
        reader = mock.Mock()
        reader.start.side_effect = start_error
        writer = mock.Mock()

        with (
            mock.patch(
                "tedd_knowledge_graph.source_reader_process.subprocess.Popen",
                return_value=process,
            ),
            mock.patch(
                "tedd_knowledge_graph.source_reader_process.threading.Thread",
                side_effect=(reader, writer),
            ),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            _start_bounded(
                self.child("pass"),
                b"payload",
                output_limit=64,
                deadline=time.monotonic() + 1,
            )

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertIs(raised.exception.__cause__, start_error)
        self.assertTrue(process.terminated)
        self.assertTrue(process.reaped)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def test_thread_start_cleanup_failure_remains_typed_and_observable(self) -> None:
        process = _Process()
        start_error = RuntimeError("private reader startup detail")
        cleanup_error = SourceArchiveError("source_unavailable")
        reader = mock.Mock()
        reader.start.side_effect = start_error

        with (
            mock.patch(
                "tedd_knowledge_graph.source_reader_process.subprocess.Popen",
                return_value=process,
            ),
            mock.patch(
                "tedd_knowledge_graph.source_reader_process.threading.Thread",
                side_effect=(reader, mock.Mock()),
            ),
            mock.patch(
                "tedd_knowledge_graph.source_reader_process._ManagedProcess.terminate_and_reap",
                side_effect=cleanup_error,
            ),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            _start_bounded(
                self.child("pass"),
                b"payload",
                output_limit=64,
                deadline=time.monotonic() + 1,
            )

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertIs(raised.exception.__cause__, cleanup_error)

    @unittest.skipIf(os.name == "nt", "nonblocking input setup is Unix-only")
    def test_writer_thread_start_failure_closes_its_pipe_and_reaps_child(self) -> None:
        process = _Process()
        start_error = RuntimeError("private writer startup detail")
        reader = mock.Mock()
        writer = mock.Mock()
        writer.start.side_effect = start_error

        with (
            mock.patch(
                "tedd_knowledge_graph.source_reader_process.subprocess.Popen",
                return_value=process,
            ),
            mock.patch(
                "tedd_knowledge_graph.source_reader_process.threading.Thread",
                side_effect=(reader, writer),
            ),
            mock.patch("tedd_knowledge_graph.source_reader_process.os.set_blocking"),
            self.assertRaises(SourceArchiveError) as raised,
        ):
            _start_bounded(
                self.child("pass"),
                b"payload",
                output_limit=64,
                deadline=time.monotonic() + 1,
            )

        self.assertEqual(raised.exception.code, "source_unavailable")
        self.assertIs(raised.exception.__cause__, start_error)
        self.assertTrue(process.terminated)
        self.assertTrue(process.reaped)
        self.assertTrue(process.stdin.closed)
        reader.join.assert_called_once()

    @unittest.skipIf(os.name == "nt", "SIGTERM resistance and PID probes are POSIX-only")
    def test_sigterm_resistant_child_is_killed_and_reaped_within_absolute_deadline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as raw:
            pid_file = Path(raw) / "pid"
            ready_file = Path(raw) / "ready"
            process = _start_bounded(
                self.child(
                    "import os,pathlib,signal,sys,time; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                    "pathlib.Path(sys.argv[2]).write_text('ready'); "
                    "time.sleep(30)",
                    str(pid_file),
                    str(ready_file),
                ),
                b"",
                output_limit=64,
            )
            try:
                ready_deadline = time.monotonic() + 5
                ready_poll = threading.Event()
                while not ready_file.exists():
                    if process._process.poll() is not None:
                        self.fail("child exited before readiness handshake")
                    if time.monotonic() >= ready_deadline:
                        self.fail("child readiness handshake timed out")
                    ready_poll.wait(0.01)

                started = time.monotonic()
                with self.assertRaises(SourceArchiveError):
                    process.wait(started + 0.1)
                elapsed = time.monotonic() - started
            finally:
                process.terminate_and_reap(time.monotonic() + 1)

            self.assertTrue(pid_file.exists())
            pid = int(pid_file.read_text())
            self.assertLess(elapsed, 0.3)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_cancellation_switches_to_discard_only_and_clears_retained_prefix(self) -> None:
        cancelled = threading.Event()
        process = _start_bounded(
            self.child(
                "import sys,time; sys.stdout.buffer.write(b'private-prefix'); "
                "sys.stdout.buffer.flush(); time.sleep(30)"
            ),
            b"",
            output_limit=1024,
        )
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        try:
            with self.assertRaises(SourceArchiveError):
                process.wait(time.monotonic() + 2, cancelled)
        finally:
            timer.cancel()

        with process._lock:
            self.assertEqual(bytes(process._output), b"")
            self.assertTrue(process._discard)

    @unittest.skipIf(os.name == "nt", "attached reader transport uses Unix sockets")
    def test_input_stop_interrupts_a_full_pipe_without_terminating_the_child(self) -> None:
        process = _start_bounded(
            self.child("import time; time.sleep(30)"),
            b"p" * (8 * 1024**2),
            output_limit=64,
        )
        try:
            time.sleep(0.1)

            process.stop_input()
            process._writer.join(0.3)

            self.assertFalse(process._writer.is_alive())
            self.assertIsNone(process._process.poll())
        finally:
            process.terminate_and_reap(time.monotonic() + 1)

    def test_termination_uses_only_the_remaining_deadline_in_term_then_kill_order(
        self,
    ) -> None:
        calls: list[tuple[str, float | None]] = []

        class Process:
            stdin = None
            stdout = None
            returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                calls.append(("terminate", None))

            def kill(self) -> None:
                calls.append(("kill", None))

            def wait(self, timeout: float) -> int:
                calls.append(("wait", timeout))
                if len([call for call in calls if call[0] == "wait"]) == 1:
                    raise __import__("subprocess").TimeoutExpired("synthetic", timeout)
                self.returncode = -9
                return self.returncode

        process = object.__new__(
            __import__(
                "tedd_knowledge_graph.source_reader_process",
                fromlist=["_ManagedProcess"],
            )._ManagedProcess
        )
        process._process = Process()
        process._output = bytearray(b"private")
        process._discard = False
        process._lock = threading.Lock()
        process._input_stopped = threading.Event()
        process._reader_started = False
        process._writer_started = False

        with mock.patch(
            "tedd_knowledge_graph.source_reader_process.time.monotonic",
            side_effect=(10.0, 10.0, 10.0, 10.1),
        ):
            process.terminate_and_reap(10.2)

        self.assertEqual([name for name, _timeout in calls], ["terminate", "wait", "kill", "wait"])
        waits = [timeout for name, timeout in calls if name == "wait"]
        self.assertEqual(len(waits), 2)
        first, second = waits
        assert first is not None and second is not None
        self.assertAlmostEqual(first, 0.1)
        self.assertAlmostEqual(second, 0.1)


if __name__ == "__main__":
    unittest.main()
