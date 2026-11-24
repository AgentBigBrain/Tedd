"""Compare the pinned Ruff complexity and statement counts with reviewed limits."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from importlib.util import find_spec
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = {"C901": 25, "PLR0915": 100}
REVIEW_FIELDS = {"owner", "reason", "review_when"}
Baseline = dict[str, dict[str, int]]
Finding = tuple[str, str, int]


def read_baseline(path: Path) -> Baseline:
    data: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Expected an object of reviewed function limits")
    limits: Baseline = {}
    for key, entry in data.items():
        filename, separator, name = key.partition("::")
        relative = PurePosixPath(filename)
        if (
            not separator
            or not filename.endswith(".py")
            or relative.is_absolute()
            or relative.as_posix() != filename
            or ".." in relative.parts
            or "\\" in filename
            or ":" in filename
            or not all(part.isidentifier() for part in name.split("."))
        ):
            raise ValueError(f"{key}: expected relative/path.py::Qualified.name")
        if not isinstance(entry, dict) or entry.keys() - REVIEW_FIELDS - THRESHOLDS.keys():
            raise ValueError(f"{key}: unexpected baseline fields")
        for field in sorted(REVIEW_FIELDS):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key}: supply a nonempty {field}")
        limits[key] = {}
        for code, minimum in THRESHOLDS.items():
            if code in entry:
                count = entry[code]
                if type(count) is not int or count <= minimum:
                    raise ValueError(f"{key}: {code} must be an integer above {minimum}")
                limits[key][code] = count
        if not limits[key]:
            raise ValueError(f"{key}: no measured rule")
    return limits


def function_names(path: Path) -> dict[int, str]:
    """Ruff reports the definition line; AST scopes give that line a stable name."""
    names: dict[int, str] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            qualified = prefix + node.name
            if not isinstance(node, ast.ClassDef):
                names[node.lineno] = qualified
            prefix = qualified + "."
        for child in ast.iter_child_nodes(node):
            visit(child, prefix)

    visit(ast.parse(path.read_bytes(), filename=str(path)), "")
    return names


def discovery_options(root: Path) -> list[str]:
    """Carry the repository's file selection into a Ruff run without lint suppressions."""
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    with (root / "pyproject.toml").open("rb") as stream:
        tool = tomllib.load(stream).get("tool", {})
    config = tool.get("ruff") if isinstance(tool, dict) else None
    if not isinstance(config, dict) or not config.get("include"):
        raise ValueError("Expected a nonempty tool.ruff.include in pyproject.toml")
    keys = (
        "include",
        "extend-include",
        "exclude",
        "extend-exclude",
        "respect-gitignore",
        "target-version",
    )
    return [
        f"--config={key}={json.dumps(config[key], ensure_ascii=False)}"
        for key in keys
        if key in config
    ]


def ruff_findings(root: Path) -> list[Finding]:
    if find_spec("ruff") is None:
        raise ValueError(
            "Ruff is required. Install the development tools with python -m pip install --group dev"
        )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            ".",
            "--isolated",
            *discovery_options(root),
            "--no-fix",
            "--ignore-noqa",
            "--select",
            ",".join(THRESHOLDS),
            "--output-format",
            "json",
            "--config",
            f"lint.mccabe.max-complexity={THRESHOLDS['C901']}",
            "--config",
            f"lint.pylint.max-statements={THRESHOLDS['PLR0915']}",
        ],
        cwd=root,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ValueError(f"Ruff exited {result.returncode}: {result.stderr.strip()}")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or bool(rows) != (result.returncode == 1):
        raise ValueError("Unexpected Ruff diagnostic output")
    names: dict[Path, dict[int, str]] = {}
    findings: list[Finding] = []
    for row in rows:
        code = row["code"]
        match = re.search(r"\((\d+) > (\d+)\)$", row["message"])
        if code not in THRESHOLDS or not match or int(match[2]) != THRESHOLDS[code]:
            raise ValueError(f"Unexpected Ruff diagnostic: {row['message']}")
        path = Path(row["filename"])
        if path not in names:
            names[path] = function_names(path)
        line = row["location"]["row"]
        if line not in names[path]:
            raise ValueError(f"{path}:{line}: expected a function definition")
        key = f"{path.relative_to(root).as_posix()}::{names[path][line]}"
        findings.append((key, code, int(match[1])))
    return findings


def growth_errors(findings: list[Finding], baseline: Baseline) -> list[str]:
    errors = []
    for key, code, count in findings:
        limit = baseline.get(key, {}).get(code, THRESHOLDS[code])
        if count > limit:
            errors.append(f"{key}: {code} measured {count}, allowed {limit}")
    return errors


def main() -> int:
    try:
        baseline = read_baseline(ROOT / "scripts/function_growth_baseline.json")
        findings = ruff_findings(ROOT)
    except (OSError, ValueError, SyntaxError, ModuleNotFoundError) as error:
        print(f"Function growth check failed: {error}", file=sys.stderr)
        return 1
    errors = growth_errors(findings, baseline)
    if errors:
        print(*errors, sep="\n", file=sys.stderr)
        print(
            "Review the design. If growth is justified, explain the change in the baseline.",
            file=sys.stderr,
        )
        return 1
    print(
        f"Function growth check passed ({len({key for key, _, _ in findings})} functions above trial thresholds)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
