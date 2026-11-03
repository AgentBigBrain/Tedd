#!/usr/bin/env python3
"""Focused tests for the repository-local Python skill contract."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from tests.support.paths import REPOSITORY_ROOT as ROOT

SKILL_ROOT = ROOT / ".agents/skills/python-development"

import check_repository_boundary as boundary  # noqa: E402


class PythonSkillContractTests(unittest.TestCase):
    def test_rule_cards_remain_discoverable_through_a_focused_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill_root = self.copy_skill(temporary)
            skill = skill_root / "SKILL.md"
            content = skill.read_text(encoding="utf-8")
            start = content.index("### Coding cards")
            end = content.index("Use the [rule authoring template]")
            index = skill_root / "references/card-index.md"
            index.write_text(
                "# Focused coding guidance\n\n" + content[start:end].replace("(references/", "("),
                encoding="utf-8",
            )
            skill.write_text(
                content[:start]
                + "Read [the card index](references/card-index.md) for focused rules.\n\n"
                + content[end:],
                encoding="utf-8",
            )

            self.assertEqual(boundary.python_skill_contract_errors(skill_root), [])

    def copy_skill(self, temporary: str) -> Path:
        destination = Path(temporary) / "python-development"
        shutil.copytree(SKILL_ROOT, destination)
        return destination

    def replace_once(self, path: Path, old: str, new: str) -> None:
        content = path.read_text(encoding="utf-8")
        self.assertEqual(content.count(old), 1, f"fixture marker must be unique: {old}")
        path.write_text(content.replace(old, new, 1), encoding="utf-8")

    def test_actual_skill_copy_satisfies_exact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill_root = self.copy_skill(temporary)
            self.assertEqual(boundary.python_skill_contract_errors(skill_root), [])

    def test_fabricated_upstream_path_is_rejected(self) -> None:
        expected = "skills/testing/rules/fixture-conftest.md"
        fabricated = "skills/testing/rules/fixture-fabricated.md"
        with tempfile.TemporaryDirectory() as temporary:
            skill_root = self.copy_skill(temporary)
            coverage = skill_root / "references/UPSTREAM_COVERAGE.md"
            self.replace_once(coverage, expected, fabricated)

            errors = boundary.python_skill_contract_errors(skill_root)

        self.assertTrue(
            any(f"unexpected upstream rule: {fabricated}" in error for error in errors),
            errors,
        )
        self.assertTrue(
            any(expected in error and "found 0" in error for error in errors),
            errors,
        )

    def test_missing_upstream_path_is_rejected(self) -> None:
        missing = "skills/tooling/rules/type-mypy.md"
        with tempfile.TemporaryDirectory() as temporary:
            skill_root = self.copy_skill(temporary)
            coverage = skill_root / "references/UPSTREAM_COVERAGE.md"
            lines = coverage.read_text(encoding="utf-8").splitlines(keepends=True)
            matching = [line for line in lines if missing in line]
            self.assertEqual(len(matching), 1)
            coverage.write_text(
                "".join(line for line in lines if missing not in line),
                encoding="utf-8",
            )

            errors = boundary.python_skill_contract_errors(skill_root)

        self.assertTrue(
            any(missing in error and "found 0" in error for error in errors),
            errors,
        )

    def test_unreachable_rule_cards_are_rejected(self) -> None:
        local_name = "coding-error-no-silent-exceptions.md"
        with tempfile.TemporaryDirectory() as temporary:
            skill_root = self.copy_skill(temporary)
            skill = skill_root / "SKILL.md"
            skill.write_text("# Python skill without reference routes\n", encoding="utf-8")

            errors = boundary.python_skill_contract_errors(skill_root)

        self.assertTrue(
            any(local_name in error and "unreachable" in error for error in errors),
            errors,
        )

    def test_card_section_wording_can_evolve(self) -> None:
        local_name = "coding-design-control-flow.md"
        with tempfile.TemporaryDirectory() as temporary:
            skill_root = self.copy_skill(temporary)
            card = skill_root / "references" / local_name
            self.replace_once(card, "## Verification", "## Evidence")

            errors = boundary.python_skill_contract_errors(skill_root)

        self.assertEqual(errors, [])

    def test_reference_navigation_does_not_depend_on_a_contents_heading(self) -> None:
        local_name = "coding-async-task-ownership.md"
        with tempfile.TemporaryDirectory() as temporary:
            skill_root = self.copy_skill(temporary)
            card = skill_root / "references" / local_name
            self.replace_once(card, "## Contents", "Contents")

            errors = boundary.python_skill_contract_errors(skill_root)

        self.assertEqual(errors, [])

    def test_unbalanced_example_fences_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            skill_root = self.copy_skill(temporary)
            card = skill_root / "references/coding-design-control-flow.md"
            card.write_text(card.read_text(encoding="utf-8") + "\n```python\n", encoding="utf-8")
            errors = boundary.python_skill_contract_errors(skill_root)
        self.assertTrue(any("unbalanced fenced code" in error for error in errors), errors)

    def test_duplicate_coverage_mapping_is_rejected(self) -> None:
        upstream = "skills/coding-standards/rules/error-no-silent-exceptions.md"
        local_name = "coding-error-no-silent-exceptions.md"
        with tempfile.TemporaryDirectory() as temporary:
            skill_root = self.copy_skill(temporary)
            coverage = skill_root / "references/UPSTREAM_COVERAGE.md"
            content = coverage.read_text(encoding="utf-8")
            rows = [line for line in content.splitlines(keepends=True) if upstream in line]
            self.assertEqual(len(rows), 1)
            coverage.write_text(
                content.replace(rows[0], rows[0] + rows[0], 1),
                encoding="utf-8",
            )

            errors = boundary.python_skill_contract_errors(skill_root)

        self.assertTrue(
            any(
                "duplicate mapping" in error and upstream in error and local_name in error
                for error in errors
            ),
            errors,
        )

    def test_renamed_card_mapping_and_links_reject_old_local_targets(self) -> None:
        renames = (
            ("testing-fixture-conftest.md", "testing-fixture-shared-support.md"),
            ("testing-param-parametrize.md", "testing-param-subtest-cases.md"),
            ("tooling-pkg-uv.md", "tooling-pkg-reviewed-dependencies.md"),
            ("tooling-test-pytest.md", "tooling-test-unittest.md"),
        )
        for old, new in renames:
            with self.subTest(target=new), tempfile.TemporaryDirectory() as temporary:
                skill_root = self.copy_skill(temporary)
                coverage = skill_root / "references/UPSTREAM_COVERAGE.md"
                self.replace_once(coverage, f"]({new})", f"]({old})")
                errors = boundary.python_skill_contract_errors(skill_root)
                self.assertTrue(
                    any(f"to {old}, expected {new}" in error for error in errors), errors
                )
                self.replace_once(coverage, f"]({old})", f"]({new})")
                skill = skill_root / "SKILL.md"
                self.replace_once(skill, f"(references/{new})", f"(references/{old})")
                errors = boundary.python_skill_contract_errors(skill_root)
                self.assertTrue(
                    any(old in error and "missing skill reference" in error for error in errors),
                    errors,
                )


if __name__ == "__main__":
    unittest.main()
