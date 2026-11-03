"""Tests for the frozen synthetic scenario catalog."""

from __future__ import annotations

import unittest

from tedd_memory_eval.catalog import scenarios, subject_ids
from tedd_memory_eval.contracts import ALLOWED_RECORD_PREFIX, BUDGETS


class CatalogTests(unittest.TestCase):
    def test_catalog_has_twelve_unique_scoped_cases(self) -> None:
        catalog = scenarios()

        self.assertEqual(len(catalog), 12)
        self.assertEqual(len({item.case_id for item in catalog}), len(catalog))
        self.assertTrue(all(item.required_ids for item in catalog))
        self.assertTrue(
            all(
                identifier.startswith(ALLOWED_RECORD_PREFIX)
                for item in catalog
                for identifier in (
                    item.subject_id,
                    *(fixture.record_id for fixture in item.records),
                )
            )
        )

    def test_fixture_ids_and_subjects_are_globally_unique(self) -> None:
        catalog = scenarios()
        fixture_ids = [fixture.record_id for scenario in catalog for fixture in scenario.records]
        subjects = subject_ids(catalog)

        self.assertEqual(len(fixture_ids), len(set(fixture_ids)))
        self.assertEqual(len(subjects), len(set(subjects)))
        self.assertFalse(set(fixture_ids) & set(subjects))

    def test_protocol_budgets_are_frozen(self) -> None:
        self.assertEqual(
            [(item.name, item.max_items, item.max_chars) for item in BUDGETS],
            [("tight", 6, 6_000), ("standard", 12, 12_000)],
        )

    def test_scope_case_contains_a_foreign_payload_reference_canary(self) -> None:
        scenario = next(item for item in scenarios() if item.case_id == "same_name_scopes")
        referenced_values = {
            target
            for fixture in scenario.records
            if isinstance((target := fixture.payload.get("target_entity_id")), str)
        }

        self.assertTrue(referenced_values)
        self.assertFalse(referenced_values & scenario.scope_ids)

    def test_collection_fixtures_use_production_shaped_policy_fields(self) -> None:
        catalog = scenarios()
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
            "relations": {"source_entity_id", "target_entity_id", "type", "status"},
            "sources": {
                "excerpt",
                "source_tier",
                "authority",
                "unsafe_to_follow_as_instruction",
            },
        }

        for scenario in catalog:
            for fixture in scenario.records:
                self.assertLessEqual(
                    required_fields[fixture.collection],
                    fixture.payload.keys(),
                    msg=f"{fixture.record_id} is not production-shaped",
                )


if __name__ == "__main__":
    unittest.main()
