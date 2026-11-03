"""Synthetic CLI-to-fresh-MCP workflows, without model calls or production data."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import knowledge_graph as graph

from tests.knowledge_graph.test_handoff_receipts import HandoffFixture
from tests.support.paths import GRAPH_SCRIPTS


class MemoryWorkflowTests(HandoffFixture):
    def setUp(self) -> None:
        super().setUp()
        self.config = self.root / "config.json"
        graph.atomic_write(self.config, json.dumps({"store_path": str(self.store_path)}))
        self.environment = {
            key: os.environ[key]
            for key in ("SystemRoot", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA")
            if key in os.environ
        }
        self.environment.update(
            HOME=str(self.root), USERPROFILE=str(self.root), PYTHONDONTWRITEBYTECODE="1"
        )
        self.capture_count = 0

    def invoke_cli(self, *arguments, environment=None):
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(GRAPH_SCRIPTS / "knowledge_graph.py"),
                "--config",
                str(self.config),
                *arguments,
            ],
            env=self.environment if environment is None else environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def cli(self, *arguments):
        result = self.invoke_cli(*arguments)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("", result.stderr)
        return json.loads(result.stdout)

    def state(self):
        return self.cli("status")["graph"]

    def reviewed_capture(self, *, handoff=False, replace=False, **records):
        self.capture_count += 1
        packet = self.root / f"packet-{self.capture_count}.json"
        plan = self.root / f"plan-{self.capture_count}.json"
        graph.atomic_write(
            packet,
            json.dumps(
                {
                    "source": {
                        "kind": "test",
                        "role": "user",
                        "title": "Synthetic workflow evidence",
                        "observed_at": "2026-01-03T12:00:00Z",
                        "source_tier": "validated_structured_candidate",
                    },
                    **records,
                }
            ),
        )
        options = [
            "capture",
            "--input",
            str(packet),
            "--consumer-id",
            "consumer_alpha",
            "--producer-agent-id",
            "agent_alpha",
            "--task-id",
            "task_alpha",
            "--producer-run-id",
            "run_save",
            "--discovery-kind",
            "verification",
            "--based-on-watermark",
            str(self.state()["watermark"]),
            "--authority-ceiling",
            "validated_structured_candidate",
        ]
        if replace:
            options += ["--allow-replace"]
        preview = self.cli(*options, "--dry-run", "--plan-output", str(plan))
        options += ["--expect-fingerprint", preview["fingerprint"], "--expect-plan-file", str(plan)]
        if handoff:
            options += ["--handoff", "--handoff-prompt-id", "prompt_alpha"]
        return self.cli(*options), options

    def fresh_reader(self, *subjects):
        self.cli(
            "consumer-register",
            "--consumer-id",
            "consumer_reader",
            "--producer-agent-id",
            "agent_reader",
            "--task-id",
            "task_reader",
            "--producer-run-id",
            "run_reader",
            "--based-on-watermark",
            str(self.state()["watermark"]),
        )
        options = [
            "subscribe",
            "--consumer-id",
            "consumer_reader",
            "--subscription-id",
            "scope_reader",
            "--backfill-from-watermark",
            "0",
        ]
        for subject in subjects:
            options += ["--subject-id", subject, "--record-id", subject]
        self.cli(*options)
        return {
            "consumer_id": "consumer_reader",
            "agent_id": "agent_reader",
            "task_id": "task_reader",
            "subscription_id": "scope_reader",
        }

    def tool(self, name, arguments):
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        ]
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(GRAPH_SCRIPTS / "tedd_graph_mcp.py"),
                "--config",
                str(self.config),
            ],
            input="".join(json.dumps(item) + "\n" for item in requests).encode("utf-8"),
            env=self.environment,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(b"", result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([1, 2], [item["id"] for item in responses], responses)
        return responses[1]["result"]["structuredContent"]

    def test_saved_handoff_is_retrievable_by_a_fresh_reader_after_duplicate_retry(self):
        saved, options = self.reviewed_capture(
            handoff=True,
            events=[
                {
                    "id": "event_logo",
                    "kind": "approval",
                    "title": "Approved teal logo",
                    "summary": "Use teal for the logo.",
                    "entity_refs": ["entity_project"],
                }
            ],
        )
        self.assertEqual("verified", saved["handoff_receipt"]["status"])
        before = self.state()
        retry = self.cli(*options, "--producer-run-id", "run_retry")
        self.assertEqual("duplicate", retry["status"])
        self.assertEqual([], retry["changed_ids"])
        receipt = self.cli(
            "handoff",
            "--check",
            "--session",
            "task_alpha",
            "--prompt",
            "prompt_alpha",
            "--consumer-id",
            "consumer_alpha",
            "--producer-agent-id",
            "agent_alpha",
        )
        self.assertEqual("verified", receipt["status"])
        self.assertEqual(1, len(receipt["mutations"]))
        self.assertEqual("run_save", receipt["mutations"][0]["run_id"])
        reader = self.fresh_reader("entity_project")
        found = self.tool("search_memory", {**reader, "query": "approved teal logo"})
        self.assertEqual(["event_logo"], [item["record_id"] for item in found["results"]])
        self.assertEqual("Use teal for the logo.", found["results"][0]["payload"]["summary"])
        self.assertEqual(before, self.state())

    def test_strict_handoff_refusal_cannot_appear_as_saved_to_a_fresh_reader(self):
        packet = self.root / "excluded.json"
        graph.atomic_write(packet, self.request("event_excluded").input_bytes.decode())
        before = self.state()
        refused = self.invoke_cli(
            "capture",
            "--input",
            str(packet),
            "--handoff",
            "--handoff-prompt-id",
            "prompt_alpha",
            environment={**self.environment, "TEDD_NO_LOCAL_WRITES": "1"},
        )
        self.assertNotEqual(0, refused.returncode)
        self.assertIn("no-local-writes", refused.stderr)
        reader = self.fresh_reader("entity_project")
        found = self.tool("search_memory", {**reader, "query": "event_excluded"})
        self.assertEqual([], found["results"])
        self.assertFalse((self.root / ".codex/tedd/handoffs").exists())
        self.assertEqual(before, self.state())

    def test_resolving_overdue_work_changes_fresh_context_but_preserves_history(self):
        self.reviewed_capture(
            open_loops=[
                {
                    "id": "loop_review",
                    "title": "Review the synthetic logo",
                    "summary": "Awaiting review.",
                    "entity_refs": ["entity_project"],
                    "created_at": "2000-01-01T12:00:00Z",
                    "checkpoint_at": "2000-01-02T12:00:00Z",
                    "status": "open",
                    "next_action": "Inspect the reviewed logo.",
                }
            ]
        )
        reader = self.fresh_reader("entity_project")
        before_read = self.state()
        context = self.tool("recall_context", {**reader, "max_items": 1, "max_chars": 12000})
        self.assertEqual(["loop_review"], [item["id"] for item in context["overdue"]["items"]])
        self.assertEqual(before_read, self.state())
        self.cli(
            "resolve-loop",
            "--id",
            "loop_review",
            "--resolution",
            "Review confirmed complete.",
            "--producer-agent-id",
            "agent_alpha",
            "--task-id",
            "task_alpha",
            "--producer-run-id",
            "run_resolve",
            "--based-on-watermark",
            str(before_read["watermark"]),
        )
        resolved_state = self.state()
        after = self.tool("recall_context", {**reader, "max_items": 1, "max_chars": 12000})
        self.assertEqual([], after["overdue"]["items"])
        record = self.cli("get", "--id", "loop_review")
        self.assertEqual("resolved", record["payload"]["status"])
        self.assertEqual("Review confirmed complete.", record["payload"]["resolution"])
        history = self.cli("history", "--id", "loop_review")
        self.assertEqual(2, len(history["history"]))
        self.assertEqual("open", history["history"][1]["payload"]["status"])
        self.assertEqual(resolved_state, self.state())

    def test_dated_replacement_and_conflict_survive_fresh_mcp_reads(self):
        def preference(value, date, policy):
            return {
                "surface_id": "surface_style",
                "family": "preference",
                "key": "style",
                "subject_refs": ["entity_project"],
                "value": value,
                "valid_from": date,
                "conflict_policy": policy,
            }

        old = "Prefer: brief answers. When: reporting. Because: they are easy to scan."
        current = "Prefer: detailed answers. When: reporting. Because: context helps."
        conflicting = "Prefer: tables. When: reporting. Because: comparison helps."
        future = "Prefer: diagrams. When: reporting. Because: structure helps."
        self.reviewed_capture(claims=[preference(old, "2000-01-01T00:00:00Z", "preserve")])
        reader = self.fresh_reader("entity_project")
        first = self.tool("recall_context", {**reader, "max_chars": 12000})
        old_id = first["preferences"]["items"][0]["id"]
        self.reviewed_capture(
            replace=True, claims=[preference(current, "2001-01-01T00:00:00Z", "replace")]
        )
        replaced = self.cli("get", "--id", old_id)["payload"]
        self.assertEqual("superseded", replaced["lifecycle"])
        self.assertEqual("2001-01-01T00:00:00Z", replaced["valid_to"])
        self.reviewed_capture(claims=[preference(conflicting, "2002-01-01T00:00:00Z", "preserve")])
        self.reviewed_capture(
            replace=True, claims=[preference(future, "2099-01-01T00:00:00Z", "replace")]
        )
        before = self.state()
        context = self.tool("recall_context", {**reader, "max_chars": 12000})
        self.assertEqual("preferences", next(iter(context)))
        items = context["preferences"]["items"]
        self.assertEqual([conflicting, current], [item["value"] for item in items])
        self.assertTrue(all(item["effective_conflict"] for item in items))
        self.assertTrue(all(item["valid_to"] == "2099-01-01T00:00:00Z" for item in items))
        self.assertEqual(
            old, self.cli("get", "--id", old_id, "--revision", "1")["payload"]["value"]
        )
        self.assertEqual(before, self.state())

    def test_combined_context_enforces_scope_privacy_budgets_and_reader_ownership(self):
        self.reviewed_capture(
            entities=[{"id": "entity_other", "type": "project", "name": "Other project"}],
            open_loops=[
                {
                    "id": "loop_visible",
                    "title": "Visible follow-up",
                    "status": "open",
                    "created_at": "2000-01-01T00:00:00Z",
                    "entity_refs": ["entity_project"],
                },
                {
                    "id": "loop_hidden",
                    "title": "PRIVATE_SENTINEL",
                    "status": "open",
                    "created_at": "2000-01-01T00:00:00Z",
                    "entity_refs": ["entity_project"],
                    "sensitive": True,
                },
                {
                    "id": "loop_foreign",
                    "title": "FOREIGN_SENTINEL",
                    "status": "open",
                    "created_at": "2000-01-01T00:00:00Z",
                    "entity_refs": ["entity_other"],
                },
            ],
            claims=[
                {
                    "surface_id": "surface_style",
                    "family": "preference",
                    "key": "style",
                    "subject_refs": ["entity_project"],
                    "valid_from": "2000-01-01T00:00:00Z",
                    "value": "Prefer: short reports. When: reporting. Because: clarity matters.",
                },
                {
                    "surface_id": "surface_private_preference",
                    "family": "preference",
                    "key": "private_style",
                    "subject_refs": ["entity_project"],
                    "sensitive": True,
                    "valid_from": "2000-01-01T00:00:00Z",
                    "value": "Prefer: PRIVATE_PREFERENCE_SENTINEL. When: reporting. Because: privacy.",
                },
                {
                    "surface_id": "surface_foreign_preference",
                    "family": "preference",
                    "key": "foreign_style",
                    "subject_refs": ["entity_other"],
                    "entity_refs": ["entity_project"],
                    "valid_from": "2000-01-01T00:00:00Z",
                    "value": "Prefer: FOREIGN_PREFERENCE_SENTINEL. When: reporting. Because: scope.",
                },
            ],
        )
        reader = self.fresh_reader("entity_project")
        before, consumer = (
            self.state(),
            self.cli("consumer-status", "--consumer-id", "consumer_reader"),
        )
        self.assertEqual(0, consumer["active_subscriptions"][0]["last_ack_watermark"])
        self.assertGreater(before["watermark"], 0)
        for budget in (12000, 1500, 700):
            with self.subTest(budget=budget):
                context = self.tool(
                    "recall_context", {**reader, "max_items": 1, "max_chars": budget}
                )
                encoded = json.dumps(context, ensure_ascii=False)
                self.assertLessEqual(len(encoded), budget)
                self.assertNotIn("PRIVATE_SENTINEL", encoded)
                self.assertNotIn("FOREIGN_SENTINEL", encoded)
                self.assertNotIn("PRIVATE_PREFERENCE_SENTINEL", json.dumps(context["preferences"]))
                self.assertNotIn("FOREIGN_PREFERENCE_SENTINEL", json.dumps(context["preferences"]))
                if budget == 12000:
                    self.assertEqual(
                        ["loop_visible"], [item["id"] for item in context["overdue"]["items"]]
                    )
                    self.assertEqual(
                        ["Prefer: short reports. When: reporting. Because: clarity matters."],
                        [item["value"] for item in context["preferences"]["items"]],
                    )
                for section, ceiling in (("overdue", 1500), ("preferences", 2000)):
                    self.assertLessEqual(
                        len(json.dumps(context[section], ensure_ascii=False)), ceiling
                    )
                    if not context[section]["items"]:
                        self.assertTrue(context[section]["omitted"])
                        self.assertEqual("budget_exhausted", context[section]["reason"])
        denied = self.tool("recall_context", {**reader, "consumer_id": "consumer_beta"})
        self.assertEqual("scope_denied", denied["category"])
        self.assertEqual(before, self.state())
        self.assertEqual(consumer, self.cli("consumer-status", "--consumer-id", "consumer_reader"))

    def test_memorybench_scores_actual_context_sections_and_refuses_omission_as_empty(self):
        preference = "Prefer: concise reports. When: reviewing. Because: clear results help."
        self.reviewed_capture(
            open_loops=[
                {
                    "id": "loop_bench_review",
                    "title": "Synthetic review",
                    "status": "open",
                    "entity_refs": ["entity_project"],
                    "created_at": "2000-01-01T00:00:00Z",
                    "next_action": "Review the synthetic result.",
                }
            ],
            claims=[
                {
                    "surface_id": "surface_bench_style",
                    "family": "preference",
                    "key": "style",
                    "subject_refs": ["entity_project"],
                    "value": preference,
                    "valid_from": "2000-01-01T00:00:00Z",
                }
            ],
        )
        reader = self.fresh_reader("entity_project")
        before = self.state(), self.cli("consumer-status", "--consumer-id", "consumer_reader")
        suite = {
            "schema_version": "tedd-memorybench-v2",
            "suite_id": "synthetic-context",
            "read_path": "mcp_recall_context",
            "expected_store_id": before[0]["store_id"],
            "expected_generation": before[0]["generation"],
            "defaults": {"max_chars": 12000},
            "cases": [
                {
                    "id": "preferences",
                    "session": reader,
                    "context_section": "preferences",
                    "max_items": 1,
                    "required": [
                        {
                            "surface_id": "surface_bench_style",
                            "max_rank": 1,
                            "payload_equals": {"value": preference, "effective_conflict": False},
                        }
                    ],
                },
                {
                    "id": "overdue",
                    "session": reader,
                    "context_section": "overdue",
                    "max_items": 1,
                    "required": [
                        {
                            "record_id": "loop_bench_review",
                            "max_rank": 1,
                            "payload_equals": {"next_action": "Review the synthetic result."},
                        }
                    ],
                },
            ],
        }
        suite_path = self.root / "bench.json"

        def run_bench():
            graph.atomic_write(suite_path, json.dumps(suite))
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(GRAPH_SCRIPTS / "memorybench_knowledge_graph.py"),
                    "--config",
                    str(self.config),
                    "--suite",
                    str(suite_path),
                ],
                env=self.environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
            self.assertEqual("", result.stderr)
            return result.returncode, json.loads(result.stdout)

        code, report = run_bench()
        self.assertEqual(0, code, report)
        self.assertEqual("mcp_recall_context", report["read_path"])
        self.assertEqual([1, 1], [case["result_count"] for case in report["cases"]])
        self.assertEqual([0.083333, 0.2], [case["precision_at_k"] for case in report["cases"]])
        self.assertNotIn(preference, json.dumps(report))
        suite["cases"] = [
            {
                "id": "budget_not_empty",
                "session": reader,
                "context_section": "preferences",
                "max_items": 1,
                "max_chars": 700,
                "max_result_count": 0,
            }
        ]
        code, omitted = run_bench()
        self.assertEqual(1, code, omitted)
        self.assertTrue(omitted["cases"][0]["omitted"])
        self.assertTrue(any("omitted" in gate for gate in omitted["cases"][0]["gates"]))
        self.assertEqual(
            before, (self.state(), self.cli("consumer-status", "--consumer-id", "consumer_reader"))
        )
