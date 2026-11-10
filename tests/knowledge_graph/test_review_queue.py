"""The follow-through queue: what waits on a person inside one task scope."""

from __future__ import annotations

import json
import sqlite3
from unittest import mock

import knowledge_graph_v4 as v4

from tests.knowledge_graph.outcome_support import (
    INFERENCE,
    LATER,
    NOW,
    SCOPE,
    USER,
    OutcomeFixture,
)
from tests.support.graph_cases import PLAN_KEY


class ReviewQueueFixture(OutcomeFixture):
    """Six unfinished commitments and two undecided proposals in the project scope.

    One finished loop, one settled proposal, and foreign or sensitive records
    surround them so every test also proves what the queue leaves out.
    """

    def setUp(self) -> None:
        super().setUp()
        self.save(
            {
                **self.packet(),
                "entities": [
                    {"id": "entity_other", "type": "project", "name": "Other project"},
                    {"id": "entity_agent", "type": "agent", "name": "Agent Alpha"},
                    {
                        "id": "entity_person",
                        "type": "person",
                        "namespace": "entity_project",
                        "name": "Sam Example",
                    },
                ],
                "open_loops": [
                    self.loop("loop_overdue_old", checkpoint_at="2026-01-05T12:00:00Z"),
                    self.loop(
                        "loop_overdue_new",
                        checkpoint_at="2026-02-01T12:00:00Z",
                        owner_refs=["entity_agent"],
                    ),
                    self.loop("loop_undated_old"),
                    self.loop(
                        "loop_soon",
                        checkpoint_at="2026-02-12T12:00:00Z",
                        owner_refs=["entity_person"],
                    ),
                    self.loop(
                        "loop_candidate",
                        status="candidate",
                        checkpoint_at="2026-03-01T12:00:00Z",
                    ),
                    self.loop("loop_far", checkpoint_at="2026-06-01T12:00:00Z"),
                    self.loop("loop_done", status="resolved"),
                    self.loop(
                        "loop_other",
                        checkpoint_at="2025-01-01T12:00:00Z",
                        entity_refs=["entity_other"],
                    ),
                    self.loop("loop_hidden", checkpoint_at="2025-01-01T12:00:00Z", sensitive=True),
                ],
                "decisions": [self.decision("decision_accepted")],
            }
        )
        proposals = self.packet(tier=INFERENCE)
        proposals["decisions"] = [
            self.decision("decision_idea_old", decided_at="2025-12-01T12:00:00Z"),
            self.decision("decision_idea_new", decided_at="2026-02-05T12:00:00Z"),
            self.decision("decision_idea_settled"),
            self.decision("decision_idea_other", entity_refs=["entity_other"]),
            self.decision("decision_idea_hidden", sensitive=True),
        ]
        self.save(proposals, ceiling=INFERENCE)
        verdict = self.packet(observed_at=LATER, tier=USER)
        verdict["decisions"] = [self.decision("decision_idea_settled", status="rejected")]
        self.save(verdict, ceiling=USER)
        self.subscribe()

    def queue(self, **options: int) -> dict[str, object]:
        return self.read("review_queue", SCOPE, **options)

    @staticmethod
    def ids(section: object) -> list[str]:
        assert isinstance(section, dict)
        return [item["id"] for item in section["items"]]


