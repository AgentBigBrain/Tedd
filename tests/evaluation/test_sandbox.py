"""Integration tests for disposable graph isolation and CLI containment."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from memory_eval_lab import parser
from tedd_memory_eval.contracts import BUDGETS, PROTOCOL_ID, Scenario
from tedd_memory_eval.providers import ContextRecencyProvider, NoMemoryProvider
from tedd_memory_eval.sandbox import (
    MemoryEvalSandbox,
    _assert_owned_plain_path,
    _physical_temporary_root,
)


class SandboxTests(unittest.TestCase):
    def test_default_catalog_remains_the_frozen_v1_catalog(self) -> None:
        self.assertEqual(len(MemoryEvalSandbox(timeout_seconds=30.0).catalog), 12)

    def test_public_parser_exposes_no_caller_selected_state_path(self) -> None:
        option_strings = {
            option for action in parser()._actions for option in action.option_strings
        }

        self.assertTrue({"--pretty", "--timeout-seconds"} <= option_strings)
        self.assertFalse({"--config", "--store", "--suite", "--output", "--keep"} & option_strings)

    def test_internal_client_rejects_write_commands_and_config_override(self) -> None:
        sandbox = MemoryEvalSandbox(timeout_seconds=30.0)

        with self.assertRaises(RuntimeError):
            sandbox.call(("configure", "--store", "outside.sqlite3"))
        with self.assertRaises(RuntimeError):
            sandbox.call(
                (
                    "get",
                    "--id",
                    "lab_record",
                    "--mode",
                    "review_safe",
                    "--config",
                    "outside.json",
                )
            )
        with self.assertRaises(RuntimeError):
            sandbox.call(("get", "--id", "production_record", "--mode", "review_safe"))

    def test_timeout_must_be_finite_and_bounded(self) -> None:
        for value in (float("nan"), float("inf"), 0.0, 301.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                MemoryEvalSandbox(timeout_seconds=value)

    def test_custom_catalog_and_protocol_require_bounded_typed_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "catalog must contain typed"):
            MemoryEvalSandbox(catalog=())
        with self.assertRaisesRegex(ValueError, "catalog must contain typed"):
            MemoryEvalSandbox(catalog=(object(),))  # type: ignore[arg-type]
        for protocol_id in ("", " outer", "x" * 257):
            with (
                self.subTest(protocol_id=protocol_id),
                self.assertRaisesRegex(ValueError, "protocol_id"),
            ):
                MemoryEvalSandbox(protocol_id=protocol_id)

    def test_custom_expected_empty_catalog_runs_at_the_real_boundary(self) -> None:
        scenario = Scenario(
            case_id="custom_empty",
            family="abstention",
            query="Is any authorized synthetic memory available?",
            subject_id="lab_subject_custom_empty",
            records=(),
            required_ids=(),
            relevant_ids=(),
            oracle_ids=(),
            required_assertions=(),
        )
        root: Path | None = None

        with MemoryEvalSandbox(catalog=(scenario,), protocol_id=PROTOCOL_ID) as sandbox:
            root = sandbox.root
            baseline = ContextRecencyProvider().retrieve(sandbox, scenario, BUDGETS[0])
            empty = NoMemoryProvider().retrieve(sandbox, scenario, BUDGETS[0])
            self.assertEqual(baseline.record_ids, (scenario.subject_id,))
            self.assertEqual(empty.record_ids, ())
            self.assertEqual(empty.evaluation_tokens, 0)
            self.assertEqual(sandbox.initial_state, sandbox.final_state())

        self.assertIsNotNone(root)
        self.assertFalse(root.exists() if root is not None else True)

    def test_physical_root_accepts_a_darwin_style_parent_alias(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-root-test-") as temporary:
            base = Path(temporary)
            physical_parent = base / "private" / "var"
            physical_parent.mkdir(parents=True)
            alias_parent = base / "var"
            try:
                os.symlink(physical_parent, alias_parent, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            lexical_root = alias_parent / "folders" / "allocated"
            lexical_root.mkdir(parents=True)

            bound = _physical_temporary_root(lexical_root)

            self.assertEqual(bound, lexical_root.resolve(strict=True))
            self.assertFalse(bound.is_symlink())

    def test_physical_root_and_owned_paths_reject_controlled_links(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-root-test-") as temporary:
            base = Path(temporary)
            physical_root = base / "owned"
            physical_root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            linked_leaf = base / "linked-root"
            linked_child = physical_root / "linked-child"
            try:
                os.symlink(physical_root, linked_leaf, target_is_directory=True)
                os.symlink(outside, linked_child, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")

            with self.assertRaisesRegex(RuntimeError, "cannot be a link"):
                _physical_temporary_root(linked_leaf)
            with self.assertRaisesRegex(RuntimeError, "cannot contain links"):
                _assert_owned_plain_path(physical_root, linked_child / "artifact.json")

    def test_physical_root_rejects_reparse_metadata_and_identity_substitution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tedd-root-test-") as temporary:
            raw_root = Path(temporary)
            real = raw_root.lstat()
            reparse = SimpleNamespace(
                st_mode=real.st_mode,
                st_dev=real.st_dev,
                st_ino=real.st_ino,
                st_file_attributes=0x400,
            )
            changed = SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_dev=real.st_dev,
                st_ino=real.st_ino + 1,
                st_file_attributes=0,
            )

            with (
                mock.patch.object(Path, "lstat", return_value=reparse),
                self.assertRaisesRegex(RuntimeError, "reparse point"),
            ):
                _physical_temporary_root(raw_root)
            with (
                mock.patch.object(Path, "lstat", side_effect=(real, real, changed)),
                self.assertRaisesRegex(RuntimeError, "changed during"),
            ):
                _physical_temporary_root(raw_root)

    def test_failed_entry_removes_the_allocated_root(self) -> None:
        captured: list[Path] = []

        def fail_configure(sandbox: MemoryEvalSandbox) -> None:
            if sandbox.root is not None:
                captured.append(sandbox.root)
            raise RuntimeError("injected setup failure")

        sandbox = MemoryEvalSandbox(timeout_seconds=30.0)
        with mock.patch.object(MemoryEvalSandbox, "_configure", fail_configure):
            with self.assertRaisesRegex(RuntimeError, "injected setup failure"):
                sandbox.__enter__()

        self.assertEqual(len(captured), 1)
        self.assertFalse(captured[0].exists())
        self.assertIsNone(sandbox.root)

    def test_sandbox_is_disposable_and_retrieval_does_not_mutate_state(self) -> None:
        root: Path | None = None
        with MemoryEvalSandbox(timeout_seconds=30.0) as sandbox:
            root = sandbox.root
            self.assertIsNotNone(root)
            self.assertTrue(root.is_dir() if root is not None else False)
            packet = ContextRecencyProvider().retrieve(sandbox, sandbox.catalog[0], BUDGETS[0])
            self.assertTrue(packet.record_ids)
            self.assertEqual(sandbox.initial_state, sandbox.final_state())

            with mock.patch(
                "tedd_memory_eval.sandbox.subprocess.run",
                side_effect=subprocess.TimeoutExpired(("synthetic",), 0.1),
            ):
                with self.assertRaisesRegex(RuntimeError, "exceeded its time limit") as raised:
                    sandbox.call(("get", "--id", "lab_record", "--mode", "review_safe"))
            self.assertNotIn(str(sandbox.root), str(raised.exception))

        self.assertIsNotNone(root)
        self.assertFalse(root.exists() if root is not None else True)


if __name__ == "__main__":
    unittest.main()
