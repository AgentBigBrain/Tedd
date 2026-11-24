#!/usr/bin/env python3
"""Validate repository-local Markdown links, CLI calls, JSON and MCP references."""

from __future__ import annotations

import argparse
import io
import json
import re
import shlex
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import cast
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
GRAPH_SCRIPTS = ROOT / "plugins/tedd/skills/obsidian-knowledge-graph/scripts"
TEDD_SCRIPTS = ROOT / "plugins/tedd/skills/tedd/scripts"
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FENCED_SHELL = re.compile(
    r"^```(bash|sh|powershell)[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL
)
FENCED_JSON = re.compile(r"```json\n(.*?)```", re.DOTALL)
PYTHON_CALL = re.compile(
    r"^(?:\$\w+\s*=\s*)?(?:python3?(?:\.exe)?|py(?:\.exe)?|&\s+(?:\$\w+|\"[^\"]+\"|'[^']+'))\s+"
)
PS_ARRAY = re.compile(r"\$(\w+)\s*(\+?=)\s*@\(([^()]*)\)")
PLACEHOLDER = re.compile(r"(?:\$(?:\w+(?:\.\w+)*|\{\w+\})|<[^<>]+>)\Z")
BASH_ARRAY = re.compile(r"\$\{\w+\[@\]\}\Z")
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
    import tedd_setup
    import tedd_sources

    return {
        "knowledge_graph.py": knowledge_graph.build_parser(),
        "memorybench_knowledge_graph.py": memorybench_knowledge_graph.parser(),
        "tedd_profile.py": tedd_profile.build_parser(),
        "tedd_sources.py": tedd_sources.parser(),
        "tedd_setup.py": tedd_setup.build_parser(),
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


def shell_words(line: str, powershell: bool, *, array: bool = False) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars="|")
    lexer.whitespace_split = True
    if powershell:
        lexer.escape = ""  # Backslashes are ordinary Windows path characters.
    if array:
        lexer.whitespace += ","
    return list(lexer)


def command_arguments(
    line: str,
    powershell: bool,
    arrays: dict[str, list[str]],
    parsers: dict[str, argparse.ArgumentParser],
) -> tuple[str, list[str]] | None:
    match = PYTHON_CALL.match(line)
    if not match:
        return None
    command = line[match.end() :]
    if not any(name in command for name in parsers) and not (
        powershell and any(word in arrays for word in command.split())
    ):
        return None
    words = shell_words(command, powershell)
    if powershell:
        words = [value for word in words for value in arrays.get(word, [word])]
    # A following pipeline processes the CLI output; it supplies no arguments.
    if "|" in words:
        words = words[: words.index("|")]
    while words and re.fullmatch(r"-B|-3(?:\.\d+)?", words[0]):
        words = words[1:]
    if not words:
        return None
    script = words[0].replace("\\", "/").rsplit("/", 1)[-1]
    return (script, words[1:]) if script in parsers else None


def parse_documented_args(
    parser: argparse.ArgumentParser, tokens: list[str], *, powershell: bool
) -> str | None:
    choices = parser_choices(parser)
    options = dict(parser._option_string_actions)
    if choices:
        arguments = iter(tokens)
        for token in arguments:
            if token in options and options[token].nargs is None:
                next(arguments, None)  # A global option can have a subcommand name as its value.
            elif token in choices:
                options.update(choices[token]._option_string_actions)
                break
        else:
            if not any(token in {"-h", "--help"} for token in tokens):
                return "example has no valid subcommand"
    unknown = sorted(
        {
            word.partition("=")[0]
            for word in tokens
            if word.startswith("--") and word != "--" and word.partition("=")[0] not in options
        }
    )
    if unknown:
        return f"unsupported options: {unknown}"
    if not powershell and any(BASH_ARRAY.fullmatch(word) for word in tokens):
        return None  # Required arguments may come from this shell array.

    prepared = []
    for index, word in enumerate(tokens):
        flag, equals, value = word.partition("=")
        option = flag if equals else tokens[index - 1] if index else ""
        action = options.get(option)
        if PLACEHOLDER.fullmatch(value if equals and flag.startswith("-") else word):
            if action is not None and action.choices:
                sample = str(next(iter(action.choices)))
            elif action is not None and action.type in (int, float):
                sample = "1"
            else:
                sample = "example"
            word = f"{flag}={sample}" if equals else sample
        prepared.append(word)
    stderr = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(io.StringIO()):
        try:
            parser.parse_args(prepared)
        except SystemExit as error:
            if error.code:
                return stderr.getvalue().strip().splitlines()[-1]
    return None


