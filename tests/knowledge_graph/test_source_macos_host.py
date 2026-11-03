"""Unqualified macOS workers fail before touching source bytes or native tools."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from unittest import mock

from tests.support.paths import GRAPH_SCRIPTS


class OtherWorkerBoundaryTests(unittest.TestCase):
    def test_non_macos_worker_qualification_is_unchanged(self) -> None:
        from tedd_knowledge_graph import source_macos_host

        for platform in ("linux", "win32", "freebsd14"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                source_macos_host.require_worker_boundary()


@unittest.skipUnless(sys.platform == "darwin", "Actual macOS process boundary")
class MacOSWorkerBoundaryTests(unittest.TestCase):
    def child(self, body: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import sys; sys.path.insert(0, sys.argv.pop()); " + body,
                str(GRAPH_SCRIPTS),
            ],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def test_cli_host_routes_refuse_before_worker_launch_or_office_staging(self) -> None:
        for expression in (
            "sources.host_reading_response(b'private synthetic',sys.executable,'source_pdf_host.py')",
            "sources.host_reading_response(b'private synthetic',sys.executable,"
            "'source_office_host.py','docx')",
            "sources.host_reading_response(b'private synthetic',sys.executable,"
            "'source_office_host.py','image')",
            "sources._render_pdf_png(b'private synthetic',1,Path(sys.executable),Path(sys.executable))",
            "sources._office_document_page(None,argparse.Namespace(reader_python=sys.executable,"
            "renderer=sys.executable,office_converter=sys.executable,office_work_root='unused',"
            "config='unused'),b'private synthetic','docx',None)",
        ):
            with self.subTest(expression=expression):
                result = self.child(
                    "import argparse,json,subprocess; from pathlib import Path; "
                    "from unittest.mock import patch; import tedd_sources as sources\n"
                    "with patch.object(subprocess,'run',side_effect=AssertionError('worker launch')), "
                    "patch.object(subprocess,'Popen',side_effect=AssertionError('worker launch')), "
                    "patch.object(sources,'load_config',side_effect=AssertionError('config read')):\n"
                    " try:\n"
                    f"  {expression}\n"
                    " except sources.SourceArchiveError as error:\n"
                    "  print(json.dumps({'status':error.code,'reason':getattr(error,'reason',None)}))"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout),
                    {
                        "status": "capability_unavailable",
                        "reason": "macos_worker_boundary_unqualified",
                    },
                )

    def test_cli_serializes_only_the_fixed_capability_diagnostic(self) -> None:
        result = self.child(
            "from unittest.mock import patch; import tedd_sources as sources; "
            "from tedd_knowledge_graph.source_macos_host import MacOSWorkerUnavailable\n"
            "with patch.object(sources,'execute_cli',side_effect=MacOSWorkerUnavailable()):\n"
            " raise SystemExit(sources.main(['--config','unused','status']))"
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "status": "capability_unavailable",
                "reason": "macos_worker_boundary_unqualified",
            },
        )

    def test_private_viewer_entrypoint_exits_without_input_or_diagnostics(self) -> None:
        result = self.child(
            "import runpy; from tedd_knowledge_graph import source_viewer\n"
            "class UnreadableInput:\n"
            " @property\n"
            " def buffer(self):\n"
            "  print('private input accessed',flush=True)\n"
            "  raise AssertionError\n"
            "sys.stdin=UnreadableInput()\n"
            "sys.argv=[source_viewer.__file__,'--worker']\n"
            "runpy.run_path(source_viewer.__file__,run_name='__main__')"
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_direct_office_routes_refuse_before_parsing_or_private_staging(self) -> None:
        for expression in (
            "office.document_page(b'private synthetic','docx',1,reader=executable,"
            "converter=executable,renderer=executable,work_root=Path('/synthetic-unused'))",
            "office.render_document(b'private synthetic','docx',1,executable,executable,"
            "Path('/synthetic-unused'))",
        ):
            with self.subTest(expression=expression):
                result = self.child(
                    "import json,subprocess; from pathlib import Path; "
                    "from unittest.mock import patch; "
                    "from tedd_knowledge_graph import source_office_host as office,"
                    "source_files,source_office; "
                    "from tedd_knowledge_graph.source_contracts import SourceArchiveError; "
                    "executable=Path(sys.executable)\n"
                    "with patch.object(source_files,'private_root',"
                    "side_effect=AssertionError('private staging')), "
                    "patch.object(source_office,'read_office',"
                    "side_effect=AssertionError('private input parsed')), "
                    "patch.object(subprocess,'Popen',side_effect=AssertionError('worker launch')):\n"
                    " try:\n"
                    f"  {expression}\n"
                    " except SourceArchiveError as error:\n"
                    "  print(json.dumps({'status':error.code,'reason':getattr(error,'reason',None)}))"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout),
                    {
                        "status": "capability_unavailable",
                        "reason": "macos_worker_boundary_unqualified",
                    },
                )

    def test_mcp_serializes_typed_capability_reason_but_not_other_error_details(self) -> None:
        result = self.child(
            "import json; from unittest.mock import patch; "
            "from tedd_knowledge_graph.source_macos_host import MacOSWorkerUnavailable; "
            "from tedd_knowledge_graph.source_mcp import SourceMcpServer,SourceTurn; "
            "from tedd_knowledge_graph.source_contracts import SourceArchiveError\n"
            "for error in (MacOSWorkerUnavailable(),SourceArchiveError('privacy_rejected')):\n"
            " def operation(*args):\n  raise error\n"
            " server=SourceMcpServer(operation)\n"
            " try:\n"
            '  server.dispatch_line(b\'{"jsonrpc":"2.0","id":0,"method":"initialize",\''
            'b\'"params":{"protocolVersion":"2024-11-05"}}\')\n'
            "  with patch.object(server.lifecycle,'acquire',return_value=SourceTurn()):\n"
            '   response=server.dispatch_line(b\'{"jsonrpc":"2.0","id":1,"method":"tools/call",\''
            'b\'"params":{"name":"intake_file","arguments":{"argv":[]},"_meta":{}}}\')\n'
            "  print(json.dumps(response['result']))\n"
            " finally:\n  server.close()"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        for response in responses:
            self.assertTrue(response["isError"])
        self.assertEqual(
            responses[0]["structuredContent"],
            {
                "status": "capability_unavailable",
                "reason": "macos_worker_boundary_unqualified",
            },
        )
        self.assertEqual(responses[1]["structuredContent"], {"status": "privacy_rejected"})

    def test_worker_refuses_with_typed_reason_without_mutating_inherited_limits(self) -> None:
        result = self.child(
            "import json,resource; "
            "from tedd_knowledge_graph.source_pdf_host import limit_worker; "
            "resources=(resource.RLIMIT_CORE,resource.RLIMIT_CPU,resource.RLIMIT_AS); "
            "before=[resource.getrlimit(item) for item in resources]\n"
            "try:\n limit_worker(512*1024**2,30)\n"
            "except (OSError,ValueError) as error:\n"
            " print(json.dumps({'type':type(error).__name__,"
            "'status':getattr(error,'code',None),'reason':getattr(error,'reason',None),"
            "'unchanged':before==[resource.getrlimit(item) for item in resources]}))"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "type": "MacOSWorkerUnavailable",
                "status": "capability_unavailable",
                "reason": "macos_worker_boundary_unqualified",
                "unchanged": True,
            },
        )

    def test_pdf_office_and_image_workers_refuse_before_reading_input(self) -> None:
        for module, arguments in (
            ("source_pdf_host", ["worker"]),
            ("source_office_host", ["worker", "docx"]),
            ("source_office_host", ["worker", "image"]),
        ):
            with self.subTest(module=module, arguments=arguments):
                result = self.child(
                    f"from tedd_knowledge_graph import {module} as host; "
                    f"sys.argv={arguments!r}\n"
                    "class UnreadableInput:\n"
                    " @property\n"
                    " def buffer(self):\n"
                    "  raise AssertionError('synthetic private input was accessed')\n"
                    "sys.stdin=UnreadableInput()\n"
                    "raise SystemExit(host.main())"
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout),
                    {
                        "status": "capability_unavailable",
                        "reason": "macos_worker_boundary_unqualified",
                    },
                )
                self.assertEqual(result.stderr, "")

    def test_native_helper_refuses_before_opening_pins_or_launching(self) -> None:
        result = self.child(
            "import json,subprocess; from pathlib import Path; from unittest.mock import patch; "
            "from tedd_knowledge_graph import source_native_host as native\n"
            "with patch.object(native,'pinned_helper_files',"
            "side_effect=AssertionError('helper opened')), "
            "patch.object(subprocess,'run',side_effect=AssertionError('native launched')):\n"
            " try:\n"
            "  native.run_native_helper(['/synthetic-unused'],b'synthetic private source',"
            "((Path('/synthetic-unused'),'0'*64),),None)\n"
            " except native.SourceArchiveError as error:\n"
            "  print(json.dumps({'status':error.code,'reason':getattr(error,'reason',None)}))"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "status": "capability_unavailable",
                "reason": "macos_worker_boundary_unqualified",
            },
        )
        self.assertNotIn("synthetic private source", result.stdout + result.stderr)

    def test_direct_image_worker_wrapper_refuses_before_launch(self) -> None:
        result = self.child(
            "import json,subprocess; from unittest.mock import patch; "
            "from tedd_knowledge_graph import source_native_host as native\n"
            "with patch.object(subprocess,'Popen',side_effect=AssertionError('worker launch')):\n"
            " try:\n"
            "  native.run_image_worker([sys.executable,'unused'],b'private synthetic')\n"
            " except native.SourceArchiveError as error:\n"
            "  print(json.dumps({'status':error.code,'reason':getattr(error,'reason',None)}))"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "status": "capability_unavailable",
                "reason": "macos_worker_boundary_unqualified",
            },
        )

    def test_pdf_render_refuses_before_native_launch(self) -> None:
        result = self.child(
            "import json,subprocess; from pathlib import Path; from unittest.mock import patch; "
            "from tedd_knowledge_graph.source_pdf_host import render\n"
            "with patch.object(subprocess,'run',side_effect=AssertionError('native launched')):\n"
            " print(json.dumps(render(b'%PDF-synthetic private source',1,Path(sys.executable))))"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "status": "capability_unavailable",
                "reason": "macos_worker_boundary_unqualified",
            },
        )

    def test_native_command_allocation_cannot_bypass_the_gate(self) -> None:
        result = self.child(
            "import json; from tedd_knowledge_graph import source_native_host as native\n"
            "try:\n"
            " with native.native_child_arguments(['unused'],None):\n"
            "  raise AssertionError('unqualified native command was admitted')\n"
            "except native.SourceArchiveError as error:\n"
            " print(json.dumps({'status':error.code,'reason':getattr(error,'reason',None)}))"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "status": "capability_unavailable",
                "reason": "macos_worker_boundary_unqualified",
            },
        )


if __name__ == "__main__":
    unittest.main()
