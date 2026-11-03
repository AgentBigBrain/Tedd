"""Behavioral tests for the isolated reader guest and fixed worker dispatcher."""

from __future__ import annotations

import io
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import TextIO, cast
from unittest import mock

from tedd_knowledge_graph import (
    source_office_host,
    source_pdf_host,
)
from tedd_knowledge_graph import (
    source_reader_guest as guest,
)
from tedd_knowledge_graph import (
    source_reader_worker as worker,
)
from tedd_knowledge_graph.source_contracts import SourceArchiveError
from tedd_knowledge_graph.source_reader_contracts import ReaderRequest

PACKAGE_SCRIPTS = Path(guest.__file__).resolve().parent.parent
SUPERVISOR_PROGRAM = """
import sys
sys.path.insert(0, sys.argv[1])
from tedd_knowledge_graph.source_reader_guest import supervise
status = supervise((sys.executable, '-I', '-B', '-c', sys.argv[3], *sys.argv[4:]),
                   seconds=float(sys.argv[2]))
raise SystemExit(status)
"""
REPORTING_SUPERVISOR_PROGRAM = """
import sys
sys.path.insert(0, sys.argv[1])
from tedd_knowledge_graph import source_reader_guest as guest
real_popen = guest.subprocess.Popen
def reporting_popen(*args, **kwargs):
    process = real_popen(*args, **kwargs)
    print(process.pid, flush=True)
    return process
guest.subprocess.Popen = reporting_popen
status = guest.supervise((sys.executable, '-I', '-B', '-c', sys.argv[3]),
                         seconds=float(sys.argv[2]))
raise SystemExit(status)
"""


def _supervisor_command(seconds: float, child: str, *arguments: str) -> tuple[str, ...]:
    return (
        sys.executable,
        "-I",
        "-B",
        "-c",
        SUPERVISOR_PROGRAM,
        str(PACKAGE_SCRIPTS),
        str(seconds),
        child,
        *arguments,
    )


def _reporting_supervisor_command(seconds: float, child: str) -> tuple[str, ...]:
    return (
        sys.executable,
        "-I",
        "-B",
        "-c",
        REPORTING_SUPERVISOR_PROGRAM,
        str(PACKAGE_SCRIPTS),
        str(seconds),
        child,
    )


class _BinaryInput:
    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


class _BinaryOutput:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


def _run_worker(arguments: tuple[str, ...], payload: bytes) -> tuple[int, bytes]:
    stdin = _BinaryInput(payload)
    stdout = _BinaryOutput()
    with (
        mock.patch.object(sys, "stdin", cast(TextIO, stdin)),
        mock.patch.object(sys, "stdout", cast(TextIO, stdout)),
    ):
        status = worker.run(arguments)
    return status, stdout.buffer.getvalue()