def check_cli_examples(files: list[Path]) -> tuple[list[str], int]:
    parsers = command_parsers()
    errors = []
    count = 0
    for path in files:
        markdown = path.read_text(encoding="utf-8")
        for match in FENCED_SHELL.finditer(markdown):
            powershell = match[1] == "powershell"
            arrays: dict[str, list[str]] = {}
            block = match[2].replace("`\n" if powershell else "\\\n", " ")
            for line in block.splitlines():
                try:
                    assignment = PS_ARRAY.search(line) if powershell else None
                    if assignment:
                        name, operator, values = assignment.groups()
                        words = shell_words(values, True, array=True)
                        arrays["@" + name] = (
                            arrays.get("@" + name, []) if operator == "+=" else []
                        ) + words
                    call = command_arguments(line.strip(), powershell, arrays, parsers)
                    if call:
                        script, tokens = call
                        count += 1
                        problem = parse_documented_args(
                            parsers[script], tokens, powershell=powershell
                        )
                        if problem:
                            errors.append(
                                f"{path.relative_to(ROOT)}: {script}: {problem}: {line.strip()}"
                            )
                except ValueError as error:
                    errors.append(f"{path.relative_to(ROOT)}: invalid shell example: {error}")
    return errors, count


def mcp_tool_inputs() -> dict[str, dict[str, set[str]]]:
    """Use the same descriptors returned by each server's tools/list handler."""
    sys.path.insert(0, str(GRAPH_SCRIPTS))
    from tedd_knowledge_graph import source_mcp, v4_mcp_stdio

    result: dict[str, dict[str, set[str]]] = {}
    for server, descriptors in (("source", source_mcp.TOOLS), ("graph", v4_mcp_stdio.TOOLS)):
        inputs = {}
        for descriptor in descriptors:
            name = descriptor["name"]
            schema = descriptor["inputSchema"]
            assert isinstance(name, str) and isinstance(schema, dict)
            properties = schema["properties"]
            assert isinstance(properties, dict)
            inputs[name] = set(properties)
        result[server] = inputs
    return result


MCP_MARKER = re.compile(r"<!-- mcp:([^>]*?) -->")
INLINE_REFERENCE = re.compile(r"`([^`\n]+)`\Z")


def check_mcp_examples(
    files: list[Path], schemas: dict[str, dict[str, set[str]]]
) -> tuple[list[str], int]:
    errors: list[str] = []
    checked = 0
    for path in files:
        markdown = path.read_text(encoding="utf-8")
        for marker in MCP_MARKER.finditer(markdown):
            checked += 1
            server, _, tool = marker[1].partition(".")
            label = f"{path.relative_to(ROOT)}:{markdown.count(chr(10), 0, marker.start()) + 1}"
            line_start = markdown.rfind("\n", 0, marker.start()) + 1
            inline = INLINE_REFERENCE.search(markdown, line_start, marker.start())
            block = FENCED_JSON.match(markdown, marker.end() + 1)
            if inline:
                value = inline[1]
            elif block and tool:
                value = block[1]
            else:
                errors.append(f"{label}: attach {marker[0]} to an inline name or JSON input")
                continue
            names = tool.split(",") if tool else [value]
            arguments: set[str] = set()
            if tool:
                if block or value.startswith("{"):
                    try:
                        obj = json.loads(value)
                    except ValueError:
                        obj = None
                    if not isinstance(obj, dict):
                        errors.append(f"{label}: expected a JSON input object for {server}.{tool}")
                        continue
                    arguments = set(obj)
                else:
                    arguments = {value.partition(":")[0]}
            for name in names:
                allowed = schemas.get(server, {}).get(name)
                if allowed is None:
                    errors.append(f"{label}: unknown {server} MCP tool {name!r}")
                elif unknown := sorted(arguments - allowed):
                    errors.append(f"{label}: unknown {server}.{name} arguments: {unknown}")
    return errors, checked


def check_json_examples(
    files: list[Path], schemas: dict[str, dict[str, set[str]]]
) -> tuple[list[str], int]:
    errors: list[str] = []
    checked = 0
    tools = {name: fields for server in schemas.values() for name, fields in server.items()}
    for path in files:
        markdown = path.read_text(encoding="utf-8")
        for index, block in enumerate(FENCED_JSON.findall(markdown), start=1):
            checked += 1
            try:
                value = json.loads(block)
            except json.JSONDecodeError as error:
                errors.append(f"{path.relative_to(ROOT)}: invalid JSON example {index}: {error}")
                continue
            if isinstance(value, dict) and value.get("method") == "tools/call":
                label = f"{path.relative_to(ROOT)}: MCP call in JSON example {index}"
                params = value.get("params")
                if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                    errors.append(f"{label} needs a tool name")
                    continue
                name = params["name"]
                arguments = params.get("arguments", {})
                if name not in tools:
                    errors.append(f"{label}: unknown MCP tool {name!r}")
                elif not isinstance(arguments, dict):
                    errors.append(f"{label}: {name} requires an argument object")
                elif unknown := sorted(set(arguments) - tools[name]):
                    errors.append(f"{label}: unknown {name} arguments: {unknown}")
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
    schemas = mcp_tool_inputs()
    mcp_errors, mcp_count = check_mcp_examples(files, schemas)
    json_errors, json_count = check_json_examples(files, schemas)
    errors = link_errors + cli_errors + mcp_errors + json_errors + routing_evaluation_errors()
    if errors:
        print("Documentation check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        "Documentation check passed: "
        f"{len(files)} Markdown files, {link_count} local links, "
        f"{command_count} CLI examples, {mcp_count} MCP references, and {json_count} JSON examples verified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
