"""Tests for aggregate qualification and report privacy."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tedd_memory_eval import reporting
from tedd_memory_eval.catalog import INERT_CANARY, scenarios
from tedd_memory_eval.contracts import (
    BUDGETS,
    EVALUATOR_TOKENIZER_ID,
    PROTOCOL_ID,
    REPORT_SCHEMA_ID,
    ContextPacket,
    Observation,
    StateSnapshot,
)
from tedd_memory_eval.providers import VARIANT_IDS
from tedd_memory_eval.reporting import assert_report_private, build_report
from tedd_memory_eval.scoring import hard_detector_self_test, score_packet


def observations() -> list[Observation]:
    catalog = scenarios()
    result: list[Observation] = []
    for variant in VARIANT_IDS:
        for budget in BUDGETS:
            for scenario in catalog:
                record_ids = scenario.oracle_ids if variant == "oracle_v1" else ()
                packet = ContextPacket(
                    record_ids=record_ids,
                    payload_chars=100 * len(record_ids),
                    reported_chars=100 * len(record_ids),
                    item_count=len(record_ids),
                    truncated=False,
                    duration_ms=1.0,
                    evaluation_tokens=10 * len(record_ids),
                )
                for repetition in range(5):
                    result.append(
                        score_packet(
                            variant=variant,
                            budget=budget,
                            scenario=scenario,
                            repetition=repetition,
                            packet=packet,
                        )
                    )
    return result


class ReportingTests(unittest.TestCase):
    def test_graph_runtime_is_bound_to_digest_and_dirty_scope(self) -> None:
        self.assertIn(
            reporting.GRAPH_RUNTIME_ROOT / "knowledge_graph_v4.py",
            reporting.EVALUATOR_PATHS,
        )
        self.assertIn(
            "plugins/tedd/skills/obsidian-knowledge-graph/scripts",
            reporting.GIT_REPRODUCIBILITY_PATHS,
        )

    def test_evaluator_digest_is_stable_across_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            source = root / "instrument.py"
            with (
                patch.object(reporting, "REPOSITORY_ROOT", root),
                patch.object(reporting, "EVALUATOR_PATHS", (source,)),
            ):
                source.write_bytes(b'print("stable")\n')
                lf_digest = reporting.evaluator_digest()
                source.write_bytes(b'print("stable")\r\n')
                crlf_digest = reporting.evaluator_digest()

        self.assertEqual(lf_digest, crlf_digest)

    def test_runtime_qualification_passes_for_valid_oracle_and_lower_bound(self) -> None:
        catalog = scenarios()

        report = build_report(
            observations(),
            catalog,
            warmups=2,
            repetitions=5,
            state_unchanged=True,
            initial_state=StateSnapshot(
                generation=1,
                watermark=10,
                journal_head_hash="synthetic-head",
                record_count=20,
            ),
            detector_self_test_passed=hard_detector_self_test(catalog, BUDGETS[0]),
        )

        qualification = report["qualification"]
        self.assertIsInstance(qualification, dict)
        self.assertTrue(cast(Mapping[str, object], qualification)["passed"])
        self.assertEqual(report["schema"], REPORT_SCHEMA_ID)
        self.assertEqual(report["protocol"], PROTOCOL_ID)
        reproducibility = cast(Mapping[str, object], report["reproducibility"])
        self.assertEqual(reproducibility["evaluator_tokenizer"], EVALUATOR_TOKENIZER_ID)
        aggregates = cast(list[Mapping[str, object]], report["baselines"])
        oracle = next(
            aggregate
            for aggregate in aggregates
            if aggregate["variant"] == "oracle_v1" and aggregate["budget"] == "standard"
        )
        boundedness = cast(Mapping[str, object], oracle["boundedness"])
        mean_tokens = boundedness["mean_evaluator_tokens"]
        hits_per_tokens = boundedness["relevant_hits_per_1000_evaluator_tokens"]
        self.assertIsInstance(mean_tokens, (int, float))
        self.assertIsInstance(hits_per_tokens, (int, float))
        self.assertGreater(cast(int | float, mean_tokens), 0)
        self.assertGreater(cast(int | float, hits_per_tokens), 0)
        assert_report_private(report)

    def test_report_privacy_rejects_canary_or_local_path(self) -> None:
        with self.assertRaises(RuntimeError):
            assert_report_private({"leak": INERT_CANARY})
        with self.assertRaises(RuntimeError):
            assert_report_private({"leak": "C:\\Users\\person\\graph.sqlite3"})

    def test_report_privacy_rejects_injected_run_specific_identifier(self) -> None:
        synthetic_store_id = "00000000-1111-2222-3333-444444444444"

        with self.assertRaises(RuntimeError):
            assert_report_private(
                {"store_id": synthetic_store_id},
                forbidden_values=(synthetic_store_id,),
            )

    def test_report_privacy_rejects_identifier_bearing_fields(self) -> None:
        with self.assertRaises(RuntimeError):
            assert_report_private({"record_ids": ["opaque-value"]})


if __name__ == "__main__":
    unittest.main()
