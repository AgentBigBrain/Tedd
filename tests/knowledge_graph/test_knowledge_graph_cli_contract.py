#!/usr/bin/env python3
"""Black-box tests for the knowledge-graph CLI error contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from tests.support.paths import GRAPH_SCRIPTS as SCRIPT_DIR

SCRIPT = SCRIPT_DIR / "knowledge_graph.py"


class KnowledgeGraphCLIContractTests(unittest.TestCase):
    def assert_invalid_arguments(self, *arguments: str) -> None:
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=SCRIPT_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(result.returncode, 2, result)
        self.assertEqual(result.stdout, "")
        payload = json.loads(result.stderr)
        self.assertEqual(
            payload,
            {
                "status": "error",
                "error_code": "invalid_arguments",
                "error": payload["error"],
            },
        )
        self.assertIsInstance(payload["error"], str)
        self.assertTrue(payload["error"])
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_command_is_structured(self) -> None:
        self.assert_invalid_arguments()

    def test_unknown_command_is_structured(self) -> None:
        self.assert_invalid_arguments("not-a-command")

    def test_invalid_legacy_option_type_is_structured(self) -> None:
        self.assert_invalid_arguments("--lock-timeout", "not-a-number", "doctor")

    def test_missing_v4_required_option_is_structured(self) -> None:
        self.assert_invalid_arguments("history")

    def test_backup_force_is_rejected_as_an_invalid_argument(self) -> None:
        self.assert_invalid_arguments("backup", "--output", "unused-backup.sqlite3", "--force")


if __name__ == "__main__":
    unittest.main()
