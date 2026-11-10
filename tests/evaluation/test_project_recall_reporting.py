"""Tests for project recall aggregation, qualification, and report privacy."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tedd_memory_eval.contracts import ContextPacket, StateSnapshot
from tedd_memory_eval.reporting import assert_report_private
from tedd_project_recall import reporting
from tedd_project_recall.catalog import cases_digest, project_cases
from tedd_project_recall.contracts import (
    ARM_IDS,
    BUDGETS,
    PROTOCOL_ID,
    REPETITIONS,
    REPORT_SCHEMA_ID,
    SCENARIO_CATALOG_ID,
    WARMUPS,
    Delivery,
    DeliveryObservation,
    ProjectCase,
)
from tedd_project_recall.delivery import CONTEXT_ARM_ID, score_delivery
from tedd_project_recall.reporting import build_report

STATE = StateSnapshot(generation=1, watermark=10, journal_head_hash="synthetic", record_count=20)


def returned_ids(arm_id: str, case: ProjectCase) -> tuple[str, ...]:
    """Model an exact-ID read, an empty read, and a recency read of five records."""

    records = case.scenario.records
    if arm_id == "oracle_v1":
        return case.scenario.required_ids
    if arm_id == "no_memory_v1":
        return ()
    window = records if case.placement == "fits_one_recall" else records[-5:]
    return tuple(item.record_id for item in reversed(window))


Vary = Callable[[str, ProjectCase, int, Delivery], Delivery]


def observations(
    cases: Sequence[ProjectCase], *, payload_mismatches: int = 0, vary: Vary | None = None
) -> list[DeliveryObservation]:
    """Build the whole matrix; ``vary`` may alter one read by arm, case and repetition."""

    result: list[DeliveryObservation] = []
    for arm_id in ARM_IDS:
        for budget in BUDGETS:
            for case in cases:
                ids = returned_ids(arm_id, case)
                delivery = Delivery(
                    packet=ContextPacket(
                        record_ids=ids,
                        payload_chars=100 * len(ids),
                        reported_chars=100 * len(ids),
                        item_count=len(ids),
                        truncated=False,
                        duration_ms=1.0,
                        evaluation_tokens=20 * len(ids),
                    ),
                    listed_ids=(),
                    carried_ids=(),
                    response_chars=130 * len(ids),
                    response_tokens=26 * len(ids),
                    payload_mismatches=payload_mismatches,
                )
                result.extend(
                    score_delivery(
                        arm_id=arm_id,
                        budget=budget,
                        case=case,
                        repetition=repetition,
                        delivery=(
                            delivery if vary is None else vary(arm_id, case, repetition, delivery)
                        ),
                    )
                    for repetition in range(REPETITIONS)
                )
    return result


def in_one_repetition(change: Callable[[ProjectCase, Delivery], Delivery]) -> Vary:
    """Apply ``change`` to the context arm's open-commitment read in repetition one."""

    def vary(arm_id: str, case: ProjectCase, repetition: int, delivery: Delivery) -> Delivery:
        chosen = arm_id == CONTEXT_ARM_ID and case.case_id == "open_commitment" and repetition == 1
        return change(case, delivery) if chosen else delivery

    return vary


def report_for(
    material: Sequence[DeliveryObservation], cases: Sequence[ProjectCase]
) -> dict[str, object]:
    return build_report(
        material,
        cases,
        warmups=WARMUPS,
        repetitions=REPETITIONS,
        state_unchanged=True,
        initial_state=STATE,
    )


def member(value: object, key: str) -> Mapping[str, object]:
    return cast(Mapping[str, object], cast(Mapping[str, object], value)[key])


class ValidReportTests(unittest.TestCase):
    cases: tuple[ProjectCase, ...]
    report: dict[str, object]

    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = project_cases()
        cls.report = report_for(observations(cls.cases), cls.cases)

    def test_runtime_qualification_passes_for_a_valid_oracle_and_lower_bound(self) -> None:
        qualification = member(self.report, "qualification")

        self.assertTrue(qualification["passed"], msg=qualification["checks"])
        self.assertEqual(
            (self.report["schema"], self.report["protocol"], self.report["scenario_catalog"]),
            (REPORT_SCHEMA_ID, PROTOCOL_ID, SCENARIO_CATALOG_ID),
        )

    def test_report_holds_no_identifier_payload_or_path(self) -> None:
        assert_report_private(self.report)

    def test_interpretation_separates_oversized_cases_from_controls(self) -> None:
        interpretation = member(self.report, "interpretation")

        self.assertEqual(interpretation["outside_recall_sufficiency"], 0.0)
        self.assertEqual(interpretation["outside_recall_oracle_sufficiency"], 1.0)
        self.assertIs(interpretation["controls_sufficient"], True)
        self.assertEqual(
            interpretation["critical_insufficient_cases"], ["old_decision", "open_commitment"]
        )

    def test_full_context_lists_every_project_and_whether_it_fits_one_recall(self) -> None:
        rows = cast(list[Mapping[str, object]], member(self.report, "full_context")["cases"])

        self.assertEqual(
            {str(row["case_id"]): row["record_count"] for row in rows},
            {case.case_id: len(case.scenario.records) for case in self.cases},
        )
        self.assertEqual(
            [row["case_id"] for row in rows if row["fits_default_budget"]], ["small_project"]
        )

    def test_each_arm_reports_its_share_of_the_whole_project(self) -> None:
        arms = cast(list[Mapping[str, object]], self.report["arms"])
        context = next(
            item
            for item in arms
            if item["arm"] == "context_response_v1" and item["budget"] == "default"
        )
        size = member(context, "size")

        self.assertEqual(len(arms), len(ARM_IDS) * len(BUDGETS))
        self.assertEqual(context["scenario_count"], len(self.cases))
        self.assertLess(cast(float, size["mean_share_of_project_items"]), 0.2)
        self.assertGreater(cast(float, size["mean_share_of_full_context_chars"]), 0.0)

    def test_interpretation_summarizes_every_arm_at_the_default_budget(self) -> None:
        routes = cast(
            list[Mapping[str, object]], member(self.report, "interpretation")["routes_at_default"]
        )

        self.assertEqual([route["arm"] for route in routes], list(ARM_IDS))
        self.assertEqual(
            {str(route["arm"]): route["outside_recall_sufficiency"] for route in routes},
            {
                "no_memory_v1": 0.0,
                "context_response_v1": 0.0,
                "recall_fts_v1": 0.0,
                "mcp_search_v1": 0.0,
                "mcp_context_search_v1": 0.0,
                "oracle_v1": 1.0,
            },
        )

    def test_reproducibility_binds_the_catalog_and_both_instruments(self) -> None:
        identity = member(self.report, "reproducibility")

        self.assertEqual(identity["catalog_sha256"], cases_digest(self.cases))
        self.assertNotEqual(identity["evaluator_sha256"], identity["memory_eval_evaluator_sha256"])


class QualificationFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = project_cases()

    def check(self, material: Sequence[DeliveryObservation], name: str) -> None:
        qualification = member(report_for(material, self.cases), "qualification")

        self.assertIs(cast(Mapping[str, object], qualification["checks"])[name], False)
        self.assertFalse(qualification["passed"])

    def test_a_missing_measurement_fails_the_matrix_check(self) -> None:
        self.check(observations(self.cases)[:-1], "complete_measurement_matrix")

    def test_a_returned_payload_that_differs_from_the_catalog_fails_qualification(self) -> None:
        self.check(
            observations(self.cases, payload_mismatches=1),
            "returned_payloads_match_authored_catalog",
        )

    def test_a_section_whose_text_changes_between_repetitions_fails_qualification(self) -> None:
        def changed_text(case: ProjectCase, delivery: Delivery) -> Delivery:
            # The section still names the loop, but no longer carries its action.
            return replace(delivery, carried_ids=(), sections_sha256="changed")

        def named(arm_id: str, case: ProjectCase, repetition: int, delivery: Delivery) -> Delivery:
            if arm_id != CONTEXT_ARM_ID or case.case_id != "open_commitment":
                return delivery
            ids = case.scenario.required_ids
            return replace(delivery, listed_ids=ids, carried_ids=ids, sections_sha256="original")

        def vary(arm_id: str, case: ProjectCase, repetition: int, delivery: Delivery) -> Delivery:
            listed = named(arm_id, case, repetition, delivery)
            return in_one_repetition(changed_text)(arm_id, case, repetition, listed)

        material = observations(self.cases, vary=vary)

        delivered = [
            item.delivered_recall
            for item in material
            if item.records.variant == CONTEXT_ARM_ID
            and item.records.case_id == "open_commitment"
            and item.records.budget == "default"
        ]
        self.assertEqual(delivered, [1.0, 0.0, 1.0])
        self.check(material, "identical_measured_repetitions")

    def test_a_changed_section_digest_alone_fails_qualification(self) -> None:
        def other_content(case: ProjectCase, delivery: Delivery) -> Delivery:
            return replace(delivery, sections_sha256="changed")

        self.check(
            observations(self.cases, vary=in_one_repetition(other_content)),
            "identical_measured_repetitions",
        )

    def test_other_warmup_or_repetition_counts_fail_qualification(self) -> None:
        report = build_report(
            observations(self.cases),
            self.cases,
            warmups=WARMUPS + 1,
            repetitions=REPETITIONS,
            state_unchanged=True,
            initial_state=STATE,
        )

        checks = cast(Mapping[str, object], member(report, "qualification")["checks"])
        self.assertIs(checks["frozen_warmups_and_repetitions"], False)


class EvaluatorDigestTests(unittest.TestCase):
    def digest(self, root: Path, content: bytes) -> str:
        source = root / "instrument.py"
        source.write_bytes(content)
        with (
            patch.object(reporting, "REPOSITORY_ROOT", root),
            patch.object(reporting, "INSTRUMENT_PATHS", (source,)),
        ):
            return reporting.evaluator_digest()

    def test_digest_ignores_line_endings_and_follows_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            unix = self.digest(root, b'print("stable")\n')
            windows = self.digest(root, b'print("stable")\r\n')
            changed = self.digest(root, b'print("changed")\n')

        self.assertEqual(unix, windows)
        self.assertNotEqual(unix, changed)

    def test_instrument_paths_include_the_protocol_and_every_module(self) -> None:
        names = {path.name for path in reporting.INSTRUMENT_PATHS}

        self.assertLessEqual(
            {"PROTOCOL.md", "project_recall_lab.py", "catalog.py", "delivery.py", "reporting.py"},
            names,
        )


if __name__ == "__main__":
    unittest.main()
