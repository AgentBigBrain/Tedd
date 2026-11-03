#!/usr/bin/env python3
"""Fail closed when private runtime material enters the Tedd source tree."""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import MappingProxyType
from typing import Final, Mapping
from urllib.parse import unquote

from agent_plugin_contract import portable_mcp_errors, portable_plugin_errors

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins/tedd"
PLUGIN_MANIFEST = ROOT / "plugins/tedd/.codex-plugin/plugin.json"
PORTABLE_PLUGIN_MANIFEST = PLUGIN_ROOT / "plugin.json"
PORTABLE_MCP_MANIFEST = PLUGIN_ROOT / "mcp.json"
MCP_CONFIG = PLUGIN_ROOT / ".mcp.json"
PLUGIN_ICON = ROOT / "plugins/tedd/assets/tedd-brain.png"
MARKETPLACE_MANIFEST = ROOT / ".agents/plugins/marketplace.json"
CLAUDE_PLUGIN_MANIFEST = PLUGIN_ROOT / ".claude-plugin/plugin.json"
CLAUDE_MARKETPLACE_MANIFEST = ROOT / ".claude-plugin/marketplace.json"
ALLOWED_JSON = {
    PLUGIN_MANIFEST.resolve(),
    PORTABLE_PLUGIN_MANIFEST.resolve(),
    PORTABLE_MCP_MANIFEST.resolve(),
    MCP_CONFIG.resolve(),
    MARKETPLACE_MANIFEST.resolve(),
    CLAUDE_PLUGIN_MANIFEST.resolve(),
    CLAUDE_MARKETPLACE_MANIFEST.resolve(),
    (PLUGIN_ROOT / "hooks/codex.json").resolve(),
}
ALLOWED_BINARY_PATHS: Final[frozenset[Path]] = frozenset(
    {Path("plugins/tedd/assets/tedd-brain.png")}
)
ALLOWED_SUPPORT_TEXT_PATHS: Final[frozenset[Path]] = frozenset(
    {
        Path("scripts/native/source_av1_validate.c"),
        Path("tests/knowledge_graph/libavif-fixture-LICENSE.txt"),
    }
)
PLUGIN_ICON_REFERENCE: Final = "./assets/tedd-brain.png"
PLUGIN_ICON_FIELDS: Final = ("composerIcon", "logo", "logoDark")
PACKAGED_SKILLS: Final[frozenset[str]] = frozenset(
    {"obsidian-knowledge-graph", "orchestrate-work", "tedd", "tedd-development"}
)
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
WINDOWS_REPARSE_POINT: Final = 0x400
MAX_MCP_CONFIG_BYTES: Final = 64 * 1024

