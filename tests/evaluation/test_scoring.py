"""Tests for retrieval metrics and fail-closed safety detectors."""

from __future__ import annotations

import unittest
from dataclasses import replace

from tedd_memory_eval.catalog import scenarios
from tedd_memory_eval.contracts import BUDGETS, ContextPacket
from tedd_memory_eval.scoring import hard_detector_self_test, score_packet


class ScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = scenarios()
        self.by_case = {item.case_id: item for item in self.catalog}

    @staticmethod
    def packet(*record_ids: str, chars: int = 100) -> ContextPacket:
        return ContextPacket(
            record_ids=record_ids,
            payload_chars=chars,
            reported_chars=chars,
            item_count=len(record_ids),
            truncated=False,
            duration_ms=1.0,
            evaluation_tokens=len(record_ids),
        )

    def test_perfect_packet_is_sufficient(self) -> None:
        scenario = self.by_case["provenance_explanation"]

        result = score_packet(
            variant="oracle_v1",
            budget=BUDGETS[0],
            scenario=scenario,
            repetition=0,
            packet=self.packet(*scenario.required_ids),
        )

        self.assertEqual(result.required_recall, 1.0)
        self.assertEqual(result.precision, 1.0)
        self.assertTrue(result.sufficient)
        self.assertEqual(result.worst_required_rank, 2)

    def test_stale_record_is_cleanliness_signal_not_automatic_failure(self) -> None:
        scenario = self.by_case["current_correction"]

        result = score_packet(
            variant="context_recency_v1",
            budget=BUDGETS[0],
            scenario=scenario,
            repetition=0,
            packet=self.packet(*scenario.required_ids, *scenario.stale_ids),
        )

        self.assertEqual(result.required_recall, 1.0)
        self.assertEqual(result.stale_exposure, 1)
        self.assertTrue(result.sufficient)
        self.assertEqual(result.current_over_stale_accuracy, 1.0)

    def test_surviving_unsafe_text_fails_safe_sufficiency(self) -> None:
        scenario = self.by_case["untrusted_instruction"]
        packet = self.packet(*scenario.required_ids)
        leaking = replace(packet, untrusted_text_exposure=1)

        result = score_packet(
            variant="context_recency_v1",
            budget=BUDGETS[0],
            scenario=scenario,
            repetition=0,
            packet=leaking,
        )

        self.assertFalse(result.sufficient)
        self.assertEqual(result.untrusted_text_exposure, 1)

    def test_subject_entity_is_in_scope_but_other_lab_fixture_is_not(self) -> None:
        scenario = self.by_case["project_resumption"]
        other = self.by_case["changed_preference"].required_ids[0]

        subject_result = score_packet(
            variant="context_recency_v1",
            budget=BUDGETS[0],
            scenario=scenario,
            repetition=0,
            packet=self.packet(*scenario.required_ids, scenario.subject_id),
        )
        foreign_result = score_packet(
            variant="context_recency_v1",
            budget=BUDGETS[0],
            scenario=scenario,
            repetition=0,
            packet=self.packet(*scenario.required_ids, other),
        )

        self.assertEqual(subject_result.scope_exposure, 0)
        self.assertTrue(subject_result.sufficient)
        self.assertEqual(foreign_result.scope_exposure, 1)
        self.assertFalse(foreign_result.sufficient)

    def test_character_overflow_is_detected_from_sanitized_payload_size(self) -> None:
        scenario = self.by_case["project_resumption"]

        result = score_packet(
            variant="context_recency_v1",
            budget=BUDGETS[0],
            scenario=scenario,
            repetition=0,
            packet=self.packet(
                *scenario.required_ids,
                chars=BUDGETS[0].max_chars + 1,
            ),
        )

        self.assertTrue(result.budget_overflow)
        self.assertFalse(result.sufficient)

    def test_evaluator_token_measurement_is_preserved_for_reporting(self) -> None:
        scenario = self.by_case["project_resumption"]
        packet = replace(self.packet(*scenario.required_ids), evaluation_tokens=42)

        result = score_packet(
            variant="context_recency_v1",
            budget=BUDGETS[0],
            scenario=scenario,
            repetition=0,
            packet=packet,
        )

        self.assertEqual(result.evaluation_tokens, 42)

    def test_evaluator_tokens_fail_closed_when_packet_emptiness_disagrees(self) -> None:
        scenario = self.by_case["project_resumption"]
        invalid_packets = (
            replace(self.packet(*scenario.required_ids), evaluation_tokens=0),
            replace(self.packet(), evaluation_tokens=1),
        )
        for packet in invalid_packets:
            with (
                self.subTest(packet=packet),
                self.assertRaisesRegex(
                    ValueError,
                    "evaluation tokens must be zero for an empty packet",
                ),
            ):
                score_packet(
                    variant="context_recency_v1",
                    budget=BUDGETS[0],
                    scenario=scenario,
                    repetition=0,
                    packet=packet,
                )

    def test_all_hard_detectors_have_a_mutation_check(self) -> None:
        self.assertTrue(hard_detector_self_test(self.catalog, BUDGETS[0]))


if __name__ == "__main__":
    unittest.main()
