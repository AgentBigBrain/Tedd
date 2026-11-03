#!/usr/bin/env python3
"""Focused tests for Tedd's repository and plugin-asset boundary."""

from __future__ import annotations

import ast
import copy
import json
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_plugin_contract as portable  # noqa: E402
import check_repository_boundary as boundary  # noqa: E402

from tests.support.paths import REPOSITORY_ROOT as ROOT


class McpLauncherAvailabilityTests(unittest.TestCase):
    """A PATH entry alone must not qualify a working Python runtime."""

    def test_missing_launcher_is_reported_without_execution(self) -> None:
        with (
            mock.patch.object(boundary.shutil, "which", return_value=None),
            mock.patch.object(boundary.subprocess, "run") as run,
        ):
            self.assertEqual(
                "Optional MCP unavailable: python3 launcher not found",
                boundary.mcp_launcher_availability(),
            )
        run.assert_not_called()

    def test_real_supported_interpreter_is_available(self) -> None:
        with mock.patch.object(boundary.shutil, "which", return_value=sys.executable):
            self.assertIsNone(boundary.mcp_launcher_availability())

    def test_store_alias_bad_output_and_old_python_are_unavailable(self) -> None:
        cases = (
            ("store-alias", 9009, b""),
            ("old-python", 1, b""),
            ("unrecognized-output", 0, b"not a Python probe"),
            ("empty-output", 0, b""),
        )
        for label, exit_code, output in cases:
            with (
                self.subTest(label=label),
                mock.patch.object(boundary.shutil, "which", return_value=sys.executable),
                mock.patch.object(
                    boundary.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], exit_code, output),
                ),
            ):
                self.assertEqual(
                    "Optional MCP unavailable: python3 is not a working Python 3.10+ interpreter",
                    boundary.mcp_launcher_availability(),
                )

    def test_failed_or_timed_out_probe_is_content_free(self) -> None:
        for error in (PermissionError("private path"), subprocess.TimeoutExpired("private", 5)):
            with (
                self.subTest(error=type(error).__name__),
                mock.patch.object(boundary.shutil, "which", return_value=sys.executable),
                mock.patch.object(boundary.subprocess, "run", side_effect=error),
            ):
                self.assertEqual(
                    "Optional MCP unavailable: python3 is not a working Python 3.10+ interpreter",
                    boundary.mcp_launcher_availability(),
                )

    def test_probe_has_no_shell_user_imports_or_inherited_stdin(self) -> None:
        with (
            mock.patch.object(boundary.shutil, "which", return_value=sys.executable),
            mock.patch.object(
                boundary.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, b"tedd-python-3.10+\n"),
            ) as run,
        ):
            self.assertIsNone(boundary.mcp_launcher_availability())
        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual([sys.executable, "-I", "-S", "-B", "-c"], command[:-1])
        options = run.call_args.kwargs
        self.assertFalse(options.get("shell", False))
        self.assertEqual(subprocess.DEVNULL, options["stdin"])
        self.assertEqual(subprocess.DEVNULL, options["stderr"])
        self.assertLessEqual(options["timeout"], 5)


class ContributorScratchBoundaryTests(unittest.TestCase):
    def test_private_source_archive_markers_are_rejected(self) -> None:
        for name in ("archive.json", "scope.json", "backup.json"):
            with self.subTest(name=name):
                errors = boundary.artifact_errors(ROOT / "sources" / name, b"{}")
                self.assertTrue(errors)

    def test_superpowers_notes_are_rejected_even_if_force_added(self) -> None:
        """Ignore rules must not be the only barrier to publishing local notes."""

        for directory in ("docs/superpowers", ".superpowers", "superpowers", "docs/Superpowers"):
            with self.subTest(directory=directory):
                errors = boundary.artifact_errors(
                    ROOT / directory / "plans/example.md", b"# Synthetic planning note\n"
                )
                self.assertTrue(any("contributor scratch directory" in error for error in errors))

    def test_published_documentation_is_not_treated_as_scratch(self) -> None:
        self.assertEqual(
            [], boundary.artifact_errors(ROOT / "docs/RELEASING.md", b"# Release instructions\n")
        )