FORBIDDEN_EXACT_NAMES = {
    ".DS_Store",
    ".env",
    "cases.json",
    "config.json",
    "graph.json",
    "graph.sqlite3",
}
FORBIDDEN_SUFFIXES = {
    ".backup",
    ".db",
    ".key",
    ".lock",
    ".p12",
    ".pem",
    ".pfx",
    ".pyc",
    ".sqlite",
    ".sqlite3",
}
FORBIDDEN_PARTS = {
    ".obsidian",
    "Codex Knowledge",
    "Tedd Knowledge",
    "__pycache__",
    "backups",
    "packets",
}
TOKEN_PATTERNS = {
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "basic or bearer credential": re.compile(
        rb"\b(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]{16,}", re.I
    ),
    "GitHub token": re.compile(rb"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    "GitHub fine-grained token": re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "GitLab token": re.compile(rb"\bglpat-[A-Za-z0-9_-]{10,}\b", re.I),
    "Google API key": re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "JWT": re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "npm token": re.compile(rb"\bnpm_[A-Za-z0-9]{20,}\b", re.I),
    "provider secret token": re.compile(rb"\b(?:sk|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b", re.I),
    "private key": re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "signed credential URL": re.compile(
        rb"[?&](?:access_token|api_key|apikey|auth|authorization|client_secret|key|password|secret|sig|signature|token)=[^&#\s]+",
        re.I,
    ),
    "URL userinfo credential": re.compile(rb"\b[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@", re.I),
}
ALLOWED_SUFFIXES = {".md", ".ps1", ".py", ".sh", ".toml", ".yaml", ".yml"}
ALLOWED_EXTENSIONLESS = {".gitattributes", ".gitignore", "LICENSE"}
SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?$"
)
PYTHON_RULE_MAPPING: Final[Mapping[str, str]] = MappingProxyType(
    {
        "skills/coding-standards/rules/error-no-silent-exceptions.md": (
            "coding-error-no-silent-exceptions.md"
        ),
        "skills/coding-standards/rules/async-context-manager.md": (
            "coding-async-resource-lifecycle.md"
        ),
        "skills/coding-standards/rules/async-create-task.md": "coding-async-task-ownership.md",
        "skills/coding-standards/rules/async-gather.md": ("coding-async-concurrency-results.md"),
        "skills/coding-standards/rules/async-semaphore.md": "coding-async-bounded-work.md",
        "skills/coding-standards/rules/design-dependency-injection.md": (
            "coding-design-dependency-injection.md"
        ),
        "skills/coding-standards/rules/design-early-return.md": ("coding-design-control-flow.md"),
        "skills/coding-standards/rules/design-no-global-singleton.md": (
            "coding-design-shared-state.md"
        ),
        "skills/coding-standards/rules/design-philosophy.md": ("coding-design-pragmatism.md"),
        "skills/coding-standards/rules/design-pure-functions.md": (
            "coding-design-pure-boundaries.md"
        ),
        "skills/coding-standards/rules/design-single-responsibility.md": (
            "coding-design-responsibility-seams.md"
        ),
        "skills/coding-standards/rules/doc-docstring.md": ("coding-documentation-api-contracts.md"),
        "skills/coding-standards/rules/doc-type-hints.md": ("coding-typing-public-contracts.md"),
        "skills/coding-standards/rules/validation-pydantic.md": (
            "coding-validation-boundary-data.md"
        ),
        "skills/coding-standards/rules/oop-composition-over-inheritance.md": (
            "coding-design-composition.md"
        ),
        "skills/coding-standards/rules/oop-dataclass.md": "coding-data-dataclasses.md",
        "skills/coding-standards/rules/oop-property.md": "coding-data-properties.md",
        "skills/coding-standards/rules/oop-protocol.md": "coding-typing-protocols.md",
        "skills/coding-standards/rules/perf-dict-get.md": ("coding-collections-mapping-access.md"),
        "skills/coding-standards/rules/perf-generator-expression.md": (
            "coding-performance-materialization.md"
        ),
        "skills/coding-standards/rules/perf-list-comprehension.md": (
            "coding-collections-comprehensions.md"
        ),
        "skills/coding-standards/rules/perf-set-lookup.md": ("coding-collections-membership.md"),
        "skills/coding-standards/rules/perf-str-join.md": (
            "coding-text-building-and-serialization.md"
        ),
        "skills/coding-standards/rules/solid-isp.md": ("coding-design-interface-segregation.md"),
        "skills/coding-standards/rules/solid-lsp.md": ("coding-design-substitutability.md"),
        "skills/coding-standards/rules/solid-ocp.md": ("coding-design-extension-points.md"),
        "skills/testing/rules/fixture-conftest.md": "testing-fixture-shared-support.md",
        "skills/testing/rules/fixture-factory.md": "testing-fixture-factory.md",
        "skills/testing/rules/fixture-scope.md": "testing-fixture-scope.md",
        "skills/testing/rules/mock-autospec.md": "testing-mock-autospec.md",
        "skills/testing/rules/mock-boundaries.md": "testing-mock-boundaries.md",
        "skills/testing/rules/mock-monkeypatch.md": "testing-mock-monkeypatch.md",
        "skills/testing/rules/param-ids.md": "testing-param-ids.md",
        "skills/testing/rules/param-parametrize.md": "testing-param-subtest-cases.md",
        "skills/testing/rules/struct-aaa-pattern.md": "testing-struct-aaa-pattern.md",
        "skills/testing/rules/struct-no-logic.md": "testing-struct-no-logic.md",
        "skills/testing/rules/struct-one-behavior.md": "testing-struct-one-behavior.md",
        "skills/testing/rules/struct-test-naming.md": "testing-struct-test-naming.md",
        "skills/tooling/rules/analysis-pyscn.md": "tooling-analysis-pyscn.md",
        "skills/tooling/rules/fmt-ruff.md": "tooling-fmt-ruff.md",
        "skills/tooling/rules/lint-ruff.md": "tooling-lint-ruff.md",
        "skills/tooling/rules/pkg-pyproject.md": "tooling-pkg-pyproject.md",
        "skills/tooling/rules/pkg-uv.md": "tooling-pkg-reviewed-dependencies.md",
        "skills/tooling/rules/test-pytest.md": "tooling-test-unittest.md",
        "skills/tooling/rules/type-mypy.md": "tooling-type-mypy.md",
    }
)
PYTHON_RULE_FILE = re.compile(r"^(?:coding|testing|tooling)-.+\.md$")
COVERAGE_ROW = re.compile(
    r"(?m)^\|\s*`(?P<upstream>skills/[^`|\r\n]+/rules/[^`|\r\n]+\.md)`"
    r"\s*\|\s*\[[^\]\r\n]*\]\((?P<local>[^)\r\n]+\.md)\)\s*\|"
)
PUBLISHER_IDENTITY_FILES = {
    (ROOT / "LICENSE").resolve(),
    (ROOT / "SECURITY.md").resolve(),
    PLUGIN_MANIFEST.resolve(),
    PORTABLE_PLUGIN_MANIFEST.resolve(),
    Path(__file__).resolve(),
}
PUBLISHER_IDENTITY_PATTERN = re.compile(
    rb"\banthony(?:\s+j\.)?\s+benacquisto\b",
    re.I,
)
DEVELOPER_HOME_PATH = re.compile(
    rb"(?:/(?:Users|home)|[A-Za-z]:[\\/]+Users)[\\/]+(?P<user>[A-Za-z0-9_][A-Za-z0-9._-]*)(?=$|[^A-Za-z0-9._-])",
    re.I,
)
SYNTHETIC_DEVELOPER_HOME_NAMES: Final[frozenset[bytes]] = frozenset(
    {b"alice", b"example", b"operator", b"person"}
)


def repository_files() -> list[Path]:
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
    candidates = (ROOT / Path(raw.decode("utf-8")) for raw in result.stdout.split(b"\x00") if raw)
    # Removed tracked paths have no payload; required package files have separate contracts.
    paths: list[Path] = []
    for path in candidates:
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        paths.append(path)
    return sorted(paths)


def synthetic_security_fixture(path: Path, match: bytes) -> bool:
    """Permit only conspicuously synthetic credential-shaped sanitizer fixtures."""

    return path.name.startswith("test_") and any(
        marker in match for marker in (b"ULTRA_UNIQUE", b"TOPSECRET", b".internal")
    )


