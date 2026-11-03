#!/usr/bin/env python3
"""Behavioral contracts for contributor-documentation validation."""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Iterator
from unittest import mock

import check_documentation as documentation  # noqa: E402


class DocumentationContractTests(unittest.TestCase):
    def test_inventory_permission_error_does_not_become_an_empty_pass(self) -> None:
        with (
            mock.patch.object(
                documentation.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, b"blocked.md\x00"),
            ),
            mock.patch.object(Path, "lstat", side_effect=PermissionError("synthetic denied")),
        ):
            with self.assertRaises(PermissionError):
                documentation.markdown_files()

    def test_renamed_tracked_docs_keep_current_links_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            old = repository / "old.md"
            old.write_text("# Old\n", encoding="utf-8")
            guide = repository / "guide.md"
            guide.write_text("[stale](old.md)\n", encoding="utf-8")
            with (
                self.initialize_repository(repository),
                mock.patch.object(documentation, "ROOT", repository),
            ):
                subprocess.run(
                    ["git", "-C", str(repository), "add", "old.md", "guide.md"],
                    check=True,
                    timeout=10,
                )
                old.rename(repository / "new.md")
                files = documentation.markdown_files()
                self.assertEqual(["guide.md", "new.md"], [path.name for path in files])
                errors, _ = documentation.check_links(files)
                self.assertEqual(["guide.md: missing link target: old.md"], errors)

    def test_source_cli_examples_validate_subcommands_and_options(self) -> None:
        """Do not silently skip invalid source commands in operator guides."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            guide = root / "sources.md"
            guide.write_text(
                "```bash\n"
                "python3 -B scripts/tedd_sources.py --config config.json status\n"
                "python3 scripts/tedd_sources.py --config config.json inspect "
                "--source-uri saved --invented-flag\n"
                "python3 scripts/tedd_sources.py --config config.json invented-command\n"
                "```\n",
                encoding="utf-8",
            )
            with mock.patch.object(documentation, "ROOT", root):
                errors, count = documentation.check_cli_examples([guide])

        self.assertEqual(3, count)
        self.assertEqual(2, len(errors))
        self.assertIn("unsupported tedd_sources.py options: ['--invented-flag']", errors[0])
        self.assertIn("tedd_sources.py example has no valid subcommand", errors[1])

    @contextlib.contextmanager
    def initialize_repository(self, root: Path) -> Iterator[None]:
        """Keep every fixture Git call independent of the operator's Git setup."""

        if shutil.which("git") is None:
            raise RuntimeError("Git is required for the documentation repository tests")
        environment = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
        }
        environment.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_ATTR_NOSYSTEM": "1",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.excludesFile",
                "GIT_CONFIG_VALUE_0": os.devnull,
            }
        )
        with mock.patch.dict(os.environ, environment, clear=True):
            subprocess.run(
                ["git", "init", "--quiet", "--template=", str(root)],
                check=True,
                capture_output=True,
                timeout=10,
            )
            yield

    def test_markdown_inventory_excludes_ignored_scratch_but_keeps_tracked_and_new_docs(
        self,
    ) -> None:
        """Catch a release check that silently stops validating contributor documents."""

        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve()
            (repository / ".gitignore").write_text("scratch.md\n", encoding="utf-8")
            (repository / "scratch.md").write_text("[lost](missing.md)\n", encoding="utf-8")
            (repository / "tracked.md").write_text("[lost](missing.md)\n", encoding="utf-8")
            (repository / "new.md").write_text("[lost](missing.md)\n", encoding="utf-8")
            with (
                self.initialize_repository(repository),
                mock.patch.object(documentation, "ROOT", repository),
            ):
                subprocess.run(
                    ["git", "-C", str(repository), "add", ".gitignore", "tracked.md"],
                    check=True,
                )
                files = documentation.markdown_files()
                errors, _ = documentation.check_links(files)

        self.assertEqual(["new.md", "tracked.md"], [path.name for path in files])
        self.assertEqual(
            [
                "new.md: missing link target: missing.md",
                "tracked.md: missing link target: missing.md",
            ],
            errors,
        )

    def test_fixture_inventory_ignores_external_git_configuration(self) -> None:
        """An operator's global excludes must not hide fixture documentation."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository = root / "repository"
            repository.mkdir()
            (repository / "new.md").write_text("# New documentation\n", encoding="utf-8")
            excludes = root / "global-ignore"
            excludes.write_text("new.md\n", encoding="utf-8")
            config = root / "global-config"
            config.write_text(f'[core]\nexcludesFile = "{excludes.as_posix()}"\n', encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}),
                self.initialize_repository(repository),
                mock.patch.object(documentation, "ROOT", repository),
            ):
                files = documentation.markdown_files()

        self.assertEqual(["new.md"], [path.name for path in files])

    def test_fixture_cannot_be_redirected_or_seeded_by_inherited_git_overrides(self) -> None:
        """Git init, add, and inventory must all stay inside the intended fixture."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository = root / "repository"
            repository.mkdir()
            foreign = root / "foreign"
            foreign.mkdir()
            templates = root / "templates"
            templates.mkdir()
            (templates / "external-marker").write_text("external\n", encoding="utf-8")
            inherited = {
                "GIT_DIR": str(foreign / ".git"),
                "GIT_WORK_TREE": str(foreign),
                "GIT_INDEX_FILE": str(foreign / "index"),
                "GIT_TEMPLATE_DIR": str(templates),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.excludesFile",
                "GIT_CONFIG_VALUE_0": str(root / "ignored"),
            }
            with mock.patch.dict(os.environ, inherited):
                with (
                    self.initialize_repository(repository),
                    mock.patch.object(documentation, "ROOT", repository),
                ):
                    self.assertTrue((repository / ".git").is_dir())
                    (repository / "tracked.md").write_text("# Fixture\n", encoding="utf-8")
                    subprocess.run(["git", "-C", str(repository), "add", "tracked.md"], check=True)
                    files = documentation.markdown_files()
                    self.assertTrue((repository / ".git" / "index").is_file())
                    self.assertFalse((repository / ".git" / "external-marker").exists())
                    self.assertEqual([], list(foreign.iterdir()))
                    self.assertEqual(["tracked.md"], [path.name for path in files])
                for key, value in inherited.items():
                    self.assertEqual(value, os.environ[key])

    def test_missing_git_is_an_explicit_dependency_failure(self) -> None:
        """Do not turn a missing release-gate dependency into a passing skip."""

        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(shutil, "which", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "Git is required"):
                with self.initialize_repository(Path(temporary)):
                    pass

    def test_failed_git_init_remains_a_failure_and_restores_the_environment(self) -> None:
        """A write-invalid fixture must not become a passing or skipped test."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            invalid_repository = root / "regular-file"
            invalid_repository.write_text("not a directory\n", encoding="utf-8")
            inherited_config = str(root / "operator-config")
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": inherited_config}):
                with self.assertRaises(subprocess.CalledProcessError) as raised:
                    with self.initialize_repository(invalid_repository):
                        self.fail("git init must reject a regular file as its repository")
                self.assertNotEqual(0, raised.exception.returncode)
                self.assertTrue(raised.exception.stderr)
                self.assertEqual(inherited_config, os.environ["GIT_CONFIG_GLOBAL"])

    def test_routing_evaluation_contract_requires_schema_marker(self) -> None:
        """Catch a routing report that cannot identify its evaluation schema."""

        with tempfile.TemporaryDirectory() as temporary:
            evaluations = Path(temporary)
            report = evaluations / "2040-01-01-tedd-routing-baseline.md"
            report.write_text("# Routing baseline\n", encoding="utf-8")

            errors = documentation.routing_evaluation_errors(evaluations)

        self.assertEqual(
            [
                "2040-01-01-tedd-routing-baseline.md: "
                "routing evaluation does not name the evaluator schema/version"
            ],
            errors,
        )

    def test_routing_evaluation_contract_rejects_private_task_links(self) -> None:
        """Catch a routing report that retains a private Codex task deep link."""

        with tempfile.TemporaryDirectory() as temporary:
            evaluations = Path(temporary)
            report = evaluations / "2040-01-01-tedd-routing-baseline.md"
            report.write_text(
                "- Evaluator schema: `tedd-routing-observations-v1`\n"
                "[private task](codex://thread/private)\n",
                encoding="utf-8",
            )

            errors = documentation.routing_evaluation_errors(evaluations)

        self.assertEqual(
            ["2040-01-01-tedd-routing-baseline.md: routing evaluation contains a Codex task URI"],
            errors,
        )

    def test_routing_evaluation_contract_accepts_new_dated_schema_report(self) -> None:
        """Allow later routing baselines without preserving a historical filename."""

        with tempfile.TemporaryDirectory() as temporary:
            evaluations = Path(temporary)
            report = evaluations / "2040-01-01-tedd-routing-baseline.md"
            report.write_text(
                "- Evaluator schema: `tedd-routing-observations-v1`\n",
                encoding="utf-8",
            )

            errors = documentation.routing_evaluation_errors(evaluations)

        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
