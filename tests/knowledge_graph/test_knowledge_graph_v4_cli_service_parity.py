#!/usr/bin/env python3
"""Black-box compatibility and service-routing tests for v4 CLI reads."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unittest
from typing import cast
from unittest import mock

from tests.support.paths import GRAPH_SCRIPTS as SCRIPT_DIRECTORY

CLI_SCRIPT = SCRIPT_DIRECTORY / "knowledge_graph.py"

import knowledge_graph_v4 as v4  # noqa: E402
import knowledge_graph_v4_cli as v4_cli  # noqa: E402
from platform_support import set_private_path_mode  # noqa: E402
from tedd_knowledge_graph.v4_application_contracts import (  # noqa: E402
    BrainStatusRequest,
    GraphReadResult,
    PullChangesRequest,
    RecallContextRequest,
)
from tedd_knowledge_graph.v4_cli_contracts import JsonObject, V4CLIError  # noqa: E402

from tests.support.graph_cases import PLAN_KEY, V4TestCase, applied, change  # noqa: E402


class _RecordingReadService:
    """A narrow fake that makes transport routing observable without a store."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def brain_status(self, request: BrainStatusRequest) -> GraphReadResult:
        self.calls.append(request)
        if request.consumer_id is not None:
            return GraphReadResult("brain_status", {"consumer": {"routed": "brain_status"}})
        return GraphReadResult("brain_status", {"routed": "brain_status"})

    def recall_context(self, request: RecallContextRequest) -> GraphReadResult:
        self.calls.append(request)
        return GraphReadResult("recall_context", {"routed": "recall_context"})

    def pull_changes(self, request: PullChangesRequest) -> GraphReadResult:
        self.calls.append(request)
        return GraphReadResult("pull_changes", {"routed": "pull_changes"})