def synthetic_path_fixture(path: Path, match: re.Match[bytes]) -> bool:
    """Permit only explicitly named synthetic home-path fixtures in tests."""

    synthetic_user = match.group("user").lower()
    return path.name.startswith("test_") and (
        synthetic_user.startswith(b"synthetic-") or synthetic_user in SYNTHETIC_DEVELOPER_HOME_NAMES
    )


def artifact_errors(path: Path, payload: bytes) -> list[str]:
    relative = path.relative_to(ROOT)
    parts = set(relative.parts)
    lower_parts = {part.lower() for part in relative.parts}
    lower_path = relative.as_posix().lower()
    name = path.name
    errors: list[str] = []

    if path.suffix not in ALLOWED_SUFFIXES and name not in ALLOWED_EXTENSIONLESS:
        if (
            path.resolve() not in ALLOWED_JSON
            and relative not in ALLOWED_BINARY_PATHS
            and relative not in ALLOWED_SUPPORT_TEXT_PATHS
        ):
            errors.append(f"unapproved infrastructure file type: {relative}")

    if (
        name in FORBIDDEN_EXACT_NAMES
        or name.startswith(".env.")
        or name.startswith("graph.sqlite3")
        or name.startswith("graph.db")
        or name.startswith("graph.json")
    ):
        errors.append(f"forbidden runtime/config file: {relative}")
    if any(name.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        errors.append(f"forbidden private/generated suffix: {relative}")
    if name.endswith("-wal") or name.endswith("-shm"):
        errors.append(f"forbidden SQLite sidecar: {relative}")
    if path.suffix == ".jsonl":
        errors.append(f"forbidden runtime data stream: {relative}")
    if parts & FORBIDDEN_PARTS:
        errors.append(f"forbidden runtime/generated directory: {relative}")
    if lower_path.startswith(("docs/superpowers/", ".superpowers/", "superpowers/")):
        errors.append(f"forbidden contributor scratch directory: {relative}")
    if "memorybench" in lower_parts:
        errors.append(f"forbidden private MemoryBench directory: {relative}")
    if path.suffix == ".json" and path.resolve() not in ALLOWED_JSON:
        errors.append(f"unexpected JSON data file: {relative}")
    if payload.startswith(b"SQLite format 3\x00"):
        errors.append(f"embedded SQLite database: {relative}")
    if path.suffix == ".py":
        try:
            compile(payload, str(relative), "exec")
        except (SyntaxError, ValueError) as exc:
            errors.append(f"invalid Python source {relative}: {exc}")
    for label, pattern in TOKEN_PATTERNS.items():
        for match in pattern.finditer(payload):
            if not synthetic_security_fixture(path, match.group(0)):
                errors.append(f"possible {label} in {relative}")
                break
    if path.resolve() not in PUBLISHER_IDENTITY_FILES and PUBLISHER_IDENTITY_PATTERN.search(
        payload
    ):
        errors.append(f"publisher identity leaked into portable runtime content: {relative}")
    for match in DEVELOPER_HOME_PATH.finditer(payload):
        if not synthetic_path_fixture(path, match):
            errors.append(
                f"developer-home path leaked into portable contributor content: {relative}"
            )
            break
    return errors


def _read_contract_text(path: Path, label: str, errors: list[str]) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"could not read repository-local Python {label}: {exc}")
        return None


def _markdown_link_destinations(markdown: str) -> set[str]:
    """Read inline destinations using the documentation checker's narrow syntax."""

    destinations: set[str] = set()
    for raw in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", markdown):
        parts = raw.strip().split(maxsplit=1)
        if parts:
            destinations.add(parts[0].strip("<>"))
    return destinations


def reachable_markdown(entry: Path, root: Path) -> tuple[set[Path], list[str]]:
    """Follow local documentation links within one package without reading outside it."""

    root = root.resolve()
    pending = [entry.resolve()]
    visited: set[Path] = set()
    errors: list[str] = []
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        if not path.is_relative_to(root):
            errors.append(f"skill reference leaves its package: {path.name}")
            continue
        visited.add(path)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append(f"unreadable skill reference: {path.relative_to(root)}")
            continue
        for raw in sorted(_markdown_link_destinations(content)):
            if raw.startswith(("https://", "http://", "mailto:", "codex://")):
                continue
            target_text = unquote(raw.split("#", 1)[0])
            target = (path.parent / target_text).resolve() if target_text else path
            if not target.is_relative_to(root):
                errors.append(f"skill reference leaves its package: {path.relative_to(root)}")
            elif not target.is_file():
                errors.append(f"missing skill reference: {target.relative_to(root)}")
            elif target.suffix.casefold() == ".md" and target not in visited:
                pending.append(target)
    return visited, errors


