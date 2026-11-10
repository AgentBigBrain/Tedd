"""Cross-check the project recall protocol, command, and recorded baseline."""

from __future__ import annotations

import unittest

from project_recall_lab import parser
from tedd_memory_eval.contracts import EVALUATOR_TOKENIZER_ID
from tedd_memory_eval.contracts import PROTOCOL_ID as MEMORY_EVAL_PROTOCOL_ID
from tedd_project_recall.catalog import NEEDED_FIELDS, cases_digest, project_cases
from tedd_project_recall.contracts import (
    ARM_IDS,
    BUDGETS,
    PLACEMENTS,
    PROTOCOL_ID,
    REPETITIONS,
    REPORT_SCHEMA_ID,
    SCENARIO_CATALOG_ID,
    SEARCH_STEP_BUDGET,
    WARMUPS,
)

from tests.support.paths import REPOSITORY_ROOT

LAB = REPOSITORY_ROOT / "research/project-recall-lab"


class ProtocolContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.protocol = (LAB / "PROTOCOL.md").read_text(encoding="utf-8")

    def test_protocol_document_matches_executable_constants(self) -> None:
        for identifier in (
            PROTOCOL_ID,
            REPORT_SCHEMA_ID,
            SCENARIO_CATALOG_ID,
            EVALUATOR_TOKENIZER_ID,
        ):
            self.assertIn(identifier, self.protocol)
        for name in (*ARM_IDS, *PLACEMENTS):
            self.assertIn(f"`{name}`", self.protocol)
        for budget in BUDGETS:
            self.assertIn(
                f"| `{budget.name}` | {budget.max_items} | {budget.max_chars:,} |", self.protocol
            )
        self.assertIn(
            f"plus {SEARCH_STEP_BUDGET.max_items} results and "
            f"{SEARCH_STEP_BUDGET.max_chars:,} characters for its search",
            self.protocol,
        )
        self.assertEqual((WARMUPS, REPETITIONS), (1, 3))
        self.assertIn("one unmeasured warm-up and three measured", self.protocol)
        self.assertIn("python scripts/project_recall_lab.py --pretty", self.protocol)

    def test_protocol_lists_every_case_with_its_size_and_placement(self) -> None:
        for case in project_cases():
            row = f"| `{case.case_id}` | {len(case.scenario.records)} | `{case.placement}` |"
            self.assertIn(row, self.protocol)

    def test_protocol_names_the_needed_fields_of_every_required_kind(self) -> None:
        for fields in NEEDED_FIELDS.values():
            self.assertIn(", ".join(f"`{field}`" for field in fields), self.protocol)

    def test_memory_evaluation_lab_keeps_its_own_protocol_identity(self) -> None:
        self.assertNotEqual(PROTOCOL_ID, MEMORY_EVAL_PROTOCOL_ID)
        self.assertNotIn(PROTOCOL_ID, self.memory_eval_protocols())

    def memory_eval_protocols(self) -> str:
        folder = REPOSITORY_ROOT / "research/memory-eval-lab"
        return "".join(path.read_text(encoding="utf-8") for path in sorted(folder.glob("*.md")))

    def test_public_parser_exposes_no_caller_selected_state_path(self) -> None:
        options = {option for action in parser()._actions for option in action.option_strings}

        self.assertEqual(options, {"-h", "--help", "--pretty", "--timeout-seconds"})

    def test_recorded_baseline_is_for_this_protocol_and_catalog(self) -> None:
        baseline = (LAB / "BASELINE.md").read_text(encoding="utf-8")

        self.assertIn(PROTOCOL_ID, baseline)
        self.assertIn(cases_digest(project_cases()), baseline)


if __name__ == "__main__":
    unittest.main()
