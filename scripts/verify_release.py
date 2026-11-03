#!/usr/bin/env python3
"""Run Tedd's self-contained release verification on supported platforms."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import import_module, metadata
from pathlib import Path
from typing import Mapping, Protocol, Sequence, cast

ROOT = Path(__file__).resolve().parents[1]
SHELL_SEARCH_ROOTS = (
    ROOT / "scripts",
    ROOT / "plugins/tedd/skills",
)
TEST_SUITES = ("tests",)
DEV_TOOL_MODULES = {
    "coverage": "coverage",
    "mypy": "mypy",
    "PyYAML": "yaml",
    "ruff": "ruff",
}
DEV_DISTRIBUTION_VERSIONS = {
    "coverage": "7.15.3",
    "mypy": "1.20.2",
    "mypy-extensions": "1.1.0",
    "pathspec": "1.1.1",
    "PyYAML": "6.0.3",
    "ruff": "0.16.1",
    "typing-extensions": "4.16.0",
}
STRICT_TEST_WARNINGS = "error::DeprecationWarning,error::PendingDeprecationWarning"
SHIPPED_PYTHON_ROOTS = (
    Path("scripts"),
    Path("plugins/tedd/skills/tedd/scripts"),
    Path("plugins/tedd/skills/obsidian-knowledge-graph/scripts"),
)
NON_PRODUCTION_PYTHON_DIRECTORIES = frozenset({"benchmark", "benchmarks", "test", "tests"})


class YamlLoader(Protocol):
    """Typed surface used from the optional-at-runtime PyYAML dependency."""

    def safe_load(self, stream: str) -> object:
        """Decode a YAML document into an object that callers must validate."""

        ...


def python_major_minor(value: str) -> tuple[int, int]:
    """Parse an expected Python version in MAJOR.MINOR form."""

    parts = value.split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise argparse.ArgumentTypeError(
            "expected a Python version in MAJOR.MINOR form (for example, 3.12)"
        )
    return int(parts[0]), int(parts[1])


def validate_python_version(expected: tuple[int, int] | None) -> None:
    """Validate the running interpreter against the supported and expected versions."""

    actual = sys.version_info[:2]
    executable = sys.executable or "<unknown executable>"
    actual_version = ".".join(str(part) for part in sys.version_info[:3])
    if actual < (3, 10):
        raise SystemExit(
            "Python 3.10 or newer is required to verify Tedd; "
            f"running Python {actual_version} from {executable}."
        )
    if expected is not None and actual != expected:
        expected_version = ".".join(str(part) for part in expected)
        raise SystemExit(
            f"Expected Python {expected_version}, but release verification is running "
            f"Python {actual_version} from {executable}."
        )


def run(command: Sequence[str], *, env: dict[str, str]) -> None:
    """Run one verification command from the repository root."""

    subprocess.run(command, cwd=ROOT, env=env, check=True)


def validate_json(path: Path) -> None:
    """Parse one repository manifest without changing it."""

    with path.open("r", encoding="utf-8") as handle:
        json.load(handle)


def windows_git_bash_candidates() -> list[Path]:
    """Return likely Git for Windows Bash locations in stable order."""

    candidates: list[Path] = []
    git = shutil.which("git")
    if git:
        git_path = Path(git).resolve()
        candidates.extend(
            (
                git_path.parent.parent / "bin/bash.exe",
                git_path.parent.parent / "usr/bin/bash.exe",
            )
        )
    for variable in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(variable)
        if root:
            candidates.extend(
                (
                    Path(root) / "Git/bin/bash.exe",
                    Path(root) / "Git/usr/bin/bash.exe",
                )
            )
    return candidates


def find_bash() -> Path | None:
    """Locate Bash without mistaking the Windows WSL launcher for Git Bash."""

    override = os.environ.get("TEDD_BASH")
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None
    if os.name == "nt":
        for candidate in windows_git_bash_candidates():
            if candidate.is_file():
                return candidate
        return None
    bash_path = shutil.which("bash")
    return Path(bash_path) if bash_path else None


def validate_dev_tools() -> None:
    """Require the complete reviewed developer dependency set and exact versions."""

    missing = [
        label
        for label, module in DEV_TOOL_MODULES.items()
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        raise RuntimeError(
            "Developer quality checks require the PEP 735 dev dependency group. "
            "Install it with `python -m pip install --group dev`; missing: " + ", ".join(missing)
        )

    expected_versions = dict(DEV_DISTRIBUTION_VERSIONS)
    if sys.version_info < (3, 11):
        expected_versions["tomli"] = "2.4.1"
    if sys.implementation.name != "pypy":
        expected_versions["librt"] = "0.13.0"

    version_errors: list[str] = []
    for distribution, expected in expected_versions.items():
        try:
            actual = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            version_errors.append(f"{distribution}=={expected} is not installed")
            continue
        if actual != expected:
            version_errors.append(f"{distribution}=={expected} required, found {actual}")
    if version_errors:
        raise RuntimeError(
            "Developer quality checks require the exact reviewed PEP 735 dependency set: "
            + "; ".join(version_errors)
        )


def validate_repository_skill_yaml() -> None:
    """Parse the repository-local skill frontmatter with the reviewed YAML parser."""

    yaml = cast(YamlLoader, import_module("yaml"))

    skill = ROOT / ".agents/skills/python-development/SKILL.md"
    lines = skill.read_text(encoding="utf-8").splitlines()
    try:
        closing_delimiter = lines.index("---", 1)
    except ValueError as error:
        raise RuntimeError("repository-local skill frontmatter is not closed") from error
    metadata = yaml.safe_load("\n".join(lines[1:closing_delimiter]))
    if not isinstance(metadata, dict):
        raise RuntimeError("repository-local skill frontmatter must be a YAML object")
    if metadata.get("name") != "python-development":
        raise RuntimeError("repository-local skill name must be python-development")
    if not isinstance(metadata.get("description"), str) or not metadata["description"].strip():
        raise RuntimeError("repository-local skill description must be a non-empty string")


def production_python_files(root: Path) -> set[Path]:
    """Discover every shipped Python module while excluding tests and benchmarks."""

    discovered: set[Path] = set()
    for relative_root in SHIPPED_PYTHON_ROOTS:
        script_root = root / relative_root
        for path in script_root.rglob("*.py"):
            relative = path.relative_to(script_root)
            if any(part in NON_PRODUCTION_PYTHON_DIRECTORIES for part in relative.parts[:-1]):
                continue
            if path.name == "conftest.py" or path.name.startswith(
                ("test_", "benchmark_", "memorybench_")
            ):
                continue
            if path.name.endswith("_test.py"):
                continue
            if path.is_file():
                discovered.add(path)
    return discovered


def _configuration_covers(relative_path: str, entries: Sequence[object]) -> bool:
    """Match the exact files, directories, and recursive Python globs used here."""

    for raw_entry in entries:
        if not isinstance(raw_entry, str):
            continue
        entry = raw_entry.replace("\\", "/").rstrip("/")
        if entry.endswith("/**/*.py"):
            prefix = entry.removesuffix("**/*.py")
            if relative_path.startswith(prefix) and relative_path.endswith(".py"):
                return True
        elif entry.endswith(".py"):
            if relative_path == entry:
                return True
        elif relative_path == entry or relative_path.startswith(f"{entry}/"):
            return True
    return False


def _glob_matches(relative_path: str, pattern: str) -> bool:
    """Match one repository-relative path using segment-aware ``**`` semantics."""

    path_parts = tuple(part for part in relative_path.replace("\\", "/").split("/") if part)
    pattern_parts = tuple(part for part in pattern.replace("\\", "/").split("/") if part)

    def matches(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)
        pattern_part = pattern_parts[pattern_index]
        if pattern_part == "**":
            return matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and matches(path_index + 1, pattern_index)
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], pattern_part)
            and matches(path_index + 1, pattern_index + 1)
        )

    return matches(0, 0)


def _configuration_excludes(relative_path: str, entries: Sequence[object]) -> bool:
    """Apply Ruff-style exact, directory, and glob exclusions to one path."""

    normalized_path = relative_path.replace("\\", "/").strip("/")
    path_parts = tuple(part for part in normalized_path.split("/") if part)
    for raw_entry in entries:
        if not isinstance(raw_entry, str):
            continue
        entry = raw_entry.replace("\\", "/").strip("/")
        if not entry:
            continue
        has_glob = any(marker in entry for marker in ("*", "?", "["))
        if "/" not in entry:
            if has_glob and any(fnmatch.fnmatchcase(part, entry) for part in path_parts):
                return True
            if not has_glob and entry in path_parts:
                return True
        elif has_glob:
            path_prefixes = (
                "/".join(path_parts[:length]) for length in range(1, len(path_parts) + 1)
            )
            if any(_glob_matches(prefix, entry) for prefix in path_prefixes):
                return True
        elif normalized_path == entry or normalized_path.startswith(f"{entry}/"):
            return True
    return False


def _regex_configuration_excludes(relative_path: str, entries: Sequence[object]) -> bool:
    """Apply mypy's repository-relative regular-expression exclusions."""

    for raw_entry in entries:
        if not isinstance(raw_entry, str):
            continue
        try:
            if re.search(raw_entry, relative_path):
                return True
        except re.error as error:
            raise RuntimeError(f"Invalid mypy exclude regular expression: {raw_entry}") from error
    return False


