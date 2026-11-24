"""Regression tests for the reviewed Ruff function limits."""

from __future__ import annotations

import json
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path

from scripts import check_function_growth as growth


class FunctionGrowthTests(unittest.TestCase):
    def test_new_function_or_new_metric_uses_the_trial_threshold(self) -> None:
        key = "example.py::Reader.open"
        for code, measured in (("C901", 26), ("PLR0915", 101)):
            with self.subTest(code=code):
                self.assertIn(key, growth.growth_errors([(key, code, measured)], {})[0])
        errors = growth.growth_errors([(key, "PLR0915", 101)], {key: {"C901": 28}})
        self.assertIn("100", errors[0])

    def test_listed_function_exceeding_its_value_needs_review(self) -> None:
        key = "example.py::Reader.open"
        errors = growth.growth_errors([(key, "C901", 30)], {key: {"C901": 29}})
        self.assertEqual(len(errors), 1)
        for part in (key, "C901", "30", "29"):
            self.assertIn(part, errors[0])

    def test_equal_shrinking_and_resolved_findings_pass(self) -> None:
        key = "example.py::Reader.open"
        baseline = {key: {"C901": 29}}
        for findings in ([(key, "C901", 29)], [(key, "C901", 26)], []):
            with self.subTest(findings=findings):
                self.assertEqual(growth.growth_errors(findings, baseline), [])

    def test_baseline_requires_review_metadata_and_a_measured_limit(self) -> None:
        entry = {
            "owner": "Maintainers",
            "reason": "Format rules",
            "review_when": "Format changes",
            "C901": 30,
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "baseline.json"
            for field, value in (
                ("owner", ""),
                ("reason", "  "),
                ("review_when", None),
                ("C901", True),
                ("C901", 25),
                ("C910", 31),
            ):
                with self.subTest(field=field, value=value):
                    path.write_text(
                        json.dumps({"example.py::read": {**entry, field: value}}), encoding="utf-8"
                    )
                    with self.assertRaisesRegex(ValueError, "example.py::read"):
                        growth.read_baseline(path)

    @unittest.skipUnless(find_spec("ruff"), "Ruff runs in the developer quality environment")
    def test_real_ruff_findings_use_qualified_names_even_with_inline_ignores(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            # The disposable directory can sit beneath a checkout with its own Ruff include list.
            (root / "pyproject.toml").write_text(
                '[tool.ruff]\ninclude = ["sample.py"]\n', encoding="utf-8"
            )
            body = "        if value:\n            pass\n" * 25
            source = "class First:\n    async def read(self, value):  # noqa: C901\n" + body
            source += "def outer():\n    def read():\n" + "        pass\n" * 101
            (root / "sample.py").write_text(source, encoding="utf-8")
            findings = growth.ruff_findings(root)
            self.assertIn(("sample.py::First.read", "C901", 26), findings)
            self.assertIn(("sample.py::outer.read", "PLR0915", 101), findings)
