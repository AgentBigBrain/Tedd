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

    def send(self, method: str, params: object, *, notification: bool = False) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = 17
        self.server.receive_line(json.dumps(message).encode(), self.responses.put)

    def result(self) -> JsonObject:
        return self.responses.get(timeout=3)["result"]

    def prepare(self) -> None:
        self.send("tools/call", {"name": "prepare_lookup", "arguments": self.grant})
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

    def test_bound_lookup_checks_the_local_archive_and_rechecks_project_approval(self) -> None:
        from tests.knowledge_graph.test_source_cli import SourceCliTests

        fixture = SourceCliTests()
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

        def lookup(args: dict[str, object]) -> JsonObject:
            self.prepare()
            self.send("tools/call", {**self.lookup, "arguments": args})
            return self.result()

        # This second consumer is valid in the same graph and can see the same source in the CLI.
        other = {"argv": fixture.session}
        expected = fixture.invoke("lookup", *fixture.session)
        self.assertEqual(len(expected["sources"]), 1)
        self.assertEqual(lookup(other)["structuredContent"], {"status": "scope_denied"})
        self.assertEqual(lookup(named)["structuredContent"], expected)
        real_lookup = self.server.operations.local_lookup

        def revoke(request: LookupRequest, turn: SourceTurn) -> JsonObject:
            payload = real_lookup(request, turn)
            self.assertEqual(payload, expected)
            path = profile.parent / "project-bindings.json"
            with setup.graph.store_lock(path):
                setup.graph.atomic_write(path, '{"version":1,"bindings":[]}')
            return payload

        with patch.object(self.server.operations, "local_lookup", side_effect=revoke):
            denied = lookup(named)
        self.assertEqual(denied["structuredContent"], {"status": "scope_denied"})
        self.assertNotIn("tedd-source://", json.dumps(denied))
