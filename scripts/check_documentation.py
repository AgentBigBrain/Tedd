#!/usr/bin/env python3
"""Validate repository-local Markdown links and documented CLI options."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import cast
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
GRAPH_SCRIPTS = ROOT / "plugins/tedd/skills/obsidian-knowledge-graph/scripts"
TEDD_SCRIPTS = ROOT / "plugins/tedd/skills/tedd/scripts"
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FENCED_SHELL = re.compile(r"```(?:bash|sh)\n(.*?)```", re.DOTALL)
FENCED_JSON = re.compile(r"```json\n(.*?)```", re.DOTALL)
DOCUMENTED_GRAPH_COMMAND = re.compile(
    r"^python3 (?:-B )?scripts/"
    r"(knowledge_graph\.py|memorybench_knowledge_graph\.py|tedd_sources\.py)\s+(.+)$"
)
DOCUMENTED_PROFILE_COMMAND = re.compile(
    r"^python3 (?:scripts/|/path/to/installed/plugin/skills/tedd/scripts/)(tedd_profile\.py)\s+(.+)$"
)
ROUTING_EVALUATIONS = ROOT / "docs/evaluations"
ROUTING_EVALUATOR_SCHEMA = "tedd-routing-observations-v1"


def markdown_files() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=True,
        capture_output=True,
    )
    candidates = (
        ROOT / Path(raw.decode("utf-8"))
        for raw in result.stdout.split(b"\x00")
        if raw and raw.decode("utf-8").endswith(".md")
    )
    # Git's cached inventory still lists tracked files removed by a working-tree rename.
    paths: list[Path] = []
    for path in candidates:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        paths.append(path)
    return sorted(paths)


def github_heading_slugs(markdown: str) -> set[str]:
    counts: dict[str, int] = {}
    slugs: set[str] = set()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*$", markdown, re.MULTILINE):
        normalized = re.sub(r"[^\w\- ]", "", heading.strip().lower())
        normalized = re.sub(r"-+", "-", normalized.replace(" ", "-"))
        duplicate = counts.get(normalized, 0)
        counts[normalized] = duplicate + 1
        slugs.add(normalized if duplicate == 0 else f"{normalized}-{duplicate}")
    return slugs


def check_links(files: list[Path]) -> tuple[list[str], int]:
    errors: list[str] = []
    checked = 0
    for path in files:
        markdown = path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(markdown):
            raw = match.group(1).strip()
            if raw.startswith(("http://", "https://", "mailto:", "codex://")):
                continue
            destination = raw.split(maxsplit=1)[0].strip("<>")
            target_text, _, anchor = destination.partition("#")
            target = (path.parent / unquote(target_text or path.name)).resolve()
            checked += 1
            try:
                target.relative_to(ROOT)
            except ValueError:
                errors.append(f"{path.relative_to(ROOT)}: link leaves repository: {raw}")
                continue
            if not target.exists():
                errors.append(f"{path.relative_to(ROOT)}: missing link target: {raw}")
                continue
            if anchor and target.suffix.lower() == ".md":
                slugs = github_heading_slugs(target.read_text(encoding="utf-8"))
                if unquote(anchor).lower() not in slugs:
                    errors.append(f"{path.relative_to(ROOT)}: missing heading anchor: {raw}")
    return errors, checked


def command_parsers() -> dict[str, argparse.ArgumentParser]:
    sys.path.insert(0, str(GRAPH_SCRIPTS))
    sys.path.insert(0, str(TEDD_SCRIPTS))
    import knowledge_graph
    import memorybench_knowledge_graph
    import tedd_profile
    import tedd_sources

    return {
        "knowledge_graph.py": knowledge_graph.build_parser(),
        "memorybench_knowledge_graph.py": memorybench_knowledge_graph.parser(),
        "tedd_profile.py": tedd_profile.build_parser(),
        "tedd_sources.py": tedd_sources.parser(),
    }


def parser_choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if (
            isinstance(choices, dict)
            and choices
            and all(isinstance(name, str) for name in choices)
            and all(isinstance(choice, argparse.ArgumentParser) for choice in choices.values())
        ):
            return cast(dict[str, argparse.ArgumentParser], choices)
    return {}


def documented_commands(files: list[Path]) -> list[tuple[Path, str, str]]:
    commands: list[tuple[Path, str, str]] = []
    for path in files:
        markdown = path.read_text(encoding="utf-8")
        for block in FENCED_SHELL.findall(markdown):
            for line in block.replace("\\\n", " ").splitlines():
                match = DOCUMENTED_GRAPH_COMMAND.match(line.strip())
                if match:
                    commands.append((path, match.group(1), match.group(2)))
                    continue
                match = DOCUMENTED_PROFILE_COMMAND.match(line.strip())
                if match:
                    commands.append((path, match.group(1), match.group(2)))
    return commands


def check_cli_examples(files: list[Path]) -> tuple[list[str], int]:
    parsers = command_parsers()
    errors: list[str] = []
    commands = documented_commands(files)
    for path, script, command_text in commands:
        try:
            tokens = shlex.split(command_text)
        except ValueError as error:
            errors.append(f"{path.relative_to(ROOT)}: invalid shell example: {error}")
            continue
        parser = parsers[script]
        if script in {"knowledge_graph.py", "tedd_profile.py", "tedd_sources.py"}:
            choices = parser_choices(parser)
            command = next((token for token in tokens if token in choices), None)
            if command is None:
                errors.append(
                    f"{path.relative_to(ROOT)}: {script} example has no valid subcommand: {command_text}"
                )
                continue
            valid_options = set(parser._option_string_actions) | set(
                choices[command]._option_string_actions
            )
        else:
            valid_options = set(parser._option_string_actions)
        unknown = sorted(
            token for token in tokens if token.startswith("--") and token not in valid_options
        )
        if unknown:
            errors.append(f"{path.relative_to(ROOT)}: unsupported {script} options: {unknown}")
    return errors, len(commands)


def check_json_examples(files: list[Path]) -> tuple[list[str], int]:
    errors: list[str] = []
    checked = 0
    for path in files:
        markdown = path.read_text(encoding="utf-8")
        for index, block in enumerate(FENCED_JSON.findall(markdown), start=1):
            checked += 1
            try:
                json.loads(block)
            except json.JSONDecodeError as error:
                errors.append(f"{path.relative_to(ROOT)}: invalid JSON example {index}: {error}")
    return errors, checked


def routing_evaluation_errors(evaluations: Path = ROUTING_EVALUATIONS) -> list[str]:
    """Keep routing evaluations schema-marked and free of task deep links."""

    reports = sorted(evaluations.glob("*routing*.md"))
    if not reports:
        return ["missing routing evaluation report"]
    errors: list[str] = []
    for report in reports:
        evaluation = report.read_text(encoding="utf-8")
        label = report.relative_to(evaluations)
        if ROUTING_EVALUATOR_SCHEMA not in evaluation:
            errors.append(f"{label}: routing evaluation does not name the evaluator schema/version")
        if "codex://" in evaluation:
            errors.append(f"{label}: routing evaluation contains a Codex task URI")
    return errors


def main() -> int:
    files = markdown_files()
    link_errors, link_count = check_links(files)
    cli_errors, command_count = check_cli_examples(files)
    json_errors, json_count = check_json_examples(files)
    errors = link_errors + cli_errors + json_errors + routing_evaluation_errors()
    if errors:
        print("Documentation check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        "Documentation check passed: "
        f"{len(files)} Markdown files, {link_count} local links, "
        f"{command_count} CLI examples, and {json_count} JSON examples verified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