class FixedDispatchTests(unittest.TestCase):
    def test_guest_and_worker_accept_exact_reader_request_arguments(self) -> None:
        package = Path(worker.__file__).resolve().parent
        python = sys.executable
        cases = (
            (
                ReaderRequest("image-inspect"),
                (
                    python,
                    "-I",
                    "-B",
                    str(package / "source_office_host.py"),
                    "image",
                ),
            ),
            (
                ReaderRequest("image-page", 2),
                (
                    python,
                    "-I",
                    "-B",
                    str(package / "source_office_host.py"),
                    "image",
                    "--render-unit",
                    "2",
                ),
            ),
            (
                ReaderRequest("pdf-text"),
                (python, "-I", "-B", str(package / "source_pdf_host.py")),
            ),
            (
                ReaderRequest("pdf-page", 3),
                (
                    python,
                    "-I",
                    "-B",
                    str(package / "source_pdf_host.py"),
                    "--render-page",
                    "3",
                    "--renderer",
                    "/usr/bin/pdftoppm",
                ),
            ),
            (
                ReaderRequest("docx-text"),
                (
                    python,
                    "-I",
                    "-B",
                    str(package / "source_office_host.py"),
                    "docx",
                ),
            ),
            (ReaderRequest("docx-page", 4), None),
        )

        for request, expected_worker in cases:
            arguments = request.worker_arguments()
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    (
                        sys.executable,
                        "-I",
                        "-B",
                        str(Path(guest.__file__).resolve().with_name("source_reader_worker.py")),
                        *arguments,
                    ),
                    guest.worker_argv(arguments),
                )
                self.assertEqual(expected_worker, worker.host_argv(arguments))

    def test_dispatch_rejects_noncanonical_or_wrong_arity_arguments(self) -> None:
        invalid = (
            (),
            ("",),
            ("unknown",),
            ("pdf-text", "1"),
            ("image-inspect", "1"),
            ("docx-text", "1"),
            ("image-page",),
            ("pdf-page",),
            ("docx-page",),
            ("image-page", "0"),
            ("image-page", "257"),
            ("image-page", "01"),
            ("pdf-page", "+1"),
            ("docx-page", " 1"),
            ("docx-page", "1", "extra"),
        )

        for arguments in invalid:
            for parse in (guest.worker_argv, worker.host_argv):
                with self.subTest(arguments=arguments, parse=parse.__module__):
                    with self.assertRaisesRegex(ValueError, "invalid reader operation") as caught:
                        parse(arguments)
                    self.assertIsNone(caught.exception.__cause__)

    def test_worker_execs_only_the_derived_fixed_host_command(self) -> None:
        arguments = ReaderRequest("pdf-page", 5).worker_arguments()
        expected = worker.host_argv(arguments)
        self.assertIsNotNone(expected)
        assert expected is not None
        with mock.patch.object(worker.os, "execv", side_effect=OSError("private path")) as execute:
            status = worker.run(arguments)

        self.assertEqual(127, status)
        execute.assert_called_once_with(expected[0], expected)


class SupervisorTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "bounded process-group cleanup requires POSIX sessions")
    def test_sleeping_child_times_out_and_is_reaped(self) -> None:
        child = "import time; time.sleep(10)"
        started = time.monotonic()
        process = subprocess.Popen(
            _reporting_supervisor_command(0.15, child),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.assertIsNotNone(process.stdout)
        stdout = process.stdout
        assert stdout is not None
        ready: queue.Queue[bytes] = queue.Queue(maxsize=1)
        reader = threading.Thread(target=lambda: ready.put(stdout.readline()), daemon=True)
        reader_started = False
        kill_group = cast(Callable[[int, int], None], vars(os)["killpg"])
        sigkill = cast(int, vars(signal)["SIGKILL"])
        try:
            reader.start()
            reader_started = True
            child_pid = int(ready.get(timeout=2.0))
            reader.join(1.0)
            self.assertFalse(reader.is_alive())
            output, errors = process.communicate(timeout=3.0)
            status = process.returncode
            elapsed = time.monotonic() - started
            self.assertEqual(b"", output)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if process.returncode is None:
                try:
                    kill_group(process.pid, sigkill)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=1.0)
            stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            if reader_started:
                reader.join(1.0)

        self.assertEqual(124, status)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(b"", errors)

    def test_child_receives_ordinary_stdin_eof_and_can_succeed(self) -> None:
        child = "import sys; raise SystemExit(0 if sys.stdin.buffer.read() == b'' else 9)"
        result = subprocess.run(
            _supervisor_command(1.0, child),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)

    def test_nonzero_child_exit_is_returned(self) -> None:
        result = subprocess.run(
            _supervisor_command(1.0, "raise SystemExit(23)"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
            check=False,
        )

        self.assertEqual(23, result.returncode)
        self.assertEqual(b"", result.stderr)

    def test_child_output_is_inherited_on_success(self) -> None:
        result = subprocess.run(
            _supervisor_command(3.0, "import os; os.write(1, b'ready')"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            check=False,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual(b"ready", result.stdout)
        self.assertEqual(b"", result.stderr)

    def test_blocked_inherited_stdout_cannot_extend_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            marker = Path(raw) / "ready"
            child = (
                "import os,pathlib,sys\n"
                "pathlib.Path(sys.argv[1]).write_text('ready')\n"
                "while True: os.write(1, b'x' * 65536)"
            )
            started = time.monotonic()
            process = subprocess.Popen(
                (*_supervisor_command(3.0, child), str(marker)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                ready_deadline = time.monotonic() + 5
                ready_poll = threading.Event()
                while not marker.exists():
                    if process.poll() is not None:
                        self.fail("child exited before readiness handshake")
                    if time.monotonic() >= ready_deadline:
                        self.fail("child readiness handshake timed out")
                    ready_poll.wait(0.01)
                status = process.wait(timeout=5)
                elapsed = time.monotonic() - started
                _output, errors = process.communicate(timeout=1)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=1)

        self.assertEqual(124, status)
        self.assertLess(elapsed, 5.0)
        self.assertEqual(b"", errors)

    def test_launch_failure_returns_content_free_127(self) -> None:
        program = """
import sys
sys.path.insert(0, sys.argv[1])
from tedd_knowledge_graph.source_reader_guest import supervise
raise SystemExit(supervise(('/definitely/not/a/reader-worker',), seconds=1.0))
"""
        result = subprocess.run(
            (sys.executable, "-I", "-B", "-c", program, str(PACKAGE_SCRIPTS)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
            check=False,
        )

        self.assertEqual(127, result.returncode)
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)

    def test_invalid_deadlines_refuse_before_launch(self) -> None:
        program = """
import pathlib
import sys
sys.path.insert(0, sys.argv[1])
from tedd_knowledge_graph.source_reader_guest import supervise
marker = pathlib.Path(sys.argv[3])
child = (sys.executable, '-I', '-B', '-c',
         'import pathlib,sys; pathlib.Path(sys.argv[1]).touch()', str(marker))
try:
    supervise(child, seconds=float(sys.argv[2]))
except ValueError as error:
    raise SystemExit(0 if str(error) == 'invalid supervisor arguments' else 2)
raise SystemExit(3)
"""
        with tempfile.TemporaryDirectory() as raw:
            for seconds in ("0", "-1", "31", "nan", "inf"):
                marker = Path(raw) / seconds
                with self.subTest(seconds=seconds):
                    result = subprocess.run(
                        (
                            sys.executable,
                            "-I",
                            "-B",
                            "-c",
                            program,
                            str(PACKAGE_SCRIPTS),
                            seconds,
                            str(marker),
                        ),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=3,
                        check=False,
                    )
                    self.assertEqual(0, result.returncode)
                    self.assertFalse(marker.exists())


class DocxPageWorkerTests(unittest.TestCase):
    def test_docx_page_limits_before_read_and_uses_fixed_lifecycle(self) -> None:
        events: list[str] = []
        observed_root: Path | None = None

        class OrderedBuffer(io.BytesIO):
            def read(self, maximum: int | None = -1) -> bytes:
                events.append(f"read:{maximum}")
                return super().read(maximum)

        stdin = _BinaryInput(b"")
        stdin.buffer = OrderedBuffer(b"PK\x03\x04synthetic")
        stdout = _BinaryOutput()

        def limit(memory: int, seconds: int) -> int:
            events.append(f"limit:{memory}:{seconds}")
            return 17

        def document_page(
            payload: bytes,
            kind: str,
            page: int,
            *,
            reader: Path,
            converter: Path,
            renderer: Path,
            work_root: Path,
        ) -> dict[str, object]:
            nonlocal observed_root
            events.append("document_page")
            self.assertEqual(b"PK\x03\x04synthetic", payload)
            self.assertEqual(("docx", 7), (kind, page))
            self.assertEqual(Path(sys.executable), reader)
            self.assertEqual(Path("/usr/bin/libreoffice"), converter)
            self.assertEqual(Path("/usr/bin/pdftoppm"), renderer)
            self.assertTrue(work_root.is_dir())
            observed_root = work_root
            return {"status": "text_screened", "page": 7, "resources": "bounded"}

        with tempfile.TemporaryDirectory() as raw:
            work_base = Path(raw)
            with (
                mock.patch.object(worker, "_WORK_ROOT", work_base, create=True),
                mock.patch.object(source_pdf_host, "limit_worker", side_effect=limit),
                mock.patch.object(source_office_host, "document_page", side_effect=document_page),
                mock.patch.object(sys, "stdin", cast(TextIO, stdin)),
                mock.patch.object(sys, "stdout", cast(TextIO, stdout)),
            ):
                status = worker.run(("docx-page", "7"))

            self.assertIsNotNone(observed_root)
            assert observed_root is not None
            self.assertEqual(work_base, observed_root.parent)
            self.assertFalse(observed_root.exists())

        self.assertEqual(
            [f"limit:{2 * 1024**3}:30", f"read:{64 * 1024**2 + 1}", "document_page"],
            events,
        )
        self.assertEqual(0, status)
        self.assertEqual(
            {"status": "text_screened", "page": 7, "resources": "bounded"},
            json.loads(stdout.buffer.getvalue()),
        )

    def test_docx_page_rejects_empty_and_oversized_input_after_limit(self) -> None:
        cases = ((b"", "source_corrupt"), (b"x" * (64 * 1024**2 + 1), "limit_exceeded"))
        for payload, expected in cases:
            with (
                self.subTest(expected=expected),
                mock.patch.object(source_pdf_host, "limit_worker", return_value=0) as limit,
                mock.patch.object(source_office_host, "document_page") as document_page,
            ):
                status, output = _run_worker(("docx-page", "1"), payload)
            self.assertEqual(1, status)
            self.assertEqual({"status": expected}, json.loads(output))
            limit.assert_called_once_with(2 * 1024**3, 30)
            document_page.assert_not_called()

    def test_docx_page_emits_only_closed_content_free_errors(self) -> None:
        marker = "DO_NOT_ECHO_source_body_8f21"
        cases = (
            (SourceArchiveError("privacy_rejected"), "privacy_rejected"),
            (SourceArchiveError("cleanup_incomplete"), "cleanup_incomplete"),
            (SourceArchiveError(marker), "capability_unavailable"),
            (RuntimeError(marker), "capability_unavailable"),
        )
        for failure, expected in cases:
            with (
                self.subTest(expected=expected),
                tempfile.TemporaryDirectory() as raw,
                mock.patch.object(worker, "_WORK_ROOT", Path(raw), create=True),
                mock.patch.object(source_pdf_host, "limit_worker", return_value=0),
                mock.patch.object(source_office_host, "document_page", side_effect=failure),
            ):
                status, output = _run_worker(("docx-page", "1"), b"PK\x03\x04synthetic")
            self.assertEqual(1, status)
            self.assertEqual({"status": expected}, json.loads(output))
            self.assertNotIn(marker.encode(), output)

    def test_docx_page_caps_serialized_output_at_32_mib(self) -> None:
        response = {"status": "text_screened", "content": "x" * (32 * 1024**2)}
        with (
            tempfile.TemporaryDirectory() as raw,
            mock.patch.object(worker, "_WORK_ROOT", Path(raw), create=True),
            mock.patch.object(source_pdf_host, "limit_worker", return_value=0),
            mock.patch.object(source_office_host, "document_page", return_value=response),
        ):
            status, output = _run_worker(("docx-page", "1"), b"PK\x03\x04synthetic")

        self.assertEqual(1, status)
        self.assertEqual({"status": "limit_exceeded"}, json.loads(output))
        self.assertLess(len(output), 128)


class GuestEntrypointTests(unittest.TestCase):
    def test_real_non_pid1_entrypoint_refuses_without_output(self) -> None:
        result = subprocess.run(
            (
                sys.executable,
                "-I",
                "-B",
                str(Path(guest.__file__).resolve()),
                "pdf-text",
            ),
            input=b"DO_NOT_READ_SOURCE",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3,
            check=False,
        )

        self.assertEqual(126, result.returncode)
        self.assertEqual(b"", result.stdout)
        self.assertEqual(b"", result.stderr)

    def test_non_linux_or_non_pid1_refuses_before_dispatch(self) -> None:
        cases = (("darwin", 1), ("linux", 2))
        for platform, pid in cases:
            with (
                self.subTest(platform=platform, pid=pid),
                mock.patch.object(guest, "_IS_LINUX", platform == "linux"),
                mock.patch.object(guest.os, "getpid", return_value=pid),
                mock.patch.object(guest, "worker_argv") as worker_argv,
                mock.patch.object(guest, "supervise") as supervise,
            ):
                status = guest.entrypoint(("pdf-text",))
            self.assertEqual(126, status)
            worker_argv.assert_not_called()
            supervise.assert_not_called()

    def test_invalid_operation_refuses_before_alarm_or_dumpability(self) -> None:
        with (
            mock.patch.object(guest, "_IS_LINUX", True),
            mock.patch.object(guest.os, "getpid", return_value=1),
            mock.patch.object(guest, "arm_deadline") as arm,
            mock.patch.object(guest, "disable_dumpability") as disable,
            mock.patch.object(guest, "supervise") as supervise,
        ):
            status = guest.entrypoint(("pdf-text", "extra"))

        self.assertEqual(126, status)
        arm.assert_not_called()
        disable.assert_not_called()
        supervise.assert_not_called()

    def test_pid1_arms_hard_deadline_and_disables_dumpability_before_launch(self) -> None:
        events: list[str] = []
        arguments = ReaderRequest("pdf-page", 2).worker_arguments()
        command = guest.worker_argv(arguments)

        def arm() -> None:
            events.append("alarm")

        def disable() -> None:
            events.append("dumpability")

        def supervise(argv: tuple[str, ...], *, seconds: float = 30.0) -> int:
            self.assertEqual(command, argv)
            self.assertEqual(30.0, seconds)
            events.append("launch")
            return 19

        with (
            mock.patch.object(guest, "_IS_LINUX", True),
            mock.patch.object(guest.os, "getpid", return_value=1),
            mock.patch.object(guest, "arm_deadline", side_effect=arm),
            mock.patch.object(guest, "disable_dumpability", side_effect=disable),
            mock.patch.object(guest, "supervise", side_effect=supervise),
        ):
            status = guest.entrypoint(arguments)

        self.assertEqual(19, status)
        self.assertEqual(["alarm", "dumpability", "launch"], events)

    def test_alarm_handler_uses_immediate_timeout_exit(self) -> None:
        with mock.patch.object(guest.os, "_exit", side_effect=RuntimeError("stopped")) as exit_now:
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                guest.hard_timeout(14, None)
        exit_now.assert_called_once_with(124)

    def test_alarm_uses_real_time_and_exact_guest_lifetime(self) -> None:
        alarm = mock.Mock(SIGALRM=14, ITIMER_REAL=0)
        with (
            mock.patch.object(guest, "_ALARM", alarm),
        ):
            guest.arm_deadline()

        alarm.signal.assert_called_once_with(alarm.SIGALRM, guest.hard_timeout)
        alarm.setitimer.assert_called_once_with(alarm.ITIMER_REAL, 30.0)

    def test_dumpability_is_disabled_with_linux_prctl(self) -> None:
        class Prctl:
            argtypes: object = None
            restype: object = None

            def __init__(self) -> None:
                self.calls: list[tuple[int, int, int, int, int]] = []

            def __call__(self, one: int, two: int, three: int, four: int, five: int) -> int:
                self.calls.append((one, two, three, four, five))
                return 0

        prctl = Prctl()
        library = mock.Mock(prctl=prctl)
        with mock.patch.object(guest.ctypes, "CDLL", return_value=library) as load:
            guest.disable_dumpability()

        load.assert_called_once_with(None, use_errno=True)
        self.assertEqual([(4, 0, 0, 0, 0)], prctl.calls)

    def test_main_uses_immediate_exit_without_stdout_flush(self) -> None:
        with (
            mock.patch.object(guest, "entrypoint", return_value=23),
            mock.patch.object(guest.os, "_exit", side_effect=RuntimeError("stopped")) as exit_now,
            mock.patch.object(sys.stdout, "flush", side_effect=AssertionError("flush attempted")),
            mock.patch.object(sys, "argv", ["source_reader_guest.py", "pdf-text"]),
        ):
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                guest.main()
        exit_now.assert_called_once_with(23)

    def test_main_maps_unexpected_failure_to_content_free_immediate_exit(self) -> None:
        marker = "DO_NOT_ECHO_unexpected_51cb"
        with (
            mock.patch.object(guest, "entrypoint", side_effect=RuntimeError(marker)),
            mock.patch.object(guest.os, "_exit", side_effect=RuntimeError("stopped")) as exit_now,
            mock.patch.object(sys.stdout, "flush", side_effect=AssertionError("flush attempted")),
            mock.patch.object(sys, "argv", ["source_reader_guest.py", "pdf-text"]),
        ):
            with self.assertRaisesRegex(RuntimeError, "stopped") as caught:
                guest.main()
        exit_now.assert_called_once_with(127)
        self.assertNotIn(marker, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