def python_skill_contract_errors(skill_root: Path) -> list[str]:
    """Validate rule-card inventory, reachable documentation and upstream provenance."""

    errors: list[str] = []
    skill_root = skill_root.resolve()
    references = skill_root / "references"
    skill_file = skill_root / "SKILL.md"
    coverage_file = references / "UPSTREAM_COVERAGE.md"
    required_files = {
        "skill": skill_file,
        "coverage manifest": coverage_file,
        "provenance": references / "upstream-provenance.md",
        "rule authoring template": references / "rule-authoring-template.md",
    }
    for label, path in required_files.items():
        if not path.is_file():
            errors.append(f"missing repository-local Python {label}: {path}")

    if len(PYTHON_RULE_MAPPING) != 45:
        errors.append("internal repository-local Python rule mapping must contain exactly 45 rules")
    duplicate_targets = sorted(
        target for target, count in Counter(PYTHON_RULE_MAPPING.values()).items() if count != 1
    )
    if duplicate_targets:
        errors.append(
            "internal repository-local Python rule mapping contains duplicate targets: "
            + ", ".join(duplicate_targets)
        )

    expected_rules = set(PYTHON_RULE_MAPPING.values())
    actual_rules: set[str] = set()
    if references.is_dir():
        for path in references.rglob("*.md"):
            relative = path.relative_to(references)
            if PYTHON_RULE_FILE.fullmatch(path.name) or (
                relative.parts and relative.parts[0] == "rules"
            ):
                actual_rules.add(relative.as_posix())
    for missing in sorted(expected_rules - actual_rules):
        errors.append(f"missing repository-local Python rule card: {missing}")
    for unexpected in sorted(actual_rules - expected_rules):
        errors.append(f"unexpected repository-local Python rule card: {unexpected}")

    reachable, link_errors = reachable_markdown(skill_file, skill_root)
    errors.extend(link_errors)
    for local_name in sorted(expected_rules):
        if (references / local_name).resolve() not in reachable:
            errors.append(f"repository-local Python rule card is unreachable: {local_name}")

    for local_name in sorted(expected_rules & actual_rules):
        card = references / local_name
        card_text = _read_contract_text(card, f"rule card {local_name}", errors)
        if card_text is None:
            continue
        fence_count = len(re.findall(r"(?m)^[ \t]*```", card_text))
        if fence_count % 2:
            errors.append(
                f"repository-local Python rule card {local_name} has "
                f"unbalanced fenced code blocks ({fence_count} fences)"
            )

    coverage_text = (
        _read_contract_text(coverage_file, "coverage manifest", errors)
        if coverage_file.is_file()
        else None
    )
    if coverage_text is not None:
        coverage_rows = [
            (match.group("upstream"), match.group("local"))
            for match in COVERAGE_ROW.finditer(coverage_text)
        ]
        pair_counts = Counter(coverage_rows)
        upstream_counts = Counter(upstream for upstream, _ in coverage_rows)
        local_counts = Counter(local for _, local in coverage_rows)

        for upstream, local_name in PYTHON_RULE_MAPPING.items():
            if upstream_counts[upstream] != 1:
                errors.append(
                    "repository-local Python coverage manifest must contain upstream "
                    f"rule {upstream} exactly once, found {upstream_counts[upstream]}"
                )
            if local_counts[local_name] != 1:
                errors.append(
                    "repository-local Python coverage manifest must contain local "
                    f"target {local_name} exactly once, found {local_counts[local_name]}"
                )
            if pair_counts[(upstream, local_name)] != 1:
                errors.append(
                    "repository-local Python coverage manifest must map "
                    f"{upstream} to {local_name} exactly once"
                )

        for (upstream, local_name), count in sorted(pair_counts.items()):
            expected_local = PYTHON_RULE_MAPPING.get(upstream)
            if expected_local is None:
                errors.append(
                    "repository-local Python coverage manifest contains unexpected "
                    f"upstream rule: {upstream}"
                )
            elif expected_local != local_name:
                errors.append(
                    "repository-local Python coverage manifest maps "
                    f"{upstream} to {local_name}, expected {expected_local}"
                )
            if local_name not in expected_rules:
                errors.append(
                    "repository-local Python coverage manifest contains unexpected "
                    f"local target: {local_name}"
                )
            if count > 1:
                errors.append(
                    "repository-local Python coverage manifest contains duplicate "
                    f"mapping: {upstream} -> {local_name}"
                )

        if len(coverage_rows) != len(PYTHON_RULE_MAPPING):
            errors.append(
                "repository-local Python coverage manifest must contain exactly "
                f"45 rule rows, found {len(coverage_rows)}"
            )

    return errors


def plugin_icon_reference_errors(interface: Mapping[str, object]) -> list[str]:
    """Require every supported icon field to reference the one packaged asset."""

    return [
        f"plugin interface.{field} must be '{PLUGIN_ICON_REFERENCE}'"
        for field in PLUGIN_ICON_FIELDS
        if interface.get(field) != PLUGIN_ICON_REFERENCE
    ]


def plugin_icon_file_errors(icon_path: Path) -> list[str]:
    """Validate the packaged icon without admitting arbitrary binary assets."""

    try:
        metadata = icon_path.lstat()
    except OSError:
        return ["plugin icon asset is missing"]
    if not stat.S_ISREG(metadata.st_mode) or (
        getattr(metadata, "st_file_attributes", 0) & WINDOWS_REPARSE_POINT
    ):
        return ["plugin icon asset must be a regular non-reparse file"]
    try:
        signature = icon_path.read_bytes()[: len(PNG_SIGNATURE)]
    except OSError:
        return ["plugin icon asset could not be read"]
    if signature != PNG_SIGNATURE:
        return ["plugin icon asset must have a valid PNG signature"]
    return []


