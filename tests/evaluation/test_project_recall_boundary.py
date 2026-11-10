"""Integration test for project recall arms at the real CLI and MCP boundaries."""

from __future__ import annotations

import unittest
from pathlib import Path

from tedd_memory_eval.sandbox import MemoryEvalSandbox
from tedd_project_recall.contracts import DEFAULT_BUDGET, PROTOCOL_ID
from tedd_project_recall.delivery import (
    CONTEXT_ARM_ID,
    SEARCH_ARM_ID,
    WORKFLOW_ARM_ID,
    arms,
    score_delivery,
)

from tests.evaluation.project_recall_support import MiniProject


class RealBoundaryTests(unittest.TestCase):
    def test_arms_read_a_small_project_without_changing_it(self) -> None:
        mini = MiniProject()
        case = mini.case(mini.loop, mini.current)
        root: Path | None = None

        with MemoryEvalSandbox(catalog=(case.scenario,), protocol_id=PROTOCOL_ID) as sandbox:
            root = sandbox.root
            seen = {
                arm.arm_id: score_delivery(
                    arm_id=arm.arm_id,
                    budget=arm.allowance(DEFAULT_BUDGET),
                    case=case,
                    repetition=0,
                    delivery=arm.read(sandbox, case, DEFAULT_BUDGET),
                )
                for arm in arms()
                if arm.arm_id != "recall_fts_v1"
            }
            unchanged = sandbox.initial_state == sandbox.final_state()

        context, exact = seen[CONTEXT_ARM_ID], seen["oracle_v1"]
        search, workflow = seen[SEARCH_ARM_ID], seen[WORKFLOW_ARM_ID]
        # The whole project fits this budget, so every authored record comes back
        # with its authored payload and the overdue section names the open loop.
        self.assertEqual(context.records.required_recall, 1.0)
        self.assertEqual({item.payload_mismatches for item in seen.values()}, {0})
        self.assertIn(mini.loop.record_id, context.named_ids[: context.section_items])
        self.assertEqual(context.records.hard_violation_count, 0)
        self.assertGreater(context.response_chars, context.records.payload_chars)
        self.assertTrue(exact.sufficient)
        # Scoped search finds the decision by the question's words, and the
        # workflow's first step is the same context read, over MCP.
        self.assertIn(mini.current.record_id, search.records.ordered_ids)
        self.assertEqual(search.section_items, 0)
        self.assertEqual(
            workflow.records.ordered_ids[: len(context.records.ordered_ids)],
            context.records.ordered_ids,
        )
        self.assertEqual(workflow.section_items, context.section_items)
        self.assertGreater(workflow.response_chars, context.response_chars)
        self.assertTrue(unchanged)
        self.assertFalse(root.exists() if root is not None else True)


if __name__ == "__main__":
    unittest.main()
