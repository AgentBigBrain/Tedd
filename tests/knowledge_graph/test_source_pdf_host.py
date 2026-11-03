"""Resource boundaries in disposable child processes, not this test runner."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support.paths import GRAPH_SCRIPTS


class PdfHostLimitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scripts = GRAPH_SCRIPTS

    @unittest.skipUnless(
        sys.platform == "linux" and os.environ.get("TEDD_TEST_DUMPABILITY_PROBE"),
        "Dedicated Linux non-crashing native probe",
    )
    def test_sealed_native_launch_is_not_dumpable_and_closes_copy_descriptors(self) -> None:
        from tedd_knowledge_graph import source_native_host as native

        path = Path(os.environ["TEDD_TEST_DUMPABILITY_PROBE"])
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        created: list[int] = []
        factory = getattr(os, "memfd_create", None)
        assert callable(factory)

        def observed(name: str, flags: int) -> int:
            fd = factory(name, flags)
            assert isinstance(fd, int)
            created.append(fd)
            return fd

        with mock.patch.object(os, "memfd_create", side_effect=observed):
            result = native.run_native_helper([str(path)], b"", ((path, expected),), None)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual(len(created), 1)
        with self.assertRaises(OSError):
            os.fstat(created[0])

        for failure in (subprocess.TimeoutExpired("probe", 1), OSError("synthetic launch failure")):
            created.clear()
            with (
                self.subTest(failure=type(failure).__name__),
                mock.patch.object(os, "memfd_create", side_effect=observed),
                mock.patch.object(subprocess, "run", side_effect=failure),
                self.assertRaises(type(failure)),
            ):
                native.run_native_helper([str(path)], b"", ((path, expected),), None)
            self.assertEqual(len(created), 1)
            with self.assertRaises(OSError):
                os.fstat(created[0])

        fcntl = importlib.import_module("fcntl")
        for fault in (
            mock.patch.object(os, "read", return_value=b"x" * 1024**2),
            mock.patch.object(os, "write", return_value=0),
            mock.patch.object(fcntl, "fcntl", side_effect=OSError("synthetic seal failure")),
        ):
            created.clear()
            with (
                fault,
                mock.patch.object(os, "memfd_create", side_effect=observed),
                mock.patch.object(subprocess, "run") as launch,
                self.assertRaises((native.SourceArchiveError, OSError)),
            ):
                native.run_native_helper([str(path)], b"", ((path, expected),), None)
            launch.assert_not_called()
            self.assertEqual(len(created), 1)
            with self.assertRaises(OSError):
                os.fstat(created[0])

    @unittest.skipUnless(
        sys.platform == "linux" and os.environ.get("TEDD_TEST_DUMPABILITY_PROBE"),
        "Dedicated Linux non-crashing native probe",
    )
    def test_sealed_copy_preserves_bytes_and_refuses_mutation_or_read_bypass(self) -> None:
        from tedd_knowledge_graph import source_native_host as native

        path = Path(os.environ["TEDD_TEST_DUMPABILITY_PROBE"])
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        with native.pinned_helper_files(((path, expected),)) as descriptors:
            with self.assertRaises(native.SourceArchiveError):
                with native.sealed_linux_executable(descriptors[0], "0" * 64):
                    self.fail("changed helper bytes must refuse")
            with native.sealed_linux_executable(descriptors[0], expected) as fd:
                self.assertEqual(os.fstat(fd).st_mode & 0o777, 0o100)
                self.assertFalse(os.access(f"/proc/self/fd/{fd}", os.R_OK, effective_ids=True))
                with self.assertRaises(OSError):
                    os.write(fd, b"changed")
                with self.assertRaises(OSError):
                    os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                self.assertEqual(hashlib.sha256(os.read(fd, 32 * 1024**2)).hexdigest(), expected)
            with (
                mock.patch.object(os, "access", return_value=True) as access,
                mock.patch.object(os, "supports_effective_ids", {access}),
                self.assertRaises(native.SourceArchiveError),
            ):
                with native.sealed_linux_executable(descriptors[0], expected):
                    self.fail("read-bypass credentials must refuse")

    @unittest.skipUnless(sys.platform == "linux", "Linux dump-policy prerequisite")
    def test_linux_native_policy_refuses_unknown_or_nonzero_kernel_dump_policy(self) -> None:
        from tedd_knowledge_graph import source_native_host as native

        for value in (b"1\n", b"2\n", b"", b"0\nx"):
            with (
                self.subTest(policy=value),
                mock.patch.object(Path, "open", return_value=io.BytesIO(value)),
                self.assertRaises(native.SourceArchiveError),
            ):
                native.require_linux_exec_privacy()
        with (
            mock.patch.object(os, "geteuid", return_value=0),
            self.assertRaises(native.SourceArchiveError),
        ):
            native.require_linux_exec_privacy()

    @unittest.skipUnless(sys.platform == "linux", "Linux per-worker crash privacy")
    def test_worker_disables_inherited_core_dumps_before_source_processing(self) -> None:
        result = self.child(
            "import ctypes,json,resource; "
            "libc=ctypes.CDLL(None,use_errno=True); "
            "libc.prctl.argtypes=[ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong]; "
            "libc.prctl.restype=ctypes.c_int; "
            "assert libc.prctl(4,1,0,0,0)==0; "
            "resource.setrlimit(resource.RLIMIT_CORE,(1024**2,resource.getrlimit(resource.RLIMIT_CORE)[1])); "
            "from tedd_knowledge_graph.source_pdf_host import limit_worker; "
            "limit_worker(64*1024**2,5); "
            "print(json.dumps({'core':resource.getrlimit(resource.RLIMIT_CORE),'dumpable':libc.prctl(3,0,0,0,0)}))"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"core": [0, 0], "dumpable": 0})

    @unittest.skipUnless(sys.platform == "linux", "Linux allocation uses native rlimits")
    def test_native_phase_allocates_memory_and_cpu_without_resetting_the_budget(self) -> None:
        result = self.child(
            "import json,resource,time; "
            "from tedd_knowledge_graph.source_pdf_host import limit_worker; "
            "from tedd_knowledge_graph.source_native_host import native_child_arguments; "
            "limit_worker(2 * 1024**3,30); "
            "probe = 'import json,resource; print(json.dumps([resource.getrlimit(resource.RLIMIT_AS),resource.getrlimit(resource.RLIMIT_CPU)]))'; "
            "\nfor _ in range(2):\n"
            " with native_child_arguments([sys.executable,'-I','-B','-c',probe], time.monotonic()+10, reserved_memory=32*1024**2) as command:\n"
            "  parent = [resource.getrlimit(resource.RLIMIT_AS),resource.getrlimit(resource.RLIMIT_CPU)]\n"
            "  import subprocess\n"
            "  native = subprocess.run(command, capture_output=True, text=True, timeout=5, check=True)\n"
            "  print(json.dumps({'parent':parent,'child':json.loads(native.stdout)}))\n"
            "print(json.dumps({'after':[resource.getrlimit(resource.RLIMIT_AS),resource.getrlimit(resource.RLIMIT_CPU)]}))"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        first, second, restored = (json.loads(line) for line in result.stdout.splitlines())
        for phase in (first, second):
            self.assertEqual(phase["parent"][0], [512 * 1024**2, 2 * 1024**3])
            self.assertEqual(phase["child"][0], [1504 * 1024**2] * 2)
            self.assertEqual(
                phase["parent"][0][0] + phase["child"][0][1] + 32 * 1024**2, 2 * 1024**3
            )
            self.assertEqual(phase["parent"][1], [20, 20])
            self.assertLessEqual(phase["parent"][1][1] + phase["child"][1][1], 30)
        self.assertLess(second["child"][1][1], first["child"][1][1])
        self.assertEqual(restored["after"][0], [2 * 1024**3] * 2)
        self.assertEqual(restored["after"][1], [20, 20])

    @unittest.skipUnless(sys.platform == "linux", "Linux allocation uses native rlimits")
    def test_native_phase_refuses_existing_parent_memory_above_its_allocation(self) -> None:
        result = self.child(
            "import mmap,time; "
            "from tedd_knowledge_graph.source_pdf_host import limit_worker; "
            "from tedd_knowledge_graph.source_native_host import native_child_arguments; "
            "from tedd_knowledge_graph.source_contracts import SourceArchiveError; "
            "limit_worker(2*1024**3,30); allocation=mmap.mmap(-1,600*1024**2)\n"
            "try:\n"
            " with native_child_arguments(['unused'],time.monotonic()+10):\n"
            "  print('unbounded')\n"
            "except SourceArchiveError as error:\n"
            " print(error.code)\n"
            "finally:\n"
            " allocation.close()"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "limit_exceeded")

    @unittest.skipUnless(sys.platform == "linux", "Linux process-group lifetime")
    def test_image_worker_timeout_terminates_its_descendant_process(self) -> None:
        from tedd_knowledge_graph.source_native_host import run_image_worker

        body = (
            "import os,subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-I','-B','-c',"
            "'import os; print(os.getpid(),flush=True); os.read(0,1)'],stdin=subprocess.PIPE); "
            "child.wait()"
        )
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            run_image_worker([sys.executable, "-I", "-B", "-c", body], b"", timeout=2)
        descendant = int(caught.exception.output.strip())
        self.assertNotEqual(descendant, os.getpid())
        self.assert_descendant_stopped(descendant)

    def assert_descendant_stopped(self, descendant: int) -> None:
        # A reparented zombie may await the host's reaper; it cannot execute or
        # retain the source pipe. SIGKILL is asynchronous, so allow its delivery
        # to settle while checking only this synthetic child's exact PID.
        deadline = time.monotonic() + 1
        while True:
            try:
                state = Path(f"/proc/{descendant}/stat").read_text().split(") ", 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                state = "gone"
            if state in ("gone", "Z") or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        self.assertIn(state, ("gone", "Z"))

    def test_descendant_probe_handles_disappearance_but_rejects_live_processes(self) -> None:
        for error in (FileNotFoundError(), ProcessLookupError()):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(Path, "read_text", side_effect=error):
                    self.assert_descendant_stopped(123)
        with mock.patch.object(
            Path, "read_text", side_effect=("123 (synthetic) R 1", "123 (synthetic) Z 1")
        ):
            self.assert_descendant_stopped(123)
        with (
            mock.patch.object(Path, "read_text", return_value="123 (synthetic) S 1"),
            mock.patch.object(time, "monotonic", side_effect=(0, 1)),
        ):
            with self.assertRaises(AssertionError):
                self.assert_descendant_stopped(123)
        with mock.patch.object(Path, "read_text", side_effect=PermissionError()):
            with self.assertRaises(PermissionError):
                self.assert_descendant_stopped(123)

    def test_native_helpers_share_the_remaining_operation_deadline(self) -> None:
        from tedd_knowledge_graph import source_native_host

        with mock.patch.object(
            source_native_host.time, "monotonic", side_effect=(100.0, 127.5, 130.0)
        ):
            self.assertEqual(source_native_host.remaining_native_seconds(130.0), 25.0)
            self.assertEqual(source_native_host.remaining_native_seconds(130.0), 2.5)
            with self.assertRaisesRegex(source_native_host.SourceArchiveError, "limit_exceeded"):
                source_native_host.remaining_native_seconds(130.0)

    def test_native_deadline_refuses_nonfinite_and_non_numeric_values(self) -> None:
        from tedd_knowledge_graph import source_native_host

        for value in (float("inf"), float("nan"), True, "130"):
            with (
                self.subTest(value=value),
                self.assertRaises(source_native_host.SourceArchiveError),
            ):
                source_native_host.remaining_native_seconds(value)

    def test_image_worker_passes_one_deadline_and_refuses_a_late_result(self) -> None:
        body = (
            "from unittest.mock import patch; "
            "from tedd_knowledge_graph import source_office_host as host; "
            "from tedd_knowledge_graph import source_image_host; "
            "sys.argv = ['worker', 'image', '--svg-renderer', 'unused-svg', "
            "'--svg-font', 'unused-font', '--av1-validator', 'unused-av1']; "
            "\ndef result(*args, **kwargs):\n"
            " assert kwargs['svg'].deadline == 130.0\n"
            " assert kwargs['av1'].deadline == 130.0\n"
            " return {'status': 'text_screened', 'private': 'must not escape'}\n"
            "with patch('tedd_knowledge_graph.source_pdf_host.limit_worker', return_value=0), "
            "patch.object(host.time, 'monotonic', side_effect=(100.0, 131.0)), "
            "patch.object(source_image_host, 'image_response', side_effect=result):\n"
            " host.main()"
        )
        result = self.child(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"status": "limit_exceeded"})
        self.assertNotIn("must not escape", result.stdout + result.stderr)

    def child(self, body: str, *, timeout: float = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); " + body,
                str(self.scripts),
            ],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    @unittest.skipIf(sys.platform == "darwin", "macOS worker boundary is explicitly unqualified")
    def test_native_memory_limit_refuses_large_allocation_before_parsing(self) -> None:
        result = self.child(
            "from tedd_knowledge_graph.source_pdf_host import limit_worker; "
            "limit_worker(64 * 1024**2, 5)\n"
            "try:\n value = bytearray(256 * 1024**2)\n print('unbounded')"
            "\nexcept MemoryError:\n print('bounded')"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "bounded")

    def test_missing_optional_pdf_tools_is_a_content_free_capability_result(self) -> None:
        result = self.child(
            "from unittest.mock import patch; "
            "from tedd_knowledge_graph.source_pdf_host import extract\n"
            "with patch('importlib.import_module', side_effect=ModuleNotFoundError):"
            "\n print(extract(b'%PDF-neutral')['status'])"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "capability_unavailable")

    def test_malformed_input_is_never_returned_in_worker_errors(self) -> None:
        worker = self.scripts / "tedd_knowledge_graph" / "source_pdf_host.py"
        payload = b"not a PDF; synthetic private input"
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(worker)],
            input=payload,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertNotIn(payload, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(
            json.loads(result.stdout),
            {"status": "capability_unavailable", "reason": "macos_worker_boundary_unqualified"}
            if sys.platform == "darwin"
            else {"status": "source_corrupt"},
        )

    @unittest.skipIf(sys.platform == "darwin", "macOS worker boundary is explicitly unqualified")
    @unittest.skipUnless(
        importlib.util.find_spec("pypdf") and importlib.util.find_spec("pdfplumber"),
        "Optional PDF reader is unavailable",
    )
    def test_known_pdf_parse_failure_is_not_a_missing_host_capability(self) -> None:
        worker = self.scripts / "tedd_knowledge_graph" / "source_pdf_host.py"
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(worker)],
            input=b"%PDF-1.7\nSYNTHETIC-UNPARSEABLE\n%%EOF\n",
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(json.loads(result.stdout), {"status": "source_corrupt"})
        self.assertNotIn(b"SYNTHETIC-UNPARSEABLE", result.stdout + result.stderr)

    def test_worker_json_is_utf8_even_with_windows_legacy_console_encoding(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv.pop()); "
                "from unittest.mock import patch; "
                "from tedd_knowledge_graph import source_pdf_host as host; "
                "sys.stdout.reconfigure(encoding='cp1252'); "
                "sys.argv = ['worker']; "
                "\nwith patch.object(host, 'limit_worker', return_value=0), "
                "patch.object(host, 'extract', return_value="
                "{'status':'text_screened','text':'not approved — staging only'}):"
                "\n host.main()",
                str(self.scripts),
            ],
            input=b"",
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["text"], "not approved — staging only")

    def test_render_refuses_invalid_page_or_unknown_renderer_without_launch(self) -> None:
        from tedd_knowledge_graph import source_pdf_host

        with mock.patch.object(subprocess, "run") as launch:
            self.assertEqual(
                source_pdf_host.render(b"%PDF-neutral", 0, Path(sys.executable))["status"],
                "limit_exceeded",
            )
            self.assertEqual(
                source_pdf_host.render(b"%PDF-neutral", 1, Path("unknown"))["status"],
                "capability_unavailable",
            )
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
