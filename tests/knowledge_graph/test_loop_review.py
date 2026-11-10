"""D2 selection against real, reviewed synthetic captures."""

from __future__ import annotations

import copy
import dataclasses
import json
import sqlite3
from unittest import mock

import knowledge_graph as graph
import knowledge_graph_v4 as v4
import knowledge_graph_v4_capture as capture
import knowledge_graph_v4_cli as cli
from knowledge_graph_v4_integration import resolve_loop_v4, sanitize_review_safe_value

from tests.knowledge_graph.test_handoff_receipts import HandoffFixture
from tests.support.graph_cases import PLAN_KEY

NOW = "2026-02-10T12:00:00Z"


class LoopReviewTests(HandoffFixture):
    def test_optional_plan_budgets_recent_records_before_undisclosed_overdue_ids(self):
        self.seed_loops(extra_count=4)
        fingerprint = "synthetic-d2-read"
        with (
            mock.patch.object(v4, "utc_now", return_value=NOW),
            v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store,
        ):
            baseline = dict(store.context_pack("scope_alpha", max_items=1))
            record_ids = [item["record_id"] for item in baseline["records"]]
            baseline["plan_token"] = store.create_plan_token(record_ids, fingerprint=fingerprint)
            # The waiting counts and the fact sheet are reserved before recent records.
            baseline["waiting"] = store.waiting_summary("scope_alpha")
            baseline["current_state"] = store.current_state("scope_alpha")
        baseline["overdue"] = {
            "items": [],
            "truncated": True,
            "omitted": True,
            "reason": "budget_exhausted",
        }
        baseline["preferences"] = {
            "label": "stated preferences, with dates",
            "items": [],
            "truncated": False,
            "omitted": False,
        }
        budget = len(json.dumps(baseline, ensure_ascii=False))
        args = graph.build_parser().parse_args(
            [
                "context",
                "--subscription-id",
                "scope_alpha",
                "--max-items",
                "1",
                "--max-chars",
                str(budget),
                "--plan",
                "--fingerprint",
                fingerprint,
                "--mode",
                "operator_full",
            ]
        )
        with mock.patch.object(v4, "utc_now", return_value=NOW):
            result = cli.handle_v4_command(args, db_path=self.store_path, plan_hmac_key=PLAN_KEY)
        payload = {key: value for key, value in result.items() if key not in {"command", "status"}}
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False)), budget)
        self.assertEqual(result["records"], baseline["records"])
        self.assertTrue(result["overdue"]["omitted"])
        token = v4.verify_plan_token(result["plan_token"], PLAN_KEY)
        self.assertEqual([item["record_id"] for item in token["read_set"]], record_ids)

    def test_cli_context_budgets_redaction_expansion_before_total_and_overdue_caps(self):
        self.seed_loops(next_action=" ".join(['"/a"'] * 40))
        for budget in (2_590, 50_000):
            with self.subTest(budget=budget):
                result = self.context(budget)
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), budget)
                self.assertLessEqual(len(json.dumps(result["overdue"], ensure_ascii=False)), 1_500)
                self.assertNotIn('"/a"', json.dumps(result))

    def test_context_sanitizes_whole_candidates_without_resanitizing_disclosed_values(self):
        self.seed_loops(extra_count=4)

        def sanitize_once(value):
            self.assertIn("records", value)
            self.assertIn("overdue", value)
            self.assertNotIn("sanitized_once", value)
            return {**sanitize_review_safe_value(value), "sanitized_once": True}

        args = graph.build_parser().parse_args(
            ["context", "--subscription-id", "scope_alpha", "--max-chars", "2590"]
        )
        with mock.patch.object(v4, "utc_now", return_value=NOW):
            result = cli.handle_v4_command(
                args,
                db_path=self.store_path,
                plan_hmac_key=PLAN_KEY,
                review_sanitizer=sanitize_once,
            )
        self.assertTrue(result["sanitized_once"])
        payload = {key: value for key, value in result.items() if key not in {"command", "status"}}
        self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False)), 2_590)

    def test_optional_context_plan_covers_displayed_overdue_loops_outside_recent_records(self):
        self.seed_loops()
        args = graph.build_parser().parse_args(
            [
                "context",
                "--subscription-id",
                "scope_alpha",
                "--max-items",
                "1",
                "--max-chars",
                "20000",
                "--plan",
                "--fingerprint",
                "synthetic-d2-read",
                "--mode",
                "operator_full",
            ]
        )
        with mock.patch.object(v4, "utc_now", return_value=NOW):
            result = cli.handle_v4_command(args, db_path=self.store_path, plan_hmac_key=PLAN_KEY)
        plan = v4.verify_plan_token(result["plan_token"], PLAN_KEY)
        covered = {item["record_id"] for item in plan["read_set"]}
        visible = {item["id"] for item in result["overdue"]["items"]}
        self.assertTrue(visible - {item["record_id"] for item in result["records"]})
        self.assertLessEqual(visible, covered)

    def test_cli_uses_the_existing_consumer_and_refuses_another_consumers_subscription(self):
        self.seed_loops()
        args = graph.build_parser().parse_args(
            [
                "loops-review",
                "--consumer-id",
                "consumer_alpha",
                "--subscription-id",
                "scope_alpha",
                "--limit",
                "2",
                "--mode",
                "operator_full",
            ]
        )
        with mock.patch.object(v4, "utc_now", return_value=NOW):
            result = cli.handle_v4_command(args, db_path=self.store_path, plan_hmac_key=PLAN_KEY)
        self.assertEqual([item["id"] for item in result["items"]], ["loop_a", "loop_b"])
        args.consumer_id = "consumer_beta"
        with self.assertRaisesRegex(ValueError, "consumer"):
            cli.handle_v4_command(args, db_path=self.store_path, plan_hmac_key=PLAN_KEY)

    def test_loop_selection_never_writes_or_acknowledges(self):
        self.seed_loops()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertTrue(
                callable(getattr(store, "review_loops", None)), "D2 loop selection missing"
            )
            before = store.state()
            before_consumer = store.consumer_status("consumer_alpha")
            writes = []

            def no_writes(action, arg1, arg2, database, trigger):
                if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                    writes.append((action, arg1))
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            store.connection.set_authorizer(no_writes)
            with mock.patch.object(v4, "utc_now", return_value=NOW):
                result = store.review_loops("scope_alpha")
            self.assertTrue(result["items"])
            self.assertEqual(writes, [])
            self.assertEqual(store.state(), before)
            self.assertEqual(store.consumer_status("consumer_alpha"), before_consumer)

    def seed_loops(self, *, hidden_count: int = 0, extra_count: int = 0, next_action=None):
        loops = [
            self.loop("loop_a", checkpoint="2026-01-01T12:00:00Z"),
            self.loop("loop_b", checkpoint="2026-01-01T12:00:00Z", status="candidate"),
            self.loop("loop_age"),
            self.loop("loop_future", checkpoint="2026-02-11T12:00:00Z"),
            self.loop("loop_now", checkpoint=NOW),
            self.loop("loop_other", checkpoint="2025-01-01T12:00:00Z", subject="entity_other"),
        ]
        loops.extend(
            {
                **self.loop(f"loop_hidden_{index}", checkpoint="2025-01-01T12:00:00Z"),
                "sensitive": True,
            }
            for index in range(hidden_count)
        )
        loops.extend(
            self.loop(f"loop_extra_{index:02}", checkpoint="2026-01-02T12:00:00Z")
            for index in range(extra_count)
        )
        if next_action is not None:
            loops[0]["next_action"] = next_action
        request = self.request("unused", prompt=None)
        packet = json.loads(request.input_bytes)
        packet.pop("events")
        packet["entities"] = [{"id": "entity_other", "type": "project", "name": "Other project"}]
        packet["open_loops"] = loops
        request = dataclasses.replace(request, input_bytes=json.dumps(packet).encode())
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(store, request)
            store.subscribe(
                "consumer_alpha", {"entity_ids": ["entity_project"]}, subscription_id="scope_alpha"
            )
            self.assertTrue(store.validate(full=True)["valid"])

    def context(self, max_chars=50_000):
        args = graph.build_parser().parse_args(
            [
                "context",
                "--subscription-id",
                "scope_alpha",
                "--max-chars",
                str(max_chars),
                "--mode",
                "review_safe",
            ]
        )
        with mock.patch.object(v4, "utc_now", return_value=NOW):
            result = cli.handle_v4_command(
                args,
                db_path=self.store_path,
                plan_hmac_key=PLAN_KEY,
                review_sanitizer=sanitize_review_safe_value,
            )
        return {key: value for key, value in result.items() if key not in {"command", "status"}}

    def test_context_preserves_records_and_caps_overdue_detail_after_sanitizing(self):
        self.seed_loops(extra_count=24)
        result = self.context()
        section = result.pop("overdue")
        self.assertEqual([], result.pop("preferences")["items"])
        self.assertEqual([], result.pop("current_state")["items"])
        self.assertEqual(29, result.pop("waiting")["commitments"]["unfinished"])
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            baseline = sanitize_review_safe_value(
                store.context_pack("scope_alpha", max_chars=50_000)
            )
        self.assertEqual(result, baseline)
        self.assertLessEqual(len(section["items"]), 5)
        self.assertLessEqual(len(json.dumps(section, ensure_ascii=False)), 1_500)
        self.assertTrue(section["items"])
        self.assertTrue(section["truncated"])

    def test_exhausted_context_budget_reports_omission_not_a_false_empty_result(self):
        self.seed_loops(next_action="Synthetic follow-up details " * 100)
        result = self.context(1_000)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 1_000)
        self.assertEqual(
            result["overdue"],
            {"items": [], "truncated": True, "omitted": True, "reason": "budget_exhausted"},
        )

    def test_flags_are_bounded_and_do_not_change_stored_checkpoints(self):
        self.seed_loops()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            before = copy.deepcopy(store.get_record("loop_a"))
            with mock.patch.object(v4, "utc_now", return_value=NOW):
                result = store.review_loops("scope_alpha", overdue_days=41, age_days=40)
                self.assertEqual(result["items"], [])
                for options in (
                    {"limit": 0},
                    {"limit": 21},
                    {"age_days": 0},
                    {"overdue_days": -1},
                    {"limit": True},
                    {"age_days": 36_501},
                ):
                    with self.subTest(options=options), self.assertRaises(ValueError):
                        store.review_loops("scope_alpha", **options)
            self.assertEqual(before, store.get_record("loop_a"))

    def test_default_limit_returns_twenty_and_honestly_reports_more(self):
        self.seed_loops(extra_count=24)
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            with mock.patch.object(v4, "utc_now", return_value=NOW):
                result = store.review_loops("scope_alpha")
            self.assertEqual(len(result["items"]), 20)
            self.assertEqual(
                [item["id"] for item in result["items"][:3]], ["loop_a", "loop_b", "loop_extra_00"]
            )
            self.assertTrue(result["truncated"])

    def test_resolved_loops_are_excluded_and_undated_age_uses_first_observation(self):
        self.seed_loops()
        request = self.request("unused", prompt=None)
        packet = json.loads(request.input_bytes)
        packet.pop("events")
        packet["source"]["observed_at"] = "2026-02-09T12:00:00Z"
        packet["open_loops"] = [
            {**self.loop("loop_age"), "summary": "Recent update to an old loop"}
        ]
        request = dataclasses.replace(request, input_bytes=json.dumps(packet).encode())
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            self.apply(store, request)
        args = graph.build_parser().parse_args(
            [
                "resolve-loop",
                "--id",
                "loop_a",
                "--resolution",
                "Synthetic owner confirmed completion",
                "--producer-agent-id",
                "agent_alpha",
                "--task-id",
                "task_alpha",
                "--producer-run-id",
                "run_resolve",
                "--based-on-watermark",
                "3",
            ]
        )
        result = resolve_loop_v4(self.store_path, args)
        self.assertEqual(result["result"]["status"], "resolved")
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            with mock.patch.object(v4, "utc_now", return_value=NOW):
                result = store.review_loops("scope_alpha")
            self.assertEqual([item["id"] for item in result["items"]], ["loop_b", "loop_age"])
            self.assertEqual(result["items"][1]["days_overdue"], 8)

    def test_checkpoint_and_age_thresholds_are_strict(self):
        self.seed_loops()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            for timestamp, expected in (
                ("2026-02-02T12:00:00Z", ["loop_a", "loop_b"]),
                ("2026-02-02T12:00:01Z", ["loop_a", "loop_b", "loop_age"]),
            ):
                with (
                    self.subTest(timestamp=timestamp),
                    mock.patch.object(v4, "utc_now", return_value=timestamp),
                ):
                    result = store.review_loops("scope_alpha")
                    self.assertEqual([item["id"] for item in result["items"]], expected)

    @staticmethod
    def loop(identifier, *, checkpoint=None, status="open", subject="entity_project"):
        return {
            "id": identifier,
            "title": identifier,
            "summary": "Synthetic follow-up",
            "created_at": "2026-01-03T12:00:00Z",
            "checkpoint_at": checkpoint,
            "status": status,
            "entity_refs": [subject],
            "owner_refs": [],
            "next_action": "Ask for a disposition",
            "terminal_condition": "Owner confirms disposition",
        }

    def test_selection_preserves_scope_status_and_orders_oldest_then_id(self):
        self.seed_loops()
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertTrue(
                callable(getattr(store, "review_loops", None)), "D2 loop selection missing"
            )
            with mock.patch.object(v4, "utc_now", return_value=NOW):
                result = store.review_loops("scope_alpha")
            self.assertEqual(
                [item["id"] for item in result["items"]], ["loop_a", "loop_b", "loop_age"]
            )
            self.assertEqual([item["days_overdue"] for item in result["items"]], [40, 40, 8])
            self.assertEqual(
                [item["status"] for item in result["items"]], ["open", "candidate", "open"]
            )
            self.assertFalse(result["truncated"])

    def test_sensitive_and_other_scope_records_cannot_displace_visible_items(self):
        self.seed_loops(hidden_count=24)
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertTrue(
                callable(getattr(store, "review_loops", None)), "D2 loop selection missing"
            )
            with mock.patch.object(v4, "utc_now", return_value=NOW):
                result = store.review_loops("scope_alpha", limit=2)
            self.assertEqual([item["id"] for item in result["items"]], ["loop_a", "loop_b"])
            self.assertTrue(result["truncated"])

    def test_missing_checkpoint_warning_does_not_require_invented_dates(self):
        request = self.request("unused", prompt=None)
        packet = json.loads(request.input_bytes)
        packet.pop("events")
        packet["open_loops"] = [self.loop("loop_new")]
        request = dataclasses.replace(request, input_bytes=json.dumps(packet).encode())
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            before = store.state()
            preview = capture.execute_capture(store, request)
            self.assertIn("loop_without_checkpoint", preview["policy_adjustments"])
            self.assertEqual(before, store.state())
            saved = self.apply(store, request)
            self.assertEqual(saved["status"], "captured")
            self.assertIsNone(store.get_record("loop_new")["payload"]["checkpoint_at"])