class RepositoryBoundaryTests(unittest.TestCase):
    def test_inventory_permission_error_does_not_become_an_empty_pass(self) -> None:
        with (
            mock.patch.object(
                boundary.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, b"blocked.md\x00"),
            ),
            mock.patch.object(Path, "lstat", side_effect=PermissionError("synthetic denied")),
        ):
            with self.assertRaises(PermissionError):
                boundary.repository_files()

    def test_repository_inventory_omits_removed_paths_but_keeps_forced_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "new.md").write_text("# Renamed\n", encoding="utf-8")
            scratch = root / ".superpowers/notes.md"
            scratch.parent.mkdir()
            scratch.write_text("synthetic scratch\n", encoding="utf-8")
            with (
                mock.patch.object(boundary, "ROOT", root),
                mock.patch.object(
                    boundary.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, b"old.md\x00new.md\x00.superpowers/notes.md\x00"
                    ),
                ),
            ):
                paths = boundary.repository_files()
                self.assertEqual([scratch, root / "new.md"], paths)
                self.assertIn(
                    f"forbidden contributor scratch directory: {scratch.relative_to(root)}",
                    boundary.artifact_errors(scratch, scratch.read_bytes()),
                )

    def setUp(self) -> None:
        self.codex_manifest: dict[str, object] = json.loads(
            boundary.PLUGIN_MANIFEST.read_text(encoding="utf-8")
        )
        self.portable_manifest: dict[str, object] = json.loads(
            boundary.PORTABLE_PLUGIN_MANIFEST.read_text(encoding="utf-8")
        )
        self.portable_mcp: dict[str, object] = {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": {
                "tedd-graph": {
                    "type": "stdio",
                    "command": "python3",
                    "args": ["./skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py"],
                    "cwd": "./",
                }
            },
        }

    def write_portable_plugin(
        self,
        root: Path,
        *,
        document: object | None = None,
    ) -> None:
        root.mkdir(parents=True)
        for skill_name in boundary.PACKAGED_SKILLS:
            skill_root = root / "skills" / skill_name
            skill_root.mkdir(parents=True)
            (skill_root / "SKILL.md").write_text(
                f"---\nname: {skill_name}\ndescription: Synthetic test skill.\n---\n",
                encoding="utf-8",
            )
        (root / "plugin.json").write_text(
            json.dumps(self.portable_manifest if document is None else document),
            encoding="utf-8",
        )
        (root / "mcp.json").write_text(json.dumps(self.portable_mcp), encoding="utf-8")
        launcher = root / "skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("# synthetic launcher\n", encoding="utf-8")

    def test_checked_in_portable_mcp_is_a_single_local_stdio_server(self) -> None:
        """Catch a portable package that loses its closed graph-read server contract."""

        self.assertEqual([], portable.portable_mcp_errors(ROOT / "plugins/tedd"))

    def test_portable_mcp_validates_locally_without_fetching_its_schema(self) -> None:
        """Catch a package check that makes validation depend on schema-network availability."""

        with mock.patch.object(socket, "create_connection", side_effect=AssertionError):
            self.assertEqual([], portable.portable_mcp_errors(ROOT / "plugins/tedd"))

    def test_portable_mcp_shape_does_not_grant_launcher_approval(self) -> None:
        """A well-formed future launcher still needs explicit Tedd policy approval."""

        document = copy.deepcopy(self.portable_mcp)
        document["mcpServers"]["tedd-graph"]["command"] = "python"  # type: ignore[index]

        self.assertEqual([], portable.portable_mcp_shape_errors(document))
        self.assertTrue(portable.portable_mcp_document_errors(document))

    def test_portable_mcp_shape_rejects_malformed_launch_fields(self) -> None:
        """Reject malformed argv before applying an approved launch profile."""

        for field, value in (
            ("command", None),
            ("command", ""),
            ("command", "python\x00"),
            ("args", "launcher.py"),
            ("args", []),
            ("args", [1]),
            ("args", ["launcher.py\x00"]),
            ("cwd", None),
            ("cwd", ""),
            ("cwd", "./\x00"),
        ):
            with self.subTest(field=field, value=value):
                document = copy.deepcopy(self.portable_mcp)
                document["mcpServers"]["tedd-graph"][field] = value  # type: ignore[index]

                self.assertTrue(portable.portable_mcp_shape_errors(document))
                self.assertTrue(portable.portable_mcp_document_errors(document))

    def test_existing_unapproved_launcher_cannot_pass_the_package_contract(self) -> None:
        """Existence and containment alone are not permission to run arbitrary code."""

        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary) / "tedd"
            self.write_portable_plugin(plugin_root)
            (plugin_root / "other.py").write_text("# synthetic alternative\n", encoding="utf-8")
            document = copy.deepcopy(self.portable_mcp)
            document["mcpServers"]["tedd-graph"]["args"] = ["./other.py"]  # type: ignore[index]
            (plugin_root / "mcp.json").write_text(json.dumps(document), encoding="utf-8")

            self.assertEqual([], portable.portable_mcp_shape_errors(document))
            self.assertTrue(portable.portable_mcp_errors(plugin_root))

    def test_portable_mcp_cwd_matches_native_contained_path_contract(self) -> None:
        """Catch accepting bare dot, which Codex rejects before starting the server."""

        for cwd, valid in ((".", False), ("./", True)):
            with self.subTest(cwd=cwd), tempfile.TemporaryDirectory() as temporary:
                plugin_root = Path(temporary) / "tedd"
                self.write_portable_plugin(plugin_root)
                document = copy.deepcopy(self.portable_mcp)
                document["mcpServers"]["tedd-graph"]["cwd"] = cwd  # type: ignore[index]
                (plugin_root / "mcp.json").write_text(json.dumps(document), encoding="utf-8")

                errors = portable.portable_mcp_errors(plugin_root)

                self.assertEqual(valid, not errors, errors)

    def test_portable_mcp_rejects_wrong_schema_transport_and_launcher_contract(self) -> None:
        """Catch a portable declaration that changes the local stdio launch boundary."""

        defects: tuple[tuple[str, object, str], ...] = (
            ("$schema", "https://example.invalid/mcp.json", "portable MCP $schema must be"),
            ("type", "sse", "portable MCP server type must be 'stdio'"),
            ("command", "python", "portable MCP server command must be 'python3'"),
            ("args", ["../outside.py"], "portable MCP server args must be"),
            ("cwd", "..", "portable MCP server cwd must be './'"),
        )
        for field, value, expected_error in defects:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                plugin_root = Path(temporary) / "tedd"
                self.write_portable_plugin(plugin_root)
                document = copy.deepcopy(self.portable_mcp)
                if field == "$schema":
                    document[field] = value
                else:
                    document["mcpServers"]["tedd-graph"][field] = value  # type: ignore[index]
                (plugin_root / "mcp.json").write_text(json.dumps(document), encoding="utf-8")

                errors = portable.portable_mcp_errors(plugin_root)

                self.assertTrue(any(expected_error in error for error in errors), errors)

    def test_portable_mcp_rejects_unknown_and_duplicate_fields(self) -> None:
        """Catch ambiguous portable JSON that a host could parse differently."""

        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary) / "tedd"
            self.write_portable_plugin(plugin_root)
            mcp_path = plugin_root / "mcp.json"
            mcp_path.write_text(
                '{"$schema":"https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",'
                '"$schema":"https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",'
                '"mcpServers":{}}',
                encoding="utf-8",
            )

            duplicate_errors = portable.portable_mcp_errors(plugin_root)

            self.assertTrue(
                any("not valid bounded JSON" in error for error in duplicate_errors),
                duplicate_errors,
            )

            document = copy.deepcopy(self.portable_mcp)
            document["unexpected"] = True
            mcp_path.write_text(json.dumps(document), encoding="utf-8")

            unknown_errors = portable.portable_mcp_errors(plugin_root)

        self.assertIn("unsupported portable MCP fields: ['unexpected']", unknown_errors)

    def test_portable_mcp_rejects_path_escape_and_symlinked_launcher(self) -> None:
        """Catch a portable launcher that reaches outside a physical package entry."""

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            plugin_root = temporary_root / "tedd"
            self.write_portable_plugin(plugin_root)
            launcher = plugin_root / "skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py"
            external = temporary_root / "external.py"
            external.write_text("# synthetic external launcher\n", encoding="utf-8")
            launcher.unlink()
            try:
                launcher.symlink_to(external)
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")

            errors = portable.portable_mcp_errors(plugin_root)

        self.assertIn("portable MCP launcher must not be a symlink or reparse point", errors)
        self.assertIn("portable MCP launcher resolves outside the portable plugin root", errors)

    def test_portable_mcp_reparse_point_is_rejected(self) -> None:
        """Catch a Windows package entry that redirects portable MCP loading."""

        mcp_path = ROOT / "plugins/tedd/mcp.json"
        metadata = mock.Mock(
            st_mode=stat.S_IFREG,
            st_file_attributes=portable.WINDOWS_REPARSE_POINT,
        )
        with mock.patch.object(Path, "lstat", return_value=metadata):
            errors = portable._filesystem_entry_errors(
                mcp_path,
                label="portable MCP manifest",
                plugin_root=ROOT / "plugins/tedd",
                directory=False,
            )

        self.assertIn("portable MCP manifest must not be a symlink or reparse point", errors)

    def test_missing_portable_mcp_does_not_invalidate_portable_skills(self) -> None:
        """Catch an optional-MCP absence that is incorrectly treated as a skill failure."""

        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary) / "tedd"
            self.write_portable_plugin(plugin_root)
            (plugin_root / "mcp.json").unlink()

            component_errors = portable.portable_component_errors(
                plugin_root,
                expected_skills=boundary.PACKAGED_SKILLS,
            )
            mcp_errors = portable.portable_mcp_errors(plugin_root)

        self.assertEqual([], component_errors)
        self.assertEqual(["portable MCP manifest is missing or inaccessible"], mcp_errors)

    def test_checked_in_manifest_and_icon_satisfy_contract(self) -> None:
        self.assertEqual(boundary.manifest_errors(), [])
        self.assertEqual(boundary.plugin_icon_file_errors(boundary.PLUGIN_ICON), [])

    def test_claude_package_contract_rejects_missing_or_changed_launch_boundary(self) -> None:
        original = json.loads(boundary.CLAUDE_PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plugin.json"
            with mock.patch.object(boundary, "CLAUDE_PLUGIN_MANIFEST", path):
                self.assertTrue(boundary.claude_manifest_errors(self.codex_manifest))
                for field, value in (
                    ("version", "0.0.0"),
                    ("hooks", {}),
                    ("mcpServers", {}),
                    ("userConfig", {}),
                ):
                    with self.subTest(field=field):
                        path.write_text(json.dumps({**original, field: value}), encoding="utf-8")
                        self.assertTrue(boundary.claude_manifest_errors(self.codex_manifest))
                path.write_text(json.dumps(original), encoding="utf-8")
                self.assertEqual([], boundary.claude_manifest_errors(self.codex_manifest))
                path.write_text('{"mcpServers":{},' + json.dumps(original)[1:], encoding="utf-8")
                self.assertTrue(boundary.claude_manifest_errors(self.codex_manifest))

    def test_mcp_config_rejects_duplicate_keys_and_link_escapes(self) -> None:
        """Catch an MCP config that can hide an unsafe host-visible launch target."""

        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary) / "tedd"
            shutil.copytree(ROOT / "plugins/tedd", plugin_root)
            config = plugin_root / ".mcp.json"
            launcher = plugin_root / "skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py"
            with (
                mock.patch.object(boundary, "PLUGIN_ROOT", plugin_root),
                mock.patch.object(boundary, "MCP_CONFIG", config),
            ):
                config.write_text(
                    '{"mcpServers":{},"mcpServers":{"tedd-graph":{"command":"python3",'
                    '"args":["./skills/obsidian-knowledge-graph/scripts/tedd_graph_mcp.py"],'
                    '"cwd":"."}}}',
                    encoding="utf-8",
                )
                self.assertIn(
                    "MCP config parse failure: MCP config contains a duplicate key",
                    boundary.mcp_packaging_errors(),
                )

                config.unlink()
                config.symlink_to(launcher)
                self.assertIn(
                    "MCP config must not be a symlink or reparse point",
                    boundary.mcp_packaging_errors(),
                )

    def test_mcp_config_rejects_oversize_and_deep_json_without_crashing(self) -> None:
        """Catch hostile configuration input that can terminate the repository gate."""

        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary) / "tedd"
            shutil.copytree(ROOT / "plugins/tedd", plugin_root)
            config = plugin_root / ".mcp.json"
            with (
                mock.patch.object(boundary, "PLUGIN_ROOT", plugin_root),
                mock.patch.object(boundary, "MCP_CONFIG", config),
            ):
                config.write_bytes(b" " * (64 * 1024 + 1))
                self.assertEqual(
                    ["MCP config exceeds the byte limit"], boundary.mcp_packaging_errors()
                )

                config.write_text('{"mcpServers":' + ("[" * 2_000) + ("]" * 2_000) + "}")
                errors = boundary.mcp_packaging_errors()
                self.assertEqual(1, len(errors))
                self.assertTrue(
                    errors[0].startswith("MCP config parse failure: ")
                    or errors
                    == ["MCP config must contain exactly the bundled local tedd-graph server"],
                    errors,
                )

    def test_mcp_config_reports_decoder_recursion_as_parse_failure(self) -> None:
        """Catch a decoder recursion failure that must not terminate the repository gate."""

        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary) / "tedd"
            shutil.copytree(ROOT / "plugins/tedd", plugin_root)
            config = plugin_root / ".mcp.json"
            with (
                mock.patch.object(boundary, "PLUGIN_ROOT", plugin_root),
                mock.patch.object(boundary, "MCP_CONFIG", config),
                mock.patch.object(
                    boundary.json,
                    "loads",
                    side_effect=RecursionError("synthetic decoder recursion"),
                ),
            ):
                self.assertEqual(
                    ["MCP config parse failure: synthetic decoder recursion"],
                    boundary.mcp_packaging_errors(),
                )

    def test_mcp_config_rejects_in_root_and_outside_launcher_ancestor_links(self) -> None:
        """Catch a physical launcher reached through a symlinked configured ancestor."""

        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            plugin_root = temporary_root / "tedd"
            shutil.copytree(ROOT / "plugins/tedd", plugin_root)
            config = plugin_root / ".mcp.json"
            scripts_root = plugin_root / "skills/obsidian-knowledge-graph/scripts"
            in_root_target = plugin_root / "scripts-real"
            scripts_root.rename(in_root_target)
            try:
                scripts_root.symlink_to(in_root_target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")
            with (
                mock.patch.object(boundary, "PLUGIN_ROOT", plugin_root),
                mock.patch.object(boundary, "MCP_CONFIG", config),
            ):
                self.assertIn(
                    "MCP launcher has a symlink or reparse-point ancestor",
                    boundary.mcp_packaging_errors(),
                )

            shutil.rmtree(plugin_root)
            shutil.copytree(ROOT / "plugins/tedd", plugin_root)
            config = plugin_root / ".mcp.json"
            scripts_root = plugin_root / "skills/obsidian-knowledge-graph/scripts"
            outside_target = temporary_root / "outside-scripts"
            scripts_root.rename(outside_target)
            try:
                scripts_root.symlink_to(outside_target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")
            with (
                mock.patch.object(boundary, "PLUGIN_ROOT", plugin_root),
                mock.patch.object(boundary, "MCP_CONFIG", config),
            ):
                self.assertIn(
                    "MCP launcher resolves outside the packaged plugin root",
                    boundary.mcp_packaging_errors(),
                )

    def test_mcp_config_rejects_windows_reparse_point_launcher_ancestor(self) -> None:
        """Catch a Windows-only configured ancestor escape without requiring symlink privileges."""

        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary) / "tedd"
            shutil.copytree(ROOT / "plugins/tedd", plugin_root)
            config = plugin_root / ".mcp.json"
            scripts_root = plugin_root / "skills/obsidian-knowledge-graph/scripts"
            original_lstat = Path.lstat

            def reparse_lstat(path: Path) -> object:
                metadata = original_lstat(path)
                if path == scripts_root:
                    return mock.Mock(
                        st_mode=metadata.st_mode,
                        st_file_attributes=boundary.WINDOWS_REPARSE_POINT,
                    )
                return metadata

            with (
                mock.patch.object(boundary, "PLUGIN_ROOT", plugin_root),
                mock.patch.object(boundary, "MCP_CONFIG", config),
                mock.patch.object(Path, "lstat", autospec=True, side_effect=reparse_lstat),
            ):
                self.assertIn(
                    "MCP launcher has a symlink or reparse-point ancestor",
                    boundary.mcp_packaging_errors(),
                )

    def test_checked_in_orchestration_skill_satisfies_contract(self) -> None:
        skill_root = ROOT / "plugins/tedd/skills/orchestrate-work"
        tedd_skill = ROOT / "plugins/tedd/skills/tedd/SKILL.md"

        self.assertEqual(boundary.orchestration_skill_contract_errors(skill_root, tedd_skill), [])

    def test_orchestration_contract_accepts_reorganized_core_guidance(self) -> None:
        """Packaging checks must not prescribe the core skill's prose or sequence."""
        skill_root = ROOT / "plugins/tedd/skills/orchestrate-work"
        with tempfile.TemporaryDirectory() as temporary:
            tedd_skill = Path(temporary) / "SKILL.md"
            tedd_skill.write_text(
                "---\nname: tedd\ndescription: Coordinate delegated work.\n---\n\n"
                "# Tedd\n\nYou are Tedd. Tedd is your public agent identity.\n\n"
                "## Complete the requested outcome\n\n"
                "Continue authorized work; keep privacy and verification intact.\n\n"
                "## Select relevant context and helpers\n\n"
                "Use [orchestration](../orchestrate-work/SKILL.md) for independent lanes.\n",
                encoding="utf-8",
            )

            errors = boundary.orchestration_skill_contract_errors(skill_root, tedd_skill)

        self.assertEqual(errors, [])

    def test_orchestration_contract_requires_canonical_handoff_fields(self) -> None:
        skill_root = ROOT / "plugins/tedd/skills/orchestrate-work"
        tedd_skill = ROOT / "plugins/tedd/skills/tedd/SKILL.md"
        with tempfile.TemporaryDirectory() as temporary:
            copied_root = Path(temporary) / "orchestrate-work"
            shutil.copytree(skill_root, copied_root)
            handoff = copied_root / "references/handoff-packet.md"
            handoff.write_text(
                "\n".join(
                    (
                        "# Handoff packet",
                        "",
                        "## Handoff packet template",
                        "",
                        "- objective:",
                        "- verified state:",
                        "- hypotheses:",
                        "- permissions:",
                        "- owned artifacts:",
                        "- current watermark and scope identifiers:",
                        "- output contract:",
                        "- leaf boundary:",
                        "",
                        "## Live-delta format",
                        "",
                        "- delta kind:",
                    )
                ),
                encoding="utf-8",
            )

            errors = boundary.orchestration_skill_contract_errors(copied_root, tedd_skill)

        self.assertIn("handoff packet must contain field: stop condition", errors)

    def test_orchestration_allows_equivalent_prose_and_section_names(self) -> None:
        skill_root = ROOT / "plugins/tedd/skills/orchestrate-work"
        tedd_skill = ROOT / "plugins/tedd/skills/tedd/SKILL.md"
        replacements = {
            "Assign lane ownership": "Assign accountable owners",
            "Prevent overlapping writers.": "Give intersecting artifacts one writer.",
            "Re-read the provider thread": "Refresh the provider conversation",
            "Never invent a clock ETA": "Use only evidenced timing",
            "Claim that a running deployment": "Report that an in-flight deployment",
            "exactly one channel liaison": "a single preparation liaison",
        }
        with tempfile.TemporaryDirectory() as temporary:
            copied_root = Path(temporary) / "orchestrate-work"
            shutil.copytree(skill_root, copied_root)
            for path in (copied_root / "SKILL.md", copied_root / "references/routing-policy.md"):
                content = path.read_text(encoding="utf-8")
                for old, new in replacements.items():
                    content = content.replace(old, new)
                path.write_text(content, encoding="utf-8")

            self.assertEqual(
                boundary.orchestration_skill_contract_errors(copied_root, tedd_skill), []
            )

    def test_orchestration_requires_real_canonical_links(self) -> None:
        """Plain mentions cannot substitute for reachable routing and packet links."""
        skill_root = ROOT / "plugins/tedd/skills/orchestrate-work"
        tedd_skill = ROOT / "plugins/tedd/skills/tedd/SKILL.md"
        cases = (
            ("SKILL.md", "../tedd/SKILL.md", "route through canonical Tedd"),
            ("SKILL.md", "references/routing-policy.md", "link its routing policy"),
            ("SKILL.md", "references/handoff-packet.md", "link the canonical handoff packet"),
            (
                "references/routing-policy.md",
                "handoff-packet.md",
                "link the canonical handoff packet",
            ),
        )
        for filename, target, message in cases:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                copied_root = Path(temporary) / "orchestrate-work"
                shutil.copytree(skill_root, copied_root)
                path = copied_root / filename
                content = path.read_text(encoding="utf-8")
                content = content.replace(f"]({target})", f"] `{target}`")
                path.write_text(content, encoding="utf-8")
                errors = boundary.orchestration_skill_contract_errors(copied_root, tedd_skill)
                self.assertTrue(any(message in error for error in errors), errors)

    def test_skill_references_allow_indirection_cycles_and_external_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entry = root / "SKILL.md"
            index = root / "index.md"
            procedure = root / "procedure.md"
            entry.write_text(
                "[Index](index.md) [Web](https://example.invalid/doc)", encoding="utf-8"
            )
            index.write_text("[Procedure](procedure.md#action)", encoding="utf-8")
            procedure.write_text("[Root](SKILL.md)\n## Action\n", encoding="utf-8")
            reachable, errors = boundary.reachable_markdown(entry, root)
            self.assertEqual({path.resolve() for path in (entry, index, procedure)}, reachable)
            self.assertEqual([], errors)

    def test_skill_references_reject_missing_or_unreadable_procedures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entry = root / "SKILL.md"
            entry.write_text("[Procedure](procedure.md)", encoding="utf-8")
            _, errors = boundary.reachable_markdown(entry, root)
            self.assertIn("missing skill reference: procedure.md", errors)
            (root / "procedure.md").write_bytes(b"\xff\xfe")
            _, errors = boundary.reachable_markdown(entry, root)
            self.assertIn("unreadable skill reference: procedure.md", errors)

    def test_skill_references_accept_optional_titles_and_outer_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entry = root / "SKILL.md"
            procedure = root / "procedure.md"
            procedure.write_text("## Action\n", encoding="utf-8")
            for destination in (
                'procedure.md "Review procedure"',
                'procedure.md#action "Review procedure"',
                ' <procedure.md> "Review procedure" ',
            ):
                with self.subTest(destination=destination):
                    entry.write_text(
                        f"[Procedure]({destination}) "
                        '[Web]( https://example.invalid/doc "External guide" )',
                        encoding="utf-8",
                    )
                    reachable, errors = boundary.reachable_markdown(entry, root)
                    self.assertEqual([], errors)
                    self.assertEqual({entry.resolve(), procedure.resolve()}, reachable)

    def test_orchestration_links_accept_optional_titles(self) -> None:
        skill_root = ROOT / "plugins/tedd/skills/orchestrate-work"
        with tempfile.TemporaryDirectory() as temporary:
            copied_root = Path(temporary) / "orchestrate-work"
            shutil.copytree(skill_root, copied_root)
            tedd_skill = Path(temporary) / "tedd.md"
            tedd_skill.write_text(
                '[Orchestrate](../orchestrate-work/SKILL.md "Delegate")', encoding="utf-8"
            )
            for filename in ("SKILL.md", "references/routing-policy.md"):
                path = copied_root / filename
                content = re.sub(
                    r"\]\(([^)]+\.md)\)", r']( \1 "Procedure" )', path.read_text(encoding="utf-8")
                )
                path.write_text(content, encoding="utf-8")
            self.assertEqual(
                [], boundary.orchestration_skill_contract_errors(copied_root, tedd_skill)
            )

    def test_skill_references_reject_encoded_package_escape_without_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            outside = Path(temporary) / "outside.md"
            outside.write_bytes(b"\xff\xfe")
            root = Path(temporary) / "package"
            root.mkdir()
            entry = root / "SKILL.md"
            entry.write_text("[Outside](%2e%2e/outside.md)", encoding="utf-8")
            reachable, errors = boundary.reachable_markdown(entry, root)
            self.assertEqual({entry.resolve()}, reachable)
            self.assertEqual(["skill reference leaves its package: SKILL.md"], errors)

    def test_orchestration_contract_requires_expanded_handoff_fields(self) -> None:
        skill_root = ROOT / "plugins/tedd/skills/orchestrate-work"
        tedd_skill = ROOT / "plugins/tedd/skills/tedd/SKILL.md"
        fields = (
            "relevant user instructions or messages",
            "task-specific constraints",
            "required durable records",
        )
        for field in fields:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                copied_root = Path(temporary) / "orchestrate-work"
                shutil.copytree(skill_root, copied_root)
                handoff = copied_root / "references/handoff-packet.md"
                handoff.write_text(
                    handoff.read_text(encoding="utf-8").replace(f"- {field}:\n", "", 1),
                    encoding="utf-8",
                )

                errors = boundary.orchestration_skill_contract_errors(copied_root, tedd_skill)

                self.assertIn(f"handoff packet must contain field: {field}", errors)

    def test_orchestration_contract_requires_expanded_live_delta_fields(self) -> None:
        skill_root = ROOT / "plugins/tedd/skills/orchestrate-work"
        tedd_skill = ROOT / "plugins/tedd/skills/tedd/SKILL.md"
        fields = ("status", "changed artifacts", "blockers or needs", "next action")
        for field in fields:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                copied_root = Path(temporary) / "orchestrate-work"
                shutil.copytree(skill_root, copied_root)
                handoff = copied_root / "references/handoff-packet.md"
                handoff.write_text(
                    handoff.read_text(encoding="utf-8").replace(f"{field}: ", "omitted: ", 1),
                    encoding="utf-8",
                )

                errors = boundary.orchestration_skill_contract_errors(copied_root, tedd_skill)

                self.assertIn(f"live-delta format must contain field: {field}", errors)

    def test_orchestration_contract_requires_exact_live_delta_status_vocabulary(self) -> None:
        skill_root = ROOT / "plugins/tedd/skills/orchestrate-work"
        tedd_skill = ROOT / "plugins/tedd/skills/tedd/SKILL.md"
        invalid_vocabularies = (
            "current | blocked | waiting | complete",
            "investigating | implementing | blocked | complete",
            "investigating | implementing | verifying | blocked | complete | waiting",
        )
        for vocabulary in invalid_vocabularies:
            with self.subTest(vocabulary=vocabulary), tempfile.TemporaryDirectory() as temporary:
                copied_root = Path(temporary) / "orchestrate-work"
                shutil.copytree(skill_root, copied_root)
                handoff = copied_root / "references/handoff-packet.md"
                handoff.write_text(
                    handoff.read_text(encoding="utf-8").replace(
                        "status: investigating | implementing | verifying | blocked | complete",
                        f"status: {vocabulary}",
                        1,
                    ),
                    encoding="utf-8",
                )

                errors = boundary.orchestration_skill_contract_errors(copied_root, tedd_skill)

                self.assertIn(
                    "live-delta status must use exactly: investigating | implementing | verifying | "
                    "blocked | complete",
                    errors,
                )

    def test_semantic_coverage_release_contract_is_documented(self) -> None:
        graph_root = ROOT / "plugins/tedd/skills/obsidian-knowledge-graph"
        schema = (graph_root / "references/capture-schema.md").read_text(encoding="utf-8")
        protocol = (graph_root / "references/multi-agent-protocol.md").read_text(encoding="utf-8")
        capture = (graph_root / "references/capture-and-correction.md").read_text(encoding="utf-8")
        capture_source = (graph_root / "scripts/tedd_knowledge_graph/legacy_capture.py").read_text(
            encoding="utf-8"
        )

        capture_module = ast.parse(capture_source)
        omission_assignment = next(
            node
            for node in capture_module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_COVERAGE_OMISSION_REASONS"
                for target in node.targets
            )
        )
        self.assertIsInstance(omission_assignment.value, ast.Call)
        omission_call = omission_assignment.value
        assert isinstance(omission_call, ast.Call)
        self.assertEqual(len(omission_call.args), 1)
        executable_omission_reasons = set(ast.literal_eval(omission_call.args[0]))

        expected_omission_reasons = {
            "unsupported_inference",
            "insufficient_authority",
            "transient",
            "no_actionable_owner_or_checkpoint",
            "out_of_scope",
            "privacy_or_security",
        }
        self.assertEqual(executable_omission_reasons, expected_omission_reasons)
        for document in (capture, schema):
            documented_tokens = set(re.findall(r"(?<!`)`([^`\r\n]+)`(?!`)", document))
            self.assertTrue(executable_omission_reasons <= documented_tokens)

        for category in (
            "identities_relationships_status",
            "verified_current_state",
            "conclusions_risks_unknowns",
            "recommendations_decisions",
            "obligations_next_actions",
            "artifacts_events_receipts",
        ):
            self.assertIn(category, schema)
        self.assertIn("`role: assistant`", schema)
        self.assertIn("`role: tool`", schema)
        self.assertIn("`substantial_analysis`", protocol)
        for token in (
            "existing",
            "omitted",
            "not_applicable",
            "record_id",
            "reason: no_material_outcome",
        ):
            self.assertIn(f"`{token}`", capture)
        reachable, errors = boundary.reachable_markdown(
            graph_root / "SKILL.md", ROOT / "plugins/tedd"
        )
        self.assertEqual([], errors)
        for name in ("capture-schema.md", "capture-and-correction.md", "multi-agent-protocol.md"):
            self.assertIn((graph_root / "references" / name).resolve(), reachable)

    def test_orchestration_contract_requires_implicit_tedd_routing(self) -> None:
        skill_root = ROOT / "plugins/tedd/skills/orchestrate-work"
        tedd_skill = ROOT / "plugins/tedd/skills/tedd/SKILL.md"
        with tempfile.TemporaryDirectory() as temporary:
            copied_root = Path(temporary) / "orchestrate-work"
            copied_tedd = Path(temporary) / "tedd-SKILL.md"
            shutil.copytree(skill_root, copied_root)
            copied_tedd.write_text(
                tedd_skill.read_text(encoding="utf-8").replace(
                    "../orchestrate-work/SKILL.md",
                    "missing-orchestration-skill.md",
                ),
                encoding="utf-8",
            )
            metadata = copied_root / "agents/openai.yaml"
            metadata.write_text(
                metadata.read_text(encoding="utf-8").replace(
                    "allow_implicit_invocation: true",
                    "allow_implicit_invocation: false",
                ),
                encoding="utf-8",
            )

            errors = boundary.orchestration_skill_contract_errors(copied_root, copied_tedd)

        self.assertIn(
            "orchestrate-work must allow implicit invocation for adaptive routing", errors
        )
        self.assertIn("canonical Tedd skill must link the orchestrate-work skill", errors)

    def test_portable_manifest_rejects_unknown_component_fields(self) -> None:
        for field in ("interface", "mcpServers", "skills"):
            with self.subTest(field=field):
                document = copy.deepcopy(self.portable_manifest)
                document[field] = object()

                errors = portable.portable_manifest_document_errors(
                    document,
                    self.codex_manifest,
                    expected_name="tedd",
                )

                self.assertIn(
                    f"unsupported portable plugin manifest fields: ['{field}']",
                    errors,
                )

    def test_portable_manifest_rejects_schema_type_and_parity_defects(self) -> None:
        defects: tuple[tuple[str, object, str], ...] = (
            (
                "$schema",
                "https://example.invalid/schema.json",
                "portable plugin $schema must be",
            ),
            ("name", "Tedd", "portable plugin name violates"),
            ("description", 7, "portable plugin description must be a string"),
            ("keywords", ["agent", 7], "portable plugin keywords must be a string array"),
            ("extensions", {"com.example": 7}, "portable plugin extensions must map"),
        )
        for field, value, expected_error in defects:
            with self.subTest(field=field):
                document = copy.deepcopy(self.portable_manifest)
                document[field] = value

                errors = portable.portable_manifest_document_errors(
                    document,
                    self.codex_manifest,
                    expected_name="tedd",
                )

                self.assertTrue(
                    any(expected_error in error for error in errors),
                    errors,
                )

        changed = copy.deepcopy(self.portable_manifest)
        changed["license"] = "MIT"
        self.assertIn(
            "portable plugin license must match the Codex manifest",
            portable.portable_manifest_document_errors(
                changed,
                self.codex_manifest,
                expected_name="tedd",
            ),
        )

    def test_portable_manifest_enforces_name_and_author_constraints(self) -> None:
        invalid_names = ("", "A", "-tedd", "tedd-", "te--dd", "te..dd", "tedd_name", "a" * 65)
        for name in invalid_names:
            with self.subTest(name=name):
                document = copy.deepcopy(self.portable_manifest)
                document["name"] = name
                errors = portable.portable_manifest_document_errors(
                    document,
                    self.codex_manifest,
                    expected_name="tedd",
                )
                self.assertTrue(
                    any("name violates" in error for error in errors),
                    errors,
                )

        for author in ({"company": "synthetic"}, {"name": 7}):
            with self.subTest(author=author):
                document = copy.deepcopy(self.portable_manifest)
                document["author"] = author
                errors = portable.portable_manifest_document_errors(
                    document,
                    self.codex_manifest,
                    expected_name="tedd",
                )
                self.assertTrue(
                    any("portable plugin author" in error for error in errors),
                    errors,
                )

    def test_portable_manifest_rejects_malformed_and_duplicate_json(self) -> None:
        malformed_documents = (b"{", b'{"name":"tedd","name":"other"}')
        for payload in malformed_documents:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                plugin_root = Path(temporary) / "tedd"
                self.write_portable_plugin(plugin_root)
                (plugin_root / "plugin.json").write_bytes(payload)

                errors = portable.portable_plugin_errors(
                    plugin_root,
                    self.codex_manifest,
                    expected_skills=boundary.PACKAGED_SKILLS,
                )

                self.assertTrue(
                    any("not valid bounded JSON" in error for error in errors),
                    errors,
                )

    def test_portable_skill_discovery_requires_exact_immediate_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary) / "tedd"
            self.write_portable_plugin(plugin_root)
            unexpected = plugin_root / "skills" / "nested-extra"
            unexpected.mkdir()
            (unexpected / "SKILL.md").write_text("synthetic", encoding="utf-8")
            missing = plugin_root / "skills" / "tedd-development"
            (missing / "SKILL.md").unlink()
            missing.rmdir()

            errors = portable.portable_component_errors(
                plugin_root,
                expected_skills=boundary.PACKAGED_SKILLS,
            )

        self.assertIn("missing portable skill directory: tedd-development", errors)
        self.assertIn("unexpected portable skills-root entry: nested-extra", errors)

    def test_portable_manifest_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            plugin_root = temporary_root / "tedd"
            self.write_portable_plugin(plugin_root)
            manifest = plugin_root / "plugin.json"
            target = temporary_root / "external.json"
            target.write_text(json.dumps(self.portable_manifest), encoding="utf-8")
            manifest.unlink()
            try:
                manifest.symlink_to(target)
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable: {exc}")

            errors = portable.portable_plugin_errors(
                plugin_root,
                self.codex_manifest,
                expected_skills=boundary.PACKAGED_SKILLS,
            )

        self.assertIn(
            "portable plugin manifest must not be a symlink or reparse point",
            errors,
        )

    def test_portable_manifest_reparse_point_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            plugin_root = Path(temporary) / "tedd"
            self.write_portable_plugin(plugin_root)
            manifest = plugin_root / "plugin.json"
            metadata = mock.Mock(
                st_mode=stat.S_IFREG,
                st_file_attributes=portable.WINDOWS_REPARSE_POINT,
            )
            with mock.patch.object(Path, "lstat", return_value=metadata):
                errors = portable._filesystem_entry_errors(
                    manifest,
                    label="portable plugin manifest",
                    plugin_root=plugin_root,
                    directory=False,
                )

        self.assertIn(
            "portable plugin manifest must not be a symlink or reparse point",
            errors,
        )

    def test_portable_package_entry_must_resolve_inside_plugin_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            plugin_root = temporary_root / "tedd"
            plugin_root.mkdir()
            external = temporary_root / "external.json"
            external.write_text("{}", encoding="utf-8")

            errors = portable._filesystem_entry_errors(
                external,
                label="portable test entry",
                plugin_root=plugin_root,
                directory=False,
            )

        self.assertIn(
            "portable test entry resolves outside the portable plugin root",
            errors,
        )

    def test_invalid_png_signature_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            icon = Path(temporary) / "icon.png"
            icon.write_bytes(b"not a PNG")

            errors = boundary.plugin_icon_file_errors(icon)

        self.assertEqual(errors, ["plugin icon asset must have a valid PNG signature"])

    def test_missing_icon_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            errors = boundary.plugin_icon_file_errors(Path(temporary) / "missing.png")

        self.assertEqual(errors, ["plugin icon asset is missing"])

    def test_only_exact_icon_path_is_an_approved_binary(self) -> None:
        valid_png = boundary.PNG_SIGNATURE + b"synthetic"

        approved_errors = boundary.artifact_errors(boundary.PLUGIN_ICON, valid_png)
        rogue_errors = boundary.artifact_errors(ROOT / "docs/rogue.png", valid_png)

        self.assertFalse(
            any("unapproved infrastructure file type" in error for error in approved_errors),
            approved_errors,
        )
        self.assertTrue(
            any(
                f"unapproved infrastructure file type: {Path('docs/rogue.png')}" in error
                for error in rogue_errors
            ),
            rogue_errors,
        )

    def test_only_reviewed_support_text_paths_are_admitted(self) -> None:
        for relative in (
            Path("scripts/native/source_av1_validate.c"),
            Path("tests/knowledge_graph/libavif-fixture-LICENSE.txt"),
            Path("docs/assets/deepwiki-badge.svg"),
        ):
            with self.subTest(path=relative):
                path = boundary.ROOT / relative
                self.assertEqual(boundary.artifact_errors(path, b"neutral source or license"), [])
                self.assertTrue(boundary.artifact_errors(path, b"SQLite format 3\x00"))
                self.assertTrue(boundary.artifact_errors(path, b"Bearer TOPSECRET_SYNTHETIC"))
        for relative in (
            Path("scripts/native/extra.c"),
            Path("tests/knowledge_graph/extra.txt"),
            Path("docs/source_av1_validate.c"),
            Path("docs/libavif-fixture-LICENSE.txt"),
            Path("docs/assets/extra-badge.svg"),
        ):
            with self.subTest(unapproved=relative):
                self.assertTrue(
                    any(
                        "unapproved infrastructure file type" in error
                        for error in boundary.artifact_errors(boundary.ROOT / relative, b"neutral")
                    )
                )

    def test_artifact_errors_rejects_developer_home_paths_in_contributor_markdown(self) -> None:
        """Catch contributor instructions that only work on one developer machine."""

        contributor_doc = ROOT / "docs/contributor-guide.md"
        errors = boundary.artifact_errors(
            contributor_doc,
            b"python3 /Users/" + b"developer/Tools/validate_plugin.py plugins/tedd\n",
        )

        self.assertTrue(
            any("developer-home path" in error for error in errors),
            errors,
        )

    def test_artifact_errors_rejects_bare_developer_home_path_boundaries(self) -> None:
        """Catch home paths that end at a command or ordinary prose boundary."""

        cases = (
            ("POSIX end", b"/Users/" + b"developer"),
            ("POSIX whitespace", b"/home/" + b"developer\n"),
            ("POSIX punctuation", b"/Users/" + b"developer,"),
            ("POSIX markup", b"/Users/" + b"developer>"),
            ("Windows end", b"C:\\Users\\" + b"developer"),
            ("Windows punctuation", b"C:\\Users\\" + b"developer)"),
            ("Windows pipe", b"C:\\Users\\" + b"developer|"),
        )

        for label, payload in cases:
            with self.subTest(label=label):
                errors = boundary.artifact_errors(ROOT / "docs/contributor-guide.md", payload)
                self.assertTrue(
                    any("developer-home path" in error for error in errors),
                    errors,
                )

    def test_artifact_errors_rejects_leading_underscore_developer_home_paths(self) -> None:
        """Catch private home paths whose username begins with an underscore."""

        cases = (
            ("POSIX bare", b"/home/" + b"_developer"),
            ("POSIX descendant", b"/home/" + b"_developer/tools/check.py"),
            ("Windows bare", b"C:\\Users\\" + b"_developer"),
            ("Windows descendant", b"C:\\Users\\" + b"_developer\\tools\\check.py"),
        )

        for label, payload in cases:
            with self.subTest(label=label):
                errors = boundary.artifact_errors(ROOT / "docs/contributor-guide.md", payload)
                self.assertTrue(
                    any("developer-home path" in error for error in errors),
                    errors,
                )

    def test_artifact_errors_allows_portable_and_explicitly_synthetic_path_controls(self) -> None:
        """Keep portable commands and clearly synthetic path fixtures usable."""

        controls = (
            (ROOT / "docs/contributor-guide.md", b"python3 ~/.codex/tools/check.py\n"),
            (ROOT / "docs/contributor-guide.md", b"python3 scripts/check.py\n"),
            (ROOT / "tests/test_path_examples.py", b"/Users/synthetic-user/fixture\n"),
            (
                ROOT / "tests/knowledge_graph/test_knowledge_graph.py",
                b'PATH = "/Users/alice/fixture"\n',
            ),
        )

        for path, payload in controls:
            with self.subTest(path=path, payload=payload):
                errors = boundary.artifact_errors(path, payload)
                self.assertFalse(
                    any("developer-home path" in error for error in errors),
                    errors,
                )

    def test_artifact_errors_rejects_non_synthetic_home_path_in_test_file(self) -> None:
        """Keep test fixtures from becoming a blanket developer-home exception."""

        errors = boundary.artifact_errors(
            ROOT / "tests/test_path_examples.py",
            b"/Users/" + b"developer/fixture\n",
        )

        self.assertTrue(
            any("developer-home path" in error for error in errors),
            errors,
        )

    def test_icon_references_must_all_use_exact_packaged_path(self) -> None:
        valid = {field: boundary.PLUGIN_ICON_REFERENCE for field in boundary.PLUGIN_ICON_FIELDS}
        self.assertEqual(boundary.plugin_icon_reference_errors(valid), [])

        for field in boundary.PLUGIN_ICON_FIELDS:
            with self.subTest(field=field):
                missing = valid.copy()
                del missing[field]
                self.assertEqual(
                    boundary.plugin_icon_reference_errors(missing),
                    [f"plugin interface.{field} must be '{boundary.PLUGIN_ICON_REFERENCE}'"],
                )

                wrong = valid.copy()
                wrong[field] = "./assets/other.png"
                self.assertEqual(
                    boundary.plugin_icon_reference_errors(wrong),
                    [f"plugin interface.{field} must be '{boundary.PLUGIN_ICON_REFERENCE}'"],
                )

    def test_plugin_version_accepts_stable_and_prerelease_semver(self) -> None:
        for version in (
            "1.0.0",
            "0.0.0",
            "1.1.0-beta.1",
            "1.1.0-rc.12",
            "1.1.0-0",
            "1.1.0-1alpha",
        ):
            with self.subTest(version=version):
                self.assertIsNotNone(boundary.SEMVER.fullmatch(version))

    def test_plugin_version_rejects_malformed_or_build_metadata_semver(self) -> None:
        for version in (
            "",
            "1.0",
            "01.0.0",
            "1.01.0",
            "1.0.01",
            "1.0.0-01",
            "1.0.0-",
            "1.0.0-beta.",
            "1.0.0-.beta",
            "1.0.0-beta..1",
            "1.0.0-beta_1",
            "1.0.0-beta 1",
            "1.0.0-beta\n1",
            "١.٠.٠",
            "1.0.0-béta",
            "1.0.0+codex.20260807160325",
        ):
            with self.subTest(version=version):
                self.assertIsNone(boundary.SEMVER.fullmatch(version))

    def test_manifest_accepts_matching_prerelease_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_root = Path(temporary)
            plugin_root = temporary_root / "tedd"
            manifest_path = temporary_root / "plugin.json"
            shutil.copytree(boundary.PLUGIN_ROOT, plugin_root)
            codex_manifest = copy.deepcopy(self.codex_manifest)
            portable_manifest = copy.deepcopy(self.portable_manifest)
            codex_manifest["version"] = "1.1.0-beta.1"
            portable_manifest["version"] = "1.1.0-beta.1"
            manifest_path.write_text(json.dumps(codex_manifest), encoding="utf-8")
            (plugin_root / "plugin.json").write_text(
                json.dumps(portable_manifest), encoding="utf-8"
            )
            claude_path = plugin_root / ".claude-plugin/plugin.json"
            claude_manifest = json.loads(claude_path.read_text(encoding="utf-8"))
            claude_manifest["version"] = "1.1.0-beta.1"
            claude_path.write_text(json.dumps(claude_manifest), encoding="utf-8")

            with (
                mock.patch.object(boundary, "PLUGIN_ROOT", plugin_root),
                mock.patch.object(boundary, "PLUGIN_MANIFEST", manifest_path),
                mock.patch.object(
                    boundary, "PORTABLE_PLUGIN_MANIFEST", plugin_root / "plugin.json"
                ),
                mock.patch.object(boundary, "MCP_CONFIG", plugin_root / ".mcp.json"),
                mock.patch.object(boundary, "CLAUDE_PLUGIN_MANIFEST", claude_path),
            ):
                self.assertEqual([], boundary.manifest_errors())


if __name__ == "__main__":
    unittest.main()
