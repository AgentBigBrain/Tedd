"""Tests for the read-only MCP transport of the project recall lab."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest import mock

from tedd_memory_eval.sandbox import MemoryEvalSandbox
from tedd_project_recall.mcp_reads import LAB_AGENT_ID, McpReads

from tests.evaluation.project_recall_support import MiniProject

ROOT = Path("synthetic-root")
RUN = "tedd_project_recall.mcp_reads.subprocess.run"


def sandbox(*, prepared: bool = True) -> MemoryEvalSandbox:
    """Stand in for a prepared sandbox; the transport reads three attributes."""

    return cast(
        MemoryEvalSandbox,
        SimpleNamespace(
            root=ROOT if prepared else None,
            config_path=ROOT / "config.json" if prepared else None,
            timeout_seconds=30.0,
        ),
    )


def reply(index: int, body: Mapping[str, object], *, refused: bool = False) -> dict[str, object]:
    result: dict[str, object] = {"structuredContent": dict(body)}
    if refused:
        result["isError"] = True
    return {"jsonrpc": "2.0", "id": index, "result": result}


def completed(*lines: object, returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    output = b"".join(
        line if isinstance(line, bytes) else json.dumps(line).encode("utf-8") + b"\n"
        for line in lines
    )
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=output, stderr=b"")


class McpReadsTests(unittest.TestCase):
    def setUp(self) -> None:
        mini = MiniProject()
        self.scenario = mini.case(mini.current).scenario
        self.search = ("search_memory", {"query": "How often?", "limit": 10, "max_chars": 12_000})
        self.context = ("recall_context", {"max_items": 12, "max_chars": 12_000})

    def test_calls_share_one_process_and_the_scenarios_registered_identity(self) -> None:
        answers = completed(reply(0, {}), reply(1, {"records": []}), reply(2, {"results": []}))

        with mock.patch(RUN, return_value=answers) as run:
            bodies = McpReads(sandbox()).call(self.scenario, [self.context, self.search])

        self.assertEqual(bodies, [{"records": []}, {"results": []}])
        run.assert_called_once()
        self.assertIn(str(ROOT / "config.json"), run.call_args.args[0])
        sent = [json.loads(line) for line in run.call_args.kwargs["input"].splitlines()]
        self.assertEqual(
            [item["method"] for item in sent],
            ["initialize", "notifications/initialized", "tools/call", "tools/call"],
        )
        self.assertEqual(
            sent[3]["params"],
            {
                "name": "search_memory",
                "arguments": {
                    "query": "How often?",
                    "limit": 10,
                    "max_chars": 12_000,
                    "subscription_id": "lab_subscription_mini",
                    "consumer_id": "lab_consumer_mini",
                    "agent_id": LAB_AGENT_ID,
                    "task_id": "lab_task_mini",
                },
            },
        )

    def test_child_process_is_isolated_inside_the_sandbox_root(self) -> None:
        with (
            mock.patch.dict(os.environ, {"PYTHONPATH": "outside"}),
            mock.patch(RUN, return_value=completed(reply(0, {}), reply(1, {}))) as run,
        ):
            McpReads(sandbox()).call(self.scenario, [self.context])

        environment = run.call_args.kwargs["env"]
        self.assertEqual(
            {environment[name] for name in ("HOME", "USERPROFILE", "APPDATA", "TEMP", "TMP")},
            {str(ROOT)},
        )
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(run.call_args.kwargs["timeout"], 30.0)

    def test_a_read_outside_the_allowlist_is_refused_before_any_process_starts(self) -> None:
        refused = {
            "a tool that is not a frozen read": ("pull_changes", {}),
            "an argument the lab does not use": ("search_memory", {"query": "x", "match": "any"}),
            "a caller-chosen identity": ("recall_context", {"subscription_id": "lab_elsewhere"}),
        }

        for label, call in refused.items():
            with (
                self.subTest(label=label),
                mock.patch(RUN) as run,
                self.assertRaisesRegex(RuntimeError, "frozen MCP reads"),
            ):
                McpReads(sandbox()).call(self.scenario, [call])
            run.assert_not_called()

    def test_an_unprepared_sandbox_is_refused(self) -> None:
        with mock.patch(RUN) as run, self.assertRaisesRegex(RuntimeError, "not prepared"):
            McpReads(sandbox(prepared=False)).call(self.scenario, [self.context])

        run.assert_not_called()

    def test_server_failures_fail_closed(self) -> None:
        failures = {
            "non-zero exit": (completed(reply(0, {}), returncode=1), "server failed"),
            "invalid JSON": (completed(b"not json\n"), "invalid JSON"),
            "a reply that is not an object": (completed([1, 2]), "must be a JSON object"),
            "a missing answer": (completed(reply(0, {})), "did not answer every read"),
            "a result without a body": (
                completed({"jsonrpc": "2.0", "id": 1, "result": {}}),
                "must be a JSON object",
            ),
        }

        for label, (answers, message) in failures.items():
            with (
                self.subTest(label=label),
                mock.patch(RUN, return_value=answers),
                self.assertRaisesRegex(RuntimeError, message),
            ):
                McpReads(sandbox()).call(self.scenario, [self.context])

    def test_a_timeout_is_reported_without_the_command_line(self) -> None:
        expired = subprocess.TimeoutExpired((str(ROOT / "config.json"),), 30.0)

        with (
            mock.patch(RUN, side_effect=expired),
            self.assertRaisesRegex(RuntimeError, "exceeded its time limit") as raised,
        ):
            McpReads(sandbox()).call(self.scenario, [self.context])

        self.assertNotIn(str(ROOT), str(raised.exception))

    def test_a_refused_read_reports_its_category_and_not_its_message(self) -> None:
        body = {"category": "scope_denied", "error": f"no access to {ROOT / 'graph.sqlite3'}"}

        with (
            mock.patch(RUN, return_value=completed(reply(0, {}), reply(1, body, refused=True))),
            self.assertRaisesRegex(RuntimeError, "refused: scope_denied") as raised,
        ):
            McpReads(sandbox()).call(self.scenario, [self.context])

        self.assertNotIn("graph.sqlite3", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