def _atx_heading_offsets(markdown: str) -> list[tuple[int, int, str]]:
    """Return ATX heading levels, offsets, and normalized titles."""

    return [
        (len(match.group(1)), match.start(), match.group(2).strip().casefold())
        for match in re.finditer(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*$", markdown)
    ]


def _markdown_section(markdown: str, title: str) -> tuple[int, str] | None:
    """Find one ATX section and its body through the next peer heading."""

    headings = _atx_heading_offsets(markdown)
    normalized_title = title.casefold()
    for index, (level, offset, heading) in enumerate(headings):
        if heading != normalized_title:
            continue
        end = len(markdown)
        for next_level, next_offset, _ in headings[index + 1 :]:
            if next_level <= level:
                end = next_offset
                break
        return offset, markdown[offset:end]
    return None


def _has_heading(markdown: str, title: str) -> bool:
    return _markdown_section(markdown, title) is not None


def _template_field_names(markdown: str) -> set[str]:
    """Extract line-oriented template keys without inspecting template values."""

    return {
        match.group(1).strip().casefold()
        for match in re.finditer(r"(?m)^\s*(?:-\s+)?([A-Za-z][^:\r\n]+):", markdown)
    }


def _template_field_value(markdown: str, field: str) -> str | None:
    """Return one line-oriented template value without interpreting its contents."""

    match = re.search(
        r"(?m)^\s*(?:-\s+)?" + re.escape(field) + r":\s*(.*?)\s*$",
        markdown,
    )
    return match.group(1) if match is not None else None


def orchestration_skill_contract_errors(skill_root: Path, tedd_skill: Path) -> list[str]:
    """Check orchestration packaging and documented handoff contracts.

    These static checks do not establish agent behavior. The core Tedd skill's
    prose and section order can evolve without prescribing a fixed itinerary.
    Keep behavioral guarantees in the skill and verify them with scoped scenarios.
    """

    errors: list[str] = []
    skill_file = skill_root / "SKILL.md"
    routing_policy = skill_root / "references/routing-policy.md"
    handoff_packet = skill_root / "references/handoff-packet.md"
    metadata_file = skill_root / "agents/openai.yaml"
    required_files = {
        "orchestration skill": skill_file,
        "orchestration routing policy": routing_policy,
        "canonical handoff packet": handoff_packet,
        "orchestration UI metadata": metadata_file,
        "canonical Tedd skill": tedd_skill,
    }
    contents: dict[str, str] = {}
    for label, path in required_files.items():
        try:
            contents[label] = path.read_text(encoding="utf-8")
        except OSError:
            errors.append(f"missing {label}: {path}")

    skill_text = contents.get("orchestration skill", "")
    policy_text = contents.get("orchestration routing policy", "")
    handoff_text = contents.get("canonical handoff packet", "")
    metadata_text = contents.get("orchestration UI metadata", "")
    tedd_text = contents.get("canonical Tedd skill", "")

    required_links = {
        "../tedd/SKILL.md": "orchestration skill must route through canonical Tedd",
        "references/routing-policy.md": "orchestration skill must link its routing policy",
        "references/handoff-packet.md": "orchestration skill must link the canonical handoff packet",
    }
    linked_paths = {
        unquote(target.split("#", 1)[0]) for target in _markdown_link_destinations(skill_text)
    }
    for target, error in required_links.items():
        if target not in linked_paths:
            errors.append(error)
    policy_paths = {
        unquote(target.split("#", 1)[0]) for target in _markdown_link_destinations(policy_text)
    }
    if "handoff-packet.md" not in policy_paths:
        errors.append("routing policy must link the canonical handoff packet")

    # Authority, single-writer ownership, router choice and interruption semantics
    # need scenario review. Matching a sentence cannot prove those decisions.
    # These two headings identify the public templates, whose field keys and
    # status vocabulary remain contracts; ordinary workflow headings are flexible.
    handoff_section = _markdown_section(handoff_text, "Handoff packet template")
    handoff_fields = _template_field_names(handoff_section[1]) if handoff_section else set()
    for field in (
        "objective",
        "verified state",
        "hypotheses",
        "permissions",
        "owned artifacts",
        "relevant user instructions or messages",
        "task-specific constraints",
        "required durable records",
        "current watermark and scope identifiers",
        "output contract",
        "stop condition",
        "leaf boundary",
    ):
        if field not in handoff_fields:
            errors.append(f"handoff packet must contain field: {field}")
    if not _has_heading(handoff_text, "Live-delta format"):
        errors.append("handoff packet must define a live-delta format")
    else:
        live_delta_section = _markdown_section(handoff_text, "Live-delta format")
        assert live_delta_section is not None
        live_delta_fields = _template_field_names(live_delta_section[1])
        for field in (
            "delta kind",
            "current watermark and scope identifiers",
            "verified change",
            "hypothesis update",
            "status",
            "changed artifacts",
            "blockers or needs",
            "next action",
        ):
            if field not in live_delta_fields:
                errors.append(f"live-delta format must contain field: {field}")
        status_value = _template_field_value(live_delta_section[1], "status")
        expected_statuses = (
            "investigating",
            "implementing",
            "verifying",
            "blocked",
            "complete",
        )
        actual_statuses = (
            tuple(part.strip() for part in status_value.split("|"))
            if status_value is not None
            else ()
        )
        if actual_statuses != expected_statuses:
            errors.append(
                "live-delta status must use exactly: investigating | implementing | verifying | "
                "blocked | complete"
            )

    if not re.search(
        r"(?m)^\s*allow_implicit_invocation:\s*true\s*$",
        metadata_text,
    ):
        errors.append("orchestrate-work must allow implicit invocation for adaptive routing")
    tedd_paths = {
        unquote(target.split("#", 1)[0]) for target in _markdown_link_destinations(tedd_text)
    }
    if "../orchestrate-work/SKILL.md" not in tedd_paths:
        errors.append("canonical Tedd skill must link the orchestrate-work skill")
    return errors


def claude_manifest_errors(plugin: Mapping[str, object]) -> list[str]:
    """Keep the Claude package limited to shared skills and one read-only graph launch."""
    documents: list[dict[str, object]] = []
    for path in (CLAUDE_PLUGIN_MANIFEST, CLAUDE_MARKETPLACE_MANIFEST):
        try:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or (getattr(info, "st_file_attributes", 0) & WINDOWS_REPARSE_POINT)
                or info.st_size > MAX_MCP_CONFIG_BYTES
            ):
                return ["Claude manifest must be a bounded physical file"]
            document = json.loads(path.read_bytes(), object_pairs_hook=_unique_mcp_json_object)
        except (OSError, ValueError, RecursionError):
            return ["Claude manifest is missing or invalid"]
        if not isinstance(document, dict):
            return ["Claude manifest must contain a JSON object"]
        documents.append(document)
    manifest, marketplace = documents
    errors: list[str] = []
    metadata = ("name", "version", "description", "author", "homepage", "repository", "license")
    if set(manifest) != {*metadata, "userConfig", "mcpServers", "hooks"} or any(
        manifest.get(key) != plugin.get(key) for key in metadata
    ):
        errors.append("Claude metadata must match the shared package without extra components")
    options = manifest.get("userConfig")
    if not isinstance(options, dict) or set(options) != {"python", "graph_config"}:
        errors.append("Claude requires exactly Python and graph-config file options")
    else:
        for option in options.values():
            if (
                not isinstance(option, dict)
                or set(option) != {"type", "required", "title", "description"}
                or option.get("type") != "file"
                or option.get("required") is not True
                or any(
                    not isinstance(option.get(key), str) or not option[key].strip()
                    for key in ("title", "description")
                )
            ):
                errors.append("Claude file options must be required and have no machine defaults")
    expected_server = {
        "tedd-graph": {
            "command": "${user_config.python}",
            "args": [
                "-B",
                "${CLAUDE_PLUGIN_ROOT}/skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py",
                "--config",
                "${user_config.graph_config}",
            ],
        }
    }
    if manifest.get("mcpServers") != expected_server:
        errors.append("Claude must launch only the fixed graph script with explicit per-user paths")
    expected_hook = {
        "type": "command",
        "command": "${user_config.python}",
        "args": [
            "-B",
            "${CLAUDE_PLUGIN_ROOT}/skills/tedd/scripts/tedd_handoff_hook.py",
            "--host",
            "claude",
            "--config",
            "${user_config.graph_config}",
        ],
        "timeout": 10,
    }
    if manifest.get("hooks") != {
        event: [{"hooks": [expected_hook]}]
        for event in ("SessionStart", "UserPromptSubmit", "Stop")
    }:
        errors.append("Claude hooks must use only the bounded handoff checker with per-user paths")
    expected_marketplace = {
        "name": "tedd",
        "description": plugin.get("description"),
        "owner": {"name": "AgentBigBrain"},
        "plugins": [
            {"name": "tedd", "source": "./plugins/tedd", "description": plugin.get("description")}
        ],
    }
    if marketplace != expected_marketplace:
        errors.append("Claude marketplace must point only to the shared Tedd package")
    return errors