class V4CLIServiceParityTests(V4TestCase):
    def _args(self, command: str, **values: object) -> argparse.Namespace:
        defaults: dict[str, object] = {
            "command": command,
            "mode": "operator_full",
            "store": None,
            "query": "",
            "since_watermark": None,
            "change_cursor": None,
            "through_watermark": None,
            "consumer_id": None,
            "at_watermark": None,
            "section": [],
            "limit": 10,
            "subject_id": [],
            "max_chars": 12_000,
            "discovery_kind": [],
            "subscription_id": "subscription_alpha",
            "max_items": 10,
            "plan": False,
            "fingerprint": None,
            "cursor": None,
        }
        defaults.update(values)
        return argparse.Namespace(**defaults)

    def _seed(self) -> None:
        with self.create_store() as store:
            applied(store, [change("record_alpha"), change("record_beta")], fingerprint="cli")
            store.register_consumer("consumer_alpha", agent_id="agent_alpha", task_id="task_alpha")
            store.subscribe(
                "consumer_alpha",
                {"all": True},
                subscription_id="subscription_alpha",
                start_watermark=0,
            )

    def _seed_empty_subscription(self) -> None:
        with self.create_store() as store:
            store.register_consumer("consumer_alpha", agent_id="agent_alpha", task_id="task_alpha")
            store.subscribe(
                "consumer_alpha",
                {"all": True},
                subscription_id="subscription_alpha",
                start_watermark=0,
            )

    @staticmethod
    def _wrapped(command: str, payload: dict[str, object]) -> dict[str, object]:
        result = {"command": command, **payload}
        if command == "context":
            result["overdue"] = {"items": [], "truncated": False, "omitted": False}
            result["preferences"] = {
                "label": "stated preferences, with dates",
                "items": [],
                "truncated": False,
                "omitted": False,
            }
            # The seeded scopes hold entities only: no claims, loops, decisions or notes.
            result["current_state"] = {
                "label": "current claims, with dates",
                "items": [],
                "truncated": False,
                "omitted": False,
            }
            result["waiting"] = {
                "commitments": {
                    "unfinished": 0,
                    "overdue": 0,
                    "unassigned": 0,
                    "agent_owned": 0,
                    "named_owner": 0,
                },
                "proposals": {"undecided": 0, "older_than_days": 30, "older": 0},
                "notes": {"unfolded": 0},
            }
        result.setdefault("status", "ok")
        return result

    def _run(self, args: argparse.Namespace) -> JsonObject:
        result = v4_cli.handle_v4_command(
            args,
            db_path=self.store_path,
            plan_hmac_key=PLAN_KEY,
            review_sanitizer=lambda value: value,
        )
        self.assertIsInstance(result, dict)
        return cast(JsonObject, result)

    def test_six_read_commands_are_thin_service_adapters(self) -> None:
        """This fails if a CLI read falls back to direct store shaping or opens twice."""

        commands: tuple[tuple[str, dict[str, object], str], ...] = (
            ("status", {}, "brain_status"),
            ("consumer-status", {"consumer_id": "consumer_alpha"}, "brain_status"),
            ("recall", {"query": "alpha"}, "recall_context"),
            ("context", {}, "recall_context"),
            ("changes", {"since_watermark": 0}, "pull_changes"),
            ("inbox", {}, "pull_changes"),
        )
        for command, values, expected_route in commands:
            with self.subTest(command=command):
                recorder = _RecordingReadService()
                with mock.patch.object(
                    v4_cli,
                    "_make_graph_read_service",
                    return_value=recorder,
                ) as factory:
                    result = self._run(self._args(command, **values))

                self.assertEqual(result["routed"], expected_route)
                self.assertEqual(len(recorder.calls), 1)
                factory.assert_called_once()

    def test_cli_service_keeps_malformed_cursor_and_missing_graph_errors_safe(self) -> None:
        """Composition injection must preserve useful CLI errors without backend text."""

        self._seed()
        with self.assertRaises(V4CLIError) as malformed:
            self._run(self._args("changes", since_watermark=0, cursor="not-a-signed-token"))
        self.assertEqual("Malformed signed token", str(malformed.exception))

        missing_path = self.store_path.with_name("missing-graph.sqlite3")
        with self.assertRaises(V4CLIError) as unavailable:
            v4_cli.handle_v4_command(
                self._args("status"),
                db_path=missing_path,
                plan_hmac_key=PLAN_KEY,
                review_sanitizer=lambda value: value,
            )
        self.assertEqual("graph is unavailable", str(unavailable.exception))

    def test_service_payloads_match_disposable_store_reads(self) -> None:
        """This fails if service routing changes records, cursors, or ack eligibility."""

        self._seed()
        cases = (
            (
                self._args("status"),
                lambda store: {
                    "store": {
                        "safe": v4.inspect_store_path(self.store_path)["safe"],
                        "coordination_scope": "same-machine-only",
                    },
                    "graph": store.state(),
                },
            ),
            (
                self._args("consumer-status", consumer_id="consumer_alpha"),
                lambda store: store.consumer_status("consumer_alpha"),
            ),
            (
                self._args("recall", query="record", limit=1),
                lambda store: store.search("record", limit=1, collections=None),
            ),
            (
                self._args("context", max_items=1),
                lambda store: store.context_pack(
                    "subscription_alpha", max_items=1, max_chars=12_000
                ),
            ),
            (
                self._args("changes", since_watermark=0, limit=1),
                lambda store: store.mutation_feed(
                    0,
                    limit=1,
                    selector={
                        "all": True,
                        "entity_ids": [],
                        "record_ids": [],
                        "collections": [],
                        "discovery_kinds": [],
                    },
                ),
            ),
            (
                self._args("inbox", limit=1),
                lambda store: store.inbox("subscription_alpha", limit=1),
            ),
        )
        for args, direct in cases:
            with self.subTest(command=args.command):
                with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
                    expected = direct(store)
                actual = self._run(args)
                wrapped = self._wrapped(args.command, expected)
                self.assertEqual(actual, wrapped)

    def test_recall_truncation_and_review_safe_presentation_are_preserved(self) -> None:
        """This fails if the adapter bypasses the CLI character budget or final sanitizer."""

        self._seed()
        args = self._args("recall", query="record", max_chars=3, mode="review_safe")
        result = v4_cli.handle_v4_command(
            args,
            db_path=self.store_path,
            plan_hmac_key=PLAN_KEY,
            review_sanitizer=lambda value: {"reviewed": value["truncated_by_char_budget"]},
        )
        self.assertEqual(
            result,
            {"command": "recall", "reviewed": True, "status": "ok"},
        )

    def test_empty_read_payloads_match_the_historical_direct_store_contract(self) -> None:
        """This fails if service routing reshapes valid empty read results."""

        self._seed_empty_subscription()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            expected = {
                "recall": store.search("absent", limit=10, collections=None),
                "changes": store.mutation_feed(
                    0,
                    limit=10,
                    selector={
                        "all": True,
                        "entity_ids": [],
                        "record_ids": [],
                        "collections": [],
                        "discovery_kinds": [],
                    },
                ),
                "context": store.context_pack("subscription_alpha", max_items=10, max_chars=12_000),
                "inbox": store.inbox("subscription_alpha", limit=10),
            }
        arguments = {
            "recall": self._args("recall", query="absent"),
            "changes": self._args("changes", since_watermark=0),
            "context": self._args("context"),
            "inbox": self._args("inbox"),
        }
        for command, args in arguments.items():
            with self.subTest(command=command):
                self.assertEqual(self._run(args), self._wrapped(command, expected[command]))

    def test_changes_and_inbox_preserve_direct_cursor_snapshot_pages(self) -> None:
        """This fails if a continuation loses its direct-store cursor or pinned watermark."""

        self._seed()
        selector = {
            "all": True,
            "entity_ids": [],
            "record_ids": [],
            "collections": [],
            "discovery_kinds": [],
        }
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            snapshot_watermark = store.state()["watermark"]
            change_first = store.mutation_feed(
                0,
                through_watermark=snapshot_watermark,
                limit=1,
                selector=selector,
            )
            change_second = store.mutation_feed(
                0,
                through_watermark=snapshot_watermark,
                cursor=change_first["next_cursor"],
                limit=1,
                selector=selector,
            )
            inbox_first = store.inbox(
                "subscription_alpha", through_watermark=snapshot_watermark, limit=1
            )
            inbox_second = store.inbox(
                "subscription_alpha",
                through_watermark=snapshot_watermark,
                cursor=inbox_first["next_cursor"],
                limit=1,
            )

        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            applied(store, [change("record_gamma")], fingerprint="post-cursor")

        change_arguments = self._args(
            "changes", since_watermark=0, through_watermark=snapshot_watermark, limit=1
        )
        inbox_arguments = self._args("inbox", through_watermark=snapshot_watermark, limit=1)
        self.assertEqual(self._run(change_arguments), self._wrapped("changes", change_first))
        self.assertEqual(
            self._run(
                self._args(
                    "changes",
                    since_watermark=0,
                    through_watermark=snapshot_watermark,
                    limit=1,
                    cursor=change_first["next_cursor"],
                )
            ),
            self._wrapped("changes", change_second),
        )
        self.assertEqual(self._run(inbox_arguments), self._wrapped("inbox", inbox_first))
        self.assertEqual(
            self._run(
                self._args(
                    "inbox",
                    through_watermark=snapshot_watermark,
                    limit=1,
                    cursor=inbox_first["next_cursor"],
                )
            ),
            self._wrapped("inbox", inbox_second),
        )
        self.assertEqual(change_second["snapshot_watermark"], snapshot_watermark)
        self.assertEqual(inbox_second["snapshot_watermark"], snapshot_watermark)

    def test_consumer_and_subscription_absence_preserve_historical_cli_results(self) -> None:
        """This fails if safe service errors erase the CLI's established local contract."""

        self._seed_empty_subscription()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            missing_consumer = store.consumer_status("consumer_missing")
            expected_errors: dict[str, str] = {}
            for command, subscription_id in (
                ("context", "subscription_missing"),
                ("inbox", "subscription_missing"),
            ):
                with self.assertRaises(v4.SubscriptionError) as raised:
                    if command == "context":
                        store.context_pack(subscription_id, max_items=10, max_chars=12_000)
                    else:
                        store.inbox(subscription_id, limit=10)
                expected_errors[command] = str(raised.exception)
        self.assertEqual(
            self._run(self._args("consumer-status", consumer_id="consumer_missing")),
            self._wrapped("consumer-status", missing_consumer),
        )
        for command, message in expected_errors.items():
            with self.subTest(command=command):
                with self.assertRaises(v4_cli.V4CLIError) as raised:
                    self._run(self._args(command, subscription_id="subscription_missing"))
                self.assertEqual(str(raised.exception), message)

    def test_inactive_subscription_preserves_historical_cli_error(self) -> None:
        """This fails if an inactive subscription becomes distinguishable from the legacy result."""

        self._seed_empty_subscription()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            store.connection.execute(
                "UPDATE subscriptions SET active=0 WHERE subscription_id=?",
                ("subscription_alpha",),
            )
            store.connection.commit()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            with self.assertRaises(v4.SubscriptionError) as raised:
                store.inbox("subscription_alpha", limit=10)
        with self.assertRaises(v4_cli.V4CLIError) as actual:
            self._run(self._args("inbox"))
        self.assertEqual(str(actual.exception), str(raised.exception))

    def test_context_plan_token_and_inbox_ack_eligibility_are_read_only_parity(self) -> None:
        """This fails if read transports mint the wrong token or advance acknowledgement state."""

        self._seed()
        # Exact signed-token parity requires both transports to share an issuance time.
        with mock.patch.object(v4, "utc_now", return_value="2026-09-04T12:00:00+00:00"):
            with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
                direct_context = store.context_pack(
                    "subscription_alpha", max_items=10, max_chars=12_000
                )
                records = direct_context["records"]
                direct_context["plan_token"] = store.create_plan_token(
                    [record["record_id"] for record in records], fingerprint="parity-plan"
                )
                before_state = store.state()
                before_consumer = store.consumer_status("consumer_alpha")
                direct_inbox = store.inbox("subscription_alpha", limit=10)
                direct_partial_inbox = store.inbox("subscription_alpha", limit=1)
            planned = self._run(self._args("context", plan=True, fingerprint="parity-plan"))
            unplanned = self._run(self._args("context"))
            delivered = self._run(self._args("inbox", limit=10))
            partial_delivery = self._run(self._args("inbox", limit=1))
            with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
                after_state = store.state()
                after_consumer = store.consumer_status("consumer_alpha")
        self.assertEqual(planned, self._wrapped("context", direct_context))
        self.assertNotIn("plan_token", unplanned)
        self.assertEqual(delivered, self._wrapped("inbox", direct_inbox))
        self.assertIsInstance(delivered["ack_token"], str)
        self.assertEqual(partial_delivery, self._wrapped("inbox", direct_partial_inbox))
        self.assertIsNone(partial_delivery["ack_token"])
        self.assertEqual(before_state, after_state)
        self.assertEqual(before_consumer, after_consumer)

    def test_absent_or_unhealthy_store_maps_to_the_cli_operational_error(self) -> None:
        """This fails if a service read leaks a storage failure instead of the stable CLI error."""

        with self.assertRaises(v4_cli.V4CLIError) as raised:
            self._run(self._args("status"))
        self.assertEqual(str(raised.exception), "graph is unavailable")
        self.store_path.write_bytes(b"not a v4 sqlite graph")
        with self.assertRaises(v4_cli.V4CLIError) as unhealthy:
            self._run(self._args("status"))
        self.assertEqual(str(unhealthy.exception), "graph is unavailable")

    def test_service_backed_commands_open_one_readonly_store(self) -> None:
        """This fails if the root opens a handler store in addition to the service store."""

        self._seed()
        commands = (
            self._args("status"),
            self._args("consumer-status", consumer_id="consumer_alpha"),
            self._args("recall", query="record"),
            self._args("context"),
            self._args("changes", since_watermark=0),
            self._args("inbox"),
        )
        original_open = v4_cli._open
        for args in commands:
            with self.subTest(command=args.command):
                with mock.patch.object(v4_cli, "_open", wraps=original_open) as opened:
                    self._run(args)
                self.assertEqual(opened.call_count, 1)
                self.assertTrue(opened.call_args.kwargs["readonly"])

    def test_black_box_parse_and_operational_failures_keep_exit_contracts(self) -> None:
        """This fails if service wiring changes parse or operational stderr/exit behavior."""

        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        parse = subprocess.run(
            [sys.executable, str(CLI_SCRIPT), "history"],
            cwd=SCRIPT_DIRECTORY,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        operational = subprocess.run(
            [sys.executable, str(CLI_SCRIPT), "--lock-timeout", "-1", "doctor"],
            cwd=SCRIPT_DIRECTORY,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(parse.returncode, 2, parse)
        self.assertEqual(parse.stdout, "")
        self.assertEqual(json.loads(parse.stderr)["error_code"], "invalid_arguments")
        self.assertNotIn("Traceback", parse.stderr)
        self.assertEqual(operational.returncode, 1, operational)
        self.assertEqual(operational.stdout, "")
        self.assertEqual(
            json.loads(operational.stderr),
            {"status": "error", "error": "--lock-timeout must be between 0 and 300 seconds"},
        )
        self.assertNotIn("Traceback", operational.stderr)

    def test_black_box_cli_keeps_malformed_signed_cursor_diagnostic_safe(self) -> None:
        """The public CLI must retain its useful cursor diagnostic after service extraction."""

        self._seed()
        config_path = self.store_path.with_name("cli-config.json")
        config_path.write_text(json.dumps({"store_path": str(self.store_path)}), encoding="utf-8")
        set_private_path_mode(config_path, 0o600)
        completed = subprocess.run(
            [
                sys.executable,
                str(CLI_SCRIPT),
                "--config",
                str(config_path),
                "changes",
                "--since-watermark",
                "0",
                "--cursor",
                "not-a-signed-token",
            ],
            cwd=SCRIPT_DIRECTORY,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(1, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertEqual(
            {"status": "error", "error": "Malformed signed token"},
            json.loads(completed.stderr),
        )
        self.assertNotIn(str(self.store_path), completed.stderr)


if __name__ == "__main__":
    unittest.main()
