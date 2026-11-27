"""The Claude host must authorize the exact call that receives source metadata."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from tests.knowledge_graph import test_source_cli
from tests.support.paths import GRAPH_SCRIPTS, PROFILE_SCRIPTS, prepend_import_paths

prepend_import_paths(PROFILE_SCRIPTS, GRAPH_SCRIPTS)

import tedd_claude_lookup as endpoint  # noqa: E402
import tedd_setup as setup  # noqa: E402
from tedd_knowledge_graph.source_lookup import LookupRequest  # noqa: E402
from tedd_knowledge_graph.source_mcp import SourceTurn  # noqa: E402
from tedd_knowledge_graph.v4_application_contracts import JsonObject  # noqa: E402
from tedd_setup import SetupRequest  # noqa: E402


class ClaudeSourceLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        project = Path.cwd().resolve()
        request = SetupRequest(
            "claude", project / "config", project, project / "profile", project, project
        )
        self.server = endpoint.ClaudeLookupServer(request, "session")
        self.addCleanup(self.server.close)
        self.responses: queue.Queue[JsonObject] = queue.Queue()
        self.send("initialize", {"protocolVersion": "2025-06-18"})
        self.responses.get(timeout=2)
        self.grant = {"session_id": "session", "cwd": str(project), "tool_use_id": "call_a"}
        self.lookup = {
            "name": "lookup",
            "_meta": {"claudecode/toolUseId": "call_a"},
            "arguments": {},
        }

    def send(
        self, method: str, params: object, *, notification: bool = False, request_id: int = 17
    ) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = request_id
        self.server.receive_line(json.dumps(message).encode(), self.responses.put)

    def result(self) -> JsonObject:
        return self.responses.get(timeout=3)["result"]

    def prepare(self, call_id: str = "call_a") -> None:
        self.send(
            "tools/call",
            {"name": "prepare_lookup", "arguments": {**self.grant, "tool_use_id": call_id}},
            request_id=1,
        )
        self.assertFalse(self.result().get("isError"))

    def test_only_a_matching_host_call_can_consume_one_preparation(self) -> None:
        self.send("tools/list", {})
        self.assertEqual({t["name"] for t in self.result()["tools"]}, {"lookup", "prepare_lookup"})
        for extra, change in (
            (self.lookup["_meta"], {}),
            ({}, {"session_id": "parent"}),
            ({}, {"cwd": "relative"}),
        ):
            with self.subTest(change=change, metadata=extra):
                self.send(
                    "tools/call",
                    {
                        "name": "prepare_lookup",
                        "_meta": extra,
                        "arguments": {**self.grant, **change},
                    },
                )
                self.assertEqual(self.result()["structuredContent"], {"status": "scope_denied"})
        self.prepare()
        self.send("tools/call", {**self.lookup, "_meta": {"claudecode/toolUseId": "other"}})
        self.assertEqual(self.result()["structuredContent"], {"status": "scope_denied"})
        self.send("tools/call", self.lookup)
        self.assertEqual(self.result()["structuredContent"], {"status": "invalid_request"})
        self.send("tools/call", self.lookup)
        self.assertEqual(self.result()["structuredContent"], {"status": "scope_denied"})

    def test_cancellation_while_preparing_a_reply_withholds_metadata(self) -> None:
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def read(_args: object, turn: SourceTurn) -> JsonObject:
            turn.local_delivery_hold = nullcontext
            entered.set()
            if not release.wait(3):
                raise TimeoutError
            return {"sources": ["private fixture"]}

        with (
            patch.object(self.server.operations, "lookup_request"),
            patch.object(self.server.operations, "local_lookup", side_effect=read),
            patch.object(self.server, "check_scope"),
        ):
            self.prepare()
            self.send("tools/call", self.lookup)
            self.assertTrue(entered.wait(2))
            self.send("notifications/cancelled", {"requestId": 17}, notification=True)
            release.set()
            response = self.result()
            self.assertEqual(response["structuredContent"], {"status": "scope_denied"})
            self.assertNotIn("private fixture", str(response))

    def bind_archive(self) -> tuple[test_source_cli.SourceCliTests, JsonObject]:
        fixture = test_source_cli.SourceCliTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture._capture_chat_fixture()
        profile = fixture.root / "identity/profile.json"
        subprocess.run(
            [
                sys.executable,
                "-B",
                str(PROFILE_SCRIPTS / "tedd_profile.py"),
                "--profile",
                str(profile),
                "init",
                "--preferred-name",
                "Example",
                "--display-name",
                "Example",
                "--email",
                "owner@example.invalid",
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        agent = json.loads(profile.read_text(encoding="utf-8"))["agent_instance_id"]
        req = SetupRequest(
            "claude",
            fixture.config,
            fixture.root,
            profile,
            fixture.root,
            Path(sys.executable),
            entity_ids=("entity_task",),
        )
        setup.apply_setup(req, setup.preview_setup(req)["fingerprint"])
        with setup.storage.open_store(fixture.store) as store:
            store.register_consumer("claude:session", agent_id=agent, task_id="session")
            store.subscribe(
                "claude:session",
                {"entity_ids": ["entity_task"]},
                subscription_id="claude_subscription",
            )
        self.server = endpoint.ClaudeLookupServer(req, "session")
        self.addCleanup(self.server.close)
        self.send("initialize", {"protocolVersion": "2025-06-18"})
        self.result()
        self.grant["cwd"] = str(req.project)
        named = {
            "consumer_id": "claude:session",
            "producer_agent_id": agent,
            "task_id": "session",
            "producer_run_id": "run",
            "subscription_id": "claude_subscription",
            "subject_id": ["entity_task"],
        }
        return fixture, named

    def prepared_lookup(self, args: JsonObject) -> JsonObject:
        self.prepare()
        self.send("tools/call", {**self.lookup, "arguments": args})
        return self.result()

    def test_bound_lookup_checks_the_local_archive_and_rechecks_project_approval(self) -> None:
        fixture, named = self.bind_archive()
        # This second consumer is valid in the same graph and can see the same source in the CLI.
        other = {"argv": fixture.session}
        expected = fixture.invoke("lookup", *fixture.session)
        self.assertEqual(len(expected["sources"]), 1)
        self.assertEqual(
            self.prepared_lookup(other)["structuredContent"], {"status": "scope_denied"}
        )
        self.assertEqual(self.prepared_lookup(named)["structuredContent"], expected)
        real_lookup = self.server.operations.local_lookup

        def revoke(request: LookupRequest, turn: SourceTurn) -> JsonObject:
            payload = real_lookup(request, turn)
            self.assertEqual(payload, expected)
            path = self.server.setup.profile.parent / "project-bindings.json"
            with setup.graph.store_lock(path):
                setup.graph.atomic_write(path, '{"version":1,"bindings":[]}')
            return payload

        with patch.object(self.server.operations, "local_lookup", side_effect=revoke):
            denied = self.prepared_lookup(named)
        self.assertEqual(denied["structuredContent"], {"status": "scope_denied"})
        self.assertNotIn("tedd-source://", json.dumps(denied))

    def test_restricting_the_returned_source_before_delivery_withholds_it(self) -> None:
        fixture, named = self.bind_archive()
        expected = fixture.invoke("lookup", *fixture.session)
        source = expected["sources"][0]["source_uri"]
        real_lookup = self.server.operations.local_lookup

        def restrict(request: LookupRequest, turn: SourceTurn) -> JsonObject:
            payload = real_lookup(request, turn)
            self.assertEqual(payload, expected)
            update = fixture.invoke("restrict", "--source-uri", source, *fixture.session)
            self.assertEqual(update["status"], "source_restricted")
            return payload

        with patch.object(self.server.operations, "local_lookup", side_effect=restrict):
            self.prepare()
            self.send("tools/call", {**self.lookup, "arguments": named})
            denied = self.responses.get(timeout=25)["result"]
        self.assertEqual(denied["structuredContent"], {"status": "scope_denied"})
        self.assertNotIn(source, json.dumps(denied))

    def test_preparation_expires_at_120_seconds_and_a_fresh_call_can_retry(self) -> None:
        fixture, named = self.bind_archive()
        expected = fixture.invoke("lookup", *fixture.session)
        with patch.object(endpoint, "time") as clock:
            clock.monotonic.return_value = 100
            self.prepare()
            clock.monotonic.return_value = 220
            self.send("tools/call", {**self.lookup, "arguments": named})
            self.assertEqual(self.result()["structuredContent"], {"status": "scope_denied"})
            self.prepare("retry")
            clock.monotonic.return_value += 119
            self.send(
                "tools/call",
                {**self.lookup, "_meta": {"claudecode/toolUseId": "retry"}, "arguments": named},
            )
            self.assertEqual(self.result()["structuredContent"], expected)

    def test_busy_server_refuses_second_call_and_delivers_first_reply(self) -> None:
        fixture, named = self.bind_archive()
        expected = fixture.invoke("lookup", *fixture.session)
        entered, release = threading.Event(), threading.Event()
        real_lookup = self.server.operations.local_lookup

        def pause(request: LookupRequest, turn: SourceTurn) -> JsonObject:
            payload = real_lookup(request, turn)
            entered.set()
            if not release.wait(15):
                raise TimeoutError
            return payload

        with patch.object(self.server.operations, "local_lookup", side_effect=pause):
            self.prepare()
            self.send("tools/call", {**self.lookup, "arguments": named})
            try:
                self.assertTrue(entered.wait(10))
                self.prepare("call_b")
                self.send(
                    "tools/call",
                    {
                        **self.lookup,
                        "_meta": {"claudecode/toolUseId": "call_b"},
                        "arguments": named,
                    },
                    request_id=18,
                )
                busy = self.responses.get(timeout=3)
                self.assertEqual(busy["id"], 18)
                self.assertEqual(
                    busy["result"]["structuredContent"], {"status": "capability_unavailable"}
                )
            finally:
                release.set()
            first = self.responses.get(timeout=5)
            self.assertEqual(first["id"], 17)
            self.assertEqual(first["result"]["structuredContent"], expected)

    def test_unexpected_lookup_exception_returns_only_the_public_error(self) -> None:
        with patch.object(
            self.server.operations,
            "lookup_request",
            side_effect=RuntimeError("private fixture detail"),
        ):
            self.prepare()
            self.send("tools/call", self.lookup)
            reply = self.responses.get(timeout=3)
        self.assertEqual(
            reply,
            {
                "jsonrpc": "2.0",
                "id": 17,
                "error": {"code": -32603, "message": "request could not be completed"},
            },
        )