def manifest_errors() -> list[str]:
    errors: list[str] = []
    try:
        plugin = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        marketplace = json.loads(MARKETPLACE_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"manifest parse failure: {exc}"]

    allowed_plugin_fields = {
        "author",
        "description",
        "homepage",
        "interface",
        "keywords",
        "license",
        "mcpServers",
        "hooks",
        "name",
        "repository",
        "skills",
        "version",
    }
    unknown_plugin_fields = sorted(set(plugin) - allowed_plugin_fields)
    if unknown_plugin_fields:
        errors.append(f"unsupported plugin manifest fields: {unknown_plugin_fields}")
    if plugin.get("name") != "tedd":
        errors.append("plugin manifest name must be 'tedd'")
    if not SEMVER.fullmatch(str(plugin.get("version", ""))):
        errors.append(
            "plugin version must be SemVer MAJOR.MINOR.PATCH with an optional prerelease suffix"
        )
    if not isinstance(plugin.get("description"), str) or not plugin["description"].strip():
        errors.append("plugin description must be a non-empty string")
    if plugin.get("skills") != "./skills/":
        errors.append("plugin skills path must be './skills/'")
    if plugin.get("mcpServers") != "./.mcp.json":
        errors.append("plugin mcpServers path must be './.mcp.json'")
    if plugin.get("repository") != "https://github.com/AgentBigBrain/Tedd":
        errors.append("plugin repository URL is incorrect")
    if plugin.get("homepage") != "https://github.com/AgentBigBrain/Tedd#readme":
        errors.append("plugin homepage URL is incorrect")
    if plugin.get("license") != "Apache-2.0":
        errors.append("plugin license must be Apache-2.0")

    author = plugin.get("author")
    if not isinstance(author, dict):
        errors.append("plugin author must be an object")
    else:
        if author.get("name") != "AgentBigBrain":
            errors.append("plugin author name is incorrect")
        if "email" in author:
            errors.append("optional personal author email must not be packaged")
        if not str(author.get("url", "")).startswith("https://"):
            errors.append("plugin author URL must use HTTPS")
    keywords = plugin.get("keywords")
    if (
        not isinstance(keywords, list)
        or not keywords
        or not all(isinstance(item, str) and item.strip() for item in keywords)
    ):
        errors.append("plugin keywords must be a non-empty string array")

    interface = plugin.get("interface")
    required_interface_fields = {
        "capabilities",
        "category",
        "composerIcon",
        "defaultPrompt",
        "developerName",
        "displayName",
        "logo",
        "logoDark",
        "longDescription",
        "shortDescription",
    }
    if not isinstance(interface, dict):
        errors.append("plugin interface must be an object")
    else:
        missing_interface = sorted(required_interface_fields - set(interface))
        if missing_interface:
            errors.append(f"plugin interface is missing fields: {missing_interface}")
        for key in (
            "category",
            "developerName",
            "displayName",
            "longDescription",
            "shortDescription",
        ):
            if not isinstance(interface.get(key), str) or not interface[key].strip():
                errors.append(f"plugin interface.{key} must be a non-empty string")
        capabilities = interface.get("capabilities")
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) and item.strip() for item in capabilities
        ):
            errors.append("plugin interface.capabilities must be a string array")
        prompts = interface.get("defaultPrompt")
        if not isinstance(prompts, list) or not 1 <= len(prompts) <= 3:
            errors.append("plugin interface.defaultPrompt must contain one to three prompts")
        elif not all(
            isinstance(prompt, str) and prompt.strip() and len(prompt) <= 128 for prompt in prompts
        ):
            errors.append("plugin starter prompts must be non-empty and at most 128 characters")
        errors.extend(plugin_icon_reference_errors(interface))

    errors.extend(plugin_icon_file_errors(PLUGIN_ICON))
    errors.extend(mcp_packaging_errors())
    errors.extend(
        portable_plugin_errors(
            PLUGIN_ROOT,
            plugin,
            expected_skills=PACKAGED_SKILLS,
        )
    )
    errors.extend(portable_mcp_errors(PLUGIN_ROOT))
    errors.extend(claude_manifest_errors(plugin))

    entries = marketplace.get("plugins")
    if marketplace.get("name") != "tedd":
        errors.append("marketplace name must be 'tedd'")
    if not isinstance(entries, list) or len(entries) != 1:
        errors.append("marketplace must contain exactly one plugin entry")
    else:
        entry = entries[0]
        source = entry.get("source", {})
        if entry.get("name") != "tedd":
            errors.append("marketplace plugin name must be 'tedd'")
        if source.get("source") != "local" or source.get("path") != "./plugins/tedd":
            errors.append("marketplace plugin source must be ./plugins/tedd")
        policy = entry.get("policy")
        if not isinstance(policy, dict):
            errors.append("marketplace plugin policy must be an object")
        else:
            if policy.get("installation") not in {
                "AVAILABLE",
                "INSTALLED_BY_DEFAULT",
                "NOT_AVAILABLE",
            }:
                errors.append("marketplace installation policy is invalid")
            if policy.get("authentication") not in {"ON_INSTALL", "ON_USE"}:
                errors.append("marketplace authentication policy is invalid")
        if entry.get("category") != plugin.get("interface", {}).get("category"):
            errors.append("marketplace category must match plugin interface category")

    for skill_name in sorted(PACKAGED_SKILLS):
        skill_file = ROOT / f"plugins/tedd/skills/{skill_name}/SKILL.md"
        if not skill_file.is_file():
            errors.append(f"missing packaged skill: {skill_file.relative_to(ROOT)}")
        else:
            frontmatter = skill_file.read_text(encoding="utf-8").splitlines()[:8]
            if not frontmatter or frontmatter[0] != "---":
                errors.append(f"missing skill frontmatter: {skill_file.relative_to(ROOT)}")
            if f"name: {skill_name}" not in frontmatter:
                errors.append(f"skill name does not match directory: {skill_name}")

    for skill_name in sorted(PACKAGED_SKILLS):
        _, reference_errors = reachable_markdown(
            PLUGIN_ROOT / "skills" / skill_name / "SKILL.md", PLUGIN_ROOT
        )
        errors.extend(reference_errors)

    repository_skill = ROOT / ".agents/skills/python-development/SKILL.md"
    if not repository_skill.is_file():
        errors.append("missing repository-local Python development skill")
    else:
        frontmatter = repository_skill.read_text(encoding="utf-8").splitlines()[:8]
        if not frontmatter or frontmatter[0] != "---":
            errors.append("missing repository-local Python development skill frontmatter")
        if "name: python-development" not in frontmatter:
            errors.append("repository-local Python development skill name is incorrect")
        if not any(line.startswith("description: ") for line in frontmatter):
            errors.append("repository-local Python development skill description is missing")

    errors.extend(python_skill_contract_errors(repository_skill.parent))

    tedd_skill = ROOT / "plugins/tedd/skills/tedd/SKILL.md"
    if tedd_skill.is_file() and (
        "You are Tedd. Tedd is your public agent identity."
        not in tedd_skill.read_text(encoding="utf-8")
    ):
        errors.append("canonical Tedd skill must declare its public identity")

    errors.extend(
        orchestration_skill_contract_errors(
            ROOT / "plugins/tedd/skills/orchestrate-work",
            tedd_skill,
        )
    )

    alias_skill = ROOT / "plugins/tedd/skills/tedd-development/SKILL.md"
    alias_metadata = ROOT / "plugins/tedd/skills/tedd-development/agents/openai.yaml"
    if alias_skill.is_file() and "../tedd/SKILL.md" not in alias_skill.read_text(encoding="utf-8"):
        errors.append("tedd-development must remain a thin alias to ../tedd/SKILL.md")
    if alias_metadata.is_file() and not re.search(
        r"(?m)^\s*allow_implicit_invocation:\s*false\s*$",
        alias_metadata.read_text(encoding="utf-8"),
    ):
        errors.append("tedd-development compatibility alias must not invoke implicitly")

    for required in (
        "README.md",
        "SECURITY.md",
        "LICENSE",
        "CHANGELOG.md",
        "pyproject.toml",
        ".agents/skills/python-development/SKILL.md",
        ".agents/skills/python-development/references/UPSTREAM_COVERAGE.md",
        "docs/IDENTITY.md",
        "docs/MEMORY_RESEARCH.md",
        "docs/SCALE.md",
    ):
        if not (ROOT / required).is_file():
            errors.append(f"missing repository contract: {required}")
    return errors