def _configuration_entries(value: object) -> tuple[object, ...]:
    """Normalize one TOML scalar or array into configuration entries."""

    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(value)
    return ()


def _explicit_any_bindings(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """Return imported names and module aliases that resolve to ``typing.Any``."""

    direct_names: set[str] = set()
    module_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name in {"typing", "typing_extensions"}:
                    module_names.add(imported.asname or imported.name)
        elif isinstance(node, ast.ImportFrom) and node.module in {"typing", "typing_extensions"}:
            for imported in node.names:
                if imported.name == "Any":
                    direct_names.add(imported.asname or imported.name)
    return frozenset(direct_names), frozenset(module_names)


def _is_explicit_any(
    node: ast.AST,
    direct_names: frozenset[str],
    module_names: frozenset[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in direct_names
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "Any"
        and isinstance(node.value, ast.Name)
        and node.value.id in module_names
    )


def _explicit_any_lines(tree: ast.Module) -> list[int]:
    """Locate every explicit ``Any`` use in one production module."""

    direct_names, module_names = _explicit_any_bindings(tree)
    return sorted(
        {
            node.lineno
            for node in ast.walk(tree)
            if hasattr(node, "lineno") and _is_explicit_any(node, direct_names, module_names)
        }
    )


def _toml_configuration(path: Path) -> Mapping[str, object]:
    """Load TOML with the standard library or the pinned 3.10 backport."""

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    loaded: object = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"TOML configuration must be an object: {path}")
    return cast(Mapping[str, object], loaded)


def _table(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"Missing TOML table: {key}")
    return cast(Mapping[str, object], value)


def validate_python_quality_scope() -> None:
    """Prevent production modules or internal ``Any`` debt from bypassing CI."""

    configuration = _toml_configuration(ROOT / "pyproject.toml")
    tool = _table(configuration, "tool")
    ruff = _table(tool, "ruff")
    mypy = _table(tool, "mypy")
    ruff_entries = _configuration_entries(ruff.get("include", ()))
    mypy_entries = _configuration_entries(mypy.get("files", ()))
    format_configuration = _table(ruff, "format")
    format_excludes = (
        *_configuration_entries(ruff.get("exclude", ())),
        *_configuration_entries(ruff.get("extend-exclude", ())),
        *_configuration_entries(format_configuration.get("exclude", ())),
    )
    mypy_excludes = _configuration_entries(mypy.get("exclude", ()))

    uncovered_ruff: list[str] = []
    uncovered_mypy: list[str] = []
    excluded_from_format: list[str] = []
    excluded_from_mypy: list[str] = []
    for path in sorted(production_python_files(ROOT)):
        relative = path.relative_to(ROOT).as_posix()
        if not _configuration_covers(relative, ruff_entries):
            uncovered_ruff.append(relative)
        if not _configuration_covers(relative, mypy_entries):
            uncovered_mypy.append(relative)
        if _configuration_excludes(relative, format_excludes):
            excluded_from_format.append(relative)
        if _regex_configuration_excludes(relative, mypy_excludes):
            excluded_from_mypy.append(relative)

    unexpected_any: list[str] = []
    for path in sorted(production_python_files(ROOT)):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        unexpected_any.extend(f"{relative}:{line}" for line in _explicit_any_lines(tree))

    failures: list[str] = []
    if uncovered_ruff:
        failures.append("missing from Ruff: " + ", ".join(uncovered_ruff))
    if uncovered_mypy:
        failures.append("missing from mypy: " + ", ".join(uncovered_mypy))
    if excluded_from_format:
        failures.append(
            "production formatter exclusions remain: " + ", ".join(excluded_from_format)
        )
    if excluded_from_mypy:
        failures.append("production mypy exclusions remain: " + ", ".join(excluded_from_mypy))
    if unexpected_any:
        failures.append(
            "production Any outside validated JSON boundaries: " + ", ".join(unexpected_any)
        )
    if failures:
        raise RuntimeError("Python quality scope is incomplete; " + "; ".join(failures))


def unittest_command() -> tuple[str, ...]:
    """Build one deterministic unittest discovery command."""

    return (
        "-m",
        "unittest",
        "discover",
        "-s",
        "tests",
        "-t",
        ".",
        "-p",
        "test_*.py",
    )


@contextmanager
def isolated_test_environment(inherited: dict[str, str]) -> Iterator[dict[str, str]]:
    """Keep disposable Windows fixtures out of potentially untrusted AppData ancestry.

    Only a newly allocated directory receives private permissions. Existing home,
    AppData, and system temporary directories are never modified. The environment
    override belongs to child test processes, not the caller or installed Tedd.
    """

    parent = Path.home() if os.name == "nt" else None
    with tempfile.TemporaryDirectory(prefix=".tedd-release-test-", dir=parent) as temporary:
        root = Path(temporary).resolve(strict=True)
        if os.name == "nt":
            scripts = ROOT / "plugins/tedd/skills/obsidian-knowledge-graph/scripts"
            old_path = sys.path[:]
            try:
                sys.path.insert(0, str(scripts))
                support = import_module("platform_support")
                expected = (scripts / "platform_support.py").resolve(strict=True)
                if Path(str(support.__file__)).resolve(strict=True) != expected:
                    raise RuntimeError("Refusing a shadowed platform permission helper")
                support.set_private_path_mode(root, 0o700)
            finally:
                sys.path[:] = old_path
        environment = inherited.copy()
        for variable in ("TEMP", "TMP", "TMPDIR", "LOCALAPPDATA"):
            environment[variable] = str(root)
        yield environment


def run_tests(*, env: dict[str, str], with_coverage: bool) -> None:
    """Run all test suites directly or under branch coverage."""

    test_env = env.copy()
    test_env["PYTHONWARNINGS"] = STRICT_TEST_WARNINGS
    if not with_coverage:
        run((sys.executable, *unittest_command()), env=test_env)
        return

    check_env = env.copy()
    script_path = str(ROOT / "scripts")
    existing_pythonpath = check_env.get("PYTHONPATH")
    check_env["PYTHONPATH"] = (
        script_path + os.pathsep + existing_pythonpath if existing_pythonpath else script_path
    )
    run((sys.executable, "-m", "coverage", "erase"), env=env)
    for script in ("scripts/check_repository_boundary.py", "scripts/check_documentation.py"):
        run(
            (sys.executable, "-m", "coverage", "run", "--append", script),
            env=check_env,
        )
    run(
        (
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--append",
            *unittest_command(),
        ),
        env=test_env,
    )
    run((sys.executable, "-m", "coverage", "report"), env=env)


def run_quality_checks(*, env: dict[str, str]) -> None:
    """Run repository-scoped lint, format, and gradual type-checking gates."""

    validate_dev_tools()
    validate_repository_skill_yaml()
    validate_python_quality_scope()
    run((sys.executable, "-m", "ruff", "check", "."), env=env)
    run((sys.executable, "-m", "ruff", "format", "--check", "."), env=env)
    run((sys.executable, "-m", "mypy"), env=env)


def validate_shell_scripts(*, env: dict[str, str], require_bash: bool) -> None:
    """Run Bash syntax checks when Bash is available or explicitly required."""

    scripts = sorted(
        path for root in SHELL_SEARCH_ROOTS for path in root.rglob("*.sh") if path.is_file()
    )
    bash = find_bash()
    if bash is None:
        message = (
            "Bash was not found; shell syntax validation was skipped. "
            "Set TEDD_BASH to a Bash executable to enable it."
        )
        if require_bash:
            raise RuntimeError(message)
        print(message)
        return
    for script in scripts:
        argument = script.as_posix() if os.name == "nt" else str(script)
        run((str(bash), "-n", argument), env=env)
    print(f"Shell syntax check passed: {len(scripts)} scripts verified.")


def parser() -> argparse.ArgumentParser:
    """Build the release verifier command-line parser."""

    result = argparse.ArgumentParser(
        description="Run Tedd's isolated repository release verification"
    )
    result.add_argument(
        "--require-bash",
        action="store_true",
        help="fail instead of skipping shell syntax checks when Bash is unavailable",
    )
    result.add_argument(
        "--expected-python",
        type=python_major_minor,
        metavar="MAJOR.MINOR",
        help="fail unless the running interpreter has this major and minor version",
    )
    result.add_argument(
        "--quality",
        action="store_true",
        help="require the dev dependency group and run lint, format, types, and branch coverage",
    )
    return result


def main() -> int:
    """Run repository, documentation, manifest, shell, and test gates."""

    args = parser().parse_args()
    validate_python_version(args.expected_python)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    run((sys.executable, "scripts/check_repository_boundary.py"), env=env)
    run((sys.executable, "scripts/check_documentation.py"), env=env)
    validate_json(ROOT / "plugins/tedd/.codex-plugin/plugin.json")
    validate_json(ROOT / "plugins/tedd/.mcp.json")
    validate_json(ROOT / ".agents/plugins/marketplace.json")
    validate_shell_scripts(env=env, require_bash=args.require_bash)
    if args.quality:
        run_quality_checks(env=env)
    with isolated_test_environment(env) as test_env:
        run_tests(env=test_env, with_coverage=args.quality)
    print("Tedd release verification passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
