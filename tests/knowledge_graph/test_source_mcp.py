"""Source lifecycle authority comes from the Codex host, never source arguments."""

from __future__ import annotations

import base64
import io
import json
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tedd_knowledge_graph.source_contracts import SourceArchiveError
from tedd_knowledge_graph.source_mcp import CodexSourceLifecycle, SourceTurn
from tedd_knowledge_graph.v4_application_contracts import JsonObject

from tests.support.paths import GRAPH_SCRIPTS


class SourceEndpointStartupTests(unittest.TestCase):
    def test_mac_cold_factory_supplies_matching_profile_authority(self) -> None:
        import tedd_sources_mcp

        runtime = mock.Mock()
        authority = mock.Mock()
        cold = mock.Mock(runtime=runtime)
        cold.profile_authority.return_value = authority
        with (
            mock.patch.object(
                tedd_sources_mcp, "load_mac_cold_entitlement", return_value=cold
            ) as load,
            mock.patch.object(tedd_sources_mcp, "ReaderCoordinator") as coordinator,
        ):
            result = tedd_sources_mcp.load_mac_cold_reader(Path("private-cold.json"))

        load.assert_called_once_with(Path("private-cold.json"))
        coordinator.assert_called_once_with(runtime, cold=cold, profile_owners=authority)
        self.assertIs(result, coordinator.return_value)

    def test_mac_cold_config_is_startup_only_and_passed_as_reader(self) -> None:
        import tedd_sources
        import tedd_sources_mcp

        reader = mock.Mock()
        with (
            mock.patch.object(
                tedd_sources_mcp, "load_mac_cold_reader", return_value=reader
            ) as load,
            mock.patch.object(tedd_sources_mcp, "SourceOperations") as operations,
            mock.patch.object(
                tedd_sources_mcp, "stdio_streams", return_value=(mock.Mock(), mock.Mock())
            ),
            mock.patch.object(tedd_sources_mcp, "serve", return_value=0),
        ):
            self.assertEqual(
                tedd_sources_mcp.main(
                    ["--config", "neutral.json", "--mac-reader-cold-config", "private-cold.json"]
                ),
                0,
            )

        load.assert_called_once_with(Path("private-cold.json"))
        self.assertIs(operations.call_args.kwargs["reader"], reader)
        with self.assertRaises(SourceArchiveError):
            tedd_sources.parser().parse_args(["--mac-reader-cold-config", "private-cold.json"])

    def test_invalid_mac_cold_config_fails_before_serve(self) -> None:
        import tedd_sources_mcp

        with (
            mock.patch.object(
                tedd_sources_mcp,
                "load_mac_cold_reader",
                side_effect=SourceArchiveError("source_unavailable"),
            ),
            mock.patch.object(tedd_sources_mcp, "SourceOperations") as operations,
            mock.patch.object(tedd_sources_mcp, "serve") as serve,
            self.assertRaises(SystemExit) as raised,
        ):
            tedd_sources_mcp.main(
                ["--config", "neutral.json", "--mac-reader-cold-config", "invalid.json"]
            )

        self.assertEqual(raised.exception.code, 2)
        operations.assert_not_called()
        serve.assert_not_called()

    def test_mac_cold_config_rejects_warm_and_legacy_reader_options(self) -> None:
        import tedd_sources_mcp

        alternatives = (
            ("--mac-reader-runtime-config", "warm.json"),
            ("--reader-python", "python"),
            ("--renderer", "renderer"),
            ("--svg-renderer", "resvg"),
            ("--svg-font", "font"),
            ("--av1-validator", "av1"),
            ("--office-converter", "office"),
            ("--office-work-root", "work"),
        )
        for option in alternatives:
            with self.subTest(option=option), self.assertRaises(SystemExit):
                tedd_sources_mcp.main(
                    ["--config", "neutral.json", "--mac-reader-cold-config", "cold.json", *option]
                )

    def test_mac_runtime_factory_binds_the_loaded_runtime(self) -> None:
        import tedd_sources_mcp

        runtime = mock.Mock()
        binding = mock.Mock(runtime=runtime)
        with (
            mock.patch.object(
                tedd_sources_mcp, "load_mac_runtime_binding", return_value=binding
            ) as load,
            mock.patch.object(tedd_sources_mcp, "ReaderCoordinator") as coordinator,
        ):
            result = tedd_sources_mcp.load_mac_reader(Path("private-runtime.json"))

        load.assert_called_once_with(Path("private-runtime.json"))
        coordinator.assert_called_once_with(runtime, binding=binding)
        self.assertIs(result, coordinator.return_value)

    def test_mac_runtime_config_is_startup_only_and_passed_as_reader(self) -> None:
        import tedd_sources
        import tedd_sources_mcp

        reader = mock.Mock()
        streams = (mock.Mock(), mock.Mock())
        with (
            mock.patch.object(tedd_sources_mcp, "load_mac_reader", return_value=reader) as load,
            mock.patch.object(tedd_sources_mcp, "SourceOperations") as operations,
            mock.patch.object(tedd_sources_mcp, "stdio_streams", return_value=streams),
            mock.patch.object(tedd_sources_mcp, "serve", return_value=0),
        ):
            self.assertEqual(
                tedd_sources_mcp.main(
                    [
                        "--config",
                        "neutral.json",
                        "--mac-reader-runtime-config",
                        "private-runtime.json",
                    ]
                ),
                0,
            )

        load.assert_called_once_with(Path("private-runtime.json"))
        self.assertIs(operations.call_args.kwargs["reader"], reader)
        with self.assertRaises(SourceArchiveError):
            tedd_sources.parser().parse_args(
                ["--mac-reader-runtime-config", "private-runtime.json"]
            )

    def test_invalid_mac_runtime_config_fails_before_serve_without_fallback(self) -> None:
        import tedd_sources_mcp

        with (
            mock.patch.object(
                tedd_sources_mcp,
                "load_mac_reader",
                side_effect=SourceArchiveError("source_unavailable"),
            ),
            mock.patch.object(tedd_sources_mcp, "SourceOperations") as operations,
            mock.patch.object(tedd_sources_mcp, "serve") as serve,
            self.assertRaises(SystemExit) as raised,
        ):
            tedd_sources_mcp.main(
                [
                    "--config",
                    "neutral.json",
                    "--mac-reader-runtime-config",
                    "invalid.json",
                ]
            )

        self.assertEqual(raised.exception.code, 2)
        operations.assert_not_called()
        serve.assert_not_called()

    def test_mac_runtime_config_rejects_legacy_reader_options(self) -> None:
        import tedd_sources_mcp

        legacy = (
            ("--reader-python", "python"),
            ("--renderer", "renderer"),
            ("--svg-renderer", "resvg"),
            ("--svg-font", "font"),
            ("--av1-validator", "av1"),
            ("--office-converter", "office"),
            ("--office-work-root", "work"),
        )
        for option in legacy:
            with self.subTest(option=option), self.assertRaises(SystemExit):
                tedd_sources_mcp.main(
                    [
                        "--config",
                        "neutral.json",
                        "--mac-reader-runtime-config",
                        "private-runtime.json",
                        *option,
                    ]
                )

    def test_image_tools_advertise_an_executable_four_argument_rectangle(self) -> None:
        import tedd_sources
        from tedd_knowledge_graph.source_mcp import SourceMcpServer

        operation = mock.Mock()
        server = SourceMcpServer(operation)
        self.addCleanup(server.close)
        server.dispatch_line(
            b'{"jsonrpc":"2.0","id":0,"method":"initialize",'
            b'"params":{"protocolVersion":"2024-11-05"}}'
        )
        reply = server.dispatch_line(b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
        assert reply is not None
        result = reply["result"]
        assert isinstance(result, dict) and isinstance(result["tools"], list)
        example = '["--image-rectangle", "10", "20", "110", "220"]'
        image_tools = {"intake_file", "render_image", "owner_view"}
        found: set[str] = set()
        for tool in result["tools"]:
            assert isinstance(tool, dict)
            name, description = tool["name"], tool["description"]
            assert isinstance(name, str) and isinstance(description, str)
            if name in image_tools:
                found.add(name)
                with self.subTest(tool=name):
                    self.assertIn(example, description)
        self.assertEqual(found, image_tools)
        scope = [
            part
            for name in (
                "consumer-id",
                "producer-agent-id",
                "task-id",
                "producer-run-id",
                "subscription-id",
                "subject-id",
            )
            for part in ("--" + name, "neutral")
        ]
        argv = ["--config", "neutral.json", "render-image", "--source-uri", "neutral", *scope]
        parsed = tedd_sources.parser().parse_args([*argv, *json.loads(example)])
        self.assertEqual(parsed.image_rectangle, [10, 20, 110, 220])
        for invalid in (["10,20,110,220"], ["10", "20", "110"], ["x", "20", "110", "220"]):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(SourceArchiveError, "source_corrupt"),
            ):
                tedd_sources.parser().parse_args([*argv, "--image-rectangle", *invalid])
        operation.assert_not_called()

    def test_av1_binding_requires_a_host_reader_without_executing_the_helper(self) -> None:
        from tedd_sources_mcp import SourceOperations

        with self.assertRaises(SourceArchiveError):
            SourceOperations(Path("unused-config"), av1_validator=Path("validator"))
        with mock.patch("tedd_knowledge_graph.source_av1_host.subprocess.run") as process:
            operation = SourceOperations(
                Path("unused-config"),
                reader_python=Path(sys.executable),
                av1_validator=Path("validator"),
            )
            operation.close()
            process.assert_not_called()

    def test_svg_binding_requires_both_host_paths_and_a_reader(self) -> None:
        from tedd_sources_mcp import SourceOperations

        for renderer, font in (
            (Path("renderer"), None),
            (None, Path("font")),
            (Path("renderer"), Path("font")),
        ):
            with self.subTest(renderer=renderer, font=font), self.assertRaises(SourceArchiveError):
                SourceOperations(Path("unused-config"), svg_renderer=renderer, svg_font=font)
        with self.assertRaises(SourceArchiveError):
            SourceOperations(
                Path("unused-config"),
                reader_python=Path(sys.executable),
                svg_renderer=Path("renderer"),
            )
        # Binding does not read or execute a helper, nor initialize live state.
        with mock.patch("tedd_knowledge_graph.source_svg_host.subprocess.run") as process:
            operation = SourceOperations(
                Path("unused-config"),
                reader_python=Path(sys.executable),
                svg_renderer=Path("renderer"),
                svg_font=Path("font"),
            )
            operation.close()
            process.assert_not_called()

    def test_cli_google_controls_forward_only_operator_configuration_and_close(self) -> None:
        import tedd_sources
        import tedd_sources_mcp

        for action in ("disconnect-google", "reconnect-google"):
            with self.subTest(action=action):
                args = tedd_sources.parser().parse_args(
                    [
                        "--config",
                        "neutral-config.json",
                        action,
                        "--google-client",
                        "neutral-client.json",
                        "--google-account",
                        "owner@example.com",
                        "--google-credentials",
                        "neutral-root",
                        "--operation-id",
                        "a" * 64,
                    ]
                )
                with (
                    mock.patch(
                        "tedd_knowledge_graph.source_credentials.CredentialStore"
                    ) as credentials,
                    mock.patch.object(tedd_sources_mcp, "SourceOperations") as operations,
                ):
                    operations.return_value.control.return_value = {"status": "synthetic-control"}
                    self.assertEqual(
                        tedd_sources.execute_cli(args), {"status": "synthetic-control"}
                    )
                credentials.assert_called_once_with(Path("neutral-root"))
                self.assertEqual(
                    operations.return_value.control.call_args.args[:2],
                    (action.replace("-", "_"), "a" * 64),
                )
                operations.return_value.close.assert_called_once()
                with self.assertRaises(SourceArchiveError):
                    tedd_sources.parser().parse_args(
                        [
                            "--config",
                            "neutral-config.json",
                            action,
                            "--google-client",
                            "neutral-client.json",
                            "--google-account",
                            "owner@example.com",
                            "--google-credentials",
                            "neutral-root",
                            "--approved",
                        ]
                    )

    def initialize(self, *options: str) -> subprocess.CompletedProcess[bytes]:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "neutral-startup", "version": "1"},
            },
        }
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(GRAPH_SCRIPTS / "tedd_sources_mcp.py"),
                "--config",
                "unused-neutral-config.json",
                *options,
            ],
            input=(json.dumps(request) + "\n").encode("utf-8"),
            capture_output=True,
            timeout=10,
            check=False,
        )

    def test_endpoint_initializes_without_google_credentials_or_config_reads(self) -> None:
        result = self.initialize()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        reply = json.loads(result.stdout)
        self.assertEqual(reply["id"], 1)
        self.assertEqual(reply["result"]["serverInfo"]["name"], "tedd-sources")

    def test_partial_google_configuration_refuses_before_serving(self) -> None:
        for options in (
            ("--google-client", "unused-client.json"),
            ("--google-account", "owner@example.com"),
            ("--google-credentials", "unused-credentials"),
        ):
            with self.subTest(options=options):
                result = self.initialize(*options)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")

    def test_missing_mac_runtime_config_refuses_before_serving_without_traceback(self) -> None:
        result = self.initialize(
            "--mac-reader-runtime-config", "missing-private-runtime/reader-runtime.json"
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(b"Traceback", result.stderr)

    def test_configured_google_startup_does_not_open_sign_in(self) -> None:
        result = self.initialize(
            "--google-client", "unused-client.json", "--google-account", "owner@example.com"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["id"], 1)

    def test_office_reader_does_not_require_pdf_rendering_software(self) -> None:
        result = self.initialize("--reader-python", sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)


class SourceLifecycleTests(unittest.TestCase):
    def test_local_review_acknowledgment_does_not_inherit_prior_google_operation_fence(
        self,
    ) -> None:
        from contextlib import contextmanager

        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_mcp import SourceMcpServer
        from tedd_knowledge_graph.source_review import ReviewRendering

        from tests.knowledge_graph.test_source_office import png_image

        png = png_image()
        disabled = threading.Event()
        tokens: list[str] = []

        @contextmanager
        def provider_hold() -> Iterator[None]:
            if disabled.is_set():
                raise SourceArchiveError("reauthentication_required")
            yield

        def operation(command: str, _argv: list[str], turn: SourceTurn) -> JsonObject:
            if command == "lookup":
                turn.provider_hold = provider_hold
                return {"status": "synthetic_google_read"}
            rendering = ReviewRendering(
                "a" * 64, "b" * 64, "c" * 64, "u1", "model", digest(png), (1, 1), (1, 1)
            )
            token = turn.reviews.offer(rendering)
            tokens.append(token)
            return {
                "status": "source_review",
                "review_id": token,
                "png_base64": base64.b64encode(png).decode("ascii"),
                "png_sha256": digest(png),
            }

        def control(_name: str, _operation_id: str, _turn: SourceTurn) -> JsonObject:
            disabled.set()
            return {"status": "google_disconnected"}

        server = SourceMcpServer(operation, control=control)
        self.addCleanup(server.close)
        server.dispatch_line(
            b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}'
        )
        server.lifecycle.begin({"session_id": "thread", "turn_id": "turn"}, self.hook)
        replies: queue.Queue[JsonObject] = queue.Queue()

        def call(name: str, arguments: JsonObject) -> JsonObject:
            server.receive_line(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments, "_meta": self.call},
                    }
                ).encode(),
                replies.put,
            )
            response = replies.get(timeout=5)
            gate = server._control_busy if name == "disconnect_google" else server._busy
            self.assertTrue(gate.acquire(timeout=5))
            gate.release()
            result = response["result"]
            assert isinstance(result, dict)
            return result

        self.assertFalse(call("intake_file", {"argv": []}).get("isError", False))
        self.assertFalse(call("lookup", {"argv": []}).get("isError", False))
        self.assertFalse(call("disconnect_google", {}).get("isError", False))
        acknowledged = call("acknowledge_review", {"review_id": tokens[0]})
        self.assertFalse(acknowledged.get("isError", False), acknowledged)
        content = acknowledged["structuredContent"]
        assert isinstance(content, dict)
        self.assertEqual(content["stage"], "reviewed")

    def test_image_acknowledgment_requires_delivery_and_refuses_owner_impersonation(self) -> None:
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_mcp import SourceMcpServer
        from tedd_knowledge_graph.source_review import ReviewRendering

        from tests.knowledge_graph.test_source_office import png_image

        png = png_image()
        for audience in ("model", "owner"):
            with self.subTest(audience=audience):
                tokens: list[str] = []

                def operation(
                    command: str,
                    argv: list[str],
                    turn: SourceTurn,
                    *,
                    audience: str = audience,
                    tokens: list[str] = tokens,
                ) -> JsonObject:
                    rendering = ReviewRendering(
                        "a" * 64,
                        "b" * 64,
                        "c" * 64,
                        "u1",
                        audience,
                        digest(png),
                        (1, 1),
                        (1, 1),
                    )
                    token = turn.reviews.offer(rendering)
                    tokens.append(token)
                    return {
                        "status": "source_review",
                        "review_id": token,
                        "png_base64": base64.b64encode(png).decode("ascii"),
                        "png_sha256": digest(png),
                    }

                server = SourceMcpServer(operation)
                self.addCleanup(server.close)
                server.dispatch_line(
                    b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}'
                )
                server.lifecycle.begin({"session_id": "thread", "turn_id": "turn"}, self.hook)

                def request(name: str, arguments: JsonObject) -> bytes:
                    return json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {"name": name, "arguments": arguments, "_meta": self.call},
                        }
                    ).encode()

                render = request("intake_file", {"argv": []})
                prepared = server.dispatch_line(render)
                self.assertIsNotNone(prepared)
                self.assertTrue(tokens, prepared)
                # Building a valid response without transport is not delivery.
                pending = server.dispatch_line(
                    request("acknowledge_review", {"review_id": tokens[-1]})
                )
                assert pending is not None
                self.assertIn("result", pending)
                result = pending["result"]
                assert isinstance(result, dict)
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"], {"status": "scope_denied"})

                replies: queue.Queue[JsonObject] = queue.Queue()
                server.receive_line(render, replies.put)
                delivered = replies.get(timeout=5)
                # The callback runs inside the transport reservation. Wait for
                # that existing operation gate, not a timing-based sleep.
                self.assertTrue(server._busy.acquire(timeout=5))
                server._busy.release()
                acknowledged = server.dispatch_line(
                    request("acknowledge_review", {"review_id": tokens[-1]})
                )
                assert acknowledged is not None
                result = acknowledged["result"]
                assert isinstance(result, dict)
                if audience == "model":
                    value = result["structuredContent"]
                    assert isinstance(value, dict)
                    self.assertEqual(value["stage"], "reviewed")
                    self.assertEqual(value["coverage"], "overview")
                else:
                    self.assertTrue(result["isError"])
                    self.assertNotIn(base64.b64encode(png).decode(), json.dumps(delivered))
                forged = server.dispatch_line(
                    request("acknowledge_review", {"review_id": tokens[-1], "audience": "owner"})
                )
                assert forged is not None
                forged_result = forged["result"]
                assert isinstance(forged_result, dict)
                self.assertTrue(forged_result["isError"])

    def test_cancelled_turn_retires_review_acknowledgments(self) -> None:
        from tedd_knowledge_graph.source_review import ReviewRendering

        turn = SourceTurn()
        rendering = ReviewRendering(
            "a" * 64, "b" * 64, "c" * 64, "u1", "model", "d" * 64, (1, 1), (1, 1)
        )
        token = turn.reviews.offer(rendering)
        turn.reviews.delivered(token)
        turn.cancel()
        with self.assertRaises(SourceArchiveError):
            turn.reviews.acknowledge(token, audience="model")

    def test_review_token_without_image_or_with_another_digest_never_reaches_transport(
        self,
    ) -> None:
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_mcp import SourceMcpServer
        from tedd_knowledge_graph.source_review import ReviewRendering

        from tests.knowledge_graph.test_source_office import png_image

        for mode in ("no_image", "wrong_digest"):
            with self.subTest(mode=mode):

                def operation(
                    command: str,
                    argv: list[str],
                    turn: SourceTurn,
                    *,
                    mode: str = mode,
                ) -> JsonObject:
                    token = turn.reviews.offer(
                        ReviewRendering(
                            "a" * 64,
                            "b" * 64,
                            "c" * 64,
                            "u1",
                            "model",
                            "d" * 64,
                            (1, 1),
                            (1, 1),
                        )
                    )
                    payload: JsonObject = {"status": "source_review", "review_id": token}
                    if mode == "wrong_digest":
                        payload.update(
                            png_base64=base64.b64encode(png_image()).decode(),
                            png_sha256=digest(png_image()),
                        )
                    return payload

                server = SourceMcpServer(operation)
                self.addCleanup(server.close)
                server.dispatch_line(
                    b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}'
                )
                server.lifecycle.begin({"session_id": "thread", "turn_id": "turn"}, self.hook)
                response = server.dispatch_line(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "intake_file",
                                "arguments": {"argv": []},
                                "_meta": self.call,
                            },
                        }
                    ).encode()
                )
                assert response is not None
                result = response["result"]
                assert isinstance(result, dict)
                self.assertTrue(result.get("isError"))
                self.assertNotIn("review_id", json.dumps(result))

    def test_selected_image_is_an_mcp_image_not_a_base64_text_dump(self) -> None:
        from tedd_knowledge_graph.source_contracts import digest
        from tedd_knowledge_graph.source_mcp import SourceMcpServer

        from tests.knowledge_graph.test_source_office import png_image

        encoded = base64.b64encode(png_image()).decode("ascii")
        payload: JsonObject = {
            "status": "source_review",
            "png_base64": encoded,
            "png_sha256": digest(png_image()),
            "preserved": False,
        }
        server = SourceMcpServer(lambda command, argv, turn: payload)
        self.addCleanup(server.close)
        server.dispatch_line(
            b'{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}'
        )
        server.lifecycle.begin({"session_id": "thread", "turn_id": "turn"}, self.hook)

        def request() -> JsonObject:
            reply = server.dispatch_line(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "intake_file",
                            "arguments": {"argv": []},
                            "_meta": self.call,
                        },
                    }
                ).encode()
            )
            assert reply is not None
            result = reply["result"]
            assert isinstance(result, dict)
            return result

        result = request()
        self.assertNotIn(encoded, json.dumps(result["structuredContent"]))
        content = result["content"]
        assert isinstance(content, list)
        self.assertEqual(content[1], {"type": "image", "mimeType": "image/png", "data": encoded})
        self.assertIn("png_base64", payload)  # Do not mutate the operation's result.
        payload["png_sha256"] = "0" * 64
        rejected = request()
        self.assertTrue(rejected["isError"])
        self.assertNotIn(encoded, json.dumps(rejected))
        # Mock only the trusted-worker result: oversized output must be rejected
        # before transport without killing an otherwise usable source endpoint.
        from tedd_knowledge_graph.v4_mcp_contracts import MAX_RESPONSE_BYTES

        oversized = b"\x89PNG\r\n\x1a\n" + bytes(MAX_RESPONSE_BYTES)
        payload.update(
            png_base64=base64.b64encode(oversized).decode("ascii"), png_sha256=digest(oversized)
        )
        self.assertEqual(request()["structuredContent"], {"status": "limit_exceeded"})
        payload.clear()
        payload.update(status="ok")
        self.assertEqual(request()["structuredContent"], {"status": "ok"})

    def setUp(self) -> None:
        self.lifecycle = CodexSourceLifecycle()
        self.hook = {"threadId": "thread"}
        self.call = {
            "threadId": "thread",
            "callId": "call",
            "x-codex-turn-metadata": {
                "thread_id": "thread",
                "session_id": "thread",
                "turn_id": "turn",
            },
        }

    def test_untrusted_arguments_cannot_create_an_active_turn(self) -> None:
        with self.assertRaises(SourceArchiveError):
            self.lifecycle.acquire(self.call)
        with self.assertRaises(SourceArchiveError):
            self.lifecycle.begin({"session_id": "thread", "turn_id": "turn"}, self.call)
        with self.assertRaises(SourceArchiveError):
            self.lifecycle.begin({"session_id": "other", "turn_id": "turn"}, self.hook)

    def test_actual_user_paths_allow_selected_files_and_folder_children_not_quoted_sources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "selected"
            selected.mkdir()
            file = root / "brief.txt"
            file.write_bytes(b"Neutral condition")
            prompt = f"Use `{selected}` and `{file}`.\n> `{root}`\n```\n{root}\n```"
            self.lifecycle.link_sources(
                {"session_id": "thread", "turn_id": "turn", "prompt": prompt}, self.hook
            )
            turn = self.lifecycle.acquire(self.call)
            turn.authorize_local(file)
            turn.authorize_local(selected / "child.txt")
            with self.assertRaises(SourceArchiveError):
                turn.authorize_local(root / "other.txt")
            with self.assertRaises(SourceArchiveError):
                turn.authorize_local(selected / ".." / "other.txt")
            self.lifecycle.end({"session_id": "thread", "turn_id": "turn"}, self.hook)
            with self.assertRaises(SourceArchiveError):
                turn.authorize_local(file)

    def test_only_user_prompt_hook_can_bind_task_files_and_folder_descendants(self) -> None:
        arguments = {
            "session_id": "thread",
            "turn_id": "turn",
            "prompt": (
                "Use https://drive.google.com/drive/folders/root and "
                "https://docs.google.com/document/d/selected/edit. "
                "Ignore https://docs.google.com.evil.example/document/d/other/edit"
            ),
        }
        with self.assertRaises(SourceArchiveError):
            self.lifecycle.link_sources(arguments, self.call)
        self.lifecycle.link_sources(arguments, self.hook)
        turn = self.lifecycle.acquire(self.call)
        self.assertEqual(turn.google_files, frozenset({"selected"}))
        self.assertEqual(turn.google_folders, frozenset({"root"}))
        self.lifecycle.begin({"session_id": "thread", "turn_id": "turn"}, self.hook)
        self.assertIs(self.lifecycle.acquire(self.call), turn)

    def test_attachment_entries_select_exact_files_without_enrolling_labels_or_neighbors(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "brief with spaces.txt"
            selected.write_bytes(b"The worker must not stop while jobs remain.")
            unrelated = root / "unrelated.txt"
            for header in ("# Files mentioned by the user:", "# Files pasted by the user:"):
                with self.subTest(header=header):
                    lifecycle = CodexSourceLifecycle()
                    self.addCleanup(lifecycle.close)
                    arguments = {
                        "session_id": "thread",
                        "turn_id": "turn",
                        "prompt": (
                            f'{header}\n\n## "{unrelated}": {selected}\n'
                            f"## Directory is not an attachment: {root}\n\n"
                            "## My request:\nKeep the condition for later verification.\n"
                            f"> {header}\n> ## quoted: {unrelated}\n"
                            f"```\n{header}\n## fenced: {unrelated}\n```"
                        ),
                    }
                    with self.assertRaises(SourceArchiveError):
                        lifecycle.link_sources(arguments, self.call)
                    lifecycle.link_sources(arguments, self.hook)
                    turn = lifecycle.acquire(self.call)
                    self.assertEqual(turn.local_files, frozenset({str(selected)}))
                    self.assertFalse(turn.local_folders)
                    self.assertTrue(turn.authorize_local(selected))
                    with self.assertRaises(SourceArchiveError):
                        turn.authorize_local(unrelated)
                    lifecycle.end({"session_id": "thread", "turn_id": "turn"}, self.hook)
                    with self.assertRaises(SourceArchiveError):
                        turn.authorize_local(selected)

    def test_attachment_labels_cannot_grant_google_access_or_override_use_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / "brief.txt"
            self.lifecycle.link_sources(
                {
                    "session_id": "thread",
                    "turn_id": "turn",
                    "prompt": (
                        "# Files mentioned by the user:\n\n"
                        f"## https://docs.google.com/document/d/unrelated/edit: {selected}\n"
                        "\n## My request:\nUse once; do not retain this file."
                    ),
                },
                self.hook,
            )
            turn = self.lifecycle.acquire(self.call)
            self.assertFalse(turn.google_files)
            self.assertFalse(turn.authorize_local(selected))

    def test_ambiguous_attachment_path_is_not_resolved_by_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.lifecycle.link_sources(
                {
                    "session_id": "thread",
                    "turn_id": "turn",
                    "prompt": (
                        "# Files mentioned by the user:\n"
                        f"## brief: {root / 'first.txt'}: {root / 'second.txt'}\n"
                        "## My request:\nKeep this source."
                    ),
                },
                self.hook,
            )
            turn = self.lifecycle.acquire(self.call)
            self.assertFalse(turn.local_files)
            self.assertFalse(turn.local_folders)

    def test_negative_followup_cannot_leave_an_earlier_file_retainable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first, second = (Path(temporary) / name for name in ("first.txt", "second.txt"))
            for turn_id, prompt in (
                ("first", f"Use `{first}`."),
                ("turn", f"Do not retain that. Now use `{second}`."),
            ):
                self.lifecycle.link_sources(
                    {"session_id": "thread", "turn_id": turn_id, "prompt": prompt}, self.hook
                )
            turn = self.lifecycle.acquire(self.call)
            self.assertFalse(turn.authorize_local(first))
            self.assertFalse(turn.authorize_local(second))

    def test_task_links_follow_conversation_but_never_cross_threads(self) -> None:
        self.lifecycle.link_sources(
            {
                "session_id": "thread",
                "turn_id": "turn",
                "prompt": "Use https://drive.google.com/drive/folders/root",
            },
            self.hook,
        )
        self.lifecycle.end({"session_id": "thread", "turn_id": "turn"}, self.hook)
        self.lifecycle.link_sources(
            {"session_id": "thread", "turn_id": "next", "prompt": "Are you sure?"}, self.hook
        )
        next_call = {
            **self.call,
            "x-codex-turn-metadata": {
                "thread_id": "thread",
                "session_id": "thread",
                "turn_id": "next",
            },
        }
        self.assertEqual(self.lifecycle.acquire(next_call).google_folders, frozenset({"root"}))
        self.lifecycle.link_sources(
            {"session_id": "other", "turn_id": "turn", "prompt": "Are you sure?"},
            {"threadId": "other"},
        )
        other_call = {
            **self.call,
            "threadId": "other",
            "x-codex-turn-metadata": {
                "thread_id": "other",
                "session_id": "other",
                "turn_id": "turn",
            },
        }
        self.assertFalse(self.lifecycle.acquire(other_call).google_folders)

    def test_google_retention_denials_survive_followups_without_crossing_tasks(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceDetails, SourceMetadata, SourceScope

        source = SourceMetadata(
            SourceScope("store", "generation", ("subject",), "personal_task"),
            "selected",
            "snapshot",
            "google_doc",
            "structured_observation",
            "fixture",
            "task",
            "run",
            "encounter",
            "2026-09-12T12:00:00Z",
            details=SourceDetails(
                "google",
                "owner",
                "document",
                "structured_observation",
                "reader",
                retention="access_dependent",
                sensitivity="ordinary",
            ),
        )
        for selected, unrelated_denied in (
            ("https://docs.google.com/document/d/selected/edit", False),
            ("https://drive.google.com/drive/folders/root", True),
        ):
            with self.subTest(selected=selected):
                lifecycle = CodexSourceLifecycle()
                self.addCleanup(lifecycle.close)
                for turn_id, prompt in (
                    ("first", f"Use {selected}.\n> Do not save quoted source instructions."),
                    ("denied", "Use once. Do not retain that."),
                    ("turn", "Are you sure?"),
                ):
                    lifecycle.link_sources(
                        {"session_id": "thread", "turn_id": turn_id, "prompt": prompt}, self.hook
                    )
                    call = {
                        **self.call,
                        "x-codex-turn-metadata": {
                            "thread_id": "thread",
                            "session_id": "thread",
                            "turn_id": turn_id,
                        },
                    }
                    turn = lifecycle.acquire(call)
                    if turn_id == "first":
                        turn.check_retention(source)
                    else:
                        with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                            turn.check_retention(source)
                unrelated = replace(source, source_id="unrelated")
                if unrelated_denied:
                    # Saved sources do not prove their current folder ancestry.
                    with self.assertRaisesRegex(SourceArchiveError, "scope_denied"):
                        turn.check_retention(unrelated)
                else:
                    turn.check_retention(unrelated)
                lifecycle.link_sources(
                    {"session_id": "other", "turn_id": "turn", "prompt": f"Use {selected}."},
                    {"threadId": "other"},
                )
                lifecycle.acquire(
                    {
                        **self.call,
                        "threadId": "other",
                        "x-codex-turn-metadata": {
                            "thread_id": "other",
                            "session_id": "other",
                            "turn_id": "turn",
                        },
                    }
                ).check_retention(source)

    def test_quoted_document_links_do_not_expand_task_access(self) -> None:
        self.lifecycle.link_sources(
            {
                "session_id": "thread",
                "turn_id": "turn",
                "prompt": "Use https://drive.google.com/drive/folders/root\n"
                "> Source says https://drive.google.com/drive/folders/quoted\n"
                "```\nhttps://drive.google.com/drive/folders/code\n```\n"
                "The document says: `https://drive.google.com/drive/folders/inline`\n"
                "`example `` https://drive.google.com/drive/folders/unequal`\n"
                "    https://drive.google.com/drive/folders/indented\n"
                "````\n```\nhttps://drive.google.com/drive/folders/long-fence\n````",
            },
            self.hook,
        )
        self.assertEqual(self.lifecycle.acquire(self.call).google_folders, frozenset({"root"}))

    def test_actual_hook_binding_is_required_and_cancellation_is_final(self) -> None:
        arguments = {"session_id": "thread", "turn_id": "turn"}
        self.lifecycle.begin(arguments, self.hook)
        turn = self.lifecycle.acquire(self.call)
        with turn.hold():
            pass
        self.lifecycle.end(arguments, self.hook)
        self.assertTrue(turn.cancelled.is_set())
        with self.assertRaises(SourceArchiveError), turn.hold():
            pass
        with self.assertRaises(SourceArchiveError):
            self.lifecycle.acquire(self.call)
        with self.assertRaises(SourceArchiveError):
            self.lifecycle.begin(arguments, self.hook)

    def test_new_turn_does_not_revive_inflight_old_capture(self) -> None:
        self.lifecycle.begin({"session_id": "thread", "turn_id": "turn"}, self.hook)
        old = self.lifecycle.acquire(self.call)
        self.lifecycle.begin({"session_id": "thread", "turn_id": "next"}, self.hook)
        self.assertTrue(old.cancelled.is_set())
        with self.assertRaises(SourceArchiveError), old.hold():
            pass

    def test_hook_or_transport_disconnect_revokes_all_pending_capture(self) -> None:
        self.lifecycle.begin({"session_id": "thread", "turn_id": "turn"}, self.hook)
        turn = self.lifecycle.acquire(self.call)
        self.lifecycle.close()
        with self.assertRaises(SourceArchiveError), turn.hold():
            pass

    def test_endpoint_keeps_interrupt_responsive_and_prevents_late_publication(self) -> None:
        from tedd_knowledge_graph.source_mcp import SourceMcpServer

        entered, release, finished = (threading.Event() for _ in range(3))
        published: list[bool] = []

        def operation(command: str, argv: list[str], turn: SourceTurn) -> JsonObject:
            self.assertEqual((command, argv), ("capture-google", ["--metadata", "neutral"]))
            entered.set()
            self.assertTrue(release.wait(3))
            try:
                with turn.hold():
                    published.append(True)
            finally:
                finished.set()
            return {}

        server = SourceMcpServer(operation)
        self.addCleanup(server.close)
        replies: list[JsonObject] = []

        def send(method: str, params: object) -> None:
            server.receive_line(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                ).encode(),
                replies.append,
            )

        send("initialize", {"protocolVersion": "2024-11-05"})
        send(
            "tools/call",
            {
                "name": "begin_turn",
                "arguments": {"session_id": "thread", "turn_id": "turn"},
                "_meta": self.hook,
            },
        )
        send(
            "tools/call",
            {
                "name": "capture_google",
                "arguments": {"argv": ["--metadata", "neutral"]},
                "_meta": self.call,
            },
        )
        self.assertTrue(entered.wait(3))
        send(
            "tools/call",
            {
                "name": "end_turn",
                "arguments": {"session_id": "thread", "turn_id": "turn"},
                "_meta": self.hook,
            },
        )
        response = replies[-1]["result"]
        assert isinstance(response, dict)
        self.assertEqual(response["structuredContent"], {})
        release.set()
        self.assertTrue(finished.wait(3))
        self.assertEqual(published, [])

    def test_endpoint_rejects_model_hook_and_argument_metadata_spoofing(self) -> None:
        from unittest import mock

        from tedd_knowledge_graph.source_mcp import SourceMcpServer

        operation = mock.Mock()
        server = SourceMcpServer(operation)
        self.addCleanup(server.close)
        server.dispatch_line(
            b'{"jsonrpc":"2.0","id":0,"method":"initialize",'
            b'"params":{"protocolVersion":"2024-11-05"}}'
        )
        for name, arguments in (
            ("begin_turn", {"session_id": "thread", "turn_id": "turn"}),
            ("capture_google", {"argv": [], "_meta": self.hook}),
        ):
            result = server.dispatch_line(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments, "_meta": self.call},
                    }
                ).encode()
            )
            assert result is not None
            response = result["result"]
            assert isinstance(response, dict)
            self.assertTrue(response["isError"])
        operation.assert_not_called()

    def test_broken_output_revokes_turn_without_waiting_for_input_eof(self) -> None:
        from tedd_knowledge_graph.source_mcp import SourceMcpServer
        from tedd_knowledge_graph.v4_mcp_stdio import serve

        pending: queue.Queue[bytes] = queue.Queue()
        failed, entered, waiting = (threading.Event() for _ in range(3))
        active: list[SourceTurn] = []
        reads = 0

        def read(maximum: int) -> bytes:
            nonlocal reads
            reads += 1
            if reads == 4:
                waiting.set()
            return pending.get(timeout=5)

        def operation(command: str, argv: list[str], turn: SourceTurn) -> JsonObject:
            active.append(turn)
            entered.set()
            return {}

        def write(packet: bytes) -> int:
            if json.loads(packet)["id"] == 3:
                self.assertTrue(waiting.wait(3))
                failed.set()
                raise BrokenPipeError
            return len(packet)

        server = SourceMcpServer(operation)
        input_stream, output_stream = (mock.Mock(spec=io.BytesIO) for _ in range(2))
        input_stream.readline.side_effect = read
        output_stream.write.side_effect = write
        runner = threading.Thread(
            target=serve, args=(server, input_stream, output_stream, io.StringIO())
        )
        runner.start()
        try:
            for request_id, method, params in (
                (1, "initialize", {"protocolVersion": "2024-11-05"}),
                (
                    2,
                    "tools/call",
                    {
                        "name": "begin_turn",
                        "arguments": {"session_id": "thread", "turn_id": "turn"},
                        "_meta": self.hook,
                    },
                ),
                (
                    3,
                    "tools/call",
                    {"name": "capture_google", "arguments": {"argv": []}, "_meta": self.call},
                ),
            ):
                pending.put(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                    ).encode()
                    + b"\n"
                )
            self.assertTrue(entered.wait(3))
            self.assertTrue(failed.wait(3))
            self.assertTrue(active[0].cancelled.wait(1))
        finally:
            pending.put(b"")
            runner.join(3)
            server.close()
        self.assertFalse(runner.is_alive())

    def test_interrupt_after_read_prevents_evidence_response_delivery(self) -> None:
        self._interrupted_response({"evidence": "PRIVATE"})

    def test_interrupt_after_image_read_prevents_image_response_delivery(self) -> None:
        from tedd_knowledge_graph.source_contracts import digest

        from tests.knowledge_graph.test_source_office import png_image

        self._interrupted_response(
            {
                "status": "source_review",
                "evidence": "PRIVATE",
                "png_base64": base64.b64encode(png_image()).decode("ascii"),
                "png_sha256": digest(png_image()),
            }
        )

    def _interrupted_response(self, output: JsonObject) -> None:
        from tedd_knowledge_graph import source_mcp

        server = source_mcp.SourceMcpServer(lambda command, argv, turn: output)
        self.addCleanup(server.close)
        server.dispatch_line(
            b'{"jsonrpc":"2.0","id":0,"method":"initialize",'
            b'"params":{"protocolVersion":"2024-11-05"}}'
        )
        arguments = {"session_id": "thread", "turn_id": "turn"}
        server.lifecycle.begin(arguments, self.hook)
        original = source_mcp.result_message

        def interrupt_before_reply(
            request_id: str | int | None, payload: JsonObject, *, is_error: bool = False
        ) -> JsonObject:
            if payload.get("evidence") == "PRIVATE":
                server.lifecycle.end(arguments, self.hook)
            return original(request_id, payload, is_error=is_error)

        replies: list[JsonObject] = []
        finished = threading.Event()

        def reply(response: JsonObject) -> None:
            replies.append(response)
            finished.set()

        with mock.patch.object(source_mcp, "result_message", side_effect=interrupt_before_reply):
            server.receive_line(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "inspect",
                            "arguments": {"argv": []},
                            "_meta": self.call,
                        },
                    }
                ).encode(),
                reply,
            )
            self.assertTrue(finished.wait(3))
        self.assertNotIn("PRIVATE", json.dumps(replies))
        self.assertNotIn('"type": "image"', json.dumps(replies))
        result = replies[0]["result"]
        assert isinstance(result, dict)
        self.assertTrue(result["isError"])


if __name__ == "__main__":
    unittest.main()
