"""Cross-check the frozen protocol, catalog, and release discovery contract."""

from __future__ import annotations

import unittest

import verify_release
from tedd_memory_eval.catalog import catalog_digest, scenarios
from tedd_memory_eval.contracts import (
    BUDGETS,
    EVALUATOR_TOKENIZER_ID,
    PROTOCOL_ID,
    REPORT_SCHEMA_ID,
    SCENARIO_CATALOG_ID,
)
from tedd_memory_eval.providers import VARIANT_IDS
from tedd_memory_eval.runner import DEFAULT_REPETITIONS, DEFAULT_WARMUPS

from tests.support.paths import REPOSITORY_ROOT

FROZEN_CATALOG_SHA256 = "d60791fdfaefd687ae2de9388de22f4b1d01d2abef242838aa528d009cd3a185"


class ProtocolContractTests(unittest.TestCase):
    def test_catalog_digest_and_partitions_are_frozen(self) -> None:
        catalog = scenarios()

        self.assertEqual(catalog_digest(catalog), FROZEN_CATALOG_SHA256)
        self.assertEqual(
            {
                partition: sum(item.partition == partition for item in catalog)
                for partition in {"calibration", "confirmation"}
            },
            {"calibration": 8, "confirmation": 4},
        )
        self.assertTrue(all("_v1" not in item.query for item in catalog))

    def test_protocol_document_matches_executable_constants(self) -> None:
        document = (REPOSITORY_ROOT / "research/memory-eval-lab/PROTOCOL-V2.md").read_text(
            encoding="utf-8"
        )
        inherited = (REPOSITORY_ROOT / "research/memory-eval-lab/PROTOCOL.md").read_text(
            encoding="utf-8"
        )
        bundle = f"{document}\n{inherited}"

        self.assertIn(PROTOCOL_ID, document)
        self.assertIn(REPORT_SCHEMA_ID, document)
        self.assertIn(SCENARIO_CATALOG_ID, document)
        self.assertIn(EVALUATOR_TOKENIZER_ID, document)
        for variant in VARIANT_IDS:
            self.assertIn(variant, bundle)
        for budget in BUDGETS:
            self.assertIn(f"`{budget.name}`", bundle)
            self.assertIn(f"{budget.max_items}", bundle)
            self.assertIn(f"{budget.max_chars:,}", bundle)
        self.assertEqual((DEFAULT_WARMUPS, DEFAULT_REPETITIONS), (2, 5))
        self.assertIn("python scripts/memory_eval_lab.py --pretty", document)

    def test_frozen_v1_protocol_and_source_identity_remain_documented(self) -> None:
        frozen = (REPOSITORY_ROOT / "research/memory-eval-lab/PROTOCOL.md").read_text(
            encoding="utf-8"
        )
        current = (REPOSITORY_ROOT / "research/memory-eval-lab/PROTOCOL-V2.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("tedd-memory-eval-protocol-v1", frozen)
        self.assertNotIn(PROTOCOL_ID, frozen)
        self.assertIn("508206d654c0a8e716af22672eafa85df324e8f2", current)
        self.assertIn("research-archive/memory-eval-lab", current)

    def test_release_gate_discovers_the_research_test_package(self) -> None:
        test_files = tuple((REPOSITORY_ROOT / "tests/evaluation").glob("test_*.py"))

        self.assertEqual(("tests",), verify_release.TEST_SUITES)
        self.assertGreaterEqual(len(test_files), 6)


if __name__ == "__main__":
    unittest.main()
