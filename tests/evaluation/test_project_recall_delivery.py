"""Tests for what a project read delivers and how that is scored."""

from __future__ import annotations

import unittest

from tedd_memory_eval.providers import canonical_payload_json
from tedd_project_recall.contracts import (
    ARM_IDS,
    DEFAULT_BUDGET,
    SEARCH_STEP_BUDGET,
    STANDARD_BUDGET,
    DeliveryObservation,
    ProjectCase,
)
from tedd_project_recall.delivery import (
    CONTEXT_ARM_ID,
    SEARCH_ARM_ID,
    WORKFLOW_ARM_ID,
    McpContextSearchArm,
    McpSearchArm,
    ReadArm,
    arms,
    frozen_packet,
    score_delivery,
    section_items,
    sections_digest,
)

from tests.evaluation.project_recall_support import (
    FakeMcp,
    FakeSandbox,
    MiniProject,
    context_body,
    record,
    search_body,
)


def arm(arm_id: str) -> ReadArm:
    return next(item for item in arms() if item.arm_id == arm_id)


def observe(arm_id: str, fake: FakeSandbox, case: ProjectCase) -> DeliveryObservation:
    delivery = arm(arm_id).read(fake.as_sandbox(), case, DEFAULT_BUDGET)
    return score_delivery(
        arm_id=arm_id, budget=DEFAULT_BUDGET, case=case, repetition=0, delivery=delivery
    )


class ContextSectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mini = MiniProject()
        self.case = self.mini.case(self.mini.loop)
        self.newest = [record(item) for item in self.case.scenario.records[-2:]]
        loop = self.mini.loop.fixture.payload
        self.loop_item = {
            "id": self.mini.loop.record_id,
            "title": loop["title"],
            "status": loop["status"],
            "next_action": loop["next_action"],
            "checkpoint_at": loop["checkpoint_at"],
        }

    def context(self, **sections: object) -> DeliveryObservation:
        fake = FakeSandbox(self.case, context_records=self.newest, sections=sections)
        return observe(CONTEXT_ARM_ID, fake, self.case)

    def test_a_section_item_with_every_needed_field_delivers_the_record(self) -> None:
        seen = self.context(overdue={"items": [self.loop_item]})

        self.assertEqual(
            (seen.records.required_recall, seen.listed_recall, seen.delivered_recall),
            (0.0, 1.0, 1.0),
        )
        self.assertTrue(seen.sufficient)
        self.assertEqual(seen.section_items, 1)

    def test_a_section_item_with_only_a_title_is_listed_but_not_delivered(self) -> None:
        titled = {"id": self.loop_item["id"], "title": self.loop_item["title"]}

        seen = self.context(overdue={"items": [titled]})

        self.assertEqual((seen.listed_recall, seen.delivered_recall), (1.0, 0.0))
        self.assertFalse(seen.sufficient)

    def test_a_section_item_with_a_changed_value_is_not_delivered(self) -> None:
        changed = {**self.loop_item, "next_action": "Something else."}

        seen = self.context(overdue={"items": [changed]})

        self.assertEqual((seen.listed_recall, seen.delivered_recall), (1.0, 0.0))

    def test_a_section_is_recognised_by_shape_not_by_name(self) -> None:
        seen = self.context(a_later_section={"items": [self.loop_item]})

        self.assertEqual(seen.delivered_recall, 1.0)

    def test_a_section_naming_a_record_outside_the_project_is_a_scope_exposure(self) -> None:
        foreign = {"id": "lab_project_elsewhere_0001_note"}

        seen = self.context(overdue={"items": [self.loop_item, foreign]})

        self.assertEqual(seen.section_scope_exposure, 1)
        self.assertFalse(seen.sufficient)

    def test_a_section_left_out_for_lack_of_budget_is_counted_as_withheld(self) -> None:
        seen = self.context(
            overdue={"items": [], "truncated": True, "omitted": True, "reason": "budget_exhausted"},
            preferences={"items": [], "truncated": False, "omitted": False},
        )

        self.assertEqual((seen.withheld_sections, seen.section_items), (1, 0))

    def test_a_stale_record_named_by_a_section_is_counted(self) -> None:
        seen = self.context(overdue={"items": [{"id": self.mini.replaced.record_id}]})

        self.assertEqual(seen.stale_named, 1)

    def test_response_size_covers_the_whole_response_not_only_payloads(self) -> None:
        fake = FakeSandbox(
            self.case,
            context_records=self.newest,
            sections={"overdue": {"items": [self.loop_item]}},
        )

        delivery = arm(CONTEXT_ARM_ID).read(fake.as_sandbox(), self.case, DEFAULT_BUDGET)

        response = fake.call(fake.calls[0])
        self.assertEqual(delivery.response_chars, len(canonical_payload_json(response)))
        self.assertGreater(delivery.response_chars, delivery.packet.payload_chars)
        self.assertGreater(delivery.response_tokens, delivery.packet.evaluation_tokens)

    def test_a_returned_payload_that_differs_from_the_catalog_is_counted(self) -> None:
        altered = record(self.case.scenario.records[-1], summary="Edited after authoring.")
        fake = FakeSandbox(self.case, context_records=[altered, self.newest[0]])

        seen = observe(CONTEXT_ARM_ID, fake, self.case)

        self.assertEqual(seen.payload_mismatches, 1)

    def test_members_without_identified_items_are_not_sections(self) -> None:
        response: dict[str, object] = {
            "waiting": {"commitments": {"unfinished": 1}},
            "selector": {"items": "not a list"},
            "records": [{"id": "a record list is not a section"}],
            "preferences": {"items": [{"label": "no id"}, "text", {"id": 7}]},
            "overdue": {"items": [self.loop_item]},
        }

        self.assertEqual(section_items(response), [self.loop_item])

    def test_section_digest_follows_content_under_an_unchanged_id(self) -> None:
        same_id_new_text = {**self.loop_item, "next_action": "Something else."}
        reordered = dict(reversed(list(self.loop_item.items())))
        digests = {
            label: sections_digest([{"overdue": {"items": items, "omitted": omitted}}])
            for label, items, omitted in (
                ("original", [self.loop_item], False),
                ("same content, other key order", [reordered], False),
                ("new text", [same_id_new_text], False),
                ("withheld", [], True),
            )
        }

        self.assertEqual(digests["original"], digests["same content, other key order"])
        self.assertEqual(len(set(digests.values())), 3)

    def test_a_read_carries_the_digest_of_the_sections_it_read(self) -> None:
        sections: dict[str, object] = {"overdue": {"items": [self.loop_item]}}
        fake = FakeSandbox(self.case, context_records=self.newest, sections=sections)

        seen = observe(CONTEXT_ARM_ID, fake, self.case)

        self.assertEqual(seen.sections_sha256, sections_digest([fake.call(fake.calls[0])]))
        self.assertEqual(seen.allowed_chars, DEFAULT_BUDGET.max_chars)


class OtherArmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mini = MiniProject()
        self.case = self.mini.case(self.mini.current)

    def test_arms_keep_their_frozen_order_and_two_read_sections(self) -> None:
        self.assertEqual(tuple(item.arm_id for item in arms()), ARM_IDS)
        self.assertEqual(
            [item.arm_id for item in arms() if item.reads_sections],
            [CONTEXT_ARM_ID, WORKFLOW_ARM_ID],
        )

    def test_text_search_ignores_section_shaped_members(self) -> None:
        listed = {"items": [{"id": self.mini.current.record_id}]}
        fake = FakeSandbox(
            self.case,
            recall_records=[record(self.case.scenario.records[-1])],
            recall_extra={"index": listed},
        )

        seen = observe("recall_fts_v1", fake, self.case)

        self.assertEqual(
            (seen.section_items, seen.listed_recall, seen.sections_sha256), (0, 0.0, "")
        )

    def test_text_search_that_returns_only_the_replaced_decision_is_insufficient(self) -> None:
        fake = FakeSandbox(self.case, recall_records=[record(self.mini.replaced.fixture)])

        seen = observe("recall_fts_v1", fake, self.case)

        self.assertEqual((seen.stale_named, seen.delivered_recall), (1, 0.0))
        self.assertGreater(seen.records.hard_violation_count, 0)

    def test_exact_id_arm_reads_each_required_record_once_and_is_sufficient(self) -> None:
        fake = FakeSandbox(self.case)

        seen = observe("oracle_v1", fake, self.case)

        self.assertEqual(
            [call[call.index("--id") + 1] for call in fake.calls],
            list(self.case.scenario.required_ids),
        )
        self.assertEqual((seen.delivered_recall, seen.payload_mismatches), (1.0, 0))
        self.assertTrue(seen.sufficient)

    def test_no_memory_arm_reads_nothing_and_delivers_nothing(self) -> None:
        fake = FakeSandbox(self.case)

        seen = observe("no_memory_v1", fake, self.case)

        self.assertEqual(fake.calls, [])
        self.assertEqual((seen.delivered_recall, seen.response_chars), (0.0, 0))
        self.assertFalse(seen.sufficient)


class McpArmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mini = MiniProject()
        self.case = self.mini.case(self.mini.loop, self.mini.current)
        self.sandbox = FakeSandbox(self.case).as_sandbox()
        self.newest = [record(item) for item in self.case.scenario.records[-2:]]
        self.found = [record(self.mini.current.fixture), self.newest[0]]
        loop = self.mini.loop.fixture.payload
        self.loop_item = {
            "id": self.mini.loop.record_id,
            "status": loop["status"],
            "next_action": loop["next_action"],
            "checkpoint_at": loop["checkpoint_at"],
        }

    def score(self, arm_under_test: ReadArm) -> DeliveryObservation:
        return score_delivery(
            arm_id=arm_under_test.arm_id,
            budget=arm_under_test.allowance(STANDARD_BUDGET),
            case=self.case,
            repetition=0,
            delivery=arm_under_test.read(self.sandbox, self.case, STANDARD_BUDGET),
        )

    def test_search_arm_asks_the_question_at_the_budget_and_scores_its_results(self) -> None:
        fake = FakeMcp(search=search_body(self.found))

        seen = self.score(McpSearchArm(connect=fake.connect))

        self.assertEqual(
            fake.calls,
            [
                (
                    "search_memory",
                    {
                        "query": self.case.scenario.query,
                        "limit": STANDARD_BUDGET.max_items,
                        "max_chars": STANDARD_BUDGET.max_chars,
                    },
                )
            ],
        )
        self.assertEqual((seen.records.variant, seen.delivered_recall), (SEARCH_ARM_ID, 0.5))
        self.assertEqual((seen.payload_mismatches, seen.section_items), (0, 0))
        self.assertEqual(seen.allowed_chars, STANDARD_BUDGET.max_chars)

    def test_search_results_are_measured_by_the_frozen_packet_rules(self) -> None:
        foreign_namespace = record(self.mini.current.fixture, namespace="production")
        fake = FakeMcp(search=search_body([foreign_namespace]))

        seen = self.score(McpSearchArm(connect=fake.connect))

        self.assertEqual(seen.records.payload_namespace_exposure, 1)
        self.assertEqual(seen.payload_mismatches, 1)
        self.assertFalse(seen.sufficient)

    def test_search_arm_rejects_a_non_boolean_truncation_marker(self) -> None:
        fake = FakeMcp(search={**search_body(self.found), "truncated": "false"})

        with self.assertRaisesRegex(RuntimeError, "must be a boolean"):
            McpSearchArm(connect=fake.connect).read(self.sandbox, self.case, STANDARD_BUDGET)

    def test_workflow_reads_context_then_searches_at_the_search_defaults(self) -> None:
        fake = FakeMcp(context=context_body(self.newest), search=search_body(self.found))

        McpContextSearchArm(connect=fake.connect).read(self.sandbox, self.case, STANDARD_BUDGET)

        self.assertEqual(
            fake.calls,
            [
                (
                    "recall_context",
                    {
                        "max_items": STANDARD_BUDGET.max_items,
                        "max_chars": STANDARD_BUDGET.max_chars,
                    },
                ),
                (
                    "search_memory",
                    {
                        "query": self.case.scenario.query,
                        "limit": SEARCH_STEP_BUDGET.max_items,
                        "max_chars": SEARCH_STEP_BUDGET.max_chars,
                    },
                ),
            ],
        )

    def test_workflow_delivers_what_either_step_or_a_section_delivers(self) -> None:
        context = context_body(self.newest, overdue={"items": [self.loop_item]})
        fake = FakeMcp(context=context, search=search_body(self.found))

        seen = self.score(McpContextSearchArm(connect=fake.connect))

        self.assertEqual((seen.delivered_recall, seen.records.required_recall), (1.0, 0.5))
        self.assertTrue(seen.sufficient)
        self.assertEqual(
            seen.named_ids,
            (
                self.mini.loop.record_id,
                *(str(item["record_id"]) for item in self.newest),
                self.mini.current.record_id,
            ),
        )

    def test_workflow_pays_twice_for_a_record_both_steps_return(self) -> None:
        bodies = (context_body(self.newest), search_body(self.found))
        fake = FakeMcp(context=bodies[0], search=bodies[1])
        arm_under_test = McpContextSearchArm(connect=fake.connect)

        delivery = arm_under_test.read(self.sandbox, self.case, STANDARD_BUDGET)

        both = [*self.newest, *self.found]
        self.assertEqual(delivery.packet.item_count, 4)
        self.assertEqual(len(delivery.packet.record_ids), 3)
        self.assertEqual(
            delivery.packet.payload_chars,
            sum(len(canonical_payload_json(item["payload"])) for item in both),
        )
        self.assertEqual(
            delivery.response_chars,
            sum(len(canonical_payload_json(body)) for body in bodies),
        )

    def test_workflow_allowance_adds_the_search_step_to_the_budget(self) -> None:
        allowance = McpContextSearchArm().allowance(STANDARD_BUDGET)

        self.assertEqual(
            (allowance.name, allowance.max_items, allowance.max_chars),
            (
                STANDARD_BUDGET.name,
                STANDARD_BUDGET.max_items + SEARCH_STEP_BUDGET.max_items,
                STANDARD_BUDGET.max_chars + SEARCH_STEP_BUDGET.max_chars,
            ),
        )
        self.assertEqual(McpSearchArm().allowance(STANDARD_BUDGET), STANDARD_BUDGET)

    def test_frozen_packet_rejects_a_repeated_record(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "duplicate record IDs"):
            frozen_packet(
                [self.newest[0], self.newest[0]],
                self.case.scenario,
                truncated=False,
                duration_ms=0.0,
            )


if __name__ == "__main__":
    unittest.main()
