"""Behavioral tests for the fixed reader guest entrypoint."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import TextIO, cast
from unittest import mock

from tedd_knowledge_graph import source_files, source_office_host, source_pdf_host
from tedd_knowledge_graph.source_contracts import SourceArchiveError

from scripts import macos_reader_guest as guest

MAX_INPUT = 64 * 1024**2
MAX_OUTPUT = 32 * 1024**2


class _BinaryInput:
    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)


class _BinaryOutput:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()


class _UnreadableBuffer:
    def read(self, _maximum: int = -1) -> bytes:
        raise AssertionError("stdin read attempted")


class _UnreadableInput:
    def __init__(self) -> None:
        self.buffer = _UnreadableBuffer()


def _invoke(
    argv: list[str], payload: bytes = b"", *, stdin: object | None = None
) -> tuple[int, bytes]:
    input_stream = _BinaryInput(payload) if stdin is None else stdin
    output_stream = _BinaryOutput()
    with (
        mock.patch.object(sys, "argv", argv),
        mock.patch.object(sys, "stdin", cast(TextIO, input_stream)),
        mock.patch.object(sys, "stdout", cast(TextIO, output_stream)),
    ):
        status = guest.main()
    return status, output_stream.buffer.getvalue()


def _decoded(output: bytes) -> dict[str, object]:
    value = json.loads(output)
    if not isinstance(value, dict):
        raise AssertionError("guest response is not an object")
    return cast(dict[str, object], value)


class MacosReaderGuestTests(unittest.TestCase):
    def test_pdf_text_is_a_fixed_worker(self) -> None:
        self.assertEqual(
            guest.worker_argv("pdf-text", "/usr/bin/python3"),
            (
                "/usr/bin/python3",
                "-I",
                "-B",
                "/opt/tedd-reader/scripts/tedd_knowledge_graph/source_pdf_host.py",
            ),
        )
        with self.assertRaises(ValueError):
            guest.worker_argv("--arbitrary", "/usr/bin/python3")

    def test_every_route_has_literal_arguments(self) -> None:
        """Catch a worker path, fixed option, or special-route classification changing."""

        expected = {
            "image-overview": (
                "/usr/bin/python3",
                "-I",
                "-B",
                "/opt/tedd-reader/scripts/tedd_knowledge_graph/source_office_host.py",
                "image",
                "--render-unit",
                "1",
            ),
            "pdf-text": (
                "/usr/bin/python3",
                "-I",
                "-B",
                "/opt/tedd-reader/scripts/tedd_knowledge_graph/source_pdf_host.py",
            ),
            "pdf-page": (
                "/usr/bin/python3",
                "-I",
                "-B",
                "/opt/tedd-reader/scripts/tedd_knowledge_graph/source_pdf_host.py",
                "--render-page",
                "1",
                "--renderer",
                "/usr/bin/pdftoppm",
            ),
            "office-page": None,
            "self-check": None,
        }

        for operation, argv in expected.items():
            with self.subTest(operation=operation):
                self.assertEqual(argv, guest.worker_argv(operation, "/usr/bin/python3"))

    def test_worker_rejects_unknown_and_non_string_operations_without_echoing(self) -> None:
        """Catch arbitrary modes crossing the guest boundary or leaking in errors."""

        marker = "DO_NOT_ECHO_4e33"
        for operation in (marker, "", "PDF-TEXT", 7, None):
            with self.subTest(operation=operation), self.assertRaises(ValueError) as raised:
                guest.worker_argv(cast(str, operation), "/usr/bin/python3")
            self.assertNotIn(marker, str(raised.exception))

    def test_exec_routes_use_only_fixed_argv_and_safe_failure_output(self) -> None:
        """Catch shell dispatch, caller arguments, or an exec failure leaking details."""

        marker = "DO_NOT_ECHO_exec_7b2a"
        for operation in ("image-overview", "pdf-text", "pdf-page"):
            expected = guest.worker_argv(operation, sys.executable)
            self.assertIsNotNone(expected)
            with (
                self.subTest(operation=operation),
                mock.patch.object(os, "execv", side_effect=OSError(marker)) as execute,
            ):
                status, output = _invoke(["tedd-reader-probe", operation])
            self.assertEqual(1, status)
            self.assertEqual({"status": "capability_unavailable"}, _decoded(output))
            self.assertNotIn(marker.encode(), output)
            execute.assert_called_once_with(expected[0], expected)

    def test_exact_argc_and_operation_are_required_before_dispatch(self) -> None:
        """Catch missing, extra, or unknown caller arguments reaching a worker or stdin."""

        marker = "DO_NOT_ECHO_arg_82ac"
        for argv in (
            ["tedd-reader-probe"],
            ["tedd-reader-probe", "pdf-text", "extra"],
            ["tedd-reader-probe", marker],
        ):
            with (
                self.subTest(argv=argv),
                mock.patch.object(os, "execv") as execute,
            ):
                status, output = _invoke(argv, stdin=_UnreadableInput())
            self.assertEqual(1, status)
            self.assertEqual({"status": "capability_unavailable"}, _decoded(output))
            self.assertNotIn(marker.encode(), output)
            execute.assert_not_called()

    def test_office_page_limits_before_read_and_preserves_document_lifecycle(self) -> None:
        """Catch source bytes being read early or bypassing document_page lifecycle checks."""

        events: list[str] = []
        used_root: Path | None = None

        class OrderedBuffer:
            def read(self, maximum: int = -1) -> bytes:
                events.append(f"read:{maximum}")
                return b"synthetic-docx"

        class OrderedInput:
            def __init__(self) -> None:
                self.buffer = OrderedBuffer()

        def record_limit(memory: int, seconds: int) -> int:
            events.append(f"limit:{memory}:{seconds}")
            return 0

        def render_page(
            payload: bytes,
            kind: str,
            page: int,
            *,
            reader: Path,
            converter: Path,
            renderer: Path,
            work_root: Path,
        ) -> dict[str, object]:
            nonlocal used_root
            self.assertEqual(b"synthetic-docx", payload)
            self.assertEqual(("docx", 1), (kind, page))
            self.assertEqual(Path(sys.executable), reader)
            self.assertEqual(Path("/usr/bin/libreoffice"), converter)
            self.assertEqual(Path("/usr/bin/pdftoppm"), renderer)
            self.assertTrue(work_root.is_dir())
            used_root = work_root
            return {"status": "text_screened", "page": 1, "resources": "bounded"}

        result = {"status": "text_screened", "page": 1, "resources": "bounded"}
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw).resolve()
            with (
                mock.patch.object(guest, "_WORK_ROOT", base, create=True),
                mock.patch.object(
                    source_pdf_host,
                    "limit_worker",
                    side_effect=record_limit,
                ),
                mock.patch.object(
                    source_office_host, "document_page", side_effect=render_page
                ) as render,
            ):
                status, output = _invoke(["tedd-reader-probe", "office-page"], stdin=OrderedInput())

            self.assertIsNotNone(used_root)
            assert used_root is not None
            self.assertEqual(base, used_root.parent)
            self.assertFalse(used_root.exists())

        self.assertEqual([f"limit:{2 * 1024**3}:30", f"read:{MAX_INPUT + 1}"], events)
        self.assertEqual(0, status)
        self.assertEqual(result, _decoded(output))
        render.assert_called_once()

    @unittest.skipIf(os.name == "nt", "POSIX guest privacy requires POSIX mode semantics")
    def test_office_page_uses_private_child_when_work_base_is_home(self) -> None:
        """Catch passing the HOME-equal /work base directly to document_page."""

        observed: list[Path] = []

        def inspect_private_child(
            _payload: bytes,
            _kind: str,
            _page: int,
            *,
            reader: Path,
            converter: Path,
            renderer: Path,
            work_root: Path,
        ) -> dict[str, object]:
            self.assertEqual(Path(sys.executable), reader)
            self.assertEqual(Path("/usr/bin/libreoffice"), converter)
            self.assertEqual(Path("/usr/bin/pdftoppm"), renderer)
            self.assertEqual(work_root, source_files.private_root(work_root))
            self.assertEqual(0o700, work_root.stat().st_mode & 0o777)
            observed.append(work_root)
            return {"status": "text_screened", "resources": "bounded"}

        with tempfile.TemporaryDirectory() as raw:
            # macOS exposes /var as a symlink; resolve it so this test isolates
            # the HOME-equality guard rather than the independent link guard.
            base = Path(raw).resolve()
            with (
                mock.patch.object(guest, "_WORK_ROOT", base, create=True),
                mock.patch.object(Path, "home", return_value=base),
                mock.patch.object(source_pdf_host, "limit_worker", return_value=0),
                mock.patch.object(
                    source_office_host,
                    "document_page",
                    side_effect=inspect_private_child,
                ),
            ):
                status, output = _invoke(["tedd-reader-probe", "office-page"], b"docx")

            self.assertEqual(1, len(observed))
            self.assertEqual(base, observed[0].parent)
            self.assertFalse(observed[0].exists())

        self.assertEqual(0, status)
        self.assertEqual("text_screened", _decoded(output)["status"])

    def test_office_page_rejects_empty_and_oversized_input(self) -> None:
        """Catch zero-byte or over-limit source input reaching the Office lifecycle."""

        cases = ((b"", "source_corrupt"), (b"x" * (MAX_INPUT + 1), "limit_exceeded"))
        for payload, expected in cases:
            with (
                self.subTest(expected=expected),
                mock.patch.object(source_pdf_host, "limit_worker", return_value=0),
                mock.patch.object(source_office_host, "document_page") as render,
            ):
                status, output = _invoke(["tedd-reader-probe", "office-page"], payload)
            self.assertEqual(1, status)
            self.assertEqual({"status": expected}, _decoded(output))
            render.assert_not_called()

    def test_office_page_refuses_oversized_json_output(self) -> None:
        """Catch a successful dependency result escaping the guest output cap."""

        result = {"status": "text_screened", "content": "x" * MAX_OUTPUT}
        with (
            mock.patch.object(guest, "_WORK_ROOT", Path(tempfile.gettempdir()), create=True),
            mock.patch.object(source_pdf_host, "limit_worker", return_value=0),
            mock.patch.object(source_office_host, "document_page", return_value=result),
        ):
            status, output = _invoke(["tedd-reader-probe", "office-page"], b"docx")

        self.assertEqual(1, status)
        self.assertEqual({"status": "limit_exceeded"}, _decoded(output))
        self.assertLess(len(output), 128)

    def test_office_page_maps_only_recognized_source_errors(self) -> None:
        """Catch caller content or arbitrary exception codes becoming public output."""

        marker = "DO_NOT_ECHO_error_73c9"
        cases = (
            (SourceArchiveError("privacy_rejected"), "privacy_rejected"),
            (SourceArchiveError(marker), "capability_unavailable"),
            (RuntimeError(marker), "capability_unavailable"),
        )
        for failure, expected in cases:
            with (
                self.subTest(failure=type(failure).__name__, expected=expected),
                mock.patch.object(guest, "_WORK_ROOT", Path(tempfile.gettempdir()), create=True),
                mock.patch.object(source_pdf_host, "limit_worker", return_value=0),
                mock.patch.object(source_office_host, "document_page", side_effect=failure),
            ):
                status, output = _invoke(["tedd-reader-probe", "office-page"], b"docx")
            self.assertEqual(1, status)
            self.assertEqual({"status": expected}, _decoded(output))
            self.assertNotIn(marker.encode(), output)

    def test_self_check_has_fixed_schema_and_does_not_read_stdin(self) -> None:
        """Catch self-check consuming source input or omitting a fixed boundary diagnostic."""

        values = {
            "/sys/fs/cgroup/memory.max": b"2147483648\n",
            "/sys/fs/cgroup/memory.swap.max": b"2147483648\n",
            "/sys/fs/cgroup/pids.max": b"64\n",
            "/sys/fs/cgroup/cpu.max": b"200000 100000\n",
        }
        opened: list[str] = []

        def fixed_open(path: Path, mode: str = "r", **_kwargs: object) -> io.BytesIO:
            self.assertEqual("rb", mode)
            guest_path = path.as_posix()
            opened.append(guest_path)
            return io.BytesIO(values[guest_path])

        with (
            mock.patch.object(source_pdf_host, "limit_worker", return_value=0) as limit,
            mock.patch.object(Path, "open", autospec=True, side_effect=fixed_open),
            mock.patch.object(os, "getuid", return_value=65532, create=True),
        ):
            status, output = _invoke(["tedd-reader-probe", "self-check"], stdin=_UnreadableInput())

        result = _decoded(output)
        self.assertEqual(0, status)
        self.assertEqual(
            {
                "status",
                "uid",
                "architecture",
                "python_version",
                "memory.max",
                "memory.swap.max",
                "pids.max",
                "cpu.max",
            },
            set(result),
        )
        self.assertEqual("self_check", result["status"])
        self.assertEqual(65532, result["uid"])
        self.assertIsInstance(result["architecture"], str)
        self.assertIsInstance(result["python_version"], str)
        for path, raw in values.items():
            self.assertEqual(raw.decode("ascii").strip(), result[path.rsplit("/", 1)[-1]])
        self.assertEqual(list(values), opened)
        limit.assert_called_once_with(512 * 1024**2, 30)

    def test_self_check_fails_closed_when_a_cgroup_value_is_unavailable(self) -> None:
        """Catch incomplete diagnostics being reported as a successful self-check."""

        marker = "DO_NOT_ECHO_cgroup_18ad"
        with (
            mock.patch.object(source_pdf_host, "limit_worker", return_value=0),
            mock.patch.object(Path, "open", side_effect=OSError(marker)),
            mock.patch.object(os, "getuid", return_value=65532, create=True),
        ):
            status, output = _invoke(["tedd-reader-probe", "self-check"], stdin=_UnreadableInput())

        self.assertEqual(1, status)
        self.assertEqual({"status": "capability_unavailable"}, _decoded(output))
        self.assertNotIn(marker.encode(), output)


if __name__ == "__main__":
    unittest.main()
