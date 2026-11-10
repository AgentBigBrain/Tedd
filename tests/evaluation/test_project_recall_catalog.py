"""Tests for the frozen project recall catalog."""

from __future__ import annotations

import unittest
from dataclasses import replace

from tedd_memory_eval.contracts import ALLOWED_RECORD_PREFIX, PayloadAssertion
from tedd_project_recall.catalog import (
    SyntheticProject,
    cases_digest,
    full_context,
    project_case,
    project_cases,
    validate_cases,
)
from tedd_project_recall.contracts import DEFAULT_BUDGET, STANDARD_BUDGET, ProjectCase

from tests.evaluation.project_recall_support import MiniProject

FROZEN_CASES_SHA256 = "f38329b084ad0d284d3c7050b775f68c2efb7268d2e28f7dd320bf334680f276"


def rank_from_newest(case: ProjectCase, record_id: str) -> int:
    """Return one for the newest record, two for the one before it, and so on."""

    ids = [item.record_id for item in case.scenario.records]
    return len(ids) - ids.index(record_id)


class FrozenCatalogTests(unittest.TestCase):
    cases: tuple[ProjectCase, ...]

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = project_cases()

    def test_catalog_digest_sizes_and_partitions_are_frozen(self) -> None:
        self.assertEqual(cases_digest(self.cases), FROZEN_CASES_SHA256)
        self.assertEqual(
            {case.case_id: (len(case.scenario.records), case.placement) for case in self.cases},
            {
                "old_decision": (300, "outside_recall"),
                "decisions_over_time": (650, "outside_recall"),
                "replaced_decision": (300, "outside_recall"),
                "open_commitment": (250, "outside_recall"),
                "old_note": (250, "outside_recall"),
                "recent_work": (300, "recent_work"),
                "small_project": (30, "fits_one_recall"),
            },
        )
        self.assertEqual(
            sorted(
                case.case_id for case in self.cases if case.scenario.partition == "confirmation"
            ),
            ["decisions_over_time", "old_note", "small_project"],
        )
        self.assertEqual(
            [case.case_id for case in self.cases if case.scenario.critical],
            ["old_decision", "open_commitment"],
        )

    def test_generation_is_identical_on_every_call(self) -> None:
        self.assertEqual(project_cases(), self.cases)

    def test_every_identifier_is_synthetic_and_globally_unique(self) -> None:
        identifiers = [
            identifier
            for case in self.cases
            for identifier in (
                case.scenario.subject_id,
                *(item.record_id for item in case.scenario.records),
            )
        ]

        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertTrue(all(item.startswith(ALLOWED_RECORD_PREFIX) for item in identifiers))

    def test_oversized_cases_keep_required_records_beyond_the_default_item_limit(self) -> None:
        for case in self.cases:
            if case.control:
                continue
            for record_id in case.scenario.required_ids:
                with self.subTest(case=case.case_id):
                    self.assertGreater(rank_from_newest(case, record_id), DEFAULT_BUDGET.max_items)

    def test_control_cases_keep_required_records_within_reach(self) -> None:
        by_case = {case.case_id: case for case in self.cases}
        recent, small = by_case["recent_work"], by_case["small_project"]

        self.assertTrue(
            all(rank_from_newest(recent, item) <= 6 for item in recent.scenario.required_ids)
        )
        self.assertLess(len(small.scenario.records), DEFAULT_BUDGET.max_items)
        self.assertLess(full_context(small).payload_chars, DEFAULT_BUDGET.max_chars)
        self.assertGreater(
            rank_from_newest(small, small.scenario.required_ids[0]), STANDARD_BUDGET.max_items
        )

    def test_projects_keep_the_documented_record_mix(self) -> None:
        case = next(item for item in self.cases if item.case_id == "decisions_over_time")
        total = len(case.scenario.records)
        shares = {
            collection: sum(item.collection == collection for item in case.scenario.records) / total
            for collection in ("observations", "receipts", "decisions", "events", "open_loops")
        }

        self.assertEqual(
            {collection: round(share, 2) for collection, share in shares.items()},
            {
                "observations": 0.45,
                "receipts": 0.25,
                "decisions": 0.15,
                "events": 0.1,
                "open_loops": 0.05,
            },
        )
        self.assertEqual(round(full_context(case).payload_chars / total, -1), 930)

    def test_fixtures_use_production_shaped_policy_fields(self) -> None:
        required_fields = {
            "decisions": {"status", "authority_state", "source_tier", "decided_at"},
            "events": {"kind", "status", "occurred_at"},
            "observations": {"family", "key", "value", "source_tier", "observed_at"},
            "open_loops": {
                "status",
                "authority_state",
                "source_tier",
                "next_action",
                "checkpoint_at",
            },
            "receipts": {"kind", "verified", "status", "verified_at"},
        }

        for case in self.cases:
            for fixture in case.scenario.records:
                self.assertLessEqual(
                    required_fields[fixture.collection],
                    fixture.payload.keys(),
                    msg=f"{case.case_id} holds a record that is not production-shaped",
                )

    def test_the_replaced_decision_points_at_its_successor_and_is_stale(self) -> None:
        case = next(item for item in self.cases if item.case_id == "replaced_decision")
        fixtures = {item.record_id: item for item in case.scenario.records}
        (current_id,), (stale_id,) = case.scenario.required_ids, case.scenario.stale_ids

        self.assertEqual(fixtures[stale_id].payload["status"], "superseded")
        self.assertEqual(fixtures[stale_id].payload["superseded_by"], current_id)
        self.assertIn((current_id, "superseded_by"), fixtures[stale_id].refs)
        self.assertEqual(case.scenario.current_before_stale, ((current_id, stale_id),))
        self.assertLess(
            str(fixtures[stale_id].payload["decided_at"]),
            str(fixtures[current_id].payload["decided_at"]),
        )

    def test_no_record_is_indexed_under_its_question(self) -> None:
        for case in self.cases:
            with self.subTest(case=case.case_id):
                self.assertFalse(
                    any(case.scenario.query in item.search_text for item in case.scenario.records)
                )

    def test_full_context_counts_every_authored_record(self) -> None:
        case = next(item for item in self.cases if item.case_id == "replaced_decision")

        reference = full_context(case)

        self.assertEqual((reference.record_count, reference.stale_records), (300, 1))
        self.assertGreater(reference.payload_chars, DEFAULT_BUDGET.max_chars)
        self.assertGreater(reference.evaluation_tokens, 0)


class CatalogValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mini = MiniProject()

    def test_a_recent_record_declared_outside_recall_is_rejected(self) -> None:
        case = self.mini.case(self.mini.current)

        with self.assertRaisesRegex(ValueError, "placement"):
            validate_cases((case,))

    def test_a_repeated_identifier_is_rejected(self) -> None:
        case = project_cases()[-1]

        with self.assertRaisesRegex(ValueError, "repeats an identifier"):
            validate_cases((case, case))

    def test_an_assertion_that_disagrees_with_its_fixture_is_rejected(self) -> None:
        case = project_cases()[-1]
        (record_id,) = case.scenario.required_ids
        wrong = replace(
            case.scenario,
            required_assertions=(PayloadAssertion(record_id, "status", "rejected"),),
        )

        with self.assertRaisesRegex(ValueError, "payload assertions"):
            validate_cases((replace(case, scenario=wrong),))

    def test_planted_records_must_belong_to_the_project_and_not_collide(self) -> None:
        project = SyntheticProject("small", 20)
        other = SyntheticProject("other", 20)
        first = project.note(4, "first", family="finding", key="first", value="One.")
        collisions = {
            "same position": (
                first,
                project.note(4, "second", family="finding", key="b", value="Two."),
            ),
            "outside the project": (
                project.note(20, "late", family="finding", key="c", value="Three."),
            ),
            "another project": (
                other.note(5, "foreign", family="finding", key="d", value="Four."),
            ),
        }

        for label, planted in collisions.items():
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "plants"):
                project_case(
                    project,
                    query="What was found?",
                    placement="fits_one_recall",
                    required=planted,
                    family="synthetic",
                )


if __name__ == "__main__":
    unittest.main()
