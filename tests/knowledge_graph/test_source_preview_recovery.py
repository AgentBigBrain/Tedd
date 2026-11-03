"""Linux preview recovery on disposable native files; no viewer qualification."""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest import mock

from benchmark_support import private_temporary_parent

if TYPE_CHECKING:
    from tedd_knowledge_graph.source_preview_recovery import PreviewRecovery


@unittest.skipUnless(sys.platform == "linux", "Linux recovery boundary")
class SourcePreviewRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        from tedd_knowledge_graph import source_files
        from tedd_knowledge_graph.source_preview_recovery import PreviewRecovery

        temporary = tempfile.TemporaryDirectory(dir=private_temporary_parent())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = self.root / "vault"
        source_files.directory(self.vault)
        self.path = self.vault / "neutral.md"
        self.expected = b"Not approved. Only after review."
        source_files.write_bytes(self.path, self.expected)
        info = self.path.stat()
        self.identity = (info.st_dev, info.st_ino)
        self.recovery = PreviewRecovery(self.root / "recovery", "a" * 64, "b" * 64)

    def payloads(self) -> list[Path]:
        return sorted(self.recovery.root.glob("*/payload"))

    def mutate(self, replacement: bytes | None = None) -> bool:
        return self.recovery.mutate(self.path, self.identity, self.expected, replacement)

    def test_unchanged_delete_and_update_leave_no_recovery_content(self) -> None:
        self.assertTrue(self.mutate(b"Current approved source."))
        self.assertEqual(self.path.read_bytes(), b"Current approved source.")
        self.assertEqual(self.recovery.report()["recovery_entries"], [])
        info = self.path.stat()
        self.assertTrue(
            self.recovery.mutate(
                self.path, (info.st_dev, info.st_ino), self.path.read_bytes(), None
            )
        )
        self.assertFalse(self.path.exists())
        self.assertEqual(self.recovery.report()["recovery_status"], "clean")

    def test_existing_edit_stays_in_place(self) -> None:
        self.path.write_bytes(b"Owner edit before cleanup.")
        self.assertFalse(self.mutate())
        self.assertEqual(self.path.read_bytes(), b"Owner edit before cleanup.")
        self.assertEqual(self.payloads(), [])

    def shared_export_recovery(self) -> PreviewRecovery:
        self.vault.chmod(0o755)
        self.path.chmod(0o644)
        return replace(self.recovery, export_root=self.vault)

    def test_shared_export_cleanup_does_not_relax_preview_rules(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceArchiveError

        recovery = self.shared_export_recovery()
        with self.assertRaises(SourceArchiveError):
            self.mutate()
        with self.assertRaises(SourceArchiveError):
            recovery.mutate(self.path, self.identity, self.expected, b"No shared rewrite.")
        self.path.write_bytes(b"Edit already present.")
        self.assertFalse(recovery.mutate(self.path, self.identity, self.expected, None))
        self.assertEqual(self.path.read_bytes(), b"Edit already present.")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)
        self.assertEqual(recovery.report()["recovery_status"], "clean")

    def test_shared_raced_edit_is_private_and_content_free_recovery_is_reported(self) -> None:
        recovery = self.shared_export_recovery()
        real_replace = os.replace

        def edit_then_move(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            self.path.write_bytes(b"Owner edit in shared cache.")
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with mock.patch("os.replace", side_effect=edit_then_move):
            self.assertFalse(recovery.mutate(self.path, self.identity, self.expected, None))
        self.assertFalse(self.path.exists())
        payload = self.payloads()[0]
        self.assertEqual(payload.read_bytes(), b"Owner edit in shared cache.")
        self.assertEqual(payload.stat().st_mode & 0o777, 0o600)
        self.assertEqual(payload.parent.stat().st_mode & 0o777, 0o700)
        report = recovery.reconcile()
        self.assertEqual(report["relocated_files"], 1)
        self.assertEqual(report["recovery_status"], "required")
        self.assertNotIn(str(self.path), str(report))
        self.assertNotIn("Owner edit", str(report))

    def test_shared_export_crash_before_hardening_recovers_only_authenticated_bytes(self) -> None:
        recovery = self.shared_export_recovery()
        real_replace = os.replace

        def interrupt_move(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            raise OSError("interrupted before private chmod")

        with mock.patch("os.replace", side_effect=interrupt_move):
            with self.assertRaises(OSError):
                recovery.mutate(self.path, self.identity, self.expected, None)
        payload = self.payloads()[0]
        self.assertEqual(payload.stat().st_mode & 0o777, 0o644)
        self.assertEqual(payload.read_bytes(), self.expected)
        for other in (replace(recovery, key="c" * 64), replace(recovery, binding="d" * 64)):
            self.assertEqual(other.reconcile()["recovery_status"], "required")
            self.assertTrue(payload.exists())
            self.assertEqual(payload.stat().st_mode & 0o777, 0o644)
        self.assertEqual(recovery.reconcile()["recovery_status"], "clean")
        self.assertFalse(payload.exists())

    def test_shared_export_writer_remains_recoverable_until_closed(self) -> None:
        recovery = self.shared_export_recovery()
        with self.path.open("r+b") as writer:
            self.assertFalse(recovery.mutate(self.path, self.identity, self.expected, None))
            writer.write(b"Owner edit")
            writer.flush()
            self.assertEqual(recovery.reconcile()["recovery_status"], "required")
        self.assertEqual(recovery.reconcile()["recovery_status"], "required")
        self.assertTrue(self.payloads()[0].read_bytes().startswith(b"Owner edit"))

    def test_shared_export_cannot_mutate_outside_root_or_overlapping_recovery(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceArchiveError

        recovery = self.shared_export_recovery()
        for other in (
            replace(recovery, export_root=self.root / "other"),
            replace(recovery, root=self.vault / "recovery"),
        ):
            with self.assertRaises(SourceArchiveError):
                other.mutate(self.path, self.identity, self.expected, None)
        self.assertEqual(self.path.read_bytes(), self.expected)

    def test_shared_export_link_swap_neither_follows_nor_changes_linked_original(self) -> None:
        recovery = self.shared_export_recovery()
        real_replace = os.replace
        outside = self.root / "outside"
        outside.write_bytes(b"Original must remain untouched.")
        outside.chmod(0o644)

        def swap_link(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            self.path.unlink()
            os.link(outside, self.path)
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with mock.patch("os.replace", side_effect=swap_link):
            with self.assertRaises(ValueError):
                recovery.mutate(self.path, self.identity, self.expected, None)
        self.assertEqual(recovery.reconcile()["recovery_status"], "required")
        self.assertEqual(outside.stat().st_mode & 0o777, 0o644)
        self.assertEqual(outside.read_bytes(), b"Original must remain untouched.")
        self.assertEqual(self.payloads()[0].read_bytes(), outside.read_bytes())

    def test_shared_export_supports_larger_office_bytes_without_larger_preview_limit(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceArchiveError

        recovery = self.shared_export_recovery()
        payload = b"x" * (9 * 1024**2)
        self.path.write_bytes(payload)
        with self.assertRaises(SourceArchiveError):
            self.recovery.mutate(self.path, self.identity, payload, None)
        self.assertTrue(recovery.mutate(self.path, self.identity, payload, None))
        self.assertFalse(self.path.exists())

    def test_concurrent_edit_is_retained_and_receipt_is_content_free(self) -> None:
        real_replace = os.replace

        def edit_then_move(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            self.path.write_bytes(b"Private edited wording.")
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with mock.patch("os.replace", side_effect=edit_then_move):
            self.assertFalse(self.mutate(b"Replacement must not be published."))
        self.assertFalse(self.path.exists())
        self.assertEqual(self.payloads()[0].read_bytes(), b"Private edited wording.")
        report = self.recovery.reconcile()
        self.assertEqual(report["recovery_status"], "required")
        self.assertEqual(report["recovery_entries"], [self.payloads()[0].parent.name])
        for excluded in ("Private", "neutral.md", str(self.root)):
            self.assertNotIn(excluded, str(report))
        for path in (self.recovery.root, self.payloads()[0].parent):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.payloads()[0].stat().st_mode & 0o777, 0o600)

    def test_open_writer_is_retained_until_closed_and_reverified(self) -> None:
        with self.path.open("r+b") as writer:
            self.assertFalse(self.mutate())
            self.assertFalse(self.path.exists())
            writer.write(b"Owner edit")
            writer.flush()
            self.assertEqual(self.recovery.reconcile()["recovery_status"], "required")
        self.assertEqual(self.recovery.reconcile()["recovery_status"], "required")
        self.assertTrue(self.payloads()[0].read_bytes().startswith(b"Owner edit"))

    def test_new_owner_destination_wins_and_recovery_never_restores_it(self) -> None:
        real_replace = os.replace

        def replace_then_create(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            self.path.write_bytes(b"New owner file.")
            self.path.chmod(0o600)

        with mock.patch("os.replace", side_effect=replace_then_create):
            with self.assertRaises(OSError):
                self.mutate(b"Generated replacement.")
        self.assertEqual(self.path.read_bytes(), b"New owner file.")
        self.recovery.reconcile()
        self.assertEqual(self.path.read_bytes(), b"New owner file.")

    def test_directory_swap_and_cross_device_refuse_without_moving_source(self) -> None:
        real_replace = os.replace

        def swap_directory(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            self.path.unlink()
            self.path.mkdir(mode=0o700)
            (self.path / "owner.txt").write_bytes(b"Do not move this directory.")
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with mock.patch("os.replace", side_effect=swap_directory):
            with self.assertRaises(OSError):
                self.mutate()
        self.assertEqual((self.path / "owner.txt").read_bytes(), b"Do not move this directory.")
        self.assertFalse(any(path.is_dir() for path in self.payloads()))

    def test_cross_device_error_never_falls_back_to_copy_delete(self) -> None:
        with mock.patch("os.replace", side_effect=OSError(errno.EXDEV, "cross device")):
            with self.assertRaises(OSError):
                self.mutate()
        self.assertEqual(self.path.read_bytes(), self.expected)
        self.recovery.reconcile()
        self.assertEqual(self.path.read_bytes(), self.expected)

    def test_symlink_swap_is_retained_without_following_target(self) -> None:
        real_replace = os.replace
        outside = self.root / "outside"
        outside.write_bytes(b"Never read or change this target.")

        def swap_link(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            self.path.unlink()
            self.path.symlink_to(outside)
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with mock.patch("os.replace", side_effect=swap_link):
            self.assertFalse(self.mutate())
        self.assertTrue(self.payloads()[0].is_symlink())
        self.assertEqual(self.recovery.reconcile()["recovery_status"], "required")
        self.assertEqual(outside.read_bytes(), b"Never read or change this target.")

    def test_interrupted_move_is_discoverable_and_recovery_only_disposes_unchanged(self) -> None:
        real_replace = os.replace

        def interrupt_after_move(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            raise OSError("injected post-move failure")

        with mock.patch("os.replace", side_effect=interrupt_after_move):
            with self.assertRaises(OSError):
                self.mutate(b"Never publish during recovery.")
        self.assertEqual(self.payloads()[0].read_bytes(), self.expected)
        self.assertEqual(self.recovery.report()["recovery_status"], "required")
        self.assertEqual(self.recovery.reconcile()["recovery_status"], "clean")
        self.assertFalse(self.path.exists())

    def test_corrupt_or_differently_bound_intents_are_preserved(self) -> None:
        from tedd_knowledge_graph.source_preview_recovery import PreviewRecovery

        real_replace = os.replace

        def interrupt_after_move(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            raise OSError("interrupted")

        with mock.patch("os.replace", side_effect=interrupt_after_move):
            with self.assertRaises(OSError):
                self.mutate()
        for key, binding in (("c" * 64, "b" * 64), ("a" * 64, "d" * 64)):
            other = PreviewRecovery(self.recovery.root, key, binding)
            self.assertEqual(other.reconcile()["recovery_status"], "required")
        intent = self.payloads()[0].parent / "intent.json"
        intent.write_bytes(b"{}")
        self.assertEqual(self.recovery.reconcile()["recovery_status"], "required")
        self.assertEqual(self.payloads()[0].read_bytes(), self.expected)

    def test_pending_recovery_blocks_further_mutation_even_after_vault_file_is_gone(self) -> None:
        real_replace = os.replace

        def edit_then_move(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            self.path.write_bytes(b"Owner retained edit.")
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with mock.patch("os.replace", side_effect=edit_then_move):
            self.assertFalse(self.mutate())
        self.path.write_bytes(self.expected)
        self.path.chmod(0o600)
        info = self.path.stat()
        self.assertFalse(
            self.recovery.mutate(self.path, (info.st_dev, info.st_ino), self.expected, None)
        )
        self.assertEqual(self.path.read_bytes(), self.expected)
        self.assertEqual(len(self.payloads()), 1)

    def test_crash_after_intent_removal_recovers_only_empty_operation_directory(self) -> None:
        real_unlink = Path.unlink

        def crash_after_intent(path: Path, missing_ok: bool = False) -> None:
            real_unlink(path, missing_ok=missing_ok)
            if path.name == "intent.json":
                raise OSError("interrupted after intent removal")

        with mock.patch.object(Path, "unlink", autospec=True, side_effect=crash_after_intent):
            with self.assertRaises(OSError):
                self.mutate()
        self.assertEqual(self.recovery.report()["recovery_status"], "required")
        self.assertEqual(self.recovery.reconcile()["recovery_status"], "clean")
        self.assertFalse(self.path.exists())

    def test_recovery_directory_sync_error_cannot_report_clean(self) -> None:
        from tedd_knowledge_graph import source_files

        self.assertTrue(self.mutate())
        with mock.patch.object(source_files, "fsync_directory", side_effect=OSError("sync failed")):
            self.assertEqual(self.recovery.reconcile()["recovery_status"], "required")
        self.assertEqual(self.recovery.reconcile()["recovery_status"], "clean")

    def test_replaced_parent_never_receives_generated_replacement(self) -> None:
        real_replace = os.replace
        moved = self.root / "moved-vault"

        def move_then_swap_parent(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
            self.vault.rename(moved)
            self.vault.mkdir(mode=0o700)

        with mock.patch("os.replace", side_effect=move_then_swap_parent):
            with self.assertRaises((OSError, ValueError)):
                self.mutate(b"Must not enter a substitute directory.")
        self.assertEqual(list(self.vault.iterdir()), [])
        self.assertEqual(self.recovery.report()["recovery_status"], "required")
        self.recovery.reconcile()
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_first_recovery_root_is_durable_before_public_file_moves(self) -> None:
        from tedd_knowledge_graph import source_files

        real_sync = source_files.fsync_directory

        def deny_root_parent(path: Path) -> None:
            if path == self.recovery.root.parent:
                raise OSError("injected root-parent sync failure")
            real_sync(path)

        with mock.patch.object(source_files, "fsync_directory", side_effect=deny_root_parent):
            with self.assertRaises(OSError):
                self.mutate()
        self.assertEqual(self.path.read_bytes(), self.expected)

    def test_real_lease_break_retains_bytes_without_changing_process_signal_handler(self) -> None:
        import importlib
        import signal

        from tedd_knowledge_graph.source_preview_recovery import _Leases, _ThreadSignals

        leases = importlib.import_module("fcntl")
        real_fcntl = cast(_Leases, leases).fcntl
        getlease = cast(_Leases, leases).F_GETLEASE
        sigio = cast(_ThreadSignals, signal).SIGIO
        before = signal.getsignal(sigio)
        nonblock_name = "O_NONBLOCK"
        nonblock = int(getattr(os, nonblock_name))

        def break_lease(fd: int, operation: int, argument: int | bytes = 0) -> int | bytes:
            if operation == getlease:
                with self.assertRaises(BlockingIOError):
                    writer = os.open(self.payloads()[0], os.O_WRONLY | nonblock)
                    os.close(writer)
            return real_fcntl(fd, operation, argument)

        with mock.patch.object(leases, "fcntl", side_effect=break_lease):
            self.assertFalse(self.mutate())
        self.assertEqual(signal.getsignal(sigio), before)
        self.assertEqual(self.payloads()[0].read_bytes(), self.expected)
        self.assertEqual(self.recovery.reconcile()["recovery_status"], "clean")

    def test_post_move_inventory_error_still_reports_known_relocation(self) -> None:
        real_replace = os.replace

        def edit_then_move(src: str, dst: str, *, src_dir_fd: int, dst_dir_fd: int) -> None:
            self.path.write_bytes(b"Retain this concurrent edit.")
            real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        with mock.patch("os.replace", side_effect=edit_then_move):
            self.assertFalse(self.mutate())
        operation = self.payloads()[0].parent.name
        with mock.patch("os.scandir", side_effect=OSError("inventory unavailable")):
            report = self.recovery.report()
        self.assertTrue(report["recovery_unreadable"])
        self.assertEqual(report["relocated_files"], 1)
        self.assertEqual(report["recovery_entries"], [operation])

    def test_interrupted_no_clobber_link_retains_owner_writable_copy(self) -> None:
        from tedd_knowledge_graph.source_preview_recovery import _PinnedPublication

        with mock.patch.object(
            _PinnedPublication, "unlink", side_effect=OSError("interrupted link")
        ):
            with self.assertRaises(OSError):
                self.mutate(b"Generated replacement.")
        self.path.write_bytes(b"Owner edited the published replacement.")
        self.assertEqual(self.recovery.reconcile()["recovery_status"], "required")
        self.assertEqual(self.path.read_bytes(), b"Owner edited the published replacement.")
        copies = list(self.recovery.root.glob("*/replacement"))
        self.assertEqual(copies[0].read_bytes(), self.path.read_bytes())

    def test_unsafe_root_and_over_limit_input_refuse_before_relocation(self) -> None:
        from tedd_knowledge_graph.source_contracts import SourceArchiveError
        from tedd_knowledge_graph.source_preview_recovery import PreviewRecovery

        self.recovery.root.symlink_to(self.vault, target_is_directory=True)
        self.assertFalse(self.mutate())
        self.assertEqual(self.path.read_bytes(), self.expected)
        self.recovery.root.unlink()
        with mock.patch("tedd_knowledge_graph.source_preview_recovery.MAX_PREVIEW_FILE_BYTES", 2):
            with self.assertRaises(SourceArchiveError):
                self.mutate()
        self.recovery.root.mkdir(mode=0o700)
        for number in range(3):
            (self.recovery.root / f"{number:032x}").mkdir(mode=0o700)
        with mock.patch("tedd_knowledge_graph.source_preview_recovery.MAX_PREVIEW_FILES", 2):
            self.assertFalse(self.mutate())
        self.assertEqual(self.path.read_bytes(), self.expected)
        self.assertEqual(len(list(self.recovery.root.iterdir())), 3)
        other = PreviewRecovery(self.root / "unrelated-recovery", "c" * 64, "d" * 64)
        self.assertEqual(other.report()["recovery_entries"], [])


if __name__ == "__main__":
    unittest.main()
