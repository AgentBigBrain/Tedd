"""Compatibility and effect-order tests for extracted legacy facade services."""

from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from contextlib import AbstractContextManager
from dataclasses import fields
from pathlib import Path
from typing import IO, cast
from unittest import mock

import knowledge_graph as kg
from tedd_knowledge_graph import legacy_filesystem


class LegacyFacadeServiceTests(unittest.TestCase):
    def test_new_default_is_tedd_but_configure_can_preserve_an_existing_root(self) -> None:
        self.assertEqual("Tedd Knowledge", kg.DEFAULT_ROOT_NAME)
        parsed = kg.build_parser().parse_args(["configure", "--vault", "example-vault"])
        self.assertIsNone(parsed.root_name)

    def test_configure_without_a_root_preserves_custom_and_legacy_implicit_ownership(self) -> None:
        from tedd_knowledge_graph import legacy_configuration

        vault = Path("example-vault").resolve()
        store = Path("example-store.sqlite3").resolve()
        for root_name in ("Owner Knowledge", None):
            with self.subTest(root_name=root_name):
                config: dict[str, object] = {
                    "vault_path": str(vault),
                    "store_path": str(store),
                    "projection_manifest_key": "unchanged-key",
                    "projection_root_id": "unchanged-root",
                }
                if root_name is not None:
                    config["root_name"] = root_name
                expected = root_name or "Codex Knowledge"
                args = argparse.Namespace(vault=str(vault), root_name=None, store=None)
                with (
                    mock.patch.object(kg, "load_config", return_value=config),
                    mock.patch.object(kg, "ensure_projection_target"),
                    mock.patch.object(kg, "secure_state_path", return_value=store),
                    mock.patch.object(kg, "state_privacy_warnings", return_value=[]),
                    mock.patch.object(kg, "atomic_write"),
                ):
                    result = legacy_configuration.configure_locked(
                        args, Path("example-config.json"), kg._legacy_configuration_dependencies()
                    )
                self.assertEqual(expected, result["root_name"])
                self.assertEqual("unchanged-key", result["projection_manifest_key"])
                self.assertEqual("unchanged-root", result["projection_root_id"])
                self.assertEqual(
                    expected,
                    kg.resolve_projection(argparse.Namespace(vault=None, root_name=None), config)[
                        1
                    ],
                )

    def test_filesystem_contracts_are_responsibility_specific(self) -> None:
        path_fields = {field.name for field in fields(legacy_filesystem.PathSecurityDependencies)}
        publication_fields = {
            field.name for field in fields(legacy_filesystem.AtomicWriteDependencies)
        }
        lock_fields = {field.name for field in fields(legacy_filesystem.LockDependencies)}

        self.assertIsNone(getattr(legacy_filesystem, "FilesystemDependencies", None))
        self.assertNotIn("try_file_lock", path_fields | publication_fields)
        self.assertNotIn("make_temp_file", path_fields | lock_fields)
        self.assertNotIn("absolute_path", publication_fields | lock_fields)
        self.assertLessEqual(max(map(len, (path_fields, publication_fields, lock_fields))), 25)

    def test_atomic_publication_preserves_durability_effect_order(self) -> None:
        events: list[str] = []

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "graph.json"

            def ensure_private_directory(path: Path) -> None:
                self.assertEqual(path, target.parent)
                events.append("ensure-directory")

            def make_temp_file(prefix: str, directory: str) -> tuple[int, str]:
                events.append("stage-file")
                return tempfile.mkstemp(prefix=prefix, dir=directory)

            def open_temp_text(descriptor: int) -> AbstractContextManager[IO[str]]:
                events.append("open-stage")
                return cast(
                    AbstractContextManager[IO[str]],
                    os.fdopen(descriptor, "w", encoding="utf-8", newline="\n"),
                )

            def sync_descriptor(descriptor: int) -> None:
                self.assertGreaterEqual(descriptor, 0)
                events.append("sync-stage")

            def set_private_path_mode(path: Path, mode: int) -> None:
                self.assertEqual(mode, 0o600)
                self.assertTrue(path.exists())
                events.append("harden-stage")

            def replace_file_atomically(source: Path, destination: Path) -> None:
                events.append("publish-stage")
                os.replace(source, destination)

            def fsync_parent_directory(path: Path) -> None:
                self.assertEqual(path, target.parent)
                events.append("sync-directory")

            dependencies = legacy_filesystem.AtomicWriteDependencies(
                sync_descriptor=sync_descriptor,
                set_private_path_mode=set_private_path_mode,
                open_temp_text=open_temp_text,
                make_temp_file=make_temp_file,
                ensure_private_directory=ensure_private_directory,
                replace_file_atomically=replace_file_atomically,
                fsync_parent_directory=fsync_parent_directory,
            )

            legacy_filesystem.atomic_write(target, "canonical\n", 0o600, dependencies)

            self.assertEqual(target.read_text(encoding="utf-8"), "canonical\n")
            self.assertEqual(
                events,
                [
                    "ensure-directory",
                    "stage-file",
                    "open-stage",
                    "sync-stage",
                    "harden-stage",
                    "publish-stage",
                    "sync-directory",
                ],
            )

    def test_configuration_facade_resolves_hooks_at_call_time(self) -> None:
        target = Path("resolved-graph.json")
        args = argparse.Namespace(store="requested-graph.json")
        with mock.patch.object(kg, "secure_state_path", return_value=target) as secure:
            self.assertEqual(kg.resolve_store(args, {}), target)
        secure.assert_called_once_with("requested-graph.json", "Canonical store")

    def test_coordination_facade_resolves_hooks_at_call_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            index_path = Path(temporary) / "graph.index.sqlite3"
            index_path.touch()
            metadata = {
                "index_schema_version": kg.INDEX_SCHEMA_VERSION,
                "graph_schema_version": kg.GRAPH_SCHEMA_VERSION,
                "canonical_sha256": "digest",
                "canonical_watermark": "7",
            }
            with (
                mock.patch.object(kg, "recall_index_path", return_value=index_path),
                mock.patch.object(kg, "read_recall_index_meta", return_value=metadata),
                mock.patch.object(kg, "recall_index_healthy", return_value=True) as healthy,
                mock.patch.object(kg, "committed_watermark", return_value=7),
            ):
                status = kg.index_status({}, Path("graph.json"), "digest")

        self.assertTrue(status["healthy"])
        self.assertTrue(status["current"])
        healthy.assert_called_once_with(index_path, full=True)

    def test_parser_and_packet_facades_keep_public_contracts(self) -> None:
        parser = kg.build_parser()
        parsed = parser.parse_args(["capture", "--input", "packet.json"])
        self.assertEqual(parsed.command, "capture")
        self.assertEqual(parsed.authority_ceiling, "assistant_inference")

        with tempfile.TemporaryDirectory() as temporary:
            packet_path = Path(temporary) / "packet.json"
            packet_path.write_text('{"summary": "ready"}', encoding="utf-8")
            self.assertEqual(kg.read_packet(str(packet_path)), {"summary": "ready"})


if __name__ == "__main__":
    unittest.main()
