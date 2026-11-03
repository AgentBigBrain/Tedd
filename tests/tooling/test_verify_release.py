#!/usr/bin/env python3
"""Focused tests for the cross-platform release verifier."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import verify_release  # noqa: E402


def quality_configuration(
    *,
    ruff_include: tuple[str, ...],
    mypy_files: tuple[str, ...],
    format_exclude: tuple[str, ...] = (),
    ruff_exclude: tuple[str, ...] = (),
    mypy_exclude: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return the smallest valid quality configuration used by scope tests."""

    return {
        "tool": {
            "ruff": {
                "include": list(ruff_include),
                "exclude": list(ruff_exclude),
                "format": {"exclude": list(format_exclude)},
            },
            "mypy": {
                "files": list(mypy_files),
                "exclude": list(mypy_exclude),
            },
        }
    }


def write_python(path: Path, content: str = '"""Temporary quality-scope fixture."""\n') -> None:
    """Create one importable placeholder beneath a temporary shipped root."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class VerifyReleaseTests(unittest.TestCase):
    def test_test_environment_uses_a_disposable_private_root_without_changing_parent(self) -> None:
        inherited = {"PATH": "unchanged", "LOCALAPPDATA": "untrusted-parent", "TEMP": "old-temp"}
        before = inherited.copy()
        with verify_release.isolated_test_environment(inherited) as environment:
            root = Path(environment["TEMP"])
            self.assertTrue(root.is_dir())
            self.assertEqual(str(root), environment["TMP"])
            self.assertEqual(str(root), environment["TMPDIR"])
            self.assertEqual(str(root), environment["LOCALAPPDATA"])
            self.assertEqual("unchanged", environment["PATH"])
            self.assertEqual(before, inherited)
            if os.name != "nt":
                self.assertEqual(0o700, root.stat().st_mode & 0o777)
        self.assertFalse(root.exists())

    def test_test_environment_cleans_up_when_a_test_process_fails(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            with verify_release.isolated_test_environment({}) as environment:
                root = Path(environment["TEMP"])
                (root / "fixture.txt").write_text("synthetic", encoding="utf-8")
                raise RuntimeError("synthetic failure")
        self.assertFalse(root.exists())

    def test_python_major_minor_accepts_numeric_pair(self) -> None:
        self.assertEqual(verify_release.python_major_minor("3.14"), (3, 14))

    def test_python_major_minor_rejects_other_shapes(self) -> None:
        for value in ("3", "3.14.1", "three.fourteen", "3.x"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                verify_release.python_major_minor(value)

    def test_validate_python_version_accepts_current_interpreter(self) -> None:
        verify_release.validate_python_version(sys.version_info[:2])

    def test_validate_python_version_rejects_mismatch(self) -> None:
        expected = (sys.version_info.major, sys.version_info.minor + 1)
        with self.assertRaises(SystemExit):
            verify_release.validate_python_version(expected)

    def test_find_bash_honors_existing_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "bash-test"
            executable.touch()
            with mock.patch.dict("os.environ", {"TEDD_BASH": str(executable)}):
                self.assertEqual(verify_release.find_bash(), executable)

    def test_find_bash_rejects_missing_override(self) -> None:
        with mock.patch.dict("os.environ", {"TEDD_BASH": "missing-bash"}):
            self.assertIsNone(verify_release.find_bash())

    def test_validate_json_accepts_manifest_and_rejects_invalid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            valid = Path(temporary) / "valid.json"
            invalid = Path(temporary) / "invalid.json"
            valid.write_text(json.dumps({"ok": True}), encoding="utf-8")
            invalid.write_text("{", encoding="utf-8")
            verify_release.validate_json(valid)
            with self.assertRaises(json.JSONDecodeError):
                verify_release.validate_json(invalid)

    def test_validate_dev_tools_reports_missing_modules(self) -> None:
        with mock.patch.object(verify_release.importlib.util, "find_spec", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "coverage, mypy, PyYAML, ruff"):
                verify_release.validate_dev_tools()

    def test_validate_dev_tools_rejects_unreviewed_versions(self) -> None:
        with (
            mock.patch.object(verify_release.importlib.util, "find_spec", return_value=object()),
            mock.patch.object(verify_release.metadata, "version", return_value="0.0"),
        ):
            with self.assertRaisesRegex(RuntimeError, "coverage==7.15.3 required, found 0.0"):
                verify_release.validate_dev_tools()

    def test_repository_skill_yaml_rejects_wrong_name(self) -> None:
        class FakeYamlModule(types.ModuleType):
            def safe_load(self, _payload: str) -> object:
                return {"name": "wrong", "description": "present"}

        fake_yaml = FakeYamlModule("yaml")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / ".agents/skills/python-development/SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: wrong\ndescription: present\n---\n", encoding="utf-8")
            with (
                mock.patch.object(verify_release, "ROOT", root),
                mock.patch.dict(sys.modules, {"yaml": fake_yaml}),
                self.assertRaisesRegex(RuntimeError, "name must be python-development"),
            ):
                verify_release.validate_repository_skill_yaml()

    def test_recursive_quality_scope_pattern_covers_direct_and_nested_modules(self) -> None:
        entries: list[object] = ["package/**/*.py"]

        self.assertTrue(verify_release._configuration_covers("package/module.py", entries))
        self.assertTrue(verify_release._configuration_covers("package/nested/module.py", entries))
        self.assertFalse(verify_release._configuration_covers("other/module.py", entries))

    def test_production_discovery_is_recursive_and_excludes_test_and_benchmark_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = {
                root / "scripts/nested/release_helper.py",
                root / "plugins/tedd/skills/tedd/scripts/profile/profile_store.py",
                root / "plugins/tedd/skills/obsidian-knowledge-graph/scripts/"
                "tedd_knowledge_graph/query_service.py",
            }
            excluded = {
                root / "scripts/nested/test_release_helper.py",
                root / "plugins/tedd/skills/tedd/scripts/profile_store_test.py",
                root / "plugins/tedd/skills/obsidian-knowledge-graph/scripts/"
                "benchmarks/query_service.py",
                root / "plugins/tedd/skills/obsidian-knowledge-graph/scripts/memorybench_graph.py",
            }
            for path in expected | excluded:
                write_python(path)

            self.assertEqual(verify_release.production_python_files(root), expected)

    def test_recursive_production_module_cannot_bypass_ruff_or_mypy_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            new_module = root / "scripts/nested/new_module.py"
            write_python(new_module)
            configuration = quality_configuration(
                ruff_include=("scripts/reviewed.py",),
                mypy_files=("scripts/reviewed.py",),
            )

            with (
                mock.patch.object(verify_release, "ROOT", root),
                mock.patch.object(
                    verify_release,
                    "_toml_configuration",
                    return_value=configuration,
                ),
                self.assertRaises(RuntimeError) as raised,
            ):
                verify_release.validate_python_quality_scope()

        message = str(raised.exception)
        self.assertIn("missing from Ruff: scripts/nested/new_module.py", message)
        self.assertIn("missing from mypy: scripts/nested/new_module.py", message)

    def test_production_formatter_directory_and_glob_exclusions_are_rejected(self) -> None:
        exclusions = (
            "scripts/nested/new_module.py",
            "scripts/nested",
            "scripts/**/*.py",
            "scripts/nested/*.py",
            "scripts/**/nested",
        )
        for exclusion in exclusions:
            with self.subTest(exclusion=exclusion), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_python(root / "scripts/nested/new_module.py")
                configuration = quality_configuration(
                    ruff_include=("scripts/**/*.py",),
                    mypy_files=("scripts",),
                    format_exclude=(exclusion,),
                )

                with (
                    mock.patch.object(verify_release, "ROOT", root),
                    mock.patch.object(
                        verify_release,
                        "_toml_configuration",
                        return_value=configuration,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "production formatter exclusions remain: scripts/nested/new_module.py",
                    ),
                ):
                    verify_release.validate_python_quality_scope()

    def test_top_level_ruff_exclusion_also_blocks_formatter_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_python(root / "scripts/nested/new_module.py")
            configuration = quality_configuration(
                ruff_include=("scripts/**/*.py",),
                mypy_files=("scripts",),
                ruff_exclude=("scripts/**",),
            )

            with (
                mock.patch.object(verify_release, "ROOT", root),
                mock.patch.object(
                    verify_release,
                    "_toml_configuration",
                    return_value=configuration,
                ),
                self.assertRaisesRegex(RuntimeError, "production formatter exclusions remain"),
            ):
                verify_release.validate_python_quality_scope()

    def test_mypy_regular_expression_exclusion_cannot_hide_production(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_python(root / "scripts/nested/new_module.py")
            configuration = quality_configuration(
                ruff_include=("scripts/**/*.py",),
                mypy_files=("scripts",),
                mypy_exclude=(r"scripts/nested/.*\.py",),
            )

            with (
                mock.patch.object(verify_release, "ROOT", root),
                mock.patch.object(
                    verify_release,
                    "_toml_configuration",
                    return_value=configuration,
                ),
                self.assertRaisesRegex(RuntimeError, "production mypy exclusions remain"),
            ):
                verify_release.validate_python_quality_scope()

    def test_explicit_dynamic_type_alias_is_also_rejected(self) -> None:
        tree = verify_release.ast.parse(
            "from typing import Any as Dynamic, TypeAlias\n"
            "JSONMap: TypeAlias = dict[str, Dynamic]\n"
            "def leak(value: Dynamic) -> object:\n"
            "    return value\n"
        )

        self.assertEqual(
            verify_release._explicit_any_lines(tree),
            [2, 3],
        )

    def test_explicit_dynamic_type_in_any_production_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "scripts/nested/new_module.py"
            write_python(module, "from typing import Any\nvalue: Any = None\n")
            configuration = quality_configuration(
                ruff_include=("scripts/**/*.py",),
                mypy_files=("scripts",),
            )

            with (
                mock.patch.object(verify_release, "ROOT", root),
                mock.patch.object(
                    verify_release,
                    "_toml_configuration",
                    return_value=configuration,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "production Any outside validated JSON boundaries: scripts/nested/new_module.py:2",
                ),
            ):
                verify_release.validate_python_quality_scope()

    def test_coverage_runs_every_check_and_test_suite(self) -> None:
        env = {"PYTHONDONTWRITEBYTECODE": "1"}
        caller_env = env.copy()
        check_env = {
            **caller_env,
            "PYTHONPATH": str(verify_release.ROOT / "scripts"),
        }
        expected = [
            (sys.executable, "-m", "coverage", "erase"),
            (
                sys.executable,
                "-m",
                "coverage",
                "run",
                "--append",
                "scripts/check_repository_boundary.py",
            ),
            (
                sys.executable,
                "-m",
                "coverage",
                "run",
                "--append",
                "scripts/check_documentation.py",
            ),
        ]
        expected.extend(
            [
                (
                    sys.executable,
                    "-m",
                    "coverage",
                    "run",
                    "--append",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-t",
                    ".",
                    "-p",
                    "test_*.py",
                ),
            ]
        )
        expected.append((sys.executable, "-m", "coverage", "report"))

        with mock.patch.object(verify_release, "run") as run:
            verify_release.run_tests(env=env, with_coverage=True)

        self.assertEqual([call.args[0] for call in run.call_args_list], expected)
        for index, call in enumerate(run.call_args_list):
            call_env = call.kwargs["env"]
            if index in (1, 2):
                self.assertEqual(call_env, check_env)
            elif index == 3:
                self.assertEqual(
                    call_env,
                    {
                        **caller_env,
                        "PYTHONWARNINGS": verify_release.STRICT_TEST_WARNINGS,
                    },
                )
            else:
                self.assertEqual(call_env, caller_env)
        self.assertEqual(env, caller_env)

    def test_coverage_preserves_existing_pythonpath_after_repository_scripts(self) -> None:
        env = {"PYTHONPATH": "existing-imports"}

        with mock.patch.object(verify_release, "run") as run:
            verify_release.run_tests(env=env, with_coverage=True)

        expected = str(verify_release.ROOT / "scripts") + os.pathsep + "existing-imports"
        for index in (1, 2):
            self.assertEqual(run.call_args_list[index].kwargs["env"]["PYTHONPATH"], expected)
        self.assertEqual(env, {"PYTHONPATH": "existing-imports"})

    def test_dependency_free_tests_make_deprecations_fatal(self) -> None:
        env = {"PYTHONDONTWRITEBYTECODE": "1"}
        caller_env = env.copy()
        expected = [
            (
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-t",
                ".",
                "-p",
                "test_*.py",
            ),
        ]
        with mock.patch.object(verify_release, "run") as run:
            verify_release.run_tests(env=env, with_coverage=False)

        self.assertEqual([call.args[0] for call in run.call_args_list], expected)
        for call in run.call_args_list:
            self.assertEqual(
                call.kwargs["env"],
                {
                    **caller_env,
                    "PYTHONWARNINGS": verify_release.STRICT_TEST_WARNINGS,
                },
            )
        self.assertEqual(env, caller_env)

    def test_quality_gate_runs_lint_format_and_types(self) -> None:
        env = {"PYTHONDONTWRITEBYTECODE": "1"}
        with (
            mock.patch.object(verify_release, "validate_dev_tools") as validate_tools,
            mock.patch.object(verify_release, "validate_repository_skill_yaml") as validate_skill,
            mock.patch.object(verify_release, "validate_python_quality_scope") as validate_scope,
            mock.patch.object(verify_release, "run") as run,
        ):
            verify_release.run_quality_checks(env=env)

        validate_tools.assert_called_once_with()
        validate_skill.assert_called_once_with()
        validate_scope.assert_called_once_with()
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                (sys.executable, "-m", "ruff", "check", "."),
                (sys.executable, "-m", "ruff", "format", "--check", "."),
                (sys.executable, "-m", "mypy"),
            ],
        )

    def test_main_quality_path_enables_coverage(self) -> None:
        parsed = argparse.Namespace(require_bash=False, expected_python=None, quality=True)
        fake_parser = mock.Mock()
        fake_parser.parse_args.return_value = parsed
        with (
            mock.patch.object(verify_release, "parser", return_value=fake_parser),
            mock.patch.object(verify_release, "validate_python_version"),
            mock.patch.object(verify_release, "run"),
            mock.patch.object(verify_release, "validate_json"),
            mock.patch.object(verify_release, "validate_shell_scripts"),
            mock.patch.object(verify_release, "run_quality_checks") as quality,
            mock.patch.object(verify_release, "run_tests") as tests,
        ):
            self.assertEqual(verify_release.main(), 0)

        quality_env = quality.call_args.kwargs["env"]
        self.assertEqual(quality_env["PYTHONDONTWRITEBYTECODE"], "1")
        tests.assert_called_once()
        self.assertTrue(tests.call_args.kwargs["with_coverage"])
        test_env = tests.call_args.kwargs["env"]
        self.assertEqual(test_env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotEqual(test_env["TEMP"], quality_env.get("TEMP"))
        self.assertFalse(Path(test_env["TEMP"]).exists())


if __name__ == "__main__":
    unittest.main()
