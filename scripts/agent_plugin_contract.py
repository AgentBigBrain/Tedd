#!/usr/bin/env python3
"""Validate Tedd's portable Agent Plugins manifest and fixed skill surface."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Final, Mapping, NoReturn

PORTABLE_PLUGIN_SCHEMA: Final = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
PORTABLE_MCP_SCHEMA: Final = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
PORTABLE_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "$schema",
        "author",
        "description",
        "extensions",
        "homepage",
        "keywords",
        "license",
        "name",
        "repository",
        "version",
    }
)
PORTABLE_METADATA_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
)
PORTABLE_AUTHOR_FIELDS: Final[frozenset[str]] = frozenset({"name", "email", "url"})
PORTABLE_NAME = re.compile(r"^(?!.*(?:--|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
MAX_PORTABLE_MANIFEST_BYTES: Final = 64 * 1024
MAX_PORTABLE_MCP_BYTES: Final = 64 * 1024
WINDOWS_REPARSE_POINT: Final = 0x400


class PortableManifestError(ValueError):
    """The portable manifest violates the bounded JSON input contract."""


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PortableManifestError("portable plugin manifest contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_raw: str) -> NoReturn:
    raise PortableManifestError("portable plugin manifest contains a non-finite number")


def _filesystem_entry_errors(
    path: Path,
    *,
    label: str,
    plugin_root: Path,
    directory: bool,
) -> list[str]:
    """Require one fixed package entry to be physical, contained, and correctly typed."""

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
        kind = "directory" if directory else "regular file"
        errors.append(f"{label} must be a {kind}")

    try:
        relative_path = path.relative_to(plugin_root)
    except ValueError:
        errors.append(f"{label} resolves outside the portable plugin root")
    else:
        ancestor = plugin_root
        for component in relative_path.parts[:-1]:
            ancestor /= component
            try:
                ancestor_metadata = ancestor.lstat()
            except OSError:
                errors.append(f"{label} has an inaccessible package-path ancestor")
                break
            if stat.S_ISLNK(ancestor_metadata.st_mode) or (
                getattr(ancestor_metadata, "st_file_attributes", 0) & WINDOWS_REPARSE_POINT
            ):
                errors.append(f"{label} has a symlink or reparse-point ancestor")
                break

    try:
        resolved_root = plugin_root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        errors.append(f"{label} could not be resolved")
    else:
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            errors.append(f"{label} resolves outside the portable plugin root")
    return errors


def _load_portable_manifest(path: Path) -> tuple[dict[str, object] | None, list[str]]:
    try:
        payload = path.read_bytes()
    except OSError:
        return None, ["portable plugin manifest could not be read"]
    if len(payload) > MAX_PORTABLE_MANIFEST_BYTES:
        return None, ["portable plugin manifest exceeds the byte limit"]

    try:
        document: object = json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (
        json.JSONDecodeError,
        PortableManifestError,
        RecursionError,
        UnicodeDecodeError,
    ) as exc:
        return None, [f"portable plugin manifest is not valid bounded JSON: {exc}"]
    if not isinstance(document, dict):
        return None, ["portable plugin manifest must be a JSON object"]
    return document, []


def _load_portable_mcp(path: Path) -> tuple[dict[str, object] | None, list[str]]:
    try:
        payload = path.read_bytes()
    except OSError:
        return None, ["portable MCP manifest could not be read"]
    if len(payload) > MAX_PORTABLE_MCP_BYTES:
        return None, ["portable MCP manifest exceeds the byte limit"]

    try:
        document: object = json.loads(
            payload,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (
        json.JSONDecodeError,
        PortableManifestError,
        RecursionError,
        UnicodeDecodeError,
    ) as exc:
        return None, [f"portable MCP manifest is not valid bounded JSON: {exc}"]
    if not isinstance(document, dict):
        return None, ["portable MCP manifest must be a JSON object"]
    return document, []


def portable_mcp_shape_errors(document: object) -> list[str]:
    """Validate Tedd's stdio document shape, not permission to execute its command.

    This is the locally supported subset, not a general Agent Plugins schema
    validator. The complete package check must also enforce launch approval
    and physical containment.
    """

    if not isinstance(document, dict):
        return ["portable MCP manifest must be a JSON object"]

    errors: list[str] = []
    unknown_fields = sorted(set(document) - {"$schema", "mcpServers"})
    if unknown_fields:
        errors.append(f"unsupported portable MCP fields: {unknown_fields}")
    if document.get("$schema") != PORTABLE_MCP_SCHEMA:
        errors.append(f"portable MCP $schema must be '{PORTABLE_MCP_SCHEMA}'")

    servers = document.get("mcpServers")
    if not isinstance(servers, dict):
        return [*errors, "portable MCP mcpServers must be an object"]
    if set(servers) != {"tedd-graph"}:
        errors.append("portable MCP must contain exactly the bundled local tedd-graph server")
        return errors

    server = servers["tedd-graph"]
    if not isinstance(server, dict):
        return [*errors, "portable MCP tedd-graph server must be an object"]
    unknown_server_fields = sorted(set(server) - {"type", "command", "args", "cwd"})
    if unknown_server_fields:
        errors.append(f"unsupported portable MCP server fields: {unknown_server_fields}")
    if server.get("type") != "stdio":
        errors.append("portable MCP server type must be 'stdio'")
    for field in ("command", "cwd"):
        value = server.get(field)
        if not isinstance(value, str) or not value or "\x00" in value:
            errors.append(f"portable MCP server {field} must be a nonempty NUL-free string")
    args = server.get("args")
    if (
        not isinstance(args, list)
        or not args
        or not all(
            isinstance(argument, str) and argument and "\x00" not in argument for argument in args
        )
    ):
        errors.append("portable MCP server args must be a nonempty array of NUL-free strings")
    return errors


def _portable_mcp_launch_policy_errors(server: Mapping[str, object]) -> list[str]:
    """Allow only Tedd's reviewed launch profile; new profiles require qualification."""

    errors: list[str] = []
    if server.get("command") != "python3":
        errors.append("portable MCP server command must be 'python3'")
    if server.get("args") != ["./skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py"]:
        errors.append("portable MCP server args must be the bundled graph launcher path")
    if server.get("cwd") != "./":
        errors.append("portable MCP server cwd must be './'")
    return errors