def _mcp_entry_errors(path: Path, *, label: str, directory: bool) -> list[str]:
    """Require an MCP configuration path to be a contained physical package entry."""

    try:
        metadata = path.lstat()
    except OSError:
        return [f"{label} is missing or inaccessible"]
    errors: list[str] = []
    if stat.S_ISLNK(metadata.st_mode) or (
        getattr(metadata, "st_file_attributes", 0) & WINDOWS_REPARSE_POINT
    ):
        errors.append(f"{label} must not be a symlink or reparse point")
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_kind(metadata.st_mode):
        errors.append(f"{label} must be a {'directory' if directory else 'regular file'}")
    try:
        relative_path = path.relative_to(PLUGIN_ROOT)
    except ValueError:
        errors.append(f"{label} resolves outside the packaged plugin root")
    else:
        ancestor = PLUGIN_ROOT
        for component in relative_path.parts[:-1]:
            ancestor /= component
            try:
                ancestor_metadata = ancestor.lstat()
            except OSError:
                errors.append(f"{label} has an inaccessible configured-path ancestor")
                break
            if stat.S_ISLNK(ancestor_metadata.st_mode) or (
                getattr(ancestor_metadata, "st_file_attributes", 0) & WINDOWS_REPARSE_POINT
            ):
                errors.append(f"{label} has a symlink or reparse-point ancestor")
                break
    try:
        path.resolve(strict=True).relative_to(PLUGIN_ROOT.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        errors.append(f"{label} resolves outside the packaged plugin root")
    return errors


def _unique_mcp_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate MCP keys so a host and package gate see the same server."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("MCP config contains a duplicate key")
        result[key] = value
    return result


def _reject_mcp_json_constant(_raw: str) -> None:
    """Reject non-finite values instead of accepting implementation-specific JSON."""

    raise ValueError("MCP config contains a non-finite number")


def mcp_packaging_errors() -> list[str]:
    """Validate Tedd's deliberately narrow, local-only optional MCP declaration."""

    errors = _mcp_entry_errors(MCP_CONFIG, label="MCP config", directory=False)
    if errors:
        return errors
    try:
        payload = MCP_CONFIG.read_bytes()
    except OSError as exc:
        return [f"MCP config parse failure: {exc}"]
    if len(payload) > MAX_MCP_CONFIG_BYTES:
        return ["MCP config exceeds the byte limit"]
    try:
        document = json.loads(
            payload,
            object_pairs_hook=_unique_mcp_json_object,
            parse_constant=_reject_mcp_json_constant,
        )
    except (ValueError, json.JSONDecodeError, RecursionError, UnicodeDecodeError) as exc:
        return [f"MCP config parse failure: {exc}"]
    expected = {
        "mcpServers": {
            "tedd-graph": {
                "command": "python3",
                "args": ["./skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py"],
                "cwd": ".",
            }
        }
    }
    if document != expected:
        return ["MCP config must contain exactly the bundled local tedd-graph server"]
    launcher = PLUGIN_ROOT / "skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py"
    errors.extend(_mcp_entry_errors(launcher, label="MCP launcher", directory=False))
    return errors


def mcp_launcher_availability() -> str | None:
    """Describe the optional packaged launcher limitation without invalidating Tedd."""

    launcher = shutil.which("python3")
    if launcher is None:
        return "Optional MCP unavailable: python3 launcher not found"
    unavailable = "Optional MCP unavailable: python3 is not a working Python 3.10+ interpreter"
    # Windows may resolve the Store app-execution alias rather than an interpreter.
    # Isolate the probe from user-site packages and the current working directory.
    try:
        probe = subprocess.run(
            [
                launcher,
                "-I",
                "-S",
                "-B",
                "-c",
                "import sys; sys.exit(1) if sys.version_info < (3, 10) else "
                "print('tedd-python-3.10+')",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return unavailable
    if probe.returncode != 0 or probe.stdout.strip() != b"tedd-python-3.10+":
        return unavailable
    return None


def main() -> int:
    errors: list[str] = []
    for path in repository_files():
        try:
            payload = path.read_bytes()
        except OSError as exc:
            errors.append(f"could not read {path.relative_to(ROOT)}: {exc}")
            continue
        errors.extend(artifact_errors(path, payload))
    errors.extend(manifest_errors())

    if errors:
        print("Repository boundary check failed:", file=sys.stderr)
        for error in sorted(set(errors)):
            print(f"- {error}", file=sys.stderr)
        return 1
    availability = mcp_launcher_availability()
    if availability is not None:
        print(availability)
    print("Repository boundary check passed: infrastructure-only tree verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
