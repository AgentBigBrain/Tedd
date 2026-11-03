#!/usr/bin/env python3
"""End-to-end safety tests for the v4 live-integration boundary.

All paths are private temporary paths.  The configured live graph, its config,
and the Obsidian vault are never read or written by this suite.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import errno
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from unittest import mock

import knowledge_graph as v3  # noqa: E402
import knowledge_graph_v4 as v4  # noqa: E402
import knowledge_graph_v4_capture as capture  # noqa: E402
import knowledge_graph_v4_cli as v4_cli  # noqa: E402
import knowledge_graph_v4_integration as integration  # noqa: E402
from platform_support import (  # noqa: E402
    mode_has_any,
    owner_is_current_user,
    set_private_path_mode,
)

from tests.support.paths import GRAPH_SCRIPTS as SCRIPT_DIR

FIXED_TIME = "2026-01-01T12:00:00Z"
PLAN_KEY = bytes.fromhex("33" * 32)


def private_write(path: Path, value: Any) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    set_private_path_mode(path.parent, 0o700)
    payload = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    path.write_bytes(payload)
    set_private_path_mode(path, 0o600)
    return payload


def make_v3_graph(*, extra_entities: int = 0) -> dict[str, Any]:
    graph = v3.blank_graph()
    entities = [
        {
            "id": "entity_client_alpha_site",
            "ref": "client_alpha_site",
            "type": "site",
            "namespace": "client_alpha",
            "name": "Test Client Alpha",
            "identity_keys": [
                {
                    "scheme": "client_slug",
                    "namespace": "clients",
                    "value": "client-alpha",
                }
            ],
        }
    ]
    for index in range(extra_entities):
        entities.append(
            {
                "id": f"entity_extra_{index:03d}",
                "ref": f"extra_{index:03d}",
                "type": "project",
                "namespace": "scale_fixture",
                "name": f"Scale fixture {index:03d}",
                "identity_keys": [
                    {
                        "scheme": "fixture_id",
                        "namespace": "scale_fixture",
                        "value": f"fixture-{index:03d}",
                    }
                ],
            }
        )
    packet: dict[str, object] = {
        "source": {
            "kind": "test",
            "role": "fixture",
            "title": "Client Alpha integration fixture",
            "observed_at": FIXED_TIME,
            "source_tier": "validated_structured_candidate",
        },
        "entities": entities,
        "claims": [
            {
                "surface_id": "surface_client_alpha_environment",
                "family": "deployment",
                "key": "environment",
                "value": "staging",
                "valid_from": FIXED_TIME,
                "source_tier": "validated_structured_candidate",
                "entity_refs": ["client_alpha_site"],
                "subject_refs": ["client_alpha_site"],
            }
        ],
        "open_loops": [
            {
                "id": "loop_client_alpha_review",
                "title": "Review Client Alpha library",
                "summary": "Wait for review feedback.",
                "created_at": FIXED_TIME,
                "status": "open",
                "priority": 2,
                "entity_refs": ["client_alpha_site"],
                "owner_refs": ["client_alpha_site"],
                "next_action": "Review feedback",
                "checkpoint_at": "2026-01-02T12:00:00Z",
                "terminal_condition": "Feedback is resolved.",
            }
        ],
    }
    result = v3.capture_packet(
        graph,
        packet,
        authority_ceiling="validated_structured_candidate",
        producer={"agent_id": "fixture-agent", "task_id": "fixture-task"},
        based_on_watermark=0,
        discovery_kind="verification",
    )
    if result["status"] != "captured":
        raise AssertionError(result)
    validation = v3.validate_graph(graph)
    if not validation["valid"]:
        raise AssertionError(validation)
    return graph


def required_string_field(value: Mapping[str, object], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str):
        raise AssertionError(f"{key} must be a string")
    return field


def required_object_field(value: Mapping[str, object], key: str) -> dict[str, object]:
    field = value.get(key)
    if not isinstance(field, dict):
        raise AssertionError(f"{key} must be an object")
    return cast(dict[str, object], field)


def required_object_list_field(value: Mapping[str, object], key: str) -> list[dict[str, object]]:
    field = value.get(key)
    if not isinstance(field, list) or not all(isinstance(item, dict) for item in field):
        raise AssertionError(f"{key} must be a list of objects")
    return cast(list[dict[str, object]], field)


def collections(graph: dict[str, Any]) -> dict[str, Any]:
    return {
        collection: sorted(copy.deepcopy(graph.get(collection, [])), key=lambda item: item["id"])
        for collection in v3.COLLECTIONS
        if collection != "journal"
    }


def entity_change(
    record_id: str,
    *,
    namespace: str,
    name: str,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "record_id": record_id,
        "collection": "entities",
        "payload": {
            "id": record_id,
            "namespace": namespace,
            "type": "site",
            "name": name,
            "aliases": [name],
            "identity_keys": [{"scheme": "test_id", "namespace": namespace, "value": record_id}],
            "lifecycle": "active",
            "status": "current",
            "sensitive": False,
        },
        "change_kind": "create" if expected_revision is None else "revise",
        "search_text": f"{record_id} {namespace} {name}",
    }
    if expected_revision is not None:
        result["expected_revision"] = expected_revision
    return result


class TemporaryIntegrationCase(unittest.TestCase):
    def setUp(self) -> None:
        private_parent = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
        self.temporary = tempfile.TemporaryDirectory(
            prefix="kg-v4-integration-", dir=private_parent
        )
        # macOS exposes its canonical private temp tree through /var. Keep the
        # fixture root canonical so path-boundary tests exercise only the
        # redirects they create inside that root.
        self.root = Path(self.temporary.name).resolve()
        set_private_path_mode(self.root, 0o700)

    def tearDown(self) -> None:
        self.temporary.cleanup()


class FilesystemBoundaryTests(TemporaryIntegrationCase):
    @unittest.skipIf(
        os.name == "nt", "POSIX symlink substitution; reparse rejection tested separately"
    )
    def test_plan_publication_rejects_parent_swap_before_writing_secret_bytes(self) -> None:
        directory = self.root / "plans"
        directory.mkdir(mode=0o700)
        alternate = self.root / "alternate"
        alternate.mkdir(mode=0o700)
        moved = self.root / "moved"
        real_mkstemp = tempfile.mkstemp

        def swapped_stage(*args: Any, **kwargs: Any) -> tuple[int, str]:
            directory.rename(moved)
            directory.symlink_to(alternate, target_is_directory=True)
            return real_mkstemp(*args, **kwargs)

        with (
            mock.patch.object(v4_cli.tempfile, "mkstemp", side_effect=swapped_stage),
            self.assertRaises(v4_cli.V4CLIError),
        ):
            v4_cli._write_plan_file(str(directory / "plan.json"), {"plan_token": "synthetic"})
        self.assertFalse((alternate / "plan.json").exists())
        for remaining in alternate.iterdir():
            self.assertEqual(remaining.read_bytes(), b"")

    @unittest.skipIf(
        os.name == "nt", "POSIX symlink substitution; reparse rejection tested separately"
    )
    def test_plan_input_rejects_parent_swap_before_consuming_snapshot(self) -> None:
        directory = self.root / "plans"
        directory.mkdir(mode=0o700)
        path = directory / "plan.json"
        private_write(path, {"plan_token": "synthetic"})
        moved = self.root / "moved"
        real_snapshot = v4_cli._snapshot_private_input

        def swapped_snapshot(value: str, *, maximum: int) -> v4_cli.CaptureInputSnapshot:
            directory.rename(moved)
            directory.symlink_to(moved, target_is_directory=True)
            return real_snapshot(value, maximum=maximum)

        with (
            mock.patch.object(v4_cli, "_snapshot_private_input", side_effect=swapped_snapshot),
            self.assertRaises(v4_cli.V4CLIError),
        ):
            v4_cli._snapshot_plan_input(str(path))

    def test_plan_publication_propagates_durability_failure(self) -> None:
        output = self.root / "plan.json"
        with (
            mock.patch.object(
                v4_cli, "_fsync_parent_directory", side_effect=OSError(errno.EIO, "synthetic")
            ),
            self.assertRaises(OSError) as raised,
        ):
            v4_cli._write_plan_file(str(output), {"plan_token": "synthetic"})
        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_plan_input_rejects_redirecting_ancestors_before_open(self) -> None:
        redirect = self.root / "simulated-reparse"
        with (
            mock.patch.object(
                v4_cli,
                "is_link_or_reparse_point",
                side_effect=lambda path: Path(path) == redirect,
            ),
            mock.patch.object(v4_cli.os, "open") as open_file,
            self.assertRaisesRegex(v4_cli.V4CLIError, "symlinks or reparse points"),
        ):
            v4_cli._snapshot_plan_input(str(redirect / "plan.json"))
        open_file.assert_not_called()

    def test_plan_input_rejects_stdin(self) -> None:
        with self.assertRaisesRegex(v4_cli.V4CLIError, "not stdin"):
            v4_cli._snapshot_plan_input("-")

    def test_plan_output_requires_private_parent_without_changing_permissions(self) -> None:
        directory = self.root / "shared"
        directory.mkdir(mode=0o700)
        set_private_path_mode(directory, 0o700)
        # Simulate a policy rejection without relying on POSIX chmod to make a
        # Windows DACL nonprivate. Native ACL helpers have their own test suite.
        with (
            mock.patch.object(v4_cli, "mode_has_any", side_effect=lambda path, mask: mask == 0o077),
            self.assertRaisesRegex(v4_cli.V4CLIError, "private current-user directory"),
        ):
            v4_cli._safe_plan_output(str(directory / "plan.json"))
        self.assertFalse(mode_has_any(directory, 0o077))
        self.assertFalse((directory / "plan.json").exists())

    def test_plan_input_rejects_nonprivate_file(self) -> None:
        path = self.root / "plan.json"
        private_write(path, {"version": 1})
        with (
            mock.patch.object(
                v4_cli,
                "mode_has_any",
                side_effect=lambda target, mask: isinstance(target, int),
            ),
            self.assertRaisesRegex(v4_cli.V4CLIError, "group or other users"),
        ):
            v4_cli._snapshot_plan_input(str(path))

    def test_existing_backup_output_requires_a_new_destination(self) -> None:
        output = self.root / "existing-backup.sqlite3"
        output.write_bytes(b"existing")
        set_private_path_mode(output, 0o600)

        with self.assertRaisesRegex(v4_cli.V4CLIError, "already exists; choose a new destination"):
            v4_cli._safe_output_path(
                output,
                "backup output",
                replacement_supported=False,
            )

    def test_output_path_normalizes_lexically_without_resolving(self) -> None:
        output = self.root / "unused-component" / ".." / "export.json"
        expected = self.root / "export.json"

        with mock.patch.object(
            Path,
            "resolve",
            side_effect=AssertionError("output boundary must not resolve links"),
        ):
            normalized = v4_cli._safe_output_path(output, "export output", force=True)

        self.assertEqual(normalized, expected)

    def test_output_path_uses_one_redirect_policy_for_every_component(self) -> None:
        redirect = self.root / "simulated-reparse"
        output = redirect / "export.json"

        with (
            mock.patch.object(
                v4_cli,
                "is_link_or_reparse_point",
                side_effect=lambda path: Path(path) == redirect,
            ) as redirect_check,
            self.assertRaisesRegex(v4_cli.V4CLIError, "symlinks or reparse points"),
        ):
            v4_cli._safe_output_path(output, "export output", force=True)

        self.assertIn(mock.call(redirect), redirect_check.call_args_list)
        self.assertFalse(redirect.exists())

    @unittest.skipIf(os.name == "nt", "POSIX symlink semantics")
    def test_output_path_rejects_existing_symlink_components(self) -> None:
        target_file = self.root / "target.json"
        target_file.write_text("original", encoding="utf-8")
        target_directory = self.root / "target-directory"
        target_directory.mkdir(mode=0o700)
        missing_target = self.root / "missing-target"

        final_link = self.root / "final-link.json"
        final_link.symlink_to(target_file)
        ancestor_link = self.root / "ancestor-link"
        ancestor_link.symlink_to(target_directory, target_is_directory=True)
        dangling_final = self.root / "dangling-final.json"
        dangling_final.symlink_to(missing_target)
        dangling_ancestor = self.root / "dangling-ancestor"
        dangling_ancestor.symlink_to(missing_target, target_is_directory=True)

        candidates = (
            final_link,
            ancestor_link / "export.json",
            dangling_final,
            dangling_ancestor / "export.json",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(v4_cli.V4CLIError, "symlinks or reparse points"):
                    v4_cli._safe_output_path(candidate, "export output", force=True)
        self.assertFalse(missing_target.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX FIFO semantics")
    def test_capture_fifo_is_rejected_without_waiting_for_a_writer(self) -> None:
        fifo = self.root / "capture.fifo"
        cast(Any, os).mkfifo(fifo, mode=0o600)
        script = "\n".join(
            (
                "import sys",
                f"sys.path.insert(0, {str(SCRIPT_DIR)!r})",
                "import knowledge_graph_v4_cli as cli",
                "try:",
                "    cli._snapshot_capture_input(sys.argv[1])",
                "except cli.V4CLIError as error:",
                "    print(error)",
                "    raise SystemExit(0)",
                "raise SystemExit(3)",
            )
        )

        completed = subprocess.run(
            [sys.executable, "-c", script, str(fifo)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed)
        self.assertIn("regular non-symlink file", completed.stdout)

    def test_capture_open_requests_nonblocking_boundary_flag(self) -> None:
        capture_input = self.root / "capture-input"
        nonblocking = 0x4000_0000
        observed_flags: list[int] = []

        def refuse_open(_path: Path, flags: int) -> int:
            observed_flags.append(flags)
            raise OSError(errno.ENOENT, "synthetic missing input")

        with (
            mock.patch.object(v4_cli.os, "O_NONBLOCK", nonblocking, create=True),
            mock.patch.object(v4_cli.os, "open", side_effect=refuse_open),
            self.assertRaises(v4_cli.V4CLIError),
        ):
            v4_cli._snapshot_capture_input(str(capture_input))

        self.assertEqual(len(observed_flags), 1)
        self.assertTrue(observed_flags[0] & nonblocking)

    def test_migration_parent_fsync_uses_narrow_error_policy(self) -> None:
        unsupported = OSError(errno.EINVAL, "directory fsync unsupported")
        with (
            mock.patch.object(integration.os, "name", "posix"),
            mock.patch.object(integration.os, "open", return_value=41),
            mock.patch.object(integration.os, "fsync", side_effect=unsupported),
            mock.patch.object(integration.os, "close") as close,
        ):
            integration._fsync_parent(self.root)
        close.assert_called_once_with(41)

        for error_number in (errno.EACCES, errno.EIO):
            with self.subTest(error_number=error_number):
                failure = OSError(error_number, "directory fsync failed")
                with (
                    mock.patch.object(integration.os, "name", "posix"),
                    mock.patch.object(integration.os, "open", return_value=42),
                    mock.patch.object(integration.os, "fsync", side_effect=failure),
                    mock.patch.object(integration.os, "close"),
                    self.assertRaises(OSError) as raised,
                ):
                    integration._fsync_parent(self.root)
                self.assertEqual(raised.exception.errno, error_number)

    def test_json_export_reports_exact_binary_file_size(self) -> None:
        output = self.root / "export.json"
        payload = {"message": "café", "records": [1, 2]}
        expected = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

        bytes_written = v4_cli._write_json_atomic(output, payload)

        self.assertEqual(output.read_bytes(), expected)
        self.assertEqual(bytes_written, len(expected))
        self.assertEqual(output.stat().st_size, bytes_written)


class FreshInitializationTests(TemporaryIntegrationCase):
    def test_public_publisher_new_default_and_existing_roots_preserve_state_and_files(self) -> None:
        config = self.root / "config.json"
        canonical = self.root / "graph.sqlite3"
        self.cli(config, "init-v4", "--store", str(canonical))
        vault = self.root / "vault"
        v3.ensure_private_directory(vault)
        sentinel = vault / "Owner note.md"
        v3.atomic_write(sentinel, "An owner note, not generated memory.\n")
        with v4.open_store(canonical, readonly=True) as store:
            before = store.state()
        for requested, expected in (
            (None, "Tedd Knowledge"),
            ("Codex Knowledge", "Codex Knowledge"),
            ("Custom Knowledge", "Custom Knowledge"),
        ):
            with self.subTest(root=expected):
                arguments = ["configure", "--vault", str(vault)]
                if requested is not None:
                    arguments += ["--root-name", requested]
                self.cli(config, *arguments)
                initial = v3.load_config(config)
                self.cli(config, "configure", "--vault", str(vault))
                repeated = v3.load_config(config)
                self.assertEqual(expected, repeated["root_name"])
                self.assertEqual(initial["projection_root_id"], repeated["projection_root_id"])
                self.assertEqual(
                    initial["projection_manifest_key"], repeated["projection_manifest_key"]
                )
                self.cli(config, "project", "--mode", "review_safe")
                root = vault / expected
                manifest = json.loads((root / v3.PROJECTION_MANIFEST).read_text(encoding="utf-8"))
                manifest_key = repeated["projection_manifest_key"]
                assert isinstance(manifest_key, str)
                signature = v3.projection_manifest_signature(
                    manifest_key,
                    manifest["root_id"],
                    manifest["mode"],
                    manifest["generated_files"],
                    schema_version=manifest["schema_version"],
                    graph_watermark=manifest["graph_watermark"],
                    graph_sha256=manifest["graph_sha256"],
                    content_hashes=manifest["content_hashes"],
                    canonical_store_schema_version=manifest["canonical_store_schema_version"],
                    projection_payload_schema_version=manifest["projection_payload_schema_version"],
                )
                self.assertEqual(signature, manifest["signature"])
                for relative, digest in manifest["content_hashes"].items():
                    self.assertEqual(
                        digest, hashlib.sha256((root / relative).read_bytes()).hexdigest()
                    )
                self.assertIn(
                    expected + "/", (root / "30 Bases/entities.base").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    "An owner note, not generated memory.\n", sentinel.read_text(encoding="utf-8")
                )
        with v4.open_store(canonical, readonly=True) as store:
            self.assertEqual(before, store.state())
            self.assertTrue(store.validate(full=True)["valid"])

    def cli(
        self,
        config: Path,
        *arguments: str,
        expected_returncode: int = 0,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "knowledge_graph.py"),
                "--config",
                str(config),
                *arguments,
            ],
            cwd=SCRIPT_DIR,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if completed.returncode != expected_returncode:
            self.fail(
                f"CLI returned {completed.returncode}, expected {expected_returncode}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        if expected_returncode != 0:
            return {"stdout": completed.stdout, "stderr": completed.stderr}
        return json.loads(completed.stdout)

    def test_default_first_run_is_explicit_seed_free_and_idempotent(self) -> None:
        home = self.root / "home"
        home.mkdir(mode=0o700)
        set_private_path_mode(home, 0o700)
        config = home / ".codex" / "knowledge-graph" / "config.json"
        store = home / ".codex" / "knowledge-graph" / "graph.sqlite3"
        resolved_store = store.resolve()
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)

        self.assertFalse(config.exists())
        self.assertFalse(store.exists())
        initialized = self.cli(config, "init-v4", env=env)
        self.assertEqual(initialized["status"], "initialized_empty_store")
        self.assertEqual(initialized["seeded_records"], 0)
        self.assertEqual(initialized["target"], str(resolved_store))
        self.assertEqual(initialized["target_validation"]["counts"]["records"], 0)
        self.assertEqual(initialized["target_validation"]["counts"]["revisions"], 0)
        self.assertEqual(initialized["target_validation"]["counts"]["mutations"], 0)
        self.assertTrue(all(count == 0 for count in initialized["operational_counts"].values()))
        if os.name != "nt":
            self.assertEqual(store.stat().st_mode & 0o777, 0o600)
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                store.with_name(f"{store.name}.plan.key").stat().st_mode & 0o777,
                0o600,
            )
        configured = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(configured["store_path"], str(resolved_store))
        self.assertEqual(configured["canonical_store_format"], "sqlite_v4_wal")
        self.assertNotIn("vault_path", configured)

        repeated = self.cli(config, "init-v4", env=env)
        self.assertEqual(repeated["status"], "already_initialized")
        self.assertFalse(repeated["config_changed"])
        self.assertEqual(
            repeated["target_state"]["store_id"], initialized["target_state"]["store_id"]
        )
        self.assertEqual(self.cli(config, "validate", "--full", env=env)["valid"], True)
        self.assertEqual(self.cli(config, "doctor", env=env)["graph"]["watermark"], 0)

    def test_unconfigured_legacy_path_blocks_a_parallel_fresh_store(self) -> None:
        home = self.root / "legacy-home"
        legacy = home / ".codex" / "knowledge-graph" / "graph.json"
        legacy.parent.mkdir(parents=True, mode=0o700)
        set_private_path_mode(legacy.parent, 0o700)
        legacy_bytes = b"{corrupted legacy graph\n"
        legacy.write_bytes(legacy_bytes)
        set_private_path_mode(legacy, 0o600)
        config = legacy.parent / "config.json"
        fresh_store = legacy.parent / "graph.sqlite3"
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)

        refused = self.cli(config, "init-v4", env=env, expected_returncode=1)

        self.assertIn("init-v4 refuses existing", refused["stderr"])
        self.assertEqual(legacy.read_bytes(), legacy_bytes)
        self.assertFalse(fresh_store.exists())
        self.assertFalse(config.exists())

    def test_isolated_sandbox_can_bind_an_explicit_empty_target(self) -> None:
        config = self.root / "sandbox" / "config.json"
        store = self.root / "sandbox" / "graph.sqlite3"
        initialized = self.cli(config, "init-v4", "--store", str(store))
        self.assertEqual(initialized["status"], "initialized_empty_store")
        self.assertEqual(json.loads(config.read_text())["store_path"], str(store.resolve()))
        self.assertEqual(self.cli(config, "status")["graph"]["watermark"], 0)

    def test_configure_and_init_share_one_config_lock(self) -> None:
        root = self.root / "config-race"
        root.mkdir(mode=0o700)
        set_private_path_mode(root, 0o700)
        config = root / "config.json"
        store = root / "graph.sqlite3"
        vault = root / "vault"
        vault.mkdir(mode=0o700)
        set_private_path_mode(vault, 0o700)
        base = [
            sys.executable,
            str(SCRIPT_DIR / "knowledge_graph.py"),
            "--config",
            str(config),
            "--lock-timeout",
            "10",
        ]

        with v3.store_lock(config, 10):
            init_process = subprocess.Popen(
                [*base, "init-v4", "--store", str(store)],
                cwd=SCRIPT_DIR,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            configure_process = subprocess.Popen(
                [
                    *base,
                    "configure",
                    "--vault",
                    str(vault),
                    "--store",
                    str(store),
                ],
                cwd=SCRIPT_DIR,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(0.2)
            self.assertIsNone(init_process.poll())
            self.assertIsNone(configure_process.poll())

        init_stdout, init_stderr = init_process.communicate(timeout=20)
        configure_stdout, configure_stderr = configure_process.communicate(timeout=20)
        self.assertEqual(init_process.returncode, 0, init_stderr)
        self.assertEqual(configure_process.returncode, 0, configure_stderr)
        self.assertEqual(json.loads(init_stdout)["seeded_records"], 0)
        self.assertEqual(json.loads(configure_stdout)["status"], "configured")

        settled = json.loads(config.read_text(encoding="utf-8"))
        self.assertEqual(settled["store_path"], str(store.resolve()))
        self.assertEqual(settled["canonical_store_format"], "sqlite_v4_wal")
        self.assertEqual(settled["vault_path"], str(vault.resolve()))
        self.assertEqual(settled["root_name"], v3.DEFAULT_ROOT_NAME)
        self.assertTrue(settled["projection_manifest_key"])
        self.assertTrue(settled["projection_root_id"])
        self.assertTrue(self.cli(config, "validate", "--full")["valid"])

    def test_init_refuses_a_v3_store_and_preserves_it(self) -> None:
        config = self.root / "config.json"
        source = self.root / "graph.json"
        source_bytes = private_write(source, make_v3_graph())
        private_write(config, {"schema_version": "v2", "store_path": str(source)})
        refused = self.cli(config, "init-v4", expected_returncode=1)
        self.assertIn("upgrade-v4", refused["stderr"])
        self.assertEqual(source.read_bytes(), source_bytes)

    def test_failed_init_cleans_only_new_artifacts(self) -> None:
        config = self.root / "orphan" / "config.json"
        store = self.root / "orphan" / "graph.sqlite3"
        store.parent.mkdir(mode=0o700)
        set_private_path_mode(store.parent, 0o700)
        key = store.with_name(f"{store.name}.plan.key")
        key_bytes = b"operator-owned-preexisting-key\n"
        key.write_bytes(key_bytes)
        key.chmod(0o644)
        if os.name == "nt":
            set_private_path_mode(key, 0o600)

        failed = self.cli(
            config,
            "init-v4",
            "--store",
            str(store),
            expected_returncode=1,
        )

        expected_error = (
            "HMAC key must contain at least 32 bytes"
            if os.name == "nt"
            else "Plan key permissions must be 0600"
        )
        self.assertIn(expected_error, failed["stderr"])
        self.assertFalse(store.exists())
        self.assertFalse(config.exists())
        self.assertEqual(key.read_bytes(), key_bytes)
        if os.name != "nt":
            self.assertEqual(key.stat().st_mode & 0o777, 0o644)

    def test_retry_recovers_only_an_unbound_empty_v4_store(self) -> None:
        config = self.root / "retry" / "config.json"
        store_path = self.root / "retry" / "graph.sqlite3"
        with v4.create_store(store_path) as store:
            original_store_id = store.state()["store_id"]
        plan_key = store_path.with_name(f"{store_path.name}.plan.key")
        plan_key.unlink()

        recovered = self.cli(config, "init-v4", "--store", str(store_path))
        self.assertEqual(recovered["status"], "recovered_empty_store")
        self.assertEqual(recovered["target_state"]["store_id"], original_store_id)
        self.assertTrue(recovered["plan_key_created"])
        self.assertTrue(plan_key.is_file())
        if os.name != "nt":
            self.assertEqual(plan_key.stat().st_mode & 0o777, 0o600)

        other_config = self.root / "occupied" / "config.json"
        occupied_path = self.root / "occupied" / "graph.sqlite3"
        with v4.create_store(occupied_path) as occupied:
            occupied.register_consumer(
                "consumer_existing",
                agent_id="agent-existing",
                task_id="task-existing",
            )
        refused = self.cli(
            other_config,
            "init-v4",
            "--store",
            str(occupied_path),
            expected_returncode=1,
        )
        self.assertIn("Refusing to adopt an unbound v4 store", refused["stderr"])
        self.assertFalse(other_config.exists())
        with v4.open_store(occupied_path, readonly=True) as occupied:
            self.assertEqual(occupied.consumer_status("consumer_existing")["found"], True)

        governance_config = self.root / "governance" / "config.json"
        governance_path = self.root / "governance" / "graph.sqlite3"
        with v4.create_store(governance_path) as governed:
            governed.connection.execute(
                "INSERT INTO identity_schemes(scheme,normalization,governed,created_watermark) "
                "VALUES(?,?,?,?)",
                ("private_identity", "binary", 1, 0),
            )
            governed.connection.commit()
            self.assertTrue(governed.validate(full=True)["valid"])
        governance_refused = self.cli(
            governance_config,
            "init-v4",
            "--store",
            str(governance_path),
            expected_returncode=1,
        )
        self.assertIn(
            "Refusing to adopt an unbound v4 store",
            governance_refused["stderr"],
        )
        self.assertFalse(governance_config.exists())

        invalid_key_config = self.root / "invalid-key" / "config.json"
        invalid_key_store = self.root / "invalid-key" / "graph.sqlite3"
        with v4.create_store(invalid_key_store):
            pass
        invalid_key = invalid_key_store.with_name(f"{invalid_key_store.name}.plan.key")
        invalid_key.unlink()
        invalid_key.mkdir(mode=0o700)
        invalid_key_refused = self.cli(
            invalid_key_config,
            "init-v4",
            "--store",
            str(invalid_key_store),
            expected_returncode=1,
        )
        self.assertIn("Plan key must be a regular file", invalid_key_refused["stderr"])
        self.assertFalse(invalid_key_config.exists())
        self.assertTrue(invalid_key.is_dir())


class MigrationAndDispatchTests(TemporaryIntegrationCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = self.root / "graph.json"
        self.target = self.root / "graph.sqlite3"
        self.config = self.root / "config.json"
        self.original_graph = make_v3_graph()
        self.original_bytes = private_write(self.source, self.original_graph)
        private_write(
            self.config,
            {
                "schema_version": "v2",
                "store_path": str(self.source),
                "canonical_store_format": "json_v3",
            },
        )

    def cli(self, *arguments: str, expected_returncode: int = 0) -> dict[str, Any]:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "knowledge_graph.py"),
                "--config",
                str(self.config),
                *arguments,
            ],
            cwd=SCRIPT_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            check=False,
        )
        if completed.returncode != expected_returncode:
            self.fail(
                f"CLI returned {completed.returncode}, expected {expected_returncode}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        if expected_returncode != 0:
            return {"stdout": completed.stdout, "stderr": completed.stderr}
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            self.fail(f"CLI returned non-JSON: {error}\n{completed.stdout}\n{completed.stderr}")

    def test_digest_gated_cli_upgrade_is_equivalent_recoverable_and_activated(self) -> None:
        dry = self.cli("upgrade-v4", "--dry-run")
        self.assertEqual(dry["status"], "would_upgrade")
        self.assertEqual(dry["source_sha256"], hashlib.sha256(self.original_bytes).hexdigest())
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertEqual(json.loads(self.config.read_text())["store_path"], str(self.source))

        bad = self.cli(
            "upgrade-v4",
            "--apply",
            "--expect-source-sha256",
            "0" * 64,
            "--expect-plan-fingerprint",
            dry["plan_fingerprint"],
            "--producer-agent-id",
            "migration-agent",
            "--task-id",
            "migration-task",
            "--producer-run-id",
            "migration-run-bad",
            "--based-on-watermark",
            str(dry["plan"]["source_watermark"]),
            expected_returncode=1,
        )
        self.assertIn("digest differs", bad["stderr"])
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source.read_bytes(), self.original_bytes)

        applied = self.cli(
            "upgrade-v4",
            "--apply",
            "--expect-source-sha256",
            dry["source_sha256"],
            "--expect-plan-fingerprint",
            dry["plan_fingerprint"],
            "--producer-agent-id",
            "migration-agent",
            "--task-id",
            "migration-task",
            "--producer-run-id",
            "migration-run-good",
            "--based-on-watermark",
            str(dry["plan"]["source_watermark"]),
        )
        self.assertEqual(applied["status"], "upgraded_and_activated")
        self.assertTrue(applied["config_switched"])
        self.assertTrue(self.target.is_file())
        self.assertTrue(self.target.with_name(f"{self.target.name}.plan.key").is_file())

        config = json.loads(self.config.read_text())
        self.assertEqual(config["store_path"], str(self.target.resolve()))
        self.assertEqual(config["canonical_store_format"], "sqlite_v4_wal")
        tombstone = json.loads(self.source.read_text())
        self.assertEqual(tombstone["schema_version"], "v4_redirect")
        self.assertEqual(tombstone["canonical_store"], str(self.target.resolve()))

        backup = Path(applied["source_backup"])
        rollback = Path(applied["rollback_export"])
        manifest = Path(applied["migration_manifest"])
        self.assertEqual(backup.read_bytes(), self.original_bytes)
        self.assertTrue(rollback.is_file())
        self.assertTrue(manifest.is_file())
        manifest_value = json.loads(manifest.read_text())
        self.assertEqual(manifest_value["source_sha256"], dry["source_sha256"])
        self.assertEqual(
            manifest_value["rollback_export_sha256"],
            hashlib.sha256(rollback.read_bytes()).hexdigest(),
        )

        with v4.open_store(self.target, readonly=True) as store:
            exported = store.export_current_graph(schema_version="v3")
            self.assertEqual(collections(exported), collections(self.original_graph))
            self.assertTrue(store.validate(full=True)["valid"])

        status = self.cli("status")
        self.assertTrue(status["store"]["safe"])
        self.assertEqual(status["graph"]["schema_version"], "v4")
        doctor = self.cli("doctor")
        self.assertTrue(doctor["validation"]["valid"])
        self.assertTrue(doctor["store"]["safe"])
        self.assertEqual(doctor["graph"]["schema_version"], "v4")

    def test_valid_source_change_after_review_blocks_cutover(self) -> None:
        dry = self.cli("upgrade-v4", "--dry-run")
        changed = copy.deepcopy(self.original_graph)
        result = v3.capture_packet(
            changed,
            {
                "source": {
                    "kind": "test",
                    "role": "concurrent-writer",
                    "title": "Concurrent v3 update",
                    "observed_at": "2026-01-04T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                },
                "events": [
                    {
                        "id": "event_concurrent_v3_update",
                        "kind": "milestone",
                        "title": "Concurrent v3 update",
                        "summary": "A writer committed after migration review.",
                        "occurred_at": "2026-01-04T12:00:00Z",
                        "entity_refs": ["entity_client_alpha_site"],
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
            producer={"agent_id": "peer-agent", "task_id": "peer-task"},
            based_on_watermark=v3.committed_watermark(changed),
            discovery_kind="milestone",
        )
        self.assertEqual(result["status"], "captured")
        changed_bytes = private_write(self.source, changed)
        self.assertNotEqual(hashlib.sha256(changed_bytes).hexdigest(), dry["source_sha256"])

        rejected = self.cli(
            "upgrade-v4",
            "--apply",
            "--expect-source-sha256",
            dry["source_sha256"],
            "--expect-plan-fingerprint",
            dry["plan_fingerprint"],
            "--producer-agent-id",
            "migration-agent",
            "--task-id",
            "migration-task",
            "--producer-run-id",
            "migration-run-stale",
            "--based-on-watermark",
            str(dry["plan"]["source_watermark"]),
            expected_returncode=1,
        )
        self.assertIn("digest differs", rejected["stderr"])
        self.assertFalse(self.target.exists())
        self.assertEqual(json.loads(self.config.read_text())["store_path"], str(self.source))
        self.assertEqual(self.source.read_bytes(), changed_bytes)


class CapturePlanIntegrationTests(TemporaryIntegrationCase):
    def setUp(self) -> None:
        super().setUp()
        self.store_path = self.root / "graph.sqlite3"
        graph = make_v3_graph()
        raw = (json.dumps(graph, sort_keys=True) + "\n").encode("utf-8")
        store = v4.import_v3_graph(
            graph,
            self.store_path,
            hashlib.sha256(raw).hexdigest(),
            producer_agent_id="migration-agent",
            task_id="migration-task",
            plan_hmac_key=PLAN_KEY,
        )
        with store:
            store.register_consumer(
                "consumer_client_alpha_task",
                agent_id="agent-alpha",
                task_id="task-client-alpha",
                parent_task_id="parent-work",
            )
        self.packet_path = self.root / "packet.json"

    def capture_cli(self, *arguments: str, success: bool = True) -> dict[str, Any]:
        """Exercise the public default-mode CLI against only this disposable store."""
        config = self.root / "config.json"
        private_write(
            config,
            {
                "schema_version": "v2",
                "store_path": str(self.store_path),
                "canonical_store_format": "sqlite_v4_wal",
            },
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_DIR / "knowledge_graph.py"),
                "--config",
                str(config),
                "capture",
                "--input",
                str(self.packet_path),
                "--consumer-id",
                "consumer_client_alpha_task",
                "--producer-agent-id",
                "agent-alpha",
                "--task-id",
                "task-client-alpha",
                "--parent-task-id",
                "parent-work",
                "--producer-run-id",
                "run-cli",
                "--discovery-kind",
                "verification",
                "--based-on-watermark",
                "1",
                "--authority-ceiling",
                "validated_structured_candidate",
                *arguments,
            ],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if success:
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            return json.loads(completed.stdout)
        self.assertNotEqual(completed.returncode, 0)
        return {"error_output": completed.stdout + completed.stderr}

    def test_reviewed_capture_is_searchable_with_authority_and_source_privacy_intact(self) -> None:
        private_write(
            self.packet_path,
            {
                "source": {
                    "kind": "discussion",
                    "role": "user",
                    "title": "Logo approval meeting",
                    "observed_at": "2026-01-03T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                },
                "decisions": [
                    {
                        "id": "decision_logo_approved",
                        "title": "Logo approved",
                        "summary": "The blue logo was approved for Client Alpha.",
                        "rationale": "Recorded owner decision.",
                        "decided_at": "2026-01-03T12:00:00Z",
                        "status": "accepted",
                        "entity_refs": ["entity_client_alpha_site"],
                    },
                    {
                        "id": "decision_logo_proposal",
                        "title": "Logo alternative proposed",
                        "summary": "A green logo is a proposal, not an approval.",
                        "rationale": "No owner acceptance.",
                        "decided_at": "2026-01-03T12:00:00Z",
                        "status": "proposed",
                        "entity_refs": ["entity_client_alpha_site"],
                    },
                ],
            },
        )
        plan = self.root / "search.plan.json"
        preview = self.capture_cli("--dry-run", "--plan-output", str(plan))
        self.capture_cli(
            "--expect-plan-file", str(plan), "--expect-fingerprint", preview["fingerprint"]
        )
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            store.subscribe(
                "consumer_client_alpha_task",
                {"entity_ids": ["entity_client_alpha_site"]},
                subscription_id="scope",
            )
            self.assertTrue(store.validate(full=True)["valid"])
            before = store.state()
            raw = store.search_subscription("scope", "logo", limit=10)
            safe = cast(dict[str, Any], integration.sanitize_review_safe_value(raw))
            decisions = {
                r["record_id"]: r["payload"]
                for r in safe["results"]
                if r["collection"] == "decisions"
            }
            self.assertEqual({"decision_logo_approved", "decision_logo_proposal"}, set(decisions))
            prefix = store.search_subscription("scope", "logo approv*")
            self.assertEqual(
                {"decision_logo_approved", "decision_logo_proposal"},
                {r["record_id"] for r in prefix["results"] if r["collection"] == "decisions"},
            )
            self.assertEqual(
                ["decision_logo_approved"],
                [
                    r["record_id"]
                    for r in store.search_subscription("scope", "logo approved*")["results"]
                ],
            )
            self.assertEqual("accepted", decisions["decision_logo_approved"]["status"])
            self.assertEqual("proposed", decisions["decision_logo_proposal"]["status"])
            for record_id, decision in decisions.items():
                original = cast(dict[str, object], store.get_record(record_id)["payload"])
                self.assertEqual(original["authority_state"], decision["authority_state"])
                self.assertEqual(["[withheld in review_safe mode]"], decision["evidence_refs"])
            self.assertEqual(before, store.state())

    def test_default_cli_applies_reviewed_private_plan_without_disclosing_token(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        plan_path = self.root / "review.plan.json"

        preview = self.capture_cli("--dry-run", "--plan-output", str(plan_path))

        self.assertEqual(preview["status"], "would_capture")
        self.assertEqual(preview["plan_handoff"], "saved")
        self.assertEqual(preview["plan_token"], "[redacted secret]")
        private_plan = json.loads(plan_path.read_bytes())
        self.assertEqual(set(private_plan), {"version", "fingerprint", "plan_token"})
        self.assertTrue(owner_is_current_user(plan_path))
        self.assertFalse(mode_has_any(plan_path, 0o077))
        self.assertNotIn(private_plan["plan_token"], json.dumps(preview))
        self.assertEqual(private_plan["fingerprint"], preview["fingerprint"])
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(store.watermark, preview["base_watermark"])
        applied = self.capture_cli(
            "--expect-plan-file",
            str(plan_path),
            "--expect-fingerprint",
            preview["fingerprint"],
        )

        self.assertEqual(applied["status"], "captured")
        self.assertNotIn(private_plan["plan_token"], json.dumps(applied))
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertIsNotNone(store.get_record("event_private_plan"))
            self.assertEqual(store.watermark, preview["base_watermark"] + 1)

    def test_reviewed_capture_recall_and_projection_preserve_navigable_lineage(self) -> None:
        private_write(
            self.packet_path,
            {
                "source": {
                    "kind": "discussion",
                    "role": "user",
                    "title": "Readable connection integration",
                    "observed_at": "2026-01-03T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                    "sensitive": False,
                },
                "entities": [
                    {
                        "ref": "mira",
                        "type": "person",
                        "namespace": "people",
                        "name": "Mira",
                        "sensitive": False,
                    },
                    {
                        "ref": "other_mira",
                        "type": "person",
                        "namespace": "vendors",
                        "name": "Mira",
                        "sensitive": False,
                    },
                ],
                "relations": [
                    {
                        "source": "mira",
                        "target": "entity_client_alpha_site",
                        "type": "contributes_to",
                        "status": "current",
                        "sensitive": False,
                    }
                ],
                "observations": [
                    {
                        "family": "review",
                        "key": "draft",
                        "value": "Review is pending",
                        "observed_at": "2026-01-03T12:00:00Z",
                        "entity_refs": ["entity_client_alpha_site"],
                        "sensitive": False,
                    },
                    *[
                        {
                            "family": "review_history",
                            "key": f"draft_{index:02d}",
                            "value": f"Recorded review note {index:02d}",
                            "observed_at": f"2026-01-02T{index % 24:02d}:00:00Z",
                            "entity_refs": ["entity_client_alpha_site"],
                            "sensitive": False,
                        }
                        for index in range(26)
                    ],
                ],
                "claims": [
                    {
                        "family": "review",
                        "key": "draft",
                        "value": "Review is pending",
                        "valid_from": "2026-01-03T12:00:00Z",
                        "entity_refs": ["entity_client_alpha_site"],
                        "subject_refs": ["entity_client_alpha_site"],
                        "sensitive": False,
                    }
                ],
                "decisions": [
                    {
                        "id": "decision_readable_connections",
                        "title": "Review the draft",
                        "summary": "Mira will review before publication.",
                        "rationale": "Catch errors early.",
                        "decided_at": "2026-01-03T12:05:00Z",
                        "status": "accepted",
                        "entity_refs": ["mira", "entity_client_alpha_site"],
                        "sensitive": False,
                    },
                    {
                        "id": "decision_readable_connections_candidate",
                        "title": "Consider a second reviewer",
                        "summary": "A second reviewer remains a proposal.",
                        "rationale": "No acceptance has been recorded.",
                        "decided_at": "2026-01-03T12:06:00Z",
                        "status": "candidate",
                        "entity_refs": ["entity_client_alpha_site"],
                        "sensitive": False,
                    },
                ],
                "open_loops": [
                    {
                        "id": "loop_readable_connections",
                        "title": "Finish the draft review",
                        "summary": "The review remains open.",
                        "created_at": "2026-01-03T12:10:00Z",
                        "status": "open",
                        "priority": 2,
                        "owner_refs": ["mira"],
                        "entity_refs": ["entity_client_alpha_site"],
                        "next_action": "Review the draft.",
                        "terminal_condition": "Review is recorded.",
                        "sensitive": False,
                    },
                    {
                        "id": "loop_readable_connections_resolved",
                        "title": "Archive the reviewed draft",
                        "summary": "The reviewed draft was archived.",
                        "created_at": "2026-01-03T11:10:00Z",
                        "status": "open",
                        "priority": 1,
                        "owner_refs": ["mira"],
                        "entity_refs": ["entity_client_alpha_site"],
                        "next_action": "Send the already archived draft.",
                        "terminal_condition": "Archive is recorded.",
                        "sensitive": False,
                    },
                ],
            },
        )
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            base = store.watermark
            preview = capture.execute_capture(
                store,
                self.request(base, "readable-connections-dry", dry_run=True),
            )
            self.assertEqual(preview["status"], "would_capture")
            applied = capture.execute_capture(
                store,
                self.request(
                    base,
                    "readable-connections-apply",
                    dry_run=False,
                    expected_fingerprint=required_string_field(preview, "fingerprint"),
                    plan_token=required_string_field(preview, "plan_token"),
                ),
            )
            self.assertEqual(applied["status"], "captured")

            private_write(
                self.packet_path,
                {
                    "source": {
                        "kind": "discussion",
                        "role": "user",
                        "title": "Restricted supporting evidence",
                        "observed_at": "2026-01-03T12:30:00Z",
                        "source_tier": "validated_structured_candidate",
                        "sensitive": True,
                    },
                    "events": [
                        {
                            "id": "event_unavailable_evidence",
                            "kind": "milestone",
                            "title": "Public result with unavailable evidence",
                            "summary": "The result remains visible without exposing its source.",
                            "occurred_at": "2026-01-03T12:30:00Z",
                            "status": "complete",
                            "entity_refs": ["entity_client_alpha_site"],
                            "sensitive": False,
                        }
                    ],
                },
            )
            restricted_base = store.watermark
            restricted_preview = capture.execute_capture(
                store,
                self.request(
                    restricted_base,
                    "readable-connections-restricted-dry",
                    dry_run=True,
                ),
            )
            self.assertEqual(restricted_preview["status"], "would_capture")
            restricted_applied = capture.execute_capture(
                store,
                self.request(
                    restricted_base,
                    "readable-connections-restricted-apply",
                    dry_run=False,
                    expected_fingerprint=required_string_field(restricted_preview, "fingerprint"),
                    plan_token=required_string_field(restricted_preview, "plan_token"),
                ),
            )
            self.assertEqual(restricted_applied["status"], "captured")

        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            captured_watermark = store.watermark
        with mock.patch.object(v3, "utc_now", autospec=True, return_value="2026-01-03T13:00:00Z"):
            resolved = integration.resolve_loop_v4(
                self.store_path,
                argparse.Namespace(
                    producer_agent_id="agent-alpha",
                    task_id="task-client-alpha",
                    parent_task_id="parent-work",
                    producer_run_id="readable-connections-resolve",
                    based_on_watermark=captured_watermark,
                    discovery_kind="verification",
                    id="loop_readable_connections_resolved",
                    resolution="Draft review recorded.",
                ),
            )
        self.assertEqual(resolved["status"], "applied")
        self.assertEqual(cast(dict[str, object], resolved["result"])["status"], "resolved")

        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            before = dict(store.state())
            rows = store.connection.execute(
                "SELECT collection, record_id FROM records "
                "WHERE collection IN "
                "('entities','relations','observations','claims','events','decisions','open_loops','sources') "
                "ORDER BY collection, record_id"
            ).fetchall()
            selected = store.get_records([str(row[1]) for row in rows])
            before_records = copy.deepcopy(selected)
        payloads = [cast(dict[str, object], item["payload"]) for item in selected]
        observation = next(
            item for item in payloads if item.get("key") == "draft" and item.get("source_ref")
        )
        claim = next(item for item in payloads if item.get("observation_refs"))
        relation = next(item for item in payloads if item.get("type") == "contributes_to")
        mira = next(item for item in payloads if item.get("name") == "Mira")
        decision = next(
            item for item in payloads if item.get("id") == "decision_readable_connections"
        )
        proposed_decision = next(
            item for item in payloads if item.get("id") == "decision_readable_connections_candidate"
        )
        commitment = next(
            item for item in payloads if item.get("id") == "loop_readable_connections"
        )
        resolved_commitment = next(
            item for item in payloads if item.get("id") == "loop_readable_connections_resolved"
        )
        unavailable_event = next(
            item for item in payloads if item.get("id") == "event_unavailable_evidence"
        )
        self.assertEqual(observation["value"], "Review is pending")
        self.assertIn(observation["id"], cast(list[object], claim["observation_refs"]))
        expected_connections = {
            "relation": (
                mira["id"],
                "entity_client_alpha_site",
                "contributes_to",
            ),
            "claim_observation": (claim["id"], observation["id"]),
            "observation_source": (observation["id"], observation["source_ref"]),
            "decision_entities": (
                decision["id"],
                tuple(cast(list[str], decision["entity_refs"])),
            ),
            "commitment_owner": (
                commitment["id"],
                tuple(cast(list[str], commitment["owner_refs"])),
            ),
        }
        self.assertEqual(
            expected_connections["relation"],
            (
                relation["source_entity_id"],
                relation["target_entity_id"],
                relation["type"],
            ),
        )
        self.assertEqual(expected_connections["commitment_owner"][1], (mira["id"],))
        self.assertEqual(proposed_decision["status"], "candidate")
        self.assertEqual(resolved_commitment["status"], "resolved")
        self.assertEqual(resolved_commitment["resolution"], "Draft review recorded.")
        self.assertEqual(resolved_commitment["resolved_at"], "2026-01-03T13:00:00Z")
        self.assertEqual(unavailable_event["status"], "complete")

        vault = self.root / "vault"
        vault.mkdir()
        set_private_path_mode(vault, 0o700)
        user_note = vault / "Owner note.md"
        user_note.write_text("Owner-authored sentinel.\n", encoding="utf-8")
        settings = vault / ".obsidian"
        settings.mkdir()
        settings_sentinel = settings / "workspace.json"
        settings_sentinel.write_text('{"owner":"sentinel"}\n', encoding="utf-8")
        user_note_before = user_note.read_bytes()
        settings_before = settings_sentinel.read_bytes()
        # This test proves the reviewed record-to-artifact journey. Dedicated
        # filesystem tests retain native ancestry/ACL enforcement coverage.
        with mock.patch.object(v3, "assert_trusted_directory_chain", autospec=True):
            result = integration.project_v4(
                self.store_path,
                vault=vault,
                root_name="Tedd Knowledge",
                mode="review_safe",
                manifest_key="8" * 64,
                root_id="readable-connections",
                timeout_seconds=5.0,
            )
        self.assertEqual(result["status"], "projected")
        root = vault / "Tedd Knowledge"
        observation_files = tuple((root / "22 Observations").glob("*.md"))
        self.assertEqual(len(observation_files), 27)
        claim_note = next(
            path.read_text(encoding="utf-8")
            for path in (root / "11 Claims").glob("*.md")
            if f'kg_id: "{claim["id"]}"' in path.read_text(encoding="utf-8")
        )
        self.assertIn("22 Observations/", claim_note)
        self.assertTrue(
            any("20 Evidence/" in path.read_text(encoding="utf-8") for path in observation_files)
        )
        entity_files = tuple((root / "10 Entities").glob("*.md"))
        entity_notes = "\n".join(path.read_text(encoding="utf-8") for path in entity_files)
        self.assertIn("Mira]] contributes to [[", entity_notes)
        self.assertIn("recorded owner: [[", entity_notes)
        self.assertIn("## Recorded observations", entity_notes)
        self.assertIn("Review is pending", entity_notes)
        self.assertIn("### Accepted", entity_notes)
        self.assertIn("### Proposed or needs checking", entity_notes)
        self.assertIn("Review the draft", entity_notes)
        self.assertIn("Consider a second reviewer", entity_notes)
        self.assertIn("next action: Review the draft.", entity_notes)
        self.assertIn("outcome: Draft review recorded.", entity_notes)
        self.assertIn("completed: January 3, 2026 at 13:00 UTC", entity_notes)
        client_note = next(
            path.read_text(encoding="utf-8")
            for path in entity_files
            if "# Test Client Alpha" in path.read_text(encoding="utf-8")
        )
        self.assertIn("> [!note]- More recorded observations", client_note)
        self.assertIn("Summary shortened: 2 more permitted notes", client_note)
        mira_files = [
            path for path in entity_files if "\n# Mira\n" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(len(mira_files), 2)
        self.assertEqual(len({path.name for path in mira_files}), 2)
        unavailable_event_note = next(
            path.read_text(encoding="utf-8")
            for path in (root / "12 Incidents and Events").glob("*.md")
            if f'kg_id: "{unavailable_event["id"]}"' in path.read_text(encoding="utf-8")
        )
        self.assertIn("Evidence is not available in this view", unavailable_event_note)
        dashboard = (root / "00 Dashboard.md").read_text(encoding="utf-8")
        self.assertIn("## People and projects", dashboard)
        self.assertLess(
            dashboard.index("## People and projects"), dashboard.index("## Snapshot details")
        )
        manifest_path = root / v3.PROJECTION_MANIFEST
        first_manifest = cast(dict[str, object], json.loads(manifest_path.read_text()))
        generated_files = cast(list[str], first_manifest["generated_files"])
        for relative in generated_files:
            if relative.endswith(".md"):
                self.assertIn(
                    '  - "tedd-reading"',
                    (root / relative).read_text(encoding="utf-8"),
                )
        first_projection = {
            relative: (root / relative).read_bytes() for relative in generated_files
        }
        with mock.patch.object(v3, "assert_trusted_directory_chain", autospec=True):
            repeated = integration.project_v4(
                self.store_path,
                vault=vault,
                root_name="Tedd Knowledge",
                mode="review_safe",
                manifest_key="8" * 64,
                root_id="readable-connections",
                timeout_seconds=5.0,
            )
        self.assertIn(repeated["status"], {"projected", "unchanged"})
        repeated_manifest = cast(dict[str, object], json.loads(manifest_path.read_text()))
        self.assertEqual(
            {relative: (root / relative).read_bytes() for relative in generated_files},
            first_projection,
        )
        # Publication time is intentionally fresh. The authenticated generated
        # file set, content hashes and signature must remain stable.
        for key in ("generated_files", "content_hashes", "signature"):
            self.assertEqual(repeated_manifest[key], first_manifest[key])
        self.assertEqual(user_note.read_bytes(), user_note_before)
        self.assertEqual(settings_sentinel.read_bytes(), settings_before)
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(dict(store.state()), before)
            self.assertEqual(store.get_records([str(row[1]) for row in rows]), before_records)
            self.assertTrue(store.validate(full=True)["valid"])

    def test_selected_projection_hides_omitted_or_ineligible_entity_context(self) -> None:
        private_write(
            self.packet_path,
            {
                "source": {
                    "kind": "test",
                    "role": "fixture",
                    "title": "Synthetic context boundary",
                    "observed_at": "2026-01-03T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                    "sensitive": False,
                },
                "entities": [
                    {
                        "id": "hidden-project",
                        "ref": "hidden_target",
                        "type": "project",
                        "namespace": "global",
                        "name": "Private synthetic project",
                        "sensitive": True,
                    },
                    {
                        "id": "entity_visible_context_member",
                        "ref": "visible_member",
                        "type": "person",
                        "namespace": "hidden-project",
                        "name": "Synthetic visible member",
                        "sensitive": False,
                    },
                    {
                        "id": "public-project",
                        "ref": "public_target",
                        "type": "project",
                        "namespace": "global",
                        "name": "Synthetic public project",
                        "sensitive": False,
                    },
                    {
                        "id": "entity_sensitive_context_member",
                        "ref": "sensitive_member",
                        "type": "person",
                        "namespace": "public-project",
                        "name": "Synthetic sensitive member",
                        "sensitive": True,
                    },
                ],
                "open_loops": [
                    {
                        "id": "loop_visible_context_member",
                        "title": "Review selected context",
                        "summary": "Keep the public member selected.",
                        "created_at": "2026-01-03T12:00:00Z",
                        "status": "open",
                        "entity_refs": ["visible_member"],
                        "sensitive": False,
                    }
                ],
            },
        )
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            base = store.watermark
            preview = capture.execute_capture(
                store,
                self.request(base, "context-boundary-dry", dry_run=True),
            )
            self.assertEqual(preview["status"], "would_capture")
            applied = capture.execute_capture(
                store,
                self.request(
                    base,
                    "context-boundary-apply",
                    dry_run=False,
                    expected_fingerprint=required_string_field(preview, "fingerprint"),
                    plan_token=required_string_field(preview, "plan_token"),
                ),
            )
            self.assertEqual(applied["status"], "captured")
            self.assertTrue(store.validate(full=True)["valid"])
            complete, _ = integration._projection_graph(store)
            with (
                mock.patch.object(integration, "MAX_PROJECTION_RECORDS", 3),
                mock.patch.object(integration, "MAX_PROJECTION_ROOT_RECORDS", 1),
            ):
                selected, _ = integration._projection_graph(store)

        def display_context(graph: dict[str, object], entity_id: str, mode: str) -> str:
            notes = v3.build_projection_notes(graph, mode, "Tedd Knowledge")
            marker = f"kg_id: {json.dumps(entity_id)}"
            note = next(
                content
                for path, content in notes.items()
                if path.startswith("10 Entities/") and marker in content
            )
            line = next(item for item in note.splitlines() if item.startswith("display_context: "))
            return cast(str, json.loads(line.removeprefix("display_context: ")))

        selected_ids = {
            cast(str, item["id"])
            for collection in v3.COLLECTIONS
            for item in cast(list[dict[str, object]], selected.get(collection, []))
        }
        self.assertIn("entity_visible_context_member", selected_ids)
        self.assertNotIn("hidden-project", selected_ids)
        for graph in (complete, selected):
            with self.subTest(graph="complete" if graph is complete else "selected"):
                self.assertEqual(
                    display_context(
                        graph,
                        "entity_visible_context_member",
                        "review_safe",
                    ),
                    "Context not shown",
                )

        for mode in ("review_safe", "operator_full"):
            with self.subTest(mode=mode, classification="sensitive"):
                self.assertEqual(
                    display_context(
                        complete,
                        "entity_sensitive_context_member",
                        mode,
                    ),
                    "Context not shown",
                )

        unclassified = copy.deepcopy(complete)
        unclassified_entity = next(
            item
            for item in cast(list[dict[str, object]], unclassified["entities"])
            if item["id"] == "entity_sensitive_context_member"
        )
        unclassified_entity.pop("sensitive")
        self.assertTrue(v3.validate_graph(unclassified)["valid"])
        for mode in ("review_safe", "operator_full"):
            with self.subTest(mode=mode, classification="missing"):
                self.assertEqual(
                    display_context(
                        unclassified,
                        "entity_sensitive_context_member",
                        mode,
                    ),
                    "Context not shown",
                )

    def test_private_plan_output_never_overwrites_an_existing_file(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        plan_path = self.root / "review.plan.json"
        private_write(plan_path, {"sentinel": "preserve"})
        original = plan_path.read_bytes()

        result = self.capture_cli("--dry-run", "--plan-output", str(plan_path), success=False)

        self.assertIn("already exists", result["error_output"])
        self.assertEqual(plan_path.read_bytes(), original)

    def test_private_plan_requires_explicit_reviewed_fingerprint(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        plan_path = self.root / "review.plan.json"
        self.capture_cli("--dry-run", "--plan-output", str(plan_path))

        result = self.capture_cli("--expect-plan-file", str(plan_path), success=False)

        self.assertIn("requires --expect-fingerprint", result["error_output"])
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            with self.assertRaises(KeyError):
                store.get_record("event_private_plan")

    def test_private_plan_rejects_changed_input_and_tampered_token(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        plan_path = self.root / "review.plan.json"
        preview = self.capture_cli("--dry-run", "--plan-output", str(plan_path))
        plan = json.loads(plan_path.read_bytes())
        self.packet("event_private_plan", "Changed after review")

        changed = self.capture_cli(
            "--expect-plan-file",
            str(plan_path),
            "--expect-fingerprint",
            preview["fingerprint"],
            success=False,
        )

        self.assertIn("fingerprint", changed["error_output"].lower())
        self.packet("event_private_plan", "Private plan round trip")
        plan["plan_token"] = "synthetic-invalid-plan"
        private_write(plan_path, plan)
        tampered = self.capture_cli(
            "--expect-plan-file",
            str(plan_path),
            "--expect-fingerprint",
            preview["fingerprint"],
            success=False,
        )
        self.assertNotIn("synthetic-invalid-plan", tampered["error_output"])
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            with self.assertRaises(KeyError):
                store.get_record("event_private_plan")

    def test_private_plan_dry_run_recognizes_already_committed_capture(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        plan_path = self.root / "review.plan.json"
        preview = self.capture_cli("--dry-run", "--plan-output", str(plan_path))
        self.capture_cli(
            "--expect-plan-file",
            str(plan_path),
            "--expect-fingerprint",
            preview["fingerprint"],
        )
        original = plan_path.read_bytes()

        result = self.capture_cli("--dry-run", "--plan-output", str(plan_path))

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["plan_handoff"], "not_needed")
        self.assertEqual(plan_path.read_bytes(), original)

    def test_private_plan_preserves_relevant_revision_guard(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        plan_path = self.root / "review.plan.json"
        preview = self.capture_cli("--dry-run", "--plan-output", str(plan_path))
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            current = store.get_record("entity_client_alpha_site")
            revised = copy.deepcopy(current["payload"])
            if not isinstance(revised, dict):
                self.fail("entity payload must be an object")
            revised["aliases"] = ["Revised while reviewing"]
            store.apply_changes(
                [
                    {
                        "record_id": "entity_client_alpha_site",
                        "collection": "entities",
                        "payload": revised,
                        "change_kind": "revise",
                        "expected_revision": current["revision"],
                    }
                ],
                fingerprint="synthetic-relevant-write",
                producer_agent_id="agent-peer",
                task_id="task-peer",
                run_id="run-peer",
                based_on_watermark=store.watermark,
                discovery_kind="verification",
            )
            watermark = store.watermark

        rejected = self.capture_cli(
            "--expect-plan-file",
            str(plan_path),
            "--expect-fingerprint",
            preview["fingerprint"],
            success=False,
        )

        self.assertIn("Relevant record changed after review", rejected["error_output"])
        with v4.open_store(self.store_path, readonly=True, plan_hmac_key=PLAN_KEY) as store:
            self.assertEqual(store.watermark, watermark)

    def test_private_plan_allows_unrelated_write_without_global_watermark_guard(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        plan_path = self.root / "review.plan.json"
        preview = self.capture_cli("--dry-run", "--plan-output", str(plan_path))
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            store.apply_changes(
                [entity_change("entity_unrelated", namespace="other", name="Other")],
                fingerprint="synthetic-unrelated-write",
                producer_agent_id="agent-peer",
                task_id="task-peer",
                run_id="run-peer",
                based_on_watermark=store.watermark,
                discovery_kind="verification",
            )

        result = self.capture_cli(
            "--expect-plan-file",
            str(plan_path),
            "--expect-fingerprint",
            preview["fingerprint"],
        )

        self.assertEqual(result["status"], "captured")

    def test_private_file_handoff_accepts_plan_larger_than_command_line_limit(self) -> None:
        record_ids = [f"entity_bulk_{index}" for index in range(100)]
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            store.apply_changes(
                [
                    entity_change(record_id, namespace="bulk", name=record_id)
                    for record_id in record_ids
                ],
                fingerprint="synthetic-bulk-fixture",
                producer_agent_id="agent-peer",
                task_id="task-peer",
                run_id="run-peer",
                based_on_watermark=store.watermark,
                discovery_kind="verification",
            )
        self.packet("event_large_plan", "Large private plan")
        packet = json.loads(self.packet_path.read_bytes())
        packet["events"][0]["entity_refs"] = record_ids
        private_write(self.packet_path, packet)
        plan_path = self.root / "large.plan.json"

        preview = self.capture_cli("--dry-run", "--plan-output", str(plan_path))
        plan = json.loads(plan_path.read_bytes())
        self.assertGreater(len(plan["plan_token"]), 16_384)
        applied = self.capture_cli(
            "--expect-plan-file",
            str(plan_path),
            "--expect-fingerprint",
            preview["fingerprint"],
        )

        self.assertEqual(applied["status"], "captured")

    def test_private_plan_rejects_malformed_and_oversized_documents(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        plan_path = self.root / "bad.plan.json"
        for payload in (
            b"not JSON",
            b"[" * 4000,
            b"x" * (v4_cli.MAX_CAPTURE_PLAN_BYTES + 1),
            b"{}",
        ):
            with self.subTest(size=len(payload)):
                private_write(plan_path, {})
                plan_path.write_bytes(payload)
                result = self.capture_cli(
                    "--expect-plan-file",
                    str(plan_path),
                    "--expect-fingerprint",
                    "synthetic",
                    success=False,
                )
                self.assertNotIn("Traceback", result["error_output"])

    def test_plan_version_integer_is_bounded_before_python_conversion(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        plan_path = self.root / "integer.plan.json"
        private_write(plan_path, {})
        plan_path.write_bytes(
            b'{"version":' + b"9" * 4000 + b',"fingerprint":"synthetic","plan_token":"synthetic"}'
        )

        result = self.capture_cli(
            "--expect-plan-file",
            str(plan_path),
            "--expect-fingerprint",
            "synthetic",
            success=False,
        )

        self.assertIn("not valid bounded JSON", result["error_output"])

    def test_private_plan_rejects_duplicate_json_fields(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        plan_path = self.root / "review.plan.json"
        preview = self.capture_cli("--dry-run", "--plan-output", str(plan_path))
        plan_path.write_bytes(plan_path.read_bytes().replace(b"{", b'{"version":1,', 1))

        result = self.capture_cli(
            "--expect-plan-file",
            str(plan_path),
            "--expect-fingerprint",
            preview["fingerprint"],
            success=False,
        )

        self.assertIn("not valid bounded JSON", result["error_output"])

    def test_redacted_placeholder_explains_the_private_handoff(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")

        result = self.capture_cli(
            "--expect-plan-token",
            "[redacted secret]",
            "--expect-fingerprint",
            "synthetic",
            success=False,
        )

        self.assertIn("--plan-output", result["error_output"])
        self.assertIn("--expect-plan-file", result["error_output"])

    def test_private_plan_rejects_invalid_option_combinations(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        plan_path = self.root / "review.plan.json"
        cases = [
            ("--plan-output", str(plan_path)),
            ("--dry-run", "--expect-plan-file", str(plan_path)),
            ("--expect-plan-file", str(plan_path), "--expect-plan-token", "synthetic"),
        ]
        for options in cases:
            with self.subTest(options=options):
                result = self.capture_cli(*options, success=False)
                self.assertIn("plan", result["error_output"])
                self.assertFalse(plan_path.exists())

    def test_plan_file_options_fail_closed_on_legacy_v3(self) -> None:
        self.packet("event_private_plan", "Private plan round trip")
        source = self.root / "legacy.json"
        original = private_write(source, make_v3_graph())
        self.store_path = source
        plan_path = self.root / "legacy.plan.json"
        for options in (
            ("--dry-run", "--plan-output", str(plan_path)),
            ("--expect-plan-file", str(plan_path), "--expect-fingerprint", "synthetic"),
        ):
            with self.subTest(options=options):
                result = self.capture_cli(*options, success=False)
                self.assertIn("require a v4 store", result["error_output"])
                self.assertEqual(source.read_bytes(), original)
                self.assertFalse(plan_path.exists())

    def packet(self, event_id: str, title: str) -> None:
        private_write(
            self.packet_path,
            {
                "source": {
                    "kind": "test",
                    "role": "verified",
                    "title": title,
                    "observed_at": "2026-01-03T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                },
                "events": [
                    {
                        "id": event_id,
                        "kind": "milestone",
                        "title": title,
                        "summary": f"Verified {title}",
                        "occurred_at": "2026-01-03T12:00:00Z",
                        "entity_refs": ["entity_client_alpha_site"],
                    }
                ],
            },
        )

    def analysis_packet(
        self,
        *,
        include_coverage: bool = True,
        existing_current_claim_id: str | None = None,
    ) -> None:
        observed_at = "2026-01-03T13:00:00Z"
        packet: dict[str, Any] = {
            "source": {
                "kind": "document",
                "role": "assistant",
                "title": "Synthetic proposal response analysis",
                "observed_at": observed_at,
                "source_tier": "validated_structured_candidate",
            },
            "observations": [
                {
                    "family": "proposal_risk",
                    "key": "synthetic.integration_gap",
                    "value": "The integration contract remains undefined.",
                    "observed_at": observed_at,
                },
            ],
            "claims": [
                {
                    "family": "proposal_architecture",
                    "key": "synthetic.architecture",
                    "value": "The architecture was verified from the proposal.",
                    "valid_from": observed_at,
                    "subject_refs": ["entity_client_alpha_site"],
                }
            ],
            "decisions": [
                {
                    "id": "decision_synthetic_addendum_v4",
                    "title": "Require a synthetic technical addendum",
                    "summary": "Assistant recommendation pending stakeholder acceptance.",
                    "decided_at": observed_at,
                    "status": "accepted",
                    "source_tier": "assistant_inference",
                }
            ],
            "events": [
                {
                    "id": "event_synthetic_proposal_review_v4",
                    "kind": "proposal_response_review",
                    "title": "Synthetic proposal response reviewed",
                    "occurred_at": observed_at,
                }
            ],
            "receipts": [
                {
                    "kind": "artifact_verification",
                    "title": "Synthetic proposal review artifact",
                    "verified_at": observed_at,
                    "verified": True,
                }
            ],
        }
        if existing_current_claim_id:
            packet["claims"] = [
                {
                    "surface_id": "surface_client_alpha_environment",
                    "family": "deployment",
                    "key": "environment",
                    "value": "staging",
                    "valid_from": FIXED_TIME,
                    "source_tier": "validated_structured_candidate",
                    "subject_refs": ["entity_client_alpha_site"],
                }
            ]
        if include_coverage:
            packet["coverage"] = {
                "version": "v1",
                "items": [
                    {
                        "category": "identities_relationships_status",
                        "summary": "coverage-narrative-must-not-persist",
                        "disposition": "existing",
                        "record_id": "entity_client_alpha_site",
                    },
                    {
                        "category": "verified_current_state",
                        "summary": "The proposed architecture is verified.",
                        "disposition": "captured",
                        "path": "claims[0]",
                    },
                    {
                        "category": "conclusions_risks_unknowns",
                        "summary": "The integration contract gap is material.",
                        "disposition": "captured",
                        "path": "observations[0]",
                    },
                    {
                        "category": "recommendations_decisions",
                        "summary": "A technical addendum is recommended.",
                        "disposition": "captured",
                        "path": "decisions[0]",
                    },
                    {
                        "category": "obligations_next_actions",
                        "summary": "No accepted implementation obligation exists yet.",
                        "disposition": "not_applicable",
                        "reason": "no_material_outcome",
                        "explanation": "No owner or checkpoint has been accepted.",
                    },
                    {
                        "category": "artifacts_events_receipts",
                        "summary": "The review artifact was verified.",
                        "disposition": "captured",
                        "path": "receipts[0]",
                    },
                ],
            }
            if existing_current_claim_id:
                coverage = required_object_field(packet, "coverage")
                current_item = required_object_list_field(coverage, "items")[1]
                current_item.clear()
                current_item.update(
                    {
                        "category": "verified_current_state",
                        "summary": "The prior validated environment claim remains current.",
                        "disposition": "existing",
                        "record_id": existing_current_claim_id,
                    }
                )
        private_write(self.packet_path, packet)

    def envelope(self, watermark: int, run_id: str) -> v4_cli.ProducerEnvelope:
        return v4_cli.ProducerEnvelope(
            producer_agent_id="agent-alpha",
            task_id="task-client-alpha",
            parent_task_id="parent-work",
            producer_run_id=run_id,
            discovery_kind="milestone",
            based_on_watermark=watermark,
        )

    def request(
        self,
        watermark: int,
        run_id: str,
        *,
        dry_run: bool,
        expected_fingerprint: str | None = None,
        plan_token: str | None = None,
    ) -> v4_cli.CaptureRequest:
        return v4_cli.CaptureRequest(
            input_path=str(self.packet_path),
            dry_run=dry_run,
            consumer_id="consumer_client_alpha_task",
            envelope=self.envelope(watermark, run_id),
            authority_ceiling="validated_structured_candidate",
            allow_replace=False,
            allow_redirect=False,
            expected_fingerprint=expected_fingerprint,
            expected_global_watermark=None,
            plan_token=plan_token,
            expected_revisions={},
            expected_absent=(),
        )

    def test_capture_rejects_a_non_v4_store_at_the_handler_boundary(self) -> None:
        with self.assertRaisesRegex(
            TypeError,
            "Capture handler requires an open KnowledgeGraphV4 store",
        ):
            capture.execute_capture(
                object(),
                self.request(0, "run-invalid-store", dry_run=True),
            )

    def test_registered_consumer_dry_run_and_apply(self) -> None:
        self.packet(
            "event_client_alpha_capture_happy",
            "Client Alpha capture happy path",
        )
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            base = store.watermark
            dry = capture.execute_capture(store, self.request(base, "run-happy-dry", dry_run=True))
            self.assertEqual(dry["status"], "would_capture")
            expected_revisions = dry["expected_revisions"]
            if not isinstance(expected_revisions, dict):
                self.fail("expected revisions must be an object")
            self.assertIn("entity_client_alpha_site", expected_revisions)
            applied = capture.execute_capture(
                store,
                self.request(
                    base,
                    "run-happy-apply",
                    dry_run=False,
                    expected_fingerprint=required_string_field(dry, "fingerprint"),
                    plan_token=required_string_field(dry, "plan_token"),
                ),
            )
            self.assertEqual(applied["status"], "captured")
            captured = store.get_record("event_client_alpha_capture_happy")["payload"]
            if not isinstance(captured, dict):
                self.fail("captured payload must be an object")
            self.assertEqual(
                captured["title"],
                "Client Alpha capture happy path",
            )

    def test_proposal_review_rejects_sparse_packet_before_v4_mutation(self) -> None:
        self.analysis_packet(include_coverage=False)
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            base = store.watermark
            with self.assertRaisesRegex(ValueError, "coverage ledger is required"):
                capture.execute_capture(
                    store,
                    self.request(base, "run-sparse-analysis", dry_run=True),
                )
            self.assertEqual(store.watermark, base)

    def test_v4_coverage_resolves_existing_ids_and_persists_metadata(self) -> None:
        self.analysis_packet()
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            base = store.watermark
            dry = capture.execute_capture(
                store,
                self.request(base, "run-analysis-dry", dry_run=True),
            )
            coverage = required_object_field(dry, "coverage")
            coverage_digest = required_string_field(coverage, "coverage_digest")
            self.assertRegex(coverage_digest, r"^[0-9a-f]{32}$")
            expected_revisions = required_object_field(dry, "expected_revisions")
            self.assertIn("entity_client_alpha_site", expected_revisions)
            identity_item = required_object_list_field(coverage, "items")[0]
            self.assertEqual(identity_item["record_ids"], ["entity_client_alpha_site"])
            applied = capture.execute_capture(
                store,
                self.request(
                    base,
                    "run-analysis-apply",
                    dry_run=False,
                    expected_fingerprint=required_string_field(dry, "fingerprint"),
                    plan_token=required_string_field(dry, "plan_token"),
                ),
            )
            self.assertEqual(applied["status"], "captured")
            decision = store.get_record("decision_synthetic_addendum_v4")["payload"]
            if not isinstance(decision, dict):
                self.fail("captured decision payload must be an object")
            self.assertEqual(decision["source_tier"], "assistant_inference")
            self.assertEqual(
                (decision["authority_state"], decision["status"]),
                ("candidate", "proposed"),
            )
            row = store.connection.execute(
                "SELECT details_json FROM mutations WHERE watermark=?",
                (applied["watermark"],),
            ).fetchone()
            details = json.loads(row["details_json"])
            self.assertNotIn("coverage-narrative-must-not-persist", row["details_json"])
            details_coverage = required_object_field(details, "coverage")
            self.assertEqual(
                required_string_field(details_coverage, "coverage_digest"), coverage_digest
            )

            current_item = required_object_list_field(coverage, "items")[1]
            current_ids = current_item.get("record_ids")
            if not isinstance(current_ids, list) or len(current_ids) != 1:
                self.fail("current-state coverage must resolve one derived claim ID")
            claim_id = str(current_ids[0])
            claim_record = store.get_record(claim_id)
            original_claim = copy.deepcopy(claim_record["payload"])
            if not isinstance(original_claim, dict):
                self.fail("captured claim payload must be an object")
            replacement_id = "claim_synthetic_architecture_replacement_v4"
            superseded_claim = copy.deepcopy(original_claim)
            superseded_claim.update(
                {
                    "active": False,
                    "lifecycle": "superseded",
                    "valid_to": "2026-01-04T13:00:00Z",
                    "ended_at": "2026-01-04T13:00:00Z",
                    "superseded_by": replacement_id,
                }
            )
            replacement_claim = copy.deepcopy(original_claim)
            replacement_claim.update(
                {
                    "id": replacement_id,
                    "value": "A replacement architecture is now verified.",
                    "active": True,
                    "lifecycle": "current",
                    "valid_from": "2026-01-04T13:00:00Z",
                    "valid_to": None,
                    "ended_at": None,
                    "superseded_by": None,
                }
            )
            store.apply_changes(
                [
                    {
                        "record_id": claim_id,
                        "collection": "claims",
                        "payload": superseded_claim,
                        "change_kind": "revise",
                        "expected_revision": claim_record["revision"],
                    },
                    {
                        "record_id": replacement_id,
                        "collection": "claims",
                        "payload": replacement_claim,
                        "change_kind": "create",
                    },
                ],
                fingerprint="supersede-captured-coverage-claim",
                producer_agent_id="maintenance-agent",
                task_id="maintenance-task",
                run_id="maintenance-supersede-run",
                based_on_watermark=store.watermark,
                discovery_kind="verification",
            )
            superseded_retry = capture.execute_capture(
                store,
                self.request(store.watermark, "run-analysis-superseded-duplicate", dry_run=True),
            )
            self.assertEqual(superseded_retry["status"], "duplicate")

            existing = store.get_record("entity_client_alpha_site")
            redacted_payload = copy.deepcopy(existing["payload"])
            if not isinstance(redacted_payload, dict):
                self.fail("existing entity payload must be an object")
            redacted_payload["lifecycle"] = "redacted"
            redacted_decision = copy.deepcopy(decision)
            redacted_decision["lifecycle"] = "redacted"
            decision_record = store.get_record("decision_synthetic_addendum_v4")
            artifact_item = required_object_list_field(coverage, "items")[5]
            artifact_ids = artifact_item.get("record_ids")
            if not isinstance(artifact_ids, list) or len(artifact_ids) != 1:
                self.fail("artifact coverage must resolve one derived receipt ID")
            receipt_id = str(artifact_ids[0])
            receipt_record = store.get_record(receipt_id)
            redacted_receipt = copy.deepcopy(receipt_record["payload"])
            if not isinstance(redacted_receipt, dict):
                self.fail("captured receipt payload must be an object")
            redacted_receipt["lifecycle"] = "redacted"
            risk_item = required_object_list_field(coverage, "items")[2]
            risk_ids = risk_item.get("record_ids")
            if not isinstance(risk_ids, list) or len(risk_ids) != 1:
                self.fail("risk coverage must resolve one derived observation ID")
            observation_id = str(risk_ids[0])
            observation_record = store.get_record(observation_id)
            redacted_observation = copy.deepcopy(observation_record["payload"])
            if not isinstance(redacted_observation, dict):
                self.fail("captured observation payload must be an object")
            redacted_observation["lifecycle"] = "redacted"
            claim_record = store.get_record(claim_id)
            redacted_claim = copy.deepcopy(claim_record["payload"])
            if not isinstance(redacted_claim, dict):
                self.fail("captured claim payload must be an object")
            redacted_claim["lifecycle"] = "redacted"
            redacted_claim["active"] = False
            store.apply_changes(
                [
                    {
                        "record_id": "entity_client_alpha_site",
                        "collection": "entities",
                        "payload": redacted_payload,
                        "change_kind": "revise",
                        "expected_revision": existing["revision"],
                    },
                    {
                        "record_id": "decision_synthetic_addendum_v4",
                        "collection": "decisions",
                        "payload": redacted_decision,
                        "change_kind": "revise",
                        "expected_revision": decision_record["revision"],
                    },
                    {
                        "record_id": receipt_id,
                        "collection": "receipts",
                        "payload": redacted_receipt,
                        "change_kind": "revise",
                        "expected_revision": receipt_record["revision"],
                    },
                    {
                        "record_id": observation_id,
                        "collection": "observations",
                        "payload": redacted_observation,
                        "change_kind": "revise",
                        "expected_revision": observation_record["revision"],
                    },
                    {
                        "record_id": claim_id,
                        "collection": "claims",
                        "payload": redacted_claim,
                        "change_kind": "revise",
                        "expected_revision": claim_record["revision"],
                    },
                ],
                fingerprint="redact-existing-coverage-reference",
                producer_agent_id="maintenance-agent",
                task_id="maintenance-task",
                run_id="maintenance-run",
                based_on_watermark=store.watermark,
                discovery_kind="redaction",
            )
            duplicate = capture.execute_capture(
                store,
                self.request(store.watermark, "run-analysis-duplicate", dry_run=True),
            )
            self.assertEqual(duplicate["status"], "duplicate")
            duplicate_coverage = required_object_field(duplicate, "coverage")
            self.assertEqual(
                required_string_field(duplicate_coverage, "coverage_digest"), coverage_digest
            )
            with self.assertRaisesRegex(ValueError, "based_on_watermark"):
                capture.execute_capture(
                    store,
                    self.request(
                        store.watermark + 1,
                        "run-analysis-future-watermark-duplicate",
                        dry_run=True,
                    ),
                )
            with self.assertRaisesRegex(v4.PlanTokenError, "fingerprint does not match"):
                capture.execute_capture(
                    store,
                    self.request(
                        store.watermark,
                        "run-analysis-mismatched-duplicate",
                        dry_run=True,
                        expected_fingerprint="0" * 32,
                    ),
                )

    def test_v4_existing_current_claim_retry_survives_later_supersession(self) -> None:
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            row = store.connection.execute(
                "SELECT record_id FROM records WHERE collection='claims' ORDER BY record_id LIMIT 1"
            ).fetchone()
            if row is None:
                self.fail("fixture must contain one validated current claim")
            claim_id = str(row["record_id"])
            self.analysis_packet(existing_current_claim_id=claim_id)
            base = store.watermark
            dry = capture.execute_capture(
                store,
                self.request(base, "run-existing-claim-dry", dry_run=True),
            )
            applied = capture.execute_capture(
                store,
                self.request(
                    base,
                    "run-existing-claim-apply",
                    dry_run=False,
                    expected_fingerprint=required_string_field(dry, "fingerprint"),
                    plan_token=required_string_field(dry, "plan_token"),
                ),
            )
            self.assertEqual(applied["status"], "captured")

            current = store.get_record(claim_id)
            old_claim = copy.deepcopy(current["payload"])
            if not isinstance(old_claim, dict):
                self.fail("existing claim payload must be an object")
            replacement_id = "claim_client_alpha_environment_replacement_v4"
            replacement = copy.deepcopy(old_claim)
            old_claim.update(
                {
                    "active": False,
                    "lifecycle": "superseded",
                    "valid_to": "2026-01-04T13:00:00Z",
                    "ended_at": "2026-01-04T13:00:00Z",
                    "superseded_by": replacement_id,
                }
            )
            replacement.update(
                {
                    "id": replacement_id,
                    "value": "production",
                    "active": True,
                    "lifecycle": "current",
                    "valid_from": "2026-01-04T13:00:00Z",
                    "valid_to": None,
                    "ended_at": None,
                    "superseded_by": None,
                }
            )
            store.apply_changes(
                [
                    {
                        "record_id": claim_id,
                        "collection": "claims",
                        "payload": old_claim,
                        "change_kind": "revise",
                        "expected_revision": current["revision"],
                    },
                    {
                        "record_id": replacement_id,
                        "collection": "claims",
                        "payload": replacement,
                        "change_kind": "create",
                    },
                ],
                fingerprint="supersede-existing-coverage-claim",
                producer_agent_id="maintenance-agent",
                task_id="maintenance-task",
                run_id="maintenance-existing-supersede-run",
                based_on_watermark=store.watermark,
                discovery_kind="verification",
            )
            retry = capture.execute_capture(
                store,
                self.request(store.watermark, "run-existing-claim-duplicate", dry_run=True),
            )
            self.assertEqual(retry["status"], "duplicate")

    def test_v4_overlapping_existing_decision_retry_precedes_redacted_identity(self) -> None:
        self.analysis_packet()
        packet_value = json.loads(self.packet_path.read_text(encoding="utf-8"))
        coverage = required_object_field(packet_value, "coverage")
        recommendation = required_object_list_field(coverage, "items")[3]
        recommendation.clear()
        recommendation.update(
            {
                "category": "recommendations_decisions",
                "summary": "Reuse the exact candidate recommendation.",
                "disposition": "existing",
                "record_id": "decision_synthetic_addendum_v4",
            }
        )
        private_write(self.packet_path, packet_value)

        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            store.apply_changes(
                [
                    {
                        "record_id": "decision_synthetic_addendum_v4",
                        "collection": "decisions",
                        "payload": {
                            "id": "decision_synthetic_addendum_v4",
                            "kind": "decision",
                            "title": "Require a synthetic technical addendum",
                            "summary": "Assistant recommendation pending stakeholder acceptance.",
                            "rationale": "",
                            "decided_at": "2026-01-03T13:00:00Z",
                            "status": "proposed",
                            "requested_status": "accepted",
                            "authority_state": "candidate",
                            "entity_refs": [],
                            "evidence_refs": [],
                            "source_tier": "assistant_inference",
                            "sensitive": False,
                            "first_observed_at": "2026-01-03T13:00:00Z",
                            "last_observed_at": "2026-01-03T13:00:00Z",
                        },
                        "change_kind": "create",
                    }
                ],
                fingerprint="seed-overlapping-existing-decision",
                producer_agent_id="fixture-agent",
                task_id="fixture-task",
                run_id="fixture-overlap-run",
                based_on_watermark=store.watermark,
                discovery_kind="verification",
            )
            base = store.watermark
            dry = capture.execute_capture(
                store,
                self.request(base, "run-overlap-dry", dry_run=True),
            )
            applied = capture.execute_capture(
                store,
                self.request(
                    base,
                    "run-overlap-apply",
                    dry_run=False,
                    expected_fingerprint=required_string_field(dry, "fingerprint"),
                    plan_token=required_string_field(dry, "plan_token"),
                ),
            )
            self.assertEqual(applied["status"], "captured")

            decision = store.get_record("decision_synthetic_addendum_v4")
            redacted = copy.deepcopy(decision["payload"])
            if not isinstance(redacted, dict):
                self.fail("overlapping decision payload must be an object")
            redacted.update(
                {
                    "title": "[redacted]",
                    "status": "redacted",
                    "authority_state": "redacted",
                    "lifecycle": "redacted",
                }
            )
            store.apply_changes(
                [
                    {
                        "record_id": "decision_synthetic_addendum_v4",
                        "collection": "decisions",
                        "payload": redacted,
                        "change_kind": "revise",
                        "expected_revision": decision["revision"],
                    }
                ],
                fingerprint="redact-overlapping-existing-decision",
                producer_agent_id="maintenance-agent",
                task_id="maintenance-task",
                run_id="maintenance-overlap-redaction-run",
                based_on_watermark=store.watermark,
                discovery_kind="redaction",
            )
            retry = capture.execute_capture(
                store,
                self.request(store.watermark, "run-overlap-duplicate", dry_run=True),
            )
            self.assertEqual(retry["status"], "duplicate")

            recommendation["summary"] = "A changed nonduplicate recommendation mapping."
            private_write(self.packet_path, packet_value)
            with self.assertRaisesRegex(ValueError, "semantically eligible"):
                capture.execute_capture(
                    store,
                    self.request(store.watermark, "run-overlap-stale", dry_run=True),
                )

    def test_capture_uses_validated_bytes_when_input_path_is_replaced(self) -> None:
        original_id = "event_client_alpha_snapshotted"
        replacement_id = "event_client_alpha_replacement"
        self.packet(original_id, "Snapshotted capture input")
        snapshot = v4_cli._snapshot_capture_input(str(self.packet_path))
        self.packet(replacement_id, "Replacement capture input")

        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            request = dataclasses.replace(
                self.request(store.watermark, "run-snapshot", dry_run=True),
                input_path=snapshot.path,
                input_bytes=snapshot.content,
            )
            dry = capture.execute_capture(store, request)

        changed_ids = dry["changed_ids"]
        if not isinstance(changed_ids, list):
            self.fail("changed IDs must be a list")
        self.assertIn(original_id, changed_ids)
        self.assertNotIn(replacement_id, changed_ids)

    def test_unrelated_write_preserves_plan_after_rerecall(self) -> None:
        self.packet(
            "event_client_alpha_after_beta",
            "Client Alpha after independent Client Beta write",
        )
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            dry = capture.execute_capture(
                store,
                self.request(store.watermark, "run-unrelated-dry", dry_run=True),
            )
            store.apply_changes(
                [
                    entity_change(
                        "entity_client_beta_site",
                        namespace="client_beta",
                        name="Test Client Beta",
                    )
                ],
                fingerprint="independent-client-beta-write",
                producer_agent_id="agent-beta",
                task_id="task-client-beta",
                run_id="run-client-beta",
                based_on_watermark=store.watermark,
                discovery_kind="milestone",
            )
            applied = capture.execute_capture(
                store,
                self.request(
                    store.watermark,
                    "run-unrelated-apply",
                    dry_run=False,
                    expected_fingerprint=required_string_field(dry, "fingerprint"),
                    plan_token=required_string_field(dry, "plan_token"),
                ),
            )
            self.assertEqual(applied["status"], "captured")
            self.assertIsNotNone(store.get_record("entity_client_beta_site"))
            self.assertIsNotNone(store.get_record("event_client_alpha_after_beta"))

    def test_relevant_write_invalidates_plan(self) -> None:
        self.packet("event_client_alpha_stale", "Client Alpha stale relevant plan")
        with v4.open_store(self.store_path, plan_hmac_key=PLAN_KEY) as store:
            dry = capture.execute_capture(
                store,
                self.request(store.watermark, "run-stale-dry", dry_run=True),
            )
            current = store.get_record("entity_client_alpha_site")
            current_payload = current["payload"]
            if not isinstance(current_payload, dict):
                self.fail("current payload must be an object")
            revised: dict[str, Any] = copy.deepcopy(current_payload)
            aliases = revised.get("aliases", [])
            if not isinstance(aliases, list) or not all(
                isinstance(alias, str) for alias in aliases
            ):
                self.fail("entity aliases must be a list of strings")
            revised["aliases"] = sorted(set(cast(list[str], aliases)) | {"Client Alpha"})
            store.apply_changes(
                [
                    {
                        "record_id": "entity_client_alpha_site",
                        "collection": "entities",
                        "payload": revised,
                        "change_kind": "revise",
                        "expected_revision": current["revision"],
                        "search_text": "Test Client Alpha",
                    }
                ],
                fingerprint="relevant-client-alpha-write",
                producer_agent_id="agent-alpha-peer",
                task_id="task-client-alpha-peer",
                run_id="run-client-alpha-peer",
                based_on_watermark=store.watermark,
                discovery_kind="milestone",
            )
            dry_fingerprint = required_string_field(dry, "fingerprint")
            dry_plan_token = required_string_field(dry, "plan_token")
            with self.assertRaises(v4.ConcurrencyConflict):
                capture.execute_capture(
                    store,
                    self.request(
                        store.watermark,
                        "run-stale-apply",
                        dry_run=False,
                        expected_fingerprint=dry_fingerprint,
                        plan_token=dry_plan_token,
                    ),
                )


class ProjectionIntegrationTests(TemporaryIntegrationCase):
    def test_review_safe_withholds_structural_diagnostic_paths(self) -> None:
        """This fails if a diagnostic path field reaches review-safe output."""

        raw = {
            "store": {
                "path": "opaque-local-diagnostic-location",
                "filesystem_type": "syntheticfs",
            },
            "business": {
                "summary": "Keep the launch/trial handoff with the customer.",
                "url": "https://example.invalid/launch/trial",
            },
        }

        cleaned = integration.sanitize_review_safe_value(raw)

        self.assertIsInstance(cleaned, dict)
        assert isinstance(cleaned, dict)
        self.assertEqual("[redacted local path]", cleaned["store"].get("path"))
        self.assertEqual(raw["business"], cleaned["business"])

    def test_review_safe_redacts_paths_across_records_and_diagnostics(self) -> None:
        """The public v4 boundary must use the same complete path policy as v3."""
        raw = {
            "store": {
                "path": "/custom/graphs/x.sqlite",
                "reasons": ["parent contains untrusted symlink: /custom/graphs"],
            },
            "results": [
                {
                    "id": "claim_synthetic",
                    "summary": "/Users/example/Library/Application Support/tedd/graph.sqlite",
                }
            ],
            "url": "https://example.test/custom/graphs",
        }
        expected = {
            "store": {
                "path": "[redacted local path]",
                "reasons": ["parent contains untrusted symlink: [redacted local path]"],
            },
            "results": [{"id": "claim_synthetic", "summary": "[redacted local path]"}],
            "url": "https://example.test/custom/graphs",
        }
        self.assertEqual(integration.sanitize_review_safe_value(raw), expected)
        self.assertEqual(integration.sanitize_review_safe_value(expected), expected)

    def test_review_safe_sanitizes_every_nested_json_surface(self) -> None:
        bearer_token = "synthetic_token_value_123456"
        provider_key = "sk" + "-synthetic_key_name_1234"
        raw = {
            "wrapper": {
                "password": "synthetic-password-value",
                "details": [
                    f"Authorization: Bearer {bearer_token}",
                    {"artifact": r"C:\Users\Example\private\fixture.txt"},
                ],
                provider_key: "ordinary",
                "payload_hash": "synthetic-payload-hash",
                "evidence_refs": ["source_private_fixture"],
            }
        }

        cleaned = integration.sanitize_review_safe_value(raw)
        serialized = json.dumps(cleaned, sort_keys=True)

        for private_value in (
            "synthetic-password-value",
            bearer_token,
            r"C:\Users\Example\private\fixture.txt",
            provider_key,
            "synthetic-payload-hash",
            "source_private_fixture",
        ):
            with self.subTest(private_value=private_value):
                self.assertNotIn(private_value, serialized)
        self.assertIn("[redacted secret]", serialized)
        self.assertIn("[redacted local path]", serialized)
        if not isinstance(cleaned, dict) or not isinstance(cleaned.get("wrapper"), dict):
            self.fail("review-safe wrapper must remain an object")
        wrapper = cast(dict[str, object], cleaned["wrapper"])
        self.assertEqual(
            wrapper["payload_hash"],
            "[withheld in review_safe mode]",
        )
        self.assertEqual(
            wrapper["evidence_refs"],
            ["[withheld in review_safe mode]"],
        )

    def test_review_safe_keeps_existing_id_bearing_record_policy(self) -> None:
        record = {
            "id": "event_review_safe_control",
            "title": "Private control title",
            "source_ref": "source_review_safe_control",
            "sensitive": True,
        }

        self.assertEqual(
            integration.sanitize_review_safe_value(record),
            v3.recall_record(record, "review_safe"),
        )

    def test_v4_projection_manifest_distinguishes_canonical_and_payload_schema(self) -> None:
        graph = make_v3_graph(extra_entities=1)
        source = (json.dumps(graph, sort_keys=True) + "\n").encode("utf-8")
        store_path = self.root / "graph.sqlite3"
        store = v4.import_v3_graph(
            graph,
            store_path,
            hashlib.sha256(source).hexdigest(),
            producer_agent_id="migration-agent",
            task_id="projection-schema-test",
            plan_hmac_key=PLAN_KEY,
        )
        store.close()
        vault = self.root / "vault"
        vault.mkdir()
        set_private_path_mode(vault, 0o700)
        result = integration.project_v4(
            store_path,
            vault=vault,
            root_name="Codex Knowledge",
            mode="review_safe",
            manifest_key="7" * 64,
            root_id="projection-schema-test",
            timeout_seconds=5.0,
        )
        manifest = json.loads((vault / "Codex Knowledge" / v3.PROJECTION_MANIFEST).read_text())
        self.assertEqual(result["canonical_store_schema_version"], "v4")
        self.assertEqual(result["projection_payload_schema_version"], "v3")
        self.assertEqual(manifest["graph_schema_version"], "v4")
        self.assertEqual(manifest["canonical_store_schema_version"], "v4")
        self.assertEqual(manifest["projection_payload_schema_version"], "v3")

    def test_projection_snapshot_is_bounded_and_reference_closed(self) -> None:
        graph = make_v3_graph(extra_entities=12)
        v3.capture_packet(
            graph,
            {
                "source": {
                    "kind": "test",
                    "role": "fixture",
                    "title": "Reverse-neighbor fixture",
                    "observed_at": "2026-01-02T12:00:00Z",
                    "source_tier": "validated_structured_candidate",
                    "sensitive": False,
                },
                "relations": [
                    {
                        "source": "entity_extra_011",
                        "target": "entity_client_alpha_site",
                        "type": "supports",
                        "status": "current",
                        "sensitive": False,
                    }
                ],
            },
            authority_ceiling="validated_structured_candidate",
            producer={"agent_id": "fixture-agent", "task_id": "fixture-task"},
            based_on_watermark=v3.committed_watermark(graph),
            discovery_kind="verification",
        )
        reverse_relation_id = str(graph["relations"][-1]["id"])
        source = (json.dumps(graph, sort_keys=True) + "\n").encode("utf-8")
        store_path = self.root / "graph.sqlite3"
        store = v4.import_v3_graph(
            graph,
            store_path,
            hashlib.sha256(source).hexdigest(),
            producer_agent_id="migration-agent",
            task_id="projection-test",
            plan_hmac_key=PLAN_KEY,
        )
        prior_limit = integration.MAX_PROJECTION_RECORDS
        prior_roots = integration.MAX_PROJECTION_ROOT_RECORDS
        try:
            integration.MAX_PROJECTION_RECORDS = 5
            integration.MAX_PROJECTION_ROOT_RECORDS = 3
            with store:
                projected, metadata = integration._projection_graph(cast(Any, store))
        finally:
            integration.MAX_PROJECTION_RECORDS = prior_limit
            integration.MAX_PROJECTION_ROOT_RECORDS = prior_roots
        self.assertEqual(metadata["strategy"], "priority_hot_set_with_reference_closure")
        projected_record_count = metadata["projected_record_count"]
        if not isinstance(projected_record_count, int):
            self.fail("projected record count must be an integer")
        self.assertLessEqual(projected_record_count, 5)
        self.assertFalse(metadata["complete"])
        self.assertTrue(v3.validate_graph(projected)["valid"])
        records_by_collection = {
            collection: cast(list[dict[str, Any]], projected.get(collection, []))
            for collection in v3.COLLECTIONS
        }
        selected_ids = {
            item["id"] for records in records_by_collection.values() for item in records
        }
        self.assertIn("entity_client_alpha_site", selected_ids)
        self.assertNotIn(reverse_relation_id, selected_ids)
        for collection in v3.COLLECTIONS:
            for item in records_by_collection[collection]:
                for reference in v4._extract_refs(item, ()):
                    self.assertIn(reference["target_id"], selected_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