def portable_mcp_document_errors(document: object) -> list[str]:
    """Combine independent shape validation and Tedd's closed launch approval."""

    errors = portable_mcp_shape_errors(document)
    if errors:
        return errors
    # Shape validation proves these objects; keep the narrowing explicit for mypy.
    assert isinstance(document, dict)
    servers = document["mcpServers"]
    assert isinstance(servers, dict)
    server = servers["tedd-graph"]
    assert isinstance(server, dict)
    return _portable_mcp_launch_policy_errors(server)


def portable_mcp_errors(plugin_root: Path) -> list[str]:
    """Validate the physical root MCP component without fetching its schema."""

    mcp_path = plugin_root / "mcp.json"
    errors = _filesystem_entry_errors(
        mcp_path,
        label="portable MCP manifest",
        plugin_root=plugin_root,
        directory=False,
    )
    if errors:
        return errors
    document, load_errors = _load_portable_mcp(mcp_path)
    errors.extend(load_errors)
    if document is not None:
        errors.extend(portable_mcp_document_errors(document))
    launcher = plugin_root / "skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py"
    errors.extend(
        _filesystem_entry_errors(
            launcher,
            label="portable MCP launcher",
            plugin_root=plugin_root,
            directory=False,
        )
    )
    return errors


def portable_manifest_document_errors(
    document: object,
    codex_manifest: Mapping[str, object],
    *,
    expected_name: str,
) -> list[str]:
    """Validate the closed Agent Plugins v1 manifest and shared metadata parity."""

    if not isinstance(document, dict):
        return ["portable plugin manifest must be a JSON object"]

    errors: list[str] = []
    unknown_fields = sorted(set(document) - PORTABLE_MANIFEST_FIELDS)
    if unknown_fields:
        errors.append(f"unsupported portable plugin manifest fields: {unknown_fields}")

    if document.get("$schema") != PORTABLE_PLUGIN_SCHEMA:
        errors.append(f"portable plugin $schema must be '{PORTABLE_PLUGIN_SCHEMA}'")

    name = document.get("name")
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not PORTABLE_NAME.fullmatch(name):
        errors.append("portable plugin name violates the Agent Plugins v1 name constraints")
    elif name != expected_name:
        errors.append(f"portable plugin name must match its '{expected_name}' directory")

    for field in ("version", "description", "homepage", "repository", "license"):
        value = document.get(field)
        if value is not None and not isinstance(value, str):
            errors.append(f"portable plugin {field} must be a string")

    author = document.get("author")
    if author is not None:
        if not isinstance(author, dict):
            errors.append("portable plugin author must be an object")
        else:
            unknown_author_fields = sorted(set(author) - PORTABLE_AUTHOR_FIELDS)
            if unknown_author_fields:
                errors.append(f"unsupported portable plugin author fields: {unknown_author_fields}")
            for key, value in author.items():
                if key in PORTABLE_AUTHOR_FIELDS and not isinstance(value, str):
                    errors.append(f"portable plugin author.{key} must be a string")

    keywords = document.get("keywords")
    if keywords is not None and (
        not isinstance(keywords, list) or not all(isinstance(item, str) for item in keywords)
    ):
        errors.append("portable plugin keywords must be a string array")

    extensions = document.get("extensions")
    if extensions is not None and (
        not isinstance(extensions, dict)
        or not all(isinstance(value, dict) for value in extensions.values())
    ):
        errors.append("portable plugin extensions must map namespaces to objects")

    for field in PORTABLE_METADATA_FIELDS:
        if field not in document:
            errors.append(f"portable plugin metadata is missing shared field: {field}")
        elif document[field] != codex_manifest.get(field):
            errors.append(f"portable plugin {field} must match the Codex manifest")
    return errors