class ReviewQueueTests(ReviewQueueFixture):
    def test_commitments_come_overdue_first_then_by_nearest_checkpoint(self) -> None:
        commitments = self.queue()["commitments"]

        self.assertEqual(
            self.ids(commitments),
            [
                "loop_overdue_old",
                "loop_overdue_new",
                "loop_undated_old",
                "loop_soon",
                "loop_candidate",
                "loop_far",
            ],
        )
        self.assertEqual(
            [(item["overdue"], item["days_overdue"]) for item in commitments["items"]],
            [(True, 36), (True, 9), (True, 8), (False, None), (False, None), (False, None)],
        )
        self.assertFalse(commitments["truncated"])

    def test_each_commitment_says_whether_nobody_an_agent_or_a_named_owner_holds_it(self) -> None:
        owners = {item["id"]: item["owner_kind"] for item in self.queue()["commitments"]["items"]}

        self.assertEqual(owners["loop_overdue_old"], "none")
        self.assertEqual(owners["loop_overdue_new"], "agent")
        self.assertEqual(owners["loop_soon"], "named")

    def test_proposals_wait_oldest_first_and_a_settled_one_leaves_the_queue(self) -> None:
        proposals = self.queue()["proposals"]

        self.assertEqual(self.ids(proposals), ["decision_idea_old", "decision_idea_new"])
        self.assertEqual([item["age_days"] for item in proposals["items"]], [71, 5])
        self.assertEqual({item["status"] for item in proposals["items"]}, {"proposed"})

    def test_the_summary_stays_exact_when_the_lists_are_cut_short(self) -> None:
        queue = self.queue(limit=1)

        self.assertEqual(len(queue["commitments"]["items"]), 1)
        self.assertTrue(queue["commitments"]["truncated"])
        self.assertTrue(queue["proposals"]["truncated"])
        self.assertEqual(
            queue["summary"]["commitments"],
            {
                "unfinished": 6,
                "overdue": 3,
                "unassigned": 4,
                "agent_owned": 1,
                "named_owner": 1,
            },
        )
        self.assertEqual(
            queue["summary"]["proposals"], {"undecided": 2, "older_than_days": 30, "older": 1}
        )

    def test_finished_foreign_and_sensitive_records_never_enter_the_queue(self) -> None:
        queue = self.queue(limit=50)

        listed = set(self.ids(queue["commitments"])) | set(self.ids(queue["proposals"]))
        self.assertFalse(
            listed
            & {
                "loop_done",
                "loop_other",
                "loop_hidden",
                "decision_accepted",
                "decision_idea_settled",
                "decision_idea_other",
                "decision_idea_hidden",
            }
        )

    def test_many_foreign_and_sensitive_records_change_neither_items_nor_counts(self) -> None:
        before = self.queue(limit=3)
        crowd = self.packet(observed_at=LATER)
        crowd["open_loops"] = [
            self.loop(
                f"loop_crowd_{index:02}",
                checkpoint_at="2024-01-01T12:00:00Z",
                **({"sensitive": True} if index % 2 else {"entity_refs": ["entity_other"]}),
            )
            for index in range(30)
        ]
        self.save(crowd)

        self.assertEqual(self.queue(limit=3), before)

    def test_reading_the_queue_writes_nothing(self) -> None:
        writes: list[tuple[int, str | None]] = []

        def deny_writes(action: int, target: str | None, *_: object) -> int:
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                writes.append((action, target))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        with (
            mock.patch.object(v4, "utc_now", return_value=NOW),
            v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store,
        ):
            before = (store.state(), store.consumer_status("consumer_alpha"))
            store.connection.set_authorizer(deny_writes)

            queue = store.review_queue(SCOPE)

            # The authorizer stays installed: Python 3.10 cannot clear one, and
            # these reads are allowed by it.
            after = (store.state(), store.consumer_status("consumer_alpha"))
        self.assertTrue(queue["commitments"]["items"])
        self.assertEqual(writes, [])
        self.assertEqual(after, before)

    def test_the_limit_is_bounded_and_a_boolean_is_not_a_count(self) -> None:
        for limit in (0, 51, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.queue(limit=limit)

        self.assertEqual(len(self.queue(limit=50)["commitments"]["items"]), 6)


class ReviewQueueCommandTests(ReviewQueueFixture):
    def command(self, *extra: str, consumer: str = "consumer_alpha") -> dict[str, object]:
        return self.run_cli(
            "review-queue", "--consumer-id", consumer, "--subscription-id", SCOPE, *extra
        )

    def test_the_command_returns_the_queue_for_the_owning_consumer(self) -> None:
        result = self.command("--limit", "2")

        self.assertEqual(self.ids(result["commitments"]), ["loop_overdue_old", "loop_overdue_new"])
        self.assertEqual(result["summary"]["commitments"]["unfinished"], 6)

    def test_another_consumer_cannot_read_this_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "consumer"):
            self.command(consumer="consumer_beta")

    def test_the_command_refuses_a_limit_outside_its_range(self) -> None:
        for limit in ("0", "51"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.command("--limit", limit)


class WaitingContextTests(ReviewQueueFixture):
    def test_task_context_carries_the_exact_waiting_counts(self) -> None:
        context = self.context()

        self.assertEqual(context["waiting"], self.read("waiting_summary", SCOPE))
        self.assertEqual(context["waiting"]["commitments"]["overdue"], 3)

    def test_the_counts_come_before_recent_records_in_the_context(self) -> None:
        sections = list(self.context())

        self.assertLess(sections.index("waiting"), sections.index("records"))
        self.assertEqual(sections[0], "preferences")

    def test_a_budget_with_room_for_metadata_only_reports_the_counts_as_omitted(self) -> None:
        exhausted = {"truncated": True, "omitted": True, "reason": "budget_exhausted"}
        bare = {
            **self.context(),
            "records": [],
            "item_count": 0,
            "char_count": 0,
            "truncated": True,
            "waiting": exhausted,
            "overdue": {"items": [], **exhausted},
        }

        context = self.context(max_chars=len(json.dumps(bare, ensure_ascii=False)))

        self.assertEqual(context, bare)
