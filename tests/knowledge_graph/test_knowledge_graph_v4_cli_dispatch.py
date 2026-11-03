#!/usr/bin/env python3
"""Focused contract tests for the v4 CLI command-family dispatcher."""

from __future__ import annotations

import argparse
import contextlib
import unittest
from pathlib import Path
from unittest import mock

import knowledge_graph_v4_cli as v4_cli  # noqa: E402
import tedd_sources  # noqa: E402
from tedd_knowledge_graph import v4_cli_contracts  # noqa: E402
from tedd_knowledge_graph.v4_cli_capture import CAPTURE_COMMANDS  # noqa: E402
from tedd_knowledge_graph.v4_cli_coordination import (  # noqa: E402
    COORDINATION_COMMANDS,
)
from tedd_knowledge_graph.v4_cli_coordination import (  # noqa: E402
    READONLY_COMMANDS as READONLY_COORDINATION_COMMANDS,
)
from tedd_knowledge_graph.v4_cli_maintenance import (  # noqa: E402
    MAINTENANCE_COMMANDS,
    OPEN_MAINTENANCE_COMMANDS,
    PREOPEN_MAINTENANCE_COMMANDS,
)
from tedd_knowledge_graph.v4_cli_maintenance import (  # noqa: E402
    READONLY_COMMANDS as READONLY_MAINTENANCE_COMMANDS,
)
from tedd_knowledge_graph.v4_cli_read import (  # noqa: E402
    READ_COMMANDS,
)
from tedd_knowledge_graph.v4_cli_read import (  # noqa: E402
    READONLY_COMMANDS as READONLY_READ_COMMANDS,
)
from tedd_knowledge_graph.v4_contracts import DISCOVERY_KINDS  # noqa: E402


class _Storage:
    def __init__(self) -> None:
        self.opens: list[tuple[Path, bool, bytes | str | None]] = []

    def inspect_store_path(self, path: Path) -> object:
        return {"path": str(path)}

    def detect_store(self, path: Path) -> str:
        return "sqlite_v4"

    def open_store(
        self,
        path: Path,
        *,
        readonly: bool,
        plan_hmac_key: bytes | str | None,
    ) -> contextlib.AbstractContextManager[object]:
        self.opens.append((path, readonly, plan_hmac_key))
        return contextlib.nullcontext(object())


class V4CLICommandFamilyTests(unittest.TestCase):
    def test_standalone_v4_parser_accepts_private_plan_handoff_options(self) -> None:
        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command", required=True)
        v4_cli.register_v4_subcommands(subparsers)
        dry = parser.parse_args(
            ["capture", "--input", "packet.json", "--dry-run", "--plan-output", "private.plan.json"]
        )
        apply = parser.parse_args(
            [
                "capture",
                "--input",
                "packet.json",
                "--expect-plan-file",
                "private.plan.json",
                "--expect-fingerprint",
                "reviewed",
            ]
        )
        self.assertEqual(dry.plan_output, "private.plan.json")
        self.assertEqual(apply.expect_plan_file, "private.plan.json")
        self.assertEqual(dry.mode, "review_safe")

    def test_command_families_are_disjoint_and_cover_the_public_surface(self) -> None:
        families = (
            READ_COMMANDS,
            CAPTURE_COMMANDS,
            COORDINATION_COMMANDS,
            MAINTENANCE_COMMANDS,
        )
        combined = frozenset().union(*families)

        self.assertEqual(
            combined,
            v4_cli.NEW_V4_COMMANDS | v4_cli.COMPATIBLE_COMMANDS,
        )
        self.assertEqual(sum(len(family) for family in families), len(combined))

    def test_flat_contract_reexports_keep_the_public_import_path(self) -> None:
        for name in (
            "CaptureHandler",
            "CaptureRequest",
            "InitializeHandler",
            "InitializeRequest",
            "ProducerEnvelope",
            "ReviewSanitizer",
            "UpgradeHandler",
            "UpgradeRequest",
            "V4CLIError",
        ):
            with self.subTest(name=name):
                self.assertIs(
                    getattr(v4_cli, name),
                    getattr(v4_cli_contracts, name),
                )

    def test_discovery_kind_compatibility_export_uses_the_shared_tuple(self) -> None:
        self.assertIs(v4_cli.DISCOVERY_KINDS, DISCOVERY_KINDS)

    def test_open_store_commands_route_to_exactly_one_family(self) -> None:
        handler_by_command = {
            **{command: "handle_read_command" for command in READ_COMMANDS},
            **{command: "handle_capture_command" for command in CAPTURE_COMMANDS},
            **{command: "handle_coordination_command" for command in COORDINATION_COMMANDS},
            **{command: "handle_maintenance_command" for command in OPEN_MAINTENANCE_COMMANDS},
        }
        readonly_commands = (
            READONLY_READ_COMMANDS | READONLY_COORDINATION_COMMANDS | READONLY_MAINTENANCE_COMMANDS
        ) | {"handoff"}  # Operational receipt writes never need a writable graph connection.
        for command, target in sorted(handler_by_command.items()):
            with self.subTest(command=command):
                storage = _Storage()
                args = argparse.Namespace(command=command, mode=None, store=None)
                with (
                    mock.patch.object(v4_cli, "_storage_module", return_value=storage),
                    mock.patch.object(
                        tedd_sources if command == "source-evidence" else v4_cli,
                        "evidence_command" if command == "source-evidence" else target,
                        return_value={"family": command},
                    ) as handler,
                ):
                    result = v4_cli.handle_v4_command(
                        args,
                        db_path="graph.sqlite3",
                        plan_hmac_key="test-key",
                    )

                self.assertEqual(
                    result,
                    {"command": command, "family": command, "status": "ok"},
                )
                if command in v4_cli.SERVICE_COMMANDS:
                    self.assertEqual(storage.opens, [])
                else:
                    self.assertEqual(len(storage.opens), 1)
                    self.assertEqual(
                        storage.opens[0][1:],
                        (command in readonly_commands, "test-key"),
                    )
                handler.assert_called_once()

    def test_preopen_maintenance_does_not_open_the_target_store(self) -> None:
        for command in sorted(PREOPEN_MAINTENANCE_COMMANDS):
            with self.subTest(command=command):
                storage = _Storage()
                args = argparse.Namespace(command=command, mode=None, store=None)
                initializer = mock.Mock(return_value={"initialized": True})
                upgrader = mock.Mock(return_value={"initialized": True})
                with (
                    mock.patch.object(v4_cli, "_storage_module", return_value=storage),
                    mock.patch.object(
                        v4_cli,
                        "handle_preopen_maintenance_command",
                        return_value={"initialized": True},
                    ) as handler,
                ):
                    result = v4_cli.handle_v4_command(
                        args,
                        db_path="graph.sqlite3",
                        v3_store_path="graph-v3.json",
                        initialize_handler=initializer,
                        upgrade_handler=upgrader,
                    )

                self.assertEqual(
                    result,
                    {
                        "command": command,
                        "initialized": True,
                        "status": "ok",
                    },
                )
                self.assertEqual(storage.opens, [])
                handler.assert_called_once()

    def test_unrelated_command_returns_before_loading_storage(self) -> None:
        args = argparse.Namespace(command="legacy-only")
        with mock.patch.object(v4_cli, "_storage_module") as load_storage:
            self.assertIsNone(v4_cli.handle_v4_command(args, db_path="graph.sqlite3"))
        load_storage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