def portable_component_errors(
    plugin_root: Path,
    *,
    expected_skills: frozenset[str],
) -> list[str]:
    """Validate the fixed Agent Plugins skill discovery paths and containment."""

    errors = _filesystem_entry_errors(
        plugin_root,
        label="portable plugin root",
        plugin_root=plugin_root,
        directory=True,
    )
    skills_root = plugin_root / "skills"
    skills_root_errors = _filesystem_entry_errors(
        skills_root,
        label="portable skills root",
        plugin_root=plugin_root,
        directory=True,
    )
    errors.extend(skills_root_errors)
    if skills_root_errors:
        return errors

    try:
        entries = sorted(skills_root.iterdir(), key=lambda path: path.name)
    except OSError:
        return [*errors, "portable skills root could not be inventoried"]

    actual_entries = {entry.name for entry in entries}
    for missing in sorted(expected_skills - actual_entries):
        errors.append(f"missing portable skill directory: {missing}")
    for unexpected in sorted(actual_entries - expected_skills):
        errors.append(f"unexpected portable skills-root entry: {unexpected}")

    for skill_name in sorted(expected_skills & actual_entries):
        skill_root = skills_root / skill_name
        skill_root_errors = _filesystem_entry_errors(
            skill_root,
            label=f"portable skill directory '{skill_name}'",
            plugin_root=plugin_root,
            directory=True,
        )
        errors.extend(skill_root_errors)
        if skill_root_errors:
            continue
        errors.extend(
            _filesystem_entry_errors(
                skill_root / "SKILL.md",
                label=f"portable skill '{skill_name}' SKILL.md",
                plugin_root=plugin_root,
                directory=False,
            )
        )
    return errors


def portable_plugin_errors(
    plugin_root: Path,
    codex_manifest: Mapping[str, object],
    *,
    expected_skills: frozenset[str],
) -> list[str]:
    """Validate Tedd's complete portable Agent Plugins package surface."""

    manifest_path = plugin_root / "plugin.json"
    errors = _filesystem_entry_errors(
        manifest_path,
        label="portable plugin manifest",
        plugin_root=plugin_root,
        directory=False,
    )
    if not errors:
        document, load_errors = _load_portable_manifest(manifest_path)
        errors.extend(load_errors)
        if document is not None:
            errors.extend(
                portable_manifest_document_errors(
                    document,
                    codex_manifest,
                    expected_name=plugin_root.name,
                )
            )
    errors.extend(portable_component_errors(plugin_root, expected_skills=expected_skills))
    return errors
